"""Descriptor-backed Google remote report-to-button integration checks."""
import unittest

from ovb_rc003.chromecast_buttons import HidButtonReceiver
from ovb_rc003 import remote_layout
from ovb_rc003 import chromecast_hid_tap_windows
from ovb_rc003.chromecast_hid_tap_windows import normalize_callback_report


ENTITY = "a" * 64
GENERATION = "b" * 64


class GoogleRemoteHidPipelineTests(unittest.TestCase):
    def receiver(self, product="9451"):
        result = HidButtonReceiver(ENTITY, GENERATION, product_id=product)
        result.confirm_source()
        return result

    def test_9451_volume_and_consumer_report_idle_are_independent(self):
        receiver = self.receiver()
        volume_up = bytes.fromhex("01 01 00 00 00 00 00")
        consumer_ok = bytes.fromhex("02 05 00")
        self.assertEqual([e.button for e in receiver.feed(volume_up, 1.0)], ["volume_up"])
        self.assertEqual([e.button for e in receiver.feed(consumer_ok, 1.1)], ["ok"])

        edges = receiver.feed(bytes.fromhex("02 00 00"), 1.2)
        self.assertEqual([(e.button, e.action) for e in edges], [("ok", "up")])
        edges = receiver.feed(bytes.fromhex("01 00 00 00 00 00 00"), 1.3)
        self.assertEqual([(e.button, e.action) for e in edges], [("volume_up", "up")])

    def test_9451_selector_slots_and_both_volume_keys(self):
        receiver = self.receiver("9451")
        edges = receiver.feed(bytes.fromhex("01 00 02 01 00 00 00"), 2.0)
        self.assertEqual({(e.button, e.action) for e in edges}, {
            ("volume_up", "down"), ("volume_down", "down")
        })
        edges = receiver.feed(bytes.fromhex("01 00 00 00 00 00 00"), 2.1)
        self.assertEqual({(e.button, e.action) for e in edges}, {
            ("volume_up", "up"), ("volume_down", "up")
        })

    def test_9450_legacy_consumer_usage_and_callback_code_stay_supported(self):
        receiver = self.receiver("9450")
        edges = receiver.feed(bytes.fromhex("02 e9 00"), 3.0)
        self.assertEqual([(e.button, e.action) for e in edges], [("volume_up", "down")])
        edges = receiver.feed(bytes.fromhex("02 00 00"), 3.1)
        self.assertEqual([(e.button, e.action) for e in edges], [("volume_up", "up")])

        receiver = self.receiver("9450")
        code = remote_layout.CHROMECAST_REPORT_CODES["up"]
        edges = receiver.feed(bytes((code, 0, 0, 0, 0, 0, 0, 0)), 4.0)
        self.assertEqual([(e.button, e.action) for e in edges], [("up", "down")])

        receiver = self.receiver("9450")
        edges = receiver.feed(bytes((remote_layout.CHROMECAST_REPORT_CODES["down"], 0, 0, 0, 0, 0, 0, 0)), 4.1)
        self.assertEqual([(e.button, e.action) for e in edges], [("down", "down")])

    def test_address_bound_callback_volume_payload_flows_into_receiver(self):
        script = chromecast_hid_tap_windows._SCRIPT
        self.assertIn("addressMatches(args[0])", script)
        self.assertIn("sizeOut.readU32() !== 8", script)
        self.assertNotIn("report_lengths", script)
        self.assertNotIn("NtDeviceIoControlFile", script)

        # Callback payload loses the report ID, so descriptor + selected PID
        # determine whether the six bytes can be normalized into report 1.
        payload = bytes.fromhex("00 02 00 00 00 00")
        normalized = normalize_callback_report("9451", payload)
        self.assertEqual(normalized, bytes.fromhex("01 00 02 00 00 00 00"))
        self.assertIsNone(normalize_callback_report("9451", bytes.fromhex("02 05")))
        receiver = self.receiver("9451")
        edges = receiver.feed(normalized, 4.2)
        self.assertEqual([(e.button, e.action) for e in edges], [("volume_down", "down")])

    def test_wrong_family_shape_fails_closed_for_explicit_product(self):
        receiver = self.receiver("9451")
        edges = receiver.feed(bytes.fromhex("02 e9 00"), 5.0)
        self.assertEqual(edges, [])
        self.assertTrue(receiver.stopped)
        self.assertEqual(receiver.reason, "unknown_key_report")


if __name__ == "__main__":
    unittest.main()
