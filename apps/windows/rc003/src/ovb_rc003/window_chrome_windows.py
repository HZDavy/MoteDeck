"""Best-effort native title-bar colors for the Windows settings window."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
from typing import Callable, Optional


_DWMWA_BORDER_COLOR = 34
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36


def _colorref(color: object) -> int:
    """Convert a QColor-like object to Windows COLORREF (0x00BBGGRR)."""

    red = int(color.red())  # type: ignore[attr-defined]
    green = int(color.green())  # type: ignore[attr-defined]
    blue = int(color.blue())  # type: ignore[attr-defined]
    if not all(0 <= channel <= 255 for channel in (red, green, blue)):
        raise ValueError("color channel outside 0..255")
    return red | (green << 8) | (blue << 16)


def _load_set_window_attribute() -> Callable[..., int]:
    set_attribute = ctypes.WinDLL("dwmapi").DwmSetWindowAttribute
    set_attribute.argtypes = (
        wintypes.HWND,
        wintypes.DWORD,
        wintypes.LPCVOID,
        wintypes.DWORD,
    )
    set_attribute.restype = ctypes.c_long
    return set_attribute


def apply_settings_window_chrome(
    window: object,
    *,
    platform: Optional[str] = None,
    set_window_attribute: Optional[Callable[..., int]] = None,
) -> bool:
    """Keep the native caption consistent with the QML shell when activated."""

    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return False

    try:
        hwnd = int(window.winId())  # type: ignore[attr-defined]
        property_value = window.property  # type: ignore[attr-defined]
        colors = (
            (_DWMWA_BORDER_COLOR, property_value("nativeBorderColor")),
            (_DWMWA_CAPTION_COLOR, property_value("nativeCaptionColor")),
            (_DWMWA_TEXT_COLOR, property_value("nativeCaptionTextColor")),
        )
        colorrefs = tuple((attribute, _colorref(color)) for attribute, color in colors)
        setter = set_window_attribute or _load_set_window_attribute()
    except (AttributeError, OSError, TypeError, ValueError):
        return False

    applied = True
    for attribute, colorref in colorrefs:
        value = wintypes.DWORD(colorref)
        try:
            result = setter(
                wintypes.HWND(hwnd),
                wintypes.DWORD(attribute),
                ctypes.byref(value),
                wintypes.DWORD(ctypes.sizeof(value)),
            )
        except (OSError, TypeError, ValueError):
            applied = False
            continue
        applied = applied and result == 0
    return applied


# ── 自绘标题区（原生无边框） ─────────────────────────────────

_WM_NCCALCSIZE = 0x0083
_WM_NCHITTEST = 0x0084
_WM_GETMINMAXINFO = 0x0024
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_MONITOR_DEFAULTTONEAREST = 2
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020

_HTCLIENT = 1
_HTLEFT = 10
_HTRIGHT = 11
_HTTOP = 12
_HTTOPLEFT = 13
_HTTOPRIGHT = 14
_HTBOTTOM = 15
_HTBOTTOMLEFT = 16
_HTBOTTOMRIGHT = 17

_filters: list = []


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class _MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved", wintypes.POINT),
        ("ptMaxSize", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("ptMinTrackSize", wintypes.POINT),
        ("ptMaxTrackSize", wintypes.POINT),
    ]


class _NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [
        ("rgrc", wintypes.RECT * 3),
        ("lppos", ctypes.c_void_p),
    ]


def _install_dwm(window: object) -> None:
    """圆角由系统绘制，标题文字颜色不再由 DWM 画（标题区是自绘的）。"""

    try:
        hwnd = wintypes.HWND(int(window.winId()))  # type: ignore[attr-defined]
        setter = _load_set_window_attribute()
        corner = wintypes.DWORD(2)  # DWMWCP_ROUND
        setter(hwnd, wintypes.DWORD(_DWMWA_WINDOW_CORNER_PREFERENCE),
               ctypes.byref(corner), wintypes.DWORD(ctypes.sizeof(corner)))
        dark = wintypes.DWORD(1 if window.property("darkMode") else 0)  # type: ignore[attr-defined]
        setter(hwnd, wintypes.DWORD(_DWMWA_USE_IMMERSIVE_DARK_MODE),
               ctypes.byref(dark), wintypes.DWORD(ctypes.sizeof(dark)))
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return


def install_frameless_window(window: object, *, platform: Optional[str] = None) -> bool:
    """把系统标题栏并入客户区，同时保留系统阴影、圆角与边缘拖拽缩放。

    规则：
      - WM_NCCALCSIZE 返回 0：客户区铺满窗口（含原标题栏区域）。
      - WM_NCHITTEST：距窗口边缘 6px 内返回缩放热区，其余交给 QML。
      - 拖动由 QML 的 startSystemMove() 发起，因此最大化按钮等仍可点击。
    """

    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return False

    try:
        from PySide6.QtCore import QAbstractNativeEventFilter
        user32 = ctypes.WinDLL("user32")
        user32.IsZoomed.argtypes = (wintypes.HWND,)
        user32.IsZoomed.restype = wintypes.BOOL
        user32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.GetMonitorInfoW.argtypes = (ctypes.c_void_p, ctypes.POINTER(_MONITORINFO))
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        user32.SetWindowPos.argtypes = (
            wintypes.HWND,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        )
        hwnd = wintypes.HWND(int(window.winId()))  # type: ignore[attr-defined]
        scale = max(1.0, float(window.devicePixelRatio()))  # type: ignore[attr-defined]
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, ImportError):
        return False

    border_px = max(4, int(round(6 * scale)))

    class _CaptionFilter(QAbstractNativeEventFilter):
        def nativeEventFilter(self, event_type, message):  # noqa: N802
            try:
                if bytes(event_type) != b"windows_generic_MSG":
                    return (False, 0)
                msg = wintypes.MSG.from_address(int(message))
                if msg.hWnd != hwnd.value:
                    return (False, 0)
                if msg.message == _WM_NCCALCSIZE and msg.wParam:
                    self._fit_client_to_work_area(hwnd, msg.lParam)
                    return (True, 0)
                if msg.message == _WM_GETMINMAXINFO:
                    self._apply_max_bounds(hwnd, msg.lParam)
                    return (True, 0)
                if msg.message == _WM_NCHITTEST and not user32.IsZoomed(hwnd):
                    return (True, self._hit_test(hwnd, msg.lParam))
            except (OSError, TypeError, ValueError):
                return (False, 0)
            return (False, 0)

        def _fit_client_to_work_area(self, handle, lparam):
            """最大化时系统会把窗口按边框撑出工作区，这里把客户区夹回工作区。"""

            if not user32.IsZoomed(handle):
                return
            monitor = user32.MonitorFromWindow(handle, _MONITOR_DEFAULTTONEAREST)
            if not monitor:
                return
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return
            work = info.rcWork
            params = _NCCALCSIZE_PARAMS.from_address(int(lparam))
            params.rgrc[0].left = work.left
            params.rgrc[0].top = work.top
            params.rgrc[0].right = work.right
            params.rgrc[0].bottom = work.bottom

        def _apply_max_bounds(self, handle, lparam):
            """Qt 的最小尺寸依赖这条消息，自行接手后要补回去。"""

            bounds = _MINMAXINFO.from_address(int(lparam))
            bounds.ptMinTrackSize.x = max(
                bounds.ptMinTrackSize.x, int(round(window.minimumWidth() * scale))  # type: ignore[attr-defined]
            )
            bounds.ptMinTrackSize.y = max(
                bounds.ptMinTrackSize.y, int(round(window.minimumHeight() * scale))  # type: ignore[attr-defined]
            )

        def _hit_test(self, handle, lparam):
            x = ctypes.c_short(lparam & 0xFFFF).value
            y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            rect = wintypes.RECT()
            user32.GetWindowRect(handle, ctypes.byref(rect))
            border = border_px
            left = x - rect.left < border
            right = rect.right - x < border
            top = y - rect.top < border
            bottom = rect.bottom - y < border
            if top and left:
                return _HTTOPLEFT
            if top and right:
                return _HTTOPRIGHT
            if bottom and left:
                return _HTBOTTOMLEFT
            if bottom and right:
                return _HTBOTTOMRIGHT
            if left:
                return _HTLEFT
            if right:
                return _HTRIGHT
            if top:
                return _HTTOP
            if bottom:
                return _HTBOTTOM
            return _HTCLIENT

    try:
        from PySide6.QtGui import QGuiApplication
        app = QGuiApplication.instance()
        if app is None:
            return False
        was_visible = bool(window.isVisible())  # type: ignore[attr-defined]
        geometry = (
            window.x(),  # type: ignore[attr-defined]
            window.y(),  # type: ignore[attr-defined]
            window.width(),  # type: ignore[attr-defined]
            window.height(),  # type: ignore[attr-defined]
        )
        if was_visible:
            window.setVisible(False)  # type: ignore[attr-defined]
        event_filter = _CaptionFilter()
        app.installNativeEventFilter(event_filter)
        _filters.append(event_filter)
        _install_dwm(window)
        # 窗口创建时自绘边框还没接管，必须强制系统重算一次非客户区。
        user32.SetWindowPos(
            hwnd,
            None,
            0,
            0,
            0,
            0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED,
        )
        window.setGeometry(*geometry)  # type: ignore[attr-defined]
        if was_visible:
            window.setVisible(True)  # type: ignore[attr-defined]
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, ImportError):
        return False
    return True

