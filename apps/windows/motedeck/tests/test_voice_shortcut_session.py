import time
import unittest
from unittest import mock

from motedeck import (
    injection_backend,
    voice_controller,
    voice_shortcut_session as shortcut,
    win32_input,
)


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class _ManualTimer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if self.started and not self.cancelled:
            self.callback()


def make_session(hotkey_text="ctrl+win", backend=shortcut._VOICE_HOTKEY_BACKEND_MARKED):
    services = mock.Mock()
    services.configured_backend.return_value = backend
    # No playback mute guard unless a test arms one explicitly.
    services.prepare_mute_guard.return_value = None
    trace = mock.Mock()
    trace.current_context.return_value = {}
    session = shortcut.VoiceShortcutSession(
        hotkey_text=hotkey_text, logger=mock.Mock(), trace=trace, services=services)
    # Park the per-hold worker threads for tests that only care about the
    # press/release state machine; repeat/guard tests lower these explicitly.
    session._repeat_initial_delay = 3600.0
    session._mute_poll_seconds = 3600.0
    return session, services


class RecordedTokensTests(unittest.TestCase):
    def test_recorded_tokens_combine_modifiers_and_key(self):
        session, _ = make_session("ctrl+win")
        self.assertEqual(session.recorded_tokens(), ("ctrl", "win"))

    def test_recorded_tokens_are_empty_without_a_recorded_shortcut(self):
        for text in ("", "   "):
            with self.subTest(text=text):
                session, _ = make_session(text)
                self.assertIsNone(session.hotkey)
                self.assertEqual(session.recorded_tokens(), ())


class ApplyTests(unittest.TestCase):
    def test_key_down_sends_the_recorded_tokens_and_tracks_pending_state(self):
        session, services = make_session()
        with mock.patch.object(shortcut.win32_input, "send_voice_key_combo_down") as down:
            self.assertTrue(session.apply(voice_controller.VoiceHostAction.KEY_DOWN))
        down.assert_called_once_with(("ctrl", "win"))
        self.assertEqual(session.pending_tokens, ("ctrl", "win"))
        self.assertEqual(session.pending_backend, shortcut._VOICE_HOTKEY_BACKEND_MARKED)
        self.assertEqual(session.active_backend, shortcut._VOICE_HOTKEY_BACKEND_MARKED)
        services.configured_backend.assert_called_once_with()
        session._stop_hold_repeat_locked()

    def test_key_up_releases_clears_state_and_resumes_tracking(self):
        session, services = make_session()
        with mock.patch.object(shortcut.win32_input, "send_voice_key_combo_down"), \
                mock.patch.object(shortcut.win32_input, "send_voice_key_combo_up") as up:
            session.apply(voice_controller.VoiceHostAction.KEY_DOWN)
            self.assertTrue(session.apply(voice_controller.VoiceHostAction.KEY_UP))
        up.assert_called_once_with(("ctrl", "win"))
        self.assertIsNone(session.pending_tokens)
        self.assertIsNone(session.pending_backend)
        self.assertIsNone(session.pending_inject_mode)
        self.assertIsNone(session.active_backend)
        services.resume_tracking.assert_called_once_with()

    def test_key_up_reuses_the_press_injection_mode(self):
        session, _ = make_session()
        backend = mock.Mock()
        with mock.patch.object(
            shortcut.injection_backend, "backend_for", return_value=backend
        ) as backend_for:
            session.apply(
                voice_controller.VoiceHostAction.KEY_DOWN,
                inject_mode=injection_backend.INJECT_MODE_WINUHID,
            )
            self.assertEqual(
                session.pending_inject_mode, injection_backend.INJECT_MODE_WINUHID
            )
            session.apply(voice_controller.VoiceHostAction.KEY_UP)
        backend_for.assert_any_call(injection_backend.INJECT_MODE_WINUHID)
        backend.key_down.assert_called_once_with(("ctrl", "win"))
        backend.key_up.assert_called_once_with(("ctrl", "win"))

    def test_overrides_replace_the_configured_backend_and_tokens(self):
        session, services = make_session()
        with mock.patch.object(shortcut.win32_input, "send_voice_key_combo_down") as down:
            session.apply(
                voice_controller.VoiceHostAction.KEY_DOWN,
                backend_override="override_backend",
                tokens_override=("ralt",),
                voice_slot=2,
            )
        down.assert_called_once_with(("ralt",))
        self.assertEqual(session.pending_backend, "override_backend")
        self.assertEqual(session.active_backend, "override_backend")
        services.configured_backend.assert_not_called()
        session._stop_hold_repeat_locked()

    def test_unavailable_backend_clears_pending_state_and_reports_failure(self):
        session, _ = make_session()
        with mock.patch.object(
            shortcut.win32_input,
            "send_voice_key_combo_down",
            side_effect=win32_input.Win32InputUnavailableError("no backend"),
        ):
            self.assertFalse(session.apply(voice_controller.VoiceHostAction.KEY_DOWN))
        self.assertIsNone(session.pending_tokens)
        self.assertIsNone(session.active_backend)

    def test_os_error_clears_pending_state_and_reports_failure(self):
        session, _ = make_session()
        with mock.patch.object(
            shortcut.win32_input,
            "send_voice_key_combo_down",
            side_effect=OSError("boom"),
        ):
            self.assertFalse(session.apply(voice_controller.VoiceHostAction.KEY_DOWN))
        self.assertIsNone(session.pending_tokens)
        self.assertIsNone(session.active_backend)

    def test_incomplete_cleanup_keeps_the_release_pending_and_schedules_retry(self):
        session, _ = make_session()
        timer = mock.Mock()
        created = []

        def factory(delay, callback):
            created.append((delay, callback))
            return timer

        session.timer_factory = factory
        with mock.patch.object(
            shortcut.win32_input,
            "send_voice_key_combo_up",
            side_effect=win32_input.InputCleanupIncompleteError("stuck"),
        ):
            self.assertFalse(
                session.apply(
                    voice_controller.VoiceHostAction.KEY_UP,
                    tokens_override=("ctrl", "win"),
                )
            )
        self.assertEqual(session.pending_tokens, ("ctrl", "win"))
        self.assertEqual(len(created), 1)
        self.assertEqual(
            created[0][0], shortcut._VOICE_HOTKEY_RELEASE_RETRY_INITIAL_SECONDS
        )
        timer.start.assert_called_once_with()


