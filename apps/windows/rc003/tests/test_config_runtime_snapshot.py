"""Strict runtime loading and single-snapshot config selection."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import config, key_mapping


class ConfigRuntimeSnapshotTests(unittest.TestCase):
    def test_required_config_propagates_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with self.assertRaises(FileNotFoundError):
                config.load_config(path, required=True)

    def test_required_key_bindings_propagates_missing_config_and_bindings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bindings_path = root / "key_bindings.json"
            with self.assertRaises(FileNotFoundError):
                config.load_key_bindings(bindings_path, required=True)

            (root / "config.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                config.load_key_bindings(bindings_path, required=True)

    def test_provided_selection_is_used_without_rereading_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = config.default_config()
            with mock.patch.object(
                config, "_read_settings_document", side_effect=AssertionError("reread")
            ) as read_document:
                loaded = config.load_key_bindings(
                    root / "key_bindings.json", selected=selected
                )
            read_document.assert_not_called()
            self.assertEqual(loaded["bindings"], config.default_key_bindings()["bindings"])

    def test_legacy_voice_to_hold_migration_keeps_injection_mode(self):
        bindings = {"bindings": {"mic": {
            "kind": key_mapping.ActionKind.VOICE.value,
            "inject_mode": key_mapping.INJECT_MODE_INTERCEPTION,
        }}}

        config.normalize_voice_product_boundary({"voice_mode": "hold"}, bindings)

        migrated = key_mapping.ButtonAction.from_dict(bindings["bindings"]["mic"])
        self.assertEqual(migrated.kind, key_mapping.ActionKind.VOICE_HOLD)
        self.assertEqual(migrated.inject_mode, key_mapping.INJECT_MODE_INTERCEPTION)

    def test_default_missing_file_behavior_still_uses_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(config.load_config(root / "config.json"), config.default_config())
            self.assertEqual(
                config.load_key_bindings(root / "key_bindings.json"),
                config.default_key_bindings(),
            )


if __name__ == "__main__":
    unittest.main()
