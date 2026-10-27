"""Best-effort detection of a third-party input-method panel popping up.

搜狗、豆包、Chatterfly 这些输入法没有可以订阅的会话回调，只能看窗口。实测发现
它们的面板是**预先创建好、常驻隐藏**的顶层窗口（Chatterfly 的 ``VoiceBarWnd`` /
``OimeDirectUIWindow`` / ``ImeGuard`` 都是这样，hwnd 一直不变，样式也一直是
``WS_EX_LAYERED|NOACTIVATE|TOOLWINDOW|TOPMOST``），"弹出面板"只是把已有窗口从
不可见翻成可见，并不新建 hwnd。所以判据只能是"可见集合里冒出了之前不可见的
窗口"，而不是"出现了新的 hwnd"。

时序上还有一层错位：面板在物理键**按下**的瞬间就翻成可见，松手往往又翻回隐藏，
而气泡是松手之后才显示的。只在气泡显示的那一刻取基线，面板要么已被算进基线、
要么已经重新隐藏，两种情况都发现不了。因此：

* ``arm()`` 在注入快捷键**之前**取基线，并立刻开始后台累积"按下之后新翻成可见"
  的窗口；
* ``watch()`` 在气泡显示后接手：按下期间已经累积到面板就直接收掉，没累积到就
  继续盯着，气泡显示后才翻出来的面板同样会被立刻发现。

认不出来时什么都不做，气泡仍按自己的定时器消失，所以这里只影响观感，不影响
语音本身。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional, Set

_WATCH_DURATION_MS = 2500
_WATCH_INTERVAL_MS = 40
# 按下时记的基线只在这一次按压里有效；放久了会把之后正常出现的窗口误判成面板。
_BASELINE_MAX_AGE_SECONDS = 3.0
_ENUM_CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _bind_user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.EnumWindows.argtypes = (_ENUM_CALLBACK, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    return user32


def _own_pid() -> int:
    return int(ctypes.windll.kernel32.GetCurrentProcessId())  # type: ignore[attr-defined]


def _visible_windows(user32, own_pid: int) -> Set[int]:
    """Hwnds of visible top-level windows that belong to other processes."""

    windows: Set[int] = set()

    def visit(hwnd, _param):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
                return True
            if int(pid.value) != own_pid:
                windows.add(int(hwnd))
        except BaseException:  # noqa: BLE001 - enumeration is best effort
            return True
        return True

    user32.EnumWindows(_ENUM_CALLBACK(visit), 0)
    return windows


class InputMethodWatch:
    """Watch briefly for a foreign window becoming visible and report it once."""

    def __init__(
        self,
        *,
        logger=None,
        duration_ms: int = _WATCH_DURATION_MS,
        interval_ms: int = _WATCH_INTERVAL_MS,
    ) -> None:
        self._logger = logger
        self._duration_ms = max(200, int(duration_ms))
        self._interval_ms = max(40, int(interval_ms))
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._baseline: Optional[Set[int]] = None
        self._baseline_at = 0.0
        self._seen: Set[int] = set()
        self._on_popup: Optional[Callable[[], None]] = None
        self._watch_deadline = 0.0
        self._fired = False

    def arm(self) -> None:
        """记下"按下之前"的可见窗口并开始累积按下之后新翻出来的窗口。

        必须在注入快捷键之前调用：输入法常常在按下这一瞬间就把面板翻成可见，
        而气泡要到松手之后才显示。已有观察在跑时不重新取基线，免得把已经弹出
        的面板又算成"本来就有"。
        """

        if sys.platform != "win32":
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
        baseline = self._snapshot()
        if baseline is None:
            return
        token = threading.Event()
        thread = threading.Thread(
            target=self._run,
            args=(token,),
            name="remote-mic-input-method-watch",
            daemon=True,
        )
        with self._lock:
            self._baseline = baseline
            self._baseline_at = time.monotonic()
            self._seen = set()
            self._on_popup = None
            self._watch_deadline = 0.0
            self._fired = False
            self._cancel = token
            self._thread = thread
        try:
            thread.start()
        except BaseException as exc:  # noqa: BLE001 - never break voice input
            self._warn("input method watch failed to start", exc)

    def watch(self, on_popup: Callable[[], None]) -> None:
        """Start watching; ``on_popup`` runs at most once on a new visible window."""

        if sys.platform != "win32":
            return
        self.arm()
        with self._lock:
            self._on_popup = on_popup
            self._watch_deadline = time.monotonic() + self._duration_ms / 1000.0
            self._fired = False
            already_seen = bool(self._seen)
        if already_seen:
            # 气泡显示之前（按下那一刻）面板就已经翻出来了，直接收。
            with self._lock:
                if self._fired:
                    return
                self._fired = True
            on_popup()

    def cancel(self) -> None:
        """Stop the current watch; safe from the watch thread itself."""

        with self._lock:
            token = self._cancel
            thread = self._thread
            self._thread = None
            self._on_popup = None
            self._watch_deadline = 0.0
            self._baseline = None
            self._seen = set()
        token.set()
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=0.5)

    def stop(self) -> None:
        self.cancel()

    def _snapshot(self) -> Optional[Set[int]]:
        try:
            return _visible_windows(_bind_user32(), _own_pid())
        except BaseException as exc:  # noqa: BLE001 - the watch is best effort
            self._warn("input method watch snapshot failed", exc)
            return None

    def _warn(self, message: str, exc: Optional[BaseException] = None) -> None:
        if self._logger is None:
            return
        try:
            if exc is None:
                self._logger.warning("%s", message)
            else:
                self._logger.warning(
                    "%s: error_type=%s", message, type(exc).__name__
                )
        except Exception:  # noqa: BLE001 - logging must not raise
            pass

    def _run(self, token: threading.Event) -> None:
        try:
            user32 = _bind_user32()
            own_pid = _own_pid()
            interval = self._interval_ms / 1000.0
            while not token.is_set():
                with self._lock:
                    baseline = self._baseline
                    watch_deadline = self._watch_deadline
                    baseline_at = self._baseline_at
                if baseline is None:
                    return
                fresh = _visible_windows(user32, own_pid) - baseline
                callback: Optional[Callable[[], None]] = None
                if fresh:
                    with self._lock:
                        self._seen |= fresh
                        if self._on_popup is not None and not self._fired:
                            self._fired = True
                            callback = self._on_popup
                if callback is not None:
                    callback()
                    return
                now = time.monotonic()
                if (
                    watch_deadline <= now
                    and now - baseline_at >= _BASELINE_MAX_AGE_SECONDS
                ):
                    return
                if token.wait(interval):
                    return
        except BaseException as exc:  # noqa: BLE001 - the watch is best effort
            self._warn("input method watch failed", exc)
