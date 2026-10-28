"""Configuration persistence with an enforced privacy contract.

Config root: ``%LOCALAPPDATA%\\RemoteMic\\RC003`` (falls back to the
user's home directory if ``LOCALAPPDATA`` is unset, e.g. when unit testing on
non-Windows). Two JSON files live there: ``config.json`` (tuning/behavior) and
``key_bindings.json`` (per-button actions plus the voice hotkey).

Hard privacy rule: neither file may ever contain a real Bluetooth address,
HID device interface path/GUID, or device token. ``save_config`` and
``save_key_bindings`` actively refuse to write any of ``FORBIDDEN_KEYS`` -
this is enforced in code, not just by convention, and is covered by
tests/test_config.py and tests/test_privacy_contract.py.

The guard walks the ENTIRE structure recursively (nested dicts, and dicts
nested inside lists at any depth), not just the top-level keys - a field
like ``bindings.menu.metadata.address`` is refused exactly the same as a
top-level ``address`` key (see XRBM-014 review RETRY P1 #6).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

APP_ID = "RemoteMic"
PRODUCT_ID = "RC003"

CONFIG_FILENAME = "config.json"
KEY_BINDINGS_FILENAME = "key_bindings.json"

SCHEMA_VERSION = 18

VOICE_HOTKEY_SOURCE_DEFAULT = "default"
VOICE_HOTKEY_SOURCE_AUTO = "auto"
VOICE_HOTKEY_SOURCE_MANUAL = "manual"
VALID_VOICE_HOTKEY_SOURCES = frozenset(
    {
        VOICE_HOTKEY_SOURCE_DEFAULT,
        VOICE_HOTKEY_SOURCE_AUTO,
        VOICE_HOTKEY_SOURCE_MANUAL,
    }
)

CLOSE_BEHAVIOR_HIDE_TO_TRAY = "hide_to_tray"
CLOSE_BEHAVIOR_QUIT = "quit"
VALID_CLOSE_BEHAVIORS = frozenset(
    {CLOSE_BEHAVIOR_HIDE_TO_TRAY, CLOSE_BEHAVIOR_QUIT}
)

# 语音输入源：模式1 用遥控器麦克风，模式2 用本机麦克风。
VOICE_INPUT_SOURCE_REMOTE = "remote"
VOICE_INPUT_SOURCE_LOCAL = "local"
VOICE_INPUT_SOURCES = (
    VOICE_INPUT_SOURCE_REMOTE,
    VOICE_INPUT_SOURCE_LOCAL,
)

# 每条语音键单独的录音增益：-3 ~ +3 档，每档 6dB，0 表示不调整。
VOICE_KEY_GAIN_MIN = -3
VOICE_KEY_GAIN_MAX = 3

# 模式切换气泡配色。深色（黑底白字）是默认值；off 表示切换时不弹气泡。
VOICE_CYCLE_TOAST_DARK = "dark"
VOICE_CYCLE_TOAST_LIGHT = "light"
VOICE_CYCLE_TOAST_OFF = "off"
VOICE_CYCLE_TOAST_THEMES = (
    VOICE_CYCLE_TOAST_DARK,
    VOICE_CYCLE_TOAST_LIGHT,
    VOICE_CYCLE_TOAST_OFF,
)

# 模式切换气泡相对默认位置的纵向偏移（像素，正值向上）。设置页可以让用户
# 把气泡从默认的贴底位置往上挪，避免被任务栏或输入法面板挡住。
VOICE_CYCLE_TOAST_OFFSET_MIN = 0
VOICE_CYCLE_TOAST_OFFSET_MAX = 400

# 一条语音键配置的自定义名称上限，避免配置文件被写爆。
MAX_VOICE_KEY_NAME_LENGTH = 32

RUNTIME_LEGACY_VOICE_MODE_KEY = "_legacy_voice_trigger_mode"
RUNTIME_REMOVED_VOICE_BINDINGS_KEY = "_removed_voice_bindings"
RUNTIME_REMOVED_WINDOWS_DICTATION_KEY = "_removed_windows_dictation"
RUNTIME_READ_ONLY_KEY = "_configuration_read_only"
_RUNTIME_ONLY_KEYS = frozenset(
    {
        RUNTIME_LEGACY_VOICE_MODE_KEY,
        RUNTIME_REMOVED_VOICE_BINDINGS_KEY,
        RUNTIME_REMOVED_WINDOWS_DICTATION_KEY,
        RUNTIME_READ_ONLY_KEY,
    }
)

# Any config key matching one of these names is refused at save time,
# regardless of which file it would land in.
FORBIDDEN_KEYS = frozenset(
    {
        "address",
        "bluetooth_address",
        "bt_address",
        "mac_address",
        "device_match",
        "device_id",
        "ble_device_id",
        "device_path",
        "hid_device_path",
        "raw_device_path",
        "device_token",
        "interface_id",
        "device_interface_id",
    }
)


class ConfigPrivacyError(Exception):
    """Raised when code attempts to persist a forbidden identity field."""


class ConfigFormatError(ValueError):
    """Raised when a persisted JSON document is not an object."""


class ConfigTransactionError(RuntimeError):
    """Raised when a paired settings save also fails to roll back."""


class ConfigReadOnlyError(ValueError):
    """A preview of unreadable settings must never replace the saved files."""


@dataclass(frozen=True)
class SettingsLoadIssue:
    filename: str
    error_type: str
    message: str


_SETTINGS_READ_ERRORS = (
    OSError, ValueError, TypeError, AttributeError, RecursionError, ConfigPrivacyError,
)


def _settings_load_issue(filename: str, error: Exception) -> SettingsLoadIssue:
    from . import remote_selection, remote_settings

    if isinstance(error, json.JSONDecodeError):
        message = "JSON 内容不完整或格式错误。"
    elif isinstance(error, UnicodeError):
        message = "文件编码无法读取。"
    elif isinstance(error, OSError):
        message = "文件无法读取。"
    elif isinstance(error, ConfigPrivacyError):
        message = "含有不支持保存的设备信息。"
    elif isinstance(error, (remote_selection.SelectionError, remote_settings.RemoteSettingsError)):
        message = str(error)  # These validators contain fixed messages only.
    elif isinstance(error, ConfigFormatError) and str(error) in {
        "录音方式不受支持。", "最长录音时间不受支持。",
    }:
        message = str(error)
    else:
        message = "配置内容格式不正确。"
    return SettingsLoadIssue(filename, type(error).__name__, message)


def config_root() -> Path:
    from . import dev_session
    if dev_session.is_isolated():
        return dev_session.isolated_root()
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home())
    return Path(base) / APP_ID / PRODUCT_ID


def config_path(root: Path = None) -> Path:  # type: ignore[assignment]
    return (root or config_root()) / CONFIG_FILENAME


def key_bindings_path(root: Path = None) -> Path:  # type: ignore[assignment]
    return (root or config_root()) / KEY_BINDINGS_FILENAME


# 外部插件（Vokie）下发的语音键统一打这个标记：它们自带快捷键，只能排序、
# 开关，不能改快捷键，也不能在插件仍然连接时删除。
VOICE_KEY_PLUGIN_VOKIE = "vokie"
# OpenMexia 联动后由本软件自己加的那条语音键。它和 Vokie 的键一样由外部
# 提供：快捷键不可编辑、连接期间不能删，断开连接时随连接一起清掉。真正的
# 用途是切到它时切到对应的 OpenMexia 应用配置。
VOICE_KEY_PLUGIN_OPENMEXIA = "openmexia"


def is_locked_plugin_id(value: object) -> bool:
    """Whether one plugin id means “this key is owned by an external plugin”."""

    return bool(_normalize_plugin_id(value))


def is_openmexia_plugin_id(value: object) -> bool:
    """Whether one plugin id belongs to the OpenMexia voice key."""

    return _normalize_plugin_id(value) == VOICE_KEY_PLUGIN_OPENMEXIA


def _normalize_plugin_id(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()[:64]


def _default_voice_key_config(
    *,
    slot: int = 1,
    name: str = "",
    hotkey: str = "",
    source: str = VOICE_INPUT_SOURCE_REMOTE,
    device: str = "",
    enabled: bool = True,
    plugin: str = "",
    short_press_resend: bool = False,
    gain_steps: int = 0,
    gain_auto: bool = False,
) -> Dict[str, Any]:
    return {
        "id": int(slot),
        "name": name,
        "hotkey": hotkey,
        "source": source,
        "device": device,
        # 关闭的语音键仍然保留在列表里，只是运行时会被跳过。排序和开关都
        # 直接作用在列表顺序上，所以 id 不能再按大小重排。
        "enabled": bool(enabled),
        # 非空表示这条语音键由外部插件提供（当前只有 Vokie）。
        "plugin": _normalize_plugin_id(plugin),
        # 短按延迟发送：只对这一条非插件语音键生效。按下后把这唯一一次
        # 快捷键推迟一小段时间再注入（松手不再补发第二次），避开同时支持
        # 短按/长按的输入法；插件键的快捷键由插件自己接管，这里保持 False。
        "short_press_resend": bool(short_press_resend),
        # 这条语音键单独的录音增益，-3~+3 档，每档 6dB，0 表示不调整。
        "gain_steps": _clamp_gain_steps(gain_steps),
        # 自动增益：按峰值把整段录音归一化到 70%，与手动档二选一。
        "gain_auto": bool(gain_auto),
    }


def _clamp_gain_steps(value: object) -> int:
    try:
        steps = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(VOICE_KEY_GAIN_MIN, min(VOICE_KEY_GAIN_MAX, steps))


def default_config() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        # Existing installations predate multi-device selection and must
        # continue to open as RC003 rather than silently switching behavior.
        "selected_device_profile": "xiaomi-rc003",
        # RC003's upstream decoder applies a 10 dB speech gain before the
        # 16 kHz PCM is sent to the virtual microphone.
        "gain_db": 10.0,
        "remote_recording_limit_seconds": 120,
        "retry_delay": 2.0,
        "max_retry_delay": 60.0,
        "voice_hotkey": "",
        "voice_hotkeys": {"hold": ""},
        # 语音键配置列表：每条是一套独立的按住说话配置（名称、麦克风快捷键、
        # 麦克风来源与设备）。id 一旦分配就不再变化，动作编码为 voice_hold_<id>，
        # 因此删除中间一条不会改变其它按键已经绑定的语音键。
        "voice_key_configs": [_default_voice_key_config()],
        # Per hold-to-talk slot host shortcut. Empty until the user records
        # one; nothing is injected while a slot is empty. Kept as a derived
        # compatibility mirror of the first two voice_key_configs entries.
        "voice_slot_hotkeys": {"1": "", "2": ""},
        # 语音输入源。模式1（remote）用遥控器自带麦克风：只有话筒键能触发、
        # 只有语音1 生效、只支持按住说话（短按不惊动输入法）。模式2（local）
        # 改用本机麦克风，语音1/2 可以绑任意按键，短按锁存和长按都支持。
        # 现在以 voice_key_configs 为准，这两个字段保留为该列表首项的镜像。
        "voice_input_source": VOICE_INPUT_SOURCE_REMOTE,
        # 模式2 使用本机哪个录音设备；空值表示尚未选择，模式2 保持静默。
        "voice_local_input_device": "",
        # 短按切换模式（可选，默认关闭）。打开后话筒键的短按在已配置的语音键
        # 之间循环切换；每条语音键还可以单独打开“短按延迟发送”，把这一次
        # 快捷键推迟下发，避开输入法的短按/长按竞争。
        "voice_cycle_short_press": False,
        # 使用其他按键切换模式（可选，默认关闭）。打开后按键设置页的单击、
        # 双击、长按动作里会多出“语音模式切换”，任意按键（语音键除外）都能
        # 用来切模式；话筒键同时跟随切换后的语音键。
        "voice_other_key_switch": False,
        # 切换模式时在鼠标所在屏幕底部弹出的小气泡配色：dark / light / off。
        "voice_cycle_toast_theme": VOICE_CYCLE_TOAST_DARK,
        # 气泡相对默认底部位置的额外上移像素，0 表示贴默认位置。
        "voice_cycle_toast_offset": 0,
        # Empty until the user explicitly picks one in settings; voice fails
        # closed while this is empty (see audio_output.resolve_selected_endpoint).
        # Both fields together disambiguate endpoints that share a display
        # name across host APIs (e.g. the same device exposed via both
        # Windows WASAPI and MME) - name alone is not always unique.
        "output_endpoint_name": "",
        "output_endpoint_host_api": "",
        # Desktop-shell behavior. Windows login startup itself is owned by
        # the user's HKCU Run value and is deliberately not mirrored here.
        "launch_bridge_on_app_start": False,
        # 记住用户离开时 Vokie 插件连接是开还是关：开着的话下次启动直接恢复
        # 监听，不用再点一次「开启插件连接」；关着的话启动后也保持关闭。
        "voice_plugin_auto_connect": False,
        # 记住用户离开时 OpenMexia 是连着的还是断开的：连着的话下次启动自动
        # 再连一次；断开或从未连过就保持不连。
        "openmexia_auto_connect": False,
        "diagnostic_trace_enabled": False,
        "close_behavior": CLOSE_BEHAVIOR_HIDE_TO_TRAY,
        # A portable build offers each exact helper binary once. The explicit
        # device-page action remains available after the automatic prompt.
        "hid_helper_setup_prompted_offer_id": "",
    }


def _find_forbidden_key_paths(
    node: Union[Dict[str, Any], List[Any], Any], path: str = ""
) -> List[str]:
    """Recursively collect dotted-path locations of any forbidden key,
    found at any nesting depth inside dicts and lists-of-dicts.
    """

    found: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.casefold() in FORBIDDEN_KEYS:
                found.append(child_path)
            found.extend(_find_forbidden_key_paths(value, child_path))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(_find_forbidden_key_paths(item, f"{path}[{index}]"))
    return found


def _assert_no_forbidden_keys(data: Dict[str, Any]) -> None:
    found = _find_forbidden_key_paths(data)
    if found:
        raise ConfigPrivacyError(
            "refusing to persist forbidden identity field(s) at: "
            + ", ".join(sorted(found))
        )


def load_config(path: Path) -> Dict[str, Any]:
    return _normalize_loaded_config(_read_settings_document(path))


def _normalize_loaded_config(stored: dict) -> Dict[str, Any]:
    from . import remote_selection, remote_settings
    config = default_config()
    if stored:
        stored = remote_settings.project(stored, remote_settings.CONFIG_FIELDS,
                                         remote_selection.active_key(stored))
        # Normalize the persisted voice fields before merging defaults. A
        # shallow merge would otherwise make a newly introduced default look
        # like an explicitly saved top-level/nested shortcut and could hide
        # the old value that is actually present in the file.
        _normalize_voice_hotkey(stored)
        _normalize_voice_input_source(stored)
        if "voice_key_configs" not in stored:
            # 旧文件还没有语音键列表：用旧的两个槽位和全局麦克风设置迁移，
            # 迁移后旧字段只作为镜像保留。
            stored["voice_key_configs"] = _legacy_voice_key_configs(stored)
        _normalize_voice_key_configs(stored)
        _normalize_desktop_behavior(stored)
        # Fields retired by schema 18 stay readable in an old record so the file
        # opens and its live settings survive, but they must not leak into the
        # runtime view (or ride back to disk at the top level on the next save).
        for field in remote_settings.CONFIG_RETIRED_FIELDS:
            stored.pop(field, None)
        config.update(stored)
    _normalize_voice_hotkey(config)
    _normalize_voice_input_source(config)
    _normalize_voice_key_configs(config)
    _normalize_desktop_behavior(config)
    if remote_selection.KEY in config:
        config[remote_selection.KEY] = remote_selection.normalize(config[remote_selection.KEY])
    profile = remote_selection.active_profile(config)
    if profile:
        config["selected_device_profile"] = profile
    _normalize_remote_recording_limit(config)
    return config


def _preview_config(path: Path) -> Dict[str, Any]:
    """Best-effort display only. Never repair or write the original document."""
    from . import remote_selection, remote_settings

    try:
        stored = _read_settings_document(path)
        try:
            active = remote_selection.active_key(stored)
        except remote_selection.SelectionError:
            stored.pop(remote_selection.KEY, None)
            # An unknown owner cannot borrow any device's private settings.
            if remote_settings.STORE in stored:
                for field in remote_settings.CONFIG_FIELDS:
                    stored.pop(field, None)
            stored.pop(remote_settings.STORE, None)
            active = ""
        try:
            stored = remote_settings.project(stored, remote_settings.CONFIG_FIELDS, active)
        except remote_settings.RemoteSettingsError:
            store = stored.get(remote_settings.STORE)
            try:
                # An unrelated broken record must not hide a valid selected
                # record in this read-only preview. The original stays intact.
                if (not isinstance(store, dict) or type(store.get("schema")) is not int
                        or store["schema"] != 1 or not isinstance(store.get("records"), dict)):
                    raise remote_settings.RemoteSettingsError("invalid preview store")
                preview = dict(stored)
                preview[remote_settings.STORE] = {
                    "schema": 1,
                    "records": {active: store["records"][active]} if active in store["records"] else {},
                    "unassigned": {} if active else store.get("unassigned", {}),
                }
                stored = remote_settings.project(preview, remote_settings.CONFIG_FIELDS, active)
            except remote_settings.RemoteSettingsError:
                for field in remote_settings.CONFIG_FIELDS:
                    stored.pop(field, None)
        stored.pop(remote_settings.STORE, None)
        stored.pop(remote_settings.OWNER, None)
        from .voice_preferences import LIMITS, DEFAULT_LIMIT
        stored.pop("remote_recording_mode", None)
        limit = stored.get("remote_recording_limit_seconds", DEFAULT_LIMIT)
        if type(limit) is not int or limit not in LIMITS:
            stored["remote_recording_limit_seconds"] = DEFAULT_LIMIT
        return _normalize_loaded_config(stored)
    except _SETTINGS_READ_ERRORS:
        return default_config()


def load_window_settings(root: Path) -> tuple[dict, dict, tuple[SettingsLoadIssue, ...]]:
    """Open the settings shell even when the strict runtime loaders reject data.

    Preview values remain process-local and are write-protected as a pair.
    The bridge and all other consumers retain the strict loaders above/below.
    """
    from . import remote_selection

    issues = []
    try:
        settings = load_config(config_path(root))
    except _SETTINGS_READ_ERRORS as error:
        issues.append(_settings_load_issue(CONFIG_FILENAME, error))
        settings = _preview_config(config_path(root))
    try:
        bindings = (_load_key_bindings_for_selection(key_bindings_path(root), settings)
                    if issues else load_key_bindings(key_bindings_path(root)))
    except _SETTINGS_READ_ERRORS as error:
        issues.append(_settings_load_issue(KEY_BINDINGS_FILENAME, error))
        bindings = default_key_bindings(remote_selection.active_profile(settings))
        bindings["bindings"] = {}  # Do not present fallback actions as saved mappings.
    if issues:
        settings[RUNTIME_READ_ONLY_KEY] = True
        bindings[RUNTIME_READ_ONLY_KEY] = True
    return settings, bindings, tuple(issues)


def save_config(path: Path, config: Dict[str, Any]) -> None:
    from . import remote_selection, remote_settings
    persisted = _without_runtime_only_keys(config)
    if remote_selection.KEY in persisted:
        persisted[remote_selection.KEY] = remote_selection.normalize(persisted[remote_selection.KEY])
    _assert_no_forbidden_keys(persisted)
    _normalize_voice_hotkey(persisted)
    _normalize_voice_input_source(persisted)
    _normalize_desktop_behavior(persisted)
    profile = remote_selection.active_profile(persisted)
    _normalize_remote_recording_limit(persisted)
    persisted = _without_runtime_only_keys(persisted)
    latest = _read_settings_document(path)
    if remote_settings.STORE in latest and remote_selection.active_key(latest) != persisted.get(remote_settings.OWNER):
        raise remote_settings.RemoteSettingsError("当前设备已切换，请重新载入设置。")
    persisted = remote_settings.pack(persisted, remote_settings.CONFIG_FIELDS,
                                    remote_selection.active_key(persisted), latest)
    if profile:
        persisted.pop("selected_device_profile", None)  # Derived, never a second selection.
    _save_json_atomic(path, persisted)


def _normalize_remote_recording_limit(document):
    from .voice_preferences import LIMITS, DEFAULT_LIMIT
    # The per-device recording gesture (hold/toggle) is gone in schema 18;
    # drop any legacy value so it can never be reinterpreted.
    document.pop("remote_recording_mode", None)
    limit = document.get("remote_recording_limit_seconds", DEFAULT_LIMIT)
    if type(limit) is not int or limit not in LIMITS:
        raise ConfigFormatError("最长录音时间不受支持。")
    document["remote_recording_limit_seconds"] = limit


def _read_settings_document(path):
    if not Path(path).is_file():
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ConfigFormatError("设置文件不是对象。")
    _assert_no_forbidden_keys(value)
    return value


def save_config_and_load(path: Path, config_data: Dict[str, Any]) -> Dict[str, Any]:
    """Atomically save one config and restore the previous file if verification fails."""

    previous = _read_file_snapshot(path)
    saved = False
    try:
        save_config(path, config_data)
        saved = True
        return load_config(path)
    except BaseException as save_exc:
        if saved:
            try:
                _restore_file_snapshot(path, previous)
                _verify_file_snapshot(path, previous)
            except Exception as rollback_exc:
                raise ConfigTransactionError(
                    "config save verification failed and rollback was incomplete"
                ) from save_exc
        raise


def _normalize_voice_hotkey(config: Dict[str, Any]) -> None:
    """Normalize the voice shortcut settings.

    The shortcut is no longer tied to any input method: it is the single hold
    shortcut the user recorded, mirrored into the legacy scalar and the
    per-slot table. A legacy ``voice_hotkeys["hold"]`` value backfills slot 1
    when that slot is still empty, so an old installation keeps the key it was
    actually using. The provider tables of older schemas are dropped.
    """

    current = str(config.get("voice_hotkey", "")).strip().lower()
    from . import key_mapping

    raw_mode = str(config.get("voice_trigger_mode", "hold")).strip().lower()
    raw_mode_hotkeys = config.get("voice_hotkeys")
    if not isinstance(raw_mode_hotkeys, dict):
        raw_mode_hotkeys = {}
    saved_hold_hotkey = str(raw_mode_hotkeys.get("hold", "")).strip().lower()

    if raw_mode == "toggle":
        config[RUNTIME_LEGACY_VOICE_MODE_KEY] = raw_mode
        # Never reinterpret a toggle shortcut as hold-to-talk. Use the
        # separately stored hold shortcut when available. Otherwise retain
        # the right-Alt fallback shipped with those old files; the new-install
        # default must not silently rewrite a historical configuration.
        current = saved_hold_hotkey or key_mapping.LEGACY_HOLD_VOICE_HOTKEY
    elif saved_hold_hotkey and not current:
        current = saved_hold_hotkey

    # These repairs only apply to the old global field, which used to name the
    # shortcut that woke an input method; the per-slot recordings are already
    # validated and must never be rewritten behind the user's back.
    if current in {"lctrl+win", "lctrl+lwin"}:
        current = key_mapping.LEGACY_HOLD_VOICE_HOTKEY
    if current == "lalt":
        current = key_mapping.LEGACY_HOLD_VOICE_HOTKEY

    config["schema_version"] = SCHEMA_VERSION
    # The per-provider trigger-mode selector is gone in schema 18; drop the
    # legacy key instead of pinning it to a value.
    config.pop("voice_trigger_mode", None)
    config["voice_hotkey"] = current
    config["voice_hotkeys"] = {"hold": current}
    config["voice_slot_hotkeys"] = _normalize_voice_slot_hotkeys(config, current)
    config.pop("voice_release_finish_tap_enabled", None)
    # 全局“短按自动双击”已改成每条语音键自己的“短按延迟发送”，旧字段丢弃。
    config.pop("voice_short_press_dismiss", None)
    # The input-method (provider) layer is gone in schema 18: quietly drop the
    # legacy provider tables so an old installation never resurrects them.
    config.pop("voice_program", None)
    config.pop("voice_hotkeys_by_provider", None)
    config.pop("chatterfly_voice_functions", None)


def _normalize_voice_slot_hotkeys(
    config: Dict[str, Any], fallback: str = ""
) -> Dict[str, str]:
    """Normalize the compatibility mirror of the first two voice-key shortcuts.

    Every slot keeps exactly the shortcut the user recorded; an empty entry
    stays empty, except that slot 1 backfills from the legacy scalar when it is
    still empty. Only slots 1 and 2 are mirrored here, a legacy slot 3 is
    dropped."""

    raw = config.get("voice_slot_hotkeys")
    result: Dict[str, str] = {"1": "", "2": ""}
    if isinstance(raw, dict):
        for slot in (1, 2):
            result[str(slot)] = _normalize_hotkey_text(raw.get(str(slot), ""))
    if not result["1"]:
        result["1"] = _normalize_hotkey_text(fallback)
    return result


def _normalize_hotkey_text(candidate: object) -> str:
    """Return ``candidate`` as a validated host shortcut, or "" when invalid."""

    from . import hotkey as hotkey_module, win32_keys

    text = str(candidate).strip().lower()
    if not text:
        return ""
    try:
        parsed = hotkey_module.HotkeySpec.parse(text)
        win32_keys.resolve_vk_codes(tuple(parsed.modifiers) + (parsed.key,))
    except (hotkey_module.HotkeyParseError, win32_keys.UnknownKeyTokenError):
        return ""
    return text


def _normalize_voice_key_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:MAX_VOICE_KEY_NAME_LENGTH]


def _normalize_voice_source(value: object) -> str:
    if isinstance(value, str) and value.strip().casefold() in VOICE_INPUT_SOURCES:
        return value.strip().casefold()
    return VOICE_INPUT_SOURCE_REMOTE


def _legacy_voice_key_configs(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build the initial voice-key list from a pre-voice_key_configs file."""

    source = _normalize_voice_source(config.get("voice_input_source"))
    device = config.get("voice_local_input_device", "")
    device = device.strip() if isinstance(device, str) else ""
    mirrored = config.get("voice_slot_hotkeys")
    hotkeys: Dict[str, str] = {}
    if isinstance(mirrored, dict):
        for slot in (1, 2):
            hotkeys[str(slot)] = _normalize_hotkey_text(mirrored.get(str(slot), ""))
    entries = [
        _default_voice_key_config(
            slot=1, hotkey=hotkeys.get("1", ""), source=source, device=device
        )
    ]
    if hotkeys.get("2"):
        entries.append(
            _default_voice_key_config(
                slot=2, hotkey=hotkeys["2"], source=source, device=device
            )
        )
    return entries


