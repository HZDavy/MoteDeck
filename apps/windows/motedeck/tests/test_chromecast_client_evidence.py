import unittest

from motedeck import chromecast_client


def make_client(trace_enabled):
    return chromecast_client.Client(
        "a" * 64,
        mode="detect",
        on_edge=lambda edge: None,
        trace_enabled=trace_enabled,
    )


def evidence_event():
    return {"seq": 7, "record": {"kind": "sample", "value": 1}}


class EvidenceTraceGateTests(unittest.TestCase):
    def test_disabled_trace_does_not_log_evidence(self):
        client = make_client(False)
        with self.assertNoLogs("motedeck", level="INFO"):
            client._observe_evidence(evidence_event())

    def test_enabled_trace_logs_evidence(self):
        client = make_client(True)
        with self.assertLogs("motedeck", level="INFO") as captured:
            client._observe_evidence(evidence_event())
        self.assertIn("Chromecast evidence", captured.output[0])

    def test_callable_trace_flag_is_read_live(self):
        state = {"enabled": False}
        client = make_client(lambda: state["enabled"])
        with self.assertNoLogs("motedeck", level="INFO"):
            client._observe_evidence(evidence_event())
        state["enabled"] = True
        with self.assertLogs("motedeck", level="INFO"):
            client._observe_evidence(evidence_event())


if __name__ == "__main__":
    unittest.main()
