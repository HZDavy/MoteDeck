"""Settings reload, mapping presets and profile persistence methods of RC003App, mechanically split from app.py's RC003App class body (verbatim bodies, zero behavior change); RC003App inherits this mixin."""

from __future__ import annotations

from pathlib import Path
from . import app_profiles
import asyncio
from . import config
from . import hotkey
import os
from . import preset_switch_windows
from . import voice_controller
from . import voice_interaction_diagnostics_windows
from . import win32_keys
from .app_constants import (
    _PRESET_SWITCH_POLL_SECONDS,
    _runtime_voice_hotkey_text,
)


class SettingsPresetsMixin:
    """Settings reload, mapping presets and profile persistence methods of RC003App."""


    @staticmethod
    def _settings_file_mtime_ns(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return -1

    def _voice_settings_idle_locked(self) -> bool:
        return not (
            self._voice_shortcut.controller.active
            or self._voice_mic_gesture_active
            or self._voice_audio_stream_active
            or self._ordinary_mic_gesture_active
            or self._voice_shortcut.pending_tokens is not None
        )

    def _latched_voice_stream_only_locked(self) -> bool:
        """True when the only live voice state is a latched, unowned device stream.

        A short-tap latch keeps the device microphone streaming after the host
        shortcut has been released, so ``_voice_settings_idle_locked`` reports
        busy even though no shortcut is held. A deliberate host key press may
        still repurpose the mapping in that state; an incoming device stream may
        not, which is why the takeover is opt-in per call site.

        A held physical source always implies an owned shortcut
        (``controller.active``) or a live mic gesture, so those two checks
        already cover it; the caller's own just-pressed extra key is therefore
        not treated as an owner here.
        """

        return (
            self._voice_audio_stream_active
            and not self._voice_shortcut.controller.active
            and not self._voice_mic_gesture_active
            and not self._ordinary_mic_gesture_active
            and self._voice_shortcut.pending_tokens is None
        )

    def _ordinary_button_mappings_idle(self) -> bool:
        return not (
            self._button_gestures.has_active_gestures()
            or self._button_combos.has_active_combo()
        )

    def _apply_voice_settings_locked(
        self,
        voice_hotkey: hotkey.HotkeySpec,
        *,
        update_config: bool = True,
    ) -> None:
        self._voice_shortcut.controller = voice_controller.VoiceController()
        self._voice_shortcut.hotkey = voice_hotkey
        if update_config:
            self._config["voice_hotkey"] = voice_hotkey.serialize()
        self._logger.info(
            "settings voice configuration applied: hotkey=%s",
            voice_hotkey.serialize(),
        )

    def _clear_voice_hotkey_locked(self) -> None:
        """Drop the host shortcut for a voice key that has none recorded."""

        self._voice_shortcut.controller = voice_controller.VoiceController()
        self._voice_shortcut.hotkey = None
        self._logger.info("settings voice configuration applied: hotkey=")

    def _apply_pending_voice_settings_if_idle_locked(self) -> None:
        if not self._voice_settings_idle_locked():
            return
        if self._pending_config is not None:
            self._config = self._pending_config
            self._pending_config = None
        if self._pending_voice_settings is not None:
            voice_hotkey = self._pending_voice_settings
            self._pending_voice_settings = None
            self._apply_voice_settings_locked(voice_hotkey)
        if (
            self._pending_bindings is not None
        ):
            with self._button_mapping_lock:
                if self._ordinary_button_mappings_idle():
                    self._button_combos.reset()
                    self._bindings = self._pending_bindings
                    self._pending_bindings = None
                    self._preset_override_id = None
                    self._preset_base_bindings = None
                    self._sync_physical_bindings_to_listener()
                    self._removed_voice_bindings = dict(
                        self._bindings.get(
                            config.RUNTIME_REMOVED_VOICE_BINDINGS_KEY, {}
                        )
                    )
                    self._logger.info(
                        "deferred settings mappings applied after active input became idle"
                    )
                    self._diagnostic_trace.emit("voice_configuration_applied", result="recovered")
                    self._publish_voice_mic_blocked_from_mapping()

    def _apply_pending_settings_if_idle(self) -> None:
        with self._voice_shortcut.lock:
            self._apply_pending_voice_settings_if_idle_locked()

    def _reload_settings_if_changed(self) -> None:
        """Apply mapping and voice-setting edits without a bridge restart."""

        current_config_mtime_ns = self._settings_file_mtime_ns(self._config_path)
        current_bindings_mtime_ns = self._settings_file_mtime_ns(self._bindings_path)
        if (
            current_config_mtime_ns == self._config_mtime_ns
            and current_bindings_mtime_ns == self._bindings_mtime_ns
        ):
            return
        try:
            refreshed_config = config.load_config(self._config_path)
            from . import remote_selection
            if remote_selection.active_key(refreshed_config) != remote_selection.active_key(self._config):
                raise remote_selection.SelectionError("设备选择已变化，须先停止旧服务再切换。")
            refreshed_bindings = config.load_key_bindings(self._bindings_path)
            removed_voice_bindings = config.normalize_voice_product_boundary(
                refreshed_config,
                refreshed_bindings,
            )
            refreshed_hotkey_text = _runtime_voice_hotkey_text(refreshed_config)
            # An unrecorded voice shortcut is a valid state: it must not abort
            # the whole reload, which would also drop refreshed button mappings.
            voice_hotkey = (
                hotkey.HotkeySpec.parse(refreshed_hotkey_text)
                if refreshed_hotkey_text
                else None
            )
            refreshed_hotkey_serialized = (
                voice_hotkey.serialize() if voice_hotkey is not None else ""
            )
        except Exception as exc:  # noqa: BLE001 - keep the last valid settings
            self._logger.warning("settings reload skipped: %s", exc)
            with self._voice_shortcut.lock:
                self._config_mtime_ns = current_config_mtime_ns
                self._bindings_mtime_ns = current_bindings_mtime_ns
            return

        self._diagnostic_trace.set_enabled(
            bool(refreshed_config.get("diagnostic_trace_enabled", False))
        )
        self._record_device_diagnostic_context()
        with self._voice_shortcut.lock:
            with self._button_mapping_lock:
                refreshed_settings = refreshed_hotkey_serialized
                current_settings = (
                    self._voice_shortcut.hotkey.serialize()
                    if self._voice_shortcut.hotkey is not None
                    else ""
                )
                if self._voice_settings_idle_locked():
                    self._config = refreshed_config
                    self._pending_config = None
                    self._pending_voice_settings = None
                    if (
                        voice_hotkey is not None
                        and refreshed_settings != current_settings
                    ):
                        self._apply_voice_settings_locked(voice_hotkey)
                    if self._ordinary_button_mappings_idle():
                        self._button_combos.reset()
                        self._bindings = refreshed_bindings
                        self._preset_override_id = None
                        self._preset_base_bindings = None
                        self._sync_physical_bindings_to_listener()
                        self._removed_voice_bindings = removed_voice_bindings
                        self._pending_bindings = None
                        self._publish_voice_mic_blocked_from_mapping()
                    else:
                        self._pending_bindings = refreshed_bindings
                        self._logger.info(
                            "settings mapping reload deferred until active input is idle"
                        )
                else:
                    self._pending_config = refreshed_config
                    self._pending_bindings = refreshed_bindings
                    self._pending_voice_settings = (
                        voice_hotkey if voice_hotkey is not None else None
                    )
                    self._logger.info(
                        "settings reload deferred until active voice session is idle"
                    )
                    self._diagnostic_trace.emit("voice_configuration_deferred",
                        release_pending=self._voice_shortcut.pending_tokens is not None,
                        reason="active_voice_not_idle", **self._diagnostic_trace.current_context())
                self._config_mtime_ns = current_config_mtime_ns
                self._bindings_mtime_ns = current_bindings_mtime_ns
        if removed_voice_bindings:
            self._logger.warning(
                "legacy voice mappings disabled until user reselects actions: %s",
                sorted(removed_voice_bindings),
            )
        if self._pending_bindings is None:
            self._logger.info("settings reloaded from disk")

    def request_settings_reload_now(self) -> None:
        """Apply saved settings promptly from the bridge event-loop thread."""

        self._event_loop.call_soon_threadsafe(self._reload_settings_if_changed)

    def _start_preset_switching(self) -> None:
        if self._preset_switch_task is None or self._preset_switch_task.done():
            self._preset_switch_task = self._event_loop.create_task(
                self._preset_switch_loop()
            )

    async def _stop_preset_switching(self) -> None:
        task = self._preset_switch_task
        self._preset_switch_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._stop_preset_hotkey_listener()
        with self._button_mapping_lock:
            self._reset_preset_override_locked()

    async def _preset_switch_loop(self) -> None:
        while True:
            try:
                await self._poll_preset_switch()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad poll must not end the loop
                self._logger.exception("preset switch poll failed")
            await asyncio.sleep(_PRESET_SWITCH_POLL_SECONDS)

    async def _poll_preset_switch(self) -> None:
        if not self._accept_input_events:
            return
        self._refresh_preset_hotkey_targets_if_changed()
        self._refresh_preset_override_if_changed()
        process_targets = await asyncio.to_thread(self._preset_app_targets)
        foreground = await asyncio.to_thread(self._preset_foreground_basename)
        if foreground != self._preset_switch_foreground:
            self._preset_switch_foreground = foreground
            # A manual switch holds only while the foreground process is
            # unchanged; moving to another window resumes process-scope again.
            self._preset_manual_foreground = None
        if self._preset_manual_foreground is not None:
            return
        target = process_targets.get(foreground) if foreground else None
        if target != self._preset_override_id:
            if target is None:
                with self._button_mapping_lock:
                    self._reset_preset_override_locked()
            else:
                self._apply_preset_override(target)
        # The settings window can only learn about a switch through the shared
        # profile library, so the marker always follows the overlay actually in
        # memory - including a swap that a busy gesture postponed.
        self._remember_applied_profile(
            self._preset_override_id or app_profiles.GLOBAL_ID
        )

    def _preset_app_targets(self) -> dict:
        """Map each app profile's process basename to its identity."""

        try:
            entries = self._preset_store.list_apps()
        except Exception:  # noqa: BLE001 - unreadable profiles never break input
            return {}
        result: dict = {}
        for entry in entries:
            process = entry["process"].strip().casefold()
            if process:
                result.setdefault(process, entry["id"])
        return result

    def _preset_foreground_basename(self) -> str:
        try:
            snapshot = voice_interaction_diagnostics_windows.capture_focus_snapshot(
                include_text_length=False
            )
        except Exception:  # noqa: BLE001 - best-effort foreground observation
            return ""
        name = getattr(snapshot, "foreground_executable", "") or ""
        return os.path.basename(name).strip().casefold()

    def _apply_preset_override(self, identity: str) -> bool:
        """Swap the live mapping for a process-scoped preset (in memory only)."""

        try:
            entry = self._preset_store.get(identity)
        except Exception:  # noqa: BLE001 - the preset vanished between polls
            return False
        with self._voice_shortcut.lock:
            with self._button_mapping_lock:
                if not self._accept_input_events:
                    return False
                if not self._voice_settings_idle_locked():
                    return False  # retried on the next poll
                base = (
                    self._preset_base_bindings
                    if self._preset_base_bindings is not None
                    else self._bindings
                )
                try:
                    replacement = app_profiles.apply_to(
                        base, entry, self._remote_profile
                    )
                except Exception:  # noqa: BLE001 - a bad snapshot is display-only
                    self._logger.warning(
                        "preset process override skipped: invalid snapshot"
                    )
                    return False
                self._button_combos.reset()
                self._bindings = replacement
                # Overlays stack, so the untouched mapping is kept: switching
                # straight from one app to another must resolve from the global
                # layout instead of from the previous app's overlay.
                self._preset_base_bindings = base
                self._preset_override_id = identity
                self._sync_physical_bindings_to_listener()
                self._removed_voice_bindings = dict(
                    replacement.get(config.RUNTIME_REMOVED_VOICE_BINDINGS_KEY, {})
                )
                self._publish_voice_mic_blocked_from_mapping()
                self._logger.info(
                    "preset process override applied; foreground=%s",
                    self._preset_switch_foreground,
                )
                return True

    def _refresh_preset_override_if_changed(self) -> None:
        """Re-resolve the live overlay after app_profiles.json was edited.

        Saving an app's mapping only rewrites that file, so the overlay in
        memory would keep the snapshot from the moment it was applied and a
        newly assigned key would stay dead until the scope switched away and
        back. A busy gesture keeps the edit pending for the next poll.
        """

        mtime_ns = self._settings_file_mtime_ns(self._preset_store.path)
        if mtime_ns == self._preset_content_mtime_ns:
            return
        identity = self._preset_override_id
        if identity is None:
            # Nothing is overlaid right now; the next apply reads the fresh
            # file anyway.
            self._preset_content_mtime_ns = mtime_ns
            return
        if not self._apply_preset_override(identity):
            return
        self._preset_content_mtime_ns = mtime_ns
        self._logger.info("preset override refreshed; profile=%s", identity)

    def _reset_preset_override_locked(self) -> None:
        if self._preset_override_id is None and self._preset_base_bindings is None:
            return
        self._preset_override_id = None
        base = self._preset_base_bindings
        self._preset_base_bindings = None
        if base is None:
            try:
                base = config.load_key_bindings(self._bindings_path)
            except Exception:  # noqa: BLE001 - keep the last live mapping
                self._logger.warning(
                    "preset override reset could not reload key bindings"
                )
                return
        self._bindings = base
        self._removed_voice_bindings = dict(
            base.get(config.RUNTIME_REMOVED_VOICE_BINDINGS_KEY, {})
        )
        self._sync_physical_bindings_to_listener()
        self._publish_voice_mic_blocked_from_mapping()
        self._logger.info("preset override cleared; base key bindings restored")

    def _remember_applied_profile(self, identity: str) -> None:
        """Publish the profile in effect so the settings window can highlight it."""

        if identity == self._preset_applied_id:
            return
        try:
            self._preset_store.mark_applied(identity)
        except Exception:  # noqa: BLE001 - highlighting never breaks input
            self._logger.warning("applied profile marker was not saved")
            return
        self._preset_applied_id = identity
        # Writing the marker rewrites the same file, so the content fingerprint
        # has to follow it or the next poll would treat it as a mapping edit.
        self._preset_content_mtime_ns = self._settings_file_mtime_ns(
            self._preset_store.path
        )

    def _build_preset_hotkey_targets(self) -> dict:
        try:
            entries = self._preset_store.load()
        except Exception:  # noqa: BLE001 - unreadable presets disable the listener
            self._logger.warning("saved presets unreadable; switch hotkeys disabled")
            return {}
        targets: dict = {}
        for entry in entries:
            if not entry["hotkey"]:
                continue
            try:
                codes = win32_keys.resolve_vk_codes(entry["hotkey"].split("+"))
            except Exception:  # noqa: BLE001 - a stored hotkey is revalidated on save
                continue
            # The low-level hook reports handed modifier VKs; collapse both
            # sides onto the generic vocabulary so stored chords match.
            chord = frozenset(
                preset_switch_windows.normalize_vk(code) for code in codes
            )
            if chord:
                targets[chord] = entry["id"]
        return targets

    def _refresh_preset_hotkey_targets_if_changed(self, *, force: bool = False) -> None:
        mtime_ns = self._settings_file_mtime_ns(self._preset_store.path)
        if not force and mtime_ns == self._presets_mtime_ns:
            return
        self._presets_mtime_ns = mtime_ns
        targets = self._build_preset_hotkey_targets()
        if not targets:
            self._stop_preset_hotkey_listener()
            return
        listener = self._preset_hotkey_listener
        if listener is None:
            listener = preset_switch_windows.PresetSwitchListener(
                on_switch=self._on_preset_hotkey,
                logger=self._logger,
            )
            self._preset_hotkey_listener = listener
        try:
            listener.set_targets(targets)
        except Exception:  # noqa: BLE001 - keep the last working targets
            self._logger.exception("preset switch target update failed")
            return
        if not listener.is_running:
            try:
                listener.start()
            except preset_switch_windows.PresetSwitchUnavailableError as exc:
                self._logger.warning("preset switch hotkeys unavailable: %s", exc)
            except Exception:  # noqa: BLE001 - never fail input startup
                self._logger.exception("preset switch listener failed to start")

    def _stop_preset_hotkey_listener(self) -> None:
        listener = self._preset_hotkey_listener
        self._preset_hotkey_listener = None
        if listener is None:
            return
        try:
            listener.stop()
        except Exception:  # noqa: BLE001 - teardown must always be attempted
            self._logger.exception("preset switch listener did not stop cleanly")

    def _on_preset_hotkey(self, identity: str) -> None:
        """Hook-thread callback: marshal the switch onto the event loop."""

        loop = self._event_loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._activate_preset_by_hotkey, identity)

    def _activate_preset_by_hotkey(self, identity: str) -> None:
        if not self._accept_input_events:
            return
        with self._button_mapping_lock:
            try:
                entry = self._preset_store.get(identity)
                base = (
                    self._preset_base_bindings
                    if self._preset_base_bindings is not None
                    else self._bindings
                )
                replacement = app_profiles.apply_to(
                    base, entry, self._remote_profile
                )
            except Exception:  # noqa: BLE001 - a stale id is simply ignored
                self._logger.warning("preset hotkey switch skipped: invalid preset")
                return
        try:
            config.save_key_bindings(self._bindings_path, replacement)
        except Exception:  # noqa: BLE001 - do not half-apply a failed save
            self._logger.exception("preset hotkey switch could not be saved")
            return
        self._remember_applied_profile(identity)
        with self._voice_shortcut.lock:
            with self._button_mapping_lock:
                # A deliberate hotkey switch supersedes any process override and
                # is applied persistently, exactly like the settings window.
                self._preset_override_id = None
                self._preset_base_bindings = None
                if self._voice_settings_idle_locked():
                    self._pending_config = None
                    self._pending_voice_settings = None
                if self._ordinary_button_mappings_idle():
                    self._button_combos.reset()
                    self._bindings = replacement
                    self._pending_bindings = None
                    self._sync_physical_bindings_to_listener()
                    self._removed_voice_bindings = dict(
                        replacement.get(config.RUNTIME_REMOVED_VOICE_BINDINGS_KEY, {})
                    )
                    self._publish_voice_mic_blocked_from_mapping()
                else:
                    self._pending_bindings = replacement
                self._bindings_mtime_ns = self._settings_file_mtime_ns(
                    self._bindings_path
                )
        self._preset_manual_foreground = self._preset_switch_foreground
        self._logger.info("preset hotkey switch applied; waiting for any held key")
