"""Passive, non-suppressing keyboard listener for preset switch shortcuts.

A saved mapping preset may carry a keyboard shortcut that switches to it. This
module owns one process-wide low-level keyboard hook (``WH_KEYBOARD_LL``) that
only observes key edges; every event is forwarded unchanged, so it never
suppresses or alters user input. The hook runs on its own thread with a
``GetMessageW`` pump, exactly like the voice-key physicalizer, and is torn down
with ``PostThreadMessageW(WM_QUIT)``.

Only the low-level hook's specific modifier VKs (``VK_LSHIFT``/``VK_RSHIFT`` and
friends) are normalized to their generic counterpart, because the saved chord
vocabulary is generic (``shift``/``ctrl``/``alt``/``win``). Matching is an exact
set comparison, and injected events are ignored so Remote Mic's own voice
injection can never loop back into a preset switch.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from ctypes import wintypes
from typing import Callable, Mapping, Optional

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
LLKHF_INJECTED = 0x00000010

# The low-level hook reports the handed modifier VKs; the stored chord
# vocabulary is generic, so both sides must collapse to one code.
MODIFIER_NORMALIZATION = {
    0xA0: 0x10,  # VK_LSHIFT -> VK_SHIFT
    0xA1: 0x10,  # VK_RSHIFT -> VK_SHIFT
    0xA2: 0x11,  # VK_LCONTROL -> VK_CONTROL
    0xA3: 0x11,  # VK_RCONTROL -> VK_CONTROL
    0xA4: 0x12,  # VK_LMENU -> VK_MENU
    0xA5: 0x12,  # VK_RMENU -> VK_MENU
    0x5C: 0x5B,  # VK_RWIN -> VK_LWIN (generic Win)
}


class PresetSwitchUnavailableError(Exception):
    pass


class _KeyboardEvent(ctypes.Structure):
    _fields_ = [
        ("vk", wintypes.DWORD),
        ("scan", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("extra", ctypes.c_size_t),
    ]


def normalize_vk(vk: int) -> int:
    """Collapse a handed modifier VK onto the generic stored vocabulary."""

    return MODIFIER_NORMALIZATION.get(int(vk), int(vk))


def match_chord(
    down: frozenset[int],
    targets: Mapping[frozenset[int], str],
) -> Optional[str]:
    """Return the preset whose chord exactly equals the currently held keys."""

    if not down:
        return None
    return targets.get(frozenset(down))


def _require_windows() -> None:
    if sys.platform != "win32":
        raise PresetSwitchUnavailableError("preset switch hotkeys require Windows")


class PresetSwitchListener:
    """Observe a set of switch chords and report the matching preset id.

    ``on_switch`` is invoked on the hook thread and must be fast; callers marshal
    work onto their own thread (Remote Mic uses ``loop.call_soon_threadsafe``).
    """

    def __init__(
        self,
        *,
        on_switch: Callable[[str], None],
        logger=None,
        ignore_injected: bool = True,
    ) -> None:
        self._on_switch = on_switch
        self._logger = logger
        self._ignore_injected = bool(ignore_injected)
        self._targets: dict[frozenset[int], str] = {}
        self._down: set[int] = set()
        self._latch: Optional[frozenset[int]] = None
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._thread_id = wintypes.DWORD(0)
        self._ready_event = threading.Event()
        self._stop_event = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._hook = None
        self._hookproc_keepalive = None
        self._call_next = None

    @property
    def is_running(self) -> bool:
        return bool(
            self._thread is not None
            and self._thread.is_alive()
            and self._hook
        )

    def set_targets(self, targets: Mapping[frozenset[int], str]) -> None:
        with self._lock:
            self._targets = {
                frozenset(chord): str(identity)
                for chord, identity in targets.items()
                if chord
            }
            self._latch = None

    def start(self, *, start_timeout: float = 5.0) -> None:
        if self.is_running:
            raise PresetSwitchUnavailableError(
                "preset switch listener is already running"
            )
        if os.environ.get("RC003_DISABLE_LIVE_INPUT") == "1":
            raise PresetSwitchUnavailableError(
                "live Windows keyboard hooks are disabled for this process"
            )
        _require_windows()
        self._ready_event.clear()
        self._stop_event.clear()
        self._start_error = None
        self._down.clear()
        self._latch = None
        self._thread = threading.Thread(
            target=self._run,
            name="remote-mic-preset-switch-listener",
            daemon=True,
        )
        self._thread.start()
        if not self._ready_event.wait(timeout=start_timeout):
            self._stop_event.set()
            self._join_thread()
            raise PresetSwitchUnavailableError(
                f"preset switch listener did not become ready within {start_timeout}s"
            )
        if self._start_error is not None:
            error = self._start_error
            self._stop_event.set()
            self._join_thread()
            raise error
        if not self.is_running:
            self._stop_event.set()
            self._join_thread()
            raise PresetSwitchUnavailableError(
                "preset switch listener exited immediately after reporting ready"
            )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is None:
            return
        if sys.platform == "win32" and self._thread_id.value:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            user32.PostThreadMessageW.argtypes = (
                wintypes.DWORD,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.PostThreadMessageW.restype = wintypes.BOOL
            user32.PostThreadMessageW(self._thread_id.value, WM_QUIT, 0, 0)
        self._join_thread()

    def _join_thread(self) -> None:
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=2.0)
        if thread.is_alive():
            raise PresetSwitchUnavailableError(
                "preset switch listener thread did not stop within 2.0s"
            )
        self._thread = None
        self._thread_id = wintypes.DWORD(0)

    def _handle_edge(self, vk: int, is_down: bool) -> None:
        with self._lock:
            if is_down:
                self._down.add(vk)
            else:
                self._down.discard(vk)
            chord = frozenset(self._down)
            target = match_chord(chord, self._targets)
            if target is None:
                self._latch = None
                return
            if self._latch == chord:
                return
            self._latch = chord
        self._dispatch(target)

    def _dispatch(self, target: str) -> None:
        try:
            self._on_switch(target)
        except Exception:  # noqa: BLE001 - a consumer error must not kill the hook
            pass

    def _hookproc(self, code, message, pointer):  # noqa: ANN001 - ctypes callback
        try:
            if code >= 0 and message in (
                WM_KEYDOWN,
                WM_KEYUP,
                WM_SYSKEYDOWN,
                WM_SYSKEYUP,
            ):
                event = ctypes.cast(
                    pointer, ctypes.POINTER(_KeyboardEvent)
                ).contents
                if not (
                    self._ignore_injected and (int(event.flags) & LLKHF_INJECTED)
                ):
                    self._handle_edge(
                        normalize_vk(int(event.vk)),
                        message in (WM_KEYDOWN, WM_SYSKEYDOWN),
                    )
        except Exception:  # noqa: BLE001 - never break the global hook chain
            pass
        return self._call_next(None, code, message, pointer)

    def _run(self) -> None:
        ready_announced = False
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            hook_proc_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            hookproc = hook_proc_type(self._hookproc)
            self._hookproc_keepalive = hookproc

            user32.CallNextHookEx.argtypes = (
                wintypes.HHOOK,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.CallNextHookEx.restype = ctypes.c_ssize_t
            user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
            user32.UnhookWindowsHookEx.restype = wintypes.BOOL
            user32.GetMessageW.argtypes = (
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            )
            user32.GetMessageW.restype = ctypes.c_int
            user32.PeekMessageW.argtypes = (
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
                wintypes.UINT,
            )
            user32.PeekMessageW.restype = wintypes.BOOL
            kernel32.GetCurrentThreadId.argtypes = ()
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            user32.SetWindowsHookExW.argtypes = (
                ctypes.c_int,
                hook_proc_type,
                wintypes.HINSTANCE,
                wintypes.DWORD,
            )
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            self._call_next = user32.CallNextHookEx
            self._thread_id = wintypes.DWORD(kernel32.GetCurrentThreadId())

            self._hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL,
                hookproc,
                kernel32.GetModuleHandleW(None),
                0,
            )
            if not self._hook:
                raise PresetSwitchUnavailableError("SetWindowsHookExW failed")

            # Force the message queue into existence before reporting ready;
            # PostThreadMessageW from stop() would otherwise be lost.
            probe = wintypes.MSG()
            user32.PeekMessageW(
                ctypes.byref(probe), None, 0, 0, PM_NOREMOVE
            )
            self._ready_event.set()
            ready_announced = True

            message = wintypes.MSG()
            while not self._stop_event.is_set():
                if user32.GetMessageW(ctypes.byref(message), None, 0, 0) <= 0:
                    break
        except BaseException as exc:  # noqa: BLE001 - surfaced to start()
            if not ready_announced:
                self._start_error = exc
            self._ready_event.set()
        finally:
            if self._hook:
                try:
                    ctypes.windll.user32.UnhookWindowsHookEx(  # type: ignore[attr-defined]
                        self._hook
                    )
                except Exception:
                    pass
                self._hook = None
            with self._lock:
                self._down.clear()
                self._latch = None
