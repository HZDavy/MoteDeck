// "按键" tab: an RC003 product-photo reference with a selected-button marker
// sits beside the existing one-row-per-button mapping matrix. Mapping selection
// and editing remain owned by the matrix and real-key detection.
// SettingsController/ButtonMappingModel are QML singletons - see main.qml.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Effects
import QtQuick.Dialogs
import QtQuick.Controls.FluentWinUI3 as Fluent
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens
    MappingAppPickerDialog { id: mappingAppPickerDialog; tokens: root.tokens }
    // The application bar mirrors Logi Options+: the first chip is always the
    // global layout, the rest are the saved applications, then a "+" chip.
    readonly property var mappingChips: {
        const chips = [{ id: "", name: qsTr("全局"), icon: "" }]
        const apps = SettingsController.mappingAppProfiles
        for (let i = 0; i < apps.length; ++i)
            chips.push({ id: apps[i].id, name: apps[i].name, icon: apps[i].icon,
                process: apps[i].process, plugin: apps[i].plugin === true })
        return chips
    }
    // The chip that is currently in effect, i.e. the layout the bridge is
    // really using. The store marks it even when the user is editing another
    // chip, so the bar can keep showing which application is live.
    readonly property string appliedMappingChipId: {
        const rows = SettingsController.mappingPresets
        for (let i = 0; i < rows.length; ++i)
            if (rows[i].applied)
                return rows[i].automatic ? "" : rows[i].id
        return ""
    }
    readonly property var leftButtonIds: SettingsController.isRc003Device
        ? ["power", "up", "left", "back", "home", "menu"]
        : ["up", "left", "down", "back", "home", "youtube", "power"]
    // 设计稿 .board：高 448，卡片 300×56，行距 64，中间 .stage 140 宽。
    readonly property real mappingBoardHeight: 448
    readonly property real mappingCardWidth: 300
    readonly property real mappingCardHeight: 56
    readonly property real mappingRowPitch: 64
    readonly property real mappingPhotoColumnWidth: 140
    readonly property real mappingKeyLabelWidth: 36
    // 提示气泡与其他页面统一：同样距内容区底部 16，浮在底部那一行上。
    readonly property real messageBubbleHeight: 26
    property var backTabTarget: null
    property var tabTarget: null
    property string interceptionDriverMessage: ""
    property string mappingMessage: ""
    // 导入 / 导出弹窗的结果提示（成功或失败原因）。
    property string mappingBundleMessage: ""
    // 拖拽进来的配置文件路径，等确认后再真正写入。
    property string pendingImportFile: ""
    // 导入 / 导出只作用于按键页当前编辑的这份布局。
    readonly property string mappingBundleTargetName:
        SettingsController.mappingEditTarget.length > 0
            ? (SettingsController.mappingEditTargetLabel || qsTr("这个应用"))
            : qsTr("全局布局")
    // 快捷键录入弹窗的上下文：哪一行、哪个手势、是否回填到编辑器控件。
    property int shortcutRecorderRowIndex: -1
    property string shortcutRecorderTrigger: "single_click"
    readonly property var firstFocusItem: mappingAppsBar
    readonly property var lastFocusItem: SettingsController.interceptionAvailable
        ? uninstallInterceptionDriverButton : installInterceptionDriverButton
    readonly property bool hasPendingEditorDraft:
        actionEditor.visible && actionEditor.draftDirty

    function commitPendingEditorDraft() {
        if (!actionEditor.visible)
            return true
        return actionEditor.saveDraft()
    }

    function discardPendingEditorDraft() {
        if (actionEditor.visible)
            actionEditor.close()
    }

    function settleInputUiAfterStop() {
        if (shortcutRecorder.visible && !SettingsController.hotkeyCaptureActive)
            shortcutRecorder.finishClose()
    }

    function prepareForLifecyclePrompt() {
        if (!shortcutRecorder.visible)
            return true
        return shortcutRecorder.requestClose()
    }

    onWidthChanged: scheduleConnectorRepaint()
    onHeightChanged: scheduleConnectorRepaint()
    Component.onCompleted: {
        scheduleConnectorRepaint()
        // 页面一创建就把应用栏的列表读出来，否则冷启动后必须先打开“语音”页的
        // 配置管理弹窗才会填充，按键映射页会一直只有“全局”和“+”。
        SettingsController.refreshMappingApps()
    }

    // 同一帧内的多次几何变化合并成一次重绘；连线画布尺寸固定，
    // 拖窗口时只平移，重绘只在真正影响连线形状的变化时才有意义。
    property string connectorSignature: ""

    function paintConnectorsNow() {
        if (!mappingLinesCanvas || !activeMappingLineCanvas)
            return
        // 拖窗口时卡片和照片一起平移，连线在画布内的形状不变——跳过重绘。
        const signature = connectorShapeSignature()
        if (signature === connectorSignature)
            return
        connectorSignature = signature
        mappingLinesCanvas.requestPaint()
        activeMappingLineCanvas.requestPaint()
    }

    function connectorShapeSignature() {
        const parts = [mappingLinesCanvas.width, mappingLinesCanvas.height,
            SettingsController.selectedButtonId, tokens ? tokens.darkMode : ""]
        for (let i = 0; i < ButtonMappingModel.rowCount(); i++) {
            const hotspot = photoHotspotRepeater.itemAt(i)
            if (!hotspot)
                continue
            const card = isLeftButton(hotspot.buttonId)
                ? leftCardRepeater.itemAt(i) : rightCardRepeater.itemAt(i)
            const route = connectorRoute(card, hotspot, mappingLinesCanvas)
            if (route && card.visible && hotspot.visible)
                parts.push(Math.round(route.startX * 4), Math.round(route.startY * 4),
                    Math.round(route.endX * 4), Math.round(route.endY * 4))
        }
        return parts.join(",")
    }

    function scheduleConnectorRepaint() {
        Qt.callLater(root.paintConnectorsNow)
    }

    function isLeftButton(buttonId) {
        return leftButtonIds.indexOf(buttonId) >= 0
    }

    function visualRow(buttonId) {
        if (!SettingsController.isRc003Device) {
            const chromeRows = {
                "up": 0, "left": 1, "down": 2,
                "ok": 0, "right": 1, "volume_up": 2, "volume_down": 3,
                "back": 3, "mic": 4, "home": 4, "volume_mute": 5,
                "youtube": 5, "netflix": 6, "power": 6, "input_source": 7
            }
            return chromeRows[buttonId]
        }
        const rows = {
            "power": 0, "up": 1, "left": 2, "back": 3, "home": 4, "menu": 5,
            "mic": 0, "right": 1, "ok": 2, "down": 3,
            "volume_up": 4, "volume_down": 5, "tv": 6
        }
        return rows[buttonId]
    }

    // 卡片纵向位置：稿子左列 top 36+64·row，右列 top 4+64·row。
    function cardY(buttonId) {
        const left = isLeftButton(buttonId)
        const rows = left ? leftButtonIds.length
            : ButtonMappingModel.rowCount() - leftButtonIds.length
        const pitch = Math.min(mappingRowPitch,
            (mappingBoardHeight - mappingCardHeight) / Math.max(1, rows - 1))
        const used = (rows - 1) * pitch + mappingCardHeight
        const top = SettingsController.isRc003Device
            ? (left ? 36 : 4)
            : Math.max(0, (mappingBoardHeight - used) / 2)
        return top + visualRow(buttonId) * pitch
    }

    function connectorControlRadius(startX, endX) {
        // 稿子连线：控制点在起终点水平 ±40。
        return Math.min(40, Math.abs(endX - startX) / 2)
    }

    function connectorStrokeColor(active) {
        if (active)
            return tokens.accent
        return tokens.wire
    }

    function connectorRoute(card, hotspot, coordinateItem) {
        if (!card || !hotspot)
            return null

        const leftSide = root.isLeftButton(hotspot.buttonId)
        const start = card.mapToItem(
            coordinateItem,
            leftSide ? card.width : 0,
            card.height / 2
        )
        const center = hotspot.mapToItem(
            coordinateItem,
            hotspot.width / 2,
            hotspot.height / 2
        )
        // Both profiles end the connector on the marker center, so the line
        // and the dot always meet on the key itself.
        const endX = center.x
        const endY = center.y
        const direction = leftSide ? 1 : -1
        // Both profiles use one uninterrupted cubic over the full endpoint
        // span, with horizontal tangents. Do not squeeze its bend into a gutter.
        const controlRadius = root.connectorControlRadius(start.x, endX)
        const control1X = start.x + direction * controlRadius
        const control1Y = start.y
        const control2X = endX - direction * controlRadius
        const control2Y = endY
        return {
            startX: start.x,
            startY: start.y,
            control1X: control1X,
            control1Y: control1Y,
            control2X: control2X,
            control2Y: control2Y,
            endX: endX,
            endY: endY
        }
    }

    function paintConnectors(canvas, activeOnly) {
        const ctx = canvas.getContext("2d")
        ctx.reset()
        for (let i = 0; i < ButtonMappingModel.rowCount(); i++) {
            const leftCard = leftCardRepeater.itemAt(i)
            const rightCard = rightCardRepeater.itemAt(i)
            const hotspot = photoHotspotRepeater.itemAt(i)
            const buttonId = hotspot ? hotspot.buttonId : ""
            const active = SettingsController.selectedButtonId === buttonId
            if (active !== activeOnly)
                continue
            const card = root.isLeftButton(buttonId) ? leftCard : rightCard
            if (!card || !hotspot || !card.visible || !hotspot.visible)
                continue
            const route = root.connectorRoute(card, hotspot, canvas)
            if (!route)
                continue
            ctx.beginPath()
            ctx.lineCap = "round"
            ctx.lineJoin = "round"
            ctx.moveTo(route.startX, route.startY)
            ctx.bezierCurveTo(
                route.control1X,
                route.control1Y,
                route.control2X,
                route.control2Y,
                route.endX,
                route.endY
            )
            ctx.strokeStyle = root.connectorStrokeColor(active)
            ctx.globalAlpha = active ? 0.55 : 0.7
            ctx.lineWidth = active ? 1.5 : 1
            ctx.stroke()
            ctx.globalAlpha = 1

            // 稿子 .hs 热点圆点（选中光晕由 photoHotspotMarker 绘制）。
            ctx.beginPath()
            ctx.arc(route.endX, route.endY, active ? 3 : 2.25, 0, Math.PI * 2)
            ctx.fillStyle = active ? Qt.rgba(1, 1, 1, 0.85) : Qt.rgba(1, 1, 1, 0.55)
            ctx.fill()
            ctx.lineWidth = active ? 1.25 : 0.75
            ctx.strokeStyle = active
                ? Qt.rgba(tokens.accent.r, tokens.accent.g, tokens.accent.b, 0.8)
                : Qt.rgba(0, 0, 0, 0.35)
            ctx.stroke()
        }
    }

    function shortButtonName(buttonId) {
        const names = {
            "power": qsTr("电源"), "up": qsTr("上"), "left": qsTr("左"),
            "back": qsTr("返回"), "home": qsTr("主页"), "menu": qsTr("菜单"),
            "mic": qsTr("语音"), "right": qsTr("右"), "ok": qsTr("确定"),
            "down": qsTr("下"), "volume_up": "+", "volume_down": "−", "tv": "TV"
        }
        const chromeNames = { "volume_mute": qsTr("静音"), "youtube": "YT",
            "netflix": "NF", "input_source": qsTr("信源") }
        return names[buttonId] || chromeNames[buttonId] || buttonId
    }

    function openShortcutRecorder(buttonId, rowIndex, trigger) {
        shortcutRecorderRowIndex = rowIndex
        shortcutRecorderTrigger = trigger || "single_click"
        shortcutRecorder.openRecorder()
    }

    function applyShortcutRecorderChord(chord) {
        actionEditor.applyCapturedShortcut(
            shortcutRecorderRowIndex, shortcutRecorderTrigger,
            SettingsController.formatActionText(chord))
    }

    Timer {
        interval: 100
        repeat: true
        running: SettingsController.keyDetectionActive
        onTriggered: SettingsController.pollKeyDetectionBridge()
    }

    Connections {
        target: ButtonMappingModel
        function onDataChanged() {
            root.scheduleConnectorRepaint()
        }
    }

    SettingsDialog {
        id: restoreMappingDefaultsDialog
        tokens: root.tokens
        preferredWidth: 340 + tokens.dialogPadding * 2
        objectName: "restoreMappingDefaultsDialog"
        title: qsTr("恢复默认？")
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: SettingsController.restoreMappingDefaults()

        // 只重置当前选中的那份布局（应用布局或全局），提示文案跟着所选走。
        readonly property bool resetsGlobalLayout:
            SettingsController.mappingEditTarget.length === 0
        readonly property string resetTargetName:
            SettingsController.mappingEditTargetLabel.length > 0
                ? SettingsController.mappingEditTargetLabel
                : qsTr("当前应用")

        UiLabel {
            tokens: root.tokens
            kind: bodyKind
            width: 340
            wrapMode: Text.WordWrap
            text: restoreMappingDefaultsDialog.resetsGlobalLayout
                ? qsTr("这会把全局布局的 %1 个按键恢复为内置映射，并立即自动保存。各应用自己的布局不受影响。")
                    .arg(SettingsController.isRc003Device ? 13 : 15)
                : qsTr("这会把「%1」布局的 %2 个按键恢复为内置映射，并立即自动保存。全局布局和其它应用不受影响。")
                    .arg(restoreMappingDefaultsDialog.resetTargetName)
                    .arg(SettingsController.isRc003Device ? 13 : 15)
        }
    }

    // 导入 / 导出：一个弹窗两种操作，只处理按键页当前编辑的这一份布局。
    SettingsDialog {
        id: mappingBundleDialog
        objectName: "mappingBundleDialog"
        tokens: root.tokens
        preferredWidth: 360 + tokens.dialogPadding * 2
        title: qsTr("导入 / 导出配置")
        closeButtonObjectName: "mappingBundleCloseButton"
        // 每次打开只显示本次操作的结果，别把上次的提示带进来。
        onOpened: root.mappingBundleMessage = ""

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingLarge

            UiLabel {
                tokens: root.tokens
                kind: bodyKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("当前操作的是「%1」：导出只写这一份布局的按键映射，导入只用文件里的映射替换它。其它布局不受影响。")
                    .arg(root.mappingBundleTargetName)
            }

            UiLabel {
                objectName: "mappingBundleMessage"
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                visible: text.length > 0
                text: root.mappingBundleMessage
                textFormat: Text.PlainText
                wrapMode: Text.WrapAnywhere
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: root.tokens.spacingSmall

                CompactButton {
                    objectName: "mappingBundleExportButton"
                    tokens: root.tokens
                    text: qsTr("导出配置…")
                    enabled: !SettingsController.configurationReadOnly
                    onClicked: mappingBundleExportDialog.open()
                }
                CompactButton {
                    objectName: "mappingBundleImportButton"
                    tokens: root.tokens
                    text: qsTr("导入配置…")
                    enabled: !SettingsController.configurationReadOnly
                    onClicked: mappingBundleImportDialog.open()
                }
                Item { Layout.fillWidth: true }
                CompactButton {
                    objectName: "mappingBundleDoneButton"
                    tokens: root.tokens
                    flat: true
                    text: qsTr("关闭")
                    onClicked: mappingBundleDialog.close()
                }
            }
        }
    }

    // 窗口层的拖放区接到文件后交给这里处理：先校验扩展名，再弹确认窗。
    function handleImportDrop(drop) {
        if (!drop.hasUrls || drop.urls.length === 0) {
            drop.accepted = false
            return
        }
        const path = drop.urls[0].toString()
        drop.acceptProposedAction()
        if (!/\.json$/i.test(path)) {
            root.mappingMessage = qsTr("只能导入 .json 配置文件")
            return
        }
        root.pendingImportFile = path
        mappingImportConfirmDialog.open()
    }

    SettingsDialog {
        id: mappingImportConfirmDialog
        objectName: "mappingImportConfirmDialog"
        tokens: root.tokens
        preferredWidth: 380 + tokens.dialogPadding * 2
        title: qsTr("导入按键配置")
        closeButtonObjectName: "mappingImportConfirmCloseButton"

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingLarge

            UiLabel {
                objectName: "mappingImportConfirmMessage"
                tokens: root.tokens
                kind: bodyKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("导入会用文件里的按键映射覆盖「%1」当前的配置，原有按键映射将被替换，其它布局不受影响。")
                    .arg(root.mappingBundleTargetName)
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: root.tokens.spacingSmall

                Item { Layout.fillWidth: true }
                CompactButton {
                    objectName: "mappingImportConfirmCancelButton"
                    tokens: root.tokens
                    flat: true
                    text: qsTr("取消")
                    onClicked: {
                        root.pendingImportFile = ""
                        mappingImportConfirmDialog.close()
                    }
                }
                CompactButton {
                    objectName: "mappingImportConfirmAcceptButton"
                    tokens: root.tokens
                    text: qsTr("覆盖导入")
                    onClicked: {
                        const path = root.pendingImportFile
                        root.pendingImportFile = ""
                        mappingImportConfirmDialog.close()
                        const result = SettingsController.importMappingTarget(path)
                        root.mappingMessage = String(result.message || "")
                    }
                }
            }
        }
    }

    FileDialog {
        id: mappingBundleExportDialog
        objectName: "mappingBundleExportFileDialog"
        title: qsTr("导出「%1」").arg(root.mappingBundleTargetName)
        fileMode: FileDialog.SaveFile
        defaultSuffix: "json"
        nameFilters: [qsTr("配置文件 (*.json)")]
        onAccepted: {
            const result = SettingsController.exportMappingTarget(
                selectedFile.toString())
            root.mappingBundleMessage = String(result.message || "")
        }
    }

    FileDialog {
        id: mappingBundleImportDialog
        objectName: "mappingBundleImportFileDialog"
        title: qsTr("导入到「%1」").arg(root.mappingBundleTargetName)
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("配置文件 (*.json)")]
        onAccepted: {
            const result = SettingsController.importMappingTarget(
                selectedFile.toString())
            root.mappingBundleMessage = String(result.message || "")
        }
    }

    // Deleting an application layout is destructive and one stray click on the
    // chip's "×" used to remove it instantly, so ask first.
    SettingsDialog {
        id: removeMappingAppConfirmDialog
        tokens: root.tokens
        objectName: "removeMappingAppConfirmDialog"
        title: qsTr("移除应用程序？")
        modal: true
        anchors.centerIn: parent
        width: Math.min(430, root.width - 28)
        standardButtons: Dialog.Ok | Dialog.Cancel
        property string pendingProfileId: ""
        property string pendingProfileName: ""
        onAccepted: {
            const result = SettingsController.removeMappingAppProfile(
                removeMappingAppConfirmDialog.pendingProfileId)
            root.mappingMessage = result.message
        }

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingLarge
            UiLabel {
                tokens: root.tokens
                kind: bodyKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("这会删除“%1”的按键布局，删除后无法撤销。全局布局和其他应用程序不受影响。")
                    .arg(removeMappingAppConfirmDialog.pendingProfileName)
            }
        }
    }

    SettingsDialog {
        id: pluginAppLockedDialog
        tokens: root.tokens
        objectName: "pluginAppLockedDialog"
        title: qsTr("无法删除")
        modal: true
        anchors.centerIn: parent
        width: Math.min(430, root.width - 28)
        standardButtons: Dialog.Ok

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingLarge
            UiLabel {
                tokens: root.tokens
                kind: bodyKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("这是插件自带的应用程序，不能删除。断开插件后它会自动移除，已保存的按键配置会保留。")
            }
        }
    }

    SettingsDialog {
        id: interceptionInstallConfirmDialog
        tokens: root.tokens
        preferredWidth: 340 + tokens.dialogPadding * 2
        objectName: "interceptionInstallConfirmDialog"
        title: qsTr("安装驱动？")
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: {
            const result = SettingsController.installInterceptionDriver()
            root.interceptionDriverMessage = result.message
        }

        UiLabel {
            tokens: root.tokens
            kind: bodyKind
            width: 340
            wrapMode: Text.WordWrap
            text: qsTr("安装需要管理员权限，装完必须重启电脑才生效。软件只会启动随包附带的官方安装程序，绝不会静默安装。确定现在安装吗？")
        }
    }


    // 共享的快捷键录入二级弹窗；语音页复用同一份实现。
    ShortcutRecorderDialog {
        id: shortcutRecorder
        tokens: root.tokens
        onChordAccepted: function(chord) {
            root.applyShortcutRecorderChord(chord)
        }
    }


    component EditorActionCombo: ComboBox {
        id: editorCombo
        property var tokens

        function setEditorValue(value) {
            const display = SettingsController.formatActionText(value)
            currentIndex = -1
            for (let i = 0; i < model.length; ++i) {
                if (SettingsController.formatActionText(String(model[i])) === display) {
                    currentIndex = i
                    break
                }
            }
            editText = display
        }

        editable: true
        onAccepted: actionEditor.validationRequested = true
        selectTextByMouse: true
        implicitHeight: tokens.controlHeight
        leftPadding: 7
        rightPadding: 22
        // 与其他下拉列表一致：弹层高度/位置交给 Fluent 样式（撑满窗口、超长可滚动）
        font.family: tokens.fontFamily
        font.pixelSize: tokens.fontSizeControl

        // 下拉列表宽度按最长选项（含分组标题）自适应，长设备名不再被省略号截断。
        readonly property real popupIdealWidth: {
            if (!tokens)
                return editorCombo.width
            const optionModel = editorCombo.model
            if (!optionModel)
                return editorCombo.width
            let widest = 0
            for (let i = 0; i < editorCombo.count; ++i) {
                widest = Math.max(widest, optionMetrics.advanceWidth(
                    SettingsController.formatActionText(String(optionModel[i]))))
                widest = Math.max(widest, groupMetrics.advanceWidth(
                    SettingsController.actionOptionGroupTitle(String(optionModel[i]))))
            }
            return widest
        }

        FontMetrics {
            id: optionMetrics
            font.family: tokens ? tokens.fontFamily : "Microsoft YaHei UI"
            font.pixelSize: tokens ? tokens.fontSizeControl : 13
        }

        FontMetrics {
            id: groupMetrics
            font.family: tokens ? tokens.fontFamily : "Microsoft YaHei UI"
            font.pixelSize: tokens ? tokens.fontSizeTiny : 11
        }

        Binding {
            target: editorCombo.popup
            property: "width"
            value: Math.max(editorCombo.width, editorCombo.popupIdealWidth + 30)
        }

        // 可编辑 ComboBox 的弹层默认用「紧贴输入框展开」的背景图（顶部直角），
        // 这里换成普通 ComboBox 的弹层背景，四角才都是圆角，与设备下拉一致。
        popup.background: Fluent.StyleImage {
            imageConfig: Fluent.Config.controls.popup["normal"].background
        }

        indicator: DropDownIndicator {
            x: editorCombo.width - width - 7
            y: (editorCombo.height - height) / 2
            indicatorColor: editorCombo.enabled
                ? tokens.textSecondary : tokens.disabledText
        }

        background: Rectangle {
            radius: tokens.cornerRadiusControl
            color: tokens.fieldBackground
            border.width: tokens.hairlineWidth
            border.color: editorCombo.activeFocus ? tokens.accent : tokens.border
        }

        delegate: ItemDelegate {
            id: optionDelegate
            readonly property string groupTitle:
                SettingsController.actionOptionGroupTitle(String(modelData))
            readonly property bool startsGroup:
                groupTitle.length > 0 && (index === 0 ||
                groupTitle !== SettingsController.actionOptionGroupTitle(
                    String(editorCombo.model[index - 1])))
            readonly property int groupHeaderHeight: startsGroup
                ? Math.ceil(tokens.fontSizeTiny) + tokens.spacingMedium : 0
            objectName: editorCombo.objectName + "_option_" + index
            width: ListView.view ? ListView.view.width : editorCombo.width
            height: tokens.controlHeight + groupHeaderHeight
            topPadding: groupHeaderHeight
            bottomPadding: 0
            leftPadding: 7
            rightPadding: 7
            highlighted: editorCombo.highlightedIndex === index
            contentItem: Label {
                text: SettingsController.formatActionText(String(modelData))
                color: tokens.textPrimary
                font.family: tokens.fontFamily
                font.pixelSize: tokens.fontSizeControl
                font.weight: index === editorCombo.currentIndex
                    ? Font.DemiBold : Font.Normal
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }
            background: Item {
                // 行底铺满整行（含分组标题区），与 SelectionComboBox 的行底一致，
                // 否则分组标题区会露出弹层自身的深色底，形成深色横带
                Rectangle {
                    anchors.fill: parent
                    color: optionDelegate.highlighted
                        ? tokens.accentSoft : tokens.surface
                }
                Rectangle {
                    visible: optionDelegate.startsGroup
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    height: tokens.hairlineWidth
                    color: tokens.border
                }
                Label {
                    visible: optionDelegate.startsGroup
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.leftMargin: 7
                    anchors.rightMargin: 7
                    anchors.topMargin: 1
                    height: optionDelegate.groupHeaderHeight - 1
                    text: optionDelegate.groupTitle
                    color: tokens.textSecondary
                    font.family: tokens.fontFamily
                    font.pixelSize: tokens.fontSizeTiny
                    font.weight: Font.Medium
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }
            }
            MouseArea {
                visible: optionDelegate.startsGroup
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                height: optionDelegate.groupHeaderHeight
                acceptedButtons: Qt.LeftButton
            }
            Accessible.name: SettingsController.formatActionText(String(modelData))
        }
    }

    component InjectModeCombo: ComboBox {
        id: injectCombo
        property var tokens

        function syncFromMode(mode) {
            currentIndex = -1
            for (let i = 0; i < model.length; ++i) {
                if (String(model[i].value) === String(mode)) {
                    currentIndex = i
                    break
                }
            }
        }

        editable: false
        implicitHeight: tokens.controlHeight
        leftPadding: 7
        rightPadding: 22
        font.family: tokens.fontFamily
        font.pixelSize: tokens.fontSizeControl
        displayText: currentIndex >= 0 && currentIndex < model.length
            ? String(model[currentIndex].label) : ""

        // 下拉列表宽度按最长选项自适应，长模式名不再被省略号截断。
        readonly property real popupIdealWidth: {
            if (!tokens)
                return injectCombo.width
            const optionModel = injectCombo.model
            if (!optionModel)
                return injectCombo.width
            let widest = 0
            for (let i = 0; i < injectCombo.count; ++i) {
                const option = optionModel[i]
                widest = Math.max(widest, optionMetrics.advanceWidth(
                    option ? String(option.label) : ""))
            }
            return widest
        }

        FontMetrics {
            id: optionMetrics
            font.family: tokens ? tokens.fontFamily : "Microsoft YaHei UI"
            font.pixelSize: tokens ? tokens.fontSizeControl : 13
        }

        Binding {
            target: injectCombo.popup
            property: "width"
            value: Math.max(injectCombo.width, injectCombo.popupIdealWidth + 30)
        }

        indicator: DropDownIndicator {
            x: injectCombo.width - width - 7
            y: (injectCombo.height - height) / 2
            indicatorColor: injectCombo.enabled
                ? tokens.textSecondary : tokens.disabledText
        }

        background: Rectangle {
            radius: tokens.cornerRadiusControl
            color: tokens.fieldBackground
            border.width: tokens.hairlineWidth
            border.color: injectCombo.activeFocus ? tokens.accent : tokens.border
        }

        delegate: ItemDelegate {
            id: injectOptionDelegate
            readonly property var option: modelData
            readonly property bool unavailable: option && (option.available === false
                || (String(option.value) === "interception"
                    && actionEditor.interceptionBlocked))
            objectName: injectCombo.objectName + "_option_" + index
            width: ListView.view ? ListView.view.width : injectCombo.width
            height: tokens.controlHeight
            leftPadding: 7
            rightPadding: 7
            highlighted: injectCombo.highlightedIndex === index
            contentItem: Label {
                text: injectOptionDelegate.option
                    ? String(injectOptionDelegate.option.label) : ""
                color: injectOptionDelegate.unavailable
                    ? tokens.disabledText : tokens.textPrimary
                font.family: tokens.fontFamily
                font.pixelSize: tokens.fontSizeControl
                font.weight: index === injectCombo.currentIndex
                    ? Font.DemiBold : Font.Normal
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }
            background: Rectangle {
                color: injectOptionDelegate.highlighted
                    ? tokens.accentSoft : tokens.surface
            }
            Accessible.name: contentItem.text
        }
    }

    Dialog {
        id: actionEditor
        objectName: "actionEditorDialog"
        modal: true
        popupType: Popup.Item
        anchors.centerIn: parent
        width: Math.min(430, root.width - tokens.spacingLarge * 2)
        readonly property string headerText: buttonName.length > 0
            ? qsTr("编辑：") + buttonName
            : qsTr("编辑")
        title: headerText
        standardButtons: Dialog.NoButton
        leftPadding: tokens.dialogPadding
        rightPadding: tokens.dialogPadding
        topPadding: 0
        bottomPadding: tokens.dialogPadding
        leftInset: 0
        rightInset: 0
        topInset: 0
        bottomInset: 0
        closePolicy: Popup.CloseOnEscape

        background: Rectangle {
            radius: tokens.cornerRadiusLarge
            color: tokens.surface
            border.width: tokens.hairlineWidth
            border.color: tokens.border
        }

        header: Item {
            width: actionEditor.width
            implicitHeight: tokens.dialogTopPadding
                + tokens.dialogTitleHeight + tokens.dialogHeaderGap

            UiLabel {
                anchors.left: parent.left
                anchors.leftMargin: actionEditor.leftPadding
                y: tokens.dialogTopPadding + (tokens.dialogTitleHeight - height) / 2
                tokens: root.tokens
                kind: sectionTitleKind
                text: actionEditor.headerText
                font.pixelSize: tokens.fontSizeTitle
                font.weight: Font.Medium
            }

            DialogCloseButton {
                id: actionEditorCloseButton
                objectName: "actionEditorCloseButton"
                tokens: root.tokens
                anchors.right: parent.right
                anchors.rightMargin: actionEditor.rightPadding - tokens.dialogCloseButtonOutset
                y: tokens.dialogTopPadding + (tokens.dialogTitleHeight - height) / 2
                onCloseRequested: actionEditor.close()
            }
        }

        property int rowIndex: -1
        property string buttonId: ""
        property string buttonName: ""
        property string primaryText: ""
        property string doubleText: "未设置"
        property string longText: "未设置"
        property string primaryNote: ""
        property string doubleNote: ""
        property string longNote: ""
        property string injectMode: "sendinput"
        property string originalPrimaryText: ""
        property string originalDoubleText: "未设置"
        property string originalLongText: "未设置"
        property string originalPrimaryNote: ""
        property string originalDoubleNote: ""
        property string originalLongNote: ""
        property string originalInjectMode: "sendinput"
        property bool syncing: false
        property bool validationRequested: false
        onPrimaryTextChanged: {
            validationRequested = false
            if (!syncing)
                syncInjectModeToActions()
        }
        onDoubleTextChanged: {
            validationRequested = false
            if (!syncing)
                syncInjectModeToActions()
        }
        onLongTextChanged: {
            validationRequested = false
            if (!syncing)
                syncInjectModeToActions()
        }
        // 下拉选项是在 syncing=true 时写进 primaryText 的，上面三个处理器会跳过；
        // 等这批写入结束再补一次检查，动作用不了驱动级就静默退回 SendInput。
        onSyncingChanged: {
            if (!syncing)
                syncInjectModeToActions()
        }
        readonly property string normalizedPrimaryText: primaryText.trim()
        readonly property string normalizedDoubleText: doubleText.trim()
        readonly property string normalizedLongText: longText.trim()
        readonly property bool primaryIsVoice:
            SettingsController.isVoiceActionText(normalizedPrimaryText)
            || normalizedPrimaryText.indexOf("已停用：旧语音配置") === 0
        // 语音键的快捷键只能在语音页录入，所以三行“录入”一起锁掉；
        // “语音模式切换”只锁它自己那一行，其它手势仍可录快捷键。
        readonly property bool primaryHotkeyLocked:
            primaryIsVoice
            || SettingsController.isVoiceModeCycleText(normalizedPrimaryText)
        readonly property bool doubleHotkeyLocked:
            primaryIsVoice
            || SettingsController.isVoiceModeCycleText(normalizedDoubleText)
        readonly property bool longHotkeyLocked:
            primaryIsVoice
            || SettingsController.isVoiceModeCycleText(normalizedLongText)
        readonly property string primaryValidationError: rowIndex >= 0
            ? SettingsController.buttonActionValidationMessage(
                buttonId, "single_click", primaryText, injectMode
            ) : ""
        readonly property string doubleValidationError: rowIndex >= 0
            ? SettingsController.buttonActionValidationMessage(
                buttonId, "double_click", doubleText, injectMode
            ) : ""
        readonly property string longValidationError: rowIndex >= 0
            ? SettingsController.buttonActionValidationMessage(
                buttonId, "long_press", longText, injectMode
            ) : ""

        function interceptionBlockReason() {
            if (rowIndex < 0)
                return ""
            const texts = [
                ["single_click", primaryText],
                ["double_click", doubleText],
                ["long_press", longText]
            ]
            for (let i = 0; i < texts.length; ++i) {
                const message = SettingsController.buttonActionValidationMessage(
                    buttonId, texts[i][0], texts[i][1], "interception"
                )
                if (message.length > 0)
                    return message
            }
            return ""
        }
        // 当前动作（元素导航、打开应用、OpenMexia 设备等）用不了 Interception 时，
        // 直接退回 SendInput，不弹任何提示。
        readonly property bool interceptionBlocked: rowIndex >= 0
            && interceptionBlockReason().length > 0
        function syncInjectModeToActions() {
            if (injectMode === "interception" && interceptionBlocked) {
                injectMode = "sendinput"
                injectModeCombo.syncFromMode("sendinput")
            }
        }
        readonly property string injectModeHintText: SettingsController.interceptionAvailable
            ? "" : SettingsController.interceptionUnavailableHint
        readonly property string validationError:
            primaryValidationError.length > 0 ? primaryValidationError
            : doubleValidationError.length > 0 ? doubleValidationError
            : longValidationError
        readonly property bool draftDirty:
            primaryText !== originalPrimaryText
            || doubleText !== originalDoubleText
            || longText !== originalLongText
            || primaryNote !== originalPrimaryNote
            || doubleNote !== originalDoubleNote
            || longNote !== originalLongNote
            || injectMode !== originalInjectMode

        function openForRow(rowIndexValue, buttonIdValue, buttonNameValue,
                            primaryValue, doubleValue, longValue,
                            primaryNoteValue, doubleNoteValue, longNoteValue) {
            syncing = true
            validationRequested = false
            rowIndex = rowIndexValue
            buttonId = buttonIdValue
            buttonName = buttonNameValue
            primaryText = primaryValue
            doubleText = doubleValue
            longText = longValue
            primaryNote = primaryNoteValue
            doubleNote = doubleNoteValue
            longNote = longNoteValue
            injectMode = SettingsController.buttonInjectMode(buttonIdValue)
            originalPrimaryText = primaryValue
            originalDoubleText = doubleValue
            originalLongText = longValue
            originalPrimaryNote = primaryNoteValue
            originalDoubleNote = doubleNoteValue
            originalLongNote = longNoteValue
            originalInjectMode = injectMode
            primaryCombo.setEditorValue(primaryValue)
            doubleCombo.setEditorValue(doubleValue)
            longCombo.setEditorValue(longValue)
            injectModeCombo.syncFromMode(injectMode)
            syncInjectModeToActions()
            primaryNoteField.text = primaryNoteValue
            doubleNoteField.text = doubleNoteValue
            longNoteField.text = longNoteValue
            syncing = false
            open()
            primaryCombo.forceActiveFocus()
        }

        function applyCapturedShortcut(targetRow, trigger, chord) {
            if (!visible || targetRow !== rowIndex)
                return
            syncing = true
            if (trigger === "single_click") {
                primaryText = chord
                primaryCombo.setEditorValue(chord)
            } else if (trigger === "double_click") {
                doubleText = chord
                doubleCombo.setEditorValue(chord)
            } else if (trigger === "long_press") {
                longText = chord
                longCombo.setEditorValue(chord)
            }
            syncing = false
        }

        function saveDraft() {
            validationRequested = true
            // 兜底：动作用不了驱动级时，保存这一步直接按 SendInput 存，
            // 不把「请改用 SendInput」当成错误拦下来。
            if (injectMode === "interception" && interceptionBlocked) {
                injectMode = "sendinput"
                injectModeCombo.syncFromMode("sendinput")
            }
            if (rowIndex < 0 || validationError.length > 0)
                return false
            ButtonMappingModel.setActionTextAt(rowIndex, primaryText)
            ButtonMappingModel.setSecondaryActionTextAt(
                rowIndex, "double_click", doubleText
            )
            ButtonMappingModel.setSecondaryActionTextAt(
                rowIndex, "long_press", longText
            )
            ButtonMappingModel.setDisplayNoteAt(
                rowIndex, "single_click", primaryNote
            )
            ButtonMappingModel.setDisplayNoteAt(
                rowIndex, "double_click", doubleNote
            )
            ButtonMappingModel.setDisplayNoteAt(
                rowIndex, "long_press", longNote
            )
            SettingsController.setButtonInjectMode(buttonId, injectMode)
            close()
            return true
        }

        onClosed: {
            syncing = true
            rowIndex = -1
        }

        contentItem: ColumnLayout {
            spacing: 9

            GridLayout {
                Layout.fillWidth: true
                columns: 3
                columnSpacing: tokens.spacingMedium
                rowSpacing: tokens.spacingSmall

                Item {
                    Layout.preferredWidth: 34
                    Layout.minimumWidth: 34
                    Layout.maximumWidth: 34
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    text: qsTr("按键录入")
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    Layout.preferredWidth: 126
                    Layout.minimumWidth: 126
                    Layout.maximumWidth: 126
                    text: qsTr("备注名称")
                }

                UiLabel {
                    id: primaryGestureTitle
                    objectName: "actionEditorPrimaryTitle"
                    tokens: root.tokens
                    kind: bodyKind
                    text: qsTr("单击")
                    font.pixelSize: tokens.fontSizeControl
                    font.weight: Font.Medium
                    Layout.fillHeight: true
                    verticalAlignment: Text.AlignVCenter
                    HoverHandler { id: primaryGestureTitleHover }
                    CompactToolTip {
                        objectName: "actionEditorPrimaryHelp"
                        tokens: root.tokens
                        active: primaryGestureTitleHover.hovered
                        text: actionEditor.buttonId === "mic"
                            ? qsTr("按键可选语音一/二/三（按住说话）、普通动作、快捷键或 Quicker URI；滚轮点按一格，按住连滚并提速；启动应用需已安装")
                            : qsTr("按键可选语音一/二/三、普通动作、快捷键或 Quicker URI；滚轮点按一格，按住连滚并提速；启动应用需已安装")
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: tokens.spacingSmall

                    EditorActionCombo {
                    id: primaryCombo
                    objectName: "actionEditorPrimaryCombo"
                        tokens: root.tokens
                    Layout.fillWidth: true
                    model: {
                        // 依赖“使用其他按键切换模式”，开关变化后重新取列表
                        SettingsController.voiceOtherKeySwitch
                        // 语音键被停用后也要马上从列表里消失
                        SettingsController.voiceKeyConfigs
                        // 连上 OpenMexia 后设备动作要马上出现
                        SettingsController.openMexiaDeviceCount
                        return SettingsController.primaryActionOptionsFor(
                            actionEditor.buttonId
                        )
                    }
                    Accessible.name: actionEditor.buttonName + qsTr("单击动作")
                    onEditTextChanged: {
                            if (!actionEditor.syncing)
                            actionEditor.primaryText = editText
                    }
                    onActivated: {
                        const selectedText = SettingsController.formatActionText(currentText)
                        actionEditor.syncing = true
                        editText = selectedText
                        actionEditor.primaryText = selectedText
                        actionEditor.syncing = false
                    }
                }
                    CompactButton {
                    objectName: "actionEditorPrimaryRecordButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth2Chars
                    enabled: !actionEditor.primaryHotkeyLocked
                    text: qsTr("录入")
                    onClicked: root.openShortcutRecorder(
                        actionEditor.buttonId, actionEditor.rowIndex,
                        "single_click"
                    )
                    Accessible.name: qsTr("录制单击快捷键")
                }
                }
                CompactTextField {
                    id: primaryNoteField
                    objectName: "actionEditorPrimaryNoteField"
                    tokens: root.tokens
                    Layout.preferredWidth: 126
                    Layout.minimumWidth: 126
                    Layout.maximumWidth: 126
                    placeholderText: qsTr("如：复制")
                    onTextChanged: {
                        if (!actionEditor.syncing)
                            actionEditor.primaryNote = text
                    }
                    Accessible.name: qsTr("单击备注名称")
                }

                UiLabel {
                    id: doubleGestureTitle
                    objectName: "actionEditorDoubleTitle"
                    tokens: root.tokens
                    kind: bodyKind
                    text: qsTr("双击")
                    font.pixelSize: tokens.fontSizeControl
                    font.weight: Font.Medium
                    Layout.fillHeight: true
                    verticalAlignment: Text.AlignVCenter
                    HoverHandler { id: doubleGestureTitleHover }
                    CompactToolTip {
                        tokens: root.tokens
                        active: doubleGestureTitleHover.hovered
                        text: qsTr("会等待约 0.3 秒区分单击和双击；设置双击或长按后，此键不再支持按住连发")
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: tokens.spacingSmall

                    EditorActionCombo {
                    id: doubleCombo
                    objectName: "actionEditorDoubleCombo"
                        tokens: root.tokens
                    Layout.fillWidth: true
                    enabled: !actionEditor.primaryIsVoice
                    model: {
                        // 依赖“使用其他按键切换模式”，开关变化后重新取列表
                        SettingsController.voiceOtherKeySwitch
                        // 连上 OpenMexia 后设备动作要马上出现
                        SettingsController.openMexiaDeviceCount
                        return SettingsController.secondaryActionOptionsFor(
                            actionEditor.buttonId
                        )
                    }
                    Accessible.name: actionEditor.buttonName + qsTr("双击动作")
                    onEditTextChanged: {
                            if (!actionEditor.syncing)
                            actionEditor.doubleText = editText
                    }
                    onActivated: {
                        const selectedText = SettingsController.formatActionText(currentText)
                        actionEditor.syncing = true
                        editText = selectedText
                        actionEditor.doubleText = selectedText
                        actionEditor.syncing = false
                    }
                }
                    CompactButton {
                    objectName: "actionEditorDoubleRecordButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth2Chars
                    enabled: !actionEditor.doubleHotkeyLocked
                    text: qsTr("录入")
                    onClicked: root.openShortcutRecorder(
                        actionEditor.buttonId, actionEditor.rowIndex,
                        "double_click"
                    )
                    Accessible.name: qsTr("录制双击快捷键")
                }
                }
                CompactTextField {
                    id: doubleNoteField
                    objectName: "actionEditorDoubleNoteField"
                    tokens: root.tokens
                    Layout.preferredWidth: 126
                    Layout.minimumWidth: 126
                    Layout.maximumWidth: 126
                    enabled: !actionEditor.primaryIsVoice
                    placeholderText: qsTr("如：复制")
                    onTextChanged: {
                        if (!actionEditor.syncing)
                            actionEditor.doubleNote = text
                    }
                    Accessible.name: qsTr("双击备注名称")
                }

                UiLabel {
                    id: longGestureTitle
                    objectName: "actionEditorLongTitle"
                    tokens: root.tokens
                    kind: bodyKind
                    text: qsTr("长按")
                    font.pixelSize: tokens.fontSizeControl
                    font.weight: Font.Medium
                    Layout.fillHeight: true
                    verticalAlignment: Text.AlignVCenter
                    HoverHandler { id: longGestureTitleHover }
                    CompactToolTip {
                        tokens: root.tokens
                        active: longGestureTitleHover.hovered
                        text: qsTr("按住约 0.55 秒触发；鼠标长按保持到松手，其它动作触发一次；设置后不再支持按住连发")
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: tokens.spacingSmall

                    EditorActionCombo {
                    id: longCombo
                    objectName: "actionEditorLongCombo"
                        tokens: root.tokens
                    Layout.fillWidth: true
                    enabled: !actionEditor.primaryIsVoice
                    model: {
                        // 依赖“使用其他按键切换模式”，开关变化后重新取列表
                        SettingsController.voiceOtherKeySwitch
                        return SettingsController.longPressActionOptionsFor(
                            actionEditor.buttonId
                        )
                    }
                    Accessible.name: actionEditor.buttonName + qsTr("长按动作")
                    onEditTextChanged: {
                            if (!actionEditor.syncing)
                            actionEditor.longText = editText
                    }
                    onActivated: {
                        const selectedText = SettingsController.formatActionText(currentText)
                        actionEditor.syncing = true
                        editText = selectedText
                        actionEditor.longText = selectedText
                        actionEditor.syncing = false
                    }
                }
                    CompactButton {
                    objectName: "actionEditorLongRecordButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth2Chars
                    enabled: !actionEditor.longHotkeyLocked
                    text: qsTr("录入")
                    onClicked: root.openShortcutRecorder(
                        actionEditor.buttonId, actionEditor.rowIndex,
                        "long_press"
                    )
                    Accessible.name: qsTr("录制长按快捷键")
                }
                }
                CompactTextField {
                    id: longNoteField
                    objectName: "actionEditorLongNoteField"
                    tokens: root.tokens
                    Layout.preferredWidth: 126
                    Layout.minimumWidth: 126
                    Layout.maximumWidth: 126
                    enabled: !actionEditor.primaryIsVoice
                    placeholderText: qsTr("如：复制")
                    onTextChanged: {
                        if (!actionEditor.syncing)
                            actionEditor.longNote = text
                    }
                    Accessible.name: qsTr("长按备注名称")
                }

                UiLabel {
                    id: injectModeTitle
                    objectName: "actionEditorInjectModeTitle"
                    tokens: root.tokens
                    kind: bodyKind
                    text: qsTr("注入方式")
                    font.pixelSize: tokens.fontSizeControl
                    font.weight: Font.Medium
                    Layout.fillHeight: true
                    verticalAlignment: Text.AlignVCenter
                    HoverHandler { id: injectModeTitleHover }
                    CompactToolTip {
                        tokens: root.tokens
                        active: injectModeTitleHover.hovered
                        text: qsTr("该按键所有动作使用的注入后端；Interception 需要安装驱动，未安装时不可用。元素导航、打开应用等非普通按键动作只支持 SendInput")
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: tokens.spacingSmall

                    InjectModeCombo {
                        id: injectModeCombo
                        objectName: "actionEditorInjectModeCombo"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        model: SettingsController.injectModeOptions
                        Accessible.name: actionEditor.buttonName + qsTr("注入方式")
                        onActivated: {
                            const option = model[index]
                            const value = option ? String(option.value) : ""
                            // 驱动级用不了的动作：这一项直接不可选，选了也停回原来的值，
                            // 不再弹任何解释。
                            if (!option || option.available === false
                                    || (value === "interception"
                                        && actionEditor.interceptionBlocked)) {
                                injectModeCombo.syncFromMode(actionEditor.injectMode)
                                return
                            }
                            actionEditor.injectMode = value
                        }
                    }
                }
                UiLabel {
                    id: injectModeHint
                    objectName: "actionEditorInjectModeHint"
                    tokens: root.tokens
                    kind: noteKind
                    Layout.preferredWidth: 126
                    Layout.minimumWidth: 126
                    Layout.maximumWidth: 126
                    visible: actionEditor.injectModeHintText.length > 0
                    text: actionEditor.injectModeHintText
                    wrapMode: Text.Wrap
                }

            }

            UiLabel {
                id: actionEditorValidationError
                objectName: "actionEditorValidationError"
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                visible: text.length > 0
                text: actionEditor.validationRequested ? actionEditor.validationError : ""
                color: tokens.errorColor
                wrapMode: Text.WordWrap
                Accessible.name: text
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: tokens.spacingSmall
                Item { Layout.fillWidth: true }
                CompactButton {
                    objectName: "actionEditorCancelButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth2Chars
                    text: qsTr("取消")
                    onClicked: actionEditor.close()
                }
                CompactButton {
                    objectName: "actionEditorSaveButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth2Chars
                    text: qsTr("完成")
                    enabled: actionEditor.rowIndex >= 0
                    highlighted: true
                    onClicked: actionEditor.saveDraft()
                }
            }
        }
    }

    Item {
        id: rc003MappingLayout
        objectName: "rc003MappingLayout"
        anchors.fill: parent

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            Item {
                id: mappingPageHeader
                objectName: "mappingPageHeader"
                Layout.fillWidth: true
                Layout.preferredHeight: 60

                RowLayout {
                    id: mappingTitleRow
                    anchors.left: parent.left
                    anchors.right: parent.right
                    height: 40
                    spacing: 12

                    Label {
                        objectName: "mappingPageTitle"
                        text: qsTr("按键映射")
                        font.family: tokens.fontFamilyDisplay
                        font.pixelSize: tokens.fontSizePageTitle
                        font.weight: Font.DemiBold
                        color: tokens.textPrimary
                        Layout.alignment: Qt.AlignBaseline
                    }
                    Label {
                        objectName: "mappingPageSubtitle"
                        text: qsTr("为每个按键设置动作")
                        font.family: tokens.fontFamily
                        font.pixelSize: tokens.fontSizeBody
                        color: tokens.textTertiary
                        Layout.alignment: Qt.AlignBaseline
                        Layout.fillWidth: true
                    }

                    MappingAppsBar {
                        id: mappingAppsBar
                        objectName: "mappingAppsBar"
                        tokens: root.tokens
                        chips: root.mappingChips
                        interactive: !SettingsController.configurationReadOnly
                        Layout.alignment: Qt.AlignVCenter
                        KeyNavigation.backtab: root.backTabTarget
                        KeyNavigation.tab: root.tabTarget
                        onAddRequested: mappingAppPickerDialog.open()
                        // 除「全局」外都能拖动排序，包括插件自带的 OpenMexia。
                        onMoveRequested: function(fromIndex, toIndex) {
                            const chips = root.mappingChips
                            const target = Math.max(0,
                                Math.min(chips.length - 2, toIndex))
                            if (fromIndex === target || !chips[fromIndex + 1])
                                return
                            const result = SettingsController.moveMappingAppProfile(
                                chips[fromIndex + 1].id, target)
                            if (!result.ok && result.message)
                                root.mappingMessage = result.message
                        }
                        onRemoveRequested: function(profileId, profileName) {
                            const chips = root.mappingChips
                            for (let i = 1; i < chips.length; ++i) {
                                if (chips[i].id === profileId && chips[i].plugin) {
                                    pluginAppLockedDialog.open()
                                    return
                                }
                            }
                            removeMappingAppConfirmDialog.pendingProfileId = profileId
                            removeMappingAppConfirmDialog.pendingProfileName = profileName
                            removeMappingAppConfirmDialog.open()
                        }
                    }
                }
            }

            // 设计稿 .board：高 448；宽度跟随窗口自适应，左右卡片贴边、实物图居中。
            Item {
                id: mappingList
                objectName: "mappingList"
                Layout.fillWidth: true
                Layout.preferredHeight: root.mappingBoardHeight
                Layout.alignment: Qt.AlignTop
                property int count: SettingsController.isRc003Device ? 13 : 15
                property int currentIndex: ButtonMappingModel.indexOfButton(
                    SettingsController.selectedButtonId
                )
                // 稿子 .kc：板宽 764 时卡片 300 =(764-140)/2-12。
                // 板变宽时左右卡片整体拉伸（内部三个手势格随之等分），
                // 中间始终给实物图留 140 宽 + 两侧 12 的间隙。
                readonly property real sideCardWidth: Math.max(
                    root.mappingCardWidth,
                    (width - root.mappingPhotoColumnWidth) / 2 - 12
                )
                // 连线画布：卡片内边两侧各外扩 8 给圆头/圆点留余量。
                // 直接由板宽求（等价于 width − 2·sideCardWidth），不经 sideCardWidth，
                // 避免绑定求值先后导致宽度瞬时跳变而触发 Canvas 重建。
                readonly property real connectorBandWidth: Math.max(1, Math.min(
                    root.mappingPhotoColumnWidth + 24,
                    width - 2 * root.mappingCardWidth
                ) + 16)
                // 取整，避免 Canvas 落在半像素上被重采样变糊。
                readonly property real connectorBandX:
                    Math.round((width - connectorBandWidth) / 2)

                // 连线只存在于"卡片内边 ↔ 实物图"之间那条窄带里，而这条带的宽度
                // （照片列 140 + 两侧间隙 12）不随窗口变化。所以 Canvas 只覆盖这条带：
                // 拖窗口时它只是平移，不重建纹理、也不用重画，连线始终可见且不闪。
                Item {
                    id: mappingLines
                    objectName: "mappingLines"
                    anchors.fill: parent
                    // 稿子 .wires 叠在 .stage 照片之上。
                    z: 2

                    // Canvas 的几何（含位置）一变 Qt 就会自动重画，所以让外层 Item
                    // 平移，Canvas 自身在其中保持 (0,0) 不动。
                    Item {
                        x: mappingList.connectorBandX
                        width: mappingList.connectorBandWidth
                        height: parent.height

                        Canvas {
                            id: mappingLinesCanvas
                            anchors.fill: parent
                            antialiasing: true
                            onPaint: root.paintConnectors(mappingLinesCanvas, false)
                            onWidthChanged: root.scheduleConnectorRepaint()
                            onHeightChanged: root.scheduleConnectorRepaint()
                        }
                    }
                }

                Item {
                    id: activeMappingLine
                    objectName: "activeMappingLine"
                    anchors.fill: parent
                    z: 2

                    Item {
                        x: mappingList.connectorBandX
                        width: mappingList.connectorBandWidth
                        height: parent.height

                        Canvas {
                            id: activeMappingLineCanvas
                            anchors.fill: parent
                            antialiasing: true
                            onPaint: root.paintConnectors(activeMappingLineCanvas, true)
                            onWidthChanged: root.scheduleConnectorRepaint()
                            onHeightChanged: root.scheduleConnectorRepaint()
                        }
                    }
                }

                Connections {
                    target: SettingsController
                    function onSelectedButtonIdChanged() {
                        root.scheduleConnectorRepaint()
                    }
                }

                Item {
                    id: leftSideCards
                    objectName: "leftMappingCards"
                    x: 0
                    y: 0
                    width: mappingList.sideCardWidth
                    height: parent.height
                    z: 3
                    onXChanged: root.scheduleConnectorRepaint()
                    onYChanged: root.scheduleConnectorRepaint()
                    onWidthChanged: root.scheduleConnectorRepaint()
                    onHeightChanged: root.scheduleConnectorRepaint()

                    Repeater {
                        id: leftCardRepeater
                        model: ButtonMappingModel
                        onItemAdded: root.scheduleConnectorRepaint()
                        delegate: MappingCard {
                            required property int index
                            required property string buttonId
                            required property string displayName
                            required property string actionText
                            required property string doubleClickText
                            required property string longPressText
                            required property string singleNote
                            required property string doubleNote
                            required property string longNote
                            required property bool isSelected

                            visible: root.isLeftButton(buttonId)
                            enabled: true
                            exposeObjectNames: visible
                            tokens: root.tokens
                            cardId: buttonId
                            x: 0
                            y: visible ? root.cardY(buttonId) : 0
                            width: mappingList.sideCardWidth
                            height: root.mappingCardHeight
                            buttonName: root.shortButtonName(buttonId)
                            keyLabelWidth: root.mappingKeyLabelWidth
                            singleText: actionText
                            doubleText: doubleClickText
                            longText: longPressText
                            singleNoteText: singleNote
                            doubleNoteText: doubleNote
                            longNoteText: longNote
                            selected: isSelected
                            voiceAction: SettingsController.isVoiceActionText(actionText.trim())
                                || actionText.indexOf("已停用：旧语音配置") === 0
                            onXChanged: root.scheduleConnectorRepaint()
                            onYChanged: root.scheduleConnectorRepaint()
                            onWidthChanged: root.scheduleConnectorRepaint()
                            onHeightChanged: root.scheduleConnectorRepaint()
                            onVisibleChanged: root.scheduleConnectorRepaint()
                            Component.onCompleted: root.scheduleConnectorRepaint()
                            onClicked: {
                                SettingsController.selectButton(buttonId)
                                actionEditor.openForRow(
                                    index, buttonId, root.shortButtonName(buttonId), actionText,
                                    doubleClickText, longPressText,
                                    singleNote, doubleNote, longNote
                                )
                            }
                        }
                    }
                }

                Item {
                    id: photoSidebar
                    objectName: "photoSidebar"
                    // 稿子 .stage：left 312（= 卡片 300 + 12），140×448。
                    x: (parent.width - width) / 2
                    y: 0
                    width: root.mappingPhotoColumnWidth
                    height: parent.height
                    z: 1

                    // .stage::before：底部 8px 的径向渐变投影椭圆 96×14。
                    Rectangle {
                        width: 96
                        height: 14
                        radius: 7
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.bottom: parent.bottom
                        anchors.bottomMargin: 8
                        visible: SettingsController.photoAvailable
                        gradient: Gradient {
                            orientation: Gradient.Horizontal
                            GradientStop { position: 0.0; color: "transparent" }
                            GradientStop {
                                position: 0.5
                                color: Qt.rgba(0, 0, 0, root.tokens.darkMode ? 0.6 : 0.20)
                            }
                            GradientStop { position: 1.0; color: "transparent" }
                        }
                        layer.enabled: GraphicsInfo.shaderType === GraphicsInfo.RhiShader
                        layer.effect: MultiEffect {
                            blurEnabled: true
                            blur: 0.6
                            blurMax: 8
                        }
                    }
                    onXChanged: root.scheduleConnectorRepaint()
                    onYChanged: root.scheduleConnectorRepaint()
                    onWidthChanged: root.scheduleConnectorRepaint()
                    onHeightChanged: root.scheduleConnectorRepaint()

                    Item {
                        id: photoFrame
                        objectName: "photoFrame"
                        // 稿子 .stage img：高 400，top 34，水平居中。
                        y: 34
                        anchors.horizontalCenter: parent.horizontalCenter
                        width: SettingsController.isRc003Device ? 400 * 240 / 360 : parent.width
                        height: 400
                        onXChanged: root.scheduleConnectorRepaint()
                        onYChanged: root.scheduleConnectorRepaint()
                        onWidthChanged: root.scheduleConnectorRepaint()
                        onHeightChanged: root.scheduleConnectorRepaint()

                        Image {
                            id: photoImage
                            objectName: "photoImage"
                            anchors.horizontalCenter: parent.horizontalCenter
                            anchors.verticalCenter: parent.verticalCenter
                            width: SettingsController.isRc003Device ? height * 240 / 360
                                : height * sourceSize.width / Math.max(1, sourceSize.height)
                            height: parent.height
                            source: SettingsController.photoAvailable
                                ? SettingsController.photoSource : ""
                            visible: SettingsController.photoAvailable
                            fillMode: SettingsController.isRc003Device ? Image.Stretch : Image.PreserveAspectFit
                            smooth: true
                            mipmap: true
                            // drop-shadow(0 14px 18px rgba(0,0,0,.16))
                            // 谷歌照片底部是整块圆弧，贴图内投影会被裁成一条硬边灰带，
                            // 所以只在小米那套实物图上保留投影。
                            layer.enabled: SettingsController.photoAvailable
                                && SettingsController.isRc003Device
                                && GraphicsInfo.shaderType === GraphicsInfo.RhiShader
                            layer.effect: MultiEffect {
                                shadowEnabled: true
                                shadowColor: Qt.rgba(0, 0, 0, 0.16)
                                shadowBlur: 1.0
                                blurMax: 18
                                shadowHorizontalOffset: 0
                                shadowVerticalOffset: 14
                                autoPaddingEnabled: true
                            }
                            onXChanged: root.scheduleConnectorRepaint()
                            onYChanged: root.scheduleConnectorRepaint()
                            onWidthChanged: root.scheduleConnectorRepaint()
                            onHeightChanged: root.scheduleConnectorRepaint()
                        }

                        UiLabel {
                            anchors.centerIn: parent
                            width: parent.width
                            visible: !SettingsController.photoAvailable
                            tokens: root.tokens
                            kind: noteKind
                            text: qsTr("实物图缺失")
                            horizontalAlignment: Text.AlignHCenter
                        }

                        Repeater {
                            id: photoHotspotRepeater
                            model: ButtonMappingModel
                            onItemAdded: root.scheduleConnectorRepaint()
                            delegate: Item {
                                id: photoHotspot
                                objectName: "photoHotspot_" + buttonId

                                required property int index
                                required property string buttonId
                                required property real hotspotX
                                required property real hotspotY
                                required property real hotspotWidth
                                required property real hotspotHeight
                                required property bool isSelected
                                required property bool isVoice

                                width: hotspotWidth * photoImage.paintedWidth
                                height: hotspotHeight * photoImage.paintedHeight
                                x: photoImage.x
                                    + (photoImage.width - photoImage.paintedWidth) / 2
                                    + hotspotX * photoImage.paintedWidth
                                    - width / 2
                                y: photoImage.y
                                    + (photoImage.height - photoImage.paintedHeight) / 2
                                    + hotspotY * photoImage.paintedHeight
                                    - height / 2
                                visible: SettingsController.photoAvailable
                                z: 2
                                onXChanged: root.scheduleConnectorRepaint()
                                onYChanged: root.scheduleConnectorRepaint()
                                onWidthChanged: root.scheduleConnectorRepaint()
                                onHeightChanged: root.scheduleConnectorRepaint()
                                onVisibleChanged: root.scheduleConnectorRepaint()
                                Component.onCompleted: root.scheduleConnectorRepaint()

                                // 稿子 .wires .halo：r 7 的选中光晕，圆点由连线 Canvas 绘制。
                                Rectangle {
                                    objectName: "photoHotspotMarker_" + photoHotspot.buttonId
                                    anchors.centerIn: parent
                                    width: 14
                                    height: 14
                                    z: 1
                                    visible: photoHotspot.isSelected
                                    radius: 7
                                    color: tokens.halo
                                }

                                TapHandler {
                                    onTapped: SettingsController.selectButton(photoHotspot.buttonId)
                                }
                                HoverHandler { id: hotspotHover }
                                Rectangle {
                                    anchors.centerIn: parent
                                    width: 14
                                    height: 14
                                    z: 1
                                    visible: hotspotHover.hovered && !photoHotspot.isSelected
                                    radius: 7
                                    color: tokens.halo
                                }
                            }
                        }

                        Connections {
                            target: photoImage
                            function onPaintedWidthChanged() {
                                root.scheduleConnectorRepaint()
                            }
                            function onPaintedHeightChanged() {
                                root.scheduleConnectorRepaint()
                            }
                        }
                    }

                }

                Item {
                    id: rightSideCards
                    objectName: "rightMappingCards"
                    x: parent.width - width
                    y: 0
                    width: mappingList.sideCardWidth
                    height: parent.height
                    z: 3
                    onXChanged: root.scheduleConnectorRepaint()
                    onYChanged: root.scheduleConnectorRepaint()
                    onWidthChanged: root.scheduleConnectorRepaint()
                    onHeightChanged: root.scheduleConnectorRepaint()

                    Repeater {
                        id: rightCardRepeater
                        model: ButtonMappingModel
                        onItemAdded: root.scheduleConnectorRepaint()
                        delegate: MappingCard {
                            required property int index
                            required property string buttonId
                            required property string displayName
                            required property string actionText
                            required property string doubleClickText
                            required property string longPressText
                            required property string singleNote
                            required property string doubleNote
                            required property string longNote
                            required property bool isSelected

                            visible: !root.isLeftButton(buttonId)
                            enabled: true
                            exposeObjectNames: visible
                            tokens: root.tokens
                            cardId: buttonId
                            x: 0
                            y: visible ? root.cardY(buttonId) : 0
                            width: mappingList.sideCardWidth
                            height: root.mappingCardHeight
                            buttonName: root.shortButtonName(buttonId)
                            keyLabelWidth: root.mappingKeyLabelWidth
                            singleText: actionText
                            doubleText: doubleClickText
                            longText: longPressText
                            singleNoteText: singleNote
                            doubleNoteText: doubleNote
                            longNoteText: longNote
                            selected: isSelected
                            voiceAction: SettingsController.isVoiceActionText(actionText.trim())
                                || actionText.indexOf("已停用：旧语音配置") === 0
                            onXChanged: root.scheduleConnectorRepaint()
                            onYChanged: root.scheduleConnectorRepaint()
                            onWidthChanged: root.scheduleConnectorRepaint()
                            onHeightChanged: root.scheduleConnectorRepaint()
                            onVisibleChanged: root.scheduleConnectorRepaint()
                            Component.onCompleted: root.scheduleConnectorRepaint()
                            onClicked: {
                                SettingsController.selectButton(buttonId)
                                actionEditor.openForRow(
                                    index, buttonId, root.shortButtonName(buttonId), actionText,
                                    doubleClickText, longPressText,
                                    singleNote, doubleNote, longNote
                                )
                            }
                        }
                    }
                }
            }

            Item { Layout.fillHeight: true }

            RowLayout {
                id: mappingActionsPanel
                objectName: "mappingActionsPanel"
                // 稿子 .toolbar：高 32，margin-top auto，gap 4。
                Layout.fillWidth: true
                Layout.preferredHeight: 32
                spacing: 4

                // 稿子 .toolbar 结构：左组（检测/恢复默认）贴左，
                // 中间 .sp 弹性留白，右组（驱动状态 + 安装/卸载）贴右。
                CompactButton {
                    id: detectRealKeyButton
                    objectName: "detectRealKeyButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth4Chars
                    // 「检测按键」/「停止检测」两态同宽，切换时整行不跳动；
                    // 比 4 字宽多留 8px，够实心态的内边距，文字不会被省略成「停止…」。
                    Layout.preferredWidth: tokens.buttonWidth4Chars + 8
                    text: SettingsController.keyDetectionActive
                        ? qsTr("停止检测") : qsTr("检测按键")
                    enabled: SettingsController.activeRemoteReady
                    highlighted: SettingsController.keyDetectionActive
                    onClicked: SettingsController.keyDetectionActive
                        ? SettingsController.stopKeyDetection()
                        : SettingsController.startKeyDetection()
                    Accessible.name: qsTr("检测真实遥控器按键")
                }
                CompactButton {
                    id: restoreMappingDefaultsButton
                    objectName: "restoreMappingDefaultsButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth4Chars
                    Layout.preferredWidth: tokens.buttonWidth4Chars
                    flat: true
                    text: qsTr("恢复默认")
                    onClicked: restoreMappingDefaultsDialog.open()
                    KeyNavigation.tab: root.lastFocusItem
                }
                // 全局 + 各应用布局的整体备份：一个弹窗里既能导出也能导入。
                CompactButton {
                    id: mappingBundleButton
                    objectName: "mappingBundleButton"
                    tokens: root.tokens
                    // 和「恢复默认」一致的文字按钮样式。
                    flat: true
                    text: qsTr("导入/导出")
                    enabled: !SettingsController.configurationReadOnly
                    onClicked: mappingBundleDialog.open()
                    Accessible.name: qsTr("导入或导出按键配置")
                }
                // 稿子 .sp：弹性留白，把驱动状态与安装按钮推到右侧。
                Item { Layout.fillWidth: true }
                // 稿子 .drv：6px 空心圆 + 「驱动」+ 粗体状态，margin-right 4。
                RowLayout {
                    objectName: "interceptionDriverStatus"
                    Layout.preferredHeight: 24
                    Layout.rightMargin: 4
                    spacing: 6

                    Rectangle {
                        Layout.preferredWidth: 6
                        Layout.preferredHeight: 6
                        Layout.alignment: Qt.AlignVCenter
                        radius: 3
                        color: "transparent"
                        border.width: 1.5
                        border.color: tokens.textTertiary
                    }
                    UiLabel {
                        id: interceptionDriverTitle
                        objectName: "actionEditorInterceptionDriverTitle"
                        tokens: root.tokens
                        kind: noteKind
                        text: qsTr("驱动")
                        font.pixelSize: tokens.fontSizeControl
                        color: tokens.textTertiary
                        verticalAlignment: Text.AlignVCenter
                        HoverHandler { id: interceptionDriverTitleHover }
                        CompactToolTip {
                            tokens: root.tokens
                            active: interceptionDriverTitleHover.hovered
                            text: qsTr("Interception 需要一次性安装内核驱动（管理员权限，安装后需重启电脑）；此处的按钮只启动随包的官方安装程序，绝不会静默安装")
                        }
                    }
                    UiLabel {
                        objectName: "interceptionDriverState"
                        tokens: root.tokens
                        kind: noteKind
                        text: SettingsController.interceptionAvailable
                            ? qsTr("已安装") : qsTr("未安装")
                        font.pixelSize: tokens.fontSizeControl
                        font.weight: Font.DemiBold
                        color: tokens.textSecondary
                        verticalAlignment: Text.AlignVCenter
                    }
                }
                UiLabel {
                    objectName: "actionEditorInterceptionDriverMessage"
                    tokens: root.tokens
                    kind: noteKind
                    Layout.maximumWidth: 200
                    // 稿子工具栏不放说明文字；仅在安装/卸载后反馈结果。
                    visible: root.interceptionDriverMessage.length > 0
                    text: SettingsController.interceptionInstallerAvailable
                        ? root.interceptionDriverMessage
                        : SettingsController.interceptionInstallerUnavailableReason
                    elide: Text.ElideRight
                    HoverHandler { id: interceptionDriverMessageHover }
                    CompactToolTip {
                        tokens: root.tokens
                        active: interceptionDriverMessageHover.hovered && text.length > 0
                        text: SettingsController.interceptionInstallerAvailable
                            ? root.interceptionDriverMessage
                            : SettingsController.interceptionInstallerUnavailableReason
                    }
                }
                CompactButton {
                    id: installInterceptionDriverButton
                    objectName: "actionEditorInterceptionInstallButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth4Chars
                    flat: true
                    // 未安装时显示「安装驱动」，已安装时换成「卸载驱动」。
                    visible: !SettingsController.interceptionAvailable
                    enabled: SettingsController.interceptionInstallerAvailable
                    text: qsTr("安装驱动")
                    Accessible.name: qsTr("安装 Interception 驱动")
                    onClicked: interceptionInstallConfirmDialog.open()
                    KeyNavigation.tab: root.tabTarget
                }
                CompactButton {
                    id: uninstallInterceptionDriverButton
                    objectName: "actionEditorInterceptionUninstallButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth4Chars
                    flat: true
                    visible: SettingsController.interceptionAvailable
                    enabled: SettingsController.interceptionInstallerAvailable
                    text: qsTr("卸载驱动")
                    Accessible.name: qsTr("卸载 Interception 驱动")
                    onClicked: {
                        const result = SettingsController.uninstallInterceptionDriver()
                        root.interceptionDriverMessage = result.message
                    }
                    KeyNavigation.tab: root.tabTarget
                }
            }

        }

        // 操作反馈气泡：浮在工具栏上方，不参与布局、不挤占按钮。
        Rectangle {
            id: mappingMessageBubble
            objectName: "mappingMessageBubble"
            property bool dismissed: false
            anchors.horizontalCenter: parent.horizontalCenter
            // 与其它页面的提示胶囊同一水平线：中线对齐底部工具按钮行的中线
            //（按钮行高 32、贴页面下边缘，中线距页面底 16）。
            y: root.height - 16 - height / 2
            z: 20
            width: Math.min(parent.width - 40, mappingMessageLabel.implicitWidth + 22)
            height: root.messageBubbleHeight
            radius: height / 2
            visible: root.mappingMessage.length > 0 && !dismissed
            color: root.tokens.surface
            border.width: 1
            border.color: root.tokens.line

            onVisibleChanged: {
                if (visible) {
                    dismissed = false
                    mappingMessageDismissTimer.restart()
                }
            }
            Connections {
                target: root
                function onMappingMessageChanged() {
                    mappingMessageBubble.dismissed = false
                    mappingMessageDismissTimer.restart()
                }
            }

            UiLabel {
                id: mappingMessageLabel
                objectName: "mappingMessage"
                anchors.centerIn: parent
                tokens: root.tokens
                kind: noteKind
                width: Math.min(implicitWidth, 520)
                // 与其它页面的提示胶囊一致：行高 1 + 垂直居中，胶囊内上下留白相同
                lineHeight: 1
                verticalAlignment: Text.AlignVCenter
                // 气泡是短提示，不带句尾句号
                text: root.mappingMessage.replace(/。+$/, "")
                color: root.tokens.textSecondary
                elide: Text.ElideRight
                maximumLineCount: 1
            }

            Timer {
                id: mappingMessageDismissTimer
                interval: 5000
                onTriggered: mappingMessageBubble.dismissed = true
            }
        }

    }

}
