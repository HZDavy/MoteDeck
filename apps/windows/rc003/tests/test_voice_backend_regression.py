"""Backend ownership survives editor round trips and transient reload failures."""
import unittest
from unittest import mock

from ovb_rc003 import config, key_mapping, remote_selection, settings_ui
from tests import test_app_wiring as wiring


class VoiceEditorBackendTests(unittest.TestCase):
    def test_hotkey_display_round_trip_keeps_driver_backend(self):
        action = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD, inject_mode="interception"
        )
        _, saved = settings_ui.build_save_model(
            button_display_map={"mic": "ralt"}, hotkey_text="ralt",
            endpoint_display_text="", base_config={"voice_hotkey": "ralt"},
            base_bindings={"schema_version": 1, "bindings": {"mic": action.to_dict()}},
            voice_slot_hotkeys={"1": "ralt"},
            button_inject_modes={"mic": "interception"},
        )
        self.assertEqual(
            key_mapping.ButtonAction.from_dict(saved["bindings"]["mic"]).inject_mode,
            "interception",
        )


class GoogleVoiceBackendReloadTests(wiring._AppWiringTestCase):
    def seed_backend(self, mode):
        config.save_config(self.app._config_path, self.app._config)
        bindings = config.default_key_bindings()
        bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.KEY_COMBO, keys=("ralt",), inject_mode=mode,
        ).to_dict()
        config.save_key_bindings(self.app._bindings_path, bindings)
        self.app._reload_settings_if_changed()
        self.app._remote_profile = remote_selection.GOOGLE_TV_PROFILE
        return bindings

    def test_failed_reload_retries_without_another_file_edit(self):
        bindings = self.seed_backend("sendinput")
        bindings["bindings"]["mic"]["inject_mode"] = "interception"
        config.save_key_bindings(self.app._bindings_path, bindings)
        with mock.patch.object(config, "load_key_bindings", side_effect=PermissionError()):
            self.app._reload_settings_if_changed()
        self.assertEqual(self.app._voice_slot_inject_mode(1), "sendinput")
        self.app._reload_settings_if_changed()
        self.assertEqual(self.app._voice_slot_inject_mode(1), "interception")

    def test_voice_start_repairs_stale_memory_with_unchanged_file_times(self):
        self.seed_backend("interception")
        self.app._bindings["bindings"]["mic"]["inject_mode"] = "sendinput"
        host = self.app._create_chromecast_voice_host()
        host.services.reload_settings()
        self.assertEqual(self.app._voice_slot_inject_mode(1), "interception")

    def test_missing_bindings_blocks_attempt_and_recovers_without_restart(self):
        self.seed_backend("interception")
        original = self.app._bindings_path.read_bytes()
        self.app._bindings_path.unlink()
        host = self.app._create_chromecast_voice_host()
        self.assertIs(host.services.reload_settings(), False)
        self.assertEqual(self.app._chromecast_voice_inject_mode(), "interception")
        self.app._bindings_path.write_bytes(original)
        self.assertIs(host.services.reload_settings(), True)
        self.assertEqual(self.app._chromecast_voice_inject_mode(), "interception")

    def test_explicit_sendinput_is_not_forced_to_interception(self):
        self.seed_backend("sendinput")
        self.assertTrue(self.app._refresh_chromecast_voice_settings())
        self.assertEqual(self.app._chromecast_voice_inject_mode(), "sendinput")

    def test_other_voice_button_cannot_take_gatt_mic_backend(self):
        self.seed_backend("interception")
        self.app._bindings["bindings"]["power"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD, inject_mode="sendinput"
        ).to_dict()
        self.assertEqual(self.app._chromecast_voice_inject_mode(), "interception")

    def test_active_application_override_survives_forced_refresh(self):
        self.seed_backend("interception")
        # Application presets currently support RC003 only. Exercise the
        # shared reload path without inventing unsupported Google TV presets.
        self.app._remote_profile = remote_selection.RC003_PROFILE
        self.app._preset_override_id = "test-app"
        override = {"profile": remote_selection.RC003_PROFILE,
                    "mapping": {"bindings": {"mic": key_mapping.ButtonAction(
                        key_mapping.ActionKind.KEY_COMBO, keys=("ralt",),
                        inject_mode="sendinput").to_dict()},
                        "secondary_bindings": {}, "display_notes": {}}}
        with mock.patch.object(self.app._preset_store, "get", return_value=override):
            self.assertTrue(self.app._reload_settings_if_changed(force=True))
        self.assertEqual(self.app._preset_override_id, "test-app")
        self.assertEqual(self.app._chromecast_voice_inject_mode(), "sendinput")
        self.assertEqual(self.app._preset_base_bindings["bindings"]["mic"]["inject_mode"], "interception")

    def test_config_changed_during_load_does_not_acknowledge_partial_snapshot(self):
        self.seed_backend("interception")
        before = self.app._bindings
        original_load = config.load_key_bindings
        def concurrent_save(*args, **kwargs):
            result = original_load(*args, **kwargs)
            self.app._config_path.write_text("{}", encoding="utf-8")
            return result
        with mock.patch.object(config, "load_key_bindings", side_effect=concurrent_save):
            self.assertFalse(self.app._refresh_chromecast_voice_settings())
        self.assertIs(self.app._bindings, before)

    def test_held_shortcut_is_not_reconfigured_by_force_refresh(self):
        self.seed_backend("interception")
        self.app._voice_shortcut.pending_tokens = ("ralt",)
        self.app._voice_shortcut.pending_inject_mode = "interception"
        self.assertFalse(self.app._refresh_chromecast_voice_settings())
        self.assertEqual(self.app._voice_shortcut.pending_tokens, ("ralt",))
        self.assertEqual(self.app._voice_shortcut.pending_inject_mode, "interception")
        self.app._voice_shortcut.pending_tokens = None

    def test_clearing_hotkey_does_not_keep_cached_right_alt(self):
        self.seed_backend("interception")
        updated = dict(self.app._config)
        updated["voice_key_configs"] = [{"id": 1, "name": "", "hotkey": "",
                                         "source": "remote", "device": ""}]
        updated["voice_hotkey"] = ""
        updated["voice_hotkeys"] = {"hold": ""}
        updated["voice_slot_hotkeys"] = {"1": ""}
        config.save_config(self.app._config_path, updated)
        self.assertTrue(self.app._refresh_chromecast_voice_settings())
        self.assertIsNone(self.app._voice_shortcut.hotkey)

    def test_same_device_profile_change_cannot_mix_runtime_and_disk(self):
        self.seed_backend("interception")
        original = {"schema": 1, "active": "a" * 64, "devices": [
            {"key": "a" * 64, "profile": remote_selection.GOOGLE_TV_PROFILE}]}
        self.app._config[remote_selection.KEY] = original
        incoming = dict(self.app._config)
        incoming[remote_selection.KEY] = {**original, "devices": [
            {"key": "a" * 64, "profile": remote_selection.CHROMECAST_PROFILE}]}
        config.save_config(self.app._config_path, incoming)
        self.assertFalse(self.app._refresh_chromecast_voice_settings())
        self.assertEqual(remote_selection.active_profile(self.app._config), remote_selection.GOOGLE_TV_PROFILE)
