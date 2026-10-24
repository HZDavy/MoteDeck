"""Minimal Interception kernel-driver **injection** runtime.

The Interception driver filters keyboard/mouse input below ``SendInput`` and
below the low-level ``WH_KEYBOARD_LL`` hook.  Its injected strokes carry no
``LLKHF_INJECTED`` flag, so applications that deliberately ignore software-
generated input (some chat clients, games and remote-desktop stacks) accept
them.  ``SendInput`` remains the default backend; this module is the optional
alternative a user can enable per button.

Only the *send* half of the Interception API is used here.  This module never
installs a filter, never receives events, and never consumes input, so it
cannot duplicate or swallow the real keyboard traffic the way a device-level
scan-code remap does.

A driver context is opened for one burst of strokes and destroyed immediately
afterwards; it is never kept between calls.  Merely *holding* a context open is
not harmless: on Windows 11 the driver can leave the keyboard latched in a
filtered state while a context handle stays open, so availability is answered
from the registry (:func:`driver_installed`) instead of by opening the device.

The driver itself is a one-time, machine-wide, administrator install with a
reboot; it is not something this process can provision.  When the DLL or the
driver is missing every entry point fails closed with
:class:`InterceptionUnavailableError` so the caller can fall back to
``SendInput``.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
import threading
from ctypes import wintypes
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

try:  # pragma: no cover - Windows-only module, guarded for import safety
    import winreg
except ImportError:  # pragma: no cover - non-Windows
    winreg = None  # type: ignore[assignment]

# Interception's own constants (interception.h).
INTERCEPTION_MAX_KEYBOARD = 10
INTERCEPTION_KEYBOARD_INDEX_MAX = INTERCEPTION_MAX_KEYBOARD - 1

# ``interception_key_stroke.state`` values.
KEY_DOWN = 0x00
KEY_UP = 0x01
KEY_E0 = 0x02

# ``interception_mouse_stroke.state`` values (buttons only; no motion).
MOUSE_LEFT_BUTTON_DOWN = 0x001
MOUSE_LEFT_BUTTON_UP = 0x002
MOUSE_RIGHT_BUTTON_DOWN = 0x004
MOUSE_RIGHT_BUTTON_UP = 0x008
MOUSE_MIDDLE_BUTTON_DOWN = 0x010
MOUSE_MIDDLE_BUTTON_UP = 0x020
MOUSE_BUTTON_4_DOWN = 0x040
MOUSE_BUTTON_4_UP = 0x080
MOUSE_BUTTON_5_DOWN = 0x100
MOUSE_BUTTON_5_UP = 0x200

_MOUSE_BUTTON_STATES = {
    "left": (MOUSE_LEFT_BUTTON_DOWN, MOUSE_LEFT_BUTTON_UP),
    "right": (MOUSE_RIGHT_BUTTON_DOWN, MOUSE_RIGHT_BUTTON_UP),
    "middle": (MOUSE_MIDDLE_BUTTON_DOWN, MOUSE_MIDDLE_BUTTON_UP),
    "x1": (MOUSE_BUTTON_4_DOWN, MOUSE_BUTTON_4_UP),
    "x2": (MOUSE_BUTTON_5_DOWN, MOUSE_BUTTON_5_UP),
}

DLL_ENV_VAR = "RC003_INTERCEPTION_DLL"
KEYBOARD_INDEX_ENV_VAR = "RC003_INTERCEPTION_KEYBOARD_INDEX"
DEFAULT_DLL_NAMES = ("interception.dll",)
DEFAULT_KEYBOARD_INDEX = 0


def _source_tree_asset_dirs() -> List[str]:
    """Return the ignored third-party folder used by source-only runs.

    A frozen build finds the DLL under ``sys._MEIPASS``; a source checkout keeps
    the same fetched binary in ``build/third_party/interception`` (written by
    ``build/fetch-interception.ps1``).  The package sits two levels below the
    app root as ``src/ovb_rc003``.
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


