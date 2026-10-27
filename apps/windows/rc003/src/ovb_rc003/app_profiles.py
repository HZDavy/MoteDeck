"""Application profiles: a global layout plus per-process overrides.

This module owns ``app_profiles.json`` (schema 6) under ``config_root``. It
replaces the older ``mapping_presets.json`` library: on first read the legacy
file, when it exists, is converted in memory and written to the new location.
The legacy file itself is never modified or deleted, so a downgrade keeps
working and the user's data survives the migration twice over.

The model is deliberately flat: one document, one list of profiles, one
``applied`` marker. Each profile carries a scope (``global`` or ``app``), the
process basename it is bound to, an optional switch hotkey, a mapping and the
voice mode it was saved with (模式1 remote microphone / 模式2 local microphone +
device name). Unlike the legacy library an app profile's mapping may be sparse -
the buttons it does not mention fall back to the global layout, which lives in
the user's ordinary ``key_bindings.json`` and is never duplicated here.

Only this module writes ``app_profiles.json``; it is user data, never a build
asset. Portable exports contain no identifiers, no device identity and no
switching state; a bundle carries the global layout and every app layout so a
whole setup moves between machines in one file.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import UUID, uuid4

from . import config, device_catalog, key_mapping, mapping_presets, remote_layout

FILENAME = "app_profiles.json"
LEGACY_FILENAME = "mapping_presets.json"
FORMAT = "RemoteMic.key-mapping"
# One file that carries the global layout plus every application layout, so a
# user can move a whole setup between machines instead of one snapshot at a time.
BUNDLE_FORMAT = "RemoteMic.full-configuration"
# Version 2 adds the voice mode (模式1/模式2 + local microphone name) to both the
# global row and every application row. Version 1 files still import unchanged.
BUNDLE_SCHEMA_VERSION = 2
_BUNDLE_V1_FIELDS = {"format", "schema_version", "profile", "global", "apps"}
_BUNDLE_V2_FIELDS = _BUNDLE_V1_FIELDS | {"voice"}
BUNDLE_FIELDS = _BUNDLE_V2_FIELDS
_BUNDLE_APP_V1_FIELDS = {"name", "process", "hotkey", "mapping"}
BUNDLE_APP_FIELDS = _BUNDLE_APP_V1_FIELDS | {"voice_input_source", "voice_local_input_device"}
# "导出全部" on the voice page. Unlike the bundle above this also carries the
# saved global-scope profiles (the copies), which never appear as application
# entries, so the whole library can be kept as one backup file.
LIBRARY_FORMAT = "RemoteMic.profile-library"
# Version 2 also carries each profile's complete voice-key list (order included).
LIBRARY_SCHEMA_VERSION = 2
MAX_BYTES = mapping_presets.MAX_BYTES
MAX_PROFILES = mapping_presets.MAX_PRESETS
MAPPING_FIELDS = mapping_presets.MAPPING_FIELDS
PROFILES = mapping_presets.PROFILES
SCOPES = mapping_presets.SCOPES
SCOPE_LABELS = mapping_presets.SCOPE_LABELS
DEFAULT_SCOPE = mapping_presets.DEFAULT_SCOPE
# The always-present row that stands for the user's ordinary key_bindings.json.
GLOBAL_ID = mapping_presets.AUTO_ID
GLOBAL_NAME = mapping_presets.AUTO_NAME
VOICE_FIELDS = ("voice_input_source", "voice_local_input_device")
# 一条语音键的完整快照：列表顺序就是用户排好的顺序，关闭的语音键也保留。
VOICE_KEY_FIELDS = ("id", "name", "hotkey", "source", "device", "enabled", "plugin")
# config.json 的语音键后来多了“延迟发送”开关。旧版保存的 profile 快照里
# 没有这个字段，所以它只能是可选字段，不能参与“字段集合必须完全相等”
# 的校验，否则老数据会让整份配置读取失败。
OPTIONAL_VOICE_KEY_FIELDS = ("short_press_resend",)
VOICE_KEYS_FIELD = "voice_keys"
ENTRY_FIELDS = ("id", "name", "scope", "process", "hotkey", "mapping",
                *VOICE_FIELDS, VOICE_KEYS_FIELD)
SCHEMA_VERSION = 6
PREVIOUS_SCHEMA_VERSIONS = (4, 5)
LEGACY_SCHEMA_VERSIONS = (1, 2, 3)
# One portable single-profile export. Version 2 carries the voice mode, version
# 3 adds the complete voice-key list.
EXPORT_SCHEMA_VERSION = 3
_EXPORT_V1_FIELDS = {"format", "schema_version", "name", "profile", "mapping"}
_EXPORT_V2_FIELDS = _EXPORT_V1_FIELDS | set(VOICE_FIELDS)
EXPORT_FIELDS = _EXPORT_V2_FIELDS | {VOICE_KEYS_FIELD}
_V4_ENTRY_FIELDS = set(ENTRY_FIELDS) - set(VOICE_FIELDS) - {VOICE_KEYS_FIELD}
_V5_ENTRY_FIELDS = set(ENTRY_FIELDS) - {VOICE_KEYS_FIELD}
VOICE_MODE_LABELS = {
    config.VOICE_INPUT_SOURCE_REMOTE: "模式1",
    config.VOICE_INPUT_SOURCE_LOCAL: "模式2",
}
VOICE_DEVICE_MAX_LENGTH = 128


_validate_name = mapping_presets.validate_name
_validate_scope = mapping_presets.validate_scope
_validate_process = mapping_presets.validate_process
_validate_hotkey = mapping_presets.validate_hotkey
_validate_action = mapping_presets._validate_action


def validate_voice(source: object = config.VOICE_INPUT_SOURCE_REMOTE,
                   device: object = "") -> tuple[str, str]:
    """Normalize one profile's voice mode.

    模式1 always drops the device name: it can only ever mean the remote's own
    microphone. 模式2 keeps the chosen device name, which may still be empty
    until the user picks one on the voice page.
    """
    if not isinstance(source, str) or source.strip().casefold() not in config.VOICE_INPUT_SOURCES:
        raise ValueError("语音输入模式无效。")
    source = source.strip().casefold()
    if not isinstance(device, str):
        raise ValueError("麦克风名称格式无效。")
    device = device.strip()
    if len(device) > VOICE_DEVICE_MAX_LENGTH:
        raise ValueError(f"麦克风名称最多 {VOICE_DEVICE_MAX_LENGTH} 个字符。")
    if any(ord(char) < 32 for char in device):
        raise ValueError("麦克风名称不能包含换行或控制字符。")
    if source == config.VOICE_INPUT_SOURCE_REMOTE:
        device = ""
    return source, device


def voice_mode_label(source: object) -> str:
    """Return the user-facing mode name for one voice source."""
    normalized, _ = validate_voice(source)
    return VOICE_MODE_LABELS[normalized]


def mode_tag(entry: dict) -> str:
    """Return the automatic tag appended to a profile name.

    模式1 needs no device name (it is always the remote microphone); 模式2
    carries the microphone it records from so two profiles on the same layout
    stay distinguishable in the list.
    """
    source, device = validate_voice(entry.get("voice_input_source", config.VOICE_INPUT_SOURCE_REMOTE),
                                    entry.get("voice_local_input_device", ""))
    if source == config.VOICE_INPUT_SOURCE_LOCAL:
        return f"{VOICE_MODE_LABELS[source]} · {device}" if device else VOICE_MODE_LABELS[source]
    return VOICE_MODE_LABELS[source]


def display_label(entry: dict) -> str:
    """Return the list label: the user's name plus the automatic mode tag."""
    return f"{entry.get('name', '')}（{mode_tag(entry)}）"


