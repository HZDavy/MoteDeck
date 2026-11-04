"""No Frida attach or keyboard injection: execute capture policy with fake APIs."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ovb_rc003 import google_tv_copy_capture as capture
from ovb_rc003 import chromecast_hid_tap_windows as tap_module
from ovb_rc003.chromecast_buttons import HidButtonReceiver


ENTITY = "a" * 64
NODE = shutil.which("node")
_HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const messages = [], hooks = [], timers = [];
let reads = 0, resolutions = 0;
const context = vm.createContext({
  Process: {findModuleByName: () => ({name:'WUDFHost.exe'})},
  Interceptor: {attach: (target, callbacks) => hooks.push(callbacks)},
  send: item => messages.push(item), setInterval: cb => timers.push(cb)
});
// Parse the entire generated production script, then stub only native helpers.
new vm.Script(input.script);
vm.runInContext(input.helpers, context);
context.recordResolution = () => {resolutions++;};
vm.runInContext(`
  initializeSourceApi = function() { sourceApi = {}; };
  deviceIoControlEntry = function() { return {}; };
  resolveCopyDevice = function(context, returnAddress, handle) {
    recordResolution(); return handle.identity;
  };
`, context);
vm.runInContext(input.capture, context);
const hooksCount = hooks.length;
function integer(value) { return {toUInt32:()=>value >>> 0, toInt32:()=>value|0}; }
for (const call of input.calls) {
  const frame = {context:{}, returnAddress:{}};
  const buffer = {isNull:()=>false, readByteArray:length=>{
    reads++; return Uint8Array.from(call.bytes).buffer;
  }};
  const returned = {isNull:()=>false, readU32:()=>call.returned === undefined ? 7 : call.returned};
  const args = [{identity:call.identity}, integer(call.ioctl === undefined ? 0x80018483 : call.ioctl),
    {}, integer(call.inputLength === undefined ? 8 : call.inputLength), buffer,
    integer(call.outputLength === undefined ? 7 : call.outputLength), returned];
  hooks[0].onEnter.call(frame, args);
  hooks[0].onLeave.call(frame, integer(call.success === false ? 0 : 1));
}
timers.forEach(cb=>cb());
console.log(JSON.stringify({messages, reads, resolutions, hooksCount}));
"""


class GoogleCopyConstructionTests(unittest.TestCase):
    def test_exact_existing_identity_helpers_and_no_nt_or_suppression_hook(self):
        source = capture.build_script(ENTITY)
        for first, last in (("insideModule", "copySource"), ("asciiBytes", "textFromBytes"),
                            ("deviceIoControlImportSlot", "installHook")):
            self.assertIn(capture._functions(first, last), source)
        self.assertEqual(source.count("Interceptor.attach("), 1)
        self.assertNotIn('getExportByName("NtDeviceIoControlFile")', source)
        self.assertNotIn('findExportByName("NtDeviceIoControlFile")', source)
        self.assertNotIn("interceptOutgoingCopy", source)
        self.assertNotIn("OnReportValueChanged", source)
        self.assertIn("sourceHandles.clear();", source)
        with self.assertRaises(ValueError):
            capture.build_script("not a selected entity")
        with mock.patch.object(capture, "GADGET_SCRIPT", ""):
            with self.assertRaises(ValueError):
                capture.build_script(ENTITY)


