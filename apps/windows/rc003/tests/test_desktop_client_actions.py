"""Desktop client presets must resolve real apps, including renamed packages."""
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import action_executor, config, key_mapping, remote_selection, settings_ui


class DesktopClientActionTests(unittest.TestCase):
    def test_codex_package_is_found_when_its_display_name_is_chatgpt(self):
        result = mock.Mock(returncode=0, stdout='['
            '{"Name":"ChatGPT","AppID":"OpenAI.ChatGPT_example!App"},'
            '{"Name":"ChatGPT","AppID":"OpenAI.Codex_example!App"}]')
        with mock.patch.object(action_executor.subprocess, "run", return_value=result), \
             mock.patch.object(action_executor.sys, "platform", "win32"), \
             mock.patch.object(Path, "is_file", return_value=True):
            command = action_executor._packaged_desktop_command(key_mapping.ActionKind.OPEN_CODEX)
        self.assertEqual(command[1], "shell:AppsFolder\\OpenAI.Codex_example!App")

    def test_codex_does_not_accept_an_unrelated_chatgpt_package(self):
        result = mock.Mock(returncode=0, stdout='{"Name":"ChatGPT","AppID":"OpenAI.ChatGPT_example!App"}')
        with mock.patch.object(action_executor.subprocess, "run", return_value=result), \
             mock.patch.object(action_executor.sys, "platform", "win32"):
            self.assertIsNone(action_executor._packaged_desktop_command(key_mapping.ActionKind.OPEN_CODEX))

    def test_google_tv_app_keys_save_as_desktop_actions(self):
        _, saved = settings_ui.build_save_model(
            button_display_map={"youtube": "打开 Codex", "netflix": "打开 WorkBuddy"},
            hotkey_text="", endpoint_display_text="", base_config=config.default_config(),
            base_bindings=config.default_key_bindings(remote_selection.GOOGLE_TV_PROFILE),
            selected_device_profile=remote_selection.GOOGLE_TV_PROFILE,
        )
        self.assertEqual(saved["bindings"]["youtube"]["kind"], "open_codex")
        self.assertEqual(saved["bindings"]["netflix"]["kind"], "open_workbuddy")
        self.assertTrue(action_executor.is_application_action(
            key_mapping.ButtonAction.from_dict(saved["bindings"]["netflix"])))
