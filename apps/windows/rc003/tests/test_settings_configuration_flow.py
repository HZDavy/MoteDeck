"""Configuration journeys use isolated files and never start real devices."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from ovb_rc003 import config, bridge_runtime_status, key_mapping, qt_settings_app
from tests import test_qt_settings_app as controller_tests


@unittest.skipUnless(controller_tests._HAS_PYSIDE6, controller_tests._SKIP_REASON)
class ConfigurationFlowTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtCore import QCoreApplication

        self.qt = QCoreApplication.instance() or QCoreApplication([])
        self.fixture = controller_tests.SettingsControllerTests()
        # We invoke this fixture manually, so unittest will not run its
        # registered patch cleanups. Register them before setup and keep
        # teardown first (cleanups execute in reverse order).
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def controller(self):
        controller, model = self.fixture._make_controller()
        self.addCleanup(lambda: controller._mapping_auto_save_timer.stop())
        return controller, model

    def select_output(self, controller, name="CABLE Input"):
        def preflight(_name, _api, completed):
            completed(True, "")
            return True

        with mock.patch.object(controller, "_request_endpoint_preflight", preflight), \
                mock.patch.object(controller, "_request_endpoint_options_refresh"):
            return controller.selectAndPersistOutputEndpoint(name, "Windows WASAPI")

    def test_output_restart_survives_healthy_poll_and_clears_after_stop(self):
        controller, _ = self.controller()
        controller._set_bridge_running(True)
        controller._set_bridge_connected(True)
        self.assertTrue(self.select_output(controller))
        self.assertTrue(controller.bridgeRestartRecommended)
        bridge_runtime_status.publish_status(
            controller._config_root, bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=os.getpid(), raw_input_state="ready", hid_tap_state="ready",
            voice_key_physicalizer_state="ready",
        )
        status = bridge_runtime_status.read_status(controller._config_root)
        with mock.patch.object(bridge_runtime_status, "runtime_identity_matches", return_value=True):
            controller._update_bridge_restart_recommendation(status, schedule_recovery=True)
        self.assertTrue(controller.bridgeRestartRecommended)
        self.assertFalse(controller._bridge_recovery_attempted)
        controller._set_bridge_running(False)
        controller._set_bridge_restart_recommended(False)
        self.assertFalse(controller.bridgeRestartRecommended)
        controller._set_bridge_running(True)
        self.assertTrue(self.select_output(controller))  # same endpoint
        self.assertFalse(controller.bridgeRestartRecommended)

    def test_output_save_failure_does_not_create_restart_requirement(self):
        controller, _ = self.controller()
        controller._set_bridge_running(True)
        before = dict(controller._config)
        with mock.patch.object(config, "save_config_and_load", side_effect=OSError("locked")):
            self.assertFalse(self.select_output(controller))
        self.assertFalse(controller.bridgeRestartRecommended)
        self.assertEqual(controller._config, before)

    def test_combined_save_also_keeps_output_restart_action(self):
        controller, _ = self.controller()
        # Slot 1 must carry a recorded shortcut before a full save can pass: an
        # empty voice slot is deliberately fail-closed instead of silently
        # falling back to a fabricated wake shortcut.
        controller._voice_slot_hotkeys[1] = "ralt"
        controller._set_bridge_running(True)
        endpoint = qt_settings_app.audio_output.AudioEndpoint("CABLE Input", "Windows WASAPI")
        controller._endpoint_values = [endpoint]
        controller._endpoint_options = [qt_settings_app.settings_ui._endpoint_display(endpoint)]
        controller._selected_endpoint_index = 0
        with mock.patch.object(controller, "_request_endpoint_preflight",
                               lambda name, api, done: (done(True, ""), True)[1]):
            self.assertTrue(controller._save(), controller.errorMessage)
        self.assertTrue(controller.bridgeRestartRecommended)

    def test_output_saved_while_stopped_does_not_require_restart(self):
        controller, _ = self.controller()
        self.assertTrue(self.select_output(controller))
        self.assertFalse(controller.bridgeRestartRecommended)

    def test_mapping_autosave_and_restore_work_with_empty_voice_hotkey(self):
        data = config.default_config()
        config.save_config(config.config_path(config.config_root()), data)
        controller, model = self.controller()
        model.setActionTextAt(model.index_of("power"), "ctrl+l")
        controller._run_mapping_auto_save()
        self.assertFalse(controller.mappingDirty, controller.errorMessage)
        saved = config.load_key_bindings(config.key_bindings_path(config.config_root()))
        self.assertEqual(saved["bindings"]["power"]["keys"], ["ctrl", "l"])
        self.assertTrue(key_mapping.is_voice_action(
            key_mapping.ButtonAction.from_dict(saved["bindings"]["mic"])
        ))
        self.assertEqual(config.load_config(config.config_path(config.config_root()))["voice_hotkey"], "")
        controller.restoreMappingDefaults()
        controller._run_mapping_auto_save()
        self.assertFalse(controller.mappingDirty, controller.errorMessage)
        self.assertEqual(controller.holdVoiceHotkeyText, "")

    def test_mapping_only_save_still_rejects_invalid_mapping(self):
        controller, model = self.controller()
        model.setActionTextAt(model.index_of("power"), "not_a_key")
        self.assertFalse(controller._save_mapping())
        self.assertTrue(controller.mappingDirty)

    def test_legacy_mapping_edit_and_unrelated_autosave_keep_saved_key_order(self):
        path = config.key_bindings_path(config.config_root())
        document = config.load_key_bindings(path)
        old = {
            "single_click": {"kind": "key_combo", "keys": ["vk_a2", "shift"]},
            "double_click": {"kind": "key_combo", "keys": ["vk_11"]},
            "long_press": {"kind": "key_combo", "keys": ["vk_00"]},
        }
        document["bindings"]["power"] = old["single_click"]
        document.setdefault("secondary_bindings", {})["power"] = {
            key: value for key, value in old.items() if key != "single_click"
        }
        config.save_key_bindings(path, document)
        before = path.read_bytes()
        controller, model = self.controller()
        row = model.index_of("power")
        self.assertEqual(path.read_bytes(), before)
        for gesture, role in (("single_click", model.ActionTextRole),
                              ("double_click", model.DoubleClickTextRole),
                              ("long_press", model.LongPressTextRole)):
            label = model.data(model.index(row, 0), role)
            self.assertEqual(controller.buttonActionValidationMessage("power", gesture, label, "sendinput"), "")
            if gesture == "single_click":
                self.assertEqual(label, "左 Ctrl（键码 0xA2） + Shift")
                model.setActionTextAt(row, label.lower().replace(" + ", "＋"))
            else:
                model.setSecondaryActionTextAt(row, gesture, label)
        self.assertFalse(controller.mappingDirty)
        self.assertEqual(path.read_bytes(), before)
        # Saving a note in this editor, then another button, must also retain
        # the invalid legacy gesture without making it executable.
        model.setDisplayNoteAt(row, "single_click", "保留旧键码")
        controller._run_mapping_auto_save()
        self.assertFalse(controller.mappingDirty, controller.errorMessage)
        model.setActionTextAt(model.index_of("up"), "Ctrl+F9")
        controller._run_mapping_auto_save()
        self.assertFalse(controller.mappingDirty, controller.errorMessage)
        saved = config.load_key_bindings(path)
        for gesture, expected in old.items():
            self.assertEqual(key_mapping.button_action_for(
                saved, "power", key_mapping.ButtonTrigger(gesture)).to_dict(), expected)
        self.assertEqual(saved["bindings"]["up"]["keys"], ["ctrl", "f9"])
        controller._load_bindings_into_model()
        self.assertEqual(model.to_display_map()["power"], "vk_a2+shift")
        from ovb_rc003 import win32_input, win32_keys
        sender = mock.Mock(side_effect=lambda events: len(events))
        action = key_mapping.button_action_for(saved, "power", key_mapping.ButtonTrigger.SINGLE_CLICK)
        win32_input.send_key_combo_tap(action.keys, _sender=sender, _key_down_query=lambda vk: False)
        sender.assert_called_once_with([(0xA2, False), (0x10, False), (0x10, True), (0xA2, True)])
        sender.reset_mock()
        invalid = key_mapping.button_action_for(saved, "power", key_mapping.ButtonTrigger.LONG_PRESS)
        with self.assertRaises(win32_keys.UnknownKeyTokenError):
            win32_input.send_key_combo_tap(invalid.keys, _sender=sender, _key_down_query=lambda vk: False)
        sender.assert_not_called()

@unittest.skipUnless(controller_tests._HAS_PYSIDE6, controller_tests._SKIP_REASON)
class ConfigurationQmlFlowTests(unittest.TestCase):
    def test_saved_output_exposes_existing_restart_action(self):
        from tests.test_hid_runtime_upgrade_qml import _SETUP

        script = _SETUP + r'''
from ovb_rc003 import bridge_runtime_status, hid_elevation_windows
from unittest import mock
find(window, "bridgeStatusRefreshTimer").setProperty("running", False)
find(window, "tabBar").setProperty("currentIndex", 2)
render(window, app)
wait_for_voice_hotkey_idle(controller, app)
controller._bridge_running = True
controller.bridgeRunningChanged.emit()
controller._set_bridge_connected(True)
controller._set_bridge_connection_state("connected")
controller._hid_helper_state = hid_elevation_windows.HidHelperState(True, "ready")
controller.hidHelperStateChanged.emit()
bridge_runtime_status.publish_status(m.config.config_root(), bridge_runtime_status.BridgeConnectionState.CONNECTED,
    pid=os.getpid(), raw_input_state="ready", hid_tap_state="ready", voice_key_physicalizer_state="ready")
controller._set_bridge_input_states(bridge_runtime_status.read_status(m.config.config_root()))
controller._set_bridge_restart_recommended(False)
page = find(window, "deviceScroll").parent()
assert not page.bridgeActionVisible()
controller._request_endpoint_preflight = lambda name, api, done: (done(True, ""), True)[1]
controller._request_endpoint_options_refresh = lambda: None
assert controller.selectAndPersistOutputEndpoint("CABLE Input", "Windows WASAPI")
assert page.bridgeActionVisible()
assert page.bridgeActionText() == "重启服务"
diagnostics._is_refreshing = False
diagnostics.isRefreshingChanged.emit()
assert find(window, "testVbCableChannelButton") is not None
find(window, "tabBar").setProperty("currentIndex", 0)
with mock.patch.object(controller, "restartBridge") as restart:
    find(window, "bridgeActionButton").clicked.emit()
    restart.assert_called_once_with()
assert '点击“应用”' not in (m._qml_directory() / "VoicePage.qml").read_text(encoding="utf-8")
assert '点击“选推荐端点”' in (m._qml_directory() / "VoicePage.qml").read_text(encoding="utf-8")
print(json.dumps({"warnings": [str(item) for item in warnings]}))
window.hide()
m._shutdown_diagnostics_workers()
'''
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, LOCALAPPDATA=tmp, QT_QPA_PLATFORM="offscreen", RC003_DISABLE_LIVE_INPUT="1")
            result = subprocess.run([sys.executable, "-B", "-c", script], env=env,
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1])["warnings"], [])
