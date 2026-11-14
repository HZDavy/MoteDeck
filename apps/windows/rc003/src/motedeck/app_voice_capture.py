"""Voice capture, ASR/plugin voice pipelines, TSF switching and toast methods of RC003App, mechanically split from app.py's RC003App class body (verbatim bodies, zero behavior change); RC003App inherits this mixin."""

from __future__ import annotations

from typing import List
from typing import Optional
from pathlib import Path
from typing import Tuple
from .voice_shortcut_session import _VOICE_HOTKEY_BACKEND_MARKED
import array
from . import bridge_runtime_status
from . import config
from . import diagnostic_trace
from . import key_mapping
from . import local_asr
from . import local_microphone_windows
import math
import os
import threading
import time
from . import tsf_input_windows
from . import voice_interaction_diagnostics_windows
from . import vokie_bridge
from . import win32_input
from . import win32_keys
from typing import List, Optional, Tuple
from .app_constants import (
    _OPENMEXIA_ASR_HOLD_SECONDS,
    _OPENMEXIA_ASR_LEVEL_BARS,
    _OPENMEXIA_ASR_MAX_SECONDS,
    _OPENMEXIA_ASR_TAIL_SECONDS,
    _PLUGIN_VOICE_HOLD_SECONDS,
    _PLUGIN_VOICE_PREROLL_SECONDS,
)