@unittest.skipUnless(NODE, "Node required to execute the generated capture policy")
class GoogleCopyPolicyTests(unittest.TestCase):
    def run_calls(self, calls):
        source = capture.build_script(ENTITY)
        result = subprocess.run([NODE, "-e", _HARNESS], input=json.dumps({
            "script": source, "helpers": source[:-len(capture._CAPTURE)],
            "capture": capture._CAPTURE, "calls": calls}), text=True,
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_wrong_or_unknown_source_never_reads_or_emits_payload(self):
        result = self.run_calls([
            {"identity": "b" * 64, "bytes": [1, 1, 0, 0, 0, 0, 0]},
            {"identity": None, "bytes": [1, 2, 0, 0, 0, 0, 0]},
        ])
        self.assertEqual(result["reads"], 0)
        self.assertEqual(result["resolutions"], 2)
        self.assertFalse(any(x["kind"] == "report" for x in result["messages"]))
        self.assertEqual(result["messages"][-1]["counts"]["source_rejected"], 2)
        self.assertNotIn("b" * 64, json.dumps(result["messages"]))

    def test_only_successful_selected_volume_shape_is_read(self):
        base = {"identity": ENTITY, "bytes": [1, 1, 0, 0, 0, 0, 0]}
        result = self.run_calls([
            dict(base, ioctl=0), dict(base, inputLength=9), dict(base, outputLength=3),
            dict(base, success=False), dict(base, returned=6),
            dict(base, bytes=[2, 1, 0, 0, 0, 0, 0]),
            dict(base, bytes=[1, 3, 0, 0, 0, 0, 0]), base,
        ])
        self.assertEqual(result["hooksCount"], 1)
        self.assertEqual(result["reads"], 4)
        self.assertEqual([x["value"] for x in result["messages"] if x["kind"] == "report"],
                         ["01010000000000", "01010000000000"])
        counts = result["messages"][-1]["counts"]
        self.assertEqual(counts["candidate"], 5)
        self.assertEqual(counts["invalid_report"], 2)
        self.assertEqual(counts["failed"], 1)

    def test_returned_zero_accepts_prepopulated_report_only_after_success(self):
        base = {"identity": ENTITY, "returned": 0, "bytes": [1, 2, 0, 0, 0, 0, 0]}
        result = self.run_calls([dict(base, success=False), base])
        self.assertEqual(result["reads"], 1)
        self.assertEqual([x["value"] for x in result["messages"] if x["kind"] == "report"],
                         ["01020000000000"])
        self.assertEqual(result["messages"][-1]["counts"]["failed"], 1)

    def test_volume_press_idle_reports_reach_real_receiver(self):
        reports = ([1, 1, 0, 0, 0, 0, 0], [1, 0, 0, 0, 0, 0, 0],
                   [1, 2, 0, 0, 0, 0, 0], [1, 0, 0, 0, 0, 0, 0])
        result = self.run_calls([{"identity": ENTITY, "bytes": report} for report in reports])
        receiver = HidButtonReceiver(ENTITY, "b" * 64, product_id="9451")
        receiver.confirm_source()
        edges = []
        for index, item in enumerate(x for x in result["messages"] if x["kind"] == "report"):
            edges.extend(receiver.feed(bytes.fromhex(item["value"]), 1 + index / 10))
        self.assertEqual([(x.button, x.action) for x in edges], [
            ("volume_up", "down"), ("volume_up", "up"),
            ("volume_down", "down"), ("volume_down", "up")])
        self.assertFalse(receiver.stopped)


class GoogleCopyTapWiringTests(unittest.TestCase):
    def start_tap(self, product):
        script, session = mock.Mock(), mock.Mock()
        session.create_script.return_value = script
        fake_frida = types.SimpleNamespace(attach=mock.Mock(return_value=session))
        selection = mock.patch.object(tap_module, "selected_host_and_address",
                                      return_value=(123, "aabbccddeeff", product))
        frida = mock.patch.dict("sys.modules", {"frida": fake_frida})
        rva = mock.patch.object(tap_module, "callback_rva", return_value=1234)
        for patch in (selection, frida, rva):
            self.addCleanup(patch.stop)
        resolver, _, callback = selection.start(), frida.start(), rva.start()
        tap = tap_module.HidTap(ENTITY)
        self.addCleanup(tap.close)
        tap.start()
        self.assertEqual(resolver.call_count, 2)
        return tap, script.on.call_args.args[1], session.create_script.call_args.args[0], callback

    def test_9451_uses_copy_script_and_strict_id_preserved_python_boundary(self):
        tap, message, source, callback = self.start_tap("9451")
        callback.assert_not_called()
        self.assertEqual(source, capture.build_script(ENTITY))
        for value in ("010000000000", "020500", "01030000000000", "01020000000000"):
            message({"type": "send", "payload": {"kind": "report", "value": value}}, None)
        events = tap.poll()
        self.assertEqual([(x[0], x[1]) for x in events], [("report", "01 02 00 00 00 00 00")])
        self.assertTrue(tap.source_alive())

    def test_9450_still_uses_private_eight_byte_callback(self):
        tap, message, source, callback = self.start_tap("9450")
        callback.assert_called_once_with()
        self.assertIn("addressMatches(args[0])", source)
        self.assertIn("module.base.add(1234)", source)
        self.assertNotIn("const copyCounts", source)
        message({"type": "send", "payload": {"kind": "report", "value": "0100000000000000"}}, None)
        self.assertEqual(tap.poll()[0][1], "01 00 00 00 00 00 00 00")

    def test_counts_are_bounded_anonymous_and_not_persisted(self):
        tap, message, _, _ = self.start_tap("9451")
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "ovb_rc003.config.config_path", return_value=Path(directory) / "config.json"
        ):
            message({"type": "send", "payload": {"kind": "copy_counts", "counts": {
                "candidate": 2**40, "selected": -1, "report": True, "secret": "device path"
            }}}, None)
            self.assertFalse((Path(directory) / "google-tv-hid-evidence.json").exists())
        self.assertEqual(tap.copy_counts, {"candidate": 2**31 - 1, "selected": 0})
        self.assertNotIn("device path", json.dumps(tap.copy_counts))


if __name__ == "__main__":
    unittest.main()
