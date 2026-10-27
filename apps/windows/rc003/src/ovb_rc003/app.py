"""Entry point wiring: connects the pieces into a running RC003 client.

Windows-only end-to-end (BLE via winrt, HID via Raw Input, key injection via
SendInput). NOT exercised against real hardware in this candidate - no
device pairing/control happens anywhere in this repository or its tests, per
the project's hard boundary. See this package's top-level README.md "Known
gaps" section for what remains 待核验 (to be verified) on a real Windows
machine with a paired RC003.

Reconnect/cleanup contract (fixed after XRBM-014 review RETRY P1 #2 - see
XRBM-014's independent review): ``RC003App`` no longer connects once
and waits forever. ``connection_supervisor.ConnectionSupervisor`` drives a
connect/wait/cleanup/retry loop; a BLE disconnect notification or a protocol
error both call ``request_reconnect()``, which ends the current wait and
guarantees ``_cleanup_once()`` runs before the next connect attempt.
``_cleanup_once()`` releases the voice hotkey and closes the BLE/audio attempt.
Raw Input, the verified HID tap, and the marked voice-key
physicalizer instead belong to the bridge-worker lifetime: BLE discovery or
reconnect failure cannot turn ordinary button handling off. Final worker exit
stops those three input owners once, after BLE/voice cleanup has run.

Voice fail-closed ordering (P1 #3): the output endpoint is resolved and
opened BEFORE any hotkey/MIC_OPEN is sent, not lazily after the device has
already started streaming. If the endpoint is missing or fails to open,
neither the hotkey nor MIC_OPEN are sent at all - voice fails fully closed
while ordinary buttons keep working.

Further fail-closed ordering (XRBM-018, fixing XRBM-014 review round 2 P1
#6): the host hotkey is now sent BEFORE MIC_OPEN, and if it fails to fully
deliver, MIC_OPEN is never sent at all - a device streaming into Windows
without ever having actually tapped/held the configured hotkey is exactly
the "voice opened after host-trigger failure" defect the round-2 review
found. A playback write failure now also fails closed (closes and discards
the sink during reconnect cleanup) and requests a reconnect, instead of
logging indefinitely while the device keeps streaming into nothing. Blocking
writes run on a bounded FIFO worker, so ordinary BLE control handling does not
wait for every PortAudio write.

Cleanup ownership (XRBM-019 P1 #2, fixing XRBM-018 round 2 finding #2):
an owner reference is cleared only after that resource confirms it stopped.
BLE/audio cleanup failures end the reconnect supervisor rather than starting
a new session over a live owner. Final input shutdown follows the same rule
for Raw Input, HID tap, and the voice-key physicalizer.
"""

from __future__ import annotations

import array
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
import uuid
from typing import List, Optional, Tuple

from . import __version__
from . import (
    audio_output,
    audio_playback,
    audio_playback_worker,
    voice_audio_session,
    voice_shortcut_session,
    action_executor,
    app_profiles,
    ble_transport_winrt,
    bridge_launcher,
    bridge_runtime_status,
    bridge_tray_windows,
    button_combo,
    button_gesture,
    config,
    connection_supervisor,
    diagnostic_trace,
    element_navigation_control_windows,
    element_navigation_runtime,
    frida_compat,
    hid_identity,
    hotkey,
    injection_backend,
    input_method_watch_windows,
    key_detection_bridge,
    key_mapping,
    local_microphone_windows,
    logging_setup,
    preset_switch_windows,
    raw_input_windows,
    remote_selection,
    voice_key_physicalizer_windows,
    voice_controller,
    voice_gesture,
    voice_interaction_diagnostics_windows,
    voice_mode_toast_windows,
    voice_playback_session_windows,
    vokie_bridge,
    win32_input,
    win32_keys,
)
from .atvv_session import AudioStarted, AudioStopped, CapsReceived, MicButtonPressed, PcmStats


from .voice_shortcut_session import _VOICE_HOTKEY_BACKEND_MARKED


class CleanupIncompleteError(RuntimeError):
    """Raised when an owned BLE/audio or process-input resource stays live."""


_BUTTON_ACTION_MOUSE_BUTTONS = {
    key_mapping.ActionKind.MOUSE_LEFT_CLICK: "left",
    key_mapping.ActionKind.MOUSE_RIGHT_CLICK: "right",
    key_mapping.ActionKind.MOUSE_MIDDLE_CLICK: "middle",
    key_mapping.ActionKind.MOUSE_X1_CLICK: "x1",
    key_mapping.ActionKind.MOUSE_X2_CLICK: "x2",
}

# Injected keys never trigger Windows' own keyboard auto-repeat, so a held
# remote button is re-sent by the app at the machine's real keyboard repeat
# rate (see ``win32_input.keyboard_repeat_interval``).

# Semantic tap mappings only. RC003 physical-edge correlation is separate.
_BUTTON_ACTION_NAVIGATION_VKS = {
    key_mapping.ActionKind.ARROW_UP: 0x26,
    key_mapping.ActionKind.ARROW_DOWN: 0x28,
    key_mapping.ActionKind.ARROW_LEFT: 0x25,
    key_mapping.ActionKind.ARROW_RIGHT: 0x27,
    key_mapping.ActionKind.RETURN: 0x0D,
    key_mapping.ActionKind.ESCAPE: 0x1B,
    key_mapping.ActionKind.CONTEXT_MENU: 0x5D,
    key_mapping.ActionKind.SYSTEM_VOLUME_UP: 0xAF,
    key_mapping.ActionKind.SYSTEM_VOLUME_DOWN: 0xAE,
}

# The remote has no dedicated parent/child actions. A user-mapped, unmodified
# PageUp/PageDown tap keeps those navigation steps available without claiming
# the computer keyboard or changing multi-key shortcut semantics.
_BUTTON_NAVIGATION_SINGLE_KEYS = {
    ("pageup",): 0x21,
    ("page_up",): 0x21,
    ("pagedown",): 0x22,
    ("page_down",): 0x22,
}

# Direction, backspace and volume keys keep the original "连击" behaviour under
# SendInput: a hold replays the whole click (down+up) at the machine's keyboard
# repeat rate, because SendInput cannot emulate the sustained key press a real
# Windows hold produces.  The Interception driver does emulate that hold, so the
# same buttons stay sustained there (see ``_begin_button_key_hold``).
_SENDINPUT_CLICK_REPEAT_KINDS = frozenset(
    {
        key_mapping.ActionKind.ARROW_UP,
        key_mapping.ActionKind.ARROW_DOWN,
        key_mapping.ActionKind.ARROW_LEFT,
        key_mapping.ActionKind.ARROW_RIGHT,
        key_mapping.ActionKind.DELETE_BACKWARD,
        key_mapping.ActionKind.SYSTEM_VOLUME_UP,
        key_mapping.ActionKind.SYSTEM_VOLUME_DOWN,
        key_mapping.ActionKind.SYSTEM_VOLUME_MUTE,
    }
)

_RAW_FALLBACK_KEY_TOKENS = {
    "mic": "f5",
    "right": "right",
    "left": "left",
    "down": "down",
    "up": "up",
    "ok": "enter",
    "home": "home",
    "menu": "apps",
    "tv": "backtick",
    "power": "vk_5f",
    "volume_mute": "volume_mute",
    "volume_up": "volume_up",
    "volume_down": "volume_down",
    "back": "browser_back",
}

_KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS = 1.0
_KEY_DETECTION_MIC_MAX_SECONDS = 10.0
_ORDINARY_MIC_RELEASE_GUARD_SECONDS = 0.12
_ORDINARY_MIC_SOURCE_STALE_SECONDS = 2.0
_ORDINARY_MIC_MAX_SECONDS = 10.0
_KEY_DETECTION_SUPPRESSION_MAX_SECONDS = 2.0
_RUNTIME_STATUS_HEARTBEAT_SECONDS = 5.0
_PRESET_SWITCH_POLL_SECONDS = 1.0
_VOICE_HOLD_SAFETY_SECONDS = 120.0
# 语音键的短按阈值：按下不到这么久就是短按（只切模式），越过才是长按（语音）。
# 这个值同时也是"延迟发送"的等待窗口，所以它直接决定按下后语音多久才开。
# 0.35 太肉；再往下压就会开始把正常的短按误判成长按。实测下探到 0.15
# 换取更快的开口手感。
_VOICE_TAP_MAX_SECONDS = 0.15
# 短按开关（界面上叫"延迟发送"）打开时：按下瞬间一个键都不发，先等满这个
# 短按阈值。窗口内松手只切模式，宿主输入法收不到任何按键，所以短按绝不会
# 变成一次语音输入。只有一直按住、越过这个阈值才是长按，这时才下发那一记
# KEY_DOWN，松手再 KEY_UP。窗口与短按判定共用同一个阈值，两边不会打架。
_VOICE_SHORTCUT_PRESS_DELAY_SECONDS = _VOICE_TAP_MAX_SECONDS
# 插件语音只按"按住多久"分岔，不看音量：按下就开遥控器话筒并把 PCM 缓进
# 一个约 0.8s 的环形缓冲，按满 ``_PLUGIN_VOICE_HOLD_SECONDS`` 才开插件会话
# 并把缓冲一起补发（插件在 session_accepted 之前会自己暂存），所以开口的
# 第一个字不会丢；不到阈值就松手等于短按，只切模式，Vokie 完全不被惊动。
# 用能量判断"有没有说话"会把开话筒的爆音和按键噪声当成人声，连按切模式
# 每次都误触发，所以这里只认时间。
_PLUGIN_VOICE_PREROLL_SECONDS = 0.8
_PLUGIN_VOICE_HOLD_SECONDS = 0.25
_VOICE_KEY_PHYSICALIZER_RETRY_SECONDS = 1.0
_VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS = 10.0
_VOICE_KEY_PHYSICALIZER_FAILED_RETRY_SECONDS = 30.0
_RAW_INPUT_RETRY_INITIAL_SECONDS = 1.0
_RAW_INPUT_RETRY_MAX_SECONDS = 30.0
_BUTTON_INPUT_RELEASE_RETRY_INITIAL_SECONDS = 0.1
_BUTTON_INPUT_RELEASE_RETRY_MAX_SECONDS = 2.0
_DIRECTION_BUTTON_IDS = frozenset({"up", "down", "left", "right"})


def _runtime_voice_hotkey_text(config_data: dict) -> str:
    """Configured hold-to-talk shortcut, or "" when the user has not recorded one.

    RC003 no longer fabricates a wake shortcut: an empty value means the voice
    buttons stay inert until the user records 语音一/语音二 in settings.
    """

    return str(config_data.get("voice_hotkey", "")).strip()


def open_configured_application(action: key_mapping.ButtonAction) -> bool:
    """Application-action seam kept at the app boundary for testability."""

    return action_executor.open_configured_application(action)


