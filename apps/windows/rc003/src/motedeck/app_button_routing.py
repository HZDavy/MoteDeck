"""Button edge/combo/long-press routing and input injection methods of RC003App, mechanically split from app.py's RC003App class body (verbatim bodies, zero behavior change); RC003App inherits this mixin."""

from __future__ import annotations

from typing import List
from typing import Optional
from typing import Tuple
from . import action_executor
from . import bridge_runtime_status
from . import button_combo
from . import button_gesture
from . import element_navigation_control_windows
from . import injection_backend
from . import key_detection_bridge
from . import key_mapping
import threading
import time
from . import voice_gesture
from . import win32_input
from . import win32_keys
from typing import List, Optional, Tuple
from .app_constants import (
    _BUTTON_ACTION_NAVIGATION_VKS,
    _BUTTON_INPUT_RELEASE_RETRY_INITIAL_SECONDS,
    _BUTTON_INPUT_RELEASE_RETRY_MAX_SECONDS,
    _BUTTON_NAVIGATION_SINGLE_KEYS,
    _KEY_DETECTION_SUPPRESSION_MAX_SECONDS,
    _OPENMEXIA_HOLD_REPEAT_SECONDS,
    _ORDINARY_MIC_MAX_SECONDS,
    _ORDINARY_MIC_RELEASE_GUARD_SECONDS,
    _ORDINARY_MIC_SOURCE_STALE_SECONDS,
    _RAW_FALLBACK_KEY_TOKENS,
    _SENDINPUT_CLICK_REPEAT_KINDS,
)


