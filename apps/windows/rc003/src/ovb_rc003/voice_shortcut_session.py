"""Shared shortcut ownership and deferred releases.

Device gestures stay in their device policy. Application callbacks supply status,
configuration and the existing recovery policy; this owner retains one set of
keys and release timers under the original locks.

The RC003 voice shortcut is no longer tied to any input method: a hold gesture
simply replays the shortcut the user recorded, so the only delivery path here is
the recorded key replay.  The target application may still reject injected keys,
which is what the Interception injection mode (``injection_backend``) is for.
"""
from __future__ import annotations
import threading
from dataclasses import dataclass
from typing import Callable, Optional, Tuple
from . import hotkey, injection_backend, voice_controller, win32_input

_VOICE_HOTKEY_RELEASE_RETRY_INITIAL_SECONDS = 0.1
_VOICE_HOTKEY_RELEASE_RETRY_MAX_SECONDS = 2.0
_VOICE_HOTKEY_BACKEND_MARKED = "marked_keybd_event"
# Injected keys never auto-repeat, so the recorded shortcut of a held voice key
# is re-sent here at the typematic rate.  The first repeat starts at the same
# 0.55 s boundary the tap/hold gesture uses, so a tap never looks like a hold.
_VOICE_HOTKEY_REPEAT_INITIAL_DELAY_SECONDS = 0.550

@dataclass(frozen=True)
class ShortcutServices:
    configured_backend: Callable
    resume_tracking: Callable
    set_result: Callable
    current_state: Callable
    request_cleanup: Callable
    prepare_mute_guard: Callable


