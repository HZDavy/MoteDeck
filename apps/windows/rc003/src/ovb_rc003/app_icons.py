"""Best-effort extraction of an executable's icon as a QML-loadable image.

The settings UI only creates a QGuiApplication, so QtWidgets (and therefore
QFileIconProvider) is unavailable. This module talks to the Win32 shell
directly: it loads the icon resource, converts the colour bitmap into a 32-bit
top-down DIB, turns it into a QImage and writes a PNG into a cache directory.

Every failure returns "" so the caller can fall back to a vector glyph. The
cache is keyed by executable path plus modification time, and files stay in the
system temporary directory - never in the project or the user's data folder.
"""
from __future__ import annotations

import ctypes
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Dict

from PySide6.QtGui import QImage

_SHGFI_ICON = 0x000000100
_SHGFI_LARGEICON = 0x000000000
_CACHE_DIR_NAME = "rc003-app-icons"


class _ICONINFO(ctypes.Structure):
    _fields_ = (
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    )


class _BITMAP(ctypes.Structure):
    _fields_ = (
        ("bmType", wintypes.LONG),
        ("bmWidth", wintypes.LONG),
        ("bmHeight", wintypes.LONG),
        ("bmWidthBytes", wintypes.LONG),
        ("bmPlanes", wintypes.WORD),
        ("bmBitsPixel", wintypes.WORD),
        ("bmBits", ctypes.c_void_p),
    )


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = (
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
    )


class _BITMAPINFO(ctypes.Structure):
    _fields_ = (
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    )


class _SHFILEINFOW(ctypes.Structure):
    _fields_ = (
        ("hIcon", ctypes.c_void_p),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", wintypes.WCHAR * 260),
        ("szTypeName", wintypes.WCHAR * 80),
    )


_MEMO: Dict[str, str] = {}


def _icon_to_image(user32, gdi32, handle) -> QImage | None:
    info = _ICONINFO()
    if not user32.GetIconInfo(handle, ctypes.byref(info)):
        return None
    try:
        if not info.hbmColor:
            return None
        bitmap = _BITMAP()
        if not gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(_BITMAP), ctypes.byref(bitmap)):
            return None
        width, height = int(bitmap.bmWidth), int(bitmap.bmHeight)
        if width <= 0 or height <= 0 or width > 1024 or height > 1024:
            return None
        header = _BITMAPINFOHEADER(
            biSize=ctypes.sizeof(_BITMAPINFOHEADER),
            biWidth=width,
            biHeight=-height,  # negative height asks for a top-down DIB
            biPlanes=1,
            biBitCount=32,
            biCompression=0,  # BI_RGB
            biSizeImage=0,
            biXPelsPerMeter=0,
            biYPelsPerMeter=0,
            biClrUsed=0,
            biClrImportant=0,
        )
        info_block = _BITMAPINFO(bmiHeader=header)
        size = width * height * 4
        buffer = ctypes.create_string_buffer(size)
        device_context = gdi32.CreateCompatibleDC(None)
        if not device_context:
            return None
        try:
            if not gdi32.GetDIBits(device_context, info.hbmColor, 0, height,
                                   buffer, ctypes.byref(info_block), 0):
                return None
        finally:
            gdi32.DeleteDC(device_context)
        image = QImage(bytes(buffer.raw), width, height, width * 4,
                       QImage.Format_ARGB32).copy()
        if image.isNull():
            return None
        if not any(image.pixelColor(x, y).alpha() for x in range(0, width, 2)
                   for y in range(0, height, 2)):
            image = image.convertToFormat(QImage.Format_RGB32)
        return image
    finally:
        if info.hbmColor:
            gdi32.DeleteObject(info.hbmColor)
        if info.hbmMask:
            gdi32.DeleteObject(info.hbmMask)


def _load_icon_handle(user32, shell32, executable: str):
    """Return (handle, release) for an executable, or (None, None)."""
    large = ctypes.c_void_p()
    small = ctypes.c_void_p()
    try:
        count = shell32.ExtractIconExW(executable, 0, ctypes.byref(large),
                                       ctypes.byref(small), 1)
    except (OSError, ValueError):
        count = 0
    if count and large.value:
        if small.value:
            user32.DestroyIcon(small)
        return large.value, user32.DestroyIcon
    if count and small.value:
        return small.value, user32.DestroyIcon
    info = _SHFILEINFOW()
    try:
        shell32.SHGetFileInfoW(executable, 0, ctypes.byref(info), ctypes.sizeof(info),
                               _SHGFI_ICON | _SHGFI_LARGEICON)
    except (OSError, ValueError):
        return None, None
    if info.hIcon:
        return info.hIcon, user32.DestroyIcon
    return None, None


def icon_file_url(executable: str) -> str:
    """A file:// URL of a freshly written PNG icon, or "" when unavailable."""
    if sys.platform != "win32" or not executable:
        return ""
    path = Path(executable)
    try:
        stamp = path.stat()
    except OSError:
        return ""
    key = f"{path}|{stamp.st_mtime_ns}|{stamp.st_size}"
    cached = _MEMO.get(key)
    if cached is not None:
        return cached
    url = ""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ExtractIconExW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), wintypes.UINT]
        shell32.ExtractIconExW.restype = wintypes.UINT
        shell32.SHGetFileInfoW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
        shell32.SHGetFileInfoW.restype = ctypes.c_void_p
        user32.DestroyIcon.argtypes = [ctypes.c_void_p]
        user32.DestroyIcon.restype = wintypes.BOOL
        user32.GetIconInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ICONINFO)]
        user32.GetIconInfo.restype = wintypes.BOOL
        gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        gdi32.GetObjectW.restype = ctypes.c_int
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
        gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.GetDIBits.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
            ctypes.c_void_p, ctypes.POINTER(_BITMAPINFO), wintypes.UINT]
        gdi32.GetDIBits.restype = ctypes.c_int
        handle, release = _load_icon_handle(user32, shell32, str(path))
        if handle:
            try:
                image = _icon_to_image(user32, gdi32, handle)
            finally:
                release(handle)
            if image is not None:
                target = Path(tempfile.gettempdir()) / _CACHE_DIR_NAME
                target.mkdir(parents=True, exist_ok=True)
                file_path = target / f"{abs(hash(key)):016x}.png"
                if not file_path.exists() and not image.save(str(file_path), "PNG"):
                    file_path = None
                if file_path is not None:
                    url = f"file:///{file_path.as_posix()}"
    except (OSError, ValueError, AttributeError):
        url = ""
    _MEMO[key] = url
    return url
