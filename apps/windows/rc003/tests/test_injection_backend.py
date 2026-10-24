import ctypes
import logging
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from ovb_rc003 import app as app_module
from ovb_rc003 import (
    injection_backend,
    interception_runtime_windows,
    key_mapping,
    win32_input,
    win32_keys,
)


# A deliberately tiny stand-in for the keys used by the tests below.
_FAKE_SCAN_CODES = {
    0x09: (0x0F, False),  # tab
    0x11: (0x1D, False),  # ctrl
    0x12: (0x38, False),  # alt
    0x41: (0x1E, False),  # a
    0x42: (0x30, False),  # b
    0x5B: (0x5B, True),   # win
    0x5D: (0x5D, True),   # apps
    0x1B: (0x01, False),  # escape
    0x26: (0x48, True),   # up
    0x4D: (0x32, False),  # m
    0xAF: (0x30, True),   # volume up
}


def _fake_physical_scan_code(vk):
    return _FAKE_SCAN_CODES.get(int(vk))


class _FakeFunction:
    """Callable that tolerates the ``restype``/``argtypes`` assignments."""

    def __init__(self, implementation):
        self._implementation = implementation
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        return self._implementation(*args)


class _FakeInterceptionDll:
    def __init__(self, *, context=0x1234, send_result=None):
        self.context = context
        self.send_result = send_result
        self.sends = []
        self.destroyed = []
        self.create_calls = 0
        self.interception_create_context = _FakeFunction(self._create_context)
        self.interception_send = _FakeFunction(self._send)
        self.interception_destroy_context = _FakeFunction(self._destroy_context)

    def _create_context(self):
        self.create_calls += 1
        return self.context

    def _send(self, context, device, strokes, count):
        self.sends.append((int(device), int(count), strokes))
        if self.send_result is not None:
            return self.send_result
        return int(count)

    def _destroy_context(self, context):
        self.destroyed.append(context)


def _decode_key_strokes(buffer, count):
    array = ctypes.cast(
        buffer, ctypes.POINTER(interception_runtime_windows._KeyStroke)
    )
    return [array[index] for index in range(count)]


class InjectModeNormalizationTests(unittest.TestCase):
    def test_default_and_rejections_land_on_sendinput(self):
        for value in (None, "", "   ", "bogus", 7):
            with self.subTest(value=value):
                self.assertEqual(
                    injection_backend.normalize_inject_mode(value), "sendinput"
                )

    def test_interception_is_case_insensitive(self):
        for value in ("sendinput", "SENDINPUT", "  SendInput "):
            with self.subTest(value=value):
                self.assertEqual(
                    injection_backend.normalize_inject_mode(value), "sendinput"
                )
        for value in ("interception", "Interception", "  INTERCEPTION "):
            with self.subTest(value=value):
                self.assertEqual(
                    injection_backend.normalize_inject_mode(value), "interception"
                )

    def test_key_mapping_and_backend_share_one_definition(self):
        self.assertEqual(
            injection_backend.DEFAULT_INJECT_MODE, key_mapping.DEFAULT_INJECT_MODE
        )
        self.assertIs(injection_backend.INJECT_MODES, key_mapping.INJECT_MODES)
        self.assertEqual(
            key_mapping.normalize_stored_inject_mode("INTERCEPTION"), "interception"
        )


