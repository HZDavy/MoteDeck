"""Entity switches use temporary files only; no hardware or real config."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import config, remote_selection as selection, remote_settings as records

A, B = "a" * 64, "b" * 64


def selected(active=A, second=selection.RC003_PROFILE):
    return {"schema": 1, "active": active, "devices": [
        {"key": A, "profile": selection.RC003_PROFILE},
        {"key": B, "profile": second}]}


class EntitySettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = self.root / config.CONFIG_FILENAME
        self.bindings = self.root / config.KEY_BINDINGS_FILENAME
        data = config.default_config()
        data[selection.KEY] = selected()
        data["gain_db"] = 7
        config.save_config(self.settings, data)
        mapping = config.default_key_bindings()
        mapping["display_notes"] = {"ok": {"single_click": "原设备"}}
        config.save_key_bindings(self.bindings, mapping)

    def switch(self, active, second=selection.RC003_PROFILE):
        return config.switch_remote_settings(self.settings, self.bindings, selected(active, second))

    def test_round_trip_restores_each_entity_and_hold_is_default(self):
        for second in (selection.RC003_PROFILE,):
            with self.subTest(second=second):
                new, mapping = self.switch(B, second)
                self.assertEqual(new["remote_recording_limit_seconds"], 120)
                self.assertNotIn("remote_recording_mode", new)
                self.assertNotIn("ok", mapping["display_notes"])
                new["gain_db"] = 3
                new["remote_recording_limit_seconds"] = 300
                mapping["display_notes"] = {"ok": {"single_click": "新设备"}}
                config.save_settings_pair(self.settings, new, self.bindings, mapping)
                old, old_mapping = self.switch(A, second)
                self.assertEqual(old["gain_db"], 7)
                self.assertEqual(old["remote_recording_limit_seconds"], 120)
                self.assertEqual(old_mapping["display_notes"]["ok"]["single_click"], "原设备")
                new, mapping = self.switch(B, second)
                self.assertEqual(new["gain_db"], 3)
                self.assertEqual(new["remote_recording_limit_seconds"], 300)
                self.switch(A, second)
                # Begin the next independent profile case with no B record.
                for filename in (self.settings, self.bindings):
                    document = json.loads(filename.read_text(encoding="utf-8"))
                    document[records.STORE]["records"].pop(B)
                    config._save_json_atomic(filename, document)

    def test_disk_has_one_active_pointer_and_no_flat_per_entity_duplicates(self):
        self.switch(B)
        for filename, fields in ((self.settings, records.CONFIG_FIELDS), (self.bindings, records.BINDING_FIELDS)):
            stored = json.loads(filename.read_text(encoding="utf-8"))
            self.assertFalse(fields & stored.keys())
            self.assertNotIn(records.OWNER, stored)
            self.assertEqual(set(stored[records.STORE]["records"]), {A, B})
        stored = json.loads(self.bindings.read_text(encoding="utf-8"))
        self.assertNotIn(selection.KEY, stored)
        self.assertNotIn("selected_device_profile", json.loads(self.settings.read_text(encoding="utf-8")))

    def test_unassigned_legacy_data_is_preserved_not_given_to_new_entity(self):
        data = config.load_config(self.settings)
        data[selection.KEY] = selected("")
        config.save_config(self.settings, data)
        new, mapping = self.switch(B)
        self.assertEqual(new["gain_db"], config.default_config()["gain_db"])
        self.assertNotIn("ok", mapping["display_notes"])
        self.assertEqual(new[records.STORE]["unassigned"]["gain_db"], 7)
        self.assertEqual(mapping[records.STORE]["unassigned"]["display_notes"]["ok"]["single_click"], "原设备")

    def test_first_explicit_xiaomi_choice_retains_legacy_without_an_extra_switch(self):
        data = config.load_config(self.settings)
        data[selection.KEY] = selected("")
        config.save_config(self.settings, data)
        sole = {"schema": 1, "active": A, "devices": [{"key": A, "profile": selection.RC003_PROFILE}]}
        restored, mappings = config.switch_remote_settings(self.settings, self.bindings, sole)
        self.assertEqual(restored["gain_db"], 7)
        self.assertEqual(mappings["display_notes"]["ok"]["single_click"], "原设备")
        self.assertEqual(restored[records.STORE]["unassigned"], {})

    def test_load_is_read_only(self):
        for migrated in (False, True):
            if migrated:
                self.switch(B)
            snapshots = [path.read_bytes() for path in (self.settings, self.bindings)]
            config.load_config(self.settings)
            config.load_key_bindings(self.bindings)
            self.assertEqual(snapshots, [path.read_bytes() for path in (self.settings, self.bindings)])

    def test_direct_choice_does_not_assign_legacy_from_only_one_registered_row(self):
        data = config.load_config(self.settings)
        data[selection.KEY] = selection.empty_selection()
        config.save_config(self.settings, data)
        paired = selected(second=selection.RC003_PROFILE)["devices"]
        choice, legacy = selection.select_paired_device(selection.empty_selection(), A, paired)
        restored, mappings = config.switch_remote_settings(
            self.settings, self.bindings, choice, allow_legacy_binding=legacy)
        self.assertEqual(restored["gain_db"], config.default_config()["gain_db"])
        self.assertEqual(restored[records.STORE]["unassigned"]["gain_db"], 7)
        self.assertEqual(mappings[records.STORE]["unassigned"]["display_notes"]["ok"]["single_click"], "原设备")

    def test_legacy_guard_does_not_discard_existing_entity_settings(self):
        self.switch(B)
        restored, mappings = config.switch_remote_settings(
            self.settings, self.bindings, selected(A), allow_legacy_binding=False)
        self.assertEqual(restored["gain_db"], 7)
        self.assertEqual(mappings["display_notes"]["ok"]["single_click"], "原设备")

    def test_stale_view_cannot_write_after_switch(self):
        for migrated in (False, True):
            if migrated:
                self.switch(A)
            old, mapping = config.load_config(self.settings), config.load_key_bindings(self.bindings)
            self.switch(B if selection.active_key(old) == A else A)
            before = [p.read_bytes() for p in (self.settings, self.bindings)]
            with self.assertRaises(records.RemoteSettingsError):
                config.save_config(self.settings, old)
            with self.assertRaises(records.RemoteSettingsError):
                config.save_key_bindings(self.bindings, mapping)
            self.assertEqual(before, [p.read_bytes() for p in (self.settings, self.bindings)])

    def test_latest_other_entity_record_survives_save(self):
        view, _ = self.switch(B)
        raw = json.loads(self.settings.read_text(encoding="utf-8"))
        raw[records.STORE]["records"][A]["gain_db"] = 12
        config._save_json_atomic(self.settings, raw)
        view["gain_db"] = 4
        config.save_config(self.settings, view)
        old, _ = self.switch(A)
        self.assertEqual(old["gain_db"], 12)

    def test_failed_second_write_restores_both_exactly(self):
        before = [p.read_bytes() for p in (self.settings, self.bindings)]
        original = config._save_json_atomic
        def write(path, document):
            if path == self.settings:
                raise OSError("disk locked")
            original(path, document)
        with mock.patch.object(config, "_save_json_atomic", side_effect=write), self.assertRaises(OSError):
            self.switch(B)
        self.assertEqual(before, [p.read_bytes() for p in (self.settings, self.bindings)])

    def test_mapping_publish_before_pointer_keeps_old_identity_valid(self):
        original = config._save_json_atomic
        observed = []
        def write(path, document):
            original(path, document)
            if path == self.bindings:
                observed.append((selection.active_key(config.load_config(self.settings)),
                                 config.load_key_bindings(self.bindings)["display_notes"]))
        with mock.patch.object(config, "_save_json_atomic", side_effect=write):
            self.switch(B)
        self.assertEqual(observed, [(A, {"ok": {"single_click": "原设备"}})])

    def test_failed_restore_is_reported_as_uncertain(self):
        with mock.patch.object(config, "_save_json_atomic", side_effect=OSError("failed")), \
             mock.patch.object(config, "_restore_file_snapshot", side_effect=OSError("failed")), \
             self.assertRaises(config.ConfigTransactionError):
            self.switch(B)

    def test_malformed_record_and_unsupported_limit_fail_closed(self):
        self.switch(B)
        stored = json.loads(self.settings.read_text(encoding="utf-8"))
        for bad in ({"address": "raw"}, {"remote_recording_limit_seconds": 999}):
            stored[records.STORE]["records"][B] = bad
            config._save_json_atomic(self.settings, stored)
            with self.assertRaises((ValueError, config.ConfigPrivacyError)):
                config.load_config(self.settings)

    def test_removed_entity_records_survive_readding(self):
        self.switch(B)
        removed = selection.remove_device(selected(B), B)
        config.switch_remote_settings(self.settings, self.bindings, removed)
        self.assertEqual(selection.active_key(config.load_config(self.settings)), "")
        self.assertIn(B, config.load_key_bindings(self.bindings)[records.STORE]["records"])
        self.switch(B)

    def test_retired_record_fields_stay_readable_and_are_cleared_on_rewrite(self):
        # A file written before schema 18 keeps the dropped provider/trigger
        # fields inside its records. It must still open with its live settings
        # intact, and the next save must physically clear them.
        self.switch(B)
        retired = {
            "voice_shortcut_enabled": True, "voice_trigger_mode": "hold",
            "voice_program": "legacy-ime", "voice_hotkeys_by_provider": {"legacy-ime": "win+h"},
            "remote_recording_mode": "hold",
        }
        self.assertEqual(set(retired), set(records.CONFIG_RETIRED_FIELDS))
        stored = json.loads(self.settings.read_text(encoding="utf-8"))
        stored[records.STORE]["records"][A].update(retired)
        stored[records.STORE]["records"][B].update(retired, gain_db=4)
        config._save_json_atomic(self.settings, stored)

        loaded = config.load_config(self.settings)
        self.assertEqual(loaded["gain_db"], 4)
        for field in records.CONFIG_RETIRED_FIELDS:
            self.assertNotIn(field, loaded)

        config.save_config(self.settings, loaded)
        rewritten = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertFalse(records.CONFIG_RETIRED_FIELDS & rewritten.keys())
        for row in rewritten[records.STORE]["records"].values():
            self.assertFalse(records.CONFIG_RETIRED_FIELDS & row.keys())
        self.assertEqual(rewritten[records.STORE]["records"][A]["gain_db"], 7)

    def test_record_toggle_shortcut_is_not_reinterpreted_as_hold(self):
        # The pre-schema-18 toggle selector only ever lived inside the record.
        # It stays readable so the old shortcut is never mistaken for a
        # hold-to-talk key, then is dropped like the other retired fields.
        self.switch(B)
        self.switch(A)
        stored = json.loads(self.settings.read_text(encoding="utf-8"))
        stored[records.STORE]["records"][A].update({
            "voice_trigger_mode": "toggle", "voice_hotkey": "win+h",
            "voice_hotkeys": {"hold": ""},
        })
        config._save_json_atomic(self.settings, stored)

        loaded = config.load_config(self.settings)
        self.assertEqual(loaded["voice_hotkey"], "ralt")
        self.assertEqual(loaded[config.RUNTIME_LEGACY_VOICE_MODE_KEY], "toggle")
        self.assertNotIn("voice_trigger_mode", loaded)


class DiscoveryTests(unittest.TestCase):
    def test_model_and_entity_are_distinct_and_short_labels_resolve_collision(self):
        first, second = "abcdef01" + "1" * 56, "abcdef02" + "2" * 56
        self.assertNotEqual(selection.label(first, selection.RC003_PROFILE, [first, second]),
                            selection.label(second, selection.RC003_PROFILE, [first, second]))
        self.assertTrue(selection.runtime_ready(selection.RC003_PROFILE))

    def test_invalid_later_scan_row_raises_contract_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.json"
            path.write_text(json.dumps([{"key": A, "profile": selection.RC003_PROFILE, "label": "x"}, {}]), encoding="utf-8")
            with self.assertRaises(selection.SelectionError):
                selection._read_scan_result(str(path), 0)
