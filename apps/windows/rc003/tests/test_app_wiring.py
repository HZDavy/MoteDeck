"""App-wiring/thread-safety tests for app.py's RC003App (XRBM-018 DoD 4).

``RC003App.__init__`` is safe to construct off Windows: config/hotkey/
voice-controller/supervisor setup is pure Python, and the real Win32/WinRT
calls only happen inside ``_connect_once()``/the HID listener, which these
tests never call. Constructing a real ``RC003App`` and substituting its
BLE-session/playback collaborators with lightweight recorders lets these
tests exercise the actual wiring DECISIONS app.py makes - host hotkey
failure suppresses MIC_OPEN, playback write failure fails closed and
requests a reconnect, and that request happens correctly from a real
worker thread - without any Windows API, matching this project's existing
"test contracts, not implementation-mirroring fakes" approach.

The host-hotkey-unavailable case doesn't even need mocking: off Windows,
win32_input.py's ``_require_windows()`` genuinely raises
``Win32InputUnavailableError`` on every call, so it exercises the exact
"hotkey failed to deliver" branch app.py must fail closed on - not a stand-
in for it.
"""

import array
import asyncio
from contextlib import ExitStack
import json
from dataclasses import replace
import logging
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import app as app_module
from ovb_rc003 import (
    bridge_runtime_status,
    config,
    frida_compat,
    identity,
    key_detection_bridge,
    key_mapping,
    logging_setup,
    app_profiles,
    preset_switch_windows,
    raw_input_windows,
    settings_ui,
    voice_gesture,
    win32_input,
    win32_keys,
)
from ovb_rc003.atvv_session import AudioStarted, AudioStopped, MicButtonPressed

DEFAULT_VOICE_TOKENS = ("ralt",)


def _run(coro):
    # Explicitly closing the loop (XRBM-018 review round 2 evidence: a
    # ResourceWarning for an unclosed test event loop) rather than letting
    # it be garbage-collected.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeBleSession:
    def __init__(self, close_raises=False):
        self.mic_open_calls = 0
        self.mic_close_calls = 0
        self.close_raises = close_raises
        self.close_calls = 0
        self.audio_arrival_watermark = -1

    def send_mic_open_threadsafe(self):
        self.mic_open_calls += 1

    def send_mic_close_threadsafe(self):
        self.mic_close_calls += 1

    async def close(self):
        self.close_calls += 1
        if self.close_raises:
            raise RuntimeError("simulated BLE worker thread that did not stop")


class _FakeSignal:
    """Minimal stand-in for VokieBridge's Qt signals."""

    def __init__(self):
        self._targets = []

    def connect(self, target):
        self._targets.append(target)

    def emit(self):
        for target in list(self._targets):
            target()


class _FakePluginBridge:
    """Stand-in for VokieBridge's audio hand-off surface."""

    def __init__(self, connected=True):
        self._connected = connected
        self.audio_starts = []
        self.audio_frames = []
        self.audio_stops = 0
        self.sessionAccepted = _FakeSignal()

    def connected(self):
        return self._connected

    def send_audio_start(self, mode):
        self.audio_starts.append(mode)
        return True

    def send_audio(self, pcm):
        self.audio_frames.append(pcm)
        return True

    def send_audio_stop(self):
        self.audio_stops += 1
        return True


class _FakeHidListener:
    def __init__(self, stop_raises=False):
        self.stop_raises = stop_raises
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1
        if self.stop_raises:
            raise RuntimeError("simulated Raw Input listener thread that did not stop")


class _FakeInputOwner:
    def __init__(self, stop_raises=False):
        self.stop_raises = stop_raises
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1
        if self.stop_raises:
            raise RuntimeError("simulated input owner that did not stop")


class _FakeVoicePhysicalizer:
    def __init__(
        self,
        *,
        start_error=None,
        immediate_exit=False,
        accepts_new_down=True,
        stop_error=None,
    ):
        self.start_error = start_error
        self.immediate_exit = immediate_exit
        self.is_running = False
        self.accepts_new_down = accepts_new_down
        self.stop_error = stop_error
        self.start_calls = 0
        self.stop_calls = 0
        self.callback = None
        self.health_callback = None
        self.tracker_generation = 1
        self.installation_epoch = 1

    def set_tracking_lost_callback(self, callback):
        self.callback = callback

    def set_health_failure_callback(self, callback):
        self.health_callback = callback

    def start(self):
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        self.is_running = not self.immediate_exit
        self.accepts_new_down = self.is_running

    def stop(self):
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error
        self.is_running = False
        self.accepts_new_down = False

    def emit_loss(self):
        if self.callback is not None:
            self.callback()


class _ManualTimer:
    def __init__(self, callback):
        self.callback = callback
        self.cancelled = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


class _FakePlaybackSink:
    def __init__(self, fail_write=False, close_raises=False):
        self.fail_write = fail_write
        self.close_raises = close_raises
        self.write_calls = []
        self.closed = False
        self.close_calls = 0

    def write(self, samples):
        self.write_calls.append(samples)
        if self.fail_write:
            raise OSError("simulated PortAudio write failure")

    def close(self):
        self.close_calls += 1
        if self.close_raises:
            raise RuntimeError("simulated PortAudio stream that did not close")
        self.closed = True


class _FakeHidListenerForFailedStart:
    """XRBM-019 review round 1 P1 #3: a fake standing in for
    RawInputButtonListener itself (not just its ``start()`` outcome), so
    ``_start_hid_listener()`` can be exercised end-to-end off Windows -
    ``is_running`` is the source of truth a failed ``start()`` must consult
    before deciding whether to keep or discard the owner reference.
    """

    def __init__(self, is_running_after_failed_start):
        self._is_running_after_failed_start = is_running_after_failed_start
        self.start_calls = 0

    @property
    def is_running(self):
        return self._is_running_after_failed_start

    def start(self, device_path):
        self.start_calls += 1
        raise app_module.raw_input_windows.RawInputUnavailableError("simulated failed start")

    def stop(self):
        pass


class _FakeRecoveringRawListener:
    def __init__(
        self,
        *,
        start_error=None,
        running_after_start=True,
        running_after_error=False,
        stop_error=None,
    ):
        self.start_error = start_error
        self.running_after_start = running_after_start
        self.running_after_error = running_after_error
        self.stop_error = stop_error
        self.is_running = False
        self.start_calls = 0
        self.stop_calls = 0
        self.sourced_callback = None
        self.raw_callback = None
        self.removed_callback = None
        self.corruption_callback = None
        self.tracking_lost_callback = None
        self.physical_bindings = None

    def set_sourced_button_event_callback(self, callback):
        self.sourced_callback = callback

    def set_raw_event_callback(self, callback):
        self.raw_callback = callback

    def set_device_removed_callback(self, callback):
        self.removed_callback = callback

    def set_input_corruption_callback(self, callback):
        self.corruption_callback = callback

    def set_physical_keyboard_tracking_lost_callback(self, callback):
        self.tracking_lost_callback = callback

    def set_physical_bindings(self, bindings):
        self.physical_bindings = dict(bindings)

    def start(self, _device_path):
        self.start_calls += 1
        if self.start_error is not None:
            self.is_running = self.running_after_error
            raise self.start_error
        self.is_running = self.running_after_start

    def stop(self):
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error
        self.is_running = False


def _build_app(tmp_root: Path) -> "app_module.RC003App":
    # Redirect config_root (and therefore logging_setup's log directory) at
    # a throwaway temp directory instead of the real machine's config/log
    # location - RC003App.__init__ always calls config.config_root()/
    # logging_setup.get_logger(), neither of which touch any Windows API.
    original = config.config_root
    config.config_root = lambda: tmp_root
    try:
        return app_module.RC003App()
    finally:
        config.config_root = original


def _build_app_with_owned_loop(tmp_root: Path):
    """Like _build_app(), but explicitly creates a fresh event loop and sets
    it as this thread's current loop before constructing the app (XRBM-026).

    RC003App.__init__ builds a ConnectionSupervisor, whose __init__ captures
    ``loop or asyncio.get_event_loop()`` (connection_supervisor.py) - called
    here synchronously, off any running loop. Without an owned loop already
    set, that would silently create-and-cache this thread's implicit default
    loop the first time any test builds an RC003App - a loop nothing then
    ever closes (see EventLoopOwnershipRegressionTests for the exact red
    evidence this reproduces and fixes). Returns ``(app, loop)``; the caller
    owns ``loop`` and must ``asyncio.set_event_loop(None)`` then
    ``loop.close()`` it when done - exactly mirroring the real app's own
    ``asyncio.run(_run())`` construction, which owns and closes its loop too.
    """

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = _build_app(tmp_root)
    return app, loop


def _stamp_voice_key(config_data, slot, **fields):
    """Record one voice key in place the way the voice page would.

    ``voice_key_configs`` is authoritative now, so stamping the legacy
    ``voice_slot_hotkeys`` mirror alone no longer reaches the runtime. A slot
    that is not in the list yet is appended, inheriting the first key's
    microphone mode - that is what the old single global source meant.
    """

    if config.voice_key_config(config_data, slot) is None:
        entries = config.voice_key_configs(config_data)
        first = entries[0]
        config_data["voice_key_configs"] = [
            *entries,
            {
                "id": int(slot),
                "name": "",
                "hotkey": "",
                "source": first["source"],
                "device": first["device"],
            },
        ]
    config_data.update(
        config.update_voice_key_config(config_data, slot, **fields)
    )
    return config_data


def _stamp_voice_mode(config_data, source, device=""):
    """Point every configured voice key at one microphone source."""

    for entry in config.voice_key_configs(config_data):
        _stamp_voice_key(config_data, entry["id"], source=source, device=device)
    config_data["voice_input_source"] = source
    config_data["voice_local_input_device"] = device
    return config_data


class _AppWiringTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # XRBM-026 red evidence (real Windows run 29644660267): 425 tests
        # passed, then the process printed an ignored "unclosed event loop"
        # ResourceWarning for a ProactorEventLoop plus two unclosed self-pipe
        # sockets - AFTER unittest's own summary, so -W error::ResourceWarning
        # never sees it and the step still exits 0 (a ResourceWarning-turned-
        # exception raised inside a __del__/finalizer is unraisable; Python
        # can only print it via sys.unraisablehook, never let it change an
        # already-computed exit code - see EventLoopOwnershipRegressionTests
        # below for a deterministic, isolated-subprocess reproduction).
        # _build_app_with_owned_loop() above threads a per-test owned loop
        # into ConnectionSupervisor instead - never the ambient, never-closed
        # default the old bare _build_app() call left behind.
        self.app, self._loop = _build_app_with_owned_loop(Path(self._tmp.name))
        # Keyboard auto-repeat is a per-machine setting. Pin it so repeat
        # cadence assertions stay deterministic instead of host-dependent.
        self.app._keyboard_repeat_interval_seconds = 0.065
        # This wiring harness represents an explicitly selected device. PnP
        # identity isolation is tested separately in test_remote_selection.
        self.app._selected_remote_key = "a" * 64
        self.app._remote_profile = app_module.remote_selection.RC003_PROFILE
        # RC003 replays only the shortcut the user recorded for each voice slot,
        # so the wiring harness has to record slot 1 the same way the voice page
        # would before the mapping pipeline can accept it. A fresh install now
        # records nothing and stays inert, so the harness both stamps the saved
        # config and mirrors the recorded chord into the live runtime session
        # (RC003App.__init__ built it empty from the unrecorded on-disk default).
        self.app._config["voice_slot_hotkeys"] = {"1": "ralt", "2": ""}
        self.app._config["voice_hotkey"] = "ralt"
        _stamp_voice_key(self.app._config, 1, hotkey="ralt")
        self.app._voice_shortcut.hotkey = app_module.hotkey.HotkeySpec.parse("ralt")
        selection_patch = mock.patch(
            "ovb_rc003.remote_selection.selected_raw_path",
            side_effect=lambda paths, _key, **_kwargs: app_module.hid_identity.select_single_device_path(paths),
        )
        selection_patch.start()
        self.addCleanup(selection_patch.stop)
        self.app._voice_audio.sink = _FakePlaybackSink()
        self.app._ble_session = _FakeBleSession()
        self.app._accept_ble_events = True
        # Most wiring tests exercise the product's mapped-button path. That
        # path is available only after the privileged HID endpoint confirms
        # that it intercepted a real report before Windows saw it.
        self.app._direct_hid_interception_ready = True
        # The real application starts both physical-input trackers before it
        # accepts mapped button events. These unit tests construct RC003App
        # directly, so establish the same ready state explicitly.
        raw_input_windows._set_physical_keyboard_tracker_active(True)
        self.app._voice_key_physicalizer_ready = True
        self.app._raw_windows_key_down_query = lambda _vk_code: False
        self._button_input_release_timers = []

        def button_input_release_timer_factory(delay, callback):
            timer = _ManualTimer(callback)
            timer.delay = delay
            self._button_input_release_timers.append(timer)
            return timer

        self.app._button_input_release_timer_factory = (
            button_input_release_timer_factory
        )
        self._voice_hotkey_release_timers = []

        def voice_hotkey_release_timer_factory(delay, callback):
            timer = _ManualTimer(callback)
            timer.delay = delay
            self._voice_hotkey_release_timers.append(timer)
            return timer

        self.app._voice_shortcut.timer_factory = (
            voice_hotkey_release_timer_factory
        )

    def tearDown(self):
        with self.app._button_action_lock:
            self.app._button_input_release_retry_stopping = True
            self.app._cancel_button_input_release_retry_locked(reset_delay=False)
        with self.app._voice_shortcut.lock:
            self.app._cancel_voice_hold_watchdog_locked()
            self.app._voice_shortcut.retry_stopping = True
            self.app._voice_shortcut.cancel_release_retry(reset_delay=False)
        with self.app._voice_key_physicalizer_lifecycle_lock:
            self.app._voice_key_physicalizer_stopping = True
            self.app._cancel_voice_key_physicalizer_retry_locked()
        with self.app._raw_input_lifecycle_lock:
            self.app._raw_input_stopping = True
            self.app._cancel_raw_input_retry_locked()
        app_module.voice_key_physicalizer_windows._set_physical_tracker_active(
            False
        )
        raw_input_windows._set_physical_keyboard_tracker_active(False)
        playback_writer = self.app._voice_audio.writer
        if playback_writer is not None:
            playback_writer.flush(1.0)
            playback_writer.stop(1.0)
            self.app._voice_audio.writer = None
        # XRBM-023: logging_setup.get_logger() configures its FileHandler
        # exactly once per process (module-global ``_configured``) and never
        # closes it - correct for a real long-running app, but in this suite
        # it leaves an open handle inside THIS test's temp directory. Windows
        # (unlike POSIX, where you can unlink a file while a handle is still
        # open on it) refuses to delete a directory containing an open file
        # handle, so ``self._tmp.cleanup()`` below would raise on Windows
        # once any prior test in this class had already configured the
        # logger. Close/remove the handler and reset the one-time-config
        # flag first so every test starts and ends with no logging state
        # leaked into the next one.
        logger = logging.getLogger(logging_setup.LOGGER_NAME)
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        logging_setup._configured = False
        self._tmp.cleanup()
        # XRBM-026: close the loop this test owns (see setUp()) and detach
        # it as the thread's current loop, so its own eventual __del__ finds
        # is_closed() already True and stays silent - and so the NEXT test's
        # setUp() cannot mistake this now-closed loop for a live ambient one.
        asyncio.set_event_loop(None)
        self._loop.close()

    def _drain_event_loop(self):
        self._loop.run_until_complete(asyncio.sleep(0))

    def _flush_playback(self):
        writer = self.app._voice_audio.writer
        self.assertIsNotNone(writer)
        return writer.flush(1.0)

    def _save_voice_settings(self, *, hotkey_text: str) -> None:
        refreshed = config.load_config(self.app._config_path)
        refreshed["voice_hotkey"] = hotkey_text
        refreshed["voice_slot_hotkeys"] = {
            **dict(refreshed.get("voice_slot_hotkeys") or {}),
            "1": hotkey_text,
        }
        _stamp_voice_key(refreshed, 1, hotkey=hotkey_text)
        config.save_config(self.app._config_path, refreshed)

    def _save_voice_slot_settings(self, *, slot: int, hotkey_text: str) -> None:
        refreshed = config.load_config(self.app._config_path)
        slot_hotkeys = dict(refreshed.get("voice_slot_hotkeys") or {})
        slot_hotkeys[str(slot)] = hotkey_text
        refreshed["voice_slot_hotkeys"] = slot_hotkeys
        _stamp_voice_key(refreshed, slot, hotkey=hotkey_text)
        config.save_config(self.app._config_path, refreshed)

    def _set_voice_mapping(self, button_id: str) -> None:
        bindings = self.app._bindings["bindings"]
        for existing_button, raw_action in list(bindings.items()):
            try:
                action = key_mapping.ButtonAction.from_dict(raw_action)
            except (KeyError, TypeError, ValueError):
                continue
            if key_mapping.is_voice_action(action):
                bindings[existing_button] = key_mapping.ButtonAction(
                    key_mapping.ActionKind.DISABLED
                ).to_dict()
        bindings[button_id] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD
        ).to_dict()


class StartupIdentityLoggingTests(unittest.TestCase):
    def test_frozen_startup_logs_version_runtime_and_package_directory(self):
        logger = mock.Mock(spec=logging.Logger)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                package_dir = Path(tmp) / "RemoteMicRC003-localtest"
                executable = package_dir / "RemoteMicRC003.exe"
                with mock.patch.object(
                    logging_setup, "get_logger", return_value=logger
                ), mock.patch.object(
                    app_module.sys, "frozen", True, create=True
                ), mock.patch.object(
                    app_module.sys, "executable", str(executable)
                ):
                    _build_app(Path(tmp))
        finally:
            asyncio.set_event_loop(None)
            loop.close()

        logger.info.assert_any_call(
            "startup: app identity: version=%s runtime=%s package=%s",
            app_module.__version__,
            "frozen",
            package_dir.name,
        )


