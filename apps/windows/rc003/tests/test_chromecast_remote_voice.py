"""Remote voice subscriptions and paired host-key/audio lifetime."""
import asyncio
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

from ovb_rc003.chromecast_device_windows import SelectedDevice
from ovb_rc003.chromecast_voice import Recording, supported_capabilities
from ovb_rc003.chromecast_voice_host import VoiceHost, VoiceHostServices
from ovb_rc003.voice_controller import VoiceController, VoiceHostAction


class DirectSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_variant_handles_subscribe_by_uuid_and_keep_channel_identity(self):
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattCharacteristicProperties as Props,
        )
        class Characteristic:
            def __init__(self, part, handle, props):
                self.uuid = f"ab5e000{part}-5a21-4f05-bc7d-af01f617b664"
                self.attribute_handle = handle
                self.characteristic_properties = props
                self.modes = []
            def add_value_changed(self, callback):
                self.callback = callback
                return 1
            def remove_value_changed(self, token):
                self.callback = None
            async def write_client_characteristic_configuration_descriptor_async(self, mode):
                self.modes.append(mode)
                return 0
        tx = Characteristic(2, 51, Props.WRITE_WITHOUT_RESPONSE)
        audio = Characteristic(3, 54, Props.NOTIFY)
        control = Characteristic(4, 57, Props.NOTIFY)
        class Service:
            session = NS(max_pdu_size=512)
            async def get_characteristics_with_cache_mode_async(self, mode):
                return NS(status=0, characteristics=[tx, audio, control])
        class Device:
            async def get_gatt_services_for_uuid_with_cache_mode_async(self, uuid, mode):
                return NS(status=0, services=[Service()])
        selected = SelectedDevice("a" * 64)
        selected.device = Device()
        await selected.open_voice(direct=True)
        self.assertEqual(selected.voice_attributes, {54: "audio", 57: "control"})
        audio.callback(None, NS(characteristic_value=b"audio"))
        control.callback(None, NS(characteristic_value=b"\x04\x03\x02\x01"))
        received = selected.poll_voice()
        self.assertEqual([(a, b) for a, b, _ in received],
                         [(54, b"audio"), (57, b"\x04\x03\x02\x01")])
        await selected.close_voice()
        self.assertIsNone(audio.callback)
        self.assertEqual(len(audio.modes), 2)


class PairedVoiceTests(unittest.TestCase):
    def setUp(self):
        self.order = []
        self.shortcut = NS(lock=threading.Lock(), controller=VoiceController(),
                           pending_tokens=None, recorded_tokens=lambda: ("ralt",),
                           release_pending=lambda: True)
        def apply(action, **kwargs):
            self.order.append((action, kwargs.get("inject_mode")))
            return True
        self.shortcut.apply = apply
        self.audio = Mock()
        self.audio.open.side_effect = lambda: self.order.append("audio_open") or True
        self.audio.writer.submit.side_effect = lambda samples: self.order.append("pcm") or True
        self.audio.flush.return_value = NS(error=None, completed=True)
        self.audio.stats.frames = 1
        self.services = VoiceHostServices(
            audio=self.audio, shortcut=self.shortcut, settings=lambda: {},
            logger=Mock(), reload_settings=Mock(), apply_pending_settings=Mock(),
            begin_diagnostic=Mock(), finish_diagnostic=Mock(), set_active=Mock(),
            set_result=Mock(), disable_forwarding=Mock(),
            voice_inject_mode=lambda: "sendinput")
        self.host = VoiceHost(self.services)
    def event(self, kind, data=""):
        self.host._handle(dict(event=kind, attempt=1, data=data))
    def test_remote_start_audio_stop_has_one_key_pair(self):
        self.event("host_start")
        self.event("host_start")  # Duplicate does not send another key down.
        self.event("control", "04030201")
        for _ in range(30):
            self.event("audio", bytes(128).hex())
        self.event("host_stop")
        self.assertEqual(self.order[0], "audio_open")
        self.assertEqual(self.order[1], (VoiceHostAction.KEY_DOWN, "sendinput"))
        self.assertIn("pcm", self.order)
        self.assertEqual(self.order[-1][0], VoiceHostAction.KEY_UP)
        self.assertFalse(self.shortcut.controller.active)
        self.assertTrue(self.host.closed)
    def test_failed_audio_endpoint_does_not_hold_right_alt(self):
        self.audio.open.side_effect = lambda: False
        self.event("host_start")
        self.assertEqual(self.order, [])
        self.assertFalse(self.shortcut.controller.active)
    def test_detection_event_cannot_start_key_without_audio(self):
        self.event("mic", "pressed")
        self.assertEqual(self.order, [])
    def test_shutdown_releases_held_key(self):
        self.event("host_start")
        self.host._close_resources()
        self.assertEqual(self.order[-1][0], VoiceHostAction.KEY_UP)
        self.assertFalse(self.shortcut.controller.active)


class GoogleTvFirmwareVoiceTests(unittest.TestCase):
    CAPABILITIES = bytes((0x0B, 0x01, 0x00, 0x01, 0x03, 0x00, 0x86, 0x00, 0x00))

    def test_observed_8k_capabilities_are_supported(self):
        self.assertTrue(supported_capabilities(self.CAPABILITIES))

    def test_observed_codec_one_start_opens_hold_session(self):
        gate = Recording(mode="hold")
        effects = gate.control(bytes((0x04, 0x03, 0x01, 0x1F)), 1.0)
        self.assertEqual(gate.state, "starting")
        self.assertEqual(gate.stream, (0x1F, 0x03))
        self.assertIn("host_start", [effect.kind for effect in effects])


if __name__ == "__main__":
    unittest.main()
