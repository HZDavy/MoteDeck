import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import (app_profiles, config, device_catalog,
                       mapping_presets as presets, key_mapping)
from tests import test_qt_settings_app as qt_tests


class PresetStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "data"
        self.store = presets.PresetStore(self.root)
        self.profile = device_catalog.RC003_ID
        self.bindings = config.default_key_bindings(self.profile)
        self.bindings["physical_bindings"] = {"power": {"vk": 123}}
        self.mapping = presets.snapshot(self.bindings, self.profile)

    def create(self, name="工作"):
        return self.store.create(name, self.profile, self.mapping)

    def test_full_roundtrip_is_independent_and_contains_no_identity(self):
        identity = self.create()
        before = copy.deepcopy(self.bindings)
        export = Path(self.tmp.name) / "backup.json"
        self.store.export_file(identity, export)
        value = json.loads(export.read_text(encoding="utf-8"))
        self.assertEqual(set(value["mapping"]), set(presets.MAPPING_FIELDS))
        self.assertNotIn("id", value)
        imported = self.store.import_file(export, self.profile)
        self.assertNotEqual(imported, identity)
        self.assertEqual(self.store.get(imported)["name"], "工作 (2)")
        self.assertEqual(self.bindings, before)
        applied = presets.apply_to(before, self.store.get(imported), self.profile)
        self.assertEqual(applied["physical_bindings"], before["physical_bindings"])
        self.assertEqual(applied["bindings"], before["bindings"])

    def test_rename_uses_id_and_delete_all_does_not_write_active_files(self):
        first, second = self.create("A"), self.create("B")
        self.store.rename(first, "A改名")
        self.assertEqual(self.store.get(second)["name"], "B")
        for identity in (first, second):
            self.store.delete(identity)
        self.assertEqual(self.store.load(), [])
        self.assertFalse(config.config_path(self.root).exists())
        self.assertFalse(config.key_bindings_path(self.root).exists())

    def test_duplicate_and_stale_ids_do_not_modify_library(self):
        identity = self.create()
        before = self.store.path.read_bytes()
        for operation in (lambda: self.create(), lambda: self.store.rename("missing", "x"),
                          lambda: self.store.delete("missing")):
            with self.assertRaises(ValueError):
                operation()
            self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.get(identity)["name"], "工作")

    def test_atomic_failure_preserves_previous_bytes_and_removes_temp_file(self):
        identity = self.create()
        before = self.store.path.read_bytes()
        with mock.patch.object(config.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.store.rename(identity, "新名称")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.store.path])

    def test_corrupted_library_is_not_silently_replaced(self):
        self.root.mkdir()
        self.store.path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(self.store.path.read_text(), "{bad")

    def test_export_cannot_overwrite_active_data_or_library(self):
        identity = self.create()
        before = self.store.path.read_bytes()
        for name in (presets.FILENAME, "config.json", "key_bindings.json"):
            with self.assertRaises(ValueError):
                self.store.export_file(identity, self.root / name)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_import_validation_rejects_wrong_profile_unknown_version_and_settings(self):
        identity = self.create()
        export = Path(self.tmp.name) / "backup.json"
        self.store.export_file(identity, export)
        original = json.loads(export.read_text())
        invalid = []
        for key, value in (("schema_version", 2), ("schema_version", True), ("profile", "other"),
                           ("voice_hotkey", "f2")):
            document = copy.deepcopy(original)
            document[key] = value
            invalid.append(document)
        document = copy.deepcopy(original)
        document["mapping"]["physical_bindings"] = {}
        invalid.append(document)
        for document in invalid:
            export.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ValueError):
                self.store.import_file(export, self.profile)
        self.assertEqual(len(self.store.load()), 1)

    def test_all_four_holds_roundtrip_only_in_long_slot(self):
        for kind in key_mapping.MOUSE_HOLD_BUTTONS:
            value = copy.deepcopy(self.mapping)
            value["secondary_bindings"] = {"ok": {"long_press": {"kind": kind.value}}}
            presets.validate_mapping(value, self.profile)
            value["secondary_bindings"]["ok"] = {"double_click": {"kind": kind.value}}
            with self.assertRaises(ValueError):
                presets.validate_mapping(value, self.profile)
            value["secondary_bindings"] = {}
            value["bindings"]["ok"] = {"kind": kind.value}
            with self.assertRaises(ValueError):
                presets.validate_mapping(value, self.profile)

    def test_unsafe_keys_and_unknown_buttons_are_rejected(self):
        for raw in ({"kind": "key_combo", "keys": ["unknown-key"]},
                    {"kind": "unknown"}):
            value = copy.deepcopy(self.mapping)
            value["bindings"]["ok"] = raw
            with self.assertRaises(ValueError):
                presets.validate_mapping(value, self.profile)
        value = copy.deepcopy(self.mapping)
        value["secondary_bindings"]["mic"] = {"long_press": {"kind": "unknown"}}
        with self.assertRaises(ValueError):
            presets.validate_mapping(value, self.profile)

    def test_supported_profiles_and_device_compatibility(self):
        for profile in presets.PROFILES:
            mapping = presets.snapshot(config.default_key_bindings(profile), profile)
            identity = self.store.create("默认", profile, mapping)
            presets.apply_to(config.default_key_bindings(profile), self.store.get(identity), profile)
        other = dict(self.store.load()[0], profile="other")
        with self.assertRaises(ValueError):
            presets.apply_to(self.bindings, other, self.profile)

    def test_automatic_row_tracks_current_settings_without_creating_an_extra_mapping_file(self):
        row = self.store.view(self.bindings, self.profile)[0]
        self.assertEqual(row["name"], "26-1012 自动保存")
        self.assertTrue(row["automatic"] and row["applied"])
        self.bindings["bindings"]["ok"] = {"kind": "escape", "keys": []}
        latest = presets.PresetStore(self.root).view(self.bindings, self.profile)[0]
        self.assertEqual(latest["name"], row["name"])
        self.assertEqual(latest["mapping"]["bindings"]["ok"]["kind"], "escape")
        self.assertFalse(self.store.path.exists())
        self.assertFalse(config.key_bindings_path(self.root).exists())

    def test_applied_marker_survives_reopen_but_never_follows_changed_values(self):
        first, second = self.create("A"), self.create("B")
        def applied(bindings=None):
            return [row["id"] for row in presets.PresetStore(self.root).view(
                self.bindings if bindings is None else bindings, self.profile) if row["applied"]]
        self.assertEqual(applied(), [presets.AUTO_ID])
        self.store.mark_applied(self.profile, second)
        self.store.rename(second, "B改名")
        self.assertEqual(applied(), [second])
        changed = copy.deepcopy(self.bindings)
        changed["bindings"]["ok"] = {"kind": "escape", "keys": []}
        self.assertEqual(applied(changed), [presets.AUTO_ID])
        self.store.delete(second)
        self.assertEqual(applied(), [presets.AUTO_ID])
        self.assertEqual(self.store.get(first)["name"], "A")

    def test_v1_upgrade_preserves_snapshots_and_failed_upgrade_keeps_original_file(self):
        identity = self.create()
        entry = self.store.load()[0]
        old_entry = {key: entry[key] for key in ("id", "name", "profile", "mapping")}
        old = {"schema_version": 1, "presets": [old_entry]}
        self.store.path.write_text(json.dumps(old), encoding="utf-8")
        before = self.store.path.read_bytes()
        loaded = self.store.load()
        self.assertEqual([{key: row[key] for key in ("id", "name", "profile", "mapping")} for row in loaded],
                         [old_entry])
        self.assertEqual((loaded[0]["scope"], loaded[0]["process"], loaded[0]["hotkey"]), ("global", "", ""))
        rows = self.store.view(self.bindings, self.profile)
        self.assertEqual(rows[1]["id"], identity)
        self.assertEqual(self.store.path.read_bytes(), before)
        with mock.patch.object(config.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.store.mark_applied(self.profile, identity)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.store.mark_applied(self.profile, identity)
        saved = json.loads(self.store.path.read_text())
        self.assertEqual([{key: row[key] for key in ("id", "name", "profile", "mapping")}
                          for row in saved["presets"]], [old_entry])
        self.assertEqual(saved["schema_version"], 3)

    def test_automatic_export_roundtrip_and_reserved_name(self):
        row = self.store.view(self.bindings, self.profile)[0]
        export = Path(self.tmp.name) / "automatic.json"
        self.store.export_entry(row, export)
        imported = self.store.import_file(export, self.profile)
        self.assertEqual(self.store.get(imported)["mapping"], row["mapping"])
        self.assertEqual(self.store.get(imported)["name"], row["name"] + " (2)")
        with self.assertRaises(ValueError):
            self.create(row["name"])
        with self.assertRaises(ValueError):
            self.store.rename(imported, row["name"])

    def test_invalid_current_mapping_does_not_hide_valid_presets_or_rewrite_data(self):
        identity = self.create()
        self.store.mark_applied(self.profile, identity)
        before = self.store.path.read_bytes()
        for raw in ({"kind": "voice_toggle", "keys": []}, {"kind": "unknown"}, {"keys": []}, None):
            with self.subTest(raw=raw):
                damaged = copy.deepcopy(self.bindings)
                damaged["bindings"]["mic"] = raw
                rows = self.store.view(damaged, self.profile)
                self.assertEqual([row["id"] for row in rows], [presets.AUTO_ID, identity])
                self.assertTrue(rows[0]["automatic"] and rows[0]["applied"])
                self.assertIsNone(rows[0]["mapping"])
                self.assertEqual(rows[1]["mapping"], self.mapping)
                with self.assertRaises(ValueError):
                    self.store.export_entry(rows[0], Path(self.tmp.name) / "invalid.json")
                self.assertEqual(self.store.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.store.view(self.bindings, "unsupported")

    def test_status_roundtrip_and_resolvers(self):
        identity = self.store.create("浏览器", self.profile, self.mapping,
                                     scope="app", process="Chrome.exe", hotkey="ctrl+alt+1")
        entry = self.store.get(identity)
        self.assertEqual((entry["scope"], entry["process"], entry["hotkey"]),
                         ("app", "Chrome.exe", "ctrl+alt+1"))
        self.assertEqual(self.store.resolve_for_process(self.profile, "chrome.EXE"), identity)
        self.assertEqual(self.store.resolve_for_process(self.profile, "  chrome.exe "), identity)
        self.assertIsNone(self.store.resolve_for_process(self.profile, "notepad.exe"))
        self.assertIsNone(self.store.resolve_for_process(self.profile, ""))
        self.assertEqual(self.store.resolve_hotkey(self.profile, "CTRL+ALT+1"), identity)
        self.assertIsNone(self.store.resolve_hotkey(self.profile, "ctrl+alt+9"))
        self.assertIsNone(self.store.resolve_hotkey(self.profile, ""))
        # A global preset never wins a process lookup.
        self.create("全局")
        self.assertIsNone(self.store.resolve_for_process(self.profile, "全局"))

    def test_status_defaults_validation_and_duplicate_bindings_are_rejected(self):
        self.store.create("一", self.profile, self.mapping,
                          scope="app", process="chrome.exe", hotkey="ctrl+1")
        before = self.store.path.read_bytes()
        with self.assertRaises(ValueError):  # same process, different casing
            self.store.create("二", self.profile, self.mapping,
                              scope="app", process="CHROME.EXE", hotkey="ctrl+2")
        with self.assertRaises(ValueError):  # same hotkey, different casing
            self.store.create("三", self.profile, self.mapping, hotkey="CTRL+1")
        with self.assertRaises(ValueError):  # an app scope needs a process name
            self.store.create("四", self.profile, self.mapping, scope="app")
        with self.assertRaises(ValueError):
            self.store.create("五", self.profile, self.mapping, scope="device")
        with self.assertRaises(ValueError):
            self.store.create("六", self.profile, self.mapping, hotkey="unknown-key")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual([entry["name"] for entry in self.store.load()], ["一"])

    def test_set_status_updates_only_state_and_survives_atomic_failure(self):
        identity = self.create("原配置")
        mapping_before = copy.deepcopy(self.store.get(identity)["mapping"])
        self.store.set_status(identity, "app", "code.exe", "alt+2")
        entry = self.store.get(identity)
        self.assertEqual((entry["scope"], entry["process"], entry["hotkey"]),
                         ("app", "code.exe", "alt+2"))
        self.assertEqual(entry["mapping"], mapping_before)
        # Switching back to a global scope clears the app binding but keeps hotkey.
        self.store.set_status(identity, "global", "", "alt+2")
        self.assertEqual(self.store.get(identity)["process"], "")
        self.assertIsNone(self.store.resolve_for_process(self.profile, "code.exe"))
        self.assertEqual(self.store.resolve_hotkey(self.profile, "alt+2"), identity)
        before = self.store.path.read_bytes()
        with mock.patch.object(config.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.store.set_status(identity, "app", "other.exe", "")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.get(identity)["process"], "")

    def test_v2_upgrade_adds_default_status_fields_and_keeps_applied_ids(self):
        self.create("A")
        second = self.create("B")
        self.store.mark_applied(self.profile, second)
        document = json.loads(self.store.path.read_text())
        v2_entries = [{key: entry[key] for key in ("id", "name", "profile", "mapping")}
                      for entry in document["presets"]]
        v2 = {"schema_version": 2, "presets": v2_entries,
              "applied_ids": {self.profile: second}}
        self.store.path.write_text(json.dumps(v2), encoding="utf-8")
        before = self.store.path.read_bytes()
        loaded = self.store.load()
        self.assertEqual([entry["name"] for entry in loaded], ["A", "B"])
        for entry in loaded:
            self.assertEqual((entry["scope"], entry["process"], entry["hotkey"]),
                             ("global", "", ""))
        rows = self.store.view(self.bindings, self.profile)
        self.assertEqual([row["id"] for row in rows if row["applied"]], [second])
        self.assertEqual(self.store.path.read_bytes(), before)

    def bundle(self, apps=None):
        return {"format": presets.BUNDLE_FORMAT, "schema_version": 1, "profile": self.profile,
                "global": self.mapping,
                "apps": [{"name": "浏览器", "process": "chrome.exe", "hotkey": "",
                          "mapping": self.mapping}] if apps is None else apps}

    def test_bundle_replaces_only_app_presets(self):
        global_id = self.create("我的全局")
        app_id = self.store.create("浏览器", self.profile, self.mapping,
                                   scope="app", process="chrome.exe", hotkey="ctrl+alt+1")
        self.store.mark_applied(self.profile, app_id)
        export = Path(self.tmp.name) / "full.json"
        self.store.export_bundle(
            self.profile, self.mapping,
            [{"name": "浏览器", "process": "chrome.exe", "hotkey": "ctrl+alt+1",
              "mapping": self.mapping},
             {"name": "编辑器", "process": "code.exe", "hotkey": "",
              "mapping": self.mapping}],
            export)
        value = json.loads(export.read_text(encoding="utf-8"))
        self.assertEqual(value["format"], presets.BUNDLE_FORMAT)
        self.assertNotIn(app_id, json.dumps(value))
        bundle = presets.read_bundle(export, self.profile)
        self.assertEqual(self.store.replace_apps(self.profile, bundle["apps"]), 2)
        entries = self.store.load()
        self.assertEqual([entry["name"] for entry in entries],
                         ["我的全局", "浏览器", "编辑器"])
        by_name = {entry["name"]: entry for entry in entries}
        self.assertNotEqual(by_name["浏览器"]["id"], app_id)      # fresh identifier
        self.assertEqual(by_name["浏览器"]["hotkey"], "ctrl+alt+1")
        self.assertEqual(by_name["编辑器"]["process"], "code.exe")
        self.assertEqual(by_name["我的全局"]["id"], global_id)     # global row kept
        self.assertEqual(self.store.resolve_for_process(self.profile, "code.exe"),
                         by_name["编辑器"]["id"])
        applied = json.loads(self.store.path.read_text(encoding="utf-8"))["applied_ids"]
        self.assertEqual(applied[self.profile], presets.AUTO_ID)

    def test_bundle_import_dedupes_names_and_drops_taken_hotkeys(self):
        self.store.create("浏览器", self.profile, self.mapping, hotkey="ctrl+alt+9")
        count = self.store.replace_apps(self.profile, [
            {"name": "浏览器", "process": "chrome.exe", "hotkey": "ctrl+alt+9",
             "mapping": self.mapping},
            {"name": "浏览器", "process": "code.exe", "hotkey": "", "mapping": self.mapping},
        ])
        self.assertEqual(count, 2)
        entries = self.store.load()
        self.assertEqual([entry["name"] for entry in entries],
                         ["浏览器", "浏览器 (2)", "浏览器 (3)"])
        self.assertEqual([entry["process"] for entry in entries[1:]], ["chrome.exe", "code.exe"])
        # The global row already owns ctrl+alt+9, so the imported row drops it
        # instead of failing the whole import.
        self.assertEqual(entries[0]["hotkey"], "ctrl+alt+9")
        self.assertEqual([entry["hotkey"] for entry in entries[1:]], ["", ""])

    def test_bundle_validation_rejects_foreign_and_damaged_documents(self):
        base = self.bundle()
        presets.validate_bundle(base, self.profile)  # a well-formed bundle passes
        damaged = [
            lambda d: d.update(format=presets.FORMAT),
            lambda d: d.update(schema_version=2),
            lambda d: d.update(schema_version=True),
            lambda d: d.update(extra="x"),
            lambda d: d.update(profile="other-model"),
            lambda d: d.pop("apps"),
            lambda d: d.update(apps={}),
            lambda d: d.update(apps=[{"name": "a"}]),
            lambda d: d.update(apps=[{"name": "a", "process": "x.exe", "hotkey": "",
                                      "mapping": self.mapping, "id": "z"}]),
            lambda d: d.update(apps=[{"name": "a", "process": "x.exe",
                                      "hotkey": "unknown-key", "mapping": self.mapping}]),
            lambda d: d.update(apps=[{"name": "a", "process": "x.exe", "hotkey": "",
                                      "mapping": {}}]),
            lambda d: d.update(apps=[{"name": "a", "process": "CHROME.EXE", "hotkey": "",
                                      "mapping": self.mapping},
                                     {"name": "b", "process": "chrome.exe", "hotkey": "",
                                      "mapping": self.mapping}]),
            lambda d: d.update(apps=[{"name": f"应用{i}", "process": f"app{i}.exe",
                                      "hotkey": "", "mapping": self.mapping}
                                     for i in range(presets.MAX_PRESETS + 1)]),
            lambda d: d["global"].update(physical_bindings={}),
        ]
        for mutate in damaged:
            document = copy.deepcopy(base)
            mutate(document)
            with self.assertRaises(ValueError):
                presets.validate_bundle(document, self.profile)
        with self.assertRaises(ValueError):
            presets.validate_bundle(base, "other-model")

    def test_bundle_export_keeps_the_data_directory_off_limits(self):
        with self.assertRaises(ValueError):
            self.store.export_bundle(self.profile, self.mapping, [],
                                     self.root / "full.json")
        self.assertFalse((self.root / "full.json").exists())
        export = Path(self.tmp.name) / "full.json"
        self.store.export_bundle(self.profile, self.mapping, [], export)
        self.assertEqual(json.loads(export.read_text(encoding="utf-8"))["apps"], [])


@unittest.skipUnless(qt_tests._HAS_PYSIDE6, qt_tests._SKIP_REASON)
class PresetControllerTests(unittest.TestCase):
    setUp = qt_tests.SettingsControllerTests.setUp
    tearDown = qt_tests.SettingsControllerTests.tearDown
    _make_controller = qt_tests.SettingsControllerTests._make_controller

    def test_copy_rename_apply_export_import_delete_without_other_settings_changes(self):
        from PySide6.QtCore import QUrl
        controller, model = self._make_controller(selected_remote=True)
        run = controller.mappingPresetAction
        controller._bindings["bindings"]["ok"] = {"kind": "mouse_right_click", "keys": []}
        controller._load_bindings_into_model()
        self.assertTrue(controller._save_mapping())
        original_config = copy.deepcopy(controller._config)
        original_physical = copy.deepcopy(controller._bindings.get("physical_bindings"))
        saved = run("copy", presets.AUTO_ID, "配置 A")
        self.assertTrue(saved["ok"], saved)
        duplicate = run("copy", saved["id"], "配置 A 副本")
        self.assertTrue(duplicate["ok"], duplicate)
        self.assertTrue(run("rename", saved["id"], "A改名")["ok"])
        self.assertEqual(controller.mappingPresets[2]["name"], "配置 A 副本")
        controller._bindings["bindings"]["ok"] = {"kind": "escape", "keys": []}
        controller._load_bindings_into_model()
        self.assertTrue(controller._save_mapping())
        result = run("apply", saved["id"], "")
        self.assertTrue(result["ok"], result)
        self.assertEqual(controller._bindings["bindings"]["ok"]["kind"], "mouse_right_click")
        self.assertEqual(controller._config, original_config)
        self.assertEqual(controller._bindings.get("physical_bindings"), original_physical)
        export = QUrl.fromLocalFile(str(Path(self._tmpdir.name) / "export.json")).toString()
        self.assertTrue(run("export", saved["id"], export)["ok"])
        before = copy.deepcopy(controller._bindings)
        result = run("import", "", export)
        self.assertTrue(result["ok"], result)
        self.assertEqual(controller._bindings, before)
        for row in list(controller.mappingPresets):
            if not row["automatic"]:
                self.assertTrue(run("delete", row["id"], "")["ok"])
        self.assertEqual(len(controller.mappingPresets), 1)
        self.assertTrue(controller.mappingPresets[0]["automatic"])
        self.assertEqual(controller._bindings, before)

    def test_automatic_row_is_listed_before_any_save(self):
        controller, _ = self._make_controller(selected_remote=True)
        rows = controller.mappingPresets
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["automatic"])
        self.assertTrue(rows[0]["applied"])
        self.assertEqual(rows[0]["id"], app_profiles.GLOBAL_ID)
        self.assertEqual(controller.voiceProfiles, rows)
        controller.refreshVoiceProfiles()
        self.assertEqual(len(controller.mappingPresets), 1)

    def test_failed_apply_keeps_disk_and_model_and_drafts_block_apply(self):
        controller, model = self._make_controller()
        self.assertTrue(controller._save_mapping())
        run = controller.mappingPresetAction
        saved = run("copy", presets.AUTO_ID, "备份")
        self.assertTrue(saved["ok"], saved)
        controller._bindings["bindings"]["ok"] = {"kind": "escape", "keys": []}
        controller._load_bindings_into_model()
        controller._save_mapping()
        before = copy.deepcopy(controller._bindings)
        display = model.to_display_map()
        with mock.patch.object(config, "save_settings_pair", side_effect=OSError("disk full")):
            self.assertFalse(run("apply", saved["id"], "")["ok"])
        self.assertEqual(controller._bindings, before)
        self.assertEqual(model.to_display_map(), display)
        controller._mapping_dirty = True
        self.assertFalse(run("apply", saved["id"], "")["ok"])
        self.assertFalse(run("copy", presets.AUTO_ID, "草稿")["ok"])

    def test_long_options_do_not_leak_to_single_or_double(self):
        controller, model = self._make_controller()
        from ovb_rc003 import settings_ui
        for label in settings_ui.LONG_PRESS_ACTION_LABELS.values():
            self.assertIn(label, controller.longPressActionOptionsFor("ok"))
            self.assertNotIn(label, controller.primaryActionOptionsFor("ok"))
            self.assertNotIn(label, controller.secondaryActionOptionsFor("ok"))

    def test_automatic_entry_and_marker_survive_reopen_and_failed_marker_write_is_reported(self):
        controller, _ = self._make_controller()
        run = controller.mappingPresetAction
        opened = run("refresh", "", "")
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["id"], presets.AUTO_ID)
        self.assertTrue(controller.mappingPresets[0]["applied"])
        for operation in ("rename", "delete"):
            self.assertFalse(run(operation, presets.AUTO_ID, "不可操作")["ok"])
        saved = run("copy", presets.AUTO_ID, "保留副本")
        self.assertTrue(saved["ok"], saved)
        self.assertTrue(controller.mappingPresets[0]["applied"])
        with mock.patch.object(app_profiles.AppProfileStore, "mark_applied", side_effect=OSError("disk full")):
            result = run("apply", saved["id"], "")
        self.assertTrue(result["ok"], result)
        self.assertIn("应用标记未能保存", result["message"])
        self.assertTrue(controller.mappingPresets[0]["applied"])
        self.assertFalse(controller.mappingPresets[1]["applied"])
        self.assertTrue(run("apply", saved["id"], "")["ok"])
        self.assertTrue(controller.mappingPresets[1]["applied"])
        result = run("refresh", "", "")
        self.assertEqual(result["id"], saved["id"])

    def test_applied_marker_uses_effective_values_after_loader_normalization(self):
        controller, _ = self._make_controller()
        profile = device_catalog.RC003_ID
        mapping = app_profiles.snapshot(controller._bindings, profile)
        mapping["bindings"]["ok"] = {"kind": "key_combo", "keys": ["enter"]}
        mapping["secondary_bindings"]["ok"] = {"double_click": {"kind": "disabled"}}
        mapping["display_notes"]["ok"] = {"single_click": "  确定  ", "long_press": ""}
        store = app_profiles.AppProfileStore(controller._config_root)
        identity = store.create("可兼容配置", mapping)
        result = controller.mappingPresetAction("apply", identity, "")
        self.assertTrue(result["ok"], result)
        self.assertEqual([row["id"] for row in controller.mappingPresets if row["applied"]], [identity])
        self.assertEqual(store.get(identity)["mapping"], mapping)
        controller._bindings["display_notes"]["ok"]["single_click"] = "已修改"
        result = controller.mappingPresetAction("refresh", "", "")
        self.assertEqual(result["id"], presets.AUTO_ID)

    def test_import_error_cannot_expand_the_modal_with_unbounded_file_content(self):
        from PySide6.QtCore import QUrl
        controller, _ = self._make_controller()
        value = {"format": presets.FORMAT, "schema_version": 1, "name": "bad",
                 "profile": device_catalog.RC003_ID,
                 "mapping": presets.snapshot(controller._bindings, device_catalog.RC003_ID)}
        value["mapping"]["bindings"]["ok"] = {"kind": "x" * 10000}
        source = Path(self._tmpdir.name) / "bad.json"
        source.write_text(json.dumps(value), encoding="utf-8")
        before = list(controller.mappingPresets)
        result = controller.mappingPresetAction("import", "", QUrl.fromLocalFile(str(source)).toString())
        self.assertFalse(result["ok"])
        self.assertLessEqual(len(result["message"]), 241)
        self.assertEqual(controller.mappingPresets, before)
        self.assertEqual([row["automatic"] for row in controller.mappingPresets], [True])

    def test_real_legacy_load_keeps_list_and_successful_import_available(self):
        from PySide6.QtCore import QUrl
        controller, _ = self._make_controller()
        root = controller._config_root
        store = app_profiles.AppProfileStore(root)
        identity = store.create("有效副本",
                                app_profiles.snapshot(controller._bindings, device_catalog.RC003_ID))
        export = Path(self._tmpdir.name) / "valid.json"
        store.export_file(identity, export)
        legacy = copy.deepcopy(controller._bindings)
        legacy["bindings"]["mic"] = {"kind": "voice_toggle", "keys": []}
        config.save_settings_pair(config.config_path(root), controller._config,
                                  config.key_bindings_path(root), legacy)
        reloaded, _ = self._make_controller()
        before = config.key_bindings_path(root).read_bytes()
        self.assertIn("mic", reloaded._removed_voice_bindings)
        run = reloaded.mappingPresetAction
        opened = run("refresh", "", "")
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(len(reloaded.mappingPresets), 2)
        imported = run("import", "", QUrl.fromLocalFile(str(export)).toString())
        self.assertTrue(imported["ok"], imported)
        self.assertIn("配置已导入", imported["message"])
        self.assertEqual(len(store.load()), 2)
        self.assertEqual(len(reloaded.mappingPresets), 3)
        self.assertTrue(run("copy", identity, "命名副本")["ok"])
        self.assertTrue(run("rename", imported["id"], "导入后改名")["ok"])
        self.assertTrue(run("export", imported["id"], QUrl.fromLocalFile(str(export)).toString())["ok"])
        for operation in ("rename", "delete"):
            self.assertFalse(run(operation, presets.AUTO_ID, "非法操作")["ok"])
        self.assertTrue(run("delete", identity, "")["ok"])
        reopened, _ = self._make_controller()
        self.assertTrue(reopened.mappingPresetAction("refresh", "", "")["ok"])
        self.assertEqual(len(reopened.mappingPresets), 3)
        self.assertEqual(config.key_bindings_path(root).read_bytes(), before)

    def test_committed_import_is_successful_when_only_the_list_refresh_fails(self):
        from PySide6.QtCore import QUrl
        controller, _ = self._make_controller()
        store = app_profiles.AppProfileStore(controller._config_root)
        identity = store.create("备份",
                                app_profiles.snapshot(controller._bindings, device_catalog.RC003_ID))
        export = Path(self._tmpdir.name) / "valid.json"
        store.export_file(identity, export)
        with mock.patch.object(type(controller), "_refresh_mapping_presets", side_effect=OSError("locked")):
            result = controller.mappingPresetAction("import", "", QUrl.fromLocalFile(str(export)).toString())
            failed_refresh = controller.mappingPresetAction("refresh", "", "")
        self.assertTrue(result["ok"], result)
        self.assertIn("配置已导入", result["message"])
        self.assertIn("列表未能刷新", result["message"])
        self.assertFalse(failed_refresh["ok"])
        self.assertEqual(len(store.load()), 2)
        self.assertEqual(store.get(result["id"])["name"], "备份 (2)")
        self.assertTrue(controller.mappingPresetAction("refresh", "", "")["ok"])
        self.assertEqual(len(controller.mappingPresets), 3)

    def test_failed_import_write_preserves_library_and_reports_failure(self):
        from PySide6.QtCore import QUrl
        controller, _ = self._make_controller()
        store = app_profiles.AppProfileStore(controller._config_root)
        identity = store.create("备份",
                                app_profiles.snapshot(controller._bindings, device_catalog.RC003_ID))
        export = Path(self._tmpdir.name) / "valid.json"
        store.export_file(identity, export)
        controller.mappingPresetAction("refresh", "", "")
        before, rows = store.path.read_bytes(), copy.deepcopy(controller.mappingPresets)
        with mock.patch.object(config.os, "replace", side_effect=PermissionError("locked")):
            result = controller.mappingPresetAction("import", "", QUrl.fromLocalFile(str(export)).toString())
        self.assertFalse(result["ok"])
        self.assertEqual(store.path.read_bytes(), before)
        self.assertEqual(controller.mappingPresets, rows)

    def test_set_status_roundtrips_and_lists_scope_fields(self):
        controller, _ = self._make_controller(selected_remote=True)
        controller.mappingPresetAction("refresh", "", "")
        self.assertEqual([option["label"] for option in controller.mappingPresetScopeOptions],
                         ["全局配置", "软件配置"])
        saved = controller.mappingPresetAction("copy", presets.AUTO_ID, "带状态")
        self.assertTrue(saved["ok"], saved)
        result = controller.setMappingPresetStatus(saved["id"], "app", "chrome.exe", "ctrl+alt+2")
        self.assertTrue(result["ok"], result)
        row = next(row for row in controller.mappingPresets if row["id"] == saved["id"])
        self.assertEqual((row["scope"], row["process"], row["hotkey"]),
                         ("app", "chrome.exe", "ctrl+alt+2"))
        reopened = app_profiles.AppProfileStore(controller._config_root).get(saved["id"])
        self.assertEqual(reopened["process"], "chrome.exe")
        self.assertFalse(controller.setMappingPresetStatus(
            presets.AUTO_ID, "app", "chrome.exe", "")["ok"])
        self.assertFalse(controller.setMappingPresetStatus(saved["id"], "app", "", "")["ok"])
        self.assertEqual(
            app_profiles.AppProfileStore(controller._config_root).get(saved["id"])["hotkey"], "ctrl+alt+2")

    def test_restore_defaults_rewrites_only_the_selected_layout(self):
        controller, model = self._make_controller(selected_remote=True)
        profile = device_catalog.RC003_ID
        controller._app_executable_cache["chrome.exe"] = ""
        # 全局先偏离内置默认并存盘，作为“不该被重置”的对照。
        controller._bindings["bindings"]["ok"] = {"kind": "escape", "keys": []}
        controller._load_bindings_into_model()
        self.assertTrue(controller._save_mapping())
        global_path = config.key_bindings_path(controller._config_root)
        global_before = global_path.read_bytes()

        store = app_profiles.AppProfileStore(controller._config_root)
        identity = store.create("浏览器", app_profiles.snapshot(controller._bindings, profile),
                                scope="app", process="chrome.exe")
        controller._refresh_mapping_presets()
        self.assertTrue(controller.selectMappingEditTarget(identity)["ok"])
        self.assertEqual(controller.mappingEditTarget, identity)

        controller.restoreMappingDefaults()
        controller._run_mapping_auto_save()
        self.assertFalse(controller.mappingDirty, controller.errorMessage)

        # 只重写选中的那份应用布局，全局文件一个字节都不变。
        self.assertEqual(global_path.read_bytes(), global_before)
        stored = store.get(identity)["mapping"]
        self.assertEqual(
            stored["bindings"]["ok"],
            key_mapping.ButtonAction(key_mapping.ActionKind.RETURN).to_dict())
        self.assertEqual(stored["secondary_bindings"], {})
        self.assertIn("「浏览器」", controller.statusMessage)

        # 回到全局再重置，写的才是全局文件。
        self.assertTrue(controller.selectMappingEditTarget("")["ok"])
        controller.restoreMappingDefaults()
        controller._run_mapping_auto_save()
        self.assertFalse(controller.mappingDirty, controller.errorMessage)
        self.assertEqual(
            config.load_key_bindings(global_path)["bindings"]["ok"],
            key_mapping.ButtonAction(key_mapping.ActionKind.RETURN).to_dict())

    def test_layout_transfer_only_touches_the_selected_layout(self):
        from PySide6.QtCore import QUrl
        controller, _ = self._make_controller(selected_remote=True)
        profile = device_catalog.RC003_ID
        # Keep the app-icon lookup off the real registry so the test stays fast.
        controller._app_executable_cache.update({"chrome.exe": "", "code.exe": ""})
        store = app_profiles.AppProfileStore(controller._config_root)

        controller._bindings["bindings"]["ok"] = {"kind": "mouse_right_click", "keys": []}
        controller._load_bindings_into_model()
        self.assertTrue(controller._save_mapping())
        app_mapping = app_profiles.snapshot(controller._bindings, profile)
        app_mapping["bindings"]["ok"] = {"kind": "escape", "keys": []}
        app_identity = store.create("浏览器", app_mapping, scope="app", process="chrome.exe")
        store.create("编辑器", app_profiles.snapshot(controller._bindings, profile),
                     scope="app", process="code.exe")
        controller._refresh_mapping_presets()

        # 全局布局的导出文件里只有这一份布局。
        export = Path(self._tmpdir.name) / "global.json"
        result = controller.exportMappingTarget(QUrl.fromLocalFile(str(export)).toString())
        self.assertTrue(result["ok"], result)
        self.assertIn("全局布局", result["message"])
        value = json.loads(export.read_text(encoding="utf-8"))
        self.assertEqual(value["format"], app_profiles.FORMAT)
        self.assertEqual(value["schema_version"], app_profiles.EXPORT_SCHEMA_VERSION)
        self.assertEqual(value["name"], "全局布局")
        self.assertNotIn("apps", value)
        self.assertEqual(value["mapping"]["bindings"]["ok"]["kind"], "mouse_right_click")

        # 全局改成别的按键，并留一条本机学习到的物理按键签名。
        controller._bindings["bindings"]["ok"] = {"kind": "return", "keys": []}
        controller._load_bindings_into_model()
        self.assertTrue(controller._save_mapping())
        controller._bindings["physical_bindings"] = {"sig-1": "mic"}
        apps_before = [(row["id"], row["name"]) for row in controller.mappingAppProfiles]

        result = controller.importMappingTarget(QUrl.fromLocalFile(str(export)).toString())
        self.assertTrue(result["ok"], result)
        self.assertIn("全局布局", result["message"])
        self.assertEqual(controller._bindings["bindings"]["ok"]["kind"], "mouse_right_click")
        self.assertEqual(controller._bindings["physical_bindings"], {"sig-1": "mic"})
        # 应用布局既没有被替换，也没有被新建。
        self.assertEqual([(row["id"], row["name"]) for row in controller.mappingAppProfiles],
                         apps_before)
        self.assertEqual(store.get(app_identity)["mapping"]["bindings"]["ok"]["kind"], "escape")
        self.assertEqual(len(store.list_apps()), 2)

        # 切到应用布局导出，文件名和内容都只描述这一份。
        self.assertTrue(controller.selectMappingEditTarget(app_identity)["ok"])
        app_export = Path(self._tmpdir.name) / "chrome.json"
        result = controller.exportMappingTarget(QUrl.fromLocalFile(str(app_export)).toString())
        self.assertTrue(result["ok"], result)
        self.assertIn("浏览器", result["message"])
        app_value = json.loads(app_export.read_text(encoding="utf-8"))
        self.assertEqual(app_value["name"], "浏览器")
        self.assertEqual(app_value["mapping"]["bindings"]["ok"]["kind"], "escape")

        # 改动这份应用布局后导入回去，只有它被还原。
        changed = copy.deepcopy(app_mapping)
        changed["bindings"]["ok"] = {"kind": "return", "keys": []}
        store.update_mapping(app_identity, changed)
        controller._load_bindings_into_model()
        result = controller.importMappingTarget(QUrl.fromLocalFile(str(app_export)).toString())
        self.assertTrue(result["ok"], result)
        self.assertIn("浏览器", result["message"])
        self.assertEqual(store.get(app_identity)["mapping"]["bindings"]["ok"]["kind"], "escape")
        self.assertEqual(store.get(app_identity)["name"], "浏览器")
        self.assertEqual(len(store.list_apps()), 2)
        # 全局布局没有跟着变。
        self.assertEqual(controller._bindings["bindings"]["ok"]["kind"], "mouse_right_click")

    def test_layout_import_rejects_foreign_files_and_export_guards_the_data_dir(self):
        from PySide6.QtCore import QUrl
        controller, _ = self._make_controller(selected_remote=True)
        controller._app_executable_cache["chrome.exe"] = ""
        store = app_profiles.AppProfileStore(controller._config_root)
        store.create("浏览器",
                     app_profiles.snapshot(controller._bindings, device_catalog.RC003_ID),
                     scope="app", process="chrome.exe")
        controller._refresh_mapping_presets()
        library_before = store.path.read_bytes()
        bindings_before = copy.deepcopy(controller._bindings)
        # 别的软件导出的文件不是本软件的按键配置。
        foreign = Path(self._tmpdir.name) / "foreign.json"
        foreign.write_text(json.dumps({"format": "something-else"}), encoding="utf-8")
        result = controller.importMappingTarget(QUrl.fromLocalFile(str(foreign)).toString())
        self.assertFalse(result["ok"])
        self.assertIn("本软件导出的按键配置文件", result["message"])
        self.assertEqual(store.path.read_bytes(), library_before)
        self.assertEqual(controller._bindings, bindings_before)
        self.assertEqual([row["name"] for row in controller.mappingAppProfiles], ["浏览器"])
        # Exporting on top of the software's own data directory is refused.
        guard = QUrl.fromLocalFile(str(controller._config_root / "global.json")).toString()
        self.assertFalse(controller.exportMappingTarget(guard)["ok"])
        self.assertFalse((controller._config_root / "global.json").exists())