def _dll_candidate_paths() -> List[str]:
    """Return the DLL locations to try, most specific first.

    The environment override wins outright.  Otherwise the DLL is looked up
    next to the frozen payload (``sys._MEIPASS``), next to the executable,
    and next to this module, before falling back to the bare library name
    (which lets the OS loader search ``PATH`` and the working directory).
    """

    override = os.environ.get(DLL_ENV_VAR, "").strip()
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
        for name in DEFAULT_DLL_NAMES:
            path = os.path.join(directory, name)
            if path not in candidates:
                candidates.append(path)
    for name in DEFAULT_DLL_NAMES:
        if name not in candidates:
            candidates.append(name)
    return candidates


class InterceptionUnavailableError(OSError):
    """Raised when the Interception DLL or its driver is not usable."""


def _require_windows() -> None:
    if sys.platform != "win32":
        raise InterceptionUnavailableError(
            "Interception injection is only available on Windows"
        )


def _require_live_input_allowed() -> None:
    if os.environ.get("RC003_DISABLE_LIVE_INPUT") == "1":
        raise InterceptionUnavailableError(
            "live Windows input is disabled for this process"
        )


# Interception installs two kernel services (``keyboard.sys`` / ``mouse.sys``)
# and registers itself in the device-class ``UpperFilters`` lists.  Both are
# plain registry values, so they can be read without opening ``\\.\interceptionNN``
# -- opening the device is exactly what latches the keyboard on Windows 11.
_KEYBOARD_CLASS_GUID = "{4D36E96B-E325-11CE-BFC1-08002BE10318}"
_MOUSE_CLASS_GUID = "{4D36E96F-E325-11CE-BFC1-08002BE10318}"
_CLASS_ROOT = r"SYSTEM\CurrentControlSet\Control\Class"
_SERVICES_ROOT = r"SYSTEM\CurrentControlSet\Services"

DRIVER_NOT_INSTALLED_REASON = (
    "driver_not_installed: the Interception keyboard filter is not registered; "
    "install the Interception driver (keyboard.sys and mouse.sys) and reboot"
)
DRIVER_NOT_LOADED_REASON = (
    "driver_not_loaded: interception_create_context failed; install the "
    "Interception driver (keyboard.sys and mouse.sys) and reboot"
)
DRIVER_PENDING_REBOOT_REASON = (
    "driver_pending_reboot: the Interception filter is registered but its "
    "services are not running yet; reboot Windows, then check again"
)
_HVCI_SCENARIO_KEY = (
    r"SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios"
    r"\HypervisorEnforcedCodeIntegrity"
)
_DRIVER_SERVICE_NAMES = ("keyboard", "mouse")


def _resolve_image_path(raw: str) -> str:
    """Turn a service ``ImagePath`` value into a filesystem path."""

    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    text = os.path.expandvars(str(raw).strip().strip('"'))
    if text[:12].lower() == "\\systemroot\\":
        return system_root + text[11:]
    if text.startswith("\\??\\"):
        return text[4:]
    if not os.path.isabs(text):
        return os.path.join(system_root, "System32", text)
    return text


def _service_driver_file(name: str) -> Optional[str]:
    """Return the resolved driver file of ``Services\\<name>``, if it exists.

    The official installer registers both kernel services without writing an
    ``ImagePath`` value; with no explicit value Windows infers
    ``System32\\drivers\\<name>.sys`` from the key name.  An absent value is
    therefore the normal shape of a successful install, not a broken one, so it
    falls back to that inferred path.

    A key whose driver file is missing (for example after a half-finished
    uninstall) is not a working install, so it returns ``None`` too.
    """

    if winreg is None:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            f"{_SERVICES_ROOT}\\{name}",
            0,
            winreg.KEY_READ,
        ) as key:
            try:
                value, _ = winreg.QueryValueEx(key, "ImagePath")
            except FileNotFoundError:
                value = ""
    except OSError:
        return None
    text = "" if value is None else str(value).strip()
    if text:
        path = _resolve_image_path(text)
    else:
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        path = os.path.join(system_root, "System32", "drivers", f"{name}.sys")
    return path if os.path.isfile(path) else None