class VoiceShortcutSession:
    def __init__(self, *, hotkey_text, logger, trace, services):
        self.services = services
        self.logger = logger
        self.trace = trace
        self.lock = threading.Lock()
        self.controller = voice_controller.VoiceController()
        self.hotkey: Optional[hotkey.HotkeySpec] = (
            hotkey.HotkeySpec.parse(hotkey_text) if str(hotkey_text).strip() else None
        )
        self.pending_tokens: Optional[Tuple[str, ...]] = None
        self.active_backend: Optional[str] = None
        self.pending_backend: Optional[str] = None
        # Injection backend the pending hold was started with.  The release has
        # to use the same one, otherwise an Interception key-down would be
        # released through SendInput and stay stuck.
        self.pending_inject_mode: Optional[str] = None
        self.retry_timer: Optional[object] = None
        self.retry_token: Optional[object] = None
        self.retry_delay = (
            _VOICE_HOTKEY_RELEASE_RETRY_INITIAL_SECONDS
        )
        self.retry_stopping = False
        self.repeat_timer: Optional[object] = None
        self.repeat_token: Optional[object] = None
        self.repeat_stopping = True
        self.timer_factory = threading.Timer

    def recorded_tokens(self) -> Tuple[str, ...]:
        """Tokens for the recorded shortcut, or () when the user has not set one."""

        spec = self.hotkey
        if spec is None:
            return ()
        return tuple(spec.modifiers) + (spec.key,)

    def apply(
        self,
        action: voice_controller.VoiceHostAction,
        *,
        backend_override: Optional[str] = None,
        tokens_override: Optional[Tuple[str, ...]] = None,
        cancelled=None,
        voice_slot: Optional[int] = None,
        inject_mode: Optional[str] = None,
    ) -> bool:
        backend = backend_override or self.services.configured_backend()
        tokens = tokens_override or self.recorded_tokens()
        if action == voice_controller.VoiceHostAction.KEY_UP:
            # Stop the typematic re-sends before the release edge goes out.
            self._stop_hold_repeat_locked()
            # The release must go out on the same injection mode the press used.
            if inject_mode is None:
                inject_mode = self.pending_inject_mode
        trace_context = {
            **self.trace.current_context(),
            "action": str(action.value),
        }
        self.trace.emit(
            "voice_hotkey_requested",
            **trace_context,
            backend=str(backend),
            inject_mode=str(inject_mode),
            tokens=list(tokens),
            source="voice_controller",
        )
        try:
            if action == voice_controller.VoiceHostAction.KEY_DOWN:
                self.pending_tokens = tokens
                self.pending_backend = backend
                self.pending_inject_mode = inject_mode
            self.send_action(action, tokens, inject_mode)
            if action == voice_controller.VoiceHostAction.KEY_DOWN:
                self.active_backend = backend
                self._start_hold_repeat_locked()
            if action == voice_controller.VoiceHostAction.KEY_UP:
                self.pending_tokens = None
                self.pending_backend = None
                self.pending_inject_mode = None
                self.active_backend = None
                self.services.resume_tracking()
            return True
        except win32_input.Win32InputUnavailableError:
            self._stop_hold_repeat_locked()
            if action == voice_controller.VoiceHostAction.KEY_DOWN:
                self.pending_tokens = None
                self.pending_backend = None
                self.pending_inject_mode = None
                self.active_backend = None
            self.logger.info("voice hotkey action skipped: no usable voice input backend")
            return False
        except win32_input.InputCleanupIncompleteError:
            self._stop_hold_repeat_locked()
            self.pending_tokens = tokens
            self.pending_backend = backend
            self.pending_inject_mode = inject_mode
            self.schedule_release_retry()
            self.logger.exception(
                "voice hotkey action failed and safety key-up remains pending"
            )
            return False
        except OSError:
            self._stop_hold_repeat_locked()
            if action == voice_controller.VoiceHostAction.KEY_DOWN:
                self.pending_tokens = None
                self.pending_backend = None
                self.pending_inject_mode = None
                self.active_backend = None
            self.logger.exception("voice hotkey action failed to fully deliver")
            return False

    @staticmethod
    def send_action(
        action: voice_controller.VoiceHostAction,
        tokens: Tuple[str, ...],
        inject_mode: Optional[str] = None,
    ) -> None:
        backend = injection_backend.backend_for(inject_mode)
        if action == voice_controller.VoiceHostAction.KEY_DOWN:
            backend.key_down(tokens)
        else:
            backend.key_up(tokens)

    def _start_hold_repeat_locked(self) -> None:
        """Begin typematic re-sends for the shortcut that is currently held."""

        self._stop_hold_repeat_locked()
        if not self.pending_tokens:
            return
        self.repeat_stopping = False
        self.repeat_token = object()
        self._schedule_repeat_locked(
            _VOICE_HOTKEY_REPEAT_INITIAL_DELAY_SECONDS
        )

    def _schedule_repeat_locked(self, delay: float) -> None:
        token = self.repeat_token
        if token is None or self.repeat_stopping:
            return
        try:
            timer = self.timer_factory(
                delay,
                lambda: self._repeat_tick(token),
            )
            timer.daemon = True
            self.repeat_timer = timer
            timer.start()
        except Exception:
            self.repeat_timer = None
            self.repeat_stopping = True
            self.logger.exception("voice hotkey repeat timer failed")

    def _repeat_tick(self, token: object) -> None:
        # Holding the session lock keeps a release edge from interleaving with
        # a repeated key-down, which would leave the key stuck down.
        with self.lock:
            if self.repeat_token is not token or self.repeat_stopping:
                return
            self.repeat_timer = None
            tokens = self.pending_tokens
            inject_mode = self.pending_inject_mode
            if tokens is None:
                self.repeat_stopping = True
                return
            try:
                injection_backend.backend_for(inject_mode).key_repeat(tokens)
            except Exception:
                self.repeat_stopping = True
                self.logger.exception(
                    "voice hotkey repeat failed; holding stopped"
                )
                return
            self._schedule_repeat_locked(win32_input.keyboard_repeat_interval())

    def _stop_hold_repeat_locked(self) -> None:
        self.repeat_stopping = True
        timer = self.repeat_timer
        self.repeat_timer = None
        self.repeat_token = None
        if timer is not None:
            cancel = getattr(timer, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:
                    pass

    def cancel_release_retry(
        self,
        *,
        reset_delay: bool = True,
    ) -> None:
        timer = self.retry_timer
        self.retry_timer = None
        self.retry_token = None
        if reset_delay:
            self.retry_delay = (
                _VOICE_HOTKEY_RELEASE_RETRY_INITIAL_SECONDS
            )
        if timer is not None:
            cancel = getattr(timer, "cancel", None)
            if callable(cancel):
                cancel()

    def schedule_release_retry(self) -> None:
        if (
            self.retry_stopping
            or self.pending_tokens is None
            or self.retry_timer is not None
        ):
            return
        token = object()
        try:
            timer = self.timer_factory(
                self.retry_delay,
                lambda: self._retry_release(token),
            )
            timer.daemon = True
            self.retry_token = token
            self.retry_timer = timer
            timer.start()
        except Exception:
            if self.retry_token is token:
                self.retry_token = None
                self.retry_timer = None
            self.logger.exception("voice hotkey safety-release timer failed")

    def _retry_release(self, token: object) -> None:
        with self.lock:
            if self.retry_token is not token:
                return
            self.retry_token = None
            self.retry_timer = None
            if (
                self.retry_stopping
                or self.pending_tokens is None
            ):
                return
            self.retry_delay = min(
                _VOICE_HOTKEY_RELEASE_RETRY_MAX_SECONDS,
                max(
                    _VOICE_HOTKEY_RELEASE_RETRY_INITIAL_SECONDS,
                    self.retry_delay * 2.0,
                ),
            )
            self.release_pending()

    def release_pending(self) -> bool:
        tokens = self.pending_tokens
        if tokens is None:
            return True
        self._stop_hold_repeat_locked()
        try:
            self.send_action(
                voice_controller.VoiceHostAction.KEY_UP,
                tokens,
                self.pending_inject_mode,
            )
        except (win32_input.Win32InputUnavailableError, OSError):
            self.logger.exception("voice hotkey safety release failed")
            self.schedule_release_retry()
            return False
        self.pending_tokens = None
        self.pending_backend = None
        self.pending_inject_mode = None
        self.active_backend = None
        self.cancel_release_retry()
        self.logger.info("voice hotkey safety release completed")
        self.services.resume_tracking()
        return True