class ReleasePendingTests(unittest.TestCase):
    def test_release_pending_without_pending_tokens_is_a_noop(self):
        session, services = make_session()
        self.assertTrue(session.release_pending())
        services.resume_tracking.assert_not_called()

    def test_release_pending_sends_key_up_and_resumes_tracking(self):
        session, services = make_session()
        session.pending_tokens = ("ctrl", "win")
        session.pending_backend = "backend"
        session.active_backend = "backend"
        with mock.patch.object(shortcut.win32_input, "send_voice_key_combo_up") as up:
            self.assertTrue(session.release_pending())
        up.assert_called_once_with(("ctrl", "win"))
        self.assertIsNone(session.pending_tokens)
        self.assertIsNone(session.pending_backend)
        self.assertIsNone(session.active_backend)
        services.resume_tracking.assert_called_once_with()

    def test_release_pending_failure_reschedules_the_retry(self):
        session, services = make_session()
        session.pending_tokens = ("ctrl", "win")
        timer = mock.Mock()
        session.timer_factory = lambda delay, callback: timer
        with mock.patch.object(
            shortcut.win32_input,
            "send_voice_key_combo_up",
            side_effect=OSError("boom"),
        ):
            self.assertFalse(session.release_pending())
        self.assertEqual(session.pending_tokens, ("ctrl", "win"))
        timer.start.assert_called_once_with()
        services.resume_tracking.assert_not_called()


class ReleaseRetryTests(unittest.TestCase):
    def test_schedule_release_retry_is_skipped_without_pending_tokens(self):
        session, _ = make_session()
        session.schedule_release_retry()
        self.assertIsNone(session.retry_timer)

    def test_schedule_release_retry_is_skipped_while_stopping(self):
        session, _ = make_session()
        session.pending_tokens = ("ctrl",)
        session.retry_stopping = True
        session.schedule_release_retry()
        self.assertIsNone(session.retry_timer)

    def test_retry_release_ignores_a_stale_token(self):
        session, _ = make_session()
        session.pending_tokens = ("ctrl",)
        session.retry_token = object()
        with mock.patch.object(
            shortcut.VoiceShortcutSession, "release_pending"
        ) as release:
            session._retry_release(object())
        release.assert_not_called()

    def test_retry_release_doubles_the_delay_and_releases(self):
        session, _ = make_session()
        session.pending_tokens = ("ctrl",)
        token = object()
        session.retry_token = token
        session.retry_timer = mock.Mock()
        with mock.patch.object(
            shortcut.VoiceShortcutSession, "release_pending"
        ) as release:
            session._retry_release(token)
        release.assert_called_once_with()
        self.assertEqual(
            session.retry_delay,
            shortcut._VOICE_HOTKEY_RELEASE_RETRY_INITIAL_SECONDS * 2.0,
        )
        self.assertIsNone(session.retry_token)
        self.assertIsNone(session.retry_timer)

    def test_cancel_release_retry_cancels_the_timer_and_resets_state(self):
        session, _ = make_session()
        timer = mock.Mock()
        session.retry_timer = timer
        session.retry_token = object()
        session.retry_delay = 1.0
        session.cancel_release_retry()
        timer.cancel.assert_called_once_with()
        self.assertIsNone(session.retry_timer)
        self.assertIsNone(session.retry_token)
        self.assertEqual(
            session.retry_delay, shortcut._VOICE_HOTKEY_RELEASE_RETRY_INITIAL_SECONDS
        )