class RC003App:
    def __init__(self) -> None:
        self._config_root = config.config_root()
        self._config_path = config.config_path(self._config_root)
        self._config = config.load_config(self._config_path)
        from . import remote_selection
        self._selected_remote_key = remote_selection.active_key(self._config)
        selected_profile = remote_selection.active_profile(self._config)
        self._remote_profile = selected_profile
        if selected_profile and not remote_selection.runtime_ready(selected_profile):
            raise remote_selection.SelectionError("当前型号尚未提供接收，请在设备页重新选择。")
        self._config_mtime_ns = self._settings_file_mtime_ns(self._config_path)
        self._bindings_path = config.key_bindings_path(self._config_root)
        self._bindings = config.load_key_bindings(
            self._bindings_path
        )
        self._removed_voice_bindings = config.normalize_voice_product_boundary(
            self._config,
            self._bindings,
        )
        self._bindings_mtime_ns = self._settings_file_mtime_ns(self._bindings_path)
        # Saved application profiles own their own file. This process switches
        # the live mapping for two reasons only: a saved profile's switch hotkey
        # (persistent, mirroring the settings window) and foreground-process
        # scope (an overlay that is cleared on the next settings reload, when
        # input becomes idle, or when the process stops owning the foreground).
        # Process scope never rewrites key_bindings.json: the resolved overlay
        # only lives in memory, while the active profile is recorded in
        # app_profiles.json so the settings window can highlight it.
        self._preset_store = app_profiles.AppProfileStore(self._config_root)
        self._presets_mtime_ns = self._settings_file_mtime_ns(self._preset_store.path)
        self._preset_override_id: Optional[str] = None
        # The untouched mapping an active overlay was resolved from, so moving
        # straight from one application to another re-resolves from the global
        # layout instead of stacking two overlays on top of each other.
        self._preset_base_bindings: Optional[dict] = None
        # The marker last written for the settings window, compared in memory so
        # a steady-state poll never touches the disk.
        try:
            self._preset_applied_id: Optional[str] = self._preset_store.applied_id()
        except Exception:  # noqa: BLE001 - an unreadable library displays as empty
            self._preset_applied_id = None
        self._preset_manual_foreground: Optional[str] = None
        self._preset_switch_foreground = ""
        self._preset_switch_task: Optional[asyncio.Task] = None
        self._preset_hotkey_listener: Optional[
            preset_switch_windows.PresetSwitchListener
        ] = None
        # Read once: the machine's keyboard auto-repeat rate drives how fast a
        # held remote button is re-sent, since injected keys never repeat.
        self._keyboard_repeat_interval_seconds = (
            win32_input.keyboard_repeat_interval()
        )
        self._button_mapping_lock = threading.RLock()
        self._button_gestures = button_gesture.ButtonGestureDispatcher(
            is_action_configured=self._is_button_action_configured,
            on_trigger=self._on_button_trigger,
            repeat_interval_for=self._button_repeat_interval,
            on_idle=self._apply_pending_settings_if_idle,
            on_diagnostic=self._on_button_gesture_diagnostic,
            is_default_hold=self._is_default_hold_button,
            is_sustained=self._is_mouse_hold_button,
            on_hold_start=self._on_button_hold_start,
            on_hold_repeat=self._on_button_hold_repeat,
            on_hold_end=self._on_button_hold_end,
        )
        self._button_combos = button_combo.ButtonComboRecognizer()
        self._logger: logging.Logger = logging_setup.get_logger(self._config_root)
        self._last_raw_input_lookup_diagnostic = None
        self._diagnostic_trace = diagnostic_trace.DiagnosticTrace(
            self._config_root,
            enabled=bool(self._config.get("diagnostic_trace_enabled", False)),
        )
        win32_input.set_diagnostic_trace(self._diagnostic_trace)
        voice_key_physicalizer_windows.set_diagnostic_trace(self._diagnostic_trace)
        element_navigation_runtime.set_diagnostic_trace(self._diagnostic_trace)
        self._record_device_diagnostic_context()
        self._voice_attempt_id: Optional[str] = None
        self._runtime_identity = bridge_runtime_status.current_runtime_identity(
            __version__
        )
        self._runtime_status_lock = threading.Lock()
        self._runtime_connection_state = (
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
        )
        self._runtime_raw_input_state = "starting"
        self._runtime_hid_tap_state = "starting"
        self._runtime_voice_key_physicalizer_state = "starting"
        self._runtime_last_button_at: Optional[float] = None
        self._runtime_last_button_source = ""
        self._runtime_voice_active = False
        self._runtime_voice_state = bridge_runtime_status.VOICE_RUNTIME_NOT_TESTED
        self._runtime_voice_provider = ""
        self._runtime_voice_updated_at: Optional[float] = None
        self._runtime_battery_level: Optional[int] = None
        self._runtime_last_button_publish_monotonic = 0.0
        self._logger.info(
            "startup: app identity: version=%s runtime=%s package=%s",
            self._runtime_identity.app_version,
            self._runtime_identity.runtime_kind,
            self._runtime_identity.package_name,
        )
        # RC003 never wakes an input method: its voice keys only replay the
        # shortcut the user recorded.
        if self._removed_voice_bindings:
            self._logger.warning(
                "legacy voice mappings disabled until user reselects actions: %s",
                sorted(self._removed_voice_bindings),
            )
        self._voice_shortcut = voice_shortcut_session.VoiceShortcutSession(
            hotkey_text=_runtime_voice_hotkey_text(self._config),
            logger=self._logger, trace=self._diagnostic_trace,
            services=voice_shortcut_session.ShortcutServices(
                configured_backend=lambda: _VOICE_HOTKEY_BACKEND_MARKED,
                resume_tracking=lambda: self._resume_degraded_voice_key_physicalizer_recovery(),
                set_result=lambda *args, **kw: self._set_runtime_voice_result(*args, **kw),
                current_state=self._current_voice_runtime_state,
                request_cleanup=lambda: self._supervisor.request_reconnect(),
                prepare_mute_guard=self._prepare_wetype_playback_mute_guard,
            ),
        )
        self._pending_voice_settings = None
        self._pending_config = None
        self._pending_bindings = None
        self._voice_audio_start_fallback_pending = False
        self._voice_focus_before: Optional[
            voice_interaction_diagnostics_windows.FocusSnapshot
        ] = None
        self._voice_focus_provider = ""
        self._voice_focus_submit_method = ""
        self._voice_text_observation = "unknown"
        self._button_action_lock = threading.RLock()
        self._button_key_release_pending: Optional[Tuple[str, ...]] = None
        self._button_mouse_release_pending: Optional[str] = None
        self._button_mouse_holds: Dict[str, set[object]] = {}
        # Key holds owned by a sustained gesture: owner -> (tokens, backend).
        self._button_key_holds: Dict[object, Tuple[Tuple[str, ...], object]] = {}
        # Sustained holds consumed by element navigation: owner -> action.
        # Element navigation taps itself, so repeats re-run the action instead
        # of injecting a sustained key-down.
        self._button_tap_hold_actions: Dict[object, key_mapping.ButtonAction] = {}
        self._button_hold_release_pending: set[str] = set()
        self._button_input_release_retry_timer: Optional[object] = None
        self._button_input_release_retry_token: Optional[object] = None
        self._button_input_release_retry_delay = (
            _BUTTON_INPUT_RELEASE_RETRY_INITIAL_SECONDS
        )
        self._button_input_release_retry_stopping = False
        self._button_input_release_timer_factory = threading.Timer
        # Raw Input and the ATVV control channel arrive on different worker
        # threads. Serialize the voice state machine so one physical press
        # cannot race into two host shortcut deliveries.
        self._logger.info(
            "startup: voice settings active: hotkey=%s",
            self._voice_shortcut.hotkey.serialize()
            if self._voice_shortcut.hotkey is not None
            else "",
        )
        # One RC003 microphone press is reported independently by HID, the
        # ATVV mic opcode, and sometimes AUDIO_STARTED first. Keep all reports
        # in one gesture so one real press produces one host down and release.
        self._voice_mic_gesture_active = False
        self._voice_mic_gesture_audio_started = False
        self._voice_mic_gesture_audio_stopped = False
        self._voice_mic_gesture_physical_seen = False
        self._voice_mic_gesture_hid_released = False
        self._voice_ignore_audio_start_until_physical_down = False
        self._voice_mic_gesture_direct_hid_seen = False
        self._voice_mic_gesture_sources_down: set[str] = set()
        self._voice_shortcut_physical_sources_down: set[str] = set()
        # The physical microphone follows its actual key-down/key-up edges.
        # Promoted voice buttons retain tap/hold behavior per voice slot.
        self._voice_gesture = voice_gesture.VoiceGestureLatch(
            tap_max_seconds=_VOICE_TAP_MAX_SECONDS
        )
        self._extra_voice_gestures: dict[int, voice_gesture.VoiceGestureLatch] = {}
        # Non-microphone buttons bound to a voice slot replay the slot's host
        # shortcut only; this tracks which slot currently owns that hold.
        self._extra_voice_slot_active: Optional[int] = None
        self._extra_voice_slot_close_pending: Optional[int] = None
        self._voice_hold_watchdog_timer: Optional[object] = None
        self._voice_hold_watchdog_token: Optional[object] = None
        self._voice_hold_watchdog_timer_factory = threading.Timer
        # 延迟发送：按下时只占住控制器（保证所有 controller.active 判断不变），
        # 一个键都不发；等满窗口才真的注入这一次 KEY_DOWN。窗口内松手就直接
        # 作废，宿主什么也收不到。
        self._voice_delayed_press_timer: Optional[object] = None
        self._voice_delayed_press_token: Optional[object] = None
        self._voice_delayed_press_action: Optional[object] = None
        self._voice_delayed_press_slot: Optional[int] = None
        self._voice_delayed_press_timer_factory = threading.Timer
        # 模式2 的本机麦克风采集；只在语音会话开着的时候运行。
        self._local_voice_capture: Optional[object] = None
        # 短按切换模式：当前生效的语音键 id，None 表示还没按过、用第一条。
        self._voice_cycle_slot: Optional[int] = None
        self._voice_mode_toast = voice_mode_toast_windows.VoiceModeToast(
            logger=self._logger
        )
        # 其他输入法没有回调，只能盯着顶层窗口：气泡显示后冒出新窗口就收掉。
        self._voice_mode_toast_watch = input_method_watch_windows.InputMethodWatch(
            logger=self._logger
        )
        # 已经挂过「输入框弹出」通知的插件桥；Vokie 接下会话时才收气泡。
        self._plugin_voice_toast_bridge: Optional[object] = None
        self._ordinary_mic_lock = threading.Lock()
        self._ordinary_mic_sources_down: set[str] = set()
        self._ordinary_mic_late_sources_down: set[str] = set()
        self._ordinary_mic_late_source_deadlines: dict[str, float] = {}
        self._ordinary_mic_sources_seen: set[str] = set()
        self._ordinary_mic_gesture_started_at = 0.0
        self._ordinary_mic_release_guard_until = 0.0
        self._ordinary_mic_gesture_active = False
        self._unsolicited_mic_close_pending = False
        self._voice_audio_stream_active = False
        self._voice_audio_stop_processed = False
        self._voice_pcm_forwarding_enabled = False
        # 插件模式只按一下宿主的快捷键，不经过虚拟声卡，所以快捷键的按下
        # 状态必须由自己记账：万一松开事件丢了，也要能补一条抬起，绝不能让
        # 右 Alt 这类修饰键卡在系统里。
        self._plugin_voice_injected_tokens: Optional[Tuple[str, ...]] = None
        # 插件连接可用时，长按直接把遥控器的 PCM 通过回环桥送进 Vokie，
        # 不再靠模拟热键让 Vokie 去开电脑自带麦克风。
        self._plugin_voice_audio_active = False
        # 当前这条设备音频流是插件会话在用的，停止时要认出来别当成本机流程。
        self._plugin_voice_audio_stream = False
        # 插件模式下短按只切换语音模式，不惊动 Vokie；快捷键要等按住超过
        # 长按阈值才下发，所以按下先挂一个待定项。
        self._plugin_voice_press_slot: Optional[int] = None
        self._plugin_voice_press_timer: Optional[object] = None
        self._plugin_voice_press_token: Optional[object] = None
        # 按下插件键后先开遥控器话筒、把 PCM 缓起来；按住满阈值才真正通知
        # Vokie，所以短按切模式不会把 Vokie 叫起来。
        self._plugin_voice_capture_pending = False
        self._plugin_voice_capture_slot: Optional[int] = None
        self._plugin_voice_capture_frames: List[array.array] = []
        self._plugin_voice_capture_samples = 0
        # 本次按下是否真的开出过插件会话：开出过就不再把它当短按切模式。
        self._plugin_voice_started_this_press = False
        # 本次按下是否已经替插件发过 MIC_OPEN（收尾时据此决定要不要关）。
        self._plugin_voice_mic_opened = False
        self._voice_raw_input_trigger_pending = False
        self._ble_session: Optional[ble_transport_winrt.RC003BleSession] = None
        self._hid_listener: Optional[raw_input_windows.RawInputButtonListener] = None
        self._raw_input_lifecycle_lock = threading.RLock()
        self._raw_input_operation_lock = threading.RLock()
        self._raw_input_generation = 0
        self._raw_input_lost_generation = -1
        self._raw_input_stopping = False
        self._raw_input_retry_timer: Optional[object] = None
        self._raw_input_retry_token: Optional[object] = None
        self._raw_input_retry_delay = _RAW_INPUT_RETRY_INITIAL_SECONDS
        self._raw_input_timer_factory = threading.Timer
        self._raw_windows_key_down_query = (
            raw_input_windows.physical_key_is_down_before_injection
        )
        self._voice_key_physicalizer: Optional[
            voice_key_physicalizer_windows.VoiceKeyPhysicalizer
        ] = None
        self._voice_key_physicalizer_ready = False
        self._voice_key_physicalizer_lifecycle_lock = threading.RLock()
        self._voice_key_physicalizer_operation_lock = threading.Lock()
        self._voice_key_physicalizer_generation = 0
        self._voice_key_physicalizer_lost_generation = -1
        self._voice_key_physicalizer_stopping = False
        self._voice_key_physicalizer_retry_timer: Optional[object] = None
        self._voice_key_physicalizer_retry_token: Optional[object] = None
        self._voice_key_physicalizer_degraded_retry_deadline = 0.0
        self._voice_key_physicalizer_timer_factory = threading.Timer
        self._hid_report_tap: Optional[frida_compat.RC003HidReportTap] = None
        self._direct_hid_usages: set[int] = set()
        self._direct_hid_report_seq = 0
        self._direct_hid_lock = threading.Lock()
        self._input_arbitration_lock = threading.RLock()
        # True only after the injected endpoint confirms it copied and cleared
        # a real RC003 report before Windows could translate that report.
        self._direct_hid_interception_ready = False
        # The helper reports ATTACHED_WAITING_IO after the hook and lease are
        # armed but before the first intercepted report proves end-to-end I/O.
        # Suspend Raw Input mapping during that short handover so one physical
        # hold cannot be split between two owners.
        self._direct_hid_interception_armed = False
        self._direct_hid_handover_waiting_for_neutral = False
        self._raw_fallback_buttons_down: set[str] = set()
        self._raw_fallback_physical_buttons_down: dict[
            str, tuple[str, Optional[str]]
        ] = {}
        self._raw_fallback_release_debts: dict[
            str, tuple[set[str], set[str]]
        ] = {}
        self._raw_fallback_tracking_active = False
        self._raw_fallback_hold_guards: dict[str, tuple[object, object]] = {}
        self._raw_fallback_timer_factory = threading.Timer
        self._input_rearm_blocked_buttons: set[str] = set()
        self._key_detection_suppressed_buttons: set[str] = set()
        self._key_detection_suppression_deadlines: dict[str, float] = {}
        self._key_detection_mic_lock = threading.Lock()
        self._key_detection_mic_gesture_active = False
        self._key_detection_mic_gesture_started_at = 0.0
        self._key_detection_mic_release_deadline: Optional[float] = None
        self._key_detection_mic_audio_started = False
        self._key_detection_mic_sources_down: set[str] = set()
        self._voice_audio = voice_audio_session.VoiceAudioSession(
            config=lambda: self._config,
            logger=self._logger,
            request_cleanup=lambda: self._supervisor.request_reconnect(),
            disable_forwarding=lambda: setattr(self, "_voice_pcm_forwarding_enabled", False),
            on_first_frame=self._on_voice_audio_first_frame,
        )
        self._event_loop = asyncio.get_event_loop()
        # Physical input belongs to the bridge-worker lifetime, not to one BLE
        # connection attempt. BLE callbacks have their own generation gate.
        self._accept_input_events = True
        self._accept_ble_events = False
        self._ble_callback_generation = 0

        self._supervisor = connection_supervisor.ConnectionSupervisor(
            connect=self._connect_once,
            cleanup=self._cleanup_once,
            retry_delay=float(self._config.get("retry_delay", 2.0)),
            max_retry_delay=float(self._config.get("max_retry_delay", 60.0)),
            logger=self._logger,
            loop=self._event_loop,
        )

    def _current_voice_runtime_state(self):
        with self._runtime_status_lock:
            return self._runtime_voice_state

    def _on_voice_audio_first_frame(self):
        with self._runtime_status_lock:
            voice_active = self._runtime_voice_active
        if voice_active:
            self._set_runtime_voice_result(bridge_runtime_status.VOICE_RUNTIME_RECEIVING_AUDIO)

    # -- lifecycle: driven by ConnectionSupervisor -------------------------

    async def run_forever(self) -> None:
        heartbeat = self._event_loop.create_task(
            self._runtime_status_heartbeat()
        )
        try:
            self._start_input_channels()
            self._start_preset_switching()
            await self._supervisor.run_forever()
        finally:
            try:
                await self._stop_preset_switching()
                self._stop_input_channels()
                self._voice_mode_toast_watch.stop()
                self._voice_mode_toast.stop()
            finally:
                element_navigation_runtime.clear_diagnostic_trace(self._diagnostic_trace)
                self._diagnostic_trace.close()
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def stop(self) -> None:
        await self._supervisor.stop()

    def request_connection_retry_now(self) -> None:
        self._logger.info("manual reconnect requested; waking retry backoff")
        self._supervisor.request_retry_now()

    async def _runtime_status_heartbeat(self) -> None:
        while True:
            self._publish_runtime_status()
            await asyncio.sleep(_RUNTIME_STATUS_HEARTBEAT_SECONDS)

    def _publish_runtime_status(
        self,
        state: Optional[bridge_runtime_status.BridgeConnectionState] = None,
    ) -> None:
        with self._runtime_status_lock:
            if state is not None:
                self._runtime_connection_state = state
            if (
                self._runtime_connection_state
                is not bridge_runtime_status.BridgeConnectionState.CONNECTED
            ):
                self._runtime_battery_level = None
            try:
                bridge_runtime_status.publish_status(
                    self._config_root,
                    self._runtime_connection_state,
                    identity=self._runtime_identity,
                    raw_input_state=self._runtime_raw_input_state,
                    hid_tap_state=self._runtime_hid_tap_state,
                    voice_key_physicalizer_state=(
                        self._runtime_voice_key_physicalizer_state
                    ),
                    last_button_at=self._runtime_last_button_at,
                    last_button_source=self._runtime_last_button_source,
                    voice_active=self._runtime_voice_active,
                    voice_runtime_state=self._runtime_voice_state,
                    voice_runtime_provider=self._runtime_voice_provider,
                    voice_runtime_updated_at=self._runtime_voice_updated_at,
                    battery_level=self._runtime_battery_level,
                )
            except (OSError, ValueError):
                self._logger.exception(
                    "bridge runtime status update failed: state=%s",
                    self._runtime_connection_state.value,
                )

    def _set_runtime_input_state(
        self,
        *,
        raw_input_state: Optional[str] = None,
        hid_tap_state: Optional[str] = None,
        voice_key_physicalizer_state: Optional[str] = None,
    ) -> None:
        with self._runtime_status_lock:
            if raw_input_state is not None:
                self._runtime_raw_input_state = str(raw_input_state)
            if hid_tap_state is not None:
                self._runtime_hid_tap_state = str(hid_tap_state)
            if voice_key_physicalizer_state is not None:
                self._runtime_voice_key_physicalizer_state = str(
                    voice_key_physicalizer_state
                )
        self._publish_runtime_status()

    def _set_runtime_voice_active(self, active: bool) -> None:
        active = bool(active)
        with self._runtime_status_lock:
            if active == self._runtime_voice_active:
                return
            self._runtime_voice_active = active
        self._publish_runtime_status()

    def _set_runtime_battery_level(self, level: Optional[int]) -> None:
        resolved = None if level is None else int(level)
        if resolved is not None and not 0 <= resolved <= 100:
            return
        with self._runtime_status_lock:
            if (
                self._runtime_connection_state
                is not bridge_runtime_status.BridgeConnectionState.CONNECTED
            ):
                return
            if resolved == self._runtime_battery_level:
                return
            self._runtime_battery_level = resolved
        self._publish_runtime_status()

    def _set_runtime_voice_result(
        self,
        state: str,
        *,
        provider: Optional[str] = None,
    ) -> None:
        # RC003 no longer selects an input method: the recorded shortcut is the
        # only thing replayed, so there is no provider identity to publish.
        resolved_provider = str(provider) if provider else ""
        with self._runtime_status_lock:
            previous_state = getattr(self, '_runtime_voice_state', 'not_tested')
            previous_provider = getattr(self, '_runtime_voice_provider', '')
            self._runtime_voice_state = str(state)
            self._runtime_voice_provider = str(resolved_provider)
            self._runtime_voice_updated_at = time.time()
        self._publish_runtime_status()
        trace = getattr(self, '_diagnostic_trace', None)
        if ((previous_state, previous_provider) != (str(state), str(resolved_provider))
                and state in {bridge_runtime_status.VOICE_RUNTIME_HOST_START_FAILED,
                              bridge_runtime_status.VOICE_RUNTIME_HOST_STOP_FAILED,
                              bridge_runtime_status.VOICE_RUNTIME_OUTPUT_OPEN_FAILED}):
            self._logger.warning("Voice runtime failed: state=%s provider=%s", state, resolved_provider,
                                 extra={"failure_key": f"voice:{resolved_provider}:{state}"})
        if (state == bridge_runtime_status.VOICE_RUNTIME_HOST_UNCONFIRMED
                and (previous_state, previous_provider) != (str(state), str(resolved_provider))):
            self._logger.info("Voice runtime unconfirmed: state=%s provider=%s; audio received, host capture unknown",
                              state, resolved_provider)
        if trace is not None and (previous_state, previous_provider) != (str(state), str(resolved_provider)):
            try:
                trace.emit('voice_runtime_state', state=str(state), previous_state=previous_state,
                           provider=str(resolved_provider), **trace.current_context())
            except Exception:
                pass  # Diagnostic failures must never change voice state or cleanup.



    def _prepare_wetype_playback_mute_guard(self):
        sink = self._voice_audio.sink
        if sink is None or not getattr(sink, "ready", False):
            return None
        name = getattr(sink, "endpoint_name", "")
        if not isinstance(name, str) or not name:
            return None
        return voice_playback_session_windows.prepare_playback_mute_guard(name)








    def _record_runtime_button(self, event_source: str) -> None:
        now_wall = time.time()
        now_monotonic = time.monotonic()
        with self._runtime_status_lock:
            self._runtime_last_button_at = now_wall
            self._runtime_last_button_source = str(event_source)
            if now_monotonic - self._runtime_last_button_publish_monotonic < 0.5:
                return
            self._runtime_last_button_publish_monotonic = now_monotonic
        self._publish_runtime_status()

    def clear_runtime_status(self) -> None:
        try:
            bridge_runtime_status.clear_status(
                self._config_root,
                pid=os.getpid(),
            )
        except OSError:
            self._logger.exception("bridge runtime status cleanup failed")

    async def _connect_once(self) -> None:
        with self._voice_shortcut.lock:
            self._ble_callback_generation += 1
            callback_generation = self._ble_callback_generation
            self._accept_ble_events = True
        with self._runtime_status_lock:
            self._runtime_battery_level = None
        self._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.CONNECTING
        )
        self._set_runtime_voice_active(False)
        stage = "discover_candidates"
        try:
            self._logger.info("startup: resolving RC003 identity")
            candidates = await ble_transport_winrt.discover_candidates(with_device_keys=True)
            # The selection is fixed for this service lifetime. Reconnection
            # must never probe or select a different physical remote.
            stage = "select_connectable_candidate"
            candidate = await ble_transport_winrt.select_connectable_candidate(
                candidates, selected_key=self._selected_remote_key
            )
            self._logger.info("startup: exactly one RC003 candidate resolved")

            stage = "create_ble_session"
            self._ble_session = ble_transport_winrt.RC003BleSession(
                on_pcm_frame=lambda samples: self._on_pcm_frame(
                    samples,
                    _ble_generation=callback_generation,
                ),
                on_pcm_frame_with_sequence=lambda samples, sequence: self._on_pcm_frame(
                    samples,
                    _ble_generation=callback_generation,
                    _arrival_sequence=sequence,
                ),
                on_control_event=lambda event: self._on_control_event(
                    event,
                    _ble_generation=callback_generation,
                ),
                on_error=lambda exc: self._on_session_error(
                    exc,
                    _ble_generation=callback_generation,
                ),
                on_disconnected=lambda: self._on_disconnected(
                    _ble_generation=callback_generation,
                ),
                on_battery_level=lambda level: self._on_battery_level(
                    level,
                    _ble_generation=callback_generation,
                ),
                gain_db=float(self._config["gain_db"]),
            )
            stage = "connect_gatt"
            await self._ble_session.connect(candidate)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._publish_runtime_status(
                bridge_runtime_status.BridgeConnectionState.RETRY_WAIT
            )
            self._logger.error(
                "startup: RC003 connection failed: stage=%s error_type=%s",
                stage,
                type(exc).__name__,
            )
            raise

        self._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.CONNECTED
        )
        start_battery_monitor = getattr(
            self._ble_session,
            "start_battery_monitor",
            None,
        )
        if callable(start_battery_monitor):
            try:
                start_battery_monitor()
            except Exception as exc:
                self._logger.warning(
                    "optional RC003 battery monitor start failed: error_type=%s",
                    type(exc).__name__,
                )

    def _start_input_channels(self) -> None:
        """Start process-lifetime input resources before any BLE attempt."""

        from . import remote_selection
        if not self._selected_remote_key:
            raise remote_selection.SelectionError("请先在设备页选择要使用的遥控器。")

        with self._voice_shortcut.lock:
            self._accept_input_events = True
        with self._voice_key_physicalizer_lifecycle_lock:
            self._voice_key_physicalizer_stopping = False
        with self._raw_input_lifecycle_lock:
            self._raw_input_stopping = False
        self._start_voice_key_physicalizer()
        self._start_hid_listener()
        self._start_hid_report_tap()

    def _start_voice_key_physicalizer(self, *, recovering: bool = False) -> None:
        with self._voice_key_physicalizer_operation_lock:
            with self._voice_key_physicalizer_lifecycle_lock:
                if (
                    self._voice_key_physicalizer_stopping
                    or not self._accept_input_events
                ):
                    return
                current = self._voice_key_physicalizer
                if (
                    current is not None
                    and current.is_running
                    and bool(getattr(current, "accepts_new_down", True))
                ):
                    return
                if current is not None and current.is_running:
                    self._voice_key_physicalizer_ready = False
                    if not self._voice_key_physicalizer_degraded_retry_deadline:
                        self._voice_key_physicalizer_degraded_retry_deadline = (
                            time.monotonic()
                            + _VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS
                        )
                    self._schedule_voice_key_physicalizer_recovery_locked()
                    return
                # A queued callback belongs to the owner/generation captured
                # when it was scheduled. Retire that work before publishing a
                # replacement so a failed replacement start can own the sole
                # retry slot.
                self._cancel_voice_key_physicalizer_retry_locked()
                self._voice_key_physicalizer = None
                self._voice_key_physicalizer_generation += 1
                generation = self._voice_key_physicalizer_generation
                self._voice_key_physicalizer_lost_generation = -1
                physicalizer = (
                    voice_key_physicalizer_windows.VoiceKeyPhysicalizer()
                )
                self._voice_key_physicalizer = physicalizer
                self._voice_key_physicalizer_ready = False
                state = "recovering" if recovering else "starting"
            self._set_runtime_input_state(
                voice_key_physicalizer_state=state
            )
            set_tracking_lost_callback = getattr(
                physicalizer,
                "set_tracking_lost_callback",
                None,
            )
            if callable(set_tracking_lost_callback):
                set_tracking_lost_callback(
                    lambda physicalizer=physicalizer, generation=generation: (
                        self._on_voice_key_physicalizer_tracking_lost(
                            physicalizer,
                            generation,
                        )
                    )
                )
            set_health_failure_callback = getattr(
                physicalizer,
                "set_health_failure_callback",
                None,
            )
            if callable(set_health_failure_callback):
                set_health_failure_callback(
                    lambda reason, snapshot, physicalizer=physicalizer,
                    generation=generation: (
                        self._on_voice_key_physicalizer_health_failure(
                            physicalizer,
                            generation,
                            reason,
                            snapshot,
                        )
                    )
                )
            try:
                physicalizer.start()
            except (
                voice_key_physicalizer_windows.
                VoiceKeyPhysicalizerUnavailableError
            ) as exc:
                with self._voice_key_physicalizer_lifecycle_lock:
                    if (
                        self._voice_key_physicalizer is physicalizer
                        and self._voice_key_physicalizer_generation == generation
                    ):
                        self._voice_key_physicalizer_ready = False
                        if not physicalizer.is_running:
                            self._voice_key_physicalizer = None
                        self._schedule_voice_key_physicalizer_recovery_locked()
                self._set_runtime_input_state(
                    voice_key_physicalizer_state="failed"
                )
                if physicalizer.is_running:
                    self._logger.exception(
                        "startup: voice key physicalizer failed but is still running; "
                        "owner retained for cleanup"
                    )
                    raise
                self._logger.warning(
                    "startup: voice key physicalizer unavailable; recovery scheduled: %s",
                    exc,
                )
                return

            stop_stale_instance = False
            with self._voice_key_physicalizer_lifecycle_lock:
                current_generation = self._voice_key_physicalizer_generation
                stale = (
                    self._voice_key_physicalizer is not physicalizer
                    or current_generation != generation
                    or self._voice_key_physicalizer_stopping
                )
                lost = self._voice_key_physicalizer_lost_generation == generation
                if stale:
                    stop_stale_instance = physicalizer.is_running
                elif lost or not physicalizer.is_running:
                    self._voice_key_physicalizer_ready = False
                    self._schedule_voice_key_physicalizer_recovery_locked()
                else:
                    self._voice_key_physicalizer_ready = True
                    self._voice_key_physicalizer_degraded_retry_deadline = 0.0
            if stop_stale_instance:
                try:
                    physicalizer.stop()
                except Exception:
                    self._logger.exception(
                        "cleanup: stale voice key physicalizer did not stop"
                    )
                return
            if lost or not physicalizer.is_running:
                self._set_runtime_input_state(
                    voice_key_physicalizer_state="recovering"
                )
                return
            self._set_runtime_input_state(
                voice_key_physicalizer_state="ready"
            )
            self._logger.info("startup: marked voice key physicalizer enabled")

    def _cancel_voice_key_physicalizer_retry_locked(self) -> None:
        timer = self._voice_key_physicalizer_retry_timer
        self._voice_key_physicalizer_retry_timer = None
        self._voice_key_physicalizer_retry_token = None
        if timer is None:
            return
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except BaseException:
                pass

    def _schedule_voice_key_physicalizer_recovery_locked(
        self,
        *,
        delay: float = _VOICE_KEY_PHYSICALIZER_RETRY_SECONDS,
    ) -> None:
        if (
            self._voice_key_physicalizer_stopping
            or not self._accept_input_events
            or self._voice_key_physicalizer_retry_timer is not None
        ):
            return
        token = object()
        expected_physicalizer = self._voice_key_physicalizer
        expected_generation = self._voice_key_physicalizer_generation
        try:
            timer = self._voice_key_physicalizer_timer_factory(
                delay,
                lambda token=token,
                expected_physicalizer=expected_physicalizer,
                expected_generation=expected_generation: (
                    self._recover_voice_key_physicalizer(
                        token,
                        expected_physicalizer,
                        expected_generation,
                    )
                ),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._voice_key_physicalizer_retry_token = token
            self._voice_key_physicalizer_retry_timer = timer
            timer.start()
        except BaseException as exc:
            if self._voice_key_physicalizer_retry_token is token:
                self._voice_key_physicalizer_retry_token = None
                self._voice_key_physicalizer_retry_timer = None
            self._logger.error(
                "voice key physicalizer recovery timer failed: error_type=%s",
                type(exc).__name__,
            )

    def _recover_voice_key_physicalizer(
        self,
        token: object,
        expected_physicalizer: Optional[
            voice_key_physicalizer_windows.VoiceKeyPhysicalizer
        ],
        expected_generation: int,
    ) -> None:
        with self._voice_key_physicalizer_lifecycle_lock:
            if self._voice_key_physicalizer_retry_token is not token:
                return
            self._voice_key_physicalizer_retry_token = None
            self._voice_key_physicalizer_retry_timer = None
            if (
                self._voice_key_physicalizer_stopping
                or not self._accept_input_events
            ):
                return
            if (
                self._voice_key_physicalizer is not expected_physicalizer
                or self._voice_key_physicalizer_generation
                != expected_generation
            ):
                return
            physicalizer = self._voice_key_physicalizer
            generation = self._voice_key_physicalizer_generation
            running = bool(physicalizer is not None and physicalizer.is_running)
            accepts_new_down = bool(
                running and getattr(physicalizer, "accepts_new_down", True)
            )
            if running and accepts_new_down:
                if self._voice_key_physicalizer_lost_generation == generation:
                    self._schedule_voice_key_physicalizer_recovery_locked()
                    return
                self._voice_key_physicalizer_ready = True
                self._voice_key_physicalizer_degraded_retry_deadline = 0.0
                return
        if running:
            with self._voice_shortcut.lock:
                cleanup_blocks_recovery = bool(
                    self._voice_shortcut.pending_tokens is not None
                    or self._voice_shortcut.controller.active
                )
            if cleanup_blocks_recovery:
                with self._voice_key_physicalizer_lifecycle_lock:
                    if (
                        self._voice_key_physicalizer is not physicalizer
                        or self._voice_key_physicalizer_generation != generation
                        or self._voice_key_physicalizer_stopping
                        or not self._accept_input_events
                    ):
                        return
                    if (
                        self._voice_key_physicalizer_degraded_retry_deadline
                        and time.monotonic()
                        < self._voice_key_physicalizer_degraded_retry_deadline
                    ):
                        self._schedule_voice_key_physicalizer_recovery_locked()
                        return
                self._set_runtime_input_state(
                    voice_key_physicalizer_state="failed"
                )
                self._diagnostic_trace.emit(
                    "voice_key_physicalizer_health",
                    status="blocked",
                    reason="owned_cleanup_unresolved",
                    app_generation=int(generation),
                )
                self._logger.error(
                    "voice key physicalizer recovery blocked: owned voice "
                    "shortcut cleanup is still unresolved; retrying in %.1fs",
                    _VOICE_KEY_PHYSICALIZER_FAILED_RETRY_SECONDS,
                )
                self._schedule_voice_key_physicalizer_recovery_locked(
                    delay=_VOICE_KEY_PHYSICALIZER_FAILED_RETRY_SECONDS
                )
                return

            stop_succeeded = False
            with self._voice_key_physicalizer_operation_lock:
                with self._voice_key_physicalizer_lifecycle_lock:
                    if (
                        self._voice_key_physicalizer is not physicalizer
                        or self._voice_key_physicalizer_generation != generation
                        or self._voice_key_physicalizer_stopping
                        or not self._accept_input_events
                    ):
                        return
                try:
                    physicalizer.stop()
                except Exception:
                    self._logger.exception(
                        "voice key physicalizer recovery could not retire the "
                        "degraded owner; replacement not started"
                    )
                else:
                    stop_succeeded = True
                    with self._voice_key_physicalizer_lifecycle_lock:
                        if (
                            self._voice_key_physicalizer is physicalizer
                            and self._voice_key_physicalizer_generation == generation
                        ):
                            self._voice_key_physicalizer = None
                            self._voice_key_physicalizer_ready = False
            if not stop_succeeded:
                self._set_runtime_input_state(
                    voice_key_physicalizer_state="failed"
                )
                self._logger.error(
                    "voice key physicalizer recovery could not retire the "
                    "degraded owner; retrying in %.1fs",
                    _VOICE_KEY_PHYSICALIZER_FAILED_RETRY_SECONDS,
                )
                self._schedule_voice_key_physicalizer_recovery_locked(
                    delay=_VOICE_KEY_PHYSICALIZER_FAILED_RETRY_SECONDS
                )
                return
            self._set_runtime_input_state(
                voice_key_physicalizer_state="recovering"
            )
            self._start_voice_key_physicalizer(recovering=True)
            return

        with self._voice_key_physicalizer_lifecycle_lock:
            if (
                self._voice_key_physicalizer is not physicalizer
                or self._voice_key_physicalizer_generation != generation
                or self._voice_key_physicalizer_stopping
                or not self._accept_input_events
            ):
                return
            self._voice_key_physicalizer = None
        self._set_runtime_input_state(
            voice_key_physicalizer_state="recovering"
        )
        self._start_voice_key_physicalizer(recovering=True)

    def _resume_degraded_voice_key_physicalizer_recovery(self) -> None:
        """Resume one blocked recovery after authoritative cleanup succeeds."""

        with self._voice_key_physicalizer_lifecycle_lock:
            physicalizer = self._voice_key_physicalizer
            if (
                physicalizer is None
                or not physicalizer.is_running
                or bool(getattr(physicalizer, "accepts_new_down", True))
                or self._voice_key_physicalizer_stopping
                or not self._accept_input_events
            ):
                return
            self._voice_key_physicalizer_degraded_retry_deadline = (
                time.monotonic()
                + _VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS
            )
            # Cleanup is now authoritative: drop any slow fallback retry and
            # retry promptly instead of waiting out the failed-state backoff.
            self._cancel_voice_key_physicalizer_retry_locked()
            self._schedule_voice_key_physicalizer_recovery_locked()


    def _cancel_raw_input_retry_locked(self) -> None:
        timer = self._raw_input_retry_timer
        self._raw_input_retry_timer = None
        self._raw_input_retry_token = None
        if timer is None:
            return
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except BaseException:
                pass

    def _schedule_raw_input_recovery_locked(self) -> None:
        if (
            self._raw_input_stopping
            or not self._accept_input_events
            or self._raw_input_retry_timer is not None
        ):
            return
        token = object()
        delay = self._raw_input_retry_delay
        self._raw_input_retry_delay = min(
            _RAW_INPUT_RETRY_MAX_SECONDS,
            max(_RAW_INPUT_RETRY_INITIAL_SECONDS, delay * 2.0),
        )
        try:
            timer = self._raw_input_timer_factory(
                delay,
                lambda token=token: self._recover_raw_input_listener(token),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._raw_input_retry_token = token
            self._raw_input_retry_timer = timer
            timer.start()
        except BaseException as exc:  # noqa: BLE001 - input remains failed closed
            if self._raw_input_retry_token is token:
                self._raw_input_retry_token = None
                self._raw_input_retry_timer = None
            self._logger.error(
                "Raw Input recovery timer failed: error_type=%s",
                type(exc).__name__,
            )

    def _raw_input_callback_is_current(
        self,
        listener: Optional[raw_input_windows.RawInputButtonListener],
        generation: Optional[int],
    ) -> bool:
        if listener is None and generation is None:
            return True
        if listener is None or generation is None:
            return False
        with self._raw_input_lifecycle_lock:
            return (
                not self._raw_input_stopping
                and self._hid_listener is listener
                and self._raw_input_generation == int(generation)
            )

    def _cancel_raw_input_ownership_locked(self, *, reason: str) -> set[str]:
        """Cancel every Raw-owned edge while the input lock is held."""

        tracked_logical_buttons, tracked_physical_buttons = (
            self._raw_fallback_tracked_button_ids_locked()
        )
        raw_fallback_buttons = (
            set(self._raw_fallback_buttons_down) | tracked_logical_buttons
        )
        raw_fallback_physical_buttons = (
            self._take_raw_fallback_physical_buttons_locked(
                raw_fallback_buttons
            )
        )
        affected_buttons = raw_fallback_buttons | tracked_physical_buttons
        self._cancel_all_raw_fallback_hold_guards_locked()
        self._raw_fallback_buttons_down.clear()
        if affected_buttons:
            self._cancel_input_gestures(
                affected_buttons,
                reason=reason,
                block_until_release=True,
            )
            self._release_raw_fallback_keyups(
                raw_fallback_physical_buttons,
                reason=reason,
            )
        return affected_buttons

    def _recover_raw_input_listener(self, token: object) -> None:
        with self._raw_input_operation_lock:
            with self._input_arbitration_lock:
                with self._raw_input_lifecycle_lock:
                    if self._raw_input_retry_token is not token:
                        return
                    self._raw_input_retry_token = None
                    self._raw_input_retry_timer = None
                    if self._raw_input_stopping or not self._accept_input_events:
                        return
                    listener = self._hid_listener
                    if listener is not None:
                        # Invalidate every callback before stop() can emit
                        # forced releases. The arbitration lock also lets an
                        # already-running callback finish before its resulting
                        # state is cancelled below.
                        self._raw_input_generation += 1
                if listener is not None:
                    self._cancel_raw_input_ownership_locked(
                        reason="raw_input_recovery"
                    )

            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    self._set_raw_listener_state("failed_stopping")
                    self._logger.exception(
                        "Raw Input recovery could not stop the old listener"
                    )
                    with self._raw_input_lifecycle_lock:
                        self._schedule_raw_input_recovery_locked()
                    return
                with self._raw_input_lifecycle_lock:
                    if self._hid_listener is listener:
                        self._hid_listener = None
                        self._raw_fallback_tracking_active = False

            try:
                self._start_hid_listener_owned(recovering=True)
            except raw_input_windows.RawInputUnavailableError:
                self._logger.exception(
                    "Raw Input recovery retained a listener that did not start cleanly"
                )
                with self._raw_input_lifecycle_lock:
                    self._schedule_raw_input_recovery_locked()

    def _start_hid_listener(self, *, recovering: bool = False) -> None:
        with self._raw_input_operation_lock:
            self._start_hid_listener_owned(recovering=recovering)

    def _set_raw_listener_state(self, state: str) -> None:
        self._set_runtime_input_state(raw_input_state=state)

    def _record_raw_input_lookup(self, generation, outcome, enumeration, selection) -> None:
        # The producers supply only fixed labels/counts/codes, never device paths.
        fields = dict(outcome=outcome, enumeration=enumeration, selection=selection,
                      selected_ref=self._selected_remote_key[:12]
                      if remote_selection.valid_key(self._selected_remote_key) else "")
        signature = json.dumps(fields, sort_keys=True, ensure_ascii=True)
        if signature == self._last_raw_input_lookup_diagnostic:
            return
        fields.update(generation=generation, app_pid=os.getpid())
        extra = {}
        if outcome in {"no_device", "unavailable", "ambiguous"}:
            extra = dict(failure_key="hid_discovery:raw_input", failure_id=uuid.uuid4().hex)
            fields.update(extra)
        recorded = False
        try:
            self._logger.info("HID diagnostic: %s", json.dumps(
                dict(event="hid_raw_input_lookup", **fields), ensure_ascii=True), extra=extra)
            recorded = True
        except Exception:
            pass
        try:
            recorded = self._diagnostic_trace.emit("hid_raw_input_lookup", **fields) or recorded
        except Exception:
            pass
        if recorded:
            self._last_raw_input_lookup_diagnostic = signature

    def _start_hid_listener_owned(self, *, recovering: bool = False) -> None:
        """Best-effort: buttons fail closed independently of BLE/voice.

        Multiple matching HID device paths -> fail closed for buttons only
        (log and leave the listener unstarted); this must not tear down the
        BLE/voice path, which does not depend on HID at all.

        XRBM-019 review round 1 P1 #3: a failed ``start()`` call does not
        necessarily mean the listener never came alive - it may have left a
        thread/window behind that its own bounded failed-start cleanup could
        not stop (see raw_input_windows.py's ``_abandon_failed_start()``,
        which keeps ``is_running`` honest for exactly this reason). Clearing
        ``self._hid_listener`` to ``None`` unconditionally here would lose
        that owner reference and let a later ``_connect_once()`` generation
        start a second listener over the still-live one. Only clear it once
        the listener itself confirms it is not running; otherwise retain and
        re-raise so this propagates up through ``_connect_once()`` into
        ``ConnectionSupervisor.run_forever()``'s except handler, which still
        falls through to ``_cleanup_once()`` - giving cleanup a chance to
        retry stopping it, exactly like any other retained-owner failure.
        """

        with self._input_arbitration_lock:
            with self._raw_input_lifecycle_lock:
                if self._raw_input_stopping or not self._accept_input_events:
                    return
                current = self._hid_listener
                if current is not None and bool(
                    getattr(current, "is_running", False)
                ):
                    return
                self._hid_listener = None
                self._raw_fallback_tracking_active = False
                self._raw_input_generation += 1
                generation = self._raw_input_generation
                self._raw_input_lost_generation = -1

        self._set_raw_listener_state("recovering" if recovering else "starting")
        enumeration, selection = {}, {}
        lookup_outcome = "lookup_interrupted"
        try:
            device_path = None
            paths = raw_input_windows.enumerate_matching_device_paths(diagnostic=enumeration)
            device_path = remote_selection.selected_raw_path(
                paths, self._selected_remote_key, diagnostic=selection)
            lookup_outcome = "path_selected"
        except raw_input_windows.RawInputUnavailableError as exc:
            lookup_outcome = "unavailable"
            self._set_runtime_input_state(raw_input_state="unavailable")
            self._logger.info("startup: Raw Input unavailable; buttons disabled: %s", exc)
            with self._raw_input_lifecycle_lock:
                if self._raw_input_generation == generation:
                    self._schedule_raw_input_recovery_locked()
            return
        except hid_identity.NoDevicePathFoundError:
            lookup_outcome = "no_device"
            self._set_runtime_input_state(raw_input_state="no_device")
            self._logger.info("startup: no RC003 HID device path found; buttons unavailable")
            with self._raw_input_lifecycle_lock:
                if self._raw_input_generation == generation:
                    self._schedule_raw_input_recovery_locked()
            return
        except hid_identity.AmbiguousDevicePathError as exc:
            lookup_outcome = "ambiguous"
            self._set_runtime_input_state(raw_input_state="ambiguous")
            self._logger.info(
                "startup: buttons failing closed, ambiguous HID device paths: %s", exc
            )
            with self._raw_input_lifecycle_lock:
                if self._raw_input_generation == generation:
                    self._schedule_raw_input_recovery_locked()
            return
        finally:
            self._record_raw_input_lookup(generation, lookup_outcome, enumeration, selection)

        listener = raw_input_windows.RawInputButtonListener(
            self._on_raw_button_event
        )
        with self._raw_input_lifecycle_lock:
            if (
                self._raw_input_stopping
                or not self._accept_input_events
                or self._raw_input_generation != generation
            ):
                return
            self._hid_listener = listener
        self._sync_physical_bindings_to_listener()
        set_sourced_button_event_callback = getattr(
            listener,
            "set_sourced_button_event_callback",
            None,
        )
        if callable(set_sourced_button_event_callback):
            set_sourced_button_event_callback(
                lambda button_id, is_pressed, source, windows_button_id: (
                    self._on_raw_button_event(
                    button_id,
                    is_pressed,
                    source,
                    windows_button_id,
                    _listener=listener,
                    _generation=generation,
                )
                )
            )
        set_raw_event_callback = getattr(
            listener,
            "set_raw_event_callback",
            None,
        )
        if callable(set_raw_event_callback):
            set_raw_event_callback(
                lambda event: self._on_raw_physical_event(
                    event,
                    _listener=listener,
                    _generation=generation,
                )
            )
            with self._raw_input_lifecycle_lock:
                if (
                    self._hid_listener is listener
                    and self._raw_input_generation == generation
                ):
                    self._raw_fallback_tracking_active = True
        set_device_removed_callback = getattr(
            listener,
            "set_device_removed_callback",
            None,
        )
        if callable(set_device_removed_callback):
            set_device_removed_callback(
                lambda: self._on_raw_input_device_removed(
                    _listener=listener,
                    _generation=generation,
                )
            )
        set_input_corruption_callback = getattr(
            listener,
            "set_input_corruption_callback",
            None,
        )
        if callable(set_input_corruption_callback):
            set_input_corruption_callback(
                lambda reason: self._on_raw_input_corruption(
                    reason,
                    _listener=listener,
                    _generation=generation,
                )
            )
        set_physical_keyboard_tracking_lost_callback = getattr(
            listener,
            "set_physical_keyboard_tracking_lost_callback",
            None,
        )
        if callable(set_physical_keyboard_tracking_lost_callback):
            set_physical_keyboard_tracking_lost_callback(
                lambda reason: self._on_physical_keyboard_tracking_lost(
                    reason,
                    _listener=listener,
                    _generation=generation,
                )
            )
        try:
            listener.start(device_path)
        except raw_input_windows.RawInputUnavailableError as exc:
            if listener.is_running:
                self._set_raw_listener_state("failed_running")
                self._logger.exception(
                    "startup: Raw Input listener failed to start but is still running; "
                    "owner retained for cleanup to retry"
                )
                raise
            self._logger.info("startup: Raw Input listener failed to start: %s", exc)
            with self._raw_input_lifecycle_lock:
                if (
                    self._hid_listener is listener
                    and self._raw_input_generation == generation
                ):
                    self._hid_listener = None
                    self._raw_fallback_tracking_active = False
                    self._schedule_raw_input_recovery_locked()
            self._set_raw_listener_state("failed")
            return
        with self._input_arbitration_lock:
            with self._raw_input_lifecycle_lock:
                stale = (
                    self._raw_input_stopping
                    or not self._accept_input_events
                    or self._hid_listener is not listener
                    or self._raw_input_generation != generation
                )
                lost = self._raw_input_lost_generation == generation
                running = listener.is_running
                if not stale and (lost or not running):
                    self._schedule_raw_input_recovery_locked()
                elif not stale:
                    self._raw_input_retry_delay = (
                        _RAW_INPUT_RETRY_INITIAL_SECONDS
                    )
            if not stale:
                self._set_raw_listener_state("recovering" if lost or not running else "ready")
        if stale:
            if listener.is_running:
                try:
                    listener.stop()
                except Exception:
                    self._logger.exception(
                        "cleanup: stale Raw Input listener did not stop"
                    )
            return
        if lost or not running:
            return

    def _sync_physical_bindings_to_listener(self) -> None:
        with self._button_mapping_lock:
            listener = self._hid_listener
            if listener is None:
                return
            set_physical_bindings = getattr(listener, "set_physical_bindings", None)
            if not callable(set_physical_bindings):
                return
            try:
                set_physical_bindings(
                    self._bindings.get("physical_bindings", {})
                )
            except Exception as exc:  # noqa: BLE001 - keep the last live decoder
                self._logger.warning(
                    "physical button bindings update failed: error_type=%s",
                    type(exc).__name__,
                )

    def _start_hid_report_tap(self) -> None:
        """Start the upstream-derived tap for usages Windows drops.

        This is independent of the normal Raw Input listener.  A missing or
        unverified Gadget is a button-only degradation and must not prevent
        BLE voice from starting.
        """

        self._set_runtime_input_state(hid_tap_state="starting")
        tap = frida_compat.RC003HidReportTap(
            self._on_direct_hid_report,
            selected_key=self._selected_remote_key,
            status_handler=self._on_hid_tap_status,
            diagnostic_trace=self._diagnostic_trace,
        )
        try:
            if tap.start():
                self._hid_report_tap = tap
                self._logger.info(
                    "startup: RC003 HID report tap thread started; state=%s",
                    tap.status,
                )
            else:
                self._set_runtime_input_state(hid_tap_state=tap.status)
                self._logger.info(
                    "startup: RC003 HID report tap unavailable: %s", tap.status
                )
        except Exception:
            self._set_runtime_input_state(
                hid_tap_state=frida_compat.HidTapState.FAILED.value
            )
            self._logger.exception("startup: RC003 HID report tap failed to start")
            try:
                tap.stop()
            except Exception:
                self._logger.exception("startup: RC003 HID report tap cleanup failed")
                self._hid_report_tap = tap
                raise

    @staticmethod
    def _direct_buttons_for_usages(usages: set[int]) -> set[str]:
        return {
            button
            for usage in usages
            if (button := frida_compat.TAP_USAGE_TO_BUTTON.get(usage)) is not None
        }

    def _cancel_input_gestures(
        self,
        buttons: set[str],
        *,
        reason: str,
        block_until_release: bool,
    ) -> None:
        if block_until_release:
            self._block_input_until_release(buttons)
        self._button_combos.reset()
        self._button_gestures.reset()
        self._key_detection_suppressed_buttons.clear()
        self._key_detection_suppression_deadlines.clear()

        if "mic" in buttons:
            with self._ordinary_mic_lock:
                self._ordinary_mic_sources_down.clear()
                self._ordinary_mic_late_sources_down.clear()
                self._ordinary_mic_late_source_deadlines.clear()
                self._ordinary_mic_sources_seen.clear()
                self._ordinary_mic_gesture_started_at = 0.0
                self._ordinary_mic_release_guard_until = 0.0
                self._ordinary_mic_gesture_active = False
            with self._key_detection_mic_lock:
                self._reset_key_detection_mic_gesture_locked()
            with self._voice_shortcut.lock:
                self._voice_mic_gesture_sources_down.clear()
                self._voice_shortcut_physical_sources_down.clear()
                self._voice_mic_gesture_hid_released = True
                if self._voice_shortcut.controller.active:
                    self._release_hold_voice_on_physical_release_locked(reason)
                if (
                    self._voice_mic_gesture_active
                    and not self._voice_audio_stream_active
                ):
                    self._finish_voice_mic_gesture()
                self._apply_pending_voice_settings_if_idle_locked()

        if buttons:
            self._logger.warning(
                "button input ownership cancelled: reason=%s buttons=%s",
                reason,
                sorted(buttons),
            )

    def _cancel_input_gestures_for_buttons(
        self,
        buttons: set[str],
        *,
        reason: str,
        block_until_release: bool,
    ) -> None:
        """Cancel only input state coupled to the named physical buttons."""

        affected_buttons = set(buttons)
        affected_buttons.update(
            self._button_combos.cancel_buttons(affected_buttons)
        )
        if block_until_release:
            self._block_input_until_release(affected_buttons)
        self._button_gestures.cancel_buttons(affected_buttons)
        self._key_detection_suppressed_buttons.difference_update(
            affected_buttons
        )
        for button_id in affected_buttons:
            self._key_detection_suppression_deadlines.pop(button_id, None)

        if "mic" in affected_buttons:
            with self._ordinary_mic_lock:
                self._ordinary_mic_sources_down.clear()
                self._ordinary_mic_late_sources_down.clear()
                self._ordinary_mic_late_source_deadlines.clear()
                self._ordinary_mic_sources_seen.clear()
                self._ordinary_mic_gesture_started_at = 0.0
                self._ordinary_mic_release_guard_until = 0.0
                self._ordinary_mic_gesture_active = False
            with self._key_detection_mic_lock:
                self._reset_key_detection_mic_gesture_locked()
            with self._voice_shortcut.lock:
                self._voice_mic_gesture_sources_down.clear()
                self._voice_mic_gesture_hid_released = True
                if self._voice_shortcut.controller.active:
                    self._release_hold_voice_on_physical_release_locked(reason)
                if (
                    self._voice_mic_gesture_active
                    and not self._voice_audio_stream_active
                ):
                    self._finish_voice_mic_gesture()
                self._apply_pending_voice_settings_if_idle_locked()

        if affected_buttons:
            self._logger.warning(
                "button input ownership cancelled: reason=%s buttons=%s",
                reason,
                sorted(affected_buttons),
            )

    def _active_audio_owns_late_raw_mic(self) -> bool:
        """Return whether ATVV audio already owns the current mic press."""

        with self._voice_shortcut.lock:
            return (
                self._voice_mic_gesture_active
                and self._voice_mic_gesture_audio_started
                and self._voice_audio_stream_active
                and (
                    self._voice_shortcut.controller.active
                    or self._voice_shortcut.pending_tokens is not None
                )
            )

    def _on_raw_physical_event(
        self,
        event: raw_input_windows.RawInputEvent,
        *,
        _listener: Optional[raw_input_windows.RawInputButtonListener] = None,
        _generation: Optional[int] = None,
    ) -> None:
        """Track the real Windows key independently of a taught button ID."""

        if not self._raw_input_callback_is_current(_listener, _generation):
            return
        logical_button = event.button_id
        physical_button = event.windows_button_id
        trace_button = logical_button or physical_button or "unknown"
        trace_source = {
            "keyboard": "raw_keyboard",
            "hid": "raw_hid",
        }.get(event.source, "raw_unknown")
        gesture_id = self._diagnostic_trace.begin_gesture(
            trace_button,
            trace_source,
            bool(event.is_pressed),
            vkey=event.vkey if event.vkey is not None else -1,
            scan_code=event.make_code if event.make_code is not None else -1,
            flags=event.flags if event.flags is not None else -1,
        )
        self._diagnostic_trace.emit(
            "raw_input_event",
            gesture_id=gesture_id,
            button_id=event.button_id or "",
            windows_button_id=event.windows_button_id or "",
            edge="down" if event.is_pressed else "up",
            vkey=event.vkey if event.vkey is not None else -1,
            make_code=event.make_code if event.make_code is not None else -1,
            flags=event.flags if event.flags is not None else -1,
            raw_source=event.source,
            interception_armed=bool(self._direct_hid_interception_armed),
            interception_ready=bool(self._direct_hid_interception_ready),
        )
        signature = raw_input_windows.physical_signature(event)
        with self._input_arbitration_lock:
            if not self._raw_input_callback_is_current(
                _listener,
                _generation,
            ):
                return
            self._raw_fallback_tracking_active = True

            if not event.is_pressed:
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="release_tracking",
                )
                tracked = self._raw_fallback_physical_buttons_down.pop(
                    signature,
                    None,
                )
                debt_logical_buttons, debt_physical_buttons = (
                    self._raw_fallback_release_debts.pop(
                        signature,
                        (set(), set()),
                    )
                )
                released_buttons = (
                    set(debt_logical_buttons) | set(debt_physical_buttons)
                )
                if tracked is not None:
                    released_buttons.add(tracked[0])
                    if tracked[1] is not None:
                        released_buttons.add(tracked[1])
                else:
                    if logical_button is not None:
                        released_buttons.add(logical_button)
                    if physical_button is not None:
                        released_buttons.add(physical_button)
                remaining = [
                    {
                        candidate
                        for candidate in tracked_pair
                        if candidate is not None
                    }
                    for tracked_pair in (
                        self._raw_fallback_physical_buttons_down.values()
                    )
                ] + [
                    set(logical_buttons) | set(physical_buttons)
                    for logical_buttons, physical_buttons in (
                        self._raw_fallback_release_debts.values()
                    )
                ]
                with self._direct_hid_lock:
                    direct_buttons = self._direct_buttons_for_usages(
                        self._direct_hid_usages
                    )
                for blocked_button in released_buttons:
                    if (
                        blocked_button not in direct_buttons
                        and all(
                            blocked_button not in candidate
                            for candidate in remaining
                        )
                    ):
                        self._input_rearm_blocked_buttons.discard(
                            blocked_button
                        )
                return

            if not self._accept_input_events:
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="ignored",
                    reason="input_not_accepted",
                )
                return
            release_debt = self._raw_fallback_release_debts.get(signature)
            if release_debt is not None:
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="blocked",
                    reason="release_debt",
                )
                debt_logical_buttons, debt_physical_buttons = release_debt
                if logical_button is not None:
                    debt_logical_buttons.add(logical_button)
                if physical_button is not None:
                    debt_physical_buttons.add(physical_button)
                self._block_input_until_release(
                    set(debt_logical_buttons) | set(debt_physical_buttons)
                )
                if physical_button in _RAW_FALLBACK_KEY_TOKENS:
                    self._release_raw_fallback_keyups(
                        {physical_button},
                        reason="raw_release_debt_repeat",
                    )
                return
            if logical_button is None:
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="ignored",
                    reason="unmapped_physical_key",
                )
                return
            was_new = signature not in self._raw_fallback_physical_buttons_down
            tracked = self._raw_fallback_physical_buttons_down.setdefault(
                signature,
                (logical_button, physical_button),
            )
            tracked_logical, tracked_physical = tracked
            if not (
                self._direct_hid_interception_armed
                or self._direct_hid_interception_ready
            ):
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="windows_original",
                    reason="hid_interception_not_armed",
                )
                return

            physical_was_blocked = (
                tracked_physical in self._input_rearm_blocked_buttons
            )
            blocked_buttons = {tracked_logical}
            if tracked_physical is not None:
                blocked_buttons.add(tracked_physical)
            with self._direct_hid_lock:
                direct_buttons = self._direct_buttons_for_usages(
                    self._direct_hid_usages
                )
            raw_only_buttons = set(blocked_buttons) - direct_buttons
            if (
                "mic" in raw_only_buttons
                and self._active_audio_owns_late_raw_mic()
            ):
                # AudioStarted can beat both the duplicated Windows F5 edge
                # and the direct HID report. The active ATVV gesture remains
                # authoritative until HID-up or AudioStopped closes it.
                raw_only_buttons.discard("mic")
            newly_quarantined = was_new and not physical_was_blocked
            if newly_quarantined:
                # A late Raw Input down is the Windows-side duplicate of a
                # report already owned by direct HID. Quarantine only aliases
                # that direct HID is not currently holding; the real HID up
                # must remain the sole release edge for the active gesture.
                if raw_only_buttons:
                    self._cancel_input_gestures_for_buttons(
                        raw_only_buttons,
                        reason="late_raw_after_hid_handover",
                        block_until_release=True,
                    )
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="quarantined",
                    reason="late_raw_after_hid_handover",
                    late=True,
                )
            else:
                self._block_input_until_release(raw_only_buttons)
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="blocked",
                    reason="already_quarantined",
                )
            if newly_quarantined and tracked_physical in _RAW_FALLBACK_KEY_TOKENS:
                self._release_raw_fallback_keyups(
                    {tracked_physical},
                    reason="late_raw_after_hid_handover",
                )

    def _take_raw_fallback_physical_buttons_locked(
        self,
        logical_buttons: set[str],
    ) -> set[str]:
        matched_signatures = {
            signature
            for signature, (logical_button, _physical_button) in (
                self._raw_fallback_physical_buttons_down.items()
            )
            if logical_button in logical_buttons
        }
        physical_buttons = {
            self._raw_fallback_physical_buttons_down[signature][1]
            for signature in matched_signatures
            if self._raw_fallback_physical_buttons_down[signature][1]
            in _RAW_FALLBACK_KEY_TOKENS
        }
        for signature in matched_signatures:
            tracked = self._raw_fallback_physical_buttons_down.pop(
                signature,
                None,
            )
            if tracked is not None:
                release_debt = self._raw_fallback_release_debts.setdefault(
                    signature,
                    (set(), set()),
                )
                debt_logical_buttons, debt_physical_buttons = release_debt
                debt_logical_buttons.add(tracked[0])
                if tracked[1] is not None:
                    debt_physical_buttons.add(tracked[1])

        # Compatibility for injected test owners and old listener fakes that
        # do not expose raw physical events. The real listener always enables
        # tracking before it can emit a logical edge, so production remaps
        # never guess the logical key here.
        if not self._raw_fallback_tracking_active:
            physical_buttons.update(
                logical_button
                for logical_button in logical_buttons
                if logical_button in _RAW_FALLBACK_KEY_TOKENS
            )
        return physical_buttons

    def _raw_fallback_tracked_button_ids_locked(
        self,
    ) -> tuple[set[str], set[str]]:
        logical_buttons = {
            logical_button
            for logical_button, _physical_button in (
                self._raw_fallback_physical_buttons_down.values()
            )
        }
        physical_buttons = {
            physical_button
            for _logical_button, physical_button in (
                self._raw_fallback_physical_buttons_down.values()
            )
            if physical_button in _RAW_FALLBACK_KEY_TOKENS
        }
        return logical_buttons, physical_buttons

    def _windows_buttons_down_before_hid_handover(
        self,
    ) -> Optional[set[str]]:
        buttons_down: set[str] = set()
        try:
            for button, token in _RAW_FALLBACK_KEY_TOKENS.items():
                vk_codes = win32_keys.resolve_vk_codes((token,))
                if any(
                    self._raw_windows_key_down_query(vk_code)
                    for vk_code in vk_codes
                ):
                    buttons_down.add(button)
        except Exception as exc:  # noqa: BLE001 - unknown state fails closed
            self._logger.warning(
                "Windows key state unavailable during HID handover: error_type=%s",
                type(exc).__name__,
            )
            return None
        return buttons_down

    def _release_raw_fallback_keyups(
        self,
        physical_buttons: set[str],
        *,
        reason: str,
    ) -> None:
        tokens = tuple(
            dict.fromkeys(
                token
                for button in sorted(physical_buttons)
                if (token := _RAW_FALLBACK_KEY_TOKENS.get(button)) is not None
            )
        )
        if not tokens:
            return
        with self._button_action_lock:
            pending = self._button_key_release_pending or ()
            owed_tokens = tuple(dict.fromkeys((*pending, *tokens)))
            self._button_key_release_pending = owed_tokens
            if self._release_pending_button_keys():
                self._logger.info(
                    "raw Windows key-up safety release completed: reason=%s buttons=%s",
                    reason,
                    sorted(physical_buttons),
                )

    def _cancel_raw_fallback_hold_guard_locked(self, button_id: str) -> None:
        guard = self._raw_fallback_hold_guards.pop(button_id, None)
        if guard is None:
            return
        _token, timer = guard
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except BaseException as exc:  # noqa: BLE001 - token is already invalid
                self._logger.warning(
                    "raw fallback hold timer cancel failed: button=%s error_type=%s",
                    button_id,
                    type(exc).__name__,
                )

    def _cancel_all_raw_fallback_hold_guards_locked(self) -> None:
        for button_id in tuple(self._raw_fallback_hold_guards):
            self._cancel_raw_fallback_hold_guard_locked(button_id)

    def _arm_raw_fallback_hold_guard_locked(self, button_id: str) -> bool:
        self._cancel_raw_fallback_hold_guard_locked(button_id)
        token = object()
        try:
            timer = self._raw_fallback_timer_factory(
                button_gesture.ButtonGestureDispatcher.MAX_HOLD_SECONDS,
                lambda button_id=button_id, token=token: (
                    self._raw_fallback_hold_timeout(button_id, token)
                ),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._raw_fallback_hold_guards[button_id] = (token, timer)
            timer.start()
        except BaseException as exc:  # noqa: BLE001 - immediately release the key
            current = self._raw_fallback_hold_guards.get(button_id)
            if current is not None and current[0] is token:
                self._raw_fallback_hold_guards.pop(button_id, None)
            self._logger.error(
                "raw fallback hold timer failed to start: button=%s error_type=%s",
                button_id,
                type(exc).__name__,
            )
            return False
        return True

    def _raw_fallback_hold_timeout(self, button_id: str, token: object) -> None:
        with self._input_arbitration_lock:
            guard = self._raw_fallback_hold_guards.get(button_id)
            if guard is None or guard[0] is not token:
                return
            self._raw_fallback_hold_guards.pop(button_id, None)
            if button_id not in self._raw_fallback_buttons_down:
                return
            self._raw_fallback_buttons_down.discard(button_id)
            physical_buttons = self._take_raw_fallback_physical_buttons_locked(
                {button_id}
            )
            self._block_input_until_release(
                {button_id} | physical_buttons
            )
            self._release_raw_fallback_keyups(
                physical_buttons,
                reason="raw_fallback_hold_timeout",
            )
            self._logger.warning(
                "raw Windows key held for %.0fs; forced release until physical up: "
                "button=%s",
                button_gesture.ButtonGestureDispatcher.MAX_HOLD_SECONDS,
                button_id,
            )

    def _on_raw_input_device_removed(
        self,
        *,
        _listener: Optional[raw_input_windows.RawInputButtonListener] = None,
        _generation: Optional[int] = None,
    ) -> None:
        if not self._raw_input_callback_is_current(_listener, _generation):
            return
        with self._input_arbitration_lock:
            if not self._raw_input_callback_is_current(
                _listener,
                _generation,
            ):
                return
            with self._raw_input_lifecycle_lock:
                resolved_generation = (
                    self._raw_input_generation
                    if _generation is None
                    else int(_generation)
                )
                self._raw_input_lost_generation = resolved_generation
            self._set_runtime_input_state(raw_input_state="device_removed")
            with self._direct_hid_lock:
                direct_buttons = self._direct_buttons_for_usages(
                    self._direct_hid_usages
                )
                self._direct_hid_usages.clear()
            tracked_logical_buttons, tracked_physical_buttons = (
                self._raw_fallback_tracked_button_ids_locked()
            )
            raw_fallback_buttons = (
                set(self._raw_fallback_buttons_down)
                | tracked_logical_buttons
            )
            raw_fallback_physical_buttons = (
                self._take_raw_fallback_physical_buttons_locked(
                    raw_fallback_buttons
                )
            )
            self._cancel_all_raw_fallback_hold_guards_locked()
            lost_buttons = (
                raw_fallback_buttons
                | tracked_physical_buttons
                | direct_buttons
                | set(self._input_rearm_blocked_buttons)
            )
            self._direct_hid_interception_ready = False
            self._direct_hid_interception_armed = False
            self._direct_hid_handover_waiting_for_neutral = False
            self._raw_fallback_buttons_down.clear()
            self._input_rearm_blocked_buttons.clear()
            # The Raw Input collection can be re-enumerated while the
            # separately owned HID tap is still alive. Preserve a guard for
            # direct buttons that were down until a later neutral report or
            # matching release proves the old hold is over.
            self._block_input_until_release(lost_buttons)
            self._cancel_input_gestures(
                lost_buttons,
                reason="raw_input_device_removed",
                block_until_release=False,
            )
            self._release_raw_fallback_keyups(
                raw_fallback_physical_buttons,
                reason="raw_input_device_removed",
            )
        self._logger.warning("RC003 Raw Input device removed; requesting reconnect")
        with self._raw_input_lifecycle_lock:
            if self._raw_input_callback_is_current(_listener, _generation):
                self._schedule_raw_input_recovery_locked()
        self._supervisor.request_reconnect()

    def _on_raw_input_corruption(
        self,
        reason: str,
        *,
        _listener: Optional[raw_input_windows.RawInputButtonListener] = None,
        _generation: Optional[int] = None,
    ) -> None:
        """Cancel Raw Input ownership while preserving the physical release."""

        if not self._raw_input_callback_is_current(_listener, _generation):
            return
        with self._input_arbitration_lock:
            if not self._raw_input_callback_is_current(
                _listener,
                _generation,
            ):
                return
            affected_buttons = self._cancel_raw_input_ownership_locked(
                reason=f"raw_input_corruption_{reason}"
            )
            if reason in {
                "raw_input_header_unavailable",
                "raw_input_listener_exited",
            }:
                with self._raw_input_lifecycle_lock:
                    resolved_generation = (
                        self._raw_input_generation
                        if _generation is None
                        else int(_generation)
                    )
                    self._raw_input_lost_generation = resolved_generation
                    self._schedule_raw_input_recovery_locked()
                self._set_runtime_input_state(raw_input_state="recovering")
        self._logger.warning(
            "RC003 Raw Input data rejected: reason=%s cancelled_buttons=%s",
            reason,
            sorted(affected_buttons),
        )

    def _on_physical_keyboard_tracking_lost(
        self,
        reason: str,
        *,
        _listener: Optional[raw_input_windows.RawInputButtonListener] = None,
        _generation: Optional[int] = None,
    ) -> None:
        """End an unsafe ordinary-key HOLD; future downs fail closed."""

        with self._input_arbitration_lock:
            if not self._raw_input_callback_is_current(
                _listener,
                _generation,
            ):
                return
            with self._raw_input_lifecycle_lock:
                resolved_generation = (
                    self._raw_input_generation
                    if _generation is None
                    else int(_generation)
                )
                self._raw_input_lost_generation = resolved_generation
                self._schedule_raw_input_recovery_locked()
            self._set_raw_listener_state("unhealthy")
            with self._voice_shortcut.lock:
                tokens = self._voice_shortcut.recorded_tokens()
                active_hold = (
                    self._voice_shortcut.controller.active
                    or self._voice_shortcut.pending_tokens is not None
                )
                if active_hold and not win32_input.can_begin_tracked_hold(tokens):
                    self._force_voice_hold_release_locked(
                        "physical keyboard tracking lost"
                    )
            self._release_pending_button_keys()
        self._logger.warning(
            "physical keyboard tracking lost: reason=%s; unsafe held shortcuts disabled",
            reason,
        )

    def _on_voice_key_physicalizer_tracking_lost(
        self,
        physicalizer: Optional[
            voice_key_physicalizer_windows.VoiceKeyPhysicalizer
        ] = None,
        generation: Optional[int] = None,
    ) -> None:
        """Release an active marked right-Alt hold before the hook disappears."""

        with self._voice_key_physicalizer_lifecycle_lock:
            current = self._voice_key_physicalizer
            current_generation = self._voice_key_physicalizer_generation
            resolved_physicalizer = physicalizer or current
            resolved_generation = (
                current_generation if generation is None else int(generation)
            )
            if (
                resolved_physicalizer is None
                or current is not resolved_physicalizer
                or current_generation != resolved_generation
                or self._voice_key_physicalizer_stopping
            ):
                return
            self._voice_key_physicalizer_lost_generation = resolved_generation
            self._voice_key_physicalizer_ready = False
        self._set_runtime_input_state(
            voice_key_physicalizer_state="recovering"
        )
        raw_tracking_available = (
            raw_input_windows.physical_keyboard_tracking_available()
        )
        with self._voice_shortcut.lock:
            tokens = self._voice_shortcut.recorded_tokens()
            try:
                uses_right_alt = (
                    win32_keys.VK_CODES["ralt"]
                    in win32_keys.resolve_vk_codes(tokens)
                )
            except win32_keys.UnknownKeyTokenError:
                uses_right_alt = False
            active_hold = (
                self._voice_shortcut.controller.active
                or self._voice_shortcut.pending_tokens is not None
            )
            if (
                active_hold
                and (
                    not raw_tracking_available
                    or uses_right_alt
                )
            ):
                self._force_voice_hold_release_locked(
                    "voice key physicalizer stopped unexpectedly"
                )
        if not raw_tracking_available:
            self._release_pending_button_keys()
        with self._voice_key_physicalizer_lifecycle_lock:
            if (
                self._voice_key_physicalizer is resolved_physicalizer
                and self._voice_key_physicalizer_generation
                == resolved_generation
                and not self._voice_key_physicalizer_stopping
            ):
                self._schedule_voice_key_physicalizer_recovery_locked()
        self._logger.warning(
            "voice key physicalizer stopped unexpectedly; recovery scheduled"
        )

    def _on_voice_key_physicalizer_health_failure(
        self,
        physicalizer: voice_key_physicalizer_windows.VoiceKeyPhysicalizer,
        generation: int,
        reason: str,
        snapshot: voice_key_physicalizer_windows.PhysicalizerHealthSnapshot,
    ) -> None:
        """Close admission immediately and recover only after owned cleanup."""

        with self._voice_key_physicalizer_lifecycle_lock:
            if (
                self._voice_key_physicalizer is not physicalizer
                or self._voice_key_physicalizer_generation != int(generation)
                or self._voice_key_physicalizer_stopping
            ):
                return
            self._voice_key_physicalizer_ready = False
            self._voice_key_physicalizer_degraded_retry_deadline = (
                time.monotonic()
                + _VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS
            )
            self._schedule_voice_key_physicalizer_recovery_locked()
        self._set_runtime_input_state(
            voice_key_physicalizer_state="recovering"
        )
        try:
            self._diagnostic_trace.emit(
                "voice_key_physicalizer_health",
                status="degraded",
                reason=str(reason),
                app_generation=int(generation),
                **snapshot.trace_fields(),
            )
        except BaseException:
            pass
        self._logger.warning(
            "voice key physicalizer degraded after required acknowledgement "
            "failure; new voice starts blocked until owned cleanup completes: "
            "generation=%s tracker_generation=%s callback_delta=%s marker_delta=%s",
            generation,
            snapshot.generation,
            snapshot.receipt_callback_entry_delta,
            snapshot.receipt_marker_callback_delta,
        )

    def _on_hid_tap_status(self, status: str, detail: str) -> None:
        self._diagnostic_trace.emit(
            "hid_tap_status",
            status=str(status),
            detail=str(detail or ""),
        )
        self._set_runtime_input_state(hid_tap_state=status)
        interception_ready = status == frida_compat.HidTapState.READY.value
        interception_armed = status in {
            frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
            frida_compat.HidTapState.READY.value,
        }
        message = "RC003 HID report tap state: %s"
        args = [status]
        if detail:
            message += " detail=%s"
            args.append(detail)
        with self._input_arbitration_lock:
            newly_armed = (
                interception_armed
                and not self._direct_hid_interception_armed
            )
            if not interception_ready and self._direct_hid_interception_ready:
                self._logger.warning(message, *args)
                with self._direct_hid_lock:
                    stale_usages = set(self._direct_hid_usages)
                    self._direct_hid_usages.clear()
                stale_buttons = self._direct_buttons_for_usages(stale_usages)
                self._cancel_input_gestures(
                    stale_buttons,
                    reason="hid_tap_ownership_lost",
                    block_until_release=True,
                )
            else:
                self._logger.info(message, *args)
            if newly_armed:
                # ATTACHED_WAITING_IO is emitted only after the helper has
                # acknowledged the interception lease. Cancel any in-flight
                # Raw Input gesture instead of completing it as a click, then
                # ignore the same physical hold until its real release arrives.
                windows_buttons_down = (
                    set()
                    if getattr(self._hid_report_tap, "native_copy_interception", False) is True
                    else self._windows_buttons_down_before_hid_handover()
                )
                self._direct_hid_handover_waiting_for_neutral = (
                    windows_buttons_down is None
                )
                windows_buttons_down = windows_buttons_down or set()
                tracked_logical_buttons, tracked_physical_buttons = (
                    self._raw_fallback_tracked_button_ids_locked()
                )
                raw_fallback_buttons = (
                    set(self._raw_fallback_buttons_down)
                    | tracked_logical_buttons
                )
                raw_fallback_physical_buttons = (
                    self._take_raw_fallback_physical_buttons_locked(
                        raw_fallback_buttons
                    )
                )
                handover_buttons = (
                    raw_fallback_buttons
                    | tracked_physical_buttons
                    | windows_buttons_down
                )
                self._cancel_all_raw_fallback_hold_guards_locked()
                self._cancel_input_gestures(
                    handover_buttons,
                    reason="hid_tap_handover",
                    block_until_release=True,
                )
                self._release_raw_fallback_keyups(
                    raw_fallback_physical_buttons | windows_buttons_down,
                    reason="hid_tap_handover",
                )
                self._raw_fallback_buttons_down.clear()
            elif not interception_armed:
                self._direct_hid_handover_waiting_for_neutral = False
            self._direct_hid_interception_armed = interception_armed
            self._direct_hid_interception_ready = interception_ready

    def _on_direct_hid_report(self, report_id: int, payload: bytes) -> None:
        """Translate every RC003 keyboard HID usage into button edges.

        The injected Gadget copies the report and clears its keyboard usages
        before Windows translates them. This callback therefore owns custom
        mapping only after the tap reports verified interception. Raw Keyboard
        remains observation/fallback and never adds a second mapped action.
        """

        observed_ns = time.perf_counter_ns()
        with self._input_arbitration_lock:
            self._direct_hid_report_seq += 1
            callback_seq = self._direct_hid_report_seq

            def record_decision(reason: str, **fields: object) -> None:
                if self._diagnostic_trace.enabled:
                    self._diagnostic_trace.emit(
                        "hid_report_decision",
                        callback_seq=callback_seq,
                        reason=reason,
                        resulting_usages=sorted(self._direct_hid_usages),
                        **fields,
                    )

            if self._diagnostic_trace.enabled:
                self._diagnostic_trace.emit(
                    "hid_report",
                    callback_seq=callback_seq,
                    observed_perf_counter_ns=observed_ns,
                    report_id=int(report_id),
                    report_length=len(payload),
                    report_hex=(
                        payload.hex() if report_id == 1 and len(payload) == 6 else ""
                    ),
                    previous_usages=sorted(self._direct_hid_usages),
                    accept_input=self._accept_input_events,
                    interception_armed=self._direct_hid_interception_armed,
                    interception_ready=self._direct_hid_interception_ready,
                    waiting_for_neutral=self._direct_hid_handover_waiting_for_neutral,
                )
            tap = self._hid_report_tap
            tap_reports_ready = (
                tap is not None
                and tap.status == frida_compat.HidTapState.READY.value
            )
            if (
                not self._accept_input_events
                or report_id != 1
                or len(payload) != 6
            ):
                record_decision(
                    "input_disabled" if not self._accept_input_events else "invalid_report"
                )
                return
            if not self._direct_hid_interception_ready:
                if not tap_reports_ready:
                    record_decision("tap_not_ready")
                    return
                # A newly received, validated tap report is stronger evidence
                # than the Raw Input collection-removal notification. Restore
                # local ownership without waiting for a duplicate status event.
                if not self._direct_hid_interception_armed:
                    windows_buttons_down = (
                        set()
                        if getattr(tap, "native_copy_interception", False) is True
                        else self._windows_buttons_down_before_hid_handover()
                    )
                    self._direct_hid_handover_waiting_for_neutral = (
                        windows_buttons_down is None
                    )
                    if windows_buttons_down:
                        self._block_input_until_release(windows_buttons_down)
                        self._release_raw_fallback_keyups(
                            windows_buttons_down,
                            reason="hid_tap_report_revalidated",
                        )
                self._direct_hid_interception_armed = True
                self._direct_hid_interception_ready = True
            elif tap is not None and not tap_reports_ready:
                # The tap updates its own state before invoking the app status
                # callback. Reject a report that raced that callback.
                record_decision("tap_lost_readiness")
                return
            active = {
                int.from_bytes(payload[index : index + 2], "little")
                for index in range(0, len(payload), 2)
            } & set(frida_compat.TAP_USAGE_TO_BUTTON)
            active_buttons = self._direct_buttons_for_usages(active)
            with self._direct_hid_lock:
                previous = self._direct_hid_usages
                previous_buttons = self._direct_buttons_for_usages(previous)
                if self._direct_hid_handover_waiting_for_neutral:
                    if active:
                        self._block_input_until_release(
                            active_buttons | previous_buttons
                        )
                        self._direct_hid_usages = set(active)
                        record_decision("waiting_for_neutral")
                        return
                    self._direct_hid_handover_waiting_for_neutral = False
                if not active:
                    # A verified neutral tap snapshot proves that every
                    # pre-handover Raw physical edge has ended. Drop those
                    # records so a later press cannot inherit stale ownership.
                    self._raw_fallback_physical_buttons_down.clear()
                    self._raw_fallback_release_debts.clear()
                self._input_rearm_blocked_buttons.difference_update(
                    self._input_rearm_blocked_buttons
                    - active_buttons
                    - previous_buttons
                )
                if active == previous:
                    record_decision("unchanged")
                    return
                pressed = active - previous
                released = previous - active
                self._direct_hid_usages = set(active)
            record_decision(
                "edges",
                pressed_usages=sorted(pressed),
                released_usages=sorted(released),
            )
            for usage in sorted(released):
                button = frida_compat.TAP_USAGE_TO_BUTTON[usage]
                self._logger.info(
                    "RC003 direct HID usage up: 0x%04x -> %s",
                    usage,
                    button,
                )
                self._record_rc003_direction_edge(usage, False)
                self._on_button_event(
                    button,
                    False,
                    event_source="hid_tap",
                    hid_usage=usage,
                )
            with self._button_mapping_lock:
                combo_modifier = key_mapping.button_combo_modifier(self._bindings)
            for usage in sorted(
                pressed,
                key=lambda candidate: (
                    frida_compat.TAP_USAGE_TO_BUTTON[candidate] != combo_modifier,
                    candidate,
                ),
            ):
                button = frida_compat.TAP_USAGE_TO_BUTTON[usage]
                self._logger.info(
                    "RC003 direct HID usage down: 0x%04x -> %s",
                    usage,
                    button,
                )
                self._record_rc003_direction_edge(usage, True)
                self._on_button_event(
                    button,
                    True,
                    event_source="hid_tap",
                    hid_usage=usage,
                )

    def _record_rc003_direction_edge(
        self,
        usage: int,
        is_pressed: bool,
    ) -> None:
        # The native report is already neutral before the kernel copy. Arming
        # the old global gate here could consume an unrelated keyboard arrow.
        if getattr(self._hid_report_tap, "native_copy_interception", False) is True:
            return
        key = frida_compat.TAP_DIRECTION_USAGE_TO_KEY.get(int(usage))
        if key is None:
            return
        vk, scan_code, extended = key
        with self._voice_key_physicalizer_lifecycle_lock:
            physicalizer = self._voice_key_physicalizer
            physicalizer_ready = self._voice_key_physicalizer_ready
        recorder = getattr(physicalizer, "record_rc003_direction_edge", None)
        if physicalizer_ready and callable(recorder):
            try:
                if recorder(vk, scan_code, extended, is_pressed):
                    return
            except Exception:
                self._logger.exception(
                    "RC003 global direction ownership failed; navigation fallback used"
                )
        element_navigation_control_windows.record_rc003_direction_edge(
            vk,
            scan_code,
            extended,
            is_pressed,
        )

    def _stop_input_channels(self) -> None:
        """Stop process-lifetime input resources exactly once at worker exit."""

        failures: List[str] = []
        with self._voice_key_physicalizer_lifecycle_lock:
            self._voice_key_physicalizer_stopping = True
            self._voice_key_physicalizer_generation += 1
            self._voice_key_physicalizer_degraded_retry_deadline = 0.0
            self._cancel_voice_key_physicalizer_retry_locked()
        with self._input_arbitration_lock:
            with self._raw_input_lifecycle_lock:
                self._raw_input_stopping = True
                self._raw_input_generation += 1
                self._cancel_raw_input_retry_locked()
        with self._button_action_lock:
            self._button_input_release_retry_stopping = True
            self._cancel_button_input_release_retry_locked(reset_delay=False)
        with self._voice_shortcut.lock:
            self._voice_shortcut.retry_stopping = True
            self._voice_shortcut.cancel_release_retry(reset_delay=False)

        # Input loss is cancellation, not a click. Clear gesture timers before
        # listener shutdown emits forced releases, and release any Windows key
        # whose original down was deliberately allowed through during fallback.
        self._button_combos.reset()
        self._button_gestures.reset()
        with self._input_arbitration_lock:
            self._cancel_all_raw_fallback_hold_guards_locked()
            tracked_logical_buttons, _tracked_physical_buttons = (
                self._raw_fallback_tracked_button_ids_locked()
            )
            raw_fallback_physical_buttons = (
                self._take_raw_fallback_physical_buttons_locked(
                    set(self._raw_fallback_buttons_down)
                    | tracked_logical_buttons
                )
            )
            self._release_raw_fallback_keyups(
                raw_fallback_physical_buttons,
                reason="input_channels_stopping",
            )

        # Keep dispatch enabled until both listeners have emitted their forced
        # release edges. The HID tap is stopped first so its explicit disable
        # handshake restores the Windows path before Raw Input is closed.
        if self._hid_report_tap is not None:
            try:
                self._hid_report_tap.stop()
                self._hid_report_tap = None
                self._set_runtime_input_state(hid_tap_state="stopped")
            except Exception:
                self._set_runtime_input_state(hid_tap_state="failed_stopping")
                self._logger.exception(
                    "cleanup: stopping the RC003 HID report tap failed"
                )
                failures.append("RC003 HID report tap did not stop; owner retained")
        else:
            self._set_runtime_input_state(hid_tap_state="stopped")

        keyboard_release_complete = True
        if not self._release_pending_button_keys():
            keyboard_release_complete = False
            failures.append(
                "ordinary button key safety release did not fully deliver; "
                "keyboard trackers retained"
            )
        if not self._release_pending_button_mouse():
            failures.append(
                "ordinary button mouse safety release did not fully deliver; "
                "state retained"
            )

        with self._voice_shortcut.lock:
            if self._voice_shortcut.pending_tokens is not None:
                if self._voice_shortcut.release_pending():
                    self._voice_shortcut.controller.cancel_pending()
                else:
                    keyboard_release_complete = False
                    failures.append(
                        "voice hotkey final safety release did not fully deliver; "
                        "keyboard trackers retained"
                    )

        if keyboard_release_complete:
            with self._raw_input_operation_lock:
                listener = self._hid_listener
                if listener is not None:
                    try:
                        listener.stop()
                        with self._raw_input_lifecycle_lock:
                            if self._hid_listener is listener:
                                self._hid_listener = None
                                self._raw_fallback_tracking_active = False
                        self._set_runtime_input_state(raw_input_state="stopped")
                    except Exception:
                        self._set_runtime_input_state(
                            raw_input_state="failed_stopping"
                        )
                        self._logger.exception(
                            "cleanup: stopping the Raw Input listener failed"
                        )
                        failures.append(
                            "Raw Input listener did not stop; owner retained"
                        )
                else:
                    self._set_runtime_input_state(raw_input_state="stopped")
        else:
            self._set_runtime_input_state(raw_input_state="retained_for_cleanup")

        with self._voice_shortcut.lock:
            self._accept_input_events = False

        with self._direct_hid_lock:
            self._direct_hid_usages.clear()
        with self._input_arbitration_lock:
            self._direct_hid_interception_ready = False
            self._direct_hid_interception_armed = False
            self._direct_hid_handover_waiting_for_neutral = False
            self._cancel_all_raw_fallback_hold_guards_locked()
            self._raw_fallback_buttons_down.clear()
            self._raw_fallback_physical_buttons_down.clear()
            self._raw_fallback_release_debts.clear()
            self._input_rearm_blocked_buttons.clear()
        self._key_detection_suppressed_buttons.clear()
        self._key_detection_suppression_deadlines.clear()
        with self._ordinary_mic_lock:
            self._ordinary_mic_sources_down.clear()
            self._ordinary_mic_late_sources_down.clear()
            self._ordinary_mic_late_source_deadlines.clear()
            self._ordinary_mic_sources_seen.clear()
            self._ordinary_mic_gesture_started_at = 0.0
            self._ordinary_mic_release_guard_until = 0.0
            self._ordinary_mic_gesture_active = False
        with self._key_detection_mic_lock:
            self._reset_key_detection_mic_gesture_locked()
        self._button_combos.reset()
        self._button_gestures.reset()

        physicalizer_state = "stopped"
        if keyboard_release_complete:
            with self._voice_key_physicalizer_operation_lock:
                with self._voice_key_physicalizer_lifecycle_lock:
                    physicalizer = self._voice_key_physicalizer
                if physicalizer is not None:
                    try:
                        physicalizer.stop()
                    except Exception:
                        physicalizer_state = "failed"
                        self._logger.exception(
                            "cleanup: stopping voice key physicalizer failed"
                        )
                        failures.append(
                            "voice key physicalizer did not stop; owner retained"
                        )
                    else:
                        with self._voice_key_physicalizer_lifecycle_lock:
                            if self._voice_key_physicalizer is physicalizer:
                                self._voice_key_physicalizer = None
        else:
            physicalizer_state = "failed"
        with self._voice_key_physicalizer_lifecycle_lock:
            self._voice_key_physicalizer_ready = False
        self._set_runtime_input_state(
            voice_key_physicalizer_state=physicalizer_state
        )

        if failures:
            raise CleanupIncompleteError(
                "cleanup could not release all owned input resources: "
                + "; ".join(failures)
            )

    async def _cleanup_once(self) -> None:
        """Release one BLE/voice/audio attempt without stopping input."""

        with self._runtime_status_lock:
            connection_state = self._runtime_connection_state
            self._runtime_battery_level = None
        if connection_state is bridge_runtime_status.BridgeConnectionState.CONNECTED:
            self._publish_runtime_status(
                bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
            )
        failures: List[str] = []
        with self._voice_shortcut.lock:
            self._accept_ble_events = False

        try:
            with self._voice_shortcut.lock:
                self._voice_audio_start_fallback_pending = False
                self._voice_raw_input_trigger_pending = False
                self._voice_audio_stream_active = False
                self._voice_audio_stop_processed = False
                self._voice_pcm_forwarding_enabled = False
                self._stop_local_voice_capture_locked()
                flush_result = self._voice_audio.flush("cleanup")
                if not flush_result.completed:
                    failures.append(
                        "audio playback queue did not flush; owner retained"
                    )
                self._unsolicited_mic_close_pending = False
                self._finish_voice_mic_gesture()
                self._drop_voice_delayed_press_locked()
                if self._voice_shortcut.pending_tokens is not None:
                    if self._voice_shortcut.release_pending():
                        self._voice_shortcut.controller.cancel_pending()
                    else:
                        failures.append(
                            "voice hotkey safety release did not fully deliver; state retained"
                        )
                else:
                    reset_action = self._voice_shortcut.controller.reset()
                    if reset_action is not None and not self._voice_shortcut.apply(
                        reset_action
                    ):
                        # _apply_voice_action() already logged the specific failure.
                        # reset() already cleared the controller's own pending
                        # state before we knew delivery would fail - restore it so
                        # a held shortcut isn't recorded as released while it may
                        # still be physically down (XRBM-019 review round 1 P1 #4).
                        self._voice_shortcut.controller.restore_pending(reset_action)
                        failures.append(
                            "voice hotkey release did not fully deliver; state retained"
                        )
                self._voice_focus_before = None
                self._voice_focus_provider = ""
                self._voice_focus_submit_method = ""
                self._finish_voice_diagnostic_attempt(
                    "unknown",
                    reason="connection_cleanup",
                )
        except Exception:
            self._logger.exception("cleanup: releasing the voice hotkey failed")
            failures.append("voice hotkey cleanup failed; state retained")

        if not self._release_pending_button_inputs():
            failures.append(
                "ordinary button input safety release did not fully deliver; "
                "state retained"
            )

        if self._ble_session is not None:
            try:
                await self._ble_session.close()
                self._ble_session = None
            except Exception:
                self._logger.exception("cleanup: closing the BLE session failed")
                failures.append("BLE session did not fully close; owner retained")
                # self._ble_session is intentionally NOT cleared here either.

        playback_writer_stopped = True
        if self._voice_audio.writer is not None:
            playback_writer_stopped = self._voice_audio.stop_writer()
            if not playback_writer_stopped:
                failures.append(
                    "audio playback writer did not stop; owner retained"
                )

        if self._voice_audio.sink is not None and playback_writer_stopped:
            try:
                self._voice_audio.close_sink()
            except Exception:
                self._logger.exception("cleanup: closing audio playback failed")
                failures.append("audio playback did not fully close; owner retained")
                # self._voice_audio.sink is intentionally NOT cleared here either -
                # it owns a PortAudio stream; discarding the reference would
                # hide an incompletely closed resource and let a reconnect
                # open a second sink over it (XRBM-019 review round 1 P1
                # #5).

        if not failures:
            with self._voice_shortcut.lock:
                self._apply_pending_voice_settings_if_idle_locked()

        self._logger.info("cleanup: attempted release of hotkey state and BLE/HID/audio")

        if failures:
            raise CleanupIncompleteError(
                "cleanup could not release all owned resources: " + "; ".join(failures)
            )

    # -- disconnect / error callbacks: hand off to the supervisor ----------

    def _on_disconnected(self, *, _ble_generation: Optional[int] = None) -> None:
        if not self._accepts_ble_callback(_ble_generation):
            return
        self._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
        )
        self._logger.info("BLE reported disconnected; requesting reconnect")
        self._supervisor.request_reconnect()

    def _on_session_error(
        self,
        exc: BaseException,
        *,
        _ble_generation: Optional[int] = None,
    ) -> None:
        if not self._accepts_ble_callback(_ble_generation):
            return
        self._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.RETRY_WAIT
        )
        self._logger.info("ATVV protocol error, requesting reconnect: %s", exc)
        self._supervisor.request_reconnect()

    def _on_battery_level(
        self,
        level: int,
        *,
        _ble_generation: Optional[int] = None,
    ) -> None:
        if not self._accepts_ble_callback(_ble_generation):
            return
        self._set_runtime_battery_level(level)

    def _accepts_ble_callback(self, generation: Optional[int]) -> bool:
        """Reject callbacks left behind by an older BLE session."""

        return self._accept_ble_events and (
            generation is None or generation == self._ble_callback_generation
        )

    def _primary_button_action(self, button_id: str) -> key_mapping.ButtonAction:
        return key_mapping.button_action_for(
            self._bindings,
            button_id,
            key_mapping.ButtonTrigger.SINGLE_CLICK,
        )

    def _configured_voice_buttons(self) -> List[str]:
        button_bindings = self._bindings.get("bindings", {})
        if not isinstance(button_bindings, dict):
            return []
        result = []
        for button_id in button_bindings:
            action = self._primary_button_action(button_id)
            if key_mapping.is_supported_voice_action(action):
                result.append(button_id)
        return result

    def _is_voice_primary_button(
        self,
        button_id: str,
        action: Optional[key_mapping.ButtonAction] = None,
    ) -> bool:
        action = action or self._primary_button_action(button_id)
        return key_mapping.is_supported_voice_action(action)

    def voice_slot_for_button(
        self,
        button_id: str,
        action: Optional[key_mapping.ButtonAction] = None,
    ) -> Optional[int]:
        action = action or self._primary_button_action(button_id)
        return key_mapping.voice_slot_for_action(action)

    def _voice_slot_inject_mode(self, slot: Optional[int]) -> Optional[str]:
        """Return the inject backend the button bound to ``slot`` selected.

        RC003 voice slots replay the shortcut recorded for them, but the button
        that triggers the slot still owns its own per-button injection backend,
        exactly like any other key.
        """

        if slot is None:
            return None
        fallback: Optional[str] = None
        for button_id in self._configured_voice_buttons():
            action = self._primary_button_action(button_id)
            mode = injection_backend.normalize_inject_mode(action.inject_mode)
            action_slot = key_mapping.voice_slot_for_action(action)
            if action_slot == slot:
                return mode
            if action_slot == 1:
                fallback = mode
            elif fallback is None:
                fallback = mode
        # 一个话筒键轮换多个语音槽时（短按切换），只有语音一真正绑在话筒键
        # 上，语音二/三没有自己的按钮。此时必须沿用触发按钮的注入方式，
        # 否则会退回 SendInput，右 Alt 这类需要驱动级注入的宿主就收不到。
        return fallback

    def _voice_slot_uses_interception(self, slot: Optional[int]) -> bool:
        """Whether ``slot`` is delivered by an available Interception driver."""

        return (
            self._voice_slot_inject_mode(slot)
            == injection_backend.INJECT_MODE_INTERCEPTION
            and injection_backend.interception_available()
        )

    def _voice_hotkey_text_for_slot(self, slot: int) -> str:
        """Resolve one voice slot's hold shortcut from the saved config.

        RC003 ignores the voice-program selection entirely: the slot only ever
        replays the shortcut the user recorded for it, so an unset slot resolves
        to an empty string and the mapping is skipped instead of firing a
        leftover provider default (``ralt``).
        """

        return config.voice_slot_hotkey(self._config, slot)

    def _voice_cycle_enabled(self) -> bool:
        """Whether a short press cycles through the configured voice keys."""

        return config.voice_cycle_short_press(self._config)

    def _voice_other_key_switch_enabled(self) -> bool:
        """Whether other buttons may switch the active voice mode."""

        return config.voice_other_key_switch(self._config)

    def _voice_cycle_follows_mic(self) -> bool:
        """Whether the microphone uses the cycled voice key instead of its own.

        Both switching entries (mic short press and another mapped button)
        move the same cursor, so as soon as either one is enabled the mic must
        follow it - otherwise a switch would appear to do nothing.
        """

        return self._voice_cycle_enabled() or self._voice_other_key_switch_enabled()

    def _voice_cycle_slots(self) -> List[int]:
        """Every enabled voice key id, in the order the user listed them."""

        return [
            int(entry["id"])
            for entry in config.enabled_voice_key_configs(self._config)
        ]

    def _active_voice_slot(self) -> int:
        """The voice key a physical mic press currently drives.

        开启短按切换模式后，话筒键不再看它自己绑的是哪一条，而是跟着循环
        游标走；关闭时保持原样，即话筒键绑定的那个语音槽。
        """

        if not self._voice_cycle_follows_mic():
            return (
                key_mapping.voice_slot_for_action(
                    self._primary_button_action("mic")
                )
                or 1
            )
        slots = self._voice_cycle_slots()
        if not slots:
            return 1
        current = self._voice_cycle_slot
        if current is not None and current in slots:
            return current
        # 循环起点是按键页给麦克风指定的那条语音键，之后按配置列表的顺序
        # 往下走；列表重新排序后顺序自动跟着变。
        mapped = key_mapping.voice_slot_for_action(
            self._primary_button_action("mic")
        )
        start = mapped if mapped in slots else slots[0]
        self._voice_cycle_slot = start
        return start

    def _advance_voice_cycle_slot_locked(self) -> int:
        """Move to the next configured voice key and return it."""

        slots = self._voice_cycle_slots()
        if not slots:
            return self._active_voice_slot()
        current = self._active_voice_slot()
        try:
            index = slots.index(current)
        except ValueError:
            index = -1
        next_slot = slots[(index + 1) % len(slots)]
        self._voice_cycle_slot = next_slot
        return next_slot

    def _voice_slot_uses_local_microphone(self, slot: Optional[int]) -> bool:
        """Whether one voice key takes its audio from this PC's microphone."""

        if slot is None:
            return False
        return (
            config.voice_input_source(self._config, slot)
            == config.VOICE_INPUT_SOURCE_LOCAL
        )

    def _voice_slot_tokens(self, slot: Optional[int]) -> Optional[Tuple[str, ...]]:
        """The key tokens a voice slot replays, or None when it is unset."""

        if slot is None:
            return None
        text = self._voice_hotkey_text_for_slot(int(slot))
        if not text:
            return None
        try:
            spec = hotkey.HotkeySpec.parse(text)
        except hotkey.HotkeyParseError:
            return None
        return (*spec.modifiers, spec.key)

    def _force_release_plugin_voice_tokens_locked(self, reason: str) -> None:
        """Send the missing key-up for a plugin voice shortcut.

        The plugin path never opens the virtual audio device, so there is no
        audio-stop event that could complete the press. Everything here is
        best effort: a stray key-up is harmless, a stuck right Alt is not.
        """

        tokens = self._plugin_voice_injected_tokens
        self._cancel_plugin_voice_hold_pending_locked()
        self._end_plugin_voice_audio_locked(reason)
        self._cancel_plugin_voice_capture_locked(reason)
        if not tokens:
            return
        self._plugin_voice_injected_tokens = None
        try:
            win32_input.send_voice_key_combo_up(tokens)
            self._logger.info("plugin voice key force released on %s", reason)
        except BaseException as exc:  # noqa: BLE001 - never trap input
            self._logger.warning(
                "plugin voice key force release failed on %s: error_type=%s",
                reason,
                type(exc).__name__,
            )

    def _cancel_plugin_voice_hold_pending_locked(self) -> None:
        """Drop a plugin press that has not reached the long-press threshold."""

        timer = self._plugin_voice_press_timer
        self._plugin_voice_press_timer = None
        self._plugin_voice_press_token = None
        self._plugin_voice_press_slot = None
        if timer is not None:
            try:
                timer.stop()
            except BaseException:  # noqa: BLE001 - timer teardown is cosmetic
                pass

    def _schedule_plugin_voice_hold_locked(self, voice_slot: int) -> bool:
        """按满长按阈值就开插件会话；短按只切模式。

        按下先开话筒、把 PCM 缓起来（见
        ``_arm_plugin_voice_capture_locked``），到点才通知 Vokie，并把缓冲
        一起补发，所以开口第一个字不会丢。没到点就松手走短按，Vokie 完全
        不被惊动。
        """

        self._cancel_plugin_voice_hold_pending_locked()
        token = object()
        self._plugin_voice_press_token = token
        self._plugin_voice_press_slot = voice_slot
        delay_seconds = _PLUGIN_VOICE_HOLD_SECONDS
        try:
            timer = threading.Timer(
                delay_seconds,
                lambda pending=token: self._plugin_voice_hold_elapsed(pending),
            )
            timer.daemon = True
            self._plugin_voice_press_timer = timer
            timer.start()
        except BaseException as exc:  # noqa: BLE001
            self._cancel_plugin_voice_hold_pending_locked()
            self._logger.warning(
                "plugin voice hold timer unavailable: error_type=%s",
                type(exc).__name__,
            )
            return False
        self._logger.info(
            "plugin voice press armed: slot=%s hold_s=%s",
            voice_slot,
            delay_seconds,
        )
        return True

    def _arm_plugin_voice_capture_locked(self, slot: int) -> bool:
        """按下插件键就开遥控器话筒，把 PCM 先缓起来。"""

        self._reset_plugin_voice_capture_locked()
        self._plugin_voice_mic_opened = False
        bridge = vokie_bridge.active_bridge()
        if bridge is None or not bridge.connected():
            return False
        session = self._ble_session
        if session is None:
            return False
        self._plugin_voice_capture_pending = True
        self._plugin_voice_capture_slot = int(slot)
        try:
            session.send_mic_open_threadsafe()
        except BaseException as exc:  # noqa: BLE001 - never trap input
            self._reset_plugin_voice_capture_locked()
            self._logger.warning(
                "plugin voice capture MIC_OPEN failed: error_type=%s",
                type(exc).__name__,
            )
            return False
        self._plugin_voice_mic_opened = True
        self._logger.info("plugin voice capture armed: slot=%s", slot)
        return True

    def _reset_plugin_voice_capture_locked(self) -> None:
        """清空预录缓冲与待定状态，不碰话筒与插件会话。"""

        self._plugin_voice_capture_pending = False
        self._plugin_voice_capture_slot = None
        self._plugin_voice_capture_frames = []
        self._plugin_voice_capture_samples = 0

    def _cancel_plugin_voice_capture_locked(
        self, reason: str, *, close_mic: bool = True
    ) -> None:
        """放弃这次预录：短按没到长按阈值时，Vokie 完全不该被惊动。"""

        if not self._plugin_voice_capture_pending:
            return
        self._reset_plugin_voice_capture_locked()
        # 松手了就把长按定时器一起撤掉，否则它到点还会把 Vokie 叫起来——
        # 连按切模式时每次都会在松手后冒出一个会话，就是这个原因。
        self._cancel_plugin_voice_hold_pending_locked()
        if self._plugin_voice_audio_active:
            return
        self._voice_pcm_forwarding_enabled = False
        opened = self._plugin_voice_mic_opened
        self._plugin_voice_mic_opened = False
        self._logger.info("plugin voice capture cancelled on %s", reason)
        if not close_mic or not opened:
            return
        session = self._ble_session
        if session is None:
            return
        try:
            session.send_mic_close_threadsafe()
        except BaseException as exc:  # noqa: BLE001 - never trap input
            self._logger.warning(
                "plugin voice capture MIC_CLOSE failed: error_type=%s",
                type(exc).__name__,
            )

    def _take_plugin_voice_capture_frames_locked(self) -> List[array.array]:
        """取走预录 PCM 并清空缓冲；调用方负责补发。"""

        frames = self._plugin_voice_capture_frames
        self._reset_plugin_voice_capture_locked()
        return frames

    def _handle_plugin_voice_capture_frame_locked(
        self, samples, generation: Optional[int]
    ) -> None:
        """缓存一帧 PCM，按满长按阈值后随会话一起补发。"""

        if not self._accepts_ble_callback(generation):
            return
        frame = array.array("h", samples)
        if not frame:
            return
        self._plugin_voice_capture_frames.append(frame)
        self._plugin_voice_capture_samples += len(frame)
        limit = int(16000 * _PLUGIN_VOICE_PREROLL_SECONDS)
        while (
            self._plugin_voice_capture_samples > limit
            and self._plugin_voice_capture_frames
        ):
            dropped = self._plugin_voice_capture_frames.pop(0)
            self._plugin_voice_capture_samples -= len(dropped)

    def _start_plugin_voice_session_locked(self, slot: Optional[int]) -> bool:
        """真正开始插件会话；说话起始与兜底定时器共用这条路径。"""

        if self._plugin_voice_audio_active:
            return True
        if slot is None or not self._voice_slot_is_plugin(slot):
            return False
        self._cancel_plugin_voice_hold_pending_locked()
        self._extra_voice_slot_active = None
        self._ensure_voice_diagnostic_attempt()
        if self._begin_plugin_voice_audio_locked(slot):
            # 插件已连上：遥控器 PCM 直接经回环桥送进 Vokie，不用模拟热键。
            self._set_runtime_voice_active(True)
            self._set_runtime_voice_result(
                bridge_runtime_status.VOICE_RUNTIME_ACTIVE
            )
            self._logger.info("plugin voice audio hold started: slot=%s", slot)
            return True
        self._cancel_plugin_voice_capture_locked(
            "plugin audio unavailable", close_mic=True
        )
        # 回退注入必须用这条语音键自己的快捷键：会话里的 hotkey 可能是上一次
        # 按压留下的别的槽位（比如语音输入），直接重放就会变成那个模式。
        tokens = self._voice_slot_tokens(slot)
        if not self._press_voice_shortcut_edge_locked(
            slot, tokens_override=tokens
        ):
            self._logger.warning("plugin voice hold replay failed: slot=%s", slot)
            self._finish_voice_diagnostic_attempt(
                "failed_before_send", reason="hotkey_delivery_failed"
            )
            self._set_runtime_voice_result(
                bridge_runtime_status.VOICE_RUNTIME_HOST_START_FAILED
            )
            self._set_runtime_voice_active(False)
            return False
        self._plugin_voice_injected_tokens = (
            self._voice_shortcut.pending_tokens or tokens
        )
        self._set_runtime_voice_active(True)
        self._set_runtime_voice_result(bridge_runtime_status.VOICE_RUNTIME_ACTIVE)
        self._hide_voice_mode_toast()
        self._logger.info("plugin voice hold started: slot=%s", slot)
        return True

    def _plugin_voice_hold_elapsed(self, token: object) -> None:
        """按满长按阈值就开插件会话；短按的定时器早已被撤掉。"""

        with self._voice_shortcut.lock:
            if self._plugin_voice_press_token is not token:
                return
            slot = self._plugin_voice_press_slot
            self._cancel_plugin_voice_hold_pending_locked()
            if slot is None or not self._voice_slot_is_plugin(slot):
                return
            self._start_plugin_voice_session_locked(slot)

    def _begin_plugin_voice_audio_locked(self, slot: int) -> bool:
        """Hand the remote microphone to the connected plugin.

        The plugin owns the transcription session, so MoteDeck only has to
        name the mode, open the remote microphone and then let
        ``_on_pcm_frame`` push samples across the loopback bridge. Returns
        False when no plugin is connected, which keeps the old hotkey replay
        as the fallback.
        """

        bridge = vokie_bridge.active_bridge()
        if bridge is None or not bridge.connected():
            return False
        self._watch_plugin_voice_input_box(bridge)
        mode_name = config.voice_key_name(self._config, int(slot))
        if not bridge.send_audio_start(mode_name):
            return False
        # 预录的 PCM 一并补发：插件在 session_accepted 之前会自己攒着，
        # 所以说话的第一个字不会被丢掉。
        buffered = self._take_plugin_voice_capture_frames_locked()
        self._plugin_voice_audio_active = True
        self._plugin_voice_started_this_press = True
        self._voice_pcm_forwarding_enabled = True
        for frame in buffered:
            if not bridge.send_audio(frame.tobytes()):
                self._voice_pcm_forwarding_enabled = False
                break
        session = self._ble_session
        if session is not None and not self._plugin_voice_mic_opened:
            try:
                session.send_mic_open_threadsafe()
            except BaseException as exc:  # noqa: BLE001 - never trap input
                self._logger.warning(
                    "plugin voice MIC_OPEN failed: error_type=%s",
                    type(exc).__name__,
                )
        self._plugin_voice_mic_opened = True
        return True

    def _end_plugin_voice_audio_locked(self, reason: str) -> None:
        """Close a plugin voice session that this app started."""

        if not self._plugin_voice_audio_active:
            return
        self._plugin_voice_audio_active = False
        self._plugin_voice_audio_stream = False
        self._voice_pcm_forwarding_enabled = False
        # 遥控器松手自己会停流，不用再补 MIC_CLOSE。
        self._plugin_voice_mic_opened = False
        bridge = vokie_bridge.active_bridge()
        if bridge is not None:
            bridge.send_audio_stop()
        self._logger.info("plugin voice audio stopped on %s", reason)

    def _watch_plugin_voice_input_box(self, bridge) -> None:
        """等 Vokie 真正接下会话（输入框弹出）时再收掉模式气泡。"""

        if self._plugin_voice_toast_bridge is bridge:
            return
        try:
            bridge.sessionAccepted.connect(self._hide_voice_mode_toast)
        except BaseException as exc:  # noqa: BLE001 - the bubble is best effort
            self._logger.warning(
                "voice mode toast watch failed: error_type=%s",
                type(exc).__name__,
            )
            return
        self._plugin_voice_toast_bridge = bridge

    def _hide_voice_mode_toast(self) -> None:
        """收起模式气泡，别压在插件自己的输入框上。"""

        try:
            self._voice_mode_toast_watch.cancel()
        except BaseException as exc:  # noqa: BLE001 - the bubble is best effort
            self._logger.warning(
                "voice mode toast watch stop failed: error_type=%s",
                type(exc).__name__,
            )
        try:
            self._voice_mode_toast.hide()
        except BaseException as exc:  # noqa: BLE001 - the bubble is best effort
            self._logger.warning(
                "voice mode toast hide failed: error_type=%s",
                type(exc).__name__,
            )

    def _voice_slot_is_plugin(self, slot: Optional[int]) -> bool:
        """Whether this voice key belongs to an external plugin (Vokie).

        Plugin rows carry their own host shortcut and capture audio themselves,
        so the only thing MoteDeck has to do for them is replay that shortcut.
        """

        if slot is None:
            return False
        entry = config.voice_key_config(self._config, slot)
        return bool(entry and str(entry.get("plugin") or "").strip())

    def _plugin_voice_session_active(self) -> bool:
        """True while a press belongs to a plugin-owned voice key."""

        return self._voice_slot_is_plugin(
            self._extra_voice_slot_active
        ) or self._voice_slot_is_plugin(self._active_voice_slot())

    def _voice_input_source(self) -> str:
        return config.voice_input_source(self._config, self._active_voice_slot())

    def _uses_local_voice_microphone(self) -> bool:
        """模式2: the audio comes from a microphone plugged into this PC."""

        return self._voice_input_source() == config.VOICE_INPUT_SOURCE_LOCAL

    def _show_voice_mode_toast(self, slot: int) -> None:
        """Pop the mode bubble under the monitor the cursor is on."""

        theme = config.voice_cycle_toast_theme(self._config)
        if theme == config.VOICE_CYCLE_TOAST_OFF:
            return
        name = config.voice_key_name(self._config, slot)
        if not name:
            return
        try:
            self._voice_mode_toast.show(
                name, theme, config.voice_cycle_toast_offset(self._config)
            )
        except BaseException as exc:  # noqa: BLE001 - the bubble is best effort
            self._logger.warning(
                "voice mode toast failed: error_type=%s",
                type(exc).__name__,
            )
            return
        # 第三方输入法弹出面板时会冒出新窗口，看到了就提前收掉气泡。
        try:
            self._voice_mode_toast_watch.watch(self._hide_voice_mode_toast)
        except BaseException as exc:  # noqa: BLE001 - the bubble is best effort
            self._logger.warning(
                "voice mode toast watch failed: error_type=%s",
                type(exc).__name__,
            )

    def _start_local_voice_capture_locked(self) -> bool:
        """模式2：会话开始时打开本机麦克风。

        本机麦克风是常开的，所以短按锁存期间音频不会断——这正是模式1
        拿不到的东西。
        """

        if not self._uses_local_voice_microphone():
            return False
        existing = self._local_voice_capture
        if existing is not None and getattr(existing, "running", False):
            return True
        device = config.voice_local_input_device(
            self._config, self._active_voice_slot()
        )
        if not device:
            self._logger.warning(
                "local voice microphone not started: no recording device selected"
            )
            self._set_runtime_voice_result(
                bridge_runtime_status.VOICE_RUNTIME_OUTPUT_OPEN_FAILED
            )
            return False
        capture = local_microphone_windows.LocalMicrophoneCapture(
            device_name=device,
            on_frame=self._on_pcm_frame,
            logger=self._logger,
        )
        if not capture.start():
            self._local_voice_capture = None
            self._set_runtime_voice_result(
                bridge_runtime_status.VOICE_RUNTIME_OUTPUT_OPEN_FAILED
            )
            return False
        self._local_voice_capture = capture
        return True

    def _stop_local_voice_capture_locked(self) -> None:
        capture = self._local_voice_capture
        self._local_voice_capture = None
        if capture is None:
            return
        try:
            capture.stop()
        except BaseException as exc:  # noqa: BLE001 - 关闭失败不再重试
            self._logger.warning(
                "local voice microphone stop failed: error_type=%s",
                type(exc).__name__,
            )

    def _prepare_voice_mapping_locked(
        self,
        button_id: str,
        action: key_mapping.ButtonAction,
        *,
        allow_latched_stream_takeover: bool = False,
    ) -> bool:
        if not self._direct_hid_interception_ready:
            self._logger.warning(
                "voice mapping ignored: RC003 HID interception is not ready"
            )
            return False
        if not self._is_voice_primary_button(button_id, action):
            self._logger.warning(
                "voice mapping ignored: button is not bound to a voice slot; "
                "button=%s configured=%s",
                button_id,
                self._configured_voice_buttons(),
            )
            return False
        if button_id == "mic" and self._voice_cycle_follows_mic():
            # 短按切换模式 / 其他按键切换模式下，话筒键跟着循环游标走，
            # 而不是它自己绑的那一条。
            slot = self._active_voice_slot()
        else:
            slot = key_mapping.voice_slot_for_action(action)
        if slot is None:
            self._logger.warning(
                "voice mapping ignored: button is not bound to a voice slot; "
                "button=%s configured=%s",
                button_id,
                self._configured_voice_buttons(),
            )
            return False
        if not config.voice_key_enabled(self._config, slot):
            self._logger.info(
                "voice mapping ignored: voice key %s is switched off", slot
            )
            return False
        hotkey_text = self._voice_hotkey_text_for_slot(slot)
        if not hotkey_text and button_id == "mic" and self._voice_cycle_follows_mic():
            # 循环到的这条语音键还没录快捷键：清空当前热键即可。注入空
            # token 是无害的空转，切换本身仍然照常工作。
            if self._voice_shortcut.hotkey is not None:
                if not self._voice_settings_idle_locked() and not (
                    allow_latched_stream_takeover
                    and self._latched_voice_stream_only_locked()
                ):
                    self._logger.info(
                        "voice mapping change deferred: active session owns hotkey=%s",
                        self._voice_shortcut.hotkey.serialize(),
                    )
                    return False
                self._clear_voice_hotkey_locked()
            return True
        try:
            voice_hotkey = hotkey.HotkeySpec.parse(hotkey_text)
            tokens = tuple(voice_hotkey.modifiers) + (voice_hotkey.key,)
            resolved_vk_codes = win32_keys.resolve_vk_codes(tokens)
        except (hotkey.HotkeyParseError, win32_keys.UnknownKeyTokenError) as exc:
            self._logger.warning(
                "voice mapping ignored: invalid voice shortcut: %s",
                exc,
            )
            return False
        if not win32_input.can_begin_tracked_hold(tokens):
            self._logger.warning(
                "voice mapping ignored: physical keyboard tracking is unavailable "
                "for shortcut=%s",
                voice_hotkey.serialize(),
            )
            return False
        if (
            win32_keys.VK_CODES["ralt"] in resolved_vk_codes
            and not self._voice_key_physicalizer_ready
            and not self._voice_slot_uses_interception(slot)
        ):
            # The Interception driver injects below the low-level hook, so the
            # stroke carries no LLKHF_INJECTED flag and never needs the
            # physicalizer marker; requiring one would block the slot outright.
            self._logger.warning(
                "voice mapping ignored: required voice key physicalizer is unavailable"
            )
            return False
        requested = voice_hotkey.serialize()
        current = (
            self._voice_shortcut.hotkey.serialize()
            if self._voice_shortcut.hotkey is not None
            else ""
        )
        if requested != current:
            if not self._voice_settings_idle_locked() and not (
                allow_latched_stream_takeover
                and self._latched_voice_stream_only_locked()
            ):
                self._logger.info(
                    "voice mapping change deferred: active session owns hotkey=%s",
                    current,
                )
                return False
            self._apply_voice_settings_locked(
                voice_hotkey, update_config=slot == 1
            )
        return True

    def _press_voice_shortcut_edge_locked(
        self,
        voice_slot: Optional[int],
        *,
        tokens_override: Optional[Tuple[str, ...]] = None,
    ) -> bool:
        # 还有一次延迟按下没落地：先把它丢掉，避免这次边沿之后多出一个
        # 没人松开的按键。
        self._drop_voice_delayed_press_locked()
        action = self._voice_shortcut.controller.on_mic_button_pressed()
        # 输入法常在"按下"这一瞬间就把面板翻成可见，而气泡要到松手才显示，
        # 所以可见窗口的基线必须在注入之前取，否则面板会被算进基线里。
        self._voice_mode_toast_watch.arm()
        delivered = self._voice_shortcut.apply(
            action,
            voice_slot=voice_slot,
            inject_mode=self._voice_slot_inject_mode(voice_slot),
            tokens_override=tokens_override,
        )
        if not delivered:
            self._voice_shortcut.controller.cancel_pending()
            return False
        if not self._arm_voice_hold_watchdog_locked():
            self._force_voice_hold_release_locked(
                "voice shortcut edge safety timer unavailable"
            )
            return False
        return True

    def _voice_shortcut_press_delay_enabled(self, voice_slot: Optional[int]) -> bool:
        """Whether this press should wait out the tap window before sending.

        只用于模式1（热键注入）的语音槽：这些槽的短按开关现在表示"延迟发送"。
        模式2 用本机麦克风，插件槽由 Vokie 自己收面板，都不延迟。
        """

        if self._voice_slot_uses_local_microphone(voice_slot):
            return False
        if self._voice_slot_is_plugin(voice_slot):
            return False
        return config.voice_key_short_press_resend(self._config, voice_slot)

    def _arm_voice_shortcut_press_delay_locked(
        self, action: object, voice_slot: Optional[int]
    ) -> bool:
        """把这一次按键整记推迟到窗口之后：窗口内松手就什么也不发。

        控制器在按下那一刻已经进入 holding，所以这里只暂存动作本身；定时器到
        点才真正注入 KEY_DOWN。返回 False 表示定时器起不来，调用方应立刻按原样
        下发，功能不受影响。调用方持有 ``_voice_shortcut.lock``。
        """

        token = object()
        self._cancel_voice_shortcut_press_delay_locked()
        self._voice_delayed_press_action = action
        self._voice_delayed_press_slot = voice_slot
        try:
            timer = self._voice_delayed_press_timer_factory(
                _VOICE_SHORTCUT_PRESS_DELAY_SECONDS,
                lambda token=token: self._voice_shortcut_press_delay_elapsed(token),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._voice_delayed_press_token = token
            self._voice_delayed_press_timer = timer
            timer.start()
        except BaseException as exc:  # noqa: BLE001 - fall back to immediate send
            self._cancel_voice_shortcut_press_delay_locked()
            self._logger.warning(
                "voice shortcut press delay timer failed: error_type=%s",
                type(exc).__name__,
            )
            return False
        return True

    def _cancel_voice_shortcut_press_delay_locked(self) -> None:
        timer = self._voice_delayed_press_timer
        self._voice_delayed_press_timer = None
        self._voice_delayed_press_token = None
        self._voice_delayed_press_action = None
        self._voice_delayed_press_slot = None
        if timer is not None:
            cancel = getattr(timer, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except BaseException:  # noqa: BLE001 - token is already invalid
                    pass

    def _drop_voice_delayed_press_locked(self) -> None:
        """丢掉还没落地的那一次延迟按下：一个键都不发。

        短按（窗口内松手）、重新按下、清理和强制释放都走这里；已经落地的
        那一次由正常的松开路径收尾，不在这里处理。
        """

        if self._voice_delayed_press_token is None:
            return
        self._cancel_voice_shortcut_press_delay_locked()
        self._voice_shortcut.controller.cancel_pending()

    def _voice_shortcut_press_delay_elapsed(self, token: object) -> None:
        """窗口走完用户还按着：这才下发这一次按键。"""

        with self._voice_shortcut.lock:
            if token is not self._voice_delayed_press_token:
                return
            action = self._voice_delayed_press_action
            voice_slot = self._voice_delayed_press_slot
            self._voice_delayed_press_token = None
            self._voice_delayed_press_timer = None
            self._voice_delayed_press_action = None
            self._voice_delayed_press_slot = None
            if action is None:
                return
            if not self._voice_shortcut.apply(
                action,
                voice_slot=voice_slot,
                inject_mode=self._voice_slot_inject_mode(voice_slot),
            ):
                self._voice_focus_before = None
                self._voice_focus_provider = ""
                self._voice_focus_submit_method = ""
                self._voice_shortcut.controller.cancel_pending()
                # 设备话筒在按下时已经开了：这条路径必须把它一起收干净。
                self._force_voice_hold_release_locked(
                    "delayed voice shortcut press delivery failed",
                    reconnect=False,
                )
                return
            self._diagnostic_trace.emit(
                "hotkey_sent",
                **self._diagnostic_trace.current_context(),
                backend=_VOICE_HOTKEY_BACKEND_MARKED,
            )
            self._logger.info(
                "voice shortcut press delivered after delay: slot=%s", voice_slot
            )

    def _handle_voice_short_tap_locked(self, reason: str) -> None:
        """One physical mic short press: switch the active voice mode.

        短按（延迟窗口内松手）一个键都不发，所以这里只推进循环游标并提示
        气泡。
        """

        if not self._voice_cycle_enabled():
            return
        self._end_latched_voice_session_for_cycle_locked()
        slot = self._advance_voice_cycle_slot_locked()
        self._show_voice_mode_toast(slot)
        self._logger.info(
            "voice mode switched by short press: slot=%s reason=%s",
            slot,
            reason,
        )

    def _switch_voice_mode_by_button(
        self, button_id: str, trigger: button_gesture.ButtonTrigger
    ) -> None:
        """Ordinary button mapped to “语音模式切换”: move the mic to the next key.

        It shares the cursor with the mic's own short press, so both entries
        always continue from the mode currently in use.
        """

        if not self._voice_other_key_switch_enabled():
            self._logger.info(
                "voice mode switch ignored: other-key switch is off; "
                "button=%s trigger=%s",
                button_id,
                trigger.value,
            )
            return
        with self._voice_shortcut.lock:
            if not self._voice_cycle_slots():
                return
            self._end_latched_voice_session_for_cycle_locked()
            slot = self._advance_voice_cycle_slot_locked()
            self._show_voice_mode_toast(slot)
        self._logger.info(
            "voice mode switched by button: slot=%s button=%s trigger=%s",
            slot,
            button_id,
            trigger.value,
        )

    def _end_latched_voice_session_for_cycle_locked(self) -> None:
        """Close a latched mic session because the user switched voice mode.

        唯一那一次按键在按下时就排好延迟下发、松手时收尾，所以这里不再发
        边沿，只把 PCM 转发、本机采集和设备话筒收干净，再落掉锁存。
        """

        if not self._voice_gesture.latched:
            return
        self._voice_pcm_forwarding_enabled = False
        self._set_runtime_voice_active(False)
        self._stop_local_voice_capture_locked()
        try:
            if self._ble_session is not None:
                self._ble_session.send_mic_close_threadsafe()
        except BaseException as exc:  # noqa: BLE001 - never trap input
            self._logger.warning(
                "voice mode switch MIC_CLOSE failed: error_type=%s",
                type(exc).__name__,
            )
        self._voice_mic_gesture_sources_down.clear()
        self._finish_voice_mic_gesture()
        self._logger.info("voice mic tap latch released by mode switch")

    def _handle_extra_voice_key_edge(
        self,
        button_id: str,
        action: key_mapping.ButtonAction,
        is_pressed: bool,
    ) -> None:
        """Run the full voice pipeline for a voice slot on a non-microphone key.

        Extra voice slots (语音二/语音三) bound to a remote button have to open
        the RC003 device microphone themselves: unlike the physical mic button
        the device does not start streaming on its own for these keys, so the
        host requests MIC_OPEN and enables PCM forwarding in addition to
        replaying the slot's configured host shortcut. The press/release
        behaviour therefore matches the physical mic button, which is what lets
        a single remote drive several external voice programs.
        """

        slot = key_mapping.voice_slot_for_action(action)
        if slot is None:
            return
        if not self._voice_slot_uses_local_microphone(slot):
            # 模式1 只认遥控器话筒键：其它按键上的语音槽不生效，界面也会
            # 提示用户改用模式2。
            self._logger.info(
                "extra voice key ignored in remote-microphone mode: "
                "button=%s slot=%s",
                button_id,
                slot,
            )
            return
        gesture = self._extra_voice_gestures.get(slot)
        if gesture is None:
            gesture = voice_gesture.VoiceGestureLatch(
                tap_max_seconds=_VOICE_TAP_MAX_SECONDS
            )
            self._extra_voice_gestures[slot] = gesture
        with self._voice_shortcut.lock:
            if is_pressed:
                self._voice_shortcut_physical_sources_down.add(button_id)
            else:
                self._voice_shortcut_physical_sources_down.discard(button_id)
            if self._extra_voice_slot_close_pending == slot:
                if not is_pressed:
                    self._voice_shortcut_physical_sources_down.discard(button_id)
                    self._extra_voice_slot_close_pending = None
                    self._release_voice_shortcut_edge_locked(
                        "extra voice key second tap release"
                    )
                return
            if gesture.latched:
                if is_pressed:
                    if gesture.on_press() == voice_gesture.VoiceGestureDecision.CLOSE:
                        self._extra_voice_slot_close_pending = slot
                        self._press_voice_shortcut_edge_locked(slot)
                        self._voice_pcm_forwarding_enabled = False
                        self._set_runtime_voice_active(False)
                        self._stop_local_voice_capture_locked()
                        if (
                            self._ble_session is not None
                            and not self._voice_slot_uses_local_microphone(slot)
                        ):
                            self._ble_session.send_mic_close_threadsafe()
                        self._logger.info(
                            "extra voice key tap latch released: button=%s slot=%s",
                            button_id,
                            slot,
                        )
                elif self._extra_voice_slot_active == slot:
                    self._extra_voice_slot_active = None
                    self._release_voice_shortcut_edge_locked(
                        "extra voice key second tap release"
                    )
                return
            if is_pressed:
                if self._extra_voice_slot_active == slot:
                    return
                if self._extra_voice_slot_active is not None:
                    self._logger.info(
                        "extra voice key ignored: another extra slot is active; "
                        "button=%s slot=%s active_slot=%s",
                        button_id,
                        slot,
                        self._extra_voice_slot_active,
                    )
                    return
                if self._voice_shortcut.controller.active:
                    if self._plugin_voice_injected_tokens is not None:
                        # 同话筒键：上次插件按键的抬起丢了，先补抬起再重来。
                        self._logger.info(
                            "plugin voice session stale; force releasing before "
                            "extra voice press"
                        )
                        self._force_release_plugin_voice_tokens_locked(
                            "stale plugin session on extra voice press"
                        )
                        self._voice_shortcut.controller.reset()
                        gesture.reset()
                    else:
                        self._logger.info(
                            "extra voice key ignored: hold session already active; "
                            "button=%s slot=%s",
                            button_id,
                            slot,
                        )
                        return
                if not self._prepare_voice_mapping_locked(
                    button_id,
                    action,
                    # A latched mic tap keeps the device stream open without
                    # holding any host shortcut, so this deliberate extra-key
                    # press may still take the mapping over.
                    allow_latched_stream_takeover=True,
                ):
                    return
                self._voice_ignore_audio_start_until_physical_down = False
                if not self._handle_mic_button_pressed(
                    # A latched mic tap already keeps the device stream open;
                    # asking for another MIC_OPEN would restart it under the
                    # latched session.
                    send_device_open=not self._voice_gesture.latched,
                    voice_slot=slot,
                ):
                    self._logger.info(
                        "extra voice key ignored: voice pipeline failed to open "
                        "the microphone; button=%s slot=%s",
                        button_id,
                        slot,
                    )
                    return
                self._extra_voice_slot_active = slot
                gesture.on_press()
                self._logger.info(
                    "extra voice key pressed: button=%s slot=%s", button_id, slot
                )
                return
            if self._extra_voice_slot_active != slot:
                return
            decision = gesture.on_release()
            if (
                decision == voice_gesture.VoiceGestureDecision.LATCH
                and self._voice_slot_is_plugin(slot)
            ):
                # 插件模式不锁存：遥控器只重放快捷键，没有音频要留着。
                decision = voice_gesture.VoiceGestureDecision.CLOSE
            released = self._release_voice_shortcut_edge_locked(
                "extra voice key release"
            )
            if decision == voice_gesture.VoiceGestureDecision.LATCH:
                self._extra_voice_slot_active = None
                # A tap keeps the microphone open until the same key is tapped
                # again. The host shortcut already got one complete
                # press/release pair, so the input method saw a real short
                # press; the device stream has to stay - or come back - open
                # underneath it. Without this the tap latch looked latched but
                # forwarded nothing, because the device had already dropped its
                # short stream while the key was still down.
                self._voice_pcm_forwarding_enabled = True
                self._set_runtime_voice_active(True)
                self._set_runtime_voice_result(
                    bridge_runtime_status.VOICE_RUNTIME_ACTIVE
                )
                if (
                    not self._voice_audio_stream_active
                    and not self._voice_slot_uses_local_microphone(slot)
                ):
                    try:
                        if self._ble_session is not None:
                            self._ble_session.send_mic_open_threadsafe()
                    except BaseException as exc:  # noqa: BLE001 - never trap input
                        self._logger.warning(
                            "extra voice tap latch MIC_OPEN failed: "
                            "error_type=%s",
                            type(exc).__name__,
                        )
                self._logger.info(
                    "extra voice key tap latched: button=%s slot=%s",
                    button_id,
                    slot,
                )
                return
            self._extra_voice_slot_active = None
            if self._voice_gesture.latched:
                # A latched mic tap still owns the open microphone, so this
                # extra shortcut must not close the stream under it.
                self._voice_pcm_forwarding_enabled = True
                self._set_runtime_voice_active(True)
                self._logger.info(
                    "extra voice key released with mic latch open: button=%s slot=%s",
                    button_id,
                    slot,
                )
                return
            self._voice_pcm_forwarding_enabled = False
            self._set_runtime_voice_active(False)
            self._stop_local_voice_capture_locked()
            if (
                released
                and self._ble_session is not None
                and not self._voice_slot_uses_local_microphone(slot)
            ):
                self._ble_session.send_mic_close_threadsafe()
            self._logger.info(
                "extra voice key released: button=%s slot=%s", button_id, slot
            )

    def _begin_voice_mic_gesture(
        self, source: str, *, physical_down: bool = False
    ) -> bool:
        """Claim one physical mic press for its first arriving event source.

        The caller holds ``_voice_trigger_lock``. Later sources are recorded
        for release tracking but must not toggle the host voice state again.
        """

        if self._voice_mic_gesture_active:
            if physical_down:
                self._voice_mic_gesture_physical_seen = True
                self._voice_mic_gesture_sources_down.add(source)
                if source == "hid_tap":
                    self._voice_mic_gesture_direct_hid_seen = True
            return False
        self._voice_mic_gesture_active = True
        self._voice_mic_gesture_audio_started = source == "audio_started"
        self._voice_mic_gesture_audio_stopped = False
        self._voice_mic_gesture_physical_seen = physical_down
        self._voice_mic_gesture_hid_released = False
        self._voice_mic_gesture_direct_hid_seen = (
            physical_down and source == "hid_tap"
        )
        self._voice_mic_gesture_sources_down = {source} if physical_down else set()
        return True

    def _finish_voice_mic_gesture(self, *, reset_gesture: bool = True) -> None:
        """Release the current cross-source mic gesture latch.

        ``reset_gesture=False`` keeps a just-latched tap alive across the
        physical release so the next tap can close the session.
        """

        self._cancel_voice_hold_watchdog_locked()
        if reset_gesture:
            self._voice_gesture.reset()
        self._voice_mic_gesture_active = False
        self._voice_mic_gesture_audio_started = False
        self._voice_mic_gesture_audio_stopped = False
        self._voice_mic_gesture_physical_seen = False
        self._voice_mic_gesture_hid_released = False
        self._voice_mic_gesture_direct_hid_seen = False
        self._voice_mic_gesture_sources_down.clear()

    def _cancel_voice_hold_watchdog_locked(self) -> None:
        timer = self._voice_hold_watchdog_timer
        self._voice_hold_watchdog_timer = None
        self._voice_hold_watchdog_token = None
        if timer is not None:
            cancel = getattr(timer, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except BaseException as exc:  # noqa: BLE001 - token is already invalid
                    self._logger.warning(
                        "voice hold safety timer cancel failed: error_type=%s",
                        type(exc).__name__,
                    )

    def _arm_voice_hold_watchdog_locked(self) -> bool:
        self._cancel_voice_hold_watchdog_locked()
        token = object()
        try:
            timer = self._voice_hold_watchdog_timer_factory(
                _VOICE_HOLD_SAFETY_SECONDS,
                lambda token=token: self._voice_hold_watchdog_expired(token),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._voice_hold_watchdog_token = token
            self._voice_hold_watchdog_timer = timer
            timer.start()
        except BaseException as exc:  # noqa: BLE001 - never leave the host key held
            if self._voice_hold_watchdog_token is token:
                self._cancel_voice_hold_watchdog_locked()
            self._logger.error(
                "voice hold safety timer failed to start: error_type=%s",
                type(exc).__name__,
            )
            return False
        return True

    def _force_voice_hold_release_locked(
        self,
        reason: str,
        *,
        reconnect: bool = True,
    ) -> bool:
        if reconnect:
            # The watchdog owns the end of the current BLE attempt. Invalidate
            # every callback already queued by that session before releasing
            # the host key; reconnect publishes a fresh generation.
            self._accept_ble_events = False
            self._ble_callback_generation += 1
        self._voice_pcm_forwarding_enabled = False
        self._voice_audio_stream_active = False
        self._voice_audio_stop_processed = True
        self._voice_raw_input_trigger_pending = False
        self._voice_audio_start_fallback_pending = False
        self._stop_local_voice_capture_locked()
        self._voice_mic_gesture_sources_down.clear()
        self._voice_shortcut_physical_sources_down.clear()
        self._voice_mic_gesture_hid_released = True

        self._drop_voice_delayed_press_locked()
        action = self._voice_shortcut.controller.reset()
        released = True
        if action is not None:
            released = self._voice_shortcut.apply(action)
            if not released:
                self._voice_shortcut.controller.restore_pending(action)
        elif self._voice_shortcut.pending_tokens is not None:
            released = self._voice_shortcut.release_pending()
        # 插件快捷键不经过音频事件，兜底补一条抬起。
        self._force_release_plugin_voice_tokens_locked(reason)

        self._finish_voice_mic_gesture()
        self._extra_voice_slot_active = None
        self._extra_voice_slot_close_pending = None
        for gesture in self._extra_voice_gestures.values():
            gesture.reset()
        if released:
            self._set_runtime_voice_active(False)
            self._log_voice_submission_observation()
            self._finish_voice_diagnostic_attempt(
                "unknown",
                reason="forced_host_release",
            )
            self._apply_pending_voice_settings_if_idle_locked()
        else:
            self._logger.error(
                "%s; %s will retry the host release",
                reason,
                "reconnect cleanup" if reconnect else "the safety timer",
            )
        try:
            if self._ble_session is not None:
                self._ble_session.send_mic_close_threadsafe()
        except BaseException as exc:  # noqa: BLE001 - reconnect must still run
            self._logger.warning(
                "voice hold safety MIC_CLOSE failed: error_type=%s",
                type(exc).__name__,
            )
        finally:
            if reconnect:
                self._supervisor.request_reconnect()
        return released

    def _voice_hold_watchdog_expired(self, token: object) -> None:
        with self._voice_shortcut.lock:
            if self._voice_hold_watchdog_token is not token:
                return
            self._voice_hold_watchdog_timer = None
            self._voice_hold_watchdog_token = None
            if not self._voice_shortcut.controller.active and self._voice_shortcut.pending_tokens is None:
                return

            self._force_voice_hold_release_locked("voice hold safety release failed")
            self._logger.warning(
                "voice hold exceeded %.0fs; forced host release and reconnect",
                _VOICE_HOLD_SAFETY_SECONDS,
            )

    def _rollover_completed_voice_mic_gesture_locked(self, next_source: str) -> bool:
        """Detach stale sources once a matched HID release proves a new press."""

        if not (
            self._voice_mic_gesture_active
            and not self._voice_shortcut.controller.active
            and self._voice_mic_gesture_hid_released
        ):
            return False
        stale_sources = sorted(self._voice_mic_gesture_sources_down)
        self._finish_voice_mic_gesture()
        self._logger.info(
            "voice completed gesture rolled over for new source=%s; "
            "detached late duplicate sources=%s",
            next_source,
            stale_sources,
        )
        return True

    def _release_voice_shortcut_edge_locked(
        self,
        reason: str,
        *,
        inject_mode: Optional[str] = None,
    ) -> bool:
        if self._voice_delayed_press_token is not None:
            # 窗口内就松手了：这一次按键整记作废，宿主一个键都收不到，所以
            # 短按只会切模式，绝不会变成语音输入。
            self._drop_voice_delayed_press_locked()
            self._cancel_voice_hold_watchdog_locked()
            self._logger.info(
                "voice shortcut press dropped inside the tap window: %s", reason
            )
            return True
        action = self._voice_shortcut.controller.on_mic_button_released()
        if action is None:
            self._cancel_voice_hold_watchdog_locked()
            # 插件模式没有音频停止事件兜底，这里补一条抬起，防止键卡住。
            self._force_release_plugin_voice_tokens_locked(reason)
            return True
        if self._voice_shortcut.apply(action, inject_mode=inject_mode):
            self._plugin_voice_injected_tokens = None
            self._cancel_voice_hold_watchdog_locked()
            self._logger.info("voice shortcut edge released on %s", reason)
            return True
        self._voice_shortcut.controller.restore_pending(action)
        self._set_runtime_voice_result(
            bridge_runtime_status.VOICE_RUNTIME_HOST_STOP_FAILED
        )
        self._logger.info(
            "voice shortcut edge release failed on %s; state retained, requesting reconnect",
            reason,
        )
        self._supervisor.request_reconnect()
        return False

    def _close_latched_voice_session_locked(self, reason: str) -> None:
        """End a latched tap session from a second physical mic tap.

        The caller holds ``_voice_trigger_lock``. The second tap still replays
        its own host edge so the input method sees a fresh press, then PCM
        forwarding stops, the device microphone is closed and the latch is
        dropped so the following tap starts a clean session.
        """

        voice_slot = self._active_voice_slot()
        self._press_voice_shortcut_edge_locked(voice_slot)
        self._voice_pcm_forwarding_enabled = False
        self._set_runtime_voice_active(False)
        self._stop_local_voice_capture_locked()
        try:
            if self._ble_session is not None:
                self._ble_session.send_mic_close_threadsafe()
        except BaseException as exc:  # noqa: BLE001 - never trap input
            self._logger.warning(
                "voice tap latch MIC_CLOSE failed: error_type=%s",
                type(exc).__name__,
            )
        self._voice_mic_gesture_sources_down.clear()
        self._finish_voice_mic_gesture()
        if self._voice_shortcut.controller.active:
            self._arm_voice_hold_watchdog_locked()
        self._logger.info("voice mic tap latch released by %s", reason)

    def _release_hold_voice_on_physical_release_locked(
        self,
        reason: str,
    ) -> bool:
        """Release HOLD shortcuts without depending solely on AUDIO_STOP."""

        if self._voice_delayed_press_token is not None:
            # 取消/清理路径也一样：还没落地的按键整记作废，不发任何键。
            self._drop_voice_delayed_press_locked()
            self._cancel_voice_hold_watchdog_locked()
            return True
        action = self._voice_shortcut.controller.on_mic_button_released()
        if action is None:
            self._cancel_voice_hold_watchdog_locked()
            return True
        self._voice_raw_input_trigger_pending = False
        self._voice_audio_start_fallback_pending = False
        self._voice_pcm_forwarding_enabled = False
        if self._voice_shortcut.apply(action):
            self._cancel_voice_hold_watchdog_locked()
            self._logger.info("voice hold hotkey released on %s", reason)
            if not self._voice_audio_stream_active:
                self._set_runtime_voice_active(False)
                self._set_runtime_voice_result(
                    bridge_runtime_status.VOICE_RUNTIME_AUDIO_EMPTY
                )
                self._log_voice_submission_observation()
                self._finish_voice_diagnostic_attempt(
                    "unknown",
                    reason="physical_release_without_audio_stop",
                    audio_signal=False,
                )
            return True

        self._voice_shortcut.controller.restore_pending(action)
        self._set_runtime_voice_result(
            bridge_runtime_status.VOICE_RUNTIME_HOST_STOP_FAILED
        )
        self._logger.info(
            "voice hold hotkey release failed on %s; state retained, "
            "requesting reconnect",
            reason,
        )
        self._supervisor.request_reconnect()
        return False

    def _reset_key_detection_mic_gesture_locked(self) -> None:
        self._key_detection_mic_gesture_active = False
        self._key_detection_mic_gesture_started_at = 0.0
        self._key_detection_mic_release_deadline = None
        self._key_detection_mic_audio_started = False
        self._key_detection_mic_sources_down.clear()

    def _expire_key_detection_mic_gesture_locked(self, now: float) -> None:
        if not self._key_detection_mic_gesture_active:
            return
        release_deadline = self._key_detection_mic_release_deadline
        hard_deadline = (
            self._key_detection_mic_gesture_started_at
            + _KEY_DETECTION_MIC_MAX_SECONDS
        )
        if (release_deadline is not None and now >= release_deadline) or (
            now >= hard_deadline
        ):
            self._reset_key_detection_mic_gesture_locked()

    def _handle_key_detection_mic_event(
        self,
        event_kind: str,
        source: str,
    ) -> Tuple[bool, bool]:
        """Capture or suppress one source from a detected physical mic press.

        A single RC003 mic gesture is reported by multiple independent paths.
        The key-detection request disappears as soon as the first path claims
        it, so a short in-process latch must keep later paths from entering the
        normal voice state machine. Returns ``(handled, newly_captured)``.
        """

        now = time.monotonic()
        starts_gesture = event_kind in {
            "physical_down",
            "atvv_press",
            "audio_started",
        }
        with self._voice_shortcut.lock:
            # A detection request may appear while a real voice press is still
            # active. Its late HID/F5/audio edges belong to that owned press and
            # must remain available to close the host shortcut. Leave the
            # request pending for the next independent press instead.
            if self._voice_mic_gesture_active:
                return False, False
            with self._key_detection_mic_lock:
                self._expire_key_detection_mic_gesture_locked(now)
                newly_captured = False
                if not self._key_detection_mic_gesture_active:
                    if not starts_gesture:
                        return False, False
                    try:
                        newly_captured = key_detection_bridge.publish_next_button(
                            self._config_root,
                            "mic",
                        )
                    except OSError as exc:
                        self._logger.warning("key detection IPC unavailable: %s", exc)
                        return False, False
                    if not newly_captured:
                        return False, False
                    self._key_detection_mic_gesture_active = True
                    self._key_detection_mic_gesture_started_at = now

                if event_kind == "physical_down":
                    self._key_detection_mic_sources_down.add(source)
                    self._key_detection_mic_release_deadline = None
                elif event_kind == "physical_up":
                    self._key_detection_mic_sources_down.discard(source)
                    if (
                        not self._key_detection_mic_sources_down
                        and not self._key_detection_mic_audio_started
                    ):
                        self._key_detection_mic_release_deadline = (
                            now + _KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS
                        )
                elif event_kind == "audio_started":
                    self._key_detection_mic_audio_started = True
                    self._key_detection_mic_release_deadline = None
                elif event_kind == "audio_stopped":
                    self._key_detection_mic_audio_started = False
                    if not self._key_detection_mic_sources_down:
                        self._key_detection_mic_release_deadline = (
                            now + _KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS
                        )
                elif not self._key_detection_mic_sources_down:
                    # MicButtonPressed has no matching release opcode. Give the
                    # physical/audio paths time to join, then let a future press
                    # through even if neither companion event ever arrives.
                    self._key_detection_mic_release_deadline = (
                        now + _KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS
                    )
                return True, newly_captured

    def _on_raw_button_event(
        self,
        button_id: str,
        is_pressed: bool,
        source: str = "unknown",
        windows_button_id: Optional[str] = None,
        *,
        _listener: Optional[raw_input_windows.RawInputButtonListener] = None,
        _generation: Optional[int] = None,
    ) -> None:
        event_source = {
            "keyboard": "raw_keyboard",
            "hid": "raw_hid",
        }.get(source, "raw_unknown")
        with self._input_arbitration_lock:
            if not self._raw_input_callback_is_current(_listener, _generation):
                return
            self._on_button_event_serialized(
                button_id,
                is_pressed,
                event_source=event_source,
                raw_windows_button_id=windows_button_id,
            )

    # -- HID button events --------------------------------------------------

    @staticmethod
    def _settings_file_mtime_ns(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return -1

    def _voice_settings_idle_locked(self) -> bool:
        return not (
            self._voice_shortcut.controller.active
            or self._voice_mic_gesture_active
            or self._voice_audio_stream_active
            or self._ordinary_mic_gesture_active
            or self._voice_shortcut.pending_tokens is not None
        )

    def _latched_voice_stream_only_locked(self) -> bool:
        """True when the only live voice state is a latched, unowned device stream.

        A short-tap latch keeps the device microphone streaming after the host
        shortcut has been released, so ``_voice_settings_idle_locked`` reports
        busy even though no shortcut is held. A deliberate host key press may
        still repurpose the mapping in that state; an incoming device stream may
        not, which is why the takeover is opt-in per call site.

        A held physical source always implies an owned shortcut
        (``controller.active``) or a live mic gesture, so those two checks
        already cover it; the caller's own just-pressed extra key is therefore
        not treated as an owner here.
        """

        return (
            self._voice_audio_stream_active
            and not self._voice_shortcut.controller.active
            and not self._voice_mic_gesture_active
            and not self._ordinary_mic_gesture_active
            and self._voice_shortcut.pending_tokens is None
        )

    def _ordinary_button_mappings_idle(self) -> bool:
        return not (
            self._button_gestures.has_active_gestures()
            or self._button_combos.has_active_combo()
        )

    def _apply_voice_settings_locked(
        self,
        voice_hotkey: hotkey.HotkeySpec,
        *,
        update_config: bool = True,
    ) -> None:
        self._voice_shortcut.controller = voice_controller.VoiceController()
        self._voice_shortcut.hotkey = voice_hotkey
        if update_config:
            self._config["voice_hotkey"] = voice_hotkey.serialize()
        self._logger.info(
            "settings voice configuration applied: hotkey=%s",
            voice_hotkey.serialize(),
        )

    def _clear_voice_hotkey_locked(self) -> None:
        """Drop the host shortcut for a voice key that has none recorded."""

        self._voice_shortcut.controller = voice_controller.VoiceController()
        self._voice_shortcut.hotkey = None
        self._logger.info("settings voice configuration applied: hotkey=")

    def _apply_pending_voice_settings_if_idle_locked(self) -> None:
        if not self._voice_settings_idle_locked():
            return
        if self._pending_config is not None:
            self._config = self._pending_config
            self._pending_config = None
        if self._pending_voice_settings is not None:
            voice_hotkey = self._pending_voice_settings
            self._pending_voice_settings = None
            self._apply_voice_settings_locked(voice_hotkey)
        if (
            self._pending_bindings is not None
        ):
            with self._button_mapping_lock:
                if self._ordinary_button_mappings_idle():
                    self._button_combos.reset()
                    self._bindings = self._pending_bindings
                    self._pending_bindings = None
                    self._preset_override_id = None
                    self._preset_base_bindings = None
                    self._sync_physical_bindings_to_listener()
                    self._removed_voice_bindings = dict(
                        self._bindings.get(
                            config.RUNTIME_REMOVED_VOICE_BINDINGS_KEY, {}
                        )
                    )
                    self._logger.info(
                        "deferred settings mappings applied after active input became idle"
                    )
                    self._diagnostic_trace.emit("voice_configuration_applied", result="recovered")

    def _apply_pending_settings_if_idle(self) -> None:
        with self._voice_shortcut.lock:
            self._apply_pending_voice_settings_if_idle_locked()

    def _reload_settings_if_changed(self) -> None:
        """Apply mapping and voice-setting edits without a bridge restart."""

        current_config_mtime_ns = self._settings_file_mtime_ns(self._config_path)
        current_bindings_mtime_ns = self._settings_file_mtime_ns(self._bindings_path)
        if (
            current_config_mtime_ns == self._config_mtime_ns
            and current_bindings_mtime_ns == self._bindings_mtime_ns
        ):
            return
        try:
            refreshed_config = config.load_config(self._config_path)
            from . import remote_selection
            if remote_selection.active_key(refreshed_config) != remote_selection.active_key(self._config):
                raise remote_selection.SelectionError("设备选择已变化，须先停止旧服务再切换。")
            refreshed_bindings = config.load_key_bindings(self._bindings_path)
            removed_voice_bindings = config.normalize_voice_product_boundary(
                refreshed_config,
                refreshed_bindings,
            )
            refreshed_hotkey_text = _runtime_voice_hotkey_text(refreshed_config)
            # An unrecorded voice shortcut is a valid state: it must not abort
            # the whole reload, which would also drop refreshed button mappings.
            voice_hotkey = (
                hotkey.HotkeySpec.parse(refreshed_hotkey_text)
                if refreshed_hotkey_text
                else None
            )
            refreshed_hotkey_serialized = (
                voice_hotkey.serialize() if voice_hotkey is not None else ""
            )
        except Exception as exc:  # noqa: BLE001 - keep the last valid settings
            self._logger.warning("settings reload skipped: %s", exc)
            with self._voice_shortcut.lock:
                self._config_mtime_ns = current_config_mtime_ns
                self._bindings_mtime_ns = current_bindings_mtime_ns
            return

        self._diagnostic_trace.set_enabled(
            bool(refreshed_config.get("diagnostic_trace_enabled", False))
        )
        self._record_device_diagnostic_context()
        with self._voice_shortcut.lock:
            with self._button_mapping_lock:
                refreshed_settings = refreshed_hotkey_serialized
                current_settings = (
                    self._voice_shortcut.hotkey.serialize()
                    if self._voice_shortcut.hotkey is not None
                    else ""
                )
                if self._voice_settings_idle_locked():
                    self._config = refreshed_config
                    self._pending_config = None
                    self._pending_voice_settings = None
                    if (
                        voice_hotkey is not None
                        and refreshed_settings != current_settings
                    ):
                        self._apply_voice_settings_locked(voice_hotkey)
                    if self._ordinary_button_mappings_idle():
                        self._button_combos.reset()
                        self._bindings = refreshed_bindings
                        self._preset_override_id = None
                        self._preset_base_bindings = None
                        self._sync_physical_bindings_to_listener()
                        self._removed_voice_bindings = removed_voice_bindings
                        self._pending_bindings = None
                    else:
                        self._pending_bindings = refreshed_bindings
                        self._logger.info(
                            "settings mapping reload deferred until active input is idle"
                        )
                else:
                    self._pending_config = refreshed_config
                    self._pending_bindings = refreshed_bindings
                    self._pending_voice_settings = (
                        voice_hotkey if voice_hotkey is not None else None
                    )
                    self._logger.info(
                        "settings reload deferred until active voice session is idle"
                    )
                    self._diagnostic_trace.emit("voice_configuration_deferred",
                        release_pending=self._voice_shortcut.pending_tokens is not None,
                        reason="active_voice_not_idle", **self._diagnostic_trace.current_context())
                self._config_mtime_ns = current_config_mtime_ns
                self._bindings_mtime_ns = current_bindings_mtime_ns
        if removed_voice_bindings:
            self._logger.warning(
                "legacy voice mappings disabled until user reselects actions: %s",
                sorted(removed_voice_bindings),
            )
        if self._pending_bindings is None:
            self._logger.info("settings reloaded from disk")

    def request_settings_reload_now(self) -> None:
        """Apply saved settings promptly from the bridge event-loop thread."""

        self._event_loop.call_soon_threadsafe(self._reload_settings_if_changed)

    # -- saved mapping presets: hotkey + foreground-process switching --------

    def _start_preset_switching(self) -> None:
        if self._preset_switch_task is None or self._preset_switch_task.done():
            self._preset_switch_task = self._event_loop.create_task(
                self._preset_switch_loop()
            )

    async def _stop_preset_switching(self) -> None:
        task = self._preset_switch_task
        self._preset_switch_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._stop_preset_hotkey_listener()
        with self._button_mapping_lock:
            self._reset_preset_override_locked()

    async def _preset_switch_loop(self) -> None:
        while True:
            try:
                await self._poll_preset_switch()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad poll must not end the loop
                self._logger.exception("preset switch poll failed")
            await asyncio.sleep(_PRESET_SWITCH_POLL_SECONDS)

    async def _poll_preset_switch(self) -> None:
        if not self._accept_input_events:
            return
        self._refresh_preset_hotkey_targets_if_changed()
        process_targets = await asyncio.to_thread(self._preset_app_targets)
        foreground = await asyncio.to_thread(self._preset_foreground_basename)
        if foreground != self._preset_switch_foreground:
            self._preset_switch_foreground = foreground
            # A manual switch holds only while the foreground process is
            # unchanged; moving to another window resumes process-scope again.
            self._preset_manual_foreground = None
        if self._preset_manual_foreground is not None:
            return
        target = process_targets.get(foreground) if foreground else None
        if target != self._preset_override_id:
            if target is None:
                with self._button_mapping_lock:
                    self._reset_preset_override_locked()
            else:
                self._apply_preset_override(target)
        # The settings window can only learn about a switch through the shared
        # profile library, so the marker always follows the overlay actually in
        # memory - including a swap that a busy gesture postponed.
        self._remember_applied_profile(
            self._preset_override_id or app_profiles.GLOBAL_ID
        )

    def _preset_app_targets(self) -> dict:
        """Map each app profile's process basename to its identity."""

        try:
            entries = self._preset_store.list_apps()
        except Exception:  # noqa: BLE001 - unreadable profiles never break input
            return {}
        result: dict = {}
        for entry in entries:
            process = entry["process"].strip().casefold()
            if process:
                result.setdefault(process, entry["id"])
        return result

    def _preset_foreground_basename(self) -> str:
        try:
            snapshot = voice_interaction_diagnostics_windows.capture_focus_snapshot(
                include_text_length=False
            )
        except Exception:  # noqa: BLE001 - best-effort foreground observation
            return ""
        name = getattr(snapshot, "foreground_executable", "") or ""
        return os.path.basename(name).strip().casefold()

    def _apply_preset_override(self, identity: str) -> None:
        """Swap the live mapping for a process-scoped preset (in memory only)."""

        try:
            entry = self._preset_store.get(identity)
        except Exception:  # noqa: BLE001 - the preset vanished between polls
            return
        with self._voice_shortcut.lock:
            with self._button_mapping_lock:
                if not self._accept_input_events:
                    return
                if not self._voice_settings_idle_locked():
                    return  # retried on the next poll
                base = (
                    self._preset_base_bindings
                    if self._preset_base_bindings is not None
                    else self._bindings
                )
                try:
                    replacement = app_profiles.apply_to(
                        base, entry, self._remote_profile
                    )
                except Exception:  # noqa: BLE001 - a bad snapshot is display-only
                    self._logger.warning(
                        "preset process override skipped: invalid snapshot"
                    )
                    return
                self._button_combos.reset()
                self._bindings = replacement
                # Overlays stack, so the untouched mapping is kept: switching
                # straight from one app to another must resolve from the global
                # layout instead of from the previous app's overlay.
                self._preset_base_bindings = base
                self._preset_override_id = identity
                self._sync_physical_bindings_to_listener()
                self._removed_voice_bindings = dict(
                    replacement.get(config.RUNTIME_REMOVED_VOICE_BINDINGS_KEY, {})
                )
                self._logger.info(
                    "preset process override applied; foreground=%s",
                    self._preset_switch_foreground,
                )

    def _reset_preset_override_locked(self) -> None:
        if self._preset_override_id is None and self._preset_base_bindings is None:
            return
        self._preset_override_id = None
        base = self._preset_base_bindings
        self._preset_base_bindings = None
        if base is None:
            try:
                base = config.load_key_bindings(self._bindings_path)
            except Exception:  # noqa: BLE001 - keep the last live mapping
                self._logger.warning(
                    "preset override reset could not reload key bindings"
                )
                return
        self._bindings = base
        self._removed_voice_bindings = dict(
            base.get(config.RUNTIME_REMOVED_VOICE_BINDINGS_KEY, {})
        )
        self._sync_physical_bindings_to_listener()
        self._logger.info("preset override cleared; base key bindings restored")

    def _remember_applied_profile(self, identity: str) -> None:
        """Publish the profile in effect so the settings window can highlight it."""

        if identity == self._preset_applied_id:
            return
        try:
            self._preset_store.mark_applied(identity)
        except Exception:  # noqa: BLE001 - highlighting never breaks input
            self._logger.warning("applied profile marker was not saved")
            return
        self._preset_applied_id = identity

    def _build_preset_hotkey_targets(self) -> dict:
        try:
            entries = self._preset_store.load()
        except Exception:  # noqa: BLE001 - unreadable presets disable the listener
            self._logger.warning("saved presets unreadable; switch hotkeys disabled")
            return {}
        targets: dict = {}
        for entry in entries:
            if not entry["hotkey"]:
                continue
            try:
                codes = win32_keys.resolve_vk_codes(entry["hotkey"].split("+"))
            except Exception:  # noqa: BLE001 - a stored hotkey is revalidated on save
                continue
            # The low-level hook reports handed modifier VKs; collapse both
            # sides onto the generic vocabulary so stored chords match.
            chord = frozenset(
                preset_switch_windows.normalize_vk(code) for code in codes
            )
            if chord:
                targets[chord] = entry["id"]
        return targets

    def _refresh_preset_hotkey_targets_if_changed(self, *, force: bool = False) -> None:
        mtime_ns = self._settings_file_mtime_ns(self._preset_store.path)
        if not force and mtime_ns == self._presets_mtime_ns:
            return
        self._presets_mtime_ns = mtime_ns
        targets = self._build_preset_hotkey_targets()
        if not targets:
            self._stop_preset_hotkey_listener()
            return
        listener = self._preset_hotkey_listener
        if listener is None:
            listener = preset_switch_windows.PresetSwitchListener(
                on_switch=self._on_preset_hotkey,
                logger=self._logger,
            )
            self._preset_hotkey_listener = listener
        try:
            listener.set_targets(targets)
        except Exception:  # noqa: BLE001 - keep the last working targets
            self._logger.exception("preset switch target update failed")
            return
        if not listener.is_running:
            try:
                listener.start()
            except preset_switch_windows.PresetSwitchUnavailableError as exc:
                self._logger.warning("preset switch hotkeys unavailable: %s", exc)
            except Exception:  # noqa: BLE001 - never fail input startup
                self._logger.exception("preset switch listener failed to start")

    def _stop_preset_hotkey_listener(self) -> None:
        listener = self._preset_hotkey_listener
        self._preset_hotkey_listener = None
        if listener is None:
            return
        try:
            listener.stop()
        except Exception:  # noqa: BLE001 - teardown must always be attempted
            self._logger.exception("preset switch listener did not stop cleanly")

    def _on_preset_hotkey(self, identity: str) -> None:
        """Hook-thread callback: marshal the switch onto the event loop."""

        loop = self._event_loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._activate_preset_by_hotkey, identity)

    def _activate_preset_by_hotkey(self, identity: str) -> None:
        if not self._accept_input_events:
            return
        with self._button_mapping_lock:
            try:
                entry = self._preset_store.get(identity)
                base = (
                    self._preset_base_bindings
                    if self._preset_base_bindings is not None
                    else self._bindings
                )
                replacement = app_profiles.apply_to(
                    base, entry, self._remote_profile
                )
            except Exception:  # noqa: BLE001 - a stale id is simply ignored
                self._logger.warning("preset hotkey switch skipped: invalid preset")
                return
        try:
            config.save_key_bindings(self._bindings_path, replacement)
        except Exception:  # noqa: BLE001 - do not half-apply a failed save
            self._logger.exception("preset hotkey switch could not be saved")
            return
        self._remember_applied_profile(identity)
        with self._voice_shortcut.lock:
            with self._button_mapping_lock:
                # A deliberate hotkey switch supersedes any process override and
                # is applied persistently, exactly like the settings window.
                self._preset_override_id = None
                self._preset_base_bindings = None
                if self._voice_settings_idle_locked():
                    self._pending_config = None
                    self._pending_voice_settings = None
                if self._ordinary_button_mappings_idle():
                    self._button_combos.reset()
                    self._bindings = replacement
                    self._pending_bindings = None
                    self._sync_physical_bindings_to_listener()
                    self._removed_voice_bindings = dict(
                        replacement.get(config.RUNTIME_REMOVED_VOICE_BINDINGS_KEY, {})
                    )
                else:
                    self._pending_bindings = replacement
                self._bindings_mtime_ns = self._settings_file_mtime_ns(
                    self._bindings_path
                )
        self._preset_manual_foreground = self._preset_switch_foreground
        self._logger.info("preset hotkey switch applied; waiting for any held key")

    def _handle_ordinary_mic_edge(
        self, event_source: str, is_pressed: bool
    ) -> None:
        """Collapse F5/HID duplicates before ordinary mic gesture dispatch."""

        dispatch = False
        cancel_stale_gesture = False
        now = time.monotonic()
        with self._ordinary_mic_lock:
            expired_late_sources = {
                source
                for source, deadline in self._ordinary_mic_late_source_deadlines.items()
                if now >= deadline
            }
            if expired_late_sources:
                self._ordinary_mic_late_sources_down.difference_update(
                    expired_late_sources
                )
                for source in expired_late_sources:
                    self._ordinary_mic_late_source_deadlines.pop(source, None)

            stale_active_gesture = self._ordinary_mic_gesture_active and (
                now - self._ordinary_mic_gesture_started_at
                >= _ORDINARY_MIC_MAX_SECONDS
                or (
                    is_pressed
                    and now - self._ordinary_mic_gesture_started_at
                    >= _ORDINARY_MIC_SOURCE_STALE_SECONDS
                )
            )
            if stale_active_gesture:
                self._ordinary_mic_sources_down.clear()
                self._ordinary_mic_late_sources_down.clear()
                self._ordinary_mic_late_source_deadlines.clear()
                self._ordinary_mic_sources_seen.clear()
                self._ordinary_mic_gesture_started_at = 0.0
                self._ordinary_mic_release_guard_until = 0.0
                self._ordinary_mic_gesture_active = False
                cancel_stale_gesture = True

            if is_pressed:
                if (
                    event_source in self._ordinary_mic_sources_down
                    or event_source in self._ordinary_mic_late_sources_down
                ):
                    return
                if self._ordinary_mic_sources_down:
                    self._ordinary_mic_sources_down.add(event_source)
                    self._ordinary_mic_sources_seen.add(event_source)
                    return
                now = time.monotonic()
                if (
                    now < self._ordinary_mic_release_guard_until
                    and event_source not in self._ordinary_mic_sources_seen
                ):
                    self._ordinary_mic_late_sources_down.add(event_source)
                    self._ordinary_mic_late_source_deadlines[event_source] = (
                        now + _ORDINARY_MIC_SOURCE_STALE_SECONDS
                    )
                    self._ordinary_mic_sources_seen.add(event_source)
                    self._logger.info(
                        "ordinary mic late duplicate ignored: source=%s",
                        event_source,
                    )
                    return
                self._ordinary_mic_sources_down.add(event_source)
                self._ordinary_mic_sources_seen = {event_source}
                self._ordinary_mic_gesture_started_at = now
                self._ordinary_mic_release_guard_until = 0.0
                dispatch = True
            else:
                if event_source in self._ordinary_mic_late_sources_down:
                    self._ordinary_mic_late_sources_down.discard(event_source)
                    self._ordinary_mic_late_source_deadlines.pop(event_source, None)
                    return
                if event_source not in self._ordinary_mic_sources_down:
                    return
                self._ordinary_mic_sources_down.discard(event_source)
                dispatch = not self._ordinary_mic_sources_down
                if dispatch:
                    self._ordinary_mic_release_guard_until = (
                        now + _ORDINARY_MIC_RELEASE_GUARD_SECONDS
                    )
                    self._ordinary_mic_gesture_started_at = 0.0
            self._ordinary_mic_gesture_active = bool(
                self._ordinary_mic_sources_down
            )
        if cancel_stale_gesture:
            self._button_gestures.reset()
        if not dispatch:
            return
        with self._button_mapping_lock:
            if is_pressed:
                self._button_gestures.press("mic")
            else:
                self._button_gestures.release("mic")

    def _on_button_event(
        self,
        button_id: str,
        is_pressed: bool,
        *,
        event_source: str = "hid",
        raw_windows_button_id: Optional[str] = None,
        hid_usage: Optional[int] = None,
    ) -> None:
        with self._input_arbitration_lock:
            self._on_button_event_serialized(
                button_id,
                is_pressed,
                event_source=event_source,
                raw_windows_button_id=raw_windows_button_id,
                hid_usage=hid_usage,
            )

    def _on_button_event_serialized(
        self,
        button_id: str,
        is_pressed: bool,
        *,
        event_source: str,
        raw_windows_button_id: Optional[str] = None,
        hid_usage: Optional[int] = None,
    ) -> None:
        if not self._accept_input_events:
            return
        gesture_id = (
            self._diagnostic_trace.current_gesture(button_id)
            if event_source.startswith("raw_")
            else None
        )
        if not gesture_id:
            gesture_id = self._diagnostic_trace.begin_gesture(
                button_id,
                event_source,
                bool(is_pressed),
                usage=int(hid_usage) if hid_usage is not None else -1,
            )
        self._diagnostic_trace.set_current_gesture(gesture_id)
        self._diagnostic_trace.emit(
            "logical_button_event",
            gesture_id=gesture_id,
            button_id=str(button_id),
            source=str(event_source),
            edge="down" if is_pressed else "up",
            usage=int(hid_usage) if hid_usage is not None else -1,
        )
        now = time.monotonic()
        with self._input_arbitration_lock:
            if button_id in self._input_rearm_blocked_buttons:
                if not is_pressed:
                    self._input_rearm_blocked_buttons.discard(button_id)
                return
        if event_source == "hid_tap":
            with self._input_arbitration_lock:
                if not self._direct_hid_interception_ready:
                    return
        raw_mapping_bypassed = False
        if event_source.startswith("raw_"):
            with self._input_arbitration_lock:
                if (
                    self._direct_hid_interception_armed
                    or self._direct_hid_interception_ready
                ):
                    return

                # Raw Input can observe Windows' original key event, but it
                # cannot suppress that event. Until the HID tap owns the
                # report, every custom mapping must therefore fail closed.
                raw_mapping_bypassed = True

                if button_id in self._raw_fallback_buttons_down:
                    if not is_pressed:
                        self._cancel_raw_fallback_hold_guard_locked(button_id)
                        self._raw_fallback_buttons_down.discard(button_id)

                else:
                    effective_windows_button = raw_windows_button_id
                    if (
                        effective_windows_button is None
                        and not self._raw_fallback_tracking_active
                    ):
                        effective_windows_button = button_id
                    preserve_windows_original = (
                        effective_windows_button in _RAW_FALLBACK_KEY_TOKENS
                    )
                    if preserve_windows_original:
                        if is_pressed:
                            self._raw_fallback_buttons_down.add(button_id)
                            if not self._arm_raw_fallback_hold_guard_locked(button_id):
                                self._raw_fallback_buttons_down.discard(button_id)
                                physical_buttons = (
                                    self._take_raw_fallback_physical_buttons_locked(
                                        {button_id}
                                    )
                                )
                                self._block_input_until_release(
                                    {button_id} | physical_buttons
                                )
                                self._release_raw_fallback_keyups(
                                    physical_buttons,
                                    reason="raw_fallback_timer_unavailable",
                                )
                            self._logger.warning(
                                "RC003 mapping bypassed for non-intercepted input; "
                                "Windows original retained: button=%s source=%s",
                                button_id,
                                event_source,
                            )
                    elif is_pressed:
                        self._logger.warning(
                            "RC003 mapping bypassed for non-intercepted input; "
                            "no Windows original available: button=%s source=%s",
                            button_id,
                            event_source,
                        )
        if is_pressed:
            self._record_runtime_button(event_source)
        if button_id == "mic":
            detection_handled, detection_captured = (
                self._handle_key_detection_mic_event(
                    "physical_down" if is_pressed else "physical_up",
                    event_source,
                )
            )
            if detection_handled:
                if detection_captured:
                    self._logger.info(
                        "key detection captured button=mic source=%s; "
                        "voice action suppressed",
                        event_source,
                    )
                return
        if button_id in self._key_detection_suppressed_buttons:
            if not is_pressed:
                self._key_detection_suppressed_buttons.discard(button_id)
                self._key_detection_suppression_deadlines.pop(button_id, None)
                return
            deadline = self._key_detection_suppression_deadlines.get(button_id)
            if deadline is None or now < deadline:
                return
            self._key_detection_suppressed_buttons.discard(button_id)
            self._key_detection_suppression_deadlines.pop(button_id, None)
        detection_captured = False
        if is_pressed and button_id != "mic":
            try:
                detection_captured = key_detection_bridge.publish_next_button(
                    self._config_root,
                    button_id,
                )
            except OSError as exc:
                self._logger.warning("key detection IPC unavailable: %s", exc)
        if detection_captured:
            self._key_detection_suppressed_buttons.add(button_id)
            self._key_detection_suppression_deadlines[button_id] = (
                now + _KEY_DETECTION_SUPPRESSION_MAX_SECONDS
            )
            self._logger.info(
                "key detection captured button=%s; mapped action suppressed",
                button_id,
            )
            return
        if raw_mapping_bypassed:
            return
        if button_id == "mic" and self._ordinary_mic_gesture_active:
            self._handle_ordinary_mic_edge(event_source, is_pressed)
            self._apply_pending_settings_if_idle()
            return
        self._reload_settings_if_changed()

        with self._button_mapping_lock:
            removed_voice_binding = button_id in self._removed_voice_bindings
            ordinary_mic_handled = False
            if removed_voice_binding:
                if not is_pressed:
                    self._button_gestures.release(button_id)
                else:
                    self._logger.warning(
                        "button action suppressed until removed voice mapping is reselected: %s",
                        button_id,
                    )
                primary_action = None
                is_voice_button = False
            else:
                primary_action = self._primary_button_action(button_id)
                is_voice_button = self._is_voice_primary_button(
                    button_id,
                    primary_action,
                )
                if button_id == "mic" and not is_voice_button:
                    self._handle_ordinary_mic_edge(event_source, is_pressed)
                    ordinary_mic_handled = True
        if removed_voice_binding:
            self._apply_pending_settings_if_idle()
            return
        if ordinary_mic_handled:
            self._apply_pending_settings_if_idle()
            return

        if is_voice_button and button_id != "mic":
            self._handle_extra_voice_key_edge(button_id, primary_action, is_pressed)
            self._apply_pending_settings_if_idle()
            return

        if button_id == "mic":
            if not is_pressed:
                with self._voice_shortcut.lock:
                    shortcut_source_was_down = (
                        event_source in self._voice_shortcut_physical_sources_down
                    )
                    if shortcut_source_was_down:
                        self._voice_shortcut_physical_sources_down.discard(
                            event_source
                        )
                        if event_source in {"hid", "hid_tap"}:
                            self._voice_shortcut_physical_sources_down.clear()
                        if (
                            event_source in {"hid", "hid_tap"}
                            or not self._voice_shortcut_physical_sources_down
                        ):
                            self._release_voice_shortcut_edge_locked(
                                "physical mic release"
                            )
                    source_was_down = (
                        self._voice_mic_gesture_active
                        and event_source in self._voice_mic_gesture_sources_down
                    )
                    if not source_was_down:
                        self._logger.info(
                            "voice physical release ignored without matching down: "
                            "source=%s",
                            event_source,
                        )
                        # 记账丢了也不能让插件快捷键留在按下状态。
                        self._force_release_plugin_voice_tokens_locked(
                            "physical mic release without matching down"
                        )
                        self._apply_pending_voice_settings_if_idle_locked()
                        return

                    matched_hid_released = event_source in {"hid", "hid_tap"}
                    if matched_hid_released:
                        self._voice_mic_gesture_hid_released = True
                        self._voice_ignore_audio_start_until_physical_down = True
                    self._voice_mic_gesture_sources_down.discard(event_source)
                    gesture_decision = None
                    short_tap = False
                    if (
                        matched_hid_released
                        or not self._voice_mic_gesture_sources_down
                    ):
                        if (
                            event_source == "hid_tap"
                            and self._voice_mic_gesture_sources_down
                        ):
                            self._logger.info(
                                "voice hold release accepted from direct HID while "
                                "late duplicate sources remain: %s",
                                sorted(self._voice_mic_gesture_sources_down),
                            )
                        gesture_decision = self._voice_gesture.on_release()
                        short_tap = (
                            gesture_decision
                            is voice_gesture.VoiceGestureDecision.LATCH
                        )
                        if (
                            gesture_decision
                            is voice_gesture.VoiceGestureDecision.LATCH
                            and (
                                not self._uses_local_voice_microphone()
                                or self._voice_slot_is_plugin(
                                    self._active_voice_slot()
                                )
                            )
                        ):
                            # 模式1 只支持长按：按下时快捷键已经立即下发，
                            # 松手就立刻结束，绝不锁存（遥控器只在按住期间
                            # 送音频，锁存会让界面显示录音中却没有声音）。
                            gesture_decision = (
                                voice_gesture.VoiceGestureDecision.CLOSE
                            )
                        self._voice_raw_input_trigger_pending = False
                        self._voice_audio_start_fallback_pending = False
                        if (
                            gesture_decision
                            is voice_gesture.VoiceGestureDecision.LATCH
                        ):
                            # Tap: the host shortcut already got one complete
                            # press/release, so the input method sees a real
                            # short press.  The microphone itself stays open
                            # until the next tap.
                            self._voice_ignore_audio_start_until_physical_down = (
                                False
                            )
                            self._voice_pcm_forwarding_enabled = True
                            self._set_runtime_voice_active(True)
                            self._set_runtime_voice_result(
                                bridge_runtime_status.VOICE_RUNTIME_ACTIVE
                            )
                            if (
                                self._voice_mic_gesture_audio_stopped
                                and not self._uses_local_voice_microphone()
                            ):
                                # The device already dropped its stream during
                                # this press; the latch needs it back open.
                                # 模式2 不需要：声音来自本机麦克风。
                                try:
                                    if self._ble_session is not None:
                                        self._ble_session.send_mic_open_threadsafe()
                                except BaseException as exc:  # noqa: BLE001
                                    self._logger.warning(
                                        "voice tap latch MIC_OPEN failed: "
                                        "error_type=%s",
                                        type(exc).__name__,
                                    )
                            self._logger.info(
                                "voice mic tap latched open until the next tap"
                            )
                        else:
                            self._voice_gesture.reset()
                            self._voice_pcm_forwarding_enabled = False
                            self._stop_local_voice_capture_locked()
                            if not self._voice_audio_stream_active:
                                self._set_runtime_voice_active(False)
                    if (
                        self._voice_mic_gesture_active
                        and (
                            matched_hid_released
                            or (
                                not self._voice_mic_gesture_sources_down
                                and self._voice_mic_gesture_audio_stopped
                            )
                        )
                    ):
                        self._finish_voice_mic_gesture(
                            reset_gesture=(
                                gesture_decision
                                is not voice_gesture.VoiceGestureDecision.LATCH
                            )
                        )
                    if short_tap:
                        # 短按松手：长按定时器可能还没到点，撤掉它并丢弃预录，
                        # 否则松手之后它照样会把 Vokie 叫起来。
                        self._cancel_plugin_voice_hold_pending_locked()
                        self._cancel_plugin_voice_capture_locked(
                            "physical mic release"
                        )
                    if short_tap and not self._plugin_voice_started_this_press:
                        self._handle_voice_short_tap_locked(
                            "physical mic release"
                        )
                    self._apply_pending_voice_settings_if_idle_locked()
                return
            with self._voice_shortcut.lock:
                self._voice_ignore_audio_start_until_physical_down = False
                if self._voice_shortcut_physical_sources_down:
                    self._voice_shortcut_physical_sources_down.add(event_source)
                    self._begin_voice_mic_gesture(
                        event_source,
                        physical_down=True,
                    )
                    self._logger.info(
                        "duplicate voice physical press merged: source=%s",
                        event_source,
                    )
                    return
                self._voice_shortcut_physical_sources_down.add(event_source)
                if self._voice_gesture.latched:
                    self._voice_shortcut_physical_sources_down.add(event_source)
                    # A second tap ends the latched session.
                    if (
                        self._voice_gesture.on_press()
                        == voice_gesture.VoiceGestureDecision.CLOSE
                    ):
                        self._close_latched_voice_session_locked("second tap")
                    self._apply_pending_voice_settings_if_idle_locked()
                    return
                self._rollover_completed_voice_mic_gesture_locked(event_source)
                if not self._prepare_voice_mapping_locked(
                    button_id,
                    primary_action,
                ):
                    return
                began = self._begin_voice_mic_gesture(
                    event_source,
                    physical_down=True,
                )
                # Record the physical edge before any early return: the
                # tap/hold decision is only made at release, so the gesture has
                # to be armed even when the device's audio-start event already
                # opened the session and the HID edge arrives afterwards.
                # ``on_press`` is idempotent while already holding.
                gesture_decision = self._voice_gesture.on_press()
                if not began:
                    self._logger.info(
                        "voice physical trigger ignored: same mic gesture source=%s",
                        event_source,
                    )
                    return
                if self._voice_shortcut.controller.active:
                    if self._plugin_voice_injected_tokens is not None:
                        # 上一次插件按键的抬起丢了，会话还挂着。先把卡住的
                        # 键补抬起并结束会话，否则这次按下会被拒到安全计时器
                        # 超时为止（键盘会一直带着 Alt）。
                        self._logger.info(
                            "plugin voice session stale; force releasing before press"
                        )
                        self._force_release_plugin_voice_tokens_locked(
                            "stale plugin session on new press"
                        )
                        self._voice_shortcut.controller.reset()
                        self._finish_voice_mic_gesture()
                        self._begin_voice_mic_gesture(event_source, physical_down=True)
                        gesture_decision = self._voice_gesture.on_press()
                    elif self._voice_delayed_press_token is not None:
                        # 上一记的按键还没落地，而且用户已经松过手了：那一次
                        # 作废，这次真实按下重新走一遍（同样延迟下发），不能
                        # 把它当成仍在按住。
                        self._logger.info(
                            "delayed voice press superseded by a new physical press"
                        )
                        self._drop_voice_delayed_press_locked()
                    else:
                        self._logger.info(
                            "voice physical trigger ignored: hold session already active"
                        )
                        return
                if gesture_decision != voice_gesture.VoiceGestureDecision.OPEN:
                    self._logger.info(
                        "voice physical trigger ignored: gesture not idle"
                    )
                    return
                # The physical key is the earliest reliable signal. Send the
                # host shortcut before the device's audio-start event so
                # voice input is already armed when PCM arrives. The matching
                # ATVV event is consumed by this pending latch.
                self._voice_raw_input_trigger_pending = True
                self._logger.info(
                    "voice physical mic trigger received before audio start"
                )
                self._handle_mic_button_pressed(
                    send_device_open=False,
                )
                if not self._voice_shortcut.controller.active:
                    self._voice_raw_input_trigger_pending = False
            return

        with self._button_mapping_lock:
            modifier = key_mapping.button_combo_modifier(self._bindings)
            configured_combo_buttons = frozenset(
                candidate
                for candidate in key_mapping.COMBO_ACTION_BUTTON_IDS
                if key_mapping.button_combo_action_for(
                    self._bindings, candidate
                ).kind
                != key_mapping.ActionKind.DISABLED
            )
            commands = (
                self._button_combos.press(
                    button_id,
                    modifier=modifier,
                    configured_buttons=configured_combo_buttons,
                )
                if is_pressed
                else self._button_combos.release(button_id)
            )
            self._dispatch_button_combo_commands(commands)
        self._apply_pending_settings_if_idle()

    def _block_input_until_release(self, buttons: set[str]) -> None:
        if not buttons:
            return
        self._input_rearm_blocked_buttons.update(buttons)

    def _dispatch_button_combo_commands(
        self, commands: List[button_combo.ComboCommand]
    ) -> None:
        for command in commands:
            if command.kind == button_combo.ComboCommandKind.FORWARD_PRESS:
                self._button_gestures.press(command.button_id)
            elif command.kind == button_combo.ComboCommandKind.FORWARD_RELEASE:
                self._button_gestures.release(command.button_id)
            elif command.kind == button_combo.ComboCommandKind.TRIGGER:
                self._on_button_combo_trigger(command.button_id)

    def _on_button_combo_trigger(self, button_id: str) -> None:
        action = key_mapping.button_combo_action_for(self._bindings, button_id)
        if action.kind == key_mapping.ActionKind.DISABLED:
            return
        self._logger.info(
            "button combination triggered: modifier=%s button=%s action=%s",
            key_mapping.button_combo_modifier(self._bindings),
            button_id,
            action.kind.value,
        )
        self._apply_button_action(action)

    def _is_default_hold_button(self, button_id: str) -> bool:
        """Whether a long press of this button should hold its single key.

        Every button that is not explicitly configured with a secondary
        long-press/double-click action and that is not a mouse-button hold
        keeps the ordinary press/release edges: the single-click key is
        pressed down at press time and released only when the physical button
        is released.  Because injected keys never auto-repeat, the app replays
        the key-down at the machine's keyboard repeat rate while the button
        stays down.  There is no per-button exception: arrows, backspace,
        volume and custom combinations all hold the same way.
        """

        if self._is_mouse_hold_button(button_id):
            return False
        action = key_mapping.button_action_for(
            self._bindings, button_id, key_mapping.ButtonTrigger.SINGLE_CLICK
        )
        return self._button_action_key_tokens(action) is not None

    def _on_button_hold_start(self, button_id: str, token: object) -> None:
        if self._is_mouse_hold_button(button_id):
            self._on_button_trigger(
                button_id,
                button_gesture.ButtonTrigger.LONG_PRESS,
                hold_owner=token,
            )
            return
        self._begin_button_key_hold(token, button_id)

    def _on_button_hold_end(self, token: object) -> None:
        self._end_button_mouse_hold(token)
        self._end_button_key_hold(token)

    def _is_mouse_hold_button(self, button_id: str) -> bool:
        return key_mapping.button_action_for(
            self._bindings, button_id, key_mapping.ButtonTrigger.LONG_PRESS
        ).kind in key_mapping.MOUSE_HOLD_BUTTONS

    def _begin_button_mouse_hold(self, owner: object, action: key_mapping.ButtonAction) -> None:
        button = key_mapping.MOUSE_HOLD_BUTTONS[action.kind]
        with self._button_action_lock:
            if not self._accept_input_events or not self._release_pending_button_inputs():
                return
            owners = self._button_mouse_holds.get(button)
            if owners:
                owners.add(owner)
                return
            try:
                win32_input.send_mouse_button_down(button)
            except win32_input.InputCleanupIncompleteError:
                self._button_hold_release_pending.add(button)
                self._schedule_button_input_release_retry_locked()
                self._logger.exception("mouse hold down has pending cleanup: %s", button)
            except (win32_input.MouseButtonInUseError, win32_input.Win32InputUnavailableError, OSError):
                self._logger.info("mouse hold down skipped: %s", button, exc_info=True)
            else:
                self._button_mouse_holds[button] = {owner}

    def _end_button_mouse_hold(self, owner: object) -> None:
        with self._button_action_lock:
            for button, owners in list(self._button_mouse_holds.items()):
                owners.discard(owner)
                if not owners:
                    del self._button_mouse_holds[button]
                    self._button_hold_release_pending.add(button)
            self._release_pending_button_inputs()

    def _begin_button_key_hold(self, owner: object, button_id: str) -> None:
        """Start the whole-hold behaviour for the button's single-click action.

        Under Interception the single-click key is held down for the whole
        physical hold and the matching key-up is delivered by
        ``_end_button_key_hold``, so the target sees one sustained key press.
        Under SendInput the driver cannot emulate that hold, so direction,
        backspace and volume buttons keep the original click-repeat behaviour:
        the first press and every repeat deliver a full tap (down+up).  Element
        navigation keeps its tap behaviour because it consumes mapped
        navigation keys itself.
        """

        action = key_mapping.button_action_for(
            self._bindings, button_id, key_mapping.ButtonTrigger.SINGLE_CLICK
        )
        tokens = self._button_action_key_tokens(action)
        if tokens is None:
            return
        with self._button_action_lock:
            if not self._accept_input_events:
                return
            if not self._release_pending_button_inputs():
                self._logger.info(
                    "button hold suppressed: an earlier input release is still pending"
                )
                return
            navigation_vk = _BUTTON_ACTION_NAVIGATION_VKS.get(action.kind)
            if action.kind == key_mapping.ActionKind.KEY_COMBO:
                navigation_vk = _BUTTON_NAVIGATION_SINGLE_KEYS.get(tuple(action.keys))
            if navigation_vk is not None and element_navigation_control_windows.route_mapped_navigation_key(navigation_vk):
                # Element navigation consumes the mapped key itself and moves
                # one step per tap.  This call already delivered the first step;
                # remember the action so each repeat re-runs it, and skip the
                # injected sustained key-down.
                self._button_tap_hold_actions[owner] = action
                return
            if (
                action.inject_mode != key_mapping.INJECT_MODE_INTERCEPTION
                and action.kind in _SENDINPUT_CLICK_REPEAT_KINDS
            ):
                # SendInput cannot emulate a Windows key hold, so direction,
                # backspace and volume buttons keep the original click-repeat
                # behaviour: this first press delivers one full tap and each
                # repeat re-runs the same tap through the ordinary action path.
                self._button_tap_hold_actions[owner] = action
                self._apply_button_action_locked(action)
                return
            backend = injection_backend.backend_for(action.inject_mode)
            try:
                backend.key_down(tokens)
            except injection_backend.InterceptionUnavailableError as exc:
                self._logger.warning(
                    "button hold skipped: interception backend unavailable: %s", exc
                )
                return
            except win32_input.Win32InputUnavailableError as exc:
                self._logger.info("button hold unavailable: %s", exc)
                return
            except win32_input.InputCleanupIncompleteError:
                self._logger.exception("button hold key-down remains incomplete")
                self._button_key_release_pending = tokens
                self._schedule_button_input_release_retry_locked()
                return
            except OSError as exc:
                self._logger.exception(
                    "button hold failed to deliver: error_type=%s",
                    type(exc).__name__,
                )
                return
            self._button_key_holds[owner] = (tokens, backend)

    def _end_button_key_hold(self, owner: object) -> None:
        with self._button_action_lock:
            self._button_tap_hold_actions.pop(owner, None)
            entry = self._button_key_holds.pop(owner, None)
            if entry is None:
                return
            tokens, backend = entry
            try:
                backend.key_up(tokens)
            except win32_input.InputCleanupIncompleteError:
                self._logger.exception("button hold key-up remains incomplete")
                self._button_key_release_pending = tokens
                self._schedule_button_input_release_retry_locked()
                return
            except win32_input.Win32InputUnavailableError:
                self._logger.info("button hold key-up unavailable")
                self._button_key_release_pending = tokens
                self._schedule_button_input_release_retry_locked()
                return
            except OSError as exc:
                # send_key_combo_up raises ordinary OSError only after its own
                # per-key fallback confirmed every requested key-up.
                self._logger.exception(
                    "button hold key-up needed fallback but completed: error_type=%s",
                    type(exc).__name__,
                )
            self._finish_button_input_release_if_complete_locked()

    def _release_pending_mouse_holds_locked(self) -> bool:
        for button in tuple(self._button_hold_release_pending):
            try:
                win32_input.send_mouse_button_up(button)
            except (win32_input.InputCleanupIncompleteError, win32_input.Win32InputUnavailableError):
                self._logger.warning("mouse hold release remains pending: %s", button)
                continue
            except OSError:
                # Ordinary OSError means the sender confirmed fallback release.
                self._logger.info("mouse hold release completed through fallback: %s", button)
            self._button_hold_release_pending.remove(button)
        if self._button_hold_release_pending:
            self._schedule_button_input_release_retry_locked()
            return False
        self._finish_button_input_release_if_complete_locked()
        return True

    def _is_button_combo_participant(self, button_id: str) -> bool:
        modifier = key_mapping.button_combo_modifier(self._bindings)
        if modifier is None:
            return False
        return button_id == modifier or (
            key_mapping.button_combo_action_for(self._bindings, button_id).kind
            != key_mapping.ActionKind.DISABLED
        )

    def _is_button_action_configured(
        self, button_id: str, trigger: button_gesture.ButtonTrigger
    ) -> bool:
        if button_id in self._removed_voice_bindings:
            return False
        action = key_mapping.button_action_for(self._bindings, button_id, trigger)
        if action.kind == key_mapping.ActionKind.DISABLED:
            return False
        if key_mapping.is_voice_action(action):
            return (
                trigger.value == key_mapping.ButtonTrigger.SINGLE_CLICK.value
                and self._is_voice_primary_button(button_id, action)
            )
        if action.kind == key_mapping.ActionKind.KEY_COMBO:
            try:
                win32_keys.resolve_vk_codes(action.keys)
            except win32_keys.UnknownKeyTokenError:
                return False
        if action_executor.is_application_action(action):
            # The action is intentional even if the app is currently not
            # installed.  Do not scan Start Menu/WindowsApps from the Raw
            # Input callback; dispatch will report the missing executable and
            # the configured mapping still correctly owns this physical key.
            return True
        return True

    def _on_button_hold_repeat(self, button_id: str, token: object) -> None:
        """Replay a held button at the machine's keyboard rate.

        Injected keys never auto-repeat, so a physical hold is simulated by the
        app.  Interception-held buttons re-send only the key-down until release
        (the matching key-up is delivered once by ``_end_button_key_hold``),
        which makes them feel like a physical keyboard hold.  SendInput-held
        direction, backspace and volume buttons re-run the whole click instead,
        as does element navigation, which consumes mapped navigation keys
        itself.
        """

        with self._button_action_lock:
            entry = self._button_key_holds.get(token)
            if entry is not None:
                tokens, backend = entry
                try:
                    backend.key_down(tokens)
                except injection_backend.InterceptionUnavailableError as exc:
                    self._logger.warning(
                        "button hold repeat skipped: interception backend "
                        "unavailable: %s",
                        exc,
                    )
                except win32_input.Win32InputUnavailableError as exc:
                    self._logger.info("button hold repeat unavailable: %s", exc)
                except win32_input.InputCleanupIncompleteError:
                    self._logger.exception(
                        "button hold repeat key-down remains incomplete"
                    )
                    self._button_key_release_pending = tokens
                    self._schedule_button_input_release_retry_locked()
                except OSError as exc:
                    self._logger.exception(
                        "button hold repeat failed to deliver: error_type=%s",
                        type(exc).__name__,
                    )
                return
            action = self._button_tap_hold_actions.get(token)
        if action is not None:
            self._apply_button_action(action)

    def _button_repeat_interval(self, button_id: str, repeat_count: int) -> float:
        """Repeat a held remote button at the machine's keyboard repeat rate.

        SendInput-injected keys never auto-repeat, so a held remote button is
        re-sent by the app.  Following the user's own keyboard repeat rate
        keeps arrows, backspace and volume holds as smooth as holding a key on
        a physical keyboard, without any per-button exceptions.
        """

        return self._keyboard_repeat_interval_seconds

    def _on_button_gesture_diagnostic(
        self, event: str, button_id: str, **fields: object
    ) -> None:
        self._diagnostic_trace.emit(
            event,
            gesture_id=self._diagnostic_trace.current_gesture(button_id) or "",
            button_id=str(button_id),
            **fields,
        )

    def _on_button_trigger(
        self, button_id: str, trigger: button_gesture.ButtonTrigger,
        *, hold_owner: Optional[object] = None,
    ) -> None:
        # ButtonGestureDispatcher keeps a callback reservation until this
        # function returns, so settings reload cannot replace the mapping we
        # are reading. Avoid taking _button_mapping_lock here: immediate input
        # enters the dispatcher while already holding it, whereas timer
        # callbacks are serialized by the dispatcher's callback gate.
        if button_id in self._removed_voice_bindings:
            self._logger.warning(
                "delayed button gesture suppressed until removed voice mapping "
                "is reselected: %s",
                button_id,
            )
            return
        action = key_mapping.button_action_for(
            self._bindings,
            button_id,
            key_mapping.ButtonTrigger(trigger.value),
        )
        self._diagnostic_trace.emit(
            "mapping_trigger",
            gesture_id=self._diagnostic_trace.current_gesture(button_id) or "",
            attempt_id=self._diagnostic_trace.current_context()["attempt_id"],
            button_id=str(button_id),
            trigger=str(trigger.value),
            action=str(action.kind.value),
        )
        try:
            if action.kind == key_mapping.ActionKind.KEY_COMBO:
                win32_keys.resolve_vk_codes(action.keys)
        except (
            KeyError,
            TypeError,
            ValueError,
            win32_keys.UnknownKeyTokenError,
        ):
            # A hand-edited or partially corrupted bindings file must disable
            # only the affected button, never escape the Raw Input callback
            # and tear down ordinary-button processing for the whole device.
            self._logger.warning(
                "invalid button binding ignored: button=%s trigger=%s",
                button_id,
                trigger.value,
            )
            return
        if action.kind == key_mapping.ActionKind.VOICE_MODE_CYCLE:
            self._switch_voice_mode_by_button(button_id, trigger)
            return
        if key_mapping.is_voice_action(action):
            self._logger.warning(
                "secondary voice action ignored: button=%s trigger=%s",
                button_id,
                trigger.value,
            )
            return
        if action.kind in key_mapping.MOUSE_HOLD_BUTTONS:
            if hold_owner is not None:
                self._begin_button_mouse_hold(hold_owner, action)
        else:
            self._apply_button_action(action)

    def _apply_button_action(self, action: key_mapping.ButtonAction) -> None:
        with self._button_action_lock:
            self._apply_button_action_locked(action)

    def _apply_button_action_locked(self, action: key_mapping.ButtonAction) -> None:
        if (
            self._button_key_release_pending is not None
            or self._button_mouse_release_pending is not None
            or self._button_hold_release_pending
        ) and not self._release_pending_button_inputs():
            self._logger.info(
                "button action suppressed: an earlier input release is still pending"
            )
            return

        action_name = str(action.kind.value)
        action_success = False
        action_error = ""
        action_error_type = ""
        self._diagnostic_trace.set_current_action(action_name)
        try:
            if action.kind == key_mapping.ActionKind.DISABLED:
                return
            navigation_vk = _BUTTON_ACTION_NAVIGATION_VKS.get(action.kind)
            if action.kind == key_mapping.ActionKind.KEY_COMBO:
                navigation_vk = _BUTTON_NAVIGATION_SINGLE_KEYS.get(tuple(action.keys))
            if navigation_vk is not None and element_navigation_control_windows.route_mapped_navigation_key(navigation_vk):
                # Accepted commands must not also be injected through SendInput.
                pass
            elif action.inject_mode == key_mapping.INJECT_MODE_INTERCEPTION and (
                injection_backend.key_tokens_for_action(action) is not None
                or injection_backend.mouse_button_for_action(action) is not None
            ):
                # Only key/mouse actions are Interception-deliverable. Anything
                # else (element-navigation toggle, Quicker URI, open-app, voice)
                # must fall through to its own handler instead of being silently
                # swallowed as "interception_unsupported_action".
                action_error, action_error_type = self._apply_button_action_interception(
                    action
                )
            elif action.kind == key_mapping.ActionKind.KEY_COMBO:
                win32_input.send_key_combo_tap(action.keys)
            elif action.kind == key_mapping.ActionKind.ESCAPE:
                win32_input.send_escape()
            elif action.kind == key_mapping.ActionKind.RETURN:
                win32_input.send_return()
            elif action.kind == key_mapping.ActionKind.ARROW_UP:
                win32_input.send_arrow_up()
            elif action.kind == key_mapping.ActionKind.ARROW_DOWN:
                win32_input.send_arrow_down()
            elif action.kind == key_mapping.ActionKind.ARROW_LEFT:
                win32_input.send_arrow_left()
            elif action.kind == key_mapping.ActionKind.ARROW_RIGHT:
                win32_input.send_arrow_right()
            elif action.kind == key_mapping.ActionKind.DELETE_BACKWARD:
                win32_input.send_delete_backward()
            elif action.kind == key_mapping.ActionKind.SHOW_DESKTOP:
                win32_input.send_show_desktop()
            elif action.kind == key_mapping.ActionKind.CONTEXT_MENU:
                win32_input.send_context_menu()
            elif action.kind == key_mapping.ActionKind.APP_SWITCHER:
                win32_input.send_app_switcher()
            elif action.kind == key_mapping.ActionKind.SYSTEM_VOLUME_UP:
                win32_input.send_volume_up()
            elif action.kind == key_mapping.ActionKind.SYSTEM_VOLUME_DOWN:
                win32_input.send_volume_down()
            elif action.kind == key_mapping.ActionKind.SYSTEM_VOLUME_MUTE:
                win32_input.send_volume_mute()
            elif action.kind == key_mapping.ActionKind.PLAY_PAUSE:
                win32_input.send_play_pause()
            elif action.kind in _BUTTON_ACTION_MOUSE_BUTTONS:
                if _BUTTON_ACTION_MOUSE_BUTTONS[action.kind] in self._button_mouse_holds:
                    return
                win32_input.send_mouse_button_click(
                    _BUTTON_ACTION_MOUSE_BUTTONS[action.kind]
                )
            elif action.kind == key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE:
                try:
                    result = (
                        element_navigation_control_windows.toggle_element_navigation()
                    )
                except Exception as exc:
                    action_error = "element_navigation_failed"
                    action_error_type = type(exc).__name__
                    self._logger.exception("element navigation toggle failed unexpectedly")
                    return
                if (
                    result.kind
                    == element_navigation_control_windows.ToggleResultKind.FAILED
                ):
                    action_error = "element_navigation_failed"
                    self._logger.warning(
                        "element navigation toggle failed: %s",
                        result.error or "unknown_error",
                    )
                self._diagnostic_trace.emit(
                    "element_navigation_toggle",
                    **self._diagnostic_trace.current_context(),
                    result=str(result.kind.value),
                    target_hwnd=int(result.target_hwnd),
                )
            elif action.kind == key_mapping.ActionKind.QUICKER_URI:
                action_executor.open_quicker_uri(action)
            elif action_executor.is_application_action(action):
                if not open_configured_application(action):
                    action_error = "application_unavailable"
                    self._logger.warning(
                        "application action unavailable: action=%s", action.kind.value
                    )
            # Voice actions are edge-driven in _on_button_event and never
            # enter this tap-only ordinary action executor.
            action_success = not action_error
        except win32_input.MouseButtonInUseError:
            action_error = "mouse_button_in_use"
            self._logger.info(
                "mouse button action skipped: the physical button is already held"
            )
        except win32_input.Win32InputUnavailableError:
            action_error = "send_input_unavailable"
            self._logger.info("button action skipped: SendInput unavailable here")
        except win32_input.InputCleanupIncompleteError:
            action_error = "input_cleanup_incomplete"
            tokens = self._button_action_key_tokens(action)
            if tokens is not None:
                self._button_key_release_pending = tokens
            mouse_button = _BUTTON_ACTION_MOUSE_BUTTONS.get(action.kind)
            if mouse_button is not None:
                self._button_mouse_release_pending = mouse_button
            self._schedule_button_input_release_retry_locked()
            self._logger.exception(
                "button action failed and safety input-up remains pending"
            )
        except OSError as exc:
            action_error = "delivery_failed"
            action_error_type = type(exc).__name__
            self._logger.exception(
                "button action failed to fully deliver: error_type=%s", action_error_type
            )
        finally:
            self._diagnostic_trace.emit(
                "action_result",
                **self._diagnostic_trace.current_context(),
                success=action_success,
                error=action_error,
                error_type=action_error_type,
            )
            self._diagnostic_trace.set_current_action(None)

    @staticmethod
    def _button_action_key_tokens(
        action: key_mapping.ButtonAction,
    ) -> Optional[Tuple[str, ...]]:
        if action.kind == key_mapping.ActionKind.KEY_COMBO:
            return tuple(action.keys)
        return injection_backend.SEMANTIC_ACTION_KEY_TOKENS.get(action.kind)

    def _apply_button_action_interception(
        self, action: key_mapping.ButtonAction
    ) -> Tuple[str, str]:
        """Deliver one action through the Interception driver.

        Returns ``(error, error_type)``; an empty ``error`` means success.

        There is deliberately no fallback to SendInput: the user picked this
        backend for a target window that ignores synthetic input, so silently
        downgrading would look like "the button does nothing" all over again.
        The settings UI greys the option out when the driver is missing.
        """

        backend = injection_backend.backend_for(
            key_mapping.INJECT_MODE_INTERCEPTION
        )
        try:
            tokens = injection_backend.key_tokens_for_action(action)
            if tokens is not None:
                backend.key_tap(tokens)
                return "", ""
            mouse_button = injection_backend.mouse_button_for_action(action)
            if mouse_button is not None:
                backend.mouse_click(mouse_button)
                return "", ""
        except injection_backend.InterceptionUnavailableError as exc:
            self._logger.warning(
                "interception action skipped: backend unavailable: %s", exc
            )
            return "interception_unavailable", type(exc).__name__
        except OSError as exc:
            self._logger.exception(
                "interception action failed to deliver: error_type=%s",
                type(exc).__name__,
            )
            return "delivery_failed", type(exc).__name__
        self._logger.warning(
            "interception action unsupported: action=%s", action.kind.value
        )
        return "interception_unsupported_action", ""

    def _cancel_button_input_release_retry_locked(
        self,
        *,
        reset_delay: bool = True,
    ) -> None:
        timer = self._button_input_release_retry_timer
        self._button_input_release_retry_timer = None
        self._button_input_release_retry_token = None
        if reset_delay:
            self._button_input_release_retry_delay = (
                _BUTTON_INPUT_RELEASE_RETRY_INITIAL_SECONDS
            )
        if timer is None:
            return
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except BaseException:
                pass

    def _schedule_button_input_release_retry_locked(self) -> None:
        if (
            self._button_input_release_retry_stopping
            or not self._accept_input_events
            or self._button_input_release_retry_timer is not None
            or (
                self._button_key_release_pending is None
                and self._button_mouse_release_pending is None
                and not self._button_hold_release_pending
            )
        ):
            return
        token = object()
        try:
            timer = self._button_input_release_timer_factory(
                self._button_input_release_retry_delay,
                lambda token=token: self._retry_pending_button_inputs(token),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._button_input_release_retry_token = token
            self._button_input_release_retry_timer = timer
            timer.start()
        except BaseException as exc:
            if self._button_input_release_retry_token is token:
                self._button_input_release_retry_token = None
                self._button_input_release_retry_timer = None
            self._logger.error(
                "button input safety-release timer failed: error_type=%s",
                type(exc).__name__,
            )

    def _retry_pending_button_inputs(self, token: object) -> None:
        with self._button_action_lock:
            if self._button_input_release_retry_token is not token:
                return
            self._button_input_release_retry_token = None
            self._button_input_release_retry_timer = None
            if (
                self._button_input_release_retry_stopping
                or not self._accept_input_events
            ):
                return
            self._button_input_release_retry_delay = min(
                _BUTTON_INPUT_RELEASE_RETRY_MAX_SECONDS,
                max(
                    _BUTTON_INPUT_RELEASE_RETRY_INITIAL_SECONDS,
                    self._button_input_release_retry_delay * 2.0,
                ),
            )
            self._release_pending_button_inputs()

    def _finish_button_input_release_if_complete_locked(self) -> None:
        if (
            self._button_key_release_pending is None
            and self._button_mouse_release_pending is None
            and not self._button_hold_release_pending
        ):
            self._cancel_button_input_release_retry_locked()

    def _release_pending_button_keys(self) -> bool:
        with self._button_action_lock:
            tokens = self._button_key_release_pending
            if tokens is None:
                self._finish_button_input_release_if_complete_locked()
                return True
            try:
                win32_input.send_key_combo_up(tokens)
            except win32_input.InputCleanupIncompleteError:
                self._logger.exception("button key safety release remains incomplete")
                self._schedule_button_input_release_retry_locked()
                return False
            except win32_input.Win32InputUnavailableError:
                self._logger.info("button key safety release unavailable")
                self._schedule_button_input_release_retry_locked()
                return False
            except OSError:
                # send_key_combo_up raises ordinary OSError only after its own
                # per-key fallback confirmed every requested key-up.
                self._logger.exception(
                    "button key safety release needed fallback but completed"
                )
            else:
                self._logger.info("button key safety release completed")
            self._button_key_release_pending = None
            self._finish_button_input_release_if_complete_locked()
            return True

    def _release_pending_button_mouse(self) -> bool:
        with self._button_action_lock:
            button = self._button_mouse_release_pending
            if button is None:
                self._finish_button_input_release_if_complete_locked()
                return True
            try:
                win32_input.send_mouse_button_up(button)
            except win32_input.InputCleanupIncompleteError:
                self._logger.exception("button mouse safety release remains incomplete")
                self._schedule_button_input_release_retry_locked()
                return False
            except win32_input.Win32InputUnavailableError:
                self._logger.info("button mouse safety release unavailable")
                self._schedule_button_input_release_retry_locked()
                return False
            except OSError:
                self._logger.exception(
                    "button mouse safety release needed fallback but completed"
                )
            else:
                self._logger.info("button mouse safety release completed")
            self._button_mouse_release_pending = None
            self._finish_button_input_release_if_complete_locked()
            return True

    def _release_pending_button_inputs(self) -> bool:
        with self._button_action_lock:
            key_complete = self._release_pending_button_keys()
            mouse_complete = self._release_pending_button_mouse()
            hold_complete = self._release_pending_mouse_holds_locked()
            return key_complete and mouse_complete and hold_complete

    # -- ATVV control-channel events (mic button + audio start/stop) ------

    def _on_control_event(
        self,
        event: object,
        *,
        _ble_generation: Optional[int] = None,
    ) -> None:
        if not self._accepts_ble_callback(_ble_generation):
            return
        atvv_session_id = getattr(event, "session_id", None)
        reason = getattr(event, "reason", None)
        self._diagnostic_trace.emit(
            "voice_control_event",
            event_type=type(event).__name__,
            atvv_session_id=atvv_session_id if atvv_session_id is not None else -1,
            reason=reason if reason is not None else -1,
        )
        # Some machines expose no usable Raw Input/F5 edge for the mic key,
        # leaving AudioStarted as the first event of the next physical press.
        # Refresh here as well so saved voice mode/hotkey edits do not depend
        # on an ordinary HID event arriving first.
        self._reload_settings_if_changed()
        if isinstance(event, CapsReceived):
            self._logger.info(
                "voice capabilities received: version=0x%04x codecs=0x%02x "
                "interaction=0x%02x selected_codec=0x%02x sample_rate=%s "
                "frame_size=%s",
                event.capabilities.version,
                event.capabilities.codecs,
                event.capabilities.interaction,
                event.capabilities.selected_codec,
                event.capabilities.sample_rate,
                event.capabilities.frame_size,
            )
        elif isinstance(event, MicButtonPressed):
            detection_handled, detection_captured = (
                self._handle_key_detection_mic_event("atvv_press", "atvv")
            )
            if detection_handled:
                if detection_captured:
                    self._logger.info(
                        "key detection captured button=mic source=atvv; "
                        "voice action suppressed"
                    )
                return
            if not self._accepts_ble_callback(_ble_generation):
                return
            with self._voice_shortcut.lock:
                extra_voice_session_owns_mic = (
                    self._extra_voice_slot_active is not None
                    or self._extra_voice_slot_close_pending is not None
                    or any(gesture.latched for gesture in self._extra_voice_gestures.values())
                )
            if extra_voice_session_owns_mic:
                self._logger.info(
                    "ATVV mic trigger merged into active extra voice slot=%s",
                    self._extra_voice_slot_active,
                )
                return
            mic_action = self._primary_button_action("mic")
            is_mic_voice_button = self._is_voice_primary_button(
                "mic",
                mic_action,
            )
            if not is_mic_voice_button:
                with self._voice_shortcut.lock:
                    if not self._accepts_ble_callback(_ble_generation):
                        return
                    if not self._voice_shortcut.controller.active:
                        self._logger.info(
                            "ATVV mic trigger ignored: physical mic has an ordinary mapping"
                        )
                        if (
                            self._ble_session is not None
                            and not self._unsolicited_mic_close_pending
                        ):
                            self._unsolicited_mic_close_pending = True
                            self._ble_session.send_mic_close_threadsafe()
                return
            with self._voice_shortcut.lock:
                if not self._accepts_ble_callback(_ble_generation):
                    return
                if not self._prepare_voice_mapping_locked("mic", mic_action):
                    if self._ble_session is not None:
                        self._ble_session.send_mic_close_threadsafe()
                    return
                if self._voice_mic_gesture_active:
                    self._voice_raw_input_trigger_pending = False
                    self._voice_audio_start_fallback_pending = False
                    self._logger.info(
                        "voice mic trigger ignored: matched current multi-source gesture"
                    )
                elif self._voice_gesture.latched:
                    # The physical mic has no HID release edge on this device,
                    # so a fresh ATVV trigger is the only signal that the user
                    # tapped again: it ends the latched tap session.
                    self._logger.info(
                        "voice mic trigger closes the latched tap session"
                    )
                    if (
                        self._voice_gesture.on_press()
                        == voice_gesture.VoiceGestureDecision.CLOSE
                    ):
                        self._close_latched_voice_session_locked("ATVV mic tap")
                else:
                    self._logger.info("voice mic trigger received from ATVV control channel")
                    if self._begin_voice_mic_gesture("atvv"):
                        # Arm the tap/hold decision now: the release has to be
                        # inferred from the matching audio stop, which is
                        # measured against this press instant.
                        self._voice_gesture.on_press()
                        self._handle_mic_button_pressed()
        elif isinstance(event, AudioStarted):
            self._diagnostic_trace.emit(
                "audio_started",
                **self._diagnostic_trace.current_context(),
                atvv_session_id=event.session_id if event.session_id is not None else -1,
                reason=event.reason if event.reason is not None else -1,
                codec=event.codec if event.codec is not None else -1,
            )
            detection_handled, detection_captured = (
                self._handle_key_detection_mic_event(
                    "audio_started",
                    "audio_started",
                )
            )
            if detection_handled:
                if detection_captured:
                    self._logger.info(
                        "key detection captured button=mic source=audio_started; "
                        "voice action suppressed"
                    )
                return
            with self._voice_shortcut.lock:
                if not self._accepts_ble_callback(_ble_generation):
                    return
                if self._plugin_voice_capture_pending:
                    # 按住插件键时话筒已经开了：先收着这条流，PCM 由
                    # _on_pcm_frame 缓存，检测到人声才真正开会话。
                    self._voice_audio_stream_active = True
                    self._voice_audio_stop_processed = False
                    self._voice_audio_start_fallback_pending = False
                    self._voice_pcm_forwarding_enabled = True
                    self._plugin_voice_audio_stream = True
                    self._voice_audio.stats.reset()
                    self._logger.info(
                        "plugin voice audio stream accepted while waiting for speech"
                    )
                    return
                if self._plugin_voice_audio_active:
                    # 插件会话已经拿到这条流：留着它，PCM 由 _on_pcm_frame
                    # 直接经回环桥推进 Vokie。
                    self._voice_audio_stream_active = True
                    self._voice_audio_stop_processed = False
                    self._voice_audio_start_fallback_pending = False
                    self._voice_pcm_forwarding_enabled = True
                    self._plugin_voice_audio_stream = True
                    self._voice_audio.stats.reset()
                    self._logger.info(
                        "plugin voice audio stream accepted for the plugin"
                    )
                    return
                if self._plugin_voice_session_active():
                    # 插件模式不收遥控器的音频：标成已处理，免得 PCM 被塞进
                    # 没打开的虚拟声卡，也免得松手的音频停止事件乱动快捷键。
                    self._voice_pcm_forwarding_enabled = False
                    self._voice_audio_stream_active = False
                    self._voice_audio_stop_processed = True
                    self._voice_audio_start_fallback_pending = False
                    self._logger.info(
                        "plugin voice mode active: device audio stream ignored"
                    )
                    return
                self._logger.info("voice audio started")
                if self._voice_audio_stream_active:
                    self._logger.info(
                        "voice duplicate audio start ignored: session_id=%s",
                        event.session_id,
                    )
                    return
                if (
                    self._voice_ignore_audio_start_until_physical_down
                    and not self._voice_gesture.latched
                ):
                    self._logger.info(
                        "late voice audio start ignored after physical mic release"
                    )
                    # Keep the host idle until a physical down confirms a new
                    # press. Do not roll back the device stream: that down can
                    # still enable PCM through the normal voice-open path.
                    self._voice_audio_stop_processed = False
                    self._voice_audio.stats.reset()
                    return
                self._voice_audio_stream_active = True
                self._voice_audio_stop_processed = False
                self._voice_audio.stats.reset()
                self._voice_audio_start_fallback_pending = False
                if (
                    self._voice_gesture.latched
                    and not self._voice_shortcut.controller.active
                ):
                    # A tap latched the microphone open. Resume playback
                    # without replaying the host shortcut: the input method
                    # already saw one complete short press.
                    self._voice_pcm_forwarding_enabled = True
                    self._set_runtime_voice_active(True)
                    self._set_runtime_voice_result(
                        bridge_runtime_status.VOICE_RUNTIME_ACTIVE
                    )
                    if self._voice_mic_gesture_active:
                        self._voice_mic_gesture_audio_started = True
                        self._voice_mic_gesture_audio_stopped = False
                    self._logger.info(
                        "voice audio resumed for latched mic session"
                    )
                    return
                if self._voice_shortcut.controller.active and (
                    self._voice_mic_gesture_sources_down
                    or self._voice_shortcut_physical_sources_down
                ):
                    # A device stream boundary does not end a physical hold.
                    # Resume PCM without replaying or releasing the owned chord.
                    self._voice_pcm_forwarding_enabled = True
                    self._set_runtime_voice_active(True)
                    self._set_runtime_voice_result(
                        bridge_runtime_status.VOICE_RUNTIME_ACTIVE
                    )
                    if self._voice_mic_gesture_active:
                        self._voice_mic_gesture_audio_started = True
                        self._voice_mic_gesture_audio_stopped = False
                    self._logger.info("voice audio resumed for physically held shortcut")
                    return
                extra_voice_owns_stream = (
                    self._extra_voice_slot_active is not None
                    or self._extra_voice_slot_close_pending is not None
                    or any(
                        gesture.latched
                        for gesture in self._extra_voice_gestures.values()
                    )
                )
                if (
                    extra_voice_owns_stream
                    and not self._voice_gesture.latched
                    and not self._voice_mic_gesture_active
                ):
                    # The device stream belongs to an extra voice slot (语音二/
                    # 语音三 bound to a non-microphone key), not to the physical
                    # mic button. Re-resolving the physical mic's slot here
                    # compares the wrong shortcut and fails the session closed,
                    # which is what made those keys appear to never open the
                    # microphone. Keep the stream and the slot's own shortcut.
                    self._voice_pcm_forwarding_enabled = True
                    self._set_runtime_voice_active(True)
                    self._set_runtime_voice_result(
                        bridge_runtime_status.VOICE_RUNTIME_ACTIVE
                    )
                    self._logger.info(
                        "voice audio resumed for extra voice slot=%s",
                        self._extra_voice_slot_active,
                    )
                    return
                mic_action = self._primary_button_action("mic")
                is_mic_voice_button = self._is_voice_primary_button(
                    "mic",
                    mic_action,
                )
                if not self._voice_shortcut.controller.active and not is_mic_voice_button:
                    self._abort_voice_audio_start_locked(
                        "physical mic has an ordinary mapping"
                    )
                    self._logger.info(
                        "unsolicited mic audio ignored: physical mic has an ordinary mapping"
                    )
                    if (
                        self._ble_session is not None
                        and not self._unsolicited_mic_close_pending
                    ):
                        self._unsolicited_mic_close_pending = True
                        self._ble_session.send_mic_close_threadsafe()
                    return
                if (
                    not self._voice_shortcut.controller.active
                    and not self._prepare_voice_mapping_locked("mic", mic_action)
                ):
                    self._abort_voice_audio_start_locked(
                        "mapped shortcut is unavailable"
                    )
                    self._logger.info(
                        "voice audio failing closed: mapped shortcut is unavailable"
                    )
                    if self._ble_session is not None:
                        self._ble_session.send_mic_close_threadsafe()
                    return
                if self._voice_mic_gesture_active:
                    self._rollover_completed_voice_mic_gesture_locked(
                        "audio_started"
                    )
                if self._voice_mic_gesture_active:
                    if self._voice_mic_gesture_audio_stopped:
                        self._logger.info(
                            "voice continuation audio start ignored until physical release"
                        )
                    else:
                        self._voice_mic_gesture_audio_started = True
                        self._logger.info(
                            "voice audio start matched current multi-source gesture"
                        )
                elif not self._voice_shortcut.controller.active:
                    self._logger.info("voice audio start used as microphone trigger")
                    if self._begin_voice_mic_gesture("audio_started"):
                        # Same tap/hold arming as the ATVV trigger: the release
                        # is inferred from the matching audio stop.
                        self._voice_gesture.on_press()
                        started = self._handle_mic_button_pressed(
                            send_device_open=False
                        )
                        if not started:
                            self._abort_voice_audio_start_locked(
                                "voice host start failed after audio started"
                            )
                        self._voice_audio_start_fallback_pending = (
                            self._voice_shortcut.controller.active
                        )
        elif isinstance(event, AudioStopped):
            self._diagnostic_trace.emit(
                "audio_stopped",
                **self._diagnostic_trace.current_context(),
                reason=event.reason if event.reason is not None else -1,
            )
            detection_handled, _ = self._handle_key_detection_mic_event(
                "audio_stopped",
                "audio_started",
            )
            if detection_handled:
                self._logger.info(
                    "key detection mic audio stopped; voice state unchanged"
                )
                return
            with self._voice_shortcut.lock:
                if not self._accepts_ble_callback(_ble_generation):
                    return
                if (
                    not self._voice_audio_stream_active
                    and self._voice_audio_stop_processed
                ):
                    self._logger.info("voice duplicate audio stop ignored")
                    return
                runtime_voice_was_active = self._runtime_voice_active
                physical_voice_sources_down = bool(
                    self._voice_mic_gesture_sources_down
                    or self._voice_shortcut_physical_sources_down
                )
                if (
                    not physical_voice_sources_down
                    and self._voice_gesture.state
                    is voice_gesture.VoiceGestureState.HOLDING
                ):
                    # The physical mic has no HID release edge on this device:
                    # the device stream stopping is the release signal. A short
                    # stream latches the microphone open, a long one closes it,
                    # mirroring the physical HID tap/hold decision.
                    if (
                        self._voice_gesture.on_release()
                        is voice_gesture.VoiceGestureDecision.LATCH
                    ):
                        self._release_voice_shortcut_edge_locked(
                            "audio stop after short voice press"
                        )
                        if (
                            self._voice_cycle_enabled()
                            and not self._plugin_voice_started_this_press
                        ):
                            # 短按切换模式：这一下只是用来切模式的，不锁存
                            # 麦克风，切完立刻把会话收干净。已经和 Vokie 说过
                            # 话的这一下不算短按，不能被切成模式。
                            self._finish_voice_mic_gesture()
                            self._handle_voice_short_tap_locked(
                                "audio stop after short voice press"
                            )
                        elif self._uses_local_voice_microphone():
                            self._finish_voice_mic_gesture(reset_gesture=False)
                        else:
                            # 模式1 只支持长按：短按一个键都没发，设备流也停
                            # 了，直接结束而不锁存。
                            self._finish_voice_mic_gesture()
                latched_session = (
                    (
                        self._voice_gesture.latched
                        or any(
                            gesture.latched
                            for gesture in self._extra_voice_gestures.values()
                        )
                    )
                    and not physical_voice_sources_down
                    and not self._voice_shortcut.controller.active
                )
                if not physical_voice_sources_down and not latched_session:
                    self._voice_gesture.reset()
                self._voice_audio_stream_active = False
                self._voice_audio_stop_processed = True
                if self._plugin_voice_capture_pending:
                    # 设备自己停流了：这次按下没等到人声，别把 Vokie 叫起来。
                    self._cancel_plugin_voice_capture_locked(
                        "device audio stop", close_mic=False
                    )
                if self._plugin_voice_audio_stream:
                    self._plugin_voice_audio_stream = False
                    self._end_plugin_voice_audio_locked("device audio stop")
                self._voice_pcm_forwarding_enabled = latched_session
                if not latched_session:
                    self._set_runtime_voice_active(False)
                flush_result = self._voice_audio.flush("audio stop")
                if flush_result.error is not None:
                    self._logger.error(
                        "voice playback flush completed with failure: %s",
                        flush_result.error,
                    )
                self._logger.info("voice audio stopped")
                stats = self._voice_audio.stats.summary()
                self._diagnostic_trace.emit(
                    "audio_summary",
                    **self._diagnostic_trace.current_context(),
                    frames=int(stats["frames"]),
                    samples=int(stats["samples"]),
                    audio_ms=float(stats["audio_ms"]),
                    peak=int(stats["peak"]),
                    result=str(stats["result"]),
                )
                self._logger.info(
                    "voice PCM summary: frames=%s samples=%s audio_ms=%.0f "
                    "peak=%s rms=%.1f mean_abs=%.1f mean=%.1f "
                    "clipped=%s(%.3f%%) zero_crossings=%s result=%s",
                    stats["frames"],
                    stats["samples"],
                    stats["audio_ms"],
                    stats["peak"],
                    stats["rms"],
                    stats["mean_abs"],
                    stats["mean"],
                    stats["clipped_samples"],
                    stats["clipped_pct"],
                    stats["zero_crossings"],
                    stats["result"],
                )
                timing_snapshot = getattr(
                    self._voice_audio.sink, "timing_snapshot", None
                )
                if callable(timing_snapshot):
                    timing = timing_snapshot()
                    self._logger.info(
                        "voice playback timing: open_ms=%.2f writes=%s "
                        "last_write_ms=%.2f max_write_ms=%.2f underflows=%s",
                        timing.open_elapsed_ms,
                        timing.write_count,
                        timing.last_write_elapsed_ms,
                        timing.max_write_elapsed_ms,
                        timing.underflow_count,
                )
                self._voice_audio_start_fallback_pending = False
                self._voice_raw_input_trigger_pending = False
                self._unsolicited_mic_close_pending = False
                if self._voice_mic_gesture_active:
                    self._voice_mic_gesture_audio_started = False
                    self._voice_mic_gesture_audio_stopped = True
                physical_voice_sources_down = bool(
                    self._voice_mic_gesture_sources_down
                    or self._voice_shortcut_physical_sources_down
                )
                if latched_session:
                    # The device dropped its stream, but the latched tap still
                    # owns an open microphone. Re-open it without touching the
                    # host shortcut or the gesture state.
                    self._voice_pcm_forwarding_enabled = True
                    self._set_runtime_voice_active(True)
                    self._set_runtime_voice_result(
                        bridge_runtime_status.VOICE_RUNTIME_ACTIVE
                    )
                    try:
                        if self._ble_session is not None:
                            self._ble_session.send_mic_open_threadsafe()
                    except BaseException as exc:  # noqa: BLE001 - never trap input
                        self._logger.warning(
                            "voice latched MIC_OPEN failed: error_type=%s",
                            type(exc).__name__,
                        )
                    self._logger.info(
                        "voice latched mic re-opened after audio stop"
                    )
                    self._apply_pending_voice_settings_if_idle_locked()
                    return
                if self._voice_delayed_press_token is not None:
                    # 窗口还没走完音频就停了：这一记按键整记作废，宿主一个键
                    # 都收不到（短按不误触语音）。
                    self._drop_voice_delayed_press_locked()
                    self._logger.info(
                        "voice delayed press dropped on audio stop before it landed"
                    )
                    action = None
                else:
                    action = (
                        None
                        if physical_voice_sources_down
                        else self._voice_shortcut.controller.on_audio_stopped()
                    )
                action_applied = (
                    True if action is None else self._voice_shortcut.apply(action)
                )
                if action_applied and (
                    action is not None or not physical_voice_sources_down
                ):
                    self._cancel_voice_hold_watchdog_locked()
                if action is not None and not action_applied:
                    # Same rule as _cleanup_once(): on_audio_stopped() already
                    # cleared the controller's pending state before we knew
                    # whether the closing key-up actually delivered. A failure
                    # here must not
                    # be recorded as a clean close - restore the owed state and
                    # fail closed by requesting a reconnect, the same way a BLE
                    # disconnect or a playback write failure does (XRBM-019
                    # review round 1 P1 #4).
                    self._voice_shortcut.controller.restore_pending(action)
                    self._logger.info(
                        "voice closing action failed to fully deliver; state retained, "
                        "requesting reconnect"
                    )
                    self._supervisor.request_reconnect()
                else:
                    if (
                        self._voice_mic_gesture_active
                        and self._voice_mic_gesture_audio_stopped
                        and not self._voice_mic_gesture_sources_down
                    ):
                        self._finish_voice_mic_gesture()
                if runtime_voice_was_active:
                    if action is not None and not action_applied:
                        self._set_runtime_voice_result(
                            bridge_runtime_status.VOICE_RUNTIME_HOST_STOP_FAILED
                        )
                    elif stats["frames"] > 0:
                        self._set_runtime_voice_result(
                            bridge_runtime_status.VOICE_RUNTIME_SUCCESS
                        )
                    else:
                        self._set_runtime_voice_result(
                            bridge_runtime_status.VOICE_RUNTIME_AUDIO_EMPTY
                        )
                observation = self._log_voice_submission_observation()
                text_changed = bool(
                    observation is not None
                    and observation.text_delta is not None
                    and observation.text_delta > 0
                )
                if text_changed:
                    attempt_result = "text_changed"
                elif (
                    getattr(self._voice_shortcut, "ui_confirmation", "unknown")
                    == "confirmed"
                    and stats["frames"] > 0
                ):
                    attempt_result = "host_confirmed_audio_ok"
                else:
                    attempt_result = "unknown"
                self._finish_voice_diagnostic_attempt(
                    attempt_result,
                    frames=int(stats["frames"]),
                    samples=int(stats["samples"]),
                    audio_signal=bool(stats["frames"] > 0),
                )
                self._apply_pending_voice_settings_if_idle_locked()

    def _record_device_diagnostic_context(self) -> None:
        selected = remote_selection.normalize(self._config.get(remote_selection.KEY))
        self._diagnostic_trace.emit(
            "device_context", app_version=__version__,
            selected_ref=selected["active"][:12],
            registered=[{"device_ref": row["key"][:12], "profile": row["profile"]}
                        for row in selected["devices"]],
        )

    def _ensure_voice_diagnostic_attempt(
        self,
        *,
        effective_hotkey_tokens: Optional[Tuple[str, ...]] = None,
    ) -> None:
        if self._voice_attempt_id is not None:
            return
        self._voice_shortcut.ui_confirmation = "unknown"
        self._voice_text_observation = "unknown"
        if not self._diagnostic_trace.enabled:
            # Preserve the active-attempt sentinel without collecting diagnostics.
            self._voice_attempt_id = ""
            return
        backend = _VOICE_HOTKEY_BACKEND_MARKED
        tokens = effective_hotkey_tokens or self._voice_shortcut.recorded_tokens()
        shortcut_mode = "hold"
        physicalizer_generation = -1
        physicalizer_installation_epoch = -1
        physicalizer_observed_generation = -1
        physicalizer_observed_installation_epoch = -1
        physicalizer_binding = "not_required"
        physicalizer_observation = "not_required"
        try:
            uses_right_alt = (
                win32_keys.VK_CODES["ralt"]
                in win32_keys.resolve_vk_codes(tokens)
            )
        except win32_keys.UnknownKeyTokenError:
            uses_right_alt = False
        if uses_right_alt and backend in {_VOICE_HOTKEY_BACKEND_MARKED}:
            with self._voice_key_physicalizer_lifecycle_lock:
                physicalizer = self._voice_key_physicalizer
                if physicalizer is not None:
                    physicalizer_observed_generation = int(
                        getattr(physicalizer, "tracker_generation", -1)
                    )
                    physicalizer_observed_installation_epoch = int(
                        getattr(physicalizer, "installation_epoch", -1)
                    )
                    physicalizer_binding = "observed"
                    physicalizer_observation = (
                        "accepting"
                        if bool(getattr(physicalizer, "accepts_new_down", True))
                        else "degraded"
                    )
                else:
                    physicalizer_binding = "unbound"
                    physicalizer_observation = "missing"
        self._voice_attempt_id = self._diagnostic_trace.begin_attempt(
            self._diagnostic_trace.current_gesture("mic"),
            provider="recorded_hotkey",
        )
        self._diagnostic_trace.emit(
            "voice_attempt_context",
            **self._diagnostic_trace.current_context(),
            backend=str(backend),
            provider_shortcut_mode=shortcut_mode,
            hotkey_tokens=list(tokens),
            physicalizer_binding=physicalizer_binding,
            physicalizer_generation=physicalizer_generation,
            physicalizer_installation_epoch=physicalizer_installation_epoch,
            physicalizer_observation=physicalizer_observation,
            physicalizer_observed_generation=physicalizer_observed_generation,
            physicalizer_observed_installation_epoch=(
                physicalizer_observed_installation_epoch
            ),
            **diagnostic_trace.foreground_context(),
        )

    def _finish_voice_diagnostic_attempt(
        self, result: str, **fields: object
    ) -> None:
        payload = {
            "host_ui": getattr(self._voice_shortcut, "ui_confirmation", "") or "unknown",
            "text_state": self._voice_text_observation or "unknown",
        }
        payload.update(fields)
        self._diagnostic_trace.end_attempt(
            self._voice_attempt_id,
            str(result),
            **payload,
        )
        self._voice_attempt_id = None

    def _handle_mic_button_pressed(
        self,
        *,
        send_device_open: bool = True,
        voice_slot: Optional[int] = None,
    ) -> bool:
        """Resolve and open the user-selected output endpoint FIRST; only
        send the hotkey if that succeeds, and only send MIC_OPEN if the
        hotkey itself fully delivered. This is the fail-closed ordering
        XRBM-014 review RETRY P1 #3 (endpoint) and review round 2 P1 #6
        (hotkey) both require: a device streaming audio into Windows
        without the configured hotkey having actually engaged voice typing
        is exactly the "opens after host-trigger failure" defect - so
        failure at either step suppresses MIC_OPEN, not just a missing
        endpoint.

        ``voice_slot`` names the hold-to-talk slot this press belongs to;
        the physical microphone resolves it from its own binding when the
        caller (an extra voice key) does not supply one.
        """

        if voice_slot is None:
            if self._voice_cycle_enabled():
                # 短按切换模式：话筒键跟着循环游标走，而不是它自己绑的那一条。
                voice_slot = self._active_voice_slot()
            else:
                voice_slot = key_mapping.voice_slot_for_action(
                    self._primary_button_action("mic")
                )

        if self._voice_slot_is_plugin(voice_slot):
            # 插件模式（Vokie）：插件已连接时长按会把遥控器的 PCM 直接推给
            # 它；插件没连上时退回只重放它的那条快捷键。两种都不开虚拟声卡。
            self._drop_voice_delayed_press_locked()
            self._voice_pcm_forwarding_enabled = False
            if self._voice_shortcut.pending_tokens is not None:
                if not self._voice_shortcut.release_pending():
                    self._voice_shortcut.pending_tokens = None
            self._force_release_plugin_voice_tokens_locked(
                "plugin voice press superseded"
            )
            # 短按只切换模式，绝不惊动 Vokie：按下先开话筒、把 PCM 缓起来，
            # 按满长按阈值才开插件会话并把缓冲补发过去。
            self._plugin_voice_started_this_press = False
            self._arm_plugin_voice_capture_locked(voice_slot)
            if not self._schedule_plugin_voice_hold_locked(voice_slot):
                self._set_runtime_voice_result(
                    bridge_runtime_status.VOICE_RUNTIME_HOST_START_FAILED
                )
                return False
            self._logger.info(
                "plugin voice key armed for long press: slot=%s", voice_slot
            )
            return True

        # 插件那次按压如果没收干净（插件中途断开、松手事件丢了），
        # 残留的会话会继续吞普通语音，“这次跟 Vokie 说过话”的标记也会
        # 一直留着，让短按切换永远被跳过。没有真正在进行的插件按压时，
        # 普通按下从这里把插件状态清干净再继续。
        bridge = vokie_bridge.active_bridge()
        plugin_press_pending = bool(
            bridge is not None
            and bridge.connected()
            and (
                self._plugin_voice_press_token is not None
                or self._plugin_voice_capture_pending
                or self._plugin_voice_audio_active
                or self._plugin_voice_injected_tokens is not None
            )
        )
        if not plugin_press_pending:
            self._force_release_plugin_voice_tokens_locked(
                "plugin state cleared before normal press"
            )
            self._plugin_voice_started_this_press = False

        # 用户又按下了话筒：还没落地的延迟按下让位给这次真实按下。
        self._drop_voice_delayed_press_locked()

        self._ensure_voice_diagnostic_attempt()
        if not self._accept_input_events or not self._accept_ble_events:
            self._voice_pcm_forwarding_enabled = False
            self._finish_voice_diagnostic_attempt(
                "failed_before_send", reason="input_unavailable"
            )
            return False

        if self._ble_session is None:
            self._voice_pcm_forwarding_enabled = False
            self._logger.info("voice ignored: BLE voice session is not connected")
            self._finish_voice_diagnostic_attempt(
                "failed_before_send", reason="ble_unavailable"
            )
            return False

        if self._voice_shortcut.pending_tokens is not None:
            if not self._voice_shortcut.release_pending():
                self._voice_pcm_forwarding_enabled = False
                self._set_runtime_voice_result(
                    bridge_runtime_status.VOICE_RUNTIME_HOST_STOP_FAILED
                )
                self._logger.info(
                    "voice failing closed: an earlier hotkey release is still pending"
                )
                self._finish_voice_diagnostic_attempt(
                    "failed_before_send", reason="previous_release_pending"
                )
                return False
            self._voice_shortcut.pending_tokens = None

        if not self._voice_audio.open():
            self._voice_pcm_forwarding_enabled = False
            self._set_runtime_voice_result(
                bridge_runtime_status.VOICE_RUNTIME_OUTPUT_OPEN_FAILED
            )
            self._logger.info(
                "voice failing closed: no usable output endpoint; hotkey/MIC_OPEN suppressed"
            )
            self._finish_voice_diagnostic_attempt(
                "failed_before_send", reason="output_open_failed"
            )
            return False

        self._capture_voice_focus_before()
        action = self._voice_shortcut.controller.on_mic_button_pressed()
        press_delayed = False
        if self._voice_shortcut_press_delay_enabled(voice_slot):
            # 短按开关打开：这一次按键推迟下发，并且只发这一次。
            press_delayed = self._arm_voice_shortcut_press_delay_locked(
                action, voice_slot
            )
        action_delivered = True
        if not press_delayed:
            action_delivered = self._voice_shortcut.apply(
                action,
                voice_slot=voice_slot,
                inject_mode=self._voice_slot_inject_mode(voice_slot),
            )
        if not action_delivered:
            self._voice_focus_before = None
            self._voice_focus_provider = ""
            self._voice_focus_submit_method = ""
            self._voice_pcm_forwarding_enabled = False
            self._set_runtime_voice_result(
                bridge_runtime_status.VOICE_RUNTIME_HOST_START_FAILED
            )
            # Nothing physically landed (win32_input.py's own batching already
            # rolled back any partial key-down), so clear the logical hold
            # without attempting a second delivery.
            self._voice_shortcut.controller.cancel_pending()
            self._logger.info(
                "voice failing closed: host hotkey delivery failed; device command suppressed"
            )
            self._finish_voice_diagnostic_attempt(
                "failed_after_send", reason="hotkey_delivery_failed"
            )
            return False

        self._voice_pcm_forwarding_enabled = True
        if not press_delayed:
            self._diagnostic_trace.emit(
                "hotkey_sent",
                **self._diagnostic_trace.current_context(),
                backend=_VOICE_HOTKEY_BACKEND_MARKED,
            )
        self._set_runtime_voice_result(bridge_runtime_status.VOICE_RUNTIME_ACTIVE)
        self._set_runtime_voice_active(True)
        if not self._arm_voice_hold_watchdog_locked():
            self._force_voice_hold_release_locked(
                "voice hold safety timer unavailable"
            )
            return False
        if self._uses_local_voice_microphone():
            # 模式2：声音来自插在这台电脑上的麦克风，遥控器那条流不需要
            # 开，也不该被转发（否则两路声音会叠在一起）。
            if not self._start_local_voice_capture_locked():
                self._logger.info(
                    "voice failing closed: local microphone unavailable"
                )
                self._force_voice_hold_release_locked(
                    "local voice microphone unavailable"
                )
                return False
        elif send_device_open and self._ble_session is not None:
            self._ble_session.send_mic_open_threadsafe()
        return True

    def _abort_voice_audio_start_locked(self, reason: str) -> None:
        self._voice_audio_stream_active = False
        self._voice_audio_stop_processed = True
        self._voice_audio_start_fallback_pending = False
        self._voice_raw_input_trigger_pending = False
        self._voice_pcm_forwarding_enabled = False
        self._set_runtime_voice_active(False)
        flush_result = self._voice_audio.flush(reason)
        if flush_result.error is not None:
            self._logger.error(
                "voice audio start rollback flush failed: %s",
                flush_result.error,
            )
        if self._voice_mic_gesture_active:
            self._voice_mic_gesture_audio_started = False
            self._voice_mic_gesture_audio_stopped = True
            if not self._voice_mic_gesture_sources_down:
                self._finish_voice_mic_gesture()

    def _capture_voice_focus_before(self) -> None:
        self._voice_focus_provider = "recorded_hotkey"
        self._voice_focus_submit_method = "hotkey_hold"
        snapshot = voice_interaction_diagnostics_windows.capture_focus_snapshot()
        self._voice_focus_before = snapshot
        self._diagnostic_trace.emit(
            "voice_focus_snapshot",
            **self._diagnostic_trace.current_context(),
            phase="before",
            provider=self._voice_focus_provider,
            submit_method=self._voice_focus_submit_method,
            supported=bool(snapshot.supported),
            foreground_pid=int(snapshot.foreground_pid),
            foreground_class=snapshot.foreground_class or "unknown",
            focus_handle=int(snapshot.focus_handle),
            focus_class=snapshot.focus_class or "unknown",
            text_length=(
                int(snapshot.text_length)
                if snapshot.text_length is not None
                else -1
            ),
            diagnostic=snapshot.error or "captured",
            voice_ui="unknown",
            foreground_executable=snapshot.foreground_executable,
            process_query_status=snapshot.process_query_status,
            foreground_thread_id=snapshot.foreground_thread_id,
            foreground_handle=snapshot.foreground_handle,
            foreground_stable=snapshot.foreground_stable,
            keyboard_layout=snapshot.keyboard_layout,
        )
        self._logger.info(
            "voice interaction start: provider=%s method=%s foreground_pid=%s "
            "foreground_class=%s focus_class=%s text_length=%s diagnostic=%s program=%s keyboard_layout=%s",
            self._voice_focus_provider,
            self._voice_focus_submit_method,
            snapshot.foreground_pid,
            snapshot.foreground_class or "unknown",
            snapshot.focus_class or "unknown",
            snapshot.text_length if snapshot.text_length is not None else "unavailable",
            snapshot.error or "captured",
            snapshot.foreground_executable or "unknown",
            snapshot.keyboard_layout,
        )

    def _log_voice_submission_observation(self):
        before = self._voice_focus_before
        if before is None:
            return None
        after = voice_interaction_diagnostics_windows.capture_focus_snapshot()
        observation = voice_interaction_diagnostics_windows.compare_submission(
            before,
            after,
        )
        self._voice_text_observation = str(observation.text_state)
        self._diagnostic_trace.emit(
            "voice_submission_observation",
            **self._diagnostic_trace.current_context(),
            provider=self._voice_focus_provider or "unknown",
            submit_method=self._voice_focus_submit_method or "unknown",
            supported=bool(after.supported),
            foreground_pid=int(after.foreground_pid),
            foreground_class=after.foreground_class or "unknown",
            focus_handle=int(after.focus_handle),
            focus_class=after.focus_class or "unknown",
            text_length=(
                int(after.text_length) if after.text_length is not None else -1
            ),
            focus_state=str(observation.focus_state),
            text_state=str(observation.text_state),
            text_delta=(
                int(observation.text_delta)
                if observation.text_delta is not None
                else -1
            ),
            diagnostic=after.error or "captured",
            voice_ui=getattr(self._voice_shortcut, "ui_confirmation", "") or "unknown",
            foreground_executable=after.foreground_executable,
            process_query_status=after.process_query_status,
            foreground_thread_id=after.foreground_thread_id,
            foreground_handle=after.foreground_handle,
            foreground_stable=after.foreground_stable,
            keyboard_layout=after.keyboard_layout,
            keyboard_layout_change=(
                "changed" if before.keyboard_layout != after.keyboard_layout else "same"
            ) if before.keyboard_layout and after.keyboard_layout and before.foreground_stable and after.foreground_stable else "unknown",
        )
        self._logger.info(
            "voice interaction result: provider=%s method=%s focus=%s "
            "text_length=%s delta=%s; host control completion alone does not prove "
            "text insertion",
            self._voice_focus_provider or "unknown",
            self._voice_focus_submit_method or "unknown",
            observation.focus_state,
            observation.text_state,
            (
                observation.text_delta
                if observation.text_delta is not None
                else "unavailable"
            ),
        )
        self._voice_focus_before = None
        self._voice_focus_provider = ""
        self._voice_focus_submit_method = ""
        return observation

    def _on_pcm_frame(
        self,
        samples,
        *,
        _ble_generation: Optional[int] = None,
        _arrival_sequence: Optional[int] = None,
    ) -> None:
        """Queue one immutable PCM frame without blocking the BLE worker."""

        with self._voice_shortcut.lock:
            if self._plugin_voice_audio_active:
                # 插件会话：遥控器的 PCM 直接经回环桥交给 Vokie 的插件，
                # 不碰虚拟声卡。
                if (
                    not self._accepts_ble_callback(_ble_generation)
                    or not self._voice_pcm_forwarding_enabled
                ):
                    return
                bridge = vokie_bridge.active_bridge()
                if bridge is None:
                    self._voice_pcm_forwarding_enabled = False
                    return
                if not bridge.send_audio(array.array("h", samples).tobytes()):
                    self._voice_pcm_forwarding_enabled = False
                return
            if self._plugin_voice_capture_pending:
                # 还没说话：先把 PCM 攒着，不惊动 Vokie。
                self._handle_plugin_voice_capture_frame_locked(
                    samples, _ble_generation
                )
                return
            sink = self._voice_audio.sink
            if (
                sink is None
                or not self._accepts_ble_callback(_ble_generation)
                or not self._voice_pcm_forwarding_enabled
            ):
                return
            if self._uses_local_voice_microphone() and _ble_generation is not None:
                # 模式2 的声音只来自本机麦克风。遥控器如果因为话筒键被按住
                # 也在送音频，一律丢掉，避免两路声音叠在一起。
                return
            if not self._voice_audio.ensure_writer(sink):
                self._voice_pcm_forwarding_enabled = False
                return
            writer = self._voice_audio.writer
            if writer is None or not writer.submit(samples):
                self._voice_pcm_forwarding_enabled = False


async def _run(
    *,
    app_factory=None,
    tray_factory=None,
    settings_launcher=None,
    show_notification_icon: bool = True,
    on_runtime_ready=None,
    on_reconnect_ready=None,
    on_settings_reload_ready=None,
) -> None:
    tray_factory = tray_factory or bridge_tray_windows.BridgeTray
    settings_launcher = settings_launcher or bridge_launcher.launch_settings
    app = RC003App() if app_factory is None else app_factory()
    loop = asyncio.get_running_loop()
    tray_exit_requested = threading.Event()
    run_task = asyncio.create_task(app.run_forever())

    def open_settings() -> None:
        result = settings_launcher()
        if result.started:
            app._logger.info("notification area: settings opened; pid=%s", result.pid)
        else:
            app._logger.warning(
                "notification area: settings launch failed: %s",
                result.error or "unknown_error",
            )

    def request_exit() -> None:
        try:
            app._logger.info("bridge cancellation requested: source=app_callback")
        except Exception:
            pass
        tray_exit_requested.set()
        loop.call_soon_threadsafe(run_task.cancel)

    def request_tray_exit() -> None:
        try:
            app._logger.info("bridge exit intent: reason=tray_or_legacy_control")
        except Exception:
            pass
        request_exit()

    if on_runtime_ready is not None:
        on_runtime_ready(request_exit)
    if on_reconnect_ready is not None:
        on_reconnect_ready(app.request_connection_retry_now)
    if on_settings_reload_ready is not None:
        on_settings_reload_ready(app.request_settings_reload_now)

    tray = None
    try:
        if show_notification_icon:
            try:
                tray = tray_factory(
                    on_open_settings=open_settings,
                    on_exit_requested=request_tray_exit,
                    status_handler=lambda message: app._logger.info(
                        "notification area: %s", message
                    ),
                )
                if tray.start():
                    app._logger.info("notification area: bridge control icon started")
                else:
                    app._logger.warning(
                        "notification area unavailable: %s",
                        tray.startup_error or "unknown_error",
                    )
            except Exception:
                app._logger.exception("notification area failed to initialize")

        try:
            await run_task
        except asyncio.CancelledError:
            if not tray_exit_requested.is_set():
                raise
            app._logger.info("notification area: graceful bridge exit requested")
    finally:
        try:
            if tray is not None and not tray.stop():
                app._logger.warning("notification area thread did not stop cleanly")
        except Exception as exc:
            app._logger.warning(
                "notification area stop failed: error_type=%s",
                type(exc).__name__,
            )
        finally:
            try:
                await app.stop()
            finally:
                clear_runtime_status = getattr(app, "clear_runtime_status", None)
                if callable(clear_runtime_status):
                    clear_runtime_status()


def main(
    *,
    show_notification_icon: bool = True,
    on_runtime_ready=None,
    on_reconnect_ready=None,
    on_settings_reload_ready=None,
) -> None:
    asyncio.run(
        _run(
            show_notification_icon=show_notification_icon,
            on_runtime_ready=on_runtime_ready,
            on_reconnect_ready=on_reconnect_ready,
            on_settings_reload_ready=on_settings_reload_ready,
        )
    )


if __name__ == "__main__":
    main()
