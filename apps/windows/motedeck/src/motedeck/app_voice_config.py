"""Voice hotkey/slot configuration, shortcut session and gesture/key detection methods of RC003App, mechanically split from app.py's RC003App class body (verbatim bodies, zero behavior change); RC003App inherits this mixin."""

from __future__ import annotations

from typing import List
from typing import Optional
from typing import Tuple
from .voice_shortcut_session import _VOICE_HOTKEY_BACKEND_MARKED
from . import bridge_runtime_status
from . import button_gesture
from . import config
from . import hotkey
from . import injection_backend
from . import key_detection_bridge
from . import key_mapping
from . import openmexia_client
from . import raw_input_windows
import threading
import time
from . import voice_gesture
from . import win32_input
from . import win32_keys
from typing import List, Optional, Tuple
from .app_constants import (
    _KEY_DETECTION_MIC_MAX_SECONDS,
    _KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS,
    _VOICE_HOLD_SAFETY_SECONDS,
    _VOICE_SHORTCUT_PRESS_DELAY_SECONDS,
    _VOICE_TAP_MAX_SECONDS,
)


class VoiceConfigMixin:
    """Voice hotkey/slot configuration, shortcut session and gesture/key detection methods of RC003App."""


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

    def _voice_slot_uses_winuhid(self, slot: Optional[int]) -> bool:
        """Whether ``slot`` is delivered by an available WinUHid backend."""

        return (
            self._voice_slot_inject_mode(slot)
            == injection_backend.INJECT_MODE_WINUHID
            and injection_backend.winuhid_available()
        )

    def _voice_slot_uses_interception(self, slot: Optional[int]) -> bool:
        """Whether ``slot`` is delivered by an available Interception driver."""

        return (
            self._voice_slot_inject_mode(slot)
            == injection_backend.INJECT_MODE_INTERCEPTION
            and injection_backend.interception_available()
        )

    def _voice_slot_uses_driver_backend(self, slot: Optional[int]) -> bool:
        """Whether ``slot`` is injected by a usable driver-level backend.

        两个驱动后端都绕过低层钩子的注入标记，语音快捷键的物理化检查对它们
        同样不适用。
        """

        return self._voice_slot_uses_winuhid(slot) or (
            self._voice_slot_uses_interception(slot)
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

    def _voice_cycle_slots(self) -> List[int]:
        """Every enabled voice key id, in the order the user listed them."""

        return [
            int(entry["id"])
            for entry in config.enabled_voice_key_configs(self._config)
        ]

    def _active_voice_slot(self) -> int:
        """The voice key a physical mic press currently drives.

        话筒键跟着循环游标走（短按切换，或映射成“语音模式切换”的其他
        按键都能推进游标）；游标起点是话筒键自己绑定的那条语音键。
        """

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

    def _schedule_openmexia_profile_for_slot(self, slot: Optional[int]) -> None:
        """Queue the OpenMexia mapping switch instead of switching inline.

        语音按下这一下正握着语音/按键锁，现场换整套映射会把正在注入的
        快捷键搅乱，所以只把「该看哪条语音键」投回事件循环，等这一轮空下来
        再换。
        """

        loop = getattr(self, "_event_loop", None)
        if loop is None:
            return
        loop.call_soon_threadsafe(self._sync_openmexia_profile_for_slot, slot)

    def _sync_openmexia_profile_for_slot(self, slot: Optional[int]) -> None:
        """语音键切到 OpenMexia 那条时，整套换成它的应用配置。

        OpenMexia 这条配置只跟语音键走：切到它就换上并一直保持，切回别的
        语音键才退回全局；不依赖前台进程，OpenMexia 收到托盘也照常生效。
        """

        if not self._accept_input_events or slot is None:
            return
        entry = config.voice_key_config(self._config, slot)
        uses_openmexia = (
            entry is not None
            and entry.get("plugin") == config.VOICE_KEY_PLUGIN_OPENMEXIA
        )
        try:
            app_entry = self._preset_store.find_app(openmexia_client.PROCESS_NAME)
        except Exception:  # noqa: BLE001 - 配置读不出来就当没有
            return
        if not uses_openmexia:
            # 切回普通语音键时，把上次为 OpenMexia 换上的那套退回去。
            if not self._openmexia_profile_override:
                return
            self._openmexia_profile_override = False
            self._openmexia_profile_id = None
            if app_entry is None or self._preset_override_id != app_entry["id"]:
                return
            with self._button_mapping_lock:
                self._reset_preset_override_locked()
            self._preset_manual_foreground = None
            return
        if app_entry is None:
            return
        self._openmexia_profile_id = app_entry["id"]
        self._openmexia_profile_override = True
        if self._preset_override_id != app_entry["id"]:
            # 语音会话正忙时换不上，交给下一轮轮询继续补。
            if self._apply_preset_override(app_entry["id"]):
                self._remember_applied_profile(app_entry["id"])

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
        if button_id == "mic":
            # 话筒键跟着循环游标走，而不是它自己绑的那一条。
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
        # 切到 OpenMexia 那条语音键时，整套按键临时换成米家设备那套配置。
        self._schedule_openmexia_profile_for_slot(slot)
        hotkey_text = self._voice_hotkey_text_for_slot(slot)
        if not hotkey_text and button_id == "mic":
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
            # 遥控器按键走驱动拦截时拿不到 HID 设备路径，物理键盘跟踪会一起
            # 缺失，含字母的语音组合键就会被整体拒绝。这里按需补一个只跟踪
            # 键盘的监听，再重新判断一次。
            self._ensure_physical_keyboard_tracking_listener()
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
            and not self._voice_slot_uses_driver_backend(slot)
        ):
            # The driver-level backends inject below the low-level hook, so the
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
        # 先切输入法再取可见窗口基线，避免输入法面板的翻起被算成基线变化。
        self._switch_voice_tsf_locked(voice_slot)
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
            self._switch_voice_tsf_locked(voice_slot)
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
        self._set_runtime_voice_active_slot(slot)
        self._schedule_openmexia_profile_for_slot(slot)
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

        with self._voice_shortcut.lock:
            if not self._voice_cycle_slots():
                return
            self._end_latched_voice_session_for_cycle_locked()
            slot = self._advance_voice_cycle_slot_locked()
            self._show_voice_mode_toast(slot)
            self._set_runtime_voice_active_slot(slot)
        self._schedule_openmexia_profile_for_slot(slot)
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
