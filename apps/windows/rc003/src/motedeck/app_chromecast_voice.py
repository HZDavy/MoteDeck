"""Chromecast device discovery and connection lifecycle methods of RC003App, mechanically split from app.py's RC003App class body (verbatim bodies, zero behavior change); RC003App inherits this mixin."""

from __future__ import annotations

from typing import Optional
import asyncio
from . import ble_transport_winrt
from . import bridge_runtime_status
from . import hotkey
from . import key_detection_bridge
from . import key_mapping
from . import win32_keys
from typing import Optional
from .app_constants import (
    _CHROMECAST_VOICE_TAP_MAX_SECONDS,
)


class ChromecastVoiceMixin:
    """Chromecast device discovery and connection lifecycle methods of RC003App."""


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

        # 蓝牙名可能只是通用名（如“小米蓝牙语音遥控器”），连上后读一次
        # GATT 型号串（0x180A/0x2A24）来区分小米遥控器2 与 2 Pro。这是
        # 可选探测：失败只留空，不影响已建立的语音链路。
        read_model_number = getattr(self._ble_session, "read_model_number", None)
        if callable(read_model_number):
            try:
                model_number = await read_model_number()
            except Exception as exc:  # noqa: BLE001 - optional probe
                model_number = None
                self._logger.info(
                    "optional RC003 model probe failed: error_type=%s",
                    type(exc).__name__,
                )
            if model_number:
                with self._runtime_status_lock:
                    self._runtime_remote_model = str(model_number)[:64]
                self._publish_runtime_status()
                self._logger.info("RC003 model number detected: %s", model_number)

    def _create_chromecast_runtime(self):
        from . import chromecast_runtime
        return chromecast_runtime.ChromecastRuntime(
            chromecast_runtime.RuntimeServices(
                selected_key=lambda: self._selected_remote_key,
                logger=self._logger,
                create_voice_host=self._create_chromecast_voice_host,
                set_input_state=lambda **kwargs: self._set_runtime_input_state(
                    **kwargs
                ),
                publish_status=lambda state: self._publish_runtime_status(state),
                start_input=self._start_input_channels,
                stop_input=self._stop_input_channels,
                disable_input=lambda: setattr(self, "_accept_input_events", False),
                cancel_mappings=self._cancel_chromecast_mappings,
                release_inputs=self._release_chromecast_inputs,
                route_edge=self._route_chromecast_edge,
                start_battery_monitor=self._start_chromecast_battery_monitor,
            )
        )

    async def _start_chromecast_battery_monitor(self):
        """Chromecast 遥控器同样带电池服务，用独立 worker 读，避免拖住主循环。"""
        from . import ble_transport_winrt, remote_selection
        from .battery_monitor_winrt import BatteryMonitor

        winrt = ble_transport_winrt._import_winrt()
        selector = winrt.bluetooth_le_device.get_device_selector_from_pairing_state(True)
        devices = await winrt.device_information.find_all_async_aqs_filter_and_additional_properties(
            selector, ["System.Devices.Aep.ContainerId"]
        )
        device_id = ""
        for info in devices:
            try:
                if remote_selection.candidate_key(info) == self._selected_remote_key:
                    device_id = str(getattr(info, "id", "") or "")
                    break
            except (remote_selection.SelectionError, OSError, TypeError, ValueError):
                continue
        if not device_id:
            return None
        monitor = BatteryMonitor(
            device_id,
            asyncio.get_running_loop(),
            self._set_runtime_battery_level,
            ble_transport_winrt._import_winrt,
            timeout=8.0,
        )
        return monitor if monitor.start() else None

    def _create_chromecast_voice_host(self):
        from .chromecast_voice_host import VoiceHost, VoiceHostServices
        return VoiceHost(
            VoiceHostServices(
                audio=self._voice_audio,
                shortcut=self._voice_shortcut,
                settings=lambda: self._config,
                logger=self._logger,
                reload_settings=self._reload_settings_if_changed,
                apply_pending_settings=(
                    self._apply_pending_voice_settings_if_idle_locked
                ),
                begin_diagnostic=lambda **kwargs: (
                    self._ensure_voice_diagnostic_attempt(**kwargs)
                ),
                finish_diagnostic=lambda result, **kwargs: (
                    self._finish_voice_diagnostic_attempt(result, **kwargs)
                ),
                set_active=self._set_runtime_voice_active,
                set_result=self._set_runtime_voice_result,
                disable_forwarding=lambda: setattr(
                    self, "_voice_pcm_forwarding_enabled", False
                ),
                prepare_shortcut=self._prepare_chromecast_voice_shortcut,
                claim_detection=self._claim_chromecast_mic_detection,
                press_delay_seconds=self._chromecast_voice_press_delay,
                on_short_tap=self._chromecast_voice_short_tap,
                inject_mode=self._chromecast_voice_inject_mode,
                needs_shortcut=self._chromecast_voice_needs_shortcut,
                begin_asr_capture=self._begin_chromecast_openmexia_voice,
                forward_asr_pcm=self._feed_chromecast_openmexia_pcm,
                end_asr_capture=self._end_chromecast_openmexia_voice,
                mic_passthrough=self._chromecast_mic_passthrough,
                mic_button_event=self._chromecast_mic_button_event,
                set_mic_blocked=self._set_runtime_voice_mic_blocked,
            )
        )

    def _prepare_chromecast_voice_shortcut(self) -> None:
        """Apply the active voice key's recorded shortcut before the mic opens.

        The Chromecast remote carries no voice button in the button map, so it
        resolves its shortcut the same way the Xiaomi mic button does.
        """

        slot = self._active_voice_slot()
        hotkey_text = self._voice_hotkey_text_for_slot(slot)
        if not hotkey_text:
            self._clear_voice_hotkey_locked()
            return
        try:
            voice_hotkey = hotkey.HotkeySpec.parse(hotkey_text)
        except (hotkey.HotkeyParseError, win32_keys.UnknownKeyTokenError) as exc:
            self._logger.warning(
                "chromecast voice shortcut ignored: invalid shortcut: %s", exc
            )
            self._clear_voice_hotkey_locked()
            return
        self._apply_voice_settings_locked(voice_hotkey, update_config=slot == 1)

    def _claim_chromecast_mic_detection(self) -> bool:
        """Claim one pending 按键检测 request for the Chromecast voice key.

        The Chromecast remote has no HID mic button, so its press only shows up
        as the voice stream opening. Claiming the request here keeps the
        shortcut, the recording and the voice-mode switch from firing while the
        user is only detecting buttons.
        """

        try:
            captured = key_detection_bridge.publish_next_button(
                self._config_root,
                "mic",
            )
        except OSError as exc:
            self._logger.warning("key detection IPC unavailable: %s", exc)
            return False
        if captured:
            self._logger.info(
                "key detection captured button=mic source=chromecast_voice; "
                "voice action suppressed",
            )
        return captured

    def _chromecast_voice_press_delay(self) -> float:
        """Tap window for the Chromecast voice key, or 0 when taps resend.

        Reads the same "延迟发送" switch the Xiaomi mic button uses: when it is
        on, the shortcut waits out the tap window so a short press only cycles
        the voice mode and never leaks a key press.
        """

        if not self._voice_shortcut_press_delay_enabled(self._active_voice_slot()):
            return 0.0
        return _CHROMECAST_VOICE_TAP_MAX_SECONDS

    def _chromecast_voice_inject_mode(self) -> Optional[str]:
        """Inject the remote shortcut the way the mic button's slot asks for.

        The Chromecast remote has no button-map entry of its own, so its voice
        key must inherit the injection backend of the slot it drives. Falling
        back to SendInput leaves LLKHF_INJECTED on the stroke, and voice-input
        hosts ignore an injected 语音键.
        """

        return self._voice_slot_inject_mode(self._active_voice_slot())

    def _chromecast_voice_needs_shortcut(self) -> bool:
        """Whether the active voice slot must replay a recorded shortcut.

        插件槽和本机麦克风槽不靠重放快捷键工作，没有快捷键是正常的，不能
        因此让谷歌语音键每次按下都报错。
        """

        slot = self._active_voice_slot()
        return not (
            self._voice_slot_is_plugin(slot)
            or self._voice_slot_uses_local_microphone(slot)
        )

    def _chromecast_mic_passthrough(self) -> bool:
        """话筒键是否绑成了普通动作（非语音）。

        谷歌遥控器的话筒键没有按键边沿，只有语音流事件；绑成普通动作时，
        这次按下由按键映射执行，语音流被忽略。
        """

        action = self._primary_button_action("mic")
        return action is not None and not key_mapping.is_supported_voice_action(action)

    def _chromecast_mic_button_event(self, button_id: str, pressed: bool) -> None:
        """把话筒键的按下/松开交给普通按键映射路径（与实体按键同一套）。"""

        self._on_button_event(button_id, pressed, event_source="chromecast_voice")

    def _chromecast_voice_short_tap(self) -> None:
        """Switch the voice mode on a Chromecast voice-key short press.

        谷歌遥控器同样上报完整的按下/松开事件，松手时直接按小米实体键的规则
        切模式，不看"延迟发送"开关。
        """

        with self._voice_shortcut.lock:
            self._handle_voice_short_tap_locked("chromecast short press")

    def _cancel_chromecast_mappings(self) -> None:
        with self._input_arbitration_lock:
            self._accept_input_events = False
            self._button_combos.reset()
            self._button_gestures.reset()

    def _release_chromecast_inputs(self) -> bool:
        with self._button_action_lock:
            self._button_input_release_retry_stopping = True
            self._cancel_button_input_release_retry_locked(reset_delay=False)
            return self._release_pending_button_inputs()

    def _route_chromecast_edge(self, edge) -> None:
        if edge.action == "cancel":
            self._cancel_input_gestures_for_buttons(
                {edge.button},
                reason="chromecast_cancel",
                block_until_release=False,
            )
            return
        self._on_button_event(
            edge.button, edge.action == "down", event_source="chromecast"
        )