class HoldRepeatTests(unittest.TestCase):
    def _press(self, session):
        with mock.patch.object(shortcut.win32_input, "send_voice_key_combo_down"):
            self.assertTrue(session.apply(voice_controller.VoiceHostAction.KEY_DOWN))

    def test_one_long_lived_thread_repeats_for_the_whole_hold(self):
        session, _ = make_session()
        session._repeat_initial_delay = 0.01
        backend = mock.Mock(name="marked_keybd_event")
        backend.name = shortcut._VOICE_HOTKEY_BACKEND_MARKED
        with mock.patch.object(
            shortcut.injection_backend, "backend_for", return_value=backend
        ), mock.patch.object(
            shortcut.win32_input, "keyboard_repeat_interval", return_value=3600.0
        ):
            self._press(session)
            thread = session.repeat_thread
            self.assertIsNotNone(thread)
            self.assertEqual(thread.name, "voice-hotkey-repeat")
            self.assertTrue(wait_for(lambda: backend.key_repeat.call_count >= 1))
            backend.key_repeat.assert_called_with(("ctrl", "win"))
            backend.key_up.assert_not_called()
            with mock.patch.object(shortcut.win32_input, "send_voice_key_combo_up"):
                self.assertTrue(
                    session.apply(voice_controller.VoiceHostAction.KEY_UP)
                )
            self.assertTrue(wait_for(lambda: not thread.is_alive()))
            calls = backend.key_repeat.call_count
            time.sleep(0.05)
            self.assertEqual(backend.key_repeat.call_count, calls)
        self.assertIsNone(session.repeat_timer)

    def test_key_up_before_the_initial_delay_never_repeats(self):
        session, _ = make_session()
        session._repeat_initial_delay = 0.05
        backend = mock.Mock(name="marked_keybd_event")
        backend.name = shortcut._VOICE_HOTKEY_BACKEND_MARKED
        with mock.patch.object(
            shortcut.injection_backend, "backend_for", return_value=backend
        ):
            self._press(session)
            with mock.patch.object(shortcut.win32_input, "send_voice_key_combo_up"):
                session.apply(voice_controller.VoiceHostAction.KEY_UP)
            time.sleep(0.12)
        backend.key_repeat.assert_not_called()

    def test_winuhid_backend_does_not_start_repeat(self):
        session, _ = make_session()
        backend = mock.Mock()
        backend.name = injection_backend.INJECT_MODE_WINUHID
        with mock.patch.object(
            shortcut.injection_backend, "backend_for", return_value=backend
        ):
            self._press(session)
        self.assertIsNone(session.repeat_thread)
        session._stop_hold_repeat_locked()

    def test_failed_repeat_stops_the_hold(self):
        session, _ = make_session()
        session._repeat_initial_delay = 0.01
        backend = mock.Mock()
        backend.name = shortcut._VOICE_HOTKEY_BACKEND_MARKED
        backend.key_repeat.side_effect = OSError("boom")
        with mock.patch.object(
            shortcut.injection_backend, "backend_for", return_value=backend
        ):
            self._press(session)
            self.assertTrue(wait_for(lambda: session.repeat_stopping))
            thread = session.repeat_thread
            if thread is not None:
                self.assertTrue(wait_for(lambda: not thread.is_alive()))
        session.logger.exception.assert_called()
        session._stop_hold_repeat_locked()


