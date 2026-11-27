"""Raw input channel startup/shutdown, BLE session wiring and input fallback methods of RC003App, mechanically split from app.py's RC003App class body (verbatim bodies, zero behavior change); RC003App inherits this mixin."""

from __future__ import annotations

from typing import List
from typing import Optional
from . import bridge_runtime_status
from . import button_gesture
from . import element_navigation_control_windows
from . import frida_compat
from . import hid_identity
import json
from . import key_mapping
import os
from . import raw_input_windows
from . import remote_selection
import threading
import time
import uuid
from . import voice_key_physicalizer_windows
from . import win32_input
from . import win32_keys
from typing import List, Optional
from .app_constants import (
    CleanupIncompleteError,
    _RAW_FALLBACK_KEY_TOKENS,
    _RAW_INPUT_RETRY_INITIAL_SECONDS,
    _RAW_INPUT_RETRY_MAX_SECONDS,
    _VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS,
    _VOICE_KEY_PHYSICALIZER_FAILED_RETRY_SECONDS,
    _VOICE_KEY_PHYSICALIZER_RETRY_SECONDS,
)


# A validated raw event while the HID tap owns mapping proves the Gadget did
# not clear that report's keyboard usages before Windows translated it. Give a
# fresh arming this long before such an event reopens the Raw Input fallback
# path (see InputChannelsMixin._raw_event_may_revert_direct_hid_locked).
_DIRECT_HID_RAW_REVERT_GRACE_SECONDS = 2.0

# After a revert proves the tap stopped clearing reports, restart the tap
# once per scheduled attempt. The delay debounces bursts of raw events, the
# cooldown keeps restarts apart, and the bounded window stops a persistently
# failing Gadget from cycling the attach forever.
_HID_TAP_REVERT_RESTART_DELAY_SECONDS = 1.0
_HID_TAP_REVERT_RESTART_COOLDOWN_SECONDS = 30.0
_HID_TAP_REVERT_RESTART_MAX_ATTEMPTS = 3
_HID_TAP_REVERT_RESTART_WINDOW_SECONDS = 600.0


