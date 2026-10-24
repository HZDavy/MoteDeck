"""Per-button injection backend selection.

Every button action is delivered to Windows by injecting input.  There are two
ways to do that, and each button may choose its own:

``sendinput`` (default)
    ``user32!SendInput`` via :mod:`win32_input`.  Works everywhere, but the
    events carry ``LLKHF_INJECTED``, so a target application that deliberately
    ignores synthetic input (some chat clients, games, remote-desktop stacks)
    will not react.

``interception``
    The Interception kernel driver via :mod:`interception_runtime_windows`.
    The strokes look exactly like real hardware.  It requires the driver to be
    installed machine-wide (administrator, one reboot) and can only inject keys
    with a stable physical scan code, so a few action kinds stay SendInput-only.

This module is the single place that answers "which backend, and can it do
this action?" - it holds the semantic action -> key-token table and the mode
normalization, and it isolates the Interception runtime so the rest of the app
never touches the driver directly.
"""

from __future__ import annotations

from typing import Dict, Optional, Protocol, Tuple

from . import interception_runtime_windows, key_mapping, win32_input, win32_keys

INJECT_MODE_SENDINPUT = key_mapping.DEFAULT_INJECT_MODE
INJECT_MODE_INTERCEPTION = key_mapping.INJECT_MODE_INTERCEPTION
DEFAULT_INJECT_MODE = key_mapping.DEFAULT_INJECT_MODE
INJECT_MODES = key_mapping.INJECT_MODES

INJECT_MODE_LABELS = {
    INJECT_MODE_SENDINPUT: "SendInput（默认）",
    INJECT_MODE_INTERCEPTION: "Interception（驱动级）",
}

# Re-exported so callers can catch an unavailable driver without importing the
# low-level runtime module themselves.
InterceptionUnavailableError = interception_runtime_windows.InterceptionUnavailableError

# The key sequence each *semantic* action injects.  These are the exact
# tokens the SendInput path already uses (see win32_input.send_show_desktop and
# friends), so both backends stay byte-for-byte equivalent in behaviour.
SEMANTIC_ACTION_KEY_TOKENS: Dict[key_mapping.ActionKind, Tuple[str, ...]] = {
    key_mapping.ActionKind.ESCAPE: ("escape",),
    key_mapping.ActionKind.RETURN: ("enter",),
    key_mapping.ActionKind.ARROW_UP: ("up",),
    key_mapping.ActionKind.ARROW_DOWN: ("down",),
    key_mapping.ActionKind.ARROW_LEFT: ("left",),
    key_mapping.ActionKind.ARROW_RIGHT: ("right",),
    key_mapping.ActionKind.DELETE_BACKWARD: ("backspace",),
    # Win+M, not Win+D: Win+D toggles, so a duplicated remote report would
    # restore every window again (see win32_input.send_show_desktop).
    key_mapping.ActionKind.SHOW_DESKTOP: ("win", "m"),
    key_mapping.ActionKind.CONTEXT_MENU: ("apps",),
    key_mapping.ActionKind.APP_SWITCHER: ("ctrl", "alt", "tab"),
    key_mapping.ActionKind.SYSTEM_VOLUME_UP: ("volume_up",),
    key_mapping.ActionKind.SYSTEM_VOLUME_DOWN: ("volume_down",),
    key_mapping.ActionKind.SYSTEM_VOLUME_MUTE: ("volume_mute",),
    key_mapping.ActionKind.PLAY_PAUSE: ("media_play_pause",),
}

_INTERCEPTION_MOUSE_CLICKS = {
    key_mapping.ActionKind.MOUSE_LEFT_CLICK: "left",
    key_mapping.ActionKind.MOUSE_RIGHT_CLICK: "right",
    key_mapping.ActionKind.MOUSE_MIDDLE_CLICK: "middle",
    key_mapping.ActionKind.MOUSE_X1_CLICK: "x1",
    key_mapping.ActionKind.MOUSE_X2_CLICK: "x2",
}

