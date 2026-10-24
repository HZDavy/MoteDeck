"""Tap-latch / hold gesture resolution for one voice key.

The product no longer distinguishes between remote generations or ships a
"short-press type / long-press type" switch. Every button promoted to a voice
slot behaves the same way:

* a **tap** (press shorter than :data:`VoiceGestureLatch.DEFAULT_TAP_MAX_SECONDS`)
  latches the voice session open until the same key is tapped again;
* a **hold** (press longer than that window) keeps the session open until the
  key is physically released.

The decision cannot happen on press alone, because the press duration is only
known at release time. The state machine therefore reports what the caller must
do for each edge and owns no I/O of its own, which keeps it directly testable.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Callable

from .button_gesture import ButtonGestureDispatcher


class VoiceGestureDecision(str, Enum):
    """What the caller must do with the voice session for one key edge."""

    OPEN = "open"
    LATCH = "latch"
    CLOSE = "close"
    IGNORE = "ignore"


class VoiceGestureState(str, Enum):
    IDLE = "idle"
    HOLDING = "holding"
    LATCHED = "latched"


class VoiceGestureLatch:
    """Resolve one voice key's tap/hold semantics.

    ``on_press`` and ``on_release`` are pure bookkeeping; the caller decides how
    to open/close the microphone and the host shortcut.
    """

    DEFAULT_TAP_MAX_SECONDS = ButtonGestureDispatcher.LONG_PRESS_SECONDS

    def __init__(
        self,
        *,
        tap_max_seconds: float = DEFAULT_TAP_MAX_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tap_max_seconds = float(tap_max_seconds)
        self._clock = clock
        self._state = VoiceGestureState.IDLE
        self._pressed_at = 0.0

    @property
    def state(self) -> VoiceGestureState:
        return self._state

    @property
    def latched(self) -> bool:
        """Return whether a tap currently keeps the session open."""

        return self._state is VoiceGestureState.LATCHED

    @property
    def active(self) -> bool:
        """Return whether the key currently owns an open session."""

        return self._state is not VoiceGestureState.IDLE

    def on_press(self) -> VoiceGestureDecision:
        if self._state is VoiceGestureState.HOLDING:
            return VoiceGestureDecision.IGNORE
        if self._state is VoiceGestureState.LATCHED:
            # "keep the microphone open until pressed again" - the next press
            # is that second press, so it ends the session.
            self._state = VoiceGestureState.IDLE
            self._pressed_at = 0.0
            return VoiceGestureDecision.CLOSE
        self._state = VoiceGestureState.HOLDING
        self._pressed_at = self._clock()
        return VoiceGestureDecision.OPEN

    def on_release(self) -> VoiceGestureDecision:
        if self._state is not VoiceGestureState.HOLDING:
            return VoiceGestureDecision.IGNORE
        held_seconds = self._clock() - self._pressed_at
        self._pressed_at = 0.0
        if held_seconds < self._tap_max_seconds:
            self._state = VoiceGestureState.LATCHED
            return VoiceGestureDecision.LATCH
        self._state = VoiceGestureState.IDLE
        return VoiceGestureDecision.CLOSE

    def reset(self) -> None:
        """Drop any half-finished or latched gesture (abort, disconnect)."""

        self._state = VoiceGestureState.IDLE
        self._pressed_at = 0.0
