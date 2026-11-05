"""Chromecast hold-to-talk voice owner.

The receiver worker decodes the remote's private voice stream and forwards
control/audio events here. This owner keeps the shared audio endpoint and the
recorded-shortcut ownership: the shortcut goes down when the remote's mic opens
and up again when the remote releases it. Nothing here is input-method
specific - no provider detection, no text submission.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .atvv_session import ATVVSession
from .voice_audio_session import VoiceAudioSession
from .voice_shortcut_session import VoiceShortcutSession
from . import bridge_runtime_status as status

# ATVV decodes mono 16 kHz PCM; 100 ms blocks keep the writer queue shallow.
_OUTPUT_BLOCK_SAMPLES = 1600


@dataclass(frozen=True)
class VoiceHostServices:
    """Shared capabilities used by the Chromecast recording policy."""

    audio: VoiceAudioSession
    shortcut: VoiceShortcutSession
    settings: Callable
    logger: object
    reload_settings: Callable
    apply_pending_settings: Callable
    begin_diagnostic: Callable
    finish_diagnostic: Callable
    set_active: Callable
    set_result: Callable
    disable_forwarding: Callable
    prepare_shortcut: Callable[[], None] | None = None
    claim_detection: Callable[[], bool] | None = None
    press_delay_seconds: Callable[[], float] | None = None
    on_short_tap: Callable[[], None] | None = None
    inject_mode: Callable[[], str | None] | None = None


class VoiceHost:
    def __init__(self, services: VoiceHostServices):
        self.services = services
        self.client = None
        self.events = queue.Queue(maxsize=256)
        self.stopping = threading.Event()
        self.thread = None
        self._cleanup_lock = threading.Lock()
        self.attempt = 0
        self.state = "idle"
        self.closed = True
        self.decoder = None
        self.engaged = False
        self.diagnostic_started = False
        self.failure_result = None
        self._output_pcm = []
        self._playback_cleanup_pending = False
        self._last_stop_snapshot = None
        self._press_timer = None
        self._press_token = None
        self._press_delivered = False
        self._detection_press = False

    # -- receiver-facing API -------------------------------------------------

    def start(self, client):
        self.client = client
        self.thread = threading.Thread(
            target=self._run, name="chromecast-voice-host", daemon=True
        )
        self.thread.start()

    def enqueue(self, event):
        if self.stopping.is_set():
            return
        try:
            self.events.put_nowait(dict(event))
        except queue.Full:
            self.request_stop()
            if self.client is not None:
                self.client.cancel.set()

    def request_stop(self):
        self.stopping.set()

    def stop(self):
        self.request_stop()
        if self.thread:
            self.thread.join(6)
        if self.thread and not self.thread.is_alive() and not self.closed:
            self._close_resources()
        if (self.thread and self.thread.is_alive()) or not self.closed:
            snapshot = (
                bool(self.thread and self.thread.is_alive()),
                self.closed,
                self.services.audio.writer is not None,
                self.services.audio.sink is not None,
            )
            if snapshot != self._last_stop_snapshot:
                self._last_stop_snapshot = snapshot
                self.services.logger.warning(
                    "Chromecast voice stop pending: thread_alive=%s closed=%s "
                    "writer_present=%s playback_present=%s",
                    *snapshot,
                )
            raise RuntimeError("语音清理尚未确认，暂时不能切换设备。")

    def reply(self, result):
        if self.client is not None and self.attempt:
            self.client.voice_host(self.attempt, result)

    # -- startup -------------------------------------------------------------

    def _start_host(self, attempt):
        services = self.services
        if self.state != "idle" or not self.closed or attempt <= self.attempt:
            return
        # Refresh before claiming the shortcut so one attempt never mixes two
        # hotkey configurations.
        services.reload_settings()
        with services.shortcut.lock:
            if self.state != "idle" or not self.closed or attempt <= self.attempt:
                return
            self.attempt, self.state = attempt, "starting"
            self.closed = False
            self.engaged = False
            self.failure_result = None
            self._output_pcm = []
            self._playback_cleanup_pending = False
            if services.claim_detection is not None and services.claim_detection():
                # 按键检测进行中：这次麦克风按下只当作检测结果，既不录音，
                # 也不发送语音快捷键、不切换语音模式。
                self._detection_press = True
                self.state = "detecting"
                self.reply("ready")
                return
            if services.prepare_shortcut is not None:
                services.prepare_shortcut()
            tokens = services.shortcut.recorded_tokens()
            if not tokens:
                self._fail_voice("尚未设置语音快捷键，无法开始语音输入")
                return
            if services.shortcut.pending_tokens and not services.shortcut.release_pending():
                self._fail_voice("上一次语音快捷键的清理尚未确认")
                return
            if not services.audio.open():
                self._fail_voice(
                    "无法打开音频输出设备",
                    status.VOICE_RUNTIME_OUTPUT_OPEN_FAILED,
                )
                return
            services.audio.stats.reset()
            self.decoder = ATVVSession(
                gain_db=float(services.settings().get("gain_db", 10.0))
            )
            services.begin_diagnostic(effective_hotkey_tokens=tokens)
            self.diagnostic_started = True
            action = services.shortcut.controller.on_mic_button_pressed()
            inject_mode = self._inject_mode()
            self._press_delivered = False
            delay = self._press_delay_seconds()
            if delay > 0:
                # Same tap window as the Xiaomi mic button: releasing before it
                # injects nothing and only switches the voice mode.
                self._arm_press_delay(action, delay, inject_mode)
            elif not services.shortcut.apply(
                action, inject_mode=inject_mode, require_tracked_hold=False
            ):
                # This remote never brings Raw Input or the voice-key
                # physicalizer, so a tracked hold can never be established; the
                # Chromecast voice key injects the recorded shortcut untracked.
                services.shortcut.controller.cancel_pending()
                self._fail_voice("语音快捷键未能送达")
                return
            else:
                self._press_delivered = True
            self.engaged = True
            self.state = "recording"
            services.set_active(True)
            services.set_result(status.VOICE_RUNTIME_ACTIVE)
            self.reply("ready")

    def _press_delay_seconds(self) -> float:
        callback = self.services.press_delay_seconds
        if callback is None:
            return 0.0
        try:
            return max(0.0, float(callback()))
        except Exception:  # noqa: BLE001 - a bad config must not kill the press
            self.services.logger.exception(
                "Chromecast voice press delay unavailable"
            )
            return 0.0

    def _inject_mode(self) -> str | None:
        callback = self.services.inject_mode
        if callback is None:
            return None
        try:
            return callback()
        except Exception:  # noqa: BLE001 - a bad config must not kill the press
            self.services.logger.exception(
                "Chromecast voice inject mode unavailable"
            )
            return None

    def _arm_press_delay(self, action, delay, inject_mode):
        token = object()
        self._cancel_press_delay()
        timer = threading.Timer(
            delay, self._deliver_delayed_press, args=(token, action, inject_mode)
        )
        timer.daemon = True
        self._press_token = token
        self._press_timer = timer
        timer.start()

    def _cancel_press_delay(self):
        timer, self._press_timer = self._press_timer, None
        self._press_token = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:  # noqa: BLE001 - cancellation is best effort
                pass

    def _deliver_delayed_press(self, token, action, inject_mode):
        """Send the held shortcut once the tap window has passed."""

        services = self.services
        with services.shortcut.lock:
            if token is not self._press_token:
                return
            self._press_token = None
            self._press_timer = None
            if self.state != "recording" or self.stopping.is_set():
                return
            if not services.shortcut.apply(
                action, inject_mode=inject_mode, require_tracked_hold=False
            ):
                services.shortcut.controller.cancel_pending()
                self._fail_voice("语音快捷键未能送达")
                return
            self._press_delivered = True
            services.logger.info(
                "Chromecast voice shortcut held: attempt=%s", self.attempt
            )

    def _fail_voice(self, reason, result=status.VOICE_RUNTIME_HOST_START_FAILED):
        # Callers may hold the shortcut lock, so only record the failure here;
        # the run loop performs the actual release outside the lock.
        self.services.logger.warning("Chromecast voice failed: %s", reason)
        self.failure_result = result
        self.services.set_result(result)
        self.state = "stopping"
        self.reply("failed")

    # -- events --------------------------------------------------------------

    def _handle(self, event):
        kind, attempt = event["event"], event["attempt"]
        if kind == "available":
            self.services.logger.info(
                "Chromecast voice control availability=%s", event["data"]
            )
            return
        if kind == "host_start":
            self._start_host(attempt)
            return
        if attempt != self.attempt:
            return
        if kind == "host_stop":
            self._stop_host(event.get("data", ""), from_remote_release=True)
        elif kind == "state":
            if event["data"] == "idle" and self.closed:
                self.state = "idle"
                self.decoder = None
        elif (
            kind == "control"
            and self.decoder is not None
            and self.state in ("starting", "recording")
        ):
            self.decoder.handle_control(bytes.fromhex(event["data"]))
        elif (
            kind == "audio"
            and self.decoder is not None
            and self.decoder.mic_open
            and self.state in ("starting", "recording")
        ):
            samples = self.decoder.handle_audio(bytes.fromhex(event["data"]))
            if samples:
                self._output(samples)

    def _output(self, samples):
        if (
            self.stopping.is_set()
            or not self.engaged
            or self.state != "recording"
            or self.failure_result is not None
        ):
            return
        self._output_pcm.extend(samples)
        while len(self._output_pcm) >= _OUTPUT_BLOCK_SAMPLES:
            block = self._output_pcm[:_OUTPUT_BLOCK_SAMPLES]
            del self._output_pcm[:_OUTPUT_BLOCK_SAMPLES]
            if not self._submit_output(block):
                return

    def _submit_output(self, samples):
        writer = self.services.audio.writer
        if writer is not None and writer.submit(samples):
            return True
        self.failure_result = (
            self.failure_result or status.VOICE_RUNTIME_HOST_STOP_FAILED
        )
        self._playback_cleanup_pending = True
        self.reply("stop")
        self.state = "stopping"
        return False

    # -- release -------------------------------------------------------------

    def _release_shortcut(self):
        services = self.services
        action = services.shortcut.controller.reset()
        if action is not None and not services.shortcut.apply(action):
            services.shortcut.controller.restore_pending(action)
            return False
        return services.shortcut.release_pending() and not services.shortcut.controller.active

    def _finish_playback(self, reason):
        if not self._playback_cleanup_pending:
            flush = self.services.audio.flush(reason)
            if flush.error is None:
                return flush.completed
            self.failure_result = (
                self.failure_result or status.VOICE_RUNTIME_HOST_STOP_FAILED
            )
            self._playback_cleanup_pending = True
            self.services.logger.error(
                "Chromecast audio failed during %s: %s", reason, flush.error
            )
        return self._retire_playback()

    def _retire_playback(self):
        services = self.services
        if services.audio.writer is not None and not services.audio.stop_writer():
            return False
        if services.audio.sink is not None:
            try:
                services.audio.close_sink()
            except Exception:
                services.logger.exception("Chromecast audio endpoint cleanup pending")
                return False
        return True

    def _notify_short_tap(self):
        callback = self.services.on_short_tap
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 - a UI toast must not break cleanup
            self.services.logger.exception("Chromecast voice mode switch failed")

    def _stop_host(self, reason="", *, from_remote_release=False):
        services = self.services
        if self.closed:
            self.reply("released")
            return True
        self.state = "stopping"
        released = False
        tapped = False
        with services.shortcut.lock:
            services.disable_forwarding()
            delivered = self._press_delivered
            detection = self._detection_press
            self._detection_press = False
            self._cancel_press_delay()
            if (
                from_remote_release
                and not delivered
                and not detection
                and self.failure_result is None
            ):
                # Released inside the tap window: no key was injected, so this
                # press only switches the voice mode, like the Xiaomi mic.
                services.shortcut.controller.cancel_pending()
                tapped = True
            tail, self._output_pcm = self._output_pcm, []
            if tail and self.failure_result is None and not self.stopping.is_set():
                # A normal remote release drains the last partial block before
                # releasing its shortcut.
                self._submit_output(tail)
            audio_released = self._finish_playback("Chromecast voice end")
            released = audio_released and (detection or self._release_shortcut())
            services.set_active(False)
            if released:
                services.apply_pending_settings()
            else:
                services.set_result(status.VOICE_RUNTIME_HOST_STOP_FAILED)
            if released and self.diagnostic_started:
                services.finish_diagnostic(
                    "chromecast_session_closed",
                    pcm_frames=services.audio.stats.frames,
                )
                self.diagnostic_started = False
        self.closed = released
        if released:
            self.engaged = False
            self.decoder = None
        self._press_delivered = False
        if tapped:
            self._notify_short_tap()
        services.logger.info(
            "Chromecast voice attempt=%s reason=%s released=%s",
            self.attempt, reason or "cleanup", released,
        )
        self.reply("released" if released else "release_failed")
        return released

    # -- loop ----------------------------------------------------------------

    def _run(self):
        try:
            while not self.stopping.is_set():
                try:
                    event = self.events.get(timeout=.02)
                except queue.Empty:
                    event = None
                if event is not None:
                    self._handle(event)
                if self.state == "stopping" and not self.closed:
                    self._stop_host()
        except Exception:
            self.services.logger.exception("Chromecast voice processing failed")
            self.reply("stop")
        finally:
            self.reply("stop")
            self._close_resources()

    def _close_resources(self):
        with self._cleanup_lock:
            session_closed = self._stop_host()
            audio_closed = self._retire_playback()
            self.closed = session_closed and audio_closed
