import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    signal openDeviceRequested()
    signal openButtonsRequested()
    property var tokens
    property var backTabTarget: null
    property var tabTarget: null
    readonly property var firstFocusItem: installVirtualAudioButton
    readonly property var lastFocusItem: testVbCableChannelButton
    // 语音快捷键只在语音页录入：这个槽号指向正在录入的那条语音键。
    property int voiceRecorderSlot: 1
    // Status line of the saved-profile list; the C++ actions return {ok,message}.
    property string voiceProfileMessage: ""
    // Name of the profile currently applied, shown by the compact section row.
    readonly property string activeVoiceProfileName: {
        const rows = SettingsController.voiceProfiles
        for (var i = 0; i < rows.length; i++) {
            if (rows[i].applied)
                return String(rows[i].name || "")
        }
        return ""
    }
    readonly property int settingsStateColumnWidth: 96
    readonly property int settingsActionColumnWidth: tokens.buttonWidth6Chars
    readonly property bool voiceHotkeyBusy: SettingsController.voiceHotkeyBusy
    readonly property bool configurationWriteBusy:
        DiagnosticsController.driverActionRunning
        || DiagnosticsController.vbCableTestRunning
        || SettingsController.bridgeLaunchBusy
        || SettingsController.endpointPreflightBusy
    readonly property bool voiceHotkeyRuntimeBusy:
        String(SettingsController.voiceRuntimeState) === "active"
        || String(SettingsController.voiceRuntimeState) === "mic_confirmed"
        || String(SettingsController.voiceRuntimeState) === "receiving_audio"
        || String(SettingsController.voiceRuntimeState) === "finishing"

    function checkResult(checkId) {
        const rows = DiagnosticsController.checkResults
        for (var i = 0; i < rows.length; i++) {
            if (String(rows[i].checkId) === checkId)
                return rows[i]
        }
        return null
    }

    function checkState(checkId) {
        if (DiagnosticsController.isRefreshing)
            return qsTr("正在检查")
        const row = checkResult(checkId)
        if (!row)
            return qsTr("未检查")
        if (row.status === "pass")
            return qsTr("正常")
        if (row.status === "manual")
            return qsTr("请手动验证")
        if (checkId === "vb_cable_endpoints")
            return String(row.detail || "").indexOf(qsTr("缺少")) >= 0
                ? qsTr("点击“安装音频”") : qsTr("点击“重新检查”")
        if (checkId === "output_endpoint")
            return qsTr("请重新选择端点")
        return qsTr("点击“重新检查”")
    }

    function checkColor(checkId) {
        const state = checkState(checkId)
        if (state === qsTr("正常"))
            return tokens.successColor
        if (state === qsTr("点击“安装音频”")
                || state === qsTr("点击“重新检查”")
                || state === qsTr("请重新选择端点"))
            return tokens.errorColor
        if (state === qsTr("正在检查") || state === qsTr("请手动验证"))
            return tokens.voiceAccent
        return tokens.disabledText
    }

    function checkDetail(checkId, fallback) {
        const row = checkResult(checkId)
        return row && String(row.detail).length > 0
            ? String(row.detail).trim().replace(/[。；;]+$/, "") : fallback
    }

    // One entry point for every saved-profile action so the status line and the
    // error reporting stay identical across buttons and dialogs.
    function runVoiceProfileAction(operation, identity, text) {
        const result = SettingsController.mappingPresetAction(
            operation, identity || "", text || "")
        voiceProfileMessage = String(result.message || "")
        return result
    }

    // 输入名称会打断 QML 绑定，保存后用它把值重新挂回模型。
    function voiceKeyConfigNameText(slotId) {
        const rows = SettingsController.voiceKeyConfigs
        for (var i = 0; i < rows.length; i++) {
            if (Number(rows[i].id) === Number(slotId))
                return String(rows[i].name || "")
        }
        return ""
    }

    SettingsDialog {
        id: driverConfirmDialog
        tokens: root.tokens
        preferredWidth: 390
        objectName: "driverConfirmDialog"
        title: qsTr("安装虚拟音频？")
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: DiagnosticsController.launchVbCableSetup()

        contentItem: UiLabel {
            tokens: root.tokens
            kind: bodyKind
            wrapMode: Text.WordWrap
            text: qsTr("将启动 VB-Audio 官方 VB-CABLE 安装程序并请求管理员权限。完成安装后需要重启电脑，再回到这里点击“选推荐端点”。")
        }
    }

    SettingsDialog {
        id: bridgeTestConfirmDialog
        tokens: root.tokens
        preferredWidth: 390
        objectName: "bridgeTestConfirmDialog"
        title: qsTr("临时停止遥控器服务？")
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: DiagnosticsController.testVbCableChannelWithBridgeRestart()

        contentItem: UiLabel {
            tokens: root.tokens
            kind: bodyKind
            wrapMode: Text.WordWrap
            text: qsTr("检查虚拟声卡时会发送一小段测试音，并临时停止遥控器服务，结束后自动恢复。此检查不测试语音识别，平时说话不用先点它。")
        }
    }

    // "保存 / 重命名 / 复制" all collect one name; the automatic mode tag is
    // appended by the store, so the field never asks for it.
    SettingsDialog {
        id: voiceProfileNameDialog
        objectName: "voiceProfileNameDialog"
        tokens: root.tokens
        preferredWidth: 420
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        property string operation: "save"
        property string targetId: ""
        property string suggestedName: ""
        title: operation === "rename"
            ? qsTr("重命名配置")
            : operation === "copy"
                ? qsTr("复制配置") : qsTr("保存为配置")
        onAboutToShow: {
            voiceProfileNameField.text = suggestedName
            Qt.callLater(voiceProfileNameField.forceActiveFocus)
        }
        onAccepted: root.runVoiceProfileAction(
            voiceProfileNameDialog.operation,
            voiceProfileNameDialog.targetId,
            voiceProfileNameField.text)

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingSmall
            UiLabel {
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("名字后面会自动加上模式标签：模式1，或模式2 + 麦克风名字。")
            }
            CompactTextField {
                id: voiceProfileNameField
                objectName: "voiceProfileNameField"
                tokens: root.tokens
                Layout.fillWidth: true
                placeholderText: qsTr("配置名称")
                Accessible.name: qsTr("配置名称")
                onAccepted: voiceProfileNameDialog.accept()
            }
        }
    }

    SettingsDialog {
        id: voiceProfileDeleteDialog
        objectName: "voiceProfileDeleteDialog"
        tokens: root.tokens
        preferredWidth: 420
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        title: qsTr("删除配置？")
        property string pendingProfileId: ""
        property string pendingProfileLabel: ""
        onAccepted: root.runVoiceProfileAction(
            "delete", voiceProfileDeleteDialog.pendingProfileId, "")

        contentItem: UiLabel {
            tokens: root.tokens
            kind: bodyKind
            wrapMode: Text.WordWrap
            text: qsTr("这会删除配置“%1”，删除后无法撤销。当前按键设置和其他配置不受影响。")
                .arg(voiceProfileDeleteDialog.pendingProfileLabel)
        }
    }

    FileDialog {
        id: voiceProfileExportDialog
        objectName: "voiceProfileExportFileDialog"
        title: qsTr("导出配置")
        fileMode: FileDialog.SaveFile
        defaultSuffix: "json"
        nameFilters: [qsTr("配置文件 (*.json)")]
        property string pendingProfileId: ""
        onAccepted: root.runVoiceProfileAction(
            "export", voiceProfileExportDialog.pendingProfileId, selectedFile.toString())
    }

    FileDialog {
        id: voiceProfileImportDialog
        objectName: "voiceProfileImportFileDialog"
        title: qsTr("导入配置")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("配置文件 (*.json)")]
        onAccepted: root.runVoiceProfileAction("import", "", selectedFile.toString())
    }

    FileDialog {
        id: voiceProfileExportAllDialog
        objectName: "voiceProfileExportAllFileDialog"
        title: qsTr("导出全部配置")
        fileMode: FileDialog.SaveFile
        defaultSuffix: "json"
        nameFilters: [qsTr("配置档案 (*.json)")]
        onAccepted: {
            const result = SettingsController.exportAllMappingPresets(
                selectedFile.toString())
            root.voiceProfileMessage = String(result.message || "")
        }
    }

    // One place to browse, apply and maintain every saved profile. Keeping the
    // cards here (instead of inline on the voice page) gives the row actions
    // enough width and keeps the settings page scannable.
    SettingsDialog {
        id: voiceProfileManagerDialog
        objectName: "voiceProfileManagerDialog"
        tokens: root.tokens
        preferredWidth: 680
        title: qsTr("配置档案")
        titleNote: qsTr("切换配置会同时切换按键映射、语音模式和麦克风")
        closeButtonObjectName: "voiceProfileManagerCloseButton"
        dismissalEnabled: !root.configurationWriteBusy
        // 每次打开都按磁盘上的最新状态重建列表，避免拿到启动时的空缓存。
        onOpened: SettingsController.refreshVoiceProfiles()

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingMedium

            ScrollView {
                id: voiceProfileScroll
                objectName: "voiceProfileListScroll"
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(360,
                    Math.max(76, voiceProfileList.implicitHeight))
                clip: true
                contentWidth: availableWidth
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                ScrollBar.vertical.policy: ScrollBar.AsNeeded

                ColumnLayout {
                    id: voiceProfileList
                    objectName: "voiceProfileList"
                    width: voiceProfileScroll.availableWidth
                    spacing: 0

                    Repeater {
                        objectName: "voiceProfileRepeater"
                        model: SettingsController.voiceProfiles

                        Rectangle {
                            id: profileRow
                            required property var modelData
                            readonly property bool automatic:
                                Boolean(modelData.automatic)
                            readonly property bool applied:
                                Boolean(modelData.applied)
                            readonly property string profileId: String(modelData.id)
                            readonly property string conflictText:
                                String(modelData.voice_conflict_text || "")
                            objectName: "voiceProfileRow_"
                                + (automatic ? "global" : String(index))
                            Layout.fillWidth: true
                            implicitHeight: profileColumn.implicitHeight + 12
                            radius: root.tokens.cornerRadiusControl
                            color: applied ? root.tokens.accentSoft : "transparent"

                            ColumnLayout {
                                id: profileColumn
                                anchors.fill: parent
                                anchors.leftMargin: 10
                                anchors.rightMargin: 10
                                anchors.topMargin: 6
                                anchors.bottomMargin: 6
                                spacing: root.tokens.spacingTiny

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: root.tokens.spacingSmall

                                    UiLabel {
                                        id: profileLabel
                                        objectName: "voiceProfileLabel"
                                        tokens: root.tokens
                                        kind: bodyKind
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 0
                                        text: String(profileRow.modelData.label || "")
                                        font.weight: Font.Medium
                                        elide: Text.ElideRight
                                        HoverHandler { id: profileLabelHover }
                                        CompactToolTip {
                                            tokens: root.tokens
                                            active: profileLabelHover.hovered
                                                && profileLabel.truncated
                                            text: String(profileRow.modelData.label || "")
                                        }
                                    }
                                    UiLabel {
                                        tokens: root.tokens
                                        kind: noteKind
                                        visible: profileRow.applied
                                        text: qsTr("当前使用")
                                        color: root.tokens.successColor
                                        font.weight: Font.Medium
                                    }
                                }

                                UiLabel {
                                    objectName: "voiceProfileConflictLabel"
                                    tokens: root.tokens
                                    kind: noteKind
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    visible: profileRow.conflictText.length > 0
                                    text: profileRow.conflictText
                                    color: root.tokens.errorColor
                                    wrapMode: Text.WordWrap
                                }

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: root.tokens.spacingSmall

                                    CompactButton {
                                        objectName: "applyVoiceProfileButton"
                                        tokens: root.tokens
                                        compactMinimumWidth: tokens.buttonWidth4Chars
                                        text: qsTr("应用此配置")
                                        enabled: !root.configurationWriteBusy
                                        onClicked: root.runVoiceProfileAction(
                                            "apply", profileRow.profileId, "")
                                    }
                                    CompactButton {
                                        objectName: "updateVoiceProfileButton"
                                        tokens: root.tokens
                                        compactMinimumWidth: tokens.buttonWidth4Chars
                                        visible: !profileRow.automatic
                                        text: qsTr("更新")
                                        enabled: !root.configurationWriteBusy
                                        onClicked: root.runVoiceProfileAction(
                                            "update", profileRow.profileId, "")
                                    }
                                    Item { Layout.fillWidth: true }
                                    CompactButton {
                                        objectName: "renameVoiceProfileButton"
                                        tokens: root.tokens
                                        compactMinimumWidth: tokens.buttonWidth4Chars
                                        visible: !profileRow.automatic
                                        text: qsTr("重命名")
                                        enabled: !root.configurationWriteBusy
                                        onClicked: {
                                            voiceProfileNameDialog.operation = "rename"
                                            voiceProfileNameDialog.targetId =
                                                profileRow.profileId
                                            voiceProfileNameDialog.suggestedName =
                                                String(profileRow.modelData.name || "")
                                            voiceProfileNameDialog.open()
                                        }
                                    }
                                    CompactButton {
                                        objectName: "copyVoiceProfileButton"
                                        tokens: root.tokens
                                        compactMinimumWidth: tokens.buttonWidth4Chars
                                        text: qsTr("复制")
                                        enabled: !root.configurationWriteBusy
                                        onClicked: {
                                            voiceProfileNameDialog.operation = "copy"
                                            voiceProfileNameDialog.targetId =
                                                profileRow.profileId
                                            voiceProfileNameDialog.suggestedName =
                                                profileRow.automatic
                                                    ? qsTr("全局配置副本")
                                                    : String(profileRow.modelData.name || "")
                                                        + qsTr(" 副本")
                                            voiceProfileNameDialog.open()
                                        }
                                    }
                                    CompactButton {
                                        objectName: "exportVoiceProfileButton"
                                        tokens: root.tokens
                                        compactMinimumWidth: tokens.buttonWidth4Chars
                                        text: qsTr("导出")
                                        enabled: !root.configurationWriteBusy
                                        onClicked: {
                                            voiceProfileExportDialog.pendingProfileId =
                                                profileRow.profileId
                                            voiceProfileExportDialog.open()
                                        }
                                    }
                                    CompactButton {
                                        objectName: "deleteVoiceProfileButton"
                                        tokens: root.tokens
                                        compactMinimumWidth: tokens.buttonWidth4Chars
                                        visible: !profileRow.automatic
                                        text: qsTr("删除")
                                        enabled: !root.configurationWriteBusy
                                        onClicked: {
                                            voiceProfileDeleteDialog.pendingProfileId =
                                                profileRow.profileId
                                            voiceProfileDeleteDialog.pendingProfileLabel =
                                                String(profileRow.modelData.label || "")
                                            voiceProfileDeleteDialog.open()
                                        }
                                    }
                                }
                            }

                            SettingsRowDivider {
                                tokens: root.tokens
                            }
                        }
                    }
                }
            }

            UiLabel {
                objectName: "voiceProfileDialogMessage"
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                visible: text.length > 0
                text: root.voiceProfileMessage
                textFormat: Text.PlainText
                wrapMode: Text.WrapAnywhere
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: root.tokens.spacingSmall

                CompactButton {
                    objectName: "saveVoiceProfileButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth4Chars
                    text: qsTr("保存当前为配置")
                    enabled: !root.configurationWriteBusy
                    onClicked: {
                        voiceProfileNameDialog.operation = "save"
                        voiceProfileNameDialog.targetId = ""
                        voiceProfileNameDialog.suggestedName = ""
                        voiceProfileNameDialog.open()
                    }
                }
                CompactButton {
                    objectName: "exportAllVoiceProfilesButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth4Chars
                    text: qsTr("导出全部")
                    enabled: !root.configurationWriteBusy
                    onClicked: voiceProfileExportAllDialog.open()
                }
                CompactButton {
                    objectName: "importVoiceProfileButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth4Chars
                    text: qsTr("导入配置")
                    enabled: !root.configurationWriteBusy
                    onClicked: voiceProfileImportDialog.open()
                }
                Item { Layout.fillWidth: true }
                CompactButton {
                    objectName: "closeVoiceProfileManagerButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth4Chars
                    text: qsTr("关闭")
                    onClicked: voiceProfileManagerDialog.reject()
                }
            }
        }
    }

    // 语音键列表：默认一条，可继续添加。每条都有自己的名称、快捷键和
    // 麦克风来源；命名会同步到按键页的动作列表。
    SettingsDialog {
        id: voiceKeyManagerDialog
        objectName: "voiceKeyManagerDialog"
        tokens: root.tokens
        preferredWidth: 760
        title: qsTr("语音键配置")
        titleNote: qsTr("每条语音键可单独设置名称、快捷键和麦克风")
        closeButtonObjectName: "voiceKeyManagerCloseButton"
        dismissalEnabled: !root.configurationWriteBusy

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingMedium

            ScrollView {
                id: voiceKeyScroll
                objectName: "voiceKeyListScroll"
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(380,
                    Math.max(96, voiceKeyList.implicitHeight))
                clip: true
                contentWidth: availableWidth
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                ScrollBar.vertical.policy: ScrollBar.AsNeeded

                ColumnLayout {
                    id: voiceKeyList
                    objectName: "voiceKeyList"
                    width: voiceKeyScroll.availableWidth
                    spacing: 0

                    Repeater {
                        id: voiceSlotRepeater
                        objectName: "voiceSlotHotkeyRepeater"
                        model: SettingsController.voiceKeyConfigs

                        Rectangle {
                            id: keyCard
                            required property var modelData
                            readonly property int slot: Number(modelData.id)
                            readonly property bool localSource:
                                String(modelData.source) === "local"
                            readonly property string slotHotkey: {
                                const entries = SettingsController.voiceSlotHotkeys
                                const value = entries[slot]
                                return value === undefined ? "" : String(value)
                            }

                            objectName: "voiceKeyConfigItem_" + slot
                            Layout.fillWidth: true
                            implicitHeight: keyCardColumn.implicitHeight + 20
                            radius: root.tokens.cornerRadiusControl
                            // 底色跟随主题：不写就是默认纯白，深色模式下会亮成白块。
                            color: root.tokens.surface

                            ColumnLayout {
                                id: keyCardColumn
                                anchors.fill: parent
                                anchors.leftMargin: 10
                                anchors.rightMargin: 10
                                anchors.topMargin: 10
                                anchors.bottomMargin: 10
                                spacing: root.tokens.spacingSmall

                                // 默认只占一行；只有选了“本地麦克风”才多出一行录音设备。
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: root.tokens.spacingSmall

                                    CompactTextField {
                                        id: keyNameField
                                        objectName: "voiceKeyNameField"
                                        tokens: root.tokens
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 0
                                        placeholderText: qsTr("名称，留空为 %1")
                                            .arg(String(keyCard.modelData.displayName))
                                        text: String(keyCard.modelData.name || "")
                                        enabled: !root.configurationWriteBusy
                                        Accessible.name: qsTr("语音键名称")
                                        onEditingFinished: {
                                            SettingsController.setVoiceKeyConfigName(
                                                keyCard.slot, text)
                                            text = Qt.binding(function() {
                                                return root.voiceKeyConfigNameText(
                                                    keyCard.slot)
                                            })
                                        }
                                    }

                                    CompactTextField {
                                        id: keyHotkeyField
                                        objectName: "voiceKeyHotkeyField"
                                        tokens: root.tokens
                                        Layout.preferredWidth: 152
                                        readOnly: true
                                        selectByMouse: false
                                        enabled: !root.voiceHotkeyBusy
                                            && !root.configurationWriteBusy
                                        text: keyCard.slotHotkey.length > 0
                                            ? SettingsController.formatHotkeyText(
                                                keyCard.slotHotkey)
                                            : ""
                                        placeholderText: qsTr("尚未录入")
                                        Accessible.name: qsTr("录入语音快捷键")
                                    }

                                    CompactButton {
                                        objectName: "recordVoiceHotkeyButton"
                                        tokens: root.tokens
                                        flat: true
                                        compactMinimumWidth: tokens.buttonWidth4Chars
                                        text: qsTr("录入")
                                        enabled: !root.voiceHotkeyBusy
                                            && !root.configurationWriteBusy
                                            && !root.voiceHotkeyRuntimeBusy
                                        onClicked: {
                                            root.voiceRecorderSlot = keyCard.slot
                                            voiceShortcutRecorder.openRecorder()
                                        }
                                    }

                                    UiLabel {
                                        tokens: root.tokens
                                        kind: noteKind
                                        text: qsTr("麦克风")
                                    }

                                    SelectionComboBox {
                                        objectName: "voiceKeySourceCombo"
                                        tokens: root.tokens
                                        Layout.preferredWidth: 128
                                        model: SettingsController.voiceSourceOptions
                                        currentIndex: Number(keyCard.modelData.sourceIndex)
                                        onActivated: SettingsController.setVoiceKeyConfigSourceIndex(
                                            keyCard.slot, index)
                                        enabled: !root.configurationWriteBusy
                                            && !root.voiceHotkeyBusy
                                        Accessible.name: qsTr("麦克风来源")
                                    }

                                    CompactButton {
                                        objectName: "removeVoiceKeyButton"
                                        tokens: root.tokens
                                        compactMinimumWidth: tokens.buttonWidth4Chars
                                        text: qsTr("删除")
                                        enabled: SettingsController.voiceKeyConfigs.length > 1
                                            && !root.configurationWriteBusy
                                        onClicked: SettingsController.removeVoiceKeyConfig(
                                            keyCard.slot)
                                    }
                                }

                                RowLayout {
                                    Layout.fillWidth: true
                                    visible: keyCard.localSource
                                    spacing: root.tokens.spacingSmall

                                    UiLabel {
                                        tokens: root.tokens
                                        kind: noteKind
                                        text: qsTr("录音设备")
                                    }

                                    SelectionComboBox {
                                        objectName: "voiceKeyDeviceCombo"
                                        tokens: root.tokens
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 0
                                        visible: SettingsController.localMicrophoneOptions.length > 0
                                        model: SettingsController.localMicrophoneOptions
                                        currentIndex: Number(keyCard.modelData.deviceIndex)
                                        onActivated: SettingsController.setVoiceKeyConfigDeviceIndex(
                                            keyCard.slot, index)
                                        enabled: !root.configurationWriteBusy
                                            && !root.voiceHotkeyBusy
                                        Accessible.name: qsTr("录音设备")
                                    }

                                    UiLabel {
                                        tokens: root.tokens
                                        kind: noteKind
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 0
                                        visible: SettingsController.localMicrophoneOptions.length === 0
                                        color: root.tokens.errorColor
                                        text: qsTr("未找到录音设备，插好后点“刷新”")
                                    }

                                    CompactButton {
                                        objectName: "refreshVoiceKeyDevicesButton"
                                        tokens: root.tokens
                                        flat: true
                                        text: qsTr("刷新")
                                        enabled: !root.configurationWriteBusy
                                        onClicked: SettingsController.refreshLocalMicrophones()
                                    }
                                }
                            }

                            SettingsRowDivider {
                                tokens: root.tokens
                            }
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: root.tokens.spacingSmall

                CompactButton {
                    objectName: "addVoiceKeyConfigButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth4Chars
                    text: qsTr("添加语音键")
                    enabled: SettingsController.canAddVoiceKeyConfig()
                        && !root.configurationWriteBusy
                    onClicked: SettingsController.addVoiceKeyConfig()
                }

                UiLabel {
                    objectName: "voiceKeyDialogMessage"
                    tokens: root.tokens
                    kind: noteKind
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    visible: text.length > 0
                    color: root.tokens.errorColor
                    text: ""
                    elide: Text.ElideRight
                }

                CompactButton {
                    objectName: "closeVoiceKeyManagerButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth4Chars
                    text: qsTr("关闭")
                    onClicked: voiceKeyManagerDialog.reject()
                }
            }
        }
    }

    // 语音快捷键复用按键页的二级录入弹窗：只有这里能把快捷键写给语音键。
    ShortcutRecorderDialog {
        id: voiceShortcutRecorder
        objectName: "voiceShortcutRecorderDialog"
        tokens: root.tokens
        onChordAccepted: function(chord) {
            SettingsController.setVoiceSlotHotkey(root.voiceRecorderSlot, chord)
        }
    }

    function settleInputUiAfterStop() {
        if (voiceShortcutRecorder.visible && !SettingsController.hotkeyCaptureActive)
            voiceShortcutRecorder.finishClose()
    }

    ScrollView {
        id: voiceScroll
        objectName: "voiceScroll"
        anchors.fill: parent
        anchors.bottomMargin: 48
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: ScrollBar.AsNeeded

        ColumnLayout {
            id: voicePageContent
            objectName: "voicePageContent"
            width: Math.max(0, voiceScroll.availableWidth)
            x: 0
            y: 0
            spacing: 0

            // ── 页面标题（稿里的 .head：40 高 + 20 下边距）──
            Item {
                id: voicePageHeader
                objectName: "voicePageHeader"
                Layout.fillWidth: true
                Layout.preferredHeight: 60

                RowLayout {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    height: 40
                    spacing: 12

                    Label {
                        objectName: "voicePageTitle"
                        text: qsTr("语音")
                        font.family: tokens.fontFamilyDisplay
                        font.pixelSize: tokens.fontSizePageTitle
                        font.weight: Font.DemiBold
                        color: tokens.textPrimary
                        Layout.alignment: Qt.AlignBaseline
                    }
                    Label {
                        objectName: "voicePageSubtitle"
                        text: qsTr("音频通路与语音键")
                        font.family: tokens.fontFamily
                        font.pixelSize: tokens.fontSizeBody
                        color: tokens.textTertiary
                        Layout.alignment: Qt.AlignBaseline
                        Layout.fillWidth: true
                    }
                }
            }

            // ── 稿里的 .cols：两列 1fr 1fr，gap 16，顶部对齐 ──
            RowLayout {
                Layout.fillWidth: true
                spacing: 16

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.preferredWidth: 0
                    Layout.alignment: Qt.AlignTop
                    spacing: 16

                    SectionFrame {
                        objectName: "audioPrerequisiteSection"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        titleText: qsTr("音频前置")
                        titleObjectName: "audioPrerequisiteSectionTitle"

                        InlineSettingsRow {
                            objectName: "virtualAudioRow"
                            tokens: root.tokens
                            stateColumnWidth: root.settingsStateColumnWidth
                            actionColumnWidth: root.settingsActionColumnWidth
                            titleText: qsTr("虚拟音频")
                            descriptionText: DiagnosticsController.driverErrorMessage.length > 0
                                ? DiagnosticsController.driverErrorMessage
                                : DiagnosticsController.driverInfoMessage.length > 0
                                    ? DiagnosticsController.driverInfoMessage
                                    : qsTr("安装后提供虚拟声卡")
                            stateText: DiagnosticsController.driverErrorMessage.length > 0
                                ? qsTr("点击“安装音频”")
                                : DiagnosticsController.driverStatusMessage.length > 0
                                    ? qsTr("已安装")
                                    : DiagnosticsController.driverInfoMessage.length > 0
                                        ? (DiagnosticsController.driverInfoMessage.indexOf(
                                            qsTr("正在")) >= 0
                                            ? qsTr("正在处理")
                                            : DiagnosticsController.driverInfoMessage.indexOf(
                                                qsTr("重启电脑")) >= 0
                                                ? qsTr("请重启电脑")
                                                : qsTr("点击“安装音频”"))
                                        : root.checkState("vb_cable_endpoints") === qsTr("正常")
                                            ? qsTr("已安装")
                                            : root.checkState("vb_cable_endpoints")
                            stateColor: DiagnosticsController.driverErrorMessage.length > 0
                                ? tokens.errorColor
                                : DiagnosticsController.driverStatusMessage.length > 0
                                    ? tokens.successColor
                                    : DiagnosticsController.driverInfoMessage.length > 0
                                        ? tokens.voiceAccent
                                        : root.checkColor("vb_cable_endpoints")

                            editorData: [
                                CompactButton {
                                    id: installVirtualAudioButton
                                    objectName: "installVirtualAudioButton"
                                    tokens: root.tokens
                                    flat: true
                                    text: qsTr("安装音频")
                                    enabled: !root.configurationWriteBusy
                                        && !root.voiceHotkeyBusy
                                    onClicked: driverConfirmDialog.open()
                                    KeyNavigation.backtab: root.backTabTarget
                                }
                            ]
                            CompactButton {
                                objectName: "applyVirtualAudioButton"
                                tokens: root.tokens
                                text: qsTr("选推荐端点")
                                highlighted: true
                                visible: SettingsController.recommendedEndpointIndex >= 0
                                    && SettingsController.selectedEndpointIndex
                                        !== SettingsController.recommendedEndpointIndex
                                enabled: !root.configurationWriteBusy
                                    && !root.voiceHotkeyBusy
                                onClicked: SettingsController.selectAndPersistOutputEndpointIndex(
                                    SettingsController.recommendedEndpointIndex
                                )
                            }
                        }

                        InlineSettingsRow {
                            objectName: "outputEndpointRow"
                            tokens: root.tokens
                            stateColumnWidth: root.settingsStateColumnWidth
                            actionColumnWidth: root.settingsActionColumnWidth
                            titleText: qsTr("输出端点")
                            descriptionText: qsTr("选择虚拟声卡输出")
                            // 稿里端点正常时不显示状态，只有出问题才亮 pill
                            statePillVisible: Qt.colorEqual(stateColor, tokens.errorColor)
                            stateText: root.checkState("output_endpoint")
                            stateColor: root.checkColor("output_endpoint")

                            editorData: [
                                SelectionComboBox {
                                    id: endpointCombo
                                    objectName: "endpointCombo"
                                    tokens: root.tokens
                                    recommendedIndex: SettingsController.recommendedEndpointIndex
                                    Layout.preferredWidth: 168
                                    model: SettingsController.endpointOptions
                                    currentIndex: SettingsController.selectedEndpointIndex
                                    onActivated: SettingsController.selectAndPersistOutputEndpointIndex(index)
                                    enabled: !root.configurationWriteBusy
                                        && !root.voiceHotkeyBusy
                                    Accessible.name: qsTr("输出端点")
                                }
                            ]
                        }

                        InlineSettingsRow {
                            objectName: "microphonePrivacyRow"
                            tokens: root.tokens
                            actionColumnWidth: root.settingsActionColumnWidth
                            titleText: qsTr("麦克风权限")
                            descriptionText: qsTr("允许语音程序访问麦克风")
                            showDivider: false

                            CompactButton {
                                objectName: "openMicrophonePrivacyButton"
                                tokens: root.tokens
                                flat: true
                                trailingIcon: "external"
                                text: qsTr("麦克风隐私")
                                onClicked: SettingsController.openMicrophonePrivacySettings()
                            }
                        }
                    }

                    SectionFrame {
                        objectName: "voiceProfileSection"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        titleText: qsTr("配置档案")
                        titleObjectName: "voiceProfileSectionTitle"

                        InlineSettingsRow {
                            objectName: "voiceProfileHintRow"
                            tokens: root.tokens
                            stateColumnWidth: 0
                            actionColumnWidth: root.settingsActionColumnWidth
                            titleText: qsTr("已保存配置")
                            descriptionText: root.activeVoiceProfileName.length > 0
                                ? qsTr("当前使用「%1」").arg(root.activeVoiceProfileName)
                                : qsTr("切换按键映射与语音模式")
                            showDivider: false

                            CompactButton {
                                objectName: "manageVoiceProfilesButton"
                                tokens: root.tokens
                                text: qsTr("管理配置…")
                                enabled: !root.configurationWriteBusy
                                onClicked: voiceProfileManagerDialog.open()
                            }
                        }

                        UiLabel {
                            objectName: "voiceProfileMessage"
                            tokens: root.tokens
                            kind: noteKind
                            Layout.fillWidth: true
                            Layout.bottomMargin: 12
                            visible: text.length > 0 && !voiceProfileManagerDialog.visible
                            text: root.voiceProfileMessage
                            textFormat: Text.PlainText
                            wrapMode: Text.WrapAnywhere
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.preferredWidth: 0
                    Layout.alignment: Qt.AlignTop
                    spacing: 16

                    SectionFrame {
                        objectName: "voiceKeySection"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        titleText: qsTr("语音键")
                        titleObjectName: "voiceKeySectionTitle"

                        // 两个短按行为可以同时打开：切换模式换槽位，自动双击
                        // 负责把被短按唤起的输入法面板收起来。
                        InlineSettingsRow {
                            objectName: "voiceCycleShortPressRow"
                            tokens: root.tokens
                            titleText: qsTr("短按切换模式")
                            descriptionText: qsTr("可适配部分输入法的双模式或多个输入法")
                            showDivider: true

                            CompactSwitch {
                                objectName: "voiceCycleShortPressSwitch"
                                tokens: root.tokens
                                checked: SettingsController.voiceCycleShortPress
                                enabled: !root.configurationWriteBusy
                                    && !root.voiceHotkeyBusy
                                Accessible.name: qsTr("短按切换模式")
                                onToggled: {
                                    if (checked !== SettingsController.voiceCycleShortPress)
                                        SettingsController.setVoiceCycleShortPress(checked)
                                }
                            }
                        }

                        InlineSettingsRow {
                            objectName: "voiceOtherKeySwitchRow"
                            tokens: root.tokens
                            titleText: qsTr("使用其他按键切换模式")
                            descriptionText: qsTr("在按键设置里指定切换键")
                            showDivider: true

                            CompactSwitch {
                                objectName: "voiceOtherKeySwitchToggle"
                                tokens: root.tokens
                                checked: SettingsController.voiceOtherKeySwitch
                                enabled: !root.configurationWriteBusy
                                    && !root.voiceHotkeyBusy
                                Accessible.name: qsTr("使用其他按键切换模式")
                                onToggled: {
                                    if (checked !== SettingsController.voiceOtherKeySwitch)
                                        SettingsController.setVoiceOtherKeySwitch(checked)
                                }
                            }
                        }

                        InlineSettingsRow {
                            objectName: "voiceShortPressDismissRow"
                            tokens: root.tokens
                            titleText: qsTr("短按自动双击")
                            descriptionText: qsTr("可屏蔽部分输入法的短按")
                            showDivider: true

                            CompactSwitch {
                                id: voiceShortPressDismissSwitch
                                objectName: "voiceShortPressDismissSwitch"
                                tokens: root.tokens
                                checked: SettingsController.voiceShortPressDismiss
                                enabled: !root.configurationWriteBusy
                                    && !root.voiceHotkeyBusy
                                Accessible.name: qsTr("短按自动双击关闭输入框")
                                onToggled: {
                                    if (checked !== SettingsController.voiceShortPressDismiss)
                                        SettingsController.setVoiceShortPressDismiss(checked)
                                }
                            }
                        }

                        InlineSettingsRow {
                            objectName: "voiceCycleToastRow"
                            tokens: root.tokens
                            titleText: qsTr("模式切换气泡")
                            descriptionText: qsTr("切换时显示模式名称")
                            showDivider: true

                            editorData: [
                                ChipGroup {
                                    objectName: "voiceCycleToastChips"
                                    tokens: root.tokens
                                    model: SettingsController.voiceCycleToastThemeOptions
                                    currentIndex: SettingsController.voiceCycleToastThemeIndex
                                    onActivated: SettingsController.setVoiceCycleToastThemeIndex(index)
                                }
                            ]
                        }

                        InlineSettingsRow {
                            objectName: "voiceKeyConfigRow"
                            tokens: root.tokens
                            titleText: qsTr("语音键配置")
                            descriptionText: qsTr("已配置 %1 个语音键")
                                .arg(SettingsController.voiceKeyConfigs.length)
                            showDivider: false

                            CompactButton {
                                objectName: "manageVoiceKeysButton"
                                tokens: root.tokens
                                text: qsTr("管理配置…")
                                enabled: !root.configurationWriteBusy
                                onClicked: voiceKeyManagerDialog.open()
                            }
                        }
                    }
                }
            }

            SectionFrame {
                Layout.fillWidth: true
                Layout.topMargin: 16
                objectName: "voiceTestSection"
                tokens: root.tokens
                horizontalPadding: 16
                titleText: qsTr("测试验证")
                titleObjectName: "voiceTestSectionTitle"

                InlineSettingsRow {
                    objectName: "soundChannelTestRow"
                    tokens: root.tokens
                    editorColumnVisible: DiagnosticsController.vbCableBridgeRecoveryNeeded
                    showDivider: false
                    titleText: qsTr("虚拟声卡")
                    descriptionText: DiagnosticsController.vbCableTestMessage.length > 0
                        ? DiagnosticsController.vbCableTestMessage
                        : qsTr("只检查声音通路")
                    descriptionObjectName: "soundChannelTestDescription"
                    stateText: DiagnosticsController.vbCableBridgeRecoveryNeeded
                        ? qsTr("点击“启动服务”")
                        : DiagnosticsController.vbCableTestRunning
                        ? qsTr("测试中")
                        : DiagnosticsController.vbCableTestStatus === "pass"
                            ? qsTr("正常")
                            : DiagnosticsController.vbCableTestStatus === "fail"
                                ? qsTr("检查未通过") : qsTr("就绪")
                    stateColor: DiagnosticsController.vbCableBridgeRecoveryNeeded
                        ? tokens.errorColor
                        : DiagnosticsController.vbCableTestRunning
                        ? tokens.voiceAccent
                        : DiagnosticsController.vbCableTestStatus === "pass"
                            ? tokens.successColor
                            : DiagnosticsController.vbCableTestStatus === "fail"
                                ? tokens.errorColor : tokens.textSecondary

                    editorData: [
                        CompactButton {
                            objectName: "recoverBridgeButton"
                            visible: DiagnosticsController.vbCableBridgeRecoveryNeeded
                            tokens: root.tokens
                            text: qsTr("启动服务")
                            highlighted: true
                            enabled: !root.configurationWriteBusy
                                && !root.voiceHotkeyBusy
                            onClicked: SettingsController.startBridge()
                        }
                    ]
                    CompactButton {
                        id: testVbCableChannelButton
                        objectName: "testVbCableChannelButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        text: DiagnosticsController.vbCableTestRunning
                            ? qsTr("检查中…") : qsTr("检查虚拟声卡")
                        enabled: !DiagnosticsController.isRefreshing
                            && !root.configurationWriteBusy
                            && !root.voiceHotkeyBusy
                        onClicked: SettingsController.bridgeRunning
                            ? bridgeTestConfirmDialog.open()
                            : DiagnosticsController.testVbCableChannel()
                        KeyNavigation.tab: root.tabTarget
                    }
                }
            }
        }
    }

    // ── 底部电平纹波：和稿子一样贴到窗口下边缘（负边距吃掉页面底部留白）──
    WaveRipple {
        objectName: "voiceLevelRipple"
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.bottomMargin: -root.tokens.pageBottomPadding
        // 稿子默认窗口尺寸下 .vu 实测 52px 高（9 行格子）。
        height: 52
        tokens: root.tokens
        level: SettingsController.microphoneLevel
        onVisibleChanged: {
            if (visible)
                SettingsController.startMicrophoneLevelMonitor()
            else
                SettingsController.stopMicrophoneLevelMonitor()
        }
        Component.onCompleted: {
            if (visible)
                SettingsController.startMicrophoneLevelMonitor()
        }
    }

}
