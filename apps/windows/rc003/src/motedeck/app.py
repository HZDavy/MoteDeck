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

Mechanical split note: the RC003App method groups now live in the sibling
app_runtime_status / app_chromecast_voice / app_input_channels /
app_voice_config / app_voice_capture / app_button_routing /
app_settings_presets mixin modules (RC003App inherits them), and the
module-level constants plus CleanupIncompleteError /
_runtime_voice_hotkey_text live in app_constants.py and are re-exported
below. Every method body was moved verbatim; behavior is unchanged.
"""

from __future__ import annotations
import array
import asyncio
import json
import logging
import math
import os
from pathlib import Path
import sys
import threading
import time
import uuid
from typing import List, Optional, Tuple
from . import __version__
from . import (
    atomic_files,
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
    local_asr,
    local_microphone_windows,
    logging_setup,
    openmexia_client,
    preset_switch_windows,
    raw_input_windows,
    remote_selection,
    tsf_input_windows,
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
from .chromecast_voice import TAP_MAX_SECONDS
from .voice_shortcut_session import _VOICE_HOTKEY_BACKEND_MARKED


from .app_constants import (
    CleanupIncompleteError,
    _BUTTON_ACTION_MOUSE_BUTTONS,
    _BUTTON_ACTION_NAVIGATION_VKS,
    _BUTTON_INPUT_RELEASE_RETRY_INITIAL_SECONDS,
    _BUTTON_INPUT_RELEASE_RETRY_MAX_SECONDS,
    _BUTTON_NAVIGATION_SINGLE_KEYS,
    _CHROMECAST_VOICE_TAP_MAX_SECONDS,
    _DIRECTION_BUTTON_IDS,
    _KEY_DETECTION_MIC_MAX_SECONDS,
    _KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS,
    _KEY_DETECTION_SUPPRESSION_MAX_SECONDS,
    _OPENMEXIA_ASR_HOLD_SECONDS,
    _OPENMEXIA_ASR_LEVEL_BARS,
    _OPENMEXIA_ASR_MAX_SECONDS,
    _OPENMEXIA_ASR_TAIL_SECONDS,
    _OPENMEXIA_HOLD_REPEAT_SECONDS,
    _ORDINARY_MIC_MAX_SECONDS,
    _ORDINARY_MIC_RELEASE_GUARD_SECONDS,
    _ORDINARY_MIC_SOURCE_STALE_SECONDS,
    _PLUGIN_VOICE_HOLD_SECONDS,
    _PLUGIN_VOICE_PREROLL_SECONDS,
    _PRESET_SWITCH_POLL_SECONDS,
    _RAW_FALLBACK_KEY_TOKENS,
    _RAW_INPUT_RETRY_INITIAL_SECONDS,
    _RAW_INPUT_RETRY_MAX_SECONDS,
    _RUNTIME_STATUS_HEARTBEAT_SECONDS,
    _SENDINPUT_CLICK_REPEAT_KINDS,
    _VOICE_HOLD_SAFETY_SECONDS,
    _VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS,
    _VOICE_KEY_PHYSICALIZER_FAILED_RETRY_SECONDS,
    _VOICE_KEY_PHYSICALIZER_RETRY_SECONDS,
    _VOICE_SHORTCUT_PRESS_DELAY_SECONDS,
    _VOICE_TAP_MAX_SECONDS,
    _runtime_voice_hotkey_text,
)


from .app_runtime_status import RuntimeStatusMixin
from .app_chromecast_voice import ChromecastVoiceMixin
from .app_input_channels import InputChannelsMixin
from .app_voice_config import VoiceConfigMixin
from .app_voice_capture import VoiceCaptureMixin
from .app_button_routing import ButtonRoutingMixin
from .app_settings_presets import SettingsPresetsMixin


def open_configured_application(action: key_mapping.ButtonAction) -> bool:
    """Application-action seam kept at the app boundary for testability."""

    return action_executor.open_configured_application(action)


class RC003App(
    RuntimeStatusMixin,
    ChromecastVoiceMixin,
    InputChannelsMixin,
    VoiceConfigMixin,
    VoiceCaptureMixin,
    ButtonRoutingMixin,
    SettingsPresetsMixin,
):
    # 切完输入法后留一点时间让目标窗口真正拿到新的 TSF，再注入快捷键。
    _VOICE_TSF_SWITCH_SETTLE_SECONDS = 0.06

    def __init__(self) -> None:
        self._config_root = config.config_root()
        self._config_path = config.config_path(self._config_root)
        self._config = config.load_config(self._config_path)
        from . import remote_selection
        self._selected_remote_key = remote_selection.active_key(self._config)
        selected_profile = remote_selection.active_profile(self._config)
        self._remote_profile = selected_profile
        # 谷歌遥控器只走真实接收链路（chromecast_runtime）。
        self._chromecast_real = bool(
            selected_profile == remote_selection.CHROMECAST_PROFILE
            and self._selected_remote_key
        )
        self._chromecast_runtime = None
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
        self._preset_store = app_profiles.AppProfileStore(
            self._config_root, self._remote_profile)
        # 软件方案按型号分库，但列表共用：启动时先把另一台遥控器上的方案
        # 补过来，避免服务先跑起来时这台看不到那些应用。
        try:
            self._preset_store.adopt_apps_from_siblings()
        except Exception:  # noqa: BLE001 - 同步失败不影响启动
            pass
        self._presets_mtime_ns = self._settings_file_mtime_ns(self._preset_store.path)
        # Tracked separately from the hotkey table: editing one app's mapping
        # must re-resolve the overlay that is live right now.
        self._preset_content_mtime_ns = self._presets_mtime_ns
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
        # OpenMexia 设备动作的网络请求都在这个后台线程里排队执行。
        self._openmexia_runner = openmexia_client.OpenMexiaRunner()
        # 当前这套映射是不是被 OpenMexia 语音键临时换上的（用来在切走时退回）。
        self._openmexia_profile_override = False
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
        self._logger.info(
            "settings root: %s (resolved: %s)",
            self._config_root,
            atomic_files.resolve_real_path(self._config_root),
        )
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
        self._runtime_voice_mic_blocked = ""
        self._runtime_voice_updated_at: Optional[float] = None
        self._runtime_battery_level: Optional[int] = None
        # 连上后读到的 GATT 型号串（0x2A24），只用于界面区分 2 / 2 Pro。
        self._runtime_remote_model = ""
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
        # 本次插件会话是不是闪记：Vokie 对外部设备的闪记会话不出任何屏幕
        # 提示（速记窗口不弹、胶囊也被它自己的规则禁止），所以复用 OpenMexia
        # 长按的电平胶囊由这边补上。
        self._plugin_voice_mode_is_quick_note = False
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
        # OpenMexia 语音键：录音走本地 SenseVoice 离线识别，识别文字再交给
        # OpenMexia 执行设备指令。识别在后台线程里做，这里只攒 PCM。
        self._asr_runner = local_asr.LocalAsrRunner(self._config_root)
        self._asr_active = False
        self._asr_slot: Optional[int] = None
        self._asr_frames: List[array.array] = []
        # 按下后先攒着等长按阈值：短按只切模式，按满才开始识别。
        self._asr_capture_armed = False
        self._asr_hold_timer: Optional[threading.Timer] = None
        self._asr_hold_token: Optional[object] = None
        # 松手后的尾音收集：再等一小段才真正收尾送识别。
        self._asr_finalize_pending = False
        self._asr_tail_timer: Optional[threading.Timer] = None
        # 电平胶囊：最近若干帧的平均峰值（滚动波形）和上一次推送的时间。
        self._asr_recent_peaks: List[float] = []
        # 自动增益的平滑值，按语音键分别保存。
        self._voice_auto_gain: dict = {}
        self._asr_pending_sum = 0.0
        self._asr_pending_count = 0
        self._asr_level_sent_at = 0.0
        self._asr_samples = 0
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
        # Monotonic time of the latest arming, so Raw Input can tell a fresh
        # handover from a tap that quietly stopped intercepting reports.
        self._direct_hid_interception_armed_monotonic: Optional[float] = None
        # Set when a Raw Input revert proved the tap stopped clearing reports.
        # While set, tap reports and late tap status events must not reclaim
        # ownership; only a fresh tap started by the revert restart may arm.
        self._direct_hid_interception_reverted_pending_restart = False
        # One pending revert-triggered tap restart. Debounced, cooled down,
        # and capped so a persistently failing Gadget cannot cycle attaches.
        self._hid_tap_restart_timer: Optional[threading.Timer] = None
        self._hid_tap_restart_timer_factory = threading.Timer
        self._hid_tap_restart_lock = threading.Lock()
        self._hid_tap_auto_restart_last_monotonic: Optional[float] = None
        self._hid_tap_auto_restart_attempts = 0
        self._hid_tap_auto_restart_window_start = 0.0
        self._hid_tap_auto_restart_exhausted_logged = False
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
            elif action.inject_mode == key_mapping.INJECT_MODE_WINUHID and (
                injection_backend.key_tokens_for_action(action) is not None
                or injection_backend.mouse_button_for_action(action) is not None
            ):
                # Only key/mouse actions are WinUHid-deliverable. Anything
                # else (element-navigation toggle, Quicker URI, open-app, voice)
                # must fall through to its own handler instead of being silently
                # swallowed as "winuhid_unsupported_action".
                action_error, action_error_type = self._apply_button_action_winuhid(
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
                try:
                    win32_input.send_app_switcher()
                except win32_input.PhysicalKeyInUseError:
                    if not self._recover_app_switcher_input(action):
                        raise
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
            elif action.kind == key_mapping.ActionKind.OPENMEXIA_DEVICE:
                # 网络请求交给后台线程，输入回调里只负责排队。
                self._openmexia_runner.submit(action.device, action.op)
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
                if self._asr_active:
                    # OpenMexia 语音识别：收下这条流，PCM 由 _on_pcm_frame
                    # 缓存，松手后整段送百度识别。
                    self._voice_audio_stream_active = True
                    self._voice_audio_stop_processed = False
                    self._voice_audio_start_fallback_pending = False
                    self._voice_pcm_forwarding_enabled = True
                    self._voice_audio.stats.reset()
                    self._logger.info("openmexia asr audio stream accepted")
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
                if is_mic_voice_button:
                    # The physical mic resolves to a voice action again; any
                    # previously published block reason is stale.
                    self._set_runtime_voice_mic_blocked("")
                if not self._voice_shortcut.controller.active and not is_mic_voice_button:
                    self._abort_voice_audio_start_locked(
                        "physical mic has an ordinary mapping"
                    )
                    self._logger.info(
                        "unsolicited mic audio ignored: physical mic has an ordinary mapping"
                    )
                    self._set_runtime_voice_mic_blocked("mic_ordinary_mapping")
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
                if self._asr_active or self._asr_capture_armed:
                    # 设备自己停流了：拿到的这段录音照常送识别。
                    self._end_openmexia_asr_locked(
                        "device audio stop", close_mic=False
                    )
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