class LiveSettingsReloadTests(_AppWiringTestCase):
    def test_reload_never_applies_another_entity_settings(self):
        from ovb_rc003 import remote_selection
        original = {"schema": 1, "active": "a" * 64, "devices": [
            {"key": "a" * 64, "profile": "xiaomi-rc003"},
            {"key": "b" * 64, "profile": "xiaomi-rc003"}]}
        self.app._config[remote_selection.KEY] = original
        incoming = dict(self.app._config)
        incoming[remote_selection.KEY] = dict(original, active="b" * 64)
        config.save_config(self.app._config_path, incoming)
        old_mapping = self.app._bindings
        self.app._reload_settings_if_changed()
        self.assertEqual(remote_selection.active_key(self.app._config), "a" * 64)
        self.assertIs(self.app._bindings, old_mapping)
        self.assertIsNone(self.app._pending_config)

    def _use_manual_gesture_timers(self):
        timers = []

        def timer_factory(_delay, callback):
            timer = _ManualTimer(callback)
            timers.append(timer)
            return timer

        self.app._button_gestures._timer_factory = timer_factory
        return timers

    def _save_button_bindings(self, bindings):
        config.save_key_bindings(self.app._bindings_path, bindings)
        self.app._reload_settings_if_changed()

    def test_zero_voice_blank_shortcuts_construct_without_crashing(self):
        stored_config = config.default_config()
        stored_config["voice_hotkey"] = ""
        stored_config["voice_hotkeys"] = {"toggle": "", "hold": ""}
        config.save_config(self.app._config_path, stored_config)
        stored_bindings = config.default_key_bindings()
        stored_bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()
        config.save_key_bindings(self.app._bindings_path, stored_bindings)

        reconstructed = _build_app(self.app._config_root)

        self.assertEqual(reconstructed._config["voice_hotkey"], "")
        self.assertEqual(reconstructed._configured_voice_buttons(), [])
        # No fabricated wake shortcut: the runtime session has none until the
        # user records 语音一/语音二.
        self.assertIsNone(reconstructed._voice_shortcut.hotkey)

    def test_voice_mode_and_hotkey_reload_while_idle(self):
        self._save_voice_settings(hotkey_text="ctrl+l")

        self.app._reload_settings_if_changed()

        self.assertEqual(self.app._voice_shortcut.hotkey.serialize(), "ctrl+l")
        self.assertIsNone(self.app._pending_voice_settings)

    def test_voice_slot_without_its_own_button_inherits_the_trigger_inject_mode(self):
        # 短按切到语音二/三时那一槽没有自己的按钮，注入方式必须沿用话筒键
        # 配置的驱动级注入；否则会退回 SendInput，右 Alt 这类宿主收不到键。
        bindings = config.default_key_bindings()
        bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD,
            inject_mode=key_mapping.INJECT_MODE_INTERCEPTION,
        ).to_dict()
        self._save_button_bindings(bindings)
        self._save_voice_slot_settings(slot=2, hotkey_text="ralt")
        self.app._reload_settings_if_changed()

        self.assertEqual(
            self.app._voice_slot_inject_mode(2),
            key_mapping.INJECT_MODE_INTERCEPTION,
        )

    def test_voice_slot_bound_to_its_own_button_keeps_its_inject_mode(self):
        bindings = config.default_key_bindings()
        bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD,
            inject_mode=key_mapping.INJECT_MODE_INTERCEPTION,
        ).to_dict()
        bindings["bindings"]["tv"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD_2,
            inject_mode=key_mapping.DEFAULT_INJECT_MODE,
        ).to_dict()
        self._save_button_bindings(bindings)
        self.app._reload_settings_if_changed()

        self.assertEqual(
            self.app._voice_slot_inject_mode(2),
            key_mapping.DEFAULT_INJECT_MODE,
        )

    def test_physical_bindings_reload_updates_the_live_raw_input_listener(self):
        listener = mock.Mock()
        self.app._hid_listener = listener
        refreshed = config.load_key_bindings(self.app._bindings_path)
        refreshed["physical_bindings"] = {"keyboard:00ff:0070:0002": "back"}
        config.save_key_bindings(self.app._bindings_path, refreshed)

        self.app._reload_settings_if_changed()

        listener.set_physical_bindings.assert_called_once_with(
            {"keyboard:00ff:0070:0002": "back"}
        )

    def test_listener_start_cannot_overwrite_newer_physical_bindings(self):
        first_sync_started = threading.Event()
        allow_first_sync = threading.Event()
        second_sync_started = threading.Event()
        calls = []
        calls_lock = threading.Lock()

        class BlockingBindingsListener(_FakeRecoveringRawListener):
            def set_physical_bindings(self, bindings):
                payload = dict(bindings)
                with calls_lock:
                    call_index = len(calls)
                    calls.append(payload)
                if call_index == 0:
                    first_sync_started.set()
                    allow_first_sync.wait(1.0)
                else:
                    second_sync_started.set()
                self.physical_bindings = payload

        listener = BlockingBindingsListener()
        self.app._bindings["physical_bindings"] = {"hid:race": "up"}
        refreshed = config.load_key_bindings(self.app._bindings_path)
        refreshed["physical_bindings"] = {"hid:race": "down"}
        config.save_key_bindings(self.app._bindings_path, refreshed)
        errors = []

        def start_listener():
            try:
                self.app._start_hid_listener()
            except BaseException as exc:  # noqa: BLE001 - surfaced by assertion
                errors.append(exc)

        def reload_settings():
            try:
                self.app._reload_settings_if_changed()
            except BaseException as exc:  # noqa: BLE001 - surfaced by assertion
                errors.append(exc)

        with mock.patch.object(
            app_module.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=["fake-path"],
        ), mock.patch.object(
            app_module.hid_identity,
            "select_single_device_path",
            return_value="fake-path",
        ), mock.patch.object(
            app_module.raw_input_windows,
            "RawInputButtonListener",
            return_value=listener,
        ):
            start_worker = threading.Thread(target=start_listener)
            start_worker.start()
            self.assertTrue(first_sync_started.wait(1.0))

            reload_worker = threading.Thread(target=reload_settings)
            reload_worker.start()
            second_sync_started.wait(0.1)
            allow_first_sync.set()
            start_worker.join(1.0)
            reload_worker.join(1.0)

        self.assertFalse(start_worker.is_alive())
        self.assertFalse(reload_worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            self.app._bindings["physical_bindings"],
            {"hid:race": "down"},
        )
        self.assertEqual(listener.physical_bindings, {"hid:race": "down"})
        self.assertEqual(calls[-1], {"hid:race": "down"})

    def test_deferred_physical_bindings_update_when_voice_becomes_idle(self):
        listener = mock.Mock()
        self.app._hid_listener = listener
        self.app._voice_shortcut.controller.on_mic_button_pressed()
        refreshed = config.load_key_bindings(self.app._bindings_path)
        refreshed["physical_bindings"] = {"hid:1234": "up"}
        config.save_key_bindings(self.app._bindings_path, refreshed)

        self.app._reload_settings_if_changed()
        listener.set_physical_bindings.assert_not_called()
        self.app._voice_shortcut.controller.on_mic_button_released()
        with self.app._voice_shortcut.lock:
            self.app._apply_pending_voice_settings_if_idle_locked()

        listener.set_physical_bindings.assert_called_once_with({"hid:1234": "up"})

    def test_audio_only_trigger_reloads_voice_settings_before_dispatch(self):
        self._set_voice_mapping("mic")
        self._save_voice_settings(hotkey_text="ctrl+l")
        delivered = []

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: delivered.append(tokens),
        ):
            self.app._on_control_event(AudioStarted(session_id=1))

        self.assertEqual(self.app._voice_shortcut.hotkey.serialize(), "ctrl+l")
        self.assertEqual(delivered, [("ctrl", "l")])

    def test_voice_settings_reload_is_deferred_until_active_hold_releases(self):
        self.app._voice_shortcut.controller.on_mic_button_pressed()
        self._save_voice_settings(hotkey_text="ctrl+l")

        self.app._reload_settings_if_changed()

        self.assertEqual(self.app._voice_shortcut.hotkey.serialize(), "ralt")
        self.assertIsNotNone(self.app._pending_voice_settings)

        self.app._voice_shortcut.controller.on_mic_button_released()
        with self.app._voice_shortcut.lock:
            self.app._apply_pending_voice_settings_if_idle_locked()

        self.assertEqual(self.app._voice_shortcut.hotkey.serialize(), "ctrl+l")
        self.assertIsNone(self.app._pending_voice_settings)

    def test_invalid_voice_settings_keep_the_last_valid_runtime_values(self):
        refreshed = config.load_config(self.app._config_path)
        refreshed["voice_hotkey"] = "ctrl"
        config.save_config(self.app._config_path, refreshed)

        self.app._reload_settings_if_changed()

        self.assertEqual(self.app._voice_shortcut.hotkey.serialize(), "ralt")
        self.assertIsNone(self.app._pending_voice_settings)

    def test_delayed_single_click_keeps_the_mapping_owned_at_first_press(self):
        timers = self._use_manual_gesture_timers()
        original = config.default_key_bindings()
        original["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()
        original["secondary_bindings"]["up"] = {
            "double_click": key_mapping.ButtonAction(
                key_mapping.ActionKind.RETURN
            ).to_dict()
        }
        self._save_button_bindings(original)

        self.app._on_button_event("up", True, event_source="hid")
        self.app._on_button_event("up", False, event_source="hid")
        changed = config.load_key_bindings(self.app._bindings_path)
        changed["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.MOUSE_LEFT_CLICK
        ).to_dict()
        self._save_button_bindings(changed)
        self.assertIsNotNone(self.app._pending_bindings)

        with mock.patch.object(win32_input, "send_escape") as old_action, mock.patch.object(
            win32_input, "send_mouse_button_click"
        ) as new_action:
            timers[0].fire()

        old_action.assert_called_once_with()
        new_action.assert_not_called()
        self.assertIsNone(self.app._pending_bindings)
        self.assertEqual(
            self.app._bindings["bindings"]["up"]["kind"],
            key_mapping.ActionKind.MOUSE_LEFT_CLICK.value,
        )

    def test_delayed_callback_reservation_blocks_a_concurrent_mapping_swap(self):
        timers = self._use_manual_gesture_timers()
        original = config.default_key_bindings()
        original["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()
        original["secondary_bindings"]["up"] = {
            "double_click": key_mapping.ButtonAction(
                key_mapping.ActionKind.RETURN
            ).to_dict()
        }
        self._save_button_bindings(original)
        self.app._on_button_event("up", True, event_source="hid")
        self.app._on_button_event("up", False, event_source="hid")

        changed = config.load_key_bindings(self.app._bindings_path)
        changed["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.MOUSE_LEFT_CLICK
        ).to_dict()
        config.save_key_bindings(self.app._bindings_path, changed)

        entered = threading.Event()
        release = threading.Event()
        original_trigger = self.app._button_gestures._on_trigger

        def blocking_trigger(button_id, trigger):
            entered.set()
            release.wait(1.0)
            original_trigger(button_id, trigger)

        self.app._button_gestures._on_trigger = blocking_trigger
        with mock.patch.object(win32_input, "send_escape") as old_action, mock.patch.object(
            win32_input, "send_mouse_button_click"
        ) as new_action:
            worker = threading.Thread(target=timers[0].fire)
            worker.start()
            self.assertTrue(entered.wait(1.0))

            self.app._reload_settings_if_changed()
            self.assertIsNotNone(self.app._pending_bindings)
            release.set()
            worker.join(1.0)

        self.assertFalse(worker.is_alive())
        old_action.assert_called_once_with()
        new_action.assert_not_called()
        self.assertIsNone(self.app._pending_bindings)

    def test_mapping_swap_serializes_with_a_new_physical_gesture(self):
        timers = self._use_manual_gesture_timers()
        original = config.default_key_bindings()
        original["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()
        original["secondary_bindings"]["up"] = {
            "double_click": key_mapping.ButtonAction(
                key_mapping.ActionKind.RETURN
            ).to_dict()
        }
        self._save_button_bindings(original)

        changed = config.load_key_bindings(self.app._bindings_path)
        changed["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.MOUSE_LEFT_CLICK
        ).to_dict()
        config.save_key_bindings(self.app._bindings_path, changed)

        original_reload = self.app._reload_settings_if_changed
        input_passed_reload = threading.Event()
        allow_input_dispatch = threading.Event()
        input_dispatching = threading.Event()
        reload_checked_idle = threading.Event()
        allow_mapping_swap = threading.Event()

        def routed_reload():
            if threading.current_thread().name == "mapping-input":
                input_passed_reload.set()
                allow_input_dispatch.wait(1.0)
                input_dispatching.set()
                return
            original_reload()

        original_idle = self.app._ordinary_button_mappings_idle

        def pause_after_idle_check():
            result = original_idle()
            reload_checked_idle.set()
            allow_mapping_swap.wait(1.0)
            return result

        with mock.patch.object(
            self.app, "_reload_settings_if_changed", side_effect=routed_reload
        ), mock.patch.object(
            self.app,
            "_ordinary_button_mappings_idle",
            side_effect=pause_after_idle_check,
        ):
            input_worker = threading.Thread(
                target=self.app._on_button_event,
                args=("up", True),
                kwargs={"event_source": "hid"},
                name="mapping-input",
            )
            input_worker.start()
            self.assertTrue(input_passed_reload.wait(1.0))

            reload_worker = threading.Thread(target=original_reload)
            reload_worker.start()
            self.assertTrue(reload_checked_idle.wait(1.0))
            allow_input_dispatch.set()
            self.assertTrue(input_dispatching.wait(1.0))
            self.assertTrue(input_worker.is_alive())

            allow_mapping_swap.set()
            reload_worker.join(1.0)
            input_worker.join(1.0)

        self.assertFalse(reload_worker.is_alive())
        self.assertFalse(input_worker.is_alive())
        self.app._on_button_event("up", False, event_source="hid")
        with mock.patch.object(win32_input, "send_mouse_button_click") as new_action:
            timers[0].fire()
        new_action.assert_called_once_with("left")

    def test_long_press_keeps_old_mapping_until_physical_release(self):
        timers = self._use_manual_gesture_timers()
        original = config.default_key_bindings()
        original["secondary_bindings"]["ok"] = {
            "long_press": key_mapping.ButtonAction(
                key_mapping.ActionKind.ESCAPE
            ).to_dict()
        }
        self._save_button_bindings(original)

        self.app._on_button_event("ok", True, event_source="hid")
        changed = config.load_key_bindings(self.app._bindings_path)
        changed["secondary_bindings"]["ok"]["long_press"] = (
            key_mapping.ButtonAction(
                key_mapping.ActionKind.MOUSE_LEFT_CLICK
            ).to_dict()
        )
        self._save_button_bindings(changed)

        with mock.patch.object(win32_input, "send_escape") as old_action, mock.patch.object(
            win32_input, "send_mouse_button_click"
        ) as new_action:
            timers[0].fire()
            self.assertIsNotNone(self.app._pending_bindings)
            self.app._on_button_event("ok", False, event_source="hid")

        old_action.assert_called_once_with()
        new_action.assert_not_called()
        self.assertIsNone(self.app._pending_bindings)

    def test_repeat_keeps_old_mapping_for_the_whole_hold(self):
        timers = self._use_manual_gesture_timers()
        original = config.default_key_bindings()
        self._save_button_bindings(original)

        with mock.patch.object(
            win32_input, "send_arrow_up"
        ) as old_action, mock.patch.object(
            win32_input, "send_key_combo_tap"
        ) as new_action:
            self.app._on_button_event("up", True, event_source="hid")
            changed = config.load_key_bindings(self.app._bindings_path)
            changed["bindings"]["up"] = {"kind": "mouse_wheel_up", "keys": []}
            self._save_button_bindings(changed)
            timers[0].fire()
            self.app._on_button_event("up", False, event_source="hid")

        # The click-repeat hold captures the action at press time, so both the
        # initial tap and the repeat replay the original mapping.
        self.assertEqual(old_action.call_count, 2)
        old_action.assert_called_with()
        new_action.assert_not_called()
        self.assertIsNone(self.app._pending_bindings)

    def test_legacy_combo_change_never_creates_an_active_combo(self):
        original = config.default_key_bindings()
        original["combo_bindings"] = {
            "modifier": "tv",
            "bindings": {
                "up": key_mapping.ButtonAction(
                    key_mapping.ActionKind.ESCAPE
                ).to_dict()
            },
            "display_notes": {},
        }
        self._save_button_bindings(original)

        self.app._on_button_event("tv", True, event_source="hid")
        self.assertFalse(self.app._button_combos.has_active_combo())
        changed = config.load_key_bindings(self.app._bindings_path)
        changed["combo_bindings"]["bindings"]["up"] = (
            key_mapping.ButtonAction(
                key_mapping.ActionKind.MOUSE_LEFT_CLICK
            ).to_dict()
        )
        self._save_button_bindings(changed)

        with mock.patch.object(win32_input, "send_mouse_button_click") as combo_action:
            self.app._on_button_event("up", True, event_source="hid")
            self.app._on_button_event("up", False, event_source="hid")
            self.app._on_button_event("tv", False, event_source="hid")

        combo_action.assert_not_called()
        self.assertFalse(self.app._button_combos.has_active_combo())
        self.assertIsNone(self.app._pending_bindings)


class CandidateResolutionWiringTests(_AppWiringTestCase):
    def test_selected_duplicate_records_reach_session_only_after_unique_probe(self):
        selected_key = self.app._selected_remote_key
        records = [identity.RC003Candidate("MI RC", False, object(), selected_key)
                   for _ in range(2)]
        other = identity.RC003Candidate("MI RC", False, object(), "b" * 64)
        transport = app_module.ble_transport_winrt
        for outcomes, error in (((False, True), None),
                                ((False, False), transport.NoReachableCandidateError),
                                ((True, True), identity.AmbiguousCandidateError)):
            with self.subTest(outcomes=outcomes):
                session = mock.Mock(connect=mock.AsyncMock(), close=mock.AsyncMock())
                self.app._ble_session = None
                with mock.patch.object(transport, "discover_candidates",
                                       return_value=[other, *records]), mock.patch.object(
                    transport, "_candidate_has_voice_service_with_hard_timeout",
                    side_effect=outcomes,
                ) as probe, mock.patch.object(
                    transport, "RC003BleSession", return_value=session,
                ) as factory:
                    if error is None:
                        self._loop.run_until_complete(self.app._connect_once())
                        session.connect.assert_awaited_once_with(records[1])
                        factory.assert_called_once()
                        expected_state = bridge_runtime_status.BridgeConnectionState.CONNECTED
                    else:
                        with self.assertRaises(error):
                            self._loop.run_until_complete(self.app._connect_once())
                        factory.assert_not_called()
                        expected_state = bridge_runtime_status.BridgeConnectionState.RETRY_WAIT
                self.assertEqual([call.args[0] for call in probe.await_args_list], records)
                status = bridge_runtime_status.read_status(self.app._config_root)
                self.assertEqual(status.state, expected_state)

    def test_runtime_status_identifies_build_channels_recent_button_and_voice(self):
        self.app._runtime_raw_input_state = "ready"
        self.app._runtime_hid_tap_state = frida_compat.HidTapState.READY.value
        self.app._runtime_voice_key_physicalizer_state = "ready"
        with mock.patch.object(app_module.time, "time", return_value=123.0), mock.patch.object(
            app_module.time,
            "monotonic",
            return_value=10.0,
        ):
            self.app._record_runtime_button("hid")
        self.app._set_runtime_voice_active(True)
        self.app._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.CONNECTED
        )

        status = bridge_runtime_status.read_status(self.app._config_root)

        self.assertEqual(status.app_version, app_module.__version__)
        self.assertTrue(status.runtime_id)
        self.assertEqual(status.raw_input_state, "ready")
        self.assertEqual(status.hid_tap_state, frida_compat.HidTapState.READY.value)
        self.assertEqual(status.voice_key_physicalizer_state, "ready")
        self.assertEqual(status.last_button_at, 123.0)
        self.assertEqual(status.last_button_source, "hid")
        self.assertTrue(status.voice_active)

    def test_runtime_status_cleanup_only_removes_the_current_process_file(self):
        other_pid = app_module.os.getpid() + 1
        bridge_runtime_status.publish_status(
            self.app._config_root,
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=other_pid,
        )

        self.app.clear_runtime_status()

        self.assertEqual(
            bridge_runtime_status.read_status(self.app._config_root).pid,
            other_pid,
        )
        bridge_runtime_status.publish_status(
            self.app._config_root,
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=app_module.os.getpid(),
        )
        self.app.clear_runtime_status()
        self.assertIsNone(bridge_runtime_status.read_status(self.app._config_root))

    def test_connect_once_uses_connectable_candidate_resolver(self):
        candidates = [object(), object()]
        chosen = object()
        resolver_calls = []
        connected = []

        async def discover(*, with_device_keys):
            self.assertTrue(with_device_keys)
            return candidates

        async def resolve(received, *, selected_key):
            self.assertEqual(selected_key, self.app._selected_remote_key)
            resolver_calls.append(received)
            return chosen

        class Session:
            def __init__(self, **_kwargs):
                pass

            async def connect(self, candidate):
                connected.append(candidate)

            async def close(self):
                pass

        with mock.patch.object(
            app_module.ble_transport_winrt, "discover_candidates", discover
        ), mock.patch.object(
            app_module.ble_transport_winrt,
            "select_connectable_candidate",
            resolve,
        ), mock.patch.object(
            app_module.ble_transport_winrt, "RC003BleSession", Session
        ), mock.patch.object(
            self.app, "_publish_runtime_status"
        ) as publish_status:
            self._loop.run_until_complete(self.app._connect_once())

        self.assertEqual(resolver_calls, [candidates])
        self.assertEqual(connected, [chosen])
        self.assertTrue(self.app._accept_ble_events)
        self.assertEqual(
            publish_status.call_args_list,
            [
                mock.call(
                    bridge_runtime_status.BridgeConnectionState.CONNECTING
                ),
                mock.call(bridge_runtime_status.BridgeConnectionState.CONNECTED),
            ],
        )

    def test_connect_once_logs_the_exact_safe_failure_stage(self):
        expected_stages = (
            "discover_candidates",
            "select_connectable_candidate",
            "create_ble_session",
            "connect_gatt",
        )
        for failed_stage in expected_stages:
            with self.subTest(failed_stage=failed_stage):
                logger = mock.Mock()
                self.app._logger = logger
                candidate = object()

                async def discover(*, with_device_keys):
                    if failed_stage == "discover_candidates":
                        raise ConnectionError("private device detail")
                    return [candidate]

                async def select(candidates, *, selected_key):
                    if failed_stage == "select_connectable_candidate":
                        raise ConnectionError("private device detail")
                    return candidates[0]

                class Session:
                    def __init__(inner_self, **_callbacks):
                        if failed_stage == "create_ble_session":
                            raise ConnectionError("private device detail")

                    async def connect(inner_self, _candidate):
                        if failed_stage == "connect_gatt":
                            raise ConnectionError("private device detail")

                with mock.patch.object(
                    app_module.ble_transport_winrt,
                    "discover_candidates",
                    discover,
                ), mock.patch.object(
                    app_module.ble_transport_winrt,
                    "select_connectable_candidate",
                    select,
                ), mock.patch.object(
                    app_module.ble_transport_winrt,
                    "RC003BleSession",
                    Session,
                ), mock.patch.object(
                    self.app,
                    "_publish_runtime_status",
                ) as publish_status, self.assertRaises(ConnectionError):
                    self._loop.run_until_complete(self.app._connect_once())

                logger.error.assert_called_once_with(
                    "startup: RC003 connection failed: stage=%s error_type=%s",
                    failed_stage,
                    "ConnectionError",
                )
                self.assertNotIn(
                    "private device detail",
                    " ".join(str(value) for value in logger.error.call_args.args),
                )
                self.assertEqual(
                    publish_status.call_args_list,
                    [
                        mock.call(
                            bridge_runtime_status.BridgeConnectionState.CONNECTING
                        ),
                        mock.call(
                            bridge_runtime_status.BridgeConnectionState.RETRY_WAIT
                        ),
                    ],
                )

    def test_optional_battery_setup_failure_does_not_fail_core_connection(self):
        candidate = object()

        async def discover(*, with_device_keys):
            self.assertTrue(with_device_keys)
            return [candidate]

        async def resolve(candidates, *, selected_key):
            return candidates[0]

        class Session:
            def __init__(inner_self, **_callbacks):
                pass

            async def connect(inner_self, selected):
                self.assertIs(selected, candidate)

            def start_battery_monitor(inner_self):
                raise OSError("private optional detail")

        logger = mock.Mock()
        self.app._logger = logger
        with mock.patch.object(
            app_module.ble_transport_winrt, "discover_candidates", discover
        ), mock.patch.object(
            app_module.ble_transport_winrt,
            "select_connectable_candidate",
            resolve,
        ), mock.patch.object(
            app_module.ble_transport_winrt, "RC003BleSession", Session
        ), mock.patch.object(
            self.app, "_publish_runtime_status"
        ) as publish_status:
            self._loop.run_until_complete(self.app._connect_once())

        self.assertEqual(
            publish_status.call_args_list[-1],
            mock.call(bridge_runtime_status.BridgeConnectionState.CONNECTED),
        )
        logger.warning.assert_called_once_with(
            "optional RC003 battery monitor start failed: error_type=%s",
            "OSError",
        )

    def test_disconnect_waits_for_device_but_protocol_error_waits_to_retry(self):
        reconnects = []
        self.app._supervisor.request_reconnect = lambda: reconnects.append(True)
        with mock.patch.object(self.app, "_publish_runtime_status") as publish_status:
            self.app._on_disconnected()
            self.app._on_session_error(RuntimeError("simulated protocol error"))

        waiting = bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
        retry_wait = bridge_runtime_status.BridgeConnectionState.RETRY_WAIT
        self.assertEqual(
            publish_status.call_args_list,
            [mock.call(waiting), mock.call(retry_wait)],
        )
        self.assertEqual(reconnects, [True, True])

    def test_late_battery_is_rejected_after_disconnect_or_session_error(self):
        states = bridge_runtime_status.BridgeConnectionState
        self.app._supervisor.request_reconnect = mock.Mock()
        self.app._accept_ble_events = True
        for callback in (
            self.app._on_disconnected,
            lambda: self.app._on_session_error(RuntimeError("test")),
        ):
            self.app._publish_runtime_status(states.CONNECTED)
            self.app._on_battery_level(59)
            self.assertEqual(self.app._runtime_battery_level, 59)
            callback()
            self.app._on_battery_level(58)
            self.assertIsNone(self.app._runtime_battery_level)
            self.assertIsNone(
                bridge_runtime_status.read_status(self.app._config_root).battery_level
            )

    def test_previous_ble_session_callbacks_are_rejected_after_reconnect(self):
        sessions = []

        async def discover(*, with_device_keys):
            return [object()]

        async def resolve(candidates, *, selected_key):
            return candidates[0]

        class Session:
            def __init__(self, **callbacks):
                self.callbacks = callbacks
                sessions.append(self)

            async def connect(self, _candidate):
                pass

            async def close(self):
                pass

        with mock.patch.object(
            app_module.ble_transport_winrt, "discover_candidates", discover
        ), mock.patch.object(
            app_module.ble_transport_winrt,
            "select_connectable_candidate",
            resolve,
        ), mock.patch.object(
            app_module.ble_transport_winrt, "RC003BleSession", Session
        ):
            self._loop.run_until_complete(self.app._connect_once())
            self.app._accept_ble_events = False
            self._loop.run_until_complete(self.app._connect_once())

        reconnects = []
        self.app._supervisor.request_reconnect = lambda: reconnects.append(True)
        self.app._voice_pcm_forwarding_enabled = True
        with mock.patch.object(
            self.app, "_handle_mic_button_pressed"
        ) as pressed, mock.patch.object(
            self.app._voice_audio, "ensure_writer", return_value=False
        ) as ensure_writer:
            sessions[0].callbacks["on_disconnected"]()
            sessions[0].callbacks["on_error"](RuntimeError("stale"))
            sessions[0].callbacks["on_control_event"](MicButtonPressed())
            sessions[0].callbacks["on_pcm_frame"]([1, 2, 3])
            sessions[0].callbacks["on_battery_level"](91)

        self.assertEqual(reconnects, [])
        pressed.assert_not_called()
        ensure_writer.assert_not_called()
        self.assertIsNone(self.app._runtime_battery_level)

        sessions[1].callbacks["on_battery_level"](47)
        self.assertEqual(self.app._runtime_battery_level, 47)
        sessions[1].callbacks["on_disconnected"]()
        self.assertEqual(reconnects, [True])
        self.assertIsNone(self.app._runtime_battery_level)


class DiagnosticVoiceWiringTests(_AppWiringTestCase):
    def setUp(self):
        super().setUp()
        self.app._diagnostic_trace.set_enabled(True)
        self.app._supervisor.request_reconnect = mock.Mock()
        focus = mock.patch.object(
            app_module.voice_interaction_diagnostics_windows,
            "capture_focus_snapshot",
            return_value=app_module.voice_interaction_diagnostics_windows.FocusSnapshot(
                supported=False
            ),
        )
        focus.start()
        self.addCleanup(focus.stop)

    def tearDown(self):
        self.app._diagnostic_trace.close()
        super().tearDown()

    def _read_trace(self):
        trace = self.app._diagnostic_trace
        trace.close()
        return [
            json.loads(line)
            for line in trace.path.read_text(encoding="utf-8").splitlines()
        ]

    def test_enabled_trace_preserves_marked_hotkey_start_and_stop(self):
        with mock.patch.object(
            win32_input, "send_voice_key_combo_down"
        ) as down, mock.patch.object(win32_input, "send_voice_key_combo_up") as up:
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(AudioStopped())
        down.assert_called_once_with(DEFAULT_VOICE_TOKENS)
        up.assert_called_once_with(DEFAULT_VOICE_TOKENS)
        self.assertFalse(self.app._voice_shortcut.controller.active)
        self.app._supervisor.request_reconnect.assert_not_called()

    def test_control_events_keep_trace_session_and_atvv_session_separate(self):
        trace_session = self.app._diagnostic_trace.session_id
        with mock.patch.object(
            self.app, "_handle_key_detection_mic_event", return_value=(True, False)
        ):
            for session_id in (0, 87, None):
                self.app._on_control_event(AudioStarted(session_id=session_id, reason=0))
            self.app._on_control_event(AudioStopped(reason=0))
        records = self._read_trace()
        self.assertEqual({r["session_id"] for r in records}, {trace_session})
        for event, expected in (
            ("voice_control_event", [0, 87, -1, -1]),
            ("audio_started", [0, 87, -1]),
        ):
            events = [r for r in records if r["event"] == event]
            self.assertEqual([r["atvv_session_id"] for r in events], expected)
            self.assertEqual({r["reason"] for r in events}, {0})


class HostHotkeyFailureSuppressesMicOpenTests(_AppWiringTestCase):
    def test_failed_voice_start_has_one_complete_diagnostic_attempt(self):
        trace = mock.Mock()
        trace.current_gesture.return_value = "gesture-1"
        trace.current_context.return_value = {
            "gesture_id": "gesture-1",
            "attempt_id": "attempt-1",
        }
        trace.begin_attempt.return_value = "attempt-1"
        self.app._diagnostic_trace = trace
        self.app._accept_input_events = False

        self.assertFalse(self.app._handle_mic_button_pressed())

        trace.begin_attempt.assert_called_once()
        trace.end_attempt.assert_called_once_with(
            "attempt-1",
            "failed_before_send",
            host_ui="unknown",
            text_state="unknown",
            reason="input_unavailable",
        )
        self.assertIsNone(self.app._voice_attempt_id)

    def test_focus_trace_keeps_unconfirmed_voice_ui_explicitly_unknown(self):
        trace = mock.Mock()
        trace.current_context.return_value = {
            "gesture_id": "gesture-2",
            "attempt_id": "attempt-2",
        }
        self.app._diagnostic_trace = trace
        before = app_module.voice_interaction_diagnostics_windows.FocusSnapshot(
            supported=True,
            foreground_pid=100,
            foreground_class="ChatWnd",
            focus_handle=200,
            focus_class="Edit",
            text_length=3,
        )
        after = replace(before, text_length=8)
        with mock.patch.object(
            app_module.voice_interaction_diagnostics_windows,
            "capture_focus_snapshot",
            side_effect=[before, after],
        ):
            self.app._capture_voice_focus_before()
            observation = self.app._log_voice_submission_observation()

        self.assertEqual(observation.text_delta, 5)
        focus_events = [
            call
            for call in trace.emit.call_args_list
            if call.args[0]
            in {"voice_focus_snapshot", "voice_submission_observation"}
        ]
        self.assertEqual(len(focus_events), 2)
        self.assertEqual(focus_events[0].kwargs["voice_ui"], "unknown")
        self.assertEqual(focus_events[1].kwargs["voice_ui"], "unknown")
        self.assertEqual(focus_events[1].kwargs["text_delta"], 5)

    """Hold-to-talk must fail closed and never leave a host key held."""

    @unittest.skipIf(
        sys.platform == "win32",
        "only exercises the off-Windows input-backend gate",
    )
    def test_hotkey_unavailable_off_windows_suppresses_mic_open(self):
        self.app._handle_mic_button_pressed()

        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_hotkey_partial_delivery_suppresses_mic_open(self):
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=OSError("simulated partial SendInput delivery"),
        ):
            self.app._handle_mic_button_pressed()

        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_incomplete_hotkey_rollback_is_retained_for_a_later_safety_release(self):
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=win32_input.InputCleanupIncompleteError(
                "simulated stuck modifier"
            ),
        ):
            self.app._handle_mic_button_pressed()

        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertFalse(self.app._voice_shortcut.controller.active)
        self.assertEqual(self.app._voice_shortcut.pending_tokens, DEFAULT_VOICE_TOKENS)

    def test_voice_hotkey_release_debt_keeps_only_one_retry_timer(self):
        self.app._voice_shortcut.pending_tokens = DEFAULT_VOICE_TOKENS

        with self.app._voice_shortcut.lock:
            self.app._voice_shortcut.schedule_release_retry()
            self.app._voice_shortcut.schedule_release_retry()

        self.assertEqual(len(self._voice_hotkey_release_timers), 1)
        self.assertIs(
            self.app._voice_shortcut.retry_timer,
            self._voice_hotkey_release_timers[0],
        )

    def test_voice_hotkey_release_retry_uses_bounded_backoff(self):
        self.app._voice_shortcut.pending_tokens = DEFAULT_VOICE_TOKENS
        self.app._voice_shortcut.pending_backend = (
            app_module._VOICE_HOTKEY_BACKEND_MARKED
        )

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=OSError("simulated key-up failure"),
        ) as send_up:
            with self.app._voice_shortcut.lock:
                self.app._voice_shortcut.schedule_release_retry()
            self._voice_hotkey_release_timers[0].fire()
            self._voice_hotkey_release_timers[1].fire()

        initial = app_module.voice_shortcut_session._VOICE_HOTKEY_RELEASE_RETRY_INITIAL_SECONDS
        self.assertEqual(
            [timer.delay for timer in self._voice_hotkey_release_timers],
            [initial, initial * 2.0, initial * 4.0],
        )
        self.assertEqual(send_up.call_count, 2)
        self.assertEqual(self.app._voice_shortcut.pending_tokens, DEFAULT_VOICE_TOKENS)

    def test_successful_voice_hotkey_release_retry_clears_all_debt(self):
        self.app._voice_shortcut.pending_tokens = DEFAULT_VOICE_TOKENS
        self.app._voice_shortcut.pending_backend = (
            app_module._VOICE_HOTKEY_BACKEND_MARKED
        )
        with self.app._voice_shortcut.lock:
            self.app._voice_shortcut.schedule_release_retry()

        with mock.patch.object(win32_input, "send_voice_key_combo_up") as send_up:
            self._voice_hotkey_release_timers[0].fire()

        send_up.assert_called_once_with(DEFAULT_VOICE_TOKENS)
        self.assertIsNone(self.app._voice_shortcut.pending_tokens)
        self.assertIsNone(self.app._voice_shortcut.pending_backend)
        self.assertIsNone(self.app._voice_shortcut.retry_timer)
        self.assertEqual(
            self.app._voice_shortcut.retry_delay,
            app_module.voice_shortcut_session._VOICE_HOTKEY_RELEASE_RETRY_INITIAL_SECONDS,
        )

    def test_plugin_voice_short_tap_never_replays_hotkey(self):
        entries = config.voice_key_configs(self.app._config)
        entries[0] = {**entries[0], "plugin": "vokie"}
        self.app._config["voice_key_configs"] = entries
        calls = []

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(tokens),
        ), mock.patch.object(
            self.app._voice_audio.__class__, "open", autospec=True
        ) as audio_open:
            self.assertTrue(
                self.app._handle_mic_button_pressed(send_device_open=False)
            )
            # 短按只切换模式：按下时不能惊动 Vokie。
            self.assertEqual(calls, [])
            self.assertIsNotNone(self.app._plugin_voice_press_token)
            with self.app._voice_shortcut.lock:
                self.app._cancel_plugin_voice_hold_pending_locked()

        self.assertEqual(calls, [])
        audio_open.assert_not_called()
        self.assertFalse(self.app._voice_shortcut.controller.active)
        self.assertFalse(self.app._voice_pcm_forwarding_enabled)

    def test_plugin_voice_long_press_replays_hotkey_without_opening_virtual_audio(self):
        entries = config.voice_key_configs(self.app._config)
        entries[0] = {**entries[0], "plugin": "vokie"}
        self.app._config["voice_key_configs"] = entries
        calls = []

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(tokens),
        ), mock.patch.object(
            self.app._voice_audio.__class__, "open", autospec=True
        ) as audio_open:
            self.assertTrue(
                self.app._handle_mic_button_pressed(send_device_open=False)
            )
            token = self.app._plugin_voice_press_token
            self.app._plugin_voice_hold_elapsed(token)

        self.assertEqual(calls, [("ralt",)])
        audio_open.assert_not_called()
        self.assertFalse(self.app._voice_pcm_forwarding_enabled)
        self.assertTrue(self.app._voice_shortcut.controller.active)

    def test_plugin_voice_fallback_replays_the_keys_own_shortcut(self):
        # 语音指令和语音输入共用同一条插件回退路径：每条键必须重放自己的
        # 快捷键，不能沿用会话里残留的上一条（否则两个模式都变成语音输入）。
        self.app._config["voice_key_configs"] = [
            {
                "id": 1,
                "name": "语音输入",
                "hotkey": "ralt",
                "source": "remote",
                "device": "",
                "plugin": "vokie",
            },
            {
                "id": 2,
                "name": "语音指令",
                "hotkey": "ralt+space",
                "source": "remote",
                "device": "",
                "plugin": "vokie",
            },
        ]
        calls = []

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(tokens),
        ), mock.patch.object(
            self.app._voice_audio.__class__, "open", autospec=True
        ):
            self.assertTrue(
                self.app._handle_mic_button_pressed(
                    send_device_open=False, voice_slot=2
                )
            )
            self.app._plugin_voice_hold_elapsed(
                self.app._plugin_voice_press_token
            )

        self.assertEqual(calls, [("ralt", "space")])
        self.assertEqual(self.app._plugin_voice_injected_tokens, ("ralt", "space"))

    def test_plugin_voice_key_up_is_never_lost_when_the_session_resets(self):
        entries = config.voice_key_configs(self.app._config)
        entries[0] = {**entries[0], "plugin": "vokie"}
        self.app._config["voice_key_configs"] = entries
        with mock.patch.object(
            win32_input, "send_voice_key_combo_down"
        ), mock.patch.object(
            self.app._voice_audio.__class__, "open", autospec=True
        ):
            self.assertTrue(
                self.app._handle_mic_button_pressed(send_device_open=False)
            )
            self.app._plugin_voice_hold_elapsed(
                self.app._plugin_voice_press_token
            )
        # 记账被重置（曾经导致右侧 Alt 卡住 120 秒，整个键盘失灵）
        self.app._voice_shortcut.controller.reset()

        with mock.patch.object(win32_input, "send_voice_key_combo_up") as send_up:
            with self.app._voice_shortcut.lock:
                self.app._release_voice_shortcut_edge_locked("lost_bookkeeping")

        send_up.assert_called_once_with(("ralt",))
        self.assertIsNone(self.app._plugin_voice_injected_tokens)

    def test_plugin_voice_long_press_streams_remote_audio_to_the_plugin(self):
        entries = config.voice_key_configs(self.app._config)
        entries[0] = {**entries[0], "plugin": "vokie"}
        self.app._config["voice_key_configs"] = entries
        bridge = _FakePluginBridge()
        calls = []

        with mock.patch.object(
            app_module.vokie_bridge, "active_bridge", return_value=bridge
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(tokens),
        ), mock.patch.object(
            self.app._voice_audio.__class__, "open", autospec=True
        ) as audio_open:
            self.assertTrue(
                self.app._handle_mic_button_pressed(send_device_open=False)
            )
            # 短按只是切模式：不能惊动 Vokie。遥控器话筒先开着缓存 PCM。
            self.assertEqual(bridge.audio_starts, [])
            self.assertTrue(self.app._plugin_voice_capture_pending)
            self.assertEqual(self.app._ble_session.mic_open_calls, 1)

            self.app._plugin_voice_hold_elapsed(
                self.app._plugin_voice_press_token
            )
            # 兜底定时器到点：模式名交给插件，话筒已经开过不重复开。
            self.assertEqual(
                bridge.audio_starts,
                [config.voice_key_name(self.app._config, entries[0]["id"])],
            )
            self.assertEqual(self.app._ble_session.mic_open_calls, 1)
            self.assertTrue(self.app._plugin_voice_audio_active)

            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_pcm_frame([100, -100])
            with self.app._voice_shortcut.lock:
                self.app._release_voice_shortcut_edge_locked(
                    "physical mic release"
                )

        self.assertEqual(calls, [])
        audio_open.assert_not_called()
        self.assertEqual(
            bridge.audio_frames,
            [array.array("h", [100, -100]).tobytes()],
        )
        self.assertEqual(bridge.audio_stops, 1)
        self.assertFalse(self.app._plugin_voice_audio_active)
        self.assertFalse(self.app._voice_pcm_forwarding_enabled)
        self.assertFalse(self.app._plugin_voice_audio_stream)

    def test_mode_toast_hides_only_after_vokie_accepts_the_session(self):
        entries = config.voice_key_configs(self.app._config)
        entries[0] = {**entries[0], "plugin": "vokie"}
        self.app._config["voice_key_configs"] = entries
        bridge = _FakePluginBridge()

        with mock.patch.object(
            app_module.vokie_bridge, "active_bridge", return_value=bridge
        ), mock.patch.object(
            self.app, "_hide_voice_mode_toast"
        ) as hide_toast, mock.patch.object(
            self.app._voice_audio.__class__, "open", autospec=True
        ):
            self.assertTrue(
                self.app._handle_mic_button_pressed(send_device_open=False)
            )
            self.app._plugin_voice_hold_elapsed(
                self.app._plugin_voice_press_token
            )
            # audio_start 只是请求会话；Vokie 还没弹输入框，气泡不能提前收。
            self.assertEqual(len(bridge.audio_starts), 1)
            hide_toast.assert_not_called()

            bridge.sessionAccepted.emit()

        hide_toast.assert_called_once_with()

    def test_plugin_voice_tap_discards_preroll_and_cancels_the_hold(self):
        entries = config.voice_key_configs(self.app._config)
        entries[0] = {**entries[0], "plugin": "vokie"}
        self.app._config["voice_key_configs"] = entries
        bridge = _FakePluginBridge()

        with mock.patch.object(
            app_module.vokie_bridge, "active_bridge", return_value=bridge
        ), mock.patch.object(
            win32_input, "send_voice_key_combo_down"
        ) as send_down:
            self.assertTrue(
                self.app._handle_mic_button_pressed(send_device_open=False)
            )
            self.app._on_control_event(AudioStarted(session_id=1))
            # 开话筒的爆音、按键噪声也不能把人声判断骗过去：只认按住时间。
            self.app._on_pcm_frame([3000, -3000])
            self.app._on_pcm_frame([3000, -3000])
            self.assertEqual(bridge.audio_starts, [])
            self.assertTrue(self.app._plugin_voice_capture_pending)

            token = self.app._plugin_voice_press_token
            self.assertIsNotNone(token)
            # 短按松手：预录丢掉、长按定时器撤掉、话筒关掉。
            with self.app._voice_shortcut.lock:
                self.app._cancel_plugin_voice_capture_locked(
                    "physical mic release"
                )
            self.assertIsNone(self.app._plugin_voice_press_token)
            self.assertFalse(self.app._plugin_voice_capture_pending)
            self.assertEqual(self.app._ble_session.mic_close_calls, 1)
            # 松手之后那个定时器就算响了，也不能再把 Vokie 叫起来。
            self.app._plugin_voice_hold_elapsed(token)

        self.assertEqual(bridge.audio_starts, [])
        self.assertEqual(bridge.audio_frames, [])
        self.assertEqual(bridge.audio_stops, 0)
        send_down.assert_not_called()
        self.assertFalse(self.app._plugin_voice_audio_active)
        self.assertFalse(self.app._plugin_voice_started_this_press)

    def test_new_voice_session_releases_previous_hotkey_debt_first(self):
        previous_tokens = ("lctrl",)
        self.app._voice_shortcut.pending_tokens = previous_tokens
        self.app._voice_shortcut.pending_backend = (
            app_module._VOICE_HOTKEY_BACKEND_MARKED
        )
        calls = []

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ):
            self.assertTrue(self.app._handle_mic_button_pressed())

        self.assertEqual(
            calls,
            [("up", previous_tokens), ("down", DEFAULT_VOICE_TOKENS)],
        )
        self.assertEqual(self.app._ble_session.mic_open_calls, 1)

    def test_safety_release_uses_the_original_shortcut_after_settings_change(self):
        self.app._voice_shortcut.pending_tokens = ("ralt",)
        self.app._voice_shortcut.hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+l")
        calls = []

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(tokens),
        ):
            self.assertTrue(self.app._voice_shortcut.release_pending())

        self.assertEqual(calls, [("ralt",)])

    def test_successful_hold_down_is_owned_until_matching_key_up(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.assertTrue(
                self.app._voice_shortcut.apply(
                    app_module.voice_controller.VoiceHostAction.KEY_DOWN
                )
            )
            self.assertEqual(
                self.app._voice_shortcut.pending_tokens,
                DEFAULT_VOICE_TOKENS,
            )
            self.assertTrue(
                self.app._voice_shortcut.apply(
                    app_module.voice_controller.VoiceHostAction.KEY_UP
                )
            )

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertIsNone(self.app._voice_shortcut.pending_tokens)

    def test_hotkey_success_sends_mic_open(self):
        with mock.patch.object(win32_input, "send_voice_key_combo_down"):
            self.app._handle_mic_button_pressed()

        self.assertEqual(self.app._ble_session.mic_open_calls, 1)
        self.assertTrue(self.app._voice_shortcut.controller.active)

    def test_audio_start_waits_for_real_pcm_before_reporting_receive(self):
        with mock.patch.object(win32_input, "send_voice_key_combo_down"):
            self.app._on_control_event(AudioStarted(session_id=1))

        waiting = bridge_runtime_status.read_status(self.app._config_root)
        self.assertEqual(
            waiting.voice_runtime_state,
            bridge_runtime_status.VOICE_RUNTIME_ACTIVE,
        )

        self.app._voice_audio.write_frame(self.app._voice_audio.sink, [100, -100])

        receiving = bridge_runtime_status.read_status(self.app._config_root)
        self.assertEqual(
            receiving.voice_runtime_state,
            bridge_runtime_status.VOICE_RUNTIME_RECEIVING_AUDIO,
        )

    def test_voice_hold_watchdog_forces_key_up_and_reconnect(self):
        timers = []
        reconnects = []
        calls = []
        self.app._voice_hold_watchdog_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback)) or timers[-1]
        )
        self.app._supervisor.request_reconnect = lambda: reconnects.append(True)

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._handle_mic_button_pressed()
            timers[0].fire()

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertFalse(self.app._voice_shortcut.controller.active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 1)
        self.assertEqual(self.app._ble_session.mic_close_calls, 1)
        self.assertEqual(reconnects, [True])

    def test_stale_voice_hold_watchdog_cannot_release_a_new_session(self):
        timers = []
        calls = []
        self.app._voice_hold_watchdog_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback)) or timers[-1]
        )

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._handle_mic_button_pressed()
            first_callback = timers[0].callback
            with self.app._voice_shortcut.lock:
                self.assertTrue(
                    self.app._release_hold_voice_on_physical_release_locked(
                        "test first release"
                    )
                )
            self.app._handle_mic_button_pressed()
            first_callback()

        self.assertTrue(self.app._voice_shortcut.controller.active)
        self.assertEqual(
            calls,
            [
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
                ("down", DEFAULT_VOICE_TOKENS),
            ],
        )

    def test_watchdog_start_failure_immediately_releases_the_host_key(self):
        reconnects = []
        calls = []
        self.app._supervisor.request_reconnect = lambda: reconnects.append(True)

        class FailingTimer:
            def start(self):
                raise RuntimeError("simulated timer start failure")

            def cancel(self):
                pass

        self.app._voice_hold_watchdog_timer_factory = (
            lambda _delay, _callback: FailingTimer()
        )

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._handle_mic_button_pressed()

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertFalse(self.app._voice_shortcut.controller.active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertEqual(self.app._ble_session.mic_close_calls, 1)
        self.assertEqual(reconnects, [True])

    def test_raw_input_press_runs_before_matching_atvv_events(self):
        clock = [100.0]
        self.app._voice_gesture = voice_gesture.VoiceGestureLatch(
            clock=lambda: clock[0]
        )
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(MicButtonPressed())
            clock[0] += voice_gesture.VoiceGestureLatch.DEFAULT_TAP_MAX_SECONDS + 0.01
            self.app._on_button_event("mic", False, event_source="hid")
            self.app._on_control_event(AudioStopped())

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_audio_start_uses_configured_hotkey_without_waiting_for_f5(self):
        self._save_voice_settings(hotkey_text="ralt")
        self.app._reload_settings_if_changed()
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(tokens),
        ):
            self.app._on_control_event(AudioStarted(session_id=1))

        self.assertEqual(calls, [("ralt",)])
        self.assertTrue(self.app._voice_shortcut.controller.active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_audio_start_can_fallback_without_a_physical_key_edge(self):
        self._save_voice_settings(hotkey_text="ctrl+l")
        self.app._reload_settings_if_changed()
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(tokens),
        ):
            self.app._on_control_event(AudioStarted(session_id=1))

        self.assertEqual(calls, [("ctrl", "l")])
        self.assertTrue(self.app._voice_shortcut.controller.active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_duplicate_audio_start_does_not_send_a_second_key_down(self):
        self._save_voice_settings(hotkey_text="ctrl+l")
        self.app._reload_settings_if_changed()
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(tokens),
        ):
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(AudioStarted(session_id=1))

        self.assertEqual(calls, [("ctrl", "l")])
        self.assertTrue(self.app._voice_shortcut.controller.active)

    def test_hid_mic_button_is_ignored_until_ble_session_is_connected(self):
        self.app._ble_session = None
        with mock.patch.object(win32_input, "send_voice_key_combo_down") as hotkey:
            self.app._on_button_event("mic", True)

        hotkey.assert_not_called()
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_mic_button_before_audio_start_does_not_send_a_second_key_down(self):
        self._save_voice_settings(hotkey_text="ctrl+l")
        self.app._reload_settings_if_changed()
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(tokens),
        ):
            self.app._on_control_event(MicButtonPressed())
            self.app._on_control_event(AudioStarted(session_id=1))

        self.assertEqual(calls, [("ctrl", "l")])
        self.assertTrue(self.app._voice_shortcut.controller.active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 1)

    def test_no_usable_endpoint_suppresses_hotkey_and_mic_open(self):
        self.app._voice_audio.sink = None
        self.app._config["output_endpoint_name"] = "some endpoint that is not open"

        with mock.patch.object(win32_input, "send_voice_key_combo_down") as hotkey:
            self.app._handle_mic_button_pressed()

        hotkey.assert_not_called()
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_removed_release_finish_setting_never_sends_an_extra_tap(self):
        # Schema 2 exposed this field. Keep the runtime fail-safe even if an
        # old in-memory config reaches the app before it has been re-saved.
        self.app._config["voice_release_finish_tap_enabled"] = True
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ), mock.patch.object(win32_input, "send_voice_key_combo_tap") as finish_tap:
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_button_event("mic", False, event_source="hid")
            self.app._on_control_event(AudioStopped())

            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_control_event(AudioStarted(session_id=2))
            self.app._on_control_event(AudioStopped())
            self.app._on_button_event("mic", False, event_source="hid")

        self.assertEqual(
            calls,
            [
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
            ],
        )
        finish_tap.assert_not_called()
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_windows_actually_delivers_the_hold_hotkey(self):
        original_platform = sys.platform
        original_sender = win32_input._real_voice_event
        sys.platform = "win32"
        win32_input._real_voice_event = lambda vk, key_up: None
        try:
            self.app._handle_mic_button_pressed()
        finally:
            sys.platform = original_platform
            win32_input._real_voice_event = original_sender

        self.assertEqual(self.app._ble_session.mic_open_calls, 1)
        self.assertTrue(self.app._voice_shortcut.controller.active)