# Consumer/multimedia controls used to have no Interception delivery because
# they carry no ``MapVirtualKey`` code. They do have fixed E0-prefixed
# scan codes (see ``win32_input._MULTIMEDIA_SCAN_CODES``), so they are now
# injectable like any other key and need no special case here.


def normalize_inject_mode(value: object) -> str:
    """Coerce any stored value to a supported mode, defaulting to SendInput."""

    return key_mapping.normalize_stored_inject_mode(value)


def key_tokens_for_action(action: key_mapping.ButtonAction) -> Optional[Tuple[str, ...]]:
    """Return the ordered key tokens ``action`` injects, or ``None``.

    ``None`` means the action does not inject keys (disabled, URI, open-app,
    element-navigation toggle, voice, mouse, ...).
    """

    if action.kind == key_mapping.ActionKind.KEY_COMBO:
        return tuple(action.keys) or None
    return SEMANTIC_ACTION_KEY_TOKENS.get(action.kind)


def mouse_button_for_action(action: key_mapping.ButtonAction) -> Optional[str]:
    """Return the mouse button ``action`` clicks via Interception, or ``None``."""

    return _INTERCEPTION_MOUSE_CLICKS.get(action.kind)


def tokens_supported_by_interception(tokens: Tuple[str, ...]) -> bool:
    """Whether every token resolves to a real physical scan code."""

    if not tokens:
        return False
    for token in tokens:
        try:
            vk_codes = win32_keys.resolve_vk_codes((token,))
        except win32_keys.UnknownKeyTokenError:
            return False
        if not vk_codes or win32_input.physical_scan_code(vk_codes[0]) is None:
            return False
    return True


def action_supported_by_interception(action: key_mapping.ButtonAction) -> bool:
    """Whether ``action`` can be delivered by the Interception backend."""

    tokens = key_tokens_for_action(action)
    if tokens is not None:
        return tokens_supported_by_interception(tokens)
    return mouse_button_for_action(action) is not None


def interception_available() -> bool:
    return interception_runtime_windows.is_available()


def interception_unavailable_reason() -> str:
    return interception_runtime_windows.unavailable_reason()


# User-facing Chinese hints for each reason :func:`interception_unavailable_reason`
# can report.  Matched by stable prefix so extra diagnostics in the suffix still
# map correctly.  A missing DLL or a disabled live-input gate must not be
# misreported as "the driver is not installed".
_UNAVAILABLE_HINTS = (
    ("driver_not_installed", "未检测到 Interception 驱动，仅可用 SendInput"),
    ("driver_not_loaded", "Interception 驱动尚未生效，请重启电脑"),
    ("dll_not_found", "缺少 interception.dll，仅可用 SendInput"),
    ("create_context_failed", "Interception 驱动上下文创建失败"),
)
_LIVE_INPUT_DISABLED_HINT = "当前进程已禁用实时输入，Interception 暂不可用"
_DEFAULT_UNAVAILABLE_HINT = "Interception 暂不可用，仅可用 SendInput"


def interception_unavailable_hint() -> str:
    """Return the Chinese, user-facing reason Interception is unavailable.

    Returns ``""`` when the backend is usable.  The settings window binds this
    instead of a fixed "driver not installed" string, so a missing DLL or a
    disabled live-input gate is reported accurately.
    """

    reason = interception_unavailable_reason()
    if not reason:
        return ""
    if "live Windows input is disabled" in reason:
        return _LIVE_INPUT_DISABLED_HINT
    for prefix, hint in _UNAVAILABLE_HINTS:
        if reason.startswith(prefix):
            return hint
    return _DEFAULT_UNAVAILABLE_HINT


def mode_available(mode: object) -> bool:
    """Whether ``mode`` can currently deliver input on this machine."""

    normalized = normalize_inject_mode(mode)
    if normalized == INJECT_MODE_INTERCEPTION:
        return interception_available()
    return True


def mode_status(mode: object) -> Tuple[bool, str]:
    """Return ``(available, reason)`` for one mode, for display in settings."""

    normalized = normalize_inject_mode(mode)
    if normalized == INJECT_MODE_INTERCEPTION:
        available = interception_available()
        return available, "" if available else interception_unavailable_reason()
    return True, ""


