"""Windows execution helpers for semantic RC003 actions.

The reference project separates an action such as ``openCodex`` from a
recorded shortcut.  This module provides the corresponding Windows boundary:
application actions resolve a real installed executable and launch it, while
keyboard/system actions stay in :mod:`win32_input`.  The resolver is kept
dependency-injectable so the action contract can be tested without starting a
real application.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Sequence, Tuple

from . import key_mapping, running_apps_windows


Command = Tuple[str, ...]
Launcher = Callable[[Sequence[str]], object]
UriLauncher = Callable[[str], object]

_MISSING_COMMAND_CACHE_SECONDS = 30.0
_application_command_cache: Dict[
    key_mapping.ActionKind, Tuple[float, Optional[Command]]
] = {}
_application_command_cache_lock = threading.Lock()


# These are executable names rather than guessed window titles.  We resolve
# them through PATH and the normal per-user/system Windows program roots so a
# different install location does not break the mapping.
_APPLICATION_EXECUTABLES: Dict[key_mapping.ActionKind, Tuple[str, ...]] = {
    key_mapping.ActionKind.OPEN_CODEX: ("Codex.exe", "codex.exe"),
    key_mapping.ActionKind.OPEN_CLAUDE: ("Claude.exe", "claude.exe"),
    key_mapping.ActionKind.OPEN_CMUX: ("cmux.exe", "cmux"),
    key_mapping.ActionKind.OPEN_WECHAT: ("WeChat.exe", "Weixin.exe"),
    key_mapping.ActionKind.OPEN_CURSOR: ("Cursor.exe", "cursor.exe"),
    key_mapping.ActionKind.OPEN_SLACK: ("slack.exe", "Slack.exe"),
    key_mapping.ActionKind.OPEN_WECOM: ("WXWork.exe", "WeCom.exe"),
    key_mapping.ActionKind.OPEN_NETEASE_MUSIC: (
        "cloudmusic.exe",
        "CloudMusic.exe",
    ),
    key_mapping.ActionKind.OPEN_CHROME: ("chrome.exe", "Chrome.exe"),
    key_mapping.ActionKind.OPEN_EDGE: ("msedge.exe", "MicrosoftEdge.exe"),
    key_mapping.ActionKind.OPEN_ZED: ("Zed.exe", "zed.exe"),
}

_APPLICATION_SHORTCUT_NAMES: Dict[key_mapping.ActionKind, Tuple[str, ...]] = {
    key_mapping.ActionKind.OPEN_CODEX: ("Codex",),
    key_mapping.ActionKind.OPEN_CLAUDE: ("Claude",),
    key_mapping.ActionKind.OPEN_CMUX: ("cmux",),
    key_mapping.ActionKind.OPEN_WECHAT: ("微信", "WeChat", "Weixin"),
    key_mapping.ActionKind.OPEN_CURSOR: ("Cursor",),
    key_mapping.ActionKind.OPEN_SLACK: ("Slack",),
    key_mapping.ActionKind.OPEN_WECOM: ("企业微信", "WeCom", "WXWork"),
    key_mapping.ActionKind.OPEN_NETEASE_MUSIC: ("网易云音乐", "NetEase Cloud Music"),
    key_mapping.ActionKind.OPEN_CHROME: ("Google Chrome", "Chrome"),
    key_mapping.ActionKind.OPEN_EDGE: ("Microsoft Edge", "Edge"),
    key_mapping.ActionKind.OPEN_ZED: ("Zed",),
}


def is_application_action(action: key_mapping.ButtonAction) -> bool:
    return action.kind in key_mapping.APPLICATION_ACTIONS


def _windows_roots() -> Iterable[Path]:
    # Preserve order: per-user installs are the most common for the desktop
    # tools named in the reference repository, followed by machine installs.
    seen = set()
    for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        raw = os.environ.get(variable, "").strip()
        if not raw:
            continue
        root = Path(raw)
        if root not in seen:
            seen.add(root)
            yield root


def _candidate_paths(executable_names: Sequence[str]) -> Iterable[Path]:
    # ``where``/PATH is the least opinionated lookup and also covers package
    # managers that intentionally install outside the usual roots.
    for executable_name in executable_names:
        resolved = shutil.which(executable_name)
        if resolved:
            yield Path(resolved)

    common_relative_directories = (
        Path("Programs"),
        Path("Programs", "Common"),
        Path("Google", "Chrome", "Application"),
        Path("Microsoft", "Edge", "Application"),
        Path("Tencent", "WeChat"),
        Path("Tencent", "WeCom"),
        Path("WXWork"),
        Path("Netease", "CloudMusic"),
    )
    for root in _windows_roots():
        for directory in common_relative_directories:
            for executable_name in executable_names:
                yield root / directory / executable_name


def _start_menu_shortcuts(names: Sequence[str]) -> Iterable[Path]:
    roots = []
    for variable in ("APPDATA", "PROGRAMDATA"):
        raw = os.environ.get(variable, "").strip()
        if not raw:
            continue
        roots.append(
            Path(raw) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        )
    wanted = tuple(name.casefold() for name in names)
    for root in roots:
        if not root.is_dir():
            continue
        try:
            shortcuts = root.rglob("*.lnk")
            for shortcut in shortcuts:
                stem = shortcut.stem.casefold()
                # A substring also matches uninstallers, updaters and other
                # applications. Only the known product names/aliases may launch.
                if stem in wanted:
                    yield shortcut
        except OSError:
            continue


def _path_is_file(path: Path) -> bool:
    return path.is_file()


def _is_agent_cli_path(kind: key_mapping.ActionKind, path: Path) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    if kind == key_mapping.ActionKind.OPEN_CODEX:
        return "openai" in parts and "codex" in parts and "bin" in parts
    if kind == key_mapping.ActionKind.OPEN_CLAUDE:
        return len(parts) >= 3 and parts[-3:-1] == (".local", "bin")
    return False


def _packaged_desktop_command(kind: key_mapping.ActionKind) -> Optional[Command]:
    # Store apps need their registered AppID. Their embedded codex.exe or
    # claude.exe may be a command-line tool rather than a desktop launcher.
    names = {
        key_mapping.ActionKind.OPEN_CODEX: "Codex",
        key_mapping.ActionKind.OPEN_CLAUDE: "Claude",
    }
    name = names.get(kind)
    if name is None or sys.platform != "win32":
        return None
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | Where-Object { $_.Name -eq 'Codex' -or $_.Name -eq 'Claude' } | "
             "Select-Object Name,AppID | ConvertTo-Json -Compress"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        entries = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("Name") != name:
            continue
        app_id = entry.get("AppID")
        if (isinstance(app_id, str) and "!" in app_id
                and not any(char in app_id for char in "\r\n\x00")):
            explorer = Path(os.environ.get("WINDIR", r"C:\Windows")) / "explorer.exe"
            if explorer.is_file():
                return (str(explorer), "shell:AppsFolder\\" + app_id)
    return None


def _resolve_generic_application_command(
    target: str,
    *,
    executable_exists: Callable[[Path], bool],
) -> Optional[Command]:
    """Resolve the program the user picked for a generic 「启动应用」 action.

    ``target`` is either a process name (``chrome.exe``) or a full executable
    path chosen through the file browser.
    """

    try:
        text = key_mapping.normalize_application_target(target)
    except (TypeError, ValueError):
        return None
    path = Path(text)
    if path.is_absolute():
        return (str(path),) if executable_exists(path) else None
    resolved = running_apps_windows.executable_for_process(text)
    if resolved:
        return (resolved,)
    executable_names = (text,) if path.suffix else (text + ".exe", text)
    for candidate in _candidate_paths(executable_names):
        if executable_exists(candidate):
            return (str(candidate),)
    shortcut_name = path.stem if path.suffix else text
    for shortcut in _start_menu_shortcuts((shortcut_name,)):
        if executable_exists(shortcut):
            return (str(shortcut),)
    return None


def _resolve_application_command_uncached(
    action: key_mapping.ButtonAction,
    *,
    executable_exists: Callable[[Path], bool],
) -> Optional[Command]:
    """Resolve an application action to an executable command.

    ``open_remote_mic`` reuses this EXE and opens the settings window.
    ``open_application`` launches whatever program the user picked.  Other
    actions are resolved by executable name.  No path or device identity is
    persisted in the config file.
    """

    if action.kind == key_mapping.ActionKind.OPEN_REMOTE_MIC:
        executable = Path(sys.executable)
        if getattr(sys, "frozen", False) and executable_exists(executable):
            return (str(executable), "--settings")
        return None

    if action.kind == key_mapping.ActionKind.OPEN_APPLICATION:
        return _resolve_generic_application_command(
            action.target,
            executable_exists=executable_exists,
        )

    names = _APPLICATION_EXECUTABLES.get(action.kind)
    if not names:
        return None
    if action.kind in {
        key_mapping.ActionKind.OPEN_CODEX,
        key_mapping.ActionKind.OPEN_CLAUDE,
    }:
        for shortcut in _start_menu_shortcuts(
            _APPLICATION_SHORTCUT_NAMES.get(action.kind, ())
        ):
            if executable_exists(shortcut):
                return (str(shortcut),)
    for candidate in _candidate_paths(names):
        if not _is_agent_cli_path(action.kind, candidate) and executable_exists(candidate):
            return (str(candidate),)
    for shortcut in _start_menu_shortcuts(_APPLICATION_SHORTCUT_NAMES.get(action.kind, ())):
        if executable_exists(shortcut):
            return (str(shortcut),)
    if action.kind in {
        key_mapping.ActionKind.OPEN_CODEX,
        key_mapping.ActionKind.OPEN_CLAUDE,
    }:
        return _packaged_desktop_command(action.kind)
    return None


def clear_application_command_cache(
    action_kind: Optional[key_mapping.ActionKind] = None,
) -> None:
    with _application_command_cache_lock:
        if action_kind is None:
            _application_command_cache.clear()
        else:
            _application_command_cache.pop(action_kind, None)


def resolve_application_command(
    action: key_mapping.ButtonAction,
    *,
    executable_exists: Callable[[Path], bool] = _path_is_file,
) -> Optional[Command]:
    """Resolve an application action with a process-local install cache."""

    if executable_exists is not _path_is_file:
        return _resolve_application_command_uncached(
            action,
            executable_exists=executable_exists,
        )

    # A generic 启动应用 action carries its own target, so the kind-keyed
    # install cache would hand one program's command to another. Resolve it
    # every time instead.
    if action.kind == key_mapping.ActionKind.OPEN_APPLICATION:
        return _resolve_application_command_uncached(
            action,
            executable_exists=_path_is_file,
        )

    now = time.monotonic()
    with _application_command_cache_lock:
        cached = _application_command_cache.get(action.kind)
    if cached is not None:
        cached_at, command = cached
        if command is not None:
            if _path_is_file(Path(command[0])):
                return command
            clear_application_command_cache(action.kind)
        elif now - cached_at < _MISSING_COMMAND_CACHE_SECONDS:
            return None

    command = _resolve_application_command_uncached(
        action,
        executable_exists=_path_is_file,
    )
    with _application_command_cache_lock:
        _application_command_cache[action.kind] = (now, command)
    return command


def open_configured_application(
    action: key_mapping.ButtonAction,
    *,
    launcher: Optional[Launcher] = None,
) -> bool:
    """Launch one configured application action and report whether it started."""

    command = resolve_application_command(action)
    if command is None:
        return False
    starter = launcher or (
        _launch_cmux_command
        if action.kind == key_mapping.ActionKind.OPEN_CMUX
        else _launch_command
    )
    try:
        starter(command)
    except Exception:
        clear_application_command_cache(action.kind)
        raise
    return True


def open_quicker_uri(
    action: key_mapping.ButtonAction,
    *,
    launcher: Optional[UriLauncher] = None,
) -> bool:
    """Open one validated Quicker action URI through the Windows handler."""

    if action.kind != key_mapping.ActionKind.QUICKER_URI:
        return False
    uri = key_mapping.normalize_quicker_uri(action.uri)
    starter = launcher or _launch_uri
    starter(uri)
    return True


def _launch_uri(uri: str) -> None:
    if sys.platform != "win32" or not hasattr(os, "startfile"):
        raise OSError("Windows URI protocol launch is unavailable")
    # Passing the URI directly to Windows' registered protocol handler avoids
    # cmd.exe/PowerShell reinterpreting action parameters as shell syntax.
    os.startfile(uri)  # type: ignore[attr-defined]


def _launch_command(command: Sequence[str]) -> None:
    if len(command) == 1 and Path(command[0]).suffix.casefold() == ".lnk":
        os.startfile(command[0])  # type: ignore[attr-defined]
        return
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=(sys.platform != "win32"),
        creationflags=creation_flags,
    )


_WM_CLOSE = 0x0010
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _post_close_message(hwnd: int) -> bool:
    """Ask one top-level window to close (WM_CLOSE), keeping unsaved state."""

    if sys.platform != "win32":
        raise OSError("window messages are unavailable")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    return bool(user32.PostMessageW(wintypes.HWND(hwnd), _WM_CLOSE, 0, 0))


def _foreground_window() -> int:
    if sys.platform != "win32":
        raise OSError("window messages are unavailable")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    return int(user32.GetForegroundWindow() or 0)


def _process_image_path(pid: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
    )
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def _close_target_matches(target: str, image_path: str) -> bool:
    if not image_path:
        return False
    wanted = str(target or "").strip().strip('"')
    if not wanted:
        return False
    if any(separator in wanted for separator in ("/", "\\")):
        return os.path.normcase(os.path.abspath(wanted)) == os.path.normcase(
            os.path.abspath(image_path)
        )
    name = Path(wanted).name.casefold()
    if not name.endswith(".exe"):
        name += ".exe"
    return Path(image_path).name.casefold() == name


def _windows_for_target(target: str) -> Tuple[int, ...]:
    """Return every visible top-level window owned by the matching process."""

    if sys.platform != "win32":
        raise OSError("window enumeration is unavailable")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]

    matches = []

    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _close_target_matches(target, _process_image_path(int(pid.value))):
            matches.append(int(hwnd))
        return True

    callback = callback_type(visit)
    user32.EnumWindows(callback, 0)
    return tuple(matches)


def close_configured_application(
    action: key_mapping.ButtonAction,
    *,
    foreground_window: Optional[Callable[[], int]] = None,
    windows_for_target: Optional[Callable[[str], Iterable[int]]] = None,
    post_close: Optional[Callable[[int], bool]] = None,
) -> bool:
    """Close the configured program (or the current foreground app).

    Only ``WM_CLOSE`` is posted, so applications still get to save their work
    instead of being terminated behind the user's back.
    """

    if action.kind != key_mapping.ActionKind.CLOSE_APPLICATION:
        return False
    sender = post_close or _post_close_message
    try:
        if action.close_mode == key_mapping.CLOSE_MODE_CURRENT:
            getter = foreground_window or _foreground_window
            hwnd = int(getter())
            return bool(hwnd) and bool(sender(hwnd))
        enumerator = windows_for_target or _windows_for_target
        handles = tuple(enumerator(action.target))
    except Exception:
        return False
    closed = False
    for hwnd in handles:
        try:
            closed = bool(sender(int(hwnd))) or closed
        except Exception:
            continue
    return closed


def _launch_cmux_command(command: Sequence[str]) -> None:
    # The Windows cmux build is a terminal program. It needs its own visible
    # console and standard handles; _launch_command would discard its UI.
    if len(command) == 1 and Path(command[0]).suffix.casefold() == ".lnk":
        os.startfile(command[0])  # type: ignore[attr-defined]
        return
    subprocess.Popen(
        list(command),
        close_fds=(sys.platform != "win32"),
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        if sys.platform == "win32" else 0,
    )