def _voice_actions(mapping: dict) -> list[tuple[str, int]]:
    """Return ``(button, slot)`` for every voice action a mapping carries."""
    found: list[tuple[str, int]] = []
    for field in ("bindings", "secondary_bindings"):
        raw = mapping.get(field, {}) if isinstance(mapping, dict) else {}
        if not isinstance(raw, dict):
            continue
        for button, value in raw.items():
            actions = value.values() if field == "secondary_bindings" and isinstance(value, dict) else (value,)
            for action in actions:
                try:
                    parsed = key_mapping.ButtonAction.from_dict(action)
                except (KeyError, TypeError, ValueError):
                    continue
                slot = key_mapping.voice_slot_for_action(parsed)
                if slot is not None:
                    found.append((button, slot))
    return found


def validate_voice_keys(value: object) -> list[dict]:
    """Validate one profile's complete voice-key snapshot, order included."""
    if not isinstance(value, list) or len(value) > key_mapping.MAX_VOICE_SLOTS:
        raise ValueError("配置里的语音键列表格式无效。")
    result, ids = [], set()
    for raw in value:
        if (not isinstance(raw, dict)
                or not set(VOICE_KEY_FIELDS) <= set(raw)
                or set(raw) - set(VOICE_KEY_FIELDS) - set(OPTIONAL_VOICE_KEY_FIELDS)):
            raise ValueError("配置里的语音键格式无效。")
        try:
            slot = int(raw["id"])
        except (TypeError, ValueError):
            raise ValueError("配置里的语音键编号无效。") from None
        if slot < 1 or slot > key_mapping.MAX_VOICE_SLOTS or slot in ids:
            raise ValueError("配置里的语音键编号无效。")
        ids.add(slot)
        name, hotkey, plugin = raw["name"], raw["hotkey"], raw["plugin"]
        if (not isinstance(name, str) or len(name) > 64
                or not isinstance(hotkey, str) or len(hotkey) > 128
                or not isinstance(plugin, str) or len(plugin) > 64
                or not isinstance(raw["enabled"], bool)):
            raise ValueError("配置里的语音键内容无效。")
        source, device = validate_voice(raw["source"], raw["device"])
        result.append({"id": slot, "name": name.strip(),
                       "hotkey": hotkey.strip().lower(), "source": source,
                       "device": device, "enabled": raw["enabled"],
                       "plugin": plugin.strip().lower(),
                       "short_press_resend": raw.get("short_press_resend", False) is not False})
    return result


def plugin_slot_ids(voice_keys: "list[dict] | None") -> set[int]:
    """Return the slots an external plugin owns, so 模式1 does not flag them."""
    return {int(entry["id"]) for entry in (voice_keys or [])
            if str(entry.get("plugin") or "").strip()}


def voice_conflicts(mapping: dict, source: object = config.VOICE_INPUT_SOURCE_REMOTE,
                    profile: str = device_catalog.RC003_ID,
                    plugin_slots: set[int] = frozenset()) -> list[str]:
    """Return the buttons 模式1 cannot honour, in remote-layout order.

    模式1 can only record while the physical microphone key is held and only
    drives 语音1, so a voice action on any other button (or on 语音2) would
    silently never fire. The list feeds the "change these keys first" warning.
    Plugin-owned slots are exempt: the plugin records its own audio, so neither
    the microphone key nor 语音1 applies to it.
    """
    if validate_voice(source)[0] != config.VOICE_INPUT_SOURCE_REMOTE:
        return []
    if profile not in PROFILES:
        raise ValueError("此配置的遥控器型号不受支持。")
    hits: set[str] = set()
    for button, slot in _voice_actions(mapping):
        if slot in plugin_slots:
            continue
        if button != "mic" or slot != 1:
            hits.add(button)
    names = remote_layout.display_names(profile)
    return [names.get(button, button) for button in remote_layout.button_order(profile)
            if button in hits]


def voice_conflict_message(buttons: list[str]) -> str:
    """Return the warning text for a 模式1 profile with unusable voice keys."""
    if not buttons:
        return ""
    return ("模式1 只能用遥控器的话筒键触发语音1；请先把这些按键上的语音操作改掉："
            + "、".join(buttons))


