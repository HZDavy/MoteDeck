"""Runtime status publishing and bridge status diagnostics methods of RC003App, mechanically split from app.py's RC003App class body (verbatim bodies, zero behavior change); RC003App inherits this mixin."""

from __future__ import annotations

from typing import Optional
from . import __version__
import asyncio
from . import bridge_runtime_status
from . import element_navigation_runtime
import os
from . import remote_selection
import time
from . import voice_playback_session_windows
from typing import Optional
from .app_constants import (
    _RUNTIME_STATUS_HEARTBEAT_SECONDS,
)


class RuntimeStatusMixin:
    """Runtime status publishing and bridge status diagnostics methods of RC003App."""


    def _current_voice_runtime_state(self):
        with self._runtime_status_lock:
            return self._runtime_voice_state

    def _on_voice_audio_first_frame(self):
        with self._runtime_status_lock:
            voice_active = self._runtime_voice_active
        if voice_active:
            self._set_runtime_voice_result(bridge_runtime_status.VOICE_RUNTIME_RECEIVING_AUDIO)

    async def run_forever(self) -> None:
        # 启动时让话筒键警告对齐当前生效的映射：绑着普通动作就直接置上，
        # 不必等第一次按键事件；绑着语音键则保持干净。
        self._publish_voice_mic_blocked_from_mapping()
        heartbeat = self._event_loop.create_task(
            self._runtime_status_heartbeat()
        )
        try:
            if self._chromecast_real:
                self._chromecast_runtime = self._create_chromecast_runtime()
                # 软件方案（按前台应用/切换快捷键覆盖映射）在谷歌遥控器链路
                # 同样要生效；chromecast_runtime 自己不管 preset，循环必须在
                # 这里启动，否则切到该机型后永远只用全局映射。
                self._start_preset_switching()
                await self._chromecast_runtime.run()
                return
            self._start_input_channels()
            self._start_preset_switching()
            await self._supervisor.run_forever()
        finally:
            try:
                await self._stop_preset_switching()
                if not self._chromecast_real:
                    self._stop_input_channels()
                self._voice_mode_toast_watch.stop()
                self._voice_mode_toast.stop()
            finally:
                element_navigation_runtime.clear_diagnostic_trace(self._diagnostic_trace)
                self._diagnostic_trace.close()
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def stop(self) -> None:
        if self._chromecast_real:
            if self._chromecast_runtime is not None:
                await self._chromecast_runtime.stop()
            return
        await self._supervisor.stop()

    def request_connection_retry_now(self) -> None:
        if self._chromecast_real:
            # The Chromecast receiver owns its own retry/stop cycle.
            return
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
                self._runtime_remote_model = ""
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
                    voice_mic_blocked_reason=self._runtime_voice_mic_blocked,
                    battery_level=self._runtime_battery_level,
                    remote_model=self._runtime_remote_model,
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

    def _set_runtime_voice_mic_blocked(self, reason: str) -> None:
        """Publish why unsolicited physical-mic audio is being dropped.

        An empty reason means the physical mic button is (or resolves to) a
        voice action, so audio flows normally. This mirrors the runtime
        status plumbing so the settings UI can surface the block instead of
        leaving the drop visible only in the log file.
        """
        reason = str(reason)
        with self._runtime_status_lock:
            if reason == self._runtime_voice_mic_blocked:
                return
            self._runtime_voice_mic_blocked = reason
        self._publish_runtime_status()

    def _publish_voice_mic_blocked_from_mapping(self) -> None:
        """让话筒键警告跟随当前生效的映射，而不是只等音频事件置位。

        映射每次变化（保存重载、应用条覆盖/恢复、启动）都会调用这里：
        话筒键此刻解析成语音动作就清掉警告，绑成普通动作就置上——判定
        与 AudioStarted 仲裁点完全一致（_is_voice_primary_button）。
        """

        self._set_runtime_voice_mic_blocked(
            "" if self._is_voice_primary_button("mic") else "mic_ordinary_mapping"
        )

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

    def _record_device_diagnostic_context(self) -> None:
        selected = remote_selection.normalize(self._config.get(remote_selection.KEY))
        self._diagnostic_trace.emit(
            "device_context", app_version=__version__,
            selected_ref=selected["active"][:12],
            registered=[{"device_ref": row["key"][:12], "profile": row["profile"]}
                        for row in selected["devices"]],
        )