class ActionTranslationTests(unittest.TestCase):
    def test_key_combo_returns_its_own_tokens(self):
        action = key_mapping.ButtonAction(
            key_mapping.ActionKind.KEY_COMBO, keys=("ctrl", "a")
        )
        self.assertEqual(
            injection_backend.key_tokens_for_action(action), ("ctrl", "a")
        )

    def test_empty_key_combo_has_no_tokens(self):
        action = key_mapping.ButtonAction(key_mapping.ActionKind.KEY_COMBO)
        self.assertIsNone(injection_backend.key_tokens_for_action(action))

    def test_semantic_actions_use_the_sendinput_sequences(self):
        for action_kind, expected in (
            (key_mapping.ActionKind.ESCAPE, ("escape",)),
            (key_mapping.ActionKind.RETURN, ("enter",)),
            (key_mapping.ActionKind.DELETE_BACKWARD, ("backspace",)),
            (key_mapping.ActionKind.SHOW_DESKTOP, ("win", "m")),
            (key_mapping.ActionKind.CONTEXT_MENU, ("apps",)),
            (key_mapping.ActionKind.APP_SWITCHER, ("ctrl", "alt", "tab")),
        ):
            with self.subTest(kind=action_kind):
                self.assertEqual(
                    injection_backend.key_tokens_for_action(
                        key_mapping.ButtonAction(action_kind)
                    ),
                    expected,
                )

    def test_non_injecting_actions_have_no_key_tokens(self):
        for action_kind in (
            key_mapping.ActionKind.DISABLED,
            key_mapping.ActionKind.QUICKER_URI,
            key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE,
            key_mapping.ActionKind.MOUSE_LEFT_CLICK,
            key_mapping.ActionKind.VOICE_HOLD,
        ):
            with self.subTest(kind=action_kind):
                self.assertIsNone(
                    injection_backend.key_tokens_for_action(
                        key_mapping.ButtonAction(action_kind)
                    )
                )

    def test_mouse_clicks_map_to_interception_buttons(self):
        for action_kind, expected in (
            (key_mapping.ActionKind.MOUSE_LEFT_CLICK, "left"),
            (key_mapping.ActionKind.MOUSE_RIGHT_CLICK, "right"),
            (key_mapping.ActionKind.MOUSE_MIDDLE_CLICK, "middle"),
            (key_mapping.ActionKind.MOUSE_X1_CLICK, "x1"),
            (key_mapping.ActionKind.MOUSE_X2_CLICK, "x2"),
        ):
            with self.subTest(kind=action_kind):
                self.assertEqual(
                    injection_backend.mouse_button_for_action(
                        key_mapping.ButtonAction(action_kind)
                    ),
                    expected,
                )
        self.assertIsNone(
            injection_backend.mouse_button_for_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ESCAPE)
            )
        )


class InterceptionCapabilityTests(unittest.TestCase):
    def test_empty_token_sequences_are_unsupported(self):
        self.assertFalse(injection_backend.tokens_supported_by_interception(()))

    def test_multimedia_tokens_have_real_scan_codes(self):
        # Consumer controls carry no MapVirtualKey code but do have fixed
        # E0-prefixed scan codes, so Interception can inject them like any key.
        for token in ("volume_up", "volume_down", "volume_mute", "media_play_pause"):
            with self.subTest(token=token):
                self.assertTrue(
                    injection_backend.tokens_supported_by_interception((token,))
                )

    def test_unknown_token_is_unsupported(self):
        self.assertFalse(
            injection_backend.tokens_supported_by_interception(("not_a_key",))
        )

    def test_tokens_with_physical_scan_codes_are_supported(self):
        with mock.patch.object(
            win32_input, "physical_scan_code", side_effect=_fake_physical_scan_code
        ):
            for tokens in (
                ("escape",),
                ("up",),
                ("apps",),
                ("win", "m"),
                ("ctrl", "alt", "tab"),
            ):
                with self.subTest(tokens=tokens):
                    self.assertTrue(
                        injection_backend.tokens_supported_by_interception(tokens)
                    )

    def test_action_support_follows_tokens_and_mouse_clicks(self):
        with mock.patch.object(
            win32_input, "physical_scan_code", side_effect=_fake_physical_scan_code
        ):
            self.assertTrue(
                injection_backend.action_supported_by_interception(
                    key_mapping.ButtonAction(key_mapping.ActionKind.ESCAPE)
                )
            )
            self.assertTrue(
                injection_backend.action_supported_by_interception(
                    key_mapping.ButtonAction(key_mapping.ActionKind.MOUSE_LEFT_CLICK)
                )
            )
            self.assertTrue(
                injection_backend.action_supported_by_interception(
                    key_mapping.ButtonAction(key_mapping.ActionKind.SYSTEM_VOLUME_UP)
                )
            )
            # Element navigation has its own host path and never goes through
            # the injection backend, so it must never report as supported.
            self.assertFalse(
                injection_backend.action_supported_by_interception(
                    key_mapping.ButtonAction(
                        key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE
                    )
                )
            )
            self.assertFalse(
                injection_backend.action_supported_by_interception(
                    key_mapping.ButtonAction(key_mapping.ActionKind.DISABLED)
                )
            )

    def test_sendinput_is_always_available(self):
        self.assertEqual(injection_backend.mode_status("sendinput"), (True, ""))
        self.assertTrue(injection_backend.mode_available("sendinput"))
        self.assertTrue(injection_backend.mode_available("bogus"))

    def test_interception_status_reports_the_driver_reason(self):
        with mock.patch.object(
            interception_runtime_windows, "is_available", return_value=False
        ), mock.patch.object(
            interception_runtime_windows,
            "unavailable_reason",
            return_value="driver_not_loaded",
        ):
            self.assertEqual(
                injection_backend.mode_status("interception"),
                (False, "driver_not_loaded"),
            )
            self.assertFalse(injection_backend.mode_available("interception"))
        with mock.patch.object(
            interception_runtime_windows, "is_available", return_value=True
        ):
            self.assertEqual(injection_backend.mode_status("interception"), (True, ""))

    def test_interception_unavailable_hint_maps_each_reason(self):
        cases = {
            "": "",
            "driver_not_installed: the Interception keyboard filter is not registered": "未检测到 Interception 驱动",
            "driver_not_loaded: interception_create_context failed": "尚未生效",
            "dll_not_found: place interception.dll next to the app": "interception.dll",
            "create_context_failed:OSError": "上下文创建失败",
            "live Windows input is disabled for this process": "已禁用实时输入",
            "something_unexpected": "Interception 暂不可用",
        }
        for reason, expected in cases.items():
            with self.subTest(reason=reason), mock.patch.object(
                interception_runtime_windows,
                "unavailable_reason",
                return_value=reason,
            ):
                hint = injection_backend.interception_unavailable_hint()
                if expected:
                    self.assertIn(expected, hint)
                else:
                    self.assertEqual(hint, "")