class ButtonRoutingMixin:
    """Button edge/combo/long-press routing and input injection methods of RC003App."""


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
                    # A validated raw event while the tap owns mapping proves
                    # the Gadget did not clear that report before Windows
                    # translated it (a working tap removes keyboard usages at
                    # the kernel copy). After a short grace window, reopen the
                    # Raw Input fallback path instead of dropping silently.
                    if not self._raw_event_may_revert_direct_hid_locked():
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
            # 话筒键已绑成普通按键：清掉上一段语音会话在物理松手时留下的
            # "忽略迟到音频开始"标记。这个标记只在语音分支的按下沿清除，
            # 绑成普通按键后没人清它，设备重试的 AudioStarted 会被一直
            # 吞掉，普通映射的仲裁点（置警告、发闭麦）永远走不到。
            with self._voice_shortcut.lock:
                self._voice_ignore_audio_start_until_physical_down = False
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

        An OpenMexia control has no key to hold, so it joins the same hold
        path by repeating the whole action instead (see
        ``_OPENMEXIA_HOLD_REPEAT_SECONDS``).
        """

        if self._is_mouse_hold_button(button_id):
            return False
        action = key_mapping.button_action_for(
            self._bindings, button_id, key_mapping.ButtonTrigger.SINGLE_CLICK
        )
        if action.kind == key_mapping.ActionKind.OPENMEXIA_DEVICE:
            return True
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

        Under a driver-level backend (WinUHid or Interception) the single-click
        key is held down for the whole physical hold and the matching key-up is
        delivered by ``_end_button_key_hold``, so the target sees one sustained
        key press.  Under SendInput the driver cannot emulate that hold, so
        direction, backspace and volume buttons keep the original click-repeat
        behaviour: the first press and every repeat deliver a full tap
        (down+up).  Element navigation keeps its tap behaviour because it
        consumes mapped navigation keys itself.
        """

        action = key_mapping.button_action_for(
            self._bindings, button_id, key_mapping.ButtonTrigger.SINGLE_CLICK
        )
        tokens = self._button_action_key_tokens(action)
        if tokens is None:
            if action.kind != key_mapping.ActionKind.OPENMEXIA_DEVICE:
                return
            with self._button_action_lock:
                if not self._accept_input_events:
                    return
                # No key can be held for an OpenMexia control, so the action
                # itself is repeated: this press delivers the first one and
                # every repeat re-runs it until the button is released.
                self._on_button_trigger(
                    button_id, button_gesture.ButtonTrigger.SINGLE_CLICK
                )
                self._button_tap_hold_actions[owner] = action
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
                action.inject_mode
                not in (
                    key_mapping.INJECT_MODE_WINUHID,
                    key_mapping.INJECT_MODE_INTERCEPTION,
                )
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
            except injection_backend.DriverUnavailableError as exc:
                self._logger.warning(
                    "button hold skipped: driver backend unavailable: %s", exc
                )
                return
            except win32_input.Win32InputUnavailableError as exc:
                self._logger.info("button hold unavailable: %s", exc)
                return
            except win32_input.PhysicalKeyInUseError as exc:
                if not self._recover_app_switcher_input(action):
                    self._logger.exception(
                        "button hold failed to deliver: error_type=%s detail=%s",
                        type(exc).__name__,
                        exc,
                    )
                    return
                try:
                    backend.key_down(tokens)
                except win32_input.InputCleanupIncompleteError:
                    self._logger.exception(
                        "button hold key-down remains incomplete"
                    )
                    self._button_key_release_pending = tokens
                    self._schedule_button_input_release_retry_locked()
                    return
                except OSError as retry_exc:
                    self._logger.exception(
                        "button hold failed to deliver: error_type=%s detail=%s",
                        type(retry_exc).__name__,
                        retry_exc,
                    )
                    return
            except win32_input.InputCleanupIncompleteError:
                self._logger.exception("button hold key-down remains incomplete")
                self._button_key_release_pending = tokens
                self._schedule_button_input_release_retry_locked()
                return
            except OSError as exc:
                self._logger.exception(
                    "button hold failed to deliver: error_type=%s detail=%s",
                    type(exc).__name__,
                    exc,
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

    def _recover_app_switcher_input(
        self, action: key_mapping.ButtonAction
    ) -> bool:
        """Clear a stale Ctrl/Alt/Tab state that blocks the app switcher.

        Only stale physical tracker records are reconciled here; synthetic
        releases remain owned by the existing cleanup paths. No voice lock is
        needed while the button mapping/action locks may be held.
        """

        if action.kind != key_mapping.ActionKind.APP_SWITCHER:
            return False
        try:
            recovered = win32_input.recover_app_switcher_input()
        except Exception:
            self._logger.exception(
                "app switcher input recovery failed unexpectedly"
            )
            return False
        if recovered:
            self._logger.info("app switcher stale physical state recovered")
        return recovered

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
        app.  Held buttons re-send only the key-down through
        ``backend.key_repeat`` until release (the matching key-up is delivered
        once by ``_end_button_key_hold``), which makes them feel like a
        physical keyboard hold.  The repeat never runs the physical-key
        preflight: the held key-down is this process's own injection and a
        preflight would mistake it for a physically held key.  SendInput-held
        direction, backspace and volume buttons re-run the whole click instead,
        as does element navigation, which consumes mapped navigation keys
        itself.
        """

        with self._button_action_lock:
            entry = self._button_key_holds.get(token)
            if entry is not None:
                tokens, backend = entry
                try:
                    backend.key_repeat(tokens)
                except injection_backend.DriverUnavailableError as exc:
                    self._logger.warning(
                        "button hold repeat skipped: driver backend "
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

        OpenMexia controls are bridge requests rather than key taps, so they
        repeat at a fixed, human-paced rate instead of the keyboard's.
        """

        action = key_mapping.button_action_for(
            self._bindings, button_id, key_mapping.ButtonTrigger.SINGLE_CLICK
        )
        if action.kind == key_mapping.ActionKind.OPENMEXIA_DEVICE:
            return _OPENMEXIA_HOLD_REPEAT_SECONDS
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

    def _apply_button_action_winuhid(
        self, action: key_mapping.ButtonAction
    ) -> Tuple[str, str]:
        """Deliver one action through the WinUHid virtual keyboard/mouse.

        Returns ``(error, error_type)``; an empty ``error`` means success.

        There is deliberately no fallback to SendInput: the user picked this
        backend for a target window that ignores synthetic input, so silently
        downgrading would look like "the button does nothing" all over again.
        """

        backend = injection_backend.backend_for(
            key_mapping.INJECT_MODE_WINUHID
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
        except injection_backend.WinUHidUnavailableError as exc:
            self._logger.warning(
                "winuhid action skipped: backend unavailable: %s", exc
            )
            return "winuhid_unavailable", type(exc).__name__
        except OSError as exc:
            self._logger.exception(
                "winuhid action failed to deliver: error_type=%s",
                type(exc).__name__,
            )
            return "delivery_failed", type(exc).__name__
        self._logger.warning(
            "winuhid action unsupported: action=%s", action.kind.value
        )
        return "winuhid_unsupported_action", ""

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