class InputChannelsMixin:
    """Raw input channel startup/shutdown, BLE session wiring and input fallback methods of RC003App."""


    def _start_input_channels(self) -> None:
        """Start process-lifetime input resources before any BLE attempt."""

        from . import remote_selection
        if not self._selected_remote_key:
            raise remote_selection.SelectionError("请先在设备页选择要使用的遥控器。")

        with self._voice_shortcut.lock:
            self._accept_input_events = True
        with self._raw_input_lifecycle_lock:
            self._raw_input_stopping = False
        if self._remote_profile == remote_selection.CHROMECAST_PROFILE:
            # A real Chromecast remote brings its own receiver process (see
            # chromecast_runtime) and never uses the HID report tap or the
            # voice-key physicalizer. The Raw Input listener still starts, in
            # keyboard-safety-only mode: no RC003 device is required, it only
            # tracks ordinary keyboard ownership so held keys (e.g. Enter) can
            # be injected and released safely.
            self._start_hid_listener()
            return
        with self._voice_key_physicalizer_lifecycle_lock:
            self._voice_key_physicalizer_stopping = False
        self._start_voice_key_physicalizer()
        self._start_hid_listener()
        self._start_hid_report_tap()

    def _start_voice_key_physicalizer(self, *, recovering: bool = False) -> None:
        with self._voice_key_physicalizer_operation_lock:
            with self._voice_key_physicalizer_lifecycle_lock:
                if (
                    self._voice_key_physicalizer_stopping
                    or not self._accept_input_events
                ):
                    return
                current = self._voice_key_physicalizer
                if (
                    current is not None
                    and current.is_running
                    and bool(getattr(current, "accepts_new_down", True))
                ):
                    return
                if current is not None and current.is_running:
                    self._voice_key_physicalizer_ready = False
                    if not self._voice_key_physicalizer_degraded_retry_deadline:
                        self._voice_key_physicalizer_degraded_retry_deadline = (
                            time.monotonic()
                            + _VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS
                        )
                    self._schedule_voice_key_physicalizer_recovery_locked()
                    return
                # A queued callback belongs to the owner/generation captured
                # when it was scheduled. Retire that work before publishing a
                # replacement so a failed replacement start can own the sole
                # retry slot.
                self._cancel_voice_key_physicalizer_retry_locked()
                self._voice_key_physicalizer = None
                self._voice_key_physicalizer_generation += 1
                generation = self._voice_key_physicalizer_generation
                self._voice_key_physicalizer_lost_generation = -1
                physicalizer = (
                    voice_key_physicalizer_windows.VoiceKeyPhysicalizer()
                )
                self._voice_key_physicalizer = physicalizer
                self._voice_key_physicalizer_ready = False
                state = "recovering" if recovering else "starting"
            self._set_runtime_input_state(
                voice_key_physicalizer_state=state
            )
            set_tracking_lost_callback = getattr(
                physicalizer,
                "set_tracking_lost_callback",
                None,
            )
            if callable(set_tracking_lost_callback):
                set_tracking_lost_callback(
                    lambda physicalizer=physicalizer, generation=generation: (
                        self._on_voice_key_physicalizer_tracking_lost(
                            physicalizer,
                            generation,
                        )
                    )
                )
            set_health_failure_callback = getattr(
                physicalizer,
                "set_health_failure_callback",
                None,
            )
            if callable(set_health_failure_callback):
                set_health_failure_callback(
                    lambda reason, snapshot, physicalizer=physicalizer,
                    generation=generation: (
                        self._on_voice_key_physicalizer_health_failure(
                            physicalizer,
                            generation,
                            reason,
                            snapshot,
                        )
                    )
                )
            try:
                physicalizer.start()
            except (
                voice_key_physicalizer_windows.
                VoiceKeyPhysicalizerUnavailableError
            ) as exc:
                with self._voice_key_physicalizer_lifecycle_lock:
                    if (
                        self._voice_key_physicalizer is physicalizer
                        and self._voice_key_physicalizer_generation == generation
                    ):
                        self._voice_key_physicalizer_ready = False
                        if not physicalizer.is_running:
                            self._voice_key_physicalizer = None
                        self._schedule_voice_key_physicalizer_recovery_locked()
                self._set_runtime_input_state(
                    voice_key_physicalizer_state="failed"
                )
                if physicalizer.is_running:
                    self._logger.exception(
                        "startup: voice key physicalizer failed but is still running; "
                        "owner retained for cleanup"
                    )
                    raise
                self._logger.warning(
                    "startup: voice key physicalizer unavailable; recovery scheduled: %s",
                    exc,
                )
                return

            stop_stale_instance = False
            with self._voice_key_physicalizer_lifecycle_lock:
                current_generation = self._voice_key_physicalizer_generation
                stale = (
                    self._voice_key_physicalizer is not physicalizer
                    or current_generation != generation
                    or self._voice_key_physicalizer_stopping
                )
                lost = self._voice_key_physicalizer_lost_generation == generation
                if stale:
                    stop_stale_instance = physicalizer.is_running
                elif lost or not physicalizer.is_running:
                    self._voice_key_physicalizer_ready = False
                    self._schedule_voice_key_physicalizer_recovery_locked()
                else:
                    self._voice_key_physicalizer_ready = True
                    self._voice_key_physicalizer_degraded_retry_deadline = 0.0
            if stop_stale_instance:
                try:
                    physicalizer.stop()
                except Exception:
                    self._logger.exception(
                        "cleanup: stale voice key physicalizer did not stop"
                    )
                return
            if lost or not physicalizer.is_running:
                self._set_runtime_input_state(
                    voice_key_physicalizer_state="recovering"
                )
                return
            self._set_runtime_input_state(
                voice_key_physicalizer_state="ready"
            )
            self._logger.info("startup: marked voice key physicalizer enabled")

    def _cancel_voice_key_physicalizer_retry_locked(self) -> None:
        timer = self._voice_key_physicalizer_retry_timer
        self._voice_key_physicalizer_retry_timer = None
        self._voice_key_physicalizer_retry_token = None
        if timer is None:
            return
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except BaseException:
                pass

    def _schedule_voice_key_physicalizer_recovery_locked(
        self,
        *,
        delay: float = _VOICE_KEY_PHYSICALIZER_RETRY_SECONDS,
    ) -> None:
        if (
            self._voice_key_physicalizer_stopping
            or not self._accept_input_events
            or self._voice_key_physicalizer_retry_timer is not None
        ):
            return
        token = object()
        expected_physicalizer = self._voice_key_physicalizer
        expected_generation = self._voice_key_physicalizer_generation
        try:
            timer = self._voice_key_physicalizer_timer_factory(
                delay,
                lambda token=token,
                expected_physicalizer=expected_physicalizer,
                expected_generation=expected_generation: (
                    self._recover_voice_key_physicalizer(
                        token,
                        expected_physicalizer,
                        expected_generation,
                    )
                ),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._voice_key_physicalizer_retry_token = token
            self._voice_key_physicalizer_retry_timer = timer
            timer.start()
        except BaseException as exc:
            if self._voice_key_physicalizer_retry_token is token:
                self._voice_key_physicalizer_retry_token = None
                self._voice_key_physicalizer_retry_timer = None
            self._logger.error(
                "voice key physicalizer recovery timer failed: error_type=%s",
                type(exc).__name__,
            )

    def _recover_voice_key_physicalizer(
        self,
        token: object,
        expected_physicalizer: Optional[
            voice_key_physicalizer_windows.VoiceKeyPhysicalizer
        ],
        expected_generation: int,
    ) -> None:
        with self._voice_key_physicalizer_lifecycle_lock:
            if self._voice_key_physicalizer_retry_token is not token:
                return
            self._voice_key_physicalizer_retry_token = None
            self._voice_key_physicalizer_retry_timer = None
            if (
                self._voice_key_physicalizer_stopping
                or not self._accept_input_events
            ):
                return
            if (
                self._voice_key_physicalizer is not expected_physicalizer
                or self._voice_key_physicalizer_generation
                != expected_generation
            ):
                return
            physicalizer = self._voice_key_physicalizer
            generation = self._voice_key_physicalizer_generation
            running = bool(physicalizer is not None and physicalizer.is_running)
            accepts_new_down = bool(
                running and getattr(physicalizer, "accepts_new_down", True)
            )
            if running and accepts_new_down:
                if self._voice_key_physicalizer_lost_generation == generation:
                    self._schedule_voice_key_physicalizer_recovery_locked()
                    return
                self._voice_key_physicalizer_ready = True
                self._voice_key_physicalizer_degraded_retry_deadline = 0.0
                return
        if running:
            with self._voice_shortcut.lock:
                cleanup_blocks_recovery = bool(
                    self._voice_shortcut.pending_tokens is not None
                    or self._voice_shortcut.controller.active
                )
            if cleanup_blocks_recovery:
                with self._voice_key_physicalizer_lifecycle_lock:
                    if (
                        self._voice_key_physicalizer is not physicalizer
                        or self._voice_key_physicalizer_generation != generation
                        or self._voice_key_physicalizer_stopping
                        or not self._accept_input_events
                    ):
                        return
                    if (
                        self._voice_key_physicalizer_degraded_retry_deadline
                        and time.monotonic()
                        < self._voice_key_physicalizer_degraded_retry_deadline
                    ):
                        self._schedule_voice_key_physicalizer_recovery_locked()
                        return
                self._set_runtime_input_state(
                    voice_key_physicalizer_state="failed"
                )
                self._diagnostic_trace.emit(
                    "voice_key_physicalizer_health",
                    status="blocked",
                    reason="owned_cleanup_unresolved",
                    app_generation=int(generation),
                )
                self._logger.error(
                    "voice key physicalizer recovery blocked: owned voice "
                    "shortcut cleanup is still unresolved; retrying in %.1fs",
                    _VOICE_KEY_PHYSICALIZER_FAILED_RETRY_SECONDS,
                )
                self._schedule_voice_key_physicalizer_recovery_locked(
                    delay=_VOICE_KEY_PHYSICALIZER_FAILED_RETRY_SECONDS
                )
                return

            stop_succeeded = False
            with self._voice_key_physicalizer_operation_lock:
                with self._voice_key_physicalizer_lifecycle_lock:
                    if (
                        self._voice_key_physicalizer is not physicalizer
                        or self._voice_key_physicalizer_generation != generation
                        or self._voice_key_physicalizer_stopping
                        or not self._accept_input_events
                    ):
                        return
                try:
                    physicalizer.stop()
                except Exception:
                    self._logger.exception(
                        "voice key physicalizer recovery could not retire the "
                        "degraded owner; replacement not started"
                    )
                else:
                    stop_succeeded = True
                    with self._voice_key_physicalizer_lifecycle_lock:
                        if (
                            self._voice_key_physicalizer is physicalizer
                            and self._voice_key_physicalizer_generation == generation
                        ):
                            self._voice_key_physicalizer = None
                            self._voice_key_physicalizer_ready = False
            if not stop_succeeded:
                self._set_runtime_input_state(
                    voice_key_physicalizer_state="failed"
                )
                self._logger.error(
                    "voice key physicalizer recovery could not retire the "
                    "degraded owner; retrying in %.1fs",
                    _VOICE_KEY_PHYSICALIZER_FAILED_RETRY_SECONDS,
                )
                self._schedule_voice_key_physicalizer_recovery_locked(
                    delay=_VOICE_KEY_PHYSICALIZER_FAILED_RETRY_SECONDS
                )
                return
            self._set_runtime_input_state(
                voice_key_physicalizer_state="recovering"
            )
            self._start_voice_key_physicalizer(recovering=True)
            return

        with self._voice_key_physicalizer_lifecycle_lock:
            if (
                self._voice_key_physicalizer is not physicalizer
                or self._voice_key_physicalizer_generation != generation
                or self._voice_key_physicalizer_stopping
                or not self._accept_input_events
            ):
                return
            self._voice_key_physicalizer = None
        self._set_runtime_input_state(
            voice_key_physicalizer_state="recovering"
        )
        self._start_voice_key_physicalizer(recovering=True)

    def _resume_degraded_voice_key_physicalizer_recovery(self) -> None:
        """Resume one blocked recovery after authoritative cleanup succeeds."""

        with self._voice_key_physicalizer_lifecycle_lock:
            physicalizer = self._voice_key_physicalizer
            if (
                physicalizer is None
                or not physicalizer.is_running
                or bool(getattr(physicalizer, "accepts_new_down", True))
                or self._voice_key_physicalizer_stopping
                or not self._accept_input_events
            ):
                return
            self._voice_key_physicalizer_degraded_retry_deadline = (
                time.monotonic()
                + _VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS
            )
            # Cleanup is now authoritative: drop any slow fallback retry and
            # retry promptly instead of waiting out the failed-state backoff.
            self._cancel_voice_key_physicalizer_retry_locked()
            self._schedule_voice_key_physicalizer_recovery_locked()

    def _cancel_raw_input_retry_locked(self) -> None:
        timer = self._raw_input_retry_timer
        self._raw_input_retry_timer = None
        self._raw_input_retry_token = None
        if timer is None:
            return
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except BaseException:
                pass

    def _schedule_raw_input_recovery_locked(self) -> None:
        if (
            self._raw_input_stopping
            or not self._accept_input_events
            or self._raw_input_retry_timer is not None
        ):
            return
        token = object()
        delay = self._raw_input_retry_delay
        self._raw_input_retry_delay = min(
            _RAW_INPUT_RETRY_MAX_SECONDS,
            max(_RAW_INPUT_RETRY_INITIAL_SECONDS, delay * 2.0),
        )
        try:
            timer = self._raw_input_timer_factory(
                delay,
                lambda token=token: self._recover_raw_input_listener(token),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._raw_input_retry_token = token
            self._raw_input_retry_timer = timer
            timer.start()
        except BaseException as exc:  # noqa: BLE001 - input remains failed closed
            if self._raw_input_retry_token is token:
                self._raw_input_retry_token = None
                self._raw_input_retry_timer = None
            self._logger.error(
                "Raw Input recovery timer failed: error_type=%s",
                type(exc).__name__,
            )

    def _raw_input_callback_is_current(
        self,
        listener: Optional[raw_input_windows.RawInputButtonListener],
        generation: Optional[int],
    ) -> bool:
        if listener is None and generation is None:
            return True
        if listener is None or generation is None:
            return False
        with self._raw_input_lifecycle_lock:
            return (
                not self._raw_input_stopping
                and self._hid_listener is listener
                and self._raw_input_generation == int(generation)
            )

    def _cancel_raw_input_ownership_locked(self, *, reason: str) -> set[str]:
        """Cancel every Raw-owned edge while the input lock is held."""

        tracked_logical_buttons, tracked_physical_buttons = (
            self._raw_fallback_tracked_button_ids_locked()
        )
        raw_fallback_buttons = (
            set(self._raw_fallback_buttons_down) | tracked_logical_buttons
        )
        raw_fallback_physical_buttons = (
            self._take_raw_fallback_physical_buttons_locked(
                raw_fallback_buttons
            )
        )
        affected_buttons = raw_fallback_buttons | tracked_physical_buttons
        self._cancel_all_raw_fallback_hold_guards_locked()
        self._raw_fallback_buttons_down.clear()
        if affected_buttons:
            self._cancel_input_gestures(
                affected_buttons,
                reason=reason,
                block_until_release=True,
            )
            self._release_raw_fallback_keyups(
                raw_fallback_physical_buttons,
                reason=reason,
            )
        return affected_buttons

    def _recover_raw_input_listener(self, token: object) -> None:
        with self._raw_input_operation_lock:
            with self._input_arbitration_lock:
                with self._raw_input_lifecycle_lock:
                    if self._raw_input_retry_token is not token:
                        return
                    self._raw_input_retry_token = None
                    self._raw_input_retry_timer = None
                    if self._raw_input_stopping or not self._accept_input_events:
                        return
                    if self._remote_profile == remote_selection.CHROMECAST_PROFILE:
                        host = getattr(self._chromecast_runtime, "voice_host", None)
                        if host is not None and not host.closed:
                            # Preserve physical ownership until the voice owner
                            # has released its keys: a tracker restart clears
                            # every snapshot.
                            self._schedule_raw_input_recovery_locked()
                            return
                        if not self._release_pending_button_keys():
                            self._schedule_raw_input_recovery_locked()
                            return
                    listener = self._hid_listener
                    if listener is not None:
                        # Invalidate every callback before stop() can emit
                        # forced releases. The arbitration lock also lets an
                        # already-running callback finish before its resulting
                        # state is cancelled below.
                        self._raw_input_generation += 1
                if listener is not None:
                    self._cancel_raw_input_ownership_locked(
                        reason="raw_input_recovery"
                    )

            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    self._set_raw_listener_state("failed_stopping")
                    self._logger.exception(
                        "Raw Input recovery could not stop the old listener"
                    )
                    with self._raw_input_lifecycle_lock:
                        self._schedule_raw_input_recovery_locked()
                    return
                with self._raw_input_lifecycle_lock:
                    if self._hid_listener is listener:
                        self._hid_listener = None
                        self._raw_fallback_tracking_active = False

            try:
                self._start_hid_listener_owned(recovering=True)
            except raw_input_windows.RawInputUnavailableError:
                self._logger.exception(
                    "Raw Input recovery retained a listener that did not start cleanly"
                )
                with self._raw_input_lifecycle_lock:
                    self._schedule_raw_input_recovery_locked()

    def _start_hid_listener(self, *, recovering: bool = False) -> None:
        with self._raw_input_operation_lock:
            self._start_hid_listener_owned(recovering=recovering)

    def _ensure_physical_keyboard_tracking_listener(self) -> None:
        """Always keep a keyboard-only Raw Input listener for physical key state.

        遥控器 HID 设备路径可能根本拿不到（按键由驱动拦截提供），而语音组合键
        必须先确认“键是不是真的按着”才允许注入；没有这个监听器，Shift+D 这类
        组合会被整体拒绝。它只跟踪键盘、不解码遥控器按键。
        """
        with self._raw_input_lifecycle_lock:
            if getattr(self, "_raw_input_stopping", False):
                return
            current = getattr(self, "_raw_tracking_listener", None)
            if current is not None and bool(getattr(current, "is_running", False)):
                return
        listener = raw_input_windows.RawInputButtonListener(lambda *args: None)
        try:
            listener.start(None)
        except Exception:
            self._logger.exception(
                "startup: physical keyboard tracking listener unavailable"
            )
            return
        with self._raw_input_lifecycle_lock:
            if getattr(self, "_raw_input_stopping", False):
                previous, self._raw_tracking_listener = listener, None
            else:
                previous, self._raw_tracking_listener = (
                    getattr(self, "_raw_tracking_listener", None), listener)
        if previous is not None and previous is not listener:
            try:
                previous.stop()
            except Exception:
                self._logger.exception(
                    "startup: old physical keyboard tracking listener did not stop"
                )

    def _set_raw_listener_state(self, state: str) -> None:
        if self._remote_profile == remote_selection.CHROMECAST_PROFILE:
            # The Chromecast receiver owns the device state (see
            # chromecast_runtime's "chromecast_ready"); the keyboard-only
            # tracker must not overwrite it with its own lifecycle labels.
            self._logger.info("chromecast keyboard tracking state: %s", state)
            return
        self._set_runtime_input_state(raw_input_state=state)

    def _record_raw_input_lookup(self, generation, outcome, enumeration, selection) -> None:
        # The producers supply only fixed labels/counts/codes, never device paths.
        fields = dict(outcome=outcome, enumeration=enumeration, selection=selection,
                      selected_ref=self._selected_remote_key[:12]
                      if remote_selection.valid_key(self._selected_remote_key) else "")
        signature = json.dumps(fields, sort_keys=True, ensure_ascii=True)
        if signature == self._last_raw_input_lookup_diagnostic:
            return
        fields.update(generation=generation, app_pid=os.getpid())
        extra = {}
        if outcome in {"no_device", "unavailable", "ambiguous"}:
            extra = dict(failure_key="hid_discovery:raw_input", failure_id=uuid.uuid4().hex)
            fields.update(extra)
        recorded = False
        try:
            self._logger.info("HID diagnostic: %s", json.dumps(
                dict(event="hid_raw_input_lookup", **fields), ensure_ascii=True), extra=extra)
            recorded = True
        except Exception:
            pass
        try:
            recorded = self._diagnostic_trace.emit("hid_raw_input_lookup", **fields) or recorded
        except Exception:
            pass
        if recorded:
            self._last_raw_input_lookup_diagnostic = signature

    def _start_hid_listener_owned(self, *, recovering: bool = False) -> None:
        """Best-effort: buttons fail closed independently of BLE/voice.

        Multiple matching HID device paths -> fail closed for buttons only
        (log and leave the listener unstarted); this must not tear down the
        BLE/voice path, which does not depend on HID at all.

        XRBM-019 review round 1 P1 #3: a failed ``start()`` call does not
        necessarily mean the listener never came alive - it may have left a
        thread/window behind that its own bounded failed-start cleanup could
        not stop (see raw_input_windows.py's ``_abandon_failed_start()``,
        which keeps ``is_running`` honest for exactly this reason). Clearing
        ``self._hid_listener`` to ``None`` unconditionally here would lose
        that owner reference and let a later ``_connect_once()`` generation
        start a second listener over the still-live one. Only clear it once
        the listener itself confirms it is not running; otherwise retain and
        re-raise so this propagates up through ``_connect_once()`` into
        ``ConnectionSupervisor.run_forever()``'s except handler, which still
        falls through to ``_cleanup_once()`` - giving cleanup a chance to
        retry stopping it, exactly like any other retained-owner failure.
        """

        with self._input_arbitration_lock:
            with self._raw_input_lifecycle_lock:
                if self._raw_input_stopping or not self._accept_input_events:
                    return
                current = self._hid_listener
                if current is not None and bool(
                    getattr(current, "is_running", False)
                ):
                    return
                self._hid_listener = None
                self._raw_fallback_tracking_active = False
                self._raw_input_generation += 1
                generation = self._raw_input_generation
                self._raw_input_lost_generation = -1

        self._set_raw_listener_state("recovering" if recovering else "starting")
        # Chromecast has no RC003 HID collection to select: the listener runs in
        # keyboard-safety-only mode, which registers the keyboard page alone and
        # tracks physical ownership without decoding any remote button.
        keyboard_only = self._remote_profile == remote_selection.CHROMECAST_PROFILE
        enumeration, selection = {}, {}
        lookup_outcome = "lookup_interrupted"
        try:
            device_path = None
            if not keyboard_only:
                paths = raw_input_windows.enumerate_matching_device_paths(diagnostic=enumeration)
                device_path = remote_selection.selected_raw_path(
                    paths, self._selected_remote_key, diagnostic=selection)
                lookup_outcome = "path_selected"
        except raw_input_windows.RawInputUnavailableError as exc:
            lookup_outcome = "unavailable"
            self._set_runtime_input_state(raw_input_state="unavailable")
            self._logger.info("startup: Raw Input unavailable; buttons disabled: %s", exc)
            with self._raw_input_lifecycle_lock:
                if self._raw_input_generation == generation:
                    self._schedule_raw_input_recovery_locked()
            return
        except hid_identity.NoDevicePathFoundError:
            lookup_outcome = "no_device"
            self._set_runtime_input_state(raw_input_state="no_device")
            self._logger.info("startup: no RC003 HID device path found; buttons unavailable")
            with self._raw_input_lifecycle_lock:
                if self._raw_input_generation == generation:
                    self._schedule_raw_input_recovery_locked()
            return
        except hid_identity.AmbiguousDevicePathError as exc:
            lookup_outcome = "ambiguous"
            self._set_runtime_input_state(raw_input_state="ambiguous")
            self._logger.info(
                "startup: buttons failing closed, ambiguous HID device paths: %s", exc
            )
            with self._raw_input_lifecycle_lock:
                if self._raw_input_generation == generation:
                    self._schedule_raw_input_recovery_locked()
            return
        finally:
            if not keyboard_only:
                self._record_raw_input_lookup(generation, lookup_outcome, enumeration, selection)

        listener = raw_input_windows.RawInputButtonListener(
            self._on_raw_button_event
        )
        with self._raw_input_lifecycle_lock:
            if (
                self._raw_input_stopping
                or not self._accept_input_events
                or self._raw_input_generation != generation
            ):
                return
            self._hid_listener = listener
        if not keyboard_only:
            self._sync_physical_bindings_to_listener()
        set_sourced_button_event_callback = getattr(
            listener,
            "set_sourced_button_event_callback",
            None,
        )
        if not keyboard_only and callable(set_sourced_button_event_callback):
            set_sourced_button_event_callback(
                lambda button_id, is_pressed, source, windows_button_id: (
                    self._on_raw_button_event(
                    button_id,
                    is_pressed,
                    source,
                    windows_button_id,
                    _listener=listener,
                    _generation=generation,
                )
                )
            )
        set_raw_event_callback = getattr(
            listener,
            "set_raw_event_callback",
            None,
        )
        if not keyboard_only and callable(set_raw_event_callback):
            set_raw_event_callback(
                lambda event: self._on_raw_physical_event(
                    event,
                    _listener=listener,
                    _generation=generation,
                )
            )
            with self._raw_input_lifecycle_lock:
                if (
                    self._hid_listener is listener
                    and self._raw_input_generation == generation
                ):
                    self._raw_fallback_tracking_active = True
        set_device_removed_callback = getattr(
            listener,
            "set_device_removed_callback",
            None,
        )
        if not keyboard_only and callable(set_device_removed_callback):
            set_device_removed_callback(
                lambda: self._on_raw_input_device_removed(
                    _listener=listener,
                    _generation=generation,
                )
            )
        set_input_corruption_callback = getattr(
            listener,
            "set_input_corruption_callback",
            None,
        )
        if not keyboard_only and callable(set_input_corruption_callback):
            set_input_corruption_callback(
                lambda reason: self._on_raw_input_corruption(
                    reason,
                    _listener=listener,
                    _generation=generation,
                )
            )
        set_physical_keyboard_tracking_lost_callback = getattr(
            listener,
            "set_physical_keyboard_tracking_lost_callback",
            None,
        )
        if callable(set_physical_keyboard_tracking_lost_callback):
            set_physical_keyboard_tracking_lost_callback(
                lambda reason: self._on_physical_keyboard_tracking_lost(
                    reason,
                    _listener=listener,
                    _generation=generation,
                )
            )
        try:
            listener.start(device_path)
        except raw_input_windows.RawInputUnavailableError as exc:
            if listener.is_running:
                self._set_raw_listener_state("failed_running")
                self._logger.exception(
                    "startup: Raw Input listener failed to start but is still running; "
                    "owner retained for cleanup to retry"
                )
                raise
            self._logger.info("startup: Raw Input listener failed to start: %s", exc)
            with self._raw_input_lifecycle_lock:
                if (
                    self._hid_listener is listener
                    and self._raw_input_generation == generation
                ):
                    self._hid_listener = None
                    self._raw_fallback_tracking_active = False
                    self._schedule_raw_input_recovery_locked()
            self._set_raw_listener_state("failed")
            return
        with self._input_arbitration_lock:
            with self._raw_input_lifecycle_lock:
                stale = (
                    self._raw_input_stopping
                    or not self._accept_input_events
                    or self._hid_listener is not listener
                    or self._raw_input_generation != generation
                )
                lost = self._raw_input_lost_generation == generation
                running = listener.is_running
                if not stale and (lost or not running):
                    self._schedule_raw_input_recovery_locked()
                elif not stale:
                    self._raw_input_retry_delay = (
                        _RAW_INPUT_RETRY_INITIAL_SECONDS
                    )
            if not stale:
                self._set_raw_listener_state("recovering" if lost or not running else "ready")
        if stale:
            if listener.is_running:
                try:
                    listener.stop()
                except Exception:
                    self._logger.exception(
                        "cleanup: stale Raw Input listener did not stop"
                    )
            return
        if lost or not running:
            return

    def _sync_physical_bindings_to_listener(self) -> None:
        with self._button_mapping_lock:
            listener = self._hid_listener
            if listener is None:
                return
            set_physical_bindings = getattr(listener, "set_physical_bindings", None)
            if not callable(set_physical_bindings):
                return
            try:
                set_physical_bindings(
                    self._bindings.get("physical_bindings", {})
                )
            except Exception as exc:  # noqa: BLE001 - keep the last live decoder
                self._logger.warning(
                    "physical button bindings update failed: error_type=%s",
                    type(exc).__name__,
                )

    def _start_hid_report_tap(self) -> None:
        """Start the upstream-derived tap for usages Windows drops.

        This is independent of the normal Raw Input listener.  A missing or
        unverified Gadget is a button-only degradation and must not prevent
        BLE voice from starting.

        Every launch tries the tap again: a previous failure is reported for
        that session only and is never remembered, so the key channel behaves
        like the Chromecast path instead of silently staying off.
        """

        self._set_runtime_input_state(hid_tap_state="starting")
        tap = frida_compat.RC003HidReportTap(
            self._on_direct_hid_report,
            selected_key=self._selected_remote_key,
            status_handler=self._on_hid_tap_status,
            diagnostic_trace=self._diagnostic_trace,
        )
        try:
            if tap.start():
                self._hid_report_tap = tap
                self._logger.info(
                    "startup: RC003 HID report tap thread started; state=%s",
                    tap.status,
                )
            else:
                self._set_runtime_input_state(hid_tap_state=tap.status)
                self._logger.info(
                    "startup: RC003 HID report tap unavailable: %s", tap.status
                )
        except Exception:
            self._set_runtime_input_state(
                hid_tap_state=frida_compat.HidTapState.FAILED.value
            )
            self._logger.exception("startup: RC003 HID report tap failed to start")
            try:
                tap.stop()
            except Exception:
                self._logger.exception("startup: RC003 HID report tap cleanup failed")
                self._hid_report_tap = tap
                raise

    @staticmethod
    def _direct_buttons_for_usages(usages: set[int]) -> set[str]:
        return {
            button
            for usage in usages
            if (button := frida_compat.TAP_USAGE_TO_BUTTON.get(usage)) is not None
        }

    def _cancel_input_gestures(
        self,
        buttons: set[str],
        *,
        reason: str,
        block_until_release: bool,
    ) -> None:
        if block_until_release:
            self._block_input_until_release(buttons)
        self._button_combos.reset()
        self._button_gestures.reset()
        self._key_detection_suppressed_buttons.clear()
        self._key_detection_suppression_deadlines.clear()

        if "mic" in buttons:
            with self._ordinary_mic_lock:
                self._ordinary_mic_sources_down.clear()
                self._ordinary_mic_late_sources_down.clear()
                self._ordinary_mic_late_source_deadlines.clear()
                self._ordinary_mic_sources_seen.clear()
                self._ordinary_mic_gesture_started_at = 0.0
                self._ordinary_mic_release_guard_until = 0.0
                self._ordinary_mic_gesture_active = False
            with self._key_detection_mic_lock:
                self._reset_key_detection_mic_gesture_locked()
            with self._voice_shortcut.lock:
                self._voice_mic_gesture_sources_down.clear()
                self._voice_shortcut_physical_sources_down.clear()
                self._voice_mic_gesture_hid_released = True
                if self._voice_shortcut.controller.active:
                    self._release_hold_voice_on_physical_release_locked(reason)
                if (
                    self._voice_mic_gesture_active
                    and not self._voice_audio_stream_active
                ):
                    self._finish_voice_mic_gesture()
                self._apply_pending_voice_settings_if_idle_locked()

        if buttons:
            self._logger.warning(
                "button input ownership cancelled: reason=%s buttons=%s",
                reason,
                sorted(buttons),
            )

    def _cancel_input_gestures_for_buttons(
        self,
        buttons: set[str],
        *,
        reason: str,
        block_until_release: bool,
    ) -> None:
        """Cancel only input state coupled to the named physical buttons."""

        affected_buttons = set(buttons)
        affected_buttons.update(
            self._button_combos.cancel_buttons(affected_buttons)
        )
        if block_until_release:
            self._block_input_until_release(affected_buttons)
        self._button_gestures.cancel_buttons(affected_buttons)
        self._key_detection_suppressed_buttons.difference_update(
            affected_buttons
        )
        for button_id in affected_buttons:
            self._key_detection_suppression_deadlines.pop(button_id, None)

        if "mic" in affected_buttons:
            with self._ordinary_mic_lock:
                self._ordinary_mic_sources_down.clear()
                self._ordinary_mic_late_sources_down.clear()
                self._ordinary_mic_late_source_deadlines.clear()
                self._ordinary_mic_sources_seen.clear()
                self._ordinary_mic_gesture_started_at = 0.0
                self._ordinary_mic_release_guard_until = 0.0
                self._ordinary_mic_gesture_active = False
            with self._key_detection_mic_lock:
                self._reset_key_detection_mic_gesture_locked()
            with self._voice_shortcut.lock:
                self._voice_mic_gesture_sources_down.clear()
                self._voice_mic_gesture_hid_released = True
                if self._voice_shortcut.controller.active:
                    self._release_hold_voice_on_physical_release_locked(reason)
                if (
                    self._voice_mic_gesture_active
                    and not self._voice_audio_stream_active
                ):
                    self._finish_voice_mic_gesture()
                self._apply_pending_voice_settings_if_idle_locked()

        if affected_buttons:
            self._logger.warning(
                "button input ownership cancelled: reason=%s buttons=%s",
                reason,
                sorted(affected_buttons),
            )

    def _active_audio_owns_late_raw_mic(self) -> bool:
        """Return whether ATVV audio already owns the current mic press."""

        with self._voice_shortcut.lock:
            return (
                self._voice_mic_gesture_active
                and self._voice_mic_gesture_audio_started
                and self._voice_audio_stream_active
                and (
                    self._voice_shortcut.controller.active
                    or self._voice_shortcut.pending_tokens is not None
                )
            )

    def _on_raw_physical_event(
        self,
        event: raw_input_windows.RawInputEvent,
        *,
        _listener: Optional[raw_input_windows.RawInputButtonListener] = None,
        _generation: Optional[int] = None,
    ) -> None:
        """Track the real Windows key independently of a taught button ID."""

        if not self._raw_input_callback_is_current(_listener, _generation):
            return
        logical_button = event.button_id
        physical_button = event.windows_button_id
        trace_button = logical_button or physical_button or "unknown"
        trace_source = {
            "keyboard": "raw_keyboard",
            "hid": "raw_hid",
        }.get(event.source, "raw_unknown")
        gesture_id = self._diagnostic_trace.begin_gesture(
            trace_button,
            trace_source,
            bool(event.is_pressed),
            vkey=event.vkey if event.vkey is not None else -1,
            scan_code=event.make_code if event.make_code is not None else -1,
            flags=event.flags if event.flags is not None else -1,
        )
        self._diagnostic_trace.emit(
            "raw_input_event",
            gesture_id=gesture_id,
            button_id=event.button_id or "",
            windows_button_id=event.windows_button_id or "",
            edge="down" if event.is_pressed else "up",
            vkey=event.vkey if event.vkey is not None else -1,
            make_code=event.make_code if event.make_code is not None else -1,
            flags=event.flags if event.flags is not None else -1,
            raw_source=event.source,
            interception_armed=bool(self._direct_hid_interception_armed),
            interception_ready=bool(self._direct_hid_interception_ready),
        )
        signature = raw_input_windows.physical_signature(event)
        with self._input_arbitration_lock:
            if not self._raw_input_callback_is_current(
                _listener,
                _generation,
            ):
                return
            self._raw_fallback_tracking_active = True

            if not event.is_pressed:
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="release_tracking",
                )
                tracked = self._raw_fallback_physical_buttons_down.pop(
                    signature,
                    None,
                )
                debt_logical_buttons, debt_physical_buttons = (
                    self._raw_fallback_release_debts.pop(
                        signature,
                        (set(), set()),
                    )
                )
                released_buttons = (
                    set(debt_logical_buttons) | set(debt_physical_buttons)
                )
                if tracked is not None:
                    released_buttons.add(tracked[0])
                    if tracked[1] is not None:
                        released_buttons.add(tracked[1])
                else:
                    if logical_button is not None:
                        released_buttons.add(logical_button)
                    if physical_button is not None:
                        released_buttons.add(physical_button)
                remaining = [
                    {
                        candidate
                        for candidate in tracked_pair
                        if candidate is not None
                    }
                    for tracked_pair in (
                        self._raw_fallback_physical_buttons_down.values()
                    )
                ] + [
                    set(logical_buttons) | set(physical_buttons)
                    for logical_buttons, physical_buttons in (
                        self._raw_fallback_release_debts.values()
                    )
                ]
                with self._direct_hid_lock:
                    direct_buttons = self._direct_buttons_for_usages(
                        self._direct_hid_usages
                    )
                for blocked_button in released_buttons:
                    if (
                        blocked_button not in direct_buttons
                        and all(
                            blocked_button not in candidate
                            for candidate in remaining
                        )
                    ):
                        self._input_rearm_blocked_buttons.discard(
                            blocked_button
                        )
                return

            if not self._accept_input_events:
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="ignored",
                    reason="input_not_accepted",
                )
                return
            release_debt = self._raw_fallback_release_debts.get(signature)
            if release_debt is not None:
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="blocked",
                    reason="release_debt",
                )
                debt_logical_buttons, debt_physical_buttons = release_debt
                if logical_button is not None:
                    debt_logical_buttons.add(logical_button)
                if physical_button is not None:
                    debt_physical_buttons.add(physical_button)
                self._block_input_until_release(
                    set(debt_logical_buttons) | set(debt_physical_buttons)
                )
                if physical_button in _RAW_FALLBACK_KEY_TOKENS:
                    self._release_raw_fallback_keyups(
                        {physical_button},
                        reason="raw_release_debt_repeat",
                    )
                return
            if logical_button is None:
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="ignored",
                    reason="unmapped_physical_key",
                )
                return
            was_new = signature not in self._raw_fallback_physical_buttons_down
            tracked = self._raw_fallback_physical_buttons_down.setdefault(
                signature,
                (logical_button, physical_button),
            )
            tracked_logical, tracked_physical = tracked
            if not (
                self._direct_hid_interception_armed
                or self._direct_hid_interception_ready
            ):
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="windows_original",
                    reason="hid_interception_not_armed",
                )
                return

            physical_was_blocked = (
                tracked_physical in self._input_rearm_blocked_buttons
            )
            blocked_buttons = {tracked_logical}
            if tracked_physical is not None:
                blocked_buttons.add(tracked_physical)
            with self._direct_hid_lock:
                direct_buttons = self._direct_buttons_for_usages(
                    self._direct_hid_usages
                )
            raw_only_buttons = set(blocked_buttons) - direct_buttons
            if (
                "mic" in raw_only_buttons
                and self._active_audio_owns_late_raw_mic()
            ):
                # AudioStarted can beat both the duplicated Windows F5 edge
                # and the direct HID report. The active ATVV gesture remains
                # authoritative until HID-up or AudioStopped closes it.
                raw_only_buttons.discard("mic")
            newly_quarantined = was_new and not physical_was_blocked
            if newly_quarantined:
                # A late Raw Input down is the Windows-side duplicate of a
                # report already owned by direct HID. Quarantine only aliases
                # that direct HID is not currently holding; the real HID up
                # must remain the sole release edge for the active gesture.
                if raw_only_buttons:
                    self._cancel_input_gestures_for_buttons(
                        raw_only_buttons,
                        reason="late_raw_after_hid_handover",
                        block_until_release=True,
                    )
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="quarantined",
                    reason="late_raw_after_hid_handover",
                    late=True,
                )
            else:
                self._block_input_until_release(raw_only_buttons)
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="blocked",
                    reason="already_quarantined",
                )
            if newly_quarantined and tracked_physical in _RAW_FALLBACK_KEY_TOKENS:
                self._release_raw_fallback_keyups(
                    {tracked_physical},
                    reason="late_raw_after_hid_handover",
                )

    def _take_raw_fallback_physical_buttons_locked(
        self,
        logical_buttons: set[str],
    ) -> set[str]:
        matched_signatures = {
            signature
            for signature, (logical_button, _physical_button) in (
                self._raw_fallback_physical_buttons_down.items()
            )
            if logical_button in logical_buttons
        }
        physical_buttons = {
            self._raw_fallback_physical_buttons_down[signature][1]
            for signature in matched_signatures
            if self._raw_fallback_physical_buttons_down[signature][1]
            in _RAW_FALLBACK_KEY_TOKENS
        }
        for signature in matched_signatures:
            tracked = self._raw_fallback_physical_buttons_down.pop(
                signature,
                None,
            )
            if tracked is not None:
                release_debt = self._raw_fallback_release_debts.setdefault(
                    signature,
                    (set(), set()),
                )
                debt_logical_buttons, debt_physical_buttons = release_debt
                debt_logical_buttons.add(tracked[0])
                if tracked[1] is not None:
                    debt_physical_buttons.add(tracked[1])

        # Compatibility for injected test owners and old listener fakes that
        # do not expose raw physical events. The real listener always enables
        # tracking before it can emit a logical edge, so production remaps
        # never guess the logical key here.
        if not self._raw_fallback_tracking_active:
            physical_buttons.update(
                logical_button
                for logical_button in logical_buttons
                if logical_button in _RAW_FALLBACK_KEY_TOKENS
            )
        return physical_buttons

    def _raw_fallback_tracked_button_ids_locked(
        self,
    ) -> tuple[set[str], set[str]]:
        logical_buttons = {
            logical_button
            for logical_button, _physical_button in (
                self._raw_fallback_physical_buttons_down.values()
            )
        }
        physical_buttons = {
            physical_button
            for _logical_button, physical_button in (
                self._raw_fallback_physical_buttons_down.values()
            )
            if physical_button in _RAW_FALLBACK_KEY_TOKENS
        }
        return logical_buttons, physical_buttons

    def _windows_buttons_down_before_hid_handover(
        self,
    ) -> Optional[set[str]]:
        buttons_down: set[str] = set()
        try:
            for button, token in _RAW_FALLBACK_KEY_TOKENS.items():
                vk_codes = win32_keys.resolve_vk_codes((token,))
                if any(
                    self._raw_windows_key_down_query(vk_code)
                    for vk_code in vk_codes
                ):
                    buttons_down.add(button)
        except Exception as exc:  # noqa: BLE001 - unknown state fails closed
            self._logger.warning(
                "Windows key state unavailable during HID handover: error_type=%s",
                type(exc).__name__,
            )
            return None
        return buttons_down

    def _release_raw_fallback_keyups(
        self,
        physical_buttons: set[str],
        *,
        reason: str,
    ) -> None:
        tokens = tuple(
            dict.fromkeys(
                token
                for button in sorted(physical_buttons)
                if (token := _RAW_FALLBACK_KEY_TOKENS.get(button)) is not None
            )
        )
        if not tokens:
            return
        with self._button_action_lock:
            pending = self._button_key_release_pending or ()
            owed_tokens = tuple(dict.fromkeys((*pending, *tokens)))
            self._button_key_release_pending = owed_tokens
            if self._release_pending_button_keys():
                self._logger.info(
                    "raw Windows key-up safety release completed: reason=%s buttons=%s",
                    reason,
                    sorted(physical_buttons),
                )

    def _cancel_raw_fallback_hold_guard_locked(self, button_id: str) -> None:
        guard = self._raw_fallback_hold_guards.pop(button_id, None)
        if guard is None:
            return
        _token, timer = guard
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except BaseException as exc:  # noqa: BLE001 - token is already invalid
                self._logger.warning(
                    "raw fallback hold timer cancel failed: button=%s error_type=%s",
                    button_id,
                    type(exc).__name__,
                )

    def _cancel_all_raw_fallback_hold_guards_locked(self) -> None:
        for button_id in tuple(self._raw_fallback_hold_guards):
            self._cancel_raw_fallback_hold_guard_locked(button_id)

    def _arm_raw_fallback_hold_guard_locked(self, button_id: str) -> bool:
        self._cancel_raw_fallback_hold_guard_locked(button_id)
        token = object()
        try:
            timer = self._raw_fallback_timer_factory(
                button_gesture.ButtonGestureDispatcher.MAX_HOLD_SECONDS,
                lambda button_id=button_id, token=token: (
                    self._raw_fallback_hold_timeout(button_id, token)
                ),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._raw_fallback_hold_guards[button_id] = (token, timer)
            timer.start()
        except BaseException as exc:  # noqa: BLE001 - immediately release the key
            current = self._raw_fallback_hold_guards.get(button_id)
            if current is not None and current[0] is token:
                self._raw_fallback_hold_guards.pop(button_id, None)
            self._logger.error(
                "raw fallback hold timer failed to start: button=%s error_type=%s",
                button_id,
                type(exc).__name__,
            )
            return False
        return True

    def _raw_fallback_hold_timeout(self, button_id: str, token: object) -> None:
        with self._input_arbitration_lock:
            guard = self._raw_fallback_hold_guards.get(button_id)
            if guard is None or guard[0] is not token:
                return
            self._raw_fallback_hold_guards.pop(button_id, None)
            if button_id not in self._raw_fallback_buttons_down:
                return
            self._raw_fallback_buttons_down.discard(button_id)
            physical_buttons = self._take_raw_fallback_physical_buttons_locked(
                {button_id}
            )
            self._block_input_until_release(
                {button_id} | physical_buttons
            )
            self._release_raw_fallback_keyups(
                physical_buttons,
                reason="raw_fallback_hold_timeout",
            )
            self._logger.warning(
                "raw Windows key held for %.0fs; forced release until physical up: "
                "button=%s",
                button_gesture.ButtonGestureDispatcher.MAX_HOLD_SECONDS,
                button_id,
            )

    def _on_raw_input_device_removed(
        self,
        *,
        _listener: Optional[raw_input_windows.RawInputButtonListener] = None,
        _generation: Optional[int] = None,
    ) -> None:
        if not self._raw_input_callback_is_current(_listener, _generation):
            return
        with self._input_arbitration_lock:
            if not self._raw_input_callback_is_current(
                _listener,
                _generation,
            ):
                return
            with self._raw_input_lifecycle_lock:
                resolved_generation = (
                    self._raw_input_generation
                    if _generation is None
                    else int(_generation)
                )
                self._raw_input_lost_generation = resolved_generation
            self._set_runtime_input_state(raw_input_state="device_removed")
            with self._direct_hid_lock:
                direct_buttons = self._direct_buttons_for_usages(
                    self._direct_hid_usages
                )
                self._direct_hid_usages.clear()
            tracked_logical_buttons, tracked_physical_buttons = (
                self._raw_fallback_tracked_button_ids_locked()
            )
            raw_fallback_buttons = (
                set(self._raw_fallback_buttons_down)
                | tracked_logical_buttons
            )
            raw_fallback_physical_buttons = (
                self._take_raw_fallback_physical_buttons_locked(
                    raw_fallback_buttons
                )
            )
            self._cancel_all_raw_fallback_hold_guards_locked()
            lost_buttons = (
                raw_fallback_buttons
                | tracked_physical_buttons
                | direct_buttons
                | set(self._input_rearm_blocked_buttons)
            )
            self._direct_hid_interception_ready = False
            self._direct_hid_interception_armed = False
            self._direct_hid_handover_waiting_for_neutral = False
            self._direct_hid_interception_armed_monotonic = None
            self._raw_fallback_buttons_down.clear()
            self._input_rearm_blocked_buttons.clear()
            # The Raw Input collection can be re-enumerated while the
            # separately owned HID tap is still alive. Preserve a guard for
            # direct buttons that were down until a later neutral report or
            # matching release proves the old hold is over.
            self._block_input_until_release(lost_buttons)
            self._cancel_input_gestures(
                lost_buttons,
                reason="raw_input_device_removed",
                block_until_release=False,
            )
            self._release_raw_fallback_keyups(
                raw_fallback_physical_buttons,
                reason="raw_input_device_removed",
            )
        self._logger.warning("RC003 Raw Input device removed; requesting reconnect")
        with self._raw_input_lifecycle_lock:
            if self._raw_input_callback_is_current(_listener, _generation):
                self._schedule_raw_input_recovery_locked()
        self._supervisor.request_reconnect()

    def _on_raw_input_corruption(
        self,
        reason: str,
        *,
        _listener: Optional[raw_input_windows.RawInputButtonListener] = None,
        _generation: Optional[int] = None,
    ) -> None:
        """Cancel Raw Input ownership while preserving the physical release."""

        if not self._raw_input_callback_is_current(_listener, _generation):
            return
        with self._input_arbitration_lock:
            if not self._raw_input_callback_is_current(
                _listener,
                _generation,
            ):
                return
            affected_buttons = self._cancel_raw_input_ownership_locked(
                reason=f"raw_input_corruption_{reason}"
            )
            if reason in {
                "raw_input_header_unavailable",
                "raw_input_listener_exited",
            }:
                with self._raw_input_lifecycle_lock:
                    resolved_generation = (
                        self._raw_input_generation
                        if _generation is None
                        else int(_generation)
                    )
                    self._raw_input_lost_generation = resolved_generation
                    self._schedule_raw_input_recovery_locked()
                self._set_runtime_input_state(raw_input_state="recovering")
        self._logger.warning(
            "RC003 Raw Input data rejected: reason=%s cancelled_buttons=%s",
            reason,
            sorted(affected_buttons),
        )

    def _on_physical_keyboard_tracking_lost(
        self,
        reason: str,
        *,
        _listener: Optional[raw_input_windows.RawInputButtonListener] = None,
        _generation: Optional[int] = None,
    ) -> None:
        """End an unsafe ordinary-key HOLD; future downs fail closed."""

        with self._input_arbitration_lock:
            if not self._raw_input_callback_is_current(
                _listener,
                _generation,
            ):
                return
            with self._raw_input_lifecycle_lock:
                resolved_generation = (
                    self._raw_input_generation
                    if _generation is None
                    else int(_generation)
                )
                self._raw_input_lost_generation = resolved_generation
                self._schedule_raw_input_recovery_locked()
            self._set_raw_listener_state("unhealthy")
            with self._voice_shortcut.lock:
                tokens = self._voice_shortcut.recorded_tokens()
                active_hold = (
                    self._voice_shortcut.controller.active
                    or self._voice_shortcut.pending_tokens is not None
                )
                if (
                    self._remote_profile != remote_selection.CHROMECAST_PROFILE
                    and active_hold
                    and not win32_input.can_begin_tracked_hold(tokens)
                ):
                    self._force_voice_hold_release_locked(
                        "physical keyboard tracking lost"
                    )
            self._release_pending_button_keys()
        self._logger.warning(
            "physical keyboard tracking lost: reason=%s; unsafe held shortcuts disabled",
            reason,
        )

    def _on_voice_key_physicalizer_tracking_lost(
        self,
        physicalizer: Optional[
            voice_key_physicalizer_windows.VoiceKeyPhysicalizer
        ] = None,
        generation: Optional[int] = None,
    ) -> None:
        """Release an active marked right-Alt hold before the hook disappears."""

        with self._voice_key_physicalizer_lifecycle_lock:
            current = self._voice_key_physicalizer
            current_generation = self._voice_key_physicalizer_generation
            resolved_physicalizer = physicalizer or current
            resolved_generation = (
                current_generation if generation is None else int(generation)
            )
            if (
                resolved_physicalizer is None
                or current is not resolved_physicalizer
                or current_generation != resolved_generation
                or self._voice_key_physicalizer_stopping
            ):
                return
            self._voice_key_physicalizer_lost_generation = resolved_generation
            self._voice_key_physicalizer_ready = False
        self._set_runtime_input_state(
            voice_key_physicalizer_state="recovering"
        )
        raw_tracking_available = (
            raw_input_windows.physical_keyboard_tracking_available()
        )
        with self._voice_shortcut.lock:
            tokens = self._voice_shortcut.recorded_tokens()
            try:
                uses_right_alt = (
                    win32_keys.VK_CODES["ralt"]
                    in win32_keys.resolve_vk_codes(tokens)
                )
            except win32_keys.UnknownKeyTokenError:
                uses_right_alt = False
            active_hold = (
                self._voice_shortcut.controller.active
                or self._voice_shortcut.pending_tokens is not None
            )
            if (
                active_hold
                and (
                    not raw_tracking_available
                    or uses_right_alt
                )
            ):
                self._force_voice_hold_release_locked(
                    "voice key physicalizer stopped unexpectedly"
                )
        if not raw_tracking_available:
            self._release_pending_button_keys()
        with self._voice_key_physicalizer_lifecycle_lock:
            if (
                self._voice_key_physicalizer is resolved_physicalizer
                and self._voice_key_physicalizer_generation
                == resolved_generation
                and not self._voice_key_physicalizer_stopping
            ):
                self._schedule_voice_key_physicalizer_recovery_locked()
        self._logger.warning(
            "voice key physicalizer stopped unexpectedly; recovery scheduled"
        )

    def _on_voice_key_physicalizer_health_failure(
        self,
        physicalizer: voice_key_physicalizer_windows.VoiceKeyPhysicalizer,
        generation: int,
        reason: str,
        snapshot: voice_key_physicalizer_windows.PhysicalizerHealthSnapshot,
    ) -> None:
        """Close admission immediately and recover only after owned cleanup."""

        with self._voice_key_physicalizer_lifecycle_lock:
            if (
                self._voice_key_physicalizer is not physicalizer
                or self._voice_key_physicalizer_generation != int(generation)
                or self._voice_key_physicalizer_stopping
            ):
                return
            self._voice_key_physicalizer_ready = False
            self._voice_key_physicalizer_degraded_retry_deadline = (
                time.monotonic()
                + _VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS
            )
            self._schedule_voice_key_physicalizer_recovery_locked()
        self._set_runtime_input_state(
            voice_key_physicalizer_state="recovering"
        )
        try:
            self._diagnostic_trace.emit(
                "voice_key_physicalizer_health",
                status="degraded",
                reason=str(reason),
                app_generation=int(generation),
                **snapshot.trace_fields(),
            )
        except BaseException:
            pass
        self._logger.warning(
            "voice key physicalizer degraded after required acknowledgement "
            "failure; new voice starts blocked until owned cleanup completes: "
            "generation=%s tracker_generation=%s callback_delta=%s marker_delta=%s",
            generation,
            snapshot.generation,
            snapshot.receipt_callback_entry_delta,
            snapshot.receipt_marker_callback_delta,
        )

    def _raw_event_may_revert_direct_hid_locked(self) -> bool:
        """Reopen the Raw Input fallback path when the tap stops intercepting.

        The caller must hold ``self._input_arbitration_lock``. Raw Input only
        sees the RC003's translated keyboard usages when the HID tap failed to
        clear that report before Windows translated it, so such an event is
        direct evidence the tap is no longer intercepting. A ready arming gets
        a short grace window because the tap may simply not have intercepted a
        report yet, and an arming whose age is unknown is treated as fresh;
        after that, drop the armed/ready ownership so later raw
        events run the fail-closed raw fallback instead of being discarded.
        An arming that has never verified a single interception is still in
        the neutral-wait handover, which deliberately lets the in-flight key
        episode reach Windows, so its raw events never count as tap failure.
        """

        if not (
            self._direct_hid_interception_armed
            or self._direct_hid_interception_ready
        ):
            return False
        if not self._direct_hid_interception_ready:
            # The tap owns no key episode before its first verified
            # interception (prebound identity or a neutral handover report),
            # so a raw event now is expected handover traffic, not evidence
            # the tap stopped clearing. Fail closed without reverting.
            return False
        armed_at = self._direct_hid_interception_armed_monotonic
        if (
            armed_at is None
            or time.monotonic() - armed_at < _DIRECT_HID_RAW_REVERT_GRACE_SECONDS
        ):
            return False
        self._logger.warning(
            "RC003 raw input reached Windows while the HID tap owned mapping; "
            "reverting custom mappings to the raw fallback path"
        )
        self._direct_hid_interception_armed = False
        self._direct_hid_interception_ready = False
        self._direct_hid_handover_waiting_for_neutral = False
        self._direct_hid_interception_armed_monotonic = None
        # Block every reclaim path (late tap reports, late tap status) until
        # the scheduled restart hands ownership to a fresh tap.
        self._direct_hid_interception_reverted_pending_restart = True
        self._schedule_hid_tap_revert_restart_locked()
        with self._direct_hid_lock:
            stale_usages = set(self._direct_hid_usages)
            self._direct_hid_usages.clear()
        stale_buttons = self._direct_buttons_for_usages(stale_usages)
        self._cancel_input_gestures(
            stale_buttons,
            reason="hid_tap_reverted_to_raw",
            block_until_release=True,
        )
        return True

    def _cancel_hid_tap_revert_restart_locked(self) -> None:
        """Cancel a pending revert-triggered tap restart.

        The caller must hold ``self._input_arbitration_lock``.
        """

        timer = self._hid_tap_restart_timer
        self._hid_tap_restart_timer = None
        if timer is None:
            return
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except BaseException:
                pass

    def _schedule_hid_tap_revert_restart_locked(self) -> None:
        """Schedule one tap restart after a Raw Input fallback revert.

        The caller must hold ``self._input_arbitration_lock``. Reverts can
        burst while every keypress leaks through, so the restart is debounced,
        kept away from the previous restart by a cooldown, and capped to a few
        attempts per window so a persistently failing Gadget cannot cycle the
        attach forever.
        """

        if self._raw_input_stopping or not self._accept_input_events:
            return
        if self._hid_tap_restart_timer is not None:
            return
        now = time.monotonic()
        window_start = self._hid_tap_auto_restart_window_start
        if (
            not window_start
            or now - window_start >= _HID_TAP_REVERT_RESTART_WINDOW_SECONDS
        ):
            self._hid_tap_auto_restart_window_start = now
            self._hid_tap_auto_restart_attempts = 0
            self._hid_tap_auto_restart_exhausted_logged = False
        if (
            self._hid_tap_auto_restart_attempts
            >= _HID_TAP_REVERT_RESTART_MAX_ATTEMPTS
        ):
            if not self._hid_tap_auto_restart_exhausted_logged:
                self._hid_tap_auto_restart_exhausted_logged = True
                self._logger.warning(
                    "RC003 HID tap restart attempts exhausted for this "
                    "window; staying on the Raw Input fallback path"
                )
            return
        delay = _HID_TAP_REVERT_RESTART_DELAY_SECONDS
        last = self._hid_tap_auto_restart_last_monotonic
        if last is not None:
            delay = max(
                delay,
                _HID_TAP_REVERT_RESTART_COOLDOWN_SECONDS - (now - last),
            )
        timer = self._hid_tap_restart_timer_factory(
            delay, self._run_hid_tap_auto_restart
        )
        timer.daemon = True
        self._hid_tap_restart_timer = timer
        timer.start()

    def _run_hid_tap_auto_restart(self) -> None:
        """Restart the HID tap after a revert, on the restart timer thread."""

        with self._hid_tap_restart_lock:
            self._hid_tap_restart_timer = None
            with self._voice_shortcut.lock:
                accept_input = self._accept_input_events
            with self._raw_input_lifecycle_lock:
                stopping = self._raw_input_stopping
            if stopping or not accept_input:
                return
            self._hid_tap_auto_restart_last_monotonic = time.monotonic()
            self._hid_tap_auto_restart_attempts += 1
            tap = self._hid_report_tap
            with self._input_arbitration_lock:
                self._hid_report_tap = None
                # Lift the reclaim block exactly here so only the fresh tap
                # started below can arm ownership again.
                self._direct_hid_interception_reverted_pending_restart = False
            if tap is not None:
                try:
                    tap.stop()
                except Exception:
                    self._logger.exception(
                        "restart: stopping the RC003 HID report tap failed"
                    )
            self._logger.warning(
                "RC003 HID tap stopped clearing reports; restarting the "
                "HID report tap"
            )
            self._start_hid_report_tap()

    def _on_hid_tap_status(self, status: str, detail: str) -> None:
        self._diagnostic_trace.emit(
            "hid_tap_status",
            status=str(status),
            detail=str(detail or ""),
        )
        self._set_runtime_input_state(hid_tap_state=status)
        interception_ready = status == frida_compat.HidTapState.READY.value
        interception_armed = status in {
            frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
            frida_compat.HidTapState.READY.value,
        }
        message = "RC003 HID report tap state: %s"
        args = [status]
        if detail:
            message += " detail=%s"
            args.append(detail)
        with self._input_arbitration_lock:
            if self._direct_hid_interception_reverted_pending_restart and (
                interception_armed or interception_ready
            ):
                # A revert proved this tap stopped clearing reports. Its late
                # status must not silently reclaim ownership; only the fresh
                # tap started by the revert restart may arm again.
                interception_ready = False
                interception_armed = False
            newly_armed = (
                interception_armed
                and not self._direct_hid_interception_armed
            )
            if not interception_ready and self._direct_hid_interception_ready:
                self._logger.warning(message, *args)
                with self._direct_hid_lock:
                    stale_usages = set(self._direct_hid_usages)
                    self._direct_hid_usages.clear()
                stale_buttons = self._direct_buttons_for_usages(stale_usages)
                self._cancel_input_gestures(
                    stale_buttons,
                    reason="hid_tap_ownership_lost",
                    block_until_release=True,
                )
            else:
                self._logger.info(message, *args)
            if newly_armed:
                # ATTACHED_WAITING_IO is emitted only after the helper has
                # acknowledged the interception lease. Cancel any in-flight
                # Raw Input gesture instead of completing it as a click, then
                # ignore the same physical hold until its real release arrives.
                windows_buttons_down = (
                    set()
                    if getattr(self._hid_report_tap, "native_copy_interception", False) is True
                    else self._windows_buttons_down_before_hid_handover()
                )
                self._direct_hid_handover_waiting_for_neutral = (
                    windows_buttons_down is None
                )
                windows_buttons_down = windows_buttons_down or set()
                tracked_logical_buttons, tracked_physical_buttons = (
                    self._raw_fallback_tracked_button_ids_locked()
                )
                raw_fallback_buttons = (
                    set(self._raw_fallback_buttons_down)
                    | tracked_logical_buttons
                )
                raw_fallback_physical_buttons = (
                    self._take_raw_fallback_physical_buttons_locked(
                        raw_fallback_buttons
                    )
                )
                handover_buttons = (
                    raw_fallback_buttons
                    | tracked_physical_buttons
                    | windows_buttons_down
                )
                self._cancel_all_raw_fallback_hold_guards_locked()
                self._cancel_input_gestures(
                    handover_buttons,
                    reason="hid_tap_handover",
                    block_until_release=True,
                )
                self._release_raw_fallback_keyups(
                    raw_fallback_physical_buttons | windows_buttons_down,
                    reason="hid_tap_handover",
                )
                self._raw_fallback_buttons_down.clear()
            elif not interception_armed:
                self._direct_hid_handover_waiting_for_neutral = False
            self._direct_hid_interception_armed = interception_armed
            self._direct_hid_interception_ready = interception_ready
            self._direct_hid_interception_armed_monotonic = (
                time.monotonic() if interception_armed else None
            )

    def _on_direct_hid_report(self, report_id: int, payload: bytes) -> None:
        """Translate every RC003 keyboard HID usage into button edges.

        The injected Gadget copies the report and clears its keyboard usages
        before Windows translates them. This callback therefore owns custom
        mapping only after the tap reports verified interception. Raw Keyboard
        remains observation/fallback and never adds a second mapped action.
        """

        observed_ns = time.perf_counter_ns()
        with self._input_arbitration_lock:
            self._direct_hid_report_seq += 1
            callback_seq = self._direct_hid_report_seq

            def record_decision(reason: str, **fields: object) -> None:
                if self._diagnostic_trace.enabled:
                    self._diagnostic_trace.emit(
                        "hid_report_decision",
                        callback_seq=callback_seq,
                        reason=reason,
                        resulting_usages=sorted(self._direct_hid_usages),
                        **fields,
                    )

            if self._diagnostic_trace.enabled:
                self._diagnostic_trace.emit(
                    "hid_report",
                    callback_seq=callback_seq,
                    observed_perf_counter_ns=observed_ns,
                    report_id=int(report_id),
                    report_length=len(payload),
                    report_hex=(
                        payload.hex() if report_id == 1 and len(payload) == 6 else ""
                    ),
                    previous_usages=sorted(self._direct_hid_usages),
                    accept_input=self._accept_input_events,
                    interception_armed=self._direct_hid_interception_armed,
                    interception_ready=self._direct_hid_interception_ready,
                    waiting_for_neutral=self._direct_hid_handover_waiting_for_neutral,
                )
            tap = self._hid_report_tap
            tap_reports_ready = (
                tap is not None
                and tap.status == frida_compat.HidTapState.READY.value
            )
            if (
                not self._accept_input_events
                or report_id != 1
                or len(payload) != 6
            ):
                record_decision(
                    "input_disabled" if not self._accept_input_events else "invalid_report"
                )
                return
            if self._direct_hid_interception_reverted_pending_restart:
                # A revert proved the running tap stopped clearing reports.
                # Its reports must not reclaim ownership; the scheduled
                # restart will hand it to a fresh tap instead.
                record_decision("reverted_pending_tap_restart")
                return
            if not self._direct_hid_interception_ready:
                if not tap_reports_ready:
                    record_decision("tap_not_ready")
                    return
                # A newly received, validated tap report is stronger evidence
                # than the Raw Input collection-removal notification. Restore
                # local ownership without waiting for a duplicate status event.
                if not self._direct_hid_interception_armed:
                    windows_buttons_down = (
                        set()
                        if getattr(tap, "native_copy_interception", False) is True
                        else self._windows_buttons_down_before_hid_handover()
                    )
                    self._direct_hid_handover_waiting_for_neutral = (
                        windows_buttons_down is None
                    )
                    if windows_buttons_down:
                        self._block_input_until_release(windows_buttons_down)
                        self._release_raw_fallback_keyups(
                            windows_buttons_down,
                            reason="hid_tap_report_revalidated",
                        )
                self._direct_hid_interception_armed = True
                self._direct_hid_interception_ready = True
                self._direct_hid_interception_armed_monotonic = time.monotonic()
            elif tap is not None and not tap_reports_ready:
                # The tap updates its own state before invoking the app status
                # callback. Reject a report that raced that callback.
                record_decision("tap_lost_readiness")
                return
            active = {
                int.from_bytes(payload[index : index + 2], "little")
                for index in range(0, len(payload), 2)
            } & set(frida_compat.TAP_USAGE_TO_BUTTON)
            active_buttons = self._direct_buttons_for_usages(active)
            with self._direct_hid_lock:
                previous = self._direct_hid_usages
                previous_buttons = self._direct_buttons_for_usages(previous)
                if self._direct_hid_handover_waiting_for_neutral:
                    if active:
                        self._block_input_until_release(
                            active_buttons | previous_buttons
                        )
                        self._direct_hid_usages = set(active)
                        record_decision("waiting_for_neutral")
                        return
                    self._direct_hid_handover_waiting_for_neutral = False
                if not active:
                    # A verified neutral tap snapshot proves that every
                    # pre-handover Raw physical edge has ended. Drop those
                    # records so a later press cannot inherit stale ownership.
                    self._raw_fallback_physical_buttons_down.clear()
                    self._raw_fallback_release_debts.clear()
                self._input_rearm_blocked_buttons.difference_update(
                    self._input_rearm_blocked_buttons
                    - active_buttons
                    - previous_buttons
                )
                if active == previous:
                    record_decision("unchanged")
                    return
                pressed = active - previous
                released = previous - active
                self._direct_hid_usages = set(active)
            record_decision(
                "edges",
                pressed_usages=sorted(pressed),
                released_usages=sorted(released),
            )
            for usage in sorted(released):
                button = frida_compat.TAP_USAGE_TO_BUTTON[usage]
                self._logger.info(
                    "RC003 direct HID usage up: 0x%04x -> %s",
                    usage,
                    button,
                )
                self._record_rc003_direction_edge(usage, False)
                self._on_button_event(
                    button,
                    False,
                    event_source="hid_tap",
                    hid_usage=usage,
                )
            with self._button_mapping_lock:
                combo_modifier = key_mapping.button_combo_modifier(self._bindings)
            for usage in sorted(
                pressed,
                key=lambda candidate: (
                    frida_compat.TAP_USAGE_TO_BUTTON[candidate] != combo_modifier,
                    candidate,
                ),
            ):
                button = frida_compat.TAP_USAGE_TO_BUTTON[usage]
                self._logger.info(
                    "RC003 direct HID usage down: 0x%04x -> %s",
                    usage,
                    button,
                )
                self._record_rc003_direction_edge(usage, True)
                self._on_button_event(
                    button,
                    True,
                    event_source="hid_tap",
                    hid_usage=usage,
                )

    def _record_rc003_direction_edge(
        self,
        usage: int,
        is_pressed: bool,
    ) -> None:
        # The native report is already neutral before the kernel copy. Arming
        # the old global gate here could consume an unrelated keyboard arrow.
        if getattr(self._hid_report_tap, "native_copy_interception", False) is True:
            return
        key = frida_compat.TAP_DIRECTION_USAGE_TO_KEY.get(int(usage))
        if key is None:
            return
        vk, scan_code, extended = key
        with self._voice_key_physicalizer_lifecycle_lock:
            physicalizer = self._voice_key_physicalizer
            physicalizer_ready = self._voice_key_physicalizer_ready
        recorder = getattr(physicalizer, "record_rc003_direction_edge", None)
        if physicalizer_ready and callable(recorder):
            try:
                if recorder(vk, scan_code, extended, is_pressed):
                    return
            except Exception:
                self._logger.exception(
                    "RC003 global direction ownership failed; navigation fallback used"
                )
        element_navigation_control_windows.record_rc003_direction_edge(
            vk,
            scan_code,
            extended,
            is_pressed,
        )

    def _stop_input_channels(self) -> None:
        """Stop process-lifetime input resources exactly once at worker exit."""

        failures: List[str] = []
        with self._voice_key_physicalizer_lifecycle_lock:
            self._voice_key_physicalizer_stopping = True
            self._voice_key_physicalizer_generation += 1
            self._voice_key_physicalizer_degraded_retry_deadline = 0.0
            self._cancel_voice_key_physicalizer_retry_locked()
        with self._input_arbitration_lock:
            with self._raw_input_lifecycle_lock:
                self._raw_input_stopping = True
                self._raw_input_generation += 1
                self._cancel_raw_input_retry_locked()
            self._cancel_hid_tap_revert_restart_locked()
        with self._button_action_lock:
            self._button_input_release_retry_stopping = True
            self._cancel_button_input_release_retry_locked(reset_delay=False)
        with self._voice_shortcut.lock:
            self._voice_shortcut.retry_stopping = True
            self._voice_shortcut.cancel_release_retry(reset_delay=False)

        # Input loss is cancellation, not a click. Clear gesture timers before
        # listener shutdown emits forced releases, and release any Windows key
        # whose original down was deliberately allowed through during fallback.
        self._button_combos.reset()
        self._button_gestures.reset()
        with self._input_arbitration_lock:
            self._cancel_all_raw_fallback_hold_guards_locked()
            tracked_logical_buttons, _tracked_physical_buttons = (
                self._raw_fallback_tracked_button_ids_locked()
            )
            raw_fallback_physical_buttons = (
                self._take_raw_fallback_physical_buttons_locked(
                    set(self._raw_fallback_buttons_down)
                    | tracked_logical_buttons
                )
            )
            self._release_raw_fallback_keyups(
                raw_fallback_physical_buttons,
                reason="input_channels_stopping",
            )

        # Keep dispatch enabled until both listeners have emitted their forced
        # release edges. The HID tap is stopped first so its explicit disable
        # handshake restores the Windows path before Raw Input is closed. The
        # restart lock keeps a concurrent revert-triggered restart from
        # starting a replacement after this stop.
        with self._hid_tap_restart_lock:
            if self._hid_report_tap is not None:
                try:
                    self._hid_report_tap.stop()
                    self._hid_report_tap = None
                    self._set_runtime_input_state(hid_tap_state="stopped")
                except Exception:
                    self._set_runtime_input_state(hid_tap_state="failed_stopping")
                    self._logger.exception(
                        "cleanup: stopping the RC003 HID report tap failed"
                    )
                    failures.append(
                        "RC003 HID report tap did not stop; owner retained"
                    )
            else:
                self._set_runtime_input_state(hid_tap_state="stopped")

        keyboard_release_complete = True
        if not self._release_pending_button_keys():
            keyboard_release_complete = False
            failures.append(
                "ordinary button key safety release did not fully deliver; "
                "keyboard trackers retained"
            )
        if not self._release_pending_button_mouse():
            failures.append(
                "ordinary button mouse safety release did not fully deliver; "
                "state retained"
            )

        with self._voice_shortcut.lock:
            if self._voice_shortcut.pending_tokens is not None:
                if self._voice_shortcut.release_pending():
                    self._voice_shortcut.controller.cancel_pending()
                else:
                    keyboard_release_complete = False
                    failures.append(
                        "voice hotkey final safety release did not fully deliver; "
                        "keyboard trackers retained"
                    )

        if keyboard_release_complete:
            with self._raw_input_operation_lock:
                listener = self._hid_listener
                if listener is not None:
                    try:
                        listener.stop()
                        with self._raw_input_lifecycle_lock:
                            if self._hid_listener is listener:
                                self._hid_listener = None
                                self._raw_fallback_tracking_active = False
                        self._set_runtime_input_state(raw_input_state="stopped")
                    except Exception:
                        self._set_runtime_input_state(
                            raw_input_state="failed_stopping"
                        )
                        self._logger.exception(
                            "cleanup: stopping the Raw Input listener failed"
                        )
                        failures.append(
                            "Raw Input listener did not stop; owner retained"
                        )
                else:
                    self._set_runtime_input_state(raw_input_state="stopped")
        else:
            self._set_runtime_input_state(raw_input_state="retained_for_cleanup")

        with self._raw_input_lifecycle_lock:
            tracking, self._raw_tracking_listener = self._raw_tracking_listener, None
        if tracking is not None:
            try:
                tracking.stop()
            except Exception:
                self._logger.exception(
                    "cleanup: physical keyboard tracking listener did not stop"
                )

        with self._voice_shortcut.lock:
            self._accept_input_events = False

        with self._direct_hid_lock:
            self._direct_hid_usages.clear()
        with self._input_arbitration_lock:
            self._direct_hid_interception_ready = False
            self._direct_hid_interception_armed = False
            self._direct_hid_handover_waiting_for_neutral = False
            self._direct_hid_interception_armed_monotonic = None
            self._cancel_all_raw_fallback_hold_guards_locked()
            self._raw_fallback_buttons_down.clear()
            self._raw_fallback_physical_buttons_down.clear()
            self._raw_fallback_release_debts.clear()
            self._input_rearm_blocked_buttons.clear()
        self._key_detection_suppressed_buttons.clear()
        self._key_detection_suppression_deadlines.clear()
        with self._ordinary_mic_lock:
            self._ordinary_mic_sources_down.clear()
            self._ordinary_mic_late_sources_down.clear()
            self._ordinary_mic_late_source_deadlines.clear()
            self._ordinary_mic_sources_seen.clear()
            self._ordinary_mic_gesture_started_at = 0.0
            self._ordinary_mic_release_guard_until = 0.0
            self._ordinary_mic_gesture_active = False
        with self._key_detection_mic_lock:
            self._reset_key_detection_mic_gesture_locked()
        self._button_combos.reset()
        self._button_gestures.reset()

        physicalizer_state = "stopped"
        if keyboard_release_complete:
            with self._voice_key_physicalizer_operation_lock:
                with self._voice_key_physicalizer_lifecycle_lock:
                    physicalizer = self._voice_key_physicalizer
                if physicalizer is not None:
                    try:
                        physicalizer.stop()
                    except Exception:
                        physicalizer_state = "failed"
                        self._logger.exception(
                            "cleanup: stopping voice key physicalizer failed"
                        )
                        failures.append(
                            "voice key physicalizer did not stop; owner retained"
                        )
                    else:
                        with self._voice_key_physicalizer_lifecycle_lock:
                            if self._voice_key_physicalizer is physicalizer:
                                self._voice_key_physicalizer = None
        else:
            physicalizer_state = "failed"
        with self._voice_key_physicalizer_lifecycle_lock:
            self._voice_key_physicalizer_ready = False
        self._set_runtime_input_state(
            voice_key_physicalizer_state=physicalizer_state
        )

        if failures:
            raise CleanupIncompleteError(
                "cleanup could not release all owned input resources: "
                + "; ".join(failures)
            )

    async def _cleanup_once(self) -> None:
        """Release one BLE/voice/audio attempt without stopping input."""

        with self._runtime_status_lock:
            connection_state = self._runtime_connection_state
            self._runtime_battery_level = None
        if connection_state is bridge_runtime_status.BridgeConnectionState.CONNECTED:
            self._publish_runtime_status(
                bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
            )
        failures: List[str] = []
        with self._voice_shortcut.lock:
            self._accept_ble_events = False

        try:
            with self._voice_shortcut.lock:
                self._voice_audio_start_fallback_pending = False
                self._voice_raw_input_trigger_pending = False
                self._voice_audio_stream_active = False
                self._voice_audio_stop_processed = False
                self._voice_pcm_forwarding_enabled = False
                self._stop_local_voice_capture_locked()
                flush_result = self._voice_audio.flush("cleanup")
                if not flush_result.completed:
                    failures.append(
                        "audio playback queue did not flush; owner retained"
                    )
                self._unsolicited_mic_close_pending = False
                self._finish_voice_mic_gesture()
                self._drop_voice_delayed_press_locked()
                if self._voice_shortcut.pending_tokens is not None:
                    if self._voice_shortcut.release_pending():
                        self._voice_shortcut.controller.cancel_pending()
                    else:
                        failures.append(
                            "voice hotkey safety release did not fully deliver; state retained"
                        )
                else:
                    reset_action = self._voice_shortcut.controller.reset()
                    if reset_action is not None and not self._voice_shortcut.apply(
                        reset_action
                    ):
                        # _apply_voice_action() already logged the specific failure.
                        # reset() already cleared the controller's own pending
                        # state before we knew delivery would fail - restore it so
                        # a held shortcut isn't recorded as released while it may
                        # still be physically down (XRBM-019 review round 1 P1 #4).
                        self._voice_shortcut.controller.restore_pending(reset_action)
                        failures.append(
                            "voice hotkey release did not fully deliver; state retained"
                        )
                self._voice_focus_before = None
                self._voice_focus_provider = ""
                self._voice_focus_submit_method = ""
                self._finish_voice_diagnostic_attempt(
                    "unknown",
                    reason="connection_cleanup",
                )
        except Exception:
            self._logger.exception("cleanup: releasing the voice hotkey failed")
            failures.append("voice hotkey cleanup failed; state retained")

        if not self._release_pending_button_inputs():
            failures.append(
                "ordinary button input safety release did not fully deliver; "
                "state retained"
            )

        if self._ble_session is not None:
            try:
                await self._ble_session.close()
                self._ble_session = None
            except Exception:
                self._logger.exception("cleanup: closing the BLE session failed")
                failures.append("BLE session did not fully close; owner retained")
                # self._ble_session is intentionally NOT cleared here either.

        playback_writer_stopped = True
        if self._voice_audio.writer is not None:
            playback_writer_stopped = self._voice_audio.stop_writer()
            if not playback_writer_stopped:
                failures.append(
                    "audio playback writer did not stop; owner retained"
                )

        if self._voice_audio.sink is not None and playback_writer_stopped:
            try:
                self._voice_audio.close_sink()
            except Exception:
                self._logger.exception("cleanup: closing audio playback failed")
                failures.append("audio playback did not fully close; owner retained")
                # self._voice_audio.sink is intentionally NOT cleared here either -
                # it owns a PortAudio stream; discarding the reference would
                # hide an incompletely closed resource and let a reconnect
                # open a second sink over it (XRBM-019 review round 1 P1
                # #5).

        if not failures:
            with self._voice_shortcut.lock:
                self._apply_pending_voice_settings_if_idle_locked()

        self._logger.info("cleanup: attempted release of hotkey state and BLE/HID/audio")

        if failures:
            raise CleanupIncompleteError(
                "cleanup could not release all owned resources: " + "; ".join(failures)
            )

    def _on_disconnected(self, *, _ble_generation: Optional[int] = None) -> None:
        if not self._accepts_ble_callback(_ble_generation):
            return
        self._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
        )
        self._logger.info("BLE reported disconnected; requesting reconnect")
        self._supervisor.request_reconnect()

    def _on_session_error(
        self,
        exc: BaseException,
        *,
        _ble_generation: Optional[int] = None,
    ) -> None:
        if not self._accepts_ble_callback(_ble_generation):
            return
        self._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.RETRY_WAIT
        )
        self._logger.info("ATVV protocol error, requesting reconnect: %s", exc)
        self._supervisor.request_reconnect()

    def _on_battery_level(
        self,
        level: int,
        *,
        _ble_generation: Optional[int] = None,
    ) -> None:
        if not self._accepts_ble_callback(_ble_generation):
            return
        self._set_runtime_battery_level(level)

    def _accepts_ble_callback(self, generation: Optional[int]) -> bool:
        """Reject callbacks left behind by an older BLE session."""

        return self._accept_ble_events and (
            generation is None or generation == self._ble_callback_generation
        )

    def _primary_button_action(self, button_id: str) -> key_mapping.ButtonAction:
        return key_mapping.button_action_for(
            self._bindings,
            button_id,
            key_mapping.ButtonTrigger.SINGLE_CLICK,
        )