def _class_filter_registered(guid: str, name: str) -> bool:
    """Whether the device class ``UpperFilters`` list contains ``name``."""

    if winreg is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            f"{_CLASS_ROOT}\\{guid}",
            0,
            winreg.KEY_READ,
        ) as key:
            value, _ = winreg.QueryValueEx(key, "UpperFilters")
    except OSError:
        return False
    if isinstance(value, str):
        entries = [value]
    else:
        try:
            entries = list(value)
        except TypeError:
            return False
    return name.lower() in {str(entry).strip().lower() for entry in entries}


def driver_installed() -> bool:
    r"""Whether the complete Interception filter is registered on this machine.

    Both kernel services must exist with their driver files on disk, and both
    device classes must list their filter.  Any missing piece means the install
    did not finish, so the answer is ``False``.

    This is a *registry-only* probe.  It never opens ``\\.\interceptionNN``, so
    it cannot disturb the keyboard: on Windows 11 opening a context is what makes
    the driver latch the keyboard in a filtered state.
    """

    services_ok = all(
        _service_driver_file(name) is not None for name in _DRIVER_SERVICE_NAMES
    )
    return (
        services_ok
        and _class_filter_registered(_KEYBOARD_CLASS_GUID, "keyboard")
        and _class_filter_registered(_MOUSE_CLASS_GUID, "mouse")
    )


_SC_MANAGER_CONNECT = 0x0001
_SERVICE_QUERY_STATUS = 0x0004
_SERVICE_RUNNING = 0x00000004


class _ServiceStatus(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", ctypes.c_uint32),
        ("dwCurrentState", ctypes.c_uint32),
        ("dwControlsAccepted", ctypes.c_uint32),
        ("dwWin32ExitCode", ctypes.c_uint32),
        ("dwServiceSpecificExitCode", ctypes.c_uint32),
        ("dwCheckPoint", ctypes.c_uint32),
        ("dwWaitHint", ctypes.c_uint32),
    ]


def _service_running(name: str) -> bool:
    """Whether the kernel service ``name`` is in the RUNNING state.

    Read-only: it only queries service status through the Service Control
    Manager and never starts or stops anything.
    """

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    advapi32.OpenSCManagerW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
    advapi32.OpenSCManagerW.restype = ctypes.c_void_p
    advapi32.OpenServiceW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32)
    advapi32.OpenServiceW.restype = ctypes.c_void_p
    advapi32.QueryServiceStatus.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_ServiceStatus),
    )
    advapi32.QueryServiceStatus.restype = ctypes.c_int
    advapi32.CloseServiceHandle.argtypes = (ctypes.c_void_p,)
    advapi32.CloseServiceHandle.restype = ctypes.c_int

    manager = advapi32.OpenSCManagerW(None, None, _SC_MANAGER_CONNECT)
    if not manager:
        return False
    try:
        service = advapi32.OpenServiceW(manager, name, _SERVICE_QUERY_STATUS)
        if not service:
            return False
        try:
            status = _ServiceStatus()
            if not advapi32.QueryServiceStatus(service, ctypes.byref(status)):
                return False
            return int(status.dwCurrentState) == _SERVICE_RUNNING
        finally:
            advapi32.CloseServiceHandle(service)
    finally:
        advapi32.CloseServiceHandle(manager)


def driver_running() -> bool:
    """Whether both Interception kernel services are running right now.

    A registered driver only takes effect after a reboot, so a registered but
    not running driver is reported as "pending reboot", not as usable.
    """

    if sys.platform != "win32":
        return False
    return all(_service_running(name) for name in _DRIVER_SERVICE_NAMES)


def driver_ready() -> bool:
    """Registered *and* running: the only state in which injection may be used.

    Memory Integrity (HVCI) is *not* part of this check.  This driver is signed
    and loads fine with HVCI enabled (verified: HVCI running while the keyboard
    service reports RUNNING), so treating HVCI as a blocker only produced false
    "unavailable" results.
    """

    return driver_installed() and driver_running()


