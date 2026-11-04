import unittest
from unittest import mock

from ovb_rc003 import (
    injection_backend,
    voice_controller,
    voice_shortcut_session as shortcut,
    win32_input,
)


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
    trace = mock.Mock()
    trace.current_context.return_value = {}
    session = shortcut.VoiceShortcutSession(
        hotkey_text=hotkey_text, logger=mock.Mock(), trace=trace, services=services)
    session.timers = []

    def timer_factory(delay, callback):
        timer = _ManualTimer(delay, callback)
        session.timers.append(timer)
        return timer

    # Never leave a real timer running in a unit test: the repeat tick would
    # inject a real key-down 0.55 s after a synthetic KEY_DOWN.
    session.timer_factory = timer_factory
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
        with mock.patch.object(
            shortcut.injection_backend, "send_hold_tokens_down"
        ) as down, mock.patch.object(
            shortcut.injection_backend, "send_hold_tokens_up"
        ) as up:
            session.apply(
                voice_controller.VoiceHostAction.KEY_DOWN,
                inject_mode=injection_backend.INJECT_MODE_INTERCEPTION,
            )
            self.assertEqual(
                session.pending_inject_mode, injection_backend.INJECT_MODE_INTERCEPTION
            )
            session.apply(voice_controller.VoiceHostAction.KEY_UP)
        down.assert_called_once_with(("ctrl", "win"))
        up.assert_called_once_with(("ctrl", "win"))

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
        return session.timers[-1]

    def test_key_down_starts_the_first_repeat_at_the_hold_boundary(self):
        session, _ = make_session()
        timer = self._press(session)
        self.assertEqual(
            timer.delay, shortcut._VOICE_HOTKEY_REPEAT_INITIAL_DELAY_SECONDS
        )
        self.assertTrue(timer.started)
        self.assertEqual(session.repeat_timer, timer)

    def test_repeat_resends_only_the_held_key_and_reschedules(self):
        session, _ = make_session()
        timer = self._press(session)
        backend = mock.Mock()
        with mock.patch.object(
            shortcut.injection_backend, "backend_for", return_value=backend
        ) as backend_for, mock.patch.object(
            shortcut.win32_input, "keyboard_repeat_interval", return_value=0.031
        ):
            timer.fire()
        backend_for.assert_called_once_with(session.pending_inject_mode)
        backend.key_repeat.assert_called_once_with(("ctrl", "win"))
        backend.key_down.assert_not_called()
        backend.key_up.assert_not_called()
        self.assertEqual(len(session.timers), 2)
        self.assertEqual(session.timers[-1].delay, 0.031)
        self.assertTrue(session.timers[-1].started)

    def test_key_up_cancels_the_pending_repeat(self):
        session, _ = make_session()
        timer = self._press(session)
        with mock.patch.object(
            shortcut.win32_input, "send_voice_key_combo_up"
        ), mock.patch.object(
            shortcut.injection_backend, "backend_for"
        ) as backend_for:
            self.assertTrue(session.apply(voice_controller.VoiceHostAction.KEY_UP))
        self.assertTrue(timer.cancelled)
        backend_for.reset_mock()
        timer.fire()
        backend_for.assert_not_called()
        self.assertIsNone(session.repeat_timer)

    def test_stale_repeat_tick_is_ignored(self):
        session, _ = make_session()
        self._press(session)
        with mock.patch.object(
            shortcut.injection_backend, "backend_for"
        ) as backend_for:
            session._repeat_tick(object())
        backend_for.assert_not_called()

    def test_failed_repeat_stops_the_hold(self):
        session, _ = make_session()
        timer = self._press(session)
        with mock.patch.object(
            shortcut.injection_backend,
            "backend_for",
            side_effect=OSError("boom"),
        ):
            timer.fire()
        self.assertTrue(session.repeat_stopping)
        self.assertIsNone(session.repeat_timer)


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