class CorruptButtonBindingFailsClosedTests(_AppWiringTestCase):
    def test_unknown_or_malformed_binding_does_not_escape_raw_input_callback(self):
        for malformed in (
            {"kind": "unknown", "keys": []},
            {"kind": "key_combo", "keys": ["not_a_real_key"]},
            {"kind": "key_combo", "keys": "ctrl+a"},
            {},
            "not-a-mapping",
        ):
            self.app._bindings = {"bindings": {"back": malformed}}
            self.app._on_button_event("back", True)

        self.assertFalse(self.app._voice_shortcut.controller.active)


class OrdinaryButtonGestureWiringTests(_AppWiringTestCase):
    def _use_recorded_repeat_timers(self):
        timers = []

        def timer_factory(delay, callback):
            timer = _ManualTimer(callback)
            timer.delay = delay
            timers.append(timer)
            return timer

        self.app._button_gestures._timer_factory = timer_factory
        self.app._button_gestures._hold_timer_factory = (
            lambda _delay, callback: _ManualTimer(callback)
        )
        self.addCleanup(self.app._button_gestures.reset)
        return timers

    def test_direction_hold_replays_full_clicks_and_release_stops_repeat(self):
        timers = self._use_recorded_repeat_timers()
        for source in ("hid_tap",):
            for button in ("up", "down", "left", "right"):
                with self.subTest(source=source, button=button), mock.patch.object(
                    win32_input, f"send_arrow_{button}"
                ) as tap, mock.patch.object(
                    app_module.element_navigation_control_windows,
                    "route_mapped_navigation_key", return_value=False,
                ):
                    self.app._on_button_event(button, True, event_source=source)
                    # SendInput cannot emulate a Windows key hold, so a held
                    # direction button replays the whole click (down+up) at the
                    # machine's keyboard repeat rate.
                    tap.assert_called_once_with()
                    self.assertEqual(timers[-1].delay, 0.350)
                    # Repeated device DOWN reports must not add extra clicks.
                    self.app._on_button_event(button, True, event_source=source)
                    tap.assert_called_once_with()
                    for _ in range(3):
                        timers[-1].fire()
                        self.assertEqual(timers[-1].delay, 0.065)
                    self.assertEqual(tap.call_count, 4)
                    stale_timer = timers[-1]
                    self.app._on_button_event(button, False, event_source=source)
                    # Release only stops the repeat: the last click was already
                    # complete, so no extra key-up is delivered.
                    self.assertEqual(tap.call_count, 4)
                    # A timer already dispatched before cancellation is stale too.
                    stale_timer.callback()
                    self.assertEqual(tap.call_count, 4)
                    self.app._on_button_event(button, True, event_source=source)
                    self.assertEqual(tap.call_count, 5)
                    self.assertEqual(timers[-1].delay, 0.350)
                    stale_timer.callback()
                    self.assertEqual(tap.call_count, 5)
                    self.app._on_button_event(button, False, event_source=source)

    def test_interception_direction_hold_replays_key_down_until_release(self):
        timers = self._use_recorded_repeat_timers()
        fake_backend = mock.Mock()
        with mock.patch.object(
            app_module.injection_backend, "backend_for", return_value=fake_backend
        ) as backend_for, mock.patch.object(
            app_module.element_navigation_control_windows,
            "route_mapped_navigation_key", return_value=False,
        ), mock.patch.object(win32_input, "send_arrow_up") as tap:
            self.app._bindings["bindings"]["up"] = key_mapping.ButtonAction(
                kind=key_mapping.ActionKind.ARROW_UP,
                inject_mode=key_mapping.INJECT_MODE_INTERCEPTION,
            ).to_dict()
            self.app._on_button_event("up", True, event_source="hid_tap")
            # Interception emulates a Windows key hold, so the press delivers
            # only a key-down and never a whole click.
            backend_for.assert_called_once_with(key_mapping.INJECT_MODE_INTERCEPTION)
            tokens = fake_backend.key_down.call_args.args[0]
            self.assertEqual(fake_backend.key_down.call_count, 1)
            fake_backend.key_up.assert_not_called()
            tap.assert_not_called()
            # Each repeat re-sends only the key-down, keeping one sustained press.
            timers[-1].fire()
            self.assertEqual(fake_backend.key_down.call_count, 2)
            self.assertEqual(fake_backend.key_down.call_args.args[0], tokens)
            fake_backend.key_up.assert_not_called()
            tap.assert_not_called()
            # The single matching key-up is delivered only on release.
            self.app._on_button_event("up", False, event_source="hid_tap")
            fake_backend.key_up.assert_called_once_with(tokens)

    def test_every_held_action_uses_the_system_repeat_rate(self):
        timers = self._use_recorded_repeat_timers()
        # No per-button exceptions: arrows, backspace and volume all repeat at
        # the machine's own keyboard repeat rate.  Under SendInput every repeat
        # is a whole click, so the mocked sender counts full taps.
        self.app._keyboard_repeat_interval_seconds = 0.033
        cases = (
            ("up", key_mapping.ActionKind.ARROW_UP, "send_arrow_up"),
            ("down", key_mapping.ActionKind.ARROW_DOWN, "send_arrow_down"),
            ("up", key_mapping.ActionKind.SYSTEM_VOLUME_UP, "send_volume_up"),
            ("up", key_mapping.ActionKind.DELETE_BACKWARD, "send_delete_backward"),
            ("volume_up", key_mapping.ActionKind.SYSTEM_VOLUME_UP, "send_volume_up"),
            ("volume_down", key_mapping.ActionKind.ARROW_DOWN, "send_arrow_down"),
            ("back", key_mapping.ActionKind.DELETE_BACKWARD, "send_delete_backward"),
        )
        for button, kind, sender in cases:
            with self.subTest(button=button, kind=kind), mock.patch.object(
                win32_input, sender
            ) as tap, mock.patch.object(
                app_module.element_navigation_control_windows,
                "route_mapped_navigation_key", return_value=False,
            ):
                self.app._bindings["bindings"][button] = key_mapping.ButtonAction(kind).to_dict()
                self.app._on_button_event(button, True, event_source="hid_tap")
                self.assertEqual(timers[-1].delay, 0.350)
                tap.assert_called_once_with()
                timers[-1].fire()
                self.assertEqual(timers[-1].delay, 0.033)
                self.assertEqual(tap.call_count, 2)
                self.app._on_button_event(button, False, event_source="hid_tap")

    def test_faster_direction_repeat_routes_navigation_without_duplicate_input(self):
        timers = self._use_recorded_repeat_timers()
        with mock.patch.object(
            app_module.element_navigation_control_windows,
            "route_mapped_navigation_key", return_value=True,
        ) as route, mock.patch.object(win32_input, "send_arrow_up") as send:
            self.app._on_button_event("up", True, event_source="hid_tap")
            timers[-1].fire()
            self.assertEqual(timers[-1].delay, 0.065)
            self.app._on_button_event("up", False, event_source="hid_tap")
            timers[-1].callback()
            self.assertEqual(route.call_args_list, [mock.call(0x26), mock.call(0x26)])
            send.assert_not_called()

    def test_dispatch_callback_does_not_reacquire_the_mapping_lock(self):
        lock_held = threading.Event()
        release_lock = threading.Event()
        callback_finished = threading.Event()

        def hold_mapping_lock():
            with self.app._button_mapping_lock:
                lock_held.set()
                release_lock.wait(1.0)

        holder = threading.Thread(target=hold_mapping_lock)
        callback = threading.Thread(
            target=lambda: (
                self.app._on_button_trigger(
                    "up",
                    app_module.button_gesture.ButtonTrigger.SINGLE_CLICK,
                ),
                callback_finished.set(),
            )
        )
        holder.start()
        try:
            self.assertTrue(lock_held.wait(1.0))
            with mock.patch.object(win32_input, "send_arrow_up"):
                callback.start()
                self.assertTrue(callback_finished.wait(0.5))
        finally:
            release_lock.set()
            holder.join(1.0)
            callback.join(1.0)

        self.assertFalse(holder.is_alive())
        self.assertFalse(callback.is_alive())

    def test_saved_mapping_is_reloaded_before_the_next_button_event(self):
        updated = config.default_key_bindings()
        updated["bindings"]["back"] = {"kind": "key_combo", "keys": ["f8"]}
        config.save_key_bindings(self.app._bindings_path, updated)

        downs = []
        ups = []
        original_down = win32_input.send_voice_key_combo_down
        original_up = win32_input.send_voice_key_combo_up
        win32_input.send_voice_key_combo_down = lambda keys: downs.append(tuple(keys))
        win32_input.send_voice_key_combo_up = lambda keys: ups.append(tuple(keys))
        try:
            self.app._on_button_event("back", True)
            self.app._on_button_event("back", False)
        finally:
            win32_input.send_voice_key_combo_down = original_down
            win32_input.send_voice_key_combo_up = original_up

        # A non-repeatable key with no configured secondary action holds its
        # single-click key for the whole physical hold: key-down on press and
        # the matching key-up only on release.
        self.assertEqual(downs, [("f8",)])
        self.assertEqual(ups, [("f8",)])

    def test_semantic_arrow_action_uses_its_function_executor(self):
        calls = []
        original = getattr(win32_input, "send_arrow_up", None)
        win32_input.send_arrow_up = lambda: calls.append("arrow_up")
        try:
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_UP)
            )
        finally:
            if original is None:
                delattr(win32_input, "send_arrow_up")
            else:
                win32_input.send_arrow_up = original

        self.assertEqual(calls, ["arrow_up"])

    def test_navigation_owned_mapping_is_not_also_injected(self):
        control = app_module.element_navigation_control_windows
        for accepted in (True, False):
            with self.subTest(accepted=accepted), mock.patch.object(
                control, "route_mapped_navigation_key", return_value=accepted
            ) as route, mock.patch.object(win32_input, "send_arrow_up") as inject:
                self.app._apply_button_action(key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_UP))
                route.assert_called_once_with(0x26)
                self.assertEqual(inject.call_count, 0 if accepted else 1)

    def test_custom_combo_bypasses_semantic_navigation_route(self):
        with mock.patch.object(app_module.element_navigation_control_windows,
                               "route_mapped_navigation_key", return_value=True) as route, mock.patch.object(
            win32_input, "send_key_combo_tap"
        ) as inject:
            self.app._apply_button_action(key_mapping.ButtonAction(key_mapping.ActionKind.KEY_COMBO, keys=("up",)))
            route.assert_not_called()
            inject.assert_called_once_with(("up",))

    def test_remote_page_key_controls_navigation_only_when_claimed(self):
        control = app_module.element_navigation_control_windows
        for token, vk in (
            ("pageup", 0x21), ("page_up", 0x21),
            ("pagedown", 0x22), ("page_down", 0x22),
        ):
            action = settings_ui._display_to_action(token)
            for accepted in (True, False):
                with self.subTest(token=token, accepted=accepted), mock.patch.object(
                    control, "route_mapped_navigation_key", return_value=accepted
                ) as route, mock.patch.object(win32_input, "send_key_combo_tap") as inject:
                    self.app._apply_button_action(action)
                    route.assert_called_once_with(vk)
                    self.assertEqual(inject.call_count, 0 if accepted else 1)

    def test_app_switcher_cleanup_tracks_all_persistent_switcher_keys(self):
        with mock.patch.object(
            win32_input,
            "send_app_switcher",
            side_effect=win32_input.InputCleanupIncompleteError("stuck"),
        ):
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.APP_SWITCHER)
            )

        self.assertEqual(
            self.app._button_key_release_pending,
            ("ctrl", "alt", "tab"),
        )
        self.assertEqual(len(self._button_input_release_timers), 1)

    def test_element_navigation_action_uses_the_isolated_controller(self):
        result = app_module.element_navigation_control_windows.ToggleResult(
            app_module.element_navigation_control_windows.ToggleResultKind.DELIVERED,
            321,
        )
        with mock.patch.object(
            app_module.element_navigation_control_windows,
            "toggle_element_navigation",
            return_value=result,
        ) as toggle:
            self.app._apply_button_action(
                key_mapping.ButtonAction(
                    key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE
                )
            )

        toggle.assert_called_once_with()

    def test_interception_element_navigation_still_routes_to_the_toggle(self):
        # Element navigation is not a plain key, so it can never be delivered
        # by Interception.  A driver-level inject mode must not swallow it as
        # "interception_unsupported_action"; it still has to reach the toggle
        # controller.
        result = app_module.element_navigation_control_windows.ToggleResult(
            app_module.element_navigation_control_windows.ToggleResultKind.DELIVERED,
            321,
        )
        with mock.patch.object(
            app_module.element_navigation_control_windows,
            "toggle_element_navigation",
            return_value=result,
        ) as toggle, mock.patch.object(
            self.app, "_apply_button_action_interception"
        ) as interception, mock.patch.object(
            app_module.injection_backend, "backend_for"
        ) as backend_for:
            self.app._apply_button_action(
                key_mapping.ButtonAction(
                    key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE,
                    inject_mode=key_mapping.INJECT_MODE_INTERCEPTION,
                )
            )

        toggle.assert_called_once_with()
        interception.assert_not_called()
        backend_for.assert_not_called()

    def test_element_navigation_failure_does_not_stop_button_processing(self):
        with mock.patch.object(
            app_module.element_navigation_control_windows,
            "toggle_element_navigation",
            side_effect=RuntimeError("simulated companion failure"),
        ), self.assertLogs(level="ERROR") as captured:
            self.app._apply_button_action(
                key_mapping.ButtonAction(
                    key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE
                )
            )

        self.assertTrue(
            any("element navigation toggle failed" in line for line in captured.output)
        )

    def test_element_navigation_diagnostic_matches_the_dispatch_result(self):
        control = app_module.element_navigation_control_windows
        cases = [
            (control.ToggleResult(kind, 321, error="unavailable"),
             kind != control.ToggleResultKind.FAILED, "")
            for kind in control.ToggleResultKind
        ]
        cases.append((RuntimeError("simulated companion failure"), False, "RuntimeError"))
        for result, success, error_type in cases:
            with self.subTest(result=result), mock.patch.object(
                control, "toggle_element_navigation",
                side_effect=result if isinstance(result, Exception) else None,
                return_value=result,
            ), mock.patch.object(self.app._diagnostic_trace, "emit") as emit:
                self.app._apply_button_action(key_mapping.ButtonAction(
                    key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE))
                outcomes = [call.kwargs for call in emit.call_args_list
                            if call.args[0] == "action_result"]
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(outcomes[0]["success"], success)
                self.assertEqual(outcomes[0]["error"],
                                 "" if success else "element_navigation_failed")
                self.assertEqual(outcomes[0]["error_type"], error_type)

    def test_application_diagnostic_matches_launch_availability(self):
        for available in (False, True):
            with self.subTest(available=available), mock.patch.object(
                app_module, "open_configured_application", return_value=available,
            ) as launch, mock.patch.object(self.app._diagnostic_trace, "emit") as emit:
                action = key_mapping.ButtonAction(key_mapping.ActionKind.OPEN_CODEX)
                self.app._apply_button_action(action)
                launch.assert_called_once_with(action)
                outcomes = [call.kwargs for call in emit.call_args_list
                            if call.args[0] == "action_result"]
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(outcomes[0]["success"], available)
                self.assertEqual(outcomes[0]["error"],
                                 "" if available else "application_unavailable")

    def test_incomplete_button_rollback_is_released_before_the_next_action(self):
        original_up = win32_input.send_arrow_up
        original_down = win32_input.send_arrow_down
        original_release = win32_input.send_key_combo_up
        releases = []
        actions = []
        win32_input.send_arrow_up = lambda: (_ for _ in ()).throw(
            win32_input.InputCleanupIncompleteError("simulated stuck arrow key")
        )
        win32_input.send_arrow_down = lambda: actions.append("arrow_down")
        win32_input.send_key_combo_up = lambda keys: releases.append(tuple(keys))
        try:
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_UP)
            )
            self.assertEqual(self.app._button_key_release_pending, ("up",))

            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_DOWN)
            )
        finally:
            win32_input.send_arrow_up = original_up
            win32_input.send_arrow_down = original_down
            win32_input.send_key_combo_up = original_release

        self.assertEqual(releases, [("up",)])
        self.assertEqual(actions, ["arrow_down"])
        self.assertIsNone(self.app._button_key_release_pending)

    def test_pending_button_release_blocks_new_actions_when_retry_is_incomplete(self):
        original_down = win32_input.send_arrow_down
        original_release = win32_input.send_key_combo_up
        actions = []
        self.app._button_key_release_pending = ("ctrl", "l")
        win32_input.send_arrow_down = lambda: actions.append("arrow_down")
        win32_input.send_key_combo_up = lambda _keys: (_ for _ in ()).throw(
            win32_input.InputCleanupIncompleteError("still stuck")
        )
        try:
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_DOWN)
            )
        finally:
            win32_input.send_arrow_down = original_down
            win32_input.send_key_combo_up = original_release

        self.assertEqual(actions, [])
        self.assertEqual(self.app._button_key_release_pending, ("ctrl", "l"))

    def test_incomplete_button_release_retries_without_another_action(self):
        with mock.patch.object(
            win32_input,
            "send_arrow_up",
            side_effect=win32_input.InputCleanupIncompleteError("stuck"),
        ), mock.patch.object(
            win32_input,
            "send_key_combo_up",
        ) as send_up:
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_UP)
            )
            self.assertEqual(len(self._button_input_release_timers), 1)
            self._button_input_release_timers[0].fire()

        send_up.assert_called_once_with(("up",))
        self.assertIsNone(self.app._button_key_release_pending)
        self.assertIsNone(self.app._button_input_release_retry_timer)
        self.assertEqual(
            self.app._button_input_release_retry_delay,
            app_module._BUTTON_INPUT_RELEASE_RETRY_INITIAL_SECONDS,
        )

    def test_repeated_release_failures_keep_only_one_live_retry(self):
        self.app._button_key_release_pending = ("up",)
        with mock.patch.object(
            win32_input,
            "send_key_combo_up",
            side_effect=win32_input.InputCleanupIncompleteError("still stuck"),
        ):
            self.assertFalse(self.app._release_pending_button_keys())
            first_timer = self.app._button_input_release_retry_timer
            self.assertFalse(self.app._release_pending_button_keys())
            self.assertIs(self.app._button_input_release_retry_timer, first_timer)
            self.assertEqual(len(self._button_input_release_timers), 1)

            self._button_input_release_timers[0].fire()
            second_timer = self.app._button_input_release_retry_timer
            self.assertIsNot(second_timer, first_timer)
            self.assertEqual(len(self._button_input_release_timers), 2)

            self._button_input_release_timers[0].fire()
            self.assertIs(self.app._button_input_release_retry_timer, second_timer)
            self.assertEqual(len(self._button_input_release_timers), 2)

    def test_key_and_mouse_release_debt_share_one_retry_timer(self):
        self.app._button_key_release_pending = ("up",)
        self.app._button_mouse_release_pending = "left"
        with mock.patch.object(
            win32_input,
            "send_key_combo_up",
            side_effect=win32_input.InputCleanupIncompleteError("key stuck"),
        ), mock.patch.object(
            win32_input,
            "send_mouse_button_up",
            side_effect=win32_input.InputCleanupIncompleteError("mouse stuck"),
        ):
            self.assertFalse(self.app._release_pending_button_inputs())

        self.assertEqual(len(self._button_input_release_timers), 1)
        self.assertIs(
            self.app._button_input_release_retry_timer,
            self._button_input_release_timers[0],
        )

    def test_mouse_actions_dispatch_physical_buttons(self):
        button_calls = []
        with mock.patch.object(
            win32_input,
            "send_mouse_button_click",
            side_effect=lambda button: button_calls.append(button),
        ):
            for action_kind in (
                key_mapping.ActionKind.MOUSE_LEFT_CLICK,
                key_mapping.ActionKind.MOUSE_RIGHT_CLICK,
                key_mapping.ActionKind.MOUSE_MIDDLE_CLICK,
                key_mapping.ActionKind.MOUSE_X1_CLICK,
                key_mapping.ActionKind.MOUSE_X2_CLICK,
            ):
                self.app._apply_button_action(key_mapping.ButtonAction(action_kind))

        self.assertEqual(button_calls, ["left", "right", "middle", "x1", "x2"])

    def test_old_wheel_mapping_becomes_page_key(self):
        expected_keys = {
            key_mapping.ActionKind.MOUSE_WHEEL_UP: ("pageup",),
            key_mapping.ActionKind.MOUSE_WHEEL_DOWN: ("pagedown",),
        }
        for button_id in ("mic", "power", "up", "down", "left", "right", "ok", "back",
                          "volume_up", "volume_down", "home", "menu", "tv"):
            for kind, keys in expected_keys.items():
                with self.subTest(button_id=button_id, kind=kind):
                    self.app._bindings["bindings"][button_id] = {"kind": kind.value, "keys": []}
                    action = key_mapping.button_action_for(
                        self.app._bindings, button_id, key_mapping.ButtonTrigger.SINGLE_CLICK
                    )
                    self.assertEqual(action.kind, key_mapping.ActionKind.KEY_COMBO)
                    self.assertEqual(action.keys, keys)

    def test_every_visible_action_option_saves_and_has_an_execution_route(self):
        senders = (
            "send_key_combo_tap", "send_escape", "send_return", "send_arrow_up",
            "send_arrow_down", "send_arrow_left", "send_arrow_right",
            "send_delete_backward", "send_show_desktop", "send_context_menu",
            "send_app_switcher", "send_volume_up", "send_volume_down",
            "send_volume_mute", "send_play_pause", "send_mouse_button_click",
        )
        with ExitStack() as stack:
            effects = [stack.enter_context(mock.patch.object(win32_input, name)) for name in senders]
            effects.append(stack.enter_context(mock.patch.object(
                app_module, "open_configured_application", return_value=True
            )))
            effects.append(stack.enter_context(mock.patch.object(
                app_module.element_navigation_control_windows,
                "toggle_element_navigation",
                return_value=app_module.element_navigation_control_windows.ToggleResult(
                    app_module.element_navigation_control_windows.ToggleResultKind.DELIVERED, 321
                ),
            )))
            stack.enter_context(mock.patch.object(
                app_module.element_navigation_control_windows,
                "route_mapped_navigation_key", return_value=False
            ))
            for option in settings_ui._PRESET_KEY_COMBOS:
                with self.subTest(option=option):
                    self.assertEqual(settings_ui.button_action_validation_message(
                        "up", "single_click", option
                    ), "")
                    self.assertEqual(settings_ui.button_action_validation_message(
                        "up", "double_click", option
                    ), "")
                    action = settings_ui._display_to_action(option)
                    if action.kind in (
                        key_mapping.ActionKind.DISABLED,
                        # 语音模式切换在 _on_button_trigger 里就分发了，不走
                        # 注入后端；它的执行路径由专门的循环测试覆盖。
                        key_mapping.ActionKind.VOICE_MODE_CYCLE,
                    ):
                        continue
                    before = sum(effect.call_count for effect in effects)
                    self.app._apply_button_action(action)
                    self.assertEqual(sum(effect.call_count for effect in effects), before + 1)

    def test_physical_mouse_hold_skips_click_without_recording_cleanup_debt(self):
        with mock.patch.object(
            win32_input,
            "send_mouse_button_click",
            side_effect=win32_input.MouseButtonInUseError("physical hold"),
        ):
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.MOUSE_LEFT_CLICK)
            )

        self.assertIsNone(self.app._button_mouse_release_pending)

    def test_incomplete_mouse_click_is_retained_and_released_before_next_action(self):
        actions = []
        releases = []
        with mock.patch.object(
            win32_input,
            "send_mouse_button_click",
            side_effect=win32_input.InputCleanupIncompleteError("still down"),
        ), mock.patch.object(
            win32_input,
            "send_mouse_button_up",
            side_effect=lambda button: releases.append(button),
        ), mock.patch.object(
            win32_input,
            "send_arrow_down",
            side_effect=lambda: actions.append("arrow_down"),
        ):
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.MOUSE_X2_CLICK)
            )
            self.assertEqual(self.app._button_mouse_release_pending, "x2")

            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_DOWN)
            )

        self.assertEqual(releases, ["x2"])
        self.assertEqual(actions, ["arrow_down"])
        self.assertIsNone(self.app._button_mouse_release_pending)

    def test_pending_mouse_release_blocks_new_actions_when_retry_is_incomplete(self):
        actions = []
        self.app._button_mouse_release_pending = "left"
        with mock.patch.object(
            win32_input,
            "send_mouse_button_up",
            side_effect=win32_input.InputCleanupIncompleteError("still down"),
        ), mock.patch.object(
            win32_input,
            "send_arrow_down",
            side_effect=lambda: actions.append("arrow_down"),
        ):
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_DOWN)
            )

        self.assertEqual(actions, [])
        self.assertEqual(self.app._button_mouse_release_pending, "left")

    def test_fallback_safety_release_cannot_erase_a_new_pending_action_release(self):
        first_release_started = threading.Event()
        allow_first_release = threading.Event()
        action_attempted = threading.Event()
        release_calls = []
        release_calls_lock = threading.Lock()

        def release_keys(keys):
            with release_calls_lock:
                release_calls.append(tuple(keys))
                call_number = len(release_calls)
            if call_number == 1:
                first_release_started.set()
                allow_first_release.wait(1.0)

        def fail_action():
            action_attempted.set()
            raise win32_input.InputCleanupIncompleteError("simulated stuck down key")

        with mock.patch.object(
            win32_input,
            "send_key_combo_up",
            side_effect=release_keys,
        ), mock.patch.object(
            win32_input,
            "send_arrow_down",
            side_effect=fail_action,
        ):
            fallback_thread = threading.Thread(
                target=lambda: self.app._release_raw_fallback_keyups(
                    {"up"},
                    reason="test_handover",
                )
            )
            fallback_thread.start()
            self.assertTrue(first_release_started.wait(1.0))

            action_thread = threading.Thread(
                target=lambda: self.app._apply_button_action(
                    key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_DOWN)
                )
            )
            action_thread.start()
            self.assertFalse(action_attempted.wait(0.1))

            allow_first_release.set()
            fallback_thread.join(1.0)
            action_thread.join(1.0)

        self.assertFalse(fallback_thread.is_alive())
        self.assertFalse(action_thread.is_alive())
        self.assertEqual(release_calls, [("up",)])
        self.assertEqual(self.app._button_key_release_pending, ("down",))

    def test_open_app_action_uses_application_executor(self):
        calls = []
        original = getattr(app_module, "open_configured_application", None)
        app_module.open_configured_application = lambda action: calls.append(action.kind)
        try:
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.OPEN_CODEX)
            )
        finally:
            if original is None:
                delattr(app_module, "open_configured_application")
            else:
                app_module.open_configured_application = original

        self.assertEqual(calls, [key_mapping.ActionKind.OPEN_CODEX])

    def test_one_physical_direction_press_holds_one_mapping_action(self):
        with mock.patch.object(win32_input, "send_arrow_up") as tap:
            self.app._on_button_event("up", True, event_source="hid")
            self.app._on_button_event("up", True, event_source="hid_tap")
            self.app._on_button_event("up", True, event_source="hid")
            self.app._on_button_event("up", False, event_source="hid_tap")
            self.app._on_button_event("up", False, event_source="hid")

        # Duplicate physical edges collapse into a single hold: one click is
        # delivered and the repeats are not doubled.
        tap.assert_called_once_with()

    def test_direct_direction_edge_arms_global_hook_before_mapping_injection(self):
        up_usage = next(
            usage
            for usage, button in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button == "up"
        )
        expected_key = frida_compat.TAP_DIRECTION_USAGE_TO_KEY[up_usage]
        order = []
        physicalizer = mock.Mock()
        physicalizer.record_rc003_direction_edge.side_effect = (
            lambda *args: order.append(("global_edge", args)) or True
        )
        self.app._voice_key_physicalizer = physicalizer

        with mock.patch.object(
            app_module.element_navigation_control_windows,
            "record_rc003_direction_edge",
        ) as navigation_fallback, mock.patch.object(
            win32_input,
            "send_arrow_up",
            side_effect=lambda: order.append(("mapping",)),
        ):
            self.app._on_direct_hid_report(
                1,
                up_usage.to_bytes(2, "little") + b"\x00\x00\x00\x00",
            )
            self.app._on_direct_hid_report(1, b"\x00" * 6)

        self.assertEqual(
            order,
            [
                ("global_edge", (*expected_key, True)),
                ("mapping",),
                ("global_edge", (*expected_key, False)),
            ],
        )
        navigation_fallback.assert_not_called()

    def test_direct_direction_edge_is_consumed_by_the_process_hook(self):
        left_usage = next(
            usage
            for usage, button in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button == "left"
        )
        vk, scan_code, extended = frida_compat.TAP_DIRECTION_USAGE_TO_KEY[
            left_usage
        ]
        physicalizer = (
            app_module.voice_key_physicalizer_windows.VoiceKeyPhysicalizer()
        )
        self.app._voice_key_physicalizer = physicalizer
        event = app_module.voice_key_physicalizer_windows.KBDLLHOOKSTRUCT(
            vkCode=vk,
            scanCode=scan_code,
            flags=(
                app_module.voice_key_physicalizer_windows.LLKHF_EXTENDED
                if extended
                else 0
            ),
            time=0,
            dwExtraInfo=0,
        )

        with mock.patch.object(win32_input, "send_arrow_left"):
            self.app._on_direct_hid_report(
                1,
                left_usage.to_bytes(2, "little") + b"\x00\x00\x00\x00",
            )
            self.assertTrue(
                physicalizer.consume_rc003_direction_event(event, True)
            )
            self.app._on_direct_hid_report(1, b"\x00" * 6)
            self.assertTrue(
                physicalizer.consume_rc003_direction_event(event, False)
            )

    def test_direction_edge_uses_navigation_fallback_without_global_hook(self):
        up_usage = next(
            usage
            for usage, button in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button == "up"
        )
        expected_key = frida_compat.TAP_DIRECTION_USAGE_TO_KEY[up_usage]
        order = []

        with mock.patch.object(
            app_module.element_navigation_control_windows,
            "record_rc003_direction_edge",
            side_effect=lambda *args: order.append(("fallback", args)) or True,
        ), mock.patch.object(
            win32_input,
            "send_arrow_up",
            side_effect=lambda: order.append(("mapping",)),
        ):
            self.app._on_direct_hid_report(
                1,
                up_usage.to_bytes(2, "little") + b"\x00\x00\x00\x00",
            )
            self.app._on_direct_hid_report(1, b"\x00" * 6)

        self.assertEqual(
            order,
            [
                ("fallback", (*expected_key, True)),
                ("mapping",),
                ("fallback", (*expected_key, False)),
            ],
        )

    def test_custom_combo_on_direction_button_holds_its_combo(self):
        timers = self._use_recorded_repeat_timers()
        self.app._bindings["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.KEY_COMBO,
            ("shift", "3"),
        ).to_dict()

        downs = []
        ups = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda keys: downs.append(tuple(keys)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda keys: ups.append(tuple(keys)),
        ), mock.patch.object(win32_input, "send_key_combo_tap") as tap:
            self.app._on_button_event("up", True, event_source="hid_tap")
            self.assertEqual(timers[-1].delay, 0.350)
            self.app._on_button_event("up", False, event_source="hid_tap")

        # A custom key combo is held like any other key: one sustained
        # key-down/key-up pair with key-down replays in between - never a tap.
        tap.assert_not_called()
        self.assertEqual(downs, [("shift", "3")])
        self.assertEqual(ups, [("shift", "3")])

    def test_legacy_remote_combo_configuration_keeps_single_actions(self):
        self.app._bindings["bindings"]["tv"] = {
            "kind": "escape",
            "keys": [],
        }
        self.app._bindings["bindings"]["up"] = {
            "kind": "arrow_up",
            "keys": [],
        }
        self.app._bindings["combo_bindings"] = {
            "modifier": "tv",
            "bindings": {"up": {"kind": "return", "keys": []}},
            "display_notes": {},
        }

        downs = []
        ups = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda keys: downs.append(tuple(keys)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda keys: ups.append(tuple(keys)),
        ), mock.patch.object(win32_input, "send_arrow_up") as up_action, mock.patch.object(
            win32_input, "send_return"
        ) as combo_action:
            self.app._on_button_event("tv", True, event_source="hid_tap")
            self.app._on_button_event("up", True, event_source="hid_tap")
            self.app._on_button_event("up", False, event_source="hid_tap")
            self.app._on_button_event("tv", False, event_source="hid_tap")

        combo_action.assert_not_called()
        # The tv modifier never becomes a combo prefix, so it holds its own
        # escape key for the whole physical hold.  The up button is a SendInput
        # direction key, so it delivers one full click instead of a hold.
        self.assertEqual(downs, [("escape",)])
        self.assertEqual(ups, [("escape",)])
        up_action.assert_called_once_with()

    def test_direct_snapshot_holds_old_modifier_with_new_target(self):
        self.app._bindings["bindings"]["tv"] = {
            "kind": "escape",
            "keys": [],
        }
        self.app._bindings["bindings"]["up"] = {
            "kind": "arrow_up",
            "keys": [],
        }
        self.app._bindings["combo_bindings"] = {
            "modifier": "tv",
            "bindings": {"up": {"kind": "return", "keys": []}},
            "display_notes": {},
        }
        tv_usage = next(
            usage
            for usage, button in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button == "tv"
        )
        up_usage = next(
            usage
            for usage, button in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button == "up"
        )

        downs = []
        ups = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda keys: downs.append(tuple(keys)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda keys: ups.append(tuple(keys)),
        ), mock.patch.object(win32_input, "send_arrow_up") as up_action, mock.patch.object(
            win32_input, "send_return"
        ) as combo_action:
            self.app._on_direct_hid_report(
                1, tv_usage.to_bytes(2, "little") + b"\x00\x00\x00\x00"
            )
            self.app._on_direct_hid_report(
                1, up_usage.to_bytes(2, "little") + b"\x00\x00\x00\x00"
            )
            self.app._on_direct_hid_report(1, b"\x00" * 6)

        self.assertEqual(downs, [("escape",)])
        self.assertEqual(ups, [("escape",)])
        up_action.assert_called_once_with()
        combo_action.assert_not_called()

    def test_direct_multi_usage_snapshot_keeps_independent_single_actions(self):
        self.app._bindings["bindings"]["menu"] = {
            "kind": "context_menu",
            "keys": [],
        }
        self.app._bindings["bindings"]["up"] = {
            "kind": "arrow_up",
            "keys": [],
        }
        self.app._bindings["combo_bindings"] = {
            "modifier": "menu",
            "bindings": {"up": {"kind": "return", "keys": []}},
            "display_notes": {},
        }
        menu_usage = next(
            usage
            for usage, button in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button == "menu"
        )
        up_usage = next(
            usage
            for usage, button in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button == "up"
        )
        payload = (
            up_usage.to_bytes(2, "little")
            + menu_usage.to_bytes(2, "little")
            + b"\x00\x00"
        )

        downs = []
        ups = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda keys: downs.append(tuple(keys)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda keys: ups.append(tuple(keys)),
        ), mock.patch.object(win32_input, "send_arrow_up") as up_action, mock.patch.object(
            win32_input, "send_return"
        ) as combo_action:
            self.app._on_direct_hid_report(1, payload)
            self.app._on_direct_hid_report(1, b"\x00" * 6)

        combo_action.assert_not_called()
        # The menu button holds its own context-menu key for the whole physical
        # hold; the up button is a SendInput direction key, so it clicks once.
        self.assertEqual(downs, [("apps",)])
        self.assertEqual(ups, [("apps",)])
        up_action.assert_called_once_with()

    def test_unused_remote_combo_modifier_keeps_its_single_click(self):
        self.app._bindings["bindings"]["tv"] = {
            "kind": "escape",
            "keys": [],
        }
        self.app._bindings["combo_bindings"] = {
            "modifier": "tv",
            "bindings": {"up": {"kind": "return", "keys": []}},
            "display_notes": {},
        }

        downs = []
        ups = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda keys: downs.append(tuple(keys)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda keys: ups.append(tuple(keys)),
        ), mock.patch.object(win32_input, "send_return") as combo_action:
            self.app._on_button_event("tv", True)
            self.app._on_button_event("tv", False)

        combo_action.assert_not_called()
        self.assertEqual(downs, [("escape",)])
        self.assertEqual(ups, [("escape",)])

    def test_quicker_uri_action_uses_the_protocol_executor(self):
        action = key_mapping.ButtonAction(
            key_mapping.ActionKind.QUICKER_URI,
            uri="quicker:runaction:pin-window",
        )

        with mock.patch.object(
            app_module.action_executor, "open_quicker_uri"
        ) as launcher:
            self.app._apply_button_action(action)

        launcher.assert_called_once_with(action)

    def test_raw_keyboard_fallback_never_executes_a_mapping(self):
        self.app._direct_hid_interception_ready = False

        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release:
            self.app._on_raw_button_event("up", True, "keyboard")
            self.app._on_raw_button_event("up", False, "keyboard")

        press.assert_not_called()
        release.assert_not_called()
        self.assertEqual(self.app._raw_fallback_buttons_down, set())

    def test_raw_keyboard_fallback_tracks_both_edges_without_sticking(self):
        self.app._direct_hid_interception_ready = False

        self.app._on_raw_button_event("up", True, "keyboard")
        self.assertEqual(self.app._raw_fallback_buttons_down, {"up"})
        self.app._on_raw_button_event("up", False, "keyboard")

        self.assertEqual(self.app._raw_fallback_buttons_down, set())

    def test_unknown_physical_key_mapping_is_disabled_without_hid_interception(self):
        self.app._direct_hid_interception_ready = False
        self.app._direct_hid_interception_armed = False
        raw_down = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="up",
            windows_button_id=None,
            vkey=0xFF,
            make_code=0x7F,
            flags=0,
            message=0x0100,
        )
        raw_up = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=False,
            button_id="up",
            windows_button_id=None,
            vkey=0xFF,
            make_code=0x7F,
            flags=1,
            message=0x0101,
        )

        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures,
            "release",
        ) as release:
            self.app._on_raw_physical_event(raw_down)
            self.app._on_raw_button_event("up", True, "keyboard", None)
            self.app._on_raw_physical_event(raw_up)
            self.app._on_raw_button_event("up", False, "keyboard", None)

        press.assert_not_called()
        release.assert_not_called()
        self.assertEqual(self.app._raw_fallback_buttons_down, set())
        self.assertEqual(self.app._raw_fallback_physical_buttons_down, {})

        self.app._direct_hid_interception_ready = True
        with mock.patch.object(
            self.app._button_gestures,
            "press",
        ) as recovered_press, mock.patch.object(
            self.app._button_gestures,
            "release",
        ) as recovered_release:
            self.app._on_button_event("up", True, event_source="hid_tap")
            self.app._on_button_event("up", False, event_source="hid_tap")

        recovered_press.assert_called_once_with("up")
        recovered_release.assert_called_once_with("up")
        self.assertEqual(self.app._input_rearm_blocked_buttons, set())

    def test_direction_fallback_handles_cross_source_edges_without_mapping(self):
        self.app._direct_hid_interception_ready = False

        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release:
            self.app._on_raw_button_event("up", True, "hid")
            self.app._on_raw_button_event("up", False, "keyboard")
            self.app._on_raw_button_event("down", True, "keyboard")
            self.app._on_raw_button_event("down", False, "hid")

        press.assert_not_called()
        release.assert_not_called()
        self.assertEqual(self.app._raw_fallback_buttons_down, set())

    def test_raw_hid_fallback_never_executes_a_mapping(self):
        self.app._direct_hid_interception_ready = False
        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release:
            self.app._on_raw_button_event("ok", True, "hid")
            self.app._on_raw_button_event("ok", False, "hid")

        press.assert_not_called()
        release.assert_not_called()
        self.assertEqual(self.app._raw_fallback_buttons_down, set())

        self.app._direct_hid_interception_ready = True
        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release:
            self.app._on_raw_button_event("ok", True, "hid")
            self.app._on_raw_button_event("ok", False, "hid")

        press.assert_not_called()
        release.assert_not_called()

    def test_raw_hid_fallback_clears_when_keyboard_edge_arrives_last(self):
        self.app._direct_hid_interception_ready = False

        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release:
            self.app._on_raw_button_event("ok", True, "hid")
            self.app._on_raw_button_event("ok", False, "keyboard")

        press.assert_not_called()
        release.assert_not_called()
        self.assertEqual(self.app._raw_fallback_buttons_down, set())

    def test_every_raw_button_preserves_only_the_windows_original(self):
        self.app._direct_hid_interception_ready = False

        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release, mock.patch.object(
            win32_input, "send_voice_key_combo_down"
        ) as voice_down:
            for button_id in app_module._RAW_FALLBACK_KEY_TOKENS:
                with self.subTest(button_id=button_id):
                    self.app._on_raw_button_event(button_id, True, "hid")
                    self.app._on_raw_button_event(button_id, False, "keyboard")

        press.assert_not_called()
        release.assert_not_called()
        voice_down.assert_not_called()
        self.assertEqual(self.app._raw_fallback_buttons_down, set())

    def test_raw_corruption_cancels_active_buttons_until_real_release(self):
        self.app._direct_hid_interception_ready = False
        self.app._raw_fallback_buttons_down = {"up"}

        with mock.patch.object(win32_input, "send_key_combo_up") as release_keys, mock.patch.object(
            self.app._button_gestures, "reset"
        ) as gesture_reset, mock.patch.object(
            self.app._button_combos, "reset"
        ) as combo_reset:
            self.app._on_raw_input_corruption("hid_payload_invalid")

        release_keys.assert_called_once_with(("up",))
        gesture_reset.assert_called_once_with()
        combo_reset.assert_called_once_with()
        self.assertEqual(self.app._raw_fallback_buttons_down, set())
        self.assertEqual(self.app._input_rearm_blocked_buttons, {"up"})

        with mock.patch.object(self.app._button_gestures, "press") as press:
            self.app._on_raw_button_event("ok", True, "hid")
            self.app._on_raw_button_event("ok", False, "hid")
            self.app._on_raw_button_event("ok", True, "hid")

        press.assert_not_called()
        self.assertEqual(self.app._input_rearm_blocked_buttons, {"up"})
        self.assertEqual(self.app._raw_fallback_buttons_down, {"ok"})
        self.app._on_raw_button_event("ok", False, "hid")

    def test_raw_corruption_without_raw_owned_buttons_leaves_direct_input_alone(self):
        self.app._direct_hid_interception_ready = True

        with mock.patch.object(
            self.app._button_gestures, "reset"
        ) as gesture_reset, mock.patch.object(
            self.app._button_combos, "reset"
        ) as combo_reset:
            self.app._on_raw_input_corruption("keyboard_body_too_short")

        gesture_reset.assert_not_called()
        combo_reset.assert_not_called()

    def test_tap_arming_cancels_raw_owned_gesture_before_handover(self):
        self.app._direct_hid_interception_ready = False

        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release, mock.patch.object(
            self.app._button_gestures, "reset"
        ) as reset, mock.patch.object(
            win32_input, "send_key_combo_up"
        ) as release_keys:
            self.app._on_raw_button_event("ok", True, "hid")
            self.app._on_hid_tap_status(
                app_module.frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
                "hid_interception_armed",
            )
            self.app._on_raw_button_event("ok", False, "keyboard")

        press.assert_not_called()
        release.assert_not_called()
        reset.assert_called_once_with()
        release_keys.assert_called_once_with(("enter",))
        self.assertTrue(self.app._direct_hid_interception_armed)
        self.assertFalse(self.app._direct_hid_interception_ready)
        self.assertEqual(self.app._input_rearm_blocked_buttons, set())

    def test_tap_handover_cannot_reset_then_receive_a_late_raw_press(self):
        self.app._direct_hid_interception_ready = False
        raw_paused = threading.Event()
        allow_raw = threading.Event()
        calls = []

        def pause_raw(_source):
            raw_paused.set()
            allow_raw.wait(1.0)

        with mock.patch.object(
            self.app, "_record_runtime_button", side_effect=pause_raw
        ), mock.patch.object(
            self.app._button_gestures,
            "press",
            side_effect=lambda button: calls.append(("press", button)),
        ), mock.patch.object(
            self.app._button_gestures,
            "reset",
            side_effect=lambda: calls.append(("reset", None)),
        ):
            raw_worker = threading.Thread(
                target=self.app._on_raw_button_event,
                args=("ok", True, "hid"),
            )
            raw_worker.start()
            self.assertTrue(raw_paused.wait(1.0))

            handover_worker = threading.Thread(
                target=self.app._on_hid_tap_status,
                args=(
                    app_module.frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
                    "hid_interception_armed",
                ),
            )
            handover_worker.start()
            handover_worker.join(0.1)
            self.assertTrue(handover_worker.is_alive())

            allow_raw.set()
            raw_worker.join(1.0)
            handover_worker.join(1.0)

        self.assertFalse(raw_worker.is_alive())
        self.assertFalse(handover_worker.is_alive())
        self.assertEqual(calls, [("reset", None)])
        self.assertEqual(self.app._input_rearm_blocked_buttons, {"ok"})

    def test_late_raw_down_after_tap_arming_is_released_and_quarantined(self):
        self.app._direct_hid_interception_ready = False
        self.app._direct_hid_interception_armed = False
        usage = next(
            candidate
            for candidate, button_id in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "up"
        )
        raw_down = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="ok",
            windows_button_id="up",
            vkey=0x26,
            make_code=0x48,
            flags=0,
            message=0x0100,
        )
        direct_down = usage.to_bytes(2, "little") + b"\x00\x00\x00\x00"

        self.app._on_hid_tap_status(
            frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
            "hid_interception_armed",
        )
        with mock.patch.object(win32_input, "send_key_combo_up") as release_keys, mock.patch.object(
            self.app._button_gestures,
            "press",
        ) as press:
            self.app._on_raw_physical_event(raw_down)
            self.app._on_raw_physical_event(raw_down)
            self.app._on_raw_button_event("ok", True, "keyboard", "up")
            self.app._on_hid_tap_status(
                frida_compat.HidTapState.READY.value,
                "hid_interception_verified",
            )
            self.assertEqual(
                self.app._input_rearm_blocked_buttons,
                {"ok", "up"},
            )
            self.app._on_direct_hid_report(1, direct_down)

            release_keys.assert_called_once_with(("up",))
            press.assert_not_called()
            self.assertEqual(
                self.app._input_rearm_blocked_buttons,
                {"up"},
            )

            self.app._on_direct_hid_report(1, b"\x00" * 6)
            self.assertEqual(self.app._input_rearm_blocked_buttons, set())
            self.assertEqual(
                self.app._raw_fallback_physical_buttons_down,
                {},
            )
            self.app._on_direct_hid_report(1, direct_down)

        press.assert_called_once_with("up")

    def test_first_direct_snapshot_before_late_raw_down_is_quarantined(self):
        self.app._direct_hid_interception_ready = False
        self.app._direct_hid_interception_armed = False
        usage = next(
            candidate
            for candidate, button_id in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "up"
        )
        raw_down = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="ok",
            windows_button_id="up",
            vkey=0x26,
            make_code=0x48,
            flags=0,
            message=0x0100,
        )
        direct_down = usage.to_bytes(2, "little") + b"\x00\x00\x00\x00"
        up_vk = win32_keys.VK_CODES["up"]
        self.app._raw_windows_key_down_query = (
            lambda vk_code: vk_code == up_vk
        )

        with mock.patch.object(win32_input, "send_key_combo_up") as release_keys, mock.patch.object(
            self.app._button_gestures,
            "press",
        ) as press:
            self.app._on_hid_tap_status(
                frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
                "hid_interception_armed",
            )
            self.app._on_hid_tap_status(
                frida_compat.HidTapState.READY.value,
                "hid_interception_verified",
            )
            self.app._on_direct_hid_report(1, direct_down)
            self.app._on_raw_physical_event(raw_down)
            self.app._on_raw_button_event("ok", True, "keyboard", "up")

            release_keys.assert_called_once_with(("up",))
            press.assert_not_called()
            self.assertEqual(
                self.app._input_rearm_blocked_buttons,
                {"ok", "up"},
            )

            self.app._on_direct_hid_report(1, b"\x00" * 6)
            self.app._on_direct_hid_report(1, direct_down)

        press.assert_called_once_with("up")

    def test_late_raw_down_preserves_the_active_direct_gesture_until_hid_up(self):
        self.app._direct_hid_interception_ready = False
        self.app._direct_hid_interception_armed = False
        usage = next(
            candidate
            for candidate, button_id in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "up"
        )
        raw_down = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="ok",
            windows_button_id="up",
            vkey=0x26,
            make_code=0x48,
            flags=0,
            message=0x0100,
        )
        direct_down = usage.to_bytes(2, "little") + b"\x00\x00\x00\x00"
        direct_neutral = b"\x00" * 6

        self.app._on_hid_tap_status(
            frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
            "hid_interception_armed",
        )
        self.app._on_hid_tap_status(
            frida_compat.HidTapState.READY.value,
            "hid_interception_verified",
        )
        self.app._on_direct_hid_report(1, direct_neutral)

        with mock.patch.object(
            win32_input,
            "send_key_combo_up",
        ) as release_keys, mock.patch.object(
            self.app._button_gestures,
            "press",
        ) as press, mock.patch.object(
            self.app._button_gestures,
            "release",
        ) as release, mock.patch.object(
            self.app._button_gestures,
            "cancel_buttons",
        ) as cancel_buttons, mock.patch.object(
            self.app._button_combos,
            "cancel_buttons",
            return_value=set(),
        ):
            self.app._on_direct_hid_report(1, direct_down)
            self.app._on_raw_physical_event(raw_down)
            self.app._on_raw_physical_event(raw_down)
            self.app._on_direct_hid_report(1, direct_neutral)
            self.assertEqual(self.app._input_rearm_blocked_buttons, set())
            self.app._on_direct_hid_report(1, direct_down)

        self.assertEqual(press.call_args_list, [mock.call("up"), mock.call("up")])
        release.assert_called_once_with("up")
        cancel_buttons.assert_called_once_with({"ok"})
        release_keys.assert_called_once_with(("up",))
        self.assertFalse(self.app._direct_hid_handover_waiting_for_neutral)

    def test_late_raw_mic_duplicate_does_not_release_the_active_voice_hold(self):
        mic_usage = next(
            usage
            for usage, button_id in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "mic"
        )
        raw_down = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="mic",
            windows_button_id="f5",
            vkey=0x74,
            make_code=0x3F,
            flags=0,
            message=0x0100,
        )
        direct_down = mic_usage.to_bytes(2, "little") + b"\x00\x00\x00\x00"
        # A physical hold (long press) is what closes on release; a tap would
        # latch the session open instead.
        clock = [100.0]
        self.app._voice_gesture = voice_gesture.VoiceGestureLatch(
            clock=lambda: clock[0]
        )

        with mock.patch.object(
            win32_input, "send_voice_key_combo_down"
        ) as voice_down, mock.patch.object(
            win32_input, "send_voice_key_combo_up"
        ) as voice_up:
            self.app._on_direct_hid_report(1, direct_down)
            self.assertTrue(self.app._voice_shortcut.controller.active)

            self.app._on_raw_physical_event(raw_down)
            self.app._on_raw_physical_event(raw_down)

            self.assertTrue(self.app._voice_shortcut.controller.active)
            voice_up.assert_not_called()
            clock[0] += voice_gesture.VoiceGestureLatch.DEFAULT_TAP_MAX_SECONDS + 0.01
            self.app._on_direct_hid_report(1, b"\x00" * 6)

        voice_down.assert_called_once()
        voice_up.assert_called_once()

    def test_audio_started_voice_hold_survives_raw_before_direct_hid(self):
        mic_usage = next(
            usage
            for usage, button_id in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "mic"
        )
        raw_down = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="mic",
            windows_button_id="f5",
            vkey=0x74,
            make_code=0x3F,
            flags=0,
            message=0x0100,
        )
        direct_down = mic_usage.to_bytes(2, "little") + b"\x00\x00\x00\x00"
        # The physical release has to read as a hold (long press) so it closes
        # the session; a tap would latch it open instead.
        clock = [100.0]
        self.app._voice_gesture = voice_gesture.VoiceGestureLatch(
            clock=lambda: clock[0]
        )

        with mock.patch.object(
            win32_input, "send_voice_key_combo_down"
        ) as voice_down, mock.patch.object(
            win32_input, "send_voice_key_combo_up"
        ) as voice_up:
            self.app._on_control_event(AudioStarted(session_id=1))
            self.assertTrue(self.app._voice_shortcut.controller.active)
            self.assertTrue(self.app._voice_pcm_forwarding_enabled)

            self.app._on_raw_physical_event(raw_down)
            self.assertTrue(self.app._voice_shortcut.controller.active)
            self.assertTrue(self.app._voice_pcm_forwarding_enabled)
            voice_up.assert_not_called()

            self.app._on_direct_hid_report(1, direct_down)
            self.assertIn("hid_tap", self.app._voice_mic_gesture_sources_down)
            self.app._on_pcm_frame([1, 2, 3])
            self.assertTrue(self.app._voice_audio.writer.flush(1.0).completed)
            self.assertEqual(self.app._voice_audio.sink.write_calls, [(1, 2, 3)])

            clock[0] += voice_gesture.VoiceGestureLatch.DEFAULT_TAP_MAX_SECONDS + 0.01
            self.app._on_direct_hid_report(1, b"\x00" * 6)
            self.assertFalse(self.app._voice_shortcut.controller.active)
            self.assertFalse(self.app._voice_pcm_forwarding_enabled)
            self.app._on_control_event(AudioStopped())

        voice_down.assert_called_once()
        voice_up.assert_called_once()

    def test_late_raw_for_other_button_does_not_cancel_active_direct_hold(self):
        up_usage = next(
            usage
            for usage, button_id in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "up"
        )
        self.app._direct_hid_interception_ready = True
        self.app._direct_hid_interception_armed = True
        self.app._direct_hid_usages = {up_usage}
        raw_right = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="right",
            windows_button_id="right",
            vkey=0x27,
            make_code=0x4D,
            flags=0,
            message=0x0100,
        )

        with mock.patch.object(
            self.app._button_gestures,
            "cancel_buttons",
        ) as cancel_buttons, mock.patch.object(
            self.app._button_gestures,
            "reset",
        ) as gesture_reset, mock.patch.object(
            self.app._button_combos,
            "cancel_buttons",
            return_value=set(),
        ) as cancel_combo, mock.patch.object(
            self.app._button_combos,
            "reset",
        ) as combo_reset, mock.patch.object(
            win32_input,
            "send_key_combo_up",
        ) as release_keys:
            self.app._on_raw_physical_event(raw_right)

        cancel_combo.assert_called_once_with({"right"})
        cancel_buttons.assert_called_once_with({"right"})
        gesture_reset.assert_not_called()
        combo_reset.assert_not_called()
        release_keys.assert_called_once_with(("right",))
        self.assertEqual(self.app._direct_hid_usages, {up_usage})
        self.assertEqual(self.app._input_rearm_blocked_buttons, {"right"})
        self.assertFalse(self.app._direct_hid_handover_waiting_for_neutral)

    def test_late_raw_for_active_direct_key_keeps_following_hid_edges_live(self):
        usages = {
            button_id: usage
            for usage, button_id in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id in {"tv", "up", "right"}
        }
        self.app._direct_hid_interception_ready = True
        self.app._direct_hid_interception_armed = True
        self.app._direct_hid_usages = {usages["tv"], usages["up"]}
        raw_up = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="ok",
            windows_button_id="up",
            vkey=0x26,
            make_code=0x48,
            flags=0,
            message=0x0100,
        )

        with mock.patch.object(win32_input, "send_key_combo_up"):
            self.app._on_raw_physical_event(raw_up)

        self.assertFalse(self.app._direct_hid_handover_waiting_for_neutral)
        self.assertEqual(
            self.app._input_rearm_blocked_buttons,
            {"ok"},
        )
        tv_right_report = (
            usages["tv"].to_bytes(2, "little")
            + usages["right"].to_bytes(2, "little")
            + b"\x00\x00"
        )
        with mock.patch.object(self.app, "_on_button_event") as button_event:
            self.app._on_direct_hid_report(1, tv_right_report)
        self.assertEqual(
            button_event.call_args_list,
            [
                mock.call(
                    "up",
                    False,
                    event_source="hid_tap",
                    hid_usage=usages["up"],
                ),
                mock.call(
                    "right",
                    True,
                    event_source="hid_tap",
                    hid_usage=usages["right"],
                ),
            ],
        )
        self.assertFalse(self.app._direct_hid_handover_waiting_for_neutral)
        self.assertEqual(self.app._input_rearm_blocked_buttons, set())

        self.app._on_direct_hid_report(1, b"\x00" * 6)

        self.assertFalse(self.app._direct_hid_handover_waiting_for_neutral)
        self.assertEqual(self.app._input_rearm_blocked_buttons, set())

    def test_raw_release_after_failed_handover_unblocks_physical_alias(self):
        self.app._direct_hid_interception_ready = False
        self.app._direct_hid_interception_armed = False
        raw_down = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="ok",
            windows_button_id="up",
            vkey=0x26,
            make_code=0x48,
            flags=0,
            message=0x0100,
        )
        raw_up = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=False,
            button_id="ok",
            windows_button_id="up",
            vkey=0x26,
            make_code=0x48,
            flags=1,
            message=0x0101,
        )

        self.app._on_hid_tap_status(
            frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
            "hid_interception_armed",
        )
        with mock.patch.object(win32_input, "send_key_combo_up") as release_keys:
            self.app._on_raw_physical_event(raw_down)
            self.app._on_raw_button_event("ok", True, "keyboard", "up")
            self.app._on_hid_tap_status(
                frida_compat.HidTapState.FAILED.value,
                "gadget_connection_closed",
            )
            self.app._on_raw_physical_event(raw_up)
            self.app._on_raw_button_event("ok", False, "keyboard", "up")

        release_keys.assert_called_once_with(("up",))
        self.assertEqual(self.app._input_rearm_blocked_buttons, set())

    def test_tap_arming_releases_a_windows_fallback_key_and_blocks_the_hold(self):
        self.app._direct_hid_interception_ready = False
        self.app._on_raw_button_event("up", True, "keyboard")

        with mock.patch.object(win32_input, "send_key_combo_up") as release_keys:
            self.app._on_hid_tap_status(
                app_module.frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
                "hid_interception_armed",
            )

        release_keys.assert_called_once_with(("up",))
        self.assertEqual(self.app._raw_fallback_buttons_down, set())
        self.assertEqual(self.app._input_rearm_blocked_buttons, {"up"})

        self.app._on_raw_button_event("up", False, "keyboard")
        self.assertEqual(self.app._input_rearm_blocked_buttons, set())

    def test_raw_fallback_stays_unmapped_if_armed_tap_fails_before_ready(self):
        self.app._direct_hid_interception_ready = False
        self.app._on_hid_tap_status(
            app_module.frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
            "hid_interception_armed",
        )
        self.app._on_hid_tap_status(
            app_module.frida_compat.HidTapState.FAILED.value,
            "gadget_connection_closed",
        )

        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release:
            self.app._on_raw_button_event("ok", True, "hid")
            self.app._on_raw_button_event("ok", False, "keyboard")

        press.assert_not_called()
        release.assert_not_called()
        self.assertFalse(self.app._direct_hid_interception_armed)
        self.assertEqual(self.app._raw_fallback_buttons_down, set())

    def test_raw_fallback_hold_timeout_releases_windows_key_and_waits_for_up(self):
        self.app._direct_hid_interception_ready = False
        timers = []

        def timer_factory(_delay, callback):
            timer = _ManualTimer(callback)
            timers.append(timer)
            return timer

        self.app._raw_fallback_timer_factory = timer_factory
        with mock.patch.object(win32_input, "send_key_combo_up") as release_keys, mock.patch.object(
            self.app._button_gestures, "press"
        ) as press:
            self.app._on_raw_button_event("up", True, "keyboard")
            timers[0].fire()
            self.app._on_raw_button_event("up", True, "keyboard")
            self.app._on_raw_button_event("up", False, "keyboard")

        release_keys.assert_called_once_with(("up",))
        press.assert_not_called()
        self.assertEqual(self.app._raw_fallback_buttons_down, set())
        self.assertEqual(self.app._input_rearm_blocked_buttons, set())

    def test_rearm_guard_never_treats_elapsed_time_as_a_release(self):
        self.app._input_rearm_blocked_buttons = {"ok"}

        with mock.patch.object(app_module.time, "monotonic", return_value=101.0), mock.patch.object(
            self.app._button_gestures, "press"
        ) as press:
            self.app._on_button_event("ok", True, event_source="hid_tap")

        press.assert_not_called()
        self.assertEqual(self.app._input_rearm_blocked_buttons, {"ok"})
        self.app._on_button_event("ok", False, event_source="hid_tap")
        self.assertEqual(self.app._input_rearm_blocked_buttons, set())

        with mock.patch.object(self.app._button_gestures, "press") as press:
            self.app._on_button_event("ok", True, event_source="hid_tap")
        press.assert_called_once_with("ok")

    def test_clean_handover_keeps_the_first_direct_press(self):
        self.app._direct_hid_interception_ready = False
        self.app._direct_hid_interception_armed = False
        usage = next(
            candidate
            for candidate, button_id in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "up"
        )
        down_report = usage.to_bytes(2, "little") + b"\x00\x00\x00\x00"

        self.app._on_hid_tap_status(
            frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
            "hid_interception_armed",
        )
        self.app._on_hid_tap_status(
            frida_compat.HidTapState.READY.value,
            "hid_interception_verified",
        )
        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures,
            "release",
        ) as release:
            self.app._on_direct_hid_report(1, down_report)
            self.app._on_direct_hid_report(1, b"\x00" * 6)

        press.assert_called_once_with("up")
        release.assert_called_once_with("up")

    def test_unknown_windows_state_quarantines_until_direct_neutral(self):
        self.app._direct_hid_interception_ready = False
        self.app._direct_hid_interception_armed = False
        self.app._raw_windows_key_down_query = mock.Mock(
            side_effect=OSError("state unavailable")
        )
        usage = next(
            candidate
            for candidate, button_id in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "up"
        )
        down_report = usage.to_bytes(2, "little") + b"\x00\x00\x00\x00"

        self.app._on_hid_tap_status(
            frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
            "hid_interception_armed",
        )
        self.app._on_hid_tap_status(
            frida_compat.HidTapState.READY.value,
            "hid_interception_verified",
        )
        with mock.patch.object(self.app._button_gestures, "press") as press:
            self.app._on_direct_hid_report(1, down_report)
            press.assert_not_called()
            self.app._on_direct_hid_report(1, b"\x00" * 6)
            self.app._on_direct_hid_report(1, down_report)

        press.assert_called_once_with("up")

    def test_direct_hid_report_emits_one_complete_hold(self):
        usage = next(
            usage
            for usage, button_id in app_module.frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "up"
        )
        down_report = usage.to_bytes(2, "little") + b"\x00\x00\x00\x00"
        up_report = b"\x00" * 6

        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release:
            self.app._on_direct_hid_report(1, down_report)
            self.app._on_direct_hid_report(1, down_report)
            self.app._on_direct_hid_report(1, up_report)

        press.assert_called_once_with("up")
        release.assert_called_once_with("up")
        self.assertEqual(self.app._direct_hid_usages, set())

    def test_hid_trace_links_raw_reports_decisions_and_logical_edges(self):
        trace = self.app._diagnostic_trace
        trace.set_enabled(True)
        tap = frida_compat.RC003HidReportTap(
            self.app._on_direct_hid_report,
            enabled=False,
            diagnostic_trace=trace,
        )
        down = bytes.fromhex("010000350000000000")
        neutral = bytes.fromhex("010000000000000000")
        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release:
            for report in (down, down, neutral, down, neutral):
                tap._handle_ioctl_output(report)
        trace.close()
        self.assertEqual(press.call_args_list, [mock.call("tv"), mock.call("tv")])
        self.assertEqual(release.call_args_list, [mock.call("tv"), mock.call("tv")])
        records = [json.loads(line) for line in trace.path.read_text().splitlines()]
        raw = [row for row in records if row["event"] == "hid_tap_report"]
        callbacks = [row for row in records if row["event"] == "hid_report"]
        decisions = [row for row in records if row["event"] == "hid_report_decision"]
        edges = [row for row in records if row["event"] == "button_edge"]
        self.assertEqual([row["tap_report_seq"] for row in raw], [1, 2, 3, 4, 5])
        self.assertEqual([row["forwarded"] for row in raw], [True, False, True, True, True])
        self.assertEqual(raw[0]["raw_hex"], down.hex())
        self.assertEqual(raw[1]["decision"], "unchanged")
        expected_ids = [row["hid_report_id"] for row in raw if row["forwarded"]]
        for rows in (callbacks, decisions, edges):
            self.assertEqual([row["hid_report_id"] for row in rows], expected_ids)
        self.assertEqual([row["resulting_usages"] for row in decisions], [[53], [], [53], []])
        self.assertEqual([row["callback_seq"] for row in callbacks], [1, 2, 3, 4])

    def test_hid_trace_records_ignored_callback_without_an_edge(self):
        trace = self.app._diagnostic_trace
        trace.set_enabled(True)
        self.app._direct_hid_interception_ready = False
        with mock.patch.object(self.app._button_gestures, "press") as press:
            self.app._on_direct_hid_report(1, bytes.fromhex("350000000000"))
        trace.close()
        press.assert_not_called()
        records = [json.loads(line) for line in trace.path.read_text().splitlines()]
        decision = next(row for row in records if row["event"] == "hid_report_decision")
        self.assertEqual(decision["reason"], "tap_not_ready")
        self.assertEqual(decision["resulting_usages"], [])

    def test_direct_neutral_report_clears_handover_block(self):
        self.app._input_rearm_blocked_buttons = {"up", "ok"}

        self.app._on_direct_hid_report(1, b"\x00" * 6)

        self.assertEqual(self.app._input_rearm_blocked_buttons, set())

    def test_direct_snapshot_clears_only_blocked_buttons_that_are_absent(self):
        up_usage = next(
            usage
            for usage, button_id in app_module.frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "up"
        )
        self.app._input_rearm_blocked_buttons = {"up", "ok"}

        self.app._on_direct_hid_report(
            1,
            up_usage.to_bytes(2, "little") + b"\x00\x00\x00\x00",
        )

        self.assertEqual(self.app._input_rearm_blocked_buttons, {"up"})

    def test_same_hold_across_hid_tap_recovery_never_starts_twice(self):
        usage = next(
            candidate
            for candidate, button_id in app_module.frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "up"
        )
        down_report = usage.to_bytes(2, "little") + b"\x00\x00\x00\x00"
        neutral_report = b"\x00" * 6

        class ReadyTap:
            status = app_module.frida_compat.HidTapState.READY.value

        with mock.patch.object(self.app._button_gestures, "press") as press:
            self.app._on_direct_hid_report(1, down_report)
            self.app._on_hid_tap_status(
                app_module.frida_compat.HidTapState.UNHEALTHY.value,
                "simulated_loss",
            )
            self.app._hid_report_tap = ReadyTap()
            with mock.patch.object(app_module.time, "monotonic", return_value=1000.0):
                self.app._on_direct_hid_report(1, down_report)
            self.app._on_direct_hid_report(1, neutral_report)
            self.app._on_direct_hid_report(1, down_report)

        self.assertEqual(press.call_args_list, [mock.call("up"), mock.call("up")])

    def test_raw_device_removal_cancels_all_button_state_and_reconnects(self):
        usage = next(
            usage
            for usage, button_id in app_module.frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "down"
        )
        self.app._direct_hid_usages = {usage}
        self.app._raw_fallback_buttons_down = {"up"}
        self.app._input_rearm_blocked_buttons = {"left"}
        self.app._direct_hid_interception_armed = True
        reconnects = []
        self.app._supervisor.request_reconnect = lambda: reconnects.append(True)

        with mock.patch.object(win32_input, "send_key_combo_up") as release_keys, mock.patch.object(
            self.app._button_gestures, "reset"
        ) as gesture_reset, mock.patch.object(
            self.app._button_combos, "reset"
        ) as combo_reset:
            self.app._on_raw_input_device_removed()

        release_keys.assert_called_once_with(("up",))
        gesture_reset.assert_called_once_with()
        combo_reset.assert_called_once_with()
        self.assertEqual(reconnects, [True])
        self.assertEqual(self.app._direct_hid_usages, set())
        self.assertEqual(self.app._raw_fallback_buttons_down, set())
        self.assertEqual(
            self.app._input_rearm_blocked_buttons,
            {"down", "left", "up"},
        )
        self.assertFalse(self.app._direct_hid_interception_ready)
        self.assertFalse(self.app._direct_hid_interception_armed)

    def test_raw_device_removal_blocks_physical_alias_until_release(self):
        self.app._direct_hid_interception_ready = False
        self.app._direct_hid_interception_armed = False
        raw_down = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="ok",
            windows_button_id="up",
            vkey=0x26,
            make_code=0x48,
            flags=0,
            message=0x0100,
        )
        raw_up = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=False,
            button_id="down",
            windows_button_id="up",
            vkey=0x26,
            make_code=0x48,
            flags=1,
            message=0x0101,
        )
        self.app._supervisor.request_reconnect = lambda: None
        self.app._on_raw_physical_event(raw_down)

        with mock.patch.object(win32_input, "send_key_combo_up") as release_keys:
            self.app._on_raw_input_device_removed()

        release_keys.assert_called_once_with(("up",))
        self.assertEqual(
            self.app._input_rearm_blocked_buttons,
            {"ok", "up"},
        )
        self.assertEqual(
            self.app._raw_fallback_release_debts,
            {
                raw_input_windows.physical_signature(raw_down): (
                    {"ok"},
                    {"up"},
                )
            },
        )

        self.app._on_raw_physical_event(raw_up)
        self.assertEqual(self.app._input_rearm_blocked_buttons, set())
        self.assertEqual(self.app._raw_fallback_release_debts, {})

    def test_release_debt_accumulates_remapped_alias_across_two_recoveries(self):
        first_down = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="ok",
            windows_button_id="up",
            vkey=0x26,
            make_code=0x48,
            flags=0,
            message=0x0100,
        )
        remapped_down = replace(first_down, button_id="down")
        remapped_up = replace(
            remapped_down,
            is_pressed=False,
            flags=1,
            message=0x0101,
        )
        signature = raw_input_windows.physical_signature(first_down)
        self.app._direct_hid_interception_ready = False
        self.app._direct_hid_interception_armed = False
        self.app._supervisor.request_reconnect = lambda: None
        self.app._on_raw_physical_event(first_down)

        with mock.patch.object(win32_input, "send_key_combo_up") as release_keys:
            self.app._on_raw_input_device_removed()
            self.app._on_raw_physical_event(remapped_down)
            self.app._on_raw_input_device_removed()

        self.assertEqual(release_keys.call_count, 2)
        release_keys.assert_has_calls([mock.call(("up",)), mock.call(("up",))])
        self.assertEqual(
            self.app._raw_fallback_release_debts,
            {signature: ({"ok", "down"}, {"up"})},
        )
        self.assertEqual(
            self.app._input_rearm_blocked_buttons,
            {"ok", "down", "up"},
        )

        self.app._on_raw_physical_event(remapped_up)

        self.assertEqual(self.app._raw_fallback_release_debts, {})
        self.assertEqual(self.app._input_rearm_blocked_buttons, set())
        with mock.patch.object(self.app._button_gestures, "press") as press:
            self.app._on_button_event("down", True)
        press.assert_called_once_with("down")

        with mock.patch.object(self.app._button_gestures, "press") as press:
            self.app._on_button_event("ok", True)
        press.assert_called_once_with("ok")

    def test_raw_release_debt_waits_for_every_physical_signature(self):
        self.app._direct_hid_interception_ready = False
        self.app._direct_hid_interception_armed = False
        first_down = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="ok",
            windows_button_id="up",
            vkey=0x26,
            make_code=0x48,
            flags=0,
            message=0x0100,
        )
        second_down = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="ok",
            windows_button_id="up",
            vkey=0x26,
            make_code=0x49,
            flags=0,
            message=0x0100,
        )
        first_up = replace(
            first_down,
            is_pressed=False,
            flags=1,
            message=0x0101,
        )
        second_up = replace(
            second_down,
            is_pressed=False,
            flags=1,
            message=0x0101,
        )
        self.app._supervisor.request_reconnect = lambda: None
        self.app._on_raw_physical_event(first_down)
        self.app._on_raw_physical_event(second_down)

        with mock.patch.object(win32_input, "send_key_combo_up") as release_keys:
            self.app._on_raw_input_device_removed()
            self.app._on_raw_input_corruption("keyboard_body_too_short")

        release_keys.assert_called_once_with(("up",))
        self.assertEqual(self.app._input_rearm_blocked_buttons, {"ok", "up"})
        self.app._on_raw_physical_event(first_up)
        self.assertEqual(self.app._input_rearm_blocked_buttons, {"ok", "up"})
        self.app._on_raw_physical_event(second_up)
        self.assertEqual(self.app._input_rearm_blocked_buttons, set())
        self.assertEqual(self.app._raw_fallback_release_debts, {})

    def test_removed_custom_mapping_still_clears_release_debt(self):
        self.app._direct_hid_interception_ready = False
        self.app._direct_hid_interception_armed = False
        raw_down = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="ok",
            windows_button_id=None,
            vkey=0x70,
            make_code=0x3B,
            flags=0,
            message=0x0100,
        )
        raw_up_without_mapping = replace(
            raw_down,
            is_pressed=False,
            button_id=None,
            flags=1,
            message=0x0101,
        )
        self.app._supervisor.request_reconnect = lambda: None
        self.app._on_raw_physical_event(raw_down)

        self.app._on_raw_input_device_removed()
        self.assertEqual(self.app._input_rearm_blocked_buttons, {"ok"})
        self.app._on_raw_physical_event(raw_up_without_mapping)

        self.assertEqual(self.app._input_rearm_blocked_buttons, set())
        self.assertEqual(self.app._raw_fallback_release_debts, {})

    def test_release_debt_cannot_unblock_a_still_active_direct_button(self):
        raw_down = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="ok",
            windows_button_id="up",
            vkey=0x26,
            make_code=0x48,
            flags=0,
            message=0x0100,
        )
        raw_up = replace(
            raw_down,
            is_pressed=False,
            flags=1,
            message=0x0101,
        )
        up_usage = next(
            usage
            for usage, button_id in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "up"
        )
        signature = raw_input_windows.physical_signature(raw_down)
        self.app._raw_fallback_release_debts[signature] = ({"ok"}, {"up"})
        self.app._input_rearm_blocked_buttons = {"ok", "up"}
        self.app._direct_hid_usages = {up_usage}

        self.app._on_raw_physical_event(raw_up)

        self.assertEqual(self.app._input_rearm_blocked_buttons, {"up"})
        self.assertEqual(self.app._raw_fallback_release_debts, {})

    def test_direct_report_revalidates_after_raw_collection_is_removed(self):
        usage = next(
            usage
            for usage, button_id in app_module.frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "up"
        )

        class ReadyTap:
            status = app_module.frida_compat.HidTapState.READY.value

        self.app._hid_report_tap = ReadyTap()
        self.app._direct_hid_usages = {usage}
        self.app._supervisor.request_reconnect = lambda: None

        self.app._on_raw_input_device_removed()
        self.assertEqual(self.app._input_rearm_blocked_buttons, {"up"})
        self.assertFalse(self.app._direct_hid_interception_ready)

        self.app._on_direct_hid_report(1, b"\x00" * 6)

        self.assertTrue(self.app._direct_hid_interception_ready)
        self.assertTrue(self.app._direct_hid_interception_armed)
        self.assertEqual(self.app._input_rearm_blocked_buttons, set())

    def test_tap_failure_serializes_stale_release_against_final_report(self):
        usage = next(
            usage
            for usage, button_id in app_module.frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "up"
        )
        self.app._direct_hid_usages = {usage}
        status_started = threading.Event()
        original_set_runtime_input_state = self.app._set_runtime_input_state

        def record_status_start(**kwargs):
            status_started.set()
            original_set_runtime_input_state(**kwargs)

        self.app._set_runtime_input_state = record_status_start
        with mock.patch.object(self.app._button_gestures, "reset") as reset:
            with self.app._input_arbitration_lock:
                worker = threading.Thread(
                    target=self.app._on_hid_tap_status,
                    args=(
                        app_module.frida_compat.HidTapState.UNHEALTHY.value,
                        "socket_lost",
                    ),
                )
                worker.start()
                self.assertTrue(status_started.wait(1.0))
                worker.join(timeout=0.1)
                self.assertTrue(worker.is_alive())
                self.assertEqual(self.app._direct_hid_usages, {usage})

            worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(self.app._direct_hid_usages, set())
        reset.assert_called_once_with()
        self.assertEqual(self.app._input_rearm_blocked_buttons, {"up"})