class VoiceCaptureMixin:
    """Voice capture, ASR/plugin voice pipelines, TSF switching and toast methods of RC003App."""


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
        self._end_openmexia_asr_locked(reason)
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

    def _openmexia_asr_ready(self, slot: Optional[int]) -> bool:
        """这条语音键是不是 OpenMexia 的，并且本地识别模型已经装好。"""

        if slot is None:
            return False
        entry = config.voice_key_config(self._config, slot)
        if entry is None:
            return False
        if not config.is_openmexia_plugin_id(entry.get("plugin")):
            return False
        return local_asr.model_available(self._config_root)

    def _voice_key_gain_locked(self, slot: Optional[int]) -> float:
        """这条语音键的录音增益倍数（1.0 表示不调整）。"""

        if slot is None:
            return 1.0
        try:
            return config.voice_key_gain_scale(self._config, int(slot))
        except BaseException:  # noqa: BLE001 - 增益只是可选调整
            return 1.0

    def _voice_key_gain_auto_locked(self, slot: Optional[int]) -> bool:
        """这条语音键是不是选了「自动」增益。"""

        if slot is None:
            return False
        try:
            return config.voice_key_gain_auto(self._config, int(slot))
        except BaseException:  # noqa: BLE001 - 增益只是可选调整
            return False

    def _auto_gain_scale_locked(self, slot: Optional[int], frame) -> float:
        """自动增益：按这一帧的峰值把音量往 70% 拉，平滑避免忽大忽小。"""

        source = array.array("h", frame)
        peak = 0
        for value in source:
            magnitude = value if value >= 0 else -value
            if magnitude > peak:
                peak = magnitude
        if peak <= 0:
            return 1.0
        target = min(6.0, max(0.5, 0.7 * 32768.0 / peak))
        key = int(slot) if slot is not None else 0
        current = self._voice_auto_gain.get(key, target)
        value = current * 0.7 + target * 0.3
        self._voice_auto_gain[key] = value
        return value

    def _frame_gain_locked(self, slot: Optional[int], frame) -> float:
        """这一帧该乘的增益：自动档按峰值算，否则用手动档位。"""

        if self._voice_key_gain_auto_locked(slot):
            return self._auto_gain_scale_locked(slot, frame)
        return self._voice_key_gain_locked(slot)

    @staticmethod
    def _apply_voice_gain(samples, scale: float):
        """把一帧 PCM 整体放大/缩小，结果夹在 16bit 范围内。"""

        if scale == 1.0:
            return samples
        source = array.array("h", samples)
        if not source:
            return source
        return array.array(
            "h",
            [max(-32768, min(32767, int(value * scale))) for value in source],
        )

    def _reset_asr_capture_locked(self) -> None:
        self._asr_frames = []
        self._asr_samples = 0
        self._asr_recent_peaks = []
        self._asr_pending_sum = 0.0
        self._asr_pending_count = 0

    def _handle_asr_frame_locked(self, samples, generation: Optional[int]) -> None:
        """缓存一帧 PCM，留着松手后整段送去识别。"""

        if not self._accepts_ble_callback(generation):
            return
        self._store_asr_frame_locked(array.array("h", samples))

    def _store_asr_frame_locked(self, frame: "array.array") -> None:
        """缓存一帧已经确认来源的 PCM。"""

        if not frame:
            return
        self._asr_frames.append(frame)
        self._asr_samples += len(frame)
        self._update_asr_level_locked(frame)
        limit = int(16000 * _OPENMEXIA_ASR_MAX_SECONDS)
        while self._asr_samples > limit and self._asr_frames:
            dropped = self._asr_frames.pop(0)
            self._asr_samples -= len(dropped)

    def _update_asr_level_locked(self, frame: "array.array") -> None:
        """按时间顺序滚动播报波形：每段取一个峰值，跟着录音往左走。"""

        if not frame:
            return
        peak = 0
        for value in frame:
            magnitude = value if value >= 0 else -value
            if magnitude > peak:
                peak = magnitude
        # 这一小段里取平均峰值（而不是最大值），个别爆音不会直接顶满。
        self._asr_pending_sum += peak
        self._asr_pending_count += 1
        now = time.monotonic()
        if now - self._asr_level_sent_at < 0.05:
            return
        self._asr_level_sent_at = now
        average = self._asr_pending_sum / max(1, self._asr_pending_count)
        self._asr_pending_sum = 0.0
        self._asr_pending_count = 0
        count = _OPENMEXIA_ASR_LEVEL_BARS
        self._asr_recent_peaks.append(average / 32768.0)
        if len(self._asr_recent_peaks) > count:
            del self._asr_recent_peaks[: len(self._asr_recent_peaks) - count]
        levels = [0.0] * (count - len(self._asr_recent_peaks))
        for value in self._asr_recent_peaks:
            # 放宽到 55dB、最高只到 0.9：声音起伏依旧能看出来，但不会整天满格。
            if value <= 0.0005:
                levels.append(0.0)
                continue
            dbfs = 20.0 * math.log10(value)
            levels.append(min(0.9, max(0.0, (dbfs + 60.0) / 55.0)))
        try:
            self._voice_mode_toast.update_level(levels)
        except BaseException as exc:  # noqa: BLE001 - the capsule is best effort
            self._logger.warning(
                "voice level capsule update failed: error_type=%s",
                type(exc).__name__,
            )

    def _arm_openmexia_asr_locked(self, slot: int) -> bool:
        """按下 OpenMexia 语音键：先开遥控器话筒，PCM 攒着等长按阈值。

        不到 ``_OPENMEXIA_ASR_HOLD_SECONDS`` 就松手算短按：只切模式，不弹
        胶囊也不识别。按满了才进入识别状态，预录的这段音频一起用上，开口
        第一个字不会丢。
        """

        self._cancel_openmexia_asr_hold_locked()
        self._reset_asr_capture_locked()
        self._asr_slot = int(slot)
        session = self._ble_session
        if session is None:
            self._logger.info(
                "openmexia asr ignored: BLE voice session is not connected"
            )
            self._set_runtime_voice_result(
                bridge_runtime_status.VOICE_RUNTIME_HOST_START_FAILED
            )
            return False
        try:
            session.send_mic_open_threadsafe()
        except BaseException as exc:  # noqa: BLE001 - never trap input
            self._asr_slot = None
            self._logger.warning(
                "openmexia asr MIC_OPEN failed: error_type=%s",
                type(exc).__name__,
            )
            self._set_runtime_voice_result(
                bridge_runtime_status.VOICE_RUNTIME_HOST_START_FAILED
            )
            return False
        return self._arm_asr_hold_timer_locked(slot)

    def _arm_asr_hold_timer_locked(self, slot: int) -> bool:
        """Start the hold-threshold timer shared by both remote links."""

        self._asr_capture_armed = True
        token = object()
        self._asr_hold_token = token
        try:
            timer = threading.Timer(
                _OPENMEXIA_ASR_HOLD_SECONDS,
                lambda pending=token: self._openmexia_asr_hold_elapsed(pending),
            )
            timer.daemon = True
            self._asr_hold_timer = timer
            timer.start()
        except BaseException as exc:  # noqa: BLE001 - never trap input
            self._asr_capture_armed = False
            self._asr_slot = None
            self._logger.warning(
                "openmexia asr hold timer failed: error_type=%s",
                type(exc).__name__,
            )
            return False
        self._logger.info("openmexia asr capture armed: slot=%s", slot)
        return True

    def _cancel_openmexia_asr_hold_locked(self) -> None:
        timer = self._asr_hold_timer
        self._asr_hold_timer = None
        self._asr_hold_token = None
        if timer is not None:
            try:
                timer.cancel()
            except BaseException:  # noqa: BLE001 - token is already invalid
                pass

    def _openmexia_asr_hold_elapsed(self, token: object) -> None:
        """按满长按阈值：正式进入识别并弹出电平胶囊。"""

        with self._voice_shortcut.lock:
            if token is not self._asr_hold_token:
                return
            self._cancel_openmexia_asr_hold_locked()
            if not self._asr_capture_armed or self._asr_slot is None:
                return
            self._start_openmexia_asr_locked(self._asr_slot)

    def _start_openmexia_asr_locked(self, slot: int) -> bool:
        """长按确认：话筒已经开着，进入识别状态并弹出电平胶囊。"""

        self._asr_capture_armed = False
        self._asr_active = True
        # 这一下已经用于语音识别，不能再被当成短按切模式。
        self._plugin_voice_started_this_press = True
        self._voice_pcm_forwarding_enabled = False
        self._set_runtime_voice_active(True)
        self._set_runtime_voice_result(bridge_runtime_status.VOICE_RUNTIME_ACTIVE)
        # 长按期间的反馈：在模式气泡的位置弹出电平胶囊，告诉用户开始说话。
        self._show_voice_level_capsule()
        self._logger.info("openmexia asr started: slot=%s", slot)
        return True

    def _end_openmexia_asr_locked(
        self, reason: str, *, close_mic: bool = True
    ) -> None:
        """松手（或设备停流）：把这段录音交给本地模型识别，文字交给 OpenMexia。"""

        was_active = self._asr_active
        if not (was_active or self._asr_capture_armed):
            return
        self._cancel_openmexia_asr_hold_locked()
        if was_active and close_mic and not self._asr_finalize_pending:
            # 松手瞬间最后一个字还在 BLE 链路上，立刻关话筒会被切掉：
            # 再收 `_OPENMEXIA_ASR_TAIL_SECONDS` 秒，然后才真正送识别。
            self._asr_finalize_pending = True
            self._hide_voice_level_capsule()
            timer = threading.Timer(
                _OPENMEXIA_ASR_TAIL_SECONDS, self._openmexia_asr_tail_elapsed
            )
            timer.daemon = True
            self._asr_tail_timer = timer
            timer.start()
            self._logger.info("openmexia asr tail capture on %s", reason)
            return
        self._finish_openmexia_asr_locked(reason, close_mic=close_mic)

    def _openmexia_asr_tail_elapsed(self) -> None:
        """尾音收够了：正式收尾并送识别。"""

        with self._voice_shortcut.lock:
            if not self._asr_finalize_pending:
                return
            self._finish_openmexia_asr_locked("release tail", close_mic=True)

    def _finish_openmexia_asr_locked(
        self, reason: str, *, close_mic: bool
    ) -> None:
        """真正结束这次录音：关话筒、把攒下的 PCM 交给识别线程。"""

        was_active = self._asr_active
        slot = self._asr_slot
        self._asr_finalize_pending = False
        timer = self._asr_tail_timer
        self._asr_tail_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except BaseException:  # noqa: BLE001 - token is already invalid
                pass
        self._asr_active = False
        self._asr_capture_armed = False
        self._asr_slot = None
        self._voice_pcm_forwarding_enabled = False
        self._set_runtime_voice_active(False)
        frames = self._asr_frames
        self._reset_asr_capture_locked()
        self._hide_voice_level_capsule()
        if close_mic:
            session = self._ble_session
            if session is not None:
                try:
                    session.send_mic_close_threadsafe()
                except BaseException as exc:  # noqa: BLE001 - never trap input
                    self._logger.warning(
                        "openmexia asr MIC_CLOSE failed: error_type=%s",
                        type(exc).__name__,
                    )
        if not was_active:
            # 短按：这段预录原样丢掉，只当一次切模式。
            self._logger.info(
                "openmexia asr cancelled on %s: short press", reason
            )
            return
        pcm = b"".join(frame.tobytes() for frame in frames)
        if pcm:
            samples = array.array("h")
            samples.frombytes(pcm)
            if self._voice_key_gain_auto_locked(slot):
                # 自动：整段按峰值归一化到 70%，最多放大 6 倍。
                peak = 0
                for value in samples:
                    magnitude = value if value >= 0 else -value
                    if magnitude > peak:
                        peak = magnitude
                gain = (
                    min(6.0, max(0.5, 0.7 * 32768.0 / peak)) if peak > 0 else 1.0
                )
            else:
                gain = self._voice_key_gain_locked(slot)
            if gain != 1.0:
                pcm = self._apply_voice_gain(samples, gain).tobytes()
        peak = 0
        for frame in frames:
            if not frame:
                continue
            local = max(max(frame), -min(frame))
            if local > peak:
                peak = local
        self._logger.info(
            "openmexia asr ended on %s: samples=%s peak=%s",
            reason,
            len(pcm) // 2,
            peak,
        )
        if not pcm:
            return
        # 排查识别问题时用：设了 MOTEDECK_ASR_DUMP 就把这段录音存下来。
        dump_root = os.environ.get("MOTEDECK_ASR_DUMP", "").strip()
        if dump_root:
            try:
                local_asr.write_wav(
                    Path(dump_root) / f"asr-{int(time.time() * 1000)}.wav", pcm
                )
            except BaseException as exc:  # noqa: BLE001 - 调试输出不能影响识别
                self._logger.warning(
                    "openmexia asr dump failed: error_type=%s",
                    type(exc).__name__,
                )
        try:
            self._asr_runner.submit(
                pcm,
                on_result=self._on_asr_result,
            )
        except BaseException as exc:  # noqa: BLE001 - never trap input
            self._logger.warning(
                "openmexia asr submit failed: error_type=%s",
                type(exc).__name__,
            )

    def _begin_chromecast_openmexia_voice(self) -> bool:
        """Chromecast 按下麦克风：OpenMexia 槽由本机识别接管这次录音。

        谷歌遥控器按下时会自己开麦，链路不需要 MIC_OPEN，也没有快捷键可重放；
        这里先把 PCM 攒起来，按满长按阈值才算识别，短按仍然只切模式。
        调用方（语音宿主）此时已持有语音快捷键锁。
        """

        slot = self._active_voice_slot()
        if not self._openmexia_asr_ready(slot):
            return False
        self._cancel_openmexia_asr_hold_locked()
        self._reset_asr_capture_locked()
        self._asr_slot = int(slot)
        return self._arm_asr_hold_timer_locked(int(slot))

    def _feed_chromecast_openmexia_pcm(self, samples) -> None:
        """把 Chromecast 链路解出来的 PCM 交给等待中的 OpenMexia 识别。"""

        with self._voice_shortcut.lock:
            if not (self._asr_active or self._asr_capture_armed):
                return
            self._store_asr_frame_locked(array.array("h", samples))

    def _end_chromecast_openmexia_voice(self) -> bool:
        """Chromecast 松开麦克风：返回这次按下是否已经算作识别。"""

        with self._voice_shortcut.lock:
            if not (self._asr_active or self._asr_capture_armed):
                return False
            committed = self._asr_active
            self._end_openmexia_asr_locked(
                "chromecast release", close_mic=False
            )
            return committed

    def _on_asr_result(self, text: str, error: Optional[str]) -> None:
        """识别线程回调：成功就把文字交给 OpenMexia 去执行。"""

        if error:
            self._logger.info("openmexia asr failed: %s", error)
            return
        value = str(text).strip()
        if not value:
            self._logger.info("openmexia asr returned no text")
            return
        self._logger.info("openmexia asr text: %s", value)
        self._openmexia_runner.submit_text(value)

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
        entry = config.voice_key_config(self._config, int(slot))
        mode_name = config.voice_key_name(self._config, int(slot))
        # 插件模式 id 是稳定标识，用户改名字也不变；旧配置还没回填 id 的
        # 时候退回按名字认。
        mode_id = str(entry.get("plugin_mode") or "") if entry else ""
        if not bridge.send_audio_start(mode_name, mode_id):
            return False
        # 闪记在 Vokie 侧没有任何屏幕提示，复用 OpenMexia 长按的电平胶囊：
        # 会话一开始就显示，波形由 _on_pcm_frame 逐帧喂进来。
        self._plugin_voice_mode_is_quick_note = (
            mode_id == "vokie-quick-note" or "闪记" in mode_name
        )
        if self._plugin_voice_mode_is_quick_note:
            self._reset_asr_capture_locked()
            # 短按切模式可能挂过「冒新窗口就收气泡」的观察器，先撤掉，
            # 别把电平胶囊误收掉。
            try:
                self._voice_mode_toast_watch.cancel()
            except BaseException as exc:  # noqa: BLE001 - the capsule is best effort
                self._logger.warning(
                    "voice mode toast watch stop failed: error_type=%s",
                    type(exc).__name__,
                )
            self._show_voice_level_capsule()
        # 预录的 PCM 一并补发：插件在 session_accepted 之前会自己攒着，
        # 所以说话的第一个字不会被丢掉。
        buffered = self._take_plugin_voice_capture_frames_locked()
        self._plugin_voice_audio_active = True
        self._plugin_voice_started_this_press = True
        self._voice_pcm_forwarding_enabled = True
        for frame in buffered:
            frame = self._apply_voice_gain(
                frame, self._frame_gain_locked(slot, frame)
            )
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
        if self._plugin_voice_mode_is_quick_note:
            self._plugin_voice_mode_is_quick_note = False
            self._hide_voice_level_capsule()
        self._logger.info("plugin voice audio stopped on %s", reason)

    def _watch_plugin_voice_input_box(self, bridge) -> None:
        """等 Vokie 真正接下会话（输入框弹出）时再收掉模式气泡。"""

        if self._plugin_voice_toast_bridge is bridge:
            return
        try:
            bridge.sessionAccepted.connect(self._on_plugin_voice_session_accepted)
            # 链路断掉的话没人来收常驻胶囊，这边自己收。
            bridge.pluginDisconnected.connect(self._on_plugin_voice_link_lost)
        except BaseException as exc:  # noqa: BLE001 - the bubble is best effort
            self._logger.warning(
                "voice mode toast watch failed: error_type=%s",
                type(exc).__name__,
            )
            return
        self._plugin_voice_toast_bridge = bridge

    def _on_plugin_voice_session_accepted(self) -> None:
        """Vokie 接下会话：闪记的电平胶囊保持显示，其它模式照旧收气泡。"""

        if self._plugin_voice_mode_is_quick_note:
            return
        self._hide_voice_mode_toast()

    def _on_plugin_voice_link_lost(self) -> None:
        """插件链路断了：电平胶囊没人来收，这边自己收掉。"""

        self._plugin_voice_mode_is_quick_note = False
        self._hide_voice_level_capsule()
        self._hide_voice_mode_toast()

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

    def _switch_voice_tsf_locked(self, voice_slot: Optional[int]) -> None:
        """切到这条语音键配置的输入法。

        部分输入法只有成为当前 TSF 才会响应语音快捷键，所以注入之前先把
        系统输入法切过去。没有配置时（含插件语音键）什么也不做；切换失败
        只记日志，不影响语音按键本身。
        """

        profile = config.voice_key_tsf_profile(self._config, voice_slot)
        if not profile:
            return
        try:
            switched = tsf_input_windows.activate(profile)
        except BaseException as exc:  # noqa: BLE001 - never break the hotkey
            self._logger.warning(
                "tsf switch failed: slot=%s error_type=%s",
                voice_slot,
                type(exc).__name__,
            )
            return
        if switched:
            time.sleep(self._VOICE_TSF_SWITCH_SETTLE_SECONDS)

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

    def _show_voice_level_capsule(self) -> None:
        """长按 OpenMexia 语音键：在和模式气泡同一位置弹出电平胶囊。"""

        theme = config.voice_cycle_toast_theme(self._config)
        if theme == config.VOICE_CYCLE_TOAST_OFF:
            theme = config.VOICE_CYCLE_TOAST_DARK
        try:
            shown = self._voice_mode_toast.show_level(
                theme, config.voice_cycle_toast_offset(self._config)
            )
        except BaseException as exc:  # noqa: BLE001 - the capsule is best effort
            self._logger.warning(
                "voice level capsule failed: error_type=%s",
                type(exc).__name__,
            )

    def _hide_voice_level_capsule(self) -> None:
        try:
            self._voice_mode_toast.hide_level()
        except BaseException as exc:  # noqa: BLE001 - the capsule is best effort
            self._logger.warning(
                "voice level capsule hide failed: error_type=%s",
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
            if self._openmexia_asr_ready(voice_slot):
                # OpenMexia 的语音键：录音攒到松手，送百度识别，文字交给
                # OpenMexia 执行，不走插件会话。
                self._drop_voice_delayed_press_locked()
                self._force_release_plugin_voice_tokens_locked(
                    "openmexia asr press superseded"
                )
                self._plugin_voice_started_this_press = False
                return self._arm_openmexia_asr_locked(voice_slot)
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
            self._switch_voice_tsf_locked(voice_slot)
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
            if self._asr_active or self._asr_capture_armed:
                # OpenMexia 语音识别：先按原样收流，松手后整段送识别。
                self._handle_asr_frame_locked(samples, _ble_generation)
                return
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
                frame = self._apply_voice_gain(
                    samples,
                    self._frame_gain_locked(self._plugin_voice_press_slot, samples),
                )
                if self._plugin_voice_mode_is_quick_note:
                    # 闪记没有 Vokie 自己的提示，波形直接画在这边的电平胶囊上。
                    self._update_asr_level_locked(frame)
                if not bridge.send_audio(array.array("h", frame).tobytes()):
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
            scaled = self._apply_voice_gain(
                samples,
                self._frame_gain_locked(self._active_voice_slot(), samples),
            )
            if writer is None or not writer.submit(scaled):
                self._voice_pcm_forwarding_enabled = False
