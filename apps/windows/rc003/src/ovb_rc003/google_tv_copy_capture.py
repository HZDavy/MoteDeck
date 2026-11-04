"""Read-only Google 9451 volume capture, bound to a verified UMDF device.

Reuse the existing callsite/interface/ContainerID resolver verbatim. A shared
WUDFHost and a matching packet length are never sufficient source evidence.
This transport still needs live Google 9451 hardware acceptance.
"""
from __future__ import annotations

import json

from . import remote_selection
from .frida_hid_tap_runtime import GADGET_SCRIPT


def _functions(start: str, end: str) -> str:
    """Fail construction if the upstream helper boundaries change."""
    first, last = f"function {start}(", f"function {end}("
    if GADGET_SCRIPT.count(first) != 1 or GADGET_SCRIPT.count(last) != 1:
        raise ValueError("copy_source_helpers_changed")
    begin, finish = GADGET_SCRIPT.index(first), GADGET_SCRIPT.index(last)
    if finish <= begin:
        raise ValueError("copy_source_helpers_changed")
    return GADGET_SCRIPT[begin:finish]


_STATE = r"""
const selectedSourceKey = __ENTITY__;
let sourceApi = null;
const sourceEvidenceKeys = new Set();
const sourceDiagnosticsEnabled = false;
const SOURCE_BINDING_REVISION = 1;
let sourceEntryStatus = "not_checked";
const sourceHandles = new Map();
const sourceCallsites = new Map();
const sourceVtables = new Map();
// Helper evidence emission is disabled: only anonymous, fixed counters leave.
function emit(_payload) {}
"""

_CAPTURE = r"""
const copyCounts = {
  ioctl: 0, volume_shape: 0,
  candidate: 0, source_rejected: 0, selected: 0, failed: 0,
  invalid_report: 0, report: 0, error: 0
};
let sourceReason = "none";
function count(name) { copyCounts[name] = Math.min(2147483647, copyCounts[name] + 1); }
initializeSourceApi();
if (sourceApi === null) throw new Error("hid_source_unconfirmed");
const sourceHost = Process.findModuleByName("WUDFHost.exe");
const target = sourceHost === null ? null : deviceIoControlEntry(sourceHost);
if (target === null) throw new Error("hid_copy_entry_unverified");
Interceptor.attach(target, {
  onEnter(args) {
    this.volumeCopy = null;
    if (args[1].toUInt32() !== 0x80018483) return;
    count("ioctl");
    if (args[5].toUInt32() !== 7) return;
    count("volume_shape");
    if (args[3].toUInt32() !== 8) return;
    count("candidate");
    try {
      // Resolve the live device every time, including after handle reuse.
      sourceHandles.clear();
      const diagnostic = {};
      const key = resolveCopyDevice(this.context, this.returnAddress, args[0], diagnostic);
      if (key !== selectedSourceKey) {
        sourceReason = key === null ? (diagnostic.reason || "unknown") : "other_device";
        count("source_rejected"); return;
      }
      count("selected");
      if (args[4].isNull()) { count("invalid_report"); return; }
      this.volumeCopy = {buffer: args[4]};
    } catch (_) { count("error"); }
  },
  onLeave(result) {
    const frame = this.volumeCopy;
    this.volumeCopy = null;
    if (frame === null) return;
    if (result.toInt32() === 0) { count("failed"); return; }
    try {
      // This internal UMDF COPY forwards a prepopulated buffer. Its
      // lpBytesReturned is not the HID report length; it may be zero.
      // Use the declared seven-byte length checked on entry, after success.
      // No output bytes are read before the selected-device identity gate.
      const bytes = new Uint8Array(frame.buffer.readByteArray(7));
      if (bytes[0] !== 1 || !bytes.slice(1).every(x => x === 0 || x === 1 || x === 2)) {
        count("invalid_report"); return;
      }
      count("report");
      send({kind:"report", value:Array.from(bytes).map(x => x.toString(16).padStart(2,"0")).join("")});
    } catch (_) { count("error"); }
  }
});
send({kind:"hook_ready"});
setInterval(function() { send({kind:"copy_counts", counts:{...copyCounts}, source_reason:sourceReason}); }, 5000);
"""


def build_script(entity: str) -> str:
    if not remote_selection.valid_key(entity):
        raise ValueError("hid_source_unconfirmed")
    return "\n".join((
        _STATE.replace("__ENTITY__", json.dumps(entity)),
        _functions("insideModule", "copySource"),
        _functions("asciiBytes", "textFromBytes"),
        _functions("deviceIoControlImportSlot", "installHook"),
        _CAPTURE,
    ))