class _FakeLocalCapture:
    """模式2 的本机麦克风替身：不碰真实声卡。"""

    def __init__(self, *, device_name, on_frame, logger, sample_rate=None):
        self.device_name = device_name
        self.on_frame = on_frame
        self.logger = logger
        self.sample_rate = sample_rate
        self.running = False
        self.stopped = False
        self.frames = 0

    def start(self) -> bool:
        self.running = True
        return True

    def stop(self) -> None:
        self.running = False
        self.stopped = True


class VoiceMappingProductBoundaryTests(_AppWiringTestCase):
    def setUp(self):
        super().setUp()
        # 这个用例集覆盖的是"语音槽可以落在任意按键、短按锁存和长按都
        # 生效"这一整套语义——那正是模式2（本机麦克风）的行为。模式1 的
        # 收紧（只认话筒键、只支持长按）由 VoiceRemoteMicrophoneModeTests
        # 单独覆盖。
        self._use_local_microphone()

    def _use_local_microphone(self) -> None:
        refreshed = config.load_config(self.app._config_path)
        _stamp_voice_mode(
            refreshed, config.VOICE_INPUT_SOURCE_LOCAL, "测试麦克风"
        )
        config.save_config(self.app._config_path, refreshed)
        _stamp_voice_mode(
            self.app._config, config.VOICE_INPUT_SOURCE_LOCAL, "测试麦克风"
        )
        # 写盘是为了让后面 load/save 一轮的设置写入也带上模式2；但必须同步
        # 刷新 mtime 记录，否则下一次按键会触发一次"设置已变化"重载，把用例
        # 在内存里改的按键映射冲掉。
        self.app._config_mtime_ns = self.app._settings_file_mtime_ns(
            self.app._config_path
        )
        self.app._bindings_mtime_ns = self.app._settings_file_mtime_ns(
            self.app._bindings_path
        )
        # 模式2 会去打开真实录音设备；这里换成替身，用例不依赖本机声卡。
        capture_patch = mock.patch.object(
            app_module.local_microphone_windows,
            "LocalMicrophoneCapture",
            _FakeLocalCapture,
        )
        capture_patch.start()
        self.addCleanup(capture_patch.stop)

    def _save_bindings(self, bindings):
        config.save_key_bindings(self.app._bindings_path, bindings)
        self.app._reload_settings_if_changed()

    def test_voice_slot_two_owns_atvv_audio_until_its_physical_release(self):
        self._save_voice_settings(hotkey_text="ralt")
        self._save_voice_slot_settings(slot=2, hotkey_text="ctrl+l")
        bindings = config.default_key_bindings()
        bindings["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD_2
        ).to_dict()
        self._save_bindings(bindings)

        calls = []
        slot_two_tokens = tuple(app_module.hotkey.HotkeySpec.parse("ctrl+l").modifiers) + (
            app_module.hotkey.HotkeySpec.parse("ctrl+l").key,
        )
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_button_event("up", True, event_source="hid_tap")
            self.app._on_control_event(MicButtonPressed())
            self.assertEqual(self.app._ble_session.mic_close_calls, 0)
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(AudioStopped())
            self.assertEqual(calls, [("down", slot_two_tokens)])
            self.assertTrue(self.app._voice_shortcut.controller.active)

            self.app._on_button_event("up", False, event_source="hid_tap")

        self.assertEqual(
            calls,
            [("down", slot_two_tokens), ("up", slot_two_tokens)],
        )
        # 模式2 的声音来自本机麦克风，所以整场都不需要遥控器开流：
        # 遥控器那条流开了也没用，锁存期间由本机麦克风一直供声。
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertEqual(self.app._ble_session.mic_close_calls, 0)
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_extra_voice_tap_latch_survives_a_stream_stop_during_the_press(self):
        # A tap is short enough that the device can open and drop its stream
        # while the key is still down. The latch must still end up with an open
        # microphone: PCM forwarding stays on, the runtime reports active, and
        # the device stream is asked for again instead of being left closed.
        self._save_voice_settings(hotkey_text="ralt")
        self._save_voice_slot_settings(slot=2, hotkey_text="rctrl")
        bindings = config.default_key_bindings()
        bindings["bindings"]["power"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD_2
        ).to_dict()
        self._save_bindings(bindings)

        calls = []
        with mock.patch.object(
            win32_input, "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input, "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_button_event("power", True, event_source="hid_tap")
            self.app._on_control_event(AudioStarted(session_id=9))
            self.app._on_control_event(AudioStopped())
            self.assertFalse(self.app._voice_audio_stream_active)

            self.app._on_button_event("power", False, event_source="hid_tap")

            self.assertTrue(self.app._extra_voice_gestures[2].latched)
            self.assertTrue(self.app._voice_pcm_forwarding_enabled)
            self.assertTrue(self.app._runtime_voice_active)
            # 模式2 不依赖遥控器那条流，所以一次 MIC_OPEN 都不发。
            self.assertEqual(self.app._ble_session.mic_open_calls, 0)
            self.assertEqual(self.app._ble_session.mic_close_calls, 0)
            self.assertEqual(
                calls,
                [("down", ("rctrl",)), ("up", ("rctrl",))],
            )

            # The re-opened stream is forwarded like any other latched session.
            self.app._on_control_event(AudioStarted(session_id=10))
            self.app._on_pcm_frame([100, -100])
            self.assertTrue(self._flush_playback().ok)
            self.assertEqual(self.app._voice_audio.sink.write_calls, [(100, -100)])

            # 模式2 的"关流"只是把本机麦克风的采集停掉，不会再发 MIC_CLOSE。
            self.app._on_button_event("power", True, event_source="hid_tap")
            self.assertEqual(self.app._ble_session.mic_close_calls, 0)
            self.app._on_button_event("power", False, event_source="hid_tap")

        self.assertFalse(self.app._extra_voice_gestures[2].latched)
        self.assertFalse(self.app._voice_pcm_forwarding_enabled)

    def test_extra_voice_hold_after_mic_release_forwards_audio(self):
        self._save_voice_settings(hotkey_text="ralt")
        self._save_voice_slot_settings(slot=2, hotkey_text="rctrl")
        bindings = config.default_key_bindings()
        bindings["bindings"]["power"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD_2
        ).to_dict()
        self._save_bindings(bindings)
        clock = [100.0]
        self.app._extra_voice_gestures[2] = voice_gesture.VoiceGestureLatch(
            clock=lambda: clock[0]
        )
        calls = []
        with mock.patch.object(
            win32_input, "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input, "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_button_event("mic", True, event_source="hid_tap")
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_button_event("mic", False, event_source="hid_tap")
            self.app._on_control_event(AudioStopped())
            self.assertFalse(self.app._voice_ignore_audio_start_until_physical_down)
            self.assertTrue(self.app._voice_gesture.latched)

            self.app._on_button_event("power", True, event_source="hid_tap")
            self.app._on_control_event(AudioStarted(session_id=2))
            self.app._on_control_event(AudioStarted(session_id=2))
            self.app._on_pcm_frame([100, -100])
            self.assertTrue(self._flush_playback().ok)
            self.assertEqual(self.app._voice_audio.sink.write_calls, [(100, -100)])
            self.assertTrue(self.app._voice_audio_stream_active)
            self.assertTrue(self.app._runtime_voice_active)
            self.assertEqual(calls[-1], ("down", ("rctrl",)))
            self.assertEqual(len(calls), 3)

            clock[0] += 4.6
            self.app._on_button_event("power", False, event_source="hid_tap")
            self.app._on_control_event(AudioStopped())

        self.assertEqual(calls, [
            ("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS),
            ("down", ("rctrl",)), ("up", ("rctrl",)),
        ])
        self.assertTrue(self.app._voice_pcm_forwarding_enabled)
        self.assertTrue(self.app._runtime_voice_active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 2)
        self.assertEqual(self.app._ble_session.mic_close_calls, 0)

    def test_local_microphone_feeds_the_sink_and_remote_pcm_is_dropped(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_button_event("mic", True, event_source="hid")
            capture = self.app._local_voice_capture
            self.assertIsNotNone(capture, "模式2 应该在开麦时启动本机麦克风")
            self.assertTrue(capture.running)

            # 本机麦克风的声音直接进播放侧。
            capture.on_frame([100, -100])
            self.assertTrue(self._flush_playback().ok)
            self.assertEqual(
                self.app._voice_audio.sink.write_calls, [(100, -100)]
            )

            # 同一时刻遥控器如果也在送音频，必须被丢掉，不能两路叠在一起。
            self.app._on_pcm_frame([7, 7], _ble_generation=0)
            self.assertTrue(self._flush_playback().ok)
            self.assertEqual(
                self.app._voice_audio.sink.write_calls, [(100, -100)]
            )

            # 短按锁存：松手之后本机麦克风必须继续供声——这正是模式1
            # 拿不到的效果。
            self.app._on_button_event("mic", False, event_source="hid")
            self.assertTrue(self.app._voice_gesture.latched)
            self.assertTrue(capture.running)

            # 再按一次结束锁存，采集随之停止。
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_button_event("mic", False, event_source="hid")

        self.assertFalse(self.app._voice_gesture.latched)
        self.assertFalse(capture.running)

    def test_non_mic_voice_mapping_replays_its_slot_shortcut(self):
        # The harness only overrides the in-memory voice provider; persist it
        # so the settings reload triggered by _save_bindings keeps it.
        self._save_voice_settings(hotkey_text="ralt")
        bindings = config.default_key_bindings()
        bindings["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD
        ).to_dict()
        self._save_bindings(bindings)

        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            # Each physical tap owns an independent host shortcut edge pair;
            # the device microphone latch remains open after the first pair.
            self.app._on_button_event("up", True, event_source="hid_tap")
            self.app._on_button_event("up", False, event_source="hid_tap")
            self.assertEqual(
                calls,
                [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
            )
            self.assertTrue(self.app._extra_voice_gestures[1].latched)
            # 模式2 从头到尾没有向遥控器要过流，所以这里也不会有"关流"。

            # The second tap ends the microphone latch but has its own pair.
            self.app._on_button_event("up", True, event_source="hid_tap")
            self.app._on_button_event("up", False, event_source="hid_tap")

        self.assertEqual(self.app._removed_voice_bindings, {})
        self.assertEqual(
            calls,
            [
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
            ],
        )
        # 模式2 下这条流水线完全不碰遥控器的麦克风流：音频来自本机麦克风，
        # 所以既不需要 MIC_OPEN，也不需要在锁存结束时 MIC_CLOSE。
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertEqual(self.app._ble_session.mic_close_calls, 0)
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_old_toggle_mapping_disables_mic_and_its_secondary_gestures(self):
        bindings = config.default_key_bindings()
        bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_TOGGLE
        ).to_dict()
        bindings["secondary_bindings"]["mic"] = {
            "long_press": key_mapping.ButtonAction(
                key_mapping.ActionKind.ARROW_UP
            ).to_dict()
        }
        self._save_bindings(bindings)

        with mock.patch.object(win32_input, "send_voice_key_combo_down") as voice, mock.patch.object(
            win32_input, "send_arrow_up"
        ) as secondary:
            self.app._on_button_event("mic", True)
            self.app._on_button_event("mic", False)
            self.app._on_button_trigger(
                "mic", app_module.button_gesture.ButtonTrigger.LONG_PRESS
            )

        self.assertEqual(self.app._removed_voice_bindings, {"mic": "voice_toggle"})
        voice.assert_not_called()
        secondary.assert_not_called()
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_secondary_voice_mapping_disables_the_whole_button(self):
        bindings = config.default_key_bindings()
        bindings["secondary_bindings"]["left"] = {
            "double_click": key_mapping.ButtonAction(
                key_mapping.ActionKind.VOICE_HOLD
            ).to_dict()
        }
        self._save_bindings(bindings)

        with mock.patch.object(win32_input, "send_arrow_left") as primary:
            self.app._on_button_event("left", True, event_source="hid_tap")
            self.app._on_button_event("left", False, event_source="hid_tap")

        self.assertEqual(self.app._removed_voice_bindings, {"left": "voice_hold"})
        primary.assert_not_called()
        self.assertFalse(
            self.app._is_button_action_configured(
                "left", app_module.button_gesture.ButtonTrigger.SINGLE_CLICK
            )
        )

    def test_reselecting_an_ordinary_action_dispatches_it(self):
        legacy = config.default_key_bindings()
        legacy["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD
        ).to_dict()
        self._save_bindings(legacy)
        self.assertNotIn("up", self.app._removed_voice_bindings)

        refreshed = config.default_key_bindings()
        refreshed["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ARROW_UP
        ).to_dict()
        self._save_bindings(refreshed)

        with mock.patch.object(win32_input, "send_arrow_up") as tap:
            self.app._on_button_event("up", True, event_source="hid_tap")
            self.app._on_button_event("up", False, event_source="hid_tap")

        self.assertNotIn("up", self.app._removed_voice_bindings)
        tap.assert_called_once_with()

    def test_physical_mic_hold_release_sends_one_down_and_one_up(self):
        clock = [100.0]
        self.app._voice_gesture = voice_gesture.VoiceGestureLatch(
            clock=lambda: clock[0]
        )
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_button_event("mic", True, event_source="hid")
            clock[0] += voice_gesture.VoiceGestureLatch.DEFAULT_TAP_MAX_SECONDS + 0.01
            self.app._on_button_event("mic", False, event_source="hid")

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_physical_mic_tap_releases_the_host_edge_and_latches_the_mic(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_button_event("mic", False, event_source="hid")

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertFalse(self.app._voice_shortcut.controller.active)
        self.assertTrue(self.app._voice_gesture.latched)
        self.assertTrue(self.app._voice_pcm_forwarding_enabled)
        self.assertTrue(self.app._runtime_voice_active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertEqual(self.app._ble_session.mic_close_calls, 0)

    def test_latched_mic_tap_keeps_playing_until_the_next_tap(self):
        calls = []
        with mock.patch.object(
            win32_input, "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input, "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_button_event("mic", True, event_source="hid_tap")
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_button_event("mic", False, event_source="hid_tap")
            # A tap latches the microphone open: the device stream ending does
            # not end the session, so the host asks for it again.
            self.app._on_control_event(AudioStopped())
            self.assertEqual(calls, [
                ("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS),
            ])
            self.assertTrue(self.app._voice_gesture.latched)
            self.assertTrue(self.app._voice_pcm_forwarding_enabled)
            self.assertTrue(self.app._runtime_voice_active)
            self.assertEqual(self.app._ble_session.mic_open_calls, 1)
            self.assertEqual(self.app._ble_session.mic_close_calls, 0)

            self.app._on_control_event(AudioStarted(session_id=2))
            self.app._on_pcm_frame([100, -100])
            self.assertTrue(self._flush_playback().ok)
            self.assertEqual(self.app._voice_audio.sink.write_calls, [(100, -100)])
            # Resuming a latched stream never replays the host shortcut.
            self.assertEqual(len(calls), 2)

            # The next tap closes the latched session with its own edge pair.
            self.app._on_button_event("mic", True, event_source="hid_tap")
            self.assertFalse(self.app._voice_pcm_forwarding_enabled)
            self.assertFalse(self.app._runtime_voice_active)
            self.assertEqual(self.app._ble_session.mic_close_calls, 1)
            self.app._on_button_event("mic", False, event_source="hid_tap")

        self.assertEqual(calls, [
            ("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS),
            ("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS),
        ])
        self.assertFalse(self.app._voice_gesture.latched)
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_late_audio_start_after_physical_release_does_not_retrigger_shortcut(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_button_event("mic", False, event_source="hid")
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_pcm_frame([100, -100])
            self.assertTrue(self._flush_playback().ok)
            self.assertEqual(self.app._voice_audio.sink.write_calls, [(100, -100)])
            self.app._on_control_event(AudioStopped())

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertFalse(self.app._voice_shortcut.controller.active)
        self.assertFalse(self.app._voice_audio_stream_active)
        # The tap latch survives the stream boundary.
        self.assertTrue(self.app._voice_gesture.latched)
        self.assertTrue(self.app._voice_pcm_forwarding_enabled)
        self.assertTrue(self.app._runtime_voice_active)











    def test_audio_stopped_does_not_release_a_physically_held_mic_key(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(AudioStopped())

            self.assertEqual(calls, [("down", DEFAULT_VOICE_TOKENS)])
            self.assertTrue(self.app._voice_shortcut.controller.active)
            self.assertFalse(self.app._voice_gesture.latched)
            self.assertEqual(self.app._voice_shortcut_physical_sources_down, {"hid"})

            self.app._on_button_event("mic", False, event_source="hid")

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertFalse(self.app._voice_shortcut.controller.active)
        # A quick press still latches on release. 模式2 的声音来自本机麦克风，
        # 所以锁存期间不需要再向遥控器要流，转发保持打开即可。
        self.assertTrue(self.app._voice_gesture.latched)
        self.assertTrue(self.app._voice_pcm_forwarding_enabled)
        self.assertTrue(self.app._runtime_voice_active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def _assert_held_voice_recovers_after_audio_stop(self, button, slot):
        self._save_voice_settings(hotkey_text="ralt")
        self._save_voice_slot_settings(slot=2, hotkey_text="rctrl")
        bindings = config.default_key_bindings()
        if slot == 2:
            bindings["bindings"][button] = key_mapping.ButtonAction(
                key_mapping.ActionKind.VOICE_HOLD_2
            ).to_dict()
        self._save_bindings(bindings)
        clock = [100.0]
        gesture = voice_gesture.VoiceGestureLatch(clock=lambda: clock[0])
        if slot == 1:
            self.app._voice_gesture = gesture
        else:
            self.app._extra_voice_gestures[slot] = gesture
        tokens = DEFAULT_VOICE_TOKENS if slot == 1 else ("rctrl",)
        calls = []
        with mock.patch.object(
            win32_input, "send_voice_key_combo_down",
            side_effect=lambda value: calls.append(("down", value)),
        ), mock.patch.object(
            win32_input, "send_voice_key_combo_up",
            side_effect=lambda value: calls.append(("up", value)),
        ):
            self.app._on_button_event(button, True, event_source="hid_tap")
            self.app._on_control_event(AudioStarted(session_id=1))
            watchdog = self.app._voice_hold_watchdog_token
            clock[0] += 2.0
            self.app._on_control_event(AudioStopped())
            self.app._on_control_event(AudioStopped())
            self.assertTrue(gesture.active)
            self.assertFalse(self.app._voice_pcm_forwarding_enabled)
            self.assertEqual(calls, [("down", tokens)])
            self.app._on_control_event(AudioStarted(session_id=2))
            self.app._on_control_event(AudioStarted(session_id=2))
            self.app._on_pcm_frame([123, -123])
            self.assertTrue(self._flush_playback().ok)
            self.assertEqual(self.app._voice_audio.sink.write_calls, [(123, -123)])
            self.assertTrue(self.app._runtime_voice_active)
            self.assertIs(self.app._voice_hold_watchdog_token, watchdog)
            self.assertEqual(calls, [("down", tokens)])
            self.app._on_button_event(button, False, event_source="hid_tap")
            self.app._on_control_event(AudioStopped())
            self.app._on_control_event(AudioStarted(session_id=3))
            self.app._on_pcm_frame([456, -456])
            self.assertTrue(self._flush_playback().ok)
            self.assertEqual(self.app._voice_audio.sink.write_calls, [(123, -123)])
        self.assertEqual(calls, [("down", tokens), ("up", tokens)])
        self.assertFalse(self.app._voice_shortcut.controller.active)
        self.assertFalse(gesture.active)

    def test_held_mic_recovers_audio_without_releasing_or_repressing_shortcut(self):
        self._assert_held_voice_recovers_after_audio_stop("mic", 1)

    def test_held_extra_voice_recovers_audio_without_releasing_or_repressing_shortcut(self):
        self._assert_held_voice_recovers_after_audio_stop("power", 2)

    def test_extra_voice_key_takes_over_mapping_from_latched_mic_session(self):
        # Regression: a latched (short-tap) mic session keeps the device
        # stream open without holding any host shortcut, so the idle gate
        # reported "busy" and every later voice key bound to another slot was
        # dropped outright ("voice mapping change deferred: active session owns
        # hotkey=ralt"). A deliberate extra-key press must still take the
        # mapping over and inject its own shortcut.
        self._save_voice_settings(hotkey_text="ralt")
        self._save_voice_slot_settings(slot=2, hotkey_text="rctrl")
        bindings = config.default_key_bindings()
        bindings["bindings"]["power"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD_2
        ).to_dict()
        self._save_bindings(bindings)

        clock = [100.0]
        mic_gesture = voice_gesture.VoiceGestureLatch(clock=lambda: clock[0])
        self.app._voice_gesture = mic_gesture
        self.app._extra_voice_gestures[2] = voice_gesture.VoiceGestureLatch(
            clock=lambda: clock[0]
        )
        # Reproduce the latched session: the host shortcut is already released
        # and only the device stream is still open.
        mic_gesture.on_press()
        mic_gesture.on_release()
        self.assertTrue(mic_gesture.latched)
        self.app._voice_audio_stream_active = True
        self.app._voice_shortcut_physical_sources_down.clear()
        self.assertFalse(self.app._voice_shortcut.controller.active)

        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_button_event("power", True, event_source="hid_tap")

        self.assertEqual(calls, [("down", ("rctrl",))])
        self.assertEqual(self.app._extra_voice_slot_active, 2)
        # The latched mic stream is reused instead of restarted.
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_audio_lifecycle_fallback_works_without_a_physical_edge(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(AudioStopped())

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_physical_mic_release_failure_retains_state_and_reconnects(self):
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)
        clock = [100.0]
        self.app._voice_gesture = voice_gesture.VoiceGestureLatch(
            clock=lambda: clock[0]
        )

        with mock.patch.object(win32_input, "send_voice_key_combo_down"), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=OSError("simulated release failure"),
        ):
            self.app._on_button_event("mic", True, event_source="hid")
            clock[0] += voice_gesture.VoiceGestureLatch.DEFAULT_TAP_MAX_SECONDS + 0.01
            self.app._on_button_event("mic", False, event_source="hid")

        self.assertTrue(self.app._voice_shortcut.controller.active)
        self.assertEqual(reconnect_calls, [1])

    def test_mic_normal_mapping_dispatches_once_and_rejects_unsolicited_voice(self):
        self.app._bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ARROW_UP
        ).to_dict()
        downs = []
        ups = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda keys: downs.append(tuple(keys)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda keys: ups.append(tuple(keys)),
        ), mock.patch.object(win32_input, "send_arrow_up") as arrow:
            self.app._on_button_event("mic", True, event_source="hid_tap")
            self.app._on_control_event(MicButtonPressed())
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_button_event("mic", False, event_source="hid_tap")

        # The ordinary mapping delivers one click for the physical press and
        # never opens the voice session, so the voice hotkey is never injected -
        # only the mapped key itself is.
        arrow.assert_called_once_with()
        self.assertEqual(downs, [])
        self.assertEqual(ups, [])
        self.assertNotIn(DEFAULT_VOICE_TOKENS, downs)
        self.assertEqual(self.app._ble_session.mic_close_calls, 1)


    def test_ordinary_mic_same_source_next_press_is_not_suppressed(self):
        self.app._bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ARROW_UP
        ).to_dict()
        with mock.patch.object(win32_input, "send_arrow_up") as tap:
            self.app._on_button_event("mic", True, event_source="hid_tap")
            self.app._on_button_event("mic", False, event_source="hid_tap")
            self.app._on_button_event("mic", True, event_source="hid_tap")
            self.app._on_button_event("mic", False, event_source="hid_tap")

        # Each physical press delivers its own click; no press is suppressed.
        self.assertEqual(tap.call_count, 2)

    def test_stale_ordinary_mic_source_cannot_block_the_next_press(self):
        self.app._bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ARROW_UP
        ).to_dict()
        clock = [100.0]

        with mock.patch.object(
            app_module.time,
            "monotonic",
            side_effect=lambda: clock[0],
        ), mock.patch.object(win32_input, "send_arrow_up") as tap:
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_button_event("mic", True, event_source="hid_tap")
            clock[0] += app_module._ORDINARY_MIC_SOURCE_STALE_SECONDS + 0.01
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_button_event("mic", False, event_source="hid")

        # The stale gesture is cancelled and the fresh press starts a brand new
        # hold, so each delivered hold is one click.
        self.assertEqual(tap.call_count, 2)
        self.assertEqual(self.app._ordinary_mic_sources_down, set())
        self.assertFalse(self.app._ordinary_mic_gesture_active)

    def test_non_mic_voice_data_is_kept_in_runtime_bindings(self):
        bindings = config.default_key_bindings()
        bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()
        bindings["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD
        ).to_dict()
        self._save_bindings(bindings)

        self.assertNotIn("up", self.app._removed_voice_bindings)

    def test_active_voice_mapping_reload_is_deferred_until_release(self):
        clock = [100.0]
        self.app._voice_gesture = voice_gesture.VoiceGestureLatch(
            clock=lambda: clock[0]
        )
        with mock.patch.object(win32_input, "send_voice_key_combo_down"), mock.patch.object(
            win32_input, "send_voice_key_combo_up"
        ):
            self.app._on_button_event("mic", True, event_source="hid")
            refreshed = config.default_key_bindings()
            refreshed["bindings"]["mic"] = key_mapping.ButtonAction(
                key_mapping.ActionKind.ESCAPE
            ).to_dict()
            config.save_key_bindings(self.app._bindings_path, refreshed)
            self.app._reload_settings_if_changed()
            self.assertIsNotNone(self.app._pending_bindings)

            clock[0] += voice_gesture.VoiceGestureLatch.DEFAULT_TAP_MAX_SECONDS + 0.01
            self.app._on_button_event("mic", False, event_source="hid")

        self.assertIsNone(self.app._pending_bindings)
        self.assertEqual(
            self.app._bindings["bindings"]["mic"]["kind"],
            key_mapping.ActionKind.ESCAPE.value,
        )

    def test_ordinary_mic_release_finishes_before_voice_mapping_applies(self):
        self.app._bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ARROW_UP
        ).to_dict()

        with mock.patch.object(win32_input, "send_arrow_up"):
            self.app._on_button_event("mic", True, event_source="hid")

            refreshed = config.default_key_bindings()
            config.save_key_bindings(self.app._bindings_path, refreshed)
            self.app._reload_settings_if_changed()
            self.assertIsNotNone(self.app._pending_bindings)

            self.app._on_button_event("mic", False, event_source="hid")

        self.assertEqual(self.app._ordinary_mic_sources_down, set())
        self.assertFalse(self.app._ordinary_mic_gesture_active)
        self.assertIsNone(self.app._pending_bindings)
        self.assertEqual(
            self.app._bindings["bindings"]["mic"]["kind"],
            key_mapping.ActionKind.VOICE_HOLD.value,
        )

    def test_detected_removed_voice_button_reports_but_does_not_execute(self):
        bindings = config.default_key_bindings()
        bindings["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD
        ).to_dict()
        self._save_bindings(bindings)
        request = key_detection_bridge.request_detection(self.app._config_root)

        with mock.patch.object(win32_input, "send_voice_key_combo_down") as voice:
            self.app._on_button_event("up", True, event_source="hid_tap")
            self.app._on_button_event("up", False, event_source="hid_tap")

        self.assertEqual(key_detection_bridge.poll_detection(request), "up")
        voice.assert_not_called()
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)


class LiveBridgeKeyDetectionTests(_AppWiringTestCase):
    def test_next_ordinary_button_is_reported_and_its_mapping_is_suppressed(self):
        request = key_detection_bridge.request_detection(self.app._config_root)
        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release:
            self.app._on_button_event("back", True)
            self.app._on_button_event("back", False)

        self.assertEqual(key_detection_bridge.poll_detection(request), "back")
        press.assert_not_called()
        release.assert_not_called()
        self.assertEqual(self.app._key_detection_suppressed_buttons, set())

    def test_raw_keyboard_fallback_can_feed_detection_before_mapping_bypass(self):
        self.app._direct_hid_interception_ready = False
        request = key_detection_bridge.request_detection(self.app._config_root)

        with mock.patch.object(self.app._button_gestures, "press") as press:
            self.app._on_raw_button_event("up", True, "keyboard")
            self.app._on_raw_button_event("up", False, "keyboard")

        self.assertEqual(key_detection_bridge.poll_detection(request), "up")
        press.assert_not_called()
        self.assertEqual(self.app._raw_fallback_buttons_down, set())

    def test_detected_mic_button_never_triggers_host_or_device_voice(self):
        request = key_detection_bridge.request_detection(self.app._config_root)
        with mock.patch.object(win32_input, "send_voice_key_combo_down") as hotkey:
            self.app._on_button_event("mic", True)
            self.app._on_button_event("mic", False)

        self.assertEqual(key_detection_bridge.poll_detection(request), "mic")
        hotkey.assert_not_called()
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertEqual(self.app._ble_session.mic_close_calls, 0)





    def test_atvv_first_detection_expires_and_next_normal_press_triggers_voice(self):
        clock = [100.0]
        request = key_detection_bridge.request_detection(self.app._config_root)
        hotkey_calls = []
        with mock.patch.object(
            app_module.time,
            "monotonic",
            side_effect=lambda: clock[0],
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: hotkey_calls.append(tokens),
        ):
            self.app._on_control_event(MicButtonPressed())
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(AudioStopped())

            clock[0] += app_module._KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS + 0.01
            self.app._on_button_event("mic", True, event_source="hid")

        self.assertEqual(key_detection_bridge.poll_detection(request), "mic")
        self.assertEqual(hotkey_calls, [DEFAULT_VOICE_TOKENS])
        self.assertTrue(self.app._voice_shortcut.controller.active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_detection_suppression_timeout_allows_a_later_real_press(self):
        self.app._key_detection_suppressed_buttons = {"back"}
        self.app._key_detection_suppression_deadlines = {"back": 100.0}

        with mock.patch.object(app_module.time, "monotonic", return_value=101.0), mock.patch.object(
            key_detection_bridge,
            "publish_next_button",
            return_value=False,
        ), mock.patch.object(self.app._button_gestures, "press") as press:
            self.app._on_button_event("back", True, event_source="hid_tap")

        press.assert_called_once_with("back")
        self.assertEqual(self.app._key_detection_suppressed_buttons, set())
        self.app._on_button_event("back", False, event_source="hid_tap")


class PlaybackWriteFailureTests(_AppWiringTestCase):
    """XRBM-014 review round 2 P1 #6: a playback write failure must fail
    closed (discard the sink) and request a reconnect, not log indefinitely
    while the device keeps streaming.
    """

    def test_write_failure_requests_reconnect_and_cleanup_closes_sink(self):
        sink = _FakePlaybackSink(fail_write=True)
        self.app._voice_audio.sink = sink
        self.app._voice_pcm_forwarding_enabled = True
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        self.app._on_pcm_frame([0, 0])
        result = self._flush_playback()

        self.assertTrue(result.completed)
        self.assertIsInstance(result.error, OSError)
        self.assertFalse(sink.closed)
        self.assertIs(self.app._voice_audio.sink, sink)
        self.assertEqual(reconnect_calls, [1])

        _run(self.app._cleanup_once())
        self.assertTrue(sink.closed)
        self.assertIsNone(self.app._voice_audio.sink)

    def test_write_success_does_not_touch_playback_or_reconnect(self):
        self.app._voice_pcm_forwarding_enabled = True
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        self.app._on_pcm_frame([0, 0])
        self.assertTrue(self._flush_playback().ok)

        self.assertIsNotNone(self.app._voice_audio.sink)
        self.assertEqual(reconnect_calls, [])

    def test_write_success_logs_the_latest_playback_timing_snapshot(self):
        class TimedSink(_FakePlaybackSink):
            def timing_snapshot(self):
                return app_module.audio_playback.PlaybackTimingSnapshot(
                    open_elapsed_ms=12.0,
                    last_write_elapsed_ms=2.5,
                    max_write_elapsed_ms=3.5,
                    write_count=1,
                    underflow_count=2,
                )

        self.app._voice_audio.sink = TimedSink()
        self.app._voice_pcm_forwarding_enabled = True

        with self.assertLogs(self.app._logger, level="INFO") as captured:
            self.app._on_pcm_frame([1, 2, 3])
            self.assertTrue(self._flush_playback().ok)

        self.assertIn(
            "write_ms=2.50 max_write_ms=3.50 underflows=2",
            "\n".join(captured.output),
        )

    def test_pcm_enqueue_does_not_wait_for_a_blocking_sink_write(self):
        write_started = threading.Event()
        release_write = threading.Event()

        class BlockingSink(_FakePlaybackSink):
            def write(self, samples):
                write_started.set()
                release_write.wait(2.0)
                super().write(samples)

        self.app._voice_audio.sink = BlockingSink()
        self.app._voice_pcm_forwarding_enabled = True

        started = time.monotonic()
        self.app._on_pcm_frame([1, 2, 3])
        elapsed = time.monotonic() - started

        self.assertTrue(write_started.wait(1.0))
        self.assertLess(elapsed, 0.1)
        release_write.set()
        self.assertTrue(self._flush_playback().ok)

    def test_audio_stop_waits_for_queued_pcm_before_releasing_hotkey(self):
        write_started = threading.Event()
        release_write = threading.Event()
        key_up_calls = []

        class BlockingSink(_FakePlaybackSink):
            def write(self, samples):
                write_started.set()
                release_write.wait(2.0)
                super().write(samples)

        self.app._voice_audio.sink = BlockingSink()
        self.app._voice_shortcut.controller.on_mic_button_pressed()
        self.app._voice_audio_stream_active = True
        self.app._voice_audio_stop_processed = False
        self.app._voice_pcm_forwarding_enabled = True
        self.app._on_pcm_frame([1, 2, 3])
        self.assertTrue(write_started.wait(1.0))

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: key_up_calls.append(tokens),
        ):
            stop_thread = threading.Thread(
                target=self.app._on_control_event,
                args=(AudioStopped(),),
            )
            stop_thread.start()
            time.sleep(0.05)
            self.assertTrue(stop_thread.is_alive())
            self.assertEqual(key_up_calls, [])
            release_write.set()
            stop_thread.join(1.0)

        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(key_up_calls, [DEFAULT_VOICE_TOKENS])

    def test_full_playback_queue_disables_forwarding_and_requests_reconnect(self):
        write_started = threading.Event()
        release_write = threading.Event()
        reconnect_calls = []
        sink = _FakePlaybackSink()

        def write(samples):
            write_started.set()
            release_write.wait(2.0)
            self.app._voice_audio.write_frame(sink, samples)

        self.app._voice_audio.sink = sink
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)
        writer = app_module.audio_playback_worker.PlaybackWriteWorker(
            write,
            self.app._voice_audio.on_worker_error,
            max_pending_frames=1,
        )
        writer.start()
        self.app._voice_audio.writer = writer
        self.app._voice_pcm_forwarding_enabled = True
        self.app._on_pcm_frame([1])
        self.assertTrue(write_started.wait(1.0))
        self.app._on_pcm_frame([2])
        self.app._on_pcm_frame([3])

        self.assertFalse(self.app._voice_pcm_forwarding_enabled)
        self.assertEqual(reconnect_calls, [1])
        self.assertIsInstance(
            writer.failure,
            app_module.audio_playback_worker.PlaybackBackpressureError,
        )
        release_write.set()
        self.assertTrue(self._flush_playback().completed)

    def test_no_playback_open_is_a_silent_no_op(self):
        self.app._voice_audio.sink = None
        self.app._voice_pcm_forwarding_enabled = True
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        self.app._on_pcm_frame([0, 0])  # must not raise

        self.assertEqual(reconnect_calls, [])

    def test_ordinary_mic_unsolicited_audio_never_reaches_existing_sink(self):
        sink = self.app._voice_audio.sink
        self.app._bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()

        self.app._on_control_event(AudioStarted(session_id=1))
        self.app._on_pcm_frame([1, 2, 3])

        self.assertEqual(sink.write_calls, [])
        self.assertFalse(self.app._voice_pcm_forwarding_enabled)
        self.assertEqual(self.app._ble_session.mic_close_calls, 1)


class CrossThreadReconnectTests(_AppWiringTestCase):
    """ble_transport_winrt.py invokes _on_pcm_frame on its own dedicated
    worker thread, never the event-loop thread - a playback failure there
    must still correctly reach request_reconnect().
    """

    def test_on_pcm_frame_failure_from_a_real_worker_thread_requests_reconnect(self):
        self.app._voice_audio.sink = _FakePlaybackSink(fail_write=True)
        self.app._voice_pcm_forwarding_enabled = True
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(
            threading.current_thread()
        )

        worker = threading.Thread(target=self.app._on_pcm_frame, args=([0, 0],))
        worker.start()
        worker.join(timeout=2.0)
        self._flush_playback()

        self.assertEqual(len(reconnect_calls), 1)
        self.assertNotEqual(reconnect_calls[0], threading.main_thread())


class InputLifecycleTests(_AppWiringTestCase):
    def test_input_channels_start_before_the_ble_supervisor(self):
        calls = []
        self.assertIs(app_module.element_navigation_runtime._diagnostic_trace,
                      self.app._diagnostic_trace)

        class Supervisor:
            async def run_forever(self):
                calls.append("supervisor")

        self.app._supervisor = Supervisor()
        with mock.patch.object(
            self.app,
            "_start_input_channels",
            side_effect=lambda: calls.append("input_start"),
        ), mock.patch.object(
            self.app,
            "_stop_input_channels",
            side_effect=lambda: calls.append("input_stop"),
        ):
            self._loop.run_until_complete(self.app.run_forever())

        self.assertEqual(calls, ["input_start", "supervisor", "input_stop"])
        self.assertIsNone(app_module.element_navigation_runtime._diagnostic_trace)

    def test_input_channels_are_started_once_as_one_owned_group(self):
        calls = []
        with mock.patch.object(
            self.app,
            "_start_voice_key_physicalizer",
            side_effect=lambda: calls.append("physicalizer"),
        ), mock.patch.object(
            self.app,
            "_start_hid_listener",
            side_effect=lambda: calls.append("raw_input"),
        ), mock.patch.object(
            self.app,
            "_start_hid_report_tap",
            side_effect=lambda: calls.append("hid_tap"),
        ):
            self.app._start_input_channels()

        self.assertEqual(calls, ["physicalizer", "raw_input", "hid_tap"])

    def test_unavailable_voice_physicalizer_degrades_without_losing_owner_state(self):
        timers = []

        class UnavailablePhysicalizer:
            is_running = False

            def start(self):
                raise (
                    app_module.voice_key_physicalizer_windows.
                    VoiceKeyPhysicalizerUnavailableError("unavailable")
                )

        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
            UnavailablePhysicalizer,
        ):
            self.app._voice_key_physicalizer_timer_factory = (
                lambda _delay, callback: timers.append(_ManualTimer(callback))
                or timers[-1]
            )
            self.app._start_voice_key_physicalizer()

        self.assertIsNone(self.app._voice_key_physicalizer)
        self.assertFalse(self.app._voice_key_physicalizer_ready)
        self.assertEqual(self.app._runtime_voice_key_physicalizer_state, "failed")
        self.assertEqual(len(timers), 1)

    def test_still_running_voice_physicalizer_is_retained_for_shutdown(self):
        class StuckPhysicalizer:
            is_running = True

            def start(self):
                raise (
                    app_module.voice_key_physicalizer_windows.
                    VoiceKeyPhysicalizerUnavailableError("stuck")
                )

        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
            StuckPhysicalizer,
        ), self.assertRaises(
            app_module.voice_key_physicalizer_windows.
            VoiceKeyPhysicalizerUnavailableError
        ):
            self.app._start_voice_key_physicalizer()

        self.assertIsInstance(
            self.app._voice_key_physicalizer,
            StuckPhysicalizer,
        )

    def test_ready_then_immediate_exit_never_publishes_ready(self):
        timers = []
        physicalizer = _FakeVoicePhysicalizer(immediate_exit=True)
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
            return_value=physicalizer,
        ):
            self.app._start_voice_key_physicalizer()

        self.assertFalse(self.app._voice_key_physicalizer_ready)
        self.assertEqual(
            self.app._runtime_voice_key_physicalizer_state,
            "recovering",
        )
        self.assertEqual(len(timers), 1)

    def test_repeated_start_failures_keep_one_recovery_timer(self):
        timers = []
        instances = []

        def build_physicalizer():
            instance = _FakeVoicePhysicalizer(
                start_error=(
                    app_module.voice_key_physicalizer_windows.
                    VoiceKeyPhysicalizerUnavailableError("failed")
                )
            )
            instances.append(instance)
            return instance

        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
            side_effect=build_physicalizer,
        ):
            self.app._start_voice_key_physicalizer()
            with self.app._voice_key_physicalizer_lifecycle_lock:
                self.app._schedule_voice_key_physicalizer_recovery_locked()
            self.assertEqual(len(timers), 1)
            timers[0].fire()

        self.assertEqual(len(instances), 2)
        self.assertEqual(len(timers), 2)
        self.assertIsNone(self.app._voice_key_physicalizer)

    def test_normal_shutdown_cancels_recovery_and_never_restarts(self):
        timers = []
        instances = []

        def build_physicalizer():
            instance = _FakeVoicePhysicalizer(
                start_error=(
                    app_module.voice_key_physicalizer_windows.
                    VoiceKeyPhysicalizerUnavailableError("failed")
                )
            )
            instances.append(instance)
            return instance

        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
            side_effect=build_physicalizer,
        ):
            self.app._start_voice_key_physicalizer()
            self.app._stop_input_channels()
            timers[0].fire()

        self.assertEqual(len(instances), 1)
        self.assertTrue(timers[0].cancelled)
        self.assertEqual(
            self.app._runtime_voice_key_physicalizer_state,
            "stopped",
        )

    def test_stale_recovery_timer_cannot_stop_replacement_generation(self):
        timers = []
        old = _FakeVoicePhysicalizer(accepts_new_down=False)
        old.is_running = True
        self.app._voice_key_physicalizer = old
        self.app._voice_key_physicalizer_generation = 1
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._voice_key_physicalizer_lifecycle_lock:
            self.app._schedule_voice_key_physicalizer_recovery_locked()

        replacement = _FakeVoicePhysicalizer(accepts_new_down=False)
        replacement.is_running = True
        self.app._voice_key_physicalizer = replacement
        self.app._voice_key_physicalizer_generation = 2
        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
        ) as build:
            timers[0].fire()

        self.assertEqual(replacement.stop_calls, 0)
        self.assertIs(self.app._voice_key_physicalizer, replacement)
        build.assert_not_called()

    def test_required_confirmation_failure_closes_admission_and_schedules_recovery(self):
        timers = []
        physicalizer = _FakeVoicePhysicalizer(accepts_new_down=False)
        physicalizer.is_running = True
        self.app._voice_key_physicalizer = physicalizer
        self.app._voice_key_physicalizer_generation = 7
        self.app._voice_key_physicalizer_ready = True
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        snapshot = mock.Mock(
            generation=11,
            receipt_callback_entry_delta=0,
            receipt_marker_callback_delta=0,
        )
        snapshot.trace_fields.return_value = {}

        self.app._on_voice_key_physicalizer_health_failure(
            physicalizer,
            7,
            "required_confirmation_timeout",
            snapshot,
        )

        self.assertFalse(self.app._voice_key_physicalizer_ready)
        self.assertEqual(
            self.app._runtime_voice_key_physicalizer_state,
            "recovering",
        )
        self.assertEqual(len(timers), 1)

    def test_tracking_lost_owner_still_running_never_republishes_ready(self):
        timers = []
        physicalizer = _FakeVoicePhysicalizer()
        physicalizer.is_running = True
        self.app._voice_key_physicalizer = physicalizer
        self.app._voice_key_physicalizer_generation = 8
        self.app._voice_key_physicalizer_lost_generation = 8
        self.app._voice_key_physicalizer_ready = False
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._voice_key_physicalizer_lifecycle_lock:
            self.app._schedule_voice_key_physicalizer_recovery_locked()

        timers[0].fire()

        self.assertFalse(self.app._voice_key_physicalizer_ready)
        self.assertEqual(physicalizer.stop_calls, 0)
        self.assertEqual(len(timers), 2)

    def test_degraded_owner_waits_for_cleanup_then_restarts_once(self):
        timers = []
        old = _FakeVoicePhysicalizer(accepts_new_down=False)
        old.is_running = True
        replacement = _FakeVoicePhysicalizer()
        self.app._voice_key_physicalizer = old
        self.app._voice_key_physicalizer_generation = 4
        self.app._voice_key_physicalizer_ready = False
        self.app._voice_shortcut.pending_tokens = ("ralt",)
        self.app._voice_key_physicalizer_degraded_retry_deadline = (
            time.monotonic() + 10.0
        )
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._voice_key_physicalizer_lifecycle_lock:
            self.app._schedule_voice_key_physicalizer_recovery_locked()

        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
            return_value=replacement,
        ) as build:
            timers[0].fire()
            self.assertEqual(old.stop_calls, 0)
            self.assertEqual(len(timers), 2)
            self.app._voice_shortcut.pending_tokens = None
            timers[1].fire()

        self.assertEqual(old.stop_calls, 1)
        self.assertEqual(replacement.start_calls, 1)
        self.assertIs(self.app._voice_key_physicalizer, replacement)
        self.assertTrue(self.app._voice_key_physicalizer_ready)
        build.assert_called_once_with()

    def test_persistent_cleanup_debt_enters_failed_state_without_restart_loop(self):
        timers = []
        delays = []
        physicalizer = _FakeVoicePhysicalizer(accepts_new_down=False)
        physicalizer.is_running = True
        self.app._voice_key_physicalizer = physicalizer
        self.app._voice_key_physicalizer_generation = 3
        self.app._voice_key_physicalizer_ready = False
        self.app._voice_shortcut.pending_tokens = ("ralt",)
        self.app._voice_key_physicalizer_degraded_retry_deadline = (
            time.monotonic() - 0.1
        )

        def make_timer(delay, callback):
            delays.append(delay)
            timers.append(_ManualTimer(callback))
            return timers[-1]

        self.app._voice_key_physicalizer_timer_factory = make_timer
        with self.app._voice_key_physicalizer_lifecycle_lock:
            self.app._schedule_voice_key_physicalizer_recovery_locked()

        timers[0].fire()

        self.assertEqual(physicalizer.stop_calls, 0)
        # Persistent cleanup debt is reported as failed, but recovery re-arms
        # on a slow backoff instead of spinning a tight restart loop.
        self.assertEqual(len(timers), 2)
        self.assertEqual(
            delays[-1],
            app_module._VOICE_KEY_PHYSICALIZER_FAILED_RETRY_SECONDS,
        )
        self.assertEqual(
            self.app._runtime_voice_key_physicalizer_state,
            "failed",
        )

    def test_late_direct_key_up_resumes_failed_degraded_owner_recovery(self):
        timers = []
        old = _FakeVoicePhysicalizer(accepts_new_down=False)
        old.is_running = True
        replacement = _FakeVoicePhysicalizer()
        self.app._voice_key_physicalizer = old
        self.app._voice_key_physicalizer_generation = 9
        self.app._voice_key_physicalizer_ready = False
        self.app._voice_key_physicalizer_degraded_retry_deadline = (
            time.monotonic() - 0.1
        )
        self.app._voice_shortcut.pending_tokens = None
        self.app._voice_shortcut.controller.on_mic_button_pressed()
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._voice_key_physicalizer_lifecycle_lock:
            self.app._schedule_voice_key_physicalizer_recovery_locked()

        timers[0].fire()

        self.assertTrue(self.app._voice_shortcut.controller.active)
        self.assertIsNone(self.app._voice_shortcut.pending_tokens)
        self.assertEqual(
            self.app._runtime_voice_key_physicalizer_state,
            "failed",
        )
        # The failed state still re-arms on a slow fallback retry.
        self.assertEqual(len(timers), 2)

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
        ) as send_up, mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
        ) as send_down, mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
            return_value=replacement,
        ):
            with self.app._voice_shortcut.lock:
                self.assertTrue(
                    self.app._release_hold_voice_on_physical_release_locked(
                        "late physical release"
                    )
                )
            # The authoritative cleanup drops the slow fallback and re-arms a
            # prompt retry instead of waiting out the failed-state backoff.
            self.assertTrue(timers[1].cancelled)
            self.assertEqual(len(timers), 3)
            timers[2].fire()

        send_up.assert_called_once_with(DEFAULT_VOICE_TOKENS)
        send_down.assert_not_called()
        self.assertEqual(old.stop_calls, 1)
        self.assertEqual(replacement.start_calls, 1)
        self.assertEqual(replacement.stop_calls, 0)
        self.assertIs(self.app._voice_key_physicalizer, replacement)
        self.assertTrue(self.app._voice_key_physicalizer_ready)

    def test_replacement_start_failure_replaces_retired_generation_timer(self):
        timers = []
        retired = _FakeVoicePhysicalizer(accepts_new_down=False)
        failed_replacement = _FakeVoicePhysicalizer(
            start_error=(
                app_module.voice_key_physicalizer_windows.
                VoiceKeyPhysicalizerUnavailableError("failed")
            )
        )
        ready_replacement = _FakeVoicePhysicalizer()
        self.app._voice_key_physicalizer = retired
        self.app._voice_key_physicalizer_generation = 12
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._voice_key_physicalizer_lifecycle_lock:
            self.app._schedule_voice_key_physicalizer_recovery_locked()
        retired_callback = timers[0].callback

        retired.is_running = False
        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
            side_effect=[failed_replacement, ready_replacement],
        ):
            self.app._start_voice_key_physicalizer(recovering=True)
            self.assertTrue(timers[0].cancelled)
            self.assertEqual(len(timers), 2)
            self.assertIs(
                self.app._voice_key_physicalizer_retry_timer,
                timers[1],
            )

            retired_callback()
            self.assertIs(
                self.app._voice_key_physicalizer_retry_timer,
                timers[1],
            )
            timers[1].fire()
            retired_callback()

        self.assertEqual(failed_replacement.start_calls, 1)
        self.assertEqual(ready_replacement.start_calls, 1)
        self.assertEqual(ready_replacement.stop_calls, 0)
        self.assertIs(self.app._voice_key_physicalizer, ready_replacement)
        self.assertTrue(self.app._voice_key_physicalizer_ready)

    def test_failed_degraded_owner_stop_never_starts_replacement(self):
        timers = []
        physicalizer = _FakeVoicePhysicalizer(
            accepts_new_down=False,
            stop_error=RuntimeError("stuck"),
        )
        physicalizer.is_running = True
        self.app._voice_key_physicalizer = physicalizer
        self.app._voice_key_physicalizer_generation = 5
        self.app._voice_key_physicalizer_ready = False
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._voice_key_physicalizer_lifecycle_lock:
            self.app._schedule_voice_key_physicalizer_recovery_locked()

        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
        ) as build:
            timers[0].fire()

        self.assertEqual(physicalizer.stop_calls, 1)
        self.assertIs(self.app._voice_key_physicalizer, physicalizer)
        self.assertEqual(
            self.app._runtime_voice_key_physicalizer_state,
            "failed",
        )
        build.assert_not_called()

    def test_repeated_raw_start_failures_keep_one_capped_backoff_timer(self):
        timers = []

        def timer_factory(delay, callback):
            timer = _ManualTimer(callback)
            timer.delay = delay
            timers.append(timer)
            return timer

        self.app._raw_input_timer_factory = timer_factory
        with mock.patch.object(
            app_module.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=[],
        ):
            self.app._start_hid_listener()
            with self.app._raw_input_lifecycle_lock:
                self.app._schedule_raw_input_recovery_locked()
            self.assertEqual(len(timers), 1)
            for _index in range(6):
                timers[-1].fire()

        self.assertEqual(
            [timer.delay for timer in timers],
            [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0],
        )

    def test_raw_recovery_stops_old_listener_and_replaces_it_once(self):
        timers = []
        old_listener = _FakeRecoveringRawListener()
        old_listener.is_running = True
        new_listener = _FakeRecoveringRawListener()
        self.app._hid_listener = old_listener
        self.app._raw_input_generation = 4
        self.app._raw_input_retry_delay = 8.0

        def timer_factory(delay, callback):
            timer = _ManualTimer(callback)
            timer.delay = delay
            timers.append(timer)
            return timer

        self.app._raw_input_timer_factory = timer_factory
        with self.app._raw_input_lifecycle_lock:
            self.app._schedule_raw_input_recovery_locked()

        with mock.patch.object(
            app_module.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=["fake-path"],
        ), mock.patch.object(
            app_module.hid_identity,
            "select_single_device_path",
            return_value="fake-path",
        ), mock.patch.object(
            app_module.raw_input_windows,
            "RawInputButtonListener",
            return_value=new_listener,
        ):
            timers[0].fire()

        self.assertEqual(old_listener.stop_calls, 1)
        self.assertEqual(new_listener.start_calls, 1)
        self.assertIs(self.app._hid_listener, new_listener)
        self.assertEqual(self.app._raw_input_retry_delay, 1.0)
        self.assertIsNone(self.app._raw_input_retry_timer)
        self.assertIsNotNone(new_listener.raw_callback)
        self.assertIsNotNone(new_listener.sourced_callback)

    def test_raw_recovery_stop_failure_retains_old_listener_and_never_starts_second(self):
        timers = []
        old_listener = _FakeRecoveringRawListener(
            stop_error=raw_input_windows.RawInputUnavailableError("stuck")
        )
        old_listener.is_running = True
        self.app._hid_listener = old_listener
        self.app._raw_input_generation = 4
        self.app._raw_input_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._raw_input_lifecycle_lock:
            self.app._schedule_raw_input_recovery_locked()

        with mock.patch.object(
            app_module.raw_input_windows,
            "RawInputButtonListener",
        ) as build_listener, mock.patch.object(
            app_module.raw_input_windows,
            "enumerate_matching_device_paths",
        ) as enumerate_paths:
            timers[0].fire()

        self.assertEqual(old_listener.stop_calls, 1)
        self.assertIs(self.app._hid_listener, old_listener)
        build_listener.assert_not_called()
        enumerate_paths.assert_not_called()
        self.assertEqual(len(timers), 2)
        self.assertEqual(self.app._runtime_raw_input_state, "failed_stopping")

    def test_physical_tracking_loss_schedules_listener_replacement(self):
        timers = []
        old_listener = _FakeRecoveringRawListener()
        old_listener.is_running = True
        new_listener = _FakeRecoveringRawListener()
        self.app._hid_listener = old_listener
        self.app._raw_input_generation = 4
        self.app._raw_input_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )

        self.app._on_physical_keyboard_tracking_lost(
            "physical_keyboard_edge_mismatch",
            _listener=old_listener,
            _generation=4,
        )

        self.assertEqual(self.app._raw_input_lost_generation, 4)
        self.assertEqual(self.app._runtime_raw_input_state, "unhealthy")
        self.assertEqual(len(timers), 1)

        with mock.patch.object(
            app_module.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=["fake-path"],
        ), mock.patch.object(
            app_module.hid_identity,
            "select_single_device_path",
            return_value="fake-path",
        ), mock.patch.object(
            app_module.raw_input_windows,
            "RawInputButtonListener",
            return_value=new_listener,
        ):
            timers[0].fire()

        self.assertEqual(old_listener.stop_calls, 1)
        self.assertEqual(new_listener.start_calls, 1)
        self.assertIs(self.app._hid_listener, new_listener)
        self.assertEqual(self.app._runtime_raw_input_state, "ready")

    def test_device_removed_during_start_never_publishes_ready(self):
        timers = []
        reconnects = []

        class RemovedDuringStartListener(_FakeRecoveringRawListener):
            def start(self, _device_path):
                self.start_calls += 1
                self.is_running = True
                self.removed_callback()

        listener = RemovedDuringStartListener()
        self.app._raw_input_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        self.app._supervisor.request_reconnect = lambda: reconnects.append(True)

        with mock.patch.object(
            app_module.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=["fake-path"],
        ), mock.patch.object(
            app_module.hid_identity,
            "select_single_device_path",
            return_value="fake-path",
        ), mock.patch.object(
            app_module.raw_input_windows,
            "RawInputButtonListener",
            return_value=listener,
        ):
            self.app._start_hid_listener()

        self.assertEqual(listener.start_calls, 1)
        self.assertEqual(reconnects, [True])
        self.assertEqual(
            self.app._raw_input_lost_generation,
            self.app._raw_input_generation,
        )
        self.assertEqual(self.app._runtime_raw_input_state, "recovering")
        self.assertEqual(len(timers), 1)

    def test_raw_listener_exit_between_liveness_check_and_ready_publish_never_leaves_ready(self):
        timers = []
        listener = _FakeRecoveringRawListener()
        exit_attempted = threading.Event()
        exit_finished = threading.Event()
        exit_thread = []
        original_set_runtime_state = self.app._set_runtime_input_state

        def publish_state(**changes):
            if changes.get("raw_input_state") == "ready" and not exit_thread:
                def report_exit():
                    exit_attempted.set()
                    listener.corruption_callback("raw_input_listener_exited")
                    exit_finished.set()

                worker = threading.Thread(target=report_exit)
                exit_thread.append(worker)
                worker.start()
                self.assertTrue(exit_attempted.wait(1.0))
                self.assertFalse(exit_finished.wait(0.05))
            original_set_runtime_state(**changes)

        self.app._raw_input_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with mock.patch.object(
            app_module.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=["fake-path"],
        ), mock.patch.object(
            app_module.hid_identity,
            "select_single_device_path",
            return_value="fake-path",
        ), mock.patch.object(
            app_module.raw_input_windows,
            "RawInputButtonListener",
            return_value=listener,
        ), mock.patch.object(
            self.app,
            "_set_runtime_input_state",
            side_effect=publish_state,
        ):
            self.app._start_hid_listener()

        exit_thread[0].join(1.0)
        self.assertFalse(exit_thread[0].is_alive())
        self.assertEqual(self.app._runtime_raw_input_state, "recovering")
        self.assertEqual(
            self.app._raw_input_lost_generation,
            self.app._raw_input_generation,
        )
        self.assertEqual(len(timers), 1)

    def test_stale_raw_generation_callbacks_cannot_mutate_current_input_state(self):
        old_listener = _FakeRecoveringRawListener()
        current_listener = _FakeRecoveringRawListener()
        self.app._hid_listener = current_listener
        self.app._raw_input_generation = 8
        event = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="up",
            windows_button_id="up",
            vkey=0x26,
            make_code=0x48,
            flags=0,
            message=0x0100,
        )
        reconnects = []
        self.app._supervisor.request_reconnect = lambda: reconnects.append(True)

        self.app._on_raw_physical_event(
            event,
            _listener=old_listener,
            _generation=7,
        )
        self.app._on_raw_button_event(
            "up",
            True,
            "keyboard",
            "up",
            _listener=old_listener,
            _generation=7,
        )
        self.app._on_raw_input_corruption(
            "raw_input_listener_exited",
            _listener=old_listener,
            _generation=7,
        )
        self.app._on_physical_keyboard_tracking_lost(
            "raw_input_listener_exited",
            _listener=old_listener,
            _generation=7,
        )
        self.app._on_raw_input_device_removed(
            _listener=old_listener,
            _generation=7,
        )

        self.assertEqual(self.app._raw_fallback_physical_buttons_down, {})
        self.assertEqual(self.app._raw_fallback_buttons_down, set())
        self.assertEqual(self.app._input_rearm_blocked_buttons, set())
        self.assertEqual(reconnects, [])
        self.assertIsNone(self.app._raw_input_retry_timer)

    def test_raw_recovery_waits_for_inflight_callback_then_cancels_its_state(self):
        entered_callback = threading.Event()
        allow_callback = threading.Event()
        timers = []
        old_listener = _FakeRecoveringRawListener()
        old_listener.is_running = True
        self.app._hid_listener = old_listener
        self.app._raw_input_generation = 4
        self.app._direct_hid_interception_ready = False
        self.app._direct_hid_interception_armed = False

        class BlockingPhysicalState(dict):
            def setdefault(self, key, default=None):
                entered_callback.set()
                allow_callback.wait(1.0)
                return super().setdefault(key, default)

        self.app._raw_fallback_physical_buttons_down = BlockingPhysicalState()
        self.app._raw_input_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._raw_input_lifecycle_lock:
            self.app._schedule_raw_input_recovery_locked()
        event = raw_input_windows.RawInputEvent(
            source="keyboard",
            is_pressed=True,
            button_id="up",
            windows_button_id="up",
            vkey=0x26,
            make_code=0x48,
            flags=0,
            message=0x0100,
        )
        raw_worker = threading.Thread(
            target=lambda: self.app._on_raw_physical_event(
                event,
                _listener=old_listener,
                _generation=4,
            )
        )
        recovery_worker = threading.Thread(target=timers[0].fire)

        with mock.patch.object(
            app_module.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=[],
        ), mock.patch.object(
            win32_input,
            "send_key_combo_up",
        ) as release_keys:
            raw_worker.start()
            self.assertTrue(entered_callback.wait(1.0))
            recovery_worker.start()
            recovery_worker.join(0.05)
            self.assertTrue(recovery_worker.is_alive())
            self.assertEqual(self.app._raw_input_generation, 4)
            allow_callback.set()
            raw_worker.join(1.0)
            recovery_worker.join(1.0)

        self.assertFalse(raw_worker.is_alive())
        self.assertFalse(recovery_worker.is_alive())
        self.assertEqual(old_listener.stop_calls, 1)
        self.assertEqual(self.app._raw_fallback_physical_buttons_down, {})
        self.assertEqual(self.app._input_rearm_blocked_buttons, {"up"})
        release_keys.assert_called_once_with(("up",))

    def test_inflight_raw_recovery_cannot_restart_after_final_shutdown(self):
        stop_started = threading.Event()
        allow_stop = threading.Event()
        timers = []

        class BlockingStopListener(_FakeRecoveringRawListener):
            def stop(self):
                self.stop_calls += 1
                stop_started.set()
                allow_stop.wait(1.0)
                self.is_running = False

        old_listener = BlockingStopListener()
        old_listener.is_running = True
        self.app._hid_listener = old_listener
        self.app._raw_input_generation = 4
        self.app._raw_input_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._raw_input_lifecycle_lock:
            self.app._schedule_raw_input_recovery_locked()
        recovery_worker = threading.Thread(target=timers[0].fire)
        shutdown_worker = threading.Thread(target=self.app._stop_input_channels)

        with mock.patch.object(
            app_module.raw_input_windows,
            "enumerate_matching_device_paths",
        ) as enumerate_paths, mock.patch.object(
            app_module.raw_input_windows,
            "RawInputButtonListener",
        ) as build_listener:
            recovery_worker.start()
            self.assertTrue(stop_started.wait(1.0))
            shutdown_worker.start()
            allow_stop.set()
            recovery_worker.join(1.0)
            shutdown_worker.join(1.0)

        self.assertFalse(recovery_worker.is_alive())
        self.assertFalse(shutdown_worker.is_alive())
        self.assertEqual(old_listener.stop_calls, 1)
        enumerate_paths.assert_not_called()
        build_listener.assert_not_called()
        self.assertTrue(self.app._raw_input_stopping)
        self.assertFalse(self.app._accept_input_events)
        self.assertIsNone(self.app._hid_listener)
        self.assertIsNone(self.app._raw_input_retry_timer)

    def test_raw_shutdown_cancels_recovery_and_never_restarts(self):
        timers = []

        def timer_factory(_delay, callback):
            timer = _ManualTimer(callback)
            timers.append(timer)
            return timer

        self.app._raw_input_timer_factory = timer_factory
        with mock.patch.object(
            app_module.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=[],
        ) as enumerate_paths:
            self.app._start_hid_listener()
            self.app._stop_input_channels()
            timers[0].fire()

        self.assertTrue(timers[0].cancelled)
        self.assertEqual(enumerate_paths.call_count, 1)
        self.assertIsNone(self.app._raw_input_retry_timer)
        self.assertFalse(self.app._accept_input_events)

    def test_uncontrolled_recovery_listener_is_retained_and_retried(self):
        timers = []
        listener = _FakeRecoveringRawListener(
            start_error=raw_input_windows.RawInputUnavailableError("stuck"),
            running_after_error=True,
        )

        def timer_factory(delay, callback):
            timer = _ManualTimer(callback)
            timer.delay = delay
            timers.append(timer)
            return timer

        self.app._raw_input_timer_factory = timer_factory
        with self.app._raw_input_lifecycle_lock:
            self.app._schedule_raw_input_recovery_locked()
        with mock.patch.object(
            app_module.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=["fake-path"],
        ), mock.patch.object(
            app_module.hid_identity,
            "select_single_device_path",
            return_value="fake-path",
        ), mock.patch.object(
            app_module.raw_input_windows,
            "RawInputButtonListener",
            return_value=listener,
        ):
            timers[0].fire()

        self.assertIs(self.app._hid_listener, listener)
        self.assertEqual(listener.start_calls, 1)
        self.assertEqual(len(timers), 2)
        self.assertEqual([timer.delay for timer in timers], [1.0, 2.0])

    def test_raw_listener_exit_retries_without_ble_reconnect(self):
        timers = []
        listener = _FakeRecoveringRawListener()
        listener.is_running = False
        self.app._hid_listener = listener
        self.app._raw_input_generation = 7
        reconnects = []
        self.app._supervisor.request_reconnect = lambda: reconnects.append(True)
        self.app._raw_input_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )

        self.app._on_raw_input_corruption(
            "raw_input_listener_exited",
            _listener=listener,
            _generation=7,
        )
        self.app._on_raw_input_corruption(
            "raw_input_listener_exited",
            _listener=listener,
            _generation=7,
        )

        self.assertEqual(len(timers), 1)
        self.assertEqual(reconnects, [])

    def test_stale_physicalizer_callback_cannot_replace_current_state(self):
        timers = []
        old = _FakeVoicePhysicalizer()
        current = _FakeVoicePhysicalizer()
        current.is_running = True
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        self.app._voice_key_physicalizer = current
        self.app._voice_key_physicalizer_generation = 2
        self.app._voice_key_physicalizer_ready = True

        with mock.patch.object(
            self.app,
            "_force_voice_hold_release_locked",
        ) as release_voice, mock.patch.object(
            self.app,
            "_release_pending_button_keys",
        ) as release_buttons:
            self.app._on_voice_key_physicalizer_tracking_lost(old, 1)

        self.assertTrue(self.app._voice_key_physicalizer_ready)
        self.assertEqual(timers, [])
        release_voice.assert_not_called()
        release_buttons.assert_not_called()

    def test_physicalizer_loss_with_healthy_raw_releases_only_marked_right_alt(self):
        timers = []
        physicalizer = _FakeVoicePhysicalizer()
        physicalizer.is_running = True
        self.app._voice_key_physicalizer = physicalizer
        self.app._voice_key_physicalizer_generation = 1
        self.app._voice_key_physicalizer_ready = True
        self.app._voice_shortcut.hotkey = app_module.hotkey.HotkeySpec.parse("ralt")
        self.app._voice_shortcut.pending_tokens = ("ralt",)
        self.app._voice_shortcut.pending_backend = (
            app_module._VOICE_HOTKEY_BACKEND_MARKED
        )
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )

        with mock.patch.object(
            self.app,
            "_force_voice_hold_release_locked",
            return_value=True,
        ) as release_voice, mock.patch.object(
            self.app,
            "_release_pending_button_keys",
        ) as release_buttons:
            self.app._on_voice_key_physicalizer_tracking_lost(
                physicalizer,
                1,
            )

        release_voice.assert_called_once_with(
            "voice key physicalizer stopped unexpectedly"
        )
        release_buttons.assert_not_called()
        self.assertEqual(len(timers), 1)

    def test_physicalizer_loss_with_healthy_raw_preserves_other_combos(self):
        timers = []
        physicalizer = _FakeVoicePhysicalizer()
        physicalizer.is_running = True
        self.app._voice_key_physicalizer = physicalizer
        self.app._voice_key_physicalizer_generation = 1
        self.app._voice_key_physicalizer_ready = True
        self.app._voice_shortcut.hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+f9")
        self.app._voice_shortcut.pending_tokens = ("lctrl", "f9")
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )

        with mock.patch.object(
            self.app,
            "_force_voice_hold_release_locked",
        ) as release_voice, mock.patch.object(
            self.app,
            "_release_pending_button_keys",
        ) as release_buttons:
            self.app._on_voice_key_physicalizer_tracking_lost(
                physicalizer,
                1,
            )

        release_voice.assert_not_called()
        release_buttons.assert_not_called()

    def test_second_tracker_loss_releases_pending_modifier_combo_once(self):
        timers = []
        physicalizer = _FakeVoicePhysicalizer()
        physicalizer.is_running = True
        self.app._voice_key_physicalizer = physicalizer
        self.app._voice_key_physicalizer_generation = 1
        self.app._voice_key_physicalizer_ready = True
        self.app._voice_shortcut.pending_tokens = ("lctrl", "lwin")
        self.app._button_key_release_pending = ("lctrl", "lwin")
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        raw_input_windows._set_physical_keyboard_tracker_active(False)

        with mock.patch.object(
            self.app,
            "_force_voice_hold_release_locked",
            return_value=True,
        ) as release_voice, mock.patch.object(
            self.app,
            "_release_pending_button_keys",
            return_value=True,
        ) as release_buttons:
            self.app._on_voice_key_physicalizer_tracking_lost(
                physicalizer,
                1,
            )

        release_voice.assert_called_once()
        release_buttons.assert_called_once_with()

    def test_dual_tracker_loss_without_owned_hold_sends_no_key_up(self):
        timers = []
        physicalizer = _FakeVoicePhysicalizer()
        physicalizer.is_running = True
        self.app._voice_key_physicalizer = physicalizer
        self.app._voice_key_physicalizer_generation = 1
        self.app._voice_key_physicalizer_ready = True
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        raw_input_windows._set_physical_keyboard_tracker_active(False)

        with mock.patch.object(
            self.app,
            "_force_voice_hold_release_locked",
        ) as release_voice, mock.patch.object(
            win32_input,
            "send_key_combo_up",
        ) as send_up:
            self.app._on_voice_key_physicalizer_tracking_lost(
                physicalizer,
                1,
            )

        release_voice.assert_not_called()
        send_up.assert_not_called()

    def test_raw_tracker_loss_retries_pending_combo_even_with_physicalizer_ready(self):
        self.app._voice_key_physicalizer_ready = True
        self.app._button_key_release_pending = ("lctrl", "f9")

        with mock.patch.object(win32_input, "send_key_combo_up") as send_up:
            self.app._on_physical_keyboard_tracking_lost("raw_failed")

        send_up.assert_called_once_with(("lctrl", "f9"))
        self.assertIsNone(self.app._button_key_release_pending)

    def test_raw_tracker_loss_without_button_debt_sends_no_button_key_up(self):
        with mock.patch.object(win32_input, "send_key_combo_up") as send_up:
            self.app._on_physical_keyboard_tracking_lost("raw_failed")

        send_up.assert_not_called()

    def test_final_input_shutdown_stops_all_three_owners(self):
        tap = _FakeInputOwner()
        raw = _FakeHidListener()
        physicalizer = _FakeInputOwner()
        self.app._hid_report_tap = tap
        self.app._hid_listener = raw
        self.app._voice_key_physicalizer = physicalizer

        self.app._stop_input_channels()

        self.assertEqual(tap.stop_calls, 1)
        self.assertEqual(raw.stop_calls, 1)
        self.assertEqual(physicalizer.stop_calls, 1)
        self.assertIsNone(self.app._hid_report_tap)
        self.assertIsNone(self.app._hid_listener)
        self.assertIsNone(self.app._voice_key_physicalizer)
        self.assertFalse(self.app._accept_input_events)

    def test_final_input_shutdown_cancels_release_retry_before_final_key_up(self):
        self.app._button_key_release_pending = ("up",)
        with self.app._button_action_lock:
            self.app._schedule_button_input_release_retry_locked()
        retry_timer = self._button_input_release_timers[0]

        with mock.patch.object(win32_input, "send_key_combo_up") as send_up:
            self.app._stop_input_channels()
            retry_timer.fire()

        self.assertTrue(retry_timer.cancelled)
        send_up.assert_called_once_with(("up",))
        self.assertIsNone(self.app._button_key_release_pending)
        self.assertIsNone(self.app._button_input_release_retry_timer)

    def test_final_shutdown_keeps_input_enabled_until_tap_and_raw_releases_finish(self):
        events = []
        usage = next(
            usage
            for usage, button_id in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "up"
        )
        self.app._direct_hid_usages = {usage}

        class Tap:
            def stop(inner_self):
                events.append(("tap", self.app._accept_input_events))
                self.app._on_hid_tap_status(
                    frida_compat.HidTapState.STOPPED.value,
                    "",
                )

        class Raw:
            def stop(inner_self):
                events.append(("raw", self.app._accept_input_events))

        self.app._hid_report_tap = Tap()
        self.app._hid_listener = Raw()
        with mock.patch.object(self.app, "_on_button_event") as button_event:
            self.app._stop_input_channels()

        self.assertEqual(events, [("tap", True), ("raw", True)])
        button_event.assert_not_called()
        self.assertFalse(self.app._accept_input_events)

    def test_ble_cleanup_does_not_disable_ordinary_button_mapping(self):
        _run(self.app._cleanup_once())

        with mock.patch.object(win32_input, "send_arrow_up") as tap:
            self.app._on_button_event("up", True, event_source="hid_tap")
            self.app._on_button_event("up", False, event_source="hid_tap")

        tap.assert_called_once_with()
        self.assertTrue(self.app._accept_input_events)
        self.assertFalse(self.app._accept_ble_events)

    def test_ble_cleanup_rejects_late_ble_callbacks_without_reconnect(self):
        reconnects = []
        self.app._supervisor.request_reconnect = lambda: reconnects.append(True)
        _run(self.app._cleanup_once())

        self.app._on_disconnected()
        self.app._on_session_error(RuntimeError("late"))
        self.app._on_control_event(MicButtonPressed())

        self.assertEqual(reconnects, [])


