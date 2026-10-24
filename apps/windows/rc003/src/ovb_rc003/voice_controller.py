"""Host-shortcut session ownership for the RC003 voice shortcut.

The RC003 device autonomously tells the host when its own physical mic button
is pressed (ATVV control opcode 0x08) and when its audio stream stops
(opcode 0x00) - see atvv_session.py. This module owns the outstanding host
shortcut session: it reports whether a key-down is still owed a key-up. It
performs no I/O itself, so it's fully unit testable; app.py turns the reported
edges into real host key events.

- The controller reports key-down when the mic opens and key-up when the
  session closes. Both the short-press latch gesture and the long-press hold
  gesture are expressed as that same pair of edges (see voice_gesture.py for
  the press/release decision logic). The device's AUDIO_STOP remains a
  fallback for machines that expose no release edge.
- Cleanup is provable: reset() reports whether KEY_UP is still owed and never
  leaves the controller thinking a session is active.

"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class VoiceHostAction(str, Enum):
    KEY_DOWN = "key_down"
    KEY_UP = "key_up"


class VoiceController:
    def __init__(self) -> None:
        self._holding = False

    @property
    def holding(self) -> bool:
        """Whether a key-down is currently outstanding and owes key-up."""

        return self._holding

    @property
    def active(self) -> bool:
        """Whether a host shortcut session is open."""

        return self._holding

    def on_mic_button_pressed(self) -> VoiceHostAction:
        """React to the device's own MIC_BUTTON control opcode."""

        self._holding = True
        return VoiceHostAction.KEY_DOWN

    def on_audio_stopped(self) -> Optional[VoiceHostAction]:
        """React to the device's own AUDIO_STOP control opcode.

        This is the fallback release when Windows did not expose the physical
        button-up edge.
        """

        return self.on_mic_button_released()

    def on_mic_button_released(self) -> Optional[VoiceHostAction]:
        """Release an outstanding host shortcut on the physical button-up.

        Releasing here prevents Ctrl/Alt/Win from remaining logically down if
        the BLE control channel never reports AUDIO_STOP. Repeating the call
        from a later AUDIO_STOP is harmless and produces no second key-up.
        """

        if self._holding:
            self._holding = False
            return VoiceHostAction.KEY_UP
        return None

    def reset(self) -> Optional[VoiceHostAction]:
        """Force any outstanding session closed, e.g. on disconnect/shutdown
        cleanup.

        Returns KEY_UP if a key-down is still outstanding, otherwise None.
        ``active`` is False immediately after this returns.
        """

        if self._holding:
            self._holding = False
            return VoiceHostAction.KEY_UP
        return None

    def restore_pending(self, action: VoiceHostAction) -> None:
        """Undoes a closing action's eager state-clearing
        for a closing ``action`` that is now known to have failed to
        deliver (XRBM-019 review round 1 P1 #4).

        ``reset()`` clears the outstanding state before the caller has
        actually attempted to deliver the closing action. If it fails
        (``win32_input.send_key_combo_up`` now raises instead of swallowing
        that - see win32_input.py), the controller must go back to thinking
        the session is still owed, not silently "closed": a failed KEY_UP
        should not stop the caller from retrying the release.
        A caller passes exactly the closing ``VoiceHostAction`` that failed.
        """

        if action == VoiceHostAction.KEY_UP:
            self._holding = True

    def cancel_pending(self) -> None:
        """Clear an outstanding session WITHOUT emitting a compensating host
        action - used when the action ``on_mic_button_pressed()`` just
        returned failed to actually deliver (see app.py's
        ``_handle_mic_button_pressed``): nothing physically landed, so there
        is nothing to release, and attempting a compensating action would
        itself be a second delivery attempt liable to fail the same way.
        """

        self._holding = False
