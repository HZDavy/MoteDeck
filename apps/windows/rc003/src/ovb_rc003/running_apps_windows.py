"""List the applications that currently own a visible window.

The key-mapping UI lets the user bind a remote button layout to one program.
Picking from a live list is friendlier than typing an executable name, so this
module enumerates visible top-level windows and resolves each owning process to
its executable path, process name (basename) and best-effort friendly name.

Everything is best effort: on any failure the offending row is skipped and a
non-Windows platform returns an empty list. Nothing here raises for the caller.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_EXECUTABLE_CACHE_NAME = "app-executables.json"


def _query_image_path(kernel32, pid: int) -> str:
    """Full executable path of a process id, or "" when it cannot be read."""
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def _file_description(version_api, executable: str) -> str:
    """The Windows "File description" of an executable, or "" when absent."""
    if not executable:
        return ""
    try:
        ignored = wintypes.DWORD(0)
        size = version_api.GetFileVersionInfoSizeW(executable, ctypes.byref(ignored))
        if not size:
            return ""
        block = ctypes.create_string_buffer(size)
        if not version_api.GetFileVersionInfoW(executable, 0, size, block):
            return ""
        value = ctypes.c_void_p()
        length = wintypes.UINT(0)
        if not version_api.VerQueryValueW(block, "\\VarFileInfo\\Translation",
                                          ctypes.byref(value), ctypes.byref(length)):
            return ""
        if not length.value:
            return ""
        language, codepage = ctypes.cast(value, ctypes.POINTER(wintypes.WORD))[:2]
        pointer = ctypes.c_void_p()
        sub_block = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\FileDescription"
        if not version_api.VerQueryValueW(block, sub_block, ctypes.byref(pointer),
                                          ctypes.byref(length)):
            return ""
        if not length.value or not pointer.value:
            return ""
        # VerQueryValueW reports a length that includes the terminating NUL (and
        # is measured in the buffer's own units), so cut the string at the first
        # NUL instead of trusting the count. A stray NUL would otherwise be
        # stored verbatim in the saved preset name.
        return ctypes.wstring_at(pointer, length.value).split("\x00", 1)[0].strip()
    except (OSError, ValueError):
        return ""


def process_name_from_path(path: str) -> str:
    """Basename of an executable path, lower-cased for stable comparisons."""
    if not path:
        return ""
    return os.path.basename(str(path).strip().strip('"')).strip()


def file_description(executable: str) -> str:
    """Windows "File description" of an executable path, or "" when absent."""
    if sys.platform != "win32" or not executable:
        return ""
    try:
        version_api = ctypes.WinDLL("version", use_last_error=True)
    except OSError:
        return ""
    return _file_description(version_api, executable)


def _registry_executable(name: str) -> str:
    """Installed path of a process name from the registry's App Paths keys."""
    if sys.platform != "win32" or not name:
        return ""
    import winreg

    sub_keys = (
        f"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\{name}",
        f"SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\App Paths\\{name}",
    )
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub_key in sub_keys:
            try:
                with winreg.OpenKey(hive, sub_key) as handle:
                    value, _kind = winreg.QueryValueEx(handle, None)
            except OSError:
                continue
            path = str(value).strip().strip('"')
            if path and Path(path).is_file():
                return path
    return ""


def _executable_cache_path(cache_root) -> Path | None:
    if not cache_root:
        return None
    try:
        return Path(cache_root) / _EXECUTABLE_CACHE_NAME
    except (TypeError, ValueError):
        return None


def _read_executable_cache(cache_root) -> dict:
    """Persisted process name (casefolded) → executable path pairs."""
    path = _executable_cache_path(cache_root)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    pairs = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip():
            pairs[key.strip().casefold()] = value.strip()
    return pairs


def _write_executable_cache(cache_root, executables: dict) -> None:
    path = _executable_cache_path(cache_root)
    if path is None:
        return
    temporary = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(executables, stream, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temporary, path)
        temporary = ""
    except (OSError, ValueError):
        return
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def remember_executables(cache_root, executables: dict) -> None:
    """Persist process name → executable path pairs for later icon lookups.

    Icons are extracted from an executable path, but application profiles only
    record the process name. Many applications (Electron/Store/user-scope
    installs) never register under App Paths, so without this cache their icon
    would disappear as soon as the window that revealed the path is gone.
    """
    if not cache_root or not executables:
        return
    stored = _read_executable_cache(cache_root)
    changed = False
    for process, executable in executables.items():
        key = str(process or "").strip().casefold()
        value = str(executable or "").strip()
        if not key or not value:
            continue
        if stored.get(key) == value:
            continue
        stored[key] = value
        changed = True
    if changed:
        _write_executable_cache(cache_root, stored)


def resolve_executables(processes, cache_root=None) -> dict:
    """Resolve process names to executable paths.

    Lookup order: registry App Paths → executable cache persisted under
    ``cache_root`` → one scan of currently visible windows (which also refreshes
    the cache). Returns a mapping of casefolded process name → path; unknown
    names are simply absent.
    """
    wanted: list[str] = []
    seen: set[str] = set()
    for process in processes or ():
        name = process_name_from_path(str(process or ""))
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        wanted.append(name)
    if not wanted:
        return {}

    stored = _read_executable_cache(cache_root)
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name in wanted:
        key = name.casefold()
        path = _registry_executable(name)
        if not path:
            candidate = stored.get(key, "")
            if candidate and Path(candidate).is_file():
                path = candidate
        if path:
            resolved[key] = path
        else:
            missing.append(name)

    if missing and sys.platform == "win32":
        try:
            listed = list_running_applications()
        except Exception:  # noqa: BLE001 - enumeration is best effort
            listed = []
        found = {}
        for row in listed:
            key = str(row.get("process", "")).casefold()
            path = str(row.get("executable", ""))
            if key and path and key not in found:
                found[key] = path
        learned = {}
        for name in missing:
            key = name.casefold()
            path = found.get(key, "")
            if path:
                resolved[key] = path
                learned[key] = path
        if learned:
            remember_executables(cache_root, learned)
    return resolved


def executable_for_process(process: str, cache_root=None) -> str:
    """Executable path of a process name, or "" when it cannot be resolved."""
    name = process_name_from_path(str(process or ""))
    if not name:
        return ""
    return resolve_executables([name], cache_root).get(name.casefold(), "")


def list_running_applications() -> list[dict]:
    """Visible applications, de-duplicated by process name, most-recent first."""
    if sys.platform != "win32":
        return []
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        version_api = ctypes.WinDLL("version", use_last_error=True)
    except OSError:
        return []

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    rows: list[dict] = []
    seen: set[str] = set()

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            if not user32.GetWindowTextW(hwnd, title, length + 1):
                return True
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            executable = _query_image_path(kernel32, pid.value)
            process = process_name_from_path(executable)
            if not process or process in seen:
                return True
            seen.add(process)
            rows.append({
                "process": process,
                "name": _file_description(version_api, executable) or Path(process).stem,
                "executable": executable,
                "title": title.value,
            })
        except (OSError, ValueError):
            return True
        return True

    try:
        user32.EnumWindows(enum_proc(callback), 0)
    except OSError:
        return []
    return rows
