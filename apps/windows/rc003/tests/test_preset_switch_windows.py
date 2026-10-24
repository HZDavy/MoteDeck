import os
import unittest
from unittest import mock

from ovb_rc003 import preset_switch_windows as switch


class NormalizeVkTests(unittest.TestCase):
    def test_handed_modifiers_collapse_to_generic_codes(self):
        for handed, generic in switch.MODIFIER_NORMALIZATION.items():
            self.assertEqual(switch.normalize_vk(handed), generic)

    def test_non_modifier_codes_are_unchanged(self):
        for vk in (0x41, 0x31, 0x10, 0x11, 0x12, 0x5B, 0x70):
            self.assertEqual(switch.normalize_vk(vk), vk)


class MatchChordTests(unittest.TestCase):
    def test_only_an_exact_held_set_matches(self):
        targets = {frozenset({0x11, 0x41}): "preset-a"}
        self.assertEqual(switch.match_chord(frozenset({0x41, 0x11}), targets), "preset-a")
        self.assertIsNone(switch.match_chord(frozenset({0x11}), targets))
        self.assertIsNone(switch.match_chord(frozenset({0x11, 0x41, 0x12}), targets))

    def test_empty_held_set_never_matches(self):
        self.assertIsNone(switch.match_chord(frozenset(), {frozenset(): "ignored"}))


class ListenerTests(unittest.TestCase):
    def _listener(self):
        seen = []
        return switch.PresetSwitchListener(on_switch=seen.append), seen

    def test_empty_chords_are_filtered_and_latch_stops_auto_repeat(self):
        listener, seen = self._listener()
        self.assertFalse(listener.is_running)
        listener.set_targets({frozenset({0x11, 0x41}): "a", frozenset(): "ignored"})
        listener._handle_edge(0x11, True)
        self.assertEqual(seen, [])
        listener._handle_edge(0x41, True)
        self.assertEqual(seen, ["a"])
        listener._handle_edge(0x41, True)  # auto-repeat while the chord is held
        self.assertEqual(seen, ["a"])
        listener._handle_edge(0x41, False)
        listener._handle_edge(0x11, False)
        listener._handle_edge(0x11, True)
        listener._handle_edge(0x41, True)
        self.assertEqual(seen, ["a", "a"])

    def test_consumer_errors_do_not_escape_the_hook(self):
        def boom(_identity):
            raise RuntimeError("consumer failed")

        listener = switch.PresetSwitchListener(on_switch=boom)
        listener.set_targets({frozenset({0x42}): "b"})
        listener._handle_edge(0x42, True)  # must never raise back into the OS hook

    def test_start_is_refused_when_live_input_is_disabled(self):
        listener, _ = self._listener()
        listener.set_targets({frozenset({0x41}): "a"})
        with mock.patch.dict(os.environ, {"RC003_DISABLE_LIVE_INPUT": "1"}):
            with self.assertRaises(switch.PresetSwitchUnavailableError):
                listener.start()
        self.assertFalse(listener.is_running)