class CleanupOwnershipTests(_AppWiringTestCase):
    """BLE retries clean BLE/audio state without tearing down input."""

    def test_ble_cleanup_keeps_input_owner_and_clears_ble_audio(self):
        hid = _FakeHidListener()
        self.app._hid_listener = hid
        self.app._ble_session = _FakeBleSession()
        self.app._voice_audio.sink = _FakePlaybackSink()

        _run(self.app._cleanup_once())  # must not raise

        self.assertIs(self.app._hid_listener, hid)
        self.assertEqual(hid.stop_calls, 0)
        self.assertIsNone(self.app._ble_session)
        self.assertIsNone(self.app._voice_audio.sink)
        self.assertTrue(self.app._accept_input_events)
        self.assertFalse(self.app._accept_ble_events)


    def test_cleanup_never_releases_alt_without_bridge_owned_down(self):
        with mock.patch.object(win32_input, "send_voice_key_combo_up") as release:
            _run(self.app._cleanup_once())

        release.assert_not_called()

    def test_successful_cleanup_applies_deferred_bindings_before_reconnect(self):
        pending = config.default_key_bindings()
        pending["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()
        self.app._pending_bindings = pending

        _run(self.app._cleanup_once())

        self.assertIsNone(self.app._pending_bindings)
        self.assertEqual(
            self.app._bindings["bindings"]["mic"]["kind"],
            key_mapping.ActionKind.ESCAPE.value,
        )

    def test_incomplete_cleanup_retains_deferred_bindings(self):
        original_kind = self.app._bindings["bindings"]["mic"]["kind"]
        pending = config.default_key_bindings()
        pending["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()
        self.app._pending_bindings = pending
        self.app._ble_session = _FakeBleSession(close_raises=True)

        with self.assertRaises(app_module.CleanupIncompleteError):
            _run(self.app._cleanup_once())

        self.assertIs(self.app._pending_bindings, pending)
        self.assertEqual(
            self.app._bindings["bindings"]["mic"]["kind"],
            original_kind,
        )

    def test_cleanup_releases_and_clears_pending_ordinary_button_keys(self):
        original = win32_input.send_key_combo_up
        released = []
        self.app._button_key_release_pending = ("ctrl", "l")
        win32_input.send_key_combo_up = lambda keys: released.append(tuple(keys))
        try:
            _run(self.app._cleanup_once())
        finally:
            win32_input.send_key_combo_up = original

        self.assertEqual(released, [("ctrl", "l")])
        self.assertIsNone(self.app._button_key_release_pending)

    def test_cleanup_retains_incomplete_ordinary_button_release(self):
        original = win32_input.send_key_combo_up
        self.app._button_key_release_pending = ("ctrl", "l")
        win32_input.send_key_combo_up = lambda _keys: (_ for _ in ()).throw(
            win32_input.InputCleanupIncompleteError("still stuck")
        )
        try:
            with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
                _run(self.app._cleanup_once())
        finally:
            win32_input.send_key_combo_up = original

        self.assertIn("ordinary button input", str(ctx.exception))
        self.assertEqual(self.app._button_key_release_pending, ("ctrl", "l"))

    def test_cleanup_releases_and_clears_pending_mouse_button(self):
        released = []
        self.app._button_mouse_release_pending = "x1"
        with mock.patch.object(
            win32_input,
            "send_mouse_button_up",
            side_effect=lambda button: released.append(button),
        ):
            _run(self.app._cleanup_once())

        self.assertEqual(released, ["x1"])
        self.assertIsNone(self.app._button_mouse_release_pending)

    def test_cleanup_retains_incomplete_mouse_button_release(self):
        self.app._button_mouse_release_pending = "right"
        with mock.patch.object(
            win32_input,
            "send_mouse_button_up",
            side_effect=win32_input.InputCleanupIncompleteError("still down"),
        ), self.assertRaises(app_module.CleanupIncompleteError) as ctx:
            _run(self.app._cleanup_once())

        self.assertIn("ordinary button input", str(ctx.exception))
        self.assertEqual(self.app._button_mouse_release_pending, "right")

    def test_input_shutdown_failure_retains_hid_owner(self):
        hid = _FakeHidListener(stop_raises=True)
        self.app._hid_listener = hid

        with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
            self.app._stop_input_channels()
        self.assertIn("Raw Input listener", str(ctx.exception))

        self.assertIs(self.app._hid_listener, hid)
        self.assertFalse(self.app._accept_input_events)

    def test_ble_close_failure_retains_ble_owner_but_not_stop_input(self):
        hid = _FakeHidListener()
        ble = _FakeBleSession(close_raises=True)
        playback = _FakePlaybackSink()
        self.app._hid_listener = hid
        self.app._ble_session = ble
        self.app._voice_audio.sink = playback

        with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
            _run(self.app._cleanup_once())
        self.assertIn("BLE session", str(ctx.exception))

        # Retained, not hidden:
        self.assertIs(self.app._ble_session, ble)
        self.assertEqual(ble.close_calls, 1)
        self.assertEqual(hid.stop_calls, 0)
        self.assertIs(self.app._hid_listener, hid)
        self.assertTrue(playback.closed)
        self.assertIsNone(self.app._voice_audio.sink)

    def test_ble_and_input_failures_are_reported_by_separate_lifecycles(self):
        hid = _FakeHidListener(stop_raises=True)
        ble = _FakeBleSession(close_raises=True)
        playback = _FakePlaybackSink()
        self.app._hid_listener = hid
        self.app._ble_session = ble
        self.app._voice_audio.sink = playback

        with self.assertRaises(app_module.CleanupIncompleteError) as ble_ctx:
            _run(self.app._cleanup_once())
        self.assertIn("BLE session", str(ble_ctx.exception))

        with self.assertRaises(app_module.CleanupIncompleteError) as input_ctx:
            self.app._stop_input_channels()
        self.assertIn("Raw Input listener", str(input_ctx.exception))

        self.assertIs(self.app._hid_listener, hid)
        self.assertIs(self.app._ble_session, ble)
        self.assertTrue(playback.closed)
        self.assertIsNone(self.app._voice_audio.sink)

    def test_cleanup_failure_propagates_out_of_run_forever_without_a_second_connect(self):
        """End-to-end: wires _cleanup_once() as the real
        ConnectionSupervisor.cleanup callable and proves a retained-owner
        failure ends run_forever() entirely - no second connect()
        generation is ever attempted over the still-live HID listener.
        """

        hid = _FakeHidListener()
        self.app._hid_listener = hid
        self.app._ble_session = _FakeBleSession(close_raises=True)
        self.app._voice_audio.sink = _FakePlaybackSink()

        connect_calls = []

        async def scenario():
            # ConnectionSupervisor.__init__ captured its ``_loop`` at
            # construction time (setUp() built self.app synchronously,
            # off any running loop - see connection_supervisor.py's module
            # docstring). Rebind it to the loop this coroutine is actually
            # running on before calling request_reconnect(), exactly as
            # the real app does by constructing everything inside one
            # asyncio.run(); otherwise request_reconnect()'s
            # call_soon_threadsafe hop lands on a loop nothing drives and
            # run_forever() hangs forever on _disconnect_event.wait()
            # (XRBM-019 review round 1 P1 #1 - the prior version of this
            # test only "passed" because that loop mismatch raised into
            # the cleanup path, never proving the intended behavior).
            self.app._supervisor._loop = asyncio.get_running_loop()
            self.app._supervisor._connect = lambda: _record_connect(connect_calls)

            task = asyncio.ensure_future(self.app._supervisor.run_forever())
            # Let run_forever() run its first connect() and reach the
            # disconnect_event.wait() suspension point before we end the
            # attempt explicitly - request_reconnect() is what a real BLE
            # disconnect/protocol-error/playback-failure callback would
            # call; nothing here relies on an accidental cross-loop
            # exception to unblock the wait.
            await asyncio.sleep(0)
            self.app._supervisor.request_reconnect()

            # Bounded so a real regression (e.g. cleanup ownership lost
            # again, or the wait never unblocking) fails the test instead
            # of hanging the whole suite.
            with self.assertRaises(app_module.CleanupIncompleteError):
                await asyncio.wait_for(task, timeout=5.0)

        _run(scenario())

        self.assertEqual(connect_calls, [1])  # only the first attempt ever ran
        self.assertEqual(self.app._supervisor.attempt_count, 1)
        # BLE cleanup failure ends retries, while the process-lifetime input
        # owner remains untouched until RC003App.run_forever() exits.
        self.assertIs(self.app._hid_listener, hid)
        self.assertEqual(hid.stop_calls, 0)


class StartHidListenerOwnershipTests(_AppWiringTestCase):
    """XRBM-019 review round 1 P1 #3: RawInputButtonListener intentionally
    retains its thread/window when its own bounded failed-start cleanup
    cannot stop them (see raw_input_windows.py's ``_abandon_failed_start``).
    ``_start_hid_listener()`` must consult ``is_running`` rather than
    unconditionally clearing ``self._hid_listener`` to ``None`` on any
    failed ``start()`` - doing so would lose the owner and let a later
    ``_connect_once()`` generation start a second listener over a still-
    live one (the exact defect class XRBM-019 exists to eliminate; see
    also CleanupOwnershipTests' end-to-end supervisor test above, which
    proves no second connect() generation is ever reached once cleanup
    itself fails on a retained owner).
    """

    def _patch_device_discovery(self, fake_listener):
        original_enumerate = app_module.raw_input_windows.enumerate_matching_device_paths
        original_select = app_module.hid_identity.select_single_device_path
        original_listener_cls = app_module.raw_input_windows.RawInputButtonListener
        app_module.raw_input_windows.enumerate_matching_device_paths = lambda **_kwargs: ["fake-path"]
        app_module.hid_identity.select_single_device_path = lambda paths: paths[0]
        app_module.raw_input_windows.RawInputButtonListener = lambda callback: fake_listener

        def _restore():
            app_module.raw_input_windows.enumerate_matching_device_paths = original_enumerate
            app_module.hid_identity.select_single_device_path = original_select
            app_module.raw_input_windows.RawInputButtonListener = original_listener_cls

        return _restore

    def test_a_failed_start_that_is_still_running_retains_the_owner_and_raises(self):
        fake_listener = _FakeHidListenerForFailedStart(is_running_after_failed_start=True)
        restore = self._patch_device_discovery(fake_listener)
        try:
            with self.assertRaises(app_module.raw_input_windows.RawInputUnavailableError):
                self.app._start_hid_listener()
        finally:
            restore()

        self.assertIs(self.app._hid_listener, fake_listener)
        self.assertEqual(fake_listener.start_calls, 1)

    def test_a_failed_start_confirmed_stopped_clears_the_owner(self):
        fake_listener = _FakeHidListenerForFailedStart(is_running_after_failed_start=False)
        restore = self._patch_device_discovery(fake_listener)
        try:
            self.app._start_hid_listener()  # must not raise
        finally:
            restore()

        self.assertIsNone(self.app._hid_listener)
        self.assertEqual(fake_listener.start_calls, 1)



class HidTapStartupStateTests(_AppWiringTestCase):
    def test_pre_copy_tap_does_not_arm_global_arrow_gate(self):
        self.app._hid_report_tap = mock.Mock(native_copy_interception=True)
        with mock.patch.object(
            app_module.element_navigation_control_windows, "record_rc003_direction_edge"
        ) as navigation_gate:
            self.app._voice_key_physicalizer = mock.Mock()
            self.app._voice_key_physicalizer_ready = True
            self.app._record_rc003_direction_edge(0x4F, True)
            self.app._record_rc003_direction_edge(0x4F, False)
            self.app._voice_key_physicalizer.record_rc003_direction_edge.assert_not_called()
            navigation_gate.assert_not_called()

    def test_pre_copy_handover_does_not_release_physical_keyboard_key(self):
        self.app._hid_report_tap = mock.Mock(native_copy_interception=True)
        self.app._direct_hid_interception_armed = False
        with mock.patch.object(self.app, "_windows_buttons_down_before_hid_handover") as state_query, mock.patch.object(
            self.app, "_release_raw_fallback_keyups"
        ) as release:
            state_query.return_value = {"right"}
            self.app._on_hid_tap_status("attached_waiting_for_hid_io", "hid_interception_armed")
            state_query.assert_not_called()
            for call in release.call_args_list:
                self.assertNotIn("right", call.args[0])

    def test_unhealthy_tap_forces_release_of_its_active_voice_source(self):
        usage = next(
            usage
            for usage, button_id in app_module.frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "mic"
        )
        report = usage.to_bytes(2, "little") + b"\x00\x00\x00\x00"

        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_direct_hid_report(1, report)
            self.assertEqual(self.app._voice_mic_gesture_sources_down, {"hid_tap"})
            self.app._on_hid_tap_status("unhealthy", "socket_lost")

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertEqual(self.app._voice_mic_gesture_sources_down, set())
        self.assertFalse(self.app._voice_shortcut.controller.active)
        self.assertFalse(self.app._direct_hid_interception_ready)
        self.assertEqual(self.app._ble_session.mic_close_calls, 0)

    def test_thread_start_is_logged_separately_from_verified_ready(self):
        instances = []

        class FakeTap:
            def __init__(self, _report_handler, *, status_handler, diagnostic_trace, selected_key):
                self.status_handler = status_handler
                self.diagnostic_trace = diagnostic_trace
                self.status = "starting"
                instances.append(self)

            def start(self):
                return True

            def stop(self):
                pass

        with mock.patch.object(
            app_module.frida_compat, "RC003HidReportTap", FakeTap
        ), self.assertLogs(self.app._logger, level="INFO") as captured:
            self.app._start_hid_report_tap()
            instances[0].status_handler("ready", "hid_io_verified")

        self.assertIs(instances[0].diagnostic_trace, self.app._diagnostic_trace)

        text = "\n".join(captured.output)
        self.assertIn("tap thread started; state=starting", text)
        self.assertNotIn("tap enabled", text)
        self.assertIn("tap state: ready detail=hid_io_verified", text)

    def test_failed_start_cleanup_retains_tap_owner_and_raises(self):
        instances = []

        class StuckTap:
            status = "starting"

            def __init__(self, _report_handler, *, status_handler, diagnostic_trace, selected_key):
                instances.append(self)

            def start(self):
                raise RuntimeError("start failed")

            def stop(self):
                raise RuntimeError("stop failed")

        with mock.patch.object(
            app_module.frida_compat, "RC003HidReportTap", StuckTap
        ):
            with self.assertRaises(RuntimeError):
                self.app._start_hid_report_tap()

        self.assertIs(self.app._hid_report_tap, instances[0])


class VoiceCleanupFailurePreservesPendingStateTests(_AppWiringTestCase):
    """A failed key-up must remain owed after cleanup or audio stop."""

    def test_cleanup_releases_and_clears_an_owned_voice_hotkey(self):
        self.app._voice_shortcut.controller.on_mic_button_pressed()
        released = []

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: released.append(tuple(tokens)),
        ):
            _run(self.app._cleanup_once())

        self.assertEqual(released, [DEFAULT_VOICE_TOKENS])
        self.assertFalse(self.app._voice_shortcut.controller.active)
        self.assertFalse(self.app._voice_shortcut.controller.holding)

    def test_cleanup_once_preserves_hold_key_up_on_failure(self):
        self.app._voice_shortcut.controller.on_mic_button_pressed()
        self.assertTrue(self.app._voice_shortcut.controller.holding)

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=OSError("simulated key-up delivery failure"),
        ):
            with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
                _run(self.app._cleanup_once())

        self.assertIn("voice hotkey", str(ctx.exception))
        self.assertTrue(self.app._voice_shortcut.controller.holding)
        self.assertTrue(self.app._voice_shortcut.controller.active)

    def test_audio_stopped_preserves_hold_key_up_on_failure_and_reconnects(self):
        self.app._voice_shortcut.controller.on_mic_button_pressed()
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=OSError("simulated key-up delivery failure"),
        ):
            self.app._on_control_event(AudioStopped())

        self.assertTrue(self.app._voice_shortcut.controller.holding)
        self.assertEqual(reconnect_calls, [1])