def _clamp_voice_cycle_toast_offset(value: object) -> int:
    """Return the bubble offset in pixels, clamped to the supported range."""

    try:
        offset = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return VOICE_CYCLE_TOAST_OFFSET_MIN
    return max(
        VOICE_CYCLE_TOAST_OFFSET_MIN,
        min(VOICE_CYCLE_TOAST_OFFSET_MAX, offset),
    )


def _normalize_voice_key_configs(config: Dict[str, Any]) -> None:
    """Normalize the voice-key list and refresh its compatibility mirrors.

    ``voice_key_configs`` is authoritative. Files written before this list
    existed are migrated from ``voice_slot_hotkeys`` plus the global source and
    device, so an upgrade keeps the exact behaviour the user already had.
    """

    from . import key_mapping

    raw_entries = config.get("voice_key_configs")
    if not isinstance(raw_entries, list):
        raw_entries = _legacy_voice_key_configs(config)

    normalized: List[Dict[str, Any]] = []
    used_ids = set()
    next_free = 1
    for raw_entry in raw_entries:
        if len(normalized) >= key_mapping.MAX_VOICE_SLOTS:
            break
        if not isinstance(raw_entry, dict):
            continue
        try:
            slot = int(raw_entry.get("id", 0))
        except (TypeError, ValueError):
            slot = 0
        if slot < 1 or slot > key_mapping.MAX_VOICE_SLOTS or slot in used_ids:
            while next_free in used_ids:
                next_free += 1
            slot = next_free
        used_ids.add(slot)
        source = _normalize_voice_source(raw_entry.get("source"))
        device = raw_entry.get("device", "")
        device = device.strip() if isinstance(device, str) else ""
        if source == VOICE_INPUT_SOURCE_REMOTE:
            device = ""
        normalized.append(
            _default_voice_key_config(
                slot=slot,
                name=_normalize_voice_key_name(raw_entry.get("name", "")),
                hotkey=_normalize_hotkey_text(raw_entry.get("hotkey", "")),
                source=source,
                device=device,
                enabled=raw_entry.get("enabled", True) is not False,
                plugin=raw_entry.get("plugin", ""),
                short_press_resend=raw_entry.get("short_press_resend", False)
                is not False,
                gain_steps=raw_entry.get("gain_steps", 0),
                gain_auto=raw_entry.get("gain_auto", False) is not False,
            )
        )
    if not normalized:
        normalized = [_default_voice_key_config()]
    # 列表本身就是顺序：语音键配置界面支持上下移动，插件项也要能和自定义项
    # 混排，所以这里绝不能按 id 重排。
    config["voice_key_configs"] = normalized

    by_id = {entry["id"]: entry for entry in normalized}
    first = normalized[0]
    mirror = {"1": "", "2": ""}
    for slot in (1, 2):
        mirror[str(slot)] = by_id.get(slot, {}).get("hotkey", "")
    config["voice_slot_hotkeys"] = mirror
    config["voice_input_source"] = first["source"]
    config["voice_local_input_device"] = first["device"]

    # 两个短按开关彼此独立，可以同时打开：短按切换模式负责换槽位，每条语音
    # 键的“短按延迟发送”把那一次按键推迟下发。运行时按具体槽位判断。
    cycle = bool(config.get("voice_cycle_short_press", False))
    config["voice_cycle_short_press"] = cycle
    config["voice_other_key_switch"] = bool(
        config.get("voice_other_key_switch", False)
    )
    theme = str(config.get("voice_cycle_toast_theme", VOICE_CYCLE_TOAST_DARK)).strip().lower()
    if theme not in VOICE_CYCLE_TOAST_THEMES:
        theme = VOICE_CYCLE_TOAST_DARK
    config["voice_cycle_toast_theme"] = theme
    config["voice_cycle_toast_offset"] = _clamp_voice_cycle_toast_offset(
        config.get("voice_cycle_toast_offset", 0)
    )


