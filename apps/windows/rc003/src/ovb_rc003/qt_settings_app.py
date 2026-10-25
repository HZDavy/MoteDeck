"""PySide6-Essentials + Qt Quick/QML settings window (XRBM-030/XRBM-031),
replacing the previous Tk view. Every validation/save/launch/log-status
decision below still goes straight through the same pure functions in
``settings_ui.py`` - this module only bridges them to QML via a
`QAbstractListModel` (``ButtonMappingModel``) and two `QObject`s
(``SettingsController``, ``DiagnosticsController``); it never duplicates
that logic.

This module deliberately does NOT import PySide6 at module import time.
``_load_qt_classes()`` performs that import lazily (and caches the result),
so merely importing ``ovb_rc003.qt_settings_app`` - e.g. transitively via
``settings_ui`` in a ``--dry-run`` smoke check, or from a pure test that
only needs ``remote_layout``/``shell_targets`` - never requires PySide6 to
be installed. ``run_settings_window()`` is the only real entry point, and
raises ``QtUnavailableError`` with an actionable message if PySide6-
Essentials is missing (source/dev runs only: the frozen build always bundles
the Qt runtime itself - see build/RemoteMicRC003.spec - so end users
never need to separately install Python or Qt).

``DiagnosticsController`` (XRBM-031's "检查与修复" fourth page) runs every
``windows_diagnostics.run_diagnostics()`` check on a plain background
``threading.Thread`` - never on the Qt GUI thread, so a slow WinRT/PortAudio
call can never freeze the window - and delivers the result back via a
cross-thread Qt signal (``Signal(object)``), which Qt automatically queues
onto the GUI thread because the receiving ``QObject`` was constructed there;
this is the standard, documented way to marshal a background-thread result
back to a Qt object living on a different thread, and needs no ``QThread``
subclass or extra locking. An ``_is_refreshing`` guard refuses to start a
second worker while one is already running (repeated "重新检测" clicks never
overlap).

Thread lifecycle at window close / process exit (XRBM-035, hardened again in
RETRY 1 - a real Windows CI crash, not merely a theoretical race,
superseded the previous "best-effort atexit join + daemon=True is good
enough" contract described here before): every background thread this
controller starts is tracked in ``_diagnostics_threads``, and
``_shutdown_diagnostics_workers()`` signals shutdown and makes a BOUNDED
join attempt on each (``_DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS`` per
thread - DERIVED from ``windows_diagnostics.BLE_DISCOVERY_MAX_CANCELLATION_
SECONDS`` plus a safety margin, not an independently-guessed value - see
that constant's own definition below). The critical fix is WHEN this runs:
``run_settings_window()`` now calls it EXPLICITLY, synchronously, from a
``try/finally`` that starts right after ``DiagnosticsController`` is
constructed (i.e. right after its background worker could first exist) and
covers every exit path through ``app.exec()`` returning, ``engine.load()``
raising, or ``rootObjects()`` coming back empty - while every Qt/Python
object it built is still fully alive - not only via this module's
``atexit`` hook (``_shutdown_qt_settings_app_at_exit()``, still registered
as a defense-in-depth safety net for callers that bypass
``run_settings_window()``). A real Windows CI faulthandler dump proved the
old atexit-only timing insufficient: a background BLE-discovery worker was
still deep inside a native WinRT await when the interpreter itself began
finalizing, producing an ``0xC0000005`` access violation - daemon=True only
guarantees CPython does not block exit on a surviving thread, it says
nothing about whether that thread's native call can safely keep running
concurrently with interpreter teardown.

RETRY 1's independent review found the round-1 fix for the discovery side
itself - cancelling the asyncio Task awaiting ``discover_candidates()`` -
was ALSO insufficient: the locked pywinrt wrapper's own post-cancel wait is
itself unbounded (see ``windows_diagnostics.py``'s "-- BLE candidate --"
section for the exact source citation), so an in-process asyncio
cancellation request could never give a real hard bound either. BLE
candidate discovery therefore now runs in a genuinely separate, disposable
OS PROCESS (``windows_diagnostics._run_ble_diagnostics_subprocess()``) that
the parent can forcibly terminate/kill and CONFIRM dead within a real,
OS-enforced bound - the shutdown event doubles as that cancellation signal
(not just an emit-skip flag - see below), so a discovery attempt in flight
when shutdown begins is actually asked to stop and confirmed to have
stopped, giving the bounded join here a realistic chance to succeed instead
of only ever timing out. Every one of these threads is still created with
``daemon=True`` too, as a last-resort backstop if a future failure mode
ever defeats the process-level isolation above.

Shutdown-vs-teardown ordering (XRBM-031 RETRY 2, still true under XRBM-035):
a diagnostics worker still finishing around shutdown time must never emit
its result INTO a ``DiagnosticsController``/Qt runtime that
``_release_qt_classes_cache()`` may already have started tearing down. Two
things make this safe: ``_diagnostics_shutdown_event`` (a module-level
``threading.Event``) is set FIRST, before anything else, by
``_shutdown_diagnostics_workers()`` - ``refreshDiagnostics()`` refuses to
start a new worker once it is set, and a worker already running checks it
immediately before emitting and skips the emit entirely if it is set (any
exception the emit call raises anyway - the receiver could still be
mid-teardown despite the check - is caught and discarded, never crashing
the worker thread); and every worker's ``finally`` block unconditionally
calls ``_forget_diagnostics_thread()``, so the registry is cleaned up even
on that path. ``_shutdown_qt_settings_app_at_exit()`` still runs all three
shutdown steps (flag the shutdown, join outstanding workers, release the Qt
classes cache) in that explicit order, in one function - never relying on
Python's ``atexit`` LIFO-ordered execution of separately registered
functions (see that function's docstring for the full story).
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass
import gc
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import (
    __version__,
    action_executor,
    remote_selection,
    app_icons,
    application_update,
    audio_playback,
    audio_output,
    bridge_control_windows,
    bridge_launcher,
    bridge_runtime_status,
    config,
    device_catalog,
    element_navigation_control_windows,
    element_navigation_runtime,
    frida_compat,
    hid_elevation_windows,
    hid_helper_consumers,
    hotkey,
    hotkey_capture_windows,
    key_detection_bridge,
    key_mapping,
    local_microphone_windows,
    app_profiles,
    log_export,
    logging_setup,
    product_identity,
    dev_session,
    remote_layout,
    raw_input_windows,
    resources,
    running_apps_windows,
    settings_ui,
    shell_targets,
    single_instance,
    startup_windows,
    vb_cable_bundle,
    win32_keys,
    window_chrome_windows,
    windows_diagnostics,
)

# 联网更新检查已开启：应用会按需访问 GitHub 查询新版本，启动后每天最多
# 静默检查一次；下载仍需用户手动触发，不会自动运行或覆盖当前安装。
APPLICATION_UPDATE_CHECK_ENABLED = True


# Only the physical microphone button can own RC003 audio. Other buttons use
# ordinary actions; secondary gestures are ordinary actions for every button.
_ORDINARY_PRIMARY_ACTION_OPTIONS: List[str] = list(
    dict.fromkeys(
        settings_ui._PRESET_KEY_COMBOS
    )
)
_SECONDARY_ACTION_OPTIONS: List[str] = list(
    dict.fromkeys(
        (settings_ui.SECONDARY_UNCONFIGURED_DISPLAY,)
        + tuple(
            option
            for option in settings_ui._PRESET_KEY_COMBOS
            if option != "禁用"
        )
    )
)

# 只有“使用其他按键切换模式”打开后，动作下拉里才出现这一项。
_VOICE_MODE_CYCLE_LABEL = settings_ui._REFERENCE_ACTION_LABELS[
    key_mapping.ActionKind.VOICE_MODE_CYCLE
]

_COMBO_MODIFIER_LABELS = {
    "tv": "TV",
    "menu": "菜单",
    "home": "主页",
}

class QtUnavailableError(RuntimeError):
    """Raised by run_settings_window() when PySide6-Essentials is not
    importable. See module docstring.
    """


def _apply_application_icon(app, window, icon_type, icon_path: Path) -> None:
    """Applies one state icon to Qt's default and the loaded native window."""

    icon = icon_type(str(icon_path))
    set_application_icon = getattr(app, "setWindowIcon", None)
    if callable(set_application_icon):
        set_application_icon(icon)
    set_window_icon = getattr(window, "setIcon", None)
    if callable(set_window_icon):
        set_window_icon(icon)


def _apply_application_identity(app) -> None:
    """Applies the shared user-visible name to Qt's process identity."""

    set_application_name = getattr(app, "setApplicationName", None)
    if callable(set_application_name):
        set_application_name(product_identity.DISPLAY_NAME)


_UI_LATIN_FAMILIES = (
    "Segoe UI Variable Text",
    "Segoe UI Variable Display",
    "Segoe UI",
    "Cascadia Mono",
    "Consolas",
)
_UI_CJK_FALLBACKS = ["Microsoft YaHei UI", "Microsoft YaHei"]


def _apply_ui_font_fallbacks(app) -> None:
    """Segoe UI has no CJK glyphs; without an explicit substitution Qt falls
    back to SimSun. Map every Latin UI family the QML uses to YaHei UI so
    Chinese text matches the design (font-family: Segoe UI, Microsoft YaHei UI).
    """

    try:
        from PySide6.QtGui import QFont
    except ImportError:
        return
    for family in _UI_LATIN_FAMILIES:
        QFont.insertSubstitutions(family, _UI_CJK_FALLBACKS)
    get_font = getattr(app, "font", None)
    set_font = getattr(app, "setFont", None)
    if not (callable(get_font) and callable(set_font)):
        return
    font = QFont(get_font())
    font.setFamilies(["Segoe UI Variable Text", "Segoe UI", *_UI_CJK_FALLBACKS])
    set_font(font)


def _connect_application_exit(app, controller) -> None:
    """Make full exit a host responsibility instead of relying only on QML."""

    controller.applicationExitReady.connect(app.quit)


def _qml_directory() -> Path:
    """Locates the ``qml/`` directory this module's QML files live in,
    mirroring resources.py's frozen-vs-source-checkout lookup: in a frozen
    (PyInstaller) build, ``build/RemoteMicRC003.spec`` collects the
    qml sources under ``ovb_rc003_qml`` inside the COLLECT output, which the
    bootloader exposes via ``sys._MEIPASS`` at runtime (see resources.py's
    module docstring for why ``sys._MEIPASS`` - not the exe's own directory
    - is the correct base path for bundled, non-Python data in a one-dir
    build). In an unfrozen (source checkout or ``pip install``) run, the
    qml/ directory is simply this module's own sibling directory.
    """

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "ovb_rc003_qml"
    return Path(__file__).resolve().parent / "qml"


def _settings_window_handle(window: object) -> int:
    if sys.platform != "win32":
        return 0
    try:
        hwnd = int(window.winId())  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return 0
    return hwnd if hwnd > 0 else 0


def _mark_settings_window_for_activation(
    window: object,
    *,
    hwnd: Optional[int] = None,
) -> bool:
    """Mark the real native QML window for duplicate-launch reactivation."""

    resolved_hwnd = _settings_window_handle(window) if hwnd is None else int(hwnd)
    if not resolved_hwnd:
        return False
    return single_instance.mark_settings_window(resolved_hwnd)


_qt_classes_cache: Optional[dict] = None

# Every background diagnostics worker thread DiagnosticsController.
# refreshDiagnostics() starts is appended here (and discarded once it
# finishes - see _forget_diagnostics_thread()), purely so tests/diagnostics
# can observe how many are currently tracked. This is NOT what makes thread
# cleanup safe - see module docstring's "Thread lifecycle at process exit"
# section (XRBM-031 RETRY 1 item 6): _join_diagnostics_threads_at_exit()
# below only ever makes a bounded, best-effort join attempt; the actual
# safety property (process exit is never blocked indefinitely) comes from
# every one of these threads being created with daemon=True.
_diagnostics_threads: "list[threading.Thread]" = []
_diagnostics_threads_lock = threading.Lock()

# Set exactly once, by _begin_diagnostics_shutdown() (only ever called from
# _shutdown_qt_settings_app_at_exit() in production - see that function's
# docstring), BEFORE anything else happens at process exit (XRBM-031 RETRY
# 2). refreshDiagnostics() refuses to start a new worker once this is set;
# a worker already running checks it immediately before emitting its result
# and skips the emit entirely if it is set, so a diagnostics result can
# never be delivered into a DiagnosticsController/Qt runtime that may
# already be mid-teardown. Tests that set this directly (rather than via
# _shutdown_qt_settings_app_at_exit()) MUST .clear() it afterward - it is
# process-global, persistent state, not per-test.
_diagnostics_shutdown_event = threading.Event()

# Cross-controller gate for the explicit active audio test. It prevents the
# connection page from saving a different endpoint or launching the bridge
# while the synthetic signal is in flight.
_vb_cable_test_active_event = threading.Event()

# Tracks the multi-step "detect and save CABLE Input" workflow across the
# DiagnosticsController and SettingsController. Full application exit waits
# for this event to clear so a late enumeration/preflight result cannot race
# Qt teardown or persist settings after exit has begun.
_driver_action_active_event = threading.Event()

# Safety margin on top of the longest diagnostics-child cancellation bound
# below. It covers the worker thread's own small amount of Python cleanup
# after the BLE or active-audio subprocess has been confirmed stopped.
_DIAGNOSTICS_THREAD_JOIN_SAFETY_MARGIN_SECONDS = 2.0

# Per-thread bound for the best-effort atexit join below - a module-level
# constant (rather than a literal inline) specifically so a test can lower
# it and prove the join is genuinely bounded/non-hanging without waiting
# out the real default (see tests/test_qt_settings_app.py).
#
# XRBM-035 RETRY 1 P1 #2: derive this from the subprocess layer's own
# cancellation constants, rather than an independent flat guess. The max()
# keeps both BLE discovery and the explicit VB-CABLE child inside the same
# bounded shutdown contract if either implementation changes later.
_VB_CABLE_BRIDGE_RECOVERY_SECONDS = (
    bridge_control_windows.DEFAULT_EXIT_TIMEOUT_SECONDS
    + bridge_launcher.DEFAULT_GRACE_CHECKS
    * bridge_launcher.DEFAULT_POLL_INTERVAL_SECONDS
)
_DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS = (
    max(
        windows_diagnostics.BLE_DISCOVERY_MAX_CANCELLATION_SECONDS,
        windows_diagnostics.VB_CABLE_LOOPBACK_MAX_CANCELLATION_SECONDS
        + _VB_CABLE_BRIDGE_RECOVERY_SECONDS,
        windows_diagnostics.OUTPUT_ENDPOINT_PREFLIGHT_MAX_CANCELLATION_SECONDS,
    )
    + _DIAGNOSTICS_THREAD_JOIN_SAFETY_MARGIN_SECONDS
)

# A cancelled input start can spend up to five seconds waiting for its native
# hook/window to become ready, then another five seconds stopping the Raw
# Input listener and HID tap. Endpoint preflight has its own process-level
# cancellation bound. Derive the shared join ceiling from the longer path and
# leave a small margin for Python cleanup before Qt objects are released.
_INPUT_WORKER_MAX_START_SECONDS = 5.0
_INPUT_WORKER_MAX_STOP_SECONDS = 5.0
_SETTINGS_BACKGROUND_JOIN_SAFETY_MARGIN_SECONDS = 2.0
_SETTINGS_BACKGROUND_JOIN_TIMEOUT_SECONDS = (
    max(
        _INPUT_WORKER_MAX_START_SECONDS + _INPUT_WORKER_MAX_STOP_SECONDS,
        windows_diagnostics.OUTPUT_ENDPOINT_PREFLIGHT_MAX_CANCELLATION_SECONDS,
    )
    + _SETTINGS_BACKGROUND_JOIN_SAFETY_MARGIN_SECONDS
)
_APPLICATION_EXIT_WAIT_SAFETY_MARGIN_SECONDS = 2.0
_APPLICATION_EXIT_WAIT_TIMEOUT_SECONDS = (
    _SETTINGS_BACKGROUND_JOIN_TIMEOUT_SECONDS
    + bridge_control_windows.DEFAULT_EXIT_TIMEOUT_SECONDS
    + _APPLICATION_EXIT_WAIT_SAFETY_MARGIN_SECONDS
)
_APPLICATION_EXIT_SAVE_WAIT_TIMEOUT_SECONDS = (
    windows_diagnostics.OUTPUT_ENDPOINT_PREFLIGHT_PROCESS_TIMEOUT_SECONDS
    + windows_diagnostics.OUTPUT_ENDPOINT_PREFLIGHT_MAX_CANCELLATION_SECONDS
    + _SETTINGS_BACKGROUND_JOIN_SAFETY_MARGIN_SECONDS
)
_APPLICATION_EXIT_POLL_INTERVAL_MS = 100
_BRIDGE_STATUS_STALE_AFTER_SECONDS = 20.0
_BRIDGE_STATUS_MISSING_AFTER_SECONDS = _BRIDGE_STATUS_STALE_AFTER_SECONDS
_BRIDGE_RECONNECT_BUSY_TIMEOUT_MS = 10_000


@dataclass(frozen=True)
class _VbCableTestWorkflowResult:
    loopback_result: object = None
    bridge_was_running: bool = False
    stop_error: str = ""
    restart_result: object = None
    restart_skipped_for_exit: bool = False


@dataclass(frozen=True)
class _InputStartResult:
    kind: str
    ok: bool
    message: str = ""
    hotkey_capture: object = None
    bridge_request: object = None
    listener: object = None
    tap: object = None
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class _InputStopResult:
    kind: str
    ok: bool
    message: str = ""
    hotkey_capture: object = None
    bridge_request: object = None
    listener: object = None
    tap: object = None


class _AnyEvent:
    """Read-only event view that is set when any source event is set."""

    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


def _remember_diagnostics_thread(thread: "threading.Thread") -> None:
    with _diagnostics_threads_lock:
        _diagnostics_threads.append(thread)


def _forget_diagnostics_thread(thread: "threading.Thread") -> None:
    with _diagnostics_threads_lock:
        if thread in _diagnostics_threads:
            _diagnostics_threads.remove(thread)


def _begin_diagnostics_shutdown() -> None:
    """Flags that process shutdown has begun - see
    ``_diagnostics_shutdown_event``'s own comment above and
    ``_shutdown_qt_settings_app_at_exit()``'s docstring below for the full
    ordering contract this is one step of. Idempotent (``Event.set()`` is
    always safe to call more than once); kept as its own tiny function
    purely so a test can call/assert on this ONE step in isolation from the
    join/cache-release steps that follow it.
    """

    _diagnostics_shutdown_event.set()


def _join_diagnostics_threads_at_exit() -> None:
    """Best-effort courtesy only - see module docstring's "Thread lifecycle
    at process exit" section. Never treat this function returning as proof
    every thread it attempted to join has actually stopped. Deliberately
    does NOT itself touch ``_diagnostics_shutdown_event`` (that is
    ``_begin_diagnostics_shutdown()``'s job, called separately, first, by
    ``_shutdown_qt_settings_app_at_exit()``) - kept orthogonal so tests can
    still call this function alone (as XRBM-031 RETRY 1's tests already do)
    without it leaving global shutdown state behind for later tests.
    """

    with _diagnostics_threads_lock:
        threads = list(_diagnostics_threads)
    for thread in threads:
        thread.join(timeout=_DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS)


def _release_qt_classes_cache() -> None:
    """Drops this module's cached dynamically-created
    ``QObject``/``QAbstractListModel`` subclasses and asks the cyclic
    garbage collector to reclaim them while the interpreter is still fully
    alive, rather than leaving that to CPython's own shutdown-time
    finalization pass. Only ever called from
    ``_shutdown_qt_settings_app_at_exit()`` below (see that function's
    docstring for why it must run LAST, after diagnostics shutdown/join).

    Root cause this works around: ``PySide6.QtCore.Property`` descriptors
    hold a reference cycle back to their owning class that shiboken's C
    extension type does not fully support ``tp_clear`` for - reproduced
    with a minimal, completely unrelated repro (a single trivial
    ``Property``-having ``QObject`` subclass, left referenced at module
    scope with no closures, no caching, nothing else from this project
    involved). ``gc.collect()`` called explicitly *before* shutdown resolves
    that same cycle cleanly; the identical cycle left for ``Py_FinalizeEx``'s
    own final collection pass instead prints
    ``ResourceWarning: gc: N uncollectable objects at shutdown`` - a
    substring this project's ``windows-rc003-ci.yml`` test-suite step
    explicitly greps for and fails the build on (see
    tests/test_qt_lifecycle_cleanup.py for the subprocess-based regression
    proving this exact command stays clean).

    A deliberate no-op whenever ``_load_qt_classes()`` was never actually
    called in this process (the cache is still ``None``), which covers
    every non-Qt test process and ``--dry-run``.
    """

    global _qt_classes_cache
    if _qt_classes_cache is None:
        return
    _qt_classes_cache = None
    gc.collect()


def _shutdown_diagnostics_workers() -> None:
    """The ONE production shutdown contract for in-flight diagnostics
    workers (XRBM-035): flag shutdown, THEN bounded-wait for every tracked
    worker thread - steps 1+2 of ``_shutdown_qt_settings_app_at_exit()``'s
    three steps, factored out into their own function so both
    ``run_settings_window()`` (called explicitly right after ``app.exec()``
    returns, while every Qt/Python object it built is still fully alive -
    see that function) and the real QML load probe
    (``tests/test_qt_settings_app.py``'s ``_QML_LOAD_PROBE_SCRIPT``, which
    reproduces a settings window closing before a real background BLE
    discovery finishes) call the EXACT SAME helper, instead of each
    reimplementing shutdown or relying solely on this module's ``atexit``
    hook.

    Why this matters (XRBM-034 REPLAN, XRBM-035 red evidence): a Windows CI
    run's faulthandler dump showed the real crash thread still deep inside
    ``ble_transport_winrt.discover_candidates()``'s WinRT
    ``find_all_async_aqs_filter`` await, called from a
    ``DiagnosticsController`` background worker, well after this module's
    own ``atexit``-only join had already returned (its 2-second best-effort
    bound elapsing without the thread actually stopping) - by the time the
    interpreter itself began finalizing, that native WinRT call was still
    running concurrently with CPython/shiboken teardown, producing the
    observed ``0xC0000005`` access violation. Calling this function
    EXPLICITLY, synchronously, at the natural point the window is closing
    (not only via ``atexit``, which can fire arbitrarily late relative to
    Qt/native object teardown) gives every in-flight BLE discovery a real,
    bounded chance to be forcibly terminated and CONFIRMED dead at the OS
    process level (see
    ``windows_diagnostics._run_ble_diagnostics_subprocess()``) before that
    teardown ever begins - not merely a best-effort join on a thread nothing
    ever asked to stop.

    Idempotent - ``_begin_diagnostics_shutdown()`` is (``Event.set()`` is
    always safe to call more than once) and ``_join_diagnostics_threads_at_
    exit()`` is (an empty/already-finished registry joins instantly) - safe
    to call once here and again later via ``_shutdown_qt_settings_app_at_
    exit()`` as a defense-in-depth safety net for any path that does not go
    through ``run_settings_window()``.
    """

    _begin_diagnostics_shutdown()
    _join_diagnostics_threads_at_exit()


def _shutdown_qt_settings_app_at_exit() -> None:
    """The ONLY function this module registers via ``atexit`` (XRBM-031
    RETRY 2). Runs every shutdown step in one explicit, hard-coded order:

    1.-2. ``_shutdown_diagnostics_workers()`` - flags shutdown, then makes a
       bounded join attempt on every tracked worker thread (see that
       function's own docstring for why this is also called explicitly by
       ``run_settings_window()``, not only reached here);
    3. ``_release_qt_classes_cache()`` - only now, after every worker has
       either finished or had its bounded join time out, release the
       cached Qt classes.

    Why this is ONE function instead of separately ``atexit.register()``-ing
    each step (which is what the original XRBM-031 submission and its RETRY
    1 fix both did): ``atexit`` runs its registered functions in LIFO order
    (last registered, first executed) - registering the join hook first and
    the cache-release hook second, as before, meant the cache release
    actually ran FIRST at real process exit, the reverse of the order this
    module's own docstring already claimed. A diagnostics worker finishing
    right around that window could then emit its result into a
    ``DiagnosticsController``/Qt runtime whose cached classes were already
    being torn down. Collapsing every step into one function and
    registering that ONE function removes the dependency on ``atexit``'s
    LIFO ordering (and the silent-breakage risk of some future edit
    reordering two separate ``atexit.register()`` calls) entirely - the
    order is just ordinary, explicit Python statement order here.

    This remains registered as a defense-in-depth safety net (e.g. for a
    caller that never reaches ``run_settings_window()``'s own explicit
    call) - production windows must never depend on ``atexit`` alone; see
    ``_shutdown_diagnostics_workers()``'s docstring for why.
    """

    _shutdown_diagnostics_workers()
    _release_qt_classes_cache()


atexit.register(_shutdown_qt_settings_app_at_exit)


def _load_qt_classes() -> dict:
    """Imports PySide6 and defines every QObject/QAbstractListModel
    subclass this module needs, INSIDE this function body - not at module
    level - so that importing ``qt_settings_app`` itself never requires
    PySide6 (see module docstring). Cached after the first successful call
    within a process; a missing-PySide6 failure is never cached, so a
    caller that installs PySide6 into the same running process (unlikely in
    practice, but exercised by tests) would see a subsequent call succeed.
    """

    global _qt_classes_cache
    if _qt_classes_cache is not None:
        return _qt_classes_cache

    try:
        from PySide6.QtCore import (
            Property,
            QAbstractListModel,
            QByteArray,
            QEvent,
            QModelIndex,
            QObject,
            QStandardPaths,
            QTimer,
            Qt,
            QUrl,
            Signal,
            Slot,
        )
        from PySide6.QtGui import QIcon
        from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterSingletonInstance
        from PySide6.QtQuickControls2 import QQuickStyle
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        raise QtUnavailableError(
            "PySide6-Essentials 未安装，无法打开 Qt 设置界面。源码运行请先在本项目"
            "的虚拟环境中执行 `pip install -r requirements.txt`（已包含 "
            f"PySide6-Essentials）；打包后的 {product_identity.windows_executable_name(__version__)} 自带 Qt 运行"
            "时，不需要终端用户单独安装 Python 或 Qt。"
        ) from exc

    _DisplayRole = Qt.ItemDataRole.DisplayRole
    _UserRole = Qt.ItemDataRole.UserRole

    _QT_MODIFIER_KEY_TOKENS = {
        Qt.Key.Key_Control.value: "ctrl",
        Qt.Key.Key_Shift.value: "shift",
        Qt.Key.Key_Alt.value: "alt",
        Qt.Key.Key_AltGr.value: "alt",
        Qt.Key.Key_Meta.value: "win",
        Qt.Key.Key_Super_L.value: "win",
        Qt.Key.Key_Super_R.value: "win",
    }
    _QT_MODIFIER_FLAG_TOKENS = (
        (Qt.KeyboardModifier.ControlModifier.value, "ctrl"),
        (Qt.KeyboardModifier.ShiftModifier.value, "shift"),
        (Qt.KeyboardModifier.AltModifier.value, "alt"),
        (Qt.KeyboardModifier.MetaModifier.value, "win"),
    )
    _QT_SPECIAL_KEY_TOKENS = {
        Qt.Key.Key_Backspace.value: "backspace",
        Qt.Key.Key_Tab.value: "tab",
        Qt.Key.Key_Backtab.value: "tab",
        Qt.Key.Key_Return.value: "enter",
        Qt.Key.Key_Enter.value: "numpad_enter",
        Qt.Key.Key_Escape.value: "escape",
        Qt.Key.Key_Space.value: "space",
        Qt.Key.Key_PageUp.value: "page_up",
        Qt.Key.Key_PageDown.value: "page_down",
        Qt.Key.Key_End.value: "end",
        Qt.Key.Key_Home.value: "home",
        Qt.Key.Key_Left.value: "left",
        Qt.Key.Key_Up.value: "up",
        Qt.Key.Key_Right.value: "right",
        Qt.Key.Key_Down.value: "down",
        Qt.Key.Key_Insert.value: "insert",
        Qt.Key.Key_Delete.value: "delete",
        Qt.Key.Key_Menu.value: "apps",
        Qt.Key.Key_CapsLock.value: "caps_lock",
        Qt.Key.Key_NumLock.value: "num_lock",
        Qt.Key.Key_ScrollLock.value: "scroll_lock",
        Qt.Key.Key_Print.value: "print_screen",
        Qt.Key.Key_Pause.value: "pause",
        Qt.Key.Key_Back.value: "browser_back",
        Qt.Key.Key_Forward.value: "browser_forward",
        Qt.Key.Key_MediaNext.value: "media_next",
        Qt.Key.Key_MediaPrevious.value: "media_previous",
        Qt.Key.Key_MediaStop.value: "media_stop",
        Qt.Key.Key_MediaPlay.value: "media_play_pause",
        Qt.Key.Key_VolumeMute.value: "volume_mute",
        Qt.Key.Key_VolumeDown.value: "volume_down",
        Qt.Key.Key_VolumeUp.value: "volume_up",
    }
    _QT_KEYPAD_TOKENS = {
        Qt.Key.Key_Plus.value: "numpad_add",
        Qt.Key.Key_Minus.value: "numpad_subtract",
        Qt.Key.Key_Asterisk.value: "numpad_multiply",
        Qt.Key.Key_Slash.value: "numpad_divide",
        Qt.Key.Key_Period.value: "numpad_decimal",
        Qt.Key.Key_Comma.value: "numpad_decimal",
        Qt.Key.Key_Enter.value: "numpad_enter",
        Qt.Key.Key_Return.value: "numpad_enter",
        Qt.Key.Key_Clear.value: "numpad_clear",
        Qt.Key.Key_PageUp.value: "numpad_page_up",
        Qt.Key.Key_PageDown.value: "numpad_page_down",
        Qt.Key.Key_End.value: "numpad_end",
        Qt.Key.Key_Home.value: "numpad_home",
        Qt.Key.Key_Left.value: "numpad_left",
        Qt.Key.Key_Up.value: "numpad_up",
        Qt.Key.Key_Right.value: "numpad_right",
        Qt.Key.Key_Down.value: "numpad_down",
        Qt.Key.Key_Insert.value: "numpad_insert",
        Qt.Key.Key_Delete.value: "numpad_delete",
    }

    def _qt_hotkey_token(
        key: int,
        text: str,
        modifiers: int,
        *,
        native_virtual_key: int = 0,
        native_scan_code: int = 0,
    ) -> str:
        key = int(key)
        # A forwarded hook-health probe can also reach Qt. Qt does not retain
        # LLKHF_INJECTED; VK_NONAME is reserved and cannot be a user key here.
        if native_virtual_key == hotkey_capture_windows.VK_NONAME:
            return ""
        keypad_token = (_QT_KEYPAD_TOKENS.get(key)
                        if modifiers & Qt.KeyboardModifier.KeypadModifier.value else None)
        if keypad_token in win32_keys.UNSUPPORTED_KEY_MESSAGES:
            return keypad_token
        # Native identity survives Ctrl control characters, IMEs and layouts.
        # The same decoder is used by the low-level hook and the Qt fallback.
        if sys.platform == "win32" and native_virtual_key:
            flags = (hotkey_capture_windows.LLKHF_EXTENDED
                     if native_scan_code & 0xFF00 == 0xE000 else 0)
            if native_virtual_key == 0x0D and (key == Qt.Key.Key_Enter.value
                                            or modifiers & Qt.KeyboardModifier.KeypadModifier.value):
                flags |= hotkey_capture_windows.LLKHF_EXTENDED
            return hotkey_capture_windows.token_for_keyboard_event(
                native_virtual_key, native_scan_code, flags
            )
        if modifiers & Qt.KeyboardModifier.KeypadModifier.value:
            if keypad_token is not None:
                return keypad_token
            if Qt.Key.Key_0.value <= key <= Qt.Key.Key_9.value:
                return f"numpad{chr(key)}"
            return "unsupported_key"
        token = _QT_MODIFIER_KEY_TOKENS.get(key) or _QT_SPECIAL_KEY_TOKENS.get(key)
        if token is not None:
            return token
        if Qt.Key.Key_F1.value <= key <= Qt.Key.Key_F24.value:
            return f"f{key - Qt.Key.Key_F1.value + 1}"
        if Qt.Key.Key_0.value <= key <= Qt.Key.Key_9.value:
            return chr(key)
        if Qt.Key.Key_A.value <= key <= Qt.Key.Key_Z.value:
            return chr(key).lower()
        if 0 <= key <= 0x10ffff:
            symbol = chr(key)
            token = win32_keys.KEY_SYMBOL_TOKENS.get(symbol)
            if token is None and modifiers & Qt.KeyboardModifier.ShiftModifier.value:
                token = win32_keys.KEY_SHIFTED_SYMBOL_TOKENS.get(symbol)
            if token is not None:
                return token
        # Retain an unrecognized edge so a chord cannot silently lose a key.
        return "unsupported_key"

    class ButtonMappingModel(QAbstractListModel):
        """One row per physical RC003 button (13 total, in
        remote_layout.BUTTON_ORDER - 12 ordinary HID buttons plus mic),
        exposing its identity, current mapping texts and shared selected
        state to the full-width QML mapping matrix. Hotspot roles remain for
        compatibility with older consumers of the model, but the current
        settings page uses the matrix as its sole RC003 mapping view.
        """

        ButtonIdRole = _UserRole + 1
        DisplayNameRole = _UserRole + 2
        HidUsageRole = _UserRole + 3
        ActionTextRole = _UserRole + 4
        DoubleClickTextRole = _UserRole + 5
        LongPressTextRole = _UserRole + 6
        IsMicRole = _UserRole + 7
        IsSelectedRole = _UserRole + 8
        XRole = _UserRole + 9
        YRole = _UserRole + 10
        WidthRole = _UserRole + 11
        HeightRole = _UserRole + 12
        IsVoiceRole = _UserRole + 13
        SingleNoteRole = _UserRole + 14
        DoubleNoteRole = _UserRole + 15
        LongNoteRole = _UserRole + 16
        InjectModeRole = _UserRole + 17

        # Emitted whenever a QML combo box edits a row's action text
        # (button_id, new display text) - SettingsController does not need
        # this directly (it reads the model back at save time via
        # to_display_map()), but it is kept for any future listener/test.
        actionEdited = Signal(str, str)
        mappingEdited = Signal()

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self._profile = device_catalog.RC003_ID
            self._initialize_rows()

        def set_profile(self, profile: str) -> None:
            if profile == self._profile:
                return
            self.beginResetModel()
            self._profile = profile
            self._initialize_rows()
            self.endResetModel()

        def _initialize_rows(self) -> None:
            self._button_ids: List[str] = list(remote_layout.button_order(self._profile))
            self._action_text: Dict[str, str] = {bid: "" for bid in self._button_ids}
            self._secondary_action_text: Dict[str, Dict[str, str]] = {
                bid: {
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value: (
                        settings_ui.SECONDARY_UNCONFIGURED_DISPLAY
                    ),
                    key_mapping.ButtonTrigger.LONG_PRESS.value: (
                        settings_ui.SECONDARY_UNCONFIGURED_DISPLAY
                    ),
                }
                for bid in self._button_ids
            }
            self._display_notes: Dict[str, Dict[str, str]] = {
                bid: {
                    key_mapping.ButtonTrigger.SINGLE_CLICK.value: "",
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value: "",
                    key_mapping.ButtonTrigger.LONG_PRESS.value: "",
                }
                for bid in self._button_ids
            }
            self._inject_modes: Dict[str, str] = {
                bid: key_mapping.DEFAULT_INJECT_MODE for bid in self._button_ids
            }
            self._selected_button_id: str = "ok"

        def rowCount(self, parent=QModelIndex()) -> int:  # noqa: B008 - QML model convention
            if parent.isValid():
                return 0
            return len(self._button_ids)

        def roleNames(self):
            return {
                self.ButtonIdRole: QByteArray(b"buttonId"),
                self.DisplayNameRole: QByteArray(b"displayName"),
                self.HidUsageRole: QByteArray(b"hidUsage"),
                self.ActionTextRole: QByteArray(b"actionText"),
                self.DoubleClickTextRole: QByteArray(b"doubleClickText"),
                self.LongPressTextRole: QByteArray(b"longPressText"),
                self.IsMicRole: QByteArray(b"isMic"),
                self.IsSelectedRole: QByteArray(b"isSelected"),
                self.XRole: QByteArray(b"hotspotX"),
                self.YRole: QByteArray(b"hotspotY"),
                self.WidthRole: QByteArray(b"hotspotWidth"),
                self.HeightRole: QByteArray(b"hotspotHeight"),
                self.IsVoiceRole: QByteArray(b"isVoice"),
                self.SingleNoteRole: QByteArray(b"singleNote"),
                self.DoubleNoteRole: QByteArray(b"doubleNote"),
                self.LongNoteRole: QByteArray(b"longNote"),
                self.InjectModeRole: QByteArray(b"injectMode"),
            }

        def data(self, index, role: int = _DisplayRole):
            if not index.isValid() or not (0 <= index.row() < len(self._button_ids)):
                return None
            button_id = self._button_ids[index.row()]
            hotspot = remote_layout.hotspot_for(button_id, self._profile)
            if role in (self.ButtonIdRole, _DisplayRole):
                return button_id
            if role == self.DisplayNameRole:
                return remote_layout.display_names(self._profile)[button_id]
            if role == self.HidUsageRole:
                return remote_layout.hid_usage_display(button_id)
            if role == self.ActionTextRole:
                return settings_ui.format_action_text(self._action_text[button_id])
            if role == self.DoubleClickTextRole:
                return settings_ui.format_action_text(self._secondary_action_text[button_id][
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value
                ])
            if role == self.LongPressTextRole:
                return settings_ui.format_action_text(self._secondary_action_text[button_id][
                    key_mapping.ButtonTrigger.LONG_PRESS.value
                ])
            if role == self.IsMicRole:
                return button_id == "mic"
            if role == self.IsSelectedRole:
                return button_id == self._selected_button_id
            if role == self.XRole:
                return hotspot.x if hotspot else 0.0
            if role == self.YRole:
                return hotspot.y if hotspot else 0.0
            if role == self.WidthRole:
                return hotspot.width if hotspot else 0.0
            if role == self.HeightRole:
                return hotspot.height if hotspot else 0.0
            if role == self.IsVoiceRole:
                return bool(hotspot and hotspot.is_voice)
            if role == self.SingleNoteRole:
                return self._display_notes[button_id][
                    key_mapping.ButtonTrigger.SINGLE_CLICK.value
                ]
            if role == self.DoubleNoteRole:
                return self._display_notes[button_id][
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value
                ]
            if role == self.LongNoteRole:
                return self._display_notes[button_id][
                    key_mapping.ButtonTrigger.LONG_PRESS.value
                ]
            if role == self.InjectModeRole:
                return self._inject_modes[button_id]
            return None

        def load_display_map(
            self,
            display_map: Dict[str, str],
            secondary_display_map: Optional[Dict[str, Dict[str, str]]] = None,
            display_note_map: Optional[Dict[str, Dict[str, str]]] = None,
        ) -> None:
            """Apply persisted values without rebuilding QML delegates.

            The row identity/order never changes. Emitting only the roles
            whose text changed keeps cards, photo hotspots, selected state,
            and connector canvases alive across a save/reload.
            """

            for row, button_id in enumerate(self._button_ids):
                changed_roles = []
                primary_text = display_map.get(button_id, "")
                if primary_text != self._action_text[button_id]:
                    self._action_text[button_id] = primary_text
                    changed_roles.append(self.ActionTextRole)
                trigger_map = (secondary_display_map or {}).get(button_id, {})
                next_secondary = {
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value: trigger_map.get(
                        key_mapping.ButtonTrigger.DOUBLE_CLICK.value,
                        settings_ui.SECONDARY_UNCONFIGURED_DISPLAY,
                    ),
                    key_mapping.ButtonTrigger.LONG_PRESS.value: trigger_map.get(
                        key_mapping.ButtonTrigger.LONG_PRESS.value,
                        settings_ui.SECONDARY_UNCONFIGURED_DISPLAY,
                    ),
                }
                current_secondary = self._secondary_action_text[button_id]
                if (
                    next_secondary[key_mapping.ButtonTrigger.DOUBLE_CLICK.value]
                    != current_secondary[key_mapping.ButtonTrigger.DOUBLE_CLICK.value]
                ):
                    changed_roles.append(self.DoubleClickTextRole)
                if (
                    next_secondary[key_mapping.ButtonTrigger.LONG_PRESS.value]
                    != current_secondary[key_mapping.ButtonTrigger.LONG_PRESS.value]
                ):
                    changed_roles.append(self.LongPressTextRole)
                self._secondary_action_text[button_id] = next_secondary
                raw_notes = (display_note_map or {}).get(button_id, {})
                next_notes = {
                    key_mapping.ButtonTrigger.SINGLE_CLICK.value: str(
                        raw_notes.get(
                            key_mapping.ButtonTrigger.SINGLE_CLICK.value, ""
                        )
                    ),
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value: str(
                        raw_notes.get(
                            key_mapping.ButtonTrigger.DOUBLE_CLICK.value, ""
                        )
                    ),
                    key_mapping.ButtonTrigger.LONG_PRESS.value: str(
                        raw_notes.get(
                            key_mapping.ButtonTrigger.LONG_PRESS.value, ""
                        )
                    ),
                }
                current_notes = self._display_notes[button_id]
                note_roles = {
                    key_mapping.ButtonTrigger.SINGLE_CLICK.value: self.SingleNoteRole,
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value: self.DoubleNoteRole,
                    key_mapping.ButtonTrigger.LONG_PRESS.value: self.LongNoteRole,
                }
                for trigger, role in note_roles.items():
                    if next_notes[trigger] != current_notes[trigger]:
                        changed_roles.append(role)
                self._display_notes[button_id] = next_notes
                if changed_roles:
                    model_index = self.index(row, 0)
                    self.dataChanged.emit(model_index, model_index, changed_roles)

        def to_display_map(self) -> Dict[str, str]:
            """Inverse of load_display_map() for build_save_model()."""

            return dict(self._action_text)

        def to_secondary_display_map(self) -> Dict[str, Dict[str, str]]:
            return {
                button_id: {
                    trigger: (
                        ""
                        if text == settings_ui.SECONDARY_UNCONFIGURED_DISPLAY
                        else text
                    )
                    for trigger, text in trigger_map.items()
                }
                for button_id, trigger_map in self._secondary_action_text.items()
            }

        def to_display_note_map(self) -> Dict[str, Dict[str, str]]:
            return {
                button_id: {
                    trigger: note.strip()
                    for trigger, note in trigger_map.items()
                    if note.strip()
                }
                for button_id, trigger_map in self._display_notes.items()
                if any(note.strip() for note in trigger_map.values())
            }

        def load_inject_modes(
            self, inject_modes: Optional[Dict[str, str]]
        ) -> None:
            """Apply persisted per-button injection backends to the rows.

            Like load_display_map() this only refreshes the roles whose value
            changed, so the QML delegates stay alive across a save/reload.
            """

            entries = inject_modes if isinstance(inject_modes, dict) else {}
            for row, button_id in enumerate(self._button_ids):
                mode = key_mapping.normalize_stored_inject_mode(
                    entries.get(button_id)
                )
                if mode == self._inject_modes[button_id]:
                    continue
                self._inject_modes[button_id] = mode
                model_index = self.index(row, 0)
                self.dataChanged.emit(
                    model_index, model_index, [self.InjectModeRole]
                )

        def to_inject_mode_map(self) -> Dict[str, str]:
            """Inverse of load_inject_modes() for build_save_model()."""

            return dict(self._inject_modes)

        def inject_mode_for(self, button_id: str) -> str:
            return self._inject_modes.get(
                str(button_id), key_mapping.DEFAULT_INJECT_MODE
            )

        @Slot(int, str)
        def setInjectModeAt(self, row: int, mode: str) -> None:
            if not (0 <= row < len(self._button_ids)):
                return
            button_id = self._button_ids[row]
            normalized = key_mapping.normalize_stored_inject_mode(mode)
            if normalized == self._inject_modes[button_id]:
                return
            self._inject_modes[button_id] = normalized
            model_index = self.index(row, 0)
            self.dataChanged.emit(model_index, model_index, [self.InjectModeRole])
            self.mappingEdited.emit()

        def index_of(self, button_id: str) -> int:
            try:
                return self._button_ids.index(button_id)
            except ValueError:
                return -1

        @Slot(str, result=int)
        def indexOfButton(self, button_id: str) -> int:
            return self.index_of(button_id)

        @Slot(int, str)
        def setActionTextAt(self, row: int, text: str) -> None:
            if not (0 <= row < len(self._button_ids)):
                return
            button_id = self._button_ids[row]
            original = self._action_text[button_id]
            if settings_ui.action_text_matches_original(text, original):
                return
            text = settings_ui.normalize_action_text(text)
            if text == self._action_text[button_id]:
                return
            self._action_text[button_id] = text
            model_index = self.index(row, 0)
            self.dataChanged.emit(model_index, model_index, [self.ActionTextRole])
            self.actionEdited.emit(button_id, text)
            self.mappingEdited.emit()

        @Slot(int, str, str)
        def setSecondaryActionTextAt(self, row: int, trigger: str, text: str) -> None:
            if not (0 <= row < len(self._button_ids)):
                return
            if trigger not in {
                key_mapping.ButtonTrigger.DOUBLE_CLICK.value,
                key_mapping.ButtonTrigger.LONG_PRESS.value,
            }:
                return
            button_id = self._button_ids[row]
            original = self._secondary_action_text[button_id][trigger]
            if settings_ui.action_text_matches_original(text, original):
                return
            text = settings_ui.normalize_action_text(text)
            if text == self._secondary_action_text[button_id][trigger]:
                return
            self._secondary_action_text[button_id][trigger] = text
            model_index = self.index(row, 0)
            role = (
                self.DoubleClickTextRole
                if trigger == key_mapping.ButtonTrigger.DOUBLE_CLICK.value
                else self.LongPressTextRole
            )
            self.dataChanged.emit(model_index, model_index, [role])
            self.mappingEdited.emit()

        def relabel_voice_actions(
            self,
            old_labels: Dict[int, str],
            new_labels: Dict[int, str],
        ) -> None:
            """Swap renamed voice keys' display text on the button page."""

            for row, button_id in enumerate(self._button_ids):
                text = self._action_text[button_id]
                for slot, old_label in old_labels.items():
                    if text != old_label:
                        continue
                    new_label = new_labels.get(slot)
                    if new_label and new_label != text:
                        self._action_text[button_id] = new_label
                        model_index = self.index(row, 0)
                        self.dataChanged.emit(
                            model_index, model_index, [self.ActionTextRole]
                        )
                    break

        def clear_voice_action(self, slot: int, label: str = "") -> None:
            """Reset buttons bound to a deleted voice key back to 禁用."""

            changed = False
            for row, button_id in enumerate(self._button_ids):
                text = self._action_text[button_id]
                matched = settings_ui.voice_slot_for_display(text) == int(slot)
                if not matched and label and text == label:
                    matched = True
                if not matched:
                    continue
                self._action_text[button_id] = "禁用"
                model_index = self.index(row, 0)
                self.dataChanged.emit(model_index, model_index, [self.ActionTextRole])
                changed = True
            if changed:
                self.mappingEdited.emit()

        @Slot(int, str, str)
        def setDisplayNoteAt(self, row: int, trigger: str, text: str) -> None:
            if not (0 <= row < len(self._button_ids)):
                return
            valid_roles = {
                key_mapping.ButtonTrigger.SINGLE_CLICK.value: self.SingleNoteRole,
                key_mapping.ButtonTrigger.DOUBLE_CLICK.value: self.DoubleNoteRole,
                key_mapping.ButtonTrigger.LONG_PRESS.value: self.LongNoteRole,
            }
            if trigger not in valid_roles:
                return
            button_id = self._button_ids[row]
            clean_text = str(text).strip()
            if clean_text == self._display_notes[button_id][trigger]:
                return
            self._display_notes[button_id][trigger] = clean_text
            model_index = self.index(row, 0)
            self.dataChanged.emit(
                model_index, model_index, [valid_roles[trigger]]
            )
            self.mappingEdited.emit()

        def set_selected_button(self, button_id: str) -> None:
            if button_id == self._selected_button_id or button_id not in self._action_text:
                return
            old_row = self.index_of(self._selected_button_id)
            self._selected_button_id = button_id
            new_row = self.index_of(button_id)
            for row in (old_row, new_row):
                if row >= 0:
                    model_index = self.index(row, 0)
                    self.dataChanged.emit(model_index, model_index, [self.IsSelectedRole])

        def selected_button_id(self) -> str:
            return self._selected_button_id

    class SettingsController(QObject):
        """QML-facing adapter over settings_ui.py's pure functions plus
        config.py/audio_output.py/bridge_launcher.py/logging_setup.py/
        shell_targets.py - every slot below is a thin wrapper that performs
        no validation or business logic of its own.
        """

        mappingPresetsChanged = Signal()
        mappingEditTargetChanged = Signal()
        mappingAppProfilesChanged = Signal()
        runningApplicationsChanged = Signal()
        hotkeyTextChanged = Signal()
        holdVoiceHotkeyTextChanged = Signal()
        voiceSlotHotkeysChanged = Signal()
        voiceKeyConfigsChanged = Signal()
        voiceInputSourceChanged = Signal()
        voiceShortPressDismissChanged = Signal()
        voiceCycleConfigChanged = Signal()
        localMicrophoneOptionsChanged = Signal()
        localMicrophoneDeviceChanged = Signal()
        microphoneLevelChanged = Signal()
        injectModesChanged = Signal()
        endpointOptionsChanged = Signal()
        recommendedEndpointIndexChanged = Signal()
        selectedEndpointIndexChanged = Signal()
        outputEndpointPersisted = Signal()
        bridgeRunningChanged = Signal()
        bridgeConnectedChanged = Signal()
        bridgeConnectionStateChanged = Signal()
        bridgeInputStateChanged = Signal()
        remoteBatteryChanged = Signal()
        voiceRuntimeStatusChanged = Signal()
        bridgeLaunchPhaseChanged = Signal()
        bridgeLaunchElapsedSecondsChanged = Signal()
        bridgeRestartRecommendedChanged = Signal()
        bridgeReconnectAvailableChanged = Signal()
        bridgeReconnectBusyChanged = Signal()
        desktopBehaviorChanged = Signal()
        trayStateChanged = Signal()
        maintenanceExitRequested = Signal()
        windowRestoreRequested = Signal()
        applicationExitReady = Signal()
        applicationExitFailed = Signal(str)
        windowHideReady = Signal()
        windowHideFailed = Signal(str)
        launchStatusTextChanged = Signal()
        statusMessageChanged = Signal()
        errorMessageChanged = Signal()
        settingsDirtyChanged = Signal()
        mappingDirtyChanged = Signal()
        settingsSaveBusyChanged = Signal()
        mappingAutoSaveRetryAvailableChanged = Signal()
        activePageIndexChanged = Signal()
        feedbackPageIndexChanged = Signal()
        selectedButtonIdChanged = Signal()
        selectedDeviceIndexChanged = Signal()
        selectedDeviceChanged = Signal()
        remoteSelectionChanged = Signal()
        _remoteDevicesReady = Signal(object)
        _remoteSwitchReady = Signal(object)
        voiceHotkeyBusyChanged = Signal()
        voiceHotkeySaveStateChanged = Signal()
        voiceHotkeySourceChanged = Signal()
        voiceMappingReadyChanged = Signal()
        endpointPreflightBusyChanged = Signal()
        hidHelperStateChanged = Signal()
        hidHelperRepairBusyChanged = Signal()
        keyDetectionActiveChanged = Signal()
        keyDetectionTextChanged = Signal()
        hotkeyCaptureActiveChanged = Signal()
        inputOperationChanged = Signal()
        _rawKeyDetected = Signal(str, str)
        _hidTapDetectionStatus = Signal(str, str)
        hotkeyCaptured = Signal(str)
        hotkeyCaptureError = Signal(str)
        inputCleanupReady = Signal()
        inputCleanupFailed = Signal(str)
        saveSettingsAndExitFinished = Signal(bool)
        applicationUpdateChanged = Signal()
        applicationUpdateDialogRequested = Signal()
        applicationUpdateNotificationRequested = Signal(str)
        logExportBusyChanged = Signal()
        _logExportReady = Signal(object)
        _hotkeyCaptureResult = Signal(object)
        _hotkeyCaptureFailed = Signal(object)
        _endpointOptionsRefreshReady = Signal(object)
        _endpointPreflightReady = Signal(object)
        _hidHelperRepairReady = Signal(object)
        _hidHelperRemovalReady = Signal(object)
        _inputOperationReady = Signal(object)
        _applicationExitStopReady = Signal(object)
        _bridgeRestartStopReady = Signal(object)
        _bridgeStopReady = Signal(object)
        _applicationUpdateCheckReady = Signal(object)
        _applicationUpdateDownloadReady = Signal(object)
        _applicationUpdateProgressReady = Signal(object)

        _DEVICE_ORDER = (device_catalog.RC003_ID,)
        _DEVICE_PAGE_INDEX = 0
        _BUTTONS_PAGE_INDEX = 1
        _VOICE_PAGE_INDEX = 2
        _DESKTOP_BEHAVIOR_PAGE_INDEX = _DEVICE_PAGE_INDEX
        _KEY_DETECTION_TIMEOUT_SECONDS = key_detection_bridge.STALE_AFTER_SECONDS
        _KEY_DETECTION_USAGE_TO_BUTTON = {
            usage: button_id
            for usage, button_id in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id in remote_layout.BUTTON_DISPLAY_NAMES
        }

        def __init__(
            self,
            model: "ButtonMappingModel",
            parent=None,
            *,
            start_hidden: bool = False,
            start_bridge: bool = False,
            background_task_runner: Optional[
                Callable[[Callable[[], None], str], None]
            ] = None,
        ) -> None:
            super().__init__(parent)
            self._model = model
            self._available_mapping_app_labels: Optional[set[str]] = None
            self._background_task_runner = background_task_runner
            self._background_shutdown_event = threading.Event()
            self._background_threads: set[threading.Thread] = set()
            # Tests inject a same-thread runner, so emitting a result may
            # synchronously submit the next serialized hotkey step while this
            # guard is held. Production signals are queued across threads.
            self._background_threads_lock = threading.RLock()
            self._log_export_busy = False
            self._logExportReady.connect(self._on_log_export_ready)
            self._applicationUpdateCheckReady.connect(
                self._on_application_update_check_ready
            )
            self._applicationUpdateDownloadReady.connect(
                self._on_application_update_download_ready
            )
            self._applicationUpdateProgressReady.connect(
                self._on_application_update_progress_ready
            )
            self._input_worker_result_lock = threading.Lock()
            self._input_worker_result = None
            self._hid_helper_repair_busy = False
            self._config_root = config.config_root()
            application_update.cleanup_obsolete_update_downloads(
                self._config_root / "updates",
                __version__,
            )
            self._hid_helper_frozen_distribution = bool(
                getattr(sys, "frozen", False)
            )
            self._hid_helper_offer_id = (
                hid_elevation_windows.bundled_helper_offer_id()
            )
            self._hid_helper_state = (
                hid_elevation_windows.inspect_installed_helper()
            )
            if (
                self._hid_helper_frozen_distribution
                and self._hid_helper_state.available
                and not hid_helper_consumers.current_consumer_is_registered(
                    self._config_root
                )
            ):
                self._hid_helper_state = hid_elevation_windows.HidHelperState(
                    False, "helper_consumer_unregistered"
                )
            self._hid_helper_process_elevated = (
                hid_elevation_windows.is_process_elevated()
            )
            self._hid_helper_can_self_elevate = (
                not self._hid_helper_frozen_distribution
                or hid_elevation_windows.can_current_user_self_elevate()
            )
            self._hid_helper_installed_distribution = (
                hid_elevation_windows.is_installed_distribution()
            )
            self._hid_helper_portable_distribution = (
                self._hid_helper_frozen_distribution
                and not self._hid_helper_installed_distribution
            )
            self._hidHelperRepairReady.connect(
                self._on_hid_helper_repair_ready
            )
            self._hidHelperRemovalReady.connect(
                self._on_hid_helper_removal_ready
            )
            self._config, self._bindings, self._configuration_issues = (
                config.load_window_settings(self._config_root)
            )
            self._configuration_error = ""
            if self._configuration_issues:
                details = "；".join(
                    f"{issue.filename}：{issue.message}" for issue in self._configuration_issues
                )
                self._configuration_error = (
                    f"{details} 原文件已保留，当前仅供查看；未读出的内容使用临时显示值。"
                    "保存和遥控器服务已暂停，可在设备页导出日志；修复配置后请重新打开。"
                )
                try:
                    logger = logging_setup.get_logger(self._config_root)
                    for issue in self._configuration_issues:
                        logger.error(
                            "settings load failed: file=%s error_type=%s reason=%s",
                            issue.filename, issue.error_type, issue.message,
                        )
                    if not logging_setup.flush_application_logs(logging_setup.log_dir(self._config_root)):
                        self._configuration_error += " 本次原因未能完整写入日志。"
                except Exception:
                    # A read-only/unavailable data directory must not also
                    # prevent the user from seeing the recovery explanation.
                    self._configuration_error += " 本次原因未能写入日志。"
            self._remote_devices = []
            self._remote_scan_busy = False
            self._remote_scan_valid = False
            self._remote_selection_busy = False
            self._remote_selection_message = ""
            self._remote_switch_pending = None
            self._remote_save_uncertain = False
            self._remoteDevicesReady.connect(self._on_remote_devices_ready)
            self._remoteSwitchReady.connect(self._on_remote_switch_ready)
            self.remoteSelectionChanged.connect(self.trayStateChanged.emit)
            self._hid_helper_setup_prompted_offer_id = str(
                self._config.get("hid_helper_setup_prompted_offer_id", "")
            )
            self._hid_helper_setup_attempted_this_session = False
            self._start_hidden = bool(start_hidden) and not self.configurationReadOnly
            self._start_bridge_requested = bool(start_bridge)
            self._launch_bridge_on_app_start = bool(
                self._config.get("launch_bridge_on_app_start", False)
            ) and not dev_session.is_isolated()
            self._diagnostic_trace_enabled = bool(
                self._config.get("diagnostic_trace_enabled", False)
            )
            self._close_behavior = str(
                self._config.get(
                    "close_behavior", config.CLOSE_BEHAVIOR_HIDE_TO_TRAY
                )
            )
            if not self.configurationReadOnly:
                startup_windows.rebind_owned_frozen_startup()
            startup_state = startup_windows.read_startup_state()
            self._launch_at_login = startup_state.enabled
            self._application_exit_requested = False
            self._application_exit_confirmed = False
            self._maintenance_exit_pending = False
            self._maintenance_exit_request: Optional[
                single_instance.ApplicationExitRequest
            ] = None
            self._settings_window_hwnd = 0
            self._application_exit_intent = threading.Event()
            self._application_exit_deadline = 0.0
            self._application_exit_poll_scheduled = False
            self._application_exit_stop_running = False
            self._application_exit_waiting_for_save = False
            self._save_then_exit_requested = False
            self._save_then_exit_deadline = 0.0
            self._save_then_exit_poll_scheduled = False
            self._applicationExitStopReady.connect(
                self._on_application_exit_stop_ready
            )
            self._bridgeRestartStopReady.connect(
                self._on_bridge_restart_stop_ready
            )
            self._bridgeStopReady.connect(self._on_bridge_stop_ready)
            self._window_hide_requested = False
            self._removed_voice_bindings = config.normalize_voice_product_boundary(
                self._config,
                self._bindings,
            )
            saved_voice_hotkeys = self._config.get("voice_hotkeys", {})
            self._voice_hotkey = str(saved_voice_hotkeys.get("hold", ""))
            self._sync_voice_key_cache()
            self._voice_hotkey_busy = False
            self._voice_hotkey_save_state = "saved"
            self._endpoint_preflight_busy = False
            self._endpoint_preflight_token = 0
            self._endpoint_preflight_completion = None
            self._endpointPreflightReady.connect(
                self._on_endpoint_preflight_ready
            )
            try:
                self._bridge_running = single_instance.bridge_instance_running()
            except (
                single_instance.SingleInstanceUnavailableError,
                single_instance.MutexCleanupError,
            ):
                bridge_status_known = False
                self._bridge_running = False
                self._launch_status_text = settings_ui.LAUNCH_STATUS_UNKNOWN_TEXT
            else:
                bridge_status_known = True
                self._launch_status_text = (
                    settings_ui.LAUNCH_ALREADY_RUNNING_TEXT
                    if self._bridge_running
                    else settings_ui.LAUNCH_NOT_STARTED_TEXT
                )
            self._current_runtime_identity = (
                bridge_runtime_status.current_runtime_identity(__version__)
            )
            self._bridge_restart_recommended = False
            self._output_endpoint_restart_required = False
            self._bridge_recovery_attempted = False
            self._bridge_recovery_running = False
            self._legacy_bridge_launch_handoff_attempted = False
            self._bridge_reconnect_available = False
            self._bridge_reconnect_busy = False
            runtime_status = (
                bridge_runtime_status.read_status(self._config_root)
                if self._bridge_running
                else None
            )
            self._bridge_status_missing_since: Optional[float] = (
                time.monotonic()
                if self._bridge_running and runtime_status is None
                else None
            )
            self._raw_input_state = (
                runtime_status.raw_input_state
                if runtime_status is not None
                else "unknown"
            )
            self._hid_tap_state = (
                runtime_status.hid_tap_state
                if runtime_status is not None
                else "unknown"
            )
            self._voice_key_physicalizer_state = (
                runtime_status.voice_key_physicalizer_state
                if runtime_status is not None
                else "unknown"
            )
            self._remote_battery_level = (
                getattr(runtime_status, "battery_level", None)
                if runtime_status is not None
                else None
            )
            self._voice_runtime_state = (
                runtime_status.voice_runtime_state
                if runtime_status is not None
                else bridge_runtime_status.VOICE_RUNTIME_NOT_TESTED
            )
            self._bridge_connected = bool(
                runtime_status is not None
                and runtime_status.state
                is bridge_runtime_status.BridgeConnectionState.CONNECTED
            )
            self._bridge_connection_state = (
                runtime_status.state.value
                if runtime_status is not None
                else "unknown"
                if self._bridge_running
                else "stopped"
            )
            self._bridge_launch_phase = (
                "connected"
                if self._bridge_connected
                else "waiting"
                if self._bridge_running
                else "idle"
                if bridge_status_known
                else "unknown"
            )
            self._bridge_launch_started_at: Optional[float] = None
            self._bridge_launch_elapsed_seconds = 0
            self._pending_bridge_launch: Optional[
                bridge_launcher.PendingBridgeLaunch
            ] = None
            if self._bridge_connected:
                self._launch_status_text = (
                    f"服务运行中；{device_catalog.RC003_DISPLAY_NAME} 已连接"
                )
            elif self._bridge_running:
                self._launch_status_text = (
                    f"服务运行中；等待{device_catalog.RC003_DISPLAY_NAME} 连接"
                    if runtime_status is not None
                    else (
                        f"服务运行中；{device_catalog.RC003_DISPLAY_NAME} 状态未知，"
                        "正在检查"
                    )
                )
            if self._bridge_running and runtime_status is not None:
                self._launch_status_text = self._describe_runtime_status(
                    runtime_status
                )
                identity_match = bridge_runtime_status.runtime_identity_matches(
                    runtime_status,
                    self._current_runtime_identity,
                )
                self._bridge_restart_recommended = (
                    identity_match is not True
                    or self._runtime_status_requires_restart(runtime_status)
                )
            self._bridge_reconnect_available = (
                self._can_reconnect_bridge_now(runtime_status)
            )
            self._has_explicit_launch_result = False
            self._status_message = ""
            self._error_message = ""
            removed_windows_dictation = bool(
                self._config.get(config.RUNTIME_REMOVED_WINDOWS_DICTATION_KEY)
            )
            self._application_update_state = "idle"
            self._application_update_message = ""
            self._application_update_release_notes = ""
            self._application_update_release_url = application_update.RELEASES_PAGE_URL
            self._application_update_release = None
            self._application_update_available = False
            self._application_update_check_busy = False
            self._application_update_check_silent = False
            self._application_update_startup_attempted = False
            self._application_update_download_busy = False
            self._application_update_download_cancel_event = None
            self._application_update_package_name = ""
            self._application_update_package_size = 0
            self._application_update_download_received = 0
            self._application_update_download_path = ""
            self._settings_dirty = bool(self._removed_voice_bindings) and not self.configurationReadOnly
            self._mapping_dirty = bool(self._removed_voice_bindings) and not self.configurationReadOnly
            self._settings_revision = 0
            self._settings_save_busy = False
            self._mapping_preset_rows: List[dict] = []
            self._mapping_edit_target = ""
            self._mapping_app_rows: List[dict] = []
            self._running_applications: List[dict] = []
            self._app_executable_cache: Dict[str, str] = {}
            self._app_icon_cache: Dict[str, str] = {}
            self._mapping_auto_save_scheduled = False
            self._mapping_auto_save_running = False
            self._mapping_auto_save_retry_available = False
            self._mapping_blocked_log_at = 0.0
            self._mapping_auto_save_timer = QTimer(self)
            self._mapping_auto_save_timer.setSingleShot(True)
            self._mapping_auto_save_timer.timeout.connect(
                self._run_mapping_auto_save
            )
            self._microphone_level = 0.0
            self._microphone_level_monitor = None
            self._microphone_level_timer = QTimer(self)
            self._microphone_level_timer.setInterval(50)
            self._microphone_level_timer.timeout.connect(
                self._tick_microphone_level
            )
            self._active_page_index = self._DEVICE_PAGE_INDEX
            self._feedback_page_index = (
                self._BUTTONS_PAGE_INDEX
                if self._removed_voice_bindings
                else self._VOICE_PAGE_INDEX
                if removed_windows_dictation
                else self._DEVICE_PAGE_INDEX
            )
            self._selected_button_id = "ok"
            selected_device_id = device_catalog.normalize_device_id(
                self._config.get("selected_device_profile")
            )
            if selected_device_id not in self._DEVICE_ORDER:
                selected_device_id = device_catalog.RC003_ID
            self._selected_device_fallback_id = selected_device_id
            self._selected_device_index = self._DEVICE_ORDER.index(selected_device_id)
            self._key_detection_listener = None
            self._key_detection_tap = None
            self._key_detection_bridge_request = None
            self._key_detection_started_at = 0.0
            self._key_detection_tap_usages = set()
            self._key_detection_active = False
            self._key_detection_text = (
                "开始检测后，按一下遥控器上的按键"
            )
            self._rawKeyDetected.connect(self._on_raw_key_detected)
            self._hidTapDetectionStatus.connect(self._on_hid_tap_detection_status)
            self._hotkey_capture = None
            self._pending_hotkey_capture_result = None
            self._qt_hotkey_tokens: List[str] = []
            self._qt_hotkey_pressed_keys: set[str] = set()
            self._qt_hotkey_primary_key: Optional[int] = None
            self._hotkeyCaptureResult.connect(self._on_hotkey_capture_result)
            self._hotkeyCaptureFailed.connect(self._on_hotkey_capture_failed)
            application = QApplication.instance()
            if application is not None:
                application.installEventFilter(self)
            self._input_operation_kind = ""
            self._input_operation_phase = "idle"
            self._input_operation_token = 0
            self._input_operation_cancel_event: Optional[threading.Event] = None
            self._input_cleanup_requested = False
            self._pending_key_detection_stop_message = ""
            self._inputOperationReady.connect(self._on_input_operation_ready)

            self._endpoint_options: List[str] = []
            self._endpoint_values: List[audio_output.AudioEndpoint] = []
            self._recommended_endpoint_index = -1
            self._selected_endpoint_index = -1
            self._endpoint_options_refresh_running = False
            self._endpoint_options_refresh_pending = False
            self._endpointOptionsRefreshReady.connect(
                self._on_endpoint_options_refresh_ready
            )
            self._load_bindings_into_model()
            self._model.mappingEdited.connect(self._on_mapping_edited)
            self._model.set_selected_button(self._selected_button_id)
            self._request_endpoint_options_refresh()
            if removed_windows_dictation:
                self._status_message = (
                    "Windows 语音输入入口已取消，请在语音页重新选择实际使用的语音程序。"
                )
            if self._removed_voice_bindings:
                affected = "、".join(
                    remote_layout.BUTTON_DISPLAY_NAMES.get(button_id, button_id)
                    for button_id in sorted(self._removed_voice_bindings)
                )
                self._status_message = (
                    f"旧语音配置已停用（{affected}）。请重新选择动作；完成后会自动保存；"
                    "停用前不会执行该按钮的单击、双击或长按动作。"
                )
            # 配置档案里的「自动保存」行只是当前按键设置的投影，启动时就要填好，
            # 否则第一次打开配置档案会是空列表，直到某次保存才出现。
            try:
                self._refresh_mapping_presets()
            except Exception:  # noqa: BLE001 - a broken store must not block startup
                pass

        # -- internal helpers -------------------------------------------------

        def _start_background_task(
            self, target: Callable[[], None], name: str
        ) -> None:
            runner = self._background_task_runner
            if runner is not None:
                if self._background_shutdown_event.is_set():
                    raise RuntimeError("设置窗口正在关闭")
                runner(target, name)
                return

            def run_tracked() -> None:
                try:
                    if not self._background_shutdown_event.is_set():
                        target()
                finally:
                    with self._background_threads_lock:
                        self._background_threads.discard(threading.current_thread())

            thread = threading.Thread(
                target=run_tracked,
                name=name,
                daemon=True,
            )
            with self._background_threads_lock:
                if self._background_shutdown_event.is_set():
                    raise RuntimeError("设置窗口正在关闭")
                self._background_threads.add(thread)
                try:
                    thread.start()
                except BaseException:
                    self._background_threads.discard(thread)
                    raise

        def _emit_background_result(self, signal, payload: object) -> bool:
            with self._background_threads_lock:
                if self._background_shutdown_event.is_set():
                    return False
                try:
                    signal.emit(payload)
                except RuntimeError:
                    return False
                return True

        def _record_input_worker_result(
            self, token: int, action: str, result: object
        ) -> None:
            with self._input_worker_result_lock:
                self._input_worker_result = (token, action, result)

        def _take_input_worker_result(self):
            with self._input_worker_result_lock:
                result = self._input_worker_result
                self._input_worker_result = None
                return result

        def _clear_input_worker_result(self, token: int, action: str) -> None:
            with self._input_worker_result_lock:
                current = self._input_worker_result
                if current is not None and current[:2] == (token, action):
                    self._input_worker_result = None

        def shutdownBackgroundTasks(self) -> bool:
            """Stop accepting background results and bounded-wait for workers."""

            self.stopMicrophoneLevelMonitor()
            with self._background_threads_lock:
                self._background_shutdown_event.set()
                threads = list(self._background_threads)
            self._endpoint_options_refresh_pending = False
            self._endpoint_preflight_token += 1
            self._endpoint_preflight_completion = None
            cancel_event = self._application_update_download_cancel_event
            if cancel_event is not None:
                cancel_event.set()

            deadline = time.monotonic() + _SETTINGS_BACKGROUND_JOIN_TIMEOUT_SECONDS
            current_thread = threading.current_thread()
            for thread in threads:
                if thread is current_thread:
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                thread.join(timeout=remaining)
            return True

        def shutdownForProcessExit(self) -> bool:
            """Synchronously release input hooks before Qt objects disappear."""

            self._application_exit_intent.set()
            _driver_action_active_event.clear()
            cancel_event = self._input_operation_cancel_event
            if cancel_event is not None:
                cancel_event.set()
            background_settled = False
            try:
                background_settled = self.shutdownBackgroundTasks()
            finally:
                worker_result = self._take_input_worker_result()
                if worker_result is not None:
                    _token, _action, result = worker_result
                    self._hotkey_capture = result.hotkey_capture
                    self._key_detection_bridge_request = result.bridge_request
                    self._key_detection_listener = result.listener
                    self._key_detection_tap = result.tap
                result = self._stop_input_resources(
                    self._input_operation_kind,
                    hotkey_capture=self._hotkey_capture,
                    bridge_request=self._key_detection_bridge_request,
                    listener=self._key_detection_listener,
                    tap=self._key_detection_tap,
                )
                self._hotkey_capture = result.hotkey_capture
                self._key_detection_bridge_request = result.bridge_request
                self._key_detection_listener = result.listener
                self._key_detection_tap = result.tap
                self._input_operation_cancel_event = None
                self._set_key_detection_active_state(False)
                self._set_input_operation_state("", "idle")
            return background_settled

        def _endpoint_options_payload(self, config_snapshot: dict) -> dict:
            try:
                endpoints = audio_output.enumerate_output_endpoints()
                endpoints = sorted(
                    endpoints,
                    key=lambda endpoint: (
                        audio_output.output_host_api_rank(endpoint.host_api),
                        endpoint.name.casefold(),
                    ),
                )
                options = [settings_ui._endpoint_display(e) for e in endpoints]
            except audio_output.AudioOutputUnavailableError:
                endpoints = []
                options = []

            endpoint_values = list(endpoints)

            recommended_display = ""
            recommendation_candidates = [
                endpoint
                for endpoint in endpoints
                if audio_output.is_cable_input_endpoint(endpoint.name)
                and endpoint.host_api
                in ("Windows WASAPI", "Windows DirectSound")
            ]
            try:
                recommended_endpoint = audio_output.select_preferred_output_endpoint(
                    recommendation_candidates
                )
            except audio_output.AudioOutputUnavailableError:
                pass
            else:
                recommended_display = settings_ui._endpoint_display(
                    recommended_endpoint
                )

            saved_name = config_snapshot.get("output_endpoint_name", "")
            saved_host_api = config_snapshot.get("output_endpoint_host_api", "")
            saved_display = ""
            migrated_display = ""
            migration_message = ""
            if saved_name:
                saved_display = settings_ui._endpoint_display(
                    audio_output.AudioEndpoint(
                        name=saved_name,
                        host_api=saved_host_api,
                    )
                )
            if (
                saved_display
                and audio_output.is_supported_output_host_api(saved_host_api)
                and saved_display not in options
            ):
                # The previously-saved device is no longer enumerated (e.g.
                # unplugged) - still show it as a selectable-but-absent
                # option rather than silently discarding the user's saved
                # choice, matching build_save_model()/_parse_endpoint_display()
                # round-tripping whatever text is present at save time.
                options = [saved_display] + options
                endpoint_values = [
                    audio_output.AudioEndpoint(
                        name=saved_name,
                        host_api=saved_host_api,
                    )
                ] + endpoint_values

            if saved_name and not audio_output.is_supported_output_host_api(
                saved_host_api
            ):
                cable_matches = [
                    endpoint
                    for endpoint in endpoints
                    if audio_output.is_cable_input_endpoint(endpoint.name)
                ]
                try:
                    migrated_endpoint = audio_output.select_preferred_output_endpoint(
                        cable_matches
                    )
                except audio_output.AudioOutputUnavailableError:
                    pass
                else:
                    migrated_display = settings_ui._endpoint_display(
                        migrated_endpoint
                    )
                    migration_message = (
                        "旧的 Windows WDM-KS 语音端点不可用于当前播放方式；"
                        f"已为本次设置预选 {migrated_display}。请重新选择该端点以自动保存。"
                    )

            recommended_index = (
                options.index(recommended_display)
                if recommended_display in options
                else -1
            )
            selected_display = migrated_display or saved_display
            selected_index = (
                options.index(selected_display) if selected_display in options else -1
            )
            return {
                "options": options,
                "values": endpoint_values,
                "recommended_index": recommended_index,
                "selected_index": selected_index,
                "migration_message": migration_message,
            }

        def _apply_endpoint_options_payload(self, payload: dict) -> None:
            options = list(payload["options"])
            values = list(payload["values"])
            recommended_index = int(payload["recommended_index"])
            selected_index = int(payload["selected_index"])
            options_changed = options != self._endpoint_options
            recommended_changed = (
                recommended_index != self._recommended_endpoint_index
            )
            selected_changed = selected_index != self._selected_endpoint_index
            self._endpoint_options = options
            self._endpoint_values = values
            self._recommended_endpoint_index = recommended_index
            self._selected_endpoint_index = selected_index
            if options_changed:
                self.endpointOptionsChanged.emit()
            if recommended_changed:
                self.recommendedEndpointIndexChanged.emit()
            if selected_changed:
                self.selectedEndpointIndexChanged.emit()
            migration_message = str(payload.get("migration_message", ""))
            if migration_message:
                self._mark_settings_dirty()
                self._set_status_message(
                    migration_message,
                    self._VOICE_PAGE_INDEX,
                )
            elif self._settings_dirty:
                self._refresh_settings_dirty_state()

        def _refresh_endpoint_options(self) -> None:
            self._apply_endpoint_options_payload(
                self._endpoint_options_payload(dict(self._config))
            )

        def _request_endpoint_options_refresh(self) -> None:
            if self._endpoint_options_refresh_running:
                self._endpoint_options_refresh_pending = True
                return
            config_snapshot = dict(self._config)
            self._endpoint_options_refresh_running = True

            def run() -> None:
                payload = self._endpoint_options_payload(config_snapshot)
                self._emit_background_result(
                    self._endpointOptionsRefreshReady,
                    (config_snapshot, payload),
                )

            try:
                self._start_background_task(run, "audio-endpoint-refresh")
            except Exception:
                self._endpoint_options_refresh_running = False

        def _on_endpoint_options_refresh_ready(self, result: object) -> None:
            if self._background_shutdown_event.is_set():
                return
            self._endpoint_options_refresh_running = False
            config_snapshot, payload = result
            tracked_keys = (
                "output_endpoint_name",
                "output_endpoint_host_api",
                "_remote_settings_owner",
            )
            stale = any(
                config_snapshot.get(key) != self._config.get(key)
                for key in tracked_keys
            )
            if not stale:
                self._apply_endpoint_options_payload(payload)
            refresh_again = self._endpoint_options_refresh_pending or stale
            self._endpoint_options_refresh_pending = False
            if refresh_again:
                QTimer.singleShot(0, self._request_endpoint_options_refresh)

        def _load_bindings_into_model(self) -> None:
            profile = self._selected_device_id()
            self._model.set_profile(profile)
            if self._selected_button_id not in remote_layout.button_order(profile):
                self._selected_button_id = "ok"
                self.selectedButtonIdChanged.emit()
            identity, document = self._resolved_edit_target()
            if identity != self._mapping_edit_target:
                self._mapping_edit_target = identity
                self.mappingEditTargetChanged.emit()
            editing_app = bool(identity)
            bindings = document.get("bindings", {})
            display_map: Dict[str, str] = {}
            secondary_display_map: Dict[str, Dict[str, str]] = {}
            inject_mode_map: Dict[str, str] = {}
            raw_display_notes = document.get("display_notes", {})
            display_note_map = (
                raw_display_notes if isinstance(raw_display_notes, dict) else {}
            )
            for button_id in remote_layout.button_order(profile):
                if not editing_app and button_id in self._removed_voice_bindings:
                    display_map[button_id] = settings_ui._REMOVED_VOICE_DISPLAY
                    secondary_display_map[button_id] = {}
                    continue
                action_dict = bindings.get(button_id)
                if action_dict is not None:
                    try:
                        action = key_mapping.ButtonAction.from_dict(action_dict)
                        display_map[button_id] = settings_ui._action_to_display(action)
                        inject_mode_map[button_id] = action.inject_mode
                    except (KeyError, TypeError, ValueError):
                        display_map[button_id] = ""
                else:
                    display_map[button_id] = ""
                secondary_display_map[button_id] = {}
                raw_secondary = document.get("secondary_bindings", {}).get(
                    button_id, {}
                )
                if isinstance(raw_secondary, dict):
                    for trigger_name in (
                        key_mapping.ButtonTrigger.DOUBLE_CLICK.value,
                        key_mapping.ButtonTrigger.LONG_PRESS.value,
                    ):
                        action_dict = raw_secondary.get(trigger_name)
                        if not isinstance(action_dict, dict):
                            continue
                        try:
                            action = key_mapping.ButtonAction.from_dict(action_dict)
                        except (KeyError, TypeError, ValueError):
                            continue
                        secondary_display_map[button_id][trigger_name] = (
                            settings_ui._action_to_display(action)
                        )
            self._model.load_display_map(
                display_map,
                secondary_display_map,
                display_note_map,
            )
            self._model.load_inject_modes(inject_mode_map)
            self._model.set_selected_button(self._selected_button_id)
            self.voiceSlotHotkeysChanged.emit()
            self.injectModesChanged.emit()

        def _selected_device_id(self) -> str:
            from . import remote_selection
            active = remote_selection.active_profile(self._config)
            if active:
                return active
            if 0 <= self._selected_device_index < len(self._DEVICE_ORDER):
                return self._DEVICE_ORDER[self._selected_device_index]
            return self._selected_device_fallback_id

        def _remote_selection(self) -> dict:
            from . import remote_selection
            return remote_selection.normalize(self._config.get(remote_selection.KEY))

        @Property(str, notify=remoteSelectionChanged)
        def activeRemoteKey(self) -> str:
            return self._remote_selection()["active"]

        @Property(str, notify=remoteSelectionChanged)
        def activeRemoteLabel(self) -> str:
            from . import remote_selection
            selection = self._remote_selection()
            return remote_selection.label(self.activeRemoteKey,
                remote_selection.profile_for_key(selection, self.activeRemoteKey),
                [row["key"] for row in selection["devices"]])

        @Property(str, notify=remoteSelectionChanged)
        def activeRemoteProfile(self) -> str:
            from . import remote_selection
            return remote_selection.active_profile(self._config)

        @Property(bool, notify=remoteSelectionChanged)
        def activeRemoteReady(self) -> bool:
            from . import remote_selection
            return remote_selection.runtime_ready(self.activeRemoteProfile)

        @Property(str, notify=remoteSelectionChanged)
        def activeRemotePairingState(self) -> str:
            if self._remote_scan_busy:
                return "checking"
            if not self._remote_scan_valid:
                return "unchecked"
            return "paired" if any(
                row["key"] == self.activeRemoteKey and row["profile"] == self.activeRemoteProfile
                for row in self._remote_devices
            ) else "selected_missing"

        @Property(str, notify=remoteSelectionChanged)
        def currentRemoteModelName(self) -> str:
            return device_catalog.profile_for(self._selected_device_id()).display_name

        @Property(int, notify=remoteSelectionChanged)
        def remoteRecordingLimitIndex(self) -> int:
            from .voice_preferences import LIMITS, DEFAULT_LIMIT
            return LIMITS.index(self._config.get("remote_recording_limit_seconds", DEFAULT_LIMIT))

        @Property("QStringList", notify=remoteSelectionChanged)
        def remoteRecordingLimitOptions(self) -> list:
            from .voice_preferences import LIMITS
            return [f"{seconds // 60} 分钟" for seconds in LIMITS]

        @Slot(int)
        def setRemoteRecordingLimit(self, limit_index: int) -> None:
            if self.configurationReadOnly:
                return
            from .voice_preferences import LIMITS
            logger = logging_setup.get_logger(self._config_root)
            if (self.isRc003Device or self._remote_selection_busy or self._settings_save_busy
                    or self.inputCaptureInUse or self._voice_hotkey_busy or self.voiceRuntimeState in (
                        "active", "mic_confirmed", "receiving_audio", "finishing")
                    or limit_index not in range(len(LIMITS))):
                self._set_status_message("")
                self._set_error_message("当前无法更改录音设置，原设置已保留；请稍后重试。", 2)
                self.remoteSelectionChanged.emit()
                logger.info("recording limit rejected: limit_index=%s hotkey_busy=%s voice_state=%s",
                            limit_index, self._voice_hotkey_busy, self.voiceRuntimeState)
                return
            updated = dict(self._config)
            updated["remote_recording_limit_seconds"] = LIMITS[limit_index]
            try:
                saved = config.save_config_and_load(config.config_path(self._config_root), updated)
            except Exception:
                self._set_status_message("")
                self._set_error_message("录音设置保存失败，原设置已保留。", 2)
                self.remoteSelectionChanged.emit()
                logger.warning("recording limit save failed: limit=%s", LIMITS[limit_index])
                return
            self._config = saved
            logger.info("recording limit saved: limit=%s",
                        saved["remote_recording_limit_seconds"])
            self._set_voice_hotkey_text(self._voice_hotkey)
            self.voiceHotkeySourceChanged.emit()
            self._bump_settings_revision()
            self.remoteSelectionChanged.emit()
            self._set_error_message("")
            self._set_status_message("录音设置已保存，下一次录音使用新设置。", 2)

        @Property(bool, notify=remoteSelectionChanged)
        def remoteSelectionBusy(self) -> bool:
            return self._remote_selection_busy

        @Property(str, notify=remoteSelectionChanged)
        def remoteSelectionMessage(self) -> str:
            return self._remote_selection_message

        @Property("QVariantList", notify=remoteSelectionChanged)
        def remoteDeviceChoices(self) -> list:
            from . import remote_selection
            selection = self._remote_selection()
            saved = {row["key"]: row["profile"] for row in selection["devices"]}
            rows = [row for row in self._remote_devices
                    if row["profile"] in remote_selection.KNOWN_PROFILES]
            active = selection["active"]
            present = {row["key"] for row in rows}
            if active and active not in present:
                rows = rows + [{"key": active, "profile": saved[active]}]
            peers = {row["key"] for row in rows} | set(saved)
            choices = []
            for row in rows:
                key, profile = row["key"], row["profile"]
                conflict = key in saved and saved[key] != profile
                ready = self._remote_scan_valid and key in present and not conflict
                note = ("（设备信息不一致）" if conflict else
                        "（待重新读取）" if not self._remote_scan_valid else
                        "（本次未找到）" if key not in present else "")
                choices.append(dict(key=key, profile=profile,
                    label=remote_selection.label(key, saved.get(key, profile), peers) + note,
                    canUse=ready, isActive=key == active))
            return choices

        @Property(bool, notify=remoteSelectionChanged)
        def remoteDevicesRefreshing(self) -> bool:
            return self._remote_scan_busy

        def _remote_feedback(self, message: str, *, busy: bool = False) -> None:
            self._remote_selection_message = message
            self._remote_selection_busy = busy
            self.remoteSelectionChanged.emit()

        @Slot()
        def refreshRemoteDevices(self) -> None:
            if self._remote_selection_busy or self._remote_scan_busy or self._application_exit_intent.is_set():
                return
            self._remote_scan_busy = True
            self._remote_scan_valid = False
            self._remote_feedback("正在读取 Windows 已配对的设备…")
            def scan():
                from . import remote_selection
                try:
                    rows = remote_selection.scan_paired(cancel_event=self._background_shutdown_event)
                    payload = (rows, "")
                except Exception:
                    payload = (None, "读取设备失败；请稍后重试，或检查 Windows 蓝牙设置。")
                self._emit_background_result(self._remoteDevicesReady, payload)
            try:
                self._start_background_task(scan, "remote-device-list")
            except RuntimeError:
                self._remote_scan_busy = False
                self._remote_feedback("设备读取未能启动，请重试。")

        def _on_remote_devices_ready(self, payload: object) -> None:
            if self._background_shutdown_event.is_set():
                return
            rows, error = payload
            self._remote_scan_busy = False
            self._remote_scan_valid = rows is not None and not error
            if self._remote_scan_valid:
                self._remote_devices = rows
            # A failed refresh is not proof that paired devices were removed.
            self._remote_feedback(error or (""
                if any(row["profile"] for row in (rows or [])) else
                "没有找到已配对且支持的遥控器，请先在 Windows 蓝牙设置中配对。"))

        def _save_remote_selection(self, selection: dict, *, allow_legacy_binding=True) -> bool:
            if self.configurationReadOnly:
                return False
            from . import remote_selection
            try:
                # Read the latest saved settings so device management cannot
                # save unrelated page drafts or overwrite another saved field.
                path = config.config_path(self._config_root)
                updated = config.load_config(path)
                changed = remote_selection.active_key(updated) != selection["active"]
                if changed:
                    saved, saved_bindings = config.switch_remote_settings(
                        path, config.key_bindings_path(self._config_root), selection,
                        allow_legacy_binding=allow_legacy_binding)
                else:
                    updated[remote_selection.KEY] = remote_selection.normalize(selection)
                    saved = config.save_config_and_load(path, updated)
            except config.ConfigTransactionError:
                self._remote_save_uncertain = True
                self._remote_feedback("设备设置保存异常且未能恢复；请重新选择设备并保存成功后再启动服务。")
                return False
            except Exception:
                self._remote_feedback("设备选择保存失败，原选择已保留，请重试。")
                return False
            self._config = saved
            if changed:
                self._bindings = saved_bindings
                self._removed_voice_bindings = config.normalize_voice_product_boundary(saved, saved_bindings)
                self._load_bindings_into_model()
                self._voice_hotkey = str(saved["voice_hotkeys"].get("hold", ""))
                self._sync_voice_key_cache()
                self._set_mapping_dirty(False)
                self._selected_endpoint_index = next((index for index, item in enumerate(self._endpoint_values)
                    if (item.name, item.host_api) == (saved.get("output_endpoint_name", ""), saved.get("output_endpoint_host_api", ""))), -1)
                self.selectedEndpointIndexChanged.emit()
                self._request_endpoint_options_refresh()
                self.hotkeyTextChanged.emit()
                self.holdVoiceHotkeyTextChanged.emit()
                self.voiceSlotHotkeysChanged.emit()
                self.voiceKeyConfigsChanged.emit()
                self.voiceHotkeySourceChanged.emit()
                self.voiceMappingReadyChanged.emit()
                self.selectedDeviceIndexChanged.emit()
                self.selectedDeviceChanged.emit()
                self.hidHelperStateChanged.emit()
            self._remote_save_uncertain = False
            self._bump_settings_revision()
            self.remoteSelectionChanged.emit()
            return True

        @Slot(str)
        def useRemoteDevice(self, key: str) -> None:
            from . import remote_selection
            if self._remote_selection_busy:
                return
            if self._remote_scan_busy or not self._remote_scan_valid:
                self._remote_feedback("请等待设备读取完成；读取失败时请刷新后重试。")
                return
            try:
                updated, legacy_binding = remote_selection.select_paired_device(
                    self._remote_selection(), key, self._remote_devices)
            except remote_selection.SelectionError as exc:
                self._remote_feedback(str(exc))
                return
            if key == self.activeRemoteKey and not self._remote_save_uncertain:
                self._remote_feedback("当前已在使用这台设备。")
                return
            self._change_active_remote(updated, allow_legacy_binding=legacy_binding)

        def _change_active_remote(self, updated: dict, *, allow_legacy_binding=True) -> None:
            if self.configurationReadOnly:
                return
            if (self._get_bridge_launch_busy() or self._bridge_recovery_running
                    or self._settings_save_busy or self._get_input_capture_in_use()
                    or self._hid_helper_repair_busy or self._voice_hotkey_busy
                    or self._endpoint_preflight_busy or _vb_cable_test_active_event.is_set()
                    or _driver_action_active_event.is_set()
                    or self._application_exit_intent.is_set()):
                self._remote_feedback("请先结束正在进行的设置、按键检测或服务操作，再切换设备。")
                return
            if self._has_unsaved_non_mapping_settings() or self._mapping_dirty:
                self._remote_feedback("请先保存当前设备的修改，再切换设备；未保存内容不会写给另一台。")
                return
            running = self._refresh_bridge_status()
            if running is None:
                self._remote_feedback("无法确认服务是否已停止，请重新检查后再切换。")
                return
            self._remote_switch_pending = (updated, running, allow_legacy_binding)
            self._remote_feedback("正在结束旧设备的按键和录音…", busy=True)
            if not running:
                self._on_remote_switch_ready((True, ""))
                return
            def stop():
                try:
                    result = bridge_control_windows.request_bridge_exit(reason="device_change")
                    payload = (bool(result.stopped), "旧设备未能正常停止，原选择保留。")
                except Exception:
                    payload = (False, "旧设备停止失败，原选择保留。")
                self._emit_background_result(self._remoteSwitchReady, payload)
            try:
                self._start_background_task(stop, "remote-device-switch")
            except RuntimeError:
                self._remote_switch_pending = None
                self._remote_feedback("切换未能启动，原选择保留。")

        def _on_remote_switch_ready(self, payload: object) -> None:
            pending = self._remote_switch_pending
            self._remote_switch_pending = None
            if pending is None:
                return
            stopped, message = payload
            if not stopped:
                self._remote_feedback(message)
                return
            updated, was_running, allow_legacy_binding = pending
            self._set_bridge_running(False)
            self._set_bridge_connected(False)
            self._set_bridge_input_states(None)
            if self._application_exit_intent.is_set():
                self._remote_feedback("设备切换已取消。")
                return
            saved = self._save_remote_selection(updated, allow_legacy_binding=allow_legacy_binding)
            if saved:
                self._remote_feedback(("已保存当前设备；其它页面已同步。" if self.activeRemoteReady
                                      else "已保存选择；当前型号尚未提供接收。")
                                      if updated["active"] else "当前设备已移除，请重新选择设备。")
            else:
                self._remote_selection_busy = False
                self.remoteSelectionChanged.emit()
            if was_running and self.activeRemoteKey and self.activeRemoteReady and not self._remote_save_uncertain:
                self._set_bridge_launch_phase("starting")
                QTimer.singleShot(0, self._start_bridge_process)

        def _set_launch_status(self, text: str) -> None:
            if text == self._launch_status_text:
                return
            self._launch_status_text = text
            self.launchStatusTextChanged.emit()

        def _set_bridge_running(self, value: bool) -> None:
            value = bool(value)
            if not value:
                self._output_endpoint_restart_required = False
            if value == self._bridge_running:
                return
            self._bridge_running = value
            self.bridgeRunningChanged.emit()
            self.trayStateChanged.emit()

        def _set_bridge_connected(self, value: bool) -> None:
            value = bool(value)
            if value == self._bridge_connected:
                return
            self._bridge_connected = value
            self.bridgeConnectedChanged.emit()
            self.trayStateChanged.emit()

        def _set_bridge_connection_state(self, value: str) -> None:
            value = str(value)
            if value == self._bridge_connection_state:
                return
            self._bridge_connection_state = value
            self.bridgeConnectionStateChanged.emit()

        def _set_bridge_input_states(
            self,
            status: Optional[bridge_runtime_status.BridgeRuntimeStatus],
        ) -> None:
            raw_input_state = status.raw_input_state if status is not None else "unknown"
            hid_tap_state = status.hid_tap_state if status is not None else "unknown"
            voice_key_physicalizer_state = (
                getattr(status, "voice_key_physicalizer_state", "unknown")
                if status is not None
                else "unknown"
            )
            input_state_changed = not (
                raw_input_state == self._raw_input_state
                and hid_tap_state == self._hid_tap_state
                and voice_key_physicalizer_state
                == self._voice_key_physicalizer_state
            )
            if input_state_changed:
                self._raw_input_state = raw_input_state
                self._hid_tap_state = hid_tap_state
                self._voice_key_physicalizer_state = voice_key_physicalizer_state
                self.bridgeInputStateChanged.emit()
                self.trayStateChanged.emit()

            battery_level = (
                getattr(status, "battery_level", None)
                if status is not None
                else None
            )
            if battery_level != self._remote_battery_level:
                self._remote_battery_level = battery_level
                self.remoteBatteryChanged.emit()

            voice_runtime_state = (
                getattr(
                    status,
                    "voice_runtime_state",
                    bridge_runtime_status.VOICE_RUNTIME_NOT_TESTED,
                )
                if status is not None
                else bridge_runtime_status.VOICE_RUNTIME_NOT_TESTED
            )
            if voice_runtime_state != self._voice_runtime_state:
                self._voice_runtime_state = voice_runtime_state
                self.voiceRuntimeStatusChanged.emit()

        @staticmethod
        def _runtime_status_requires_restart(
            status: bridge_runtime_status.BridgeRuntimeStatus,
        ) -> bool:
            if status.hid_tap_state in {"authorization_cancelled", "authorization_failed"}:
                return False
            return (
                bridge_runtime_status.input_channels_failed(status)
                or status.voice_key_physicalizer_state in {"failed", "stopped"}
            )

        def _set_bridge_restart_recommended(self, value: bool) -> None:
            value = bool(value or self._output_endpoint_restart_required)
            if value == self._bridge_restart_recommended:
                return
            self._bridge_restart_recommended = value
            self.bridgeRestartRecommendedChanged.emit()

        def _record_saved_output_change(self, saved_config: dict) -> None:
            """Keep the existing restart action available until the service stops."""
            if self._bridge_running and any(
                saved_config.get(key, "") != self._config.get(key, "")
                for key in ("output_endpoint_name", "output_endpoint_host_api")
            ):
                self._output_endpoint_restart_required = True
                self._set_bridge_restart_recommended(True)

        def _set_bridge_reconnect_available(self, value: bool) -> None:
            value = bool(value)
            if value == self._bridge_reconnect_available:
                return
            self._bridge_reconnect_available = value
            self.bridgeReconnectAvailableChanged.emit()

        def _set_bridge_reconnect_busy(self, value: bool) -> None:
            value = bool(value)
            if value == self._bridge_reconnect_busy:
                return
            self._bridge_reconnect_busy = value
            self.bridgeReconnectBusyChanged.emit()

        def _can_reconnect_bridge_now(
            self,
            status: Optional[bridge_runtime_status.BridgeRuntimeStatus],
        ) -> bool:
            if (
                not self._bridge_running
                or self._bridge_connected
                or self._bridge_restart_recommended
                or status is None
            ):
                return False
            if status.state not in {
                bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE,
                bridge_runtime_status.BridgeConnectionState.RETRY_WAIT,
            }:
                return False
            if (
                bridge_runtime_status.runtime_identity_matches(
                    status,
                    self._current_runtime_identity,
                )
                is not True
            ):
                return False
            if (
                time.time() - status.updated_at
                > _BRIDGE_STATUS_STALE_AFTER_SECONDS
            ):
                return False
            return bridge_launcher.in_process_bridge_running()

        def _describe_runtime_status(
            self,
            status: bridge_runtime_status.BridgeRuntimeStatus,
        ) -> str:
            unconfirmed_text = "已收到声音，未确认微信开麦；若未输入文字，请松开后重试"
            connected = (
                status.state
                is bridge_runtime_status.BridgeConnectionState.CONNECTED
            )
            if connected:
                connection_text = f"{device_catalog.RC003_DISPLAY_NAME} 已连接"
            elif status.state is bridge_runtime_status.BridgeConnectionState.CONNECTING:
                connection_text = f"正在连接{device_catalog.RC003_DISPLAY_NAME}"
            elif status.state is bridge_runtime_status.BridgeConnectionState.RETRY_WAIT:
                connection_text = (
                    f"{device_catalog.RC003_DISPLAY_NAME} 连接失败，等待重试"
                )
            else:
                connection_text = f"等待{device_catalog.RC003_DISPLAY_NAME}连接"
            identity_match = bridge_runtime_status.runtime_identity_matches(
                status,
                self._current_runtime_identity,
            )
            if identity_match is True:
                identity_text = f"当前版本 {status.app_version}"
            elif identity_match is False:
                identity_text = f"其它版本 {status.app_version or '未知'}"
            else:
                identity_text = "旧版服务，来源未确认"

            ready_hid_states = {
                frida_compat.HidTapState.READY.value,
                frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
            }
            raw_ready = status.raw_input_state == "ready"
            tap_ready = status.hid_tap_state in ready_hid_states
            tap_failed = status.hid_tap_state in (
                bridge_runtime_status.FAILED_HID_TAP_STATES
                | {
                    frida_compat.HidTapState.UNAVAILABLE.value,
                    frida_compat.HidTapState.DISABLED.value,
                }
            )
            physicalizer_recovering = (
                status.voice_key_physicalizer_state
                in {"starting", "recovering"}
            )
            physicalizer_failed = (
                status.voice_key_physicalizer_state
                in {"failed", "stopped"}
            )
            physicalizer_ready = status.voice_key_physicalizer_state == "ready"
            if status.hid_tap_state in {"authorization_cancelled", "authorization_failed"}:
                input_text = "按键授权未完成；请在设备页点击“重新授权”"
            elif raw_ready and tap_ready:
                if physicalizer_recovering:
                    input_text = (
                        "自定义按键映射可用；语音快捷键通道正在恢复"
                    )
                elif physicalizer_failed:
                    input_text = (
                        "自定义按键映射可用；语音快捷键通道异常"
                    )
                elif physicalizer_ready:
                    input_text = "两个按键通道正常"
                else:
                    input_text = (
                        "自定义按键映射可用；语音快捷键通道正在检查"
                    )
            elif raw_ready and tap_failed:
                input_text = "Windows 原始按键可用；自定义按键映射已停用"
            elif raw_ready:
                input_text = "Windows 原始按键可用；自定义按键映射正在检查"
            elif tap_ready:
                input_text = "自定义按键映射可用；普通按键监听异常"
            elif bridge_runtime_status.input_channels_failed(status):
                input_text = "两个按键通道异常"
            else:
                input_text = "按键通道正在检查"
            if not (raw_ready and tap_ready):
                if physicalizer_recovering:
                    input_text += "；语音快捷键通道正在恢复"
                elif physicalizer_failed:
                    input_text += "；语音快捷键通道异常"
            if status.last_button_at is not None:
                age = max(0.0, time.time() - status.last_button_at)
                if age <= 10.0:
                    input_text += f"，刚收到按键（{status.last_button_source or '来源未知'}）"
            voice_text = "语音进行中" if status.voice_active else "语音空闲"
            if status.voice_runtime_state == bridge_runtime_status.VOICE_RUNTIME_HOST_UNCONFIRMED:
                voice_text = unconfirmed_text
            stale_text = (
                "；状态更新滞后"
                if time.time() - status.updated_at
                > _BRIDGE_STATUS_STALE_AFTER_SECONDS
                else ""
            )
            return (
                f"服务运行中；{connection_text}；{identity_text}；"
                f"{input_text}；{voice_text}{stale_text}"
            )

        def _update_bridge_restart_recommendation(
            self,
            status: Optional[bridge_runtime_status.BridgeRuntimeStatus],
            *,
            schedule_recovery: bool,
            status_missing_timed_out: bool = False,
        ) -> None:
            recommended = bool(status_missing_timed_out)
            automatic = False
            if status is not None:
                identity_match = bridge_runtime_status.runtime_identity_matches(
                    status,
                    self._current_runtime_identity,
                )
                if status.hid_tap_state in {"authorization_cancelled", "authorization_failed"}:
                    recommended = False
                elif identity_match is not True:
                    recommended = True
                    automatic = identity_match is False and not status.voice_active
                elif bridge_runtime_status.input_channels_failed(status):
                    recommended = True
                    automatic = not status.voice_active
                elif status.voice_key_physicalizer_state in {"failed", "stopped"}:
                    recommended = True
                elif (
                    time.time() - status.updated_at
                    > _BRIDGE_STATUS_STALE_AFTER_SECONDS
                ):
                    recommended = True
            self._set_bridge_restart_recommended(recommended)
            if (
                automatic
                and schedule_recovery
                and not self._bridge_recovery_attempted
                and not self._bridge_recovery_running
                and not self._get_bridge_launch_busy()
                and not self._maintenance_exit_pending
                and not self._application_exit_requested
                and not self._application_exit_confirmed
            ):
                self._bridge_recovery_attempted = True
                QTimer.singleShot(0, self._start_automatic_bridge_recovery)

        def _persist_desktop_behavior(self, key: str, value: object) -> bool:
            if self.configurationReadOnly:
                return False
            next_config = dict(self._config)
            next_config[key] = value
            try:
                saved = config.save_config_and_load(
                    config.config_path(self._config_root), next_config
                )
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                self._set_status_message("")
                self._set_error_message(
                    f"常规设置保存失败：{type(exc).__name__}",
                    self._DESKTOP_BEHAVIOR_PAGE_INDEX,
                )
                return False
            self._config = saved
            self._bump_settings_revision()
            self._set_error_message("")
            self._set_status_message(
                "启动与窗口设置已保存。",
                self._DESKTOP_BEHAVIOR_PAGE_INDEX,
            )
            return True

        def _button_receiver_usable(self) -> bool:
            return bridge_runtime_status.button_receiver_usable(
                self.activeRemoteProfile,
                raw_input_state=self._raw_input_state,
                hid_tap_state=self._hid_tap_state,
                voice_key_physicalizer_state=(
                    self._voice_key_physicalizer_state
                ),
            )

        def _tray_icon_state(self) -> str:
            if self._bridge_connected and self._button_receiver_usable():
                return "connected"
            if self._bridge_running:
                return "waiting"
            return "off"

        def _tray_icon_source(self) -> str:
            path = resources.find_app_icon(self._tray_icon_state())
            return QUrl.fromLocalFile(str(path)).toString() if path else ""

        def _brand_icon_source(self) -> str:
            path = resources.find_app_icon("connected")
            return QUrl.fromLocalFile(str(path)).toString() if path else ""

        def _tray_tooltip(self) -> str:
            label = self.activeRemoteLabel or "未选择设备"
            if not self.activeRemoteKey:
                state = "请选择设备"
            elif not self.activeRemoteReady:
                state = "接收尚未接入"
            elif not self._bridge_running:
                state = "服务未启动"
            elif not self._bridge_connected:
                state = "未连接"
            elif self._button_receiver_usable():
                state = "正常"
            else:
                state = "已连接，按键接收异常"
            return f"{product_identity.DISPLAY_NAME}：{label} · {state}"

        def _set_bridge_launch_phase(self, value: str) -> None:
            if value == self._bridge_launch_phase:
                return
            self._bridge_launch_phase = value
            self.bridgeLaunchPhaseChanged.emit()

        def _refresh_bridge_launch_elapsed(self) -> None:
            elapsed = (
                0
                if self._bridge_launch_started_at is None
                else max(0, int(time.monotonic() - self._bridge_launch_started_at))
            )
            if elapsed == self._bridge_launch_elapsed_seconds:
                return
            self._bridge_launch_elapsed_seconds = elapsed
            self.bridgeLaunchElapsedSecondsChanged.emit()

        def _sync_bridge_connection_status(self, running: bool) -> None:
            if self._get_bridge_launch_busy():
                return
            runtime_status = (
                bridge_runtime_status.read_status(self._config_root)
                if running
                else None
            )
            status_missing_timed_out = False
            if running and runtime_status is None:
                now = time.monotonic()
                if self._bridge_status_missing_since is None:
                    self._bridge_status_missing_since = now
                status_missing_timed_out = (
                    now - self._bridge_status_missing_since
                    >= _BRIDGE_STATUS_MISSING_AFTER_SECONDS
                )
            else:
                self._bridge_status_missing_since = None
            self._set_bridge_input_states(runtime_status)
            self._set_bridge_connection_state(
                runtime_status.state.value
                if runtime_status is not None
                else "unknown"
                if running
                else "stopped"
            )
            connected = bool(
                runtime_status is not None
                and runtime_status.state
                is bridge_runtime_status.BridgeConnectionState.CONNECTED
            )
            self._set_bridge_connected(connected)
            if running:
                self._update_bridge_restart_recommendation(
                    runtime_status,
                    schedule_recovery=True,
                    status_missing_timed_out=status_missing_timed_out,
                )
                reconnect_available = self._can_reconnect_bridge_now(runtime_status)
                self._set_bridge_reconnect_available(reconnect_available)
                if connected or not reconnect_available:
                    self._set_bridge_reconnect_busy(False)
                if runtime_status is not None:
                    if self._bridge_reconnect_busy and not connected:
                        self._set_bridge_launch_phase("reconnecting")
                        self._set_launch_status(
                            f"服务运行中；正在立即连接{device_catalog.RC003_DISPLAY_NAME}…"
                        )
                        return
                    self._set_bridge_launch_phase(
                        "connected" if connected else "waiting"
                    )
                    self._set_launch_status(
                        self._describe_runtime_status(runtime_status)
                    )
                    return
                if connected:
                    self._set_bridge_launch_phase("connected")
                    self._set_launch_status(
                        f"服务运行中；{device_catalog.RC003_DISPLAY_NAME} 已连接"
                    )
                else:
                    self._set_bridge_launch_phase("waiting")
                    if self._has_explicit_launch_result:
                        self._set_launch_status(
                            f"服务运行中；等待{device_catalog.RC003_DISPLAY_NAME} 连接，"
                            "首次可能约 1 分钟"
                        )
                    elif runtime_status is None:
                        if status_missing_timed_out:
                            self._set_bridge_launch_phase("failed")
                            self._set_launch_status(
                                "服务状态异常；请重新启动遥控器服务"
                            )
                        else:
                            self._set_launch_status(
                                f"服务运行中；{device_catalog.RC003_DISPLAY_NAME} 状态未知，"
                                "正在检查"
                            )
                    else:
                        self._set_launch_status(
                            f"服务运行中；等待{device_catalog.RC003_DISPLAY_NAME} 连接"
                        )
                return

            self._set_bridge_restart_recommended(False)
            self._set_bridge_reconnect_available(False)
            self._set_bridge_reconnect_busy(False)
            self._bridge_status_missing_since = None

            previous_phase = self._bridge_launch_phase
            if (
                self._has_explicit_launch_result
                and previous_phase in {"waiting", "connected"}
            ):
                self._set_bridge_launch_phase("failed")
                self._set_launch_status("服务已退出；请查看 app.log")
            elif self._has_explicit_launch_result and previous_phase in {
                "failed",
                "unknown",
            }:
                return
            elif not self._has_explicit_launch_result:
                self._set_bridge_launch_phase("idle")
                self._set_launch_status(settings_ui.LAUNCH_NOT_STARTED_TEXT)

        def _refresh_bridge_status(self) -> bool | None:
            self._refresh_bridge_launch_elapsed()
            if _vb_cable_test_active_event.is_set():
                return self._bridge_running
            try:
                running = single_instance.bridge_instance_running()
            except (
                single_instance.SingleInstanceUnavailableError,
                single_instance.MutexCleanupError,
            ):
                self._set_bridge_running(False)
                self._set_bridge_connected(False)
                self._set_bridge_input_states(None)
                self._set_bridge_reconnect_available(False)
                self._set_bridge_reconnect_busy(False)
                if (
                    self._has_explicit_launch_result
                    and self._bridge_launch_phase == "failed"
                ):
                    return None
                if not self._get_bridge_launch_busy():
                    self._set_bridge_launch_phase("unknown")
                    self._set_launch_status(settings_ui.LAUNCH_STATUS_UNKNOWN_TEXT)
                return None

            self._set_bridge_running(running)
            self._sync_bridge_connection_status(running)
            return running

        def _set_feedback_page_index(self, value: int) -> None:
            value = max(
                self._DEVICE_PAGE_INDEX,
                min(self._VOICE_PAGE_INDEX, int(value)),
            )
            if value == self._feedback_page_index:
                return
            self._feedback_page_index = value
            self.feedbackPageIndexChanged.emit()

        def _set_status_message(
            self,
            text: str,
            page_index: Optional[int] = None,
        ) -> None:
            if text:
                self._set_feedback_page_index(
                    self._active_page_index if page_index is None else page_index
                )
            self._status_message = text
            self.statusMessageChanged.emit()

        def _set_error_message(
            self,
            text: str,
            page_index: Optional[int] = None,
        ) -> None:
            if text:
                self._set_feedback_page_index(
                    self._active_page_index if page_index is None else page_index
                )
            self._error_message = text
            self.errorMessageChanged.emit()

        def _set_settings_dirty(self, value: bool) -> None:
            value = bool(value)
            if value == self._settings_dirty:
                return
            self._settings_dirty = value
            self.settingsDirtyChanged.emit()

        def _set_mapping_dirty(self, value: bool) -> None:
            value = bool(value)
            if value == self._mapping_dirty:
                return
            self._mapping_dirty = value
            self.mappingDirtyChanged.emit()

        def _set_settings_save_busy(self, value: bool) -> None:
            value = bool(value)
            if value == self._settings_save_busy:
                return
            self._settings_save_busy = value
            self.settingsSaveBusyChanged.emit()

        def _set_mapping_auto_save_retry_available(self, value: bool) -> None:
            value = bool(value)
            if value == self._mapping_auto_save_retry_available:
                return
            self._mapping_auto_save_retry_available = value
            self.mappingAutoSaveRetryAvailableChanged.emit()

        def _hid_helper_needs_repair(self) -> bool:
            return (
                self._hid_helper_frozen_distribution
                and (
                    self._hid_helper_cleanup_pending()
                    or (
                        not self._hid_helper_state.available
                        and not self._hid_helper_process_elevated
                    )
                )
            )

        def _hid_helper_account_unsupported(self) -> bool:
            return (
                self._hid_helper_needs_repair()
                and not self._hid_helper_can_self_elevate
            )

        def _hid_helper_cleanup_pending(self) -> bool:
            return (
                self._hid_helper_state.available
                and self._hid_helper_state.detail == "helper_cleanup_pending"
            )

        def _hid_helper_repair_available(self) -> bool:
            return (
                self._selected_device_id() == device_catalog.RC003_ID
                and self._hid_helper_frozen_distribution
                and self._hid_helper_can_self_elevate
                and not hid_elevation_windows.is_newer_helper_state(
                    self._hid_helper_state
                )
                and self._hid_helper_needs_repair()
            )

        def _hid_helper_setup_required(self) -> bool:
            return (
                self._selected_device_id() == device_catalog.RC003_ID
                and self._hid_helper_portable_distribution
                and not self._hid_helper_process_elevated
                and self._hid_helper_can_self_elevate
                and not self._hid_helper_state.available
                and not hid_elevation_windows.is_newer_helper_state(
                    self._hid_helper_state
                )
            )

        def _set_hid_helper_repair_busy(self, value: bool) -> None:
            value = bool(value)
            if value == self._hid_helper_repair_busy:
                return
            self._hid_helper_repair_busy = value
            self.hidHelperRepairBusyChanged.emit()

        def _set_hid_helper_state(
            self, state: hid_elevation_windows.HidHelperState
        ) -> None:
            if state == self._hid_helper_state:
                return
            self._hid_helper_state = state
            self.hidHelperStateChanged.emit()

        def _bump_settings_revision(self) -> None:
            self._settings_revision += 1

        def _mark_settings_dirty(self) -> None:
            if self._status_message:
                self._set_status_message("")
            self._bump_settings_revision()
            self._set_settings_dirty(True)

        def _mark_mapping_dirty(self) -> None:
            if self._status_message:
                self._set_status_message("")
            self._bump_settings_revision()
            self._set_mapping_dirty(True)
            self._set_settings_dirty(True)

        def _mapping_log(self):
            return logging_setup.get_logger(self._config_root)

        def _queue_mapping_auto_save(self, delay_ms: int = 0) -> None:
            if self.configurationReadOnly:
                self._mapping_log().warning(
                    "mapping auto save skipped: configuration is read-only"
                )
                return
            self._set_mapping_auto_save_retry_available(False)
            if self._mapping_auto_save_scheduled or self._mapping_auto_save_running:
                return
            self._mapping_auto_save_scheduled = True
            self._mapping_auto_save_timer.start(max(0, int(delay_ms)))

        def _on_mapping_edited(self) -> None:
            if self.configurationReadOnly:
                self._mapping_log().warning(
                    "mapping edit ignored: configuration is read-only; "
                    "model reloaded from disk"
                )
                self._load_bindings_into_model()
                return
            self._mark_mapping_dirty()
            self._set_error_message("")
            self._set_status_message(
                "正在自动保存按键映射…",
                self._BUTTONS_PAGE_INDEX,
            )
            self._queue_mapping_auto_save()

        def _mapping_auto_save_temporarily_blocked(self) -> bool:
            return bool(
                self._settings_save_busy
                or self._voice_hotkey_busy
                or _vb_cable_test_active_event.is_set()
                or self._get_bridge_launch_busy()
                or self._endpoint_preflight_busy
                or _driver_action_active_event.is_set()
            )

        def _mapping_auto_save_block_reason(self) -> str:
            if self._settings_save_busy:
                return "settings_save_busy"
            if self._voice_hotkey_busy:
                return "voice_hotkey_busy"
            if _vb_cable_test_active_event.is_set():
                return "vb_cable_test_active"
            if self._get_bridge_launch_busy():
                return "bridge_launch_busy"
            if self._endpoint_preflight_busy:
                return "endpoint_preflight_busy"
            if _driver_action_active_event.is_set():
                return "driver_action_active"
            return ""

        def _mapping_save_status_suffix(self) -> str:
            if not self._mapping_dirty:
                return ""
            if self._mapping_auto_save_retry_available:
                return "；按键映射自动保存失败，请重试。"
            return "；按键映射正在自动保存。"

        def _has_unsaved_non_mapping_settings(self) -> bool:
            # RC003 records each voice slot's shortcut and writes it to disk the
            # moment it is captured, so its legacy hold mirror is never a draft
            # waiting to be committed.
            if 0 <= self._selected_endpoint_index < len(self._endpoint_values):
                selected_endpoint = self._endpoint_values[
                    self._selected_endpoint_index
                ]
                if (
                    selected_endpoint.name
                    != str(self._config.get("output_endpoint_name", ""))
                    or selected_endpoint.host_api
                    != str(self._config.get("output_endpoint_host_api", ""))
                ):
                    return True
            return False

        def _refresh_settings_dirty_state(self) -> None:
            self._set_settings_dirty(
                self._mapping_dirty or self._has_unsaved_non_mapping_settings()
            )

        def _run_mapping_auto_save(self) -> None:
            self._mapping_auto_save_scheduled = False
            if not self._mapping_dirty:
                self._set_mapping_auto_save_retry_available(False)
                return
            if (
                self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
                or self._save_then_exit_requested
            ):
                self._mapping_log().info(
                    "mapping auto save deferred: application exit in progress"
                )
                return
            block_reason = self._mapping_auto_save_block_reason()
            if block_reason:
                now = time.monotonic()
                if now - self._mapping_blocked_log_at >= 5.0:
                    self._mapping_blocked_log_at = now
                    self._mapping_log().info(
                        "mapping auto save postponed: busy=%s", block_reason
                    )
                self._queue_mapping_auto_save(150)
                return

            attempt_revision = self._settings_revision
            self._mapping_auto_save_running = True
            completion_called = False
            self._mapping_log().info(
                "mapping auto save started: revision=%d target=%s",
                attempt_revision,
                self._mapping_edit_target or "key_bindings.json",
            )

            def finish(saved: bool) -> None:
                nonlocal completion_called
                if completion_called:
                    return
                completion_called = True
                self._mapping_auto_save_running = False
                if saved:
                    self._set_mapping_auto_save_retry_available(False)
                    if self._mapping_dirty:
                        # An edit landed while this save was running; the
                        # queue call it made was dropped, so re-arm here or
                        # the new mapping would never reach disk.
                        self._mapping_log().info(
                            "mapping changed during save; re-scheduling auto save"
                        )
                        self._queue_mapping_auto_save()
                    return
                if self._settings_revision != attempt_revision:
                    self._queue_mapping_auto_save()
                    return
                self._set_mapping_auto_save_retry_available(True)

            accepted = self._save_mapping(completion=finish)
            if not accepted and not completion_called and not self._settings_save_busy:
                finish(False)

        def _set_endpoint_preflight_busy(self, value: bool) -> None:
            value = bool(value)
            if value == self._endpoint_preflight_busy:
                return
            self._endpoint_preflight_busy = value
            self.endpointPreflightBusyChanged.emit()

        def _request_endpoint_preflight(
            self,
            endpoint_name: str,
            endpoint_host_api: str,
            completion: Callable[[bool, str], None],
        ) -> bool:
            if self._endpoint_preflight_busy:
                completion(False, "另一个输出端点正在检查，请稍后重试。")
                return False
            self._endpoint_preflight_token += 1
            token = self._endpoint_preflight_token
            self._endpoint_preflight_completion = completion
            self._set_endpoint_preflight_busy(True)

            def run() -> None:
                try:
                    windows_diagnostics.preflight_output_endpoint_isolated(
                        endpoint_name,
                        endpoint_host_api,
                        cancel_event=_AnyEvent(
                            self._background_shutdown_event,
                            self._application_exit_intent,
                        ),
                    )
                except audio_output.AudioOutputUnavailableError:
                    result = (False, "所选语音输出设备无法实际打开。")
                except Exception:  # noqa: BLE001 - keep UI text sanitized
                    result = (False, "输出端点检查失败，请稍后重试。")
                else:
                    result = (True, "")
                self._emit_background_result(
                    self._endpointPreflightReady,
                    (token, result),
                )

            try:
                self._start_background_task(run, "audio-endpoint-preflight")
            except Exception:
                self._endpoint_preflight_completion = None
                self._set_endpoint_preflight_busy(False)
                completion(False, "无法启动输出端点检查。")
                return False
            return True

        def _on_endpoint_preflight_ready(self, result: object) -> None:
            if self._background_shutdown_event.is_set():
                return
            token, payload = result
            if token != self._endpoint_preflight_token:
                return
            completion = self._endpoint_preflight_completion
            self._endpoint_preflight_completion = None
            self._set_endpoint_preflight_busy(False)
            if completion is not None:
                ok, message = payload
                completion(bool(ok), str(message))
            self._schedule_application_exit_poll()

        def _set_key_detection_text(self, text: str) -> None:
            if text != self._key_detection_text:
                self._key_detection_text = text
                self.keyDetectionTextChanged.emit()

        def _get_hotkey_capture_active(self) -> bool:
            return self._hotkey_capture is not None or (
                self._input_operation_kind == "hotkey"
                and self._input_operation_phase != "idle"
            )

        def _get_input_capture_in_use(self) -> bool:
            return bool(
                self._input_operation_phase != "idle"
                or self._hotkey_capture is not None
                or self._key_detection_bridge_request is not None
                or self._key_detection_listener is not None
                or self._key_detection_tap is not None
            )

        def _reset_qt_hotkey_capture_state(self) -> None:
            self._qt_hotkey_tokens.clear()
            self._qt_hotkey_pressed_keys.clear()
            self._qt_hotkey_primary_key = None

        def eventFilter(self, watched, event):  # noqa: N802 - Qt override
            event_type = event.type()
            if event_type not in {
                QEvent.Type.KeyPress,
                QEvent.Type.KeyRelease,
            }:
                return False
            if (
                self._input_operation_kind != "hotkey"
                or self._input_operation_phase != "active"
                or self._hotkey_capture is None
            ):
                if self._get_input_capture_in_use():
                    return False
                # A swallowed low-level edge never becomes a Qt event. This
                # fallback also covers message-based remote keyboard input.
                key_vks = {
                    Qt.Key.Key_Up: 0x26, Qt.Key.Key_Down: 0x28,
                    Qt.Key.Key_Left: 0x25, Qt.Key.Key_Right: 0x27,
                    Qt.Key.Key_Return: 0x0D, Qt.Key.Key_Enter: 0x0D,
                    Qt.Key.Key_Escape: 0x1B, Qt.Key.Key_Menu: 0x5D,
                    Qt.Key.Key_PageUp: 0x21, Qt.Key.Key_PageDown: 0x22,
                    Qt.Key.Key_VolumeUp: 0xAF, Qt.Key.Key_VolumeDown: 0xAE,
                }
                vk = key_vks.get(event.key())
                if vk is None:
                    return False
                return element_navigation_control_windows.route_local_navigation_key(
                    vk, event_type == QEvent.Type.KeyPress, event.isAutoRepeat(),
                    event.modifiers() != Qt.KeyboardModifier.NoModifier,
                )
            key = int(event.key())
            if key == Qt.Key.Key_Escape.value:
                return False
            raw_modifiers = event.modifiers()
            modifiers = int(getattr(raw_modifiers, "value", raw_modifiers))
            return self._capture_hotkey_qt_event(
                key,
                modifiers,
                event.text(),
                event_type == QEvent.Type.KeyPress,
                event.isAutoRepeat(),
                native_virtual_key=int(event.nativeVirtualKey()),
                native_scan_code=int(event.nativeScanCode()),
            )

        def _set_key_detection_active_state(self, value: bool) -> None:
            value = bool(value)
            if value == self._key_detection_active:
                return
            self._key_detection_active = value
            self.keyDetectionActiveChanged.emit()

        def _set_input_operation_state(self, kind: str, phase: str) -> None:
            previous_hotkey_active = self._get_hotkey_capture_active()
            previous_in_use = self._get_input_capture_in_use()
            changed = (
                kind != self._input_operation_kind
                or phase != self._input_operation_phase
            )
            self._input_operation_kind = kind
            self._input_operation_phase = phase
            if changed or previous_in_use != self._get_input_capture_in_use():
                self.inputOperationChanged.emit()
            if previous_hotkey_active != self._get_hotkey_capture_active():
                self.hotkeyCaptureActiveChanged.emit()

        def _stop_input_resources(
            self,
            kind: str,
            *,
            hotkey_capture=None,
            bridge_request=None,
            listener=None,
            tap=None,
        ) -> _InputStopResult:
            errors: List[str] = []
            remaining_capture = hotkey_capture
            remaining_request = bridge_request
            remaining_listener = listener
            remaining_tap = tap

            if hotkey_capture is not None:
                try:
                    hotkey_capture.stop()
                except Exception as exc:  # noqa: BLE001 - returned to Qt
                    errors.append(f"停止键盘快捷键录入时出错：{exc}")
                else:
                    remaining_capture = None
            if bridge_request is not None:
                try:
                    key_detection_bridge.cancel_detection(bridge_request)
                except Exception as exc:  # noqa: BLE001 - returned to Qt
                    errors.append(f"停止后台按键检测时出错：{exc}")
                else:
                    remaining_request = None
            if listener is not None:
                try:
                    listener.stop()
                except Exception as exc:  # noqa: BLE001 - returned to Qt
                    errors.append(f"停止 Windows 按键通道时出错：{exc}")
                else:
                    remaining_listener = None
            if tap is not None:
                try:
                    tap.stop()
                except Exception as exc:  # noqa: BLE001 - returned to Qt
                    errors.append(f"停止补充按键通道时出错：{exc}")
                else:
                    remaining_tap = None
            return _InputStopResult(
                kind=kind,
                ok=not errors,
                message="；".join(errors),
                hotkey_capture=remaining_capture,
                bridge_request=remaining_request,
                listener=remaining_listener,
                tap=remaining_tap,
            )

        def _start_hotkey_capture_worker(
            self,
            cancel_event: threading.Event,
            operation_token: int,
        ) -> _InputStartResult:
            logger = logging_setup.get_logger(self._config_root)

            def report_capture(chord: str) -> None:
                logger.info(
                    "settings hotkey capture: hook emitted token=%s chord=%s",
                    operation_token,
                    chord,
                )
                self._hotkeyCaptureResult.emit((operation_token, chord))

            capture = hotkey_capture_windows.HotkeyCapture(
                report_capture,
                accept_injected=True,
                isolated=True,
                on_error=lambda message: self._hotkeyCaptureFailed.emit((operation_token, message)),
            )
            try:
                capture.start()
            except Exception as exc:  # noqa: BLE001 - returned to Qt
                logger.error(
                    "settings hotkey capture: hook start failed token=%s error=%s",
                    operation_token,
                    exc,
                )
                if getattr(capture, "is_running", False):
                    return _InputStartResult(
                        "hotkey",
                        False,
                        f"无法启动键盘快捷键录入：{exc}",
                        hotkey_capture=capture,
                    )
                return _InputStartResult(
                    "hotkey",
                    False,
                    f"无法启动键盘快捷键录入：{exc}",
                )
            if cancel_event.is_set():
                stopped = self._stop_input_resources(
                    "hotkey", hotkey_capture=capture
                )
                return _InputStartResult(
                    "hotkey",
                    False,
                    stopped.message,
                    hotkey_capture=stopped.hotkey_capture,
                )
            logger.info(
                "settings hotkey capture: hook ready token=%s running=%s",
                operation_token,
                bool(getattr(capture, "is_running", False)),
            )
            return _InputStartResult(
                "hotkey", True, hotkey_capture=capture
            )

        def _start_key_detection_worker(
            self,
            cancel_event: threading.Event,
            *,
            bridge_running: bool,
            physical_bindings: dict,
        ) -> _InputStartResult:
            from . import remote_selection
            selected_key = remote_selection.active_key(self._config)
            if not selected_key:
                return _InputStartResult("key_detection", False, "请先在设备页选择要使用的遥控器。")
            if bridge_running:
                try:
                    request = key_detection_bridge.request_detection(
                        self._config_root
                    )
                except OSError as exc:
                    return _InputStartResult(
                        "key_detection",
                        False,
                        f"无法向后台服务启动真实按键检测：{exc}",
                    )
                if cancel_event.is_set():
                    stopped = self._stop_input_resources(
                        "key_detection", bridge_request=request
                    )
                    return _InputStartResult(
                        "key_detection",
                        False,
                        stopped.message,
                        bridge_request=stopped.bridge_request,
                    )
                return _InputStartResult(
                    "key_detection", True, bridge_request=request
                )

            listener = None
            tap = None
            failures: List[str] = []
            try:
                paths = raw_input_windows.enumerate_matching_device_paths()
                device_path = (
                    remote_selection.selected_raw_path(paths, selected_key)
                )
                listener = raw_input_windows.RawInputButtonListener(
                    lambda *_: None,
                    self._on_raw_input_event,
                )
                set_physical_bindings = getattr(
                    listener, "set_physical_bindings", None
                )
                if callable(set_physical_bindings):
                    set_physical_bindings(physical_bindings)
                listener.start(device_path)
            except Exception:  # noqa: BLE001 - keep user text stable
                failures.append("Windows 按键通道启动失败")
                if listener is not None:
                    stopped = self._stop_input_resources(
                        "key_detection", listener=listener
                    )
                    listener = stopped.listener
                    if listener is not None:
                        return _InputStartResult(
                            "key_detection",
                            False,
                            stopped.message,
                            listener=listener,
                        )

            tap = frida_compat.RC003HidReportTap(
                self._on_key_detection_hid_report,
                selected_key=selected_key,
                status_handler=self._on_key_detection_tap_status,
            )
            try:
                if not tap.start():
                    failures.append("补充按键通道启动失败")
                    tap = None
            except Exception:  # noqa: BLE001 - keep user text stable
                failures.append("补充按键通道启动失败")
                stopped = self._stop_input_resources(
                    "key_detection", listener=listener, tap=tap
                )
                if stopped.listener is not None or stopped.tap is not None:
                    return _InputStartResult(
                        "key_detection",
                        False,
                        stopped.message,
                        listener=stopped.listener,
                        tap=stopped.tap,
                    )
                listener = None
                tap = None

            if cancel_event.is_set():
                stopped = self._stop_input_resources(
                    "key_detection", listener=listener, tap=tap
                )
                return _InputStartResult(
                    "key_detection",
                    False,
                    stopped.message,
                    listener=stopped.listener,
                    tap=stopped.tap,
                )
            if listener is None and tap is None:
                return _InputStartResult(
                    "key_detection",
                    False,
                    "无法启动真实按键检测：" + "；".join(failures),
                    failures=tuple(failures),
                )
            return _InputStartResult(
                "key_detection",
                True,
                listener=listener,
                tap=tap,
                failures=tuple(failures),
            )

        def _begin_input_operation_start(
            self,
            kind: str,
            worker: Callable[[threading.Event, int], _InputStartResult],
        ) -> bool:
            if self._remote_selection_busy or self._application_exit_requested or self._window_hide_requested:
                return False
            if self._get_input_capture_in_use():
                message = "另一项按键输入操作正在进行，请先结束。"
                if kind == "hotkey":
                    self.hotkeyCaptureError.emit(message)
                else:
                    self._set_key_detection_text(message)
                return False
            self._input_operation_token += 1
            token = self._input_operation_token
            self._take_input_worker_result()
            if kind == "hotkey":
                self._pending_hotkey_capture_result = None
                self._reset_qt_hotkey_capture_state()
            cancel_event = threading.Event()
            self._input_operation_cancel_event = cancel_event
            self._set_input_operation_state(kind, "starting")

            def run() -> None:
                try:
                    result = worker(cancel_event, token)
                except Exception as exc:  # noqa: BLE001 - marshal to Qt
                    result = _InputStartResult(
                        kind,
                        False,
                        f"按键输入操作启动失败：{type(exc).__name__}",
                    )
                self._record_input_worker_result(token, "start", result)
                self._emit_background_result(
                    self._inputOperationReady,
                    (token, "start", result),
                )

            try:
                self._start_background_task(run, f"{kind}-start")
            except Exception as exc:
                self._input_operation_cancel_event = None
                self._set_input_operation_state("", "idle")
                message = f"无法启动按键输入后台任务：{exc}"
                if kind == "hotkey":
                    self.hotkeyCaptureError.emit(message)
                else:
                    self._set_key_detection_text(message)
                return False
            return True

        def _request_input_stop(
            self,
            *,
            kind: str = "",
            key_detection_success_message: str = "",
        ) -> bool:
            if key_detection_success_message:
                self._pending_key_detection_stop_message = (
                    key_detection_success_message
                )
            if self._input_operation_phase == "starting":
                if kind and kind != self._input_operation_kind:
                    return False
                cancel_event = self._input_operation_cancel_event
                if cancel_event is not None:
                    cancel_event.set()
                return True
            if self._input_operation_phase == "stopping":
                return not kind or kind == self._input_operation_kind

            active_kind = self._input_operation_kind
            if not active_kind:
                if self._hotkey_capture is not None:
                    active_kind = "hotkey"
                elif (
                    self._key_detection_bridge_request is not None
                    or self._key_detection_listener is not None
                    or self._key_detection_tap is not None
                ):
                    active_kind = "key_detection"
            if kind and active_kind and kind != active_kind:
                return False
            if not active_kind:
                if not kind or kind == "hotkey":
                    self._reset_qt_hotkey_capture_state()
                self._set_key_detection_active_state(False)
                self._set_input_operation_state("", "idle")
                self._after_input_operation_change()
                return True

            self._input_operation_token += 1
            token = self._input_operation_token
            self._take_input_worker_result()
            self._set_input_operation_state(active_kind, "stopping")
            if active_kind == "hotkey":
                self._reset_qt_hotkey_capture_state()
            capture = self._hotkey_capture
            bridge_request = self._key_detection_bridge_request
            listener = self._key_detection_listener
            tap = self._key_detection_tap
            self._hotkey_capture = None
            self._key_detection_bridge_request = None
            self._key_detection_listener = None
            self._key_detection_tap = None

            def run() -> None:
                result = self._stop_input_resources(
                    active_kind,
                    hotkey_capture=capture,
                    bridge_request=bridge_request,
                    listener=listener,
                    tap=tap,
                )
                self._record_input_worker_result(token, "stop", result)
                self._emit_background_result(
                    self._inputOperationReady,
                    (token, "stop", result),
                )

            try:
                self._start_background_task(run, f"{active_kind}-stop")
            except Exception as exc:
                self._hotkey_capture = capture
                self._key_detection_bridge_request = bridge_request
                self._key_detection_listener = listener
                self._key_detection_tap = tap
                self._set_input_operation_state(active_kind, "active")
                message = f"无法启动按键输入停止任务：{exc}"
                if active_kind == "hotkey":
                    self.hotkeyCaptureError.emit(message)
                else:
                    self._set_key_detection_text(message)
                self._fail_pending_input_cleanup(message)
                return False
            return True

        def _on_input_operation_ready(self, payload: object) -> None:
            if self._background_shutdown_event.is_set():
                return
            token, action, result = payload
            if token != self._input_operation_token:
                return
            self._clear_input_worker_result(token, action)
            hotkey_start_cancelled = bool(
                action == "start" and result.kind == "hotkey"
                and self._input_operation_cancel_event is not None
                and self._input_operation_cancel_event.is_set()
            )
            self._input_operation_cancel_event = None
            if action == "start":
                self._hotkey_capture = result.hotkey_capture
                self._key_detection_bridge_request = result.bridge_request
                self._key_detection_listener = result.listener
                self._key_detection_tap = result.tap
                has_resource = bool(
                    result.hotkey_capture is not None
                    or result.bridge_request is not None
                    or result.listener is not None
                    or result.tap is not None
                )
                if result.ok or has_resource:
                    self._set_input_operation_state(result.kind, "active")
                else:
                    self._set_input_operation_state("", "idle")
                if result.kind == "hotkey":
                    if result.message:
                        self.hotkeyCaptureError.emit(result.message)
                    # The child's error/EOF and the start worker's ready
                    # signal can arrive in either order. Cancellation still
                    # owns the handoff after the worker's last flag check.
                    if hotkey_start_cancelled:
                        self._pending_hotkey_capture_result = None
                        if has_resource:
                            self._request_input_stop(kind="hotkey")
                        else:
                            self._after_input_operation_change()
                        return
                    pending_hotkey = self._pending_hotkey_capture_result
                    self._pending_hotkey_capture_result = None
                    if (
                        result.ok
                        and pending_hotkey is not None
                        and not self._window_hide_requested
                        and not self._application_exit_requested
                        and not self._input_cleanup_requested
                    ):
                        self._on_hotkey_capture_result(pending_hotkey)
                else:
                    self._set_key_detection_active_state(has_resource)
                    if result.ok:
                        self._key_detection_tap_usages.clear()
                        self._key_detection_started_at = time.monotonic()
                        if result.bridge_request is not None:
                            message = "请按下要检测的遥控器按键"
                        elif result.listener is not None and result.tap is not None:
                            message = (
                                "Windows 按键通道已启动；常规按键可立即检测，"
                                "返回键、音量键请等待补充通道连接（约 1 分钟）"
                            )
                        elif result.tap is not None:
                            message = (
                                "补充按键通道连接中；连接后请按要检测的按键"
                                "（约 1 分钟）"
                            )
                        else:
                            message = (
                                "只能检测 Windows 可识别按键；"
                                "返回键、音量键可能测不到"
                            )
                        limited = (
                            f"；受限：{'；'.join(result.failures)}"
                            if result.failures else ""
                        )
                        self._set_key_detection_text(
                            f"{message}；检测时不执行映射{limited}"
                        )
                    elif result.message:
                        self._set_key_detection_text(result.message)
                if (
                    self._window_hide_requested
                    or self._application_exit_requested
                    or self._input_cleanup_requested
                ) and self._get_input_capture_in_use():
                    self._request_input_stop()
                else:
                    self._after_input_operation_change()
                return

            self._hotkey_capture = result.hotkey_capture
            self._key_detection_bridge_request = result.bridge_request
            self._key_detection_listener = result.listener
            self._key_detection_tap = result.tap
            has_resource = bool(
                result.hotkey_capture is not None
                or result.bridge_request is not None
                or result.listener is not None
                or result.tap is not None
            )
            if has_resource:
                self._set_input_operation_state(result.kind, "active")
            else:
                self._set_input_operation_state("", "idle")
            if result.kind == "key_detection":
                self._set_key_detection_active_state(has_resource)
                if not has_resource and self._pending_key_detection_stop_message:
                    self._set_key_detection_text(
                        self._pending_key_detection_stop_message
                    )
                self._pending_key_detection_stop_message = ""
            elif result.kind == "hotkey":
                self._pending_hotkey_capture_result = None
            if result.message:
                if result.kind == "hotkey":
                    self.hotkeyCaptureError.emit(result.message)
                else:
                    self._set_key_detection_text(result.message)
                self._fail_pending_input_cleanup(result.message)
                return
            self._after_input_operation_change()

        def _fail_pending_input_cleanup(self, message: str) -> None:
            if self._input_cleanup_requested:
                self._input_cleanup_requested = False
                self.inputCleanupFailed.emit(message)
            if self._window_hide_requested:
                self._window_hide_requested = False
                self.windowHideFailed.emit(message)
            if self._application_exit_requested:
                self._fail_application_exit(message)

        def _after_input_operation_change(self) -> None:
            if self._input_cleanup_requested and not self._get_input_capture_in_use():
                self._input_cleanup_requested = False
                self.inputCleanupReady.emit()
            if self._window_hide_requested and not self._get_input_capture_in_use():
                self._window_hide_requested = False
                self.windowHideReady.emit()
            self._schedule_application_exit_poll()

        @Slot()
        def prepareForWindowHide(self) -> None:
            if self._window_hide_requested:
                return
            if not self._get_input_capture_in_use():
                self.windowHideReady.emit()
                return
            self._window_hide_requested = True
            if not self._request_input_stop():
                self._window_hide_requested = False
                self.windowHideFailed.emit(
                    "无法停止正在进行的按键录入或检测。"
                )

        @Slot(result=bool)
        def stopInputCapture(self) -> bool:
            """Stop any active input operation before navigation or prompts."""

            if self._input_cleanup_requested:
                return True
            if not self._get_input_capture_in_use():
                self.inputCleanupReady.emit()
                return True
            self._input_cleanup_requested = True
            if self._request_input_stop():
                return True
            self._input_cleanup_requested = False
            self.inputCleanupFailed.emit(
                "无法停止正在进行的按键录入或检测。"
            )
            return False

        def _voice_settings_write_block_reason(self) -> str:
            if self.configurationReadOnly:
                return self._configuration_error
            if _vb_cable_test_active_event.is_set():
                return "VB-CABLE 通道测试正在运行；测试结束后再修改语音设置。"
            if self._get_bridge_launch_busy():
                return "遥控器服务正在启动；完成后再修改语音设置。"
            if self._endpoint_preflight_busy or _driver_action_active_event.is_set():
                return "输出端点正在处理；完成后再修改语音设置。"
            return ""

        def _voice_settings_write_start_blocked(self) -> bool:
            return bool(
                self._voice_settings_write_block_reason()
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            )

        def _on_raw_input_event(self, event: raw_input_windows.RawInputEvent) -> None:
            if not event.is_pressed:
                return
            if event.source == "keyboard":
                vkey = "--" if event.vkey is None else f"0x{event.vkey:02X}"
                make_code = "--" if event.make_code is None else f"0x{event.make_code:02X}"
                flags = "--" if event.flags is None else f"0x{event.flags:04X}"
                details = (
                    f"Windows 按键事件：键值={vkey}，扫描码={make_code}，"
                    f"标志={flags}"
                )
            else:
                details = f"Windows 按键报告：{event.report.hex(' ')}"
            if event.usages:
                details += "，按键值=" + ",".join(
                    f"0x{usage:04X}" for usage in event.usages
                )
            if event.decode_error:
                details += "，报告未能完整识别"
            self._rawKeyDetected.emit(event.button_id or "", details)

        def _on_raw_key_detected(self, button_id: str, details: str) -> None:
            """Handle one physical press on the Qt GUI thread.

            Raw Input emits only a logical button id; this detector never
            executes the configured action. It stops after the first press,
            selects the corresponding row, and leaves the user in control of
            choosing/saving the Windows mapping.
            """

            if (
                not self._key_detection_active
                or self._input_operation_phase != "active"
            ):
                return
            stop_requested = self.stopKeyDetection()
            if button_id:
                self.selectButton(button_id)
                display_name = remote_layout.display_names(self.activeRemoteProfile).get(button_id, button_id)
                result = f"已检测：{display_name}；修改完成后会自动保存"
            else:
                result = "检测到未知按键"
            if not stop_requested:
                result += "；检测未停止，请点击“停止检测”"
            elif not button_id:
                detail_text = details.strip().rstrip("。")
                if detail_text:
                    result += f"；{detail_text}"
            self._set_key_detection_text(result)

        def _on_key_detection_hid_report(self, report_id: int, payload: bytes) -> None:
            if (
                report_id != 1
                or len(payload) != 6
                or not self._key_detection_active
                or self._input_operation_phase != "active"
            ):
                return
            active = {
                int.from_bytes(payload[index : index + 2], "little")
                for index in range(0, len(payload), 2)
            } & set(self._KEY_DETECTION_USAGE_TO_BUTTON)
            pressed = active - self._key_detection_tap_usages
            self._key_detection_tap_usages = set(active)
            if not pressed:
                return
            usage = sorted(pressed)[0]
            button_id = self._KEY_DETECTION_USAGE_TO_BUTTON[usage]
            self._rawKeyDetected.emit(
                button_id,
                f"补充按键报告：按键值=0x{usage:04X}",
            )

        def _on_key_detection_tap_status(self, status: str, detail: str) -> None:
            self._hidTapDetectionStatus.emit(status, detail)

        def _on_hid_tap_detection_status(self, status: str, detail: str) -> None:
            if (
                not self._key_detection_active
                or self._input_operation_phase != "active"
            ):
                return
            if status in {"authorization_cancelled", "authorization_failed"}:
                self._set_key_detection_text("按键授权未完成；请回设备页点击“重新授权”")
            elif status == frida_compat.HidTapState.ATTACHED_WAITING_IO.value:
                if self._key_detection_listener is not None:
                    waiting_text = (
                        "补充按键通道已连接；请按要检测的按键，常规按键也可继续检测"
                    )
                else:
                    waiting_text = "补充按键通道已连接；请按要检测的按键"
                self._set_key_detection_text(waiting_text)
            elif status == frida_compat.HidTapState.READY.value:
                if self._key_detection_listener is not None:
                    ready_text = (
                        "两条按键通道均已就绪；13 个已知按键均可检测，检测时不执行映射"
                    )
                else:
                    ready_text = "补充按键通道已就绪；请按要检测的按键，检测时不执行映射"
                self._set_key_detection_text(ready_text)
            elif status in {
                frida_compat.HidTapState.FAILED.value,
                frida_compat.HidTapState.UNHEALTHY.value,
            }:
                detail_text = {
                    "gadget_connection_closed": "连接已关闭",
                }.get(detail, "")
                suffix = f"（{detail_text}）" if detail_text else ""
                self._set_key_detection_text(
                    f"补充按键通道暂不可用{suffix}；返回键、音量键可能测不到，正在重连"
                )

        def _on_hotkey_capture_failed(self, payload: object) -> None:
            token, message = payload
            if token != self._input_operation_token or self._input_operation_kind != "hotkey":
                return
            self._pending_hotkey_capture_result = None
            self._request_input_stop(kind="hotkey")
            self.hotkeyCaptureError.emit(str(message))

        def _on_hotkey_capture_result(self, payload: object) -> None:
            """Forward a hook-thread result to QML on the GUI thread."""

            logger = logging_setup.get_logger(self._config_root)
            try:
                operation_token, chord = payload
                operation_token = int(operation_token)
                chord = str(chord)
            except (TypeError, ValueError):
                logger.warning("settings hotkey capture: malformed hook result ignored")
                return
            if (
                operation_token != self._input_operation_token
                or self._input_operation_kind != "hotkey"
                or self._input_cleanup_requested
                or self._window_hide_requested
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                logger.warning(
                    "settings hotkey capture: result ignored token=%s current_token=%s "
                    "kind=%s phase=%s chord=%s",
                    operation_token,
                    self._input_operation_token,
                    self._input_operation_kind,
                    self._input_operation_phase,
                    chord,
                )
                return
            if self._input_operation_phase == "starting":
                if self._pending_hotkey_capture_result is None:
                    self._pending_hotkey_capture_result = (operation_token, chord)
                    logger.info(
                        "settings hotkey capture: result queued during start "
                        "token=%s chord=%s",
                        operation_token,
                        chord,
                    )
                return
            if (
                self._input_operation_phase != "active"
                or self._hotkey_capture is None
            ):
                logger.warning(
                    "settings hotkey capture: active result had no live owner "
                    "token=%s phase=%s chord=%s",
                    operation_token,
                    self._input_operation_phase,
                    chord,
                )
                return
            logger.info(
                "settings hotkey capture: forwarding to QML token=%s chord=%s",
                operation_token,
                chord,
            )
            self._reset_qt_hotkey_capture_state()
            self.hotkeyCaptured.emit(chord)

        def _save_mapping(
            self,
            completion: Optional[Callable[[bool], None]] = None,
            *, replacement: Optional[dict] = None,
        ) -> bool:
            """Persist the button-page model without adopting other page drafts."""

            def finish(result: bool) -> bool:
                if completion is not None:
                    completion(bool(result))
                return bool(result)

            if self.configurationReadOnly:
                self._mapping_log().warning(
                    "mapping save skipped: configuration is read-only"
                )
                return finish(False)

            saved_voice_hotkeys = self._config.get("voice_hotkeys", {})
            saved_hotkey = str(
                saved_voice_hotkeys.get(
                    "hold",
                    self._config.get("voice_hotkey", ""),
                )
            )
            saved_endpoint_name = str(
                self._config.get("output_endpoint_name", "")
            )
            saved_endpoint_host_api = str(
                self._config.get("output_endpoint_host_api", "")
            )
            saved_endpoint_display = (
                settings_ui._endpoint_display(
                    audio_output.AudioEndpoint(
                        name=saved_endpoint_name,
                        host_api=saved_endpoint_host_api,
                    )
                )
                if saved_endpoint_name
                else ""
            )
            try:
                _, new_bindings = settings_ui.build_save_model(
                    button_display_map=self._model.to_display_map(),
                    secondary_display_map=self._model.to_secondary_display_map(),
                    display_note_map=self._model.to_display_note_map(),
                    hotkey_text=saved_hotkey,
                    endpoint_display_text=saved_endpoint_display,
                    base_config=self._config,
                    base_bindings=self._active_edit_bindings(),
                    selected_device_profile=str(
                        self._config.get(
                            "selected_device_profile",
                            device_catalog.RC003_ID,
                        )
                    ),
                    voice_hotkeys={"hold": saved_hotkey},
                    voice_slot_hotkeys={
                        str(slot): self._voice_slot_hotkeys.get(slot, "")
                        for slot in sorted(self._voice_slot_hotkeys)
                    },
                    button_inject_modes=self._model.to_inject_mode_map(),
                    validate_voice_hotkeys=False,
                )
            except settings_ui.SettingsValidationError as exc:
                button_name = (
                    "话筒键"
                    if exc.button_id == "mic"
                    else remote_layout.BUTTON_DISPLAY_NAMES.get(
                        exc.button_id, exc.button_id
                    )
                )
                title = f"「{button_name}」映射无效" if button_name else "语音热键无效"
                self._set_error_message(
                    f"{title}：{exc.message}",
                    self._BUTTONS_PAGE_INDEX,
                )
                self._mapping_log().warning(
                    "mapping save rejected: button=%s message=%s",
                    exc.button_id or "-",
                    exc.message,
                )
                return finish(False)

            if replacement is not None:
                new_bindings = replacement
            if self._mapping_edit_target:
                store = app_profiles.AppProfileStore(self._config_root)
                try:
                    store.update_mapping(self._mapping_edit_target,
                                         app_profiles.snapshot(new_bindings, self._selected_device_id()))
                    app_name = store.get(self._mapping_edit_target)["name"]
                except Exception as exc:  # noqa: BLE001 - a Qt slot must not escape
                    self._set_error_message(
                        f"保存失败：{exc}",
                        self._BUTTONS_PAGE_INDEX,
                    )
                    self._mapping_log().warning(
                        "mapping save failed: target=app_profile:%s error=%s",
                        self._mapping_edit_target,
                        exc,
                    )
                    return finish(False)
                self._set_mapping_dirty(False)
                self._set_mapping_auto_save_retry_available(False)
                self._refresh_settings_dirty_state()
                try:
                    self._refresh_mapping_presets()
                except Exception:  # noqa: BLE001 - the save already committed
                    pass
                self._mapping_log().info(
                    "mapping saved: target=app_profile:%s buttons=%d",
                    self._mapping_edit_target,
                    len(new_bindings.get("bindings", {})),
                )
                self._set_status_message(
                    f"「{app_name}」的按键映射已自动保存；切换到该应用时生效。",
                    self._BUTTONS_PAGE_INDEX,
                )
                return finish(True)
            config_path = config.config_path(self._config_root)
            bindings_path = config.key_bindings_path(self._config_root)
            try:
                config.save_settings_pair(
                    config_path,
                    dict(self._config),
                    bindings_path,
                    new_bindings,
                )
                saved_config = config.load_config(config_path)
                saved_bindings = config.load_key_bindings(bindings_path)
            except Exception as exc:  # noqa: BLE001 - a Qt slot must not escape
                self._set_error_message(
                    f"保存失败：{exc}",
                    self._BUTTONS_PAGE_INDEX,
                )
                self._mapping_log().warning(
                    "mapping save failed: target=key_bindings.json error=%s", exc
                )
                return finish(False)

            previous_voice_mapping_ready = self._get_voice_mapping_ready()
            self._config = saved_config
            self._bindings = saved_bindings
            self._bump_settings_revision()
            self._removed_voice_bindings = config.normalize_voice_product_boundary(
                self._config,
                self._bindings,
            )
            if previous_voice_mapping_ready != self._get_voice_mapping_ready():
                self.voiceMappingReadyChanged.emit()
            self._load_bindings_into_model()
            self._set_mapping_dirty(False)
            unsaved_other_settings = self._has_unsaved_non_mapping_settings()
            self._refresh_settings_dirty_state()
            self._set_mapping_auto_save_retry_available(False)
            status = "按键映射已自动保存。"
            if unsaved_other_settings:
                status += "其它设置仍未保存。"
            else:
                status += "将在下一次按键时应用。"
            self._mapping_log().info(
                "mapping saved: target=key_bindings.json buttons=%d",
                len(self._bindings.get("bindings", {})),
            )
            self._set_status_message(status, self._BUTTONS_PAGE_INDEX)
            return finish(True)

        def _save(
            self,
            completion: Optional[Callable[[bool], None]] = None,
        ) -> bool:
            """Same validation as before (settings_ui.build_save_model);
            returns True only on an actual successful save, so
            saveAndLaunch() can gate the launch on it exactly like the
            previous Tk _save_and_launch() did.
            """

            def finish(result: bool) -> bool:
                if completion is not None:
                    completion(bool(result))
                if self._application_exit_waiting_for_save:
                    if result:
                        self._begin_application_exit()
                    else:
                        self._fail_application_exit(
                            "设置保存未完成，程序没有退出。"
                        )
                return bool(result)

            if self.configurationReadOnly:
                return finish(False)

            if self._settings_save_busy:
                self._set_error_message(
                    "按键映射正在保存，请等待完成。",
                    self._BUTTONS_PAGE_INDEX,
                )
                return finish(False)

            if self._voice_hotkey_busy:
                self._set_error_message(
                    "语音快捷键正在处理；完成后再保存设置。",
                    self._BUTTONS_PAGE_INDEX,
                )
                return finish(False)

            if _vb_cable_test_active_event.is_set():
                self._set_error_message(
                    "VB-CABLE 通道测试正在运行；测试结束后再保存设置。",
                    self._BUTTONS_PAGE_INDEX,
                )
                return finish(False)

            endpoint_display = (
                self._endpoint_options[self._selected_endpoint_index]
                if 0 <= self._selected_endpoint_index < len(self._endpoint_options)
                else ""
            )
            try:
                new_config, new_bindings = settings_ui.build_save_model(
                    button_display_map=self._model.to_display_map(),
                    secondary_display_map=self._model.to_secondary_display_map(),
                    display_note_map=self._model.to_display_note_map(),
                    hotkey_text=self._voice_hotkey,
                    endpoint_display_text=endpoint_display,
                    base_config=self._config,
                    base_bindings=self._bindings,
                    selected_device_profile=self._selected_device_id(),
                    voice_hotkeys={"hold": self._voice_hotkey},
                    voice_slot_hotkeys={
                        str(slot): self._voice_slot_hotkeys.get(slot, "")
                        for slot in sorted(self._voice_slot_hotkeys)
                    },
                    button_inject_modes=self._model.to_inject_mode_map(),
                )
            except settings_ui.SettingsValidationError as exc:
                button_name = (
                    "话筒键"
                    if exc.button_id == "mic"
                    else remote_layout.BUTTON_DISPLAY_NAMES.get(
                        exc.button_id, exc.button_id
                    )
                )
                title = f"「{button_name}」映射无效" if button_name else "语音热键无效"
                self._set_error_message(
                    f"{title}：{exc.message}",
                    self._BUTTONS_PAGE_INDEX,
                )
                return finish(False)

            if self._mapping_edit_target:
                # The button page is editing one application's layout. Saving
                # the settings page must never write that layout into the
                # global key_bindings.json, so fall back to the global file.
                try:
                    new_bindings = config.load_key_bindings(
                        config.key_bindings_path(self._config_root)
                    )
                except Exception:  # noqa: BLE001 - keep the built model on failure
                    pass

            endpoint_name = new_config.get("output_endpoint_name", "")
            endpoint_host_api = new_config.get("output_endpoint_host_api", "")
            save_revision = self._settings_revision

            def primary_voice_enabled(document: dict) -> bool:
                raw_bindings = document.get("bindings", {})
                if not isinstance(raw_bindings, dict):
                    return False
                for raw_action in raw_bindings.values():
                    if not isinstance(raw_action, dict):
                        continue
                    try:
                        action = key_mapping.ButtonAction.from_dict(raw_action)
                    except (KeyError, TypeError, ValueError):
                        continue
                    if key_mapping.is_voice_action(action):
                        return True
                return False

            primary_bindings = new_bindings.get("bindings", {})
            voice_mapping_enabled = primary_voice_enabled(
                {"bindings": primary_bindings}
            )
            previous_voice_mapping_enabled = primary_voice_enabled(self._bindings)
            endpoint_changed = (
                endpoint_name != self._config.get("output_endpoint_name", "")
                or endpoint_host_api
                != self._config.get("output_endpoint_host_api", "")
            )
            requires_preflight = bool(
                endpoint_name
                and voice_mapping_enabled
                and (endpoint_changed or not previous_voice_mapping_enabled)
            )

            def persist_candidate() -> bool:
                config_path = config.config_path(self._config_root)
                bindings_path = config.key_bindings_path(self._config_root)
                try:
                    config.save_settings_pair(
                        config_path,
                        new_config,
                        bindings_path,
                        new_bindings,
                    )
                    saved_config = config.load_config(config_path)
                    saved_bindings = config.load_key_bindings(bindings_path)
                except Exception as exc:  # noqa: BLE001 - a Qt slot must not escape
                    self._set_error_message(
                        f"保存失败：{exc}",
                        self._BUTTONS_PAGE_INDEX,
                    )
                    return finish(False)

                previous_voice_mapping_ready = self._get_voice_mapping_ready()
                self._record_saved_output_change(saved_config)
                self._config = saved_config
                self._bindings = saved_bindings
                self._bump_settings_revision()
                self._removed_voice_bindings = (
                    config.normalize_voice_product_boundary(
                        self._config,
                        self._bindings,
                    )
                )
                if previous_voice_mapping_ready != self._get_voice_mapping_ready():
                    self.voiceMappingReadyChanged.emit()
                self._set_voice_hotkey_text(self._voice_hotkey)
                self._load_bindings_into_model()
                self._set_settings_dirty(False)
                self._set_mapping_dirty(False)
                self._set_mapping_auto_save_retry_available(False)
                self._set_error_message("")
                self._set_status_message(
                    (
                        "按键映射已自动保存。"
                        if self._mapping_auto_save_running
                        else "已保存。"
                    )
                    + "按键映射和语音触发将在下一次按键时应用；"
                    "连接或输出设置需重启遥控器服务。",
                    self._BUTTONS_PAGE_INDEX,
                )
                return finish(True)

            if not requires_preflight:
                return persist_candidate()

            self._set_settings_save_busy(True)
            self._set_error_message("")
            self._set_status_message(
                "正在检查语音输出端点…",
                self._BUTTONS_PAGE_INDEX,
            )
            settled = False
            settled_result = False

            def after_preflight(ok: bool, message: str) -> None:
                nonlocal settled, settled_result
                self._set_settings_save_busy(False)
                if not ok:
                    self._set_status_message("")
                    self._set_error_message(
                        "保存失败："
                        + (message or "所选语音输出设备无法实际打开。"),
                        self._BUTTONS_PAGE_INDEX,
                    )
                    settled_result = finish(False)
                elif self._settings_revision != save_revision:
                    self._set_status_message("")
                    self._set_settings_dirty(True)
                    self._set_error_message(
                        "保存期间设置又发生了变化；为避免覆盖新修改，请重新保存。",
                        self._BUTTONS_PAGE_INDEX,
                    )
                    settled_result = finish(False)
                else:
                    settled_result = persist_candidate()
                settled = True
                self._schedule_application_exit_poll()

            accepted = self._request_endpoint_preflight(
                str(endpoint_name),
                str(endpoint_host_api),
                after_preflight,
            )
            return settled_result if settled else accepted

        # -- properties ---------------------------------------------------

        def _slots_from_config(self, config_data: dict) -> Dict[int, str]:
            """Resolve every configured voice key's shortcut by its id.

            An empty entry means "not recorded yet"; slot 1 then mirrors the
            shared voice shortcut at read time (see
            :func:`_effective_voice_slot_hotkey`)."""

            return {
                int(entry["id"]): str(entry.get("hotkey", "")).strip()
                for entry in config.voice_key_configs(config_data)
            }

        def _sync_voice_key_cache(self) -> None:
            """Refresh the cached shortcuts and the button-page labels."""

            self._voice_slot_hotkeys = self._slots_from_config(self._config)
            settings_ui.set_voice_slot_labels(
                config.voice_key_labels(self._config)
            )

        def _get_hotkey_text(self) -> str:
            return self._voice_hotkey

        def _set_hotkey_text(self, value: str) -> None:
            if self._set_voice_hotkey_text(value):
                self._mark_settings_dirty()

        hotkeyText = Property(str, _get_hotkey_text, _set_hotkey_text, notify=hotkeyTextChanged)

        def _set_voice_hotkey_text(self, value: str) -> bool:
            if value == self._voice_hotkey:
                return False
            self._voice_hotkey = value
            self.holdVoiceHotkeyTextChanged.emit()
            self.hotkeyTextChanged.emit()
            return True

        def _get_hold_voice_hotkey_text(self) -> str:
            return self._voice_hotkey

        def _set_hold_voice_hotkey_text(self, value: str) -> None:
            logging_setup.get_logger(self._config_root).info(
                "settings hotkey capture: QML submitted chord=%s",
                value,
            )
            self._update_and_persist_hold_voice_hotkey(value)

        def _update_and_persist_hold_voice_hotkey(self, value: str) -> bool:
            # The hold shortcut is stored locally only: the voice key replays
            # exactly what the user recorded, so nothing is synced to a
            # third-party program here.
            previous = self._voice_hotkey
            text = str(value).strip()
            try:
                text = hotkey.HotkeySpec.from_user_text(text).serialize()
            except hotkey.HotkeyParseError as exc:
                self._set_error_message(str(exc), self._VOICE_PAGE_INDEX)
                self.hotkeyCaptureError.emit(str(exc))
                return False
            if text == previous:
                return True
            new_config = dict(self._config)
            new_config["voice_hotkey"] = text
            new_config["voice_hotkeys"] = {"hold": text}
            config_path = config.config_path(self._config_root)
            try:
                saved_config = config.save_config_and_load(config_path, new_config)
            except Exception as exc:  # noqa: BLE001 - a Qt slot must not escape
                self._set_error_message(
                    f"语音设置保存失败：{exc}",
                    self._VOICE_PAGE_INDEX,
                )
                return False
            self._config = saved_config
            self._set_voice_hotkey_text(text)
            self._bump_settings_revision()
            self._refresh_settings_dirty_state()
            self._set_error_message("")
            self._set_status_message(
                "语音设置已自动保存。" + self._mapping_save_status_suffix(),
                self._VOICE_PAGE_INDEX,
            )
            return True

        holdVoiceHotkeyText = Property(
            str,
            _get_hold_voice_hotkey_text,
            _set_hold_voice_hotkey_text,
            notify=holdVoiceHotkeyTextChanged,
        )

        def _effective_voice_slot_hotkey(self, slot: int) -> str:
            """The shortcut shown/used for one slot.

            An empty slot 1 means "not recorded yet" and mirrors the shared
            hold shortcut, matching :func:`config.voice_slot_hotkey`."""

            text = str(self._voice_slot_hotkeys.get(int(slot), ""))
            if not text and int(slot) == 1:
                return str(self._voice_hotkey or "")
            return text

        def _get_voice_slot_hotkeys(self) -> Dict[str, str]:
            return {
                str(slot): self._effective_voice_slot_hotkey(slot)
                for slot in sorted(self._voice_slot_hotkeys)
            }

        voiceSlotHotkeys = Property(
            "QVariantMap",
            _get_voice_slot_hotkeys,
            notify=voiceSlotHotkeysChanged,
        )

        @Slot(int, result=str)
        def voiceSlotHotkeyText(self, slot: int) -> str:
            return self._effective_voice_slot_hotkey(int(slot))

        @Slot(int, result=str)
        def voiceSlotLabel(self, slot: int) -> str:
            return settings_ui.voice_slot_display(int(slot))

        def _get_active_voice_slots(self) -> List[int]:
            """Slots currently assigned to a physical button, in slot order."""

            display_map = self._model.to_display_map()
            slots = {
                settings_ui.voice_slot_for_display(text)
                for text in display_map.values()
            }
            return sorted(slot for slot in slots if slot is not None)

        activeVoiceSlots = Property(
            list, _get_active_voice_slots, notify=voiceSlotHotkeysChanged
        )

        @Slot(str, result=bool)
        def isVoiceActionText(self, text: str) -> bool:
            return settings_ui.voice_slot_for_display(text) is not None

        @Slot(str, result=bool)
        def isVoiceModeCycleText(self, text: str) -> bool:
            return settings_ui.is_voice_mode_cycle_display(text)

        def _get_voice_slot_options(self) -> List[str]:
            return [
                settings_ui.voice_slot_display(slot)
                for slot in sorted(self._voice_slot_hotkeys)
            ]

        voiceSlotOptions = Property(
            list, _get_voice_slot_options, notify=voiceKeyConfigsChanged
        )

        def _inject_mode_options(self) -> List[dict]:
            """The per-button backend choices, with live driver availability."""

            from . import injection_backend

            return [
                {
                    "value": mode,
                    "label": injection_backend.INJECT_MODE_LABELS.get(mode, mode),
                    "available": injection_backend.mode_available(mode),
                    "reason": injection_backend.mode_status(mode)[1],
                }
                for mode in key_mapping.INJECT_MODES
            ]

        injectModeOptions = Property(
            list, _inject_mode_options, notify=injectModesChanged
        )

        @Property(bool, notify=injectModesChanged)
        def interceptionAvailable(self) -> bool:
            from . import injection_backend

            return injection_backend.interception_available()

        @Property(str, notify=injectModesChanged)
        def interceptionUnavailableReason(self) -> str:
            from . import injection_backend

            return injection_backend.interception_unavailable_reason()

        @Property(str, notify=injectModesChanged)
        def interceptionUnavailableHint(self) -> str:
            """User-facing, reason-specific explanation for an unusable driver."""

            from . import injection_backend

            return injection_backend.interception_unavailable_hint()

        @Slot(str, result=str)
        def buttonInjectMode(self, button_id: str) -> str:
            return self._model.inject_mode_for(str(button_id))

        @Slot(str, str)
        def setButtonInjectMode(self, button_id: str, mode: str) -> None:
            row = self._model.index_of(str(button_id))
            if row < 0:
                return
            self._model.setInjectModeAt(row, mode)

        @Property(bool, notify=injectModesChanged)
        def interceptionInstallerAvailable(self) -> bool:
            """Whether this build ships the official Interception installer.

            Distinct from ``interceptionAvailable`` (the driver is loaded):
            the installer can be present while the driver is not yet installed.
            """

            from . import interception_driver_install

            return interception_driver_install.installer_available()

        @Property(str, notify=injectModesChanged)
        def interceptionInstallerUnavailableReason(self) -> str:
            from . import interception_driver_install

            return interception_driver_install.installer_unavailable_reason()

        def _launch_interception_installer(self, uninstall: bool) -> dict:
            """Shared body for the install/uninstall slots.

            Only ever reached from an explicit user click in the button
            editor.  The elevated installer is awaited, so a failed run is
            reported with its exit code instead of looking like a success.
            Success still means "reboot required": the driver is not usable
            until Windows restarts.
            """

            from . import interception_driver_install

            verb = "卸载" if uninstall else "安装"
            try:
                if uninstall:
                    interception_driver_install.uninstall_driver()
                else:
                    interception_driver_install.install_driver()
            except interception_driver_install.DriverInstallCancelledError as exc:
                return {"ok": False, "cancelled": True, "message": str(exc)}
            except interception_driver_install.DriverInstallerError as exc:
                return {"ok": False, "cancelled": False, "message": str(exc)}
            self.injectModesChanged.emit()
            return {
                "ok": True,
                "cancelled": False,
                "message": (
                    f"Interception 驱动{verb}程序已成功结束（退出码 0）。"
                    f"请重启电脑，重启后回到本页重新选择注入方式。"
                ),
            }

        @Slot(result="QVariantMap")
        def installInterceptionDriver(self) -> dict:
            return self._launch_interception_installer(uninstall=False)

        @Slot(result="QVariantMap")
        def uninstallInterceptionDriver(self) -> dict:
            return self._launch_interception_installer(uninstall=True)

        def _save_voice_config_data(
            self, new_config: Dict[str, Any], *, restart_capture: bool = False
        ) -> bool:
            """Persist one voice-page change and refresh every dependent view."""

            previous_labels = config.voice_key_labels(self._config)
            config_path = config.config_path(self._config_root)
            try:
                saved_config = config.save_config_and_load(config_path, new_config)
            except Exception as exc:  # noqa: BLE001 - a Qt slot must not escape
                self._set_error_message(
                    f"语音设置保存失败：{exc}",
                    self._VOICE_PAGE_INDEX,
                )
                return False
            self._config = saved_config
            self._sync_voice_key_cache()
            self._model.relabel_voice_actions(
                previous_labels, config.voice_key_labels(saved_config)
            )
            self._bump_settings_revision()
            for signal in (
                self.voiceSlotHotkeysChanged,
                self.voiceKeyConfigsChanged,
                self.voiceInputSourceChanged,
                self.voiceShortPressDismissChanged,
                self.voiceCycleConfigChanged,
                self.localMicrophoneDeviceChanged,
                self.voiceMappingReadyChanged,
            ):
                signal.emit()
            if restart_capture:
                self._restart_microphone_level_capture()
            self._set_error_message("")
            self._set_status_message(
                "语音设置已自动保存。" + self._mapping_save_status_suffix(),
                self._VOICE_PAGE_INDEX,
            )
            return True

        @Slot(int, str)
        def setVoiceSlotHotkey(self, slot: int, value: str) -> None:
            self._update_and_persist_voice_slot_hotkey(int(slot), value)

        def _update_and_persist_voice_slot_hotkey(self, slot: int, value: str) -> bool:
            text = str(value).strip()
            if text == self._voice_slot_hotkeys.get(slot, ""):
                return True
            if text:
                try:
                    text = hotkey.HotkeySpec.from_user_text(text).serialize()
                except hotkey.HotkeyParseError as exc:
                    self._set_error_message(str(exc), self._VOICE_PAGE_INDEX)
                    self.hotkeyCaptureError.emit(str(exc))
                    return False
            return self._save_voice_config_data(
                config.update_voice_key_config(self._config, slot, hotkey=text)
            )

        def _get_voice_input_source(self) -> str:
            return config.voice_input_source(self._config)

        voiceInputSource = Property(
            str, _get_voice_input_source, notify=voiceInputSourceChanged
        )

        # -- 语音页：短按切换模式与模式切换气泡 -----------------------------

        def _get_voice_short_press_dismiss(self) -> bool:
            return config.voice_short_press_dismiss(self._config)

        voiceShortPressDismiss = Property(
            bool,
            _get_voice_short_press_dismiss,
            notify=voiceShortPressDismissChanged,
        )

        @Slot(bool)
        def setVoiceShortPressDismiss(self, enabled: bool) -> None:
            enabled = bool(enabled)
            if enabled == self._get_voice_short_press_dismiss():
                return
            new_config = dict(self._config)
            # 只改自己这个开关：它和“短按切换模式”可以同时打开，
            # 不能因为保存一项就把另一项静默清掉。
            new_config["voice_short_press_dismiss"] = enabled
            self._save_voice_config_data(new_config)

        def _get_voice_cycle_short_press(self) -> bool:
            return config.voice_cycle_short_press(self._config)

        voiceCycleShortPress = Property(
            bool, _get_voice_cycle_short_press, notify=voiceCycleConfigChanged
        )

        @Slot(bool)
        def setVoiceCycleShortPress(self, enabled: bool) -> None:
            enabled = bool(enabled)
            if enabled == self._get_voice_cycle_short_press():
                return
            new_config = dict(self._config)
            new_config["voice_cycle_short_press"] = enabled
            self._save_voice_config_data(new_config)

        def _get_voice_other_key_switch(self) -> bool:
            return config.voice_other_key_switch(self._config)

        voiceOtherKeySwitch = Property(
            bool,
            _get_voice_other_key_switch,
            notify=voiceCycleConfigChanged,
        )

        @Slot(bool)
        def setVoiceOtherKeySwitch(self, enabled: bool) -> None:
            enabled = bool(enabled)
            if enabled == self._get_voice_other_key_switch():
                return
            new_config = dict(self._config)
            new_config["voice_other_key_switch"] = enabled
            self._save_voice_config_data(new_config)

        _VOICE_CYCLE_TOAST_ORDER = (
            config.VOICE_CYCLE_TOAST_DARK,
            config.VOICE_CYCLE_TOAST_LIGHT,
            config.VOICE_CYCLE_TOAST_OFF,
        )

        def _get_voice_cycle_toast_theme(self) -> str:
            return config.voice_cycle_toast_theme(self._config)

        voiceCycleToastTheme = Property(
            str, _get_voice_cycle_toast_theme, notify=voiceCycleConfigChanged
        )

        def _get_voice_cycle_toast_theme_index(self) -> int:
            try:
                return self._VOICE_CYCLE_TOAST_ORDER.index(
                    self._get_voice_cycle_toast_theme()
                )
            except ValueError:
                return 0

        voiceCycleToastThemeIndex = Property(
            int, _get_voice_cycle_toast_theme_index, notify=voiceCycleConfigChanged
        )

        voiceCycleToastThemeOptions = Property(
            list, lambda self: ["深色", "浅色", "关闭"], constant=True
        )

        @Slot(int)
        def setVoiceCycleToastThemeIndex(self, index: int) -> None:
            if index < 0 or index >= len(self._VOICE_CYCLE_TOAST_ORDER):
                return
            new_config = dict(self._config)
            new_config["voice_cycle_toast_theme"] = self._VOICE_CYCLE_TOAST_ORDER[index]
            self._save_voice_config_data(new_config)

        def _persist_voice_config_fields(self, updates: Dict[str, Any]) -> bool:
            new_config = dict(self._config)
            new_config.update(updates)
            if (
                "voice_input_source" in updates
                or "voice_local_input_device" in updates
            ):
                # 语音模式现在属于每一条语音键；档案层只带一个模式，落到第一条。
                new_config = config.set_voice_mode(
                    new_config,
                    updates.get(
                        "voice_input_source",
                        config.voice_input_source(self._config),
                    ),
                    updates.get(
                        "voice_local_input_device",
                        config.voice_local_input_device(self._config),
                    ),
                )
            return self._save_voice_config_data(new_config, restart_capture=True)

        def _get_local_microphone_options(self) -> List[str]:
            return local_microphone_windows.list_input_devices()

        localMicrophoneOptions = Property(
            list,
            _get_local_microphone_options,
            notify=localMicrophoneOptionsChanged,
        )

        @Slot()
        def refreshLocalMicrophones(self) -> None:
            self.localMicrophoneOptionsChanged.emit()
            self.localMicrophoneDeviceChanged.emit()
            self._restart_microphone_level_capture()

        # -- 语音键配置：可无限增删，每条有名称/快捷键/麦克风来源 ----------

        def _voice_key_config_rows(self) -> List[dict]:
            devices = local_microphone_windows.list_input_devices()
            rows: List[dict] = []
            for entry in config.voice_key_configs(self._config):
                slot = int(entry["id"])
                source = entry["source"]
                device = entry["device"]
                try:
                    device_index = devices.index(device) if device else -1
                except ValueError:
                    device_index = -1
                configured_hotkey = entry["hotkey"]
                rows.append(
                    {
                        "id": slot,
                        "name": entry["name"],
                        "displayName": config.voice_key_name(self._config, slot),
                        "hotkey": configured_hotkey,
                        "hotkeyText": (
                            hotkey.format_hotkey_text(configured_hotkey)
                            if configured_hotkey
                            else ""
                        ),
                        "source": source,
                        "sourceIndex": (
                            0
                            if source == config.VOICE_INPUT_SOURCE_REMOTE
                            else 1
                        ),
                        "sourceLabel": (
                            "遥控器麦克风"
                            if source == config.VOICE_INPUT_SOURCE_REMOTE
                            else "本机麦克风"
                        ),
                        "device": device,
                        "deviceIndex": device_index,
                    }
                )
            return rows

        voiceKeyConfigs = Property(
            list,
            lambda self: self._voice_key_config_rows(),
            notify=voiceKeyConfigsChanged,
        )

        voiceSourceOptions = Property(
            list,
            lambda self: ["遥控器麦克风", "本机麦克风"],
            constant=True,
        )

        @Slot(result=bool)
        def canAddVoiceKeyConfig(self) -> bool:
            entries = config.voice_key_configs(self._config)
            return len(entries) < key_mapping.MAX_VOICE_SLOTS

        @Slot()
        def addVoiceKeyConfig(self) -> None:
            self._save_voice_config_data(
                config.add_voice_key_config(self._config)
            )

        @Slot(int)
        def removeVoiceKeyConfig(self, slot: int) -> None:
            entry = config.voice_key_config(self._config, slot)
            if entry is None:
                return
            label = config.voice_key_name(self._config, slot)
            new_config = config.remove_voice_key_config(self._config, slot)
            if config.voice_key_configs(new_config) == config.voice_key_configs(
                self._config
            ):
                # 最后一条不允许删除。
                return
            if not self._save_voice_config_data(new_config):
                return
            # 绑定到这个语音键的物理按键回到“禁用”，并让映射自动保存跟上。
            self._model.clear_voice_action(int(slot), label)

        @Slot(int, str)
        def setVoiceKeyConfigName(self, slot: int, value: str) -> None:
            self._save_voice_config_data(
                config.update_voice_key_config(self._config, slot, name=value)
            )

        @Slot(int, str)
        def setVoiceKeyConfigHotkey(self, slot: int, value: str) -> None:
            self._update_and_persist_voice_slot_hotkey(int(slot), value)

        @Slot(int, int)
        def setVoiceKeyConfigSourceIndex(self, slot: int, index: int) -> None:
            entry = config.voice_key_config(self._config, slot)
            if entry is None or index not in (0, 1):
                return
            source = (
                config.VOICE_INPUT_SOURCE_REMOTE
                if index == 0
                else config.VOICE_INPUT_SOURCE_LOCAL
            )
            device = entry["device"]
            if source == config.VOICE_INPUT_SOURCE_LOCAL and not device:
                devices = local_microphone_windows.list_input_devices()
                device = devices[0] if devices else ""
            new_config = config.update_voice_key_config(
                self._config, slot, source=source, device=device
            )
            if self._save_voice_config_data(new_config, restart_capture=True):
                self.localMicrophoneOptionsChanged.emit()

        @Slot(int, int)
        def setVoiceKeyConfigDeviceIndex(self, slot: int, index: int) -> None:
            devices = local_microphone_windows.list_input_devices()
            if index < 0 or index >= len(devices):
                return
            self._save_voice_config_data(
                config.update_voice_key_config(
                    self._config, slot, device=devices[index]
                ),
                restart_capture=True,
            )

        def _get_microphone_level(self) -> float:
            return self._microphone_level

        microphoneLevel = Property(
            float, _get_microphone_level, notify=microphoneLevelChanged
        )

        def _set_microphone_level(self, value: float) -> None:
            value = max(0.0, min(1.0, float(value)))
            if abs(value - self._microphone_level) < 0.004:
                return
            self._microphone_level = value
            self.microphoneLevelChanged.emit()

        def _microphone_level_device_name(self) -> str:
            """语音页电平要采哪个设备。

            模式2 采用户选中的本机麦克风。模式1 的遥控器音频由 resident
            进程写进 VB-CABLE 的 CABLE Input，设置进程拿不到那条流，
            就去听它的回环端 CABLE Output；没装 VB-CABLE 时才退回系统
            默认录音设备。
            """

            if self._get_voice_input_source() == config.VOICE_INPUT_SOURCE_LOCAL:
                return config.voice_local_input_device(self._config)
            for name in local_microphone_windows.list_input_devices():
                if audio_output.is_cable_output_endpoint(name):
                    return name
            return local_microphone_windows.default_input_device_name()

        def _restart_microphone_level_capture(self) -> None:
            if not self._microphone_level_timer.isActive():
                return
            monitor = self._microphone_level_monitor
            if monitor is not None:
                monitor.stop()
            device = self._microphone_level_device_name()
            if not device:
                self._microphone_level_monitor = None
                self._set_microphone_level(0.0)
                return
            if monitor is None:
                monitor = local_microphone_windows.MicrophoneLevelMonitor(
                    logger=logging_setup.get_logger(self._config_root)
                )
                self._microphone_level_monitor = monitor
            if not monitor.start(device):
                self._set_microphone_level(0.0)

        @Slot()
        def startMicrophoneLevelMonitor(self) -> None:
            """语音页可见时开始采电平。"""

            if self._microphone_level_timer.isActive():
                return
            module_logger = logging_setup.get_logger(self._config_root)
            monitor = self._microphone_level_monitor
            if monitor is None:
                monitor = local_microphone_windows.MicrophoneLevelMonitor(
                    logger=module_logger
                )
                self._microphone_level_monitor = monitor
            device = self._microphone_level_device_name()
            if device and monitor.start(device):
                self._microphone_level_timer.start()
                return
            self._microphone_level_monitor = None
            self._set_microphone_level(0.0)

        @Slot()
        def stopMicrophoneLevelMonitor(self) -> None:
            self._microphone_level_timer.stop()
            monitor = self._microphone_level_monitor
            self._microphone_level_monitor = None
            if monitor is not None:
                monitor.stop()
            self._set_microphone_level(0.0)

        def _tick_microphone_level(self) -> None:
            monitor = self._microphone_level_monitor
            target = monitor.level if monitor is not None else 0.0
            previous = self._microphone_level
            if target >= previous:
                value = previous + (target - previous) * 0.65
            else:
                # 回落快一点，说话停顿时的方块不会停在半空。
                value = max(target, previous * 0.72)
            if value < 0.004:
                value = 0.0
            self._set_microphone_level(value)

        @Slot(int, result=bool)
        def voiceSlotEditable(self, slot: int) -> bool:
            """一条语音键是否存在；删除后它的录入行也不再出现。"""

            return config.voice_key_config(self._config, int(slot)) is not None

        voiceHotkeySource = Property(
            str,
            lambda self: str(self._config.get("voice_hotkey_source", "")),
            notify=voiceHotkeySourceChanged,
        )

        def _get_voice_hotkey_busy(self) -> bool:
            return self._voice_hotkey_busy

        voiceHotkeyBusy = Property(
            bool,
            _get_voice_hotkey_busy,
            notify=voiceHotkeyBusyChanged,
        )

        voiceHotkeySaveState = Property(
            str,
            lambda self: self._voice_hotkey_save_state,
            notify=voiceHotkeySaveStateChanged,
        )

        def _get_voice_mapping_ready(self) -> bool:
            raw_bindings = self._bindings.get("bindings", {})
            if not isinstance(raw_bindings, dict):
                return False
            raw_action = raw_bindings.get("mic")
            if not isinstance(raw_action, dict):
                return False
            try:
                action = key_mapping.ButtonAction.from_dict(raw_action)
            except (KeyError, TypeError, ValueError):
                return False
            return key_mapping.is_supported_voice_action(action)

        voiceMappingReady = Property(
            bool,
            _get_voice_mapping_ready,
            notify=voiceMappingReadyChanged,
        )

        endpointPreflightBusy = Property(
            bool,
            lambda self: self._endpoint_preflight_busy,
            notify=endpointPreflightBusyChanged,
        )


        def _get_endpoint_options(self) -> List[str]:
            return list(self._endpoint_options)

        endpointOptions = Property(
            list, _get_endpoint_options, notify=endpointOptionsChanged
        )

        def _get_recommended_endpoint_index(self) -> int:
            return self._recommended_endpoint_index

        recommendedEndpointIndex = Property(
            int,
            _get_recommended_endpoint_index,
            notify=recommendedEndpointIndexChanged,
        )

        def _get_selected_endpoint_index(self) -> int:
            return self._selected_endpoint_index

        def _set_selected_endpoint_index(self, value: int) -> None:
            if value != self._selected_endpoint_index:
                self.selectAndPersistOutputEndpointIndex(value)

        selectedEndpointIndex = Property(
            int,
            _get_selected_endpoint_index,
            _set_selected_endpoint_index,
            notify=selectedEndpointIndexChanged,
        )

        def _get_bridge_running(self) -> bool:
            return self._bridge_running

        bridgeRunning = Property(
            bool,
            _get_bridge_running,
            notify=bridgeRunningChanged,
        )

        def _get_bridge_connected(self) -> bool:
            return self._bridge_connected

        bridgeConnected = Property(
            bool,
            _get_bridge_connected,
            notify=bridgeConnectedChanged,
        )

        bridgeConnectionState = Property(
            str,
            lambda self: self._bridge_connection_state,
            notify=bridgeConnectionStateChanged,
        )

        rawInputState = Property(
            str,
            lambda self: self._raw_input_state,
            notify=bridgeInputStateChanged,
        )
        hidTapState = Property(
            str,
            lambda self: self._hid_tap_state,
            notify=bridgeInputStateChanged,
        )
        voiceKeyPhysicalizerState = Property(
            str,
            lambda self: self._voice_key_physicalizer_state,
            notify=bridgeInputStateChanged,
        )
        remoteBatteryLevel = Property(
            int,
            lambda self: (
                -1
                if self._remote_battery_level is None
                else self._remote_battery_level
            ),
            notify=remoteBatteryChanged,
        )
        buttonReceiverUsable = Property(
            bool,
            _button_receiver_usable,
            notify=trayStateChanged,
        )
        voiceRuntimeState = Property(
            str,
            lambda self: self._voice_runtime_state,
            notify=voiceRuntimeStatusChanged,
        )

        bridgeRestartRecommended = Property(
            bool,
            lambda self: self._bridge_restart_recommended,
            notify=bridgeRestartRecommendedChanged,
        )
        bridgeReconnectAvailable = Property(
            bool,
            lambda self: self._bridge_reconnect_available,
            notify=bridgeReconnectAvailableChanged,
        )
        bridgeReconnectBusy = Property(
            bool,
            lambda self: self._bridge_reconnect_busy,
            notify=bridgeReconnectBusyChanged,
        )

        hidHelperIssueVisible = Property(
            bool,
            _hid_helper_needs_repair,
            notify=hidHelperStateChanged,
        )
        hidHelperSetupRequired = Property(
            bool,
            _hid_helper_setup_required,
            notify=hidHelperStateChanged,
        )
        hidHelperCleanupPending = Property(
            bool,
            _hid_helper_cleanup_pending,
            notify=hidHelperStateChanged,
        )
        hidHelperAccountUnsupported = Property(
            bool,
            _hid_helper_account_unsupported,
            notify=hidHelperStateChanged,
        )
        hidHelperRepairVisible = Property(
            bool,
            _hid_helper_repair_available,
            notify=hidHelperStateChanged,
        )
        hidHelperRemovalVisible = Property(
            bool,
            lambda self: (
                self._hid_helper_portable_distribution
                and self._hid_helper_can_self_elevate
                and self._hid_helper_state.available
                and not self._hid_helper_cleanup_pending()
                and not hid_elevation_windows.is_newer_helper_state(
                    self._hid_helper_state
                )
            ),
            notify=hidHelperStateChanged,
        )
        hidHelperRepairBusy = Property(
            bool,
            lambda self: self._hid_helper_repair_busy,
            notify=hidHelperRepairBusyChanged,
        )
        hidHelperIssueText = Property(
            str,
            lambda self: (
                "换管理员账号"
                if self._hid_helper_account_unsupported()
                else ""
                if self._hid_helper_cleanup_pending()
                else "请安装新版"
                if hid_elevation_windows.is_newer_helper_state(
                    self._hid_helper_state
                )
                else ""
                if self._hid_helper_setup_required()
                else "改键已停用"
                if self._hid_helper_needs_repair()
                else ""
            ),
            notify=hidHelperStateChanged,
        )

        startHidden = Property(bool, lambda self: self._start_hidden, constant=True)
        launchAtLogin = Property(
            bool,
            lambda self: self._launch_at_login,
            notify=desktopBehaviorChanged,
        )
        launchBridgeOnAppStart = Property(
            bool,
            lambda self: self._launch_bridge_on_app_start,
            notify=desktopBehaviorChanged,
        )
        diagnosticTraceEnabled = Property(
            bool,
            lambda self: self._diagnostic_trace_enabled,
            notify=desktopBehaviorChanged,
        )
        logExportBusy = Property(bool, lambda self: self._log_export_busy,
                                 notify=logExportBusyChanged)
        closeBehavior = Property(
            str,
            lambda self: self._close_behavior,
            notify=desktopBehaviorChanged,
        )
        applicationExitConfirmed = Property(
            bool,
            lambda self: self._application_exit_confirmed,
        )
        closeBehaviorOptions = Property(
            list,
            lambda self: ["隐藏", "完全退出"],
            constant=True,
        )
        trayIconSource = Property(
            str,
            _tray_icon_source,
            notify=trayStateChanged,
        )
        brandIconSource = Property(
            str,
            _brand_icon_source,
            constant=True,
        )
        trayTooltip = Property(
            str,
            _tray_tooltip,
            notify=trayStateChanged,
        )

        def _get_bridge_launch_phase(self) -> str:
            return self._bridge_launch_phase

        bridgeLaunchPhase = Property(
            str,
            _get_bridge_launch_phase,
            notify=bridgeLaunchPhaseChanged,
        )

        def _get_bridge_launch_busy(self) -> bool:
            return self._bridge_launch_phase in {"saving", "starting", "restarting", "stopping"}

        bridgeLaunchBusy = Property(
            bool,
            _get_bridge_launch_busy,
            notify=bridgeLaunchPhaseChanged,
        )

        def _get_bridge_launch_elapsed_seconds(self) -> int:
            return self._bridge_launch_elapsed_seconds

        bridgeLaunchElapsedSeconds = Property(
            int,
            _get_bridge_launch_elapsed_seconds,
            notify=bridgeLaunchElapsedSecondsChanged,
        )

        def _get_launch_status_text(self) -> str:
            return self._launch_status_text

        launchStatusText = Property(str, _get_launch_status_text, notify=launchStatusTextChanged)

        def _get_status_message(self) -> str:
            return self._status_message

        statusMessage = Property(str, _get_status_message, notify=statusMessageChanged)

        def _get_error_message(self) -> str:
            return self._error_message

        errorMessage = Property(str, _get_error_message, notify=errorMessageChanged)

        configurationReadOnly = Property(
            bool, lambda self: bool(self._configuration_issues), constant=True
        )
        configurationError = Property(
            str, lambda self: self._configuration_error, constant=True
        )

        def _get_settings_dirty(self) -> bool:
            return self._settings_dirty

        settingsDirty = Property(
            bool,
            _get_settings_dirty,
            notify=settingsDirtyChanged,
        )

        mappingDirty = Property(
            bool,
            lambda self: self._mapping_dirty,
            notify=mappingDirtyChanged,
        )

        settingsSaveBusy = Property(
            bool,
            lambda self: self._settings_save_busy,
            notify=settingsSaveBusyChanged,
        )

        mappingAutoSaveRetryAvailable = Property(
            bool,
            lambda self: self._mapping_auto_save_retry_available,
            notify=mappingAutoSaveRetryAvailableChanged,
        )

        def _get_active_page_index(self) -> int:
            return self._active_page_index

        def _set_active_page_index(self, value: int) -> None:
            value = max(
                self._DEVICE_PAGE_INDEX,
                min(self._VOICE_PAGE_INDEX, int(value)),
            )
            if value == self._active_page_index:
                return
            self._active_page_index = value
            self.activePageIndexChanged.emit()

        activePageIndex = Property(
            int,
            _get_active_page_index,
            _set_active_page_index,
            notify=activePageIndexChanged,
        )

        def _get_feedback_page_index(self) -> int:
            return self._feedback_page_index

        feedbackPageIndex = Property(
            int,
            _get_feedback_page_index,
            notify=feedbackPageIndexChanged,
        )

        def _get_selected_button_id(self) -> str:
            return self._selected_button_id

        selectedButtonId = Property(str, _get_selected_button_id, notify=selectedButtonIdChanged)

        def _get_device_options(self) -> List[str]:
            return [
                device_catalog.profile_for(device_id).display_name
                for device_id in self._DEVICE_ORDER
            ]

        deviceOptions = Property(list, _get_device_options, constant=True)
        remoteDisplayName = Property(
            str,
            lambda self: device_catalog.RC003_DISPLAY_NAME,
            constant=True,
        )
        applicationDisplayName = Property(
            str,
            lambda self: product_identity.DISPLAY_NAME + (" · 隔离测试" if dev_session.is_isolated() else ""),
            constant=True,
        )
        applicationPresentationLabel = Property(
            str,
            lambda self: product_identity.windows_presentation_label(__version__)
            + (" · 隔离测试" if dev_session.is_isolated() else ""),
            constant=True,
        )
        applicationVersion = Property(
            str,
            lambda self: __version__,
            constant=True,
        )
        applicationUpdateState = Property(
            str,
            lambda self: self._application_update_state,
            notify=applicationUpdateChanged,
        )
        applicationUpdateMessage = Property(
            str,
            lambda self: self._application_update_message,
            notify=applicationUpdateChanged,
        )
        applicationUpdateLatestVersion = Property(
            str,
            lambda self: (
                self._application_update_release.version.text
                if self._application_update_release is not None
                else ""
            ),
            notify=applicationUpdateChanged,
        )
        applicationUpdateReleaseNotes = Property(
            str,
            lambda self: self._application_update_release_notes,
            notify=applicationUpdateChanged,
        )
        applicationUpdateReleaseUrl = Property(
            str,
            lambda self: self._application_update_release_url,
            notify=applicationUpdateChanged,
        )
        applicationUpdatePackageName = Property(
            str,
            lambda self: self._application_update_package_name,
            notify=applicationUpdateChanged,
        )
        applicationUpdatePackageSize = Property(
            int,
            lambda self: self._application_update_package_size,
            notify=applicationUpdateChanged,
        )
        applicationUpdatePackageSizeText = Property(
            str,
            lambda self: application_update.format_file_size(
                self._application_update_package_size
            ),
            notify=applicationUpdateChanged,
        )
        applicationUpdatePackageKindText = Property(
            str,
            lambda self: (
                "安装器"
                if self._application_update_package_kind()
                is application_update.PackageKind.INSTALLER
                else "便携版 ZIP"
            ),
            constant=True,
        )
        applicationUpdateCheckBusy = Property(
            bool,
            lambda self: self._application_update_check_busy,
            notify=applicationUpdateChanged,
        )
        applicationUpdateDownloadBusy = Property(
            bool,
            lambda self: self._application_update_download_busy,
            notify=applicationUpdateChanged,
        )
        applicationUpdateBusy = Property(
            bool,
            lambda self: (
                self._application_update_check_busy
                or self._application_update_download_busy
            ),
            notify=applicationUpdateChanged,
        )
        applicationUpdateCanDownload = Property(
            bool,
            lambda self: (
                self._application_update_available
                and self._application_update_release is not None
                and not self._application_update_check_busy
                and not self._application_update_download_busy
            ),
            notify=applicationUpdateChanged,
        )
        applicationUpdateDownloadProgress = Property(
            float,
            lambda self: (
                min(
                    1.0,
                    self._application_update_download_received
                    / self._application_update_package_size,
                )
                if self._application_update_package_size > 0
                else 0.0
            ),
            notify=applicationUpdateChanged,
        )
        applicationUpdateDownloadProgressText = Property(
            str,
            lambda self: (
                f"{application_update.format_file_size(self._application_update_download_received)}"
                f" / {application_update.format_file_size(self._application_update_package_size)}"
                if self._application_update_package_size > 0
                else ""
            ),
            notify=applicationUpdateChanged,
        )
        applicationUpdateDownloadedPath = Property(
            str,
            lambda self: self._application_update_download_path,
            notify=applicationUpdateChanged,
        )

        def _get_device_catalog_available(self) -> bool:
            return device_catalog.CATALOG_ERROR is None

        deviceCatalogAvailable = Property(
            bool, _get_device_catalog_available, constant=True
        )

        def _get_device_catalog_error_text(self) -> str:
            if device_catalog.CATALOG_ERROR is None:
                return ""
            return "设备目录不可用：" + device_catalog.CATALOG_ERROR

        deviceCatalogErrorText = Property(
            str, _get_device_catalog_error_text, constant=True
        )

        def _get_selected_device_index(self) -> int:
            if self.activeRemoteKey:
                return self._DEVICE_ORDER.index(self._selected_device_id())
            return self._selected_device_index

        def _set_selected_device_index(self, value: int) -> None:
            if self.activeRemoteKey:
                return  # Only the device page's explicit entity confirmation may switch.
            if value == self._selected_device_index or not (0 <= value < len(self._DEVICE_ORDER)):
                return
            self._selected_device_index = value
            self._selected_device_fallback_id = self._DEVICE_ORDER[value]
            self.selectedDeviceIndexChanged.emit()
            self.selectedDeviceChanged.emit()
            self._mark_settings_dirty()

        selectedDeviceIndex = Property(
            int,
            _get_selected_device_index,
            _set_selected_device_index,
            notify=selectedDeviceIndexChanged,
        )

        def _get_is_rc003_device(self) -> bool:
            return self._selected_device_id() == device_catalog.RC003_ID

        isRc003Device = Property(bool, _get_is_rc003_device, notify=selectedDeviceChanged)

        def _get_selected_device_description(self) -> str:
            if device_catalog.CATALOG_ERROR is not None:
                return self._get_device_catalog_error_text()
            return device_catalog.profile_for(self._selected_device_id()).description

        selectedDeviceDescription = Property(
            str, _get_selected_device_description, notify=selectedDeviceChanged
        )

        mappingPageTitle = Property(str, lambda self: "按键映射", constant=True)

        def _get_key_detection_active(self) -> bool:
            return self._key_detection_active

        keyDetectionActive = Property(
            bool,
            _get_key_detection_active,
            notify=keyDetectionActiveChanged,
        )

        def _get_key_detection_text(self) -> str:
            return self._key_detection_text

        keyDetectionText = Property(
            str,
            _get_key_detection_text,
            notify=keyDetectionTextChanged,
        )

        hotkeyCaptureActive = Property(
            bool,
            _get_hotkey_capture_active,
            notify=hotkeyCaptureActiveChanged,
        )

        hotkeyCaptureReady = Property(
            bool,
            lambda self: (
                self._input_operation_kind == "hotkey"
                and self._input_operation_phase == "active"
                and self._hotkey_capture is not None
            ),
            notify=inputOperationChanged,
        )

        inputCaptureInUse = Property(
            bool,
            _get_input_capture_in_use,
            notify=inputOperationChanged,
        )

        def _get_primary_action_options(self) -> List[str]:
            return list(_ORDINARY_PRIMARY_ACTION_OPTIONS)

        def _mapping_options_available_now(self, options: List[str]) -> List[str]:
            if self._available_mapping_app_labels is None:
                available = set()
                for option in dict(settings_ui.ACTION_OPTION_GROUPS)["启动应用"]:
                    action = settings_ui._display_to_action(option)
                    if action_executor.resolve_application_command(action) is not None:
                        available.add(option)
                self._available_mapping_app_labels = available
            allow_mode_cycle = config.voice_other_key_switch(self._config)
            return [
                option for option in options
                if (allow_mode_cycle or option != _VOICE_MODE_CYCLE_LABEL)
                and (
                    settings_ui.ACTION_OPTION_GROUP_BY_LABEL.get(option) != "启动应用"
                    or option in self._available_mapping_app_labels
                )
            ]

        primaryActionOptions = Property(
            list, _get_primary_action_options, constant=True
        )

        def _voice_action_displays(self, include_remote: bool) -> List[str]:
            """语音键在按键页动作列表里的显示名。

            遥控器麦克风来源的语音键只能挂到遥控器的语音键上，别的按键不列
            出来；本机麦克风来源的语音键才出现在所有按键的可选列表里。
            """

            labels = config.voice_key_labels(self._config)
            sources = {
                int(entry["id"]): entry["source"]
                for entry in config.voice_key_configs(self._config)
            }
            return [
                label
                for slot, label in labels.items()
                if sources.get(slot) != config.VOICE_INPUT_SOURCE_REMOTE
                or include_remote
            ]

        @Slot(str, result=list)
        def primaryActionOptionsFor(self, button_id: str) -> List[str]:
            if not button_id:
                return list(_ORDINARY_PRIMARY_ACTION_OPTIONS)
            voice_options = self._voice_action_displays(button_id == "mic")
            return self._mapping_options_available_now(
                list(
                    dict.fromkeys(
                        voice_options + list(_ORDINARY_PRIMARY_ACTION_OPTIONS)
                    )
                )
            )

        @Slot(str, result=str)
        def actionOptionGroupTitle(self, option: str) -> str:
            return settings_ui.ACTION_OPTION_GROUP_BY_LABEL.get(option, "")

        @Slot(str, result=bool)
        def actionOptionStartsGroup(self, option: str) -> bool:
            return option in settings_ui.ACTION_OPTION_GROUP_STARTS

        @Slot(str, result=str)
        def formatActionText(self, text: str) -> str:
            return settings_ui.format_action_text(text)

        @Slot(str, result=str)
        def formatHotkeyText(self, text: str) -> str:
            return hotkey.format_hotkey_text(text)

        @Slot(str, result="QVariantMap")
        def normalizeMappingHotkeyText(self, text: str) -> object:
            try:
                normalized = settings_ui.normalize_mapping_hotkey_text(text)
            except hotkey.HotkeyParseError as exc:
                return {"ok": False, "text": "", "message": str(exc)}
            return {"ok": True, "text": normalized, "message": ""}

        @Slot(str, str, str, str, result=str)
        def buttonActionValidationMessage(
            self,
            button_id: str,
            trigger: str,
            text: str,
            inject_mode: str,
        ) -> str:
            return settings_ui.button_action_validation_message(
                button_id,
                trigger,
                text,
                base_bindings=self._bindings,
                inject_mode=inject_mode,
            )

        def _get_secondary_action_options(self) -> List[str]:
            return list(_SECONDARY_ACTION_OPTIONS)

        secondaryActionOptions = Property(
            list, _get_secondary_action_options, constant=True
        )

        @Slot(str, result=list)
        def secondaryActionOptionsFor(self, button_id: str) -> List[str]:
            if not button_id:
                return list(_SECONDARY_ACTION_OPTIONS)
            return self._mapping_options_available_now(_SECONDARY_ACTION_OPTIONS)

        @Slot(str, result=list)
        def longPressActionOptionsFor(self, button_id: str) -> List[str]:
            return self.secondaryActionOptionsFor(button_id) + list(settings_ui.LONG_PRESS_ACTION_LABELS.values())

        # Compatibility alias for older QML probes. New code uses the two
        # semantically distinct option properties above.
        presetActionOptions = Property(
            list, _get_primary_action_options, constant=True
        )

        def _get_photo_source(self) -> str:
            photo_path = resources.find_remote_photo(self._selected_device_id())
            if photo_path is None:
                return ""
            return QUrl.fromLocalFile(str(photo_path)).toString()

        photoSource = Property(str, _get_photo_source, notify=selectedDeviceChanged)

        def _get_photo_available(self) -> bool:
            return resources.find_remote_photo(self._selected_device_id()) is not None

        photoAvailable = Property(bool, _get_photo_available, notify=selectedDeviceChanged)

        # -- slots ----------------------------------------------------------

        @Slot(result=bool)
        def saveSettings(self) -> bool:
            if (
                self._get_bridge_launch_busy()
                or self._endpoint_preflight_busy
                or _driver_action_active_event.is_set()
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                self._set_error_message(
                    "当前有其它操作正在进行；完成后再保存按键映射。",
                    self._BUTTONS_PAGE_INDEX,
                )
                return False
            return self._save()

        @Slot(result=bool)
        def retryMappingAutoSave(self) -> bool:
            if not self._mapping_dirty:
                self._set_mapping_auto_save_retry_available(False)
                return True
            if (
                self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                return False
            self._set_error_message("")
            self._set_status_message(
                "正在重新保存按键映射…",
                self._BUTTONS_PAGE_INDEX,
            )
            self._queue_mapping_auto_save()
            return True

        @Slot(bool)
        def setLaunchAtLogin(self, enabled: bool) -> None:
            if self.configurationReadOnly:
                return
            enabled = bool(enabled)
            if enabled == self._launch_at_login:
                return
            result = startup_windows.set_startup_enabled(enabled)
            if result.error:
                self.desktopBehaviorChanged.emit()
                self._set_status_message("")
                self._set_error_message(
                    f"无法修改随 Windows 启动：{result.error}",
                    self._DESKTOP_BEHAVIOR_PAGE_INDEX,
                )
                return
            self._launch_at_login = result.enabled
            self.desktopBehaviorChanged.emit()
            self._set_error_message("")
            self._set_status_message(
                "已启用随 Windows 启动。"
                if result.enabled
                else "已关闭随 Windows 启动。",
                self._DESKTOP_BEHAVIOR_PAGE_INDEX,
            )

        @Slot(bool)
        def setLaunchBridgeOnAppStart(self, enabled: bool) -> None:
            if dev_session.is_isolated():
                self._set_status_message("隔离测试请手动启动服务，不自动接管遥控器。", self._DEVICE_PAGE_INDEX)
                self.desktopBehaviorChanged.emit()
                return
            enabled = bool(enabled)
            if enabled == self._launch_bridge_on_app_start:
                return
            if not self._persist_desktop_behavior(
                "launch_bridge_on_app_start", enabled
            ):
                self.desktopBehaviorChanged.emit()
                return
            self._launch_bridge_on_app_start = enabled
            self.desktopBehaviorChanged.emit()

        @Slot(bool)
        def setDiagnosticTraceEnabled(self, enabled: bool) -> None:
            enabled = bool(enabled)
            if enabled == self._diagnostic_trace_enabled:
                return
            if not self._persist_desktop_behavior(
                "diagnostic_trace_enabled", enabled
            ):
                self.desktopBehaviorChanged.emit()
                return
            self._diagnostic_trace_enabled = enabled
            self.desktopBehaviorChanged.emit()
            bridge_launcher.reload_in_process_bridge_settings()
            self._set_error_message("")
            self._set_status_message(
                "已开启诊断日志。" if enabled else "已关闭诊断日志。",
                self._DESKTOP_BEHAVIOR_PAGE_INDEX,
            )

        @Slot(int)
        def setCloseBehaviorIndex(self, index: int) -> None:
            value = (
                config.CLOSE_BEHAVIOR_QUIT
                if int(index) == 1
                else config.CLOSE_BEHAVIOR_HIDE_TO_TRAY
            )
            if value == self._close_behavior:
                return
            if not self._persist_desktop_behavior("close_behavior", value):
                self.desktopBehaviorChanged.emit()
                return
            self._close_behavior = value
            self.desktopBehaviorChanged.emit()

        @Slot()
        def startBridgeOnApplicationStart(self) -> None:
            if self.configurationReadOnly:
                return
            if self._maintenance_exit_pending:
                return
            if (
                self._bridge_running
                and not bridge_launcher.in_process_bridge_running()
            ):
                # Upgrade path: preserve a running legacy service, but move it
                # into this process so the product does not keep two resident
                # executables after the settings shell has opened.
                self._begin_bridge_restart(automatic=True)
                return
            if (
                self._start_bridge_requested or self._launch_bridge_on_app_start
            ) and not self._bridge_running:
                self._start_bridge_requested = False
                self.startBridge()

        @Slot(result=bool)
        def canAutoSaveMappingBeforeExit(self) -> bool:
            return bool(
                self._mapping_dirty
                and not self._mapping_auto_save_retry_available
                and not self._has_unsaved_non_mapping_settings()
            )

        @Slot()
        def saveSettingsAndExit(self) -> None:
            if (
                self._application_exit_requested
                or self._application_exit_confirmed
                or self._save_then_exit_requested
            ):
                return
            self._save_then_exit_requested = True
            self._save_then_exit_deadline = (
                time.monotonic() + _APPLICATION_EXIT_SAVE_WAIT_TIMEOUT_SECONDS
            )
            if self._mapping_auto_save_scheduled:
                self._mapping_auto_save_timer.stop()
                self._mapping_auto_save_scheduled = False
            self._continue_save_then_exit()

        def _schedule_save_then_exit_poll(self) -> None:
            if (
                not self._save_then_exit_requested
                or self._save_then_exit_poll_scheduled
            ):
                return
            self._save_then_exit_poll_scheduled = True
            QTimer.singleShot(
                _APPLICATION_EXIT_POLL_INTERVAL_MS,
                self._continue_save_then_exit,
            )

        def _finish_save_then_exit(self, saved: bool) -> None:
            if not self._save_then_exit_requested:
                return
            self._save_then_exit_requested = False
            self._save_then_exit_poll_scheduled = False
            self._save_then_exit_deadline = 0.0
            if not saved:
                if self._mapping_dirty:
                    self._set_mapping_auto_save_retry_available(True)
                self._reject_pending_maintenance_exit()
            self.saveSettingsAndExitFinished.emit(bool(saved))
            if saved:
                self.requestApplicationExit()

        def _continue_save_then_exit(self) -> None:
            self._save_then_exit_poll_scheduled = False
            if not self._save_then_exit_requested:
                return
            if (
                self._mapping_auto_save_running
                or self._mapping_auto_save_temporarily_blocked()
            ):
                if time.monotonic() >= self._save_then_exit_deadline:
                    self._set_status_message("")
                    self._set_error_message(
                        "保存并退出超时：后台设置仍在处理，请稍后重试。",
                        self._BUTTONS_PAGE_INDEX,
                    )
                    self._finish_save_then_exit(False)
                    return
                self._schedule_save_then_exit_poll()
                return

            save = (
                self._save
                if self._has_unsaved_non_mapping_settings()
                or not self._mapping_dirty
                else self._save_mapping
            )
            accepted = save(completion=self._finish_save_then_exit)
            if (
                not accepted
                and self._save_then_exit_requested
                and not self._settings_save_busy
            ):
                self._finish_save_then_exit(False)

        @Slot()
        def requestApplicationExit(self) -> None:
            if (
                self._application_exit_requested
                or self._application_exit_confirmed
            ):
                return
            self._maintenance_exit_pending = False
            self._application_exit_requested = True
            self._application_exit_deadline = 0.0
            self._window_hide_requested = False
            if self._settings_save_busy:
                self._application_exit_waiting_for_save = True
                self._application_exit_deadline = (
                    time.monotonic()
                    + _APPLICATION_EXIT_SAVE_WAIT_TIMEOUT_SECONDS
                )
                self._schedule_application_exit_poll()
                return
            self._begin_application_exit()

        def _begin_application_exit(self) -> None:
            self._application_exit_waiting_for_save = False
            self._application_exit_deadline = (
                time.monotonic() + _APPLICATION_EXIT_WAIT_TIMEOUT_SECONDS
            )
            self._application_exit_intent.set()
            if self._get_input_capture_in_use():
                self._request_input_stop()
            if self._application_exit_poll_scheduled:
                return
            self._continue_application_exit()

        def _schedule_application_exit_poll(self) -> None:
            if (
                not self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_poll_scheduled
                or self._application_exit_stop_running
            ):
                return
            self._application_exit_poll_scheduled = True
            QTimer.singleShot(
                _APPLICATION_EXIT_POLL_INTERVAL_MS,
                self._continue_application_exit,
            )

        def _continue_application_exit(self) -> None:
            self._application_exit_poll_scheduled = False
            if (
                not self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_stop_running
            ):
                return
            if self._application_exit_waiting_for_save:
                if time.monotonic() >= self._application_exit_deadline:
                    self._fail_application_exit(
                        "完全退出超时：设置仍在保存，请稍后重试。"
                    )
                    return
                self._schedule_application_exit_poll()
                return
            if time.monotonic() >= self._application_exit_deadline:
                self._fail_application_exit(
                    "完全退出超时：后台操作尚未结束，请稍后重试。"
                )
                return

            if self._get_input_capture_in_use():
                self._request_input_stop()
                self._schedule_application_exit_poll()
                return
            if self._remote_selection_busy:
                self._schedule_application_exit_poll()
                return
            if (
                self._voice_hotkey_busy
                or self._settings_save_busy
                or self._endpoint_preflight_busy
                or self._hid_helper_repair_busy
                or self._bridge_recovery_running
                or _vb_cable_test_active_event.is_set()
                or _driver_action_active_event.is_set()
            ):
                self._schedule_application_exit_poll()
                return

            if self._pending_bridge_launch is not None:
                self.pollBridgeLaunch()
                if self._pending_bridge_launch is not None:
                    self._schedule_application_exit_poll()
                    return
            if (
                self._bridge_launch_phase in {"saving", "starting"}
                and self._pending_bridge_launch is None
            ):
                self._set_bridge_launch_phase("idle")

            bridge_running = self._refresh_bridge_status()
            if bridge_running is False:
                self._on_application_exit_stop_ready((True, ""))
                return

            self._application_exit_stop_running = True

            def stop_and_exit() -> None:
                try:
                    result = bridge_control_windows.request_bridge_exit(reason="application_exit")
                except Exception as exc:  # noqa: BLE001 - must remain retryable
                    payload = (False, f"完全退出失败：{type(exc).__name__}")
                else:
                    payload = (
                        bool(result.stopped),
                        result.error or "遥控器服务未能正常停止。",
                    )
                self._emit_background_result(
                    self._applicationExitStopReady,
                    payload,
                )

            try:
                self._start_background_task(
                    stop_and_exit, "remote-mic-application-exit"
                )
            except RuntimeError as exc:
                self._application_exit_stop_running = False
                self._fail_application_exit(str(exc))

        def _on_application_exit_stop_ready(self, payload: object) -> None:
            self._application_exit_stop_running = False
            if not self._application_exit_requested:
                return
            stopped, message = payload
            if stopped:
                self._application_exit_requested = False
                self._application_exit_waiting_for_save = False
                self._application_exit_confirmed = True
                self._maintenance_exit_request = None
                self._application_exit_intent.set()
                self.applicationExitReady.emit()
                return
            self._fail_application_exit(str(message))

        def _fail_application_exit(self, message: str) -> None:
            self._reject_pending_maintenance_exit()
            self._application_exit_requested = False
            self._application_exit_stop_running = False
            self._application_exit_poll_scheduled = False
            self._application_exit_waiting_for_save = False
            self._application_exit_deadline = 0.0
            self._application_exit_intent.clear()
            self.applicationExitFailed.emit(message)

        def _reject_pending_maintenance_exit(self) -> None:
            request = self._maintenance_exit_request
            self._maintenance_exit_request = None
            self._maintenance_exit_pending = False
            if request is None:
                return
            if request.window_token is not None:
                single_instance.publish_settings_window_exit_rejection(
                    self._settings_window_hwnd,
                    request.window_token,
                )
            if request.request_id is None:
                return
            try:
                single_instance.write_application_exit_rejection(
                    self._config_root,
                    request.request_id,
                    session_id=request.session_id,
                    session_scoped=request.session_scoped,
                )
            except OSError:
                # The waiting copy retains a bounded fallback timeout when a
                # response file cannot be written.
                pass

        @Slot()
        def cancelPendingMaintenanceExit(self) -> None:
            if self._application_exit_requested or self._application_exit_confirmed:
                return
            self._reject_pending_maintenance_exit()

        @Slot(result=bool)
        def claimPortableHidSetupPrompt(self) -> bool:
            if self._hid_helper_setup_attempted_this_session:
                return False
            self._hid_helper_setup_attempted_this_session = True
            if (
                not self._hid_helper_setup_required()
                or not self._hid_helper_offer_id
                or self._hid_helper_setup_prompted_offer_id
                == self._hid_helper_offer_id
                or self._hid_helper_repair_busy
                or self._maintenance_exit_pending
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
                or self._background_shutdown_event.is_set()
            ):
                return False
            return self._record_hid_helper_setup_prompted()

        def _record_hid_helper_setup_prompted(self) -> bool:
            if self.configurationReadOnly:
                return False
            offer_id = self._hid_helper_offer_id
            if not offer_id:
                if self._hid_helper_portable_distribution:
                    message = (
                        "当前程序文件不完整。请完整解压 ZIP，"
                        f"再运行根目录里的 {product_identity.windows_executable_name(__version__)}。"
                    )
                else:
                    message = "安装文件不完整，请重新安装当前版本。"
                self._set_error_message(message, self._DEVICE_PAGE_INDEX)
                return False
            if self._hid_helper_setup_prompted_offer_id == offer_id:
                return True
            updated = dict(self._config)
            updated["hid_helper_setup_prompted_offer_id"] = offer_id
            try:
                loaded = config.save_config_and_load(
                    config.config_path(self._config_root),
                    updated,
                )
            except (OSError, ValueError, config.ConfigTransactionError):
                action_text = (
                    "启用改键"
                    if self._hid_helper_portable_distribution
                    else "修复权限"
                )
                self._set_error_message(
                    f"无法记录首次提示状态，请在“按键接收”中点击“{action_text}”重试。",
                    self._DEVICE_PAGE_INDEX,
                )
                return False
            self._config = loaded
            self._hid_helper_setup_prompted_offer_id = offer_id
            return True

        @Slot()
        def repairHidHelper(self) -> None:
            self._begin_hid_helper_repair()

        def _begin_hid_helper_repair(self) -> None:
            if (
                self._hid_helper_repair_busy
                or not self._hid_helper_repair_available()
                or self._maintenance_exit_pending
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
                or self._background_shutdown_event.is_set()
            ):
                return
            if not self._record_hid_helper_setup_prompted():
                return
            self._set_hid_helper_repair_busy(True)
            self._set_error_message("")
            self._set_status_message(
                "请在 Windows 提示中确认管理员权限…",
                self._DEVICE_PAGE_INDEX,
            )

            def run() -> None:
                state = hid_helper_consumers.install_for_current_consumer(
                    self._config_root,
                    hid_elevation_windows.request_install_elevation,
                )
                self._emit_background_result(
                    self._hidHelperRepairReady,
                    state,
                )

            try:
                self._start_background_task(run, "remote-mic-hid-helper-repair")
            except RuntimeError as exc:
                self._set_hid_helper_repair_busy(False)
                self._set_status_message("")
                self._set_error_message(str(exc), self._DEVICE_PAGE_INDEX)

        def _on_hid_helper_repair_ready(self, payload: object) -> None:
            self._set_hid_helper_repair_busy(False)
            state = (
                payload
                if isinstance(payload, hid_elevation_windows.HidHelperState)
                else hid_elevation_windows.HidHelperState(
                    False, "hid_helper_setup_result_unavailable"
                )
            )
            self._set_hid_helper_state(state)
            logging_setup.write_parent_hid_helper_event(
                "setup_result",
                available=state.available,
                detail=state.detail,
                root=self._config_root,
            )
            if state.available:
                self._set_error_message("")
                if state.detail == "helper_cleanup_pending":
                    self._set_status_message(
                        "自定义按键映射可用，旧权限组件尚未清理，请重试。",
                        self._DEVICE_PAGE_INDEX,
                    )
                else:
                    self._set_status_message(
                        "管理员按键组件已启用。",
                        self._DEVICE_PAGE_INDEX,
                    )
                if (
                    self._bridge_running
                    and not self._maintenance_exit_pending
                    and not self._application_exit_requested
                    and not self._application_exit_confirmed
                    and not self._application_exit_intent.is_set()
                    and not self._background_shutdown_event.is_set()
                ):
                    self.restartBridge()
                return

            self._set_status_message("")
            if state.detail == "uac_cancelled":
                message = (
                    "未确认管理员权限。全部自定义按键映射已停用，"
                    "只保留 Windows 原始按键操作。"
                )
            elif state.detail == "current_account_cannot_self_elevate":
                message = (
                    "当前账号无法直接完成管理员授权。"
                    "请确认正在使用管理员账号后重试。"
                )
            elif hid_elevation_windows.is_newer_helper_state(state):
                message = (
                    "检测到较新版本的管理员按键组件。"
                    "当前版本不能覆盖，请使用或重新安装较新版本。"
                )
            elif state.detail == "bundled_helper_missing":
                if self._hid_helper_portable_distribution:
                    message = (
                        "当前程序文件不完整。请完整解压 ZIP，"
                        f"再运行根目录里的 {product_identity.windows_executable_name(__version__)}。"
                    )
                else:
                    message = "安装文件不完整，请重新安装当前版本。"
            elif state.detail in {
                "hid_helper_operation_busy",
                f"hid_helper_setup_exit_{hid_elevation_windows.HELPER_EXIT_OPERATION_BUSY}",
            }:
                message = "另一个管理员按键操作正在进行，请稍后重试。"
            elif state.detail in {
                "hid_helper_setup_timeout",
                "hid_helper_setup_terminate_failed",
                "hid_helper_setup_terminate_wait_failed",
                "hid_helper_setup_wait_failed",
            }:
                action_text = (
                    "启用改键"
                    if self._hid_helper_portable_distribution
                    else "修复权限"
                )
                message = f"请再次尝试点击“{action_text}”；仍失败时打开运行日志。"
            elif state.detail in {
                f"hid_helper_setup_exit_{hid_elevation_windows.HELPER_EXIT_REQUIRES_ADMIN}",
                f"hid_helper_setup_exit_{hid_elevation_windows.HELPER_EXIT_VALIDATION_FAILED}",
                f"hid_helper_setup_exit_{hid_elevation_windows.HELPER_EXIT_UNEXPECTED_FAILURE}",
            } or hid_elevation_windows.is_helper_setup_failure_detail(state.detail):
                action_text = (
                    "启用改键"
                    if self._hid_helper_portable_distribution
                    else "修复权限"
                )
                message = f"请再次尝试点击“{action_text}”；仍失败时打开运行日志。"
            else:
                message = (
                    "管理员按键组件启用失败。全部自定义按键映射已停用，"
                    "只保留 Windows 原始按键操作。"
                )
            self._set_error_message(message, self._DEVICE_PAGE_INDEX)

        @Slot()
        def removeHidHelper(self) -> None:
            if (
                self._hid_helper_repair_busy
                or not self._hid_helper_portable_distribution
                or not self._hid_helper_can_self_elevate
                or not self._hid_helper_state.available
                or self._maintenance_exit_pending
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
                or self._background_shutdown_event.is_set()
            ):
                return
            self._set_hid_helper_repair_busy(True)
            self._set_error_message("")
            self._set_status_message(
                "正在处理管理员按键组件…",
                self._DEVICE_PAGE_INDEX,
            )

            def run() -> None:
                state = hid_helper_consumers.remove_for_portable_consumer(
                    self._config_root,
                    hid_elevation_windows.request_uninstall_elevation,
                )
                self._emit_background_result(
                    self._hidHelperRemovalReady,
                    state,
                )

            try:
                self._start_background_task(run, "remote-mic-hid-helper-removal")
            except RuntimeError as exc:
                self._set_hid_helper_repair_busy(False)
                self._set_status_message("")
                self._set_error_message(str(exc), self._DEVICE_PAGE_INDEX)

        def _on_hid_helper_removal_ready(self, payload: object) -> None:
            self._set_hid_helper_repair_busy(False)
            state = (
                payload
                if isinstance(payload, hid_elevation_windows.HidHelperState)
                else hid_elevation_windows.HidHelperState(
                    False, "hid_helper_removal_result_unavailable"
                )
            )
            logging_setup.write_parent_hid_helper_event(
                "removal_result",
                available=state.available,
                detail=state.detail,
                root=self._config_root,
            )
            if state.available:
                self._set_hid_helper_state(
                    hid_elevation_windows.HidHelperState(
                        False, "helper_manifest_missing"
                    )
                )
                self._set_error_message("")
                self._set_status_message(
                    "管理员按键组件已移除。",
                    self._DEVICE_PAGE_INDEX,
                )
                if (
                    self._bridge_running
                    and not self._maintenance_exit_pending
                    and not self._application_exit_requested
                    and not self._application_exit_confirmed
                    and not self._application_exit_intent.is_set()
                    and not self._background_shutdown_event.is_set()
                ):
                    self.restartBridge()
                return

            self._set_status_message("")
            if state.detail == "uac_cancelled":
                message = "未确认管理员权限，管理员按键组件没有移除。"
            elif state.detail == "current_account_cannot_self_elevate":
                message = (
                    "当前 Windows 账号不能移除管理员按键组件。"
                    "请登录原管理员账号后重试。"
                )
            elif state.detail == "helper_preserved_newer_contract":
                message = "检测到较新版本正在使用，管理员按键组件未移除。"
            elif state.detail == "helper_consumer_marker_restore_failed":
                message = (
                    "权限没有移除，但当前版本的使用记录未能恢复。"
                    "请保留程序文件并重新打开后再试。"
                )
            else:
                message = "管理员按键组件移除失败，请稍后重试。"
            self._set_error_message(message, self._DEVICE_PAGE_INDEX)

        @Slot()
        def pollWindowRestoreRequest(self) -> None:
            if (
                self._maintenance_exit_pending
                or self._maintenance_exit_request is not None
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                return
            if single_instance.consume_settings_window_restore_request(
                self._settings_window_hwnd
            ):
                self.windowRestoreRequested.emit()

        @Slot()
        def refreshBridgeState(self) -> None:
            if (
                self._maintenance_exit_pending
                or self._maintenance_exit_request is not None
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                try:
                    single_instance.consume_bridge_start_request(
                        self._config_root,
                        session_scoped=True,
                    )
                except (
                    OSError,
                    single_instance.SingleInstanceUnavailableError,
                ):
                    pass
                self._start_bridge_requested = False
                return

            window_request_token = (
                single_instance.consume_settings_window_exit_request(
                    self._settings_window_hwnd
                )
            )
            if window_request_token is not None:
                exit_request = single_instance.ApplicationExitRequest(
                    None,
                    window_token=window_request_token,
                )
            else:
                try:
                    exit_request = (
                        single_instance.consume_and_acknowledge_application_exit_request(
                            self._config_root
                        )
                    )
                except (
                    OSError,
                    single_instance.SingleInstanceUnavailableError,
                ):
                    exit_request = None
            if exit_request is not None:
                self._maintenance_exit_pending = True
                self._maintenance_exit_request = exit_request
                self._start_bridge_requested = False
                self._window_hide_requested = False
                try:
                    single_instance.consume_bridge_start_request(
                        self._config_root,
                        session_scoped=True,
                    )
                except (
                    OSError,
                    single_instance.SingleInstanceUnavailableError,
                ):
                    pass
                self.maintenanceExitRequested.emit()
                return

            running = self._refresh_bridge_status()
            try:
                requested = single_instance.consume_bridge_start_request(
                    self._config_root,
                    session_scoped=True,
                )
            except (
                OSError,
                single_instance.SingleInstanceUnavailableError,
            ):
                requested = False
            if requested:
                self._start_bridge_requested = True
            if self._start_bridge_requested:
                if running:
                    self._start_bridge_requested = False
                elif running is False and not self._get_bridge_launch_busy():
                    self._start_bridge_requested = False
                    self.startBridge()

        @Slot()
        def startKeyDetection(self) -> None:
            """Listen for one real RC003 press without executing its action."""

            logger = logging_setup.get_logger(self._config_root)

            def blocked(reason: str, text: str) -> None:
                logger.info("key detection start blocked: %s", reason)
                self._set_key_detection_text(text)
                self._set_error_message(text, self._BUTTONS_PAGE_INDEX)

            if self._key_detection_active:
                logger.info("key detection start ignored: already_active")
                return
            if self._get_input_capture_in_use():
                blocked("input_capture_busy", "另一项按键操作尚未结束；结束后再检测按键")
                return
            if self._get_bridge_launch_busy():
                blocked("service_starting",
                    "遥控器服务正在启动；完成后再检测按键"
                )
                return
            if _vb_cable_test_active_event.is_set():
                blocked("audio_test_active",
                    "声音通道测试正在运行；结束后再检测按键"
                )
                return
            if not remote_selection.runtime_ready(self._selected_device_id()):
                blocked("device_not_ready",
                    "请先在设备页选择已适配的遥控器。"
                )
                return
            bridge_running = self._refresh_bridge_status()
            if bridge_running is None:
                blocked("service_status_unavailable",
                    "无法确认后台服务状态；请关闭设置窗口和服务后重试"
                )
                return
            if bridge_running:
                runtime_status = bridge_runtime_status.read_status(self._config_root)
                if runtime_status is None:
                    blocked("input_status_unavailable",
                        "后台服务正在运行，但按键通道状态不可用；请重启服务后再检测"
                    )
                    return
                if not bridge_runtime_status.input_channels_ready(runtime_status):
                    blocked("input_channel_unavailable",
                        "按键授权未完成；请回设备页点击“重新授权”"
                        if runtime_status.hid_tap_state in {"authorization_cancelled", "authorization_failed"}
                        else "后台服务当前没有可用的按键通道；请先修复按键通道或重启服务"
                    )
                    return
            logger.info("key detection start accepted: service_running=%s", bool(bridge_running))
            self._set_error_message("")
            self._set_key_detection_text(
                "正在启动真实按键检测…"
            )
            physical_bindings = dict(
                self._bindings.get("physical_bindings", {})
            )
            self._begin_input_operation_start(
                "key_detection",
                lambda cancel_event, _operation_token: self._start_key_detection_worker(
                    cancel_event,
                    bridge_running=bool(bridge_running),
                    physical_bindings=physical_bindings,
                ),
            )

        @Slot()
        def pollKeyDetectionBridge(self) -> None:
            request = self._key_detection_bridge_request
            if not self._key_detection_active:
                return
            if (
                time.monotonic() - self._key_detection_started_at
                >= self._KEY_DETECTION_TIMEOUT_SECONDS
            ):
                if request is not None:
                    timeout_text = (
                        "未检测到按键；请确认遥控器已连接，"
                        "再点击“检测按键”重试"
                    )
                else:
                    timeout_text = (
                        "未检测到按键；请确认遥控器已连接，"
                        "再点击“检测按键”重试"
                    )
                self._key_detection_started_at = 0.0
                self._key_detection_tap_usages.clear()
                self._request_input_stop(
                    kind="key_detection",
                    key_detection_success_message=timeout_text,
                )
                return
            if request is None:
                return
            button_id = key_detection_bridge.poll_detection(request)
            if button_id is not None:
                self._on_raw_key_detected(button_id, " 来源=后台服务。")

        def _start_hotkey_capture(self) -> bool:
            logger = logging_setup.get_logger(self._config_root)
            logger.info(
                "settings hotkey capture: start requested kind=%s phase=%s "
                "has_capture=%s key_detection=%s voice_busy=%s",
                self._input_operation_kind,
                self._input_operation_phase,
                self._hotkey_capture is not None,
                self._key_detection_active,
                self._voice_hotkey_busy,
            )
            accepted = self._begin_input_operation_start(
                "hotkey", self._start_hotkey_capture_worker
            )
            logger.info(
                "settings hotkey capture: start request accepted=%s token=%s",
                accepted,
                self._input_operation_token,
            )
            return accepted

        @Slot(result=bool)
        def startHotkeyCapture(self) -> bool:
            """Start the voice-page shortcut recorder."""

            return self._start_hotkey_capture()

        @Slot(result=bool)
        def startMappingHotkeyCapture(self) -> bool:
            """Start the ordinary button-mapping shortcut recorder."""

            return self._start_hotkey_capture()

        @Slot(result=bool)
        def stopHotkeyCapture(self) -> bool:
            """Stop the recorder, including Cancel/window close."""

            logger = logging_setup.get_logger(self._config_root)
            logger.info(
                "settings hotkey capture: stop requested kind=%s phase=%s "
                "has_capture=%s",
                self._input_operation_kind,
                self._input_operation_phase,
                self._hotkey_capture is not None,
            )
            accepted = self._request_input_stop(kind="hotkey")
            logger.info(
                "settings hotkey capture: stop request accepted=%s token=%s",
                accepted,
                self._input_operation_token,
            )
            return accepted

        @Slot(int, int, str, bool, bool, result=bool)
        def captureHotkeyQtEvent(
            self,
            key: int,
            modifiers: int,
            text: str,
            pressed: bool,
            auto_repeat: bool,
        ) -> bool:
            """Capture focused Qt key events when no low-level edge arrives."""

            return self._capture_hotkey_qt_event(
                key, modifiers, text, pressed, auto_repeat
            )

        def _capture_hotkey_qt_event(
            self,
            key: int,
            modifiers: int,
            text: str,
            pressed: bool,
            auto_repeat: bool,
            *,
            native_virtual_key: int = 0,
            native_scan_code: int = 0,
        ) -> bool:
            if (
                self._input_operation_kind != "hotkey"
                or self._input_operation_phase != "active"
                or self._hotkey_capture is None
            ):
                return False
            key = int(key)
            modifiers = int(modifiers)
            token = _qt_hotkey_token(
                key, text, modifiers,
                native_virtual_key=native_virtual_key,
                native_scan_code=native_scan_code,
            )
            if not token:
                return False
            if auto_repeat:
                return True

            modifier_family = _QT_MODIFIER_KEY_TOKENS.get(key)
            if modifier_family is not None:
                logging_setup.get_logger(self._config_root).info(
                    "settings hotkey capture: Qt modifier edge pressed=%s "
                    "native_vk=0x%X native_scan=0x%X resolved=%s",
                    pressed, native_virtual_key, native_scan_code, token,
                )
            if pressed:
                for flag, modifier_token in _QT_MODIFIER_FLAG_TOKENS:
                    family_tokens = {modifier_token, f"l{modifier_token}", f"r{modifier_token}"}
                    if (
                        modifiers & flag
                        and token not in family_tokens
                        and not family_tokens.intersection(self._qt_hotkey_tokens)
                    ):
                        self._qt_hotkey_tokens.append(modifier_token)
                if token not in self._qt_hotkey_pressed_keys:
                    self._qt_hotkey_pressed_keys.add(token)
                    # Replace a flag-only inference once its actual edge arrives.
                    if (
                        modifier_family is not None
                        and modifier_family != token
                        and modifier_family in self._qt_hotkey_tokens
                        and modifier_family not in self._qt_hotkey_pressed_keys
                    ):
                        index = self._qt_hotkey_tokens.index(modifier_family)
                        self._qt_hotkey_tokens[index] = token
                    if token not in self._qt_hotkey_tokens:
                        self._qt_hotkey_tokens.append(token)
                if modifier_family is None:
                    self._qt_hotkey_primary_key = key
                return True

            was_pressed = token in self._qt_hotkey_pressed_keys
            self._qt_hotkey_pressed_keys.discard(token)
            should_finish = (
                self._qt_hotkey_primary_key == key
                or (
                    self._qt_hotkey_primary_key is None
                    and was_pressed
                    and not self._qt_hotkey_pressed_keys
                )
            )
            if not should_finish or not self._qt_hotkey_tokens:
                return True

            chord = "+".join(self._qt_hotkey_tokens)
            logging_setup.get_logger(self._config_root).info(
                "settings hotkey capture: Qt fallback emitted token=%s chord=%s",
                self._input_operation_token,
                chord,
            )
            self._on_hotkey_capture_result((self._input_operation_token, chord))
            return True

        @Slot(str)
        def reportHotkeyCaptureUiStop(self, reason: str) -> None:
            safe_reason = str(reason).strip()
            if safe_reason not in {
                "capture_field_tapped",
                "escape_pressed",
                "focus_lost",
                "page_hidden",
            }:
                safe_reason = "unknown"
            logging_setup.get_logger(self._config_root).info(
                "settings hotkey capture: QML requested stop reason=%s",
                safe_reason,
            )

        @Slot(result=bool)
        def stopKeyDetection(self) -> bool:
            self._key_detection_started_at = 0.0
            self._key_detection_tap_usages.clear()
            return self._request_input_stop(kind="key_detection")

        @Slot()
        def saveAndLaunch(self) -> None:
            """Start the staged save/launch flow without blocking Qt."""

            if self._get_bridge_launch_busy():
                return
            if self._get_input_capture_in_use():
                self._set_error_message(
                    "按键录入或检测正在进行；结束后再启动遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            self._has_explicit_launch_result = True
            self._bridge_launch_started_at = time.monotonic()
            if self._bridge_launch_elapsed_seconds != 0:
                self._bridge_launch_elapsed_seconds = 0
                self.bridgeLaunchElapsedSecondsChanged.emit()
            self._set_bridge_launch_phase("saving")
            self._set_bridge_connected(False)
            self._set_launch_status("正在保存设置…")
            QTimer.singleShot(0, self._continue_save_and_launch)

        def _continue_save_and_launch(self) -> None:
            if self._bridge_launch_phase != "saving":
                return
            self._save(completion=self._on_save_for_launch_complete)

        def _on_save_for_launch_complete(self, saved: bool) -> None:
            if self._bridge_launch_phase != "saving":
                return
            if not saved:
                self._set_bridge_launch_phase("failed")
                self._set_launch_status("保存未完成，未启动遥控器服务。")
                return
            self._start_bridge_process()

        def _start_automatic_bridge_recovery(self) -> None:
            if not self._bridge_running or self._bridge_recovery_running:
                return
            status = bridge_runtime_status.read_status(self._config_root)
            if status is None or status.voice_active:
                return
            identity_match = bridge_runtime_status.runtime_identity_matches(
                status,
                self._current_runtime_identity,
            )
            if identity_match is True and not self._runtime_status_requires_restart(
                status
            ):
                self._set_bridge_restart_recommended(False)
                return
            self._begin_bridge_restart(automatic=True)

        @Slot()
        def restartBridge(self) -> None:
            self._begin_bridge_restart(automatic=False)

        @Slot()
        def reconnectBridgeNow(self) -> None:
            if self.configurationReadOnly:
                return
            if (
                self._bridge_reconnect_busy
                or self._maintenance_exit_pending
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                return
            running = self._refresh_bridge_status()
            if self._bridge_connected:
                self._set_error_message("")
                return
            if running is False:
                self._set_error_message(
                    "遥控器服务已停止；请点击“启动服务”。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            if running is None:
                self._set_error_message(
                    "当前无法确认服务状态；请稍后重新检查。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            if not self._bridge_reconnect_available:
                self._set_error_message(
                    "当前正在连接或暂时无法直接重连；请稍后再试。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            if bridge_launcher.reconnect_in_process_bridge_now() is not True:
                self._set_bridge_reconnect_available(False)
                self._set_error_message(
                    "立即重连请求暂未送达；请稍后再试。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            self._set_bridge_reconnect_busy(True)
            self._set_bridge_launch_phase("reconnecting")
            self._set_error_message("")
            self._set_launch_status(
                f"服务运行中；正在立即连接{device_catalog.RC003_DISPLAY_NAME}…"
            )
            QTimer.singleShot(
                _BRIDGE_RECONNECT_BUSY_TIMEOUT_MS,
                self._finish_immediate_reconnect_feedback,
            )

        def _finish_immediate_reconnect_feedback(self) -> None:
            if not self._bridge_reconnect_busy:
                return
            self._set_bridge_reconnect_busy(False)
            self._refresh_bridge_status()

        def _begin_bridge_restart(
            self,
            *,
            automatic: bool,
            allow_active_voice: bool = False,
        ) -> None:
            if self.configurationReadOnly:
                return
            if (
                self._remote_selection_busy
                or self._remote_save_uncertain
                or self._bridge_recovery_running
                or self._maintenance_exit_pending
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                return
            if not self._bridge_running:
                self.startBridge()
                return
            status = bridge_runtime_status.read_status(self._config_root)
            if status is not None and status.voice_active and not allow_active_voice:
                self._set_error_message(
                    "当前正在语音输入；结束本次语音后再重新启动服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            self._bridge_recovery_running = True
            self._has_explicit_launch_result = True
            self._bridge_launch_started_at = time.monotonic()
            self._set_bridge_launch_phase("restarting")
            self._set_bridge_restart_recommended(False)
            self._set_bridge_reconnect_available(False)
            self._set_bridge_reconnect_busy(False)
            self._set_bridge_connected(False)
            self._set_bridge_input_states(None)
            self._bridge_status_missing_since = None
            self._set_error_message("")
            self._set_launch_status(
                "检测到旧版或按键通道异常；正在正常停止服务并启动当前版本…"
                if automatic
                else "正在正常停止服务并启动当前版本…"
            )

            def stop_for_restart() -> None:
                try:
                    result = bridge_control_windows.request_bridge_exit(
                        reason="automatic_restart" if automatic else "user_restart")
                except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                    payload = (False, f"重新启动失败：{type(exc).__name__}")
                else:
                    payload = (
                        bool(result.stopped),
                        result.error or "遥控器服务未能正常停止。",
                    )
                self._emit_background_result(self._bridgeRestartStopReady, payload)

            try:
                self._start_background_task(
                    stop_for_restart,
                    "remote-mic-bridge-restart",
                )
            except RuntimeError as exc:
                self._bridge_recovery_running = False
                self._set_bridge_launch_phase("failed")
                self._set_bridge_restart_recommended(True)
                self._set_launch_status(str(exc))

        def _on_bridge_restart_stop_ready(self, payload: object) -> None:
            self._bridge_recovery_running = False
            stopped, message = payload
            if not stopped:
                self._set_bridge_launch_phase("failed")
                self._set_bridge_restart_recommended(True)
                self._set_launch_status(str(message))
                return
            self._set_bridge_running(False)
            self._set_bridge_connected(False)
            self._set_bridge_input_states(None)
            self._set_bridge_reconnect_available(False)
            self._set_bridge_reconnect_busy(False)
            self._bridge_status_missing_since = None
            self._set_bridge_launch_phase("starting")
            self._set_launch_status("旧服务已退出；正在启动当前版本…")
            QTimer.singleShot(0, self._start_bridge_process)

        def _clear_stale_bridge_status_before_launch(self) -> bool:
            """Remove only a stopped bridge's last published status."""

            try:
                stale_status = bridge_runtime_status.read_status(self._config_root)
                if stale_status is None:
                    return True
                if (
                    bridge_launcher.in_process_bridge_running()
                    or single_instance.bridge_instance_running()
                ):
                    return True
                cleared = bridge_runtime_status.clear_status(
                    self._config_root,
                    pid=stale_status.pid,
                )
            except (
                OSError,
                single_instance.SingleInstanceUnavailableError,
                single_instance.MutexCleanupError,
            ):
                return False
            if cleared:
                return True
            replacement = bridge_runtime_status.read_status(self._config_root)
            return replacement is None or replacement.pid != stale_status.pid

        def _start_bridge_process(self) -> None:
            if self.configurationReadOnly:
                return
            if self.activeRemoteKey and not self.activeRemoteReady:
                self._set_bridge_launch_phase("failed")
                self._set_launch_status("当前型号尚未提供接收，请在设备页重新选择。")
                return
            if self._bridge_launch_phase not in {"saving", "starting"}:
                return
            if self._remote_save_uncertain:
                self._set_bridge_launch_phase("failed")
                self._set_launch_status("设备选择尚未成功保存，请重新选择设备。")
                return
            if (
                self._maintenance_exit_pending
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                self._pending_bridge_launch = None
                self._set_bridge_launch_phase("idle")
                self._schedule_application_exit_poll()
                return
            saved_before_launch = self._bridge_launch_phase == "saving"
            self._set_bridge_launch_phase("starting")
            if saved_before_launch:
                self._set_launch_status("设置已保存；正在启动遥控器服务…")
            else:
                self._set_launch_status("正在按上次保存的设置启动遥控器服务…")
            if not self._clear_stale_bridge_status_before_launch():
                self._pending_bridge_launch = None
                self._set_bridge_running(False)
                self._set_bridge_connected(False)
                self._set_bridge_input_states(None)
                self._set_bridge_restart_recommended(True)
                self._set_bridge_launch_phase("failed")
                self._set_launch_status(
                    "无法清理上次服务状态；请完全退出MoteDeck后重试。"
                )
                self._schedule_application_exit_poll()
                return
            try:
                attempt = bridge_launcher.start_bridge_launch()
            except bridge_launcher.BridgeLaunchConfigurationError as exc:
                self._finish_bridge_launch(
                    bridge_launcher.LaunchResult(
                        outcome=bridge_launcher.LaunchOutcome.LAUNCH_FAILED,
                        command=(),
                        error=type(exc).__name__,
                    )
                )
                return
            if isinstance(attempt, bridge_launcher.LaunchResult):
                self._finish_bridge_launch(attempt)
                return
            self._pending_bridge_launch = attempt

        @Slot()
        def startBridge(self) -> None:
            """Start the bridge without saving unrelated unsaved page edits."""

            if self.configurationReadOnly:
                return
            if self._remote_selection_busy:
                return
            if self._remote_save_uncertain:
                self._set_error_message("设备选择尚未成功保存，请重新选择设备。", self._DEVICE_PAGE_INDEX)
                return
            if not self.activeRemoteKey:
                self._set_error_message("请先点击“选择设备”，确认要使用的遥控器。", self._DEVICE_PAGE_INDEX)
                return
            if not self.activeRemoteReady:
                self._set_error_message("当前型号尚未提供接收，请在设备页重新选择。", self._DEVICE_PAGE_INDEX)
                return

            if (
                self._maintenance_exit_pending
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                return
            if self._get_bridge_launch_busy():
                return
            if self._settings_save_busy:
                self._set_error_message(
                    "按键映射正在保存；完成后再启动遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            if self._endpoint_preflight_busy or _driver_action_active_event.is_set():
                self._set_error_message(
                    "输出端点正在处理；完成后再启动遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            if self._get_input_capture_in_use():
                self._set_error_message(
                    "按键录入或检测正在进行；结束后再启动遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            if self._voice_hotkey_busy:
                self._set_error_message(
                    "语音快捷键正在处理；完成后再启动遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            if _vb_cable_test_active_event.is_set():
                self._set_error_message(
                    "声音通道测试正在运行；测试结束后再启动遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            self._has_explicit_launch_result = True
            self._bridge_launch_started_at = time.monotonic()
            if self._bridge_launch_elapsed_seconds != 0:
                self._bridge_launch_elapsed_seconds = 0
                self.bridgeLaunchElapsedSecondsChanged.emit()
            self._set_bridge_connected(False)
            self._set_bridge_input_states(None)
            self._set_bridge_restart_recommended(False)
            self._set_bridge_reconnect_available(False)
            self._set_bridge_reconnect_busy(False)
            self._bridge_status_missing_since = None
            self._set_error_message("")
            self._set_bridge_launch_phase("starting")
            self._set_launch_status(
                "正在按上次保存的设置启动遥控器服务…"
            )
            QTimer.singleShot(0, self._start_bridge_process)

        @Slot()
        def stopBridge(self) -> None:
            """Stop the running bridge after the user confirms."""

            if self.configurationReadOnly:
                return
            if self._remote_selection_busy or self._remote_save_uncertain:
                return
            if (
                self._maintenance_exit_pending
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                return
            if self._get_bridge_launch_busy() or self._bridge_recovery_running:
                return
            if self._settings_save_busy:
                self._set_error_message(
                    "按键映射正在保存；完成后再关闭遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            if self._endpoint_preflight_busy or _driver_action_active_event.is_set():
                self._set_error_message(
                    "输出端点正在处理；完成后再关闭遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            if self._get_input_capture_in_use():
                self._set_error_message(
                    "按键录入或检测正在进行；结束后再关闭遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            if self._voice_hotkey_busy:
                self._set_error_message(
                    "语音快捷键正在处理；完成后再关闭遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            if _vb_cable_test_active_event.is_set():
                self._set_error_message(
                    "声音通道测试正在运行；测试结束后再关闭遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            if not self._bridge_running:
                return
            status = bridge_runtime_status.read_status(self._config_root)
            if status is not None and status.voice_active:
                self._set_error_message(
                    "当前正在语音输入；结束本次语音后再关闭服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return

            self._bridge_recovery_running = True
            self._set_bridge_restart_recommended(False)
            self._set_error_message("")
            self._set_bridge_launch_phase("stopping")
            self._set_launch_status("正在停止遥控器服务…")

            def stop_bridge() -> None:
                try:
                    result = bridge_control_windows.request_bridge_exit(
                        reason="user_stop"
                    )
                except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                    payload = (False, f"关闭服务失败：{type(exc).__name__}")
                else:
                    payload = (
                        bool(result.stopped),
                        result.error or "遥控器服务未能正常停止。",
                    )
                self._emit_background_result(self._bridgeStopReady, payload)

            try:
                self._start_background_task(stop_bridge, "remote-mic-bridge-stop")
            except RuntimeError as exc:
                self._bridge_recovery_running = False
                self._set_bridge_launch_phase("idle")
                self._set_launch_status(str(exc))

        def _on_bridge_stop_ready(self, payload: object) -> None:
            self._bridge_recovery_running = False
            stopped, message = payload
            if not stopped:
                self._set_bridge_launch_phase("idle")
                self._refresh_bridge_status()
                self._set_error_message(str(message), self._DEVICE_PAGE_INDEX)
                return
            self._set_bridge_running(False)
            self._set_bridge_connected(False)
            self._set_bridge_input_states(None)
            self._set_bridge_reconnect_available(False)
            self._set_bridge_reconnect_busy(False)
            self._set_bridge_restart_recommended(False)
            self._bridge_status_missing_since = None
            self._set_bridge_launch_phase("idle")
            self._set_launch_status("遥控器服务已停止。")

        def _finish_bridge_launch(
            self,
            result: bridge_launcher.LaunchResult,
        ) -> None:
            self._pending_bridge_launch = None
            self._refresh_bridge_launch_elapsed()
            if (
                result.outcome is bridge_launcher.LaunchOutcome.ALREADY_RUNNING
                and not bridge_launcher.in_process_bridge_running()
            ):
                self._set_bridge_running(True)
                self._set_bridge_connected(False)
                self._set_bridge_input_states(None)
                self._set_bridge_reconnect_available(False)
                self._set_bridge_reconnect_busy(False)
                self._bridge_status_missing_since = None
                if self._legacy_bridge_launch_handoff_attempted:
                    self._set_bridge_launch_phase("failed")
                    self._set_bridge_restart_recommended(True)
                    self._set_launch_status(
                        "旧版遥控器服务再次启动；请完全退出旧版后重新启动。"
                    )
                else:
                    self._legacy_bridge_launch_handoff_attempted = True
                    self._begin_bridge_restart(
                        automatic=True,
                        allow_active_voice=True,
                    )
                self._schedule_application_exit_poll()
                return
            if result.outcome in {
                bridge_launcher.LaunchOutcome.STARTED,
                bridge_launcher.LaunchOutcome.ALREADY_RUNNING,
            }:
                self._legacy_bridge_launch_handoff_attempted = False
                self._set_bridge_running(True)
                self._set_bridge_launch_phase("waiting")
                self._sync_bridge_connection_status(True)
                self._schedule_application_exit_poll()
                return
            self._set_bridge_running(False)
            self._set_bridge_connected(False)
            self._set_bridge_input_states(None)
            self._set_bridge_restart_recommended(False)
            self._set_bridge_reconnect_available(False)
            self._set_bridge_reconnect_busy(False)
            self._bridge_status_missing_since = None
            self._set_bridge_launch_phase(
                "unknown"
                if result.outcome is bridge_launcher.LaunchOutcome.STATUS_UNKNOWN
                else "failed"
            )
            self._set_launch_status(settings_ui.describe_launch_result(result))
            self._schedule_application_exit_poll()

        @Slot()
        def pollBridgeLaunch(self) -> None:
            self._refresh_bridge_launch_elapsed()
            pending = self._pending_bridge_launch
            if pending is None:
                return
            result = bridge_launcher.poll_bridge_launch(pending)
            if result is not None:
                self._finish_bridge_launch(result)

        @Slot()
        def restoreMappingDefaults(self) -> None:
            """Reset only the button-page values without touching voice setup.

            The auto-save that follows writes to whatever layout is being edited
            (an application profile or the global one), so this never touches
            the other layouts.
            """

            if self.configurationReadOnly:
                return
            defaults = settings_ui.default_display_state(self._selected_device_id())
            self._model.load_display_map(
                defaults.button_display_map,
                defaults.secondary_display_map,
                {},
            )
            self._mark_mapping_dirty()
            self._set_mapping_auto_save_retry_available(False)
            self._set_error_message("")
            if self._mapping_edit_target:
                scope_text = f"「{self._mapping_edit_target_label() or '当前应用'}」"
            else:
                scope_text = "全局布局"
            self._set_status_message(
                f"已恢复{scope_text}的内置默认，正在自动保存按键映射…",
                self._BUTTONS_PAGE_INDEX,
            )
            self._queue_mapping_auto_save()

        mappingPresets = Property(list, lambda self: self._mapping_preset_rows, notify=mappingPresetsChanged)

        # The voice page renders the same library, labelled with each profile's
        # automatic mode tag. Sharing one row list keeps the two pages in sync.
        voiceProfiles = Property(list, lambda self: self._mapping_preset_rows, notify=mappingPresetsChanged)

        def _live_voice_fields(self) -> tuple[str, str]:
            return (self._get_voice_input_source(),
                    config.voice_local_input_device(self._config))

        mappingPresetScopeOptions = Property(
            list,
            lambda self: [{"value": value, "label": app_profiles.SCOPE_LABELS[value]}
                          for value in app_profiles.SCOPES],
            constant=True,
        )

        @Slot(str, str, str, str, result="QVariantMap")
        def setMappingPresetStatus(self, identity: str, scope: str, process: str, hotkey: str) -> dict:
            """Persist one saved preset's switching scope/process/hotkey."""
            try:
                if (self._mapping_auto_save_temporarily_blocked() or self._remote_selection_busy
                        or self._get_input_capture_in_use() or self._application_exit_requested):
                    raise ValueError("正在处理设置或输入，请完成后重试。")
                if identity == app_profiles.GLOBAL_ID:
                    raise ValueError("自动保存项代表当前按键设置，不能设置状态。")
                app_profiles.AppProfileStore(self._config_root).set_status(
                    identity, scope, process, hotkey)
                self._refresh_mapping_presets()
                return {"ok": True, "id": identity, "message": "配置状态已保存。"}
            except Exception as exc:  # Qt slots must keep the modal usable on bad input.
                detail = str(exc)
                message = detail if len(detail) <= 240 else detail[:240] + "…"
                return {"ok": False, "id": identity, "message": message}

        @Slot()
        def refreshVoiceProfiles(self) -> None:
            """Rebuild the profile list before the manager dialog shows it."""
            try:
                self._refresh_mapping_presets()
            except Exception:  # noqa: BLE001 - the dialog stays usable on a bad store
                pass

        def _refresh_mapping_presets(self) -> None:
            profile = self._selected_device_id()
            source, device = self._live_voice_fields()
            entries = app_profiles.AppProfileStore(self._config_root).view(
                self._bindings, profile,
                voice_input_source=source, voice_local_input_device=device)
            self._mapping_preset_rows = [
                {key: entry[key] for key in ("id", "name", "automatic", "applied",
                                             "scope", "process", "hotkey",
                                             "voice_input_source",
                                             "voice_local_input_device",
                                             "mode_tag", "label", "voice_conflicts",
                                             "voice_conflict_text")}
                for entry in entries
            ]
            self.mappingPresetsChanged.emit()
            self._refresh_mapping_apps()

        def _resolved_edit_target(self) -> tuple[str, dict]:
            """The document the button page should display.

            "" means the global layout; a preset id means one application's
            own layout, layered on top of the global one. Any inconsistency
            (another remote model, a deleted preset, a broken store) falls back
            to the global layout instead of leaving the page empty.
            """
            profile = self._selected_device_id()
            identity = self._mapping_edit_target
            if not identity:
                return "", self._bindings
            try:
                entry = app_profiles.AppProfileStore(self._config_root).get(identity)
                if entry["scope"] != "app":
                    return "", self._bindings
                return identity, app_profiles.apply_to(self._bindings, entry, profile)
            except Exception:  # noqa: BLE001 - a broken store must not wipe the page
                return "", self._bindings

        def _active_edit_bindings(self) -> dict:
            return self._resolved_edit_target()[1]

        def _mapping_edit_target_label(self) -> str:
            if not self._mapping_edit_target:
                return ""
            try:
                return app_profiles.AppProfileStore(self._config_root).get(
                    self._mapping_edit_target)["name"]
            except Exception:  # noqa: BLE001 - label is cosmetic
                return ""

        def _executable_for_app(self, process: str) -> str:
            key = str(process or "").casefold()
            if not key:
                return ""
            if key not in self._app_executable_cache:
                try:
                    self._app_executable_cache[key] = (
                        running_apps_windows.executable_for_process(
                            process, self._config_root))
                except Exception:  # noqa: BLE001 - icon lookup is best effort
                    self._app_executable_cache[key] = ""
            return self._app_executable_cache[key]

        def _icon_for(self, executable: str) -> str:
            if not executable:
                return ""
            if executable not in self._app_icon_cache:
                try:
                    self._app_icon_cache[executable] = app_icons.icon_file_url(executable)
                except Exception:  # noqa: BLE001 - icons are cosmetic only
                    self._app_icon_cache[executable] = ""
            return self._app_icon_cache[executable]

        def _friendly_app_name(self, process: str, executable: str = "") -> str:
            path = executable or self._executable_for_app(process)
            description = ""
            try:
                description = running_apps_windows.file_description(path)
            except Exception:  # noqa: BLE001 - naming is best effort
                description = ""
            if not description:
                wanted = str(process or "").casefold()
                for row in self._running_applications:
                    if row["process"] == wanted:
                        description = str(row.get("name", ""))
                        break
            description = "".join(
                char for char in (description or "") if ord(char) >= 32).strip()
            description = description or Path(str(process)).stem or str(process)
            return description if len(description) <= 60 else description[:60] + "…"

        def _refresh_mapping_apps(self) -> None:
            try:
                entries = app_profiles.AppProfileStore(self._config_root).list_apps()
            except Exception:  # noqa: BLE001 - an unreadable store must not break the panel
                entries = []
            # Resolve every saved process in one pass: a single window scan can
            # recover paths for applications that never register under App Paths.
            try:
                resolved = running_apps_windows.resolve_executables(
                    [entry["process"] for entry in entries], self._config_root)
            except Exception:  # noqa: BLE001 - icon lookup is best effort
                resolved = None
            if resolved is not None:
                # Cache misses too, so the row loop below does not re-scan.
                for entry in entries:
                    key = str(entry["process"] or "").casefold()
                    if key:
                        self._app_executable_cache[key] = resolved.get(key, "")
            rows: List[dict] = []
            for entry in entries:
                executable = self._executable_for_app(entry["process"])
                rows.append({
                    "id": entry["id"],
                    "name": entry["name"],
                    "process": entry["process"],
                    "icon": self._icon_for(executable),
                    "active": entry["id"] == self._mapping_edit_target,
                })
            self._mapping_app_rows = rows
            self.mappingAppProfilesChanged.emit()

        mappingEditTarget = Property(
            str,
            lambda self: self._mapping_edit_target,
            notify=mappingEditTargetChanged,
        )

        mappingEditTargetLabel = Property(
            str,
            lambda self: self._mapping_edit_target_label(),
            notify=mappingEditTargetChanged,
        )

        mappingAppProfiles = Property(
            list,
            lambda self: self._mapping_app_rows,
            notify=mappingAppProfilesChanged,
        )

        runningApplications = Property(
            list,
            lambda self: self._running_applications,
            notify=runningApplicationsChanged,
        )

        def _busy_for_app_edit(self) -> None:
            if (self._mapping_auto_save_temporarily_blocked()
                    or self._remote_selection_busy
                    or self._get_input_capture_in_use()
                    or self._application_exit_requested):
                raise ValueError("正在处理设置或输入，请完成后重试。")

        @Slot(str, result="QVariantMap")
        def selectMappingEditTarget(self, identity: str) -> dict:
            """Point the button page at the global layout or one application."""
            try:
                self._busy_for_app_edit()
                identity = str(identity or "")
                if identity in ("", app_profiles.GLOBAL_ID):
                    identity = ""
                if identity:
                    entry = app_profiles.AppProfileStore(self._config_root).get(identity)
                    if entry["scope"] != "app":
                        raise ValueError("这套配置没有绑定应用程序。")
                if identity == self._mapping_edit_target:
                    return {"ok": True, "id": identity,
                            "message": self._mapping_edit_target_message()}
                if self._mapping_dirty:
                    # Flush the layout the user was just editing before the page
                    # swaps to another one, otherwise the edits would be lost.
                    self._run_mapping_auto_save()
                    if self._mapping_dirty:
                        raise ValueError("上一套按键映射尚未保存完成，请稍后重试。")
                self._mapping_edit_target = identity
                self.mappingEditTargetChanged.emit()
                self._load_bindings_into_model()
                self._refresh_mapping_apps()
                return {"ok": True, "id": identity,
                        "message": self._mapping_edit_target_message()}
            except Exception as exc:  # noqa: BLE001 - Qt slots must not escape
                detail = str(exc)
                message = detail if len(detail) <= 240 else detail[:240] + "…"
                return {"ok": False, "id": self._mapping_edit_target, "message": message}

        def _mapping_edit_target_message(self) -> str:
            if self._mapping_edit_target:
                return f"正在编辑「{self._mapping_edit_target_label()}」的按键映射；切换到该应用时生效。"
            return "正在编辑全局按键映射。"

        @Slot(result="QVariantMap")
        def refreshMappingApplications(self) -> dict:
            """Re-read the list of applications that own a visible window."""
            try:
                own_process = os.path.basename(sys.executable).casefold()
            except Exception:  # noqa: BLE001 - own name is only used to filter
                own_process = ""
            known = {row["process"].casefold() for row in self._mapping_app_rows}
            try:
                listed = running_apps_windows.list_running_applications()
            except Exception:  # noqa: BLE001 - enumeration is best effort
                listed = []
            rows: List[dict] = []
            learned: dict = {}
            for item in listed:
                process = str(item.get("process", ""))
                if not process or process.casefold() == own_process:
                    continue
                executable = str(item.get("executable", ""))
                if executable:
                    self._app_executable_cache[process.casefold()] = executable
                    learned[process] = executable
                rows.append({
                    "process": process,
                    "name": str(item.get("name") or process),
                    "executable": executable,
                    "title": str(item.get("title", "")),
                    "icon": self._icon_for(executable),
                    "selected": process.casefold() in known,
                })
            self._running_applications = rows
            self.runningApplicationsChanged.emit()
            # Remember the paths so icons still render after a restart, even for
            # applications that do not register themselves under App Paths.
            try:
                running_apps_windows.remember_executables(self._config_root, learned)
            except Exception:  # noqa: BLE001 - caching is best effort
                pass
            return {"ok": True, "id": "", "message": ""}

        @Slot(str, str, result="QVariantMap")
        def addMappingAppProfile(self, value: str, name: str) -> dict:
            """Add (or select) one application's own button layout."""
            try:
                if self.configurationReadOnly:
                    raise config.ConfigReadOnlyError(self._configuration_error)
                self._busy_for_app_edit()
                raw = str(value or "").strip().strip('"')
                if not raw:
                    raise ValueError("请先选择或填写应用程序。")
                if raw.lower().startswith("file:"):
                    # The file dialog hands us an url, the store wants a path.
                    raw = QUrl(raw).toLocalFile() or raw
                has_separator = any(part in raw for part in ("/", "\\", ":"))
                is_file = Path(raw).is_file() if has_separator else False
                if has_separator and not is_file:
                    raise ValueError("请填写进程名（如 chrome.exe），或用“浏览”选择 .exe 文件。")
                process = running_apps_windows.process_name_from_path(raw)
                if not process:
                    raise ValueError("无法识别该应用程序的进程名。")
                if "." not in process:
                    process += ".exe"
                profile = self._selected_device_id()
                store = app_profiles.AppProfileStore(self._config_root)
                existing = store.find_app(process)
                if existing is not None:
                    identity = existing["id"]
                else:
                    executable = raw if is_file else self._executable_for_app(process)
                    if executable:
                        try:
                            running_apps_windows.remember_executables(
                                self._config_root, {process: executable})
                        except Exception:  # noqa: BLE001 - caching is best effort
                            pass
                    # A window title or file description may carry control
                    # characters and WILL be rejected by the store, so normalise
                    # the caller's label before it is saved.
                    requested = "".join(
                        char for char in str(name or "") if ord(char) >= 32).strip()
                    display = requested or self._friendly_app_name(process, executable)
                    identity = store.ensure_app(
                        display,
                        process,
                        # A new application starts from the global layout; the
                        # caller decides whether it becomes the edit target.
                        app_profiles.snapshot(self._bindings, profile),
                    )
                entry = store.get(identity)
                # Follow the Logi Options+ flow: adding an application starts
                # editing that application, otherwise the button page would keep
                # writing the global layout and the new row would never change.
                switched = False
                if identity != self._mapping_edit_target:
                    if self._mapping_dirty:
                        self._run_mapping_auto_save()
                    if not self._mapping_dirty:
                        self._mapping_edit_target = identity
                        switched = True
                        self.mappingEditTargetChanged.emit()
                        self._load_bindings_into_model()
                self._refresh_mapping_presets()
                message = f"已添加「{entry['name']}」；它的按键映射与全局设置分开保存。"
                if switched:
                    message = (f"已添加「{entry['name']}」；按键列表已切换到它，"
                               "这里改的按键只对这个应用生效。")
                return {"ok": True, "id": identity, "message": message}
            except Exception as exc:  # noqa: BLE001 - Qt slots must not escape
                detail = str(exc)
                message = detail if len(detail) <= 240 else detail[:240] + "…"
                return {"ok": False, "id": "", "message": message}

        @Slot(str, result="QVariantMap")
        def removeMappingAppProfile(self, identity: str) -> dict:
            """Delete one application's layout; it falls back to the global one."""
            try:
                if self.configurationReadOnly:
                    raise config.ConfigReadOnlyError(self._configuration_error)
                self._busy_for_app_edit()
                if identity == app_profiles.GLOBAL_ID:
                    raise ValueError("自动保存项不能删除。")
                store = app_profiles.AppProfileStore(self._config_root)
                entry = store.get(identity)
                store.delete(identity)
                if identity == self._mapping_edit_target:
                    self._mapping_edit_target = ""
                    self.mappingEditTargetChanged.emit()
                    self._load_bindings_into_model()
                self._refresh_mapping_presets()
                return {"ok": True, "id": "",
                        "message": f"已删除「{entry['name']}」；该应用改用全局按键映射。"}
            except Exception as exc:  # noqa: BLE001 - Qt slots must not escape
                detail = str(exc)
                message = detail if len(detail) <= 240 else detail[:240] + "…"
                return {"ok": False, "id": identity, "message": message}

        @Slot(str, result="QVariantMap")
        def exportMappingTarget(self, text: str) -> dict:
            """Write only the layout the button page is currently editing."""
            try:
                if self.configurationReadOnly:
                    raise config.ConfigReadOnlyError(self._configuration_error)
                self._busy_for_app_edit()
                url = QUrl(text)
                if not url.isLocalFile():
                    raise ValueError("请选择本地 JSON 保存位置。")
                if self._mapping_dirty:
                    # Export what the user is looking at, not an older version of
                    # it; the auto save also lands the pending edits on disk.
                    self._run_mapping_auto_save()
                    if self._mapping_dirty:
                        raise ValueError("按键映射尚未保存完成，请稍后重试。")
                profile = self._selected_device_id()
                identity, _ = self._resolved_edit_target()
                source, device = self._live_voice_fields()
                store = app_profiles.AppProfileStore(self._config_root)
                if identity:
                    # An app layout stores only its own overrides, so export that
                    # instead of the global-plus-app layout shown on screen.
                    entry = store.get(identity)
                    name = entry["name"]
                    mapping = entry["mapping"]
                    source = entry["voice_input_source"]
                    device = entry["voice_local_input_device"]
                else:
                    name = "全局布局"
                    mapping = app_profiles.snapshot(self._bindings, profile)
                store.export_entry({"name": name, "mapping": mapping,
                                    "voice_input_source": source,
                                    "voice_local_input_device": device},
                                   Path(url.toLocalFile()), profile)
                message = (f"已导出「{name}」的按键映射。"
                           if identity else "已导出全局布局的按键映射。")
                return {"ok": True, "id": identity, "message": message}
            except Exception as exc:  # noqa: BLE001 - Qt slots must not escape
                detail = str(exc)
                message = detail if len(detail) <= 240 else detail[:240] + "…"
                return {"ok": False, "id": "", "message": message}

        @Slot(str, result="QVariantMap")
        def exportAllMappingPresets(self, text: str) -> dict:
            """Back up every saved profile plus the live global layout."""
            try:
                if self.configurationReadOnly:
                    raise config.ConfigReadOnlyError(self._configuration_error)
                self._busy_for_app_edit()
                url = QUrl(text)
                if not url.isLocalFile():
                    raise ValueError("请选择本地 JSON 保存位置。")
                if self._mapping_dirty:
                    self._run_mapping_auto_save()
                    if self._mapping_dirty:
                        raise ValueError("按键映射尚未保存完成，请稍后重试。")
                profile = self._selected_device_id()
                source, device = self._live_voice_fields()
                count = app_profiles.AppProfileStore(self._config_root).export_library(
                    Path(url.toLocalFile()),
                    profile,
                    app_profiles.snapshot(self._bindings, profile),
                    voice_input_source=source,
                    voice_local_input_device=device,
                )
                return {"ok": True, "id": "",
                        "message": f"已导出 {count} 个配置和当前全局按键。"}
            except Exception as exc:  # noqa: BLE001 - Qt slots must not escape
                detail = str(exc)
                message = detail if len(detail) <= 240 else detail[:240] + "…"
                return {"ok": False, "id": "", "message": message}

        @Slot(str, result="QVariantMap")
        def importMappingTarget(self, text: str) -> dict:
            """Replace only the layout the button page is currently editing."""
            try:
                if self.configurationReadOnly:
                    raise config.ConfigReadOnlyError(self._configuration_error)
                self._busy_for_app_edit()
                if self._mapping_dirty:
                    self._run_mapping_auto_save()
                    if self._mapping_dirty:
                        raise ValueError("请先等当前按键映射保存完成，再导入配置。")
                url = QUrl(text)
                if not url.isLocalFile():
                    raise ValueError("请选择本地 JSON 配置文件。")
                profile = self._selected_device_id()
                document = app_profiles.read_export(Path(url.toLocalFile()), profile)
                identity, base = self._resolved_edit_target()
                # Merge the three mapping fields over the live layout so learned
                # physical signatures and the combo layer stay on this machine.
                bindings = app_profiles.apply_to(
                    base, {"mapping": document["mapping"]}, profile)
                name = "全局布局"
                if identity:
                    name = f"「{app_profiles.AppProfileStore(self._config_root).get(identity)['name']}」"
            except Exception as exc:  # noqa: BLE001 - Qt slots must not escape
                detail = str(exc)
                message = detail if len(detail) <= 240 else detail[:240] + "…"
                return {"ok": False, "id": "", "message": message}

            if not self._save_mapping(replacement=bindings):
                return {"ok": False, "id": identity,
                        "message": "导入未能保存，请检查按键页的提示后重试。"}
            if identity:
                # The app branch of _save_mapping leaves the page alone; the
                # imported mapping has to reach the model here.
                self._load_bindings_into_model()
            return {"ok": True, "id": identity,
                    "message": f"已把文件里的映射导入到{name}。"}

        @Slot(str, str, str, result="QVariantMap")
        def mappingPresetAction(self, operation: str, identity: str, text: str) -> dict:
            """Modal UI operations use stable ids, never mutable row indices."""
            try:
                if self.configurationReadOnly:
                    raise config.ConfigReadOnlyError(self._configuration_error)
                if (self._mapping_auto_save_temporarily_blocked() or self._remote_selection_busy
                        or self._get_input_capture_in_use() or self._application_exit_requested):
                    raise ValueError("正在处理设置或输入，请完成后重试。")
                store = app_profiles.AppProfileStore(self._config_root)
                profile = self._selected_device_id()
                source, device = self._live_voice_fields()
                message = ""
                if operation == "refresh":
                    pass
                elif operation == "import":
                    url = QUrl(text)
                    if not url.isLocalFile():
                        raise ValueError("请选择本地 JSON 配置文件。")
                    identity = store.import_file(Path(url.toLocalFile()), profile)
                    message = "配置已导入；点击“应用配置”后才会生效。"
                elif operation == "save":
                    identity = store.create(
                        text, app_profiles.snapshot(self._bindings, profile),
                        voice_input_source=source, voice_local_input_device=device)
                    message = "配置已保存；点击“应用配置”可随时切回。"
                else:
                    if identity == app_profiles.GLOBAL_ID:
                        if operation in ("rename", "delete"):
                            raise ValueError("自动保存项代表当前按键设置，不能重命名或删除。")
                        if self._mapping_dirty:
                            raise ValueError("当前映射尚未保存，请先完成自动保存或重试保存。")
                        entry = {"id": app_profiles.GLOBAL_ID, "name": app_profiles.GLOBAL_NAME,
                                 "mapping": app_profiles.snapshot(self._bindings, profile),
                                 "voice_input_source": source,
                                 "voice_local_input_device": device}
                    else:
                        entry = store.get(identity)
                    if operation == "copy":
                        if entry["id"] == app_profiles.GLOBAL_ID:
                            identity = store.create(
                                text, entry["mapping"],
                                voice_input_source=entry["voice_input_source"],
                                voice_local_input_device=entry["voice_local_input_device"])
                        else:
                            identity = store.copy_profile(
                                identity, text, bindings=self._bindings, profile=profile)
                        message = "配置已复制。"
                    elif operation == "update":
                        if entry["id"] == app_profiles.GLOBAL_ID:
                            raise ValueError("自动保存项就是当前按键设置，无需更新。")
                        if (entry["scope"] == "app"
                                and self._mapping_edit_target != identity):
                            raise ValueError("请先在按键页切换到该应用配置，再更新它的按键。")
                        store.update_content(
                            identity,
                            app_profiles.snapshot(self._active_edit_bindings(), profile),
                            voice_input_source=source, voice_local_input_device=device)
                        message = "配置已更新为当前按键和语音设置。"
                    elif operation == "rename":
                        store.rename(identity, text)
                        message = "配置已重命名。"
                    elif operation == "delete":
                        store.delete(identity)
                        identity = ""
                        message = "保存的配置已删除，当前按键设置不变。"
                    elif operation == "apply":
                        if self._mapping_dirty:
                            raise ValueError("当前映射尚未保存，请先完成自动保存或重试保存。")
                        bindings = app_profiles.apply_to(self._bindings, entry, profile)
                        conflict_buttons = app_profiles.voice_conflicts(
                            bindings, entry["voice_input_source"], profile)
                        if conflict_buttons:
                            raise ValueError(app_profiles.voice_conflict_message(
                                conflict_buttons) + "，然后再应用该配置。")
                        if not self._save_mapping(replacement=bindings):
                            raise ValueError("未能确认配置保存成功，请检查按键页的保存提示并重试。")
                        message = "配置已应用；正在按住的按键结束后生效。"
                        switched = False
                        target = (entry["voice_input_source"],
                                  entry["voice_local_input_device"])
                        if target != (source, device):
                            updates = {"voice_input_source": target[0],
                                       "voice_local_input_device": target[1]}
                            if (target[0] == config.VOICE_INPUT_SOURCE_LOCAL
                                    and not target[1]):
                                devices = local_microphone_windows.list_input_devices()
                                if devices:
                                    updates["voice_local_input_device"] = devices[0]
                            if not self._persist_voice_config_fields(updates):
                                raise ValueError(
                                    "按键已应用，但语音模式未能保存，请在语音页重新选择。")
                            switched = True
                        if switched:
                            self.voiceInputSourceChanged.emit()
                            self.localMicrophoneDeviceChanged.emit()
                            message += " 语音模式已切换。"
                        try:
                            store.mark_applied(identity)
                        except Exception:
                            # Applying the mapping already succeeded. A separate marker failure
                            # must not misreport it as an unapplied configuration.
                            message += " 应用标记未能保存，重启后可能无法保留加粗提示。"
                    elif operation == "export":
                        url = QUrl(text)
                        if not url.isLocalFile():
                            raise ValueError("请选择本地 JSON 保存位置。")
                        store.export_entry(entry, Path(url.toLocalFile()))
                        message = "配置已导出。"
                    else:
                        raise ValueError("不支持的配置操作。")
                try:
                    self._refresh_mapping_presets()
                except Exception:
                    if operation == "refresh":
                        raise
                    # The operation has already committed. Do not invite the user
                    # to repeat an import/copy because a later list read failed.
                    return {"ok": True, "id": identity,
                            "message": message + " 列表未能刷新，请重新打开面板查看。"}
                if not identity:
                    identity = next(row["id"] for row in self._mapping_preset_rows if row["applied"])
                return {"ok": True, "id": identity, "message": message}
            except Exception as exc:  # Qt slots must preserve UI state on bad files or disk errors.
                # Imported values can appear in parser errors; keep the modal usable.
                detail = str(exc)
                message = detail if len(detail) <= 240 else detail[:240] + "…"
                return {"ok": False, "id": identity, "message": message}

        @Slot()
        def restoreDefaults(self) -> None:
            """Resets every widget's DISPLAYED value to the defaults - never
            writes to config.json/key_bindings.json itself (XRBM-030 RETRY 1
            blocker 4: this must never look/claim to be persisted). The
            status message says so explicitly, so a user who restores
            defaults and then simply closes the window without saving is
            not misled into thinking anything was written to disk.
            """

            if self.configurationReadOnly or self._voice_hotkey_busy:
                return

            defaults = settings_ui.default_display_state(self._selected_device_id())
            self._model.load_display_map(
                defaults.button_display_map,
                defaults.secondary_display_map,
                {},
            )
            self._set_voice_hotkey_text(defaults.voice_hotkeys["hold"])
            self._mark_mapping_dirty()
            self._set_error_message("")
            self._set_status_message(
                "已恢复默认显示，尚未保存。",
                self._BUTTONS_PAGE_INDEX,
            )

        @Slot(result=bool)
        def openUsageGuide(self) -> bool:
            result = shell_targets.open_external_target(
                application_update.WEBSITE_GUIDE_URL
            )
            self._report_external_target(result, self._DEVICE_PAGE_INDEX)
            return result.outcome is shell_targets.ExternalTargetOutcome.OPENED

        @Slot(result=bool)
        def openWebsite(self) -> bool:
            result = shell_targets.open_external_target(application_update.WEBSITE_URL)
            self._report_external_target(result, self._DEVICE_PAGE_INDEX)
            return result.outcome is shell_targets.ExternalTargetOutcome.OPENED

        @Slot(result=bool)
        def openAuthorPage(self) -> bool:
            result = shell_targets.open_external_target(application_update.AUTHOR_URL)
            self._report_external_target(result, self._DEVICE_PAGE_INDEX)
            return result.outcome is shell_targets.ExternalTargetOutcome.OPENED

        @Slot(result=bool)
        def openLicense(self) -> bool:
            result = shell_targets.open_external_target(application_update.LICENSE_URL)
            self._report_external_target(result, self._DEVICE_PAGE_INDEX)
            return result.outcome is shell_targets.ExternalTargetOutcome.OPENED

        @Slot(result=str)
        def logExportDefaultFile(self) -> str:
            folder = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
            return QUrl.fromLocalFile(str(Path(folder or str(Path.home())) / log_export.default_filename())).toString()

        @Slot(result=bool)
        def openLogLocation(self) -> bool:
            if self._application_update_operation_blocked():
                return False
            result = logging_setup.open_log_location(self._config_root)
            self._set_error_message('')
            if result.outcome is logging_setup.LogOpenOutcome.OPENED:
                self._set_status_message('已打开日志文件夹。', self._DEVICE_PAGE_INDEX)
                return True
            if result.outcome is logging_setup.LogOpenOutcome.DIRECTORY_MISSING:
                self._set_status_message('日志文件夹尚未生成。请先运行服务。', self._DEVICE_PAGE_INDEX)
            else:
                self._set_status_message('')
                self._set_error_message('无法打开日志文件夹，请稍后重试。', self._DEVICE_PAGE_INDEX)
            return False

        @Slot(str, result=bool)
        def exportLogs(self, selected_file: str) -> bool:
            if self._log_export_busy or self._application_update_operation_blocked():
                return False
            if not selected_file:
                return False
            url = QUrl(selected_file)
            if not url.isLocalFile():
                self._set_error_message('请选择一个 ZIP 文件保存位置。', self._DEVICE_PAGE_INDEX)
                return False
            destination = Path(url.toLocalFile())
            self._log_export_busy = True
            self.logExportBusyChanged.emit()
            self._set_error_message('')
            self._set_status_message('正在导出日志…', self._DEVICE_PAGE_INDEX)

            def run() -> None:
                try:
                    result = log_export.export_logs(destination, root=self._config_root,
                                                    cancel=self._background_shutdown_event)
                except Exception:
                    result = log_export.ExportResult('write_failed')
                self._emit_background_result(self._logExportReady, result)
            try:
                self._start_background_task(run, 'remote-mic-log-export')
            except Exception:
                self._on_log_export_ready(log_export.ExportResult('write_failed'))
                return False
            return True

        def _on_log_export_ready(self, result: log_export.ExportResult) -> None:
            if self._background_shutdown_event.is_set():
                return
            self._log_export_busy = False
            self.logExportBusyChanged.emit()
            message = log_export.describe_result(result)
            if result.outcome in {'exported', 'no_logs', 'cancelled'}:
                self._set_status_message(message, self._DEVICE_PAGE_INDEX)
            else:
                self._set_status_message('')
                self._set_error_message(message, self._DEVICE_PAGE_INDEX)

        def _application_update_operation_blocked(self) -> bool:
            return bool(
                self._background_shutdown_event.is_set()
                or self._application_exit_requested
                or self._application_exit_intent.is_set()
                or self._application_exit_confirmed
            )

        def _application_update_result_discarded(self) -> bool:
            return bool(
                self._background_shutdown_event.is_set()
                or self._application_exit_confirmed
            )

        def _application_update_package_kind(self):
            release = self._application_update_release
            if release is not None:
                if (
                    self._hid_helper_installed_distribution
                    and release.installer is not None
                ):
                    return application_update.PackageKind.INSTALLER
                if release.portable is not None:
                    return application_update.PackageKind.PORTABLE
                if release.installer is not None:
                    return application_update.PackageKind.INSTALLER
            return (
                application_update.PackageKind.INSTALLER
                if self._hid_helper_installed_distribution
                else application_update.PackageKind.PORTABLE
            )

        def _set_application_update_release(self, release) -> None:
            self._application_update_release = release
            self._application_update_release_url = release.release_url
            self._application_update_release_notes = (
                release.notes or "暂无更新说明。"
            )
            package = release.package_for(self._application_update_package_kind())
            self._application_update_package_name = package.name
            self._application_update_package_size = package.size
            self._application_update_download_received = 0
            self._application_update_download_path = ""

        @Slot(result=bool)
        def checkForApplicationUpdate(self) -> bool:
            return self._start_application_update_check(silent=False)

        @Slot(result=bool)
        def checkForApplicationUpdateOnStartup(self) -> bool:
            if self._application_update_startup_attempted:
                return False
            self._application_update_startup_attempted = True
            # Never clear a result or download initiated by the user before the
            # delayed startup check. The next application start can try again.
            if self._application_update_state != "idle":
                return False
            return self._start_application_update_check(silent=True)

        @Slot(result=bool)
        def showApplicationUpdate(self) -> bool:
            if self._application_update_operation_blocked():
                return False
            self.applicationUpdateDialogRequested.emit()
            return True

        def _start_application_update_check(self, *, silent: bool) -> bool:
            if not APPLICATION_UPDATE_CHECK_ENABLED:
                if not silent:
                    self._set_error_message("")
                    self._set_status_message(
                        "本特别版已停用更新检查。", self._DEVICE_PAGE_INDEX
                    )
                return False
            if (
                self._application_update_check_busy
                or self._application_update_download_busy
                or self._application_update_operation_blocked()
            ):
                return False

            self._application_update_check_busy = True
            self._application_update_check_silent = silent
            self._application_update_available = False
            self._application_update_state = "checking"
            self._application_update_message = "正在检查 GitHub 更新…"
            self._application_update_release_notes = ""
            self._application_update_release_url = application_update.RELEASES_PAGE_URL
            self._application_update_release = None
            self._application_update_package_name = ""
            self._application_update_package_size = 0
            self._application_update_download_received = 0
            self._application_update_download_path = ""
            if not silent:
                self._set_error_message("")
                self._set_status_message(
                    "正在检查 GitHub 更新…", self._DEVICE_PAGE_INDEX
                )
            self.applicationUpdateChanged.emit()

            def run() -> None:
                try:
                    if silent and not application_update.claim_daily_update_check(
                        self._config_root / "updates"
                    ):
                        result = None
                    else:
                        result = application_update.check_for_update(__version__)
                    payload = (True, result)
                except application_update.ApplicationUpdateError as exc:
                    payload = (False, exc)
                except Exception:  # noqa: BLE001 - keep remote errors sanitized
                    payload = (
                        False,
                        application_update.ApplicationUpdateError(
                            "unexpected", "检查更新失败，请稍后重试。"
                        ),
                    )
                self._emit_background_result(
                    self._applicationUpdateCheckReady, payload
                )

            try:
                self._start_background_task(run, "remote-mic-update-check")
            except Exception:
                self._application_update_check_busy = False
                self._application_update_check_silent = False
                if silent:
                    self._application_update_state = "idle"
                    self._application_update_message = ""
                    self.applicationUpdateChanged.emit()
                    return False
                self._application_update_state = "check_error"
                self._application_update_message = "无法启动更新检查后台任务。"
                self._set_status_message("")
                self._set_error_message(
                    self._application_update_message, self._DEVICE_PAGE_INDEX
                )
                self.applicationUpdateChanged.emit()
                self.applicationUpdateDialogRequested.emit()
                return False
            return True

        def _on_application_update_check_ready(self, payload: object) -> None:
            if self._application_update_result_discarded():
                return
            succeeded, value = payload
            silent = self._application_update_check_silent
            self._application_update_check_silent = False
            self._application_update_check_busy = False
            if silent and (
                not succeeded or value is None
                or value.outcome is not application_update.UpdateCheckOutcome.UPDATE_AVAILABLE
            ):
                self._application_update_state = "idle"
                self._application_update_message = ""
                self.applicationUpdateChanged.emit()
                return
            if not succeeded:
                self._application_update_available = False
                self._application_update_state = "check_error"
                self._application_update_message = str(value)
                self._set_status_message("")
                self._set_error_message(
                    f"检查更新失败：{value}", self._DEVICE_PAGE_INDEX
                )
                self.applicationUpdateChanged.emit()
                if not self._application_update_operation_blocked():
                    self.applicationUpdateDialogRequested.emit()
                return

            result = value
            self._set_application_update_release(result.release)
            if not silent:
                self._set_error_message("")
            if (
                result.outcome
                is application_update.UpdateCheckOutcome.UPDATE_AVAILABLE
            ):
                self._application_update_available = True
                self._application_update_state = "available"
                self._application_update_message = (
                    f"发现新版本 {result.release.version.text}。"
                )
            elif result.outcome is application_update.UpdateCheckOutcome.CURRENT:
                self._application_update_available = False
                self._application_update_state = "current"
                self._application_update_message = "当前已是最新版本。"
            else:
                self._application_update_available = False
                self._application_update_state = "local_newer"
                self._application_update_message = (
                    "当前版本比 GitHub 上可下载的版本更新。"
                )
            if not silent:
                self._set_status_message(
                    self._application_update_message, self._DEVICE_PAGE_INDEX
                )
            self.applicationUpdateChanged.emit()
            if not self._application_update_operation_blocked():
                if silent:
                    self.applicationUpdateNotificationRequested.emit(
                        f"发现新版本 {result.release.version.text}，点击查看并下载。"
                    )
                else:
                    self.applicationUpdateDialogRequested.emit()

        @Slot(result=bool)
        def downloadApplicationUpdate(self) -> bool:
            if (
                not self._application_update_available
                or self._application_update_release is None
                or self._application_update_check_busy
                or self._application_update_download_busy
                or self._application_update_operation_blocked()
            ):
                return False

            release = self._application_update_release
            package_kind = self._application_update_package_kind()
            desktop = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
            cancel_event = threading.Event()
            self._application_update_download_cancel_event = cancel_event
            self._application_update_download_busy = True
            self._application_update_state = "downloading"
            self._application_update_message = (
                f"正在下载{self.applicationUpdatePackageKindText}…"
            )
            self._application_update_download_received = 0
            self._application_update_download_path = ""
            self._set_error_message("")
            self._set_status_message(
                self._application_update_message, self._DEVICE_PAGE_INDEX
            )
            self.applicationUpdateChanged.emit()

            def report_progress(received: int, total: int) -> None:
                if not self._emit_background_result(
                    self._applicationUpdateProgressReady,
                    (int(received), int(total)),
                ):
                    cancel_event.set()

            def run() -> None:
                try:
                    application_update.cleanup_obsolete_update_downloads(
                        self._config_root / "updates",
                        release.version,
                        include_current=False,
                    )
                    result = application_update.download_update_package(
                        release,
                        package_kind,
                        self._config_root / "updates" / release.version.text,
                        cancel_event=cancel_event,
                        progress_callback=report_progress,
                    )
                    if not desktop:
                        raise application_update.ApplicationUpdateError(
                            "desktop_unavailable", "无法找到桌面，更新包保留在下载缓存中，请稍后重试。"
                        )
                    result = application_update.save_update_to_desktop(
                        result, Path(desktop), cancel_event=cancel_event
                    )
                    payload = (True, result)
                except application_update.ApplicationUpdateCancelled as exc:
                    payload = (False, exc)
                except application_update.ApplicationUpdateError as exc:
                    payload = (False, exc)
                except Exception:  # noqa: BLE001 - keep remote errors sanitized
                    payload = (
                        False,
                        application_update.ApplicationUpdateError(
                            "unexpected", "下载更新包失败，请稍后重试。"
                        ),
                    )
                self._emit_background_result(
                    self._applicationUpdateDownloadReady, payload
                )

            try:
                self._start_background_task(run, "remote-mic-update-download")
            except Exception:
                cancel_event.set()
                self._application_update_download_cancel_event = None
                self._application_update_download_busy = False
                self._application_update_state = "download_error"
                self._application_update_message = "无法启动更新下载后台任务。"
                self._set_status_message("")
                self._set_error_message(
                    self._application_update_message, self._DEVICE_PAGE_INDEX
                )
                self.applicationUpdateChanged.emit()
                return False
            return True

        def _on_application_update_progress_ready(self, payload: object) -> None:
            if (
                not self._application_update_download_busy
                or self._application_update_result_discarded()
            ):
                return
            received, total = payload
            if int(total) != self._application_update_package_size:
                return
            self._application_update_download_received = max(
                0, min(int(received), self._application_update_package_size)
            )
            self.applicationUpdateChanged.emit()

        def _on_application_update_download_ready(self, payload: object) -> None:
            if self._application_update_result_discarded():
                return
            succeeded, value = payload
            self._application_update_download_busy = False
            self._application_update_download_cancel_event = None
            if not succeeded:
                if isinstance(
                    value, application_update.ApplicationUpdateCancelled
                ):
                    self._application_update_state = "available"
                    self._application_update_message = "下载已取消，可以稍后重试。"
                    self._set_error_message("")
                    self._set_status_message(
                        self._application_update_message, self._DEVICE_PAGE_INDEX
                    )
                else:
                    self._application_update_state = "download_error"
                    self._application_update_message = str(value)
                    self._set_status_message("")
                    self._set_error_message(
                        f"下载更新失败：{value}", self._DEVICE_PAGE_INDEX
                    )
                self.applicationUpdateChanged.emit()
                return

            result = value
            self._application_update_available = False
            self._application_update_state = "downloaded"
            self._application_update_download_received = (
                self._application_update_package_size
            )
            self._application_update_download_path = str(result.path)
            if result.package_kind is application_update.PackageKind.INSTALLER:
                self._application_update_message = (
                    "更新包已保存到桌面并通过校验。请先完全退出MoteDeck，再在文件夹中"
                    "手动运行安装器；程序不会自动安装。"
                )
            else:
                self._application_update_message = (
                    "便携版已保存到桌面并通过校验。请先完全退出当前版本，再解压到"
                    "新的文件夹使用；程序不会覆盖现有目录。"
                )
            self._set_error_message("")
            self._set_status_message(
                "更新包已保存到桌面并通过校验。", self._DEVICE_PAGE_INDEX
            )
            self.applicationUpdateChanged.emit()

        @Slot(result=bool)
        def cancelApplicationUpdateDownload(self) -> bool:
            cancel_event = self._application_update_download_cancel_event
            if not self._application_update_download_busy or cancel_event is None:
                return False
            cancel_event.set()
            self._application_update_message = "正在取消下载…"
            self.applicationUpdateChanged.emit()
            return True

        @Slot(result=bool)
        def openApplicationUpdateRelease(self) -> bool:
            target = self._application_update_release_url
            result = shell_targets.open_external_target(target)
            self._report_external_target(result, self._DEVICE_PAGE_INDEX)
            return result.outcome is shell_targets.ExternalTargetOutcome.OPENED

        @Slot(result=bool)
        def openDownloadedApplicationUpdate(self) -> bool:
            package_path = (
                Path(self._application_update_download_path)
                if self._application_update_download_path
                else None
            )
            if package_path is None or not package_path.is_file():
                self._application_update_available = (
                    self._application_update_release is not None
                )
                self._application_update_state = "download_error"
                self._application_update_message = (
                    "已下载的更新包不存在，请重新下载。"
                )
                self._application_update_download_received = 0
                self._application_update_download_path = ""
                self._set_status_message("")
                self._set_error_message(
                    self._application_update_message, self._DEVICE_PAGE_INDEX
                )
                self.applicationUpdateChanged.emit()
                return False
            result = shell_targets.open_external_target(str(package_path.parent))
            self._report_external_target(result, self._DEVICE_PAGE_INDEX)
            return result.outcome is shell_targets.ExternalTargetOutcome.OPENED

        @Slot(str)
        def selectButton(self, button_id: str) -> None:
            if button_id == self._selected_button_id:
                return
            self._model.set_selected_button(button_id)
            self._selected_button_id = self._model.selected_button_id()
            self.selectedButtonIdChanged.emit()

        def _report_external_target(self, result, page_index: int) -> None:
            if result.outcome is shell_targets.ExternalTargetOutcome.OPENED:
                self._set_error_message("")
                self._set_status_message(f"已打开：{result.target}", page_index)
            else:
                self._set_status_message("")
                self._set_error_message(
                    f"无法打开 {result.target}（{result.error}）",
                    page_index,
                )

        @Slot()
        def openBluetoothSettings(self) -> None:
            self._report_external_target(
                shell_targets.open_external_target(shell_targets.BLUETOOTH_SETTINGS_URI),
                self._DEVICE_PAGE_INDEX,
            )

        @Slot()
        def openMicrophonePrivacySettings(self) -> None:
            self._report_external_target(
                shell_targets.open_external_target(
                    shell_targets.MICROPHONE_PRIVACY_SETTINGS_URI
                ),
                self._VOICE_PAGE_INDEX,
            )

        @Slot()
        def openSpeechSettings(self) -> None:
            self._report_external_target(
                shell_targets.open_external_target(shell_targets.SPEECH_SETTINGS_URI),
                self._VOICE_PAGE_INDEX,
            )

        @Slot()
        def openSoundSettings(self) -> None:
            self._report_external_target(
                shell_targets.open_external_target(shell_targets.SOUND_SETTINGS_URI),
                self._VOICE_PAGE_INDEX,
            )

        @Slot()
        def openAppsSettings(self) -> None:
            self._report_external_target(
                shell_targets.open_external_target(shell_targets.APPS_SETTINGS_URI),
                self._DEVICE_PAGE_INDEX,
            )

        def _select_and_persist_output_endpoint(
            self,
            name: str,
            host_api: str,
            completion: Optional[Callable[[bool, str], None]] = None,
            *,
            allow_driver_action: bool = False,
        ) -> bool:
            if self.configurationReadOnly:
                if completion is not None:
                    completion(False, self._configuration_error)
                return False
            if (
                _vb_cable_test_active_event.is_set()
                or self._voice_hotkey_busy
                or self._settings_save_busy
                or self._get_bridge_launch_busy()
                or (
                    _driver_action_active_event.is_set()
                    and not allow_driver_action
                )
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                if completion is not None:
                    completion(False, "当前有其它操作正在进行。")
                return False

            current = (
                str(self._config.get("output_endpoint_name", "")),
                str(self._config.get("output_endpoint_host_api", "")),
            )
            requested = (str(name), str(host_api))
            if requested == current:
                if completion is not None:
                    completion(True, "")
                return True

            settled = False
            settled_result = False

            def finish(ok: bool, message: str) -> None:
                nonlocal settled, settled_result
                if (
                    self._application_exit_requested
                    or self._application_exit_confirmed
                    or self._application_exit_intent.is_set()
                ):
                    ok = False
                    message = "程序正在退出，未保存输出端点。"
                if ok:
                    new_config = dict(self._config)
                    new_config["output_endpoint_name"] = requested[0]
                    new_config["output_endpoint_host_api"] = requested[1]
                    try:
                        saved_config = config.save_config_and_load(
                            config.config_path(self._config_root), new_config
                        )
                    except Exception:  # noqa: BLE001 - keep UI text sanitized
                        ok = False
                        message = "输出端点设置无法写入。"
                    else:
                        self._record_saved_output_change(saved_config)
                        self._config = saved_config
                        self._bump_settings_revision()
                        self._request_endpoint_options_refresh()
                        self.outputEndpointPersisted.emit()
                settled = True
                settled_result = bool(ok)
                if completion is not None:
                    completion(bool(ok), str(message))

            accepted = self._request_endpoint_preflight(
                requested[0], requested[1], finish
            )
            return settled_result if settled else accepted

        @Slot(str, str, result=bool)
        def selectAndPersistOutputEndpoint(self, name: str, host_api: str) -> bool:
            """Queue an isolated endpoint preflight and persist on success."""

            return self._select_and_persist_output_endpoint(name, host_api)

        @Slot(int, result=bool)
        def selectAndPersistOutputEndpointIndex(self, index: int) -> bool:
            if not 0 <= index < len(self._endpoint_values):
                self._set_error_message(
                    "输出端点保存失败：所选端点已经不可用。",
                    self._VOICE_PAGE_INDEX,
                )
                self.selectedEndpointIndexChanged.emit()
                return False

            endpoint = self._endpoint_values[index]
            self._set_error_message("")
            self._set_status_message(
                "正在检查输出端点…",
                self._VOICE_PAGE_INDEX,
            )

            def finished(ok: bool, message: str) -> None:
                if not ok:
                    self._set_status_message("")
                    self._set_error_message(
                        "输出端点保存失败："
                        + (message or "所选设备无法打开或设置无法写入。"),
                        self._VOICE_PAGE_INDEX,
                    )
                    self.selectedEndpointIndexChanged.emit()
                    return
                self._set_error_message("")
                self._set_status_message(
                    "输出端点已自动保存；重启遥控器服务后生效。"
                    + self._mapping_save_status_suffix(),
                    self._VOICE_PAGE_INDEX,
                )

            accepted = self._select_and_persist_output_endpoint(
                endpoint.name,
                endpoint.host_api,
                finished,
            )
            if not accepted and not self._endpoint_preflight_busy:
                self.selectedEndpointIndexChanged.emit()
            return accepted

    def _diagnostics_check_to_row(check: "windows_diagnostics.CheckResult") -> dict:
        return {
            "checkId": check.check_id,
            "title": check.title,
            "group": check.group.value,
            "status": check.status.value,
            "detail": check.detail,
            "resultCode": check.result_code,
        }

    class DiagnosticsController(QObject):
        """QML-facing adapter for the "检查与修复" page (XRBM-031). Every
        check runs off the Qt GUI thread (see module docstring); every
        driver-launch/endpoint-select action is a thin wrapper with no
        business logic of its own, matching SettingsController's contract.
        """

        checkResultsChanged = Signal()
        isRefreshingChanged = Signal()
        diagnosticsErrorMessageChanged = Signal()
        driverStatusMessageChanged = Signal()
        driverInfoMessageChanged = Signal()
        driverErrorMessageChanged = Signal()
        driverActionRunningChanged = Signal()
        vbCableTestChanged = Signal()
        vbCableBridgeRecoveryChanged = Signal()
        # Internal only - never connected to from QML. Carries a
        # windows_diagnostics.DiagnosticsReport (or None on an unexpected
        # worker-thread exception) back from the background thread to this
        # object's own (GUI) thread - see module docstring for why a plain
        # Signal(object) connection is sufficient here.
        _diagnosticsReady = Signal(object)
        _vbCableTestReady = Signal(object)
        _detectedEndpointReady = Signal(object)

        def __init__(
            self,
            settings_controller: "SettingsController",
            config_root,
            parent=None,
            *,
            auto_refresh: bool = True,
        ) -> None:
            super().__init__(parent)
            self._settings_controller = settings_controller
            self._config_root = config_root
            self._is_refreshing = False
            self._check_rows: List[dict] = []
            self._diagnostics_error_message = ""
            self._driver_status_message = ""
            self._driver_info_message = ""
            self._driver_error_message = ""
            self._driver_action_running = False
            self._vb_cable_test_running = False
            self._vb_cable_test_status = "idle"
            self._vb_cable_test_message = ""
            self._vb_cable_bridge_recovery_needed = False
            self._bridge_diagnostics_refresh_pending = False
            self._endpoint_diagnostics_refresh_pending = False
            self._last_bridge_connected = bool(
                self._settings_controller.bridgeConnected
            )
            self._diagnosticsReady.connect(self._on_diagnostics_ready)
            self._vbCableTestReady.connect(self._on_vb_cable_test_ready)
            self._detectedEndpointReady.connect(
                self._on_detected_endpoint_ready
            )
            self._settings_controller.endpointOptionsChanged.connect(
                self._invalidate_vb_cable_test_result
            )
            self._settings_controller.selectedEndpointIndexChanged.connect(
                self._invalidate_vb_cable_test_result
            )
            self._settings_controller.outputEndpointPersisted.connect(
                self._on_output_endpoint_persisted
            )
            self._settings_controller.bridgeConnectedChanged.connect(
                self._on_bridge_connected_changed
            )
            self._settings_controller.bridgeRunningChanged.connect(
                self._on_bridge_running_changed
            )
            self._settings_controller.remoteSelectionChanged.connect(
                self._on_remote_selection_changed
            )
            if auto_refresh:
                self.refreshDiagnostics()

        # -- internal helpers -------------------------------------------------

        def _saved_output_endpoint(self):
            saved_config = config.load_config(config.config_path(self._config_root))
            return (
                saved_config.get("output_endpoint_name", ""),
                saved_config.get("output_endpoint_host_api", ""),
            )

        def _on_remote_selection_changed(self) -> None:
            if self._settings_controller.remoteSelectionBusy:
                return
            current = self._settings_controller.activeRemoteKey
            if current == getattr(self, "_last_remote_selection", None):
                return
            self._last_remote_selection = current
            self._check_rows = []
            self.checkResultsChanged.emit()
            if not self._settings_controller.remoteSelectionBusy:
                self.refreshDiagnostics()

        def _set_driver_status(self, text: str) -> None:
            """Genuine, completed success only (e.g. an endpoint was
            actually selected and persisted) - the only message shown in
            the success/green color. See ``_set_driver_info`` for outcomes
            that are merely informational (XRBM-031 RETRY 1 item 7).
            """

            self._driver_status_message = text
            self._driver_info_message = ""
            self._driver_error_message = ""
            self.driverStatusMessageChanged.emit()
            self.driverInfoMessageChanged.emit()
            self.driverErrorMessageChanged.emit()

        def _set_driver_info(self, text: str) -> None:
            """Neutral, informational outcome - never the success/green
            color: a UAC cancellation installed nothing, and a launched
            vendor setup UI is not yet a confirmed install either (XRBM-031
            RETRY 1 item 7 - "still never say installed until endpoint
            recheck"). QML renders this in a neutral tone, distinct from
            both a real success and a real error.
            """

            self._driver_info_message = text
            self._driver_status_message = ""
            self._driver_error_message = ""
            self.driverInfoMessageChanged.emit()
            self.driverStatusMessageChanged.emit()
            self.driverErrorMessageChanged.emit()

        def _set_driver_error(self, text: str) -> None:
            self._driver_error_message = text
            self._driver_status_message = ""
            self._driver_info_message = ""
            self.driverErrorMessageChanged.emit()
            self.driverStatusMessageChanged.emit()
            self.driverInfoMessageChanged.emit()

        def _set_driver_action_running(self, value: bool) -> None:
            value = bool(value)
            if value:
                _driver_action_active_event.set()
            else:
                _driver_action_active_event.clear()
            if value == self._driver_action_running:
                if not value:
                    self._settings_controller._schedule_application_exit_poll()
                return
            self._driver_action_running = value
            self.driverActionRunningChanged.emit()
            if not value:
                self._settings_controller._schedule_application_exit_poll()

        def _set_vb_cable_test_state(
            self, status: str, message: str, *, running: bool
        ) -> None:
            self._vb_cable_test_status = status
            self._vb_cable_test_message = message
            self._vb_cable_test_running = running
            self.vbCableTestChanged.emit()
            if not running and self._bridge_diagnostics_refresh_pending:
                QTimer.singleShot(
                    0, self._try_pending_bridge_diagnostics_refresh
                )

        def _set_vb_cable_bridge_recovery_needed(self, value: bool) -> None:
            value = bool(value)
            if value == self._vb_cable_bridge_recovery_needed:
                return
            self._vb_cable_bridge_recovery_needed = value
            self.vbCableBridgeRecoveryChanged.emit()

        def _invalidate_vb_cable_test_result(self) -> None:
            if self._vb_cable_test_running:
                return
            if self._vb_cable_test_status != "idle" or self._vb_cable_test_message:
                self._set_vb_cable_test_state("idle", "", running=False)

        def _on_bridge_connected_changed(self) -> None:
            connected = bool(self._settings_controller.bridgeConnected)
            transitioned_to_connected = connected and not self._last_bridge_connected
            self._last_bridge_connected = connected
            if not connected:
                self._bridge_diagnostics_refresh_pending = False
                return
            if not transitioned_to_connected:
                return
            self._bridge_diagnostics_refresh_pending = True
            QTimer.singleShot(0, self._try_pending_bridge_diagnostics_refresh)

        def _on_bridge_running_changed(self) -> None:
            if (
                not self._settings_controller.bridgeRunning
                or not self._vb_cable_bridge_recovery_needed
            ):
                return
            self._set_vb_cable_bridge_recovery_needed(False)
            self._set_vb_cable_test_state(
                "idle",
                "遥控器服务已重新启动；请重新测试声音通道",
                running=False,
            )

        def _on_output_endpoint_persisted(self) -> None:
            self._endpoint_diagnostics_refresh_pending = True
            QTimer.singleShot(0, self._try_pending_endpoint_diagnostics_refresh)

        def _try_pending_endpoint_diagnostics_refresh(self) -> None:
            if not self._endpoint_diagnostics_refresh_pending:
                return
            if (
                self._is_refreshing
                or self._vb_cable_test_running
                or self._driver_action_running
                or _diagnostics_shutdown_event.is_set()
            ):
                return
            self._endpoint_diagnostics_refresh_pending = False
            self.refreshDiagnostics()

        def _try_pending_bridge_diagnostics_refresh(self) -> None:
            if not self._bridge_diagnostics_refresh_pending:
                return
            if not self._settings_controller.bridgeConnected:
                self._bridge_diagnostics_refresh_pending = False
                return
            if (
                self._is_refreshing
                or self._vb_cable_test_running
                or _diagnostics_shutdown_event.is_set()
            ):
                return
            self._bridge_diagnostics_refresh_pending = False
            self.refreshDiagnostics()

        def _on_diagnostics_ready(self, report) -> None:
            """Delivered (cross-thread) once ``run_diagnostics()`` returns -
            or, if the background thread's own call raised something
            ``windows_diagnostics.run_diagnostics()``'s own per-check
            isolation did not anticipate (should be exceedingly rare now
            that every individual check is isolated - see
            ``windows_diagnostics._isolated()`` - but still possible, e.g.
            if constructing the report itself somehow failed), ``None``.

            XRBM-031 RETRY 1 item 2: a ``None`` report must never leave the
            PREVIOUS run's rows on screen looking current - stale green/red
            rows next to a page that silently failed to refresh would be
            actively misleading. The rows are cleared and a page-level
            error is shown instead, prominently, not only as a driver-card
            message below the fold.
            """

            if (getattr(self, "_diagnostics_selected_remote", self._settings_controller.activeRemoteKey)
                    != self._settings_controller.activeRemoteKey):
                self._check_rows = []
                self._is_refreshing = False
                self.checkResultsChanged.emit()
                self.isRefreshingChanged.emit()
                QTimer.singleShot(0, self.refreshDiagnostics)
                return
            if not self._endpoint_diagnostics_refresh_pending:
                if report is not None:
                    self._check_rows = [
                        _diagnostics_check_to_row(c) for c in report.checks
                    ]
                    self._diagnostics_error_message = ""
                else:
                    self._check_rows = []
                    self._diagnostics_error_message = "检测失败；请重新检测"
            self._is_refreshing = False
            self.checkResultsChanged.emit()
            self.diagnosticsErrorMessageChanged.emit()
            self.isRefreshingChanged.emit()
            if self._bridge_diagnostics_refresh_pending:
                QTimer.singleShot(
                    0, self._try_pending_bridge_diagnostics_refresh
                )
            if self._endpoint_diagnostics_refresh_pending:
                QTimer.singleShot(
                    0, self._try_pending_endpoint_diagnostics_refresh
                )

        def _on_vb_cable_test_ready(self, result) -> None:
            if result is None:
                self._set_vb_cable_bridge_recovery_needed(False)
                self._set_vb_cable_test_state(
                    "fail",
                    "通道测试失败；未得到可信结果",
                    running=False,
                )
                return
            if isinstance(result, _VbCableTestWorkflowResult):
                if result.stop_error:
                    self._set_vb_cable_bridge_recovery_needed(False)
                    self._set_vb_cable_test_state(
                        "fail",
                        result.stop_error,
                        running=False,
                    )
                    self._settings_controller._refresh_bridge_status()
                    return
                if result.restart_skipped_for_exit:
                    self._set_vb_cable_bridge_recovery_needed(False)
                    self._set_vb_cable_test_state(
                        "unsupported",
                        "完全退出中，声音通道测试已停止，服务不再恢复",
                        running=False,
                    )
                    self._settings_controller._refresh_bridge_status()
                    return
                loopback_result = result.loopback_result
                restart_result = result.restart_result
                restart_ok = bool(
                    restart_result is not None
                    and restart_result.outcome
                    in {
                        bridge_launcher.LaunchOutcome.STARTED,
                        bridge_launcher.LaunchOutcome.ALREADY_RUNNING,
                    }
                )
                if result.bridge_was_running and not restart_ok:
                    self._set_vb_cable_bridge_recovery_needed(True)
                    recovery_text = (
                        settings_ui.describe_launch_result(restart_result)
                        if restart_result is not None
                        else "未得到启动结果"
                    )
                    loopback_text = (
                        loopback_result.detail
                        if loopback_result is not None
                        else "声音通道测试未得到可信结果"
                    )
                    self._set_vb_cable_test_state(
                        "fail",
                        f"{loopback_text} 遥控器服务未能自动恢复：{recovery_text}",
                        running=False,
                    )
                    self._settings_controller._refresh_bridge_status()
                    return
                self._set_vb_cable_bridge_recovery_needed(False)
                if loopback_result is None:
                    self._set_vb_cable_test_state(
                        "fail",
                        "声音通道测试失败；未得到可信结果"
                        + (
                            "；服务已自动恢复"
                            if result.bridge_was_running else ""
                        ),
                        running=False,
                    )
                else:
                    suffix = (
                        "；服务已自动恢复"
                        if result.bridge_was_running else ""
                    )
                    self._set_vb_cable_test_state(
                        loopback_result.status.value,
                        loopback_result.detail + suffix,
                        running=False,
                    )
                self._settings_controller._refresh_bridge_status()
                return
            self._set_vb_cable_bridge_recovery_needed(False)
            self._set_vb_cable_test_state(
                result.status.value,
                result.detail,
                running=False,
            )

        def _log_sound_channel_diagnostic(self, message: str, *args) -> None:
            try:
                if self._settings_controller._diagnostic_trace_enabled:
                    logging_setup.get_logger(self._config_root).info(
                        "sound channel diagnostic: " + message, *args
                    )
            except Exception:
                pass  # Logs must not stop the test or its service restoration.

        # -- properties ---------------------------------------------------

        def _get_check_results(self) -> List[dict]:
            return list(self._check_rows)

        checkResults = Property(list, _get_check_results, notify=checkResultsChanged)

        def _get_is_refreshing(self) -> bool:
            return self._is_refreshing

        isRefreshing = Property(bool, _get_is_refreshing, notify=isRefreshingChanged)

        def _get_diagnostics_error_message(self) -> str:
            return self._diagnostics_error_message

        diagnosticsErrorMessage = Property(
            str, _get_diagnostics_error_message, notify=diagnosticsErrorMessageChanged
        )

        def _get_driver_status_message(self) -> str:
            return self._driver_status_message

        driverStatusMessage = Property(
            str, _get_driver_status_message, notify=driverStatusMessageChanged
        )

        def _get_driver_info_message(self) -> str:
            return self._driver_info_message

        driverInfoMessage = Property(
            str, _get_driver_info_message, notify=driverInfoMessageChanged
        )

        def _get_driver_error_message(self) -> str:
            return self._driver_error_message

        driverErrorMessage = Property(
            str, _get_driver_error_message, notify=driverErrorMessageChanged
        )

        driverActionRunning = Property(
            bool,
            lambda self: self._driver_action_running,
            notify=driverActionRunningChanged,
        )

        def _get_vb_cable_test_running(self) -> bool:
            return self._vb_cable_test_running

        vbCableTestRunning = Property(
            bool, _get_vb_cable_test_running, notify=vbCableTestChanged
        )

        def _get_vb_cable_test_status(self) -> str:
            return self._vb_cable_test_status

        vbCableTestStatus = Property(
            str, _get_vb_cable_test_status, notify=vbCableTestChanged
        )

        def _get_vb_cable_test_message(self) -> str:
            return self._vb_cable_test_message

        vbCableTestMessage = Property(
            str, _get_vb_cable_test_message, notify=vbCableTestChanged
        )

        def _get_vb_cable_bridge_recovery_needed(self) -> bool:
            return self._vb_cable_bridge_recovery_needed

        vbCableBridgeRecoveryNeeded = Property(
            bool,
            _get_vb_cable_bridge_recovery_needed,
            notify=vbCableBridgeRecoveryChanged,
        )

        # -- slots ----------------------------------------------------------

        def _emit_diagnostics_ready(self, report) -> None:
            """Thin wrapper around emitting ``_diagnosticsReady``, isolated
            into its own method purely so a test can inject a failure here
            (simulating a receiver/Qt runtime that is already mid-teardown)
            without needing to monkeypatch PySide6's own ``Signal``
            machinery directly. Never called if
            ``_diagnostics_shutdown_event`` is already set when the worker
            checks it - see ``refreshDiagnostics()``'s ``_run_in_background``
            below.
            """

            self._diagnosticsReady.emit(report)

        def _emit_vb_cable_test_ready(self, result) -> None:
            self._vbCableTestReady.emit(result)

        @Slot()
        def startInitialDiagnostics(self) -> None:
            if self._settings_controller.configurationReadOnly:
                return
            if self._check_rows or self._is_refreshing:
                return
            self.refreshDiagnostics()

        @Slot()
        def refreshDiagnostics(self) -> None:
            """Runs every check on a background thread. Repeated clicks
            while a check is already running are ignored outright (the
            guard below), so this can never start two overlapping workers -
            see the module docstring for the full thread-safety/lifecycle
            contract.

            Also refuses to start once process shutdown has begun
            (``_diagnostics_shutdown_event`` set - XRBM-031 RETRY 2): there
            would be nothing left alive to usefully receive this worker's
            result anyway, and starting one so late would only add another
            thread ``_shutdown_qt_settings_app_at_exit()``'s bounded join
            might not have time for.
            """

            if self._settings_controller.configurationReadOnly:
                self._diagnostics_error_message = "配置读取异常，请先导出日志排查；本次未启动设备检查。"
                self.diagnosticsErrorMessageChanged.emit()
                return
            if self._is_refreshing or self._vb_cable_test_running:
                return
            if _diagnostics_shutdown_event.is_set():
                return
            self._invalidate_vb_cable_test_result()
            self._is_refreshing = True
            self.isRefreshingChanged.emit()
            saved_name, saved_host_api = self._saved_output_endpoint()

            selected_remote = self._settings_controller.activeRemoteKey
            self._diagnostics_selected_remote = selected_remote

            def _run_in_background() -> None:
                try:
                    try:
                        report = windows_diagnostics.run_diagnostics(
                            saved_output_name=saved_name,
                            saved_output_host_api=saved_host_api,
                            selected_key=selected_remote,
                            # XRBM-035: the SAME event this module's
                            # shutdown helpers set - a discovery attempt
                            # still in flight when the settings window
                            # starts closing is now actually cancelled at
                            # the WinRT level (see
                            # windows_diagnostics._discover_candidates_
                            # cancellable()), not merely abandoned to run
                            # concurrently with interpreter shutdown.
                            cancel_event=_diagnostics_shutdown_event,
                        )
                    except Exception:  # noqa: BLE001 - never crash the worker thread
                        report = None
                    if _diagnostics_shutdown_event.is_set():
                        # Process shutdown began while this check was
                        # running (XRBM-031 RETRY 2) - the
                        # DiagnosticsController/Qt runtime this would emit
                        # into may already be mid-teardown by now. Skip the
                        # emit entirely rather than race it.
                        return
                    try:
                        self._emit_diagnostics_ready(report)
                    except Exception:  # noqa: BLE001 - a receiver/Qt runtime
                        # that is ALREADY tearing down despite the check
                        # just above (an inherent, disclosed narrowing-not-
                        # elimination of the race - see module docstring)
                        # must never crash this background thread; the
                        # registry cleanup below still runs regardless.
                        pass
                finally:
                    _forget_diagnostics_thread(threading.current_thread())

            thread = threading.Thread(target=_run_in_background, daemon=True)
            _remember_diagnostics_thread(thread)
            thread.start()

        @Slot()
        def testVbCableChannel(self) -> None:
            """Runs the active CABLE Input -> CABLE Output test on demand."""

            self._start_vb_cable_test(allow_bridge_restart=False)

        @Slot()
        def testVbCableChannelWithBridgeRestart(self) -> None:
            """Temporarily stop a running bridge, test, then restore it."""

            self._start_vb_cable_test(allow_bridge_restart=True)

        def _start_vb_cable_test(self, *, allow_bridge_restart: bool) -> None:

            if self._settings_controller.configurationReadOnly:
                self._set_vb_cable_test_state(
                    "fail", "配置读取异常，本次未启动声音通道测试。", running=False,
                )
                return
            if self._vb_cable_test_running or self._is_refreshing:
                return
            if _diagnostics_shutdown_event.is_set():
                return
            if (
                self._settings_controller._application_exit_requested
                or self._settings_controller._application_exit_confirmed
                or self._settings_controller._application_exit_intent.is_set()
            ):
                return
            if self._driver_action_running:
                self._set_vb_cable_test_state(
                    "fail",
                    "输出端点正在处理；完成后再测试声音通道",
                    running=False,
                )
                return
            if self._settings_controller._get_voice_hotkey_busy():
                self._set_vb_cable_test_state(
                    "fail",
                    "语音快捷键正在处理；完成后再测试声音通道",
                    running=False,
                )
                return
            if self._settings_controller._get_input_capture_in_use():
                self._set_vb_cable_test_state(
                    "fail",
                    "按键录入或检测正在进行；结束后再测试声音通道",
                    running=False,
                )
                return
            if self._settings_controller._endpoint_preflight_busy:
                self._set_vb_cable_test_state(
                    "fail",
                    "输出端点正在检查；完成后再测试声音通道",
                    running=False,
                )
                return
            if self._settings_controller._get_bridge_launch_busy():
                self._set_vb_cable_test_state(
                    "fail",
                    "服务正在启动；停止后再测试声音通道",
                    running=False,
                )
                return
            bridge_running = self._settings_controller._refresh_bridge_status()
            if bridge_running is None:
                self._set_vb_cable_test_state(
                    "unsupported",
                    "无法确认服务状态；为避免混入真实语音，未启动测试",
                    running=False,
                )
                return
            if bridge_running:
                if not allow_bridge_restart:
                    self._set_vb_cable_test_state(
                        "fail",
                        "服务正在运行；需先确认临时停止并自动恢复",
                        running=False,
                    )
                    return

            saved_name, saved_host_api = self._saved_output_endpoint()
            self._log_sound_channel_diagnostic(
                "phase=start bridge_was_running=%s restore_requested=%s",
                bool(bridge_running), bool(bridge_running and allow_bridge_restart),
            )
            _vb_cable_test_active_event.set()
            self._set_vb_cable_bridge_recovery_needed(False)
            self._set_vb_cable_test_state(
                "running",
                (
                    "正在停止服务；随后测试并自动恢复"
                    if bridge_running
                    else "正在发送测试信号；检查 CABLE Output"
                ),
                running=True,
            )

            def _run_in_background() -> None:
                try:
                    stop_error = ""
                    loopback_result = None
                    restart_result = None
                    bridge_stopped = False
                    restart_skipped_for_exit = False
                    if bridge_running:
                        try:
                            stop_result = bridge_control_windows.request_bridge_exit(reason="audio_test")
                        except Exception:  # noqa: BLE001 - keep the test retryable
                            stop_error = (
                                "无法临时停止遥控器服务；未运行声音通道测试"
                            )
                        else:
                            if not stop_result.stopped:
                                stop_error = stop_result.error or (
                                    "未能临时停止服务；未运行声音通道测试"
                                )
                            else:
                                bridge_stopped = True
                    try:
                        if (
                            not stop_error
                            and not _diagnostics_shutdown_event.is_set()
                            and not self._settings_controller._application_exit_intent.is_set()
                        ):
                            loopback_result = (
                                windows_diagnostics.check_vb_cable_loopback_isolated(
                                    saved_name,
                                    saved_host_api,
                                    cancel_event=_AnyEvent(
                                        _diagnostics_shutdown_event,
                                        self._settings_controller._application_exit_intent,
                                    ),
                                )
                            )
                    except Exception:  # noqa: BLE001 - never crash the worker thread
                        loopback_result = None
                    finally:
                        if bridge_stopped and (
                            not self._settings_controller._application_exit_intent.is_set()
                            and not _diagnostics_shutdown_event.is_set()
                        ):
                            try:
                                restart_result = (
                                    bridge_launcher.launch_in_process_bridge()
                                )
                            except bridge_launcher.BridgeLaunchConfigurationError as exc:
                                restart_result = bridge_launcher.LaunchResult(
                                    outcome=bridge_launcher.LaunchOutcome.LAUNCH_FAILED,
                                    command=(),
                                    error=type(exc).__name__,
                                )
                            except Exception as exc:  # noqa: BLE001 - report recovery failure
                                restart_result = bridge_launcher.LaunchResult(
                                    outcome=bridge_launcher.LaunchOutcome.LAUNCH_FAILED,
                                    command=(),
                                    error=type(exc).__name__,
                                )
                        elif bridge_stopped:
                            restart_skipped_for_exit = True
                    result = _VbCableTestWorkflowResult(
                        loopback_result=loopback_result,
                        bridge_was_running=bool(bridge_running),
                        stop_error=stop_error,
                        restart_result=restart_result,
                        restart_skipped_for_exit=restart_skipped_for_exit,
                    )
                    self._log_sound_channel_diagnostic(
                        "phase=finished bridge_stopped=%s stop_failed=%s "
                        "loopback_status=%s restore_outcome=%s exit_cancelled_restore=%s",
                        bridge_stopped, bool(stop_error),
                        getattr(getattr(loopback_result, 'status', None), 'value', 'unknown'),
                        getattr(getattr(restart_result, 'outcome', None), 'value', 'not_requested'),
                        restart_skipped_for_exit,
                    )
                    _vb_cable_test_active_event.clear()
                    if _diagnostics_shutdown_event.is_set():
                        return
                    try:
                        self._emit_vb_cable_test_ready(result)
                    except Exception:  # noqa: BLE001 - Qt may already be tearing down
                        pass
                finally:
                    _vb_cable_test_active_event.clear()
                    _forget_diagnostics_thread(threading.current_thread())

            thread = threading.Thread(target=_run_in_background, daemon=True)
            _remember_diagnostics_thread(thread)
            try:
                thread.start()
            except Exception:
                _forget_diagnostics_thread(thread)
                _vb_cable_test_active_event.clear()
                self._set_vb_cable_test_state(
                    "fail",
                    "无法启动 VB-CABLE 通道测试后台任务。",
                    running=False,
                )

        @Slot(result=bool)
        def selectDetectedCableInputAsOutput(self) -> bool:
            """Detect and save CABLE Input without blocking the Qt thread."""

            if (
                self._vb_cable_test_running
                or self._driver_action_running
                or self._settings_controller._settings_save_busy
                or self._settings_controller._endpoint_preflight_busy
                or self._settings_controller._get_bridge_launch_busy()
                or self._settings_controller._get_voice_hotkey_busy()
                or _diagnostics_shutdown_event.is_set()
                or self._settings_controller._application_exit_requested
                or self._settings_controller._application_exit_intent.is_set()
                or self._settings_controller._application_exit_confirmed
            ):
                return False
            self._invalidate_vb_cable_test_result()
            self._set_driver_action_running(True)
            self._set_driver_info("正在检测并检查 CABLE Input…")

            def run() -> None:
                try:
                    endpoints = audio_output.enumerate_output_endpoints()
                    matches = [
                        endpoint
                        for endpoint in endpoints
                        if audio_output.is_cable_input_endpoint(endpoint.name)
                    ]
                    if not matches:
                        payload = (
                            None,
                            "未找到 CABLE Input；请安装 VB-CABLE 并重启电脑",
                        )
                    else:
                        try:
                            endpoint = audio_output.select_preferred_output_endpoint(
                                matches
                            )
                        except audio_output.AudioOutputUnavailableError:
                            payload = (
                                None,
                                f"找到 {len(matches)} 个 CABLE Input；"
                                "请在语音页的“输出端点”中选择",
                            )
                        else:
                            payload = (endpoint, "")
                except audio_output.AudioOutputUnavailableError:
                    payload = (None, "无法检测播放端点")
                except Exception:  # noqa: BLE001 - keep UI text sanitized
                    payload = (None, "播放端点检测失败")
                emitted = self._settings_controller._emit_background_result(
                    self._detectedEndpointReady,
                    payload,
                )
                if not emitted:
                    _driver_action_active_event.clear()

            try:
                self._settings_controller._start_background_task(
                    run, "detect-cable-output-endpoint"
                )
            except Exception:
                self._set_driver_action_running(False)
                self._set_driver_error("无法启动播放端点检测")
                return False
            return True

        def _on_detected_endpoint_ready(self, payload: object) -> None:
            if (
                _diagnostics_shutdown_event.is_set()
                or self._settings_controller._application_exit_intent.is_set()
                or self._settings_controller._application_exit_confirmed
            ):
                self._set_driver_action_running(False)
                return
            endpoint, message = payload
            if endpoint is None:
                self._set_driver_action_running(False)
                self._set_driver_error(str(message))
                return

            def finished(ok: bool, error: str) -> None:
                self._set_driver_action_running(False)
                if not ok:
                    self._set_driver_error(
                        "输出端点保存失败；"
                        + (error or "请在语音页重新选择")
                    )
                    return
                self._set_vb_cable_test_state("idle", "", running=False)
                self._set_driver_status(f"已保存输出端点：{endpoint.name}")

            try:
                accepted = self._settings_controller._select_and_persist_output_endpoint(
                    endpoint.name,
                    endpoint.host_api,
                    finished,
                    allow_driver_action=True,
                )
            except Exception:  # noqa: BLE001 - keep the async slot retryable
                accepted = False
            if not accepted and self._driver_action_running:
                self._set_driver_action_running(False)
                self._set_driver_error(
                    "输出端点保存失败；请在语音页重新选择"
                )

        @Slot()
        def launchVbCableSetup(self) -> None:
            """Launches the bundled VB-CABLE vendor setup UI with UAC. Only
            ever reached from a slot the QML page calls after its OWN
            explicit confirmation dialog (see DiagnosticsPage.qml) - never
            automatically on page load/refresh. Never reports installation
            as successful merely because the process launched; see
            vb_cable_bundle.py's module docstring for the full contract.
            """

            self._set_vb_cable_test_state("idle", "", running=False)
            try:
                vb_cable_bundle.prepare_and_launch_vendor_setup()
            except vb_cable_bundle.BundleNotFoundError as exc:
                self._set_driver_error(f"未找到随包的 VB-CABLE 安装包：{exc}")
            except vb_cable_bundle.UacCancelledError as exc:
                # Neutral/informational, never the success color (XRBM-031
                # RETRY 1 item 7): nothing was installed.
                self._set_driver_info(str(exc))
            except vb_cable_bundle.VbCableBundleError as exc:
                self._set_driver_error(f"启动 VB-CABLE 安装程序失败：{exc}")
            else:
                # Also neutral/informational, not the success color: only
                # that the vendor UI was launched, never a confirmed
                # install - that is only ever established later, by a
                # diagnostics recheck finding both endpoints present.
                self._set_driver_info(
                    "已启动 VB-CABLE 安装程序；完成安装并重启电脑后，请重新检查"
                )

    _qt_classes_cache = {
        # Keep the established cache key for tests/callers, but construct the
        # QWidget-capable application required by embedded element navigation.
        "QGuiApplication": QApplication,
        "QIcon": QIcon,
        "QQmlApplicationEngine": QQmlApplicationEngine,
        "QQuickStyle": QQuickStyle,
        "QUrl": QUrl,
        "Qt": Qt,
        "qmlRegisterSingletonInstance": qmlRegisterSingletonInstance,
        "ButtonMappingModel": ButtonMappingModel,
        "SettingsController": SettingsController,
        "DiagnosticsController": DiagnosticsController,
    }
    return _qt_classes_cache


# QML module/type names for the two QML singletons below - never
# "controller"/"model" (see run_settings_window()'s registration call for
# why).
_QML_MODULE_URI = "OvbRc003Settings"
_QML_CONTROLLER_TYPE_NAME = "SettingsController"
_QML_MAPPING_MODEL_TYPE_NAME = "ButtonMappingModel"
_QML_DIAGNOSTICS_TYPE_NAME = "DiagnosticsController"  # XRBM-031

_renderer_preference_applied = False


def _prefer_opengl_renderer() -> None:
    """Windows 的 D3D11 flip-model 交换链在拉伸窗口时会先露出纯黑的新区域
    （GDI 擦除/窗口类背景画刷都无效），OpenGL 后端不会。只在确实能创建
    OpenGL 3.2 上下文时才切换，避免在没有桌面 OpenGL 的机器（远程桌面、
    部分虚拟机）上得到一扇黑窗。必须在第一个 QQuickWindow 之前调用。
    """

    global _renderer_preference_applied
    if _renderer_preference_applied or sys.platform != "win32":
        return
    _renderer_preference_applied = True
    try:
        from PySide6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
        from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
    except ImportError:
        return
    try:
        surface_format = QSurfaceFormat()
        surface_format.setVersion(3, 2)
        surface_format.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        context = QOpenGLContext()
        context.setFormat(surface_format)
        if not context.create():
            return
        surface = QOffscreenSurface()
        surface.setFormat(context.format())
        surface.create()
        if not surface.isValid() or not context.makeCurrent(surface):
            return
        context.doneCurrent()
        QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.OpenGL)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return


def run_settings_window(
    *, start_hidden: bool = False, start_bridge: bool = False
) -> int:
    """Builds and runs the Qt Quick/QML settings window. Blocks until the
    window is closed (``QGuiApplication.exec()``), then returns its exit
    code. Raises ``QtUnavailableError`` (via ``_load_qt_classes()``) if
    PySide6-Essentials is not installed, or if ``main.qml`` fails to load at
    all (e.g. a corrupted/incomplete frozen build missing its bundled qml/
    directory) - never silently opens a blank/broken window.
    """

    classes = _load_qt_classes()
    QGuiApplication = classes["QGuiApplication"]
    QIcon = classes["QIcon"]
    QQmlApplicationEngine = classes["QQmlApplicationEngine"]
    QQuickStyle = classes["QQuickStyle"]
    QUrl = classes["QUrl"]
    qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
    ButtonMappingModel = classes["ButtonMappingModel"]
    SettingsController = classes["SettingsController"]
    DiagnosticsController = classes["DiagnosticsController"]

    # Windows 11 Fluent look (In-scope item 7/DESIGN_VARIANCE): QQuickStyle
    # must be set before the QGuiApplication/engine is constructed. Qt 6.7+
    # ships "FluentWinUI3" specifically to emulate the Windows 11 Fluent
    # design language for Qt Quick Controls; it renders (as a software
    # fallback, not native WinUI3) on non-Windows hosts too, which is what
    # this candidate's offscreen render/screenshot step below relies on.
    QQuickStyle.setStyle("FluentWinUI3")

    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    _apply_application_identity(app)
    _apply_ui_font_fallbacks(app)
    _prefer_opengl_renderer()
    set_quit_on_last_window_closed = getattr(
        app, "setQuitOnLastWindowClosed", None
    )
    if callable(set_quit_on_last_window_closed):
        set_quit_on_last_window_closed(False)

    model = ButtonMappingModel()
    controller = SettingsController(
        model,
        start_hidden=start_hidden,
        start_bridge=start_bridge,
    )
    _connect_application_exit(app, controller)

    root_window = None

    def update_application_icon() -> None:
        path = resources.find_app_icon(controller._tray_icon_state())
        if path is not None:
            _apply_application_icon(app, root_window, QIcon, path)

    update_application_icon()
    controller.trayStateChanged.connect(update_application_icon)
    diagnostics_controller = DiagnosticsController(
        controller,
        config.config_root(),
        auto_refresh=False,
    )

    # SettingsController has already started its endpoint/program status
    # workers here. Diagnostics are intentionally deferred until QML reports
    # its first frame, but may also exist before app.exec() returns. Every exit
    # path from this point therefore shares the same cleanup chain, including
    # engine.load() failures and an empty rootObjects() result.
    try:
        # Exposed to QML as SINGLETONS (resolved through the type/import
        # system at document-compile time), not as engine.rootContext()
        # context properties: a root-context property is resolved
        # dynamically through each QML object's context chain, and -
        # empirically, reproduced with a minimal isolated repro during this
        # task - a context property can observe a transient/incorrectly-null
        # value the first time it is read from a binding evaluated during a
        # child component's own construction (e.g. a ListView's
        # currentIndex binding, or any property evaluated inside a
        # ScrollView's deferred content), before every containing component
        # has finished having its own externally-supplied properties
        # assigned. A qmlRegisterSingletonInstance()-registered type has no
        # such hazard: every file that `import`s this module gets the exact
        # same already-fully-constructed instance immediately, with no
        # per-context propagation/ordering involved at all.
        qmlRegisterSingletonInstance(
            SettingsController,
            _QML_MODULE_URI,
            1,
            0,
            _QML_CONTROLLER_TYPE_NAME,
            controller,
        )
        qmlRegisterSingletonInstance(
            ButtonMappingModel,
            _QML_MODULE_URI,
            1,
            0,
            _QML_MAPPING_MODEL_TYPE_NAME,
            model,
        )
        qmlRegisterSingletonInstance(
            DiagnosticsController,
            _QML_MODULE_URI,
            1,
            0,
            _QML_DIAGNOSTICS_TYPE_NAME,
            diagnostics_controller,
        )

        engine = QQmlApplicationEngine()
        qml_dir = _qml_directory()
        engine.addImportPath(str(qml_dir))

        main_qml = qml_dir / "main.qml"
        engine.load(QUrl.fromLocalFile(str(main_qml)))
        root_objects = engine.rootObjects()
        if not root_objects:
            raise QtUnavailableError(f"无法加载 QML 设置界面：{main_qml} 未能成功加载。")
        root_window = root_objects[0]
        update_application_icon()
        window_chrome_windows.apply_settings_window_chrome(root_window)
        window_chrome_windows.install_frameless_window(root_window)

        # The real QML window and full-exit signal are ready here. Publish the
        # capability before optional element navigation startup, whose bounded
        # native hook waits must not make another version misclassify this copy.
        settings_window_hwnd = _settings_window_handle(root_window)
        controller._settings_window_hwnd = settings_window_hwnd
        single_instance.register_settings_window_restore_event(settings_window_hwnd)
        _mark_settings_window_for_activation(
            root_window,
            hwnd=settings_window_hwnd,
        )

        if (
            sys.platform == "win32"
            and os.environ.get("RC003_DISABLE_LIVE_INPUT") != "1"
        ):
            try:
                navigation_runtime = (
                    element_navigation_runtime.start_embedded_element_navigation(
                        app
                    )
                )
            except Exception as exc:
                logging_setup.get_logger(config.config_root()).warning(
                    "element navigation unavailable in main process: %s",
                    type(exc).__name__,
                )
            else:
                element_navigation_control_windows.bind_embedded_element_navigation(
                    navigation_runtime
                )

        return app.exec()
    finally:
        try:
            single_instance.release_settings_window_restore_event(
                controller._settings_window_hwnd
            )
        except Exception:
            # Notification cleanup must not skip input/audio shutdown.
            pass
        try:
            background_settled = controller.shutdownForProcessExit()
            if not background_settled:
                logging_setup.get_logger(config.config_root()).error(
                    "settings background tasks did not settle before desktop shutdown"
                )
        finally:
            try:
                bridge_stopped = bridge_launcher.stop_in_process_bridge(reason="process_exit")
                if bridge_stopped is False:
                    logging_setup.get_logger(config.config_root()).error(
                        "in-process bridge did not stop before desktop shutdown"
                    )
            finally:
                try:
                    element_navigation_control_windows.shutdown_element_navigation()
                finally:
                    try:
                        # XRBM-035: called HERE, synchronously - whether
                        # app.exec() returned normally, engine.load() raised,
                        # rootObjects() was empty, input cleanup raised, or
                        # anything else in this block raised.
                        _shutdown_diagnostics_workers()
                    finally:
                        audio_playback.cleanup_retained_portaudio_test_resources(
                            blocking=False
                        )