class InterceptionAssetDiscoveryTests(unittest.TestCase):
    """The DLL lookup must cover a source checkout as well as a frozen build."""

    def test_source_tree_third_party_folder_is_searched(self):
        module_dir = os.path.dirname(os.path.abspath(interception_runtime_windows.__file__))
        expected = os.path.join(
            os.path.normpath(os.path.join(module_dir, os.pardir, os.pardir)),
            "build",
            "third_party",
            "interception",
            "interception.dll",
        )
        self.assertIn(expected, interception_runtime_windows._dll_candidate_paths())

    def test_environment_override_wins_outright(self):
        with mock.patch.dict(
            os.environ,
            {interception_runtime_windows.DLL_ENV_VAR: "D:/custom/interception.dll"},
        ):
            self.assertEqual(
                interception_runtime_windows._dll_candidate_paths(),
                ["D:/custom/interception.dll"],
            )

    def test_frozen_build_never_searches_the_source_tree(self):
        with mock.patch.object(
            interception_runtime_windows.sys, "frozen", True, create=True
        ):
            self.assertEqual(interception_runtime_windows._source_tree_asset_dirs(), [])


class InterceptionRuntimeTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"RC003_DISABLE_LIVE_INPUT": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _runtime(dll, **kwargs):
        kwargs.setdefault("driver_probe", lambda: True)
        return interception_runtime_windows.InterceptionRuntime(
            dll_loader=lambda: dll,
            keyboard_index=0,
            scan_code_resolver=_fake_physical_scan_code,
            **kwargs,
        )

    def test_availability_never_opens_a_driver_context(self):
        dll = _FakeInterceptionDll()
        runtime = self._runtime(dll)
        self.assertTrue(runtime.is_available())
        self.assertEqual(runtime.unavailable_reason(), "")
        self.assertEqual(dll.create_calls, 0)
        self.assertEqual(dll.destroyed, [])

    def test_missing_driver_registration_is_reported(self):
        dll = _FakeInterceptionDll()
        runtime = self._runtime(dll, driver_probe=lambda: False)
        with mock.patch.object(
            interception_runtime_windows, "driver_installed", return_value=False
        ):
            self.assertFalse(runtime.is_available())
            self.assertIn("driver_not_installed", runtime.unavailable_reason())
        self.assertEqual(dll.create_calls, 0)

    def test_missing_dll_is_reported_as_not_found(self):
        def failing_loader():
            raise OSError("not found")

        runtime = interception_runtime_windows.InterceptionRuntime(
            dll_loader=failing_loader,
            scan_code_resolver=_fake_physical_scan_code,
        )
        self.assertFalse(runtime.is_available())
        self.assertIn("dll_not_found", runtime.unavailable_reason())

    def test_live_input_gate_blocks_the_driver(self):
        dll = _FakeInterceptionDll()
        runtime = self._runtime(dll)
        with mock.patch.dict(os.environ, {"RC003_DISABLE_LIVE_INPUT": "1"}):
            with self.assertRaises(
                interception_runtime_windows.InterceptionUnavailableError
            ):
                runtime.key_down(0x41)
        self.assertEqual(dll.sends, [])

    def test_key_down_and_up_send_scan_code_strokes(self):
        dll = _FakeInterceptionDll()
        runtime = self._runtime(dll)
        runtime.key_down(0x41)
        runtime.key_up(0x41)
        self.assertEqual(len(dll.sends), 2)
        self.assertEqual(dll.sends[0][0], interception_runtime_windows.keyboard_device(0))
        down = _decode_key_strokes(dll.sends[0][2], dll.sends[0][1])
        up = _decode_key_strokes(dll.sends[1][2], dll.sends[1][1])
        self.assertEqual(
            [(stroke.code, stroke.state) for stroke in down],
            [(0x1E, interception_runtime_windows.KEY_DOWN)],
        )
        self.assertEqual(
            [(stroke.code, stroke.state) for stroke in up],
            [(0x1E, interception_runtime_windows.KEY_UP)],
        )

    def test_extended_keys_combine_e0_into_state(self):
        dll = _FakeInterceptionDll()
        runtime = self._runtime(dll)
        runtime.key_down(0x26)
        runtime.key_up(0x26)
        down = _decode_key_strokes(dll.sends[0][2], dll.sends[0][1])
        up = _decode_key_strokes(dll.sends[1][2], dll.sends[1][1])
        self.assertEqual(
            [(stroke.code, stroke.state) for stroke in down],
            [
                (
                    0x48,
                    interception_runtime_windows.KEY_DOWN
                    | interception_runtime_windows.KEY_E0,
                )
            ],
        )
        self.assertEqual(
            [(stroke.code, stroke.state) for stroke in up],
            [
                (
                    0x48,
                    interception_runtime_windows.KEY_UP
                    | interception_runtime_windows.KEY_E0,
                )
            ],
        )

    def test_key_without_a_scan_code_fails_closed(self):
        dll = _FakeInterceptionDll()
        runtime = self._runtime(dll)
        with self.assertRaises(interception_runtime_windows.InterceptionUnavailableError):
            runtime.key_down(0x99)
        self.assertEqual(dll.sends, [])

    def test_short_send_is_reported(self):
        dll = _FakeInterceptionDll(send_result=0)
        runtime = self._runtime(dll)
        with self.assertRaises(interception_runtime_windows.InterceptionUnavailableError):
            runtime.key_down(0x41)

    def test_mouse_button_strokes_use_the_mouse_device(self):
        dll = _FakeInterceptionDll()
        runtime = self._runtime(dll)
        runtime.mouse_button("left", True)
        runtime.mouse_button("left", False)
        device, count, buffer = dll.sends[0]
        self.assertEqual(device, interception_runtime_windows.mouse_device(0))
        self.assertEqual(count, 1)
        stroke = ctypes.cast(
            buffer, ctypes.POINTER(interception_runtime_windows._MouseStroke)
        )[0]
        self.assertEqual(
            stroke.state, interception_runtime_windows.MOUSE_LEFT_BUTTON_DOWN
        )
        self.assertEqual(stroke.x, 0)
        self.assertEqual(stroke.y, 0)

    def test_unknown_mouse_button_is_rejected(self):
        runtime = self._runtime(_FakeInterceptionDll())
        with self.assertRaises(ValueError):
            runtime.mouse_button("thumb", True)

    def test_every_send_opens_and_releases_its_own_context(self):
        dll = _FakeInterceptionDll()
        runtime = self._runtime(dll)
        runtime.key_down(0x41)
        runtime.key_up(0x41)
        self.assertEqual(dll.create_calls, 2)
        self.assertEqual(dll.destroyed, [0x1234, 0x1234])

    def test_a_short_send_still_releases_the_context(self):
        dll = _FakeInterceptionDll(send_result=0)
        runtime = self._runtime(dll)
        with self.assertRaises(interception_runtime_windows.InterceptionUnavailableError):
            runtime.key_down(0x41)
        self.assertEqual(dll.destroyed, [0x1234])