def resolve_voice(entry: dict) -> dict:
    """Return the two voice fields of a profile, normalized."""
    source, device = validate_voice(
        entry.get("voice_input_source", config.VOICE_INPUT_SOURCE_REMOTE),
        entry.get("voice_local_input_device", ""))
    return {"voice_input_source": source, "voice_local_input_device": device}


def validate_mapping(value: object, profile: str = device_catalog.RC003_ID,
                     *, partial: bool = False) -> dict:
    """Validate one profile's mapping.

    ``partial`` allows an app profile to mention only some buttons; an omitted
    button falls back to the global layout at resolve time. A global profile is
    always complete, so the sparse form is rejected there.
    """
    if profile not in PROFILES:
        raise ValueError("此配置的遥控器型号不受支持。")
    if not isinstance(value, dict) or set(value) != set(MAPPING_FIELDS):
        raise ValueError("按键配置内容不完整或含有不支持的字段。")
    allowed = set(remote_layout.button_order(profile))
    result = copy.deepcopy(value)
    for field in MAPPING_FIELDS:
        if not isinstance(result[field], dict) or set(result[field]) - allowed:
            raise ValueError("配置包含不属于此型号的按键。")
    if not partial and set(result["bindings"]) != allowed:
        raise ValueError("配置必须包含此型号的全部按键。")
    for button, raw in result["bindings"].items():
        _validate_action(raw, button, "single_click", profile)
    for button, actions in result["secondary_bindings"].items():
        if not isinstance(actions, dict) or set(actions) - {"double_click", "long_press"}:
            raise ValueError("双击或长按配置无效。")
        for trigger, raw in actions.items():
            _validate_action(raw, button, trigger, profile)
    for notes in result["display_notes"].values():
        if not isinstance(notes, dict) or set(notes) - {trigger.value for trigger in key_mapping.ButtonTrigger}:
            raise ValueError("按键备注格式无效。")
        if any(not isinstance(note, str) or len(note) > 256 for note in notes.values()):
            raise ValueError("每条备注最多 256 个字符。")
    return result


def snapshot(bindings: dict, profile: str = device_catalog.RC003_ID) -> dict:
    allowed = remote_layout.button_order(profile)
    value = {field: {button: copy.deepcopy(data) for button, data in bindings.get(field, {}).items()
                     if button in allowed} for field in MAPPING_FIELDS}
    # Absent primary actions have always meant disabled at runtime.
    value["bindings"] = {button: value["bindings"].get(button, {"kind": "disabled", "keys": []})
                         for button in allowed}
    return validate_mapping(value, profile)


def resolve_mapping(bindings: dict, override: dict | None,
                    profile: str = device_catalog.RC003_ID) -> dict:
    """Lay one app profile over the global bindings, button by button.

    An app profile only wins for the buttons and triggers it actually defines;
    everything else keeps the global action. ``physical_bindings`` and any other
    key the mapping format does not own are passed through untouched.
    """
    result = copy.deepcopy(bindings)
    if override is None:
        return result
    if profile not in PROFILES:
        raise ValueError("此配置的遥控器型号不受支持。")
    patch = validate_mapping(override, profile, partial=True)
    result["bindings"] = {**result.get("bindings", {}), **patch["bindings"]}
    secondary = {button: dict(actions) for button, actions in result.get("secondary_bindings", {}).items()}
    for button, actions in patch["secondary_bindings"].items():
        secondary[button] = {**secondary.get(button, {}), **actions}
    result["secondary_bindings"] = secondary
    notes = {button: dict(value) for button, value in result.get("display_notes", {}).items()}
    for button, value in patch["display_notes"].items():
        notes[button] = {**notes.get(button, {}), **value}
    result["display_notes"] = notes
    return result


def apply_to(bindings: dict, entry: dict, profile: str = device_catalog.RC003_ID) -> dict:
    """Apply one profile's mapping, the way the legacy library did wholesale."""
    if profile not in PROFILES:
        raise ValueError("此配置的遥控器型号不受支持。")
    return resolve_mapping(bindings, entry["mapping"], profile)


def _comparable_mapping(mapping: dict, profile: str) -> dict:
    """Match the loader's normalization without rewriting the saved profile."""
    result = copy.deepcopy(mapping)
    config._normalize_semantic_actions(result)
    config._normalize_display_notes(result)
    result["bindings"] = {button: key_mapping.ButtonAction.from_dict(raw).to_dict()
                          for button, raw in result["bindings"].items()}
    secondary = {}
    for button, actions in result["secondary_bindings"].items():
        enabled = {}
        for trigger, raw in actions.items():
            action = key_mapping.ButtonAction.from_dict(raw)
            if action.kind != key_mapping.ActionKind.DISABLED:
                enabled[trigger] = action.to_dict()
        if enabled:
            secondary[button] = enabled
    result["secondary_bindings"] = secondary
    return result


def _read(path: Path) -> dict:
    return mapping_presets._read(path)


def validate_export(value: dict) -> dict:
    """Validate one portable single-profile export before anything is written.

    An app profile only stores the buttons it overrides, so the mapping here may
    be sparse; the target scope decides at import time whether that is allowed.
    Version 1 files predate the voice mode and import as 模式1.
    """
    if not isinstance(value, dict):
        raise ValueError("请选择本软件导出的按键配置文件（版本 1、2 或 3）。")
    version = value.get("schema_version")
    if (value.get("format") != FORMAT or type(version) is not int
            or version not in (1, 2, EXPORT_SCHEMA_VERSION)):
        raise ValueError("请选择本软件导出的按键配置文件（版本 1、2 或 3）。")
    expected = {1: _EXPORT_V1_FIELDS, 2: _EXPORT_V2_FIELDS,
                EXPORT_SCHEMA_VERSION: EXPORT_FIELDS}[version]
    if set(value) != expected:
        raise ValueError("请选择本软件导出的按键配置文件（版本 1、2 或 3）。")
    if version == 1:
        source, device = config.VOICE_INPUT_SOURCE_REMOTE, ""
    else:
        source, device = validate_voice(value["voice_input_source"],
                                        value["voice_local_input_device"])
    return {"format": FORMAT, "schema_version": EXPORT_SCHEMA_VERSION,
            "name": _validate_name(value["name"]), "profile": value["profile"],
            "mapping": validate_mapping(value["mapping"], value["profile"], partial=True),
            "voice_input_source": source, "voice_local_input_device": device,
            "voice_keys": (validate_voice_keys(value[VOICE_KEYS_FIELD])
                           if version == EXPORT_SCHEMA_VERSION else [])}


