"""User-triggered one-time install/uninstall of the Interception kernel driver.

The optional Interception injection backend (see
:mod:`interception_runtime_windows`) needs the Interception kernel driver
installed machine-wide.  That is an administrator action followed by a reboot;
it is never something this application does silently.  This module only ever
runs from an explicit user click in the settings window, and it delegates the
whole thing to the official ``install-interception.exe`` that ships inside the
very same pinned Interception release archive this project already fetches for
the client DLL (see ``build/fetch-interception.ps1``).

Contract (mirrors :mod:`vb_cable_bundle`'s vendor-launch contract):

- The official installer is launched through Windows' own ``runas``/UAC verb,
  so the desktop process itself never elevates and never becomes an
  administrator.  The elevated command is ``cmd.exe`` only to redirect the
  installer's console output into a log file; it runs exactly the pinned
  installer with one fixed argument (``/install`` or ``/uninstall``).
- The installer binary is verified against the pinned SHA-256 before launch, so
  a swapped or tampered file fails closed instead of running elevated.
- The elevated installer is awaited, so a failed install is reported instead of
  looking like a success.  A zero exit code is then cross-checked against the
  registry: every piece of the driver (both services with their files, and both
  device-class filters) must be present, or the call fails loudly.
- The driver only takes effect after a reboot, so success still asks the user to
  reboot; it is never reported as "usable right now".
- Installing while Memory Integrity (HVCI) is on is refused outright: the 2018
  driver cannot load behind HVCI, and a registered filter it cannot drive leaves
  the keyboard dead after the next reboot.  Uninstalling is never refused, since
  that is the way back.
- Each run leaves a log under ``%LOCALAPPDATA%\\MoteDeck\\logs`` with the
  installer output and a summary of the driver state, and failure messages point
  at that log.

``RC003_INTERCEPTION_INSTALLER`` overrides path discovery (used by tests and by
source-only runs).
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import tempfile
import time
from typing import Callable, Dict, List, Optional

INSTALLER_ENV_VAR = "RC003_INTERCEPTION_INSTALLER"
DEFAULT_INSTALLER_NAMES = ("install-interception.exe",)
# Pinned to the official Interception 1.0.1 release archive already pinned for
# interception.dll (see build/fetch-interception.ps1 and THIRD_PARTY_NOTICES.md).
# A different file under the same name fails closed rather than running
# elevated; updating this pin requires a reviewed change, never a silent edit.
INSTALLER_SHA256 = "e137863a79da797f08e7a137280ff2a123809044a888fd75ce9c973198915abe"

INSTALL_ARGUMENT = "/install"
UNINSTALL_ARGUMENT = "/uninstall"

_READ_CHUNK_SIZE = 1024 * 1024

# The elevated installer is a console program that copies the drivers and
# writes their service keys, then exits.  It finishes in well under a second;
# the timeout only covers a stalled process, never a slow user.
INSTALLER_WAIT_TIMEOUT_SECONDS = 180.0

# SE_ERR_ACCESSDENIED is what Windows reports for a declined UAC prompt; 1223
# is the equivalent GetLastError() value ShellExecuteEx reports (see
# hid_source_injector.py) and is accepted here too.
_SE_ERR_ACCESSDENIED = 5
_UAC_CANCELLED = 1223

_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x00000102


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_uint32),
        ("hIconOrMonitor", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


class DriverInstallerError(RuntimeError):
    """Raised when the bundled Interception installer cannot be used."""


class DriverInstallCancelledError(DriverInstallerError):
    """Raised when the user declined the UAC elevation prompt."""


def _source_tree_asset_dirs() -> List[str]:
    """Return the ignored third-party folder used by source-only runs.

    Mirrors :func:`interception_runtime_windows._source_tree_asset_dirs`: a
    frozen build finds the installer under ``sys._MEIPASS``, while a source
    checkout keeps it in ``build/third_party/interception``.
    """

    if getattr(sys, "frozen", False):
        return []
    module_dir = os.path.dirname(os.path.abspath(__file__))
    return [
        os.path.normpath(
            os.path.join(
                module_dir,
                os.pardir,
                os.pardir,
                "build",
                "third_party",
                "interception",
            )
        )
    ]


def _installer_candidate_paths() -> List[str]:
    """Return the installer locations to try, most specific first.

    Mirrors :func:`interception_runtime_windows._dll_candidate_paths`: the
    environment override wins outright, otherwise the file is looked up next to
    the frozen payload (``sys._MEIPASS``), next to the executable, and next to
    this module.
    """

    override = os.environ.get(INSTALLER_ENV_VAR, "").strip()
    if override:
        return [override]
    search_dirs: List[str] = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        search_dirs.append(str(meipass))
    if getattr(sys, "frozen", False):
        search_dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
    search_dirs.append(os.path.dirname(os.path.abspath(__file__)))
    search_dirs.extend(_source_tree_asset_dirs())
    candidates: List[str] = []
    for directory in search_dirs:
        for name in DEFAULT_INSTALLER_NAMES:
            path = os.path.join(directory, name)
            if path not in candidates:
                candidates.append(path)
    return candidates


def installer_path() -> Optional[str]:
    """Return the first existing installer path, or ``None``."""

    for candidate in _installer_candidate_paths():
        if os.path.isfile(candidate):
            return candidate
    return None


def installer_available() -> bool:
    """Whether this build ships the official Interception installer."""

    return installer_path() is not None


def installer_unavailable_reason() -> str:
    """Return a human-readable reason the installer cannot be used."""

    if installer_available():
        return ""
    return (
        "本次构建未包含随包的官方安装程序；请使用 "
        "build/fetch-interception.ps1 重新构建（installer_not_found）"
    )


def _verify_installer(path: str) -> None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(_READ_CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError as exc:
        raise DriverInstallerError(f"无法读取 Interception 安装程序：{exc}") from exc
    if digest.hexdigest() != INSTALLER_SHA256:
        raise DriverInstallerError(
            "Interception 安装程序哈希与固定值不匹配，已拒绝运行；"
            "请重新构建或重新下载官方安装包"
        )


def _log_path(argument: str) -> str:
    base = os.environ.get("LOCALAPPDATA", "").strip() or tempfile.gettempdir()
    name = "install" if argument == INSTALL_ARGUMENT else "uninstall"
    return os.path.join(base, "MoteDeck", "logs", f"interception-{name}.log")


def _elevated_parameters(path: str, arguments: str, log_path: str) -> str:
    """Build the ``cmd.exe /c`` command line that runs the pinned installer.

    ``cmd`` is used only to send the installer's console output to ``log_path``.
    ``path`` is the verified installer and ``arguments`` is one of the two
    documented constants, so nothing caller-supplied reaches the command line.
    """

    return f'/d /c ""{path}" {arguments} >> "{log_path}" 2>&1"'


def _default_launch_elevated(path: str, arguments: str, log_path: str) -> int:
    """Run the installer elevated with ``arguments`` and wait for it to exit.

    No click automation, no UAC bypass, no silent-install flag beyond the
    installer's own documented ``/install`` or ``/uninstall`` argument.  The
    returned value is the installer's own exit code, so a failed install can be
    reported instead of being mistaken for a successful launch.
    """

    if sys.platform != "win32":
        raise DriverInstallerError(
            "安装 Interception 驱动需要 Windows（ShellExecute 'runas'）"
        )
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = (ctypes.POINTER(_ShellExecuteInfo),)
    shell32.ShellExecuteExW.restype = ctypes.c_int
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.GetExitCodeProcess.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    )
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int

    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = os.path.join(system_root, "System32", "cmd.exe")
    info.lpParameters = _elevated_parameters(path, arguments, log_path)
    info.lpDirectory = os.path.dirname(path)
    info.nShow = 0
    ctypes.set_last_error(0)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        code = int(ctypes.get_last_error())
        if code in (_SE_ERR_ACCESSDENIED, _UAC_CANCELLED):
            raise DriverInstallCancelledError(
                "用户取消了 UAC 提升请求；未安装或卸载任何驱动。"
            )
        raise DriverInstallerError(
            f"启动 Interception 安装程序失败（ShellExecute 错误码 {code}）"
        )
    if not info.hProcess:
        raise DriverInstallerError(
            "Interception 安装程序已启动，但没有返回进程句柄，无法确认安装结果"
        )
    try:
        timeout_ms = int(INSTALLER_WAIT_TIMEOUT_SECONDS * 1000)
        waited = int(kernel32.WaitForSingleObject(info.hProcess, timeout_ms))
        if waited == _WAIT_TIMEOUT:
            raise DriverInstallerError(
                "等待 Interception 安装程序结束超时；它可能仍在运行，"
                "请等待其结束后重启电脑再检查"
            )
        if waited != _WAIT_OBJECT_0:
            raise DriverInstallerError(
                f"等待 Interception 安装程序结束失败（WaitForSingleObject={waited}）"
            )
        exit_code = ctypes.c_uint32()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            raise DriverInstallerError("无法读取 Interception 安装程序的退出码")
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def _driver_state() -> Dict[str, Optional[bool]]:
    from . import interception_runtime_windows as runtime

    return {
        "installed": runtime.driver_installed(),
        "running": runtime.driver_running(),
        "hvci": runtime.memory_integrity_state(),
    }


def _hvci_text(hvci: Optional[bool]) -> str:
    if hvci is True:
        return "内存完整性（内核隔离）当前为开启（该驱动已签名，不受其影响）"
    if hvci is False:
        return "内存完整性（内核隔离）当前为关闭"
    return "无法读取内存完整性（内核隔离）状态"


def _start_log(log_path: str, installer: str, argument: str) -> None:
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(
                "MoteDeck Interception 驱动安装日志\n"
                f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"安装程序: {installer}\n"
                f"参数: {argument}\n"
            )
    except OSError as exc:
        raise DriverInstallerError(
            f"无法创建安装日志 {log_path}：{exc}；为保证可追溯，已取消安装"
        ) from exc


def _append_summary(
    log_path: str,
    argument: str,
    exit_code: int,
    state: Dict[str, Optional[bool]],
) -> None:
    # Best effort: the exit code and the registry state above already decide the
    # outcome, so a summary that cannot be written must not change it.
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(
                "--- 汇总 ---\n"
                f"参数: {argument}\n"
                f"退出码: {exit_code}\n"
                f"驱动已注册: {state['installed']}\n"
                f"驱动服务运行中: {state['running']}\n"
                f"内存完整性(HVCI): {state['hvci']}\n"
            )
    except OSError:
        pass


def _run(
    argument: str,
    *,
    _launch: Callable[[str, str, str], int] = _default_launch_elevated,
) -> int:
    path = installer_path()
    if path is None:
        raise DriverInstallerError(
            "未找到随包的 Interception 安装程序；该构建未经过 "
            "build/fetch-interception.ps1"
        )
    _verify_installer(path)
    verb = "安装" if argument == INSTALL_ARGUMENT else "卸载"
    log_path = _log_path(argument)
    _start_log(log_path, path, argument)
    exit_code = int(_launch(path, argument, log_path) or 0)
    state = _driver_state()
    _append_summary(log_path, argument, exit_code, state)
    if exit_code:
        raise DriverInstallerError(
            f"Interception 安装程序返回错误码 {exit_code}，驱动{verb}未完成。"
            f"{_hvci_text(state['hvci'])}。详细记录：{log_path}"
        )
    if argument == INSTALL_ARGUMENT and not state["installed"]:
        # A zero exit code alone is not proof: security software or Windows'
        # memory integrity can stop the filter from registering, and that must
        # not be reported as a successful install.
        raise DriverInstallerError(
            "安装程序已结束（退出码 0），但注册表中仍未发现完整的 Interception "
            "键盘/鼠标过滤驱动（需要 keyboard、mouse 两个服务及对应设备类过滤器）。"
            f"{_hvci_text(state['hvci'])}。详细记录：{log_path}"
        )
    return exit_code


def install_driver(
    *, _launch: Callable[[str, str, str], int] = _default_launch_elevated
) -> int:
    """Install the kernel driver through the official elevated installer."""

    return _run(INSTALL_ARGUMENT, _launch=_launch)


def uninstall_driver(
    *, _launch: Callable[[str, str, str], int] = _default_launch_elevated
) -> int:
    """Remove the kernel driver through the official elevated installer."""

    return _run(UNINSTALL_ARGUMENT, _launch=_launch)