class DriverProbeTests(unittest.TestCase):
    """``driver_installed`` must answer from the registry, never the device."""

    @staticmethod
    def _all_present(service_names=("keyboard", "mouse")):
        return (
            mock.patch.object(
                interception_runtime_windows,
                "_service_driver_file",
                side_effect=lambda n: (
                    "C:/Windows/System32/drivers/x.sys" if n in service_names else None
                ),
            ),
            mock.patch.object(
                interception_runtime_windows,
                "_class_filter_registered",
                return_value=True,
            ),
        )

    def test_complete_driver_means_installed(self):
        patches = self._all_present()
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.assertTrue(interception_runtime_windows.driver_installed())

    def test_service_without_driver_file_means_not_installed(self):
        patches = self._all_present(service_names=("keyboard",))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.assertFalse(interception_runtime_windows.driver_installed())

    def test_missing_class_filter_means_not_installed(self):
        with mock.patch.object(
            interception_runtime_windows,
            "_service_driver_file",
            return_value="C:/Windows/System32/drivers/x.sys",
        ), mock.patch.object(
            interception_runtime_windows,
            "_class_filter_registered",
            side_effect=lambda guid, name: name == "mouse",
        ):
            self.assertFalse(interception_runtime_windows.driver_installed())

    def test_nothing_registered_means_not_installed(self):
        with mock.patch.object(
            interception_runtime_windows, "_service_driver_file", return_value=None
        ), mock.patch.object(
            interception_runtime_windows, "_class_filter_registered", return_value=False
        ):
            self.assertFalse(interception_runtime_windows.driver_installed())

    @staticmethod
    def _service_key(value=..., error=None):
        key = mock.MagicMock()
        key.__enter__.return_value = key
        key.__exit__.return_value = False
        if error is not None:
            query = mock.patch.object(
                interception_runtime_windows.winreg, "QueryValueEx", side_effect=error
            )
        else:
            query = mock.patch.object(
                interception_runtime_windows.winreg, "QueryValueEx", return_value=(value, 1)
            )
        return mock.patch.object(
            interception_runtime_windows.winreg, "OpenKey", return_value=key
        ), query

    def test_service_without_image_path_value_uses_the_drivers_directory(self):
        """The official installer writes no ``ImagePath``; the inferred path counts."""

        open_key, query = self._service_key(error=FileNotFoundError)
        with open_key, query, mock.patch.dict(
            os.environ, {"SystemRoot": r"C:\Windows"}
        ), mock.patch.object(
            interception_runtime_windows.os.path, "isfile", return_value=True
        ) as isfile:
            self.assertEqual(
                interception_runtime_windows._service_driver_file("keyboard"),
                r"C:\Windows\System32\drivers\keyboard.sys",
            )
        isfile.assert_called_once_with(r"C:\Windows\System32\drivers\keyboard.sys")

    def test_service_without_image_path_value_still_requires_the_file(self):
        open_key, query = self._service_key(error=FileNotFoundError)
        with open_key, query, mock.patch.dict(
            os.environ, {"SystemRoot": r"C:\Windows"}
        ), mock.patch.object(
            interception_runtime_windows.os.path, "isfile", return_value=False
        ):
            self.assertIsNone(
                interception_runtime_windows._service_driver_file("keyboard")
            )

    def test_explicit_image_path_value_is_still_honoured(self):
        open_key, query = self._service_key(value=r"\SystemRoot\System32\drivers\keyboard.sys")
        with open_key, query, mock.patch.dict(
            os.environ, {"SystemRoot": r"C:\Windows"}
        ), mock.patch.object(
            interception_runtime_windows.os.path, "isfile", return_value=True
        ):
            self.assertEqual(
                interception_runtime_windows._service_driver_file("keyboard"),
                r"C:\Windows\System32\drivers\keyboard.sys",
            )

    def test_missing_service_key_is_not_installed(self):
        with mock.patch.object(
            interception_runtime_windows.winreg, "OpenKey", side_effect=FileNotFoundError
        ), mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}):
            self.assertIsNone(
                interception_runtime_windows._service_driver_file("keyboard")
            )

    def test_official_install_shape_is_reported_as_installed(self):
        """A service key with no ``ImagePath`` is a working install, not a broken one.

        That is the shape the official installer leaves behind; treating it as
        missing made a successful install look uninstalled.
        """

        with tempfile.TemporaryDirectory() as root:
            drivers = os.path.join(root, "System32", "drivers")
            os.makedirs(drivers)
            for name in ("keyboard", "mouse"):
                with open(os.path.join(drivers, f"{name}.sys"), "wb") as handle:
                    handle.write(b"\x00")
            open_key, query = self._service_key(error=FileNotFoundError)
            with open_key, query, mock.patch.dict(
                os.environ, {"SystemRoot": root}
            ), mock.patch.object(
                interception_runtime_windows, "_class_filter_registered", return_value=True
            ):
                self.assertTrue(interception_runtime_windows.driver_installed())

    def test_registered_but_not_running_driver_is_not_ready(self):
        with mock.patch.object(
            interception_runtime_windows, "driver_installed", return_value=True
        ), mock.patch.object(
            interception_runtime_windows, "driver_running", return_value=False
        ):
            self.assertFalse(interception_runtime_windows.driver_ready())

    def test_registered_but_not_running_reports_pending_reboot(self):
        dll = _FakeInterceptionDll()
        runtime = InterceptionRuntimeTests._runtime(dll, driver_probe=lambda: False)
        with mock.patch.dict(os.environ, {"RC003_DISABLE_LIVE_INPUT": "0"}), mock.patch.object(
            interception_runtime_windows, "driver_installed", return_value=True
        ), mock.patch.object(
            interception_runtime_windows, "memory_integrity_state", return_value=False
        ):
            self.assertFalse(runtime.is_available())
            self.assertIn("driver_pending_reboot", runtime.unavailable_reason())
            self.assertEqual(dll.create_calls, 0)

    def test_memory_integrity_does_not_block_a_running_driver(self):
        """HVCI is on, but the signed driver still loads, so it must stay usable."""

        with mock.patch.object(
            interception_runtime_windows, "driver_installed", return_value=True
        ), mock.patch.object(
            interception_runtime_windows, "driver_running", return_value=True
        ), mock.patch.object(
            interception_runtime_windows, "memory_integrity_state", return_value=True
        ):
            self.assertTrue(interception_runtime_windows.driver_ready())

    def test_driver_behind_memory_integrity_still_reports_pending_reboot(self):
        """A registered-but-not-ready driver reports pending reboot, never HVCI."""

        dll = _FakeInterceptionDll()
        runtime = InterceptionRuntimeTests._runtime(dll, driver_probe=lambda: False)
        with mock.patch.dict(os.environ, {"RC003_DISABLE_LIVE_INPUT": "0"}), mock.patch.object(
            interception_runtime_windows, "driver_installed", return_value=True
        ), mock.patch.object(
            interception_runtime_windows, "memory_integrity_state", return_value=True
        ):
            self.assertFalse(runtime.is_available())
            reason = runtime.unavailable_reason()
            self.assertIn("driver_pending_reboot", reason)
            self.assertNotIn("driver_blocked_by_memory_integrity", reason)
            self.assertEqual(dll.create_calls, 0)

    def test_class_filter_matches_names_case_insensitively(self):
        key = mock.MagicMock()
        key.__enter__.return_value = key
        key.__exit__.return_value = False
        with mock.patch.object(
            interception_runtime_windows.winreg, "OpenKey", return_value=key
        ), mock.patch.object(
            interception_runtime_windows.winreg,
            "QueryValueEx",
            return_value=(["Keyboard"], 7),
        ):
            self.assertTrue(
                interception_runtime_windows._class_filter_registered(
                    interception_runtime_windows._KEYBOARD_CLASS_GUID, "keyboard"
                )
            )
            self.assertFalse(
                interception_runtime_windows._class_filter_registered(
                    interception_runtime_windows._KEYBOARD_CLASS_GUID, "mouse"
                )
            )