def memory_integrity_state() -> Optional[bool]:
    """Return whether Memory Integrity (HVCI) is on, or ``None`` if unknown.

    Read-only diagnostic used to explain a failed install; it never changes
    the setting.
    """

    if winreg is None:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            _HVCI_SCENARIO_KEY,
            0,
            winreg.KEY_READ,
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Enabled")
        return int(value) != 0
    except (OSError, TypeError, ValueError):
        return None


class _KeyStroke(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("state", ctypes.c_ushort),
        ("information", ctypes.c_uint32),
    ]


class _MouseStroke(ctypes.Structure):
    _fields_ = [
        ("state", ctypes.c_ushort),
        ("flags", ctypes.c_ushort),
        ("rolling", ctypes.c_int16),
        ("x", ctypes.c_int32),
        ("y", ctypes.c_int32),
        ("information", ctypes.c_uint32),
    ]


def keyboard_device(index: int = DEFAULT_KEYBOARD_INDEX) -> int:
    """Return the Interception device id for one keyboard slot."""

    if not 0 <= int(index) <= INTERCEPTION_KEYBOARD_INDEX_MAX:
        raise ValueError("Interception keyboard index out of range")
    return int(index) + 1


def mouse_device(index: int = 0) -> int:
    """Return the Interception device id for one mouse slot."""

    if not 0 <= int(index) <= 9:
        raise ValueError("Interception mouse index out of range")
    return int(index) + 1 + INTERCEPTION_MAX_KEYBOARD


