"""Activate a specific Windows TSF input-method profile.

Unlike ``Win+Space``, this module selects the requested profile directly.  A
button can therefore always switch to Doubao regardless of the user's current
keyboard-layout order.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from functools import lru_cache
import uuid
from typing import Iterable, Optional

try:  # pragma: no cover - non-Windows import safety
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]


_TIP_REGISTRY_PATH = r"SOFTWARE\Microsoft\CTF\TIP"
_DOUBAO_SERVICE_CLSID = "{9D2B2E2B-3C93-4D2F-9D35-6EEB85F0D2B0}"
_DOUBAO_PROFILE_GUID = "{2B4D4B3A-4D4F-4C0A-8E66-7F771A2B9C10}"

_CLSID_TF_INPUT_PROCESSOR_PROFILES = "{33C53A50-F456-4884-B049-85FD643ECFED}"
_IID_ITF_INPUT_PROCESSOR_PROFILE_MGR = "{71C6E74C-0F28-11D8-A82A-00065B84435C}"
_GUID_TFCAT_TIP_KEYBOARD = "{34745C63-B2F0-4784-8B67-5E12C8701A31}"

_TF_PROFILETYPE_INPUTPROCESSOR = 1
_TF_IPPMF_ENABLEPROFILE = 0x00000001
_TF_IPPMF_DONTCARECURRENTINPUTLANGUAGE = 0x00000004
_TF_IPPMF_FORSESSION = 0x20000000
_ACTIVATE_FLAGS = (
    _TF_IPPMF_ENABLEPROFILE
    | _TF_IPPMF_DONTCARECURRENTINPUTLANGUAGE
    | _TF_IPPMF_FORSESSION
)

_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
_RPC_E_CHANGED_MODE = ctypes.c_int32(0x80010106).value


class InputMethodUnavailableError(RuntimeError):
    """Raised when the requested Windows input-method profile cannot activate."""


@dataclass(frozen=True)
class InputMethodProfile:
    service_clsid: str
    langid: int
    profile_guid: str
    description: str = ""


class _GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    )

    @classmethod
    def from_string(cls, value: str) -> "_GUID":
        return cls.from_buffer_copy(uuid.UUID(value.strip("{}")).bytes_le)

    def to_string(self) -> str:
        return "{" + str(uuid.UUID(bytes_le=bytes(self))).upper() + "}"


class _TF_INPUTPROCESSORPROFILE(ctypes.Structure):
    _fields_ = (
        ("dwProfileType", ctypes.c_uint32),
        ("langid", ctypes.c_uint16),
        ("clsid", _GUID),
        ("guidProfile", _GUID),
        ("catid", _GUID),
        ("hklSubstitute", ctypes.c_void_p),
        ("dwCaps", ctypes.c_uint32),
        ("hkl", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint32),
    )


def _enum_subkeys(key) -> Iterable[str]:
    index = 0
    while True:
        try:
            yield winreg.EnumKey(key, index)
        except OSError:
            return
        index += 1


def _registry_profiles() -> Iterable[InputMethodProfile]:
    if winreg is None:
        return
    views = tuple(
        dict.fromkeys(
            (
                getattr(winreg, "KEY_WOW64_64KEY", 0),
                getattr(winreg, "KEY_WOW64_32KEY", 0),
                0,
            )
        )
    )
    seen = set()
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in views:
            try:
                tip = winreg.OpenKey(
                    hive, _TIP_REGISTRY_PATH, 0, winreg.KEY_READ | view
                )
            except OSError:
                continue
            with tip:
                for service_clsid in _enum_subkeys(tip):
                    try:
                        languages = winreg.OpenKey(tip, service_clsid + r"\LanguageProfile")
                    except OSError:
                        continue
                    with languages:
                        for language_name in _enum_subkeys(languages):
                            try:
                                langid = int(language_name, 0)
                                language = winreg.OpenKey(languages, language_name)
                            except (OSError, ValueError):
                                continue
                            with language:
                                for profile_guid in _enum_subkeys(language):
                                    identity = (
                                        service_clsid.casefold(),
                                        langid,
                                        profile_guid.casefold(),
                                    )
                                    if identity in seen:
                                        continue
                                    seen.add(identity)
                                    description = ""
                                    try:
                                        profile_key = winreg.OpenKey(language, profile_guid)
                                        with profile_key:
                                            description = str(
                                                winreg.QueryValueEx(profile_key, "Description")[0]
                                            ).strip()
                                    except OSError:
                                        pass
                                    yield InputMethodProfile(
                                        service_clsid=service_clsid,
                                        langid=langid,
                                        profile_guid=profile_guid,
                                        description=description,
                                    )


def select_doubao_profile(
    profiles: Iterable[InputMethodProfile],
) -> Optional[InputMethodProfile]:
    """Choose Doubao by its registered name, with its known IDs as fallback."""

    candidates = tuple(profiles)
    for profile in candidates:
        if profile.description.strip().casefold() == "豆包输入法":
            return profile
    for profile in candidates:
        if "豆包" in profile.description:
            return profile
    known_service = _DOUBAO_SERVICE_CLSID.casefold()
    known_profile = _DOUBAO_PROFILE_GUID.casefold()
    return next(
        (
            profile
            for profile in candidates
            if profile.service_clsid.casefold() == known_service
            and profile.profile_guid.casefold() == known_profile
        ),
        None,
    )


@lru_cache(maxsize=1)
def find_doubao_profile() -> InputMethodProfile:
    if winreg is None:  # pragma: no cover
        raise InputMethodUnavailableError("Windows TSF is unavailable")
    profile = select_doubao_profile(_registry_profiles())
    if profile is None:
        raise InputMethodUnavailableError("未找到已安装的豆包输入法")
    return profile


def _profile_manager_call(operation):
    if not hasattr(ctypes, "OleDLL"):
        raise InputMethodUnavailableError("Windows TSF is unavailable")
    ole32 = ctypes.OleDLL("ole32")
    ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    ole32.CoInitializeEx.restype = ctypes.c_int32
    ole32.CoCreateInstance.argtypes = (
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )
    ole32.CoCreateInstance.restype = ctypes.c_int32
    ole32.CoUninitialize.argtypes = ()
    ole32.CoUninitialize.restype = None

    initialized = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    if initialized not in (0, 1, _RPC_E_CHANGED_MODE):
        raise InputMethodUnavailableError(
            f"CoInitializeEx failed: 0x{initialized & 0xFFFFFFFF:08X}"
        )
    should_uninitialize = initialized in (0, 1)
    manager = ctypes.c_void_p()
    try:
        clsid = _GUID.from_string(_CLSID_TF_INPUT_PROCESSOR_PROFILES)
        iid = _GUID.from_string(_IID_ITF_INPUT_PROCESSOR_PROFILE_MGR)
        result = ole32.CoCreateInstance(
            ctypes.byref(clsid),
            None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(iid),
            ctypes.byref(manager),
        )
        if result != 0 or not manager.value:
            raise InputMethodUnavailableError(
                f"CoCreateInstance failed: 0x{result & 0xFFFFFFFF:08X}"
            )
        return operation(manager)
    finally:
        if manager.value:
            vtable = ctypes.cast(
                manager, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
            ).contents
            release_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
                ctypes.c_uint32, ctypes.c_void_p
            )
            release_type(vtable[2])(manager)
        if should_uninitialize:
            ole32.CoUninitialize()


def _activate_profile(profile: InputMethodProfile) -> None:
    def operation(manager) -> None:
        vtable = ctypes.cast(
            manager, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        activate_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint16,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(_GUID),
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        service = _GUID.from_string(profile.service_clsid)
        profile_id = _GUID.from_string(profile.profile_guid)
        result = activate_type(vtable[3])(
            manager,
            _TF_PROFILETYPE_INPUTPROCESSOR,
            profile.langid,
            ctypes.byref(service),
            ctypes.byref(profile_id),
            None,
            _ACTIVATE_FLAGS,
        )
        if result != 0:
            raise InputMethodUnavailableError(
                f"ActivateProfile failed: 0x{result & 0xFFFFFFFF:08X}"
            )

    _profile_manager_call(operation)


def active_keyboard_profile() -> InputMethodProfile:
    """Return the TSF keyboard profile currently active for this desktop."""

    def operation(manager) -> InputMethodProfile:
        vtable = ctypes.cast(
            manager, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        get_active_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(_TF_INPUTPROCESSORPROFILE),
        )
        category = _GUID.from_string(_GUID_TFCAT_TIP_KEYBOARD)
        value = _TF_INPUTPROCESSORPROFILE()
        result = get_active_type(vtable[10])(
            manager, ctypes.byref(category), ctypes.byref(value)
        )
        if result != 0:
            raise InputMethodUnavailableError(
                f"GetActiveProfile failed: 0x{result & 0xFFFFFFFF:08X}"
            )
        return InputMethodProfile(
            service_clsid=value.clsid.to_string(),
            langid=int(value.langid),
            profile_guid=value.guidProfile.to_string(),
        )

    return _profile_manager_call(operation)


def activate_doubao_input_method() -> InputMethodProfile:
    """Activate Doubao for the current Windows desktop session."""

    try:
        profile = find_doubao_profile()
        _activate_profile(profile)
        return profile
    except InputMethodUnavailableError:
        raise
    except Exception as exc:
        raise InputMethodUnavailableError(
            f"豆包输入法激活失败：{type(exc).__name__}"
        ) from exc