class InjectionDeliveryTests(unittest.TestCase):
    def test_tokens_are_pressed_in_order_and_released_in_reverse(self):
        events = []
        runtime = mock.Mock()
        runtime.key_down.side_effect = lambda vk: events.append(("down", vk))
        runtime.key_up.side_effect = lambda vk: events.append(("up", vk))
        with mock.patch.object(interception_runtime_windows, "runtime", return_value=runtime):
            injection_backend.send_tokens_via_interception(("ctrl", "alt", "tab"))
        self.assertEqual(
            events,
            [
                ("down", 0x11),
                ("down", 0x12),
                ("down", 0x09),
                ("up", 0x09),
                ("up", 0x12),
                ("up", 0x11),
            ],
        )

    def test_mouse_click_presses_then_releases(self):
        runtime = mock.Mock()
        with mock.patch.object(interception_runtime_windows, "runtime", return_value=runtime):
            injection_backend.click_mouse_via_interception("right")
        self.assertEqual(
            runtime.mouse_button.call_args_list,
            [mock.call("right", True), mock.call("right", False)],
        )


class ButtonActionPersistenceTests(unittest.TestCase):
    def test_default_mode_is_not_written(self):
        action = key_mapping.ButtonAction(key_mapping.ActionKind.ESCAPE)
        self.assertEqual(action.to_dict(), {"kind": "escape", "keys": []})

    def test_interception_mode_round_trips(self):
        action = key_mapping.ButtonAction(
            key_mapping.ActionKind.ARROW_UP, inject_mode="interception"
        )
        payload = action.to_dict()
        self.assertEqual(payload["inject_mode"], "interception")
        self.assertEqual(key_mapping.ButtonAction.from_dict(payload), action)

    def test_unknown_stored_mode_falls_back_to_sendinput(self):
        action = key_mapping.ButtonAction.from_dict(
            {"kind": "escape", "keys": [], "inject_mode": "bogus"}
        )
        self.assertEqual(action.inject_mode, "sendinput")
        self.assertNotIn("inject_mode", action.to_dict())

    def test_legacy_mouse_wheel_mapping_keeps_the_mode(self):
        action = key_mapping.ButtonAction.from_dict(
            {"kind": "mouse_wheel_up", "keys": [], "inject_mode": "interception"}
        )
        self.assertEqual(action.kind, key_mapping.ActionKind.KEY_COMBO)
        self.assertEqual(action.keys, ("pageup",))
        self.assertEqual(action.inject_mode, "interception")


