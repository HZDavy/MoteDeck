"""把键盘焦点放进当前前台应用最合适的输入框。

按键映射里的「聚焦输入框」动作调用这里：读取前台窗口的 UI Automation
控件树，按「像不像对话/搜索输入框」打分选出最合适的一个，调用 UIA 的
``SetFocus`` 并读回确认。整个过程只读控件的语义与位置，不读取用户输入
的任何文字。

不修改窗口层级、不抢别的应用的焦点：只有目标已经是前台窗口时才行动。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Iterator, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 扫描上限：Electron 应用的控件树可能上千个节点，限定深度与数量避免
# 一次按键扫描几百毫秒以上。
_MAX_SCAN_NODES = 1500
_MAX_SCAN_DEPTH = 14
_SCAN_TIMEOUT_SECONDS = 2.5
_ATTEMPT_INTERVAL_SECONDS = 0.25
_SCORE_THRESHOLD = 80

_TEXT_CONTROL_HINTS = ("Edit", "Document")

# 富文本网页编辑器（ProseMirror/TipTap 等）在 UIA 里常是 Group 而不是
# Edit，只能靠类名/自动化标识里的特征词认出来。
_EDITOR_HINTS = (
    "prosemirror",
    "tiptap",
    "contenteditable",
    "cm-editor",
    "codemirror",
    "ql-editor",
    "slate-editor",
)

_STRONG_TERMS = (
    "消息", "发送", "输入", "聊天", "对话", "搜索", "评论", "回复",
    "message", "send", "input", "chat", "composer", "reply", "comment",
    "search", "editor",
)
_SUPPORTING_TERMS = (
    "文本", "内容", "提问", "写作", "prompt", "text", "content", "ask",
)

# 明确不该聚焦的输入框：浏览器地址栏、命令面板、重命名框等。
_EXCLUDED_TERMS = (
    "地址和搜索栏", "地址栏", "omnibox", "command palette", "命令面板",
    "rename", "重命名", "查找", "find", "replace", "替换",
)

# 密码/敏感字段直接跳过，避免把焦点送进密码框。
_SENSITIVE_TERMS = ("密码", "password", "passwd", "验证码", "otp", "secret")


def _semantic_text(control) -> str:
    parts = []
    for attribute in ("Name", "AutomationId", "ClassName"):
        try:
            value = getattr(control, attribute, "")
        except Exception:
            value = ""
        if value:
            parts.append(str(value))
    return " ".join(parts).lower()


def _is_password(control) -> bool:
    try:
        return bool(control.IsPassword)
    except Exception:
        return False


def _is_read_only(control) -> bool:
    try:
        return bool(control.IsReadOnly)
    except Exception:
        return False


def _rect(control) -> Optional[Tuple[float, float, float, float]]:
    try:
        rect = control.BoundingRectangle
        left, top, right, bottom = (
            float(rect.left), float(rect.top), float(rect.right), float(rect.bottom)
        )
    except Exception:
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _passes_hard_gate(control) -> bool:
    try:
        if not control.IsEnabled or control.IsOffscreen:
            return False
        if not control.IsKeyboardFocusable:
            return False
    except Exception:
        return False
    if _is_password(control) or _is_read_only(control):
        return False
    semantic = _semantic_text(control)
    if any(term in semantic for term in _SENSITIVE_TERMS):
        return False
    if any(term in semantic for term in _EXCLUDED_TERMS):
        return False
    try:
        type_name = str(control.ControlTypeName)
    except Exception:
        type_name = ""
    is_text = any(hint in type_name for hint in _TEXT_CONTROL_HINTS)
    is_editor = any(hint in semantic for hint in _EDITOR_HINTS)
    return is_text or is_editor


def candidate_score(control, window_rect: Optional[tuple]) -> Optional[int]:
    """给一个候选输入框打分；不属于输入框或不合格时返回 ``None``。"""

    if not _passes_hard_gate(control):
        return None
    semantic = _semantic_text(control)
    try:
        type_name = str(control.ControlTypeName)
    except Exception:
        type_name = ""
    score = 50 if any(hint in type_name for hint in _TEXT_CONTROL_HINTS) else 30
    try:
        if control.HasKeyboardFocus:
            # 用户当前正在用的输入框优先保留。
            score += 40
    except Exception:
        pass
    if any(term in semantic for term in _STRONG_TERMS):
        score += 120
    elif any(term in semantic for term in _SUPPORTING_TERMS):
        score += 70

    rect = _rect(control)
    if rect is not None and window_rect is not None:
        left, top, right, bottom = rect
        win_left, win_top, win_right, win_bottom = window_rect
        win_width = max(win_right - win_left, 1.0)
        win_height = max(win_bottom - win_top, 1.0)
        width_ratio = (right - left) / win_width
        height_ratio = (bottom - top) / win_height
        top_ratio = (top - win_top) / win_height
        if width_ratio >= 0.30:
            score += 25
        if width_ratio >= 0.45:
            score += 30
        if 0.02 <= height_ratio <= 0.60:
            score += 10
        if top_ratio >= 0.55:
            score += 20
        elif top_ratio <= 0.25:
            score -= 15
    return score if score >= _SCORE_THRESHOLD else None


def _walk(control) -> Iterator[object]:
    stack = [(control, 0)]
    visited = 0
    while stack and visited < _MAX_SCAN_NODES:
        node, depth = stack.pop()
        visited += 1
        yield node
        if depth >= _MAX_SCAN_DEPTH:
            continue
        try:
            children = node.GetChildren()
        except Exception:
            children = []
        for child in reversed(list(children or ())):
            stack.append((child, depth + 1))


def _foreground_hwnd() -> int:
    import ctypes

    return int(ctypes.windll.user32.GetForegroundWindow())


def _window_pid(hwnd: int) -> int:
    import ctypes
    import ctypes.wintypes

    pid = ctypes.wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(
        ctypes.wintypes.HWND(hwnd), ctypes.byref(pid)
    )
    return int(pid.value)


def _control_pid(control) -> int:
    try:
        return int(control.ProcessId)
    except Exception:
        return 0


def _window_at_cursor() -> int:
    """鼠标指针下的顶层窗口；录制时用它当目标窗口。

    不能只看前台窗口：用户多半只是把光标移到目标输入框上，本软件的设置
    窗口仍然在前台，按前台窗口判断会把这次录制直接判成失败。
    """

    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32
    user32.WindowFromPoint.argtypes = (ctypes.wintypes.POINT,)
    user32.WindowFromPoint.restype = ctypes.wintypes.HWND
    user32.GetAncestor.argtypes = (ctypes.wintypes.HWND, ctypes.c_uint)
    user32.GetAncestor.restype = ctypes.wintypes.HWND
    point = ctypes.wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        return 0
    hwnd = user32.WindowFromPoint(point)
    if not hwnd:
        return 0
    root = user32.GetAncestor(ctypes.wintypes.HWND(hwnd), 2)  # GA_ROOT
    return int(root or hwnd)


def _capture_root():
    """录制目标：鼠标指针下那个顶层窗口的 UIA 根。

    指针还在本软件自己的界面上时返回 ``None``，避免把设置窗口学进去。
    """

    import uiautomation as auto

    hwnd = _window_at_cursor()
    if not hwnd or _window_pid(hwnd) == os.getpid():
        return None
    root = auto.ControlFromHandle(hwnd)
    if root is None:
        return None
    return root, _rect(root)


def _focus_once() -> bool:
    """扫描一次前台窗口；成功把焦点放进去返回 ``True``。"""

    import ctypes
    import uiautomation as auto

    hwnd = _foreground_hwnd()
    if not hwnd:
        return False
    root = auto.ControlFromHandle(hwnd)
    if root is None:
        return False
    try:
        if int(root.ProcessId) == os.getpid():
            # 前台是本软件自己的窗口时不抢焦点。
            return False
    except Exception:
        pass

    focused = None
    try:
        focused = auto.GetFocusedControl()
    except Exception:
        focused = None
    if focused is not None and candidate_score(focused, _rect(root)) is not None:
        return True

    best = None
    best_score = -1
    window_rect = _rect(root)
    for control in _walk(root):
        score = candidate_score(control, window_rect)
        if score is None or score <= best_score:
            continue
        best, best_score = control, score
    if best is None:
        return False
    try:
        best.SetFocus()
    except Exception:
        return False
    time.sleep(0.05)
    try:
        if best.HasKeyboardFocus:
            return True
    except Exception:
        pass
    try:
        focused = auto.GetFocusedControl()
        return focused is not None and focused.GetRuntimeId() == best.GetRuntimeId()
    except Exception:
        return False


def focus_foreground_input(timeout: float = _SCAN_TIMEOUT_SECONDS) -> bool:
    """在限定时间内反复尝试；成功返回 ``True``。"""

    import uiautomation as auto

    deadline = time.monotonic() + max(0.1, float(timeout))
    with auto.UIAutomationInitializerInThread():
        while time.monotonic() < deadline:
            try:
                if _focus_once():
                    return True
            except Exception:
                logger.debug("focus input attempt failed", exc_info=True)
            time.sleep(_ATTEMPT_INTERVAL_SECONDS)
    return False


def request_focus_foreground_input() -> None:
    """后台线程执行，避免阻塞按键回调。"""

    def _run() -> None:
        try:
            focus_foreground_input()
        except Exception:
            logger.debug("focus foreground input failed", exc_info=True)

    threading.Thread(
        target=_run, name="motedeck-focus-input", daemon=True
    ).start()


def _window_rect() -> Optional[Tuple[float, float, float, float]]:
    import ctypes
    import ctypes.wintypes

    rect = ctypes.wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(
        _foreground_hwnd(), ctypes.byref(rect)
    ):
        return None
    if rect.right <= rect.left or rect.bottom <= rect.top:
        return None
    return float(rect.left), float(rect.top), float(rect.right), float(rect.bottom)


def _rect_for_hwnd(hwnd: int) -> Optional[Tuple[float, float, float, float]]:
    """指定窗口的屏幕矩形（录制时按指针下的窗口算比例，而不是前台窗口）。"""

    import ctypes
    import ctypes.wintypes

    rect = ctypes.wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(
        ctypes.wintypes.HWND(hwnd), ctypes.byref(rect)
    ):
        return None
    if rect.right <= rect.left or rect.bottom <= rect.top:
        return None
    return float(rect.left), float(rect.top), float(rect.right), float(rect.bottom)


def _control_at_cursor():
    """返回鼠标指针下最内层的 UIA 控件；学习时用来找用户点中的输入框。"""

    import ctypes
    import ctypes.wintypes

    import uiautomation as auto

    point = ctypes.wintypes.POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        return None
    try:
        return auto.ControlFromPoint(point.x, point.y)
    except Exception:
        return None


def _looks_like_page_container(control) -> bool:
    """浏览器整页根节点不算输入框：学它只会把焦点放到网页本身。

    不能只看尺寸——记事本这类全窗口文本编辑器的 Document 也铺满窗口，
    要按自动化标识区分；带富文本编辑器特征的整页根节点仍然保留。
    """

    try:
        automation_id = str(control.AutomationId or "").lower()
        class_name = str(control.ClassName or "").lower()
        name = str(control.Name or "").lower()
    except Exception:
        return True
    if "rootwebarea" not in automation_id:
        return False
    semantic = " ".join((automation_id, class_name, name))
    return not any(hint in semantic for hint in _EDITOR_HINTS)


def _clamp_ratio(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return min(max(number, 0.0), 1.0)


def click_recorded_point(x_ratio: object, y_ratio: object) -> bool:
    """在录制时相对窗口的位置点一下左键（窗口大小变了也按比例换算）。"""

    import ctypes

    window = _window_rect()
    if window is None:
        return False
    left, top, right, bottom = window
    x = int(left + (right - left) * _clamp_ratio(x_ratio))
    y = int(top + (bottom - top) * _clamp_ratio(y_ratio))
    if not ctypes.windll.user32.SetCursorPos(x, y):
        return False
    time.sleep(0.03)
    try:
        from . import win32_input

        win32_input.send_mouse_button_click("left")
    except Exception:
        logger.debug("recorded focus point click failed", exc_info=True)
        return False
    return True


def _control_signature(
    control, window_rect: Optional[tuple]
) -> Optional[dict]:
    rect = _rect(control)
    if rect is None or window_rect is None:
        return None
    win_left, win_top, win_right, win_bottom = window_rect
    win_width = max(win_right - win_left, 1.0)
    win_height = max(win_bottom - win_top, 1.0)
    try:
        automation_id = str(control.AutomationId or "")
        class_name = str(control.ClassName or "")
        control_type = str(control.ControlTypeName or "")
        name = str(control.Name or "")[:80]
    except Exception:
        return None
    return {
        "automation_id": automation_id[:120],
        "class_name": class_name[:120],
        "control_type": control_type[:120],
        "name": name,
        "left": (rect[0] - win_left) / win_width,
        "top": (rect[1] - win_top) / win_height,
        "width": (rect[2] - rect[0]) / win_width,
        "height": (rect[3] - rect[1]) / win_height,
    }


def capture_focused_input() -> Optional[dict]:
    """学习模式：取鼠标指针下那个窗口里「正在用」或最像输入框的控件特征。

    优先看鼠标指针落点（提示里让用户把光标点进输入框），再退回当前键盘
    焦点，最后才全树扫描；整页文档/根节点一律跳过，否则会把焦点学到页面
    本身而不是输入框。
    """

    import uiautomation as auto

    try:
        with auto.UIAutomationInitializerInThread():
            target = _capture_root()
            if target is None:
                return None
            root, window_rect = target
            try:
                root_pid = int(root.ProcessId)
            except Exception:
                root_pid = _window_pid(_window_at_cursor())

            def usable(control) -> bool:
                if control is None or control is root:
                    return False
                if _looks_like_page_container(control):
                    return False
                return candidate_score(control, window_rect) is not None

            candidate = None
            node = _control_at_cursor()
            while node is not None and node is not root:
                try:
                    # 鼠标不在目标窗口里时，别把别的应用的控件学进来。
                    if int(node.ProcessId) != root_pid:
                        break
                except Exception:
                    break
                if usable(node):
                    candidate = node
                    break
                try:
                    node = node.GetParentControl()
                except Exception:
                    break

            if candidate is None:
                focused = None
                try:
                    focused = auto.GetFocusedControl()
                except Exception:
                    focused = None
                # 键盘焦点可能停在别的窗口（例如刚切过去的另一个应用），
                # 只接受目标窗口自己进程里的控件。
                if (
                    focused is not None
                    and _control_pid(focused) == root_pid
                    and usable(focused)
                ):
                    candidate = focused

            if candidate is None:
                best_score = -1
                for control in _walk(root):
                    if not usable(control):
                        continue
                    score = candidate_score(control, window_rect)
                    if score is not None and score > best_score:
                        candidate, best_score = control, score

            if candidate is None:
                return None
            return _control_signature(candidate, window_rect)
    except Exception:
        logger.debug("capture focused input failed", exc_info=True)
        return None


def capture_cursor_point() -> Optional[dict]:
    """记录模式：鼠标相对「指针下那个窗口」的位置比例。

    也按指针下的窗口算，用户不必先点一下目标应用把它切到最前面。
    """

    import ctypes
    import ctypes.wintypes

    hwnd = _window_at_cursor()
    if not hwnd or _window_pid(hwnd) == os.getpid():
        return None
    window = _rect_for_hwnd(hwnd)
    if window is None:
        return None
    point = ctypes.wintypes.POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        return None
    left, top, right, bottom = window
    width = max(right - left, 1.0)
    height = max(bottom - top, 1.0)
    return {
        "x": min(max((point.x - left) / width, 0.0), 1.0),
        "y": min(max((point.y - top) / height, 0.0), 1.0),
    }


def _matches_signature(control, signature: dict, window_rect: Optional[tuple]) -> bool:
    if not _passes_hard_gate(control):
        return False
    try:
        automation_id = str(control.AutomationId or "")
        class_name = str(control.ClassName or "")
        control_type = str(control.ControlTypeName or "")
    except Exception:
        return False
    wanted_id = str(signature.get("automation_id") or "")
    wanted_class = str(signature.get("class_name") or "")
    wanted_type = str(signature.get("control_type") or "")
    if wanted_type and control_type != wanted_type:
        return False
    if wanted_id:
        if automation_id != wanted_id:
            return False
    elif wanted_class:
        if class_name != wanted_class:
            return False
    else:
        return False
    rect = _rect(control)
    if rect is None or window_rect is None:
        return False
    win_left, win_top, win_right, win_bottom = window_rect
    width = max(win_right - win_left, 1.0)
    height = max(win_bottom - win_top, 1.0)
    center_x = ((rect[0] + rect[2]) / 2 - win_left) / width
    center_y = ((rect[1] + rect[3]) / 2 - win_top) / height
    wanted_x = float(signature.get("left", 0.5)) + float(signature.get("width", 0.0)) / 2
    wanted_y = float(signature.get("top", 0.5)) + float(signature.get("height", 0.0)) / 2
    return abs(center_x - wanted_x) <= 0.10 and abs(center_y - wanted_y) <= 0.10


def focus_learned_input(signature_json: str, timeout: float = _SCAN_TIMEOUT_SECONDS) -> bool:
    """按学习到的特征找回输入框并聚焦；找不到就返回 ``False``。"""

    import json

    import uiautomation as auto

    try:
        signature = json.loads(signature_json or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(signature, dict) or not signature:
        return False
    deadline = time.monotonic() + max(0.1, float(timeout))
    with auto.UIAutomationInitializerInThread():
        while time.monotonic() < deadline:
            try:
                hwnd = _foreground_hwnd()
                root = auto.ControlFromHandle(hwnd) if hwnd else None
                if root is not None:
                    window_rect = _rect(root)
                    for control in _walk(root):
                        if not _matches_signature(control, signature, window_rect):
                            continue
                        control.SetFocus()
                        time.sleep(0.05)
                        try:
                            if control.HasKeyboardFocus:
                                return True
                        except Exception:
                            pass
                        try:
                            focused = auto.GetFocusedControl()
                            if (
                                focused is not None
                                and focused.GetRuntimeId() == control.GetRuntimeId()
                            ):
                                return True
                        except Exception:
                            pass
            except Exception:
                logger.debug("learned input attempt failed", exc_info=True)
            time.sleep(_ATTEMPT_INTERVAL_SECONDS)
    return False


def execute_focus_action(action) -> bool:
    """执行「聚焦输入框」的某个模式（自动 / 学习 / 记录位置）。"""

    mode = str(getattr(action, "focus_mode", "auto") or "auto")
    if mode == "point":
        x, y = getattr(action, "focus_point", (0.5, 0.5))
        return click_recorded_point(x, y)
    if mode == "element":
        signature = str(getattr(action, "focus_signature", "") or "")
        if focus_learned_input(signature, timeout=1.2):
            return True
        # 目标应用改版/窗口布局变了时，学到的特征可能对不上；退回自动
        # 识别，至少别让这个按键完全没反应。
        return focus_foreground_input()
    return focus_foreground_input()


def request_focus_action(action, on_failure=None) -> None:
    """后台线程执行聚焦动作，避免阻塞按键回调。

    ``on_failure`` 在没能把焦点放进任何输入框时调用：Electron 应用
    （Trae、VS Code 等）默认不建无障碍树，扫不到控件时按键看上去完全
    没反应，界面层需要借此提示用户改用「记录点击位置」。
    """

    def _run() -> None:
        succeeded = False
        try:
            succeeded = bool(execute_focus_action(action))
        except Exception:
            logger.debug("focus action failed", exc_info=True)
        if succeeded or on_failure is None:
            return
        try:
            on_failure()
        except Exception:
            logger.debug("focus failure callback failed", exc_info=True)

    threading.Thread(
        target=_run, name="motedeck-focus-input", daemon=True
    ).start()
