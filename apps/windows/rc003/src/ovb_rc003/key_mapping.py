"""RC003 -> Windows *semantic* action mapping.

The reference app stores behavior actions (``arrowUp``, ``showDesktop``,
``openCodex``), not presentation strings such as ``"up"`` or
``"win+d"``.  Windows keeps that same separation: this module is pure action
data, while :mod:`app` and :mod:`win32_input` execute each action through its
own platform operation.  ``KEY_COMBO`` remains available for a genuinely
custom user shortcut and for backward-compatible hand-edited files.

Default table matches the reference app's action choices:

| RC003 按键 | Windows 候选动作 |
| --- | --- |
| 麦克风 | 专用语音生命周期 |
| 电源 | Escape |
| 上 / 下 / 左 / 右 | 对应方向动作 |
| 确定 | 回车 |
| 返回 | 退格 |
| 音量 + / − | 系统音量 + / − |
| 主页 | 显示桌面 |
| 菜单 | 右键菜单 |
| TV | 应用切换 |

The RC003 HID usage table also defines a "volume_mute" usage (see
device_profile.BUTTON_USAGE_IDS), but this Windows client documents that the
physical remote has no dedicated mute key - "系统静音" is only an optional
assignable action, never a default. This module mirrors that: mute is a valid,
bindable logical button but intentionally has no default entry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple


# Per-button injection backend. These strings are the single source of truth;
# :mod:`injection_backend` imports them rather than redefining them. They live
# here because ``ButtonAction`` persists the value, and a module-level import
# of ``injection_backend`` from here would be circular.
DEFAULT_INJECT_MODE = "sendinput"
INJECT_MODE_INTERCEPTION = "interception"
INJECT_MODES = (DEFAULT_INJECT_MODE, INJECT_MODE_INTERCEPTION)


def normalize_stored_inject_mode(value: object) -> str:
    """Coerce a persisted injection mode, defaulting to SendInput."""

    mode = str(value or "").strip().lower()
    return mode if mode in INJECT_MODES else DEFAULT_INJECT_MODE


# Persisted prefix of one hold-to-talk voice key action. Slot 1 keeps the
# historical bare value; every later slot appends its own numeric id.
VOICE_HOLD_KIND_PREFIX = "voice_hold"
_VOICE_HOLD_VALUE_PATTERN = re.compile(r"^voice_hold_(\d+)$")


class ActionKind(str, Enum):
    DISABLED = "disabled"
    KEY_COMBO = "key_combo"
    QUICKER_URI = "quicker_uri"
    ESCAPE = "escape"
    RETURN = "return"
    ARROW_UP = "arrow_up"
    ARROW_DOWN = "arrow_down"
    ARROW_LEFT = "arrow_left"
    ARROW_RIGHT = "arrow_right"
    DELETE_BACKWARD = "delete_backward"
    SHOW_DESKTOP = "show_desktop"
    CONTEXT_MENU = "context_menu"
    APP_SWITCHER = "app_switcher"
    SYSTEM_VOLUME_UP = "system_volume_up"
    SYSTEM_VOLUME_DOWN = "system_volume_down"
    SYSTEM_VOLUME_MUTE = "system_volume_mute"
    PLAY_PAUSE = "play_pause"
    MOUSE_LEFT_CLICK = "mouse_left_click"
    MOUSE_RIGHT_CLICK = "mouse_right_click"
    MOUSE_MIDDLE_CLICK = "mouse_middle_click"
    # Retained only to read bindings saved by older releases.
    MOUSE_WHEEL_UP = "mouse_wheel_up"
    MOUSE_WHEEL_DOWN = "mouse_wheel_down"
    MOUSE_X1_CLICK = "mouse_x1_click"
    MOUSE_X2_CLICK = "mouse_x2_click"
    MOUSE_LEFT_HOLD = "mouse_left_hold"
    MOUSE_RIGHT_HOLD = "mouse_right_hold"
    MOUSE_X1_HOLD = "mouse_x1_hold"
    MOUSE_X2_HOLD = "mouse_x2_hold"
    ELEMENT_NAVIGATION_TOGGLE = "element_navigation_toggle"
    # ``VOICE`` and ``VOICE_TOGGLE`` remain parseable only so schema-1 files
    # can be failed closed with an explicit migration notice. They are not
    # selectable or executable product actions anymore.
    VOICE = "voice"
    VOICE_TOGGLE = "voice_toggle"
    # Hold-to-talk slots. Slot 1 keeps the original persisted value so every
    # schema-1/2 file that already maps the physical microphone to
    # ``voice_hold`` keeps working unchanged; every later slot uses
    # ``voice_hold_<slot>`` and is resolved on demand by ``_missing_`` so the
    # user can create as many voice keys as they want.
    VOICE_HOLD = "voice_hold"
    VOICE_HOLD_2 = "voice_hold_2"
    VOICE_HOLD_3 = "voice_hold_3"
    # 切到下一套语音键。它是普通按键动作（不是语音动作），所以可以映射到
    # 任意按键的单击/双击/长按；是否可用由「使用其他按键切换模式」开关决定。
    VOICE_MODE_CYCLE = "voice_mode_cycle"
    OPEN_REMOTE_MIC = "open_remote_mic"
    OPEN_CODEX = "open_codex"
    OPEN_CLAUDE = "open_claude"
    OPEN_CMUX = "open_cmux"
    OPEN_WECHAT = "open_wechat"
    OPEN_CURSOR = "open_cursor"
    OPEN_SLACK = "open_slack"
    OPEN_WECOM = "open_wecom"
    OPEN_NETEASE_MUSIC = "open_netease_music"
    OPEN_CHROME = "open_chrome"
    OPEN_EDGE = "open_edge"
    OPEN_ZED = "open_zed"
    # 直接控制 OpenMexia 里的一台米家设备。动作本身只描述「哪台设备 + 哪个
    # 操作」，具体怎么执行由 OpenMexia 自己决定。
    OPENMEXIA_DEVICE = "openmexia_device"

    @classmethod
    def _missing_(cls, value: object):
        """Resolve ``voice_hold_<n>`` for voice keys beyond the built-in ones.

        Voice keys are user-created and unbounded, so their action kinds cannot
        be declared up front. Caching the new member keeps ``ActionKind``
        identity-stable: parsing the same stored value twice returns the very
        same member object, which every ``==`` comparison relies on.
        """

        if not isinstance(value, str):
            return None
        text = value.strip().lower()
        match = _VOICE_HOLD_VALUE_PATTERN.match(text)
        if match is None:
            return None
        number = int(match.group(1))
        if number < 2 or number > MAX_VOICE_SLOTS:
            return None
        member = str.__new__(cls, text)
        member._name_ = "VOICE_HOLD_" + match.group(1)
        member._value_ = text
        cls._value2member_map_[text] = member
        return member


MOUSE_HOLD_BUTTONS = {
    ActionKind.MOUSE_LEFT_HOLD: "left",
    ActionKind.MOUSE_RIGHT_HOLD: "right",
    ActionKind.MOUSE_X1_HOLD: "x1",
    ActionKind.MOUSE_X2_HOLD: "x2",
}


class ButtonTrigger(str, Enum):
    """The three gestures available for an ordinary RC003 button."""

    SINGLE_CLICK = "single_click"
    DOUBLE_CLICK = "double_click"
    LONG_PRESS = "long_press"


VOICE_ACTION_KINDS = frozenset(
    {
        ActionKind.VOICE,
        ActionKind.VOICE_TOGGLE,
        ActionKind.VOICE_HOLD,
        ActionKind.VOICE_HOLD_2,
        ActionKind.VOICE_HOLD_3,
    }
)

# A voice slot is one independent hold-to-talk lifecycle owned by one named
# voice-key configuration. Slot 1 is the historical single voice key (the
# physical microphone). Slots 2..MAX_VOICE_SLOTS are user created, so the
# upper bound only protects the persisted id space - it is not a product limit
# the UI needs to surface.
MAX_VOICE_SLOTS = 99

VOICE_SLOT_ACTION_KINDS = frozenset(
    {
        ActionKind.VOICE_HOLD,
        ActionKind.VOICE_HOLD_2,
        ActionKind.VOICE_HOLD_3,
    }
)

VOICE_SLOT_BY_ACTION_KIND = {
    ActionKind.VOICE_HOLD: 1,
    ActionKind.VOICE_HOLD_2: 2,
    ActionKind.VOICE_HOLD_3: 3,
}

VOICE_ACTION_KIND_BY_SLOT = {
    slot: kind for kind, slot in VOICE_SLOT_BY_ACTION_KIND.items()
}

SUPPORTED_VOICE_ACTION_KINDS = VOICE_SLOT_ACTION_KINDS


def voice_kind_for_slot(slot: object) -> Optional[ActionKind]:
    """Return the action kind for one 1-based voice slot, or ``None``."""

    try:
        number = int(slot)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number < 1 or number > MAX_VOICE_SLOTS:
        return None
    if number == 1:
        return ActionKind.VOICE_HOLD
    return ActionKind(f"{VOICE_HOLD_KIND_PREFIX}_{number}")


def voice_slot_from_kind(kind: object) -> Optional[int]:
    """Return the 1-based voice slot encoded by ``kind``, or ``None``."""

    value = kind.value if isinstance(kind, ActionKind) else str(kind or "")
    value = value.strip().lower()
    if value == ActionKind.VOICE_HOLD.value:
        return 1
    match = _VOICE_HOLD_VALUE_PATTERN.match(value)
    if match is None:
        return None
    number = int(match.group(1))
    if number < 2 or number > MAX_VOICE_SLOTS:
        return None
    return number


def is_voice_action(action: "ButtonAction") -> bool:
    """Return whether ``action`` is current or legacy RC003 voice data."""

    if action.kind in (ActionKind.VOICE, ActionKind.VOICE_TOGGLE):
        return True
    return voice_slot_from_kind(action.kind) is not None


def is_supported_voice_action(action: "ButtonAction") -> bool:
    """Return whether ``action`` is an executable RC003 voice action."""

    return voice_slot_from_kind(action.kind) is not None


def voice_slot_for_action(action: "ButtonAction") -> Optional[int]:
    """Return the 1-based voice slot for ``action``, or ``None``."""

    return voice_slot_from_kind(action.kind)


def voice_action_for_slot(slot: int) -> Optional["ButtonAction"]:
    """Build the hold-to-talk action for ``slot`` (1..MAX_VOICE_SLOTS)."""

    kind = voice_kind_for_slot(slot)
    if kind is None:
        return None
    return ButtonAction(kind)


# These two tokens were shipped by earlier Windows builds as an implicit
# hold-to-talk shortcut. They are kept only so schema-1 data can still be
# recognised and failed closed during migration.
LEGACY_HOLD_VOICE_HOTKEY = "ralt"
LEGACY_TOGGLE_VOICE_HOTKEY = "ralt+space"

# A fresh installation no longer fabricates a wake shortcut: the product only
# replays the shortcut the user records for slot 1/2.

# These values were shipped by earlier Windows builds. They remain recognizable
# so old configuration can keep its prior compatibility behavior without being
# mistaken for the default of a fresh installation.
LEGACY_VOICE_HOTKEYS = frozenset(
    {
        LEGACY_HOLD_VOICE_HOTKEY,
        LEGACY_TOGGLE_VOICE_HOTKEY,
        "lctrl+win",
        "lctrl+lwin",
    }
)


# Exact old Windows chords that were previously presented as the reference
# action labels.  These are migrations, not the representation used for new
# saves.  ``alt+esc`` is included because the first Windows build shipped it
# as the TV default before it was corrected to the native app-switch action.
LEGACY_SEMANTIC_ACTIONS = {
    ("escape",): ActionKind.ESCAPE,
    ("enter",): ActionKind.RETURN,
    ("up",): ActionKind.ARROW_UP,
    ("down",): ActionKind.ARROW_DOWN,
    ("left",): ActionKind.ARROW_LEFT,
    ("right",): ActionKind.ARROW_RIGHT,
    ("backspace",): ActionKind.DELETE_BACKWARD,
    ("win", "d"): ActionKind.SHOW_DESKTOP,
    ("shift", "f10"): ActionKind.CONTEXT_MENU,
    ("alt", "tab"): ActionKind.APP_SWITCHER,
    ("alt", "esc"): ActionKind.APP_SWITCHER,
}


APPLICATION_ACTIONS = frozenset(
    {
        ActionKind.OPEN_REMOTE_MIC,
        ActionKind.OPEN_CODEX,
        ActionKind.OPEN_CLAUDE,
        ActionKind.OPEN_CMUX,
        ActionKind.OPEN_WECHAT,
        ActionKind.OPEN_CURSOR,
        ActionKind.OPEN_SLACK,
        ActionKind.OPEN_WECOM,
        ActionKind.OPEN_NETEASE_MUSIC,
        ActionKind.OPEN_CHROME,
        ActionKind.OPEN_EDGE,
        ActionKind.OPEN_ZED,
    }
)

QUICKER_URI_PREFIX = "quicker:runaction:"
MAX_QUICKER_URI_LENGTH = 2048

# The first combination implementation deliberately behaves like one Fn
# layer.  Only a non-repeating utility key may be the modifier, while the
# second key stays in the ordinary navigation cluster.  This avoids exposing
# the microphone lifecycle, power behavior, or the Home+Menu pairing chord.
COMBO_MODIFIER_BUTTON_IDS = ("tv", "menu", "home")
COMBO_ACTION_BUTTON_IDS = (
    "up",
    "down",
    "left",
    "right",
    "ok",
    "back",
    "volume_up",
    "volume_down",
)

# RC003's descriptor can represent multiple active usages, but the real device
# validation completed on 2026-09-08 never produced overlapping button edges.
# Keep the legacy configuration readable for downgrade compatibility while the
# current product no longer enables remote-button combinations.
REMOTE_BUTTON_COMBOS_SUPPORTED = False


def normalize_quicker_uri(uri: str) -> str:
    """Validate the narrow Quicker action URI accepted by button mappings."""

    if not isinstance(uri, str):
        raise TypeError("Quicker URI must be text")
    normalized = uri.strip()
    if not normalized:
        raise ValueError("Quicker URI must not be empty")
    if len(normalized) > MAX_QUICKER_URI_LENGTH:
        raise ValueError("Quicker URI is too long")
    if any(character in normalized for character in ("\x00", "\r", "\n")):
        raise ValueError("Quicker URI must stay on one line")
    if not normalized.casefold().startswith(QUICKER_URI_PREFIX):
        raise ValueError("only quicker:runaction: URIs are supported")
    action_identifier = normalized[len(QUICKER_URI_PREFIX) :].partition("?")[0]
    if not action_identifier.strip():
        raise ValueError("Quicker URI must contain an action identifier")
    return QUICKER_URI_PREFIX + normalized[len(QUICKER_URI_PREFIX) :]


def semantic_action_for_keys(keys: Tuple[str, ...]) -> Optional["ButtonAction"]:
    """Return the semantic action represented by one legacy key tuple."""

    action_kind = LEGACY_SEMANTIC_ACTIONS.get(tuple(keys))
    if action_kind is None:
        return None
    return ButtonAction(action_kind)


MAX_OPENMEXIA_FIELD_LENGTH = 256


def _normalize_openmexia_field(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"OpenMexia {label} must be text")
    text = value.strip()
    if not text:
        raise ValueError(f"OpenMexia {label} must not be empty")
    if len(text) > MAX_OPENMEXIA_FIELD_LENGTH:
        raise ValueError(f"OpenMexia {label} is too long")
    if any(character in text for character in ("\x00", "\r", "\n")):
        raise ValueError(f"OpenMexia {label} must stay on one line")
    return text


def validate_openmexia_device(value: object) -> str:
    return _normalize_openmexia_field(value, "device")


def validate_openmexia_op(value: object) -> str:
    return _normalize_openmexia_field(value, "operation")


def validate_openmexia_label(value: object) -> str:
    return _normalize_openmexia_field(value, "label")


@dataclass(frozen=True)
class ButtonAction:
    kind: ActionKind
    keys: Tuple[str, ...] = field(default_factory=tuple)
    uri: str = ""
    inject_mode: str = "sendinput"
    # 只有 OPENMEXIA_DEVICE 会带这三个字段：设备 id、操作 id、显示名。
    device: str = ""
    op: str = ""
    label: str = ""

    def to_dict(self) -> dict:
        if self.kind in (ActionKind.MOUSE_WHEEL_UP, ActionKind.MOUSE_WHEEL_DOWN):
            page_key = "pageup" if self.kind == ActionKind.MOUSE_WHEEL_UP else "pagedown"
            data = {"kind": ActionKind.KEY_COMBO.value, "keys": [page_key]}
        else:
            data = {"kind": self.kind.value, "keys": list(self.keys)}
            if self.kind == ActionKind.QUICKER_URI:
                data["uri"] = normalize_quicker_uri(self.uri)
            elif self.kind == ActionKind.OPENMEXIA_DEVICE:
                data["device"] = validate_openmexia_device(self.device)
                data["op"] = validate_openmexia_op(self.op)
                data["label"] = validate_openmexia_label(self.label)
        # Only written when the button opted out of the default backend, so
        # every file written before per-button injection modes existed keeps
        # its exact previous bytes.
        if self.inject_mode != DEFAULT_INJECT_MODE:
            data["inject_mode"] = self.inject_mode
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ButtonAction":
        if not isinstance(data, dict):
            raise TypeError("button action must be a mapping")
        kind = ActionKind(data["kind"])
        inject_mode = normalize_stored_inject_mode(data.get("inject_mode"))
        raw_keys = data.get("keys", ())
        if not isinstance(raw_keys, (list, tuple)) or not all(
            isinstance(key, str) and key.strip() for key in raw_keys
        ):
            raise ValueError("button action keys must be non-empty strings")
        keys = tuple(key.strip().lower() for key in raw_keys)
        raw_uri = data.get("uri", "")
        if not isinstance(raw_uri, str):
            raise ValueError("button action URI must be text")
        uri = raw_uri.strip()
        if kind == ActionKind.KEY_COMBO and not keys:
            raise ValueError("key_combo action must contain at least one key")
        if kind != ActionKind.KEY_COMBO and keys:
            raise ValueError("non-key action must not contain keys")
        if kind == ActionKind.QUICKER_URI:
            uri = normalize_quicker_uri(uri)
        elif uri:
            raise ValueError("non-URI action must not contain a URI")
        device = ""
        op = ""
        label = ""
        if kind == ActionKind.OPENMEXIA_DEVICE:
            device = validate_openmexia_device(data.get("device"))
            op = validate_openmexia_op(data.get("op"))
            label = validate_openmexia_label(data.get("label"))
        elif any(key in data for key in ("device", "op", "label")):
            raise ValueError("non-OpenMexia action must not contain a device")
        if kind in (ActionKind.MOUSE_WHEEL_UP, ActionKind.MOUSE_WHEEL_DOWN):
            page_key = "pageup" if kind == ActionKind.MOUSE_WHEEL_UP else "pagedown"
            return cls(
                kind=ActionKind.KEY_COMBO, keys=(page_key,), inject_mode=inject_mode
            )
        return cls(
            kind=kind,
            keys=keys,
            uri=uri,
            inject_mode=inject_mode,
            device=device,
            op=op,
            label=label,
        )


def button_action_for(
    bindings: Dict[str, object], button_id: str, trigger: ButtonTrigger
) -> ButtonAction:
    """Read one gesture action from the versioned bindings document.

    The original Windows build stored the single action directly under each
    button.  Secondary actions use the reference project's separate map so
    every existing ``key_bindings.json`` remains valid and keeps its primary
    mapping unchanged.
    """

    button_bindings = bindings.get("bindings", {})
    if not isinstance(button_bindings, dict):
        return ButtonAction(ActionKind.DISABLED)
    if trigger == ButtonTrigger.SINGLE_CLICK:
        raw = button_bindings.get(button_id)
    else:
        secondary = bindings.get("secondary_bindings", {})
        raw = (
            secondary.get(button_id, {}).get(trigger.value)
            if isinstance(secondary, dict)
            and isinstance(secondary.get(button_id, {}), dict)
            else None
        )
    if not isinstance(raw, dict):
        return ButtonAction(ActionKind.DISABLED)
    try:
        action = ButtonAction.from_dict(raw)
        if action.kind in MOUSE_HOLD_BUTTONS and trigger != ButtonTrigger.LONG_PRESS:
            return ButtonAction(ActionKind.DISABLED)
        return action
    except (KeyError, TypeError, ValueError):
        return ButtonAction(ActionKind.DISABLED)


def has_secondary_action(bindings: Dict[str, object], button_id: str) -> bool:
    return any(
        button_action_for(bindings, button_id, trigger).kind != ActionKind.DISABLED
        for trigger in (ButtonTrigger.DOUBLE_CLICK, ButtonTrigger.LONG_PRESS)
    )


def button_combo_modifier(bindings: Dict[str, object]) -> Optional[str]:
    if not REMOTE_BUTTON_COMBOS_SUPPORTED:
        return None
    raw = bindings.get("combo_bindings", {})
    if not isinstance(raw, dict):
        return None
    modifier = raw.get("modifier")
    if modifier not in COMBO_MODIFIER_BUTTON_IDS:
        return None
    actions = raw.get("bindings", {})
    if not isinstance(actions, dict):
        return None
    if not any(
        button_combo_action_for(bindings, button_id).kind != ActionKind.DISABLED
        for button_id in COMBO_ACTION_BUTTON_IDS
    ):
        return None
    return str(modifier)


def button_combo_action_for(
    bindings: Dict[str, object], button_id: str
) -> ButtonAction:
    if button_id not in COMBO_ACTION_BUTTON_IDS:
        return ButtonAction(ActionKind.DISABLED)
    raw_combo = bindings.get("combo_bindings", {})
    if not isinstance(raw_combo, dict):
        return ButtonAction(ActionKind.DISABLED)
    raw_actions = raw_combo.get("bindings", {})
    if not isinstance(raw_actions, dict):
        return ButtonAction(ActionKind.DISABLED)
    raw_action = raw_actions.get(button_id)
    if not isinstance(raw_action, dict):
        return ButtonAction(ActionKind.DISABLED)
    try:
        action = ButtonAction.from_dict(raw_action)
    except (KeyError, TypeError, ValueError):
        return ButtonAction(ActionKind.DISABLED)
    if is_voice_action(action) or action.kind in MOUSE_HOLD_BUTTONS:
        return ButtonAction(ActionKind.DISABLED)
    return action


# Buttons that have a defined default action out of the box. "volume_mute" is
# deliberately absent (see module docstring).
DEFAULT_BUTTON_IDS = frozenset(
    {
        "mic",
        "power",
        "up",
        "down",
        "left",
        "right",
        "ok",
        "back",
        "volume_up",
        "volume_down",
        "home",
        "menu",
        "tv",
    }
)


def default_button_actions() -> Dict[str, ButtonAction]:
    return {
        "mic": ButtonAction(ActionKind.VOICE_HOLD),
        "power": ButtonAction(ActionKind.ESCAPE),
        "up": ButtonAction(ActionKind.ARROW_UP),
        "down": ButtonAction(ActionKind.ARROW_DOWN),
        "left": ButtonAction(ActionKind.ARROW_LEFT),
        "right": ButtonAction(ActionKind.ARROW_RIGHT),
        "ok": ButtonAction(ActionKind.RETURN),
        "back": ButtonAction(ActionKind.DELETE_BACKWARD),
        "volume_up": ButtonAction(ActionKind.SYSTEM_VOLUME_UP),
        "volume_down": ButtonAction(ActionKind.SYSTEM_VOLUME_DOWN),
        "home": ButtonAction(ActionKind.SHOW_DESKTOP),
        "menu": ButtonAction(ActionKind.CONTEXT_MENU),
        "tv": ButtonAction(ActionKind.APP_SWITCHER),
    }
