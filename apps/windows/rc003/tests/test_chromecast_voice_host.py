"""谷歌遥控器话筒键绑成普通按键后的按下/松开行为。

话筒键没有按键边沿，只有语音流事件：绑成普通动作时，按下/松开要交给
按键映射路径执行，语音快捷键、录音和短按切模式全部不发生。
"""
import logging
import threading
import unittest
from types import SimpleNamespace

from motedeck.chromecast_voice_host import VoiceHost, VoiceHostServices


class _FakeController:
    def __init__(self):
        self.press_calls = 0
        self.active = False

    def on_mic_button_pressed(self):
        self.press_calls += 1
        self.active = True
        return "press"

    def reset(self):
        self.active = False
        return None

    def cancel_pending(self):
        pass

    def restore_pending(self, action):
        pass


class _FakeShortcut:
    def __init__(self, tokens=("ctrl", "alt", "v")):
        self.lock = threading.Lock()
        self.controller = _FakeController()
        self._tokens = tokens
        self.pending_tokens = None
        self.applied = []
        self.release_calls = 0

    def recorded_tokens(self):
        return self._tokens

    def release_pending(self):
        self.release_calls += 1
        return True

    def apply(self, action, inject_mode=None, require_tracked_hold=True):
        self.applied.append(action)
        return True


class _FakeAudio:
    def __init__(self):
        self.open_calls = 0
        self.flushes = []
        self.stats = SimpleNamespace(frames=0, reset=lambda: None)

    def open(self):
        self.open_calls += 1
        return True

    def flush(self, reason):
        self.flushes.append(reason)
        return SimpleNamespace(error=None, completed=True)

    @property
    def writer(self):
        return None

    @property
    def sink(self):
        return None

    def stop_writer(self):
        return True

    def close_sink(self):
        pass


class _FakeClient:
    def __init__(self):
        self.calls = []
        self.cancel = threading.Event()

    def voice_host(self, attempt, result):
        self.calls.append((attempt, result))


def _host_start_event(attempt):
    return {"event": "host_start", "attempt": attempt, "data": None}


def _host_stop_event(attempt):
    return {"event": "host_stop", "attempt": attempt, "data": "host_stop"}


class MicPassthroughTests(unittest.TestCase):
    def setUp(self):
        self.audio = _FakeAudio()
        self.shortcut = _FakeShortcut()
        self.client = _FakeClient()
        self.button_events = []
        self.short_taps = []
        self.blocked_reasons = []
        self.mic_passthrough = True

    def _services(self, mic_passthrough=None):
        if mic_passthrough is not None:
            self.mic_passthrough = mic_passthrough
        return VoiceHostServices(
            audio=self.audio,
            shortcut=self.shortcut,
            settings=lambda: {"gain_db": 10.0},
            logger=logging.getLogger("test"),
            reload_settings=lambda: None,
            apply_pending_settings=lambda: None,
            begin_diagnostic=lambda **kwargs: None,
            finish_diagnostic=lambda *args, **kwargs: None,
            set_active=lambda state: None,
            set_result=lambda result: None,
            disable_forwarding=lambda: None,
            mic_passthrough=lambda: self.mic_passthrough,
            mic_button_event=lambda button, pressed: self.button_events.append(
                (button, pressed)
            ),
            set_mic_blocked=lambda reason: self.blocked_reasons.append(reason),
            on_short_tap=lambda: self.short_taps.append(True),
            press_delay_seconds=lambda: 0.0,
            inject_mode=lambda: None,
            needs_shortcut=lambda: True,
        )

    def _host(self, mic_passthrough=None):
        host = VoiceHost(self._services(mic_passthrough))
        host.client = self.client
        return host

    def test_ordinary_mapping_presses_the_mapped_action_without_voice(self):
        host = self._host()
        host._handle(_host_start_event(7))
        self.assertEqual(self.button_events, [("mic", True)])
        self.assertEqual(self.client.calls, [(7, "ready")])
        self.assertEqual(self.shortcut.controller.press_calls, 0)
        self.assertEqual(self.audio.open_calls, 0)

    def test_ordinary_mapping_release_lifts_the_mapped_action(self):
        host = self._host()
        host._handle(_host_start_event(7))
        host._handle(_host_stop_event(7))
        self.assertEqual(self.button_events, [("mic", True), ("mic", False)])
        self.assertEqual(self.client.calls, [(7, "ready"), (7, "released")])
        self.assertEqual(self.shortcut.release_calls, 0)

    def test_ordinary_mapping_survives_a_full_press_release_cycle(self):
        host = self._host()
        host._handle(_host_start_event(1))
        host._handle(_host_stop_event(1))
        host._handle({"event": "state", "attempt": 1, "data": "idle"})
        host._handle(_host_start_event(2))
        host._handle(_host_stop_event(2))
        self.assertEqual(
            self.button_events,
            [("mic", True), ("mic", False), ("mic", True), ("mic", False)],
        )

    def test_ordinary_mapping_short_tap_does_not_switch_voice_mode(self):
        host = self._host()
        host._handle({"event": "tap", "attempt": 0, "data": "tap"})
        self.assertEqual(self.short_taps, [])

    def test_voice_mapping_short_tap_still_switches_voice_mode(self):
        host = self._host(mic_passthrough=False)
        host._handle({"event": "tap", "attempt": 0, "data": "tap"})
        self.assertEqual(self.short_taps, [True])

    def test_voice_mapping_press_still_injects_the_recorded_shortcut(self):
        host = self._host(mic_passthrough=False)
        host._handle(_host_start_event(7))
        self.assertEqual(self.button_events, [])
        self.assertEqual(self.shortcut.controller.press_calls, 1)
        self.assertEqual(self.audio.open_calls, 1)
        self.assertEqual(self.client.calls, [(7, "ready")])

    def test_ordinary_mapping_press_publishes_the_mic_blocked_warning(self):
        # 谷歌遥控器没有按键边沿的 AudioStarted，走不到小米链路的仲裁点，
        # 警告要在 _start_host 的 passthrough 分支发布，语音页胶囊才弹得出来。
        host = self._host()
        host._handle(_host_start_event(7))
        self.assertEqual(self.blocked_reasons, ["mic_ordinary_mapping"])

    def test_voice_mapping_press_clears_a_stale_mic_blocked_warning(self):
        host = self._host()
        host._handle(_host_start_event(1))
        host._handle(_host_stop_event(1))
        host._handle({"event": "state", "attempt": 1, "data": "idle"})
        self.assertEqual(self.blocked_reasons, ["mic_ordinary_mapping"])
        # 话筒键改回语音动作：这次按下走正常语音会话，过期警告要清掉。
        self.mic_passthrough = False
        host._handle(_host_start_event(2))
        self.assertEqual(self.blocked_reasons[-1], "")


if __name__ == "__main__":
    unittest.main()
