"""只量一个量：物理按下 左 Ctrl + 左 Win 之后，Chatterfly 多久才真正打开采集会话。

Usage:
    .venv\\Scripts\\python.exe scripts\\voice_capture_latency_probe.py

Developer probe. It watches the real key edge with a low-level keyboard hook,
then reuses the product's proven Core Audio enumeration to poll until a
Chatterfly capture session turns active. It opens no audio stream, injects no
input, and changes no device.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QListWidget,
    QVBoxLayout,
    QWidget,
)

from ovb_rc003 import voice_playback_session_windows as audio
from ovb_rc003 import win32_keys

SHORTCUT = ("lctrl", "lwin")
_VKS = frozenset(win32_keys.VK_CODES[token] for token in SHORTCUT)
TARGET_MARKER = "chatterfly"  # Matches the capture session image path.
TIMEOUT_SECONDS = 5.0
_ACTIVE = 1  # AudioSessionState.Active

_WH_KEYBOARD_LL = 13
_WM_QUIT = 0x0012
_KEY_DOWN = (0x0100, 0x0104)  # WM_KEYDOWN, WM_SYSKEYDOWN
_KEY_UP = (0x0101, 0x0105)  # WM_KEYUP, WM_SYSKEYUP

LOG_PATH = Path(tempfile.gettempdir()) / "voice_capture_latency_probe.log"


def _log(line: str) -> None:
    """Append one timestamped line so the run can be read back later."""
    text = f"{time.strftime('%H:%M:%S')}  {line}"
    print(text, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except OSError:
        pass


@dataclass(frozen=True)
class SessionView:
    endpoint_id: str
    endpoint_name: str
    instance_id: str
    pid: int
    state: int

    @property
    def identity(self) -> tuple[str, str]:
        return self.endpoint_id, self.instance_id


@dataclass(frozen=True)
class Measurement:
    seconds: float | None
    session: SessionView | None


class _EndpointProbe(audio._Session):
    """One COM apartment, one endpoint list, many cheap snapshots.

    ``_Session`` gives the apartment, pointer ownership and string helpers; the
    endpoint devices and their session managers stay cached for the whole
    measurement so a poll only re-enumerates sessions.
    """

    def __init__(self) -> None:
        super().__init__()
        self._endpoints: list[tuple[object, str, str, object]] = []

    def _find(self) -> None:
        enumerator = self._pointer()
        clsid = audio._guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
        iid = audio._guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
        audio._check(int(self._ole.CoCreateInstance(
            ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(enumerator)
        )))
        devices = self._pointer()
        audio._call(enumerator, 3, (audio._I, audio._U, audio._P),
                    1, 1, ctypes.byref(devices))  # Active capture endpoints.
        count = audio._U()
        audio._call(devices, 3, (audio._P,), ctypes.byref(count))
        if count.value > 256:
            raise OSError("capture endpoint enumeration exceeds safety limit")
        for index in range(count.value):
            device = self._pointer()
            audio._call(devices, 4, (audio._U, audio._P), index, ctypes.byref(device))
            endpoint_id = self._string(device, 5)
            endpoint_name = self._device_name(device)
            manager = self._pointer()
            iid = audio._guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
            audio._call(device, 3, (audio._P, audio._U, audio._P, audio._P),
                        ctypes.byref(iid), 23, None, ctypes.byref(manager))
            self._endpoints.append((device, endpoint_id, endpoint_name, manager))

    def snapshot(self) -> tuple[SessionView, ...]:
        views: list[SessionView] = []
        for _device, endpoint_id, endpoint_name, manager in self._endpoints:
            sessions = audio._P()
            audio._call(manager, 5, (audio._P,), ctypes.byref(sessions))
            try:
                size = audio._I()
                audio._call(sessions, 3, (audio._P,), ctypes.byref(size))
                if not 0 <= size.value <= 512:
                    raise OSError("capture session enumeration exceeds safety limit")
                for session_index in range(size.value):
                    control, control2 = audio._P(), audio._P()
                    audio._call(sessions, 4, (audio._I, audio._P),
                                session_index, ctypes.byref(control))
                    try:
                        iid = audio._guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
                        audio._call(control, 0, (audio._P, audio._P),
                                    ctypes.byref(iid), ctypes.byref(control2))
                        pid = audio._U()
                        audio._call(control2, 14, (audio._P,), ctypes.byref(pid))
                        state = audio._I()
                        audio._call(control2, 3, (audio._P,), ctypes.byref(state))
                        instance_id = self._string(control2, 13)
                    finally:
                        audio._release(control2)
                        audio._release(control)
                    views.append(SessionView(
                        endpoint_id=endpoint_id,
                        endpoint_name=endpoint_name,
                        instance_id=instance_id,
                        pid=pid.value,
                        state=state.value,
                    ))
            finally:
                audio._release(sessions)
        return tuple(views)


def _process_name(pid: int) -> str:
    if pid <= 0:
        return ""
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = (
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32),
    )
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = ctypes.c_uint32(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value).name
        return ""
    finally:
        kernel32.CloseHandle(handle)


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)


class _KeyWatcher(QThread):
    """Report the exact key edge of 左 Ctrl + 左 Win from this process's hook."""

    pressed = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._thread_id = 0
        self._chord_down = False
        self._procedure = _HOOKPROC(self._on_key)

    def run(self) -> None:
        user32 = self._user32
        user32.SetWindowsHookExW.argtypes = (
            ctypes.c_int, _HOOKPROC, ctypes.c_void_p, wt.DWORD,
        )
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = (
            ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM,
        )
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        user32.UnhookWindowsHookEx.restype = wt.BOOL
        user32.GetMessageW.argtypes = (ctypes.POINTER(wt.MSG), ctypes.c_void_p, wt.UINT, wt.UINT)
        user32.GetMessageW.restype = wt.BOOL
        user32.PostThreadMessageW.argtypes = (wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
        user32.PostThreadMessageW.restype = wt.BOOL
        self._kernel32.GetModuleHandleW.argtypes = (ctypes.c_wchar_p,)
        self._kernel32.GetModuleHandleW.restype = ctypes.c_void_p

        hook = user32.SetWindowsHookExW(
            _WH_KEYBOARD_LL, self._procedure, self._kernel32.GetModuleHandleW(None), 0
        )
        if not hook:
            return
        self._thread_id = self._kernel32.GetCurrentThreadId()
        try:
            message = wt.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            user32.UnhookWindowsHookEx(hook)
            self._thread_id = 0

    def stop(self) -> None:
        if self._thread_id:
            self._user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)

    def _on_key(self, code: int, wparam: int, lparam: int) -> int:
        if code >= 0:
            vk = ctypes.cast(lparam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents.vkCode
            if vk in _VKS:
                if wparam in _KEY_DOWN:
                    if not self._chord_down and self._partner_down(vk):
                        self._chord_down = True
                        self.pressed.emit(time.perf_counter())
                elif wparam in _KEY_UP:
                    self._chord_down = False
        return self._user32.CallNextHookEx(None, code, wparam, lparam)

    def _partner_down(self, vk: int) -> bool:
        for other in _VKS:
            if other != vk and self._user32.GetAsyncKeyState(other) & 0x8000:
                return True
        return False


class _Poller(QThread):
    """Keep session states warm, then time the first activation after a press.

    The thread samples continuously so the state right before the press is
    already known; a press only asks for the first target activation that
    happens after its timestamp.
    """

    measured = Signal(object)
    failed = Signal(str)

    _IDLE_INTERVAL_SECONDS = 0.03

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._pending: float | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._states: dict[tuple[str, str], int] = {}

    def submit(self, started_at: float) -> None:
        with self._lock:
            self._pending = started_at
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        try:
            with _EndpointProbe() as probe:
                activation_at: float | None = None
                activation: SessionView | None = None
                while not self._stop.is_set():
                    views = probe.snapshot()
                    now = time.perf_counter()
                    for view in views:
                        previous = self._states.get(view.identity)
                        self._states[view.identity] = view.state
                        if (
                            view.state == _ACTIVE
                            and previous != _ACTIVE
                            and TARGET_MARKER in view.instance_id.lower()
                        ):
                            activation_at, activation = now, view
                    with self._lock:
                        started_at = self._pending
                    if started_at is None:
                        self._wake.wait(self._IDLE_INTERVAL_SECONDS)
                        self._wake.clear()
                        continue
                    if activation_at is not None and activation_at >= started_at:
                        with self._lock:
                            self._pending = None
                        self.measured.emit(
                            Measurement(activation_at - started_at, activation)
                        )
                        activation_at, activation = None, None
                    elif now >= started_at + TIMEOUT_SECONDS:
                        with self._lock:
                            self._pending = None
                        self.measured.emit(Measurement(None, None))
        except BaseException as exc:  # noqa: BLE001 - surfaced in the window
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ProbeWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("采集启动耗时")
        self.setMinimumWidth(480)

        hint = QLabel("按一下 左 Ctrl + 左 Win（在任意输入框里），这里会显示 Chatterfly 开始采集的耗时。")
        hint.setStyleSheet("color: #666;")
        hint.setWordWrap(True)

        self.value = QLabel("—")
        self.value.setStyleSheet("font-size: 44px; font-weight: 600;")

        self.status = QLabel("监听中：等待 左 Ctrl + 左 Win")
        self.status.setStyleSheet("color: #444;")
        self.status.setWordWrap(True)

        self.history = QListWidget()
        self.history.setMaximumHeight(160)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        layout.addWidget(hint)
        layout.addWidget(self.value)
        layout.addWidget(self.status)
        layout.addWidget(self.history)

        self._watcher = _KeyWatcher(self)
        self._poller = _Poller(self)
        self._watcher.pressed.connect(self._on_pressed)
        self._poller.measured.connect(self._show_result)
        self._poller.failed.connect(self._show_error)
        self._poller.start()
        self._watcher.start()
        self._last_press = 0.0
        _log(f"listening  log={LOG_PATH}")

    def _on_pressed(self, started_at: float) -> None:
        gap = started_at - self._last_press if self._last_press else 0.0
        self._last_press = started_at
        _log(f"press  gap={gap:.1f}s")
        self.status.setText("已按下，等待 Chatterfly 打开采集会话…")
        self._poller.submit(started_at)

    def _show_result(self, measurement: Measurement) -> None:
        if measurement.seconds is None:
            _log("result timeout")
            self.value.setText("超时")
            self.status.setText(
                f"按下后 {TIMEOUT_SECONDS:.0f} 秒内 Chatterfly 没有打开采集会话。"
            )
            return
        session = measurement.session
        assert session is not None
        millis = measurement.seconds * 1000.0
        self.value.setText(f"{millis:.0f} ms")
        process = _process_name(session.pid) or "未知进程"
        detail = f"{session.endpoint_name or '未命名端点'} · {process} (PID {session.pid})"
        _log(f"result {millis:.1f} ms  {detail}")
        self.status.setText(detail)
        self.history.insertItem(0, f"{millis:.1f} ms   {detail}")
        self.status.setText("监听中：等待 左 Ctrl + 左 Win")

    def _show_error(self, message: str) -> None:
        self.value.setText("失败")
        self.status.setText(message)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._watcher.stop()
        self._poller.stop()
        self._watcher.wait(1000)
        self._poller.wait(int((TIMEOUT_SECONDS + 1.0) * 1000))
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    window = ProbeWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
