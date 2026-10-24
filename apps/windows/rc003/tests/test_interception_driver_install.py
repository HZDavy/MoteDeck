"""Contract tests for the bundled Interception driver installer launcher.

The elevated installer itself is never executed here: ``_launch`` is injected
and the driver-state probes are patched, so these tests only pin how the module
interprets the installer's outcome and what it records in the log.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from ovb_rc003 import interception_driver_install as install

_RUNTIME = "ovb_rc003.interception_runtime_windows"


class _FakeLaunch:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, path: str, arguments: str, log_path: str) -> int:
        self.calls.append((path, arguments, log_path))
        return self.exit_code


class DriverInstallerOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._log_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._log_dir.cleanup)
        log_file = os.path.join(self._log_dir.name, "interception-test.log")
        patches = [
            mock.patch.object(
                install, "installer_path", return_value="C:/fake/install-interception.exe"
            ),
            mock.patch.object(install, "_verify_installer"),
            mock.patch.object(install, "_log_path", return_value=log_file),
        ]
        for patch in patches:
            self.addCleanup(patch.stop)
            patch.start()
        self.log_file = log_file

    def _state(self, *, installed: bool, running: bool = True, hvci=False):
        return [
            mock.patch(f"{_RUNTIME}.driver_installed", return_value=installed),
            mock.patch(f"{_RUNTIME}.driver_running", return_value=running),
            mock.patch(f"{_RUNTIME}.memory_integrity_state", return_value=hvci),
        ]

    def _with_state(self, **kwargs):
        patches = self._state(**kwargs)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_install_passes_documented_argument_and_reports_success(self):
        self._with_state(installed=True)
        launch = _FakeLaunch(0)
        self.assertEqual(install.install_driver(_launch=launch), 0)
        self.assertEqual(
            launch.calls,
            [("C:/fake/install-interception.exe", install.INSTALL_ARGUMENT, self.log_file)],
        )

    def test_nonzero_exit_code_is_reported_as_failure(self):
        self._with_state(installed=False, hvci=True)
        launch = _FakeLaunch(2)
        with self.assertRaises(install.DriverInstallerError) as raised:
            install.uninstall_driver(_launch=launch)
        message = str(raised.exception)
        self.assertIn("2", message)
        self.assertIn("内存完整性", message)
        self.assertIn(self.log_file, message)

    def test_zero_exit_without_registered_driver_is_a_failure(self):
        self._with_state(installed=False, hvci=None)
        launch = _FakeLaunch(0)
        with self.assertRaises(install.DriverInstallerError) as raised:
            install.install_driver(_launch=launch)
        message = str(raised.exception)
        self.assertIn("注册表", message)
        self.assertIn("无法读取内存完整性", message)
        self.assertIn(self.log_file, message)

    def test_cancelled_elevation_propagates(self):
        self._with_state(installed=False, hvci=False)

        def launch(path: str, arguments: str, log_path: str) -> int:
            raise install.DriverInstallCancelledError("cancelled")

        with self.assertRaises(install.DriverInstallCancelledError):
            install.install_driver(_launch=launch)

    def test_summary_is_appended_to_the_log(self):
        self._with_state(installed=True, running=False, hvci=False)
        install.install_driver(_launch=_FakeLaunch(0))
        with open(self.log_file, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("--- 汇总 ---", text)
        self.assertIn("退出码: 0", text)
        self.assertIn("驱动已注册: True", text)
        self.assertIn("驱动服务运行中: False", text)
        self.assertIn("内存完整性(HVCI): False", text)

    def test_uninstall_is_still_allowed_while_memory_integrity_is_on(self):
        """Uninstalling is the way back, so HVCI must never block it."""

        self._with_state(installed=True, running=True, hvci=True)
        launch = _FakeLaunch(0)
        self.assertEqual(install.uninstall_driver(_launch=launch), 0)
        self.assertEqual(
            launch.calls,
            [
                (
                    "C:/fake/install-interception.exe",
                    install.UNINSTALL_ARGUMENT,
                    self.log_file,
                )
            ],
        )

    def test_elevated_parameters_wrap_only_the_pinned_installer(self):
        parameters = install._elevated_parameters(
            "C:/fake/install-interception.exe", install.INSTALL_ARGUMENT, "C:/log.txt"
        )
        self.assertEqual(
            parameters,
            '/d /c ""C:/fake/install-interception.exe" /install >> "C:/log.txt" 2>&1"',
        )


class InstallerDiscoveryTests(unittest.TestCase):
    """The installer lookup must cover a source checkout as well as a frozen build."""

    def test_source_tree_third_party_folder_is_searched(self):
        module_dir = os.path.dirname(os.path.abspath(install.__file__))
        expected = os.path.join(
            os.path.normpath(os.path.join(module_dir, os.pardir, os.pardir)),
            "build",
            "third_party",
            "interception",
            "install-interception.exe",
        )
        self.assertIn(expected, install._installer_candidate_paths())

    def test_environment_override_wins_outright(self):
        with mock.patch.dict(
            os.environ,
            {install.INSTALLER_ENV_VAR: "D:/custom/install-interception.exe"},
        ):
            self.assertEqual(
                install._installer_candidate_paths(),
                ["D:/custom/install-interception.exe"],
            )

    def test_frozen_build_never_searches_the_source_tree(self):
        with mock.patch.object(install.sys, "frozen", True, create=True):
            self.assertEqual(install._source_tree_asset_dirs(), [])


if __name__ == "__main__":
    unittest.main()
