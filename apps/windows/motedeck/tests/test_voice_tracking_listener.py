"""物理键盘跟踪监听器的回归测试：共享计数 + 只启动一次。"""
import logging
import threading
import unittest
from unittest import mock

from motedeck import app_input_channels, raw_input_windows


class PhysicalKeyboardTrackerRefCountTests(unittest.TestCase):
    def setUp(self):
        with raw_input_windows._PHYSICAL_KEY_STATE_LOCK:
            raw_input_windows._PHYSICAL_KEY_TRACKER_OWNERS = 0
            raw_input_windows._PHYSICAL_KEY_TRACKER_ACTIVE = False
            raw_input_windows._PHYSICAL_KEY_TRACKER_HEALTHY = False

    def tearDown(self):
        with raw_input_windows._PHYSICAL_KEY_STATE_LOCK:
            raw_input_windows._PHYSICAL_KEY_TRACKER_OWNERS = 0
            raw_input_windows._PHYSICAL_KEY_TRACKER_ACTIVE = False
            raw_input_windows._PHYSICAL_KEY_TRACKER_HEALTHY = False
            raw_input_windows._PHYSICAL_KEY_TRACKER_EPOCH += 1

    def test_one_listener_stopping_keeps_the_other_alive(self):
        raw_input_windows._set_physical_keyboard_tracker_active(True)   # 设备按键监听
        raw_input_windows._set_physical_keyboard_tracker_active(True)   # 只跟踪键盘的监听
        self.assertTrue(raw_input_windows.physical_keyboard_tracking_available())

        lost = raw_input_windows._set_physical_keyboard_tracker_active(False)
        self.assertFalse(lost)
        self.assertTrue(raw_input_windows.physical_keyboard_tracking_available())

        lost = raw_input_windows._set_physical_keyboard_tracker_active(False)
        self.assertTrue(lost)
        self.assertFalse(raw_input_windows.physical_keyboard_tracking_available())

    def test_extra_release_does_not_go_negative(self):
        raw_input_windows._set_physical_keyboard_tracker_active(False)
        raw_input_windows._set_physical_keyboard_tracker_active(True)
        self.assertTrue(raw_input_windows.physical_keyboard_tracking_available())
        self.assertEqual(raw_input_windows._PHYSICAL_KEY_TRACKER_OWNERS, 1)


class _FakeListener:
    started = []

    def __init__(self, callback):
        self.callback = callback
        self.is_running = False
        self.stopped = False

    def start(self, device_path=None):
        self.device_path = device_path
        self.is_running = True
        _FakeListener.started.append(self)

    def stop(self):
        self.is_running = False
        self.stopped = True


class _FakeHost:
    def __init__(self):
        self._raw_input_lifecycle_lock = threading.RLock()
        self._raw_input_stopping = False
        self._raw_tracking_listener = None
        self._logger = logging.getLogger("motedeck.test")


class TrackingListenerStartTests(unittest.TestCase):
    def setUp(self):
        _FakeListener.started = []

    def test_starts_keyboard_only_listener_once(self):
        host = _FakeHost()
        with mock.patch.object(raw_input_windows, "RawInputButtonListener", _FakeListener):
            app_input_channels.InputChannelsMixin._ensure_physical_keyboard_tracking_listener(host)
            app_input_channels.InputChannelsMixin._ensure_physical_keyboard_tracking_listener(host)
        self.assertEqual(len(_FakeListener.started), 1)
        listener = _FakeListener.started[0]
        self.assertIsNone(listener.device_path)  # 键盘模式：只跟踪，不选设备
        self.assertIs(host._raw_tracking_listener, listener)

    def test_replaces_a_dead_listener(self):
        host = _FakeHost()
        dead = _FakeListener(None)
        dead.is_running = False
        host._raw_tracking_listener = dead
        with mock.patch.object(raw_input_windows, "RawInputButtonListener", _FakeListener):
            app_input_channels.InputChannelsMixin._ensure_physical_keyboard_tracking_listener(host)
        self.assertEqual(len(_FakeListener.started), 1)
        self.assertIs(host._raw_tracking_listener, _FakeListener.started[0])


if __name__ == "__main__":
    unittest.main()