def read_export(path: Path, profile: str = device_catalog.RC003_ID) -> dict:
    """Read one single-layout export and check it belongs to this remote model."""
    document = validate_export(_read(Path(path)))
    if document["profile"] != profile:
        raise ValueError("配置的遥控器型号与当前型号不同，请先切换设备。")
    return document


def build_bundle(profile: str, global_mapping: dict, app_entries: list[dict],
                 voice_input_source: str = config.VOICE_INPUT_SOURCE_REMOTE,
                 voice_local_input_device: str = "") -> dict:
    """Pack the global layout and every application layout into one document.

    Profile identifiers stay behind: an imported bundle always creates fresh
    entries, exactly like the single-profile export does. Each entry carries the
    voice mode it was saved with; the bundle-level voice applies to the global
    layout.
    """
    if profile not in PROFILES:
        raise ValueError("此配置的遥控器型号不受支持。")
    source, device = validate_voice(voice_input_source, voice_local_input_device)
    apps = []
    for entry in app_entries:
        entry_source, entry_device = validate_voice(
            entry.get("voice_input_source", source),
            entry.get("voice_local_input_device", device))
        apps.append({"name": _validate_name(entry["name"]),
                     "process": _validate_process(entry["process"], "app"),
                     "hotkey": _validate_hotkey(entry.get("hotkey", "")),
                     "mapping": validate_mapping(entry["mapping"], profile, partial=True),
                     "voice_input_source": entry_source,
                     "voice_local_input_device": entry_device})
    return {
        "format": BUNDLE_FORMAT,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "profile": profile,
        "voice": {"voice_input_source": source, "voice_local_input_device": device},
        "global": validate_mapping(global_mapping, profile),
        "apps": apps,
    }


def validate_bundle(value: object, profile: str) -> dict:
    """Validate one full-configuration export before anything is written."""
    if profile not in PROFILES:
        raise ValueError("此配置的遥控器型号不受支持。")
    if not isinstance(value, dict):
        raise ValueError("请选择本软件导出的整体配置文件（版本 1 或 2）。")
    version = value.get("schema_version")
    app_fields = _BUNDLE_APP_V1_FIELDS if version == 1 else BUNDLE_APP_FIELDS
    if (value.get("format") != BUNDLE_FORMAT or type(version) is not int
            or version not in (1, BUNDLE_SCHEMA_VERSION)
            or set(value) != (_BUNDLE_V1_FIELDS if version == 1 else BUNDLE_FIELDS)):
        raise ValueError("请选择本软件导出的整体配置文件（版本 1 或 2）。")
    if value.get("profile") != profile:
        raise ValueError("配置的遥控器型号与当前型号不同，请先切换设备。")
    if version == 1:
        source, device = config.VOICE_INPUT_SOURCE_REMOTE, ""
    else:
        voice = value.get("voice")
        if not isinstance(voice, dict) or set(voice) != set(VOICE_FIELDS):
            raise ValueError("整体配置文件里的语音模式无效。")
        source, device = validate_voice(voice["voice_input_source"],
                                        voice["voice_local_input_device"])
    apps = value.get("apps")
    if not isinstance(apps, list) or len(apps) > MAX_PROFILES:
        raise ValueError("整体配置文件里的应用列表无效。")
    result, processes = [], set()
    for app in apps:
        if not isinstance(app, dict) or set(app) != app_fields:
            raise ValueError("整体配置文件里的应用配置格式无效。")
        process = _validate_process(app["process"], "app")
        if process.casefold() in processes:
            raise ValueError(f"整体配置里有两份绑定同一进程 {process} 的配置。")
        processes.add(process.casefold())
        app_source, app_device = (source, device) if version == 1 else validate_voice(
            app["voice_input_source"], app["voice_local_input_device"])
        result.append({"name": _validate_name(app["name"]), "process": process,
                       "hotkey": _validate_hotkey(app["hotkey"]),
                       "mapping": validate_mapping(app["mapping"], profile, partial=True),
                       "voice_input_source": app_source,
                       "voice_local_input_device": app_device})
    return {"profile": profile, "global": validate_mapping(value["global"], profile),
            "voice_input_source": source, "voice_local_input_device": device,
            "apps": result}


def read_bundle(path: Path, profile: str) -> dict:
    """Read and validate one full-configuration export from disk."""
    return validate_bundle(_read(Path(path)), profile)


def build_library(profile: str, global_mapping: dict, entries: list[dict], *,
                  voice_input_source: str = config.VOICE_INPUT_SOURCE_REMOTE,
                  voice_local_input_device: str = "",
                  voice_keys: "list[dict] | None" = None) -> dict:
    """Build the "导出全部" document: the live global row plus every saved profile."""
    if profile not in PROFILES:
        raise ValueError("此配置的遥控器型号不受支持。")
    source, device = validate_voice(voice_input_source, voice_local_input_device)
    if len(entries) > MAX_PROFILES:
        raise ValueError("配置数量超出上限。")
    profiles = []
    for entry in entries:
        scope = _validate_scope(entry["scope"])
        entry_source, entry_device = validate_voice(
            entry.get("voice_input_source", source),
            entry.get("voice_local_input_device", device))
        profiles.append({
            "id": str(entry["id"]),
            "name": _validate_name(entry["name"]),
            "scope": scope,
            "process": _validate_process(entry.get("process", ""), scope),
            "hotkey": _validate_hotkey(entry.get("hotkey", "")),
            "mapping": validate_mapping(
                entry["mapping"], profile, partial=scope == "app"),
            "voice_input_source": entry_source,
            "voice_local_input_device": entry_device,
            "voice_keys": validate_voice_keys(entry.get(VOICE_KEYS_FIELD, [])),
        })
    return {
        "format": LIBRARY_FORMAT,
        "schema_version": LIBRARY_SCHEMA_VERSION,
        "profile": profile,
        "global": {
            "mapping": validate_mapping(global_mapping, profile),
            "voice_input_source": source,
            "voice_local_input_device": device,
            "voice_keys": validate_voice_keys(voice_keys or []),
        },
        "profiles": profiles,
    }


class AppProfileStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / FILENAME
        self.legacy_path = self.root / LEGACY_FILENAME

    def _empty_document(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "profiles": [], "applied": GLOBAL_ID}

    def _load_legacy(self) -> "dict | None":
        """Convert the legacy library once, leaving its file on disk untouched."""
        if not self.legacy_path.exists():
            return None
        legacy = mapping_presets.PresetStore(self.root)._load_document()
        profiles = [{"id": entry["id"], "name": entry["name"], "scope": entry["scope"],
                     "process": entry["process"], "hotkey": entry["hotkey"],
                     "mapping": entry["mapping"],
                     "voice_input_source": config.VOICE_INPUT_SOURCE_REMOTE,
                     "voice_local_input_device": "",
                     VOICE_KEYS_FIELD: []} for entry in legacy["presets"]]
        applied = legacy["applied_ids"].get(device_catalog.RC003_ID, GLOBAL_ID)
        return {"schema_version": SCHEMA_VERSION, "profiles": profiles, "applied": applied}

    def _load_document(self) -> dict:
        try:
            value = _read(self.path)
        except FileNotFoundError:
            migrated = self._load_legacy()
            if migrated is None:
                return self._empty_document()
            document = self._validate_document(migrated)
            self._save(document)
            return document
        document = self._validate_document(value)
        if isinstance(value, dict) and value.get("schema_version") != SCHEMA_VERSION:
            # Rewrite schema 4 in place so the next read is free of compatibility
            # branches; the voice fields it gains are the 模式1 defaults.
            self._save(document)
        return document

    def _validate_document(self, value: dict) -> dict:
        expected = {"schema_version", "profiles", "applied"}
        version = value.get("schema_version") if isinstance(value, dict) else None
        if (not isinstance(value, dict) or set(value) != expected
                or version not in (SCHEMA_VERSION, *PREVIOUS_SCHEMA_VERSIONS)
                or not isinstance(value["profiles"], list)
                or len(value["profiles"]) > MAX_PROFILES):
            raise ValueError("保存的应用配置库损坏或版本不受支持；原文件已保留。")
        entry_fields = {SCHEMA_VERSION: set(ENTRY_FIELDS), 5: _V5_ENTRY_FIELDS,
                        4: _V4_ENTRY_FIELDS}[version]
        result, ids, names, processes, hotkeys = [], set(), set(), set(), set()
        for entry in value["profiles"]:
            if not isinstance(entry, dict) or set(entry) != entry_fields:
                raise ValueError("保存的配置格式无效；原文件已保留。")
            identity = str(UUID(entry["id"]))
            name = _validate_name(entry["name"])
            scope = _validate_scope(entry.get("scope", DEFAULT_SCOPE))
            process = _validate_process(entry.get("process", ""), scope)
            hotkey = _validate_hotkey(entry.get("hotkey", ""))
            if version == SCHEMA_VERSION:
                source, device = validate_voice(entry["voice_input_source"],
                                                entry["voice_local_input_device"])
                voice_keys = validate_voice_keys(entry[VOICE_KEYS_FIELD])
            elif version == 5:
                source, device = validate_voice(entry["voice_input_source"],
                                                entry["voice_local_input_device"])
                voice_keys = []
            else:
                source, device = config.VOICE_INPUT_SOURCE_REMOTE, ""
                voice_keys = []
            name_key = name.casefold()
            if identity in ids or name_key in names:
                raise ValueError("保存的配置含重复名称或标识；原文件已保留。")
            ids.add(identity)
            names.add(name_key)
            if scope == "app":
                if process.casefold() in processes:
                    raise ValueError("保存的配置含重复绑定的进程；原文件已保留。")
                processes.add(process.casefold())
            if hotkey:
                if hotkey.casefold() in hotkeys:
                    raise ValueError("保存的配置含重复的快捷键；原文件已保留。")
                hotkeys.add(hotkey.casefold())
            result.append({"id": identity, "name": name, "scope": scope, "process": process,
                           "hotkey": hotkey,
                           "mapping": validate_mapping(entry["mapping"], partial=scope == "app"),
                           "voice_input_source": source, "voice_local_input_device": device,
                           VOICE_KEYS_FIELD: voice_keys})
        applied = value["applied"]
        if not isinstance(applied, str) or (applied != GLOBAL_ID and applied not in ids):
            raise ValueError("配置库的应用标记无效；原文件已保留。")
        return {"schema_version": SCHEMA_VERSION, "profiles": result, "applied": applied}

    def load(self) -> list[dict]:
        return copy.deepcopy(self._load_document()["profiles"])

    def get(self, identity: str) -> dict:
        return copy.deepcopy(self._find(self._load_document()["profiles"], identity))

    def view(self, bindings: dict, profile: str = device_catalog.RC003_ID, *,
             voice_input_source: str = config.VOICE_INPUT_SOURCE_REMOTE,
             voice_local_input_device: str = "",
             voice_keys: "list[dict] | None" = None) -> list[dict]:
        """Read list metadata even when the active mapping needs correction.

        The global row never owns a second saved mapping: it is the user's live
        ``key_bindings.json`` rendered as a row. An unreadable snapshot is
        display-only; copying/exporting must validate it separately. The live
        voice mode and voice-key list are passed in because they live in
        ``config.json``, not here.
        """
        if profile not in PROFILES:
            raise ValueError("此配置的遥控器型号不受支持。")
        live_source, live_device = validate_voice(voice_input_source, voice_local_input_device)
        live_keys = validate_voice_keys(voice_keys or [])
        document = self._load_document()
        try:
            current = snapshot(bindings, profile)
        except (KeyError, TypeError, ValueError):
            current = None
        entries = document["profiles"]
        active = document["applied"]
        if current is None:
            active = GLOBAL_ID
        elif active != GLOBAL_ID:
            matched = next((entry for entry in entries if entry["id"] == active), None)
            # A saved global layout is a copy of key_bindings.json, so it stops
            # matching as soon as the user edits a key. An app profile is an
            # overlay: its sparse mapping never equals the full global layout,
            # and the marker alone says which app is currently in effect.
            if matched is None or (matched["scope"] == "global"
                                   and _comparable_mapping(matched["mapping"], profile)
                                   != _comparable_mapping(current, profile)):
                active = GLOBAL_ID
        rows = [{"id": GLOBAL_ID, "name": GLOBAL_NAME, "scope": DEFAULT_SCOPE, "process": "",
                 "hotkey": "", "mapping": current,
                 "voice_input_source": live_source,
                 "voice_local_input_device": live_device,
                 VOICE_KEYS_FIELD: live_keys}, *entries]
        result = []
        for entry in rows:
            conflicts: list[str] = []
            if entry["voice_input_source"] == config.VOICE_INPUT_SOURCE_REMOTE:
                effective = entry["mapping"]
                if entry["scope"] == "app" and isinstance(effective, dict):
                    try:
                        effective = resolve_mapping(bindings, effective, profile)
                    except (KeyError, TypeError, ValueError):
                        effective = entry["mapping"]
                if isinstance(effective, dict):
                    try:
                        conflicts = voice_conflicts(
                            effective, entry["voice_input_source"], profile,
                            plugin_slots=plugin_slot_ids(
                                entry.get(VOICE_KEYS_FIELD) or live_keys))
                    except ValueError:
                        conflicts = []
            result.append({**entry,
                           "automatic": entry["id"] == GLOBAL_ID,
                           "applied": entry["id"] == active,
                           "mode_tag": mode_tag(entry),
                           "label": display_label(entry),
                           "voice_conflicts": conflicts,
                           "voice_conflict_text": voice_conflict_message(conflicts)})
        return result

    def resolve_for_process(self, process_basename: str) -> str | None:
        """Return the app profile bound to a running process, if any."""
        wanted = (process_basename or "").strip().casefold()
        if not wanted:
            return None
        for entry in self._load_document()["profiles"]:
            if entry["scope"] == "app" and entry["process"].casefold() == wanted:
                return entry["id"]
        return None

    def resolve_hotkey(self, hotkey: str) -> str | None:
        """Return the profile bound to a configured switch hotkey, if any."""
        wanted = (hotkey or "").strip().casefold()
        if not wanted:
            return None
        for entry in self._load_document()["profiles"]:
            if entry["hotkey"] and entry["hotkey"].casefold() == wanted:
                return entry["id"]
        return None

    def applied_id(self) -> str:
        """Return the identifier recorded as currently in effect."""
        return self._load_document()["applied"]

    def mark_applied(self, identity: str) -> None:
        document = self._load_document()
        if identity != GLOBAL_ID:
            self._find(document["profiles"], identity)
        document["applied"] = identity
        self._save(document)

    def _save(self, document: dict) -> None:
        if len(document["profiles"]) > MAX_PROFILES:
            raise ValueError("最多保存 100 套配置，请先导出或删除不用的配置。")
        if len((json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")) > MAX_BYTES:
            raise ValueError("保存的配置总量超过 2 MB，请先导出或删除不用的配置。")
        config._save_json_atomic(self.path, document)

    @staticmethod
    def _find(entries: list[dict], identity: str) -> dict:
        for entry in entries:
            if entry["id"] == identity:
                return entry
        raise ValueError("所选配置已不存在，请重新选择。")

    def create(self, name: str, mapping: dict, *, unique: bool = False,
               scope: str = DEFAULT_SCOPE, process: str = "",
               hotkey: str = "",
               voice_input_source: str = config.VOICE_INPUT_SOURCE_REMOTE,
               voice_local_input_device: str = "",
               voice_keys: "list[dict] | None" = None) -> str:
        document = self._load_document()
        entries = document["profiles"]
        name = _validate_name(name)
        names = {entry["name"].casefold() for entry in entries}
        names.add(GLOBAL_NAME.casefold())
        if unique:
            base, index = name, 2
            while name.casefold() in names:
                suffix = f" ({index})"
                name = base[:64 - len(suffix)] + suffix
                index += 1
        elif name.casefold() in names:
            raise ValueError("已有同名配置，请换一个名称。")
        scope = _validate_scope(scope)
        process = _validate_process(process, scope)
        hotkey = _validate_hotkey(hotkey)
        source, device = validate_voice(voice_input_source, voice_local_input_device)
        self._check_bindings_free(entries, "", process, hotkey)
        identity = str(uuid4())
        entries.append({"id": identity, "name": name, "scope": scope, "process": process,
                        "hotkey": hotkey,
                        "mapping": validate_mapping(mapping, partial=scope == "app"),
                        "voice_input_source": source, "voice_local_input_device": device,
                        VOICE_KEYS_FIELD: validate_voice_keys(voice_keys or [])})
        self._save(document)
        return identity

    def copy_profile(self, identity: str, name: str = "", *,
                     bindings: "dict | None" = None,
                     profile: str = device_catalog.RC003_ID) -> str:
        """Duplicate one profile, voice mode included, under a fresh name.

        An application profile stores only its overrides, so copying it needs
        the live global layout to produce a complete, standalone copy; without
        that the copy would silently drop every button it does not mention.
        """
        document = self._load_document()
        entry = self._find(document["profiles"], identity)
        mapping = copy.deepcopy(entry["mapping"])
        if entry["scope"] == "app":
            if bindings is None:
                raise ValueError("复制软件配置需要当前的全局按键配置，请先在按键页确认它可用。")
            mapping = snapshot(apply_to(bindings, entry, profile), profile)
        label = _validate_name(name) if str(name or "").strip() else (
            f"{entry['name']} 副本")[:64]
        return self.create(label, mapping, unique=True, scope="global",
                           voice_input_source=entry["voice_input_source"],
                           voice_local_input_device=entry["voice_local_input_device"],
                           voice_keys=entry.get(VOICE_KEYS_FIELD))

    def update_voice(self, identity: str, source: str, device: str = "") -> None:
        """Replace one saved profile's voice mode without touching its mapping."""
        document = self._load_document()
        entry = self._find(document["profiles"], identity)
        entry["voice_input_source"], entry["voice_local_input_device"] = validate_voice(
            source, device)
        self._save(document)

    def update_content(self, identity: str, mapping: dict, *,
                       voice_input_source: str = config.VOICE_INPUT_SOURCE_REMOTE,
                       voice_local_input_device: str = "",
                       voice_keys: "list[dict] | None" = None) -> None:
        """Overwrite one saved profile with the current mapping and voice mode."""
        document = self._load_document()
        entry = self._find(document["profiles"], identity)
        entry["mapping"] = validate_mapping(mapping, partial=entry["scope"] == "app")
        entry["voice_input_source"], entry["voice_local_input_device"] = validate_voice(
            voice_input_source, voice_local_input_device)
        entry[VOICE_KEYS_FIELD] = validate_voice_keys(voice_keys or [])
        self._save(document)

    @staticmethod
    def _check_bindings_free(entries: list[dict], identity: str, process: str, hotkey: str) -> None:
        process_key = process.casefold()
        hotkey_key = hotkey.casefold()
        for other in entries:
            if other["id"] == identity:
                continue
            if process_key and other["scope"] == "app" and other["process"].casefold() == process_key:
                raise ValueError("已有配置绑定该进程，请先修改或删除它。")
            if hotkey_key and other["hotkey"].casefold() == hotkey_key:
                raise ValueError("已有配置使用该快捷键，请换一个快捷键。")

    def set_status(self, identity: str, scope: str, process: str, hotkey: str) -> None:
        """Update a saved profile's switching state without touching its mapping."""
        document = self._load_document()
        entries = document["profiles"]
        entry = self._find(entries, identity)
        scope = _validate_scope(scope)
        process = _validate_process(process, scope)
        hotkey = _validate_hotkey(hotkey)
        self._check_bindings_free(entries, identity, process, hotkey)
        entry["scope"], entry["process"], entry["hotkey"] = scope, process, hotkey
        self._save(document)

    def rename(self, identity: str, name: str) -> None:
        document = self._load_document()
        entries = document["profiles"]
        entry = self._find(entries, identity)
        name = _validate_name(name)
        if name.casefold() == GLOBAL_NAME.casefold() or any(
               other["id"] != identity and other["name"].casefold() == name.casefold() for other in entries):
            raise ValueError("已有同名配置，请换一个名称。")
        entry["name"] = name
        self._save(document)

    def delete(self, identity: str) -> None:
        document = self._load_document()
        entries = document["profiles"]
        self._find(entries, identity)
        document["profiles"] = [entry for entry in entries if entry["id"] != identity]
        if document["applied"] == identity:
            document["applied"] = GLOBAL_ID
        self._save(document)

    def update_mapping(self, identity: str, mapping: dict) -> None:
        """Replace a saved profile's mapping without touching its switching state."""
        document = self._load_document()
        entry = self._find(document["profiles"], identity)
        entry["mapping"] = validate_mapping(mapping, partial=entry["scope"] == "app")
        self._save(document)

    def list_apps(self) -> list[dict]:
        return [entry for entry in self.load() if entry["scope"] == "app"]

    def find_app(self, process: str) -> dict | None:
        """Return the app profile bound to a process name, if any."""
        wanted = (process or "").strip().casefold()
        if not wanted:
            return None
        for entry in self.load():
            if entry["scope"] == "app" and entry["process"].casefold() == wanted:
                return entry
        return None

    def ensure_app(self, name: str, process: str, mapping: dict) -> str:
        """Create (or reuse) the app profile bound to one process name."""
        existing = self.find_app(process)
        if existing is not None:
            return existing["id"]
        return self.create(name, mapping, unique=True, scope="app", process=process)

    def replace_apps(self, apps: list[dict], *,
                     voice_input_source: str = config.VOICE_INPUT_SOURCE_REMOTE,
                     voice_local_input_device: str = "") -> int:
        """Swap every app profile in a single write, keeping global ones.

        Imported entries get a fresh identifier and a de-duplicated name; a
        hotkey that is already taken is dropped instead of failing the import.
        A bundle entry that predates the voice fields inherits the bundle's.
        """
        document = self._load_document()
        default_source, default_device = validate_voice(
            voice_input_source, voice_local_input_device)
        replaced = {entry["id"] for entry in document["profiles"] if entry["scope"] == "app"}
        kept = [entry for entry in document["profiles"] if entry["id"] not in replaced]
        if len(kept) + len(apps) > MAX_PROFILES:
            raise ValueError("最多保存 100 套配置，请先导出或删除不用的配置。")
        names = {entry["name"].casefold() for entry in kept}
        names.add(GLOBAL_NAME.casefold())
        hotkeys = {entry["hotkey"].casefold() for entry in kept if entry["hotkey"]}
        processes = {entry["process"].casefold() for entry in kept if entry["scope"] == "app"}
        created = []
        for app in apps:
            name = _validate_name(app["name"])
            process = _validate_process(app["process"], "app")
            if process.casefold() in processes:
                raise ValueError(f"已有配置绑定进程 {process}，请先修改或删除它。")
            processes.add(process.casefold())
            base, index = name, 2
            while name.casefold() in names:
                suffix = f" ({index})"
                name = base[:64 - len(suffix)] + suffix
                index += 1
            names.add(name.casefold())
            hotkey = _validate_hotkey(app["hotkey"])
            if hotkey and hotkey.casefold() in hotkeys:
                hotkey = ""
            if hotkey:
                hotkeys.add(hotkey.casefold())
            source, device = validate_voice(
                app.get("voice_input_source", default_source),
                app.get("voice_local_input_device", default_device))
            created.append({"id": str(uuid4()), "name": name, "scope": "app", "process": process,
                            "hotkey": hotkey,
                            "mapping": validate_mapping(app["mapping"], partial=True),
                            "voice_input_source": source,
                            "voice_local_input_device": device,
                            VOICE_KEYS_FIELD: validate_voice_keys(
                                app.get(VOICE_KEYS_FIELD) or [])})
        document["profiles"] = [*kept, *created]
        if document["applied"] in replaced:
            document["applied"] = GLOBAL_ID
        self._save(document)
        return len(created)

    def import_file(self, path: Path, profile: str = device_catalog.RC003_ID, *,
                    scope: str = DEFAULT_SCOPE, process: str = "", hotkey: str = "") -> str:
        document = validate_export(_read(Path(path)))
        if document["profile"] != profile:
            raise ValueError("配置的遥控器型号与当前型号不同，请先切换设备。")
        return self.create(document["name"], document["mapping"], unique=True,
                           scope=scope, process=process, hotkey=hotkey,
                           voice_input_source=document["voice_input_source"],
                           voice_local_input_device=document["voice_local_input_device"],
                           voice_keys=document[VOICE_KEYS_FIELD])

    def export_file(self, identity: str, path: Path,
                    profile: str = device_catalog.RC003_ID) -> None:
        self.export_entry(self.get(identity), path, profile)

    def export_entry(self, entry: dict, path: Path,
                     profile: str = device_catalog.RC003_ID) -> None:
        destination = Path(path).resolve()
        if destination.is_relative_to(self.root.resolve()):
            raise ValueError("请导出到桌面等位置，不能覆盖软件的数据目录。")
        document = validate_export({
            "format": FORMAT, "schema_version": EXPORT_SCHEMA_VERSION,
            **{key: entry[key] for key in ("name", "mapping")},
            "profile": profile,
            "voice_input_source": entry.get("voice_input_source",
                                            config.VOICE_INPUT_SOURCE_REMOTE),
            "voice_local_input_device": entry.get("voice_local_input_device", ""),
            VOICE_KEYS_FIELD: entry.get(VOICE_KEYS_FIELD, []),
        })
        config._save_json_atomic(destination, document)

    def export_bundle(self, profile: str, global_mapping: dict,
                      app_entries: list[dict], path: Path,
                      voice_input_source: str = config.VOICE_INPUT_SOURCE_REMOTE,
                      voice_local_input_device: str = "") -> None:
        """Write the global layout and every application layout to one file."""
        destination = Path(path).resolve()
        if destination.is_relative_to(self.root.resolve()):
            raise ValueError("请导出到桌面等位置，不能覆盖软件的数据目录。")
        config._save_json_atomic(
            destination, build_bundle(profile, global_mapping, app_entries,
                                      voice_input_source, voice_local_input_device))

    def export_library(self, path: Path, profile: str, global_mapping: dict,
                       voice_input_source: str = config.VOICE_INPUT_SOURCE_REMOTE,
                       voice_local_input_device: str = "",
                       voice_keys: "list[dict] | None" = None) -> int:
        """Back up the global row and every global profile; returns the count.

        Software-level profiles are excluded: 「配置档案」 only manages the
        whole-machine setup, while per-application layouts are imported and
        exported from the buttons page.
        """
        destination = Path(path).resolve()
        if destination.is_relative_to(self.root.resolve()):
            raise ValueError("请导出到桌面等位置，不能覆盖软件的数据目录。")
        entries = [entry for entry in self._load_document()["profiles"]
                   if entry["scope"] != "app"]
        document = build_library(
            profile, global_mapping, entries,
            voice_input_source=voice_input_source,
            voice_local_input_device=voice_local_input_device,
            voice_keys=voice_keys)
        config._save_json_atomic(destination, document)
        return len(document["profiles"])


__all__ = [
    "AppProfileStore", "FILENAME", "LEGACY_FILENAME", "FORMAT", "BUNDLE_FORMAT",
    "LIBRARY_FORMAT", "LIBRARY_SCHEMA_VERSION",
    "BUNDLE_SCHEMA_VERSION", "MAX_BYTES", "MAX_PROFILES", "MAPPING_FIELDS", "PROFILES",
    "SCOPES", "SCOPE_LABELS", "DEFAULT_SCOPE", "GLOBAL_ID", "GLOBAL_NAME", "ENTRY_FIELDS",
    "VOICE_FIELDS", "VOICE_KEY_FIELDS", "VOICE_KEYS_FIELD", "SCHEMA_VERSION",
    "PREVIOUS_SCHEMA_VERSIONS", "LEGACY_SCHEMA_VERSIONS",
    "EXPORT_SCHEMA_VERSION", "EXPORT_FIELDS", "VOICE_MODE_LABELS", "VOICE_DEVICE_MAX_LENGTH",
    "validate_mapping", "snapshot", "resolve_mapping", "apply_to", "validate_voice",
    "validate_voice_keys", "plugin_slot_ids",
    "voice_mode_label", "mode_tag", "display_label", "voice_conflicts",
    "voice_conflict_message", "resolve_voice", "build_bundle", "validate_bundle",
    "read_bundle", "read_export", "validate_export", "build_library",
]