def send_hold_tokens_down(tokens: Tuple[str, ...]) -> None:
    """Press ``tokens`` in order through the Interception driver.

    The release is not sent: hold-to-talk shortcuts stay owned by the caller
    until the matching :func:`send_hold_tokens_up` call.
    """

    vk_codes = win32_keys.resolve_vk_codes(tokens)
    runtime = interception_runtime_windows.runtime()
    for vk_code in vk_codes:
        runtime.key_down(vk_code)


def send_hold_tokens_up(tokens: Tuple[str, ...]) -> None:
    """Release ``tokens`` in reverse order through the Interception driver."""

    vk_codes = win32_keys.resolve_vk_codes(tokens)
    runtime = interception_runtime_windows.runtime()
    for vk_code in reversed(vk_codes):
        runtime.key_up(vk_code)


def send_hold_tokens_repeat(tokens: Tuple[str, ...]) -> None:
    """Re-send only the held key so typematic repeats keep modifiers down."""

    vk_codes = win32_keys.resolve_vk_codes(tokens)
    if not vk_codes:
        return
    interception_runtime_windows.runtime().key_down(vk_codes[-1])


def send_tokens_via_interception(tokens: Tuple[str, ...]) -> None:
    """Press and release ``tokens`` in order through the Interception driver."""

    send_hold_tokens_down(tokens)
    send_hold_tokens_up(tokens)


def click_mouse_via_interception(button: str) -> None:
    """Click one mouse button through the Interception driver."""

    runtime = interception_runtime_windows.runtime()
    runtime.mouse_button(button, True)
    runtime.mouse_button(button, False)


class InjectionBackend(Protocol):
    """Delivers remote-button input through one injection mechanism.

    Methods take the key tokens the rest of the app already uses, not virtual
    key codes, so both implementations resolve keys the same way.
    """

    name: str

    def key_down(self, tokens: Tuple[str, ...]) -> None: ...

    def key_up(self, tokens: Tuple[str, ...]) -> None: ...

    def key_repeat(self, tokens: Tuple[str, ...]) -> None: ...

    def key_tap(self, tokens: Tuple[str, ...]) -> None: ...

    def mouse_click(self, button: str) -> None: ...


class SendInputBackend:
    """``user32!SendInput`` path; events carry ``LLKHF_INJECTED``."""

    name = INJECT_MODE_SENDINPUT

    def key_down(self, tokens: Tuple[str, ...]) -> None:
        win32_input.send_voice_key_combo_down(tokens)

    def key_up(self, tokens: Tuple[str, ...]) -> None:
        win32_input.send_voice_key_combo_up(tokens)

    def key_repeat(self, tokens: Tuple[str, ...]) -> None:
        win32_input.send_voice_key_combo_repeat(tokens)

    def key_tap(self, tokens: Tuple[str, ...]) -> None:
        win32_input.send_key_combo_tap(tokens)

    def mouse_click(self, button: str) -> None:
        win32_input.send_mouse_button_click(button)


class InterceptionBackend:
    """Interception kernel driver path; strokes look like real hardware."""

    name = INJECT_MODE_INTERCEPTION

    def key_down(self, tokens: Tuple[str, ...]) -> None:
        send_hold_tokens_down(tokens)

    def key_up(self, tokens: Tuple[str, ...]) -> None:
        send_hold_tokens_up(tokens)

    def key_repeat(self, tokens: Tuple[str, ...]) -> None:
        send_hold_tokens_repeat(tokens)

    def key_tap(self, tokens: Tuple[str, ...]) -> None:
        send_tokens_via_interception(tokens)

    def mouse_click(self, button: str) -> None:
        click_mouse_via_interception(button)


_SENDINPUT_BACKEND = SendInputBackend()
_INTERCEPTION_BACKEND = InterceptionBackend()


def backend_for(mode: object) -> InjectionBackend:
    """Return the backend for ``mode``; unknown values fall back to SendInput."""

    if normalize_inject_mode(mode) == INJECT_MODE_INTERCEPTION:
        return _INTERCEPTION_BACKEND
    return _SENDINPUT_BACKEND