class ButtonActionInterceptionBranchTests(unittest.TestCase):
    """The app-level branch must translate errors instead of raising."""

    def setUp(self):
        holder = types.SimpleNamespace(
            _logger=logging.getLogger("test.injection_backend")
        )
        self.holder = holder
        self.apply = app_module.RC003App._apply_button_action_interception

    def test_successful_semantic_action(self):
        runtime = mock.Mock()
        with mock.patch.object(interception_runtime_windows, "runtime", return_value=runtime):
            result = self.apply(
                self.holder, key_mapping.ButtonAction(key_mapping.ActionKind.ESCAPE)
            )
        self.assertEqual(result, ("", ""))
        self.assertEqual(runtime.key_down.call_args_list, [mock.call(0x1B)])
        self.assertEqual(runtime.key_up.call_args_list, [mock.call(0x1B)])

    def test_unavailable_driver_is_reported(self):
        def failing(vk):
            raise interception_runtime_windows.InterceptionUnavailableError("gone")

        with mock.patch.object(
            interception_runtime_windows, "runtime", return_value=mock.Mock(key_down=failing)
        ):
            error, error_type = self.apply(
                self.holder, key_mapping.ButtonAction(key_mapping.ActionKind.ESCAPE)
            )
        self.assertEqual(error, "interception_unavailable")
        self.assertEqual(error_type, "InterceptionUnavailableError")

    def test_unsupported_action_is_reported_without_touching_the_driver(self):
        with mock.patch.object(interception_runtime_windows, "runtime") as runtime:
            error, error_type = self.apply(
                self.holder, key_mapping.ButtonAction(key_mapping.ActionKind.DISABLED)
            )
        runtime.assert_not_called()
        self.assertEqual(error, "interception_unsupported_action")
        self.assertEqual(error_type, "")

    def test_mouse_click_is_delivered(self):
        runtime = mock.Mock()
        with mock.patch.object(interception_runtime_windows, "runtime", return_value=runtime):
            result = self.apply(
                self.holder,
                key_mapping.ButtonAction(key_mapping.ActionKind.MOUSE_MIDDLE_CLICK),
            )
        self.assertEqual(result, ("", ""))
        self.assertEqual(
            runtime.mouse_button.call_args_list,
            [mock.call("middle", True), mock.call("middle", False)],
        )

    def test_oserror_is_reported_as_delivery_failure(self):
        runtime = mock.Mock()
        runtime.key_down.side_effect = OSError("boom")
        with mock.patch.object(interception_runtime_windows, "runtime", return_value=runtime):
            error, error_type = self.apply(
                self.holder, key_mapping.ButtonAction(key_mapping.ActionKind.ESCAPE)
            )
        self.assertEqual(error, "delivery_failed")
        self.assertEqual(error_type, "OSError")


