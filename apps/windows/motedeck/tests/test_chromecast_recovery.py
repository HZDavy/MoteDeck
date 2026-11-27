"""谷歌遥控器接收自愈、原因上报、符号降级与清理容错的回归测试。"""
import unittest
from unittest import mock

from motedeck import chromecast_hid_source, chromecast_hid_worker
from motedeck.chromecast_buttons import empty_stop_details
from motedeck.chromecast_channel import Channel, DIAGNOSTIC_REASONS, SessionIdentity
from motedeck.chromecast_client import message
from motedeck.chromecast_device_windows import SelectedDevice
from motedeck.chromecast_hid_gatt_windows import decode_report
from motedeck.chromecast_hid_tap_windows import (_KNOWN_BUILD_RVAS, SYMBOL_REASONS,
                                                TapError, _TESTED_SHA256, _TESTED_RVA)
from motedeck.chromecast_observation import valid_record
from motedeck.chromecast_pipe_windows import PipeError

ENTITY = "a" * 64


class RecoveryPolicyTests(unittest.TestCase):
    def test_transient_hid_failures_are_recoverable(self):
        for reason in ("hid_host_detached", "hid_callback_failed", "hid_queue_overflow",
                       "hid_capture_failed", "hid_source_unconfirmed", "capture_lost",
                       "capture_failed", "radio_ambiguous"):
            with self.subTest(reason=reason):
                self.assertIn(reason, chromecast_hid_worker.RECOVERABLE_REASONS)
                self.assertIn(reason, DIAGNOSTIC_REASONS)

    def test_unrecoverable_reasons_still_stop_the_receiver(self):
        for reason in ("hid_symbol_unavailable", "hid_symbol_no_public_pdb", "hid_gatt_unavailable",
                       "source_unconfirmed", "permission_required", "unsupported_layout",
                       "peer_lost", "invalid_message"):
            with self.subTest(reason=reason):
                self.assertNotIn(reason, chromecast_hid_worker.RECOVERABLE_REASONS)
                self.assertIn(reason, DIAGNOSTIC_REASONS)

    def test_backoff_is_bounded(self):
        delays = [chromecast_hid_worker.retry_delay(n) for n in range(1, 7)]
        self.assertEqual(delays, [1.0, 2.0, 5.0, 10.0, 10.0, 10.0])


class ReasonReportingTests(unittest.TestCase):
    def test_retry_markers_pass_channel_validation(self):
        channel = Channel(SessionIdentity.create(ENTITY), commands=False)
        for phase in ("begin", "done"):
            with self.subTest(phase=phase):
                # encode() 内部会做完整校验；原因不在白名单时会直接抛异常。
                channel.encode("diagnostic", stage="device_open", phase=phase,
                               reason="hid_host_detached", **empty_stop_details())

    def test_tap_reasons_have_user_messages(self):
        for reason in ("hid_host_detached", "hid_callback_failed", "hid_queue_overflow"):
            with self.subTest(reason=reason):
                self.assertNotIn("未就绪", message(reason))


class SymbolBuildTableTests(unittest.TestCase):
    def test_tested_build_stays_in_the_table(self):
        self.assertEqual(_KNOWN_BUILD_RVAS.get(_TESTED_SHA256), _TESTED_RVA)
        self.assertEqual(SYMBOL_REASONS, {"hid_symbol_unavailable", "hid_symbol_no_public_pdb"})

    def test_symbol_probe_record_is_bounded(self):
        row = {"kind": "symbol_probe", "elapsed_ms": 12, "state": "no_public_pdb",
               "sha256": "a" * 64, "pdb_index": "573C2268A9AADAB3D3D62F2F5DDC43B61",
               "http_status": 404, "pdb_bytes": -1}
        self.assertTrue(valid_record(row))
        self.assertFalse(valid_record({**row, "http_status": 999}))
        self.assertFalse(valid_record({**row, "sha256": "zz"}))

    def test_symbol_reasons_have_user_messages(self):
        for reason in ("hid_symbol_unavailable", "hid_symbol_no_public_pdb", "hid_gatt_unavailable"):
            with self.subTest(reason=reason):
                self.assertTrue(message(reason).strip())
                self.assertNotEqual(message(reason), message("capture_failed"))


class GattReportDecodeTests(unittest.TestCase):
    def test_two_byte_consumer_usage_maps_to_key_code(self):
        # volume_up = 0x00E9 -> 报告码 0x0C
        self.assertEqual(decode_report(bytes([0xE9, 0x00])), bytes([0x0C]) + bytes(7))

    def test_two_byte_neutral_report_is_kept(self):
        self.assertEqual(decode_report(bytes(2)), bytes(8))

    def test_unknown_consumer_usage_is_dropped(self):
        self.assertIsNone(decode_report(bytes([0x34, 0x12])))

    def test_eight_byte_report_passes_through(self):
        self.assertEqual(decode_report(bytes([0x07]) + bytes(7)), bytes([0x07]) + bytes(7))

    def test_invalid_reports_are_dropped(self):
        self.assertIsNone(decode_report(bytes([0x07, 0x01]) + bytes(6)))
        self.assertIsNone(decode_report(bytes([0x20]) + bytes(7)))
        self.assertIsNone(decode_report(bytes(5)))

    def test_report_id_prefix_from_downstream_copy(self):
        self.assertEqual(decode_report(bytes([0x02, 0xE9, 0x00])), bytes([0x0C]) + bytes(7))


class HidSourceFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_symbol_failure_switches_to_gatt(self):
        source = chromecast_hid_source.HidSource(ENTITY, device=object())
        with mock.patch.object(chromecast_hid_source.HidTap, "start",
                               side_effect=TapError("hid_symbol_no_public_pdb")):
            with mock.patch.object(chromecast_hid_source.GattHidTap, "start",
                                   new=mock.AsyncMock()) as gatt_start:
                await source.start()
        self.assertTrue(source.fallback)
        gatt_start.assert_awaited_once()

    async def test_gatt_failure_is_terminal(self):
        source = chromecast_hid_source.HidSource(ENTITY, device=object())
        with mock.patch.object(chromecast_hid_source.HidTap, "start",
                               side_effect=TapError("hid_symbol_unavailable")):
            with mock.patch.object(chromecast_hid_source.GattHidTap, "start",
                                   new=mock.AsyncMock(side_effect=RuntimeError("cccd"))):
                with self.assertRaises(PipeError) as raised:
                    await source.start()
        self.assertEqual(str(raised.exception), "hid_gatt_unavailable")

    async def test_other_tap_failures_propagate(self):
        source = chromecast_hid_source.HidSource(ENTITY, device=object())
        with mock.patch.object(chromecast_hid_source.HidTap, "start",
                               side_effect=TapError("hid_source_unconfirmed")):
            with self.assertRaises(TapError):
                await source.start()
        self.assertFalse(source.fallback)


class DeviceCleanupToleranceTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_device_is_not_a_cleanup_failure(self):
        selected = SelectedDevice(ENTITY)
        selected.device = mock.Mock()  # 连接状态已不可读，等同于掉线
        selected._voice_tokens = [(mock.Mock(), 1)]
        selected._voice_subscribed = [mock.Mock()]
        await selected.close_voice()  # 不抛异常即通过

    def test_close_ignores_dead_watcher_and_device(self):
        selected = SelectedDevice(ENTITY)
        selected.watcher = mock.Mock()
        selected.watcher.stop.side_effect = RuntimeError("gone")
        selected.device = mock.Mock()
        selected.device.close.side_effect = RuntimeError("gone")
        selected.close()  # 不抛异常即通过


if __name__ == "__main__":
    unittest.main()
