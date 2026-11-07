"""Synthetic identities and replayed button payloads; no user configuration."""
import contextlib
from enum import IntFlag
from types import SimpleNamespace
import sys
import unittest
from unittest import mock

from ovb_rc003 import chromecast_hid_tap_windows as tap, remote_selection
from ovb_rc003.chromecast_buttons import HidButtonReceiver
from ovb_rc003.chromecast_device_windows import SelectedDevice
from ovb_rc003.chromecast_pipe_windows import PipeError

CONTAINER = "11111111-1111-4111-8111-111111111111"
ENTITY = remote_selection.container_key(CONTAINER)
ADDRESS = "aabbccddeeff"


def service(hardware=tap._GOOGLE_HARDWARE_VARIANT, address=ADDRESS):
    return tap._HID_SERVICE + "_" + hardware + "_" + address


@contextlib.contextmanager
def registry(rows):
    root = r"SYSTEM\CurrentControlSet\Enum\BTHLEDevice"
    data = {root: {"children": []}}
    for name, container, pid in rows:
        data[root]["children"].append(name)
        path = root + "\\" + name
        data[path] = {"children": ["instance"]}
        data[path + r"\instance"] = {"ContainerID": container}
        data[path + r"\instance\Device Parameters\WUDFDiagnosticInfo"] = {"HostPid": pid}

    @contextlib.contextmanager
    def open_key(parent, child):
        path = child if parent == "HKLM" else parent + "\\" + child
        if path not in data:
            raise OSError("missing fixture key")
        yield path

    fake = SimpleNamespace(HKEY_LOCAL_MACHINE="HKLM", OpenKey=open_key,
        QueryInfoKey=lambda key: (len(data[key].get("children", [])), 0, 0),
        EnumKey=lambda key, index: data[key]["children"][index],
        QueryValueEx=lambda key, name: (data[key][name], 0))
    with mock.patch.dict(sys.modules, {"winreg": fake}):
        yield


class SourceTests(unittest.TestCase):
    def test_exact_hardware_and_case(self):
        for hw in (tap._GOOGLE_HARDWARE, tap._GOOGLE_HARDWARE_VARIANT):
            self.assertTrue(tap._supported_hid_service(service(hw).upper()))
        valid = service()
        for bad in (valid.replace("9450", "9451"), valid.replace("011b", "011c"),
                    valid.replace("18d1", "2717"), valid.replace("00001812", "00001813"),
                    valid + "junk", valid[:-1]):
            self.assertFalse(tap._supported_hid_service(bad), bad)

    def test_selected_original_and_variant(self):
        for hw, variant in ((tap._GOOGLE_HARDWARE, False), (tap._GOOGLE_HARDWARE_VARIANT, True)):
            with registry([(service(hw), CONTAINER, 42)]):
                self.assertEqual(tap._selected_source(ENTITY), (42, ADDRESS, variant))
                self.assertEqual(tap.selected_host_and_address(ENTITY), (42, ADDRESS))

    def test_unselected_missing_invalid_and_ambiguous_rejected(self):
        cases = ([], [(service(), "22222222-2222-4222-8222-222222222222", 42)],
                 [(service(), CONTAINER, 0)],
                 [(service(), CONTAINER, 42), (service(address="112233445566"), CONTAINER, 43)])
        for rows in cases:
            with self.subTest(rows=rows), registry(rows), self.assertRaises(tap.TapError):
                tap._selected_source(ENTITY)
        with registry([]), self.assertRaises(tap.TapError):
            tap._selected_source("invalid")


