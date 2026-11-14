"""SettingsController (the QObject bridging every QML page to
settings_ui's pure validation/save logic - this module only
bridges, it never duplicates that logic). Mechanically split
from qt_settings_app._load_qt_classes(); only imported lazily
from there, so importing qt_settings_app never requires PySide6."""

from __future__ import annotations

from . import __version__
from . import app_icons
from . import app_profiles
from . import application_update
from . import audio_output
from . import bridge_control_windows
from . import bridge_launcher
from . import bridge_runtime_status
from . import config
from . import dev_session
from . import device_catalog
from . import element_navigation_control_windows
from . import frida_compat
from . import hid_elevation_windows
from . import hotkey
from . import hotkey_capture_windows
from . import key_detection_bridge
from . import key_mapping
from . import local_asr
from . import local_microphone_windows
from . import log_export
from . import logging_setup
from . import openmexia_client
from . import product_identity
from . import quick_setup_windows
from . import raw_input_windows
from . import remote_layout
from . import remote_selection
from . import resources
from . import running_apps_windows
from . import settings_ui
from . import shell_targets
from . import single_instance
from . import startup_windows
from . import tsf_input_windows
from . import vokie_bridge
from . import windows_diagnostics
from pathlib import Path
import json
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from .qt_settings_qml_tokens import (
    Property,
    QApplication,
    QEvent,
    QObject,
    QStandardPaths,
    QTimer,
    QUrl,
    Qt,
    Signal,
    Slot,
    _QT_MODIFIER_FLAG_TOKENS,
    _QT_MODIFIER_KEY_TOKENS,
    _qt_hotkey_token,
)
from .qt_settings_app import (
    APPLICATION_UPDATE_CHECK_ENABLED,
    CHATTERFLY_VOICE_KEY_NAMES,
    CHATTERFLY_WEBSITE_URL,
    DOUBAO_VOICE_KEY_NAME,
    DOUBAO_WEBSITE_URL,
    HELP_CENTER_URL,
    OPENMEXIA_DOWNLOAD_URL,
    OPENMEXIA_WEBSITE_URL,
    VOICE_KEY_TUTORIAL_URL,
    VOKIE_PLUGIN_URL,
    VOKIE_WEBSITE_URL,
    WETYPE_VOICE_KEY_NAME,
    WETYPE_WEBSITE_URL,
    _APPLICATION_EXIT_POLL_INTERVAL_MS,
    _APPLICATION_EXIT_SAVE_WAIT_TIMEOUT_SECONDS,
    _APPLICATION_EXIT_WAIT_TIMEOUT_SECONDS,
    _AnyEvent,
    _BRIDGE_RECONNECT_BUSY_TIMEOUT_MS,
    _BRIDGE_STATUS_MISSING_AFTER_SECONDS,
    _BRIDGE_STATUS_STALE_AFTER_SECONDS,
    _InputStartResult,
    _InputStopResult,
    _ORDINARY_PRIMARY_ACTION_OPTIONS,
    _SECONDARY_ACTION_OPTIONS,
    _SETTINGS_BACKGROUND_JOIN_TIMEOUT_SECONDS,
    _VOICE_MODE_CYCLE_LABEL,
    _driver_action_active_event,
    _vb_cable_test_active_event,
    _with_openmexia_options,
)


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
    quickSetupChanged = Signal()
    voicePluginStateChanged = Signal()
    openMexiaStateChanged = Signal()
    openMexiaActionsChanged = Signal()
    voiceInputSourceChanged = Signal()
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
    voiceMicBlockedChanged = Signal()
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
    _inputOperationReady = Signal(object)
    _applicationExitStopReady = Signal(object)
    _bridgeRestartStopReady = Signal(object)
    _bridgeStopReady = Signal(object)
    _applicationUpdateCheckReady = Signal(object)
    _applicationUpdateDownloadReady = Signal(object)
    _applicationUpdateProgressReady = Signal(object)
    _vokiePluginDownloadReady = Signal(str)
    asrModelStateChanged = Signal()
    _asrModelInstallReady = Signal(object)

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
        self._vokiePluginDownloadReady.connect(
            self._on_vokie_plugin_download_ready
        )
        self._asrModelInstallReady.connect(self._on_asr_model_install_ready)
        self._asr_model_busy = False
        self._input_worker_result_lock = threading.Lock()
        self._input_worker_result = None
        self._config_root = config.config_root()
        application_update.cleanup_obsolete_update_downloads(
            self._config_root / "updates",
            __version__,
        )
        self._hid_helper_installed_distribution = (
            hid_elevation_windows.is_installed_distribution()
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
        self._voice_mic_blocked_reason = (
            getattr(runtime_status, "voice_mic_blocked_reason", "")
            if runtime_status is not None
            else ""
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
        self._wetype_installed: Optional[bool] = None
        self._doubao_installed: Optional[bool] = None
        self._chatterfly_installed: Optional[bool] = None
        self._openmexia_installed: Optional[bool] = None
        self._vokie_installed: Optional[bool] = None
        self._input_driver_installed: Optional[bool] = None
        self._vb_cable_installed: Optional[bool] = None
        self._microphone_level_timer = QTimer(self)
        self._microphone_level_timer.setInterval(50)
        self._microphone_level_timer.timeout.connect(
            self._tick_microphone_level
        )
        self._vokie_bridge = vokie_bridge.VokieBridge(self._config_root, self)
        self._vokie_bridge.stateChanged.connect(
            self._on_voice_plugin_state_changed
        )
        self._vokie_bridge.modesReceived.connect(self._on_vokie_modes_received)
        self._vokie_bridge.pluginDisconnected.connect(
            self._on_vokie_plugin_disconnected
        )
        # OpenMexia 联动：连接状态只存在内存里，设备清单是外部程序的，
        # 每次连接/刷新时重新拉一遍。
        self._openmexia_client = openmexia_client.OpenMexiaClient()
        self._openmexia_connected = False
        self._openmexia_devices: List[openmexia_client.OpenMexiaDevice] = []
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
        # 桥接连上后从 GATT 型号串（0x180A/0x2A24）读到的型号，只影响
        # 界面显示，不写入配置；为空时按蓝牙名判定的型号显示。
        self._detected_remote_profile = ""
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
        # 刚切换到的遥控器还没有保存过输出端点时置为 True，等端点列表刷新
        # 回来就自动采用推荐端点，免得每换一台设备都要重新选一次。
        self._pending_recommended_endpoint_apply = False
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
        # 软件方案在各型号之间共用一份列表：另一台遥控器第一次打开时，
        # 把已有方案补过来，缺的按键默认禁用。
        try:
            self._app_profile_store().adopt_apps_from_siblings()
        except Exception:  # noqa: BLE001 - 补不齐也不能挡启动
            pass
        # 配置档案里的「自动保存」行只是当前按键设置的投影，启动时就要填好，
        # 否则第一次打开配置档案会是空列表，直到某次保存才出现。
        try:
            self._refresh_mapping_presets()
        except Exception:  # noqa: BLE001 - a broken store must not block startup
            pass
        # 上次离开时插件连接是开着的，这次启动就照原样把监听开回来。
        if self._config.get("voice_plugin_auto_connect") is True:
            QTimer.singleShot(0, self._restore_voice_plugin_connection)
        # OpenMexia 同理：上次离开时连着，这次启动就自动连回去。
        if self._config.get("openmexia_auto_connect") is True:
            QTimer.singleShot(0, self._restore_openmexia_connection)

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
        self._vokie_bridge.stop()
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
        try:
            recommended_endpoint = audio_output.select_recommended_output_endpoint(
                endpoints
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
        self._apply_pending_recommended_endpoint(recommended_index, values)

    def _apply_pending_recommended_endpoint(
        self, recommended_index: int, values: List[audio_output.AudioEndpoint]
    ) -> None:
        """Adopt the recommended endpoint for a device that never had one.

        Only set right after switching to another remote whose record has no
        saved output endpoint, so the user is not asked to pick an endpoint
        again every time the active device changes. The save goes through
        the normal preflight path and stays silent: a failure simply leaves
        the field empty for the user to choose manually.
        """

        if not self._pending_recommended_endpoint_apply:
            return
        self._pending_recommended_endpoint_apply = False
        if str(self._config.get("output_endpoint_name") or ""):
            return
        if not 0 <= recommended_index < len(values):
            return
        if self._endpoint_preflight_busy:
            return
        endpoint = values[recommended_index]
        self._select_and_persist_output_endpoint(endpoint.name, endpoint.host_api)

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
            if self._detected_remote_profile in remote_selection.XIAOMI_PROFILES:
                # 连上后读到的型号串优先于蓝牙名：通用名可能把小米遥控器2
                # 当成 2 Pro，型号串不会。
                return self._detected_remote_profile
            return active
        if 0 <= self._selected_device_index < len(self._DEVICE_ORDER):
            return self._DEVICE_ORDER[self._selected_device_index]
        return self._selected_device_fallback_id

    def _app_profile_store(self) -> app_profiles.AppProfileStore:
        # 每个遥控器型号各有一份应用配置库，必须跟着当前选中的设备走。
        return app_profiles.AppProfileStore(
            self._config_root, self._selected_device_id())

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
        key = self.activeRemoteKey
        return remote_selection.label(
            key, remote_selection.profile_for_key(selection, key),
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
            conflict = key in saved and not remote_selection.profiles_compatible(
                saved[key], profile)
            ready = self._remote_scan_valid and key in present and not conflict
            note = ("（设备信息不一致）" if conflict else
                    "（待重新读取）" if not self._remote_scan_valid else
                    "（本次未找到）" if key not in present else "")
            choices.append(dict(key=key, profile=profile,
                label=remote_selection.label(key, profile, peers) + note,
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
            # 刚换到的遥控器可能还没同步过软件方案，补一次，保证两台看到
            # 的应用列表一样多。
            try:
                self._app_profile_store().adopt_apps_from_siblings()
            except Exception:  # noqa: BLE001 - 补不齐也不能挡切换
                pass
            try:
                self._refresh_mapping_presets()
            except Exception:  # noqa: BLE001 - 小米遥控器2 没有软件方案库
                # 只有 2 Pro 和 Chromecast 有方案库。切到 2 时读不到方案库
                # 不能把切换打断，否则界面会停在“正在结束旧设备”上不动。
                if self._mapping_edit_target:
                    self._mapping_edit_target = ""
                    self.mappingEditTargetChanged.emit()
                    self._load_bindings_into_model()
                self._mapping_preset_rows = []
                self.mappingPresetsChanged.emit()
                self._refresh_mapping_apps()
            self._voice_hotkey = str(saved["voice_hotkeys"].get("hold", ""))
            self._sync_voice_key_cache()
            self._set_mapping_dirty(False)
            self._selected_endpoint_index = next((index for index, item in enumerate(self._endpoint_values)
                if (item.name, item.host_api) == (saved.get("output_endpoint_name", ""), saved.get("output_endpoint_host_api", ""))), -1)
            self.selectedEndpointIndexChanged.emit()
            # 换到的这台遥控器还没存过输出端点：等下面的刷新拿回端点列表后
            # 自动采用推荐端点并保存，用户不用每次切换设备都重新选一遍。
            self._pending_recommended_endpoint_apply = not str(
                saved.get("output_endpoint_name") or ""
            )
            self._request_endpoint_options_refresh()
            self.hotkeyTextChanged.emit()
            self.holdVoiceHotkeyTextChanged.emit()
            self.voiceSlotHotkeysChanged.emit()
            self.voiceKeyConfigsChanged.emit()
            self.voiceHotkeySourceChanged.emit()
            self.voiceMappingReadyChanged.emit()
            self.selectedDeviceIndexChanged.emit()
            self.selectedDeviceChanged.emit()
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
                or self._voice_hotkey_busy
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

        from . import remote_selection
        detected_profile = remote_selection.profile_for_model_number(
            getattr(status, "remote_model", "") if status is not None else ""
        )
        if detected_profile != self._detected_remote_profile:
            self._detected_remote_profile = detected_profile
            self.selectedDeviceChanged.emit()
            self.remoteSelectionChanged.emit()

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

        voice_mic_blocked_reason = (
            getattr(status, "voice_mic_blocked_reason", "")
            if status is not None
            else ""
        )
        if voice_mic_blocked_reason != self._voice_mic_blocked_reason:
            self._voice_mic_blocked_reason = voice_mic_blocked_reason
            self.voiceMicBlockedChanged.emit()

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
        if not self._bridge_running:
            return "off"
        if self._bridge_connected and self._button_receiver_usable():
            return "connected"
        return "waiting"

    def _tray_icon_source(self) -> str:
        path = resources.find_tray_icon(self._tray_icon_state())
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
                result = (
                    False,
                    "所选语音输出设备无法实际打开；"
                    "若会议或通话软件正在占用该设备，请先退出后重试。",
                )
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
            store = self._app_profile_store()
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
        # 保存映射后立刻让 bridge 重载：话筒键警告（绑了普通动作）跟着当前
        # 生效的映射走，不能等下一次按键事件才对齐，否则改绑回语音键后切到
        # 语音页还会弹一下旧警告。
        bridge_launcher.reload_in_process_bridge_settings()
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
            if self._mapping_dirty and not self._save_mapping():
                # Commit the pending application layout through the profile
                # path first; the global-file reload below would otherwise
                # silently discard it.
                return finish(False)
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

    @Slot(str, result=bool)
    def isLaunchApplicationText(self, text: str) -> bool:
        return settings_ui.is_application_launch_text(text)

    @Slot(str, result=str)
    def applicationLaunchText(self, process: str) -> str:
        return settings_ui.application_launch_text(process)

    @Slot(str, result=str)
    def applicationLaunchTarget(self, text: str) -> str:
        return settings_ui.application_target_for_launch_text(text)

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
    def winuhidAvailable(self) -> bool:
        from . import injection_backend

        return injection_backend.winuhid_available()

    @Property(str, notify=injectModesChanged)
    def winuhidUnavailableReason(self) -> str:
        from . import injection_backend

        return injection_backend.winuhid_unavailable_reason()

    @Property(str, notify=injectModesChanged)
    def winuhidUnavailableHint(self) -> str:
        """User-facing explanation for an unusable WinUHid backend."""

        from . import injection_backend

        return injection_backend.winuhid_unavailable_hint()

    @Property(bool, notify=injectModesChanged)
    def winuhidInstallerAvailable(self) -> bool:
        """Whether this build ships the bundled WinUHid driver package.

        Distinct from ``winuhidAvailable`` (the driver is installed): the
        package can be present while the driver is not installed yet.
        """

        from . import winuhid_driver_install

        return winuhid_driver_install.installer_available()

    @Property(str, notify=injectModesChanged)
    def winuhidInstallerUnavailableReason(self) -> str:
        from . import winuhid_driver_install

        return winuhid_driver_install.installer_unavailable_reason()

    def _launch_winuhid_installer(self, uninstall: bool) -> dict:
        """Shared body for the install/uninstall slots.

        Only ever reached from an explicit user click in the button
        editor.  The elevated installer is awaited, so a failed run is
        reported with its exit code instead of looking like a success.
        """

        from . import winuhid_driver_install

        verb = "卸载" if uninstall else "安装"
        try:
            if uninstall:
                exit_code = winuhid_driver_install.uninstall_driver()
            else:
                exit_code = winuhid_driver_install.install_driver()
        except winuhid_driver_install.DriverInstallCancelledError as exc:
            return {"ok": False, "cancelled": True, "message": str(exc)}
        except winuhid_driver_install.DriverInstallerError as exc:
            return {"ok": False, "cancelled": False, "message": str(exc)}
        # The device was destroyed/recreated, so the running host can no
        # longer reach it: drop it and let the next press reconnect.
        from . import winuhid_host_windows

        winuhid_host_windows.reset_host()
        self.injectModesChanged.emit()
        if exit_code == 3010:
            tail = "请重启电脑，重启后回到本页重新选择注入方式。"
        elif uninstall:
            tail = "重新选择注入方式后即生效。"
        else:
            tail = "回到本页重新选择注入方式即可使用。"
        return {
            "ok": True,
            "cancelled": False,
            "message": f"WinUHid 虚拟键盘驱动{verb}程序已成功结束。{tail}",
        }

    @Slot(result="QVariantMap")
    def installWinUHidDriver(self) -> dict:
        return self._launch_winuhid_installer(uninstall=False)

    @Slot(result="QVariantMap")
    def uninstallWinUHidDriver(self) -> dict:
        return self._launch_winuhid_installer(uninstall=True)

    @Property(bool, notify=injectModesChanged)
    def winuhidDriverInstalled(self) -> bool:
        """Whether the WinUHid driver device exists on this machine."""

        from . import winuhid_driver_install

        return winuhid_driver_install.driver_installed() is True

    @Property(bool, notify=injectModesChanged)
    def interceptionAvailable(self) -> bool:
        from . import injection_backend

        return injection_backend.interception_available()

    @Property(str, notify=injectModesChanged)
    def interceptionUnavailableHint(self) -> str:
        """User-facing explanation for an unusable Interception backend."""

        from . import injection_backend

        return injection_backend.interception_unavailable_hint()

    @Property(bool, notify=injectModesChanged)
    def interceptionInstallerAvailable(self) -> bool:
        from . import interception_driver_install

        return interception_driver_install.installer_available()

    @Property(str, notify=injectModesChanged)
    def interceptionInstallerUnavailableReason(self) -> str:
        from . import interception_driver_install

        return interception_driver_install.installer_unavailable_reason()

    @Property(bool, notify=injectModesChanged)
    def interceptionDriverInstalled(self) -> bool:
        """Whether the Interception driver is installed on this machine."""

        from . import interception_runtime_windows

        return interception_runtime_windows.driver_installed() is True

    def _launch_interception_installer(self, uninstall: bool) -> dict:
        """Shared body for the Interception install/uninstall slots."""

        from . import interception_driver_install

        verb = "卸载" if uninstall else "安装"
        try:
            if uninstall:
                exit_code = interception_driver_install.uninstall_driver()
            else:
                exit_code = interception_driver_install.install_driver()
        except interception_driver_install.DriverInstallCancelledError as exc:
            return {"ok": False, "cancelled": True, "message": str(exc)}
        except interception_driver_install.DriverInstallerError as exc:
            return {"ok": False, "cancelled": False, "message": str(exc)}
        self.injectModesChanged.emit()
        if exit_code == 3010:
            tail = "请重启电脑，重启后回到本页重新选择注入方式。"
        elif uninstall:
            tail = "重新选择注入方式后即生效。"
        else:
            tail = "回到本页重新选择注入方式即可使用。"
        return {
            "ok": True,
            "cancelled": False,
            "message": f"Interception 驱动{verb}程序已成功结束。{tail}",
        }

    @Slot(result="QVariantMap")
    def installInterceptionDriver(self) -> dict:
        return self._launch_interception_installer(uninstall=False)

    @Slot(result="QVariantMap")
    def uninstallInterceptionDriver(self) -> dict:
        return self._launch_interception_installer(uninstall=True)

    @Slot(str, result=str)
    def buttonInjectMode(self, button_id: str) -> str:
        return self._model.inject_mode_for(str(button_id))

    @Slot(str, str)
    def setButtonInjectMode(self, button_id: str, mode: str) -> None:
        row = self._model.index_of(str(button_id))
        if row < 0:
            return
        self._model.setInjectModeAt(row, mode)

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

    def _get_voice_cycle_toast_offset(self) -> int:
        return config.voice_cycle_toast_offset(self._config)

    voiceCycleToastOffset = Property(
        int, _get_voice_cycle_toast_offset, notify=voiceCycleConfigChanged
    )

    voiceCycleToastOffsetMax = Property(
        int, lambda self: config.VOICE_CYCLE_TOAST_OFFSET_MAX, constant=True
    )

    voiceCycleToastOffIndex = Property(
        int, lambda self: len(self._VOICE_CYCLE_TOAST_ORDER) - 1, constant=True
    )

    @Slot(int)
    def setVoiceCycleToastOffset(self, offset: int) -> None:
        try:
            value = int(offset)
        except (TypeError, ValueError):
            return
        value = max(
            config.VOICE_CYCLE_TOAST_OFFSET_MIN,
            min(config.VOICE_CYCLE_TOAST_OFFSET_MAX, value),
        )
        if value == self._get_voice_cycle_toast_offset():
            return
        new_config = dict(self._config)
        new_config["voice_cycle_toast_offset"] = value
        self._save_voice_config_data(new_config)

    def _get_asr_model_ready(self) -> bool:
        return local_asr.model_available(self._config_root)

    asrModelReady = Property(bool, _get_asr_model_ready, notify=asrModelStateChanged)

    def _get_asr_model_busy(self) -> bool:
        return self._asr_model_busy

    asrModelBusy = Property(bool, _get_asr_model_busy, notify=asrModelStateChanged)

    def _get_asr_model_status_text(self) -> str:
        if self._asr_model_busy:
            return "正在下载模型…"
        return "已就绪" if self._get_asr_model_ready() else "未安装"

    asrModelStatusText = Property(
        str, _get_asr_model_status_text, notify=asrModelStateChanged
    )

    @Slot()
    def installAsrModel(self) -> None:
        """用户点了「安装」才下载模型，不默认下载。"""

        if self._asr_model_busy or self._get_asr_model_ready():
            return
        self._asr_model_busy = True
        self.asrModelStateChanged.emit()
        self._set_error_message("")
        self._set_status_message(
            "正在下载语音识别模型，约 228MB，下载期间可以继续用其他设置。",
            self._VOICE_PAGE_INDEX,
        )

        def run() -> None:
            try:
                local_asr.install_model(self._config_root)
                payload: object = (True, "")
            except local_asr.LocalAsrError as exc:
                payload = (False, str(exc))
            except BaseException:  # noqa: BLE001 - 后台线程不能甩异常
                payload = (False, "下载语音识别模型失败，请检查网络后重试。")
            self._emit_background_result(self._asrModelInstallReady, payload)

        try:
            self._start_background_task(run, "local-asr-model-install")
        except Exception:  # noqa: BLE001 - 窗口正在关闭
            self._asr_model_busy = False
            self.asrModelStateChanged.emit()

    def _on_asr_model_install_ready(self, payload: object) -> None:
        ok, message = payload if isinstance(payload, tuple) else (False, "")
        self._asr_model_busy = False
        self.asrModelStateChanged.emit()
        if ok:
            self._set_status_message(
                "语音识别模型已安装好，按住 OpenMexia 语音键说话即可。",
                self._VOICE_PAGE_INDEX,
            )
        else:
            self._set_error_message(
                str(message) or "下载语音识别模型失败，请检查网络后重试。",
                self._VOICE_PAGE_INDEX,
            )

    @Slot()
    def uninstallAsrModel(self) -> None:
        if self._asr_model_busy:
            return
        try:
            local_asr.remove_model(self._config_root)
        except local_asr.LocalAsrError as exc:
            self._set_error_message(str(exc), self._VOICE_PAGE_INDEX)
            return
        self.asrModelStateChanged.emit()
        self._set_error_message("")
        self._set_status_message(
            "已卸载语音识别模型。", self._VOICE_PAGE_INDEX
        )

    @Slot(result=bool)
    def placeVoiceToastTuner(self) -> bool:
        """Fill the tuner mask over the work area of the settings window's screen."""
        from PySide6.QtGui import QGuiApplication

        window = None
        for candidate in QGuiApplication.topLevelWindows():
            if candidate.objectName() == "voiceToastTunerWindow":
                window = candidate
                break
        if window is None:
            return False
        parent = window.transientParent()
        screen = parent.screen() if parent is not None else None
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return False
        # 子窗口默认挂在主屏上，这里先跟随设置窗口所在屏幕再铺满工作区。
        window.setScreen(screen)
        window.setGeometry(screen.availableGeometry())
        return True

    def _persist_voice_config_fields(self, updates: Dict[str, Any]) -> bool:
        new_config = dict(self._config)
        new_config.update(updates)
        if (
            ("voice_input_source" in updates
             or "voice_local_input_device" in updates)
            and "voice_key_configs" not in updates
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
        entries = config.voice_key_configs(self._config)
        rows: List[dict] = []
        for index, entry in enumerate(entries):
            slot = int(entry["id"])
            source = entry["source"]
            device = entry["device"]
            plugin = str(entry.get("plugin", ""))
            try:
                device_index = devices.index(device) if device else -1
            except ValueError:
                device_index = -1
            configured_hotkey = entry["hotkey"]
            rows.append(
                {
                    "id": slot,
                    "index": index,
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
                    "enabled": bool(entry.get("enabled", True)),
                    "shortPressResend": bool(
                        entry.get("short_press_resend", False)
                    ),
                    "gainSteps": config.voice_key_gain_steps(self._config, slot),
                    "gainAuto": config.voice_key_gain_auto(self._config, slot),
                    "tsfSwitch": config.voice_key_tsf_switch(self._config, slot),
                    "tsfProfile": config.voice_key_tsf_profile(self._config, slot),
                    "plugin": plugin,
                    "locked": config.is_locked_plugin_id(plugin),
                    "first": index == 0,
                    "last": index == len(entries) - 1,
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
        if config.is_locked_plugin_id(entry.get("plugin")):
            self._set_status_message("")
            self._set_error_message(
                "该语音键由 OpenMexia 联动提供，断开连接后会自动清除。"
                if config.is_openmexia_plugin_id(entry.get("plugin"))
                else "该语音键由 MoteDeck2Vokie 插件提供，不能删除。",
                self._VOICE_PAGE_INDEX,
            )
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

    @Slot(int, int)
    def moveVoiceKeyConfigTo(self, slot: int, target_index: int) -> None:
        self._save_voice_config_data(
            config.move_voice_key_config_to(self._config, slot, target_index)
        )

    @Slot(int, bool)
    def setVoiceKeyConfigEnabled(self, slot: int, enabled: bool) -> None:
        self._save_voice_config_data(
            config.set_voice_key_config_enabled(self._config, slot, enabled)
        )

    @Slot(int, bool)
    def setVoiceKeyConfigShortPressResend(self, slot: int, enabled: bool) -> None:
        self._save_voice_config_data(
            config.set_voice_key_config_short_press_resend(
                self._config, slot, enabled
            )
        )

    @Slot(int, str)
    def setVoiceKeyConfigTsf(self, slot: int, profile: str) -> None:
        self._save_voice_config_data(
            config.set_voice_key_config_tsf(self._config, slot, profile)
        )

    @Slot(result="QVariantList")
    def tsfInputMethods(self) -> list:
        return tsf_input_windows.list_input_methods()

    @Slot(int, int, bool)
    def setVoiceKeyConfigGain(self, slot: int, steps: int, auto: bool) -> None:
        self._save_voice_config_data(
            config.set_voice_key_config_gain(self._config, slot, steps, auto)
        )

    @Slot(int, str)
    def setVoiceKeyConfigName(self, slot: int, value: str) -> None:
        self._save_voice_config_data(
            config.update_voice_key_config(self._config, slot, name=value)
        )

    @Slot(int, str)
    def setVoiceKeyConfigHotkey(self, slot: int, value: str) -> None:
        entry = config.voice_key_config(self._config, slot)
        if entry is not None and config.is_locked_plugin_id(entry.get("plugin")):
            self._set_status_message("")
            self._set_error_message(
                "该语音键由 OpenMexia 联动提供，快捷键不可编辑。"
                if config.is_openmexia_plugin_id(entry.get("plugin"))
                else "该快捷键由 MoteDeck2Vokie 插件自带，不能在这里修改。",
                self._VOICE_PAGE_INDEX,
            )
            return
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

    # -- 快速配置：微信输入法 / 豆包输入法 -------------------------------

    def _quick_setup_voice_key_entry(
        self, name: str
    ) -> Optional[Dict[str, object]]:
        for entry in config.voice_key_configs(self._config):
            if str(entry.get("name", "")) == name:
                return entry
        return None

    @Slot()
    def refreshQuickSetup(self) -> None:
        self._wetype_installed = quick_setup_windows.wetype_installed()
        self._doubao_installed = quick_setup_windows.doubao_installed()
        self._chatterfly_installed = quick_setup_windows.chatterfly_installed()
        self._openmexia_installed = quick_setup_windows.openmexia_installed()
        self._vokie_installed = quick_setup_windows.vokie_installed()
        self._input_driver_installed = (
            quick_setup_windows.input_driver_installed()
        )
        self._vb_cable_installed = self._detect_vb_cable_installed()
        self.quickSetupChanged.emit()

    def _get_wetype_installed(self) -> bool:
        if self._wetype_installed is None:
            self._wetype_installed = quick_setup_windows.wetype_installed()
        return self._wetype_installed

    wetypeInstalled = Property(
        bool, _get_wetype_installed, notify=quickSetupChanged
    )

    def _get_doubao_installed(self) -> bool:
        if self._doubao_installed is None:
            self._doubao_installed = quick_setup_windows.doubao_installed()
        return self._doubao_installed

    doubaoInstalled = Property(
        bool, _get_doubao_installed, notify=quickSetupChanged
    )

    def _get_chatterfly_installed(self) -> bool:
        if self._chatterfly_installed is None:
            self._chatterfly_installed = (
                quick_setup_windows.chatterfly_installed()
            )
        return self._chatterfly_installed

    chatterflyInstalled = Property(
        bool, _get_chatterfly_installed, notify=quickSetupChanged
    )

    def _get_input_driver_installed(self) -> bool:
        if self._input_driver_installed is None:
            self._input_driver_installed = (
                quick_setup_windows.input_driver_installed()
            )
        return self._input_driver_installed

    inputDriverInstalled = Property(
        bool, _get_input_driver_installed, notify=quickSetupChanged
    )

    # 虚拟声卡（VB-CABLE）是否已安装：快速配置底栏没装时提示先安装并重启。
    def _detect_vb_cable_installed(self) -> bool:
        try:
            return any(
                audio_output.is_cable_input_endpoint(endpoint.name)
                for endpoint in audio_output.enumerate_output_endpoints()
            )
        except audio_output.AudioOutputUnavailableError:
            return False

    def _get_vb_cable_installed(self) -> bool:
        if self._vb_cable_installed is None:
            self._vb_cable_installed = self._detect_vb_cable_installed()
        return self._vb_cable_installed

    vbCableInstalled = Property(
        bool, _get_vb_cable_installed, notify=quickSetupChanged
    )

    def _get_openmexia_installed(self) -> bool:
        if self._openmexia_installed is None:
            self._openmexia_installed = (
                quick_setup_windows.openmexia_installed()
            )
        return self._openmexia_installed

    openMexiaInstalled = Property(
        bool, _get_openmexia_installed, notify=quickSetupChanged
    )

    def _get_vokie_installed(self) -> bool:
        if self._vokie_installed is None:
            self._vokie_installed = quick_setup_windows.vokie_installed()
        return self._vokie_installed

    vokieInstalled = Property(
        bool, _get_vokie_installed, notify=quickSetupChanged
    )

    def _quick_setup_hotkey_text(self, name: str) -> str:
        entry = self._quick_setup_voice_key_entry(name)
        value = str(entry.get("hotkey", "")) if entry is not None else ""
        return hotkey.format_hotkey_text(value) if value else ""

    def _get_wetype_voice_key_hotkey_text(self) -> str:
        return self._quick_setup_hotkey_text(WETYPE_VOICE_KEY_NAME)

    wetypeVoiceKeyHotkeyText = Property(
        str, _get_wetype_voice_key_hotkey_text, notify=quickSetupChanged
    )

    def _get_doubao_voice_key_hotkey_text(self) -> str:
        return self._quick_setup_hotkey_text(DOUBAO_VOICE_KEY_NAME)

    doubaoVoiceKeyHotkeyText = Property(
        str, _get_doubao_voice_key_hotkey_text, notify=quickSetupChanged
    )

    def _get_chatterfly_voice_key_hotkey_texts(self) -> List[str]:
        return [
            self._quick_setup_hotkey_text(name)
            for name in CHATTERFLY_VOICE_KEY_NAMES
        ]

    chatterflyVoiceKeyHotkeyTexts = Property(
        "QVariantList",
        _get_chatterfly_voice_key_hotkey_texts,
        notify=quickSetupChanged,
    )

    def _create_quick_setup_voice_key(
        self,
        name: str,
        chord: str,
        *,
        short_press_resend: bool = False,
    ) -> Optional[tuple]:
        """Create or update the named voice key and persist it.

        Returns ``(config, slot)`` on success, ``None`` after reporting the
        failure to the voice page. ``short_press_resend`` only ever turns
        the option on (Chatterfly 的「延迟发送」)；为 False 时保持用户原配置。
        """

        text = str(chord).strip()
        if not text:
            return None
        try:
            text = hotkey.HotkeySpec.from_user_text(text).serialize()
        except hotkey.HotkeyParseError as exc:
            self._set_error_message(str(exc), self._VOICE_PAGE_INDEX)
            return None
        extra = {"short_press_resend": True} if short_press_resend else {}
        entry = self._quick_setup_voice_key_entry(name)
        if entry is not None:
            slot = int(entry["id"])
            new_config = config.update_voice_key_config(
                self._config,
                slot,
                hotkey=text,
                **extra,
            )
        else:
            new_config = config.add_voice_key_config(self._config)
            slots = [
                int(item["id"])
                for item in config.voice_key_configs(new_config)
            ]
            if len(slots) <= len(config.voice_key_configs(self._config)):
                self._set_error_message(
                    "语音键数量已达上限，无法再创建。",
                    self._VOICE_PAGE_INDEX,
                )
                return None
            slot = max(slots)
            new_config = config.update_voice_key_config(
                new_config,
                slot,
                name=name,
                hotkey=text,
                **extra,
            )
        if not self._save_voice_config_data(new_config):
            return None
        return new_config, slot

    @Slot(str, result=bool)
    def createWetypeVoiceKey(self, chord: str) -> bool:
        created = self._create_quick_setup_voice_key(
            WETYPE_VOICE_KEY_NAME, chord
        )
        if created is None:
            return False
        new_config, slot = created
        self._bind_mic_to_voice_key(new_config, slot)
        self.quickSetupChanged.emit()
        return True

    @Slot(str, result=bool)
    def createDoubaoVoiceKey(self, chord: str) -> bool:
        created = self._create_quick_setup_voice_key(
            DOUBAO_VOICE_KEY_NAME, chord
        )
        if created is None:
            return False
        new_config, slot = created
        # 豆包输入法要驱动级注入：装的是哪个驱动就用哪个。
        self._bind_mic_to_voice_key(
            new_config,
            slot,
            inject_mode=quick_setup_windows.installed_driver_inject_mode(),
        )
        self.quickSetupChanged.emit()
        return True

    @Slot(int, str, result=bool)
    def createChatterflyVoiceKey(self, index: int, chord: str) -> bool:
        if index < 0 or index >= len(CHATTERFLY_VOICE_KEY_NAMES):
            return False
        created = self._create_quick_setup_voice_key(
            CHATTERFLY_VOICE_KEY_NAMES[index],
            chord,
            # Chatterfly 要自动打开「延迟发送」。
            short_press_resend=True,
        )
        if created is None:
            return False
        new_config, slot = created
        # 和豆包输入法一样，装的是哪个驱动就用哪个。
        self._bind_mic_to_voice_key(
            new_config,
            slot,
            inject_mode=quick_setup_windows.installed_driver_inject_mode(),
        )
        self.quickSetupChanged.emit()
        return True

    def _bind_mic_to_voice_key(
        self,
        config_data: Dict[str, object],
        slot: int,
        *,
        inject_mode: str = "",
    ) -> None:
        """把麦克风键的映射指到刚建好的语音键。

        微信输入法只认落在正常输入流里的按键，驱动级注入（WinUHid /
        Interception）是另一块虚拟键盘，它那边触发不了，所以默认固定走
        SendInput。豆包输入法相反，调用方会传入已安装的驱动级注入方式。
        """

        row = self._model.index_of("mic")
        if row < 0:
            return
        label = config.voice_key_labels(config_data).get(slot, "")
        if label:
            self._model.setActionTextAt(row, label)
        self._model.setInjectModeAt(
            row, inject_mode or key_mapping.DEFAULT_INJECT_MODE
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
            # 关流要等 PortAudio/WASAPI 把设备收回去，实测阻塞 8~25ms。
            # 离开语音页时这段阻塞正好压在切页那一帧上，所以延到本轮事件
            # 处理之后再做，让切页那一帧先画出去。仍在 UI 线程执行，
            # 不跨线程碰 PortAudio 的流。
            QTimer.singleShot(0, monitor.stop)
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

    # -- 语音页：外部应用程序支持（Vokie 插件）-------------------------

    @Slot(result=bool)
    def connectVoicePlugin(self) -> bool:
        """开启本机回环服务，等待 Vokie 插件把语音模式送进来。"""

        if not self._vokie_bridge.start():
            self._set_status_message("")
            self._set_error_message(
                "无法开启插件连接，请稍后重试。", self._VOICE_PAGE_INDEX
            )
            return False
        self._remember_voice_plugin_connection(True)
        self._set_error_message("")
        self._set_status_message(
            "已开启插件连接，请在 Vokie 中点击“开启连接”。",
            self._VOICE_PAGE_INDEX,
        )
        self.voicePluginStateChanged.emit()
        self.voiceKeyConfigsChanged.emit()
        return True

    @Slot()
    def disconnectVoicePlugin(self) -> None:
        self._vokie_bridge.stop()
        self._remember_voice_plugin_connection(False)
        self._release_voice_plugin_keys()
        self.voicePluginStateChanged.emit()
        self.voiceKeyConfigsChanged.emit()
        self._set_error_message("")
        self._set_status_message(
            "已断开插件连接，插件提供的语音键已清除。", self._VOICE_PAGE_INDEX
        )

    def _release_voice_plugin_keys(
        self, plugin_id: str = config.VOICE_KEY_PLUGIN_VOKIE
    ) -> None:
        """插件断开后把它带来的语音键清掉，绑过它的按键回到禁用。"""

        if not any(
            entry.get("plugin") for entry in config.voice_key_configs(self._config)
        ):
            return
        previous_labels = config.voice_key_labels(self._config)
        new_config = config.release_plugin_voice_keys(self._config, plugin_id)
        if not self._save_voice_config_data(new_config):
            return
        for slot, label in previous_labels.items():
            if config.voice_key_config(self._config, slot) is None:
                self._model.clear_voice_action(int(slot), label)

    def _remember_voice_plugin_connection(self, enabled: bool) -> None:
        """记住插件连接开关的状态，下次启动照它决定要不要自动恢复监听。"""

        if self.configurationReadOnly:
            return
        if bool(self._config.get("voice_plugin_auto_connect")) == enabled:
            return
        next_config = dict(self._config)
        next_config["voice_plugin_auto_connect"] = enabled
        try:
            self._config = config.save_config_and_load(
                config.config_path(self._config_root), next_config
            )
        except Exception:  # noqa: BLE001 - 只影响下次自动连，当前连接照常
            pass
        else:
            self._bump_settings_revision()

    def _restore_voice_plugin_connection(self) -> None:
        """启动时按上次的状态把监听开回来，不弹成功提示。"""

        if not self._config.get("voice_plugin_auto_connect"):
            return
        if not self._vokie_bridge.start():
            self._set_error_message(
                "无法开启插件连接，请稍后重试。", self._VOICE_PAGE_INDEX
            )
            return
        self.voicePluginStateChanged.emit()
        self.voiceKeyConfigsChanged.emit()

    def _on_voice_plugin_state_changed(self) -> None:
        self.voicePluginStateChanged.emit()
        self.voiceKeyConfigsChanged.emit()

    def _on_vokie_modes_received(self, modes: object) -> None:
        rows = vokie_bridge.normalize_modes(modes)
        if not rows:
            self._set_status_message("")
            self._set_error_message(
                "MoteDeck2Vokie 插件没有提供可用的语音模式。", self._VOICE_PAGE_INDEX
            )
            return
        previous_labels = config.voice_key_labels(self._config)
        new_config = config.apply_plugin_voice_keys(
            self._config, config.VOICE_KEY_PLUGIN_VOKIE, rows
        )
        if not self._save_voice_config_data(new_config):
            return
        # 被插件换掉的语音键可能还挂在物理按键上，需要一起解绑。
        for slot, label in previous_labels.items():
            if config.voice_key_config(self._config, slot) is None:
                self._model.clear_voice_action(int(slot), label)
        self._set_status_message(
            f"已接收 Vokie 的 {len(rows)} 个语音模式，加在原有语音键后面。",
            self._VOICE_PAGE_INDEX,
        )

    def _on_vokie_plugin_disconnected(self) -> None:
        # 插件关了或连接断了，它带来的语音键已经没有意义，直接从配置里清掉。
        self._release_voice_plugin_keys()
        self.voicePluginStateChanged.emit()
        self.voiceKeyConfigsChanged.emit()
        self._set_status_message(
            "MoteDeck2Vokie 插件已断开，插件提供的语音键已清除。",
            self._VOICE_PAGE_INDEX,
        )

    voicePluginConnected = Property(
        bool,
        lambda self: bool(self._vokie_bridge.connected()),
        notify=voicePluginStateChanged,
    )

    # -- 语音页：OpenMexia 联动 ------------------------------------------

    def _openmexia_register_actions(self) -> None:
        """把当前设备清单注册成动作下拉里的「设备 · 操作」条目。"""

        settings_ui.set_openmexia_actions(
            [
                (control.label, control.device, control.op)
                for device in self._openmexia_devices
                for control in device.controls
            ]
        )

    @Slot(result=bool)
    def connectOpenMexia(self) -> bool:
        """连上正在运行的 OpenMexia：拉设备、加语音键、加应用配置。"""

        return self._openmexia_connect(announce=True)

    def _openmexia_connect(self, *, announce: bool) -> bool:
        """真正的连接流程；启动时自动恢复用 announce=False 静默处理。"""

        try:
            self._openmexia_devices = self._openmexia_client.devices()
        except openmexia_client.OpenMexiaError as exc:
            self._openmexia_drop_stale_app_profiles()
            if announce:
                self._set_error_message(str(exc), self._VOICE_PAGE_INDEX)
            return False
        except Exception as exc:  # noqa: BLE001 - Qt 槽不能甩异常
            self._openmexia_drop_stale_app_profiles()
            if announce:
                self._set_error_message(
                    f"连接 OpenMexia 失败：{exc}", self._VOICE_PAGE_INDEX
                )
            return False
        self._openmexia_register_actions()
        self._openmexia_ensure_voice_key()
        self._openmexia_ensure_app_profile()
        self._openmexia_connected = True
        self._remember_openmexia_connection(True)
        self.openMexiaStateChanged.emit()
        self.openMexiaActionsChanged.emit()
        self._set_error_message("")
        if announce:
            self._set_status_message(
                f"已连接 OpenMexia，读取到 {self._openmexia_device_count()} 台设备；"
                "按键动作里已可以选择它们。",
                self._VOICE_PAGE_INDEX,
            )
        return True

    @Slot()
    def disconnectOpenMexia(self) -> None:
        self._openmexia_connected = False
        self._openmexia_devices = []
        self._remember_openmexia_connection(False)
        settings_ui.set_openmexia_actions(())
        # 这条语音键由联动提供，断开后它已经没有意义，直接清掉。
        self._release_voice_plugin_keys(config.VOICE_KEY_PLUGIN_OPENMEXIA)
        # 插件自带的软件配置也跟着断开移除，但之前调好的按键映射先存下来，
        # 下次连上时原样恢复。
        self._openmexia_release_app_profile()
        self.openMexiaStateChanged.emit()
        self.openMexiaActionsChanged.emit()
        self.voiceKeyConfigsChanged.emit()
        self._set_error_message("")
        self._set_status_message(
            "已断开 OpenMexia，它添加的语音键已清除。", self._VOICE_PAGE_INDEX
        )

    @Slot(result=bool)
    def refreshOpenMexiaDevices(self) -> bool:
        if not self._openmexia_connected:
            return False
        return self.connectOpenMexia()

    def _openmexia_ensure_voice_key(self) -> None:
        """连接后自动多一条 OpenMexia 语音键（已有就不重复加）。"""

        if config.openmexia_voice_key_slot(self._config) is not None:
            return
        new_config, slot = config.ensure_openmexia_voice_key(self._config)
        if slot is None:
            return
        self._save_voice_config_data(new_config)

    def _openmexia_ensure_app_profile(self) -> None:
        """连接后自动多一条 OpenMexia 的软件配置，不动全局和其他软件。"""

        if self.configurationReadOnly:
            return
        profile = self._selected_device_id()
        store = self._app_profile_store()
        try:
            mapping = app_profiles.snapshot(self._bindings, profile)
            restored = self._openmexia_read_app_stash(profile)
            if restored is not None:
                mapping = restored
            identity = store.ensure_app(
                openmexia_client.APP_PROFILE_NAME,
                openmexia_client.PROCESS_NAME,
                mapping,
            )
            if restored is not None:
                self._openmexia_drop_app_stash(profile)
            # 另一台遥控器上那条配置已经跟着同步过去了，把它自己调过的
            # 那份映射补回去，别用这台遥控器的映射覆盖它。
            for other in app_profiles.PROFILES:
                if other == profile:
                    continue
                stash = self._openmexia_read_app_stash(other)
                if stash is None:
                    continue
                try:
                    app_profiles.AppProfileStore(
                        self._config_root, other).update_mapping(identity, stash)
                except Exception:  # noqa: BLE001 - 恢复失败就先用同步过来的
                    continue
                self._openmexia_drop_app_stash(other)
        except Exception as exc:  # noqa: BLE001 - 配置写不进去不影响连接
            self._set_error_message(f"OpenMexia 应用配置添加失败：{exc}", self._VOICE_PAGE_INDEX)
            return
        self._refresh_mapping_presets()

    def _openmexia_app_stash_path(self) -> Path:
        return Path(self._config_root) / "openmexia_app_mapping.json"

    def _openmexia_read_stash_table(self) -> Dict[str, dict]:
        """读出按遥控器型号分开的暂存表；读不出来当空。"""

        try:
            with self._openmexia_app_stash_path().open("r", encoding="utf-8") as handle:
                stored = json.load(handle)
        except Exception:  # noqa: BLE001 - 暂存只是锦上添花
            return {}
        if not isinstance(stored, dict):
            return {}
        table = stored.get("profiles")
        if isinstance(table, dict):
            return {key: row for key, row in table.items() if isinstance(row, dict)}
        # 旧版只存一份、没记型号，按小米处理。
        if "mapping" in stored:
            return {device_catalog.RC003_ID: stored}
        return {}

    def _openmexia_write_app_stash(self, profile: str, entry: dict) -> None:
        try:
            table = self._openmexia_read_stash_table()
            table[profile] = {"name": entry["name"], "mapping": entry["mapping"]}
            config._save_json_atomic(
                self._openmexia_app_stash_path(), {"profiles": table})
        except Exception:  # noqa: BLE001 - 存不下也照常移除
            pass

    def _openmexia_drop_app_stash(self, profile: str) -> None:
        try:
            table = self._openmexia_read_stash_table()
            if table.pop(profile, None) is None:
                return
            if table:
                config._save_json_atomic(
                    self._openmexia_app_stash_path(), {"profiles": table})
            else:
                self._openmexia_app_stash_path().unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 - 暂存只是锦上添花
            pass

    def _openmexia_read_app_stash(self, profile: str) -> Optional[dict]:
        """断开时暂存的那份按键映射；读不出来就当没有。"""

        try:
            stored = self._openmexia_read_stash_table().get(profile)
            if stored is None:
                return None
            return app_profiles.validate_mapping(stored["mapping"], profile, partial=True)
        except Exception:  # noqa: BLE001 - 暂存只是锦上添花
            return None

    def _openmexia_drop_stale_app_profiles(self) -> None:
        """连不上插件时，把它以前留下的软件配置一并清掉。

        只是“重新拉一次设备”失败（原本就连着）时不动配置。
        """

        if self._openmexia_connected:
            return
        self._openmexia_release_app_profile()

    def _openmexia_release_app_profile(self) -> None:
        """断开插件：移除它自带的软件配置，但把按键映射暂存下来。"""

        if self.configurationReadOnly:
            return
        active = self._selected_device_id()
        # 软件方案在各型号的库之间是同步的，先把每份映射都读出来存好，
        # 再删一次——删这一下会跟着同步到其它型号。
        found: Dict[str, dict] = {}
        for profile in app_profiles.PROFILES:
            try:
                entry = app_profiles.AppProfileStore(
                    self._config_root, profile
                ).find_app(openmexia_client.PROCESS_NAME)
            except Exception:  # noqa: BLE001 - 断开流程不能因此中断
                continue
            if entry is not None:
                found[profile] = entry
        if not found:
            return
        for profile, entry in found.items():
            self._openmexia_write_app_stash(profile, entry)
        source = active if active in found else next(iter(found))
        entry = found[source]
        try:
            app_profiles.AppProfileStore(self._config_root, source).delete(entry["id"])
        except Exception:  # noqa: BLE001 - 断开流程不能因此中断
            return
        if entry["id"] == self._mapping_edit_target:
            self._mapping_edit_target = ""
            self.mappingEditTargetChanged.emit()
            self._load_bindings_into_model()
        self._refresh_mapping_presets()

    def _remember_openmexia_connection(self, enabled: bool) -> None:
        """记住 OpenMexia 的连接开关，下次启动照它决定要不要自动连回去。"""

        if self.configurationReadOnly:
            return
        if bool(self._config.get("openmexia_auto_connect")) == enabled:
            return
        next_config = dict(self._config)
        next_config["openmexia_auto_connect"] = enabled
        try:
            self._config = config.save_config_and_load(
                config.config_path(self._config_root), next_config
            )
        except Exception:  # noqa: BLE001 - 只影响下次自动连，当前连接照常
            pass
        else:
            self._bump_settings_revision()

    def _restore_openmexia_connection(self) -> None:
        """启动时按上次的状态自动连回 OpenMexia，不弹成功提示。"""

        if not self._config.get("openmexia_auto_connect"):
            return
        self._openmexia_connect(announce=False)

    def _get_openmexia_connected(self) -> bool:
        return bool(self._openmexia_connected)

    openMexiaConnected = Property(
        bool, _get_openmexia_connected, notify=openMexiaStateChanged
    )

    def _openmexia_device_count(self) -> int:
        # 房间是 OpenMexia 额外挂出来的虚拟条目，不占真实设备数
        return sum(
            1
            for device in self._openmexia_devices
            if device.type != "room"
        )

    def _get_openmexia_device_count(self) -> int:
        return self._openmexia_device_count()

    openMexiaDeviceCount = Property(
        int, _get_openmexia_device_count, notify=openMexiaStateChanged
    )

    voicePluginState = Property(
        str,
        lambda self: self._vokie_bridge.state(),
        notify=voicePluginStateChanged,
    )

    voicePluginName = Property(
        str,
        lambda self: self._vokie_bridge.pluginName(),
        notify=voicePluginStateChanged,
    )

    voicePluginModeCount = Property(
        int,
        lambda self: len(self._vokie_bridge.modes()),
        notify=voicePluginStateChanged,
    )

    @Slot(result=bool)
    def openVokieWebsite(self) -> bool:
        result = shell_targets.open_external_target(VOKIE_WEBSITE_URL)
        self._report_external_target(result, self._VOICE_PAGE_INDEX)
        return result.outcome is shell_targets.ExternalTargetOutcome.OPENED

    @Slot(result=bool)
    def openWetypeWebsite(self) -> bool:
        result = shell_targets.open_external_target(WETYPE_WEBSITE_URL)
        self._report_external_target(result, self._VOICE_PAGE_INDEX)
        return result.outcome is shell_targets.ExternalTargetOutcome.OPENED

    @Slot(result=bool)
    def openDoubaoWebsite(self) -> bool:
        result = shell_targets.open_external_target(DOUBAO_WEBSITE_URL)
        self._report_external_target(result, self._VOICE_PAGE_INDEX)
        return result.outcome is shell_targets.ExternalTargetOutcome.OPENED

    @Slot(result=bool)
    def openChatterflyWebsite(self) -> bool:
        result = shell_targets.open_external_target(CHATTERFLY_WEBSITE_URL)
        self._report_external_target(result, self._VOICE_PAGE_INDEX)
        return result.outcome is shell_targets.ExternalTargetOutcome.OPENED

    @Slot(result=bool)
    def openVokiePluginDownload(self) -> bool:
        # 两处下载按钮（语音页、快速设置弹窗）都走这里：先在后台解析
        # 带版本号的插件包下载地址，失败回退固定下载链接。
        def resolve() -> None:
            try:
                url = application_update.resolve_latest_plugin_download().download_url
            except Exception:  # noqa: BLE001 - 解析失败一律回退固定下载链接
                url = VOKIE_PLUGIN_URL
            self._emit_background_result(self._vokiePluginDownloadReady, url)

        try:
            self._start_background_task(resolve, "vokie-plugin-download")
        except Exception:
            return False
        return True

    @Slot(result=bool)
    def openOpenMexiaWebsite(self) -> bool:
        result = shell_targets.open_external_target(OPENMEXIA_WEBSITE_URL)
        self._report_external_target(result, self._VOICE_PAGE_INDEX)
        return result.outcome is shell_targets.ExternalTargetOutcome.OPENED

    @Slot(result=bool)
    def openOpenMexiaDownload(self) -> bool:
        result = shell_targets.open_external_target(OPENMEXIA_DOWNLOAD_URL)
        self._report_external_target(result, self._VOICE_PAGE_INDEX)
        return result.outcome is shell_targets.ExternalTargetOutcome.OPENED

    @Slot(result=bool)
    def openVoiceKeyTutorial(self) -> bool:
        result = shell_targets.open_external_target(VOICE_KEY_TUTORIAL_URL)
        self._report_external_target(result, self._VOICE_PAGE_INDEX)
        return result.outcome is shell_targets.ExternalTargetOutcome.OPENED

    @Slot(result=bool)
    def openHelpCenter(self) -> bool:
        result = shell_targets.open_external_target(HELP_CENTER_URL)
        self._report_external_target(result, self._DEVICE_PAGE_INDEX)
        return result.outcome is shell_targets.ExternalTargetOutcome.OPENED

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

    voiceMicBlockedReason = Property(
        str,
        lambda self: self._voice_mic_blocked_reason,
        notify=voiceMicBlockedChanged,
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
            profile = self._selected_device_id()
            if profile in self._DEVICE_ORDER:
                return self._DEVICE_ORDER.index(profile)
            return self._selected_device_index
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
        # QML 里这个属性实际是“小米竖版 13 键遥控器”的布局开关，
        # 小米遥控器2（RC001）按键集合完全一致，共用同一套布局。
        return self._selected_device_id() in (
            device_catalog.RC003_ID, device_catalog.RC001_ID)

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
        allow_mode_cycle = config.voice_other_key_switch(self._config)
        return [
            option for option in options
            if allow_mode_cycle or option != _VOICE_MODE_CYCLE_LABEL
        ]

    primaryActionOptions = Property(
        list, _get_primary_action_options, constant=True
    )

    def _voice_action_displays(self, include_remote: bool) -> List[str]:
        """语音键在按键页动作列表里的显示名。

        遥控器麦克风来源的语音键只能挂到遥控器的语音键上，别的按键不列
        出来；本机麦克风来源的语音键才出现在所有按键的可选列表里。
        已在语音键配置里关掉的语音键不再出现，避免关掉还能继续选。
        """

        labels = config.voice_key_labels(self._config)
        sources = {
            int(entry["id"]): entry["source"]
            for entry in config.voice_key_configs(self._config)
        }
        enabled = {
            int(entry["id"]): bool(entry.get("enabled", True))
            for entry in config.voice_key_configs(self._config)
        }
        return [
            label
            for slot, label in labels.items()
            if enabled.get(slot, True)
            and (
                sources.get(slot) != config.VOICE_INPUT_SOURCE_REMOTE
                or include_remote
            )
        ]

    @Slot(str, result=list)
    def primaryActionOptionsFor(self, button_id: str) -> List[str]:
        options = list(_ORDINARY_PRIMARY_ACTION_OPTIONS)
        if not button_id:
            return options
        voice_options = self._voice_action_displays(button_id == "mic")
        return _with_openmexia_options(
            self._mapping_options_available_now(
                list(dict.fromkeys(voice_options + options))
            )
        )

    @Slot(str, result=str)
    def actionOptionGroupTitle(self, option: str) -> str:
        return (
            settings_ui.ACTION_OPTION_GROUP_BY_LABEL.get(option, "")
            or settings_ui.openmexia_action_group_title(option)
        )

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
        return _with_openmexia_options(
            self._mapping_options_available_now(_SECONDARY_ACTION_OPTIONS)
        )

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

    # The voice page renders the same library, but only whole-machine
    # profiles: application layouts are imported/exported from the buttons
    # page instead.
    voiceProfiles = Property(
        list,
        lambda self: [row for row in self._mapping_preset_rows
                      if row["scope"] != "app"],
        notify=mappingPresetsChanged)

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
            self._app_profile_store().set_status(
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

    @Slot()
    def refreshMappingApps(self) -> None:
        """Rebuild the button page's application bar.

        Kept separate from refreshVoiceProfiles() so a failure while
        building the preset list can no longer leave the app bar empty.
        """
        self._refresh_mapping_apps()

    def _refresh_mapping_presets(self) -> None:
        profile = self._selected_device_id()
        source, device = self._live_voice_fields()
        entries = self._app_profile_store().view(
            self._bindings, profile,
            voice_input_source=source, voice_local_input_device=device,
            voice_keys=config.voice_key_configs(self._config))
        self._mapping_preset_rows = [
            {key: entry[key] for key in ("id", "name", "automatic", "applied",
                                         "scope", "process", "hotkey",
                                         "voice_input_source",
                                         "voice_local_input_device",
                                         "voice_keys",
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
            entry = self._app_profile_store().get(identity)
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
            return self._app_profile_store().get(
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
            entries = self._app_profile_store().list_apps()
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
                # 插件自带的条目（OpenMexia）不能删，只能等插件断开时自动移除。
                "plugin": str(entry["process"]).casefold()
                    == openmexia_client.PROCESS_NAME,
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
                entry = self._app_profile_store().get(identity)
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
            store = self._app_profile_store()
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
            store = self._app_profile_store()
            entry = store.get(identity)
            if str(entry["process"]).casefold() == openmexia_client.PROCESS_NAME:
                raise ValueError(
                    "这是 OpenMexia 插件自带的应用程序，不能删除；断开插件后会自动移除。")
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

    @Slot(str, int, result="QVariantMap")
    def moveMappingAppProfile(self, identity: str, position: int) -> dict:
        """Reorder one application in the app bar (``position`` is 0-based)."""
        try:
            self._busy_for_app_edit()
            if self._mapping_dirty:
                self._run_mapping_auto_save()
                if self._mapping_dirty:
                    raise ValueError("按键映射尚未保存完成，请稍后重试。")
            self._app_profile_store().move_app(
                str(identity or ""), int(position))
            self._refresh_mapping_apps()
            return {"ok": True, "id": identity, "message": ""}
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
            store = self._app_profile_store()
            if identity:
                # An app layout stores only its own overrides, so export that
                # instead of the global-plus-app layout shown on screen.
                entry = store.get(identity)
                name = entry["name"]
                mapping = entry["mapping"]
                source = entry["voice_input_source"]
                device = entry["voice_local_input_device"]
                keys = entry.get(app_profiles.VOICE_KEYS_FIELD) or []
            else:
                name = "全局布局"
                mapping = app_profiles.snapshot(self._bindings, profile)
                keys = config.voice_key_configs(self._config)
            store.export_entry({"name": name, "mapping": mapping,
                                "voice_input_source": source,
                                "voice_local_input_device": device,
                                app_profiles.VOICE_KEYS_FIELD: keys},
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
            count = self._app_profile_store().export_library(
                Path(url.toLocalFile()),
                profile,
                app_profiles.snapshot(self._bindings, profile),
                voice_input_source=source,
                voice_local_input_device=device,
                voice_keys=config.voice_key_configs(self._config),
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
                name = f"「{self._app_profile_store().get(identity)['name']}」"
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
            store = self._app_profile_store()
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
                    voice_input_source=source, voice_local_input_device=device,
                    voice_keys=config.voice_key_configs(self._config))
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
                             "voice_local_input_device": device,
                             app_profiles.VOICE_KEYS_FIELD:
                                 config.voice_key_configs(self._config)}
                else:
                    entry = store.get(identity)
                if operation == "copy":
                    if entry["id"] == app_profiles.GLOBAL_ID:
                        identity = store.create(
                            text, entry["mapping"],
                            voice_input_source=entry["voice_input_source"],
                            voice_local_input_device=entry["voice_local_input_device"],
                            voice_keys=entry[app_profiles.VOICE_KEYS_FIELD])
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
                        voice_input_source=source, voice_local_input_device=device,
                        voice_keys=config.voice_key_configs(self._config))
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
                    keys = entry.get(app_profiles.VOICE_KEYS_FIELD) or []
                    conflict_buttons = app_profiles.voice_conflicts(
                        bindings, entry["voice_input_source"], profile,
                        plugin_slots=app_profiles.plugin_slot_ids(keys))
                    if conflict_buttons:
                        raise ValueError(app_profiles.voice_conflict_message(
                            conflict_buttons) + "，然后再应用该配置。")
                    if not self._save_mapping(replacement=bindings):
                        raise ValueError("未能确认配置保存成功，请检查按键页的保存提示并重试。")
                    message = "配置已应用；正在按住的按键结束后生效。"
                    switched = False
                    target = (entry["voice_input_source"],
                              entry["voice_local_input_device"])
                    if keys:
                        # 一条配置保存的是整份语音键快照：顺序、开关、来源与
                        # 快捷键一起恢复，镜像字段由归一化回写。
                        updates = {"voice_key_configs": keys}
                        head = keys[0]
                        switched = (str(head["source"]), str(head["device"])) != (
                            source, device)
                    elif target != (source, device):
                        updates = {"voice_input_source": target[0],
                                   "voice_local_input_device": target[1]}
                        if (target[0] == config.VOICE_INPUT_SOURCE_LOCAL
                                and not target[1]):
                            devices = local_microphone_windows.list_input_devices()
                            if devices:
                                updates["voice_local_input_device"] = devices[0]
                        switched = True
                    else:
                        updates = None
                    if updates is not None:
                        if not self._persist_voice_config_fields(updates):
                            raise ValueError(
                                "按键已应用，但语音设置未能保存，请在语音页重新选择。")
                    if switched:
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

    def _on_vokie_plugin_download_ready(self, url: str) -> None:
        self._report_external_target(
            shell_targets.open_external_target(url), self._VOICE_PAGE_INDEX
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