class PlaybackCleanupOwnershipTests(_AppWiringTestCase):
    """XRBM-019 review round 1 P1 #5: both _cleanup_once() and
    _on_pcm_frame() must retain (not discard) the playback sink owner when
    its own close() call fails - EndpointPlaybackSink owns a PortAudio
    stream, and clearing the reference would hide an incompletely closed
    resource and let a reconnect open a second sink over it.
    """

    def test_open_success_logs_selected_endpoint_and_host_api(self):
        class OpenSink:
            owns_stream = False
            ready = True
            output_sample_rate_hz = 48000
            output_channels = 2

            def __init__(self, _name, _host_api):
                pass

            def open(self):
                pass

            def timing_snapshot(self):
                return app_module.audio_playback.PlaybackTimingSnapshot(
                    open_elapsed_ms=12.5,
                    last_write_elapsed_ms=0.0,
                    max_write_elapsed_ms=0.0,
                    write_count=0,
                    underflow_count=0,
                )

        self.app._voice_audio.sink = None
        self.app._config["output_endpoint_name"] = "CABLE Input"
        self.app._config["output_endpoint_host_api"] = "Windows WASAPI"
        endpoint = app_module.audio_output.AudioEndpoint(
            name="CABLE Input", host_api="Windows WASAPI"
        )

        with mock.patch.object(
            app_module.audio_output,
            "enumerate_output_endpoints",
            return_value=[endpoint],
        ), mock.patch.object(
            app_module.audio_output,
            "resolve_selected_endpoint",
            return_value=endpoint,
        ), mock.patch.object(
            app_module.audio_playback,
            "EndpointPlaybackSink",
            OpenSink,
        ), self.assertLogs(self.app._logger, level="INFO") as captured:
            self.assertTrue(self.app._voice_audio.open())

        self.assertIn(
            "voice playback opened: endpoint=CABLE Input "
            "host_api=Windows WASAPI sample_rate=48000 channels=2 open_ms=12.50",
            "\n".join(captured.output),
        )

    def test_cleanup_once_retains_playback_owner_on_close_failure(self):
        sink = _FakePlaybackSink(close_raises=True)
        self.app._hid_listener = None
        self.app._ble_session = _FakeBleSession()
        self.app._voice_audio.sink = sink

        with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
            _run(self.app._cleanup_once())
        self.assertIn("audio playback", str(ctx.exception))

        self.assertIs(self.app._voice_audio.sink, sink)
        self.assertEqual(sink.close_calls, 1)
        self.assertFalse(sink.closed)

    def test_cleanup_retains_sink_when_playback_writer_does_not_stop(self):
        sink = _FakePlaybackSink()

        class StuckWriter:
            def flush(self, _timeout=None):
                return app_module.audio_playback_worker.PlaybackFlushResult(
                    False,
                    app_module.audio_playback_worker.PlaybackFlushTimeoutError(
                        "stuck"
                    ),
                )

            def stop(self, _timeout=None):
                return False

        self.app._hid_listener = None
        self.app._ble_session = _FakeBleSession()
        self.app._voice_audio.sink = sink
        self.app._voice_audio.writer = StuckWriter()

        with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
            _run(self.app._cleanup_once())

        self.assertIn("audio playback writer", str(ctx.exception))
        self.assertIs(self.app._voice_audio.sink, sink)
        self.assertIsNotNone(self.app._voice_audio.writer)
        self.assertEqual(sink.close_calls, 0)

    def test_write_fail_then_cleanup_close_raise_retains_owner(self):
        sink = _FakePlaybackSink(fail_write=True, close_raises=True)
        self.app._voice_audio.sink = sink
        self.app._voice_pcm_forwarding_enabled = True
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        self.app._on_pcm_frame([0, 0])  # must not raise
        result = self._flush_playback()
        self.assertTrue(result.completed)
        self.assertIsInstance(result.error, OSError)

        with self.assertRaises(app_module.CleanupIncompleteError):
            _run(self.app._cleanup_once())

        # Retained, not discarded - close() also failed:
        self.assertIs(self.app._voice_audio.sink, sink)
        self.assertEqual(sink.close_calls, 1)
        # Still fails closed via reconnect either way:
        self.assertEqual(reconnect_calls, [1])

    def test_open_failure_with_unclean_stream_retains_owner_and_reconnects(self):
        instances = []

        class FailedOpenSink:
            owns_stream = True
            ready = False

            def __init__(self, _name, _host_api):
                instances.append(self)

            def open(self):
                raise app_module.audio_output.AudioOutputUnavailableError(
                    "simulated open failure"
                )

        self.app._voice_audio.sink = None
        self.app._config["output_endpoint_name"] = "CABLE Input"
        self.app._config["output_endpoint_host_api"] = "Windows WASAPI"
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)
        endpoint = app_module.audio_output.AudioEndpoint(
            name="CABLE Input", host_api="Windows WASAPI"
        )

        with mock.patch.object(
            app_module.audio_output,
            "enumerate_output_endpoints",
            return_value=[endpoint],
        ), mock.patch.object(
            app_module.audio_output,
            "resolve_selected_endpoint",
            return_value=endpoint,
        ), mock.patch.object(
            app_module.audio_playback,
            "EndpointPlaybackSink",
            FailedOpenSink,
        ):
            self.assertFalse(self.app._voice_audio.open())

        self.assertIs(self.app._voice_audio.sink, instances[0])
        self.assertEqual(reconnect_calls, [1])


