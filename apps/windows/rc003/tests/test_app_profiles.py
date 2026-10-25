import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import app_profiles, config, device_catalog, key_mapping, mapping_presets


class AppProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "data"
        self.store = app_profiles.AppProfileStore(self.root)
        self.profile = device_catalog.RC003_ID
        self.bindings = config.default_key_bindings(self.profile)
        self.bindings["physical_bindings"] = {"power": {"vk": 123}}
        self.mapping = app_profiles.snapshot(self.bindings, self.profile)
        self.legacy = mapping_presets.PresetStore(self.root)

    def create(self, name="工作", **kwargs):
        return self.store.create(name, self.mapping, **kwargs)

    def sparse(self, button="ok", action=None):
        return {"bindings": {button: action or {"kind": "escape", "keys": []}},
                "secondary_bindings": {}, "display_notes": {}}

    # -- migration ---------------------------------------------------------

    def test_legacy_library_is_converted_once_and_kept_untouched(self):
        global_id = self.legacy.create("我的全局", self.profile, self.mapping,
                                       hotkey="ctrl+alt+9")
        app_id = self.legacy.create("浏览器", self.profile, self.mapping, scope="app",
                                    process="chrome.exe", hotkey="ctrl+alt+1")
        self.legacy.mark_applied(self.profile, app_id)
        before = self.legacy.path.read_bytes()

        loaded = self.store.load()
        self.assertEqual([entry["id"] for entry in loaded], [global_id, app_id])
        self.assertEqual([entry["name"] for entry in loaded], ["我的全局", "浏览器"])
        for entry, old in zip(loaded, self.legacy.load()):
            self.assertEqual({key: entry[key] for key in
                              ("id", "name", "scope", "process", "hotkey", "mapping")},
                             {key: old[key] for key in
                              ("id", "name", "scope", "process", "hotkey", "mapping")})
            # The legacy file predates the voice mode, so it converts to 模式1.
            self.assertEqual((entry["voice_input_source"], entry["voice_local_input_device"]),
                             ("remote", ""))
        document = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(document["schema_version"], app_profiles.SCHEMA_VERSION)
        self.assertEqual(document["applied"], app_id)
        self.assertEqual(self.legacy.path.read_bytes(), before)

        written = self.store.path.read_bytes()
        self.assertEqual(self.store.load(), loaded)
        self.assertEqual(self.store.path.read_bytes(), written)

    def test_v1_and_v2_upgrade_fill_defaults_and_keep_applied_ids(self):
        identity = self.legacy.create("旧配置", self.profile, self.mapping)
        entry = self.legacy.load()[0]
        old_entry = {key: entry[key] for key in ("id", "name", "profile", "mapping")}
        self.legacy.path.write_text(json.dumps({"schema_version": 1, "presets": [old_entry]}),
                                    encoding="utf-8")
        self.assertEqual(self.store.load()[0]["id"], identity)
        self.assertEqual({key: self.store.load()[0][key]
                          for key in ("scope", "process", "hotkey")},
                         {"scope": "global", "process": "", "hotkey": ""})
        self.assertEqual(json.loads(self.store.path.read_text())["applied"],
                         app_profiles.GLOBAL_ID)

        self.store.path.unlink()
        self.legacy.path.write_text(json.dumps({
            "schema_version": 2, "presets": [old_entry],
            "applied_ids": {self.profile: identity}}), encoding="utf-8")
        self.store.load()
        self.assertEqual(json.loads(self.store.path.read_text())["applied"], identity)

    def test_schema_4_library_gains_the_voice_fields_in_place(self):
        identity = self.create("我的全局")
        document = json.loads(self.store.path.read_text(encoding="utf-8"))
        document["schema_version"] = 4
        for entry in document["profiles"]:
            entry.pop("voice_input_source")
            entry.pop("voice_local_input_device")
            entry.pop("voice_keys")
        self.store.path.write_text(json.dumps(document), encoding="utf-8")

        entry = self.store.load()[0]
        self.assertEqual((entry["id"], entry["voice_input_source"],
                          entry["voice_local_input_device"]), (identity, "remote", ""))
        self.assertEqual(entry["voice_keys"], [])
        upgraded = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(upgraded["schema_version"], app_profiles.SCHEMA_VERSION)
        self.assertEqual(set(upgraded["profiles"][0]), set(app_profiles.ENTRY_FIELDS))

    def test_failed_migration_keeps_legacy_file_and_writes_nothing(self):
        self.legacy.create("工作", self.profile, self.mapping)
        before = self.legacy.path.read_bytes()
        with mock.patch.object(config.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.store.load()
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.legacy.path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.legacy.path])

    def test_corrupted_library_is_not_silently_replaced(self):
        self.root.mkdir()
        self.store.path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(self.store.path.read_text(), "{bad")

    # -- resolution --------------------------------------------------------

    def test_resolve_mapping_overlays_only_the_defined_buttons_and_triggers(self):
        original = copy.deepcopy(self.bindings)
        override = {
            "bindings": {"ok": {"kind": "escape", "keys": []}},
            "secondary_bindings": {"ok": {"long_press": {"kind": "mouse_left_hold", "keys": []}}},
            "display_notes": {"ok": {"single_click": "确认"}},
        }
        resolved = app_profiles.resolve_mapping(self.bindings, override, self.profile)
        self.assertEqual(resolved["bindings"]["ok"], {"kind": "escape", "keys": []})
        # Every button the profile does not mention still comes from the global layout.
        for button, action in original["bindings"].items():
            if button != "ok":
                self.assertEqual(resolved["bindings"][button], action)
        self.assertEqual(resolved["secondary_bindings"]["ok"]["long_press"]["kind"],
                         "mouse_left_hold")
        self.assertEqual(resolved["secondary_bindings"]["ok"].get("double_click"),
                         original["secondary_bindings"].get("ok", {}).get("double_click"))
        self.assertEqual(resolved["display_notes"]["ok"]["single_click"], "确认")
        self.assertEqual(resolved["physical_bindings"], original["physical_bindings"])
        self.assertEqual(self.bindings, original)

        untouched = app_profiles.resolve_mapping(self.bindings, None, self.profile)
        self.assertIsNot(untouched, self.bindings)
        untouched["bindings"]["ok"] = {"kind": "disabled", "keys": []}
        self.assertEqual(self.bindings, original)

    def test_resolve_mapping_rejects_unknown_buttons_and_extra_fields(self):
        for override in ({"bindings": {"nope": {"kind": "escape", "keys": []}},
                          "secondary_bindings": {}, "display_notes": {}},
                         {**self.sparse(), "physical_bindings": {}},
                         self.sparse(action={"kind": "unknown"})):
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    app_profiles.resolve_mapping(self.bindings, override, self.profile)

    def test_sparse_mappings_are_only_allowed_for_app_profiles(self):
        sparse = self.sparse()
        self.assertEqual(app_profiles.validate_mapping(sparse, self.profile, partial=True), sparse)
        with self.assertRaises(ValueError):
            app_profiles.validate_mapping(sparse, self.profile)
        self.assertEqual(app_profiles.validate_mapping(self.mapping, self.profile), self.mapping)
        for value in ({**sparse, "bindings": {"ok": {"kind": "x"}}},
                      {**sparse, "secondary_bindings": {"ok": {"triple_click": {"kind": "escape"}}}},
                      {**sparse, "display_notes": {"ok": {"triple_click": "x"}}},
                      {**sparse, "display_notes": {"ok": {"single_click": "x" * 257}}}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    app_profiles.validate_mapping(value, self.profile, partial=True)
        with self.assertRaises(ValueError):
            app_profiles.validate_mapping(self.mapping, "unsupported")

    def test_snapshot_fills_absent_primary_actions_and_drops_unknown_fields(self):
        bindings = copy.deepcopy(self.bindings)
        del bindings["bindings"]["ok"]
        value = app_profiles.snapshot(bindings, self.profile)
        self.assertEqual(value["bindings"]["ok"], {"kind": "disabled", "keys": []})
        self.assertEqual(set(value), set(app_profiles.MAPPING_FIELDS))
        self.assertEqual(app_profiles.apply_to(self.bindings, {"mapping": self.mapping})["bindings"],
                         self.mapping["bindings"])

    # -- profile library ---------------------------------------------------

    def test_duplicate_names_processes_and_hotkeys_are_rejected_on_write(self):
        self.store.create("浏览器", self.sparse(), scope="app",
                          process="chrome.exe", hotkey="ctrl+alt+1")
        before = self.store.path.read_bytes()
        invalid = [
            lambda: self.store.create("浏览器", self.mapping),
            lambda: self.store.create("另一个", self.sparse(), scope="app", process="CHROME.EXE"),
            lambda: self.store.create("第三个", self.mapping, hotkey="CTRL+ALT+1"),
            lambda: self.store.create("缺进程", self.sparse(), scope="app"),
            lambda: self.store.create("坏状态", self.mapping, scope="device"),
            lambda: self.store.create("坏热键", self.mapping, hotkey="unknown-key"),
            lambda: self.store.create(app_profiles.GLOBAL_NAME, self.mapping),
        ]
        for operation in invalid:
            with self.assertRaises(ValueError):
                operation()
            self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual([entry["name"] for entry in self.store.load()], ["浏览器"])

    def test_rename_update_and_delete_keep_other_fields_intact(self):
        app_id = self.store.create("浏览器", self.sparse(), scope="app",
                                   process="chrome.exe", hotkey="alt+1")
        global_id = self.create("我的全局")
        self.store.rename(app_id, "浏览器改名")
        self.assertEqual(self.store.get(app_id)["process"], "chrome.exe")
        with self.assertRaises(ValueError):
            self.store.rename(global_id, "浏览器改名")
        with self.assertRaises(ValueError):
            self.store.rename(app_id, app_profiles.GLOBAL_NAME)
        with self.assertRaises(ValueError):
            self.store.rename("missing", "x")

        sparse = self.sparse(button="back", action={"kind": "return", "keys": []})
        self.store.update_mapping(app_id, sparse)
        self.assertEqual(self.store.get(app_id)["mapping"], sparse)
        before = self.store.path.read_bytes()
        with self.assertRaises(ValueError):  # a global layout must stay complete
            self.store.update_mapping(global_id, sparse)
        with self.assertRaises(ValueError):
            self.store.update_mapping("missing", sparse)
        self.assertEqual(self.store.path.read_bytes(), before)

        self.store.mark_applied(app_id)
        self.store.delete(app_id)
        self.assertEqual([entry["id"] for entry in self.store.load()], [global_id])
        self.assertEqual(json.loads(self.store.path.read_text())["applied"],
                         app_profiles.GLOBAL_ID)
        with self.assertRaises(ValueError):
            self.store.delete(app_profiles.GLOBAL_ID)

    def test_set_status_updates_only_switching_state_and_survives_atomic_failure(self):
        identity = self.create("原配置")
        mapping_before = copy.deepcopy(self.store.get(identity)["mapping"])
        self.store.set_status(identity, "app", "code.exe", "alt+2")
        entry = self.store.get(identity)
        self.assertEqual((entry["scope"], entry["process"], entry["hotkey"]),
                         ("app", "code.exe", "alt+2"))
        self.assertEqual(entry["mapping"], mapping_before)
        self.store.set_status(identity, "global", "", "alt+2")
        self.assertEqual(self.store.get(identity)["process"], "")
        self.assertIsNone(self.store.resolve_for_process("code.exe"))
        self.assertEqual(self.store.resolve_hotkey("alt+2"), identity)

        before = self.store.path.read_bytes()
        with mock.patch.object(config.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.store.set_status(identity, "app", "other.exe", "")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.get(identity)["process"], "")

    def test_resolvers_ignore_global_profiles_and_normalize_input(self):
        identity = self.store.create("浏览器", self.sparse(), scope="app",
                                     process="Chrome.exe", hotkey="ctrl+alt+1")
        self.create("我的全局", hotkey="")
        self.assertEqual(self.store.resolve_for_process("chrome.EXE"), identity)
        self.assertEqual(self.store.resolve_for_process("  chrome.exe "), identity)
        self.assertIsNone(self.store.resolve_for_process("notepad.exe"))
        self.assertIsNone(self.store.resolve_for_process(""))
        self.assertEqual(self.store.resolve_hotkey("CTRL+ALT+1"), identity)
        self.assertIsNone(self.store.resolve_hotkey("ctrl+alt+9"))
        self.assertIsNone(self.store.resolve_hotkey(""))

    def test_ensure_app_reuses_the_process_and_lists_only_app_profiles(self):
        self.create("我的全局")
        identity = self.store.ensure_app("浏览器", "chrome.exe", self.sparse())
        self.assertEqual(self.store.ensure_app("浏览器", "CHROME.EXE", self.mapping), identity)
        self.assertEqual([entry["name"] for entry in self.store.list_apps()], ["浏览器"])
        self.assertEqual(self.store.find_app("chrome.exe")["id"], identity)
        self.assertIsNone(self.store.find_app(""))
        self.assertIsNone(self.store.find_app("code.exe"))

    # -- view --------------------------------------------------------------

    def test_view_renders_the_live_global_row_without_writing(self):
        rows = self.store.view(self.bindings, self.profile)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["id"], rows[0]["name"], rows[0]["scope"]),
                         (app_profiles.GLOBAL_ID, app_profiles.GLOBAL_NAME, "global"))
        self.assertTrue(rows[0]["automatic"] and rows[0]["applied"])
        self.bindings["bindings"]["ok"] = {"kind": "escape", "keys": []}
        latest = app_profiles.AppProfileStore(self.root).view(self.bindings, self.profile)[0]
        self.assertEqual(latest["mapping"]["bindings"]["ok"]["kind"], "escape")
        self.assertFalse(self.store.path.exists())
        self.assertFalse(config.key_bindings_path(self.root).exists())
        with self.assertRaises(ValueError):
            self.store.view(self.bindings, "unsupported")

    def test_view_marks_the_active_app_profile_and_hides_an_unreadable_snapshot(self):
        identity = self.store.create("浏览器", self.sparse(), scope="app",
                                     process="chrome.exe")
        self.store.mark_applied(identity)
        self.assertEqual(self.store.applied_id(), identity)
        rows = self.store.view(self.bindings, self.profile)
        self.assertEqual([row["id"] for row in rows if row["applied"]], [identity])
        self.assertEqual([row["id"] for row in rows if row["automatic"]],
                         [app_profiles.GLOBAL_ID])

        damaged = copy.deepcopy(self.bindings)
        damaged["bindings"]["mic"] = {"kind": "unknown"}
        rows = self.store.view(damaged, self.profile)
        self.assertEqual([row["id"] for row in rows if row["applied"]],
                         [app_profiles.GLOBAL_ID])
        self.assertIsNone(rows[0]["mapping"])
        with self.assertRaises(ValueError):
            self.store.export_entry(rows[0], Path(self.tmp.name) / "invalid.json")

    def test_applied_global_profile_follows_content_and_survives_rename(self):
        identity = self.create("我的全局")
        self.store.mark_applied(identity)
        self.assertEqual([row["id"] for row in self.store.view(self.bindings, self.profile)
                          if row["applied"]], [identity])
        self.store.rename(identity, "改名后")
        changed = copy.deepcopy(self.bindings)
        changed["bindings"]["ok"] = {"kind": "escape", "keys": []}
        self.assertEqual([row["id"] for row in self.store.view(changed, self.profile)
                          if row["applied"]], [app_profiles.GLOBAL_ID])

    def test_atomic_failure_preserves_previous_bytes_and_removes_temp_file(self):
        identity = self.create()
        before = self.store.path.read_bytes()
        with mock.patch.object(config.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.store.rename(identity, "新名称")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.store.path])

    # -- portable files ----------------------------------------------------

    def test_single_export_import_roundtrip_and_data_directory_guard(self):
        name = app_profiles.GLOBAL_NAME + "副本"
        identity = self.create(name)
        export = Path(self.tmp.name) / "backup.json"
        self.store.export_file(identity, export)
        value = json.loads(export.read_text(encoding="utf-8"))
        self.assertEqual(set(value), {"format", "profile", "name", "mapping",
                                     "schema_version", "voice_input_source",
                                     "voice_local_input_device", "voice_keys"})
        self.assertEqual(value["schema_version"], app_profiles.EXPORT_SCHEMA_VERSION)
        self.assertEqual((value["voice_input_source"], value["voice_local_input_device"]),
                         ("remote", ""))
        self.assertEqual(value["voice_keys"], [])
        self.assertNotIn("id", value)
        self.assertNotIn("physical_bindings", value["mapping"])

        imported = self.store.import_file(export, self.profile, scope="app", process="code.exe")
        self.assertNotEqual(imported, identity)
        entry = self.store.get(imported)
        self.assertEqual((entry["name"], entry["process"]), (name + " (2)", "code.exe"))
        self.assertEqual(entry["mapping"], self.mapping)

        sparse_id = self.store.ensure_app("浏览器", "chrome.exe", self.sparse())
        sparse_export = Path(self.tmp.name) / "sparse.json"
        self.store.export_file(sparse_id, sparse_export)
        self.assertEqual(json.loads(sparse_export.read_text())["mapping"], self.sparse())
        renamed = self.store.import_file(sparse_export, self.profile, scope="app",
                                         process="notepad.exe")
        self.assertEqual(self.store.get(renamed)["mapping"], self.sparse())
        with self.assertRaises(ValueError):  # a sparse file cannot become the global layout
            self.store.import_file(sparse_export, self.profile)

        before = self.store.path.read_bytes()
        for name in (app_profiles.FILENAME, app_profiles.LEGACY_FILENAME,
                     "config.json", "key_bindings.json"):
            with self.assertRaises(ValueError):
                self.store.export_file(identity, self.root / name)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_import_rejects_foreign_and_damaged_documents(self):
        identity = self.create()
        export = Path(self.tmp.name) / "backup.json"
        self.store.export_file(identity, export)
        original = json.loads(export.read_text())
        damaged = []
        for key, value in (("schema_version", 99), ("schema_version", True),
                           ("profile", "other"), ("voice_hotkey", "f2")):
            document = copy.deepcopy(original)
            document[key] = value
            damaged.append(document)
        for mutate in (lambda d: d["mapping"].update(physical_bindings={}),
                       lambda d: d["mapping"].pop("display_notes"),
                       lambda d: d["mapping"]["bindings"].update(nope={"kind": "escape"}),
                       lambda d: d["mapping"]["bindings"].update(mic={"kind": "voice"})):
            document = copy.deepcopy(original)
            mutate(document)
            damaged.append(document)
        for document in damaged:
            export.write_text(json.dumps(document), encoding="utf-8")
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    self.store.import_file(export, self.profile)
        self.assertEqual(len(self.store.load()), 1)

    def bundle(self, apps=None):
        return {"format": app_profiles.BUNDLE_FORMAT, "schema_version": 1,
                "profile": self.profile, "global": self.mapping,
                "apps": [{"name": "浏览器", "process": "chrome.exe", "hotkey": "",
                          "mapping": self.sparse()}] if apps is None else apps}

    def test_bundle_moves_the_global_and_every_application_layout_together(self):
        global_id = self.create("我的全局")
        app_id = self.store.create("浏览器", self.sparse(), scope="app",
                                   process="chrome.exe", hotkey="ctrl+alt+1")
        self.store.mark_applied(app_id)
        export = Path(self.tmp.name) / "full.json"
        self.store.export_bundle(self.profile, self.mapping,
                                 [{"name": "浏览器", "process": "chrome.exe",
                                   "hotkey": "ctrl+alt+1", "mapping": self.sparse()},
                                  {"name": "编辑器", "process": "code.exe",
                                   "hotkey": "", "mapping": self.sparse()}], export)
        value = json.loads(export.read_text(encoding="utf-8"))
        self.assertEqual(value["format"], app_profiles.BUNDLE_FORMAT)
        self.assertNotIn(app_id, json.dumps(value))
        self.assertEqual(value["global"]["bindings"]["ok"], self.mapping["bindings"]["ok"])

        bundle = app_profiles.read_bundle(export, self.profile)
        self.assertEqual(self.store.replace_apps(bundle["apps"]), 2)
        entries = self.store.load()
        self.assertEqual([entry["name"] for entry in entries], ["我的全局", "浏览器", "编辑器"])
        by_name = {entry["name"]: entry for entry in entries}
        self.assertNotEqual(by_name["浏览器"]["id"], app_id)
        self.assertEqual(by_name["浏览器"]["hotkey"], "ctrl+alt+1")
        self.assertEqual(by_name["编辑器"]["process"], "code.exe")
        self.assertEqual(by_name["我的全局"]["id"], global_id)
        self.assertEqual(self.store.resolve_for_process("code.exe"), by_name["编辑器"]["id"])
        self.assertEqual(json.loads(self.store.path.read_text())["applied"],
                         app_profiles.GLOBAL_ID)

    def test_bundle_import_dedupes_names_and_drops_taken_hotkeys(self):
        self.create("浏览器", hotkey="ctrl+alt+9")
        count = self.store.replace_apps([
            {"name": "浏览器", "process": "chrome.exe", "hotkey": "ctrl+alt+9",
             "mapping": self.sparse()},
            {"name": "浏览器", "process": "code.exe", "hotkey": "", "mapping": self.sparse()},
        ])
        self.assertEqual(count, 2)
        entries = self.store.load()
        self.assertEqual([entry["name"] for entry in entries],
                         ["浏览器", "浏览器 (2)", "浏览器 (3)"])
        self.assertEqual([entry["process"] for entry in entries[1:]], ["chrome.exe", "code.exe"])
        # The global row already owns ctrl+alt+9, so the imported row drops it.
        self.assertEqual(entries[0]["hotkey"], "ctrl+alt+9")
        self.assertEqual([entry["hotkey"] for entry in entries[1:]], ["", ""])
        with self.assertRaises(ValueError):  # duplicate process inside one import
            self.store.replace_apps([
                {"name": "一", "process": "same.exe", "hotkey": "", "mapping": self.sparse()},
                {"name": "二", "process": "SAME.EXE", "hotkey": "", "mapping": self.sparse()},
            ])

    def test_bundle_validation_rejects_foreign_and_damaged_documents(self):
        base = self.bundle()
        app_profiles.validate_bundle(base, self.profile)
        damaged = [
            lambda d: d.update(format=app_profiles.FORMAT),
            lambda d: d.update(schema_version=99),
            lambda d: d.update(schema_version=True),
            lambda d: d.update(extra="x"),
            lambda d: d.update(profile="other-model"),
            lambda d: d.pop("apps"),
            lambda d: d.update(apps={}),
            lambda d: d.update(apps=[{"name": "a"}]),
            lambda d: d.update(apps=[{"name": "a", "process": "x.exe", "hotkey": "",
                                      "mapping": self.sparse(), "id": "z"}]),
            lambda d: d.update(apps=[{"name": "a", "process": "x.exe",
                                      "hotkey": "unknown-key", "mapping": self.sparse()}]),
            lambda d: d.update(apps=[{"name": "a", "process": "x.exe", "hotkey": "",
                                      "mapping": {}}]),
            lambda d: d.update(apps=[{"name": "a", "process": "CHROME.EXE", "hotkey": "",
                                      "mapping": self.sparse()},
                                     {"name": "b", "process": "chrome.exe", "hotkey": "",
                                      "mapping": self.sparse()}]),
            lambda d: d.update(apps=[{"name": f"应用{i}", "process": f"app{i}.exe",
                                      "hotkey": "", "mapping": self.sparse()}
                                     for i in range(app_profiles.MAX_PROFILES + 1)]),
            lambda d: d["global"].update(physical_bindings={}),
        ]
        for mutate in damaged:
            document = copy.deepcopy(base)
            mutate(document)
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    app_profiles.validate_bundle(document, self.profile)
        with self.assertRaises(ValueError):
            app_profiles.validate_bundle(base, "other-model")

    def test_bundle_export_keeps_the_data_directory_off_limits(self):
        with self.assertRaises(ValueError):
            self.store.export_bundle(self.profile, self.mapping, [], self.root / "full.json")
        self.assertFalse((self.root / "full.json").exists())
        export = Path(self.tmp.name) / "full.json"
        self.store.export_bundle(self.profile, self.mapping, [], export)
        self.assertEqual(json.loads(export.read_text(encoding="utf-8"))["apps"], [])

    def test_library_export_carries_only_whole_machine_profiles(self):
        first = self.create("本机麦", voice_input_source="local",
                            voice_local_input_device="USB 麦克风",
                            voice_keys=[{"id": 1, "name": "麦克风键", "hotkey": "",
                                         "source": "local", "device": "USB 麦克风",
                                         "enabled": True, "plugin": ""}])
        second = self.create("备用全局")
        self.store.create("浏览器", self.sparse(), scope="app", process="chrome.exe")
        export = Path(self.tmp.name) / "library.json"
        count = self.store.export_library(
            export, self.profile, self.mapping,
            voice_input_source="remote", voice_local_input_device="USB 麦克风")
        # 软件级配置由按键页单独导入导出，不进「配置档案」的整体备份。
        self.assertEqual(count, 2)
        document = json.loads(export.read_text(encoding="utf-8"))
        self.assertEqual(document["format"], app_profiles.LIBRARY_FORMAT)
        self.assertEqual(document["schema_version"], app_profiles.LIBRARY_SCHEMA_VERSION)
        self.assertEqual([entry["id"] for entry in document["profiles"]],
                         [first, second])
        self.assertEqual(
            [(entry["voice_input_source"], entry["voice_local_input_device"])
             for entry in document["profiles"]],
            [("local", "USB 麦克风"), ("remote", "")])
        self.assertEqual(document["profiles"][0]["voice_keys"][0]["name"], "麦克风键")
        self.assertEqual(document["profiles"][1]["voice_keys"], [])
        self.assertEqual(document["global"]["voice_keys"], [])
        self.assertEqual(document["global"]["mapping"]["bindings"]["ok"],
                         self.mapping["bindings"]["ok"])
        # 模式1 drops the device it was handed; the live row stays consistent.
        self.assertEqual(document["global"]["voice_input_source"], "remote")
        self.assertEqual(document["global"]["voice_local_input_device"], "")
        self.assertNotIn("physical_bindings", document["global"]["mapping"])

        with self.assertRaises(ValueError):
            self.store.export_library(self.root / "library.json", self.profile, self.mapping)
        self.assertFalse((self.root / "library.json").exists())

    # -- voice mode --------------------------------------------------------

    def test_voice_mode_is_stored_copied_and_updatable(self):
        identity = self.create("本机麦", voice_input_source="local",
                               voice_local_input_device="USB 麦克风")
        entry = self.store.get(identity)
        self.assertEqual((entry["voice_input_source"], entry["voice_local_input_device"]),
                         ("local", "USB 麦克风"))

        duplicate = self.store.copy_profile(identity)
        copied = self.store.get(duplicate)
        self.assertEqual(copied["name"], "本机麦 副本")
        self.assertEqual((copied["voice_input_source"], copied["voice_local_input_device"]),
                         ("local", "USB 麦克风"))

        self.store.update_voice(duplicate, "remote", "USB 麦克风")
        updated = self.store.get(duplicate)
        self.assertEqual((updated["voice_input_source"], updated["voice_local_input_device"]),
                         ("remote", ""))
        self.assertEqual(self.store.get(identity)["voice_input_source"], "local")

    def test_copying_an_app_profile_needs_the_live_global_layout(self):
        app_id = self.store.create("浏览器", self.sparse(), scope="app", process="chrome.exe")
        with self.assertRaises(ValueError):
            self.store.copy_profile(app_id)
        copied = self.store.get(self.store.copy_profile(app_id, bindings=self.bindings))
        self.assertEqual(copied["scope"], "global")
        # The copy is a standalone layout: the overlay plus every untouched button.
        self.assertEqual(set(copied["mapping"]["bindings"]),
                         set(app_profiles.remote_layout.button_order(self.profile)))
        self.assertEqual(copied["mapping"]["bindings"]["ok"], {"kind": "escape", "keys": []})
        self.assertNotIn("physical_bindings", copied["mapping"])

    def test_voice_validation_rejects_unknown_sources_and_oversized_devices(self):
        with self.assertRaises(ValueError):
            app_profiles.validate_voice("wireless")
        with self.assertRaises(ValueError):
            app_profiles.validate_voice("local", "x" * (app_profiles.VOICE_DEVICE_MAX_LENGTH + 1))
        with self.assertRaises(ValueError):
            app_profiles.validate_voice("local", "bad\nname")
        self.assertEqual(app_profiles.validate_voice(" LOCAL ", " USB 麦克风 "),
                         ("local", "USB 麦克风"))
        self.assertEqual(app_profiles.validate_voice("remote", "会被丢弃"), ("remote", ""))
        self.assertEqual(app_profiles.voice_mode_label("local"), "模式2")

    def test_mode_tag_and_display_label_carry_the_mode_and_device(self):
        remote = {"name": "办公", "voice_input_source": "remote",
                  "voice_local_input_device": ""}
        local = {"name": "会议", "voice_input_source": "local",
                 "voice_local_input_device": "USB 麦克风"}
        undecided = {"name": "待选", "voice_input_source": "local",
                     "voice_local_input_device": ""}
        self.assertEqual(app_profiles.mode_tag(remote), "模式1")
        self.assertEqual(app_profiles.mode_tag(local), "模式2 · USB 麦克风")
        self.assertEqual(app_profiles.mode_tag(undecided), "模式2")
        self.assertEqual(app_profiles.display_label(remote), "办公（模式1）")
        self.assertEqual(app_profiles.display_label(local), "会议（模式2 · USB 麦克风）")

    def test_voice_conflicts_flag_mode_1_keys_that_could_never_fire(self):
        on_ok = self.sparse(button="ok", action={"kind": "voice_hold", "keys": []})
        self.assertEqual(app_profiles.voice_conflicts(on_ok, "remote"), ["确定键"])
        self.assertEqual(app_profiles.voice_conflicts(on_ok, "local"), [])

        second_slot = self.sparse(button="mic", action={"kind": "voice_hold_2", "keys": []})
        self.assertEqual(app_profiles.voice_conflicts(second_slot, "remote"), ["麦克风键"])

        long_press = {"bindings": {}, "display_notes": {},
                      "secondary_bindings": {"ok": {
                          "long_press": {"kind": "voice_hold", "keys": []}}}}
        self.assertEqual(app_profiles.voice_conflicts(long_press, "remote"), ["确定键"])

        clean = self.sparse(button="mic", action={"kind": "voice_hold", "keys": []})
        self.assertEqual(app_profiles.voice_conflicts(clean, "remote"), [])
        self.assertEqual(app_profiles.voice_conflict_message([]), "")
        self.assertIn("麦克风键", app_profiles.voice_conflict_message(["麦克风键"]))

    def test_voice_conflicts_exempt_plugin_owned_voice_keys(self):
        keys = [{"id": 1, "name": "麦克风键", "hotkey": "", "source": "remote",
                 "device": "", "enabled": True, "plugin": ""},
                {"id": 2, "name": "Vokie", "hotkey": "", "source": "remote",
                 "device": "", "enabled": True, "plugin": "vokie"}]
        # The plugin records its own audio, so 模式1 neither restricts the button
        # nor limits it to 语音1.
        second_slot = self.sparse(button="mic", action={"kind": "voice_hold_2", "keys": []})
        self.assertEqual(app_profiles.plugin_slot_ids(keys), {2})
        self.assertEqual(app_profiles.voice_conflicts(
            second_slot, "remote", plugin_slots=app_profiles.plugin_slot_ids(keys)), [])
        self.assertEqual(app_profiles.voice_conflicts(
            second_slot, "remote", plugin_slots={1}), ["麦克风键"])

    def test_view_exposes_the_live_tag_and_the_conflict_warning(self):
        identity = self.store.create(
            "会议", {"bindings": {"ok": {"kind": "voice_hold", "keys": []}},
                     "secondary_bindings": {}, "display_notes": {}},
            scope="app", process="code.exe",
            voice_input_source="local", voice_local_input_device="USB 麦克风")
        rows = self.store.view(self.bindings, self.profile)
        row = next(item for item in rows if item["id"] == identity)
        self.assertEqual(row["mode_tag"], "模式2 · USB 麦克风")
        self.assertEqual(row["label"], "会议（模式2 · USB 麦克风）")
        self.assertEqual(row["voice_conflicts"], [])
        self.assertEqual(row["voice_conflict_text"], "")

        self.store.update_voice(identity, "remote")
        rows = self.store.view(self.bindings, self.profile,
                               voice_input_source="local",
                               voice_local_input_device="板载麦克风")
        row = next(item for item in rows if item["id"] == identity)
        self.assertEqual(row["mode_tag"], "模式1")
        self.assertEqual(row["voice_conflicts"], ["确定键"])
        self.assertIn("确定键", row["voice_conflict_text"])
        # The live global row reads the voice mode passed by the caller.
        self.assertEqual((rows[0]["mode_tag"], rows[0]["voice_input_source"]),
                         ("模式2 · 板载麦克风", "local"))

    def test_single_export_keeps_the_voice_mode_and_v1_files_still_import(self):
        identity = self.create("会议", voice_input_source="local",
                               voice_local_input_device="USB 麦克风")
        export = Path(self.tmp.name) / "mode2.json"
        self.store.export_file(identity, export)
        value = json.loads(export.read_text(encoding="utf-8"))
        self.assertEqual((value["voice_input_source"], value["voice_local_input_device"]),
                         ("local", "USB 麦克风"))
        imported = self.store.import_file(export, self.profile, scope="app",
                                          process="code.exe")
        self.assertEqual(self.store.get(imported)["voice_local_input_device"], "USB 麦克风")

        legacy = {key: value[key] for key in ("format", "profile", "name", "mapping")}
        legacy["schema_version"] = 1
        export.write_text(json.dumps(legacy), encoding="utf-8")
        downgraded = self.store.get(self.store.import_file(export, self.profile,
                                                           scope="app", process="vim.exe"))
        self.assertEqual((downgraded["voice_input_source"],
                          downgraded["voice_local_input_device"]), ("remote", ""))

    def test_bundle_v2_carries_the_voice_mode_and_v1_bundles_still_import(self):
        apps = [{"name": "浏览器", "process": "chrome.exe", "hotkey": "",
                 "mapping": self.sparse(), "voice_input_source": "local",
                 "voice_local_input_device": "USB 麦克风"},
                {"name": "编辑器", "process": "code.exe", "hotkey": "",
                 "mapping": self.sparse()}]
        export = Path(self.tmp.name) / "full.json"
        self.store.export_bundle(self.profile, self.mapping, apps, export,
                                 voice_input_source="local",
                                 voice_local_input_device="板载麦克风")
        value = json.loads(export.read_text(encoding="utf-8"))
        self.assertEqual(value["schema_version"], app_profiles.BUNDLE_SCHEMA_VERSION)
        self.assertEqual(value["voice"], {"voice_input_source": "local",
                                          "voice_local_input_device": "板载麦克风"})

        bundle = app_profiles.read_bundle(export, self.profile)
        self.assertEqual((bundle["voice_input_source"],
                          bundle["voice_local_input_device"]), ("local", "板载麦克风"))
        self.store.replace_apps(bundle["apps"], **{key: bundle[key] for key in
                                                   app_profiles.VOICE_FIELDS})
        by_process = {entry["process"]: entry for entry in self.store.load()
                      if entry["scope"] == "app"}
        self.assertEqual(by_process["chrome.exe"]["voice_local_input_device"], "USB 麦克风")
        # An app without its own voice fields inherits the bundle's.
        self.assertEqual(by_process["code.exe"]["voice_local_input_device"], "板载麦克风")

        legacy = {"format": app_profiles.BUNDLE_FORMAT, "schema_version": 1,
                  "profile": self.profile, "global": self.mapping, "apps": []}
        export.write_text(json.dumps(legacy), encoding="utf-8")
        old = app_profiles.read_bundle(export, self.profile)
        self.assertEqual((old["voice_input_source"], old["voice_local_input_device"]),
                         ("remote", ""))

    def test_voice_slots_remain_valid_but_retired_voice_kinds_are_rejected(self):
        for kind in ("voice_hold", "voice_hold_2"):
            value = self.sparse(button="mic", action={"kind": kind, "keys": []})
            app_profiles.validate_mapping(value, self.profile, partial=True)
        for kind in ("voice", "voice_toggle"):
            value = self.sparse(button="mic", action={"kind": kind, "keys": []})
            with self.assertRaises(ValueError):
                app_profiles.validate_mapping(value, self.profile, partial=True)
        self.assertEqual(key_mapping.MAX_VOICE_SLOTS, 99)


if __name__ == "__main__":
    unittest.main()
