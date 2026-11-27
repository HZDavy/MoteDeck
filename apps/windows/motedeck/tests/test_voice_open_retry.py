"""VoiceReceiver.open 的退避重试：冷启动 GATT 服务发现超时后要自己扛过去。

只覆盖重试决策与证据/事件顺序，不涉及 WinRT；设备对象用可编排的假实现，
``open_voice`` 的调用次数、传入超时和 ``reset_voice`` 清理都有断言。
"""

import asyncio
import unittest
from unittest import mock

from motedeck import chromecast_observation, chromecast_voice_receiver
from motedeck.chromecast_pipe_windows import PipeError
from motedeck.chromecast_voice_receiver import VoiceReceiver


_SENTINEL_EVIDENCE = dict(
    step="service", status=-1, tx=-1, audio=-1, control=-1,
    tx_handle=-1, audio_handle=-1, control_handle=-1,
    service_count=-1, characteristic_count=-1, mtu=-1,
)
_READY_EVIDENCE = dict(
    step="ready", status=0, tx=4, audio=18, control=18,
    tx_handle=57, audio_handle=59, control_handle=62,
    service_count=1, characteristic_count=3, mtu=247,
)


class _FakeDevice:
    def __init__(self, failures=0, error=None, fail_evidence=None):
        self.remaining_failures = failures
        self.error = error or TimeoutError()
        self.fail_evidence = fail_evidence or _SENTINEL_EVIDENCE
        self.calls = []
        self.resets = 0
        self.voice_evidence = dict(_SENTINEL_EVIDENCE)
        self.voice_attributes = {}

    async def open_voice(self, *, direct=False, discovery_timeout=5):
        self.calls.append((direct, discovery_timeout))
        if self.remaining_failures:
            self.remaining_failures -= 1
            self.voice_evidence = dict(self.fail_evidence)
            raise self.error
        self.voice_evidence = dict(_READY_EVIDENCE)
        self.voice_attributes = {0x3C: "audio", 0x3F: "control"}

    async def reset_voice(self):
        self.resets += 1


class _Recorder:
    def __init__(self):
        self.events = []
        self.rows = []

    def send(self, kind, attempt, data, stamp):
        self.events.append((kind, attempt, data, stamp))

    def row(self, row):
        self.rows.append(row)

    def stages(self):
        return [row["stage"] for row in self.rows if row["kind"] == "stage"]

    def gatt_rows(self):
        return [row for row in self.rows if row["kind"] == "gatt"]


class VoiceOpenRetryTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            chromecast_voice_receiver, "VOICE_OPEN_RETRY_DELAYS", (0, 0, 0, 0, 0)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        config_patcher = mock.patch.object(
            chromecast_voice_receiver.config, "load_config", return_value={}
        )
        config_patcher.start()
        self.addCleanup(config_patcher.stop)

    def _receiver(self, device, recorder):
        return VoiceReceiver(
            device, recorder.send, detect_only=True,
            observation=chromecast_observation.Observation(recorder.row),
        )

    def test_retries_with_wider_timeout_until_success(self):
        device, recorder = _FakeDevice(failures=2), _Recorder()
        receiver = self._receiver(device, recorder)

        asyncio.run(receiver.open())

        self.assertTrue(receiver.available)
        self.assertEqual(device.calls, [(False, 5), (False, 15), (False, 15)])
        self.assertEqual(device.resets, 2)
        self.assertEqual(recorder.stages().count("voice_open_begin"), 3)
        self.assertEqual(recorder.stages()[-1], "voice_ready")
        self.assertNotIn("voice_unavailable", recorder.stages())
        self.assertEqual(len(recorder.gatt_rows()), 3)
        self.assertIn(("available", 0, "ready"), [event[:3] for event in recorder.events])

    def test_gives_up_after_last_attempt_and_reports_unavailable(self):
        device, recorder = _FakeDevice(failures=99), _Recorder()
        receiver = self._receiver(device, recorder)

        asyncio.run(receiver.open())

        self.assertFalse(receiver.available)
        self.assertEqual(len(device.calls), 6)
        self.assertEqual([timeout for _, timeout in device.calls], [5, 15, 15, 15, 15, 15])
        self.assertEqual(device.resets, 5)
        self.assertEqual(recorder.stages()[-1], "voice_unavailable")
        self.assertIn(("available", 0, "unavailable"), [event[:3] for event in recorder.events])

    def test_deterministic_layout_mismatch_is_not_retried(self):
        device = _FakeDevice(
            failures=1, error=PipeError("unsupported_layout"),
            fail_evidence=dict(_SENTINEL_EVIDENCE, status=0),
        )
        recorder = _Recorder()
        receiver = self._receiver(device, recorder)

        asyncio.run(receiver.open())

        self.assertFalse(receiver.available)
        self.assertEqual(len(device.calls), 1)
        self.assertEqual(device.resets, 0)
        self.assertEqual(recorder.stages(), ["voice_open_begin", "voice_unavailable"])


if __name__ == "__main__":
    unittest.main()