class LoggingHandlerCleanupRegressionTests(unittest.TestCase):
    """Regression for XRBM-023 outcome 1: proves _AppWiringTestCase's
    tearDown fix (close/remove the FileHandler, reset ``_configured``)
    actually decouples one app build's logging handler from the next -
    the exact defect that made a real Windows CI runner's
    ``tempfile.TemporaryDirectory().cleanup()`` raise a PermissionError
    on the very first test the suite's discovery order ever runs
    (``CleanupOwnershipTests.test_ble_close_failure_retains_ble_owner_but_
    still_completes_hid_and_playback``): ``logging_setup.get_logger()``
    configures its FileHandler exactly once per process and never closes
    it, so without this cleanup the handle stays open inside that first
    test's temp directory for the rest of the run - and Windows, unlike
    POSIX, refuses to delete a directory containing a still-open handle.
    """

    def test_a_second_app_build_gets_its_own_fresh_handler_after_cleanup(self):
        tmp1 = tempfile.TemporaryDirectory()
        loop1 = None
        try:
            # _build_app_with_owned_loop() (not the bare _build_app()): this
            # test constructs RC003App synchronously, same as
            # _AppWiringTestCase.setUp() - see XRBM-026's
            # EventLoopOwnershipRegressionTests for why a bare
            # asyncio.get_event_loop() call here would leak too.
            _, loop1 = _build_app_with_owned_loop(Path(tmp1.name))
            logger = logging.getLogger(logging_setup.LOGGER_NAME)
            self.assertEqual(len(logger.handlers), 1)
            handler1 = logger.handlers[0]
            self.assertEqual(
                Path(handler1.baseFilename).parent, Path(tmp1.name) / "logs"
            )
            # App logging is asynchronous; wait for the queued startup record
            # before inspecting its worker-owned stream.
            self.assertTrue(handler1.flush_pending())
            self.assertIsNotNone(handler1.stream)

            # Exactly what _AppWiringTestCase.tearDown now does.
            handler1.close()
            logger.removeHandler(handler1)
            logging_setup._configured = False

            self.assertIsNone(handler1.stream)
            self.assertEqual(logger.handlers, [])
        finally:
            asyncio.set_event_loop(None)
            if loop1 is not None:
                loop1.close()
            for handler in list(logging.getLogger(logging_setup.LOGGER_NAME).handlers):
                handler.close()
                logging.getLogger(logging_setup.LOGGER_NAME).removeHandler(handler)
            logging_setup._configured = False
            # Must not raise: on Windows this would be the PermissionError
            # from outcome 1 if the handle above were still open.
            tmp1.cleanup()

        tmp2 = tempfile.TemporaryDirectory()
        loop2 = None
        try:
            _, loop2 = _build_app_with_owned_loop(Path(tmp2.name))
            logger = logging.getLogger(logging_setup.LOGGER_NAME)
            self.assertEqual(len(logger.handlers), 1)
            handler2 = logger.handlers[0]
            self.assertIsNot(handler2, handler1)
            self.assertEqual(
                Path(handler2.baseFilename).parent, Path(tmp2.name) / "logs"
            )

            handler2.close()
            logger.removeHandler(handler2)
            logging_setup._configured = False
        finally:
            asyncio.set_event_loop(None)
            if loop2 is not None:
                loop2.close()
            tmp2.cleanup()