class MuteGuardTests(unittest.TestCase):
    def _press(self, session):
        with mock.patch.object(shortcut.win32_input, "send_voice_key_combo_down"):
            self.assertTrue(session.apply(voice_controller.VoiceHostAction.KEY_DOWN))

    def test_key_down_arms_the_guard_before_injection_and_polls(self):
        session, services = make_session()
        session._mute_poll_seconds = 0.01
        guard = mock.Mock()
        guard.restore_if_muted.return_value = "waiting"
        order = []

        def prepare():
            order.append("prepare")
            return guard

        services.prepare_mute_guard.side_effect = prepare
        with mock.patch.object(
            shortcut.win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda *_args: order.append("down"),
        ):
            self.assertTrue(
                session.apply(voice_controller.VoiceHostAction.KEY_DOWN)
            )
        self.assertEqual(order[:2], ["prepare", "down"])
        self.assertTrue(wait_for(lambda: guard.restore_if_muted.call_count >= 1))
        with mock.patch.object(shortcut.win32_input, "send_voice_key_combo_up"):
            session.apply(voice_controller.VoiceHostAction.KEY_UP)
        calls = guard.restore_if_muted.call_count
        time.sleep(0.05)
        self.assertEqual(guard.restore_if_muted.call_count, calls)

    def test_external_mute_is_restored_and_logged(self):
        session, services = make_session()
        session._mute_poll_seconds = 0.01
        guard = mock.Mock()
        guard.restore_if_muted.return_value = "restored"
        services.prepare_mute_guard.return_value = guard
        self._press(session)
        self.assertTrue(wait_for(lambda: session.logger.warning.called))
        with mock.patch.object(shortcut.win32_input, "send_voice_key_combo_up"):
            session.apply(voice_controller.VoiceHostAction.KEY_UP)

    def test_session_changed_rearms_with_a_fresh_guard(self):
        session, services = make_session()
        session._mute_poll_seconds = 0.01
        guard = mock.Mock()
        guard.restore_if_muted.return_value = "session_changed"
        fresh = mock.Mock()
        services.prepare_mute_guard.side_effect = [guard, fresh]
        self._press(session)
        self.assertTrue(wait_for(lambda: session.mute_guard is fresh))
        with mock.patch.object(shortcut.win32_input, "send_voice_key_combo_up"):
            session.apply(voice_controller.VoiceHostAction.KEY_UP)

    def test_session_changed_without_a_new_guard_stops_polling(self):
        session, services = make_session()
        session._mute_poll_seconds = 0.01
        guard = mock.Mock()
        guard.restore_if_muted.return_value = "session_changed"
        services.prepare_mute_guard.side_effect = [guard, None]
        self._press(session)
        self.assertTrue(wait_for(lambda: session.mute_guard_stopping))
        self.assertIsNone(session.mute_guard)

    def test_key_up_stops_the_polling_and_discards_the_guard(self):
        session, services = make_session()
        session._mute_poll_seconds = 0.01
        guard = mock.Mock()
        guard.restore_if_muted.return_value = "waiting"
        services.prepare_mute_guard.return_value = guard
        self._press(session)
        with mock.patch.object(shortcut.win32_input, "send_voice_key_combo_up"):
            self.assertTrue(session.apply(voice_controller.VoiceHostAction.KEY_UP))
        self.assertIsNone(session.mute_guard)
        calls = guard.restore_if_muted.call_count
        time.sleep(0.05)
        self.assertEqual(guard.restore_if_muted.call_count, calls)

    def test_failed_polling_stops_the_guard(self):
        session, services = make_session()
        session._mute_poll_seconds = 0.01
        guard = mock.Mock()
        guard.restore_if_muted.side_effect = OSError("audio gone")
        services.prepare_mute_guard.return_value = guard
        self._press(session)
        self.assertTrue(wait_for(lambda: session.mute_guard_stopping))
        self.assertIsNone(session.mute_guard_timer)

    def test_prepare_failure_never_breaks_the_hotkey(self):
        session, services = make_session()
        services.prepare_mute_guard.side_effect = OSError("no audio endpoint")
        self._press(session)
        self.assertIsNone(session.mute_guard)
        self.assertIsNone(session.mute_guard_thread)
        session.logger.exception.assert_called()
        session._stop_hold_repeat_locked()


class VoiceControllerTests(unittest.TestCase):
    def test_press_marks_the_session_active_until_reset(self):
        session, _ = make_session()
        self.assertFalse(session.controller.active)
        action = session.controller.on_mic_button_pressed()
        self.assertEqual(action, voice_controller.VoiceHostAction.KEY_DOWN)
        self.assertTrue(session.controller.active)
        self.assertEqual(
            session.controller.reset(), voice_controller.VoiceHostAction.KEY_UP
        )
        self.assertFalse(session.controller.active)
        self.assertIsNone(session.controller.reset())

    def test_cancel_pending_clears_the_session_without_a_host_action(self):
        session, _ = make_session()
        session.controller.on_mic_button_pressed()
        self.assertTrue(session.controller.active)
        session.controller.cancel_pending()
        self.assertFalse(session.controller.active)
        self.assertIsNone(session.controller.reset())


if __name__ == "__main__":
    unittest.main()