def voice_key_configs(config_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return every configured voice key, in the order the user arranged them."""

    raw_entries = config_data.get("voice_key_configs")
    if not isinstance(raw_entries, list) or not raw_entries:
        raw_entries = _legacy_voice_key_configs(config_data)
    entries: List[Dict[str, Any]] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            continue
        try:
            slot = int(raw_entry.get("id", 0))
        except (TypeError, ValueError):
            continue
        if slot < 1:
            continue
        source = _normalize_voice_source(raw_entry.get("source"))
        device = raw_entry.get("device", "")
        device = device.strip() if isinstance(device, str) else ""
        entries.append(
            _default_voice_key_config(
                slot=slot,
                name=_normalize_voice_key_name(raw_entry.get("name", "")),
                hotkey=str(raw_entry.get("hotkey", "") or "").strip().lower(),
                source=source,
                device="" if source == VOICE_INPUT_SOURCE_REMOTE else device,
                enabled=raw_entry.get("enabled", True) is not False,
                plugin=raw_entry.get("plugin", ""),
                short_press_resend=raw_entry.get("short_press_resend", False)
                is not False,
                gain_steps=raw_entry.get("gain_steps", 0),
                gain_auto=raw_entry.get("gain_auto", False) is not False,
            )
        )
    if not entries:
        entries = [_default_voice_key_config()]
    return entries


def enabled_voice_key_configs(config_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the voice keys the runtime may still fire, in list order."""

    return [
        entry for entry in voice_key_configs(config_data) if entry.get("enabled", True)
    ]


def voice_key_enabled(config_data: Dict[str, Any], slot: int) -> bool:
    """Whether one voice key is switched on in the configuration list."""

    entry = voice_key_config(config_data, slot)
    return bool(entry and entry.get("enabled", True))


def voice_key_config(
    config_data: Dict[str, Any], slot: int
) -> Optional[Dict[str, Any]]:
    """Return one voice key by its id, or ``None`` when it does not exist."""

    try:
        slot_number = int(slot)
    except (TypeError, ValueError):
        return None
    for entry in voice_key_configs(config_data):
        if entry["id"] == slot_number:
            return entry
    return None


def voice_key_name(config_data: Dict[str, Any], slot: int) -> str:
    """Return the user-visible name of one voice key.

    An unnamed key falls back to 语音1 / 语音2 / ... so the bubble and the button
    page always have something meaningful to show.
    """

    try:
        slot_number = int(slot)
    except (TypeError, ValueError):
        return ""
    for position, entry in enumerate(voice_key_configs(config_data), start=1):
        if entry["id"] != slot_number:
            continue
        name = _normalize_voice_key_name(entry.get("name"))
        # 没起名的按键按它在列表里的位置叫语音1..n：上下移动之后名字跟着位置
        # 重排，用户自己输入的名字则保持不变。
        return name or f"语音{position}"
    return f"语音{slot_number}"


def voice_key_labels(config_data: Dict[str, Any]) -> Dict[int, str]:
    """Return the unique display name of every voice key.

    Two keys may share the same user name; the button page still needs one
    unambiguous label per action, so a duplicate gets its id appended.
    """

    labels: Dict[int, str] = {}
    used = set()
    for position, entry in enumerate(voice_key_configs(config_data), start=1):
        slot = entry["id"]
        # 默认名跟着列表位置走（语音1..n），自定义名字保持原样。
        base = _normalize_voice_key_name(entry.get("name")) or f"语音{position}"
        label = base
        suffix = 2
        while label in used:
            label = f"{base}（{slot}）" if suffix == 2 else f"{base}（{slot}-{suffix}）"
            suffix += 1
        used.add(label)
        labels[slot] = label
    return labels


def voice_slot_hotkey(config_data: Dict[str, Any], slot: int) -> str:
    """Return one hold-to-talk slot's host shortcut.

    The slot resolves from what the user recorded on the voice page. Slot 1 is
    the historical single voice key, so an empty recording there keeps using the
    legacy shared hold shortcut - exactly what the settings page shows - instead
    of claiming a key it never injects. Other slots stay empty when unrecorded.
    """

    try:
        slot_number = int(slot)
    except (TypeError, ValueError):
        return ""
    entry = voice_key_config(config_data, slot_number)
    if entry is not None:
        recorded = str(entry.get("hotkey", "")).strip().lower()
        if recorded or slot_number != 1 or entry.get("plugin"):
            return recorded
        shared = config_data.get("voice_hotkeys")
        if isinstance(shared, dict):
            legacy = str(shared.get("hold", "")).strip().lower()
            if legacy:
                return legacy
        return str(config_data.get("voice_hotkey", "")).strip().lower()
    entries = config_data.get("voice_slot_hotkeys")
    if isinstance(entries, dict):
        candidate = str(entries.get(str(slot_number), "")).strip().lower()
        if candidate:
            return candidate
    return ""


def voice_input_source(config_data: Dict[str, Any], slot: int = 1) -> str:
    """Return one voice key's microphone source (``remote`` or ``local``).

    Slot 1 is the historical single voice key, so omitting ``slot`` keeps every
    existing caller working unchanged.
    """

    entry = voice_key_config(config_data, slot)
    if entry is not None:
        return entry["source"]
    value = config_data.get("voice_input_source", VOICE_INPUT_SOURCE_REMOTE)
    if isinstance(value, str) and value.strip().casefold() in VOICE_INPUT_SOURCES:
        return value.strip().casefold()
    return VOICE_INPUT_SOURCE_REMOTE


def voice_local_input_device(config_data: Dict[str, Any], slot: int = 1) -> str:
    """Return one voice key's 模式2 recording device name, or ""."""

    entry = voice_key_config(config_data, slot)
    if entry is not None:
        return entry["device"]
    value = config_data.get("voice_local_input_device", "")
    return value.strip() if isinstance(value, str) else ""


def voice_key_short_press_resend(config_data: Dict[str, Any], slot: int) -> bool:
    """Return whether one voice key delays its only press on a short press.

    Only the key the user turned it on for is affected; plugin-provided keys
    always report False because the plugin owns their shortcut.
    """

    entry = voice_key_config(config_data, slot)
    if entry is None or _normalize_plugin_id(entry.get("plugin")):
        return False
    return bool(entry.get("short_press_resend", False))


def voice_cycle_short_press(config_data: Dict[str, Any]) -> bool:
    """Return whether a short press cycles through the configured voice keys."""

    return bool(config_data.get("voice_cycle_short_press", False))


def voice_other_key_switch(config_data: Dict[str, Any]) -> bool:
    """Return whether ordinary buttons may switch the active voice mode.

    It only unlocks the "语音模式切换" mapping action; the switch itself is
    still performed by whichever gesture the user maps it to.
    """

    return bool(config_data.get("voice_other_key_switch", False))


def voice_cycle_toast_theme(config_data: Dict[str, Any]) -> str:
    """Return the mode-switch bubble palette: ``dark``, ``light`` or ``off``."""

    theme = config_data.get("voice_cycle_toast_theme", VOICE_CYCLE_TOAST_DARK)
    if isinstance(theme, str) and theme.strip().lower() in VOICE_CYCLE_TOAST_THEMES:
        return theme.strip().lower()
    return VOICE_CYCLE_TOAST_DARK


def voice_cycle_toast_offset(config_data: Dict[str, Any]) -> int:
    """Return how many pixels the mode-switch bubble sits above its default spot."""

    return _clamp_voice_cycle_toast_offset(
        config_data.get("voice_cycle_toast_offset", VOICE_CYCLE_TOAST_OFFSET_MIN)
    )


_VOICE_KEY_FIELD_UNSET = object()


def _with_voice_key_configs(
    config_data: Dict[str, Any], entries: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Return a copy whose ``voice_key_configs`` is ``entries``, normalized."""

    updated = dict(config_data)
    updated["voice_key_configs"] = [dict(entry) for entry in entries]
    _normalize_voice_key_configs(updated)
    return updated


def update_voice_key_config(
    config_data: Dict[str, Any],
    slot: int,
    *,
    name: object = _VOICE_KEY_FIELD_UNSET,
    hotkey: object = _VOICE_KEY_FIELD_UNSET,
    source: object = _VOICE_KEY_FIELD_UNSET,
    device: object = _VOICE_KEY_FIELD_UNSET,
    enabled: object = _VOICE_KEY_FIELD_UNSET,
    short_press_resend: object = _VOICE_KEY_FIELD_UNSET,
    gain_steps: object = _VOICE_KEY_FIELD_UNSET,
    gain_auto: object = _VOICE_KEY_FIELD_UNSET,
    force: bool = False,
) -> Dict[str, Any]:
    """Return a copy with one voice key's fields replaced.

    Only the fields passed by the caller change, so a caller that renames a key
    can never rewrite its recorded shortcut. Unknown ids return an unchanged
    copy instead of inventing an entry. A plugin-provided key keeps its own
    shortcut: the plugin ships it, so writing one here would desynchronise the
    two ends.
    """

    entries = voice_key_configs(config_data)
    target = None
    for entry in entries:
        if entry["id"] == int(slot):
            target = entry
            break
    if target is None:
        return dict(config_data)
    if name is not _VOICE_KEY_FIELD_UNSET:
        target["name"] = _normalize_voice_key_name(name)
    if hotkey is not _VOICE_KEY_FIELD_UNSET and (force or not is_locked_plugin_id(target.get("plugin"))):
        target["hotkey"] = _normalize_hotkey_text(hotkey)
    if enabled is not _VOICE_KEY_FIELD_UNSET:
        target["enabled"] = bool(enabled)
    # 插件键的快捷键由插件接管，短按延迟发送对它没有意义，所以不写入。
    if short_press_resend is not _VOICE_KEY_FIELD_UNSET and not is_locked_plugin_id(
        target.get("plugin")
    ):
        target["short_press_resend"] = bool(short_press_resend)
    # 录音增益对每一条语音键都有效，插件键也能单独调。
    if gain_steps is not _VOICE_KEY_FIELD_UNSET:
        target["gain_steps"] = _clamp_gain_steps(gain_steps)
    if gain_auto is not _VOICE_KEY_FIELD_UNSET:
        target["gain_auto"] = bool(gain_auto)
    if source is not _VOICE_KEY_FIELD_UNSET:
        target["source"] = _normalize_voice_source(source)
        if target["source"] == VOICE_INPUT_SOURCE_REMOTE:
            target["device"] = ""
    if device is not _VOICE_KEY_FIELD_UNSET:
        text = device.strip() if isinstance(device, str) else ""
        target["device"] = (
            "" if target["source"] == VOICE_INPUT_SOURCE_REMOTE else text
        )
    return _with_voice_key_configs(config_data, entries)


def add_voice_key_config(config_data: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy with one more empty voice key appended."""

    from . import key_mapping

    entries = voice_key_configs(config_data)
    if len(entries) >= key_mapping.MAX_VOICE_SLOTS:
        return dict(config_data)
    used = {entry["id"] for entry in entries}
    # 新键一律接在现有最大 id 之后，不复用空位：id 1 是历史单语音键，它没录
    # 快捷键时会回退到旧版全局快捷键，新键不该莫名其妙继承那个值。
    slot = max(used, default=1) + 1
    entries.append(_default_voice_key_config(slot=slot))
    return _with_voice_key_configs(config_data, entries)


def openmexia_voice_key_slot(config_data: Dict[str, Any]) -> Optional[int]:
    """The id of the OpenMexia voice key, or ``None`` when there is none."""

    for entry in voice_key_configs(config_data):
        if _normalize_plugin_id(entry.get("plugin")) == VOICE_KEY_PLUGIN_OPENMEXIA:
            return int(entry["id"])
    return None


def ensure_openmexia_voice_key(
    config_data: Dict[str, Any], *, name: str = "OpenMexia"
) -> Tuple[Dict[str, Any], Optional[int]]:
    """Make sure the OpenMexia voice key exists; return the copy and its id.

    Created only when there is none yet, so connecting twice never adds a
    second key. The new key is appended after every existing one, which keeps
    the user's own voice keys where they were.
    """

    from . import key_mapping

    existing = openmexia_voice_key_slot(config_data)
    if existing is not None:
        return dict(config_data), existing
    entries = voice_key_configs(config_data)
    if len(entries) >= key_mapping.MAX_VOICE_SLOTS:
        return dict(config_data), None
    slot = max((entry["id"] for entry in entries), default=1) + 1
    entries.append(
        _default_voice_key_config(
            slot=slot,
            name=name,
            plugin=VOICE_KEY_PLUGIN_OPENMEXIA,
        )
    )
    return _with_voice_key_configs(config_data, entries), slot


def remove_voice_key_config(
    config_data: Dict[str, Any], slot: int, *, force: bool = False
) -> Dict[str, Any]:
    """Return a copy without one voice key; the last remaining key is kept.

    A plugin-provided key stays until the plugin itself is disconnected, so it
    is skipped here and the caller decides how to report that. ``force`` lets
    the settings window delete it once the plugin is no longer connected.
    """

    entries = voice_key_configs(config_data)
    remaining = [
        entry
        for entry in entries
        if entry["id"] != int(slot)
        or (is_locked_plugin_id(entry.get("plugin")) and not force)
    ]
    if not remaining or len(remaining) == len(entries):
        return dict(config_data)
    return _with_voice_key_configs(config_data, remaining)


def move_voice_key_config(
    config_data: Dict[str, Any], slot: int, delta: int
) -> Dict[str, Any]:
    """Return a copy with one voice key moved ``delta`` places in the list."""

    entries = voice_key_configs(config_data)
    ids = [entry["id"] for entry in entries]
    try:
        index = ids.index(int(slot))
    except ValueError:
        return dict(config_data)
    try:
        offset = int(delta)
    except (TypeError, ValueError):
        return dict(config_data)
    target = index + offset
    if offset == 0 or target < 0 or target >= len(entries):
        return dict(config_data)
    entries[index], entries[target] = entries[target], entries[index]
    return _with_voice_key_configs(config_data, entries)


def set_voice_key_config_enabled(
    config_data: Dict[str, Any], slot: int, enabled: bool
) -> Dict[str, Any]:
    """Return a copy with one voice key switched on or off."""

    return update_voice_key_config(config_data, int(slot), enabled=bool(enabled))


def set_voice_key_config_short_press_resend(
    config_data: Dict[str, Any], slot: int, enabled: bool
) -> Dict[str, Any]:
    """Return a copy with one voice key's short-press resend toggled."""

    return update_voice_key_config(
        config_data, int(slot), short_press_resend=bool(enabled)
    )


def set_voice_key_config_gain_steps(
    config_data: Dict[str, Any], slot: int, steps: int
) -> Dict[str, Any]:
    """Return a copy with one voice key's recording gain (-3..+3) replaced."""

    return update_voice_key_config(config_data, int(slot), gain_steps=steps)


def set_voice_key_config_gain(
    config_data: Dict[str, Any], slot: int, steps: int, auto: bool
) -> Dict[str, Any]:
    """Return a copy with one voice key's manual/automatic gain replaced."""

    return update_voice_key_config(
        config_data, int(slot), gain_steps=steps, gain_auto=bool(auto)
    )


def voice_key_gain_auto(config_data: Dict[str, Any], slot: int) -> bool:
    """Whether one voice key uses the automatic peak normalization."""

    entry = voice_key_config(config_data, slot)
    return bool(entry.get("gain_auto", False)) if entry else False


def voice_key_gain_steps(config_data: Dict[str, Any], slot: int) -> int:
    """Return one voice key's recording gain in steps; 0 means untouched."""

    entry = voice_key_config(config_data, slot)
    if entry is None:
        return 0
    return _clamp_gain_steps(entry.get("gain_steps", 0))


def voice_key_gain_scale(config_data: Dict[str, Any], slot: int) -> float:
    """Return the linear multiplier for one voice key (6dB per step)."""

    steps = voice_key_gain_steps(config_data, slot)
    if not steps:
        return 1.0
    return 10 ** (steps * 6.0 / 20.0)


def apply_plugin_voice_keys(
    config_data: Dict[str, Any],
    plugin_id: str,
    entries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Merge the modes a connected plugin offers into the voice-key list.

    The user's own keys are never touched: the plugin's modes are appended
    after them and only the rows that plugin contributed earlier are replaced.
    A mode that already had a row keeps that row's id and on/off state, so the
    physical button bound to it survives a reconnect.
    """

    from . import key_mapping

    plugin = _normalize_plugin_id(plugin_id)
    if not plugin:
        return dict(config_data)
    existing = voice_key_configs(config_data)
    merged: List[Dict[str, Any]] = [
        entry
        for entry in existing
        if _normalize_plugin_id(entry.get("plugin")) != plugin
    ]
    # 优先按快捷键认回同一条语音键，其次才按名字。快捷键由插件自带、用户改
    # 不了，所以它稳定；而名字用户可能改过，若按插件默认名去匹配就会认不回来，
    # 每次插件重连都会把自定义名字刷回默认名。
    reusable_by_hotkey: Dict[str, Dict[str, Any]] = {}
    reusable_by_name: Dict[str, Dict[str, Any]] = {}
    for entry in existing:
        if _normalize_plugin_id(entry.get("plugin")) != plugin:
            continue
        hotkey = _normalize_hotkey_text(entry.get("hotkey", ""))
        if hotkey and hotkey not in reusable_by_hotkey:
            reusable_by_hotkey[hotkey] = entry
        name = _normalize_voice_key_name(entry.get("name"))
        if name and name not in reusable_by_name:
            reusable_by_name[name] = entry
    used_ids = {entry["id"] for entry in merged}
    for raw_entry in entries:
        if len(merged) >= key_mapping.MAX_VOICE_SLOTS:
            break
        if not isinstance(raw_entry, dict):
            continue
        name = _normalize_voice_key_name(raw_entry.get("name", ""))
        hotkey = _normalize_hotkey_text(raw_entry.get("hotkey", ""))
        if not name and not hotkey:
            continue
        previous = reusable_by_hotkey.get(hotkey) if hotkey else None
        if previous is None and name:
            previous = reusable_by_name.get(name)
        slot = 0
        if previous is not None and previous["id"] not in used_ids:
            slot = previous["id"]
        if slot <= 0:
            for candidate in range(1, key_mapping.MAX_VOICE_SLOTS + 1):
                if candidate not in used_ids:
                    slot = candidate
                    break
        if slot <= 0:
            break
        used_ids.add(slot)
        merged.append(
            _default_voice_key_config(
                slot=slot,
                # 认回同一条时保留用户改过的名字；全新行才用插件给的名字。
                name=previous["name"] if previous else name,
                hotkey=hotkey,
                enabled=bool(previous.get("enabled", True)) if previous else True,
                plugin=plugin,
                # 插件重连时保留用户给这条键调过的录音增益。
                gain_steps=previous.get("gain_steps", 0) if previous else 0,
                gain_auto=previous.get("gain_auto", False) if previous else False,
            )
        )
    return _with_voice_key_configs(config_data, merged)


def release_plugin_voice_keys(
    config_data: Dict[str, Any], plugin_id: str
) -> Dict[str, Any]:
    """Return a copy without the keys one plugin contributed."""

    plugin = _normalize_plugin_id(plugin_id)
    if not plugin:
        return dict(config_data)
    entries = voice_key_configs(config_data)
    remaining = [
        entry for entry in entries if _normalize_plugin_id(entry.get("plugin")) != plugin
    ]
    if len(remaining) == len(entries):
        return dict(config_data)
    if not remaining:
        remaining = [_default_voice_key_config()]
    return _with_voice_key_configs(config_data, remaining)


def set_voice_mode(
    config_data: Dict[str, Any], source: object, device: object = ""
) -> Dict[str, Any]:
    """Apply a profile-level voice mode to the first voice key.

    Saved profiles only carry one voice mode, so it targets the key the voice
    page lists first while the compatibility mirror stays aligned.
    """

    entries = voice_key_configs(config_data)
    return update_voice_key_config(
        config_data, entries[0]["id"], source=source, device=device
    )


def _normalize_voice_input_source(config: Dict[str, Any]) -> None:
    """Coerce the voice input source to a supported value.

    An unknown or missing value falls back to 模式1 (the remote microphone):
    that is the behaviour every existing installation already has, so an old
    or hand-edited file must never silently switch the audio source.
    """

    source = config.get("voice_input_source", VOICE_INPUT_SOURCE_REMOTE)
    if not isinstance(source, str) or source.strip().casefold() not in VOICE_INPUT_SOURCES:
        source = VOICE_INPUT_SOURCE_REMOTE
    else:
        source = source.strip().casefold()
    config["voice_input_source"] = source
    device = config.get("voice_local_input_device", "")
    if not isinstance(device, str):
        device = ""
    config["voice_local_input_device"] = device.strip()


def _normalize_desktop_behavior(config: Dict[str, Any]) -> None:
    config["launch_bridge_on_app_start"] = bool(
        config.get("launch_bridge_on_app_start", False)
    )
    config["diagnostic_trace_enabled"] = bool(
        config.get("diagnostic_trace_enabled", False)
    )
    prompted_offer_id = config.get("hid_helper_setup_prompted_offer_id", "")
    if not isinstance(prompted_offer_id, str) or len(prompted_offer_id) > 128:
        prompted_offer_id = ""
    prompted_offer_id = prompted_offer_id.strip()
    legacy_generation = config.pop("hid_helper_setup_prompted_generation", None)
    legacy_prompted = config.pop("hid_helper_setup_prompted", False)
    if not prompted_offer_id:
        if (
            isinstance(legacy_generation, int)
            and not isinstance(legacy_generation, bool)
            and legacy_generation > 0
        ):
            prompted_offer_id = f"legacy-generation:{legacy_generation}"
        elif legacy_prompted is True:
            prompted_offer_id = "legacy-generation:1"
    config["hid_helper_setup_prompted_offer_id"] = prompted_offer_id
    close_behavior = str(
        config.get("close_behavior", CLOSE_BEHAVIOR_HIDE_TO_TRAY)
    ).strip()
    if close_behavior not in VALID_CLOSE_BEHAVIORS:
        close_behavior = CLOSE_BEHAVIOR_HIDE_TO_TRAY
    config["close_behavior"] = close_behavior
    config["schema_version"] = SCHEMA_VERSION


def default_key_bindings(profile: str = "xiaomi-rc003") -> Dict[str, Any]:
    # Imported lazily to avoid a hard import-order dependency between the two
    # modules at package-load time.
    from . import key_mapping, remote_layout

    return {
        "schema_version": SCHEMA_VERSION,
        "bindings": {
            button_id: action.to_dict()
            for button_id, action in remote_layout.default_actions(profile).items()
        },
        # Secondary gestures follow the reference project's separate map.
        # Keeping the primary action flat preserves compatibility with all
        # existing Windows config files.
        "secondary_bindings": {},
        # One optional remote-button combination layer.  The modifier is
        # inert until at least one second-key action is configured.
        "combo_bindings": {
            "modifier": key_mapping.COMBO_MODIFIER_BUTTON_IDS[0],
            "bindings": {},
            "display_notes": {},
        },
        # Optional user-facing labels for each gesture. They never participate
        # in action parsing or execution and are safe to omit in older files.
        "display_notes": {},
        # Physical signatures are learned from Raw Input captures. They are
        # deliberately independent of semantic actions and contain no device
        # path or Bluetooth identity.
        "physical_bindings": {},
    }


def load_key_bindings(path: Path) -> Dict[str, Any]:
    selected = _read_settings_document(Path(path).with_name(CONFIG_FILENAME))
    return _load_key_bindings_for_selection(path, selected)


def _load_key_bindings_for_selection(path: Path, selected: dict) -> Dict[str, Any]:
    from . import remote_settings, remote_selection
    profile = remote_selection.active_profile(selected) or remote_selection.RC003_PROFILE
    bindings = default_key_bindings(profile)
    if path.is_file():
        with path.open("r", encoding="utf-8-sig") as handle:
            stored = json.load(handle)
        if not isinstance(stored, dict):
            raise ConfigFormatError("key_bindings.json root must be a JSON object")
        _assert_no_forbidden_keys(stored)
        if remote_settings.STORE in stored:
            stored = remote_settings.project(stored, remote_settings.BINDING_FIELDS,
                                             remote_selection.active_key(selected))
        for key, value in stored.items():
            if key in {
                "bindings",
                "secondary_bindings",
                "combo_bindings",
                "physical_bindings",
                "display_notes",
            }:
                if isinstance(value, dict):
                    current = bindings.get(key)
                    if not isinstance(current, dict):
                        current = {}
                        bindings[key] = current
                    current.update(value)
                else:
                    bindings[key] = value
            else:
                bindings[key] = value
    if not isinstance(bindings.get("bindings"), dict):
        bindings["bindings"] = {}
    if not isinstance(bindings.get("secondary_bindings"), dict):
        bindings["secondary_bindings"] = {}
    _normalize_physical_bindings(bindings)
    _normalize_semantic_actions(bindings)
    _normalize_secondary_bindings(bindings)
    _normalize_combo_bindings(bindings)
    _normalize_display_notes(bindings)
    bindings["schema_version"] = SCHEMA_VERSION
    return bindings


def normalize_voice_product_boundary(
    config_data: Dict[str, Any], bindings: Dict[str, Any]
) -> Dict[str, str]:
    """Fail closed for voice mappings the RC003 cannot actually support.

    The returned map is runtime-only and names every button that requires an
    explicit user choice. The caller may display it, log it, or suppress the
    whole button gesture. The original file is not overwritten until save.
    """

    from . import key_mapping

    primary = bindings.get("bindings")
    if not isinstance(primary, dict):
        bindings[RUNTIME_REMOVED_VOICE_BINDINGS_KEY] = {}
        return {}
    legacy_mode = str(
        config_data.get(RUNTIME_LEGACY_VOICE_MODE_KEY, "hold")
    ).strip().lower()
    removed: Dict[str, str] = {}
    for button_id, raw_action in list(primary.items()):
        try:
            action = key_mapping.ButtonAction.from_dict(raw_action)
        except (KeyError, TypeError, ValueError):
            continue
        if action.kind == key_mapping.ActionKind.VOICE:
            if button_id == "mic" and legacy_mode == "hold":
                primary[button_id] = key_mapping.ButtonAction(
                    key_mapping.ActionKind.VOICE_HOLD
                ).to_dict()
            else:
                removed[button_id] = action.kind.value
        elif action.kind == key_mapping.ActionKind.VOICE_TOGGLE:
            removed[button_id] = action.kind.value

    secondary = bindings.get("secondary_bindings")
    if isinstance(secondary, dict):
        for button_id, trigger_map in secondary.items():
            if not isinstance(trigger_map, dict):
                continue
            for raw_action in trigger_map.values():
                try:
                    action = key_mapping.ButtonAction.from_dict(raw_action)
                except (KeyError, TypeError, ValueError):
                    continue
                if key_mapping.is_voice_action(action):
                    removed.setdefault(button_id, action.kind.value)
    bindings[RUNTIME_REMOVED_VOICE_BINDINGS_KEY] = removed
    return dict(removed)


def _normalize_semantic_actions(bindings: Dict[str, Any]) -> None:
    """Migrate old reference-looking key chords to real action kinds.

    Builds before the semantic action layer wrote values such as
    ``{"kind":"key_combo","keys":["up"]}``.  Keeping those values in
    memory would make the UI look correct while the runtime still takes the
    generic shortcut path.  Convert only the exact built-in chords; arbitrary
    user-recorded combinations remain custom ``key_combo`` actions.
    """

    from . import key_mapping

    def normalize(raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        try:
            action = key_mapping.ButtonAction.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            return raw
        migrated = key_mapping.semantic_action_for_keys(action.keys)
        return migrated.to_dict() if migrated is not None else raw

    primary = bindings.get("bindings")
    if isinstance(primary, dict):
        for button_id, raw in list(primary.items()):
            primary[button_id] = normalize(raw)

    secondary = bindings.get("secondary_bindings")
    if isinstance(secondary, dict):
        for button_id, trigger_map in list(secondary.items()):
            if not isinstance(trigger_map, dict):
                continue
            for trigger_name, raw in list(trigger_map.items()):
                trigger_map[trigger_name] = normalize(raw)

    combo = bindings.get("combo_bindings")
    if isinstance(combo, dict):
        combo_actions = combo.get("bindings")
        if isinstance(combo_actions, dict):
            for button_id, raw in list(combo_actions.items()):
                combo_actions[button_id] = normalize(raw)


def _normalize_secondary_bindings(bindings: Dict[str, Any]) -> None:
    """Keep optional double/long mappings structurally safe on load.

    A malformed secondary entry is ignored by the runtime action lookup, but
    the container itself must still be a mapping so a damaged config cannot
    make the settings page or save path crash. Every physical button,
    including the microphone button, may use ordinary secondary gestures.
    """

    secondary = bindings.get("secondary_bindings")
    if not isinstance(secondary, dict):
        bindings["secondary_bindings"] = {}
        return
    for button_id in list(secondary):
        entry = secondary[button_id]
        if not isinstance(entry, dict):
            secondary.pop(button_id, None)
            continue
        for trigger in list(entry):
            if trigger not in {"double_click", "long_press"} or not isinstance(
                entry[trigger], dict
            ):
                entry.pop(trigger, None)
        if not entry:
            secondary.pop(button_id, None)


def _normalize_combo_bindings(bindings: Dict[str, Any]) -> None:
    """Keep the optional one-modifier combination layer fail-closed."""

    from . import key_mapping

    raw_combo = bindings.get("combo_bindings")
    if not isinstance(raw_combo, dict):
        raw_combo = {}
    modifier = raw_combo.get("modifier")
    if modifier not in key_mapping.COMBO_MODIFIER_BUTTON_IDS:
        modifier = key_mapping.COMBO_MODIFIER_BUTTON_IDS[0]

    normalized_actions: Dict[str, dict] = {}
    raw_actions = raw_combo.get("bindings")
    if isinstance(raw_actions, dict):
        for button_id in key_mapping.COMBO_ACTION_BUTTON_IDS:
            raw_action = raw_actions.get(button_id)
            if not isinstance(raw_action, dict):
                continue
            try:
                action = key_mapping.ButtonAction.from_dict(raw_action)
            except (KeyError, TypeError, ValueError):
                continue
            if (
                action.kind != key_mapping.ActionKind.DISABLED
                and not key_mapping.is_voice_action(action)
            ):
                normalized_actions[button_id] = action.to_dict()

    # A hand-edited file must not let the same physical modifier own both
    # the Fn-style combination layer and delayed double/long gestures. Keep
    # the older single-button gestures and fail the newer combination layer
    # closed until the conflict is resolved in Settings.
    if normalized_actions and key_mapping.has_secondary_action(
        bindings, str(modifier)
    ):
        normalized_actions = {}

    normalized_notes: Dict[str, str] = {}
    raw_notes = raw_combo.get("display_notes")
    if isinstance(raw_notes, dict):
        for button_id in normalized_actions:
            note = raw_notes.get(button_id)
            if isinstance(note, str) and note.strip() and note.strip() != "未命名":
                normalized_notes[button_id] = note.strip()

    bindings["combo_bindings"] = {
        "modifier": str(modifier),
        "bindings": normalized_actions,
        "display_notes": normalized_notes,
    }


def _normalize_physical_bindings(bindings: Dict[str, Any]) -> None:
    """Keep learned physical overrides portable and action-safe."""

    from . import device_profile

    physical = bindings.get("physical_bindings")
    if not isinstance(physical, dict):
        bindings["physical_bindings"] = {}
        return
    bindings["physical_bindings"] = {
        str(signature): str(button_id)
        for signature, button_id in physical.items()
        if isinstance(signature, str)
        and signature.strip()
        and isinstance(button_id, str)
        and button_id in device_profile.ALL_BUTTON_IDS
    }


def _normalize_display_notes(bindings: Dict[str, Any]) -> None:
    """Keep optional display-only labels separate from executable actions."""

    from . import device_profile, key_mapping

    valid_buttons = set(device_profile.ALL_BUTTON_IDS)

    raw_notes = bindings.get("display_notes")
    if not isinstance(raw_notes, dict):
        bindings["display_notes"] = {}
        return
    valid_triggers = {
        key_mapping.ButtonTrigger.SINGLE_CLICK.value,
        key_mapping.ButtonTrigger.DOUBLE_CLICK.value,
        key_mapping.ButtonTrigger.LONG_PRESS.value,
    }
    normalized: Dict[str, Dict[str, str]] = {}
    for button_id, trigger_map in raw_notes.items():
        if button_id not in valid_buttons or not isinstance(
            trigger_map, dict
        ):
            continue
        clean_trigger_map = {
            str(trigger): str(note).strip()
            for trigger, note in trigger_map.items()
            if trigger in valid_triggers
            and isinstance(note, str)
            and note.strip()
            and note.strip() != "未命名"
        }
        if clean_trigger_map:
            normalized[str(button_id)] = clean_trigger_map
    bindings["display_notes"] = normalized


def save_key_bindings(path: Path, bindings: Dict[str, Any]) -> None:
    from . import remote_settings, remote_selection
    persisted = _without_runtime_only_keys(bindings)
    persisted["schema_version"] = SCHEMA_VERSION
    _assert_no_forbidden_keys(persisted)
    latest = _read_settings_document(path)
    selected = _read_settings_document(Path(path).with_name(CONFIG_FILENAME))
    persisted = remote_settings.pack(persisted, remote_settings.BINDING_FIELDS,
                                    remote_selection.active_key(selected), latest)
    _save_json_atomic(path, persisted)


def switch_remote_settings(config_file: Path, bindings_file: Path, selection: dict,
                           *, allow_legacy_binding: bool = True):
    """Switch canonical records only after the caller has stopped old execution.

    Migration is explicit and reversible, not a side effect of load. Unbound
    legacy fields stay unassigned, never inherited by an arbitrary new device.
    """
    from . import remote_settings, remote_selection
    if (Path(bindings_file).with_name(CONFIG_FILENAME).absolute() != Path(config_file).absolute()
            or Path(config_file).absolute() == Path(bindings_file).absolute()):
        raise ValueError("remote settings must use the same configuration directory")
    old_config = load_config(config_file)
    old_bindings = load_key_bindings(bindings_file)
    settings, bindings = remote_settings.switch_views(
        old_config, old_bindings, selection, allow_legacy_binding=allow_legacy_binding)
    # Supply fresh defaults for a genuinely new entity, never the old view.
    merged = default_config()
    merged.update(settings)
    settings = merged
    merged = default_key_bindings(remote_selection.active_profile(settings))
    merged.update(bindings)
    bindings = merged
    _normalize_voice_hotkey(settings)
    _normalize_voice_input_source(settings)
    _normalize_remote_recording_limit(settings)
    active = remote_selection.active_key(settings)
    settings = remote_settings.pack(_without_runtime_only_keys(settings), remote_settings.CONFIG_FIELDS, active)
    bindings = remote_settings.pack(_without_runtime_only_keys(bindings), remote_settings.BINDING_FIELDS, active)
    settings.pop("selected_device_profile", None)
    _assert_no_forbidden_keys(settings)
    _assert_no_forbidden_keys(bindings)
    snapshots = [(config_file, _read_file_snapshot(config_file)),
                 (bindings_file, _read_file_snapshot(bindings_file))]
    try:
        # Both old and new mapping records are present. Publish them before the
        # single active-selection pointer, so an interrupted switch cannot
        # interpret a legacy flat mapping as belonging to the new entity.
        _save_json_atomic(bindings_file, bindings)
        _save_json_atomic(config_file, settings)
        return load_config(config_file), load_key_bindings(bindings_file)
    except BaseException as error:
        failures = []
        for filename, snapshot in snapshots:
            try:
                _restore_file_snapshot(filename, snapshot)
                _verify_file_snapshot(filename, snapshot)
            except Exception as restore_error:
                failures.append(restore_error)
        if failures:
            raise ConfigTransactionError("设备切换失败且设置未能完全恢复。") from error
        raise


def save_settings_pair(
    config_file: Path,
    config_data: Dict[str, Any],
    bindings_file: Path,
    bindings_data: Dict[str, Any],
) -> None:
    """Save the two user-facing settings documents as one recoverable action.

    NTFS does not provide a multi-file atomic replace. Validate both
    documents before touching disk, then restore the first file byte-for-byte
    if the second atomic write fails. This prevents ordinary disk-lock,
    permission, serialization, and replace failures from leaving a mixed
    configuration behind.
    """

    if Path(config_file).absolute() == Path(bindings_file).absolute():
        raise ValueError("paired settings paths must be distinct")
    _assert_settings_writable(config_data)
    _assert_settings_writable(bindings_data)
    _assert_no_forbidden_keys(config_data)
    _assert_no_forbidden_keys(bindings_data)

    previous_config = _read_file_snapshot(config_file)
    config_saved = False
    try:
        save_config(config_file, config_data)
        config_saved = True
        save_key_bindings(bindings_file, bindings_data)
    except BaseException as save_exc:
        if config_saved:
            try:
                _restore_file_snapshot(config_file, previous_config)
            except Exception as rollback_exc:
                raise ConfigTransactionError(
                    "paired settings save failed and config rollback was incomplete"
                ) from save_exc
        raise


def _save_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    """Write one settings file without exposing a half-written JSON file."""

    content = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _save_bytes_atomic(path, content)


def _without_runtime_only_keys(data: Dict[str, Any]) -> Dict[str, Any]:
    _assert_settings_writable(data)
    return {
        key: value
        for key, value in data.items()
        if key not in _RUNTIME_ONLY_KEYS
    }


def _assert_settings_writable(data: dict) -> None:
    if data.get(RUNTIME_READ_ONLY_KEY):
        raise ConfigReadOnlyError("配置读取异常，原文件已保留；修复后请重新打开MoteDeck。")


def _save_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _restore_file_snapshot(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    _save_bytes_atomic(path, content)


def _read_file_snapshot(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _verify_file_snapshot(path: Path, content: bytes | None) -> None:
    if content is None:
        try:
            path.stat()
        except FileNotFoundError:
            return
        raise OSError("restored file should not exist")
    if not path.is_file() or path.read_bytes() != content:
        raise OSError("restored file does not match its previous contents")
