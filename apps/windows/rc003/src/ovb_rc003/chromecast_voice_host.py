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
            if not services.shortcut.apply(action):
                services.shortcut.controller.cancel_pending()
                self._fail_voice("语音快捷键未能送达")
                return
            self.engaged = True
            self.state = "recording"
            services.set_active(True)
            services.set_result(status.VOICE_RUNTIME_ACTIVE)
            self.reply("ready")

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
            self._stop_host(event.get("data", ""))
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

    def _stop_host(self, reason=""):
        services = self.services
        if self.closed:
            self.reply("released")
            return True
        self.state = "stopping"
        released = False
        with services.shortcut.lock:
            services.disable_forwarding()
            tail, self._output_pcm = self._output_pcm, []
            if tail and self.failure_result is None and not self.stopping.is_set():
                # A normal remote release drains the last partial block before
                # releasing its shortcut.
                self._submit_output(tail)
            audio_released = self._finish_playback("Chromecast voice end")
            released = self._release_shortcut() and audio_released
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
