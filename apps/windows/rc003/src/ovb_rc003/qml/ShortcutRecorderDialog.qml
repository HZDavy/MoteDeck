import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

// 共享的快捷键录入二级弹窗：按键录入 / 手动输入两种模式。
// 参数校验、录入提示和快捷键规范化都在这里完成；调用方只需在
// chordAccepted 里把规范化后的短字符串写回自己的位置。
Dialog {
    id: recorder
    objectName: "shortcutRecorderDialog"
    property var tokens
    readonly property string headerText: qsTr("录入快捷键")
    property string previewText: ""
    property bool pendingClose: false
    property string pendingChord: ""
    property string inputMode: "capture"
    property string manualErrorText: ""

    signal chordAccepted(string chord)

    parent: Overlay.overlay
    modal: true
    popupType: Popup.Item
    anchors.centerIn: parent
    width: Math.min(430, parent.width - 28)
    title: headerText
    standardButtons: Dialog.NoButton
    leftPadding: recorder.tokens.dialogPadding
    rightPadding: recorder.tokens.dialogPadding
    topPadding: 0
    bottomPadding: recorder.tokens.dialogPadding
    leftInset: 0
    rightInset: 0
    topInset: 0
    bottomInset: 0
    closePolicy: Popup.NoAutoClose

    // 每次打开都重置为“按键录入”，调用方先把上下文（哪一行、哪个
    // 语音槽）记在自己的属性里，再调用它。
    function openRecorder() {
        pendingClose = false
        pendingChord = ""
        inputMode = "capture"
        manualErrorText = ""
        manualShortcutField.text = ""
        open()
    }

    function beginCapture() {
        previewText = qsTr("请按下希望遥控器发送的键盘快捷键")
        captureArea.forceActiveFocus()
        const promptBeforeStart = previewText
        if (!SettingsController.startMappingHotkeyCapture()
                && previewText === promptBeforeStart) {
            previewText = qsTr("无法开始录入，请结束其它按键操作后重试")
        }
    }

    function selectCaptureMode() {
        if (inputMode === "capture")
            return
        inputMode = "capture"
        manualErrorText = ""
        beginCapture()
    }

    function selectManualMode() {
        if (inputMode === "manual")
            return
        if (SettingsController.hotkeyCaptureActive
                && !SettingsController.stopHotkeyCapture()) {
            previewText = qsTr("无法停止快捷键录入，请重试")
            return
        }
        inputMode = "manual"
        manualErrorText = ""
        Qt.callLater(function() {
            if (!SettingsController.hotkeyCaptureActive)
                manualShortcutField.forceActiveFocus()
        })
    }

    function commitManualShortcut() {
        const result = SettingsController.normalizeMappingHotkeyText(
            manualShortcutField.text
        )
        if (!result || !result.ok) {
            manualErrorText = result && result.message
                ? result.message : qsTr("快捷键格式无效")
            manualShortcutField.forceActiveFocus()
            return
        }
        manualErrorText = ""
        commitShortcut(result.text)
    }

    function commitShortcut(chord) {
        const result = SettingsController.normalizeMappingHotkeyText(chord)
        if (!result.ok) {
            pendingChord = ""
            selectManualMode()
            manualShortcutField.text = SettingsController.formatHotkeyText(chord)
            manualErrorText = result.message
            return
        }
        previewText = SettingsController.formatHotkeyText(result.text)
        pendingChord = result.text
        requestClose()
    }

    function finishClose() {
        const chord = pendingChord
        pendingChord = ""
        pendingClose = false
        close()
        if (chord.length > 0)
            recorder.chordAccepted(chord)
    }

    function requestClose() {
        pendingClose = true
        if (!SettingsController.hotkeyCaptureActive) {
            finishClose()
            return true
        }
        if (!SettingsController.stopHotkeyCapture()) {
            pendingClose = false
            previewText = qsTr("无法停止快捷键录入，请重试")
            return false
        }
        if (!SettingsController.hotkeyCaptureActive)
            finishClose()
        return true
    }

    onOpened: {
        pendingClose = false
        pendingChord = ""
        inputMode = "capture"
        manualErrorText = ""
        manualShortcutField.text = ""
        beginCapture()
    }

    onClosed: {
        if (SettingsController.hotkeyCaptureActive)
            SettingsController.stopHotkeyCapture()
        pendingClose = false
        pendingChord = ""
        manualErrorText = ""
    }

    background: Rectangle {
        radius: recorder.tokens.cornerRadiusLarge
        color: recorder.tokens.surface
        border.width: recorder.tokens.hairlineWidth
        border.color: recorder.tokens.border
    }

    header: Item {
        width: recorder.width
        implicitHeight: recorder.tokens.dialogTopPadding
            + recorder.tokens.dialogTitleHeight + recorder.tokens.dialogHeaderGap

        UiLabel {
            anchors.left: parent.left
            anchors.leftMargin: recorder.leftPadding
            y: recorder.tokens.dialogTopPadding
                + (recorder.tokens.dialogTitleHeight - height) / 2
            tokens: recorder.tokens
            kind: sectionTitleKind
            text: recorder.headerText
            font.pixelSize: recorder.tokens.fontSizeTitle
            font.weight: Font.Medium
        }

        DialogCloseButton {
            id: shortcutRecorderCloseButton
            objectName: "shortcutRecorderCloseButton"
            tokens: recorder.tokens
            anchors.right: parent.right
            anchors.rightMargin: recorder.rightPadding
                - recorder.tokens.dialogCloseButtonOutset
            y: recorder.tokens.dialogTopPadding
                + (recorder.tokens.dialogTitleHeight - height) / 2
            onCloseRequested: recorder.requestClose()
        }
    }

    Connections {
        target: SettingsController
        function onHotkeyCaptured(chord) {
            if (recorder.visible && recorder.inputMode === "capture")
                recorder.commitShortcut(chord)
        }
        function onHotkeyCaptureError(message) {
            if (recorder.visible && recorder.inputMode === "capture") {
                recorder.previewText = message
                recorder.pendingClose = false
            }
        }
        function onHotkeyCaptureActiveChanged() {
            if (recorder.visible
                    && recorder.pendingClose
                    && !SettingsController.hotkeyCaptureActive) {
                recorder.finishClose()
            } else if (recorder.visible
                    && recorder.inputMode === "manual"
                    && !SettingsController.hotkeyCaptureActive) {
                Qt.callLater(function() {
                    manualShortcutField.forceActiveFocus()
                })
            }
        }
    }

    contentItem: FocusScope {
        id: captureArea
        implicitHeight: 164
        focus: true
        Keys.onEscapePressed: recorder.requestClose()

        ColumnLayout {
            anchors.fill: parent
            spacing: recorder.tokens.spacingMedium

            RowLayout {
                Layout.fillWidth: true
                spacing: 0

                CompactButton {
                    id: captureModeButton
                    objectName: "shortcutRecorderCaptureModeButton"
                    tokens: recorder.tokens
                    Layout.fillWidth: true
                    checkable: true
                    checked: recorder.inputMode === "capture"
                    highlighted: checked
                    enabled: recorder.inputMode === "capture"
                        || !SettingsController.hotkeyCaptureActive
                    text: qsTr("按键录入")
                    onClicked: recorder.selectCaptureMode()
                }
                CompactButton {
                    id: manualModeButton
                    objectName: "shortcutRecorderManualModeButton"
                    tokens: recorder.tokens
                    Layout.fillWidth: true
                    checkable: true
                    checked: recorder.inputMode === "manual"
                    highlighted: checked
                    text: qsTr("手动输入")
                    onClicked: recorder.selectManualMode()
                }
            }

            ColumnLayout {
                visible: recorder.inputMode === "capture"
                Layout.fillWidth: true
                spacing: recorder.tokens.spacingSmall

                UiLabel {
                    tokens: recorder.tokens
                    kind: sectionTitleKind
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: recorder.previewText
                    color: recorder.tokens.accent
                }
                UiLabel {
                    tokens: recorder.tokens
                    kind: noteKind
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: qsTr("按下要发送的单键或组合键")
                    elide: Text.ElideRight
                }
            }

            ColumnLayout {
                visible: recorder.inputMode === "manual"
                Layout.fillWidth: true
                spacing: recorder.tokens.spacingTiny

                CompactTextField {
                    id: manualShortcutField
                    objectName: "shortcutRecorderManualField"
                    tokens: recorder.tokens
                    Layout.fillWidth: true
                    enabled: !SettingsController.hotkeyCaptureActive
                    placeholderText: qsTr("例如 Ctrl+[、Win+L 或 左 Ctrl+←")
                    selectByMouse: true
                    onTextChanged: recorder.manualErrorText = ""
                    onAccepted: recorder.commitManualShortcut()
                    Accessible.name: qsTr("手动输入快捷键")
                }
                UiLabel {
                    tokens: recorder.tokens
                    kind: noteKind
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: recorder.manualErrorText.length > 0
                        ? recorder.manualErrorText
                        : qsTr("可直接修改键名；用 + 连接，确认时检查；单独 Win 按左 Win 发送")
                    color: recorder.manualErrorText.length > 0
                        ? recorder.tokens.errorColor
                        : recorder.tokens.textSecondary
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                CompactButton {
                    tokens: recorder.tokens
                    compactMinimumWidth: recorder.tokens.buttonWidth2Chars
                    text: qsTr("取消")
                    onClicked: recorder.requestClose()
                }
                CompactButton {
                    objectName: "shortcutRecorderManualConfirmButton"
                    tokens: recorder.tokens
                    visible: recorder.inputMode === "manual"
                    enabled: manualShortcutField.text.trim().length > 0
                        && !SettingsController.hotkeyCaptureActive
                    compactMinimumWidth: recorder.tokens.buttonWidth2Chars
                    text: qsTr("确定")
                    highlighted: true
                    onClicked: recorder.commitManualShortcut()
                }
            }
        }
    }
}