class CapturedReplayTests(unittest.TestCase):
    def test_physical_capture_press_and_release(self):
        stream = "4200 0000 4300 0000 4400 0000 4500 0000 4100 0000 2402 0000 2302 0000 e900 0000 ea00 0000 e200 0000 7700 0000 7800 0000 9e01 0000 8901 0000"
        expected = "up down left right ok back home volume_up volume_down volume_mute youtube netflix power input_source".split()
        receiver = HidButtonReceiver(ENTITY, "test")
        receiver.confirm_source()
        events = []
        for index, raw in enumerate(stream.split()):
            events.extend(receiver.feed(tap.normalize_report(bytes.fromhex(raw), True), float(index)))
        self.assertEqual([(e.button, e.action) for e in events],
                         [(button, action) for button in expected for action in ("down", "up")])
        self.assertFalse(receiver.stopped)

    def test_formats_are_bound_to_selected_revision(self):
        self.assertIsNone(tap.normalize_report(bytes.fromhex("4200"), False))
        original = bytes.fromhex("0300000000000000")
        self.assertIsNone(tap.normalize_report(original, True))
        self.assertEqual(tap.normalize_report(original), original)
        for bad in (None, "4200", b"", bytes.fromhex("ffff"), bytes.fromhex("420000")):
            self.assertIsNone(tap.normalize_report(bad, True))
        self.assertIsNone(tap.normalize_report(bytes.fromhex("0301000000000000")))


class VoiceLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def check_layout(self, handles, variant, direct, accepted):
        class Properties(IntFlag):
            WRITE = 8
            WRITE_WITHOUT_RESPONSE = 4
            NOTIFY = 16
            INDICATE = 32
        characteristics = []
        for part, handle in zip((2, 3, 4), handles):
            characteristics.append(SimpleNamespace(
                uuid=f"ab5e000{part}-5a21-4f05-bc7d-af01f617b664", attribute_handle=handle,
                characteristic_properties=Properties.WRITE if part == 2 else Properties.NOTIFY,
                add_value_changed=mock.Mock(return_value=1),
                write_client_characteristic_configuration_descriptor_async=mock.AsyncMock(return_value=0)))
        voice_service = SimpleNamespace(session=SimpleNamespace(max_pdu_size=247),
            get_characteristics_with_cache_mode_async=mock.AsyncMock(return_value=SimpleNamespace(status=0, characteristics=characteristics)))
        device = SelectedDevice(ENTITY)
        device.device = SimpleNamespace(get_gatt_services_for_uuid_with_cache_mode_async=mock.AsyncMock(
            return_value=SimpleNamespace(status=0, services=[voice_service])))
        modules = {
            "winrt.windows.devices.bluetooth": SimpleNamespace(BluetoothCacheMode=SimpleNamespace(UNCACHED=0)),
            "winrt.windows.devices.bluetooth.genericattributeprofile": SimpleNamespace(
                GattCharacteristicProperties=Properties,
                GattWriteOption=SimpleNamespace(WRITE_WITH_RESPONSE=0, WRITE_WITHOUT_RESPONSE=1),
                GattClientCharacteristicConfigurationDescriptorValue=SimpleNamespace(NOTIFY=1, INDICATE=2))}
        with mock.patch.dict(sys.modules, modules), mock.patch.object(tap, "_selected_source", return_value=(42, ADDRESS, variant)):
            if accepted:
                await device.open_voice(direct=direct)
                self.assertIs(device.voice_tx, characteristics[0])
                if direct:
                    for characteristic in characteristics[1:]:
                        characteristic.add_value_changed.assert_called_once()
                        characteristic.write_client_characteristic_configuration_descriptor_async.assert_awaited_once()
            else:
                with self.assertRaises(PipeError):
                    await device.open_voice(direct=direct)
                for characteristic in characteristics:
                    characteristic.add_value_changed.assert_not_called()

    async def test_original_layout_preserved(self):
        await self.check_layout((0x39, 0x3B, 0x3E), False, True, True)

    async def test_observed_variant_direct_callbacks(self):
        await self.check_layout((0x51, 0x53, 0x56), True, True, True)

    async def test_variant_requires_direct_selected_identity_and_exact_layout(self):
        for handles, variant, direct in (((0x51, 0x53, 0x56), False, True),
                                        ((0x51, 0x53, 0x56), True, False),
                                        ((0x51, 0x53, 0x57), True, True)):
            await self.check_layout(handles, variant, direct, False)


if __name__ == "__main__":
    unittest.main()
