import unittest

from ovb_rc003.voice_gesture import (
    VoiceGestureDecision,
    VoiceGestureLatch,
    VoiceGestureState,
)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class VoiceGestureLatchTests(unittest.TestCase):
    def setUp(self):
        self.clock = _FakeClock()
        self.gesture = VoiceGestureLatch(clock=self.clock)

    def test_idle_state_ignores_a_bare_release(self):
        self.assertEqual(
            self.gesture.on_release(), VoiceGestureDecision.IGNORE
        )
        self.assertFalse(self.gesture.active)
        self.assertFalse(self.gesture.latched)

    def test_press_opens_the_session(self):
        self.assertEqual(self.gesture.on_press(), VoiceGestureDecision.OPEN)
        self.assertEqual(self.gesture.state, VoiceGestureState.HOLDING)

    def test_short_press_latches_on_release(self):
        self.gesture.on_press()
        self.clock.advance(0.2)
        self.assertEqual(self.gesture.on_release(), VoiceGestureDecision.LATCH)
        self.assertTrue(self.gesture.latched)
        self.assertTrue(self.gesture.active)

    def test_long_press_closes_on_release(self):
        self.gesture.on_press()
        self.clock.advance(2.0)
        self.assertEqual(self.gesture.on_release(), VoiceGestureDecision.CLOSE)
        self.assertFalse(self.gesture.active)

    def test_second_tap_closes_a_latched_session(self):
        self.gesture.on_press()
        self.clock.advance(0.1)
        self.gesture.on_release()
        self.assertTrue(self.gesture.latched)
        self.assertEqual(self.gesture.on_press(), VoiceGestureDecision.CLOSE)
        self.assertFalse(self.gesture.active)
        self.assertFalse(self.gesture.latched)
        self.assertEqual(
            self.gesture.on_release(), VoiceGestureDecision.IGNORE
        )

    def test_duplicate_press_while_holding_is_ignored(self):
        self.gesture.on_press()
        self.assertEqual(self.gesture.on_press(), VoiceGestureDecision.IGNORE)
        self.assertEqual(self.gesture.state, VoiceGestureState.HOLDING)

    def test_release_while_latched_is_ignored(self):
        self.gesture.on_press()
        self.clock.advance(0.1)
        self.gesture.on_release()
        self.assertEqual(
            self.gesture.on_release(), VoiceGestureDecision.IGNORE
        )
        self.assertTrue(self.gesture.latched)

    def test_tap_boundary_is_exclusive(self):
        self.clock.now = 0.0
        self.gesture.on_press()
        self.clock.advance(VoiceGestureLatch.DEFAULT_TAP_MAX_SECONDS)
        self.assertEqual(self.gesture.on_release(), VoiceGestureDecision.CLOSE)

    def test_tap_threshold_is_the_shared_long_press_constant(self):
        self.assertEqual(VoiceGestureLatch.DEFAULT_TAP_MAX_SECONDS, 0.550)

    def test_reset_drops_a_latched_gesture(self):
        self.gesture.on_press()
        self.clock.advance(0.1)
        self.gesture.on_release()
        self.gesture.reset()
        self.assertFalse(self.gesture.active)
        self.assertFalse(self.gesture.latched)
        self.assertEqual(self.gesture.on_release(), VoiceGestureDecision.IGNORE)

    def test_reset_drops_a_held_gesture(self):
        self.gesture.on_press()
        self.gesture.reset()
        self.assertFalse(self.gesture.active)
        self.assertEqual(self.gesture.on_press(), VoiceGestureDecision.OPEN)


if __name__ == "__main__":
    unittest.main()
