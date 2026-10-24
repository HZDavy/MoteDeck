"""Settings-window pure logic: button mapping list, voice hotkey, output
endpoint, bridge-launch and log-location status text.

This module is deliberately Tk/Qt-free (XRBM-030 replaced the previous Tk
view with a PySide6-Essentials + Qt Quick/QML one - see
``qt_settings_app.py`` and ``qml/`` - but every piece of validation/save/
launch/log-status logic below stays here so it remains
directly unit-testable without constructing any window at all, matching the
   contract fixed after XRBM-014 review RETRY P1 #7): every piece of
validation/save logic is a plain function (``_action_to_display``,
``_display_to_action``, ``build_save_model``, ``_endpoint_display``,
``_parse_endpoint_display``, ``describe_launch_result``,
``describe_log_open_result``) that tests call directly - see
   tests/test_settings_ui_helpers.py. Legacy voice values render as an
   explicit disabled notice; new saves allow up to
   ``key_mapping.MAX_VOICE_SLOTS`` primary mappings to carry a voice action,
   on any physical button.

``main()`` at the bottom of this module is the only place that touches Qt at
all, and does so via a lazy import inside the function body - importing this
module (e.g. from ``__main__.py``'s ``--dry-run`` smoke check) never
requires PySide6 to be installed, the same optional-dependency convention
this package already uses for ``sounddevice``/``numpy``/``winrt`` (see
``qt_settings_app.py``'s module docstring for the exact error raised when
Qt is missing).
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, replace
from typing import Dict, Optional, Tuple

from . import (
    audio_output,
    bridge_launcher,
    device_catalog,
    device_profile,
    hotkey,
    injection_backend,
    key_mapping,
    logging_setup,
    product_identity,
    win32_keys,
)

_OPEN_APPLICATION_DISPLAY = f"打开{product_identity.DISPLAY_NAME}"

# Reference action names map to semantic values.  The Windows implementation
# is intentionally behind these values; the dropdown must never turn
# ``方向上`` back into a generic ``key_combo`` just because the platform uses
# a key event to deliver it.
_REFERENCE_ACTION_LABELS: Dict[key_mapping.ActionKind, str] = {
    key_mapping.ActionKind.ESCAPE: "Escape",
    key_mapping.ActionKind.RETURN: "回车",
    key_mapping.ActionKind.ARROW_UP: "方向上",
    key_mapping.ActionKind.ARROW_DOWN: "方向下",
    key_mapping.ActionKind.ARROW_LEFT: "方向左",
    key_mapping.ActionKind.ARROW_RIGHT: "方向右",
    key_mapping.ActionKind.DELETE_BACKWARD: "退格",
    key_mapping.ActionKind.SHOW_DESKTOP: "显示桌面",
    key_mapping.ActionKind.CONTEXT_MENU: "右键菜单",
    key_mapping.ActionKind.APP_SWITCHER: "应用切换",
    key_mapping.ActionKind.SYSTEM_VOLUME_UP: "系统音量 +",
    key_mapping.ActionKind.SYSTEM_VOLUME_DOWN: "系统音量 −",
    key_mapping.ActionKind.SYSTEM_VOLUME_MUTE: "系统静音",
    key_mapping.ActionKind.PLAY_PAUSE: "播放 / 暂停",
    key_mapping.ActionKind.MOUSE_LEFT_CLICK: "鼠标左键单击",
    key_mapping.ActionKind.MOUSE_RIGHT_CLICK: "鼠标右键单击",
    key_mapping.ActionKind.MOUSE_MIDDLE_CLICK: "鼠标中键单击",
    key_mapping.ActionKind.MOUSE_X1_CLICK: "鼠标 X1 单击",
    key_mapping.ActionKind.MOUSE_X2_CLICK: "鼠标 X2 单击",
    key_mapping.ActionKind.VOICE_MODE_CYCLE: "语音模式切换",
    key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE: "元素导航开关",
    key_mapping.ActionKind.OPEN_REMOTE_MIC: _OPEN_APPLICATION_DISPLAY,
    key_mapping.ActionKind.OPEN_CODEX: "打开 Codex",
    key_mapping.ActionKind.OPEN_CLAUDE: "打开 Claude",
    key_mapping.ActionKind.OPEN_CMUX: "打开 cmux 终端",
    # Keep saved mappings readable; this action is no longer a menu preset.
    key_mapping.ActionKind.OPEN_WECHAT: "打开微信",
    key_mapping.ActionKind.OPEN_CURSOR: "打开 Cursor",
    key_mapping.ActionKind.OPEN_SLACK: "打开 Slack",
    key_mapping.ActionKind.OPEN_WECOM: "打开企业微信",
    key_mapping.ActionKind.OPEN_NETEASE_MUSIC: "打开网易云音乐",
    key_mapping.ActionKind.OPEN_CHROME: "打开 Chrome",
    key_mapping.ActionKind.OPEN_EDGE: "打开 Edge",
    key_mapping.ActionKind.OPEN_ZED: "打开 Zed",
}
_REFERENCE_ACTION_KINDS_BY_LABEL: Dict[str, key_mapping.ActionKind] = {
    label: action_kind for action_kind, label in _REFERENCE_ACTION_LABELS.items()
}

LONG_PRESS_ACTION_LABELS = {
    key_mapping.ActionKind.MOUSE_LEFT_HOLD: "鼠标左键长按",
    key_mapping.ActionKind.MOUSE_RIGHT_HOLD: "鼠标右键长按",
    key_mapping.ActionKind.MOUSE_X1_HOLD: "鼠标 X1 长按",
    key_mapping.ActionKind.MOUSE_X2_HOLD: "鼠标 X2 长按",
}
_REFERENCE_ACTION_LABELS.update(LONG_PRESS_ACTION_LABELS)
_REFERENCE_ACTION_KINDS_BY_LABEL.update(
    {label: kind for kind, label in LONG_PRESS_ACTION_LABELS.items()}
)

# Older display spellings remain accepted for editable fields and tests that
# construct the UI model directly. Persisted bindings store semantic action
# kinds, so rendering always uses the current labels above.
_LEGACY_REFERENCE_ACTION_KINDS_BY_LABEL: Dict[str, key_mapping.ActionKind] = {
    "Return": key_mapping.ActionKind.RETURN,
    "Delete（退格）": key_mapping.ActionKind.DELETE_BACKWARD,
    "上下文菜单": key_mapping.ActionKind.CONTEXT_MENU,
    "打开 cmux": key_mapping.ActionKind.OPEN_CMUX,
}

# Preset choices shown in the mapping dropdown. Any other
# "mod+mod+key" text is still accepted as a custom shortcut through
# hotkey.HotkeySpec.parse.
ACTION_OPTION_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "按键操作",
        (
            "Escape", "回车", "退格", "方向上", "方向下", "方向左", "方向右",
            "tab", "space", "f5",
        ),
    ),
    (
        "鼠标操作",
        (
            "元素导航开关", "鼠标左键单击", "鼠标右键单击", "鼠标中键单击",
            "鼠标 X1 单击", "鼠标 X2 单击",
            "右键菜单",
        ),
    ),
    (
        "系统与媒体",
        (
            "显示桌面", "应用切换", "系统音量 +", "系统音量 −",
            "系统静音", "播放 / 暂停",
        ),
    ),
    ("语音", ("语音模式切换",)),
    (
        "启动应用",
        (
            _OPEN_APPLICATION_DISPLAY, "打开 Claude", "打开 cmux 终端",
            "打开 Cursor", "打开 Slack", "打开企业微信", "打开网易云音乐",
            "打开 Chrome", "打开 Edge", "打开 Zed",
        ),
    ),
    ("其他", ("禁用",)),
)

_PRESET_KEY_COMBOS = tuple(
    option
    for _group_title, group_options in ACTION_OPTION_GROUPS
    for option in group_options
)

ACTION_OPTION_GROUP_BY_LABEL = {
    option: group_title
    for group_title, group_options in ACTION_OPTION_GROUPS
    for option in group_options
}

ACTION_OPTION_GROUP_STARTS = frozenset(
    group_options[0]
    for _group_title, group_options in ACTION_OPTION_GROUPS
    if group_options
)
ACTION_OPTION_GROUP_BY_LABEL.update(
    {label: "鼠标长按" for label in LONG_PRESS_ACTION_LABELS.values()}
)
ACTION_OPTION_GROUP_STARTS |= {next(iter(LONG_PRESS_ACTION_LABELS.values()))}

_CTRL_HOTKEY_TOKENS = frozenset({"ctrl", "lctrl", "rctrl"})
_ALT_HOTKEY_TOKENS = frozenset({"alt", "lalt", "ralt"})

# Legacy generic voice text is still accepted so an older in-memory model can
# be saved without being parsed as a keyboard chord. New rows only expose the
# supported hold-to-talk slots below.
_VOICE_DISPLAY = "语音（使用专用组合键）"
_REMOVED_VOICE_DISPLAY = "已停用：旧语音配置（请重新选择）"

# Up to key_mapping.MAX_VOICE_SLOTS physical buttons can be promoted to an
# independent hold-to-talk lifecycle. Numbered labels let the buttons page and
# the voice page refer to the same slot ("语音1"/"语音2"). An unnamed key falls
# back to this table; a user-given name is registered at runtime through
# :func:`set_voice_slot_labels` so both pages render the same text.
_VOICE_SLOT_DISPLAYS: Dict[int, str] = {
    1: "语音1",
    2: "语音2",
    3: "语音3",
}
_VOICE_SLOT_LABELS: Dict[int, str] = {}
_VOICE_SLOT_BY_DISPLAY: Dict[str, int] = {
    label: slot for slot, label in _VOICE_SLOT_DISPLAYS.items()
}
# Spellings shipped by earlier releases stay resolvable so untouched saved
# dropdown text and older tests still map onto the right slot.
_LEGACY_VOICE_SLOT_DISPLAYS: Dict[str, int] = {
    "按住说话": 1,
    "语音一": 1,
    "语音二": 2,
    "语音三": 3,
}
_VOICE_SLOT_NUMBER_PATTERN = re.compile(r"^语音(\d+)$")
# Kept as the slot-1 spelling for callers/tests that used the old constant.
_VOICE_HOLD_DISPLAY = _VOICE_SLOT_DISPLAYS[1]
_PRIMARY_VOICE_DISPLAYS = tuple(
    _VOICE_SLOT_DISPLAYS[slot]
    for slot in sorted(_VOICE_SLOT_DISPLAYS)
    if slot <= key_mapping.MAX_VOICE_SLOTS
)


def set_voice_slot_labels(labels: Optional[Dict[int, str]]) -> None:
    """Register the user-visible name of every configured voice key."""

    _VOICE_SLOT_LABELS.clear()
    for raw_slot, raw_label in (labels or {}).items():
        try:
            slot = int(raw_slot)
        except (TypeError, ValueError):
            continue
        label = str(raw_label).strip()
        if slot > 0 and label:
            _VOICE_SLOT_LABELS[slot] = label

# Secondary gestures are optional.  Keep an explicit display value in the
# editable ComboBox so Qt does not fall back to the first real preset (usually
# ``escape``) when an older key_bindings.json has no secondary_bindings map.
SECONDARY_UNCONFIGURED_DISPLAY = "未设置"

_BUTTON_TRIGGER_LABELS = {
    key_mapping.ButtonTrigger.SINGLE_CLICK.value: "单击",
    key_mapping.ButtonTrigger.DOUBLE_CLICK.value: "双击",
    key_mapping.ButtonTrigger.LONG_PRESS.value: "长按",
}

# device_profile.ALL_BUTTON_IDS also carries "volume_mute", a HID usage-table
# entry kept for protocol compatibility (see key_mapping.py's module
# docstring) even though the physical RC003 has no dedicated mute key - only
# Volume + and Volume -. The settings window must not offer a mapping row a
# real remote can never actually trigger (XRBM-019 review round 1 P2), so
# every button list this module builds for display uses this narrowed set
# instead of ALL_BUTTON_IDS directly.
_USER_FACING_BUTTON_IDS = frozenset(device_profile.ALL_BUTTON_IDS - {"volume_mute"})

_ENDPOINT_NAME_HOST_API_SEPARATOR = " — "


class SettingsValidationError(Exception):
    """Raised by build_save_model on invalid input. ``button_id`` is None
    for a hotkey-level error, or the offending button's id for a mapping
    error.
    """

    def __init__(self, button_id: Optional[str], message: str) -> None:
        super().__init__(message)
        self.button_id = button_id
        self.message = message


def voice_slot_display(slot: int) -> str:
    """Return the dropdown label for one hold-to-talk slot."""

    try:
        slot_number = int(slot)
    except (TypeError, ValueError):
        return ""
    return (
        _VOICE_SLOT_LABELS.get(slot_number)
        or _VOICE_SLOT_DISPLAYS.get(slot_number)
        or f"语音{slot_number}"
    )


def voice_slot_for_display(text: str) -> Optional[int]:
    """Resolve a dropdown label (including legacy spellings) to a slot."""

    clean = str(text).strip()
    if not clean:
        return None
    for slot, label in _VOICE_SLOT_LABELS.items():
        if label == clean:
            return slot
    slot = _VOICE_SLOT_BY_DISPLAY.get(clean)
    if slot is not None:
        return slot
    legacy_slot = _LEGACY_VOICE_SLOT_DISPLAYS.get(clean)
    if legacy_slot is not None:
        return legacy_slot
    match = _VOICE_SLOT_NUMBER_PATTERN.match(clean)
    if match is None:
        return None
    number = int(match.group(1))
    if 1 <= number <= key_mapping.MAX_VOICE_SLOTS:
        return number
    return None


def is_voice_mode_cycle_display(text: str) -> bool:
    """True when the dropdown text is the “语音模式切换” label."""

    return str(text).strip() == _REFERENCE_ACTION_LABELS[
        key_mapping.ActionKind.VOICE_MODE_CYCLE
    ]


def _action_to_display(action: key_mapping.ButtonAction) -> str:
    if action.kind == key_mapping.ActionKind.KEY_COMBO and action.keys in (("pageup",), ("page_up",)):
        return "PageUp"
    if action.kind == key_mapping.ActionKind.KEY_COMBO and action.keys in (("pagedown",), ("page_down",)):
        return "PageDown"
    if action.kind == key_mapping.ActionKind.MOUSE_WHEEL_UP:
        return "PageUp"
    if action.kind == key_mapping.ActionKind.MOUSE_WHEEL_DOWN:
        return "PageDown"
    if action.kind == key_mapping.ActionKind.DISABLED:
        return "禁用"
    if action.kind == key_mapping.ActionKind.VOICE:
        return _REMOVED_VOICE_DISPLAY
    if action.kind == key_mapping.ActionKind.VOICE_TOGGLE:
        return _REMOVED_VOICE_DISPLAY
    voice_slot = key_mapping.voice_slot_for_action(action)
    if voice_slot is not None:
        return voice_slot_display(voice_slot)
    if action.kind == key_mapping.ActionKind.QUICKER_URI:
        return action.uri
    reference_label = _REFERENCE_ACTION_LABELS.get(action.kind)
    if reference_label is not None:
        return reference_label
    # Make old configs readable even before the loader has had a chance to
    # migrate them (e.g. a caller is rendering a raw document in a test).
    legacy_action = key_mapping.semantic_action_for_keys(action.keys)
    if legacy_action is not None:
        return _REFERENCE_ACTION_LABELS[legacy_action.kind]
    return "+".join(action.keys)


def normalize_mapping_hotkey_text(text: str) -> str:
    """Validate manual button-mapping text and return persisted key tokens.

    Names use the shared editor codec. Mapping-specific policy treats a lone
    Win as the left Windows key and rejects secure attention sequences;
    voice providers keep their own restrictions.
    """

    parsed = hotkey.HotkeySpec.from_user_text(text, mapping=True)
    resolved_tokens = tuple(parsed.modifiers) + (parsed.key,)
    if (
        win32_keys.resolve_vk_codes((parsed.key,)) == [win32_keys.VK_CODES["delete"]]
        and _CTRL_HOTKEY_TOKENS.intersection(resolved_tokens)
        and _ALT_HOTKEY_TOKENS.intersection(resolved_tokens)
    ):
        raise hotkey.HotkeyParseError(
            "Ctrl+Alt+Delete 是 Windows 安全按键，软件不能模拟。"
        )
    try:
        win32_keys.resolve_vk_codes(resolved_tokens)
    except win32_keys.UnknownKeyTokenError as exc:
        raise hotkey.HotkeyParseError(str(exc)) from exc
    return "+".join(resolved_tokens)


def _display_to_action(text: str) -> key_mapping.ButtonAction:
    text = text.strip()
    if text in ("禁用", "disabled", SECONDARY_UNCONFIGURED_DISPLAY):
        return key_mapping.ButtonAction(key_mapping.ActionKind.DISABLED)
    if text == _VOICE_DISPLAY:
        return key_mapping.ButtonAction(key_mapping.ActionKind.VOICE)
    voice_slot = voice_slot_for_display(text)
    if voice_slot is not None:
        slot_action = key_mapping.voice_action_for_slot(voice_slot)
        if slot_action is not None:
            return slot_action
    if text == "系统音量 -":
        text = "系统音量 −"
    reference_kind = _REFERENCE_ACTION_KINDS_BY_LABEL.get(text)
    if reference_kind is None:
        reference_kind = _LEGACY_REFERENCE_ACTION_KINDS_BY_LABEL.get(text)
    if reference_kind is not None:
        return key_mapping.ButtonAction(reference_kind)
    # Keep the previous spelling accepted for users who copied the macOS
    # reference label into the Windows field.
    if text == "Command-Tab":
        return key_mapping.ButtonAction(key_mapping.ActionKind.APP_SWITCHER)
    # The .72 PageUp/PageDown options use these persisted spellings when
    # converting saved wheel actions. Keep that storage format stable.
    if text in ("PageUp", "PageDown"):
        return key_mapping.ButtonAction(
            key_mapping.ActionKind.KEY_COMBO, (text.lower(),)
        )
    if text.casefold().startswith("quicker:"):
        try:
            uri = key_mapping.normalize_quicker_uri(text)
        except (TypeError, ValueError) as exc:
            raise hotkey.HotkeyParseError(
                "Quicker URI 必须使用 quicker:runaction:动作ID或名称，可在末尾附加 ?参数。"
            ) from exc
        return key_mapping.ButtonAction(
            key_mapping.ActionKind.QUICKER_URI,
            uri=uri,
        )
    normalized_hotkey = normalize_mapping_hotkey_text(text)
    return key_mapping.ButtonAction(
        key_mapping.ActionKind.KEY_COMBO, tuple(normalized_hotkey.split("+"))
    )


def format_action_text(text: str) -> str:
    """Format only key chords; semantic actions and incomplete drafts stay intact."""
    try:
        action = _display_to_action(text)
    except hotkey.HotkeyParseError:
        action = None
    if action is not None and action.kind != key_mapping.ActionKind.KEY_COMBO:
        return text
    # Button actions replay the stored list directly, unlike voice hotkeys
    # whose parser orders modifiers before a trigger. Never reorder a saved
    # mapping just to render it, including labels formatted more than once.
    try:
        tokens = [
            win32_keys.key_token_from_text(part)
            for part in text.replace("＋", "+").split("+")
        ]
    except win32_keys.UnknownKeyTokenError:
        return text
    return " + ".join(win32_keys.key_label(token) for token in tokens)


def normalize_action_text(text: str) -> str:
    """Keep model storage canonical, including after editing a displayed label."""
    try:
        action = _display_to_action(text)
    except hotkey.HotkeyParseError:
        return text
    return "+".join(action.keys) if action.kind == key_mapping.ActionKind.KEY_COMBO else text


def action_text_matches_original(text: str, original: str) -> bool:
    """Compare editor labels with the stored, ordered key sequence."""
    if text.strip() in (original, format_action_text(original)):
        return True
    for candidate in (original, text):
        try:
            if _display_to_action(candidate).kind != key_mapping.ActionKind.KEY_COMBO:
                return False
        except hotkey.HotkeyParseError:
            pass
    try:
        tokens = [
            win32_keys.key_token_from_text(part)
            for part in text.replace("＋", "+").split("+")
        ]
        return win32_keys.resolve_vk_codes(tokens) == win32_keys.resolve_vk_codes(
            original.split("+")
        )
    except win32_keys.UnknownKeyTokenError:
        return False


def _validated_button_action(
    button_id: str,
    trigger: str,
    text: str,
    *,
    base_bindings: Optional[dict] = None,
    inject_mode: str = key_mapping.DEFAULT_INJECT_MODE,
) -> Optional[key_mapping.ButtonAction]:
    """Parse one editor field using the same rules as the final save.

    ``inject_mode`` is the per-button backend the editor picked (SendInput or
    Interception). It is stamped onto the parsed action so an unchanged text
    field still remembers the user's choice instead of silently reverting to
    the default backend.
    """

    trigger_label = _BUTTON_TRIGGER_LABELS.get(trigger)
    if trigger_label is None:
        raise SettingsValidationError(button_id, f"不支持的按键操作：{trigger}")

    mode = key_mapping.normalize_stored_inject_mode(inject_mode)
    clean_text = str(text).strip()
    if not clean_text:
        return None
    if trigger != key_mapping.ButtonTrigger.SINGLE_CLICK.value and clean_text in (
        "禁用",
        "disabled",
        SECONDARY_UNCONFIGURED_DISPLAY,
    ):
        return None
    if clean_text == _REMOVED_VOICE_DISPLAY:
        raise SettingsValidationError(
            button_id,
            f"{trigger_label}：旧语音配置已停用，请重新选择",
        )

    if base_bindings is not None:
        original = key_mapping.button_action_for(
            base_bindings, button_id, key_mapping.ButtonTrigger(trigger)
        )
        if original.kind == key_mapping.ActionKind.KEY_COMBO:
            original_text = "+".join(original.keys)
            if action_text_matches_original(clean_text, original_text):
                # Retain untouched legacy values, including keys no longer
                # accepted for new input. The sender still rejects unsafe VKs.
                _ensure_interception_supports(button_id, trigger_label, original, mode)
                return replace(original, inject_mode=mode)

    try:
        action = _display_to_action(clean_text)
    except hotkey.HotkeyParseError as exc:
        detail = str(exc).strip()
        message = f"{trigger_label}：{detail}"
        raise SettingsValidationError(button_id, message) from exc

    if action.kind in key_mapping.MOUSE_HOLD_BUTTONS and trigger != "long_press":
        raise SettingsValidationError(button_id, "鼠标长按只能用于长按映射。")
    if key_mapping.is_voice_action(action):
        if trigger != key_mapping.ButtonTrigger.SINGLE_CLICK.value:
            raise SettingsValidationError(
                button_id,
                f"{trigger_label}：语音动作只能用于主映射，请选择普通动作或快捷键",
            )
    _ensure_interception_supports(button_id, trigger_label, action, mode)
    return replace(action, inject_mode=mode)


def _ensure_interception_supports(
    button_id: str,
    trigger_label: str,
    action: key_mapping.ButtonAction,
    mode: str,
) -> None:
    """Reject Interception for an action it cannot reproduce.

    Only actions that inject a key or a mouse button go through the backend.
    Voice and "no action" are delivered by their own paths and keep whatever
    backend is stored, but an action that has a dedicated host path of its own
    (element navigation, quick links, open-app, mouse hold) has no
    Interception equivalent at all, so the mode must not be selectable for it.
    """

    if mode != key_mapping.INJECT_MODE_INTERCEPTION:
        return
    if key_mapping.is_voice_action(action):
        return
    if action.kind == key_mapping.ActionKind.DISABLED:
        return
    if action.kind == key_mapping.ActionKind.VOICE_MODE_CYCLE:
        # 语音模式切换由应用自己处理，不经过任何注入后端。
        return
    if injection_backend.action_supported_by_interception(action):
        return
    if (
        injection_backend.key_tokens_for_action(action) is None
        and injection_backend.mouse_button_for_action(action) is None
    ):
        raise SettingsValidationError(
            button_id,
            f"{trigger_label}：该动作不是普通按键动作，无法使用 Interception（驱动级），请改用 SendInput",
        )
    raise SettingsValidationError(
        button_id,
        f"{trigger_label}：该按键无法通过 Interception 注入，请改用 SendInput",
    )


def _resolve_inject_mode(
    button_inject_modes: Optional[Dict[str, str]], button_id: str
) -> str:
    """The backend chosen for one button, defaulting to SendInput."""

    if not isinstance(button_inject_modes, dict):
        return key_mapping.DEFAULT_INJECT_MODE
    return key_mapping.normalize_stored_inject_mode(button_inject_modes.get(button_id))



def button_action_validation_message(
    button_id: str,
    trigger: str,
    text: str,
    *,
    base_bindings: Optional[dict] = None,
    inject_mode: str = key_mapping.DEFAULT_INJECT_MODE,
) -> str:
    """Return a user-facing editor error, or an empty string when valid."""

    try:
        _validated_button_action(
            button_id,
            trigger,
            text,
            base_bindings=base_bindings,
            inject_mode=inject_mode,
        )
    except SettingsValidationError as exc:
        return exc.message
    return ""


def _endpoint_display(endpoint: audio_output.AudioEndpoint) -> str:
    if endpoint.host_api:
        return f"{endpoint.name}{_ENDPOINT_NAME_HOST_API_SEPARATOR}{endpoint.host_api}"
    return endpoint.name


def _parse_endpoint_display(text: str) -> Tuple[str, str]:
    """Inverse of _endpoint_display: returns (name, host_api), where
    host_api is "" if the text has no disambiguating suffix.
    """

    text = text.strip()
    if _ENDPOINT_NAME_HOST_API_SEPARATOR in text:
        name, host_api = text.rsplit(_ENDPOINT_NAME_HOST_API_SEPARATOR, 1)
        return name.strip(), host_api.strip()
    return text, ""


def _resolve_slot_hotkeys(
    voice_slot_hotkeys: Optional[Dict[str, str]],
    voice_hotkeys: Optional[Dict[str, str]],
    hotkey_text: str,
) -> Dict[int, str]:
    """Coerce the per-slot shortcut inputs into ``{slot: hotkey_text}``.

    ``voice_slot_hotkeys`` is the current contract. ``voice_hotkeys`` and the
    scalar ``hotkey_text`` remain accepted so older callers (and the shared
    hold-shortcut mirror) keep writing slot 1 unchanged. An empty slot 1 in
    ``voice_slot_hotkeys`` means "not recorded yet" and falls back to that
    shared mirror, matching :func:`config.voice_slot_hotkey`.
    """

    slots = set(_VOICE_SLOT_DISPLAYS)
    if voice_slot_hotkeys is not None:
        for raw_slot in voice_slot_hotkeys:
            try:
                slots.add(int(raw_slot))
            except (TypeError, ValueError):
                continue
    result: Dict[int, str] = {slot: "" for slot in sorted(slots)}
    if voice_slot_hotkeys is not None:
        for slot in result:
            result[slot] = str(voice_slot_hotkeys.get(str(slot), "")).strip()
        if not result[1]:
            result[1] = _resolve_hold_shortcut(voice_hotkeys, hotkey_text)
        return result
    result[1] = _resolve_hold_shortcut(voice_hotkeys, hotkey_text)
    return result


def _resolve_hold_shortcut(
    voice_hotkeys: Optional[Dict[str, str]], hotkey_text: str
) -> str:
    if voice_hotkeys is not None:
        return str(voice_hotkeys.get("hold", "")).strip()
    return str(hotkey_text).strip()


def build_save_model(
    *,
    button_display_map: Dict[str, str],
    secondary_display_map: Optional[Dict[str, Dict[str, str]]] = None,
    display_note_map: Optional[Dict[str, Dict[str, str]]] = None,
    hotkey_text: str,
    endpoint_display_text: str,
    base_config: dict,
    base_bindings: dict,
    selected_device_profile: str = device_catalog.RC003_ID,
    voice_hotkeys: Optional[Dict[str, str]] = None,
    voice_slot_hotkeys: Optional[Dict[str, str]] = None,
    validate_voice_hotkeys: bool = True,
    button_inject_modes: Optional[Dict[str, str]] = None,
) -> Tuple[dict, dict]:
    """Pure validation+build step for "Save"/"Restore defaults", with no Tk
    dependency at all - directly unit tested without constructing any
    window (see tests/test_settings_ui_helpers.py). Raises
    SettingsValidationError on invalid input; never raises a Tk exception.
    """

    from . import remote_layout
    allowed_buttons = set(remote_layout.button_order(selected_device_profile))
    slot_hotkeys = _resolve_slot_hotkeys(
        voice_slot_hotkeys, voice_hotkeys, hotkey_text
    )
    mode_hotkeys = {"hold": slot_hotkeys.get(1, "")}

    bindings: Dict[str, dict] = {}
    voice_slot_bindings: Dict[int, str] = {}
    for button_id, text in button_display_map.items():
        action = _validated_button_action(
            button_id,
            key_mapping.ButtonTrigger.SINGLE_CLICK.value,
            text,
            base_bindings=base_bindings,
            inject_mode=_resolve_inject_mode(button_inject_modes, button_id),
        )
        if action is None:
            continue
        voice_slot = key_mapping.voice_slot_for_action(action)
        if voice_slot is not None:
            occupant = voice_slot_bindings.get(voice_slot)
            if occupant is not None and occupant != button_id:
                raise SettingsValidationError(
                    button_id,
                    f"{voice_slot_display(voice_slot)}已分配给按键“{occupant}”，"
                    "每个语音标号只能对应一个按键",
                )
            slot_action = key_mapping.voice_action_for_slot(voice_slot)
            if slot_action is None:
                raise SettingsValidationError(button_id, "语音标号无效，请重新选择。")
            voice_slot_bindings[voice_slot] = button_id
            # 保留用户在编辑器里选的注入后端，否则语音键重开后会退回默认。
            action = replace(slot_action, inject_mode=action.inject_mode)
        bindings[button_id] = action.to_dict()

    # An empty slot means "not recorded yet": it is a valid saved state, and the
    # runtime simply refuses to inject that slot's chord until the user records
    # one (see RC003App._prepare_voice_mapping_locked). Only the recorded values
    # are validated here.
    if validate_voice_hotkeys:
        for slot in sorted(slot_hotkeys):
            candidate = slot_hotkeys[slot]
            if not candidate:
                continue
            try:
                parsed_hotkey = hotkey.HotkeySpec.parse(candidate)
                win32_keys.resolve_vk_codes(
                    tuple(parsed_hotkey.modifiers) + (parsed_hotkey.key,)
                )
            except hotkey.HotkeyParseError as exc:
                raise SettingsValidationError(
                    None, f"{voice_slot_display(slot)}快捷键：{exc}"
                ) from exc
            except win32_keys.UnknownKeyTokenError as exc:
                raise SettingsValidationError(
                    None, f"{voice_slot_display(slot)}快捷键：{exc}"
                ) from exc
    active_hotkey_text = slot_hotkeys.get(1, "")

    if secondary_display_map is None:
        raw_secondary = base_bindings.get("secondary_bindings", {})
        secondary_bindings = (
            copy.deepcopy(raw_secondary) if isinstance(raw_secondary, dict) else {}
        )
        for button_id, trigger_map in secondary_bindings.items():
            if not isinstance(trigger_map, dict):
                continue
            for trigger_name, raw_action in trigger_map.items():
                try:
                    action = key_mapping.ButtonAction.from_dict(raw_action)
                except (KeyError, TypeError, ValueError) as exc:
                    raise SettingsValidationError(
                        button_id,
                        "双击或长按动作配置无效，请重新选择后保存。",
                    ) from exc
                if action.kind == key_mapping.ActionKind.DISABLED:
                    continue
                if action.kind in key_mapping.MOUSE_HOLD_BUTTONS and trigger_name != "long_press":
                    raise SettingsValidationError(button_id, "鼠标长按只能用于长按映射。")
                if key_mapping.is_voice_action(action):
                    raise SettingsValidationError(
                        button_id,
                        "语音动作只能用于主映射，不能设置为双击或长按动作。",
                    )
    else:
        secondary_bindings: Dict[str, Dict[str, dict]] = {}
        for button_id, trigger_map in secondary_display_map.items():
            if not isinstance(trigger_map, dict):
                continue
            for trigger_name, text in trigger_map.items():
                action = _validated_button_action(
                    button_id,
                    trigger_name,
                    text,
                    base_bindings=base_bindings,
                    inject_mode=_resolve_inject_mode(button_inject_modes, button_id),
                )
                if action is None:
                    continue
                if action.kind == key_mapping.ActionKind.DISABLED:
                    continue
                secondary_bindings.setdefault(button_id, {})[trigger_name] = action.to_dict()

    if display_note_map is None:
        raw_display_notes = base_bindings.get("display_notes", {})
        display_notes = (
            copy.deepcopy(raw_display_notes)
            if isinstance(raw_display_notes, dict)
            else {}
        )
    else:
        display_notes: Dict[str, Dict[str, str]] = {}
        valid_note_triggers = {
            key_mapping.ButtonTrigger.SINGLE_CLICK.value,
            key_mapping.ButtonTrigger.DOUBLE_CLICK.value,
            key_mapping.ButtonTrigger.LONG_PRESS.value,
        }
        for button_id, trigger_map in display_note_map.items():
            if button_id not in allowed_buttons or not isinstance(
                trigger_map, dict
            ):
                continue
            for trigger_name, note in trigger_map.items():
                if trigger_name not in valid_note_triggers:
                    continue
                if not isinstance(note, str):
                    continue
                clean_note = note.strip()
                if clean_note and clean_note != "未命名":
                    display_notes.setdefault(button_id, {})[
                        trigger_name
                    ] = clean_note

    # RC003 remote-button combinations are retired. Keep the normalized
    # legacy document opaque for downgrade compatibility, but never let it
    # participate in validation of the current visible mappings.
    raw_combo = base_bindings.get("combo_bindings", {})
    combo_bindings = copy.deepcopy(raw_combo) if isinstance(raw_combo, dict) else {}

    endpoint_name, endpoint_host_api = _parse_endpoint_display(endpoint_display_text)

    new_config = dict(base_config)
    new_config["selected_device_profile"] = device_catalog.normalize_device_id(
        selected_device_profile
    )
    new_config["voice_hotkey"] = active_hotkey_text
    new_config["voice_hotkeys"] = mode_hotkeys
    new_config["voice_slot_hotkeys"] = {
        str(slot): slot_hotkeys.get(slot, "")
        for slot in sorted(slot_hotkeys)
    }
    # The per-provider trigger-mode selector is gone in schema 18; drop the
    # legacy key instead of pinning it to a value.
    new_config.pop("voice_trigger_mode", None)
    new_config.pop("voice_release_finish_tap_enabled", None)
    new_config["output_endpoint_name"] = endpoint_name
    new_config["output_endpoint_host_api"] = endpoint_host_api

    new_bindings = dict(base_bindings)
    new_bindings["bindings"] = bindings
    new_bindings["secondary_bindings"] = secondary_bindings
    new_bindings["display_notes"] = display_notes
    new_bindings["combo_bindings"] = combo_bindings

    return new_config, new_bindings


@dataclass(frozen=True)
class DefaultDisplayState:
    """What "restore defaults" resets every widget to - a pure snapshot, so
    it can be asserted on directly in tests without touching a StringVar.
    """

    button_display_map: Dict[str, str]
    secondary_display_map: Dict[str, Dict[str, str]]
    hotkey_text: str
    voice_hotkeys: Dict[str, str]


def default_display_state(profile: str = "xiaomi-rc003") -> DefaultDisplayState:
    from . import remote_layout
    defaults = remote_layout.default_actions(profile)
    button_display_map = {
        button_id: _action_to_display(action) for button_id, action in defaults.items()
    }
    for button_id in remote_layout.button_order(profile):
        button_display_map.setdefault(button_id, "")
    secondary_display_map = {
        button_id: {
            key_mapping.ButtonTrigger.DOUBLE_CLICK.value: "",
            key_mapping.ButtonTrigger.LONG_PRESS.value: "",
        }
        for button_id in remote_layout.button_order(profile)
    }
    voice_hotkeys = {"hold": ""}
    return DefaultDisplayState(
        button_display_map=button_display_map,
        secondary_display_map=secondary_display_map,
        hotkey_text=voice_hotkeys["hold"],
        voice_hotkeys=voice_hotkeys,
    )


# Bridge-control status text (XRBM-029). Kept as pure, Tk-free functions -
# same testability contract as the save-model helpers above (see
# tests/test_settings_ui_helpers.py) - so every stable state is asserted on
# directly without constructing a window or a real subprocess.
#
# Wording contract: a STARTED result is deliberately never described as
# "RC003 已连接"/"RC003 connected" - only as the process itself still being
# alive. The settings controller continues polling the runtime status and
# promotes the UI to the connected state only after the bridge reports it.
LAUNCH_NOT_STARTED_TEXT = "服务未运行；点击“启动服务”后，按键和语音才会生效"
LAUNCH_ALREADY_RUNNING_TEXT = (
    f"服务已在运行；正在检查{device_catalog.RC003_DISPLAY_NAME}的连接"
)
LAUNCH_STATUS_UNKNOWN_TEXT = "无法确认服务状态；请查看 app.log，勿重复启动"


def describe_launch_result(result: bridge_launcher.LaunchResult) -> str:
    if result.outcome is bridge_launcher.LaunchOutcome.STARTED:
        pid_text = f"（PID {result.pid}）" if result.pid is not None else ""
        return (
            f"服务已启动{pid_text}；正在连接{device_catalog.RC003_DISPLAY_NAME}，"
            "首次可能约 1 分钟"
        )
    if result.outcome is bridge_launcher.LaunchOutcome.ALREADY_RUNNING:
        return f"服务已在运行（代码 {result.exit_code}）；无需重复启动"
    if result.outcome is bridge_launcher.LaunchOutcome.STATUS_UNKNOWN:
        pid_text = f"（PID {result.pid}）" if result.pid is not None else ""
        return f"服务已创建{pid_text}，但状态未知；请查看 app.log，勿重复启动"
    if result.outcome is bridge_launcher.LaunchOutcome.QUICK_EXIT:
        return f"服务启动后立即退出（代码 {result.exit_code}）；请查看 app.log"
    # LAUNCH_FAILED
    return f"服务启动失败：{result.error}；请查看 app.log"


def describe_log_open_result(result: logging_setup.LogOpenResult) -> str:
    if result.outcome is logging_setup.LogOpenOutcome.OPENED:
        note = ""
        if result.location.status is logging_setup.LogLocationStatus.FILE_MISSING:
            note = "（该目录存在，但 app.log 尚不存在——桥接可能还没有运行过一次，这不是错误。）"
        return f"已打开日志目录：{result.location.directory}{note}"
    if result.outcome is logging_setup.LogOpenOutcome.DIRECTORY_MISSING:
        return (
            f"日志目录尚不存在：{result.location.directory}。这通常表示桥接程序在这台"
            "电脑上还没有运行过；本程序不会为了显示而伪造日志。"
        )
    return f"无法打开日志目录（{result.error}）：{result.location.directory}"


def main(*, start_hidden: bool = False, start_bridge: bool = False) -> None:
    """Launches the Qt Quick/QML settings window (XRBM-030). Imports
    ``qt_settings_app`` lazily so importing THIS module (e.g. from
    ``__main__.py``'s ``--dry-run`` smoke check, or from any test that only
    needs the pure functions above) never requires PySide6 to be installed -
    see ``qt_settings_app.py``'s module docstring for the exact, clear error
    raised here if it is missing.
    """

    from . import qt_settings_app

    qt_settings_app.run_settings_window(
        start_hidden=start_hidden,
        start_bridge=start_bridge,
    )


if __name__ == "__main__":
    main()
