import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Qt.labs.platform as Platform
import OvbRc003Settings 1.0

ApplicationWindow {
    id: window
    title: SettingsController.applicationPresentationLabel
    width: 880
    height: 660
    minimumWidth: 880
    minimumHeight: 660
    visible: true
    color: tokens.background

    // 无边框窗口：标题区由 QML 绘制，拖动交给系统（startSystemMove），
    // 原生边框与圆角由 window_chrome_windows.py 处理。
    readonly property int titleBarHeight: tokens.titleBarHeight
    readonly property bool darkMode: tokens.darkMode
    property Tokens tokens: Tokens {}

    property bool initialDiagnosticsStarted: false
    property string applicationExitError: ""
    property string lifecycleErrorTitle: qsTr("操作未完成")
    property int pendingPageIndex: -1
    property bool pendingExitPrompt: false
    property bool pendingExitAutoSave: false
    property bool applicationExitInProgress: false
    property bool portableHidSetupPromptAttempted: false

    // 按键页拖拽导入的中间态：暗罩和提示卡分两层，
    // 卡片不跟着遮罩一起变透明，文字才清楚。
    readonly property bool mappingImportDragActive: mappingImportDropArea.containsDrag
    readonly property string mappingImportTargetName:
        buttonsPageLoader.item ? (buttonsPageLoader.item.mappingBundleTargetName || "") : ""

    function restoreWindow() {
        // 隐藏到托盘或最小化后都要能重新显示
        if (!window.visible || window.visibility === Window.Minimized)
            window.showNormal()
        window.raise()
        window.requestActivate()
        SettingsController.refreshBridgeState()
    }

    function toggleMaximized() {
        if (window.visibility === Window.Maximized)
            window.showNormal()
        else
            window.showMaximized()
    }

    function buttonsPage() {
        return buttonsPageLoader.status === Loader.Ready
            ? buttonsPageLoader.item : null
    }

    function voicePage() {
        return voicePageLoader.status === Loader.Ready
            ? voicePageLoader.item : null
    }

    function currentPageItem() {
        if (tabBar.currentIndex === 0)
            return devicePage
        if (tabBar.currentIndex === 1)
            return buttonsPage()
        return voicePage()
    }

    function currentPageFirstFocusItem() {
        const page = currentPageItem()
        return page ? page.firstFocusItem : null
    }

    function currentPageLastFocusItem() {
        const page = currentPageItem()
        return page ? page.lastFocusItem : null
    }

    function hasPendingMappingDraft() {
        const page = buttonsPage()
        return page ? page.hasPendingEditorDraft : false
    }

    function settleInputUiAfterStop() {
        const mappingPage = buttonsPage()
        if (mappingPage)
            mappingPage.settleInputUiAfterStop()
        const voice = voicePage()
        if (voice)
            voice.settleInputUiAfterStop()
    }

    function showLifecycleError(title, message) {
        restoreWindow()
        lifecycleErrorTitle = title
        applicationExitError = message
        exitFailedDialog.open()
    }

    function requestPage(index) {
        if (index < 0 || index > 2 || index === tabBar.currentIndex
                || SettingsController.settingsSaveBusy
                || unsavedExitDialog.exitCommitInProgress
                || window.applicationExitInProgress)
            return
        pendingExitPrompt = false
        pendingExitAutoSave = false
        if (SettingsController.inputCaptureInUse) {
            pendingPageIndex = index
            if (!SettingsController.stopInputCapture()) {
                pendingPageIndex = -1
                showLifecycleError(
                    qsTr("无法切换页面"),
                    qsTr("无法停止正在进行的按键录入或检测。"))
            }
            return
        }
        settleInputUiAfterStop()
        pendingPageIndex = -1
        tabBar.currentIndex = index
    }

    function openUnsavedExitPrompt() {
        settleInputUiAfterStop()
        pendingExitPrompt = false
        pendingExitAutoSave = false
        unsavedExitDialog.saveAttempted = false
        unsavedExitDialog.open()
    }

    function requestWindowHide() {
        SettingsController.prepareForWindowHide()
    }

    function beginApplicationExit() {
        window.applicationExitInProgress = true
        window.hide()
        SettingsController.requestApplicationExit()
    }

    function requestFullExit() {
        if (unsavedExitDialog.exitCommitInProgress
                || window.applicationExitInProgress)
            return
        if (SettingsController.settingsSaveBusy) {
            window.beginApplicationExit()
            return
        }
        const pendingDraft = hasPendingMappingDraft()
        const autoSaveBeforeExit =
            SettingsController.canAutoSaveMappingBeforeExit() && !pendingDraft
        if (SettingsController.settingsDirty || pendingDraft) {
            restoreWindow()
            pendingPageIndex = -1
            if (SettingsController.inputCaptureInUse) {
                pendingExitPrompt = !autoSaveBeforeExit
                pendingExitAutoSave = autoSaveBeforeExit
                if (!SettingsController.stopInputCapture()) {
                    pendingExitPrompt = false
                    pendingExitAutoSave = false
                    SettingsController.cancelPendingMaintenanceExit()
                    showLifecycleError(
                        qsTr("无法准备退出"),
                        qsTr("无法停止正在进行的按键录入或检测。"))
                }
            } else if (autoSaveBeforeExit) {
                saveAndExit()
            } else {
                openUnsavedExitPrompt()
            }
            return
        }
        window.beginApplicationExit()
    }

    function saveAndExit() {
        const page = buttonsPage()
        if (page && !page.commitPendingEditorDraft())
            return
        unsavedExitDialog.saveAttempted = true
        unsavedExitDialog.exitCommitInProgress = true
        window.applicationExitInProgress = true
        window.hide()
        SettingsController.saveSettingsAndExit()
    }

    function discardAndExit() {
        const page = buttonsPage()
        if (page)
            page.discardPendingEditorDraft()
        unsavedExitDialog.exitCommitInProgress = true
        unsavedExitDialog.close()
        window.beginApplicationExit()
    }

    function openHidHelperSetupPrompt() {
        if (!hidHelperSetupDialog.visible
                && !window.applicationExitInProgress)
            hidHelperSetupDialog.open()
    }

    function openHidHelperRemovalPrompt() {
        if (!hidHelperRemovalDialog.visible
                && !window.applicationExitInProgress)
            hidHelperRemovalDialog.open()
    }

    Component.onCompleted: {
        if (SettingsController.startHidden)
            window.hide()
        SettingsController.startBridgeOnApplicationStart()
        startupUpdateTimer.start()
    }

    Timer {
        id: startupUpdateTimer
        objectName: "startupUpdateTimer"
        interval: 5000
        repeat: false
        onTriggered: SettingsController.checkForApplicationUpdateOnStartup()
    }

    onClosing: function(close) {
        if (SettingsController.applicationExitConfirmed) {
            close.accepted = true
            return
        }
        close.accepted = false
        if (SettingsController.closeBehavior === "quit") {
            window.requestFullExit()
        } else {
            window.requestWindowHide()
        }
    }

    Connections {
        target: SettingsController
        function onWindowRestoreRequested() { window.restoreWindow() }
        function onMaintenanceExitRequested() { window.requestFullExit() }
        function onApplicationExitReady() { Qt.quit() }
        function onApplicationExitFailed(message) {
            window.applicationExitInProgress = false
            unsavedExitDialog.exitCommitInProgress = false
            if (unsavedExitDialog.visible)
                unsavedExitDialog.close()
            window.restoreWindow()
            window.lifecycleErrorTitle = qsTr("无法完全退出")
            window.applicationExitError = message
            exitFailedDialog.open()
        }
        function onSaveSettingsAndExitFinished(saved) {
            if (!saved) {
                window.applicationExitInProgress = false
                unsavedExitDialog.exitCommitInProgress = false
                unsavedExitDialog.saveAttempted = true
                window.restoreWindow()
                if (!unsavedExitDialog.visible)
                    unsavedExitDialog.open()
            }
        }
        function onInputCleanupReady() {
            window.settleInputUiAfterStop()
            if (window.pendingExitPrompt) {
                window.openUnsavedExitPrompt()
                return
            }
            if (window.pendingExitAutoSave) {
                window.pendingExitAutoSave = false
                window.saveAndExit()
                return
            }
            if (window.pendingPageIndex >= 0) {
                const index = window.pendingPageIndex
                window.pendingPageIndex = -1
                tabBar.currentIndex = index
            }
        }
        function onInputCleanupFailed(message) {
            const exiting = window.pendingExitPrompt || window.pendingExitAutoSave
            window.pendingExitPrompt = false
            window.pendingExitAutoSave = false
            window.pendingPageIndex = -1
            if (exiting)
                SettingsController.cancelPendingMaintenanceExit()
            window.showLifecycleError(
                exiting ? qsTr("无法准备退出") : qsTr("无法切换页面"),
                message)
        }
        function onWindowHideReady() {
            window.settleInputUiAfterStop()
            window.hide()
        }
        function onWindowHideFailed(message) {
            window.restoreWindow()
            window.lifecycleErrorTitle = qsTr("无法隐藏窗口")
            window.applicationExitError = message
            exitFailedDialog.open()
        }
        function onApplicationUpdateDialogRequested() {
            const returnFocus = window.activeFocusItem
            window.restoreWindow()
            applicationUpdateDialog.returnFocusItem = returnFocus
            applicationUpdateDialog.open()
        }
        function onApplicationUpdateNotificationRequested(message) {
            systemTrayIcon.showMessage(SettingsController.applicationDisplayName,
                message, Platform.SystemTrayIcon.Information)
        }
    }

    // 文件拖到窗口任意位置都接住，交给按键页处理（仅按键页启用）。
    DropArea {
        id: mappingImportDropArea
        objectName: "mappingImportDropArea"
        anchors.fill: parent
        enabled: tabBar.currentIndex === 1
            && buttonsPageLoader.item !== null
            && !SettingsController.configurationReadOnly
        onDropped: function(drop) {
            if (buttonsPageLoader.item)
                buttonsPageLoader.item.handleImportDrop(drop)
        }
    }

    // 拖文件进按键页时的整窗遮罩：铺满窗口，和弹窗那种压暗一致。
    Rectangle {
        id: mappingImportScrim
        objectName: "mappingImportScrim"
        anchors.fill: parent
        z: 90
        visible: opacity > 0
        color: "#000000"
        opacity: window.mappingImportDragActive ? 0.42 : 0
        Behavior on opacity {
            NumberAnimation { duration: 140; easing.type: Easing.OutCubic }
        }
    }

    // 提示卡单独一层，不继承遮罩的不透明度，保证文字清楚。
    Rectangle {
        id: mappingImportHint
        objectName: "mappingImportHint"
        anchors.centerIn: parent
        z: 91
        width: Math.min(Math.max(360, mappingImportHintColumn.implicitWidth + 48),
                        parent.width - 48)
        implicitHeight: mappingImportHintColumn.implicitHeight + 40
        radius: window.tokens.cornerRadiusCard
        color: window.tokens.surface
        border.width: window.tokens.hairlineWidth
        border.color: window.tokens.borderStrong
        visible: opacity > 0
        opacity: window.mappingImportDragActive ? 1 : 0
        // 轻轻弹一下，给"要接住文件"的反馈，不做整体放大。
        scale: window.mappingImportDragActive ? 1 : 0.95
        Behavior on opacity {
            NumberAnimation { duration: 140; easing.type: Easing.OutCubic }
        }
        Behavior on scale {
            NumberAnimation {
                duration: 180
                easing.type: Easing.OutBack
                easing.overshoot: 1.3
            }
        }

        ColumnLayout {
            id: mappingImportHintColumn
            anchors.centerIn: parent
            width: Math.min(implicitWidth, parent.width - 48)
            spacing: window.tokens.spacingTiny

            UiLabel {
                objectName: "mappingImportHintTitle"
                tokens: window.tokens
                kind: sectionTitleKind
                Layout.alignment: Qt.AlignHCenter
                color: window.tokens.textPrimary
                text: qsTr("松手导入按键配置")
            }
            UiLabel {
                objectName: "mappingImportHintNote"
                tokens: window.tokens
                kind: bodyKind
                Layout.alignment: Qt.AlignHCenter
                wrapMode: Text.NoWrap
                color: window.tokens.textSecondary
                text: qsTr("将覆盖「%1」当前的按键映射")
                    .arg(window.mappingImportTargetName)
            }
        }
    }

    SettingsDialog {
        id: applicationUpdateDialog
        tokens: window.tokens
        objectName: "applicationUpdateDialog"
        property var returnFocusItem: null
        anchors.centerIn: parent
        modal: true
        popupType: Popup.Item
        title: SettingsController.applicationUpdateState === "available"
            ? qsTr("发现新版本")
            : SettingsController.applicationUpdateState === "downloading"
                ? qsTr("正在下载更新")
                : SettingsController.applicationUpdateState === "downloaded"
                    ? qsTr("更新包已下载")
                    : SettingsController.applicationUpdateState === "download_error"
                        ? qsTr("下载更新失败")
                    : SettingsController.applicationUpdateState === "local_newer"
                        ? qsTr("当前版本较新")
                        : SettingsController.applicationUpdateState === "current"
                            ? qsTr("已是最新版") : qsTr("检查更新失败")
        standardButtons: Dialog.NoButton
        dismissalEnabled: !SettingsController.applicationUpdateDownloadBusy
        width: Math.min(500, window.width - 32)
        onClosed: {
            const target = returnFocusItem
            returnFocusItem = null
            if (target)
                Qt.callLater(function() {
                    Qt.callLater(function() {
                        if (target.visible && target.enabled)
                            target.forceActiveFocus(Qt.TabFocusReason)
                    })
                })
        }

        contentItem: ColumnLayout {
            spacing: window.tokens.spacingMedium

            UiLabel {
                objectName: "applicationUpdateMessage"
                tokens: window.tokens
                kind: bodyKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: SettingsController.applicationUpdateMessage
                color: SettingsController.applicationUpdateState === "check_error"
                    || SettingsController.applicationUpdateState === "download_error"
                    ? window.tokens.errorColor : window.tokens.textPrimary
            }

            UiLabel {
                objectName: "applicationUpdateVersionSummary"
                visible: SettingsController.applicationUpdateLatestVersion.length > 0
                tokens: window.tokens
                kind: noteKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("当前版本：%1\nGitHub 版本：%2")
                    .arg(SettingsController.applicationVersion)
                    .arg(SettingsController.applicationUpdateLatestVersion)
            }

            UiLabel {
                objectName: "applicationUpdatePackageSummary"
                visible: SettingsController.applicationUpdatePackageSize > 0
                    && (SettingsController.applicationUpdateState === "available"
                        || SettingsController.applicationUpdateState === "downloading"
                        || SettingsController.applicationUpdateState === "download_error"
                        || SettingsController.applicationUpdateState === "downloaded")
                tokens: window.tokens
                kind: noteKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("下载内容：%1，%2")
                    .arg(SettingsController.applicationUpdatePackageKindText)
                    .arg(SettingsController.applicationUpdatePackageSizeText)
            }

            UiLabel {
                visible: applicationUpdateNotes.visible
                tokens: window.tokens
                kind: noteKind
                Layout.fillWidth: true
                text: qsTr("更新说明")
                font.weight: Font.Medium
            }

            ScrollView {
                id: applicationUpdateNotes
                objectName: "applicationUpdateNotes"
                visible: SettingsController.applicationUpdateReleaseNotes.length > 0
                    && SettingsController.applicationUpdateState !== "check_error"
                Layout.fillWidth: true
                Layout.preferredHeight: 118
                clip: true

                TextArea {
                    width: applicationUpdateNotes.availableWidth
                    readOnly: true
                    selectByMouse: true
                    wrapMode: TextEdit.Wrap
                    textFormat: TextEdit.PlainText
                    text: SettingsController.applicationUpdateReleaseNotes
                    font.family: window.tokens.fontFamily
                    font.pixelSize: window.tokens.fontSizeSmall
                    color: window.tokens.textPrimary
                    background: Rectangle {
                        color: window.tokens.fieldBackground
                        border.width: window.tokens.hairlineWidth
                        border.color: window.tokens.border
                        radius: window.tokens.cornerRadiusControl
                    }
                }
            }

            ProgressBar {
                id: applicationUpdateProgress
                objectName: "applicationUpdateProgress"
                visible: SettingsController.applicationUpdateDownloadBusy
                    || SettingsController.applicationUpdateState === "downloaded"
                Layout.fillWidth: true
                from: 0
                to: 1
                value: SettingsController.applicationUpdateDownloadProgress
            }

            UiLabel {
                objectName: "applicationUpdateProgressText"
                visible: applicationUpdateProgress.visible
                tokens: window.tokens
                kind: noteKind
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignRight
                text: SettingsController.applicationUpdateDownloadProgressText
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: window.tokens.spacingSmall
                Item { Layout.fillWidth: true }

                CompactButton {
                    objectName: "closeApplicationUpdateButton"
                    visible: !SettingsController.applicationUpdateDownloadBusy
                    tokens: window.tokens
                    text: SettingsController.applicationUpdateState === "available"
                        ? qsTr("稍后") : qsTr("关闭")
                    flat: SettingsController.applicationUpdateState === "available"
                    onClicked: applicationUpdateDialog.close()
                }

                CompactButton {
                    objectName: "openApplicationUpdateReleaseButton"
                    visible: !SettingsController.applicationUpdateDownloadBusy
                        && SettingsController.applicationUpdateState !== "downloaded"
                    tokens: window.tokens
                    text: qsTr("发布页")
                    onClicked: SettingsController.openApplicationUpdateRelease()
                }

                CompactButton {
                    objectName: "cancelApplicationUpdateDownloadButton"
                    visible: SettingsController.applicationUpdateDownloadBusy
                    tokens: window.tokens
                    text: qsTr("取消下载")
                    enabled: SettingsController.applicationUpdateMessage
                        !== qsTr("正在取消下载…")
                    onClicked: SettingsController.cancelApplicationUpdateDownload()
                }

                CompactButton {
                    objectName: "downloadApplicationUpdateButton"
                    visible: SettingsController.applicationUpdateCanDownload
                    tokens: window.tokens
                    text: SettingsController.applicationUpdateState === "download_error"
                        ? qsTr("重新下载") : qsTr("下载更新")
                    highlighted: true
                    onClicked: SettingsController.downloadApplicationUpdate()
                }

                CompactButton {
                    objectName: "openDownloadedApplicationUpdateButton"
                    visible: SettingsController.applicationUpdateState === "downloaded"
                    tokens: window.tokens
                    text: qsTr("打开桌面")
                    highlighted: true
                    onClicked: {
                        if (SettingsController.openDownloadedApplicationUpdate())
                            applicationUpdateDialog.close()
                    }
                }
            }
        }
    }

    SettingsDialog {
        id: hidHelperSetupDialog
        tokens: window.tokens
        objectName: "hidHelperSetupDialog"
        anchors.centerIn: parent
        modal: true
        popupType: Popup.Item
        title: SettingsController.hidHelperCleanupPending
            ? qsTr("完成权限清理？")
            : SettingsController.hidHelperSetupRequired
                ? qsTr("建议打开管理员按键权限")
                : qsTr("修复管理员按键权限？")
        standardButtons: Dialog.NoButton
        dismissalEnabled: !SettingsController.hidHelperRepairBusy
        width: Math.min(390, window.width - 32)

        contentItem: ColumnLayout {
            spacing: window.tokens.spacingLarge

            UiLabel {
                objectName: "hidHelperSetupBody"
                tokens: window.tokens
                kind: bodyKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: SettingsController.hidHelperCleanupPending
                    ? qsTr("自定义按键映射已可用。确认管理员权限，清理旧组件。")
                    : SettingsController.hidHelperSetupRequired
                        ? qsTr("用于自定义改键和遥控器语音键。\n\n打开时 Windows 会确认一次；成功后普通启动和自启动不再询问。现在不打开时，只保留 Windows 原始按键。")
                        : qsTr("修复后恢复自定义改键和遥控器语音键。Windows 会请求一次管理员确认。")
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: window.tokens.spacingSmall
                Item { Layout.fillWidth: true }
                CompactButton {
                    objectName: "cancelHidHelperSetupButton"
                    tokens: window.tokens
                    text: SettingsController.hidHelperCleanupPending
                        ? qsTr("取消")
                        : SettingsController.hidHelperSetupRequired
                            ? qsTr("不打开") : qsTr("取消")
                    flat: SettingsController.hidHelperSetupRequired
                    enabled: !SettingsController.hidHelperRepairBusy
                    onClicked: hidHelperSetupDialog.close()
                }
                CompactButton {
                    objectName: "confirmHidHelperSetupButton"
                    tokens: window.tokens
                    text: SettingsController.hidHelperCleanupPending
                        ? qsTr("清理")
                        : SettingsController.hidHelperSetupRequired
                            ? qsTr("打开") : qsTr("修复")
                    highlighted: true
                    enabled: !SettingsController.hidHelperRepairBusy
                    onClicked: {
                        hidHelperSetupDialog.close()
                        SettingsController.repairHidHelper()
                    }
                }
            }
        }
    }

    SettingsDialog {
        id: hidHelperRemovalDialog
        tokens: window.tokens
        objectName: "hidHelperRemovalDialog"
        anchors.centerIn: parent
        modal: true
        popupType: Popup.Item
        title: qsTr("移除按键映射权限？")
        standardButtons: Dialog.NoButton
        dismissalEnabled: !SettingsController.hidHelperRepairBusy
        width: Math.min(390, window.width - 32)

        contentItem: ColumnLayout {
            spacing: window.tokens.spacingLarge

            UiLabel {
                tokens: window.tokens
                kind: bodyKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("移除后，本机所有MoteDeck版本都不能使用自定义按键映射。")
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: window.tokens.spacingSmall
                Item { Layout.fillWidth: true }
                CompactButton {
                    objectName: "cancelHidHelperRemovalButton"
                    tokens: window.tokens
                    text: qsTr("取消")
                    enabled: !SettingsController.hidHelperRepairBusy
                    onClicked: hidHelperRemovalDialog.close()
                }
                CompactButton {
                    objectName: "confirmHidHelperRemovalButton"
                    tokens: window.tokens
                    text: qsTr("移除")
                    highlighted: true
                    enabled: !SettingsController.hidHelperRepairBusy
                    onClicked: {
                        hidHelperRemovalDialog.close()
                        SettingsController.removeHidHelper()
                    }
                }
            }
        }
    }

    SettingsDialog {
        id: unsavedExitDialog
        tokens: window.tokens
        objectName: "unsavedExitDialog"
        anchors.centerIn: parent
        modal: true
        popupType: Popup.Item
        title: saveAttempted
            ? qsTr("设置保存失败")
            : window.hasPendingMappingDraft()
                ? qsTr("按键编辑尚未完成") : qsTr("设置尚未保存")
        standardButtons: Dialog.NoButton
        property bool saveAttempted: false
        property bool exitCommitInProgress: false
        onClosed: {
            if (!exitCommitInProgress && !window.applicationExitInProgress)
                SettingsController.cancelPendingMaintenanceExit()
        }
        dismissalEnabled: !SettingsController.settingsSaveBusy && !exitCommitInProgress
        width: Math.min(430, window.width - 32)

        contentItem: ColumnLayout {
            spacing: window.tokens.spacingLarge

            UiLabel {
                tokens: window.tokens
                kind: bodyKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: SettingsController.settingsSaveBusy
                    ? qsTr("正在保存设置，请稍候…")
                    : unsavedExitDialog.exitCommitInProgress
                        ? qsTr("设置已保存，正在完全退出…")
                        : unsavedExitDialog.saveAttempted
                            && SettingsController.errorMessage.length > 0
                            ? SettingsController.errorMessage
                            : qsTr("退出前要保存当前设置吗？")
                color: unsavedExitDialog.saveAttempted
                    && SettingsController.errorMessage.length > 0
                    && !SettingsController.settingsSaveBusy
                    && !unsavedExitDialog.exitCommitInProgress
                    ? window.tokens.errorColor : window.tokens.textPrimary
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: window.tokens.spacingSmall
                Item { Layout.fillWidth: true }
                CompactButton {
                    objectName: "cancelUnsavedExitButton"
                    tokens: window.tokens
                    text: qsTr("取消")
                    enabled: !SettingsController.settingsSaveBusy
                        && !unsavedExitDialog.exitCommitInProgress
                    onClicked: {
                        unsavedExitDialog.saveAttempted = false
                        unsavedExitDialog.close()
                    }
                }
                CompactButton {
                    objectName: "discardUnsavedExitButton"
                    tokens: window.tokens
                    text: qsTr("不保存并退出")
                    enabled: !SettingsController.settingsSaveBusy
                        && !unsavedExitDialog.exitCommitInProgress
                    onClicked: window.discardAndExit()
                }
                CompactButton {
                    objectName: "saveUnsavedExitButton"
                    tokens: window.tokens
                    text: SettingsController.settingsSaveBusy
                        ? qsTr("保存中…") : qsTr("保存并退出")
                    highlighted: true
                    enabled: !SettingsController.settingsSaveBusy
                        && !unsavedExitDialog.exitCommitInProgress
                    onClicked: window.saveAndExit()
                }
            }
        }
    }

    SettingsDialog {
        id: exitFailedDialog
        tokens: window.tokens
        preferredWidth: 360 + tokens.dialogPadding * 2
        objectName: "exitFailedDialog"
        anchors.centerIn: parent
        modal: true
        title: window.lifecycleErrorTitle
        standardButtons: Dialog.Ok
        Label {
            width: 360
            text: window.applicationExitError
            wrapMode: Text.Wrap
            color: window.tokens.textPrimary
        }
    }

    Platform.SystemTrayIcon {
        id: systemTrayIcon
        objectName: "systemTrayIcon"
        visible: true
        icon.source: SettingsController.trayIconSource
        tooltip: SettingsController.trayTooltip
        onMessageClicked: SettingsController.showApplicationUpdate()
        onActivated: function(reason) {
            if (reason !== Platform.SystemTrayIcon.Context)
                window.restoreWindow()
        }
        menu: Platform.Menu {
            Platform.MenuItem {
                text: qsTr("打开%1").arg(SettingsController.applicationDisplayName)
                onTriggered: window.restoreWindow()
            }
            Platform.MenuSeparator {}
            Platform.MenuItem {
                text: qsTr("完全退出")
                onTriggered: window.requestFullExit()
            }
        }
    }

    onFrameSwapped: {
        if (!initialDiagnosticsStarted) {
            initialDiagnosticsStarted = true
            DiagnosticsController.startInitialDiagnostics()
        }
        if (!portableHidSetupPromptAttempted && window.visible) {
            portableHidSetupPromptAttempted = true
            if (SettingsController.claimPortableHidSetupPrompt())
                window.openHidHelperSetupPrompt()
        }
    }
    font.family: tokens.fontFamily

    palette.window: tokens.background
    palette.windowText: tokens.textPrimary
    palette.button: tokens.surfaceMuted
    palette.buttonText: tokens.textPrimary
    palette.base: tokens.fieldBackground
    palette.text: tokens.textPrimary
    palette.highlight: tokens.accent
    palette.highlightedText: tokens.accentText

    Timer {
        id: bridgeStatusRefreshTimer
        objectName: "bridgeStatusRefreshTimer"
        interval: SettingsController.bridgeLaunchPhase === "saving"
            || SettingsController.bridgeLaunchPhase === "starting"
            || SettingsController.bridgeLaunchPhase === "waiting"
            || SettingsController.bridgeLaunchPhase === "reconnecting"
            ? 1000 : 2000
        repeat: true
        running: true
        onTriggered: SettingsController.refreshBridgeState()
    }

    Timer {
        id: windowRestoreRequestTimer
        objectName: "windowRestoreRequestTimer"
        interval: 150
        repeat: true
        running: true
        onTriggered: SettingsController.pollWindowRestoreRequest()
    }

    Timer {
        id: bridgeLaunchPollTimer
        objectName: "bridgeLaunchPollTimer"
        interval: 150
        repeat: true
        running: SettingsController.bridgeLaunchBusy
        onTriggered: SettingsController.pollBridgeLaunch()
    }

    onActiveChanged: {
        if (active) SettingsController.refreshBridgeState()
    }

    Item {
        id: tabBar
        objectName: "tabBar"
        visible: false
        property int currentIndex: 0
        onCurrentIndexChanged:
            SettingsController.activePageIndex = currentIndex
    }

    // ── 窗口外壳：左侧 72px 导航整列 + 上方 72px 标题区 ──
    Row {
        id: shell
        objectName: "clientShell"
        anchors.fill: parent
        spacing: 0

        Rectangle {
            id: navigationBar
            objectName: "navigationBar"
            width: window.tokens.navigationWidth
            height: parent.height
            color: window.tokens.sidebar
            enabled: !SettingsController.settingsSaveBusy
                && !unsavedExitDialog.exitCommitInProgress
                && !window.applicationExitInProgress

            Rectangle {
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: window.tokens.structuralDividerWidth
                color: window.tokens.border
            }

            // 产品标识：黑底圆角方块 + 白色遥控器，与安装包图标一致
            Rectangle {
                id: appLogo
                objectName: "appLogo"
                x: 24
                y: 24
                width: 24
                height: 24
                // 稿子 64 画布：rx16，主体 18×42 rx9，圆点 r4.5 距顶 12；按 24/64 缩放
                radius: 6
                color: window.tokens.accent
                antialiasing: true

                Rectangle {
                    width: 6.75
                    height: 15.75
                    radius: 3.375
                    anchors.centerIn: parent
                    rotation: 45
                    antialiasing: true
                    color: window.tokens.accentText

                    Rectangle {
                        width: 3.375
                        height: 3.375
                        radius: 1.6875
                        anchors.horizontalCenter: parent.horizontalCenter
                        y: 4.5 - 1.6875
                        antialiasing: true
                        color: window.tokens.accent
                    }
                }
            }

            Column {
                id: navigationItems
                anchors.top: parent.top
                anchors.topMargin: 64
                anchors.left: parent.left
                anchors.right: parent.right
                spacing: 4

                NavButton {
                    id: deviceTabButton
                    objectName: "deviceTabButton"
                    tokens: window.tokens
                    text: qsTr("设备")
                    iconName: "link"
                    checked: tabBar.currentIndex === 0
                    onPressed: window.requestPage(0)
                    Accessible.name: text
                    KeyNavigation.tab: mappingTabButton
                }
                NavButton {
                    id: mappingTabButton
                    objectName: "mappingTabButton"
                    tokens: window.tokens
                    text: qsTr("按键")
                    iconName: "keys"
                    checked: tabBar.currentIndex === 1
                    onPressed: window.requestPage(1)
                    Accessible.name: text
                    KeyNavigation.tab: voiceTabButton
                    KeyNavigation.backtab: deviceTabButton
                }
                NavButton {
                    id: voiceTabButton
                    objectName: "voiceTabButton"
                    tokens: window.tokens
                    text: qsTr("语音")
                    iconName: "mic"
                    checked: tabBar.currentIndex === 2
                    onPressed: window.requestPage(2)
                    Accessible.name: text
                    KeyNavigation.backtab: mappingTabButton
                }
            }

            // 共享选中竖条：切换页面时上下滑动到对应导航项（上下各留 12）
            Rectangle {
                id: navigationIndicator
                objectName: "navigationIndicator"
                readonly property Item target: [deviceTabButton, mappingTabButton, voiceTabButton][tabBar.currentIndex] || deviceTabButton
                x: 0
                y: navigationItems.y + target.y + 12
                width: 3
                height: target.height - 24
                topRightRadius: 3
                bottomRightRadius: 3
                color: window.tokens.accent
                Behavior on y { NumberAnimation { duration: 260; easing.type: Easing.OutCubic } }
            }

            BatteryGauge {
                id: remoteBatteryGauge
                objectName: "remoteBatteryGauge"
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.bottom: parent.bottom
                anchors.bottomMargin: 24
                tokens: window.tokens
                level: SettingsController.remoteBatteryLevel
                visible: SettingsController.isRc003Device
            }
        }

        Column {
            width: shell.width - window.tokens.navigationWidth
            height: shell.height
            spacing: 0

            // 自绘标题栏：系统标题栏已由 DWM 去掉
            Item {
                id: titleBar
                objectName: "titleBar"
                width: parent.width
                height: window.titleBarHeight

                // 品牌字标（矢量），尺寸与配色沿用原标题文字
                MoteDeckWordmark {
                    objectName: "titleWordmark"
                    anchors.left: parent.left
                    anchors.leftMargin: 20
                    anchors.verticalCenter: parent.verticalCenter
                    markHeight: 10.5
                    color: window.tokens.textSecondary
                    Accessible.name: SettingsController.applicationDisplayName
                }

                MouseArea {
                    objectName: "titleBarDragArea"
                    anchors.fill: parent
                    anchors.rightMargin: 3 * 34 + 8
                    acceptedButtons: Qt.LeftButton
                    onPressed: window.startSystemMove()
                    onDoubleClicked: window.toggleMaximized()
                }

                Row {
                    anchors.right: parent.right
                    anchors.rightMargin: 20
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 2

                    TitleBarButton {
                        objectName: "minimizeWindowButton"
                        tokens: window.tokens
                        kind: "minimize"
                        onClicked: window.showMinimized()
                        Accessible.name: qsTr("最小化")
                    }
                    TitleBarButton {
                        objectName: "maximizeWindowButton"
                        tokens: window.tokens
                        kind: window.visibility === Window.Maximized ? "restore" : "maximize"
                        onClicked: window.toggleMaximized()
                        Accessible.name: window.visibility === Window.Maximized
                            ? qsTr("还原") : qsTr("最大化")
                    }
                    TitleBarButton {
                        objectName: "closeWindowButton"
                        tokens: window.tokens
                        kind: "close"
                        onClicked: window.close()
                        Accessible.name: qsTr("关闭")
                    }
                }
            }

            // 内容区
            Item {
                id: contentArea
                objectName: "contentArea"
                width: parent.width
                height: parent.height - window.titleBarHeight

                UiLabel {
                    objectName: "configurationWarning"
                    tokens: window.tokens
                    kind: noteKind
                    anchors.top: parent.top
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.leftMargin: window.tokens.pageHorizontalPadding
                    anchors.rightMargin: window.tokens.pageRightPadding
                    anchors.topMargin: visible ? 12 : 0
                    visible: SettingsController.configurationReadOnly
                    text: SettingsController.configurationError
                    wrapMode: Text.WordWrap
                }

                // 全局反馈做成浮现的小胶囊，不占版面，也不再常驻：
                // 只有刚产生反馈时弹一次，切走页面立即收起、切回来不再重弹，
                // 宽度完全贴着文字走。
                Rectangle {
                    id: globalStatusBar
                    objectName: "globalStatusBar"
                    readonly property bool feedbackBelongsToCurrentPage:
                        SettingsController.feedbackPageIndex === tabBar.currentIndex
                    readonly property bool hasError:
                        feedbackBelongsToCurrentPage
                        && SettingsController.errorMessage.length > 0
                    readonly property bool hasDirtySettings:
                        tabBar.currentIndex === 1 && SettingsController.mappingDirty
                    readonly property bool hasMessage:
                        feedbackBelongsToCurrentPage
                        && SettingsController.statusMessage.length > 0
                    readonly property bool hasStatus:
                        hasError || hasDirtySettings || hasMessage
                    // 按键页“检测真实按键”的实时提示也走这个气泡，检测期间常驻
                    readonly property bool hasKeyDetection:
                        tabBar.currentIndex === 1 && SettingsController.keyDetectionActive
                    property bool active: false
                    z: 2
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.bottom: parent.bottom
                    // 与按键页底部那行工具按钮同一条水平中线：按钮行高 32、
                    // 卡在页面下边缘（下留白 24），中线即距内容区底部 40。
                    anchors.bottomMargin: tokens.pageBottomPadding + 16 - height / 2
                    width: Math.min(parent.width - 40, statusRow.implicitWidth + 22)
                    height: 26
                    radius: height / 2
                    visible: (active && hasStatus) || hasKeyDetection
                    color: hasError ? tokens.accentSoft : tokens.surface
                    border.width: 1
                    border.color: hasError ? tokens.errorColor : tokens.line

                    function reveal() {
                        if (!hasStatus)
                            return
                        active = true
                        if (!hasError)
                            statusDismissTimer.restart()
                    }
                    function dismiss() {
                        statusDismissTimer.stop()
                        active = false
                    }

                    // 反馈是“新发布”的才弹；只是切回页面不算新反馈
                    Connections {
                        target: SettingsController
                        function onStatusMessageChanged() {
                            globalStatusBar.reveal()
                        }
                        function onErrorMessageChanged() {
                            globalStatusBar.reveal()
                        }
                        function onMappingDirtyChanged() {
                            globalStatusBar.reveal()
                        }
                    }
                    Connections {
                        target: tabBar
                        function onCurrentIndexChanged() {
                            globalStatusBar.dismiss()
                        }
                    }

                    HoverHandler {
                        id: globalStatusHover
                        onHoveredChanged: {
                            if (hovered)
                                statusDismissTimer.stop()
                            else
                                statusDismissTimer.restart()
                        }
                    }
                    CompactToolTip {
                        tokens: window.tokens
                        active: globalStatusHover.hovered && globalStatusText.truncated
                        text: globalStatusText.text
                    }

                    Timer {
                        id: statusDismissTimer
                        interval: 5000
                        onTriggered: {
                            if (!globalStatusBar.hasError)
                                globalStatusBar.dismiss()
                        }
                    }

                    Row {
                        id: statusRow
                        anchors.centerIn: parent
                        spacing: 6

                        UiLabel {
                            id: globalStatusText
                            objectName: "globalStatusText"
                            tokens: window.tokens
                            kind: noteKind
                            width: Math.min(implicitWidth, 520)
                            // 默认 note 行高 1.32 会在字下方多留空，胶囊里看着偏上
                            lineHeight: 1
                            verticalAlignment: Text.AlignVCenter
                            // 气泡是短提示，不带句尾句号
                            text: (globalStatusBar.hasError
                                ? SettingsController.errorMessage
                                : globalStatusBar.hasKeyDetection
                                    ? (SettingsController.isRc003Device
                                        ? SettingsController.keyDetectionText
                                        : SettingsController.keyDetectionText + qsTr("；检测时不启动录音"))
                                : globalStatusBar.hasDirtySettings
                                    ? (globalStatusBar.hasMessage
                                        ? SettingsController.statusMessage
                                        : qsTr("按键映射正在等待自动保存"))
                                    : SettingsController.statusMessage).replace(/。+$/, "")
                            color: globalStatusBar.hasError
                                ? tokens.errorColor
                                : globalStatusBar.hasDirtySettings && !globalStatusBar.hasKeyDetection
                                    ? tokens.voiceAccent : tokens.textSecondary
                            elide: Text.ElideRight
                            maximumLineCount: 1
                        }

                        CompactLink {
                            objectName: "retryMappingAutoSaveButton"
                            tokens: window.tokens
                            visible: tabBar.currentIndex === 1
                                && SettingsController.mappingAutoSaveRetryAvailable
                            text: qsTr("重试保存")
                            onClicked: SettingsController.retryMappingAutoSave()
                        }
                    }
                }

                Item {
                    id: pageHost
                    anchors.top: parent.top
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    anchors.topMargin: window.tokens.pageVerticalPadding
                    anchors.leftMargin: window.tokens.pageHorizontalPadding
                    anchors.rightMargin: window.tokens.pageRightPadding
                    anchors.bottomMargin: window.tokens.pageBottomPadding

                    StackLayout {
                        id: pageStack
                        anchors.fill: parent
                        enabled: !SettingsController.settingsSaveBusy
                            && !unsavedExitDialog.exitCommitInProgress
                            && !window.applicationExitInProgress
                        currentIndex: tabBar.currentIndex

                        DevicePage {
                            id: devicePage
                            tokens: window.tokens
                            backTabTarget: voiceTabButton
                            tabTarget: deviceTabButton
                            onOpenButtonsRequested: window.requestPage(1)
                            onEnableHidHelperRequested: window.openHidHelperSetupPrompt()
                            onRemoveHidHelperRequested: window.openHidHelperRemovalPrompt()
                        }
                        Loader {
                            id: buttonsPageLoader
                            objectName: "buttonsPageLoader"
                            active: tabBar.currentIndex === 1 || status === Loader.Ready
                            sourceComponent: Component {
                                ButtonsPage {
                                    enabled: !SettingsController.configurationReadOnly
                                    tokens: window.tokens
                                    backTabTarget: voiceTabButton
                                    tabTarget: deviceTabButton
                                }
                            }
                        }
                        Loader {
                            id: voicePageLoader
                            objectName: "voicePageLoader"
                            active: tabBar.currentIndex === 2 || status === Loader.Ready
                            sourceComponent: Component {
                                VoicePage {
                                    enabled: !SettingsController.configurationReadOnly
                                    tokens: window.tokens
                                    backTabTarget: voiceTabButton
                                    tabTarget: deviceTabButton
                                    onOpenDeviceRequested: window.requestPage(0)
                                    onOpenButtonsRequested: window.requestPage(1)
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
