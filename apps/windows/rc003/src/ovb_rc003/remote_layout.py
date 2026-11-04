"""Pure, platform-independent layout data for the RC003 product-photo
hotspots and the physical-button display strings shown next to each mapping
row (XRBM-030).

Hotspot coordinates are calibrated for the bundled RC003 product photo and
stored as fractions (0..1) of the photo's own width/height, so they are
resolution-independent. ButtonsPage preserves each source photo's aspect
ratio and positions hotspots relative to the rendered image, including its
transparent padding, so the same fractions apply at any display size.

This module has no Tk/Qt import at all, so it is directly unit-testable
(see tests/test_remote_layout.py) and importable from a plain ``--dry-run``
smoke check without any GUI toolkit installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from . import device_profile

# Reverse of device_profile.BUTTON_USAGE_IDS (usage -> button_id); "mic" has
# no HID usage at all (it arrives over the ATVV BLE control channel, not a
# keyboard-page HID report - see device_profile.py's module docstring).
_BUTTON_ID_TO_HID_USAGE: Dict[str, int] = {
    button_id: usage for usage, button_id in device_profile.BUTTON_USAGE_IDS.items()
}

# Chinese name of the PHYSICAL button itself (not the action it is currently
# mapped to), plus the fixed ATVV microphone button.
BUTTON_DISPLAY_NAMES: Dict[str, str] = {
    "power": "电源键",
    "up": "上键",
    "left": "左键",
    "ok": "确定键",
    "right": "右键",
    "down": "下键",
    "back": "返回键",
    "volume_up": "音量 + 键",
    "home": "主页键",
    "volume_down": "音量 − 键",
    "menu": "菜单键",
    "tv": "TV 键",
    "mic": "麦克风键",
}


def hid_usage_display(button_id: str) -> str:
    """The HID usage column text for a mapping row. The mic button is
    deliberately never given a fabricated HID usage id - it is driven by the
    ATVV control channel, not a keyboard-page HID report (see
    device_profile.py), so this says so explicitly instead of inventing one.
    """

    usage = _BUTTON_ID_TO_HID_USAGE.get(button_id)
    if usage is None:
        return "ATVV 语音控制通道（非 HID）"
    return f"0x{usage:04X}"


@dataclass(frozen=True)
class ButtonHotspot:
    """One clickable region on the product photo, as a fraction (0..1) of
    the photo's own width/height - see module docstring for provenance.
    """

    button_id: str
    x: float
    y: float
    width: float
    height: float
    is_voice: bool = False


# Exactly the 13 physical buttons the real RC003 has (12 ordinary HID
# buttons + the fixed microphone) - deliberately excludes "volume_mute"
# (device_profile.BUTTON_USAGE_IDS carries it only for HID usage-table
# completeness; the physical remote has no dedicated mute key - see
# key_mapping.py's module docstring and settings_ui.py's
# _USER_FACING_BUTTON_IDS, which this table's ordering matches).
BUTTON_HOTSPOTS: Tuple[ButtonHotspot, ...] = (
    ButtonHotspot("power", x=0.3806, y=0.1100, width=0.150, height=0.072),
    ButtonHotspot("mic", x=0.6125, y=0.1100, width=0.150, height=0.072, is_voice=True),
    ButtonHotspot("up", x=0.4965, y=0.1727, width=0.170, height=0.058),
    ButtonHotspot("left", x=0.3630, y=0.2606, width=0.075, height=0.110),
    ButtonHotspot("ok", x=0.4965, y=0.2606, width=0.160, height=0.110),
    ButtonHotspot("right", x=0.6301, y=0.2606, width=0.075, height=0.110),
    ButtonHotspot("down", x=0.4965, y=0.3485, width=0.170, height=0.058),
    ButtonHotspot("back", x=0.3944, y=0.4132, width=0.170, height=0.080),
    ButtonHotspot("volume_up", x=0.5986, y=0.4132, width=0.160, height=0.080),
    ButtonHotspot("home", x=0.3944, y=0.5071, width=0.170, height=0.080),
    ButtonHotspot("volume_down", x=0.5986, y=0.5071, width=0.160, height=0.080),
    ButtonHotspot("menu", x=0.3944, y=0.5994, width=0.170, height=0.080),
    ButtonHotspot("tv", x=0.5986, y=0.5994, width=0.170, height=0.080),
)

# button_id -> hotspot, and the fixed display order every list/model uses -
# both derived from BUTTON_HOTSPOTS so there is exactly one source of truth.
BUTTON_HOTSPOTS_BY_ID: Dict[str, ButtonHotspot] = {
    hotspot.button_id: hotspot for hotspot in BUTTON_HOTSPOTS
}
BUTTON_ORDER: Tuple[str, ...] = tuple(hotspot.button_id for hotspot in BUTTON_HOTSPOTS)


# 小米遥控器2（RC001）：按键个数、名称、HID usage 与 RC003 完全一致，只是机身
# 更短更宽，所以按键位置单独一张表。坐标同样从 720×1080 的
# RC001-remote-photo.png 上实测得到，尺寸沿用 RC003 同款比例。
RC001_HOTSPOTS: Tuple[ButtonHotspot, ...] = (
    ButtonHotspot("power", x=0.3838, y=0.1142, width=0.150, height=0.072),
    ButtonHotspot("mic", x=0.6068, y=0.1143, width=0.150, height=0.072, is_voice=True),
    ButtonHotspot("up", x=0.4964, y=0.1801, width=0.170, height=0.058),
    ButtonHotspot("left", x=0.3674, y=0.2661, width=0.075, height=0.110),
    ButtonHotspot("ok", x=0.4964, y=0.2661, width=0.160, height=0.110),
    ButtonHotspot("right", x=0.6254, y=0.2661, width=0.075, height=0.110),
    ButtonHotspot("down", x=0.4964, y=0.3521, width=0.170, height=0.058),
    ButtonHotspot("back", x=0.3963, y=0.4233, width=0.170, height=0.080),
    ButtonHotspot("volume_up", x=0.5962, y=0.4235, width=0.160, height=0.080),
    ButtonHotspot("home", x=0.3966, y=0.5101, width=0.170, height=0.080),
    ButtonHotspot("volume_down", x=0.5962, y=0.5100, width=0.160, height=0.080),
    ButtonHotspot("menu", x=0.3964, y=0.5971, width=0.170, height=0.080),
    ButtonHotspot("tv", x=0.5968, y=0.5969, width=0.170, height=0.080),
)

# 2026-10: every key below is measured from the bundled 453×1443 photo (ring
# centroids), not guessed. Like the RC003 table, the markers sit on the outer
# edge of each printed key instead of its centre so they never cover the icon,
# and every left/right pair is mirrored about the body centre line x = 0.48786
# (221 px) so the two columns stay perfectly even.
CHROMECAST_HOTSPOTS = (
    # D-pad: markers on the arrow band at 0.70 R, R = 196 px.
    ButtonHotspot("up", .48786, .06168, .22, .045),
    ButtonHotspot("left", .18322, .15731, .17, .065),
    ButtonHotspot("ok", .48786, .15731, .28, .095),
    ButtonHotspot("right", .79249, .15731, .17, .065),
    ButtonHotspot("down", .48786, .25295, .22, .045),
    # Round keys: pushed 0.45 r outwards and 0.25 r downwards off the ring.
    ButtonHotspot("back", .17550, .37041, .32, .105),
    ButtonHotspot("mic", .80022, .37041, .32, .105, is_voice=True),
    ButtonHotspot("home", .17550, .49827, .32, .105),
    ButtonHotspot("volume_mute", .80022, .49827, .32, .105),
    ButtonHotspot("youtube", .17550, .62474, .32, .105),
    ButtonHotspot("netflix", .80022, .62474, .32, .105),
    ButtonHotspot("power", .20431, .74134, .20, .062),
    ButtonHotspot("input_source", .77141, .74134, .20, .062),
    # Side rocker: the supplied front photo shows only its right edge.
    ButtonHotspot("volume_up", .98, .208, .04, .035),
    ButtonHotspot("volume_down", .98, .252, .04, .035),
)
CHROMECAST_DISPLAY_NAMES = {
    **{
        key: value
        for key, value in BUTTON_DISPLAY_NAMES.items()
        if key not in {"menu", "tv"}
    },
    "mic": "语音 / Assistant",
    "volume_mute": "静音键",
    "youtube": "YT",
    "netflix": "NF",
    "input_source": "信源",
}
CHROMECAST_REPORT_CODES = {
    "power": 0x01, "up": 0x03, "down": 0x04, "left": 0x05, "right": 0x06,
    "ok": 0x07, "volume_mute": 0x08, "home": 0x0A, "back": 0x0B,
    "volume_up": 0x0C, "volume_down": 0x0D, "youtube": 0x0E,
    "netflix": 0x0F, "input_source": 0x11,
}


def button_order(profile: str = "xiaomi-rc003") -> Tuple[str, ...]:
    if profile == "chromecast-remote":
        return tuple(item.button_id for item in CHROMECAST_HOTSPOTS)
    return BUTTON_ORDER


def display_names(profile: str = "xiaomi-rc003") -> Dict[str, str]:
    return CHROMECAST_DISPLAY_NAMES if profile == "chromecast-remote" else BUTTON_DISPLAY_NAMES


def default_actions(profile: str = "xiaomi-rc003") -> dict:
    from . import key_mapping
    defaults = key_mapping.default_button_actions()
    if profile != "chromecast-remote":
        return defaults
    result = {
        button: defaults.get(
            button, key_mapping.ButtonAction(key_mapping.ActionKind.DISABLED)
        )
        for button in button_order(profile)
    }
    result["volume_mute"] = key_mapping.ButtonAction(
        key_mapping.ActionKind.SYSTEM_VOLUME_MUTE
    )
    return result


def hotspot_for(button_id: str, profile: str = "xiaomi-rc003") -> Optional[ButtonHotspot]:
    if profile == "chromecast-remote":
        return next(
            (item for item in CHROMECAST_HOTSPOTS if item.button_id == button_id),
            None,
        )
    if profile == "xiaomi-rc001":
        return next(
            (item for item in RC001_HOTSPOTS if item.button_id == button_id),
            None,
        )
    return BUTTON_HOTSPOTS_BY_ID.get(button_id)
