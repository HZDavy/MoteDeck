import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import QtQuick.Window
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
    // 虚拟声卡装好后「安装」变「卸载」：只认端点自检通过，启动安装程序本身不算装好。
    readonly property bool virtualAudioInstalled: {
        const row = root.checkResult("vb_cable_endpoints")
        return !!row && String(row.status) === "pass"
    }
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
                ? qsTr("点击“安装”") : qsTr("点击“重新检查”")
        if (checkId === "output_endpoint")
            return qsTr("请重新选择端点")
        return qsTr("点击“重新检查”")
    }

    function checkColor(checkId) {
        const state = checkState(checkId)
        if (state === qsTr("正常"))
            return tokens.successColor
        if (state === qsTr("点击“安装”")
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
        title: root.virtualAudioInstalled ? qsTr("卸载虚拟音频？") : qsTr("安装虚拟音频？")
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: DiagnosticsController.launchVbCableSetup()

        contentItem: UiLabel {
            tokens: root.tokens
            kind: bodyKind
            wrapMode: Text.WordWrap
            text: root.virtualAudioInstalled
                ? qsTr("将启动 VB-Audio 官方 VB-CABLE 程序并请求管理员权限；检测到已安装时，请在程序里点击“Remove Driver”完成卸载。卸载后需要重启电脑。")
                : qsTr("将启动 VB-Audio 官方 VB-CABLE 安装程序并请求管理员权限。完成安装后需要重启电脑，再回到这里在“输出端点”里选择 CABLE Input。")
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
        titleNote: qsTr("切换配置会同时切换按键映射、全部语音键（含顺序与开关）和语音模式")
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
                            required property int index
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
        preferredWidth: 880
        title: qsTr("语音键配置")
        titleNote: qsTr("可调整顺序、单独停用；插件提供的语音键只能改名")
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
                            readonly property bool entryEnabled:
                                modelData.enabled !== false
                            // 插件带来的语音键固定由插件维护：快捷键不能改、也不能删。
                            readonly property bool pluginLocked:
                                Boolean(modelData.locked)
                            readonly property bool shortPressResend:
                                modelData.shortPressResend === true
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
                                // 停用的语音键整条变淡，一眼能看出这台遥控器不会触发它。
                                opacity: keyCard.entryEnabled ? 1 : 0.5

                                // 默认只占一行；只有选了“本地麦克风”才多出一行录音设备。
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: root.tokens.spacingSmall

                                    CompactSwitch {
                                        objectName: "voiceKeyEnabledSwitch"
                                        tokens: root.tokens
                                        Layout.alignment: Qt.AlignVCenter
                                        checked: keyCard.entryEnabled
                                        enabled: !root.configurationWriteBusy
                                        Accessible.name: qsTr("启用这条语音键")
                                        onToggled: {
                                            if (checked !== keyCard.entryEnabled)
                                                SettingsController.setVoiceKeyConfigEnabled(
                                                    keyCard.slot, checked)
                                        }
                                    }

                                    CompactTextField {
                                        id: keyNameField
                                        objectName: "voiceKeyNameField"
                                        tokens: root.tokens
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 96
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
                                        Layout.preferredWidth: 140
                                        readOnly: true
                                        selectByMouse: false
                                        enabled: !root.voiceHotkeyBusy
                                            && !root.configurationWriteBusy
                                            && !keyCard.pluginLocked
                                        text: keyCard.slotHotkey.length > 0
                                            ? SettingsController.formatHotkeyText(
                                                keyCard.slotHotkey)
                                            : ""
                                        placeholderText: keyCard.pluginLocked
                                            ? qsTr("由插件提供")
                                            : qsTr("尚未录入")
                                        Accessible.name: qsTr("录入语音快捷键")

                                        // 插件语音键的说明跟在快捷键框后面同一行；
                                        // 挂在框内、不参与行布局，名称框和快捷键框才能
                                        // 和普通语音键保持对齐。横向居中于快捷键框和
                                        // 右侧调整顺序按钮之间的空白。
                                        UiLabel {
                                            objectName: "voiceKeyPluginNote"
                                            tokens: root.tokens
                                            kind: noteKind
                                            visible: keyCard.pluginLocked
                                            x: Math.max(
                                                keyHotkeyField.width
                                                    + root.tokens.spacingSmall,
                                                Math.round((keyMoveUpButton.x
                                                    - keyHotkeyField.x
                                                    + keyHotkeyField.width
                                                    - implicitWidth) / 2))
                                            anchors.verticalCenter: parent.verticalCenter
                                            text: qsTr("该按键来自Vokie，默认使用插件通信，不可编辑快捷键映射")
                                        }
                                    }

                                    CompactButton {
                                        objectName: "recordVoiceHotkeyButton"
                                        tokens: root.tokens
                                        flat: true
                                        compactMinimumWidth: tokens.buttonWidth4Chars
                                        text: qsTr("录入")
                                        // 插件语音键不录快捷键：控件保留占位，名称框才和其他行一样长。
                                        opacity: keyCard.pluginLocked ? 0 : 1
                                        enabled: !keyCard.pluginLocked
                                            && !root.voiceHotkeyBusy
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
                                        // 占位保留，名称框宽度和其他行保持一致。
                                        opacity: keyCard.pluginLocked ? 0 : 1
                                        text: qsTr("麦克风")
                                    }

                                    SelectionComboBox {
                                        objectName: "voiceKeySourceCombo"
                                        tokens: root.tokens
                                        Layout.preferredWidth: 120
                                        opacity: keyCard.pluginLocked ? 0 : 1
                                        model: SettingsController.voiceSourceOptions
                                        currentIndex: Number(keyCard.modelData.sourceIndex)
                                        onActivated: SettingsController.setVoiceKeyConfigSourceIndex(
                                            keyCard.slot, index)
                                        enabled: !keyCard.pluginLocked
                                            && !root.configurationWriteBusy
                                            && !root.voiceHotkeyBusy
                                        Accessible.name: qsTr("麦克风来源")
                                    }

                                    CompactButton {
                                        objectName: "removeVoiceKeyButton"
                                        tokens: root.tokens
                                        compactMinimumWidth: tokens.buttonWidth4Chars
                                        text: qsTr("删除")
                                        // 插件语音键不能删：隐藏按钮但保留占位。
                                        opacity: keyCard.pluginLocked ? 0 : 1
                                        enabled: !keyCard.pluginLocked
                                            && SettingsController.voiceKeyConfigs.length > 1
                                            && !root.configurationWriteBusy
                                        onClicked: SettingsController.removeVoiceKeyConfig(
                                            keyCard.slot)
                                    }

                                    CompactButton {
                                        objectName: "voiceKeyShortPressResendButton"
                                        tokens: root.tokens
                                        // 固定小方块：激活与否尺寸不变，整行不会位移。
                                        implicitWidth: 32
                                        Layout.preferredWidth: 32
                                        compactMinimumWidth: 32
                                        leftPadding: 4
                                        rightPadding: 4
                                        // 插件语音键的快捷键由插件接管，按钮不可用；
                                        // 保留占位，名称框和快捷键框才能和普通行对齐。
                                        opacity: keyCard.pluginLocked ? 0 : 1
                                        flat: !keyCard.shortPressResend
                                        highlighted: keyCard.shortPressResend
                                        enabled: !keyCard.pluginLocked
                                            && !root.configurationWriteBusy
                                            && !root.voiceHotkeyBusy
                                        Accessible.name: qsTr("短按延迟发送")
                                        onClicked: SettingsController.setVoiceKeyConfigShortPressResend(
                                            keyCard.slot, !keyCard.shortPressResend)

                                        // 两行两字排成小方块，尽量少占地儿。
                                        contentItem: Label {
                                            text: "延迟\n发送"
                                            textFormat: Text.PlainText
                                            horizontalAlignment: Text.AlignHCenter
                                            verticalAlignment: Text.AlignVCenter
                                            color: keyCard.shortPressResend
                                                ? root.tokens.accentText
                                                : root.tokens.textSecondary
                                            font.family: root.tokens.fontFamily
                                            font.pixelSize: root.tokens.fontSizeTiny
                                            font.weight: Font.Medium
                                        }
                                    }

                                    IconButton {
                                        id: keyMoveUpButton
                                        objectName: "voiceKeyMoveUpButton"
                                        tokens: root.tokens
                                        compact: true
                                        iconName: "chevronUp"
                                        enabled: keyCard.modelData.first !== true
                                            && !root.configurationWriteBusy
                                        Accessible.name: qsTr("上移")
                                        onClicked: SettingsController.moveVoiceKeyConfig(
                                            keyCard.slot, -1)
                                    }

                                    IconButton {
                                        objectName: "voiceKeyMoveDownButton"
                                        tokens: root.tokens
                                        compact: true
                                        iconName: "chevron"
                                        enabled: keyCard.modelData.last !== true
                                            && !root.configurationWriteBusy
                                        Accessible.name: qsTr("下移")
                                        onClicked: SettingsController.moveVoiceKeyConfig(
                                            keyCard.slot, 1)
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

            UiLabel {
                objectName: "voiceKeyHotkeyUniqueNote"
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("同时使用多个输入法时，快捷键不要有任何重复。")
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

    // 外部应用程序支持：两张卡片竖排、各占一整行，卡片右侧是官网 / 下载 / 连接三个操作。
    // 「访问官网」「下载插件」用直角弱化样式，「连接插件」才是圆角主按钮。
    SettingsDialog {
        id: externalAppsDialog
        objectName: "externalAppsDialog"
        tokens: root.tokens
        preferredWidth: 720
        title: qsTr("外部应用程序支持")
        titleNote: qsTr("连接后语音键会改用插件提供的语音模式")
        closeButtonObjectName: "closeExternalAppsButton"

        component PluginCard: Rectangle {
            id: card
            property string objectPrefix: ""
            property string appName: ""
            property string tagline: ""
            property string description: ""
            property string logoSource: ""
            property string statusText: ""
            property bool statusOk: false
            property color statusColor: root.tokens.textSecondary
            property string connectText: qsTr("连接插件")
            property bool connectHighlighted: true
            property bool connectEnabled: true
            signal openWebsiteRequested()
            signal downloadRequested()
            signal connectRequested()

            implicitHeight: cardRow.implicitHeight + 28
            radius: root.tokens.cornerRadiusCard
            color: root.tokens.surfaceMuted
            border.width: root.tokens.hairlineWidth
            border.color: cardHover.containsMouse
                ? root.tokens.borderStrong : root.tokens.border
            Behavior on border.color { ColorAnimation { duration: 80 } }

            // 卡片本身只做悬停反馈，点击不跳官网；跳转必须点「访问官网」。
            MouseArea {
                id: cardHover
                anchors.fill: parent
                hoverEnabled: true
            }

            RowLayout {
                id: cardRow
                anchors.fill: parent
                anchors.margins: 14
                spacing: 12

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    spacing: 8

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 10

                        // 深色圆角块放原版图形 logo，深浅主题下都能看清。
                        Rectangle {
                            Layout.alignment: Qt.AlignTop
                            width: 40
                            height: 40
                            radius: 10
                            color: "#141414"

                            Image {
                                anchors.centerIn: parent
                                source: card.logoSource
                                sourceSize.width: 24
                                sourceSize.height: 24
                                width: 22
                                height: 22
                                smooth: true
                                fillMode: Image.PreserveAspectFit
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.alignment: Qt.AlignVCenter
                            spacing: 2

                            Label {
                                text: card.appName
                                font.family: root.tokens.fontFamily
                                font.pixelSize: root.tokens.fontSizeSection
                                font.weight: Font.DemiBold
                                color: root.tokens.textPrimary
                            }

                            Label {
                                Layout.fillWidth: true
                                text: card.tagline
                                font.family: root.tokens.fontFamily
                                font.pixelSize: root.tokens.fontSizeSmall
                                color: root.tokens.textTertiary
                                elide: Text.ElideRight
                            }
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        text: card.description
                        font.family: root.tokens.fontFamily
                        font.pixelSize: root.tokens.fontSizeControl
                        lineHeight: 1.35
                        lineHeightMode: Text.ProportionalHeight
                        color: root.tokens.textSecondary
                        wrapMode: Text.WordWrap
                    }

                    StatusPill {
                        tokens: root.tokens
                        label: card.statusText
                        ok: card.statusOk
                        dotColor: card.statusOk ? "transparent" : card.statusColor
                    }
                }

                ColumnLayout {
                    Layout.alignment: Qt.AlignTop
                    // 三个动作按钮固定宽度：布局挤压时也不让文字被截成省略号。
                    Layout.minimumWidth: 140
                    Layout.preferredWidth: 140
                    Layout.maximumWidth: 140
                    spacing: 4

                    // 三个动作按钮靠右对齐，宽度按文字自适应，不铺满整列。
                    CompactButton {
                        objectName: card.objectPrefix + "WebsiteButton"
                        Layout.alignment: Qt.AlignRight
                        tokens: root.tokens
                        flat: true
                        radiusless: true
                        trailingIcon: "external"
                        text: qsTr("访问官网")
                        Accessible.name: card.appName + " " + qsTr("官网")
                        onClicked: card.openWebsiteRequested()
                    }

                    CompactButton {
                        objectName: card.objectPrefix + "DownloadButton"
                        Layout.alignment: Qt.AlignRight
                        tokens: root.tokens
                        flat: true
                        radiusless: true
                        text: qsTr("下载插件")
                        Accessible.name: card.appName + " " + qsTr("下载插件")
                        onClicked: card.downloadRequested()
                    }

                    CompactButton {
                        objectName: card.objectPrefix + "ConnectButton"
                        Layout.alignment: Qt.AlignRight
                        tokens: root.tokens
                        text: card.connectText
                        highlighted: card.connectHighlighted
                        enabled: card.connectEnabled
                        Accessible.name: card.appName + " " + card.connectText
                        onClicked: card.connectRequested()
                    }
                }
            }
        }

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingMedium

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 12

                PluginCard {
                    objectName: "vokiePluginCard"
                    Layout.fillWidth: true
                    objectPrefix: "vokie"
                    appName: "Vokie"
                    tagline: qsTr("AI 语音输入")
                    description: SettingsController.voicePluginConnected
                        ? qsTr("已接收 %1 个语音模式，语音键已由插件接管")
                            .arg(SettingsController.voicePluginModeCount)
                        : qsTr("语音输入、闪记、翻译输入与指令，都能用遥控器语音键触发")
                    logoSource: Qt.resolvedUrl("assets/vokie-mark.svg")
                    statusText: SettingsController.voicePluginConnected
                        ? qsTr("已连接")
                        : String(SettingsController.voicePluginState) === "listening"
                            ? qsTr("等待连接") : qsTr("未连接")
                    statusOk: SettingsController.voicePluginConnected
                    statusColor: SettingsController.voicePluginConnected
                        ? tokens.successColor
                        : String(SettingsController.voicePluginState) === "listening"
                            ? tokens.voiceAccent : tokens.textSecondary
                    connectText: SettingsController.voicePluginConnected
                        ? qsTr("断开连接")
                        : String(SettingsController.voicePluginState) === "listening"
                            ? qsTr("取消连接") : qsTr("连接插件")
                    connectHighlighted: !SettingsController.voicePluginConnected
                    onOpenWebsiteRequested: SettingsController.openVokieWebsite()
                    onDownloadRequested: SettingsController.openVokiePluginDownload()
                    onConnectRequested: {
                        if (SettingsController.voicePluginConnected
                                || String(SettingsController.voicePluginState) === "listening") {
                            SettingsController.disconnectVoicePlugin()
                        } else {
                            SettingsController.connectVoicePlugin()
                        }
                    }
                }

                PluginCard {
                    objectName: "openmexiaPluginCard"
                    Layout.fillWidth: true
                    objectPrefix: "openmexia"
                    appName: "OpenMexia"
                    tagline: qsTr("开源智能家居控制中枢")
                    description: qsTr("把遥控器变成家里的控制面板，目前支持米家设备")
                    logoSource: Qt.resolvedUrl("assets/openmexia-mark.svg")
                    statusText: qsTr("即将发布")
                    statusColor: tokens.textTertiary
                    connectText: qsTr("即将发布")
                    connectEnabled: false
                    onOpenWebsiteRequested: SettingsController.openOpenMexiaWebsite()
                    onDownloadRequested: SettingsController.openOpenMexiaDownload()
                }
            }

            UiLabel {
                objectName: "externalAppsNote"
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("连接后，当前语音键会替换为插件提供的语音模式；插件语音键的快捷键由插件自带，只能改名，不能删除也不能改快捷键。")
            }

            RowLayout {
                Layout.fillWidth: true

                Item { Layout.fillWidth: true }

                CompactButton {
                    objectName: "closeExternalAppsDialogButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth4Chars
                    text: qsTr("关闭")
                    onClicked: externalAppsDialog.reject()
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
                            descriptionText: DiagnosticsController.vbCableTestMessage.length > 0
                                ? DiagnosticsController.vbCableTestMessage
                                : DiagnosticsController.driverErrorMessage.length > 0
                                    ? DiagnosticsController.driverErrorMessage
                                    : DiagnosticsController.driverInfoMessage.length > 0
                                        ? DiagnosticsController.driverInfoMessage
                                        : qsTr("安装后提供虚拟声卡")
                            stateText: DiagnosticsController.driverErrorMessage.length > 0
                                ? qsTr("点击“安装”")
                                : DiagnosticsController.driverStatusMessage.length > 0
                                    ? qsTr("已安装")
                                    : DiagnosticsController.driverInfoMessage.length > 0
                                        ? (DiagnosticsController.driverInfoMessage.indexOf(
                                            qsTr("正在")) >= 0
                                            ? qsTr("正在处理")
                                            : DiagnosticsController.driverInfoMessage.indexOf(
                                                qsTr("重启电脑")) >= 0
                                                ? qsTr("请重启电脑")
                                                : qsTr("点击“安装”"))
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
                                    text: root.virtualAudioInstalled
                                        ? qsTr("卸载") : qsTr("安装")
                                    enabled: !root.configurationWriteBusy
                                        && !root.voiceHotkeyBusy
                                    onClicked: driverConfirmDialog.open()
                                    KeyNavigation.backtab: root.backTabTarget
                                },
                                CompactButton {
                                    id: testVbCableChannelButton
                                    objectName: "testVbCableChannelButton"
                                    tokens: root.tokens
                                    flat: true
                                    text: qsTr("测试")
                                    enabled: !DiagnosticsController.isRefreshing
                                        && !root.configurationWriteBusy
                                        && !root.voiceHotkeyBusy
                                    onClicked: SettingsController.bridgeRunning
                                        ? bridgeTestConfirmDialog.open()
                                        : DiagnosticsController.testVbCableChannel()
                                    KeyNavigation.tab: root.tabTarget
                                }
                            ]
                            CompactButton {
                                objectName: "recoverBridgeButton"
                                tokens: root.tokens
                                text: qsTr("启动服务")
                                highlighted: true
                                visible: DiagnosticsController.vbCableBridgeRecoveryNeeded
                                enabled: !root.configurationWriteBusy
                                    && !root.voiceHotkeyBusy
                                onClicked: SettingsController.startBridge()
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

                        // 两个短按行为可以同时打开：切换模式换槽位，每条语音键的
                        // “短按延迟发送”把唯一那一次按键推迟下发，避开输入法的
                        // 短按/长按竞争。
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
                            objectName: "voiceKeyTutorialRow"
                            tokens: root.tokens
                            actionColumnWidth: root.settingsActionColumnWidth
                            titleText: qsTr("语音键帮助")
                            descriptionText: qsTr("查看语音键录入、模式切换与延迟发送说明")
                            showDivider: true

                            // 与「麦克风隐私」入口保持同样的文字链接样式。
                            CompactButton {
                                objectName: "voiceKeyTutorialButton"
                                tokens: root.tokens
                                flat: true
                                trailingIcon: "external"
                                text: qsTr("查看教程")
                                Accessible.name: qsTr("打开语音键帮助")
                                onClicked: SettingsController.openVoiceKeyTutorial()
                            }
                        }

                        InlineSettingsRow {
                            objectName: "voiceCycleToastRow"
                            tokens: root.tokens
                            titleText: qsTr("模式切换气泡")
                            descriptionText: qsTr("切换时显示模式名称")
                            showDivider: true

                            editorData: [
                                IconButton {
                                    objectName: "voiceCycleToastAdjustButton"
                                    tokens: root.tokens
                                    iconName: "moveVertical"
                                    iconColor: root.tokens.darkMode
                                        ? root.tokens.textSecondary
                                        : "#9a9a9a"
                                    // 关闭气泡时没有可调的位置。
                                    enabled: SettingsController.voiceCycleToastThemeIndex
                                        !== SettingsController.voiceCycleToastOffIndex
                                    Accessible.name: qsTr("调整气泡位置")
                                    onClicked: voiceToastTuner.open()
                                },
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

            // 外部语音工具独占一整行：Vokie 插件连上后会把它的语音模式写进上面的语音键。
            SectionFrame {
                objectName: "externalAppsSection"
                tokens: root.tokens
                Layout.fillWidth: true
                Layout.topMargin: 16
                titleText: qsTr("外部应用程序支持")
                titleObjectName: "externalAppsSectionTitle"

                InlineSettingsRow {
                    objectName: "externalAppsRow"
                    tokens: root.tokens
                    titleText: qsTr("语音插件")
                    descriptionText: SettingsController.voicePluginConnected
                        ? qsTr("已接收 %1 个语音模式")
                            .arg(SettingsController.voicePluginModeCount)
                        : String(SettingsController.voicePluginState) === "listening"
                            ? qsTr("等待 MoteDeck2Vokie 插件连接")
                            : qsTr("接入第三方语音工具")
                    stateText: SettingsController.voicePluginConnected
                        ? qsTr("已连接")
                        : String(SettingsController.voicePluginState) === "listening"
                            ? qsTr("等待连接") : qsTr("未连接")
                    stateColor: SettingsController.voicePluginConnected
                        ? tokens.successColor
                        : String(SettingsController.voicePluginState) === "listening"
                            ? tokens.voiceAccent : tokens.textSecondary
                    showDivider: false

                    CompactButton {
                        objectName: "manageExternalAppsButton"
                        tokens: root.tokens
                        text: qsTr("管理插件")
                        enabled: !root.configurationWriteBusy
                        onClicked: externalAppsDialog.open()
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

    // ── 气泡位置微调：覆盖整个屏幕的遮罩窗口 + 可拖动的胶囊预览 ──
    Window {
        id: voiceToastTunerWindow
        objectName: "voiceToastTunerWindow"
        visible: false
        flags: Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        color: "transparent"

        onVisibleChanged: {
            if (visible) {
                requestActivate()
                voiceToastTuner.forceActiveFocus()
            }
        }

        Item {
            id: voiceToastTuner
            objectName: "voiceToastTuner"
            anchors.fill: parent
            focus: true
            // 本次调整的偏移量，点「保存」后才写入配置。
            property int draftOffset: 0
            // 已生效的偏移量，用来判断是否有未保存的调整。
            property int savedOffset: 0
            readonly property bool dirty: draftOffset !== savedOffset
            // 确认弹窗对应的操作：保存前确认为 "save"，未保存退出为 "exit"。
            property string pendingAction: ""
            readonly property int maxOffset: SettingsController.voiceCycleToastOffsetMax
            // 胶囊预览跟随所选气泡配色（1 = 浅色）。
            readonly property bool lightToast:
                SettingsController.voiceCycleToastThemeIndex === 1

            function open() {
                savedOffset = SettingsController.voiceCycleToastOffset
                draftOffset = savedOffset
                cancelConfirm()
                // 只铺设置窗口所在屏幕的可用区域，任务栏保持可见。
                SettingsController.placeVoiceToastTuner()
                voiceToastTunerWindow.visible = true
            }

            // 点「保存」先弹确认。
            function requestSave() {
                pendingAction = "save"
                voiceToastTunerConfirmLabel.text = qsTr("保存当前气泡位置？")
                voiceToastTunerConfirm.visible = true
            }

            // 有未保存的调整时，退出前先弹确认。
            function requestExit() {
                if (!dirty) {
                    closeTuner()
                    return
                }
                pendingAction = "exit"
                voiceToastTunerConfirmLabel.text = qsTr("调整尚未保存，是否保存？")
                voiceToastTunerConfirm.visible = true
            }

            function cancelConfirm() {
                pendingAction = ""
                voiceToastTunerConfirm.visible = false
                voiceToastTuner.forceActiveFocus()
            }

            function saveAndClose() {
                SettingsController.setVoiceCycleToastOffset(draftOffset)
                savedOffset = draftOffset
                closeTuner()
            }

            function closeTuner() {
                pendingAction = ""
                voiceToastTunerConfirm.visible = false
                voiceToastTunerWindow.visible = false
            }

            function applyOffset(value) {
                draftOffset = Math.max(0, Math.min(maxOffset, Math.round(value)))
            }

            Keys.onUpPressed: function(event) {
                applyOffset(draftOffset + 2)
                event.accepted = true
            }
            Keys.onDownPressed: function(event) {
                applyOffset(draftOffset - 2)
                event.accepted = true
            }
            Keys.onEscapePressed: function(event) {
                if (voiceToastTunerConfirm.visible) {
                    cancelConfirm()
                } else {
                    requestExit()
                }
                event.accepted = true
            }

            Rectangle {
                anchors.fill: parent
                color: Qt.rgba(0, 0, 0, 0.45)

                // 挡住遮罩上的点击，避免误触下面的窗口。
                MouseArea {
                    anchors.fill: parent
                    acceptedButtons: Qt.AllButtons
                    onWheel: function(wheel) { wheel.accepted = true }
                }
            }

            Rectangle {
                id: voiceToastTunerBar
                anchors.top: parent.top
                anchors.topMargin: 24
                anchors.horizontalCenter: parent.horizontalCenter
                width: voiceToastTunerBarRow.implicitWidth + 32
                height: 48
                radius: 12
                color: root.tokens.surface
                border.width: 1
                border.color: root.tokens.cardBorder

                RowLayout {
                    id: voiceToastTunerBarRow
                    anchors.centerIn: parent
                    spacing: 12

                    Label {
                        text: qsTr("拖动胶囊，或用 ↑ ↓ 微调")
                        color: root.tokens.textSecondary
                        font.family: root.tokens.fontFamily
                        font.pixelSize: root.tokens.fontSizeBody
                    }
                    CompactButton {
                        objectName: "voiceToastTunerResetButton"
                        tokens: root.tokens
                        text: qsTr("恢复默认")
                        onClicked: {
                            voiceToastTuner.applyOffset(0)
                            voiceToastTuner.forceActiveFocus()
                        }
                    }
                    CompactButton {
                        objectName: "voiceToastTunerSaveButton"
                        tokens: root.tokens
                        text: qsTr("保存")
                        onClicked: voiceToastTuner.requestSave()
                    }
                    CompactButton {
                        objectName: "voiceToastTunerExitButton"
                        tokens: root.tokens
                        text: qsTr("退出")
                        onClicked: voiceToastTuner.requestExit()
                    }
                }
            }

            // 保存、未保存退出前的确认弹窗。
            Rectangle {
                id: voiceToastTunerConfirm
                objectName: "voiceToastTunerConfirm"
                z: 10
                anchors.centerIn: parent
                visible: false
                width: voiceToastTunerConfirmColumn.implicitWidth + 48
                height: voiceToastTunerConfirmColumn.implicitHeight + 40
                radius: 12
                color: root.tokens.surface
                border.width: 1
                border.color: root.tokens.cardBorder

                ColumnLayout {
                    id: voiceToastTunerConfirmColumn
                    anchors.centerIn: parent
                    spacing: 16

                    Label {
                        id: voiceToastTunerConfirmLabel
                        Layout.maximumWidth: 420
                        wrapMode: Text.WordWrap
                        text: ""
                        color: root.tokens.textPrimary
                        font.family: root.tokens.fontFamily
                        font.pixelSize: root.tokens.fontSizeBody
                    }

                    RowLayout {
                        Layout.alignment: Qt.AlignRight
                        spacing: 8

                        CompactButton {
                            objectName: "voiceToastTunerConfirmCancelButton"
                            tokens: root.tokens
                            text: qsTr("取消")
                            onClicked: voiceToastTuner.cancelConfirm()
                        }
                        CompactButton {
                            objectName: "voiceToastTunerConfirmDiscardButton"
                            tokens: root.tokens
                            text: qsTr("放弃修改")
                            visible: voiceToastTuner.pendingAction === "exit"
                            onClicked: voiceToastTuner.closeTuner()
                        }
                        CompactButton {
                            objectName: "voiceToastTunerConfirmSaveButton"
                            tokens: root.tokens
                            text: qsTr("保存")
                            onClicked: voiceToastTuner.saveAndClose()
                        }
                    }
                }
            }

            Rectangle {
                id: voiceToastTunerPreview
                objectName: "voiceToastTunerPreview"
                anchors.horizontalCenter: parent.horizontalCenter
                // 与真实气泡一致：贴着屏幕底部，再按偏移量上移。
                y: voiceToastTuner.height - height - 10 - voiceToastTuner.draftOffset
                width: voiceToastTunerPreviewLabel.implicitWidth + 32
                height: 36
                radius: height / 2
                // 与真实气泡一致：浅色主题用白底深字，深色主题用深底白字。
                color: voiceToastTuner.lightToast ? "#ffffff" : "#121214"
                border.width: 1
                border.color: voiceToastTuner.lightToast ? "#dadade" : "#5c5c62"

                Label {
                    id: voiceToastTunerPreviewLabel
                    anchors.centerIn: parent
                    text: qsTr("语音模式")
                    color: voiceToastTuner.lightToast ? "#141416" : "#ffffff"
                    font.family: root.tokens.fontFamily
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                }

                MouseArea {
                    id: voiceToastTunerDragArea
                    anchors.fill: parent
                    cursorShape: Qt.SizeVerCursor
                    property real pressY: 0
                    property int pressOffset: 0

                    function positionInTuner(mouse) {
                        return voiceToastTunerDragArea.mapToItem(
                            voiceToastTuner, mouse.x, mouse.y).y
                    }

                    onPressed: function(mouse) {
                        pressY = positionInTuner(mouse)
                        pressOffset = voiceToastTuner.draftOffset
                        voiceToastTuner.forceActiveFocus()
                    }
                    onPositionChanged: function(mouse) {
                        voiceToastTuner.applyOffset(
                            pressOffset - (positionInTuner(mouse) - pressY))
                    }
                }
            }
        }
    }

}