class InterceptionRuntime:
    """Lazily loads ``interception.dll`` and injects scan-code strokes.

    The class is intentionally fail-closed: every method that needs the driver
    raises :class:`InterceptionUnavailableError` until the driver is installed
    and usable.  Availability is answered from the registry, and a driver
    context is only ever held open for the duration of one send, so merely
    asking about the driver never touches the keyboard.
    """

    def __init__(
        self,
        *,
        dll_loader: Optional[Callable[[], object]] = None,
        keyboard_index: Optional[int] = None,
        scan_code_resolver: Optional[Callable[[int], Optional[Tuple[int, bool]]]] = None,
        driver_probe: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._dll_loader = dll_loader
        self._dll = None
        self._reason = ""
        self._driver_probe = driver_probe if driver_probe is not None else driver_ready
        self._keyboard_index = (
            keyboard_index
            if keyboard_index is not None
            else _keyboard_index_from_environment()
        )
        self._scan_code_resolver = scan_code_resolver

    # -- capability -----------------------------------------------------
    def is_available(self) -> bool:
        """Whether the Interception driver is installed and usable.

        This never opens a driver context, so it is safe to call from the UI.
        """

        try:
            self._probe()
        except InterceptionUnavailableError:
            return False
        return True

    def unavailable_reason(self) -> str:
        """Return the human-readable reason the runtime is unusable."""

        if self.is_available():
            return ""
        return self._reason or "interception_unavailable"

    def _probe(self) -> None:
        with self._lock:
            _require_windows()
            _require_live_input_allowed()
            self._load_dll()
            if not self._driver_probe():
                self._reason = (
                    DRIVER_PENDING_REBOOT_REASON
                    if driver_installed()
                    else DRIVER_NOT_INSTALLED_REASON
                )
                raise InterceptionUnavailableError(self._reason)

    def _load_dll(self):
        if self._dll is not None:
            return self._dll
        last_error = None
        loaded = None
        if self._dll_loader is not None:
            try:
                loaded = self._dll_loader()
            except OSError as exc:
                last_error = exc
        else:
            for candidate in _dll_candidate_paths():
                try:
                    loaded = ctypes.WinDLL(candidate)  # type: ignore[attr-defined]
                    break
                except OSError as exc:
                    last_error = exc
        if loaded is None:
            self._reason = (
                f"dll_not_found: place interception.dll next to the app or set "
                f"{DLL_ENV_VAR}"
            )
            raise InterceptionUnavailableError(self._reason) from last_error
        self._dll = loaded
        return loaded

    @contextlib.contextmanager
    def _session(self) -> Iterator[Tuple[object, int]]:
        """Yield a freshly created driver context for one burst of strokes.

        The context is created on entry and destroyed on exit, so the driver
        handle is never held open between calls.
        """

        _require_windows()
        _require_live_input_allowed()
        dll = self._load_dll()
        try:
            dll.interception_create_context.restype = ctypes.c_void_p
            dll.interception_create_context.argtypes = ()
            context = dll.interception_create_context()
        except Exception as exc:  # pragma: no cover - defensive
            self._reason = f"create_context_failed:{type(exc).__name__}"
            raise InterceptionUnavailableError(self._reason) from exc
        if not context:
            self._reason = DRIVER_NOT_LOADED_REASON
            raise InterceptionUnavailableError(self._reason)
        try:
            yield dll, context
        finally:
            try:
                dll.interception_destroy_context.argtypes = (ctypes.c_void_p,)
                dll.interception_destroy_context(context)
            except Exception:  # pragma: no cover - best effort
                pass

    # -- keyboard -------------------------------------------------------
    def _resolve_scan(self, vk: int) -> Tuple[int, bool]:
        resolver = self._scan_code_resolver
        if resolver is None:
            from . import win32_input

            resolver = win32_input.physical_scan_code
        resolved = resolver(int(vk))
        if resolved is None:
            raise InterceptionUnavailableError(
                f"no physical scan code for vk 0x{int(vk):02X}"
            )
        return resolved

    def _send_key_strokes(self, vk: int, key_up: bool) -> None:
        scan_code, is_extended = self._resolve_scan(vk)
        state = KEY_UP if key_up else KEY_DOWN
        if is_extended:
            state |= KEY_E0
        strokes: List[_KeyStroke] = [
            _KeyStroke(code=scan_code, state=state, information=0)
        ]
        self._send(strokes, device=keyboard_device(self._keyboard_index))

    def key_down(self, vk: int) -> None:
        self._send_key_strokes(vk, key_up=False)

    def key_up(self, vk: int) -> None:
        self._send_key_strokes(vk, key_up=True)

    # -- mouse ----------------------------------------------------------
    def mouse_button(self, button: str, down: bool) -> None:
        try:
            states = _MOUSE_BUTTON_STATES[str(button).lower()]
        except KeyError as exc:
            raise ValueError(f"unsupported mouse button: {button}") from exc
        stroke = _MouseStroke(
            state=states[0] if down else states[1],
            flags=0,
            rolling=0,
            x=0,
            y=0,
            information=0,
        )
        self._send([stroke], device=mouse_device(0))

    # -- transport ------------------------------------------------------
    def _send(self, strokes: Sequence[ctypes.Structure], *, device: int) -> None:
        _require_live_input_allowed()
        with self._lock:
            array = type(strokes[0]) * len(strokes)
            payload = array(*strokes)
            stroke_size = ctypes.sizeof(type(strokes[0]))
            with self._session() as (dll, context):
                dll.interception_send.restype = ctypes.c_int
                dll.interception_send.argtypes = (
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.c_void_p,
                    ctypes.c_uint,
                )
                sent = dll.interception_send(
                    context, int(device), ctypes.byref(payload), len(strokes)
                )
                if int(sent) != len(strokes):
                    raise InterceptionUnavailableError(
                        f"interception_send delivered {int(sent)}/{len(strokes)} "
                        f"strokes of {stroke_size} bytes"
                    )


def _keyboard_index_from_environment() -> int:
    raw = os.environ.get(KEYBOARD_INDEX_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_KEYBOARD_INDEX
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_KEYBOARD_INDEX
    if 0 <= value <= INTERCEPTION_KEYBOARD_INDEX_MAX:
        return value
    return DEFAULT_KEYBOARD_INDEX


_runtime = InterceptionRuntime()


def runtime() -> InterceptionRuntime:
    """Return the process-wide Interception runtime singleton."""

    return _runtime


def is_available() -> bool:
    return _runtime.is_available()


def unavailable_reason() -> str:
    return _runtime.unavailable_reason()