class EventLoopOwnershipRegressionTests(unittest.TestCase):
    """Regression for XRBM-026 red evidence (real Windows run 29644660267):
    425 tests passed ("OK (skipped=3)"), then the process printed an ignored
    "unclosed event loop" ResourceWarning for a ProactorEventLoop plus two
    unclosed self-pipe sockets - AFTER unittest's own summary, so
    -W error::ResourceWarning never saw it and the step still exited 0.

    Root cause: RC003App.__init__ builds a ConnectionSupervisor, whose
    __init__ captures ``loop or asyncio.get_event_loop()``
    (connection_supervisor.py). _build_app() runs synchronously in
    _AppWiringTestCase.setUp(), off any running loop - unlike the real app,
    which only ever constructs RC003App inside ``asyncio.run(_run())``
    (app.py), where get_event_loop() correctly returns asyncio.run()'s own
    loop. With no running loop and nothing set for this thread,
    asyncio.get_event_loop() silently creates and caches an implicit
    default loop - shared by every _AppWiringTestCase subclass's setUp() -
    that nothing in the old test suite ever closed.

    These tests prove both halves of the fix: (1) the fixed setUp()/
    tearDown() pattern threads a per-test OWNED loop into ConnectionSupervisor
    instead of that ambient default, and (2) deterministically forcing the
    exact condition real interpreter shutdown eventually creates (every
    strong reference to a loop dropped, including asyncio's own thread-local
    cache, then a GC pass) reproduces the red evidence exactly for the OLD
    pattern while the FIXED pattern never reproduces it - in an isolated
    subprocess, so this test process's own asyncio/event-loop state is never
    touched either way.
    """

    def test_build_app_under_the_fixed_setup_pattern_captures_the_owned_loop(self):
        tmp = tempfile.TemporaryDirectory()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            app = _build_app(Path(tmp.name))
            self.assertIs(app._supervisor._loop, loop)
        finally:
            logger = logging.getLogger(logging_setup.LOGGER_NAME)
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
            logging_setup._configured = False
            tmp.cleanup()
            asyncio.set_event_loop(None)
            loop.close()

        self.assertTrue(loop.is_closed())

    def test_unowned_default_loop_pattern_reproduces_the_exact_red_evidence(self):
        # The OLD (pre-fix) construction pattern - asyncio.get_event_loop()
        # with no owned/running loop - run in an isolated subprocess, then
        # forced through the exact condition real interpreter shutdown
        # eventually creates. This deterministically reproduces the red
        # evidence's exact shape: one "unclosed event loop" plus two
        # "unclosed <socket.socket" warnings, both printed as unraisable
        # exceptions from inside __del__, while the script's own exit code
        # still reports 0 - proving why -W error::ResourceWarning alone
        # could never have caught it.
        script = (
            "import asyncio, gc\n"
            "class _Sup:\n"
            "    def __init__(self):\n"
            "        self._loop = asyncio.get_event_loop()\n"
            "objs = [_Sup() for _ in range(3)]\n"
            "assert all(o._loop is objs[0]._loop for o in objs)\n"
            "del objs\n"
            "asyncio.get_event_loop_policy()._local._loop = None\n"
            "gc.collect()\n"
            "print('done')\n"
        )
        result = subprocess.run(
            [sys.executable, "-W", "error::ResourceWarning", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("unclosed event loop", result.stderr)
        self.assertEqual(result.stderr.count("unclosed <socket.socket"), 2)

    def test_owned_and_closed_loop_pattern_never_reproduces_the_red_evidence(self):
        # Same forced-shutdown stress as the test above, but using the FIXED
        # pattern (_AppWiringTestCase.setUp()/tearDown()'s own approach: a
        # fresh loop is created, set current, then explicitly closed)
        # instead of the bare default-loop getter - proving the fix, not
        # just the bug.
        script = (
            "import asyncio, gc\n"
            "class _Sup:\n"
            "    def __init__(self, loop=None):\n"
            "        self._loop = loop or asyncio.get_event_loop()\n"
            "def _build_owned():\n"
            "    loop = asyncio.new_event_loop()\n"
            "    asyncio.set_event_loop(loop)\n"
            "    sup = _Sup()\n"
            "    asyncio.set_event_loop(None)\n"
            "    loop.close()\n"
            "    return sup\n"
            "objs = [_build_owned() for _ in range(3)]\n"
            "del objs\n"
            "asyncio.get_event_loop_policy()._local._loop = None\n"
            "gc.collect()\n"
            "print('done')\n"
        )
        result = subprocess.run(
            [sys.executable, "-W", "error::ResourceWarning", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0)
        self.assertNotIn("ResourceWarning", result.stderr)
        self.assertNotIn("unclosed", result.stderr)


async def _record_connect(connect_calls):
    connect_calls.append(1)


class _FakePresetSwitchListener:
    """Records switch targets without ever touching a real Win32 hook.

    ``start()`` raises the same unavailable error the production listener
    raises when live input is disabled, so the app's lazy-start path is
    exercised without installing a global keyboard hook in the test process.
    """

    def __init__(self, *, on_switch, logger):
        self.on_switch = on_switch
        self.logger = logger
        self.is_running = False
        self.targets = None

    def set_targets(self, targets):
        self.targets = targets

    def start(self):
        raise preset_switch_windows.PresetSwitchUnavailableError("test")

    def stop(self):
        self.is_running = False


class PresetSwitchRuntimeTests(_AppWiringTestCase):
    """RC003App runtime preset switching (process scope + switch hotkey).

    Process scope is an in-memory override: entering the bound foreground
    process swaps the live mapping, leaving it restores the saved
    key_bindings.json, and the settings file is never rewritten. A switch
    hotkey is persistent, mirroring the settings window, and holds until the
    foreground process changes.
    """

    def setUp(self):
        super().setUp()
        listener_patch = mock.patch.object(
            app_module.preset_switch_windows,
            "PresetSwitchListener",
            _FakePresetSwitchListener,
        )
        listener_patch.start()
        self.addCleanup(listener_patch.stop)

    def _create_preset(self, *, name, process="", hotkey="", button="ok"):
        if process:
            # An app profile is a sparse overlay: only the button it names
            # changes, everything else keeps the global layout.
            mapping = {
                "bindings": {button: {"kind": "escape", "keys": []}},
                "secondary_bindings": {},
                "display_notes": {},
            }
        else:
            mapping = app_profiles.snapshot(
                self.app._bindings, self.app._remote_profile
            )
            mapping["bindings"][button] = {"kind": "escape", "keys": []}
        return self.app._preset_store.create(
            name,
            mapping,
            scope="app" if process else "global",
            process=process,
            hotkey=hotkey,
        )

    def _poll_with_foreground(self, basename):
        with mock.patch.object(
            self.app, "_preset_foreground_basename", return_value=basename
        ):
            self._loop.run_until_complete(self.app._poll_preset_switch())

    def test_process_scope_overrides_in_memory_without_rewriting_settings(self):
        identity = self._create_preset(name="浏览器", process="chrome.exe")
        self.assertFalse(self.app._bindings_path.exists())
        self.assertEqual(self.app._bindings["bindings"]["ok"]["kind"], "return")

        self._poll_with_foreground("chrome.exe")

        self.assertEqual(self.app._preset_override_id, identity)
        self.assertEqual(self.app._bindings["bindings"]["ok"]["kind"], "escape")
        # App scope is an in-memory swap; the settings file is never written.
        self.assertFalse(self.app._bindings_path.exists())
        # The marker is the only cross-process signal the settings window reads.
        self.assertEqual(self.app._preset_store.applied_id(), identity)

    def test_leaving_the_bound_process_restores_the_saved_bindings(self):
        self._create_preset(name="浏览器", process="chrome.exe")
        self._poll_with_foreground("chrome.exe")
        self.assertEqual(self.app._bindings["bindings"]["ok"]["kind"], "escape")

        self._poll_with_foreground("explorer.exe")

        self.assertIsNone(self.app._preset_override_id)
        self.assertEqual(self.app._bindings["bindings"]["ok"]["kind"], "return")
        self.assertEqual(
            self.app._preset_store.applied_id(), app_profiles.GLOBAL_ID
        )

    def test_switching_between_apps_never_stacks_overlays(self):
        self._create_preset(name="浏览器", process="chrome.exe", button="ok")
        self._create_preset(name="记事本", process="notepad.exe", button="left")

        self._poll_with_foreground("chrome.exe")
        self.assertEqual(self.app._bindings["bindings"]["ok"]["kind"], "escape")

        self._poll_with_foreground("notepad.exe")
        self.assertEqual(self.app._bindings["bindings"]["left"]["kind"], "escape")
        # The previous app's overlay must not survive the direct switch.
        self.assertEqual(self.app._bindings["bindings"]["ok"]["kind"], "return")

    def test_process_scope_waits_for_idle_voice_settings(self):
        identity = self._create_preset(name="浏览器", process="chrome.exe")
        self.app._voice_mic_gesture_active = True
        self._poll_with_foreground("chrome.exe")
        self.assertIsNone(self.app._preset_override_id)
        self.assertEqual(self.app._bindings["bindings"]["ok"]["kind"], "return")
        self.assertEqual(
            self.app._preset_store.applied_id(), app_profiles.GLOBAL_ID
        )

        self.app._voice_mic_gesture_active = False
        self._poll_with_foreground("chrome.exe")
        self.assertEqual(self.app._preset_override_id, identity)
        self.assertEqual(self.app._bindings["bindings"]["ok"]["kind"], "escape")

    def test_switch_hotkey_is_persistent_and_holds_until_foreground_changes(self):
        identity = self._create_preset(
            name="快捷键配置", process="chrome.exe", hotkey="ctrl+alt+1"
        )
        self.app._preset_switch_foreground = "chrome.exe"

        self.app._activate_preset_by_hotkey(identity)

        saved = config.load_key_bindings(self.app._bindings_path)
        self.assertEqual(saved["bindings"]["ok"]["kind"], "escape")
        self.assertEqual(self.app._bindings["bindings"]["ok"]["kind"], "escape")
        self.assertIsNone(self.app._preset_override_id)
        self.assertEqual(self.app._preset_manual_foreground, "chrome.exe")
        self.assertEqual(self.app._preset_store.applied_id(), identity)

        # The manual switch holds while the foreground process is unchanged.
        self._poll_with_foreground("chrome.exe")
        self.assertIsNone(self.app._preset_override_id)
        self.assertEqual(self.app._bindings["bindings"]["ok"]["kind"], "escape")

        # Moving to another window resumes process scope (here: no match).
        self._poll_with_foreground("explorer.exe")
        self.assertIsNone(self.app._preset_manual_foreground)

    def test_hotkey_targets_use_normalized_modifier_codes(self):
        identity = self._create_preset(name="快捷键配置", hotkey="ctrl+alt+1")

        targets = self.app._build_preset_hotkey_targets()

        expected = frozenset(
            preset_switch_windows.normalize_vk(code)
            for code in win32_keys.resolve_vk_codes(["ctrl", "alt", "1"])
        )
        self.assertEqual(targets, {expected: identity})


class VoiceRemoteMicrophoneModeTests(_AppWiringTestCase):
    """模式1：只认遥控器话筒键、只支持长按；按下立即下发，松手立即结束。"""

    def _patch_voice_keys(self, calls):
        return (
            mock.patch.object(
                win32_input,
                "send_voice_key_combo_down",
                side_effect=lambda tokens: calls.append(("down", tokens)),
            ),
            mock.patch.object(
                win32_input,
                "send_voice_key_combo_up",
                side_effect=lambda tokens: calls.append(("up", tokens)),
            ),
        )

    def test_tap_press_sends_one_short_combo_without_latching(self):
        clock = [50.0]
        self.app._voice_gesture = voice_gesture.VoiceGestureLatch(
            clock=lambda: clock[0]
        )
        calls = []
        down_patch, up_patch = self._patch_voice_keys(calls)
        with down_patch, up_patch:
            self.app._on_button_event("mic", True, event_source="hid")
            self.assertEqual(calls, [("down", ("ralt",))], "按下必须立即下发")
            clock[0] += 0.05
            self.app._on_button_event("mic", False, event_source="hid")

        self.assertEqual(calls, [("down", ("ralt",)), ("up", ("ralt",))])
        self.assertFalse(self.app._voice_shortcut.controller.active)
        self.assertFalse(self.app._voice_gesture.latched, "模式1 不允许短按锁存")
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def _use_manual_press_timers(self):
        timers = []
        self.app._voice_delayed_press_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback)) or timers[-1]
        )
        return timers

    @staticmethod
    def _fire_press_timers(timers):
        # 已经丢掉的延迟定时器也还在列表里（回调会因 token 失效直接返回），
        # 边跑边取即可。
        while timers:
            timers.pop(0).fire()

    def test_short_press_delay_sends_nothing_inside_the_window(self):
        _stamp_voice_key(self.app._config, 1, short_press_resend=True)
        timers = self._use_manual_press_timers()
        clock = [50.0]
        self.app._voice_gesture = voice_gesture.VoiceGestureLatch(
            clock=lambda: clock[0]
        )
        calls = []
        down_patch, up_patch = self._patch_voice_keys(calls)
        with down_patch, up_patch:
            self.app._on_button_event("mic", True, event_source="hid")
            self.assertEqual(calls, [], "延迟发送：按下瞬间不能下发")
            self.assertTrue(self.app._voice_shortcut.controller.active)
            self.assertEqual(len(timers), 1, "按下只排一次延迟下发")
            clock[0] += 0.05
            self.app._on_button_event("mic", False, event_source="hid")
            self.assertEqual(calls, [], "窗口内松手一个键都不发")
            self.assertIsNone(
                self.app._voice_delayed_press_token,
                "窗口内松手要把还没落地的那一记整记丢掉",
            )
            self._fire_press_timers(timers)
            self.assertEqual(calls, [], "丢掉的那一记定时器到点也什么都不能发")
            self.assertFalse(self.app._voice_shortcut.controller.active)

        self.assertEqual(calls, [], "短按只切模式，宿主收不到任何按键")
        self.assertFalse(self.app._voice_gesture.latched)

    def test_delayed_press_lands_once_when_held_past_the_window(self):
        _stamp_voice_key(self.app._config, 1, short_press_resend=True)
        timers = self._use_manual_press_timers()
        clock = [300.0]
        self.app._voice_gesture = voice_gesture.VoiceGestureLatch(
            clock=lambda: clock[0]
        )
        calls = []
        down_patch, up_patch = self._patch_voice_keys(calls)
        with down_patch, up_patch:
            self.app._on_button_event("mic", True, event_source="hid")
            self.assertEqual(calls, [], "长按同样先等满窗口")
            self.assertEqual(len(timers), 1)
            timers.pop(0).fire()
            self.assertEqual(
                calls, [("down", ("ralt",))], "越过窗口才下发这一记按下"
            )
            clock[0] += 1.0
            self.app._on_button_event("mic", False, event_source="hid")

        self.assertEqual(calls, [("down", ("ralt",)), ("up", ("ralt",))])
        self.assertEqual(timers, [], "按满阈值松手直接松开，不再排任何按键")

    def test_short_press_delay_is_dropped_when_audio_stops_inside_the_window(self):
        _stamp_voice_key(self.app._config, 1, short_press_resend=True)
        timers = self._use_manual_press_timers()
        calls = []
        down_patch, up_patch = self._patch_voice_keys(calls)
        with down_patch, up_patch:
            self.app._on_control_event(MicButtonPressed())
            self.assertEqual(calls, [])
            self.app._on_control_event(AudioStopped(reason=0))
            self.assertEqual(calls, [], "窗口内音频就停了：一个键都不能发")
            self._fire_press_timers(timers)

        self.assertEqual(calls, [])
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_short_press_cycle_switches_slots_without_sending_any_key(self):
        _stamp_voice_key(self.app._config, 2, hotkey="lalt", short_press_resend=True)
        _stamp_voice_key(self.app._config, 1, short_press_resend=True)
        self.app._config["voice_cycle_short_press"] = True
        timers = self._use_manual_press_timers()
        clock = [50.0]
        self.app._voice_gesture = voice_gesture.VoiceGestureLatch(
            clock=lambda: clock[0]
        )
        calls = []
        down_patch, up_patch = self._patch_voice_keys(calls)
        with down_patch, up_patch:
            self.app._on_button_event("mic", True, event_source="hid")
            clock[0] += 0.05
            self.app._on_button_event("mic", False, event_source="hid")
            self.assertEqual(self.app._voice_cycle_slot, 2, "短按要切到下一个槽")
            self.assertEqual(calls, [], "切槽就是全部，什么都不发")
            self._fire_press_timers(timers)

            self.app._on_button_event("mic", True, event_source="hid")
            clock[0] += 0.05
            self.app._on_button_event("mic", False, event_source="hid")
            self.assertEqual(self.app._voice_cycle_slot, 1, "两记短按走完一圈")
            self._fire_press_timers(timers)

        self.assertEqual(calls, [], "短按一个键都不发")
        self.assertFalse(self.app._voice_shortcut.controller.active)
        self.assertFalse(self.app._voice_gesture.latched)

    def test_other_key_switch_moves_the_voice_cycle_slot(self):
        _stamp_voice_key(self.app._config, 2, hotkey="lalt")
        self.app._config["voice_other_key_switch"] = True

        self.app._switch_voice_mode_by_button(
            "power", app_module.button_gesture.ButtonTrigger.LONG_PRESS
        )
        self.assertEqual(self.app._voice_cycle_slot, 2, "任意按键都能推进循环游标")

        # 游标和话筒键共用，所以切完之后话筒键要改用新槽的快捷键。
        self.assertEqual(self.app._active_voice_slot(), 2)

        self.app._switch_voice_mode_by_button(
            "power", app_module.button_gesture.ButtonTrigger.SINGLE_CLICK
        )
        self.assertEqual(self.app._voice_cycle_slot, 1)

    def test_other_key_switch_is_ignored_while_the_toggle_is_off(self):
        _stamp_voice_key(self.app._config, 2, hotkey="lalt")

        self.app._switch_voice_mode_by_button(
            "power", app_module.button_gesture.ButtonTrigger.SINGLE_CLICK
        )
        self.assertIsNone(self.app._voice_cycle_slot)

    def test_other_key_switch_runs_from_real_button_events(self):
        # 回归：用户把「语音模式切换」映射到别的按键后，短按/长按都必须真的
        # 切槽。这条用例从 HID 边沿入口走完整链路，覆盖 _on_button_trigger
        # 里的动作分发——直调 _switch_voice_mode_by_button 的用例覆盖不到它。
        stored_config = config.load_config(self.app._config_path)
        stored_config["voice_other_key_switch"] = True
        _stamp_voice_key(stored_config, 2, hotkey="lalt")
        config.save_config(self.app._config_path, stored_config)

        bindings = config.default_key_bindings()
        bindings["bindings"]["ok"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_MODE_CYCLE
        ).to_dict()
        bindings["bindings"]["power"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.DISABLED
        ).to_dict()
        bindings["secondary_bindings"]["power"] = {
            "long_press": key_mapping.ButtonAction(
                key_mapping.ActionKind.VOICE_MODE_CYCLE
            ).to_dict()
        }
        config.save_key_bindings(self.app._bindings_path, bindings)
        self.app._reload_settings_if_changed()

        timers = []
        self.app._button_gestures._timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback)) or timers[-1]
        )

        self.app._on_button_event("ok", True, event_source="hid")
        self.app._on_button_event("ok", False, event_source="hid")
        self.assertEqual(self.app._voice_cycle_slot, 2, "单击映射必须真的切槽")

        self.app._on_button_event("power", True, event_source="hid")
        self.assertEqual(len(timers), 1, "长按必须先等手势判定")
        while timers:
            timers.pop(0).fire()
        self.app._on_button_event("power", False, event_source="hid")
        self.assertEqual(self.app._voice_cycle_slot, 1, "长按映射必须真的切槽")

    def test_short_press_delay_is_ignored_in_local_microphone_mode(self):
        _stamp_voice_key(self.app._config, 1, short_press_resend=True)
        _stamp_voice_mode(self.app._config, config.VOICE_INPUT_SOURCE_LOCAL)
        timers = self._use_manual_press_timers()
        calls = []
        down_patch, up_patch = self._patch_voice_keys(calls)
        with down_patch, up_patch:
            self.app._on_control_event(MicButtonPressed())
            self.assertIsNone(
                self.app._voice_delayed_press_token, "模式2 不延迟下发"
            )
            self.app._on_control_event(AudioStopped(reason=0))

        self.assertEqual(timers, [], "模式2 不应该排延迟下发")

    def test_a_new_press_after_a_short_press_starts_a_fresh_window(self):
        _stamp_voice_key(self.app._config, 1, short_press_resend=True)
        timers = self._use_manual_press_timers()
        clock = [50.0]
        self.app._voice_gesture = voice_gesture.VoiceGestureLatch(
            clock=lambda: clock[0]
        )
        calls = []
        down_patch, up_patch = self._patch_voice_keys(calls)
        with down_patch, up_patch:
            self.app._on_button_event("mic", True, event_source="hid")
            clock[0] += 0.05
            self.app._on_button_event("mic", False, event_source="hid")
            self.assertEqual(calls, [], "第一记是短按：什么都不发")
            self.assertIsNone(self.app._voice_delayed_press_token)
            self._fire_press_timers(timers)
            self.assertEqual(calls, [], "第一记作废后定时器不能再发键")
            # 再按一记并一直按住：这时才重新排一次延迟下发。
            clock[0] += 333.0
            self.app._on_button_event("mic", True, event_source="hid")
            self.assertEqual(len(timers), 1, "新的一记要重新计时")
            timers.pop(0).fire()
            self.assertEqual(
                calls, [("down", ("ralt",))], "上一记不能漏出幽灵按键"
            )
            clock[0] += 1.0
            self.app._on_button_event("mic", False, event_source="hid")

        self.assertEqual(calls, [("down", ("ralt",)), ("up", ("ralt",))])
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_held_press_injects_once_at_press_time(self):
        clock = [100.0]
        self.app._voice_gesture = voice_gesture.VoiceGestureLatch(
            clock=lambda: clock[0]
        )
        calls = []
        down_patch, up_patch = self._patch_voice_keys(calls)
        with down_patch, up_patch:
            self.app._on_button_event("mic", True, event_source="hid")
            self.assertEqual(calls, [("down", ("ralt",))])
            self.assertTrue(self.app._voice_shortcut.controller.active)
            clock[0] += 1.0
            self.app._on_button_event("mic", False, event_source="hid")

        self.assertEqual(calls, [("down", ("ralt",)), ("up", ("ralt",))])
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_audio_start_after_the_press_does_not_repeat_the_hotkey(self):
        calls = []
        down_patch, up_patch = self._patch_voice_keys(calls)
        with down_patch, up_patch:
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_control_event(AudioStarted(session_id=1))
            self.assertEqual(
                calls,
                [("down", ("ralt",))],
                "音频流到达不能重复下发",
            )
            self.assertTrue(self.app._voice_shortcut.controller.active)
            self.app._on_button_event("mic", False, event_source="hid")
        self.assertEqual(calls, [("down", ("ralt",)), ("up", ("ralt",))])

    def test_extra_voice_keys_are_inert_in_remote_microphone_mode(self):
        bindings = config.default_key_bindings()
        bindings["bindings"]["power"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD_2
        ).to_dict()
        config.save_key_bindings(self.app._bindings_path, bindings)
        self.app._reload_settings_if_changed()

        calls = []
        down_patch, up_patch = self._patch_voice_keys(calls)
        with down_patch, up_patch:
            self.app._on_button_event("power", True, event_source="hid_tap")
            self.app._on_button_event("power", False, event_source="hid_tap")

        self.assertEqual(calls, [])
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_atvv_press_injects_immediately_and_stops_with_the_stream(self):
        calls = []
        down_patch, up_patch = self._patch_voice_keys(calls)
        with down_patch, up_patch:
            self.app._on_control_event(MicButtonPressed())
            self.assertEqual(calls, [("down", ("ralt",))], "ATVV 触发必须立即下发")
            self.assertTrue(self.app._voice_shortcut.controller.active)
            self.app._on_control_event(AudioStarted(session_id=1))
            self.assertEqual(calls, [("down", ("ralt",))], "音频流到达不能重复下发")
            self.app._on_control_event(AudioStopped(reason=0))

        self.assertEqual(calls, [("down", ("ralt",)), ("up", ("ralt",))])
        self.assertFalse(self.app._voice_shortcut.controller.active)
        self.assertFalse(self.app._voice_gesture.latched, "模式1 不允许短按锁存")
        self.assertEqual(self.app._ble_session.mic_open_calls, 1)

    def test_atvv_held_press_keeps_the_session_until_audio_stop(self):
        clock = [200.0]
        self.app._voice_gesture = voice_gesture.VoiceGestureLatch(
            clock=lambda: clock[0]
        )
        calls = []
        down_patch, up_patch = self._patch_voice_keys(calls)
        with down_patch, up_patch:
            self.app._on_control_event(MicButtonPressed())
            self.assertEqual(calls, [("down", ("ralt",))])
            clock[0] += 1.0
            self.app._on_control_event(AudioStopped(reason=0))

        self.assertEqual(calls, [("down", ("ralt",)), ("up", ("ralt",))])
        self.assertFalse(self.app._voice_shortcut.controller.active)
        self.assertFalse(self.app._voice_gesture.latched)

    def test_audio_started_first_injects_immediately(self):
        calls = []
        down_patch, up_patch = self._patch_voice_keys(calls)
        with down_patch, up_patch:
            self.app._on_control_event(AudioStarted(session_id=1))
            self.assertEqual(
                calls,
                [("down", ("ralt",))],
                "音频流先到时也必须立即下发",
            )
            self.app._on_control_event(AudioStopped(reason=0))

        self.assertEqual(calls, [("down", ("ralt",)), ("up", ("ralt",))])
        self.assertFalse(self.app._voice_shortcut.controller.active)
        self.assertFalse(self.app._voice_gesture.latched)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def _bind_mic_to_interception(self):
        self.app._bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            kind=key_mapping.ActionKind.VOICE_HOLD,
            inject_mode=key_mapping.INJECT_MODE_INTERCEPTION,
        ).to_dict()

    def test_press_immediately_reaches_the_interception_backend(self):
        self._bind_mic_to_interception()
        backend = mock.Mock()
        with mock.patch.object(
            app_module.injection_backend,
            "backend_for",
            return_value=backend,
        ) as backend_for:
            self.app._on_control_event(MicButtonPressed())
            backend_for.assert_called_once_with(
                key_mapping.INJECT_MODE_INTERCEPTION
            )
            backend.key_down.assert_called_once_with(("ralt",))
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(AudioStopped(reason=0))

        backend.key_up.assert_called_once_with(("ralt",))
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_short_press_delay_replays_through_the_interception_backend(self):
        self._bind_mic_to_interception()
        _stamp_voice_key(self.app._config, 1, short_press_resend=True)
        timers = self._use_manual_press_timers()
        calls = []
        with mock.patch.object(
            app_module.injection_backend,
            "send_hold_tokens_down",
            side_effect=lambda tokens: calls.append(("driver-down", tokens)),
        ), mock.patch.object(
            app_module.injection_backend,
            "send_hold_tokens_up",
            side_effect=lambda tokens: calls.append(("driver-up", tokens)),
        ), mock.patch.object(
            app_module.injection_backend,
            "send_hold_tokens_repeat",
            side_effect=lambda tokens: calls.append(("driver-repeat", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("sendinput-down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("sendinput-up", tokens)),
        ):
            self.app._on_button_event("mic", True, event_source="hid")
            self.assertEqual(calls, [], "驱动级注入同样延迟")
            timers.pop(0).fire()
            self.assertEqual(calls, [("driver-down", ("ralt",))])
            self.app._on_button_event("mic", False, event_source="hid")

        self.assertEqual(
            calls,
            [("driver-down", ("ralt",)), ("driver-up", ("ralt",))],
            "延迟下发必须走驱动级，窗口走完只有一次按下/松开",
        )
        self.assertFalse(self.app._voice_shortcut.controller.active)


if __name__ == "__main__":
    unittest.main()