@unittest.skipUnless(sys.platform == "win32", "requires Windows scan-code lookup")
class PhysicalScanCodeTests(unittest.TestCase):
    def test_letters_and_named_keys_resolve(self):
        self.assertEqual(win32_input.physical_scan_code(0x41), (0x1E, False))
        self.assertEqual(win32_input.physical_scan_code(0x26), (0x48, True))

    def test_multimedia_controls_resolve_to_their_e0_scan_codes(self):
        # Consumer controls keep their virtual-key SendInput delivery but also
        # report the E0-prefixed scan code a real keyboard emits, so the
        # Interception backend can reproduce them.
        for token, expected in (
            ("volume_up", (0x30, True)),
            ("volume_down", (0x2E, True)),
            ("volume_mute", (0x20, True)),
            ("media_play_pause", (0x22, True)),
        ):
            with self.subTest(token=token):
                vk = win32_keys.VK_CODES[token]
                self.assertIn(vk, win32_input._MULTIMEDIA_VK_CODES)
                self.assertEqual(win32_input.physical_scan_code(vk), expected)


class BackendDispatchTests(unittest.TestCase):
    def test_backend_for_selects_by_mode(self):
        self.assertIs(
            injection_backend.backend_for(injection_backend.INJECT_MODE_SENDINPUT),
            injection_backend._SENDINPUT_BACKEND,
        )
        self.assertIs(
            injection_backend.backend_for(injection_backend.INJECT_MODE_INTERCEPTION),
            injection_backend._INTERCEPTION_BACKEND,
        )
        self.assertIs(
            injection_backend.backend_for("bogus"),
            injection_backend._SENDINPUT_BACKEND,
        )

    def test_sendinput_backend_delegates_to_win32_input(self):
        backend = injection_backend.SendInputBackend()
        tokens = ("a",)
        with mock.patch.object(win32_input, "send_voice_key_combo_down") as down, \
                mock.patch.object(win32_input, "send_voice_key_combo_up") as up, \
                mock.patch.object(win32_input, "send_voice_key_combo_repeat") as repeat, \
                mock.patch.object(win32_input, "send_key_combo_tap") as tap, \
                mock.patch.object(win32_input, "send_mouse_button_click") as click:
            backend.key_down(tokens)
            backend.key_up(tokens)
            backend.key_repeat(tokens)
            backend.key_tap(tokens)
            backend.mouse_click("left")
        down.assert_called_once_with(tokens)
        up.assert_called_once_with(tokens)
        repeat.assert_called_once_with(tokens)
        tap.assert_called_once_with(tokens)
        click.assert_called_once_with("left")

    def test_interception_backend_delegates_to_driver_helpers(self):
        backend = injection_backend.InterceptionBackend()
        tokens = ("a",)
        with mock.patch.object(injection_backend, "send_hold_tokens_down") as down, \
                mock.patch.object(injection_backend, "send_hold_tokens_up") as up, \
                mock.patch.object(injection_backend, "send_hold_tokens_repeat") as repeat, \
                mock.patch.object(injection_backend, "send_tokens_via_interception") as tap, \
                mock.patch.object(injection_backend, "click_mouse_via_interception") as click:
            backend.key_down(tokens)
            backend.key_up(tokens)
            backend.key_repeat(tokens)
            backend.key_tap(tokens)
            backend.mouse_click("left")
        down.assert_called_once_with(tokens)
        up.assert_called_once_with(tokens)
        repeat.assert_called_once_with(tokens)
        tap.assert_called_once_with(tokens)
        click.assert_called_once_with("left")


if __name__ == "__main__":
    unittest.main()
