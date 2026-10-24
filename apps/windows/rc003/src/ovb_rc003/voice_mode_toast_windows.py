"""Small on-screen bubble announcing the voice mode a short press switched to.

开启"短按切换模式"后，每次短按会在已配置的语音键之间循环，并在**鼠标当前
所在屏幕**的工作区底部弹出一个气泡显示模式名称。桥接进程没有 Qt，所以气泡
用 Win32 分层窗口直接画：``UpdateLayeredWindow`` + 32bpp DIB，跑道圆角与描边
逐像素用 SDF 算，文字先用 GDI 画进一张灰度蒙版再合成，这样抗锯齿边缘的
alpha 才正确（GDI 只写 RGB、不动 alpha）。

窗口样式带 ``WS_EX_LAYERED|TOOLWINDOW|TOPMOST|TRANSPARENT|NOACTIVATE``，
所以它不抢焦点、不进任务栏、也不挡鼠标；显示后由 ``WM_TIMER`` 自动隐藏。
消息泵跑在自己的线程上，``show()`` 只是投递一条 ``WM_TOAST_SHOW``，不会阻塞
输入处理。
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from typing import Optional, Tuple

WM_APP = 0x8000
WM_TOAST_SHOW = WM_APP + 1
WM_TIMER = 0x0113
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000

CS_VREDRAW = 0x0001
CS_HREDRAW = 0x0002
WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SW_HIDE = 0
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
DIB_RGB_COLORS = 0
MONITOR_DEFAULTTONEAREST = 0x00000002
LOGPIXELSY = 90
TRANSPARENT_BKMODE = 1
ANTIALIASED_QUALITY = 4
DEFAULT_CHARSET = 1
OUT_TT_PRECIS = 4
CLIP_DEFAULT_PRECIS = 0
DEFAULT_PITCH_AND_FAMILY = 0
FW_SEMIBOLD = 600
DT_CENTER = 0x00000001
DT_VCENTER = 0x00000004
DT_SINGLELINE = 0x00000020
DT_NOPREFIX = 0x00000800
DT_END_ELLIPSIS = 0x00008000

TIMER_ID = 1
# wintypes 里没有 UINT_PTR，SetTimer/KillTimer 的定时器 id 按指针宽度传。
UINT_PTR = ctypes.c_size_t
_TOAST_DURATION_MS = 1400
_TOAST_CLASS_NAME = "MoteDeckVoiceModeToast"
_FONT_FACE_PRIMARY = "Microsoft YaHei UI"
_FONT_FACE_FALLBACK = "Microsoft YaHei"
_TOAST_THEMES = {
    "dark": {"fill": (18, 18, 20), "border": (92, 92, 98), "text": (255, 255, 255)},
    "light": {
        "fill": (255, 255, 255),
        "border": (218, 218, 222),
        "text": (20, 20, 22),
    },
}


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", wintypes.BYTE),
        ("BlendFlags", wintypes.BYTE),
        ("SourceConstantAlpha", wintypes.BYTE),
        ("AlphaFormat", wintypes.BYTE),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


def _colorref(rgb: Tuple[int, int, int]) -> int:
    red, green, blue = rgb
    return int(red) | (int(green) << 8) | (int(blue) << 16)


class VoiceModeToast:
    """Fire-and-forget bubble drawn at the bottom of the cursor's monitor."""

    def __init__(self, *, logger=None, duration_ms: int = _TOAST_DURATION_MS) -> None:
        self._logger = logger
        self._duration_ms = max(200, int(duration_ms))
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._ready_event = threading.Event()
        self._pending: Optional[Tuple[str, dict]] = None
        self._stopped = False
        self._failed = False
        self._proc_keepalive: Optional[_WNDPROC] = None

    def show(self, text: str, theme: str = "dark") -> None:
        """Request one bubble; never blocks and never raises."""

        if sys.platform != "win32":
            return
        label = str(text or "").strip()
        if not label:
            return
        palette = _TOAST_THEMES.get(
            str(theme or "dark").strip().lower(), _TOAST_THEMES["dark"]
        )
        with self._lock:
            if self._stopped or self._failed:
                return
            if not self._ensure_thread_locked():
                return
            self._pending = (label, palette)
            thread_id = self._thread_id
        if thread_id:
            try:
                user32 = ctypes.windll.user32  # type: ignore[attr-defined]
                user32.PostThreadMessageW.argtypes = (
                    wintypes.DWORD,
                    wintypes.UINT,
                    wintypes.WPARAM,
                    wintypes.LPARAM,
                )
                user32.PostThreadMessageW.restype = wintypes.BOOL
                user32.PostThreadMessageW(thread_id, WM_TOAST_SHOW, 0, 0)
            except Exception:  # noqa: BLE001 - the bubble is best effort
                pass

    def stop(self) -> None:
        """Tear the pump down; safe to call more than once or before show()."""

        with self._lock:
            self._stopped = True
            self._pending = None
            thread = self._thread
            thread_id = self._thread_id
        if thread is None:
            return
        if sys.platform == "win32" and thread_id:
            try:
                user32 = ctypes.windll.user32  # type: ignore[attr-defined]
                user32.PostThreadMessageW.argtypes = (
                    wintypes.DWORD,
                    wintypes.UINT,
                    wintypes.WPARAM,
                    wintypes.LPARAM,
                )
                user32.PostThreadMessageW.restype = wintypes.BOOL
                user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        thread.join(timeout=1.5)
        with self._lock:
            self._thread = None
            self._thread_id = 0

    def _ensure_thread_locked(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        try:
            self._ready_event.clear()
            self._thread_id = 0
            thread = threading.Thread(
                target=self._run,
                name="remote-mic-voice-mode-toast",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        except BaseException as exc:  # noqa: BLE001 - never break voice input
            self._failed = True
            self._thread = None
            self._warn("voice mode toast thread failed to start", exc)
            return False
        return True

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

    def _run(self) -> None:
        hwnd = None
        try:
            user32, _gdi32, kernel32 = self._bind_win32()
            proc = _WNDPROC(self._wndproc)
            self._proc_keepalive = proc
            wnd_class = _WNDCLASSEXW()
            wnd_class.cbSize = ctypes.sizeof(_WNDCLASSEXW)
            wnd_class.style = CS_VREDRAW | CS_HREDRAW
            wnd_class.lpfnWndProc = proc
            wnd_class.hInstance = kernel32.GetModuleHandleW(None)
            wnd_class.lpszClassName = _TOAST_CLASS_NAME
            user32.RegisterClassExW(ctypes.byref(wnd_class))
            # 类可能已经被同进程的其它实例注册过，这时继续建窗口即可。
            hwnd = user32.CreateWindowExW(
                WS_EX_LAYERED
                | WS_EX_TOOLWINDOW
                | WS_EX_TOPMOST
                | WS_EX_TRANSPARENT
                | WS_EX_NOACTIVATE,
                _TOAST_CLASS_NAME,
                "MoteDeck voice mode",
                WS_POPUP,
                0,
                0,
                0,
                0,
                None,
                None,
                wnd_class.hInstance,
                None,
            )
            if not hwnd:
                self._warn("voice mode toast window creation failed")
                with self._lock:
                    self._failed = True
                return
            # 先让消息队列存在，再公布线程号；否则 show() 投递的消息会丢。
            probe = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(probe), None, 0, 0, PM_NOREMOVE)
            with self._lock:
                self._thread_id = int(kernel32.GetCurrentThreadId())
                pending = self._pending
                self._pending = None
            self._ready_event.set()
            if pending is not None:
                self._draw(hwnd, pending[0], pending[1])

            message = wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                if message.message == WM_TOAST_SHOW:
                    with self._lock:
                        pending = self._pending
                        self._pending = None
                    if pending is not None:
                        self._draw(hwnd, pending[0], pending[1])
                    continue
                if message.message == WM_TIMER and message.hWnd == hwnd:
                    user32.KillTimer(hwnd, TIMER_ID)
                    user32.ShowWindow(hwnd, SW_HIDE)
                    continue
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:  # noqa: BLE001 - the bubble is best effort
            self._warn("voice mode toast pump failed", exc)
            with self._lock:
                self._failed = True
            self._ready_event.set()
        finally:
            if hwnd:
                try:
                    ctypes.windll.user32.DestroyWindow(hwnd)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001 - shutdown must not raise
                    pass

    def _wndproc(self, hwnd, message, wparam, lparam):  # noqa: ANN001 - ctypes
        try:
            if message == WM_TIMER:
                user32 = ctypes.windll.user32  # type: ignore[attr-defined]
                user32.KillTimer(hwnd, TIMER_ID)
                user32.ShowWindow(hwnd, SW_HIDE)
                return 0
        except Exception:  # noqa: BLE001 - never break the window proc
            pass
        return ctypes.windll.user32.DefWindowProcW(  # type: ignore[attr-defined]
            hwnd, message, wparam, lparam
        )

    def _bind_win32(self):
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        gdi32 = ctypes.windll.gdi32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.GetCurrentThreadId.argtypes = ()
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterClassExW.argtypes = (ctypes.POINTER(_WNDCLASSEXW),)
        user32.RegisterClassExW.restype = wintypes.ATOM
        user32.DefWindowProcW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.CreateWindowExW.argtypes = (
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        )
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DestroyWindow.argtypes = (wintypes.HWND,)
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.ShowWindow.restype = wintypes.BOOL
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
        user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        user32.DispatchMessageW.restype = ctypes.c_ssize_t
        user32.PostThreadMessageW.argtypes = (
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.PostThreadMessageW.restype = wintypes.BOOL
        user32.SetTimer.argtypes = (
            wintypes.HWND,
            UINT_PTR,
            wintypes.UINT,
            wintypes.LPVOID,
        )
        user32.SetTimer.restype = UINT_PTR
        user32.KillTimer.argtypes = (wintypes.HWND, UINT_PTR)
        user32.KillTimer.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.MonitorFromPoint.argtypes = (wintypes.POINT, wintypes.DWORD)
        user32.MonitorFromPoint.restype = wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_MONITORINFOEXW),
        )
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.GetDC.argtypes = (wintypes.HWND,)
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
        user32.ReleaseDC.restype = ctypes.c_int
        user32.SetWindowPos.argtypes = (
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        )
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.UpdateLayeredWindow.argtypes = (
            wintypes.HWND,
            wintypes.HDC,
            ctypes.POINTER(wintypes.POINT),
            ctypes.POINTER(wintypes.SIZE),
            wintypes.HDC,
            ctypes.POINTER(wintypes.POINT),
            wintypes.DWORD,
            ctypes.POINTER(_BLENDFUNCTION),
            wintypes.DWORD,
        )
        user32.UpdateLayeredWindow.restype = wintypes.BOOL
        gdi32.CreateDCW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPVOID,
        )
        gdi32.CreateDCW.restype = wintypes.HDC
        gdi32.DeleteDC.argtypes = (wintypes.HDC,)
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.GetDeviceCaps.argtypes = (wintypes.HDC, ctypes.c_int)
        gdi32.GetDeviceCaps.restype = ctypes.c_int
        gdi32.CreateFontW.argtypes = (
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPCWSTR,
        )
        gdi32.CreateFontW.restype = wintypes.HANDLE
        gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.SetBkMode.argtypes = (wintypes.HDC, ctypes.c_int)
        gdi32.SetBkMode.restype = ctypes.c_int
        gdi32.SetTextColor.argtypes = (wintypes.HDC, wintypes.COLORREF)
        gdi32.SetTextColor.restype = wintypes.COLORREF
        gdi32.GetTextExtentPoint32W.argtypes = (
            wintypes.HDC,
            wintypes.LPCWSTR,
            ctypes.c_int,
            ctypes.POINTER(wintypes.SIZE),
        )
        gdi32.GetTextExtentPoint32W.restype = wintypes.BOOL
        # DrawTextW 由 user32 导出（gdi32 里没有这个符号）。
        user32.DrawTextW.argtypes = (
            wintypes.HDC,
            wintypes.LPCWSTR,
            ctypes.c_int,
            ctypes.POINTER(wintypes.RECT),
            wintypes.UINT,
        )
        user32.DrawTextW.restype = ctypes.c_int
        gdi32.CreateDIBSection.argtypes = (
            wintypes.HDC,
            ctypes.POINTER(_BITMAPINFOHEADER),
            wintypes.UINT,
            ctypes.POINTER(wintypes.LPVOID),
            wintypes.HANDLE,
            wintypes.DWORD,
        )
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        return user32, gdi32, kernel32

    def _monitor_for_cursor(self, user32):
        point = wintypes.POINT(0, 0)
        user32.GetCursorPos(ctypes.byref(point))
        monitor = user32.MonitorFromPoint(
            point, MONITOR_DEFAULTTONEAREST
        )
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None, None
        return monitor, info

    def _draw(self, hwnd, text: str, palette: dict) -> None:
        user32, gdi32, _ = self._bind_win32()
        monitor, info = self._monitor_for_cursor(user32)
        if info is None:
            return
        work = info.rcWork
        work_width = max(1, int(work.right) - int(work.left))

        monitor_dc = None
        if info.szDevice:
            monitor_dc = gdi32.CreateDCW(info.szDevice, None, None, None)
        owns_screen_dc = False
        if not monitor_dc:
            monitor_dc = user32.GetDC(None)
            owns_screen_dc = True
        if not monitor_dc:
            return
        font = None
        mem_dc = None
        mask_dc = None
        bitmap = None
        mask_bitmap = None
        previous_bitmap = None
        destination_dc = None
        try:
            try:
                dpi = int(gdi32.GetDeviceCaps(monitor_dc, LOGPIXELSY)) or 96
            except Exception:  # noqa: BLE001 - fall back to 96 dpi
                dpi = 96
            scale = _clamp(dpi / 96.0, 0.75, 4.0)
            font_px = max(13, int(round(16 * scale)))
            pad_x = max(12, int(round(16 * scale)))
            pad_y = max(7, int(round(8 * scale)))
            # rcWork 已经扣掉任务栏，留一点缝即可贴近任务栏，和豆包输入法一致。
            bottom_margin = max(6, int(round(10 * scale)))
            border_px = max(1, int(round(scale)))

            font = gdi32.CreateFontW(
                -font_px,
                0,
                0,
                0,
                FW_SEMIBOLD,
                0,
                0,
                0,
                DEFAULT_CHARSET,
                OUT_TT_PRECIS,
                CLIP_DEFAULT_PRECIS,
                ANTIALIASED_QUALITY,
                DEFAULT_PITCH_AND_FAMILY,
                _FONT_FACE_PRIMARY,
            )
            previous_font = gdi32.SelectObject(monitor_dc, font)
            extent = wintypes.SIZE(0, 0)
            gdi32.GetTextExtentPoint32W(
                monitor_dc, text, len(text), ctypes.byref(extent)
            )
            gdi32.SelectObject(monitor_dc, previous_font)
            text_width = max(1, int(extent.cx))

            width = max(
                int(96 * scale),
                text_width + 2 * pad_x,
            )
            width = min(width, max(1, work_width - int(80 * scale)))
            width = max(width, text_width + 2 * border_px + 2)
            height = font_px + 2 * pad_y
            position = wintypes.POINT(
                int(work.left) + (work_width - width) // 2,
                int(work.bottom) - height - bottom_margin,
            )

            mask_dc = gdi32.CreateCompatibleDC(monitor_dc)
            mask_bitmap, mask_bits = self._create_dib(gdi32, mask_dc, width, height)
            if mask_bitmap is None:
                return
            previous_mask = gdi32.SelectObject(mask_dc, mask_bitmap)
            previous_mask_font = gdi32.SelectObject(mask_dc, font)
            gdi32.SetBkMode(mask_dc, TRANSPARENT_BKMODE)
            gdi32.SetTextColor(mask_dc, 0x00FFFFFF)
            text_rect = wintypes.RECT(0, 0, width, height)
            user32.DrawTextW(
                mask_dc,
                text,
                len(text),
                ctypes.byref(text_rect),
                DT_CENTER
                | DT_VCENTER
                | DT_SINGLELINE
                | DT_NOPREFIX
                | DT_END_ELLIPSIS,
            )
            mask_bytes = ctypes.string_at(mask_bits, width * height * 4)
            gdi32.SelectObject(mask_dc, previous_mask_font)
            gdi32.SelectObject(mask_dc, previous_mask)

            pixels = self._compose_pixels(
                width,
                height,
                mask_bytes,
                palette,
                border_px,
            )

            mem_dc = gdi32.CreateCompatibleDC(monitor_dc)
            bitmap, bits = self._create_dib(gdi32, mem_dc, width, height)
            if bitmap is None:
                return
            ctypes.memmove(bits, bytes(pixels), len(pixels))
            previous_bitmap = gdi32.SelectObject(mem_dc, bitmap)
            size = wintypes.SIZE(width, height)
            source_origin = wintypes.POINT(0, 0)
            blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            destination_dc = user32.GetDC(None)
            if not user32.UpdateLayeredWindow(
                hwnd,
                destination_dc or None,
                ctypes.byref(position),
                ctypes.byref(size),
                mem_dc,
                ctypes.byref(source_origin),
                0,
                ctypes.byref(blend),
                ULW_ALPHA,
            ):
                self._warn("voice mode toast UpdateLayeredWindow failed")
                return
            user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
            user32.SetTimer(hwnd, TIMER_ID, self._duration_ms, None)
        finally:
            if destination_dc:
                try:
                    user32.ReleaseDC(None, destination_dc)
                except Exception:  # noqa: BLE001 - cleanup is best effort
                    pass
            if mem_dc and bitmap:
                try:
                    gdi32.SelectObject(mem_dc, previous_bitmap)
                except Exception:  # noqa: BLE001 - cleanup is best effort
                    pass
            if mask_dc and mask_bitmap:
                try:
                    gdi32.DeleteObject(mask_bitmap)
                except Exception:  # noqa: BLE001 - cleanup is best effort
                    pass
            if bitmap:
                try:
                    gdi32.DeleteObject(bitmap)
                except Exception:  # noqa: BLE001 - cleanup is best effort
                    pass
            for dc in (mem_dc, mask_dc):
                if dc:
                    try:
                        gdi32.DeleteDC(dc)
                    except Exception:  # noqa: BLE001 - cleanup is best effort
                        pass
            if font:
                try:
                    gdi32.DeleteObject(font)
                except Exception:  # noqa: BLE001 - cleanup is best effort
                    pass
            if monitor_dc:
                try:
                    if owns_screen_dc:
                        user32.ReleaseDC(None, monitor_dc)
                    else:
                        gdi32.DeleteDC(monitor_dc)
                except Exception:  # noqa: BLE001 - cleanup is best effort
                    pass

    @staticmethod
    def _create_dib(gdi32, dc, width: int, height: int):
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        # 正的 biHeight 表示自底向上存储：缓冲区第 0 行是图像最下面一行。
        header.biHeight = height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB
        bits = wintypes.LPVOID()
        bitmap = gdi32.CreateDIBSection(
            dc, ctypes.byref(header), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        if not bitmap or not bits.value:
            if bitmap:
                gdi32.DeleteObject(bitmap)
            return None, None
        return bitmap, bits.value

    @staticmethod
    def _compose_pixels(
        width: int,
        height: int,
        mask: bytes,
        palette: dict,
        border_px: int,
    ) -> bytearray:
        """Composite fill + border + text into premultiplied BGRA, bottom-up."""

        fill = palette["fill"]
        border = palette["border"]
        text_color = palette["text"]
        radius = height / 2.0
        half_x = width / 2.0 - radius
        center_x = width / 2.0
        center_y = height / 2.0
        pixels = bytearray(width * height * 4)
        stride = width * 4
        for row in range(height):
            y = row + 0.5 - center_y
            # DIB 自底向上：图像第 row 行存放在缓冲区的倒数第 row 行。
            base = (height - 1 - row) * stride
            for column in range(width):
                x = column + 0.5 - center_x
                qx = abs(x) - half_x
                qy = abs(y)
                far = qx if qx > qy else qy
                if far > 0.0:
                    signed = (qx * qx + qy * qy) ** 0.5 if qx > 0.0 and qy > 0.0 else far
                    distance = signed - radius
                else:
                    distance = far - radius
                outer = _clamp(0.5 - distance, 0.0, 1.0)
                if outer <= 0.0:
                    continue
                inner = _clamp(0.5 - (distance + border_px), 0.0, 1.0)
                ring = outer - inner
                alpha = _clamp(inner + ring, 0.0, 1.0)
                blue = fill[2] * inner + border[2] * ring
                green = fill[1] * inner + border[1] * ring
                red = fill[0] * inner + border[0] * ring
                premultiplied = alpha
                # 蒙版 DIB 也是自底向上：第 row 行对应缓冲区的倒数第 row 行。
                mask_row = height - 1 - row
                coverage = mask[(mask_row * width + column) * 4] / 255.0
                if coverage > 0.0 and inner > 0.0:
                    text_alpha = coverage * inner
                    keep = 1.0 - text_alpha
                    alpha = _clamp(
                        text_alpha + premultiplied * keep, 0.0, 1.0
                    )
                    blue = text_color[2] * text_alpha + blue * keep
                    green = text_color[1] * text_alpha + green * keep
                    red = text_color[0] * text_alpha + red * keep
                    premultiplied = alpha
                offset = base + column * 4
                # 已经是预乘结果：每个通道天然 <= alpha*255，只需四舍五入夹到 0..255。
                pixels[offset] = int(_clamp(blue + 0.5, 0.0, 255.0))
                pixels[offset + 1] = int(_clamp(green + 0.5, 0.0, 255.0))
                pixels[offset + 2] = int(_clamp(red + 0.5, 0.0, 255.0))
                pixels[offset + 3] = int(
                    _clamp(premultiplied * 255.0 + 0.5, 0.0, 255.0)
                )
        return pixels
