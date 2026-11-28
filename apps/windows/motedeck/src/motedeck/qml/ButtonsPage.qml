// "按键" tab: an RC003 product-photo reference with a selected-button marker
// sits beside the existing one-row-per-button mapping matrix. Mapping selection
// and editing remain owned by the matrix and real-key detection.
// SettingsController/ButtonMappingModel are QML singletons - see main.qml.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Effects
import QtQuick.Shapes
import QtQuick.Dialogs
import QtQuick.Controls.FluentWinUI3 as Fluent
import MoteDeckSettings 1.0

Item {
    id: root
    // 窗口级事件过滤器（qt_settings_app）按这个名字定位按键页根 item，
    // 用来在编辑面板打开期间转发鼠标位置（见 editorHoverScenePos）。
    objectName: "buttonsPageRoot"
    property var tokens
    MappingAppPickerDialog { id: mappingAppPickerDialog; tokens: root.tokens }
    // 「启动应用」单选用这个窗口，和多应用配置那个互不影响。
    LaunchAppPickerDialog {
        id: launchAppPickerDialog
        tokens: root.tokens
        onApplicationSelected: function(process) {
            // 从组合动作的某一步打开时，选中结果回填到那一步，而不是按键映射。
            if (root.appPickerSource === "composite") {
                compositeActionDialog.setApplicationTarget(process)
                launchAppPickerDialog.close()
                return
            }
            if (root.appPickerSource === "close_application") {
                actionEditor.applyCloseApplication(
                    closeApplicationDialog.targetRow,
                    closeApplicationDialog.targetTrigger,
                    "specified", process)
                launchAppPickerDialog.close()
                closeApplicationDialog.close()
                return
            }
            root.applyPickedApplication(process)
        }
    }
    // 默认用于按键映射；组合动作里的「启动应用」步骤复用它（三级弹窗）。
    property string appPickerSource: "mapping"
    // 聚焦输入框弹窗同理：按键映射 / 组合动作的某一步。
    property string focusDialogSource: "mapping"
    // 「聚焦输入框」的完整配置弹窗：确认后回填带方式的动作文本。
    FocusInputDialog {
        id: focusInputDialog
        tokens: root.tokens
        hoverScenePos: root.editorHoverScenePos
        property int targetRow: -1
        property string targetTrigger: "single_click"
        onFocusConfigured: function(text) {
            // 从组合动作某一步打开时，把配置回填到那一步。
            if (root.focusDialogSource === "composite") {
                compositeActionDialog.setFocusConfig(
                    SettingsController.focusActionPayload(text))
                return
            }
            actionEditor.applyCapturedShortcut(targetRow, targetTrigger, text)
        }
    }
    // 「关闭应用」先选方式；「关闭指定应用」再复用上面的应用选择器。
    CloseApplicationDialog {
        id: closeApplicationDialog
        tokens: root.tokens
        // 弹窗打开时普通 hover 事件会被编辑面板的 Overlay 吞掉，改用页面
        // 已有的窗口级鼠标位置旁路来算悬停。
        hoverScenePos: root.editorHoverScenePos
        onCurrentRequested: {
            actionEditor.applyCloseApplication(
                targetRow, targetTrigger, "current", "")
            close()
        }
        onSpecifiedRequested: {
            // 保持本弹窗开着，应用选择器作为三级弹窗叠在上面。
            appPickerRowIndex = targetRow
            appPickerTrigger = targetTrigger
            root.appPickerSource = "close_application"
            launchAppPickerDialog.purpose = "close"
            launchAppPickerDialog.selectedProcess = ""
            launchAppPickerDialog.open()
        }
    }
    // 「组合动作」编辑弹窗：确认后把排好的步骤回填成一行动作文本。
    CompositeActionDialog {
        id: compositeActionDialog
        tokens: root.tokens
        property int targetRow: -1
        property string targetTrigger: "single_click"
        onStepsAccepted: function(text) {
            actionEditor.applyCapturedShortcut(targetRow, targetTrigger, text)
        }
        onApplicationPickRequested: {
            root.appPickerSource = "composite"
            launchAppPickerDialog.purpose = "launch"
            launchAppPickerDialog.selectedProcess =
                compositeActionDialog.pendingApplicationTarget()
            launchAppPickerDialog.open()
        }
        onFocusConfigRequested: {
            root.focusDialogSource = "composite"
            focusInputDialog.openForPayload(
                compositeActionDialog.pendingFocusPayload())
        }
    }
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
    // 实物照片上每颗按键的真实形状，坐标按照片宽高归一化，圆心就是
    // 键帽中心。编辑时用它画亮绿描边，能严丝合缝地盖住键帽本身。
    // RC001（小米遥控器2）和 RC003 各自实测一张表；两个的方向盘都
    // 没有印刷箭头，四段 90° 弧画在 ok 边缘与盘缘凹槽之间的同一比例处。
    readonly property var photoKeyShapes: SettingsController.isRc001Device ? ({
        "power": { kind: "circle", cx: 0.4131, cy: 0.1148, w: 0.0888, h: 0.0592 },
        "mic": { kind: "circle", cx: 0.5793, cy: 0.1148, w: 0.0889, h: 0.0593 },
        "up": { kind: "arc", cx: 0.4965, cy: 0.2663, rx: 0.1101, ry: 0.0734,
            start: 225, sweep: 90 },
        "left": { kind: "arc", cx: 0.4965, cy: 0.2663, rx: 0.1101, ry: 0.0734,
            start: 135, sweep: 90 },
        "ok": { kind: "circle", cx: 0.4959, cy: 0.2655, w: 0.1485, h: 0.0990 },
        "right": { kind: "arc", cx: 0.4965, cy: 0.2663, rx: 0.1101, ry: 0.0734,
            start: -45, sweep: 90 },
        "down": { kind: "arc", cx: 0.4965, cy: 0.2663, rx: 0.1101, ry: 0.0734,
            start: 45, sweep: 90 },
        "back": { kind: "circle", cx: 0.4239, cy: 0.4140, w: 0.1103, h: 0.0736 },
        "volume_up": { kind: "circle", cx: 0.5685, cy: 0.4139,
            w: 0.1109, h: 0.0739 },
        "home": { kind: "circle", cx: 0.4242, cy: 0.5008, w: 0.1106, h: 0.0737 },
        "volume_down": { kind: "circle", cx: 0.5689, cy: 0.5009,
            w: 0.1106, h: 0.0737 },
        "menu": { kind: "circle", cx: 0.4242, cy: 0.5876, w: 0.1102, h: 0.0735 },
        "tv": { kind: "circle", cx: 0.5690, cy: 0.5875, w: 0.1104, h: 0.0736 }
    }) : SettingsController.isRc003Device ? ({
        "power": { kind: "circle", cx: 0.4042, cy: 0.1138, w: 0.0778, h: 0.0519 },
        "mic": { kind: "circle", cx: 0.5874, cy: 0.1138, w: 0.0778, h: 0.0519 },
        // 方向键是同一圆环上的四段弧，半径取环带中线，角度按屏幕坐标
        // （0° 在 3 点钟、顺时针为正）。
        "up": { kind: "arc", cx: 0.4957, cy: 0.2602, rx: 0.1117, ry: 0.0745,
            start: 225, sweep: 90 },
        "left": { kind: "arc", cx: 0.4957, cy: 0.2602, rx: 0.1117, ry: 0.0745,
            start: 135, sweep: 90 },
        "ok": { kind: "circle", cx: 0.4951, cy: 0.2597, w: 0.1540, h: 0.1027 },
        "right": { kind: "arc", cx: 0.4957, cy: 0.2602, rx: 0.1117, ry: 0.0745,
            start: -45, sweep: 90 },
        "down": { kind: "arc", cx: 0.4957, cy: 0.2602, rx: 0.1117, ry: 0.0745,
            start: 45, sweep: 90 },
        "back": { kind: "circle", cx: 0.4218, cy: 0.4079, w: 0.1161, h: 0.0775 },
        "volume_up": { kind: "circle", cx: 0.5686, cy: 0.4069,
            w: 0.1139, h: 0.0750 },
        "home": { kind: "circle", cx: 0.4218, cy: 0.4976, w: 0.1161, h: 0.0768 },
        "volume_down": { kind: "circle", cx: 0.5686, cy: 0.4977,
            w: 0.1139, h: 0.0741 },
        "menu": { kind: "circle", cx: 0.4218, cy: 0.5879, w: 0.1161, h: 0.0772 },
        "tv": { kind: "circle", cx: 0.5688, cy: 0.5877, w: 0.1161, h: 0.0772 }
    }) : ({
        // 谷歌遥控器（453×1443 照片实测）：前脸都是圆形键，方向键是同一个
        // 圆环（外径 394、内径 148，环带中线半径 135）上的四段 90° 弧。
        "up": { kind: "arc", cx: 0.4960, cy: 0.1575, rx: 0.2989, ry: 0.0938,
            start: 225, sweep: 90 },
        "left": { kind: "arc", cx: 0.4960, cy: 0.1575, rx: 0.2989, ry: 0.0938,
            start: 135, sweep: 90 },
        "right": { kind: "arc", cx: 0.4960, cy: 0.1575, rx: 0.2989, ry: 0.0938,
            start: -45, sweep: 90 },
        "down": { kind: "arc", cx: 0.4960, cy: 0.1575, rx: 0.2989, ry: 0.0938,
            start: 45, sweep: 90 },
        "ok": { kind: "circle", cx: 0.4949, cy: 0.1568, w: 0.3262, h: 0.1024 },
        "back": { kind: "circle", cx: 0.2499, cy: 0.3582, w: 0.3122, h: 0.0980 },
        "mic": { kind: "circle", cx: 0.7318, cy: 0.3590, w: 0.3038, h: 0.0954 },
        "home": { kind: "circle", cx: 0.2478, cy: 0.4859, w: 0.3121, h: 0.0980 },
        "volume_mute": { kind: "circle", cx: 0.7288, cy: 0.4866,
            w: 0.3122, h: 0.0980 },
        "youtube": { kind: "circle", cx: 0.2486, cy: 0.6131, w: 0.3091, h: 0.0970 },
        "netflix": { kind: "circle", cx: 0.7253, cy: 0.6131, w: 0.3091, h: 0.0970 },
        "power": { kind: "circle", cx: 0.2468, cy: 0.7334, w: 0.1752, h: 0.0550 },
        "input_source": { kind: "circle", cx: 0.7277, cy: 0.7341,
            w: 0.1751, h: 0.0550 },
        // 侧边音量摇杆在这张正面图上只露出右缘一条窄条，按上下两半画成胶囊。
        "volume_up": { kind: "rect", cx: 0.9790, cy: 0.2401,
            w: 0.0287, h: 0.0797 },
        "volume_down": { kind: "rect", cx: 0.9790, cy: 0.3174,
            w: 0.0287, h: 0.0748 }
    })
    // 没有精确键形数据时退回热点框：接近正方形画圆，长条画胶囊。
    // aspect 是照片绘制区的高宽比，用来把像素上的圆换算成按宽高归一化的值。
    function photoKeySpec(buttonId, hx, hy, hw, hh, aspect) {
        const exact = photoKeyShapes[buttonId]
        if (exact)
            return exact
        const round = Math.abs(hw - hh) <= Math.min(hw, hh) * 0.5
        if (round) {
            const side = Math.min(hw, hh * (aspect || 1))
            return { kind: "circle", cx: hx, cy: hy,
                w: side, h: side / (aspect || 1) }
        }
        return { kind: "rect", cx: hx, cy: hy, w: hw, h: hh }
    }
    // 设计稿 .board：高 448，卡片 300×56，行距 64，中间 .stage 140 宽。
    readonly property real mappingBoardHeight: 448
    readonly property real mappingCardWidth: 300
    readonly property real mappingCardHeight: 56
    readonly property real mappingRowPitch: 64
    readonly property real mappingPhotoColumnWidth: 140
    readonly property real mappingKeyLabelWidth: 36
    // 选中某个按键后右侧滑出的动作面板宽度；面板打开时板上的卡片和
    // 连线一起收起，实物图退到剩下的区域里居中，模仿 Logi Options+。
    readonly property real mappingPanelWidth: 420
    readonly property bool mappingPanelOpen: actionEditor.visible
    // 布局用的开合开关：面板一发起关闭就算收起，遥控器立刻跟着卡片一起
    // 滑回连线布局，不用停在原地等面板滑完那 300ms。点击判断仍用
    // mappingPanelOpen（面板还挡在上面时不要响应）。
    readonly property bool mappingPanelExpanded: actionEditor.panelShown
    // 进入 / 退出编辑模式只跑这一条时间线：遥控器、卡片、连线和右侧面板
    // 全按同一套节奏走，不再各自为政。
    //   进入：卡片/连线 120ms 快速让位，遥控器与面板同时滑 300ms；
    //   退出：遥控器与面板同时滑回 300ms，悬浮卡片自己 120ms 收起，
    //         卡片/连线等它收完（140ms）再花 260ms 渐显，两段不重叠。
    readonly property int editSlideDuration: 300
    readonly property int editFadeOutDuration: 120
    readonly property int editFadeInDelay: 140
    readonly property int editFadeInDuration: 260
    states: [
        State {
            name: "editing"
            when: root.mappingPanelExpanded
            PropertyChanges { target: leftSideCards; opacity: 0 }
            PropertyChanges { target: rightSideCards; opacity: 0 }
            PropertyChanges { target: mappingLines; opacity: 0 }
            PropertyChanges { target: activeMappingLine; opacity: 0 }
            PropertyChanges { target: panelShift; x: photoSidebar.panelShiftX }
        }
    ]
    transitions: [
        Transition {
            to: "editing"
            ParallelAnimation {
                NumberAnimation {
                    target: leftSideCards; property: "opacity"
                    duration: root.editFadeOutDuration
                    easing.type: Easing.OutQuad
                }
                NumberAnimation {
                    target: rightSideCards; property: "opacity"
                    duration: root.editFadeOutDuration
                    easing.type: Easing.OutQuad
                }
                NumberAnimation {
                    target: mappingLines; property: "opacity"
                    duration: root.editFadeOutDuration
                    easing.type: Easing.OutQuad
                }
                NumberAnimation {
                    target: activeMappingLine; property: "opacity"
                    duration: root.editFadeOutDuration
                    easing.type: Easing.OutQuad
                }
                NumberAnimation {
                    target: panelShift; property: "x"
                    duration: root.editSlideDuration
                    easing.type: Easing.OutCubic
                }
            }
        },
        Transition {
            from: "editing"
            ParallelAnimation {
                NumberAnimation {
                    target: panelShift; property: "x"
                    duration: root.editSlideDuration
                    easing.type: Easing.OutCubic
                }
                SequentialAnimation {
                    PauseAnimation { duration: root.editFadeInDelay }
                    ParallelAnimation {
                        NumberAnimation {
                            target: mappingLines; property: "opacity"
                            duration: root.editFadeInDuration
                            easing.type: Easing.OutCubic
                        }
                        NumberAnimation {
                            target: activeMappingLine; property: "opacity"
                            duration: root.editFadeInDuration
                            easing.type: Easing.OutCubic
                        }
                        NumberAnimation {
                            target: leftSideCards; property: "opacity"
                            duration: root.editFadeInDuration
                            easing.type: Easing.OutCubic
                        }
                        NumberAnimation {
                            target: rightSideCards; property: "opacity"
                            duration: root.editFadeInDuration
                            easing.type: Easing.OutCubic
                        }
                    }
                }
            }
        }
    ]
    // 提示气泡与其他页面统一：同样距内容区底部 16，浮在底部那一行上。
    readonly property real messageBubbleHeight: 26
    property var backTabTarget: null
    property var tabTarget: null
    property string winuhidDriverMessage: ""
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
    // 「启动应用」选择应用的上下文：只回填用户点的那一行。
    property int appPickerRowIndex: -1
    property string appPickerTrigger: "single_click"
    readonly property var firstFocusItem: mappingAppsBar
    readonly property var lastFocusItem: driverManagerButton
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

    // 换页时只把编辑面板藏起来：不改面板开合状态，遥控器位置、卡片和连线
    // 全部保持原样，切回来原样打开，整页不会因为换页播放任何动画。
    function hideEditorForPageSwitch() {
        if (!actionEditor.visible)
            return
        panelCloseTimer.stop()
        actionEditor.hiddenByPageSwitch = true
        actionEditor.visible = false
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

    // 编辑面板是非模态 Popup，但它一打开，Qt 的 Overlay 层就会吞掉页面
    // 区域的 hover 事件（offscreen 探针实测：面板打开时照片热区的
    // HoverHandler 收不到悬浮事件，挂在 Overlay 上的 MouseArea 也一样收
    // 不到——事件被 Popup 的事件过滤器重定向后就不再向普通 item 传递）。
    // QML 层无解，改由 qt_settings_app 的窗口级事件过滤器在面板打开期间
    // 把鼠标位置写进 editorHoverScenePos，页面用热区矩形自己算命中；鼠标
    // 离开窗口或面板关闭时过滤器写入负坐标/页面自行清空。面板关闭时的
    // hover 仍走热区自己的 HoverHandler（连线模式的小圆点不受影响）。
    property string editorHoverButtonId: ""
    // 编辑面板打开期间由窗口事件过滤器写入的鼠标位置（窗口坐标）；x < 0
    // 表示清空悬浮状态。
    property point editorHoverScenePos: Qt.point(-1, -1)
    onEditorHoverScenePosChanged: {
        if (editorHoverScenePos.x < 0 || anyActionDialogOpen)
            editorHoverButtonId = ""
        else
            editorHoverButtonId = editorHoverButtonAt(
                editorHoverScenePos.x, editorHoverScenePos.y)
    }
    // 面板关闭后清掉残留的悬浮高亮（过滤器在面板关闭后不再写入）。
    onMappingPanelOpenChanged: if (!mappingPanelOpen) editorHoverButtonId = ""
    // 任何弹窗盖在按键页上时都不再显示跟随鼠标的按键卡片，避免卡片
    // 叠在弹窗上面；弹窗打开时同时清掉悬浮状态，关闭后不会残留闪现。
    readonly property bool anyActionDialogOpen:
        mappingAppPickerDialog.visible
        || launchAppPickerDialog.visible
        || focusInputDialog.visible
        || closeApplicationDialog.visible
        || compositeActionDialog.visible
        || shortcutRecorder.visible
        || actionEditorNoteDialog.visible
        || actionEditorDiscardConfirmDialog.visible
        || restoreMappingDefaultsDialog.visible
        || mappingBundleDialog.visible
        || mappingImportConfirmDialog.visible
        || mappingBundleExportDialog.visible
        || mappingBundleImportDialog.visible
        || removeMappingAppConfirmDialog.visible
        || pluginAppLockedDialog.visible
        || winuhidInstallConfirmDialog.visible
        || interceptionInstallConfirmDialog.visible
        || driverManagerDialog.visible
    onAnyActionDialogOpenChanged: if (anyActionDialogOpen) editorHoverButtonId = ""
    // 编辑状态退出、回到连线页面：持续检测随之自动结束。
    onMappingPanelExpandedChanged: if (!mappingPanelExpanded
            && SettingsController.keyDetectionActive)
        SettingsController.stopKeyDetection()
    function editorHoverButtonAt(sceneX, sceneY) {
        for (let i = 0; i < photoHotspotRepeater.count; ++i) {
            const item = photoHotspotRepeater.itemAt(i)
            if (!item)
                continue
            // mapFromItem(null, ...) 是 QML 里唯一的场景坐标换算入口
            // （Item 的 mapFromScene/mapToScene 不是 QML 可调用方法）。
            const p = item.mapFromItem(null, sceneX, sceneY)
            if (p.x >= 0 && p.y >= 0 && p.x <= item.width
                    && p.y <= item.height)
                return item.buttonId
        }
        return ""
    }

    // 跟随鼠标的悬浮卡片只在编辑状态出现；连线状态不显示任何卡片。
    // 卡片里一行手势的显示文字：有备注就显示备注（与按键卡片一致），
    // 未设置的返回空串，由卡片统一显示灰色的「未设置」。
    function photoHoverText(bindingText, noteText) {
        const binding = (bindingText || "").trim()
        if (binding.length === 0 || binding === qsTr("未设置"))
            return ""
        const note = (noteText || "").trim()
        if (note.length > 0 && note !== qsTr("未命名"))
            return note
        return SettingsController.formatActionText(binding)
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
        // 面板展开时连线整体淡出、实物图正在滑动：这段时间不用重画，
        // 免得动画每帧都重建 Canvas 纹理。面板一发起关闭（expanded 变
        // false）就正常重画，遥控器滑回的最后每一帧都跟着走。
        if (root.mappingPanelExpanded)
            return
        Qt.callLater(root.paintConnectorsNow)
    }

    function isLeftButton(buttonId) {
        return leftButtonIds.indexOf(buttonId) >= 0
    }

    // 面板开着时切换了应用：用新应用的映射把同一颗按键的面板内容刷新一遍，
    // 不用再点一次这颗按键。
    function refreshOpenEditor() {
        if (!actionEditor.visible || actionEditor.rowIndex < 0)
            return
        const buttonId = actionEditor.buttonId
        const index = actionEditor.rowIndex
        const card = isLeftButton(buttonId)
            ? leftCardRepeater.itemAt(index) : rightCardRepeater.itemAt(index)
        if (!card)
            return
        actionEditor.openForRow(
            index, buttonId, shortButtonName(buttonId),
            card.actionText, card.doubleClickText, card.longPressText,
            card.singleNote, card.doubleNote, card.longNote)
    }

    // 检测到真实按键后，编辑状态下直接切到那颗键的编辑面板（不用再手点
    // 一次）；连线状态只高亮，保持原样不弹面板。
    function openEditorForDetectedButton(buttonId) {
        if (!root.mappingPanelExpanded || !buttonId)
            return
        if (actionEditor.visible && actionEditor.buttonId === buttonId)
            return
        let hotspot = null
        for (let i = 0; i < photoHotspotRepeater.count; ++i) {
            const item = photoHotspotRepeater.itemAt(i)
            if (item && item.buttonId === buttonId) {
                hotspot = item
                break
            }
        }
        if (!hotspot)
            return
        actionEditor.openForRow(
            hotspot.index, buttonId, shortButtonName(buttonId),
            hotspot.actionText, hotspot.doubleClickText,
            hotspot.longPressText, hotspot.singleNote,
            hotspot.doubleNote, hotspot.longNote)
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

    function openApplicationPicker(rowIndex, trigger, currentText) {
        appPickerRowIndex = rowIndex
        appPickerTrigger = trigger || "single_click"
        appPickerSource = "mapping"
        launchAppPickerDialog.purpose = "launch"
        // 这一行存的就是全部记忆：改成别的动作后文本变了，勾选自然也消失。
        launchAppPickerDialog.selectedProcess =
            SettingsController.applicationLaunchTarget(currentText)
        launchAppPickerDialog.open()
    }

    function applyPickedApplication(process) {
        actionEditor.applyPickedApplication(
            appPickerRowIndex, appPickerTrigger, process)
        launchAppPickerDialog.close()
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
        width: Math.min(430, root.width - 28)
        modal: true
        anchors.centerIn: parent
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
        width: Math.min(430, root.width - 28)
        modal: true
        anchors.centerIn: parent
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
        id: winuhidInstallConfirmDialog
        tokens: root.tokens
        preferredWidth: 340 + tokens.dialogPadding * 2
        objectName: "winuhidInstallConfirmDialog"
        title: qsTr("安装驱动？")
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: {
            const result = SettingsController.installWinUHidDriver()
            root.winuhidDriverMessage = result.message
        }

        UiLabel {
            tokens: root.tokens
            kind: bodyKind
            width: 340
            wrapMode: Text.WordWrap
            text: qsTr("安装需要管理员权限，软件只会启动随包附带的安装脚本，绝不会静默安装。确定现在安装吗？")
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
            root.winuhidDriverMessage = result.message
        }

        UiLabel {
            tokens: root.tokens
            kind: bodyKind
            width: 340
            wrapMode: Text.WordWrap
            text: qsTr("安装需要管理员权限，软件只会启动随包附带的安装程序，绝不会静默安装。确定现在安装吗？")
        }
    }

    // 驱动管理：两种驱动都列在这里，装哪种、装几种、什么时候卸都交给用户决定。
    SettingsDialog {
        id: driverManagerDialog
        tokens: root.tokens
        preferredWidth: 440 + tokens.dialogPadding * 2
        objectName: "driverManagerDialog"
        title: qsTr("输入驱动管理")
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Ok

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingLarge

            UiLabel {
                tokens: root.tokens
                kind: bodyKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("两种驱动只能安装其一，不能同时安装；都不安装时，沿用系统原生按键发送方式。安装后可随时在此卸载。")
            }

            UiLabel {
                objectName: "driverManagerMessage"
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                visible: root.winuhidDriverMessage.length > 0
                text: root.winuhidDriverMessage
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: root.tokens.hairlineWidth
                color: root.tokens.line
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: root.tokens.spacingSmall

                RowLayout {
                    Layout.fillWidth: true
                    spacing: root.tokens.spacingSmall

                    UiLabel {
                        tokens: root.tokens
                        kind: bodyKind
                        Layout.alignment: Qt.AlignTop
                        text: qsTr("WinUHid 虚拟键盘")
                        font.weight: Font.DemiBold
                    }
                    Rectangle {
                        id: recommendedBadge
                        objectName: "driverManagerWinUHidRecommended"
                        // 和左侧标题一样按行顶对齐；高度只取文字墨迹加 2px 内边距，
                        // 文字再上移到墨迹顶边，避免下方留出空白。
                        Layout.alignment: Qt.AlignTop
                        Layout.preferredHeight: implicitHeight
                        readonly property int glyphPadding: 2
                        readonly property rect glyphRect:
                            recommendedBadgeMetrics.tightBoundingRect(recommendedLabel.text)
                        radius: 4
                        color: root.tokens.accentSoft
                        border.width: root.tokens.hairlineWidth
                        border.color: root.tokens.line
                        implicitWidth: recommendedLabel.implicitWidth + root.tokens.spacingSmall * 2
                        implicitHeight: Math.ceil(glyphRect.height + glyphPadding * 2)

                        FontMetrics {
                            id: recommendedBadgeMetrics
                            font: recommendedLabel.font
                        }

                        UiLabel {
                            id: recommendedLabel
                            tokens: root.tokens
                            kind: noteKind
                            x: Math.round((recommendedBadge.width - width) / 2)
                            y: Math.round(recommendedBadge.glyphPadding
                                - (recommendedBadgeMetrics.ascent + recommendedBadge.glyphRect.y))
                            color: root.tokens.textPrimary
                            text: qsTr("推荐")
                        }
                    }
                    UiLabel {
                        objectName: "driverManagerWinUHidState"
                        tokens: root.tokens
                        kind: noteKind
                        Layout.alignment: Qt.AlignTop
                        text: SettingsController.winuhidDriverInstalled
                            ? qsTr("已安装") : qsTr("未安装")
                    }
                    Item { Layout.fillWidth: true }
                    CompactButton {
                        objectName: "driverManagerWinUHidButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth4Chars
                        enabled: SettingsController.winuhidInstallerAvailable
                            && (SettingsController.winuhidDriverInstalled
                                || !SettingsController.interceptionDriverInstalled)
                        text: SettingsController.winuhidDriverInstalled
                            ? qsTr("卸载") : qsTr("安装")
                        onClicked: {
                            if (SettingsController.winuhidDriverInstalled) {
                                const result = SettingsController.uninstallWinUHidDriver()
                                root.winuhidDriverMessage = result.message
                            } else {
                                winuhidInstallConfirmDialog.open()
                            }
                        }
                    }
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: qsTr("适用：聊天、浏览器、办公等绝大多数常规应用。")
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: qsTr("优势：不接管物理键盘，外接键盘可正常使用；对安全软件更友好；安装后相当于系统新增一个真实键盘设备。")
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: qsTr("局限：需管理员权限安装，通常无需重启（系统提示时重启一次即可）；极少数仅识别物理键盘的旧版程序可能收不到按键。")
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    visible: !SettingsController.winuhidInstallerAvailable
                    text: SettingsController.winuhidInstallerUnavailableReason
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: root.tokens.hairlineWidth
                color: root.tokens.line
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: root.tokens.spacingSmall

                RowLayout {
                    Layout.fillWidth: true
                    spacing: root.tokens.spacingSmall

                    UiLabel {
                        tokens: root.tokens
                        kind: bodyKind
                        text: qsTr("Interception 键盘过滤驱动")
                        font.weight: Font.DemiBold
                    }
                    UiLabel {
                        objectName: "driverManagerInterceptionState"
                        tokens: root.tokens
                        kind: noteKind
                        text: SettingsController.interceptionDriverInstalled
                            ? qsTr("已安装") : qsTr("未安装")
                    }
                    Item { Layout.fillWidth: true }
                    CompactButton {
                        objectName: "driverManagerInterceptionButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth4Chars
                        enabled: SettingsController.interceptionInstallerAvailable
                            && (SettingsController.interceptionDriverInstalled
                                || !SettingsController.winuhidDriverInstalled)
                        text: SettingsController.interceptionDriverInstalled
                            ? qsTr("卸载") : qsTr("安装")
                        onClicked: {
                            if (SettingsController.interceptionDriverInstalled) {
                                const result = SettingsController.uninstallInterceptionDriver()
                                root.winuhidDriverMessage = result.message
                            } else {
                                interceptionInstallConfirmDialog.open()
                            }
                        }
                    }
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: qsTr("适用：需要按键在所有程序中生效的场景，包括仅识别物理键盘的旧版程序。")
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: qsTr("优势：兼容范围最广，可覆盖仅识别物理键盘的旧版程序。")
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: qsTr("局限：属于键盘过滤驱动，需管理员权限安装，安装完成后须重启电脑才会生效；部分安全软件或游戏反作弊可能拦截或告警；少数外设厂商软件可能与其冲突。")
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    visible: !SettingsController.interceptionInstallerAvailable
                    text: SettingsController.interceptionInstallerUnavailableReason
                }
            }
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
                || actionEditor.isModeBlocked(String(option.value)))
            objectName: injectCombo.objectName + "_option_" + index
            width: ListView.view ? ListView.view.width : injectCombo.width
            height: tokens.controlHeight
            leftPadding: 7
            rightPadding: 7
            // 用不了的模式直接点不动，不弹任何解释。
            enabled: !unavailable
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
        // 不再居中弹窗：改成贴在按键板右侧的抽屉面板，选中按键后从右边滑入，
        // 同时板上的卡片与连线收起、实物图左移（模仿 Logi Options+）。
        modal: false
        popupType: Popup.Item
        // 不指定 parent：Item 型弹层默认挂在页面根 Item 上，坐标就是页面可视区的
        // 坐标；按键板的几何也换算到同一坐标系里，否则会差出顶栏那一截。
        readonly property real viewportWidth: parent ? parent.width : root.width
        readonly property real viewportHeight: parent ? parent.height : root.height
        readonly property rect boardRect: mappingList.mapToItem(
            actionEditor.parent, 0, 0, mappingList.width, mappingList.height)
        // 贴着按键板的高度，但不超过窗口可视高度（小窗口时页面会滚动）。
        readonly property real panelHeight: Math.max(240, Math.min(
            mappingList.height, viewportHeight))
        y: Math.max(0, Math.min(boardRect.y, viewportHeight - panelHeight))
        height: panelHeight
        width: root.mappingPanelWidth
        property bool panelShown: false
        // 切到别的页面只是把面板藏起来，回到按键页时按离开前的样子恢复。
        property bool hiddenByPageSwitch: false
        // 弹出/收起的位置由下面的滑动控制；Fluent 样式自带的进出淡入淡出会
        // 让面板在关闭后还残留在画面上约 167ms，切页时会闪在别的页面上。
        enter: Transition {}
        exit: Transition {}
        // 贴着按键板右边缘；收起时推到窗口右侧外面。
        x: panelShown
            ? Math.min(boardRect.x + boardRect.width, viewportWidth) - width
            : viewportWidth
        Behavior on x {
            // 和遥控器的侧移同一时长/缓动，滑入滑出是一体的。
            NumberAnimation {
                duration: root.editSlideDuration
                easing.type: Easing.OutCubic
            }
        }
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

        // 关闭走一次滑出动画：先把面板推出右侧，等动画结束再真正 close()。
        Timer {
            id: panelCloseTimer
            interval: root.editSlideDuration + 40
            onTriggered: actionEditor.close()
        }

        function requestClose() {
            if (!visible)
                return
            panelShown = false
            panelCloseTimer.restart()
        }

        background: Rectangle {
            // 与页面卡片、其他弹窗一致的圆角（cornerRadiusCard）。
            radius: tokens.cornerRadiusCard
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
                // ✕ 代替原来的「取消」：有未保存的草稿先弹二级确认，
                // 确认后丢弃；没改动就直接关。落盘只走「完成」。
                onCloseRequested: {
                    if (actionEditor.draftDirty)
                        actionEditorDiscardConfirmDialog.open()
                    else
                        actionEditor.requestClose()
                }
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
        // 会话级草稿缓冲：面板开着期间换行 / 换应用时，把当前行改过的草稿
        // 暂存到这里（key = 草稿所属应用 "|" + 行号），「完成」时连同当前行
        // 一起落盘，「✕ 放弃」时整个会话一起清掉——以一次编辑界面为准。
        property var pendingEdits: ({})
        // 当前行草稿是在哪个编辑目标下改的：换应用时控制器先改目标再刷新
        // 面板，暂存草稿要按草稿自己的应用记，不能按切换后的目标。
        property string draftTarget: ""
        // 保存成功 / 确认放弃时先立起这面旗，onClosed 就不再把当前行暂存
        // 进缓冲；换页隐藏面板这种「会话还在」的关闭才需要暂存。
        property bool closeFinalizesSession: false
        // 右侧分类列表里点的动作写给哪个手势（单击 / 双击 / 长按）。
        property string activeTrigger: "single_click"
        // 分类列表的展开状态：标题 → 是否展开；没记录的按「第一组展开」处理。
        property var sectionOpen: ({})
        // 沿用下拉里的分组标题把动作分节；同名分类并到一节（语音键和
        // 「语音模式切换」都在「语音」里），没有分组标题的归到「其他动作」。
        // 双击/长按可以选「未设置」来清掉这一条手势。
        readonly property var actionSections: {
            const options = SettingsController.primaryActionOptionsFor(buttonId)
            if (activeTrigger !== "single_click")
                options.push("未设置")
            const sections = []
            const byTitle = ({})
            for (let i = 0; i < options.length; ++i) {
                const option = String(options[i])
                const title = SettingsController.actionOptionGroupTitle(option)
                const key = title.length > 0 ? title : qsTr("其他动作")
                let section = byTitle[key]
                if (!section) {
                    section = { "title": key, "items": [] }
                    byTitle[key] = section
                    sections.push(section)
                }
                section.items.push(option)
            }
            return sections
        }
        function isSectionOpen(title) {
            // 默认全部收起，展开哪一组由用户自己点。
            return sectionOpen[title] === true
        }
        function toggleSection(title) {
            // 手风琴：同时只展开一个分类，展开新的自动收掉旧的。
            const next = {}
            if (!isSectionOpen(title))
                next[title] = true
            sectionOpen = next
        }
        function activeText() {
            return activeTrigger === "single_click" ? primaryText
                : activeTrigger === "double_click" ? doubleText : longText
        }
        function isCurrentOption(option) {
            return SettingsController.formatActionText(activeText())
                === SettingsController.formatActionText(option)
        }
        function applyOptionToActiveTrigger(option) {
            if (rowIndex < 0)
                return
            const formatted = SettingsController.formatActionText(option)
            // 「聚焦输入框」先配置方式，再回填。
            if (SettingsController.isFocusInputText(formatted)) {
                focusInputDialog.targetRow = rowIndex
                focusInputDialog.targetTrigger = activeTrigger
                root.focusDialogSource = "mapping"
                focusInputDialog.openFor(activeText())
                return
            }
            // 「组合动作」先编辑步骤，确认后回填。
            if (SettingsController.isCompositeText(formatted)) {
                compositeActionDialog.targetRow = rowIndex
                compositeActionDialog.targetTrigger = activeTrigger
                compositeActionDialog.openFor(activeText())
                return
            }
            // 「关闭应用」先弹方式选择；指定应用时再叠一层应用选择器。
            if (SettingsController.isCloseApplicationChoiceText(
                    SettingsController.formatActionText(option))) {
                root.appPickerSource = "mapping"
                closeApplicationDialog.targetRow = rowIndex
                closeApplicationDialog.targetTrigger = activeTrigger
                closeApplicationDialog.open()
                return
            }
            // 「启动应用」要先挑一个程序，挑完再回填。
            if (SettingsController.isLaunchApplicationText(
                    SettingsController.formatActionText(option))) {
                root.openApplicationPicker(rowIndex, activeTrigger, option)
                return
            }
            syncing = true
            if (activeTrigger === "single_click")
                primaryText = option
            else if (activeTrigger === "double_click")
                doubleText = option
            else
                longText = option
            syncing = false
        }
        readonly property string activeTriggerLabel:
            activeTrigger === "single_click" ? qsTr("单击")
            : activeTrigger === "double_click" ? qsTr("双击") : qsTr("长按")
        readonly property bool activeHotkeyLocked:
            activeTrigger === "single_click" ? primaryHotkeyLocked
            : activeTrigger === "double_click" ? doubleHotkeyLocked
            : longHotkeyLocked
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

        function blockReasonFor(mode) {
            if (rowIndex < 0)
                return ""
            const texts = [
                ["single_click", primaryText],
                ["double_click", doubleText],
                ["long_press", longText]
            ]
            for (let i = 0; i < texts.length; ++i) {
                const message = SettingsController.buttonActionValidationMessage(
                    buttonId, texts[i][0], texts[i][1], mode
                )
                if (message.length > 0)
                    return message
            }
            return ""
        }
        // 当前动作（元素导航、打开应用、OpenMexia 设备等）用不了某种驱动时，
        // 直接退回 SendInput，不弹任何提示。
        readonly property bool winuhidBlocked: rowIndex >= 0
            && blockReasonFor("winuhid").length > 0
        readonly property bool interceptionBlocked: rowIndex >= 0
            && blockReasonFor("interception").length > 0
        function isModeBlocked(mode) {
            if (mode === "winuhid")
                return winuhidBlocked
            if (mode === "interception")
                return interceptionBlocked
            return false
        }
        function syncInjectModeToActions() {
            if (isModeBlocked(injectMode))
                injectMode = "sendinput"
        }
        readonly property string validationError:
            primaryValidationError.length > 0 ? primaryValidationError
            : doubleValidationError.length > 0 ? doubleValidationError
            : longValidationError
        readonly property bool rowDirty:
            primaryText !== originalPrimaryText
            || doubleText !== originalDoubleText
            || longText !== originalLongText
            || primaryNote !== originalPrimaryNote
            || doubleNote !== originalDoubleNote
            || longNote !== originalLongNote
            || injectMode !== originalInjectMode
        // 会话里只要还有没落盘的改动（缓冲里的其他行 + 当前行），就都算
        // 有草稿；✕ 和离开提示都按整个会话判断。
        readonly property bool draftDirty:
            Object.keys(pendingEdits).length > 0 || rowDirty

        function setPendingEdit(key, entry) {
            // var 属性要整包重新赋值才会让 draftDirty 重新求值。
            const next = {}
            for (const k in pendingEdits)
                next[k] = pendingEdits[k]
            if (entry)
                next[key] = entry
            pendingEdits = next
        }

        function stashRowDraft() {
            if (rowIndex < 0)
                return
            const key = draftTarget + "|" + rowIndex
            if (rowDirty) {
                setPendingEdit(key, {
                    identity: draftTarget,
                    rowIndex: rowIndex,
                    buttonId: buttonId,
                    primary: primaryText,
                    double: doubleText,
                    long: longText,
                    primaryNote: primaryNote,
                    doubleNote: doubleNote,
                    longNote: longNote,
                    injectMode: injectMode
                })
            } else {
                setPendingEdit(key, null)
            }
        }

        // 会话里还没落盘的修改统计：改了几处、几颗按键、几个应用，
        // 给「放弃修改」弹窗做汇总提示。当前行改了但还没进缓冲的也算一处。
        function pendingEditSummary() {
            let rows = 0
            const buttons = ({})
            const apps = ({})
            for (const k in pendingEdits) {
                rows += 1
                buttons[pendingEdits[k].buttonId] = true
                apps[pendingEdits[k].identity] = true
            }
            if (rowDirty && rowIndex >= 0 && !pendingEdits[draftTarget + "|" + rowIndex]) {
                rows += 1
                buttons[buttonId] = true
                apps[draftTarget] = true
            }
            return {
                rows: rows,
                buttons: Object.keys(buttons).length,
                apps: Object.keys(apps).length
            }
        }

        function openForRow(rowIndexValue, buttonIdValue, buttonNameValue,
                            primaryValue, doubleValue, longValue,
                            primaryNoteValue, doubleNoteValue, longNoteValue) {
            syncing = true
            validationRequested = false
            // 面板开着时换行 / 换应用：先把当前行的草稿按它所属的应用
            // 暂存进会话缓冲，再载入新行，旧行的改动不丢。
            if (visible && rowIndex >= 0)
                stashRowDraft()
            // 每次点开面板都回到「单击」手势；同一次编辑里切换按键 /
            // 应用时保持当前所选不变。
            if (!visible)
                activeTrigger = "single_click"
            rowIndex = rowIndexValue
            buttonId = buttonIdValue
            buttonName = buttonNameValue
            draftTarget = SettingsController.mappingEditTarget
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
            // 这颗按键在本次编辑里改过还没落盘：接着用缓冲里的草稿。
            const pending = pendingEdits[draftTarget + "|" + rowIndexValue]
            if (pending) {
                primaryText = pending.primary
                doubleText = pending.double
                longText = pending.long
                primaryNote = pending.primaryNote
                doubleNote = pending.doubleNote
                longNote = pending.longNote
                injectMode = pending.injectMode
            }
            syncInjectModeToActions()
            // 下拉框的选中项是用户交互状态，不会跟着 injectMode 绑定走：
            // 每次载入行都按当前值同步一次，避免残留上一个按键的选择。
            injectModeCombo.syncFromMode(injectMode)
            syncing = false
            panelCloseTimer.stop()
            // 面板已经开着（用户在实物图上点了另一个按键）：只换内容，
            // 不再滑进滑出一次。
            if (visible) {
                panelShown = true
                return
            }
            panelShown = false
            open()
            // 先以收起的姿态显示，再切到展开位置，滑入动画才会真的跑起来。
            Qt.callLater(function() {
                if (actionEditor.visible)
                    actionEditor.panelShown = true
            })
        }

        function applyCapturedShortcut(targetRow, trigger, chord) {
            if (!visible || targetRow !== rowIndex)
                return
            syncing = true
            if (trigger === "single_click")
                primaryText = chord
            else if (trigger === "double_click")
                doubleText = chord
            else if (trigger === "long_press")
                longText = chord
            syncing = false
        }

        // 「启动应用」选中程序后只回填当前手势，落盘等「完成」。
        function applyCloseApplication(targetRow, trigger, mode, process) {
            if (!visible || targetRow !== rowIndex)
                return
            const text = SettingsController.closeApplicationText(mode, process)
            syncing = true
            if (trigger === "single_click")
                primaryText = text
            else if (trigger === "double_click")
                doubleText = text
            else if (trigger === "long_press")
                longText = text
            syncing = false
        }

        function applyPickedApplication(targetRow, trigger, process) {
            if (!visible || targetRow !== rowIndex)
                return
            const text = SettingsController.applicationLaunchText(process)
            syncing = true
            if (trigger === "single_click")
                primaryText = text
            else if (trigger === "double_click")
                doubleText = text
            else if (trigger === "long_press")
                longText = text
            syncing = false
        }

        // 缓冲行的有效注入方式：这行动作用不了驱动级就退回 SendInput，
        // 和当前行的兜底一致。返回 { mode, message }，message 非空表示退回
        // SendInput 之后仍然校验不过，得拦下来让用户当场改。
        function bufferedModeAndError(entry) {
            const texts = [
                ["single_click", entry.primary],
                ["double_click", entry.double],
                ["long_press", entry.long]
            ]
            const modes = [entry.injectMode]
            if (entry.injectMode !== "sendinput")
                modes.push("sendinput")
            for (let m = 0; m < modes.length; ++m) {
                let message = ""
                for (let i = 0; i < texts.length && message.length === 0; ++i) {
                    message = SettingsController.buttonActionValidationMessage(
                        entry.buttonId, texts[i][0], texts[i][1], modes[m])
                }
                if (message.length === 0)
                    return { mode: modes[m], message: "" }
                if (modes[m] === "sendinput")
                    return { mode: modes[m], message: message }
            }
            return { mode: entry.injectMode, message: "" }
        }

        // 校验不过的缓冲行：把面板切到那一行，错误提示对着它显示，
        // 用户当场改完再点「完成」。
        function showBufferedRow(entry) {
            syncing = true
            validationRequested = true
            rowIndex = entry.rowIndex
            buttonId = entry.buttonId
            draftTarget = entry.identity
            primaryText = entry.primary
            doubleText = entry.double
            longText = entry.long
            primaryNote = entry.primaryNote
            doubleNote = entry.doubleNote
            longNote = entry.longNote
            injectMode = entry.injectMode
            // original 按模型里的原值算：当前目标的行能从卡片上读回来；
            // 其他应用的行读不到模型值，就用缓冲值凑合（缓冲项还在，
            // 会话照样算有草稿）。
            if (entry.identity === SettingsController.mappingEditTarget) {
                const isLeft = SettingsController.isLeftButton(entry.buttonId)
                const card = isLeft
                    ? leftCardRepeater.itemAt(entry.rowIndex)
                    : rightCardRepeater.itemAt(entry.rowIndex)
                if (card) {
                    originalPrimaryText = card.actionText
                    originalDoubleText = card.doubleClickText
                    originalLongText = card.longPressText
                    originalPrimaryNote = card.singleNoteText
                    originalDoubleNote = card.doubleNoteText
                    originalLongNote = card.longNoteText
                } else {
                    originalPrimaryText = entry.primary
                    originalDoubleText = entry.double
                    originalLongText = entry.long
                    originalPrimaryNote = entry.primaryNote
                    originalDoubleNote = entry.doubleNote
                    originalLongNote = entry.longNote
                }
                originalInjectMode =
                    SettingsController.buttonInjectMode(entry.buttonId)
            } else {
                originalPrimaryText = entry.primary
                originalDoubleText = entry.double
                originalLongText = entry.long
                originalPrimaryNote = entry.primaryNote
                originalDoubleNote = entry.doubleNote
                originalLongNote = entry.longNote
                originalInjectMode = entry.injectMode
            }
            syncInjectModeToActions()
            // 同 openForRow：载入缓冲行也要把下拉框的选中项同步到当前值。
            injectModeCombo.syncFromMode(injectMode)
            syncing = false
        }

        function saveDraft() {
            validationRequested = true
            // 兜底：动作用不了驱动级时，保存这一步直接按 SendInput 存，
            // 不把「请改用 SendInput」当成错误拦下来。
            if (isModeBlocked(injectMode))
                injectMode = "sendinput"
            // 面板上正显示的这行可能就是某条缓冲草稿（校验失败切过来的）：
            // 先把它暂存回去，保证落盘用的是最新改动。
            stashRowDraft()
            // 缓冲里其他行逐条校验；哪条不过就把面板切到那行，等用户改完。
            const keys = Object.keys(pendingEdits)
            const editTarget = SettingsController.mappingEditTarget
            let hasOther = false
            for (let i = 0; i < keys.length; ++i) {
                const key = keys[i]
                const entry = pendingEdits[key]
                const checked = bufferedModeAndError(entry)
                if (checked.message.length > 0) {
                    showBufferedRow(entry)
                    return false
                }
                if (entry.injectMode !== checked.mode) {
                    const fixed = {}
                    for (const k in entry)
                        fixed[k] = entry[k]
                    fixed.injectMode = checked.mode
                    setPendingEdit(key, fixed)
                }
                if (entry.identity !== editTarget)
                    hasOther = true
            }
            if (rowIndex < 0 || validationError.length > 0)
                return false
            // 其他应用的草稿先落盘：这一步控制器会把模型临时换载到那些
            // 应用再换回来，必须发生在写当前应用之前，否则刚写的行会被冲掉。
            if (hasOther) {
                const groups = []
                const byTarget = ({})
                for (let i = 0; i < keys.length; ++i) {
                    const entry = pendingEdits[keys[i]]
                    if (entry.identity === editTarget)
                        continue
                    let group = byTarget[entry.identity]
                    if (!group) {
                        group = { identity: entry.identity, rows: [] }
                        byTarget[entry.identity] = group
                        groups.push(group)
                    }
                    group.rows.push({
                        buttonId: entry.buttonId,
                        primary: entry.primary,
                        double: entry.double,
                        long: entry.long,
                        primaryNote: entry.primaryNote,
                        doubleNote: entry.doubleNote,
                        longNote: entry.longNote,
                        injectMode: entry.injectMode
                    })
                }
                const result = SettingsController.flushMappingDraftEdits(groups)
                if (!result.ok)
                    return false
            }
            // 当前应用的缓冲行直接写模型（模型此刻就是当前应用的布局）。
            for (let i = 0; i < keys.length; ++i) {
                const entry = pendingEdits[keys[i]]
                if (entry.identity !== editTarget)
                    continue
                ButtonMappingModel.setActionTextAt(entry.rowIndex, entry.primary)
                ButtonMappingModel.setSecondaryActionTextAt(
                    entry.rowIndex, "double_click", entry.double
                )
                ButtonMappingModel.setSecondaryActionTextAt(
                    entry.rowIndex, "long_press", entry.long
                )
                ButtonMappingModel.setDisplayNoteAt(
                    entry.rowIndex, "single_click", entry.primaryNote
                )
                ButtonMappingModel.setDisplayNoteAt(
                    entry.rowIndex, "double_click", entry.doubleNote
                )
                ButtonMappingModel.setDisplayNoteAt(
                    entry.rowIndex, "long_press", entry.longNote
                )
                SettingsController.setButtonInjectMode(entry.buttonId, entry.injectMode)
            }
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
            // 全部落盘：清掉会话缓冲，关闭面板时不再暂存当前行。
            pendingEdits = ({})
            closeFinalizesSession = true
            requestClose()
            return true
        }

        onClosed: {
            panelCloseTimer.stop()
            // 换页隐藏时页面布局保持原样：遥控器不归位、卡片不淡入，
            // 切回来时原样打开，整页始终不播放过渡动画。
            if (!hiddenByPageSwitch)
                panelShown = false
            // 换页隐藏 / Esc 这类「会话还在」的关闭：当前行还有草稿就先
            // 暂存进缓冲，回到按键页或再点开面板时接着改。保存成功和
            // 确认放弃都立起了 closeFinalizesSession，不走这条。
            if (!closeFinalizesSession && rowIndex >= 0)
                stashRowDraft()
            closeFinalizesSession = false
            syncing = true
            rowIndex = -1
        }

        // 手势卡片固定在面板顶部不滚动，只有下面的分类列表滚动。
        contentItem: ColumnLayout {
            spacing: 9

            // 手势选择：直接复用按键卡片，点哪个手势格，下面分类里的动作
            // 就写给哪个手势；选中的格子带强调色描边。
            MappingCard {
                objectName: "actionEditorGestureCard"
                Layout.fillWidth: true
                tokens: root.tokens
                // 面板里的卡片和板上那颗重名会互相覆盖，加前缀区分。
                cardId: "editor_" + actionEditor.buttonId
                buttonName: actionEditor.buttonName
                singleText: actionEditor.primaryText
                doubleText: actionEditor.doubleText
                longText: actionEditor.longText
                singleNoteText: actionEditor.primaryNote
                doubleNoteText: actionEditor.doubleNote
                longNoteText: actionEditor.longNote
                voiceAction: SettingsController.isVoiceActionText(
                    actionEditor.primaryText.trim())
                selectableGestures: true
                activeGesture: actionEditor.activeTrigger
                onGesturePicked: function(gesture) {
                    actionEditor.activeTrigger = gesture
                }
            }

            // 自定义快捷键仍然从列表里进：录到的组合写给当前选中的手势。
            // 右边「备注」给这个手势起名，卡片上优先显示备注名。
            RowLayout {
                Layout.fillWidth: true
                spacing: root.tokens.spacingSmall

                AbstractButton {
                    objectName: "actionEditorRecordButton"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 28
                    hoverEnabled: true
                    enabled: !actionEditor.activeHotkeyLocked
                    Accessible.name: qsTr("录制")
                        + actionEditor.activeTriggerLabel + qsTr("快捷键")
                    onClicked: root.openShortcutRecorder(
                        actionEditor.buttonId, actionEditor.rowIndex,
                        actionEditor.activeTrigger)

                    contentItem: UiLabel {
                        tokens: root.tokens
                        kind: bodyKind
                        leftPadding: 0
                        rightPadding: 6
                        elide: Text.ElideRight
                        verticalAlignment: Text.AlignVCenter
                        text: qsTr("录制") + actionEditor.activeTriggerLabel
                            + qsTr("快捷键…")
                        color: actionEditor.activeHotkeyLocked
                            ? root.tokens.textTertiary : root.tokens.textPrimary
                    }
                    background: Rectangle {
                        radius: 6
                        color: parent.hovered && parent.enabled
                            ? root.tokens.buttonHover : "transparent"
                    }
                }

                AbstractButton {
                    objectName: "actionEditorNoteButton"
                    Layout.preferredHeight: 28
                    Layout.alignment: Qt.AlignVCenter
                    hoverEnabled: true
                    Accessible.name: qsTr("备注") + actionEditor.activeTriggerLabel
                        + qsTr("快捷键")
                    onClicked: actionEditorNoteDialog.openForActiveTrigger()

                    contentItem: UiLabel {
                        tokens: root.tokens
                        kind: bodyKind
                        leftPadding: 8
                        rightPadding: 8
                        verticalAlignment: Text.AlignVCenter
                        text: qsTr("备注")
                        color: root.tokens.textPrimary
                    }
                    background: Rectangle {
                        radius: 6
                        color: parent.hovered
                            ? root.tokens.buttonHover : "transparent"
                    }
                }
            }

            ScrollView {
                id: actionEditorScroll
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                contentWidth: availableWidth
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

            // 分类动作列表：沿用下拉里的分组（推荐 / AI 动作 / 其他动作…），
            // 每组单独展开；点一项立刻写给上面选中的手势。
            ColumnLayout {
                objectName: "actionEditorActionSections"
                width: actionEditorScroll.availableWidth
                spacing: 2

                Repeater {
                    model: actionEditor.actionSections

                    delegate: ColumnLayout {
                        id: actionSection
                        required property var modelData
                        readonly property string sectionTitle: String(modelData.title)
                        Layout.fillWidth: true
                        spacing: 2

                        AbstractButton {
                            id: sectionHeaderButton
                            objectName: "actionEditorSection_" + actionSection.sectionTitle
                            Layout.fillWidth: true
                            Layout.preferredHeight: 28
                            hoverEnabled: true
                            Accessible.name: actionSection.sectionTitle
                            onClicked: actionEditor.toggleSection(
                                actionSection.sectionTitle)

                            contentItem: RowLayout {
                                spacing: 6

                                UiLabel {
                                    tokens: root.tokens
                                    kind: bodyKind
                                    Layout.fillWidth: true
                                    text: actionSection.sectionTitle
                                    font.weight: Font.Medium
                                }
                                DropDownIndicator {
                                    Layout.alignment: Qt.AlignVCenter
                                    rotation: actionEditor.isSectionOpen(
                                        actionSection.sectionTitle) ? 0 : -90
                                    indicatorColor: root.tokens.textSecondary
                                }
                            }
                            background: Rectangle {
                                radius: 6
                                color: sectionHeaderButton.hovered
                                    ? root.tokens.buttonHover : "transparent"
                            }
                        }

                        // 折叠动画容器：收起时高度收到 0，展开时撑到列表
                        // 自然高度，收和放走同一段过渡。
                        Item {
                            id: sectionItemsClip
                            Layout.fillWidth: true
                            readonly property bool expanded:
                                actionEditor.isSectionOpen(actionSection.sectionTitle)
                            implicitHeight: expanded
                                ? sectionItemsColumn.implicitHeight : 0
                            clip: true
                            visible: height > 0
                            Behavior on implicitHeight {
                                NumberAnimation {
                                    duration: 160
                                    easing.type: Easing.OutCubic
                                }
                            }

                            ColumnLayout {
                                id: sectionItemsColumn
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.top: parent.top
                                spacing: 2

                                Repeater {
                                    model: actionSection.modelData.items

                                    delegate: AbstractButton {
                                        required property string modelData
                                        objectName: "actionEditorAction_" + modelData
                                        // 当前这个手势正在用的动作，字体加粗标记。
                                        readonly property bool current:
                                            actionEditor.isCurrentOption(modelData)
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: 28
                                        hoverEnabled: true
                                        Accessible.name: modelData
                                        onClicked: actionEditor.applyOptionToActiveTrigger(
                                            modelData)

                                        contentItem: UiLabel {
                                            tokens: root.tokens
                                            kind: bodyKind
                                            leftPadding: 16
                                            rightPadding: 6
                                            elide: Text.ElideRight
                                            verticalAlignment: Text.AlignVCenter
                                            text: SettingsController.formatActionText(modelData)
                                            font.weight: parent.current
                                                ? Font.DemiBold : Font.Normal
                                        }
                                        background: Rectangle {
                                            radius: 6
                                            color: parent.hovered
                                                ? root.tokens.buttonHover : "transparent"
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }

            }
        }

        // 错误提示和按钮固定在面板底部，不跟着内容滚走；左右留出面板内边距，
        // 按钮不会顶到面板边缘外面。
        footer: Item {
            // 底部留白与面板左右版心一致，注入方式那一行不贴着边缘。
            implicitHeight: footerColumn.implicitHeight + tokens.dialogPadding

            ColumnLayout {
            id: footerColumn
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.leftMargin: tokens.dialogPadding
            anchors.rightMargin: tokens.dialogPadding
            spacing: tokens.spacingSmall

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

                // 注入方式是整颗按键共用的设置，单独放在取消/完成这一行。
                UiLabel {
                    id: injectModeTitle
                    objectName: "actionEditorInjectModeTitle"
                    tokens: root.tokens
                    kind: bodyKind
                    text: qsTr("注入方式")
                    font.pixelSize: tokens.fontSizeControl
                    font.weight: Font.Medium
                    verticalAlignment: Text.AlignVCenter
                }
                InjectModeCombo {
                    id: injectModeCombo
                    objectName: "actionEditorInjectModeCombo"
                    tokens: root.tokens
                    // 宽度按最长选项给足，框里和下拉里都不截断。
                    Layout.preferredWidth:
                        Math.ceil(popupIdealWidth) + leftPadding + rightPadding
                    model: SettingsController.injectModeOptions
                    Accessible.name: actionEditor.buttonName + qsTr("注入方式")
                    onActivated: {
                        const option = model[index]
                        const value = option ? String(option.value) : ""
                        // 驱动级用不了的动作：这一项直接不可选，选了也停回原来的值，
                        // 不再弹任何解释。
                        if (!option || option.available === false
                                || actionEditor.isModeBlocked(value)) {
                            injectModeCombo.syncFromMode(actionEditor.injectMode)
                            return
                        }
                        actionEditor.injectMode = value
                    }
                }

                Item { Layout.fillWidth: true }
                CompactButton {
                    objectName: "actionEditorSaveButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth2Chars
                    text: qsTr("完成")
                    // 没有任何草稿改动时置灰：落盘只发生在真的改了东西之后。
                    enabled: actionEditor.rowIndex >= 0 && actionEditor.draftDirty
                    highlighted: true
                    onClicked: actionEditor.saveDraft()
                }
            }
            }
        }
    }

    // 编辑面板点「取消」的二级确认：面板里的改动是草稿，确认后才丢弃，
    // ✕ 和「完成」都是保存，只有这条路径不落盘。
    SettingsDialog {
        id: actionEditorDiscardConfirmDialog
        tokens: root.tokens
        objectName: "actionEditorDiscardConfirmDialog"
        title: qsTr("放弃修改？")
        modal: true
        anchors.centerIn: parent
        width: Math.min(430, root.width - 28)
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: {
            // 确认放弃：整个会话的缓冲一起清掉，关闭时不再暂存。
            actionEditor.pendingEdits = ({})
            actionEditor.closeFinalizesSession = true
            actionEditor.requestClose()
        }

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingLarge
            UiLabel {
                tokens: root.tokens
                kind: bodyKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                // 只改了当前这颗还是老话；改了多颗 / 多个应用就整个会话汇总。
                text: {
                    const summary = actionEditor.pendingEditSummary()
                    if (summary.rows <= 1)
                        return qsTr("这颗按键的修改还没保存，放弃后无法撤销。")
                    if (summary.apps > 1)
                        return qsTr("共修改了 %1 颗按键、%2 个应用，这些修改还没保存，放弃后无法撤销。")
                            .arg(summary.buttons).arg(summary.apps)
                    return qsTr("共修改了 %1 颗按键，这些修改还没保存，放弃后无法撤销。")
                        .arg(summary.buttons)
                }
            }
        }
    }

    // 手势备注名：写进当前手势的草稿，卡片上优先显示备注名；
    // 留空就回到默认（显示动作本身）。
    SettingsDialog {
        id: actionEditorNoteDialog
        tokens: root.tokens
        objectName: "actionEditorNoteDialog"
        title: qsTr("备注")
        titleNote: qsTr("留空显示动作本身")
        modal: true
        preferredWidth: 360
        standardButtons: Dialog.Ok | Dialog.Cancel

        property string targetTrigger: "single_click"

        function openForActiveTrigger() {
            targetTrigger = actionEditor.activeTrigger
            noteField.text = targetTrigger === "single_click"
                ? actionEditor.primaryNote
                : targetTrigger === "double_click"
                    ? actionEditor.doubleNote : actionEditor.longNote
            open()
        }

        onOpened: {
            noteField.forceActiveFocus()
            noteField.selectAll()
        }

        onAccepted: {
            const value = noteField.text.trim()
            if (targetTrigger === "single_click")
                actionEditor.primaryNote = value
            else if (targetTrigger === "double_click")
                actionEditor.doubleNote = value
            else
                actionEditor.longNote = value
        }

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingLarge

            CompactTextField {
                id: noteField
                objectName: "actionEditorNoteField"
                tokens: root.tokens
                Layout.fillWidth: true
                placeholderText: qsTr("留空显示动作本身")
                Accessible.name: qsTr("备注")
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
                    // 渐隐/渐显只在 "editing" 状态里设置，由根部 transitions 驱动。

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
                    // 渐隐/渐显只在 "editing" 状态里设置，由根部 transitions 驱动。

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
                        // 编辑态下检测到的按键直接跳进它的编辑面板。
                        Qt.callLater(root.openEditorForDetectedButton,
                            SettingsController.selectedButtonId)
                    }
                    function onMappingEditTargetChanged() {
                        // 控制器先发信号再换模型，所以等模型换完再刷新面板。
                        Qt.callLater(root.refreshOpenEditor)
                    }
                    function onActivePageIndexChanged() {
                        // 换页不改变按键页的布局：离开时只把面板藏起来，
                        // 回来时原样打开，遥控器/卡片/连线全程一动不动。
                        if (SettingsController.activePageIndex === 1) {
                            if (!actionEditor.hiddenByPageSwitch)
                                return
                            actionEditor.hiddenByPageSwitch = false
                            panelCloseTimer.stop()
                            actionEditor.open()
                            return
                        }
                        // 切出按键页：持续检测自动结束。
                        if (SettingsController.keyDetectionActive)
                            SettingsController.stopKeyDetection()
                        root.hideEditorForPageSwitch()
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
                    // 选中按键后卡片与连线一起淡出，把舞台让给右侧面板。
                    // 渐隐/渐显只在 "editing" 状态里设置，由根部 transitions 驱动。
                    enabled: !root.mappingPanelExpanded
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
                    // 面板打开时实物图退到「板宽 − 面板宽」的区域里居中，向左滑动。
                    // 位移放在 transform 里：普通布局变化（切页、改窗口大小）
                    // 不会带出移动动画，只有面板开关才滑。
                    // 面板展开时实物图要退到的位置（向左为负）。这里只按布局
                    // 宽度算、不看面板开关状态：状态过渡需要它当固定终点，
                    // 否则关闭瞬间它自己先变回 0，滑回动画就被吃掉了。
                    readonly property real panelShiftX: {
                        const available = Math.max(root.mappingPhotoColumnWidth,
                            mappingList.width - root.mappingPanelWidth)
                        return (available - width) / 2
                            - (mappingList.width - width) / 2
                    }
                    x: (mappingList.width - width) / 2
                    transform: Translate {
                        id: panelShift
                        // 位移由根部 states/transitions 驱动，和面板滑入/滑出同一节奏。
                        x: 0
                        // 滑动改的是 transform，不会触发布局 x/y 信号，连线
                        // 画布得跟着每帧重画，否则滑回后显示的是过期内容。
                        onXChanged: root.scheduleConnectorRepaint()
                    }
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
                                required property string actionText
                                required property string doubleClickText
                                required property string longPressText
                                required property string singleNote
                                required property string doubleNote
                                required property string longNote

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

                                // 编辑中的按键：只在面板打开时显示一块贴键形的
                                // 亮绿描边。圆键画圆、方向键画圆环上的弧段、
                                // 音量键画摇杆的半边，都严丝合缝地盖住键帽。
                                Item {
                                    id: photoKeyHighlight
                                    objectName: "photoHotspotHighlight_"
                                        + photoHotspot.buttonId
                                    // 亮绿高亮：描边用实色，填充带一点不透明
                                    // 度；选中与悬浮预览共用这一套高亮（悬浮
                                    // 时整块半透明）。
                                    readonly property color keyGreen: "#4ade80"
                                    readonly property real hotX: photoHotspot.hotspotX
                                    readonly property real hotY: photoHotspot.hotspotY
                                    readonly property var spec: root.photoKeySpec(
                                        photoHotspot.buttonId, hotX, hotY,
                                        photoHotspot.hotspotWidth,
                                        photoHotspot.hotspotHeight,
                                        photoImage.paintedWidth > 0
                                            ? photoImage.paintedHeight
                                                / photoImage.paintedWidth : 1.5)
                                    readonly property string kind:
                                        spec ? String(spec.kind) : ""
                                    readonly property real unitW:
                                        photoImage.paintedWidth
                                    readonly property real unitH:
                                        photoImage.paintedHeight
                                    anchors.fill: parent
                                    // 悬浮预览：编辑面板打开时，鼠标浮上
                                    // 未选中的按键也点亮同一块键形高亮，
                                    // 整体压低不透明度，让"这里可以点"
                                    // 的反馈更明确。面板打开时 hover 被
                                    // Overlay 层截走，所以这里用根部的
                                    // editorHoverButtonId（overlay 转发层
                                    // 算出的命中按键）判断；连线模式不点
                                    // 亮键形，也不显示悬浮卡片。
                                    readonly property bool hoveredPreview:
                                        root.mappingPanelExpanded
                                        && !photoHotspot.isSelected
                                        && root.editorHoverButtonId
                                            === photoHotspot.buttonId
                                    visible: hoveredPreview
                                        || (photoHotspot.isSelected
                                            && root.mappingPanelExpanded)
                                    opacity: hoveredPreview ? 0.5 : 1.0

                                    // 圆键 / 普通矩形键。
                                    Rectangle {
                                        readonly property bool flatKey:
                                            photoKeyHighlight.kind === "circle"
                                            || photoKeyHighlight.kind === "rect"
                                        anchors.centerIn: parent
                                        anchors.horizontalCenterOffset: flatKey
                                            ? (photoKeyHighlight.spec.cx
                                                - photoKeyHighlight.hotX)
                                                * photoKeyHighlight.unitW : 0
                                        anchors.verticalCenterOffset: flatKey
                                            ? (photoKeyHighlight.spec.cy
                                                - photoKeyHighlight.hotY)
                                                * photoKeyHighlight.unitH : 0
                                        visible: flatKey
                                        width: flatKey
                                            ? photoKeyHighlight.spec.w
                                                * photoKeyHighlight.unitW : 0
                                        height: flatKey
                                            ? photoKeyHighlight.spec.h
                                                * photoKeyHighlight.unitH : 0
                                        radius: photoKeyHighlight.kind === "circle"
                                            ? width / 2 : Math.min(width, height) / 2
                                        color: Qt.rgba(
                                            photoKeyHighlight.keyGreen.r,
                                            photoKeyHighlight.keyGreen.g,
                                            photoKeyHighlight.keyGreen.b, 0.18)
                                        border.width: 2
                                        border.color: photoKeyHighlight.keyGreen
                                    }

                                    // 方向键：圆环上的一段弧。
                                    Shape {
                                        id: arcHighlight
                                        readonly property bool isArc:
                                            photoKeyHighlight.kind === "arc"
                                        anchors.centerIn: parent
                                        anchors.horizontalCenterOffset: isArc
                                            ? (photoKeyHighlight.spec.cx
                                                - photoKeyHighlight.hotX)
                                                * photoKeyHighlight.unitW : 0
                                        anchors.verticalCenterOffset: isArc
                                            ? (photoKeyHighlight.spec.cy
                                                - photoKeyHighlight.hotY)
                                                * photoKeyHighlight.unitH : 0
                                        visible: isArc
                                        width: isArc
                                            ? photoKeyHighlight.spec.rx * 2
                                                * photoKeyHighlight.unitW : 0
                                        height: isArc
                                            ? photoKeyHighlight.spec.ry * 2
                                                * photoKeyHighlight.unitH : 0

                                        ShapePath {
                                            fillColor: "transparent"
                                            strokeColor: photoKeyHighlight.keyGreen
                                            // 只描一条细弧线，跟圆键的 2px 描边一致，
                                            // 不再把整段环带涂成实心。
                                            strokeWidth: 2
                                            capStyle: ShapePath.RoundCap
                                            PathAngleArc {
                                                centerX: arcHighlight.width / 2
                                                centerY: arcHighlight.height / 2
                                                radiusX:
                                                    photoKeyHighlight.kind === "arc"
                                                        ? photoKeyHighlight.spec.rx
                                                            * photoKeyHighlight.unitW : 0
                                                radiusY:
                                                    photoKeyHighlight.kind === "arc"
                                                        ? photoKeyHighlight.spec.ry
                                                            * photoKeyHighlight.unitH : 0
                                                startAngle:
                                                    photoKeyHighlight.kind === "arc"
                                                        ? photoKeyHighlight.spec.start : 0
                                                sweepAngle:
                                                    photoKeyHighlight.kind === "arc"
                                                        ? photoKeyHighlight.spec.sweep : 0
                                            }
                                        }
                                    }
                                }

                                // 稿子 .wires .halo：r 7 的选中光晕，圆点由连线 Canvas 绘制。
                                Rectangle {
                                    objectName: "photoHotspotMarker_" + photoHotspot.buttonId
                                    anchors.centerIn: parent
                                    width: 14
                                    height: 14
                                    z: 1
                                    visible: photoHotspot.isSelected
                                        && !root.mappingPanelExpanded
                                    radius: 7
                                    color: tokens.halo
                                }

                                TapHandler {
                                    onTapped: {
                                        // 连线模式（面板关闭）下：点未选中的
                                        // 按键先选中，再点一下已选中的按键直接
                                        // 进编辑面板，不用非得点卡片。
                                        const wasSelected =
                                            SettingsController.selectedButtonId
                                            === photoHotspot.buttonId
                                        SettingsController.selectButton(
                                            photoHotspot.buttonId)
                                        // 面板开着时可以随手点实物图上的另一个
                                        // 按键，直接切到那颗按键的映射。
                                        if (actionEditor.visible
                                                || (wasSelected
                                                    && !root.mappingPanelOpen))
                                            actionEditor.openForRow(
                                                photoHotspot.index,
                                                photoHotspot.buttonId,
                                                root.shortButtonName(
                                                    photoHotspot.buttonId),
                                                photoHotspot.actionText,
                                                photoHotspot.doubleClickText,
                                                photoHotspot.longPressText,
                                                photoHotspot.singleNote,
                                                photoHotspot.doubleNote,
                                                photoHotspot.longNote)
                                    }
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
                    // 渐隐/渐显只在 "editing" 状态里设置，由根部 transitions 驱动。
                    enabled: !root.mappingPanelExpanded
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
                // 稿子 .sp：弹性留白，把驱动入口推到右侧。
                Item { Layout.fillWidth: true }
                // 稿子 .drv：6px 空心圆 + 「驱动」+ 粗体状态，margin-right 4。
                // 任意一种驱动装上都算已安装。
                RowLayout {
                    objectName: "inputDriverStatus"
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
                        id: inputDriverTitle
                        objectName: "actionEditorInputDriverTitle"
                        tokens: root.tokens
                        kind: noteKind
                        text: qsTr("驱动")
                        font.pixelSize: tokens.fontSizeControl
                        color: tokens.textTertiary
                        verticalAlignment: Text.AlignVCenter
                        HoverHandler { id: inputDriverTitleHover }
                        CompactToolTip {
                            tokens: root.tokens
                            active: inputDriverTitleHover.hovered
                            text: qsTr("WinUHid 与 Interception 都是可选驱动，安装需要管理员权限；此处的按钮只启动随包的官方安装程序，绝不会静默安装")
                        }
                    }
                    UiLabel {
                        objectName: "inputDriverState"
                        tokens: root.tokens
                        kind: noteKind
                        text: SettingsController.winuhidDriverInstalled
                            || SettingsController.interceptionDriverInstalled
                            ? qsTr("已安装") : qsTr("未安装")
                        font.pixelSize: tokens.fontSizeControl
                        font.weight: Font.DemiBold
                        color: tokens.textSecondary
                        verticalAlignment: Text.AlignVCenter
                    }
                }
                // 两种驱动统一收进二级弹窗里管理：装哪种、装几种、卸哪种都由用户决定。
                CompactButton {
                    id: driverManagerButton
                    objectName: "driverManagerButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth4Chars
                    flat: true
                    text: qsTr("驱动管理")
                    Accessible.name: qsTr("管理输入驱动")
                    onClicked: driverManagerDialog.open()
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

        // 实物图按键的悬浮卡片：默认不显示，只有鼠标浮在某颗键上时才
        // 跟着鼠标出现，上中下三行分别是单击 / 双击 / 长按。读数直接绑
        // 模型；正在编辑的那颗键改读面板里的草稿，改了立刻同步。
        Item {
            id: photoHoverCard
            objectName: "photoHoverCard"
            readonly property int textCap: 220
            readonly property string buttonId: root.editorHoverButtonId
            // 内容锁在最近一次悬浮到的按键上：面板关闭、卡片渐隐的这
            // 150ms 里内容不会先塌成“未设置”。
            property string shownButtonId: ""
            onButtonIdChanged: if (buttonId.length > 0) shownButtonId = buttonId
            readonly property bool draftActive:
                actionEditor.buttonId === shownButtonId
            readonly property var modelRow: {
                const wanted = shownButtonId
                const count = photoHotspotRepeater.count
                for (let i = 0; i < count; ++i) {
                    const item = photoHotspotRepeater.itemAt(i)
                    if (item && item.buttonId === wanted)
                        return item
                }
                return null
            }
            readonly property string singleText: root.photoHoverText(
                draftActive ? actionEditor.primaryText
                            : (modelRow ? modelRow.actionText : ""),
                draftActive ? actionEditor.primaryNote
                            : (modelRow ? modelRow.singleNote : ""))
            readonly property string doubleText: root.photoHoverText(
                draftActive ? actionEditor.doubleText
                            : (modelRow ? modelRow.doubleClickText : ""),
                draftActive ? actionEditor.doubleNote
                            : (modelRow ? modelRow.doubleNote : ""))
            readonly property string longText: root.photoHoverText(
                draftActive ? actionEditor.longText
                            : (modelRow ? modelRow.longPressText : ""),
                draftActive ? actionEditor.longNote
                            : (modelRow ? modelRow.longNote : ""))
            // 卡片挂在 Overlay 层（见下方 parent），坐标也按 Overlay 换算。
            readonly property point cursorPoint:
                Overlay.overlay.mapFromItem(null,
                    root.editorHoverScenePos.x, root.editorHoverScenePos.y)
            // 卡片宽度按三行里最长的那行算，超过上限就省略。
            readonly property real valueWidth: Math.min(textCap,
                Math.max(singleMetrics.advanceWidth,
                    Math.max(doubleMetrics.advanceWidth,
                        longMetrics.advanceWidth)) + 2)

            // 编辑面板是 Overlay 里的 Popup.Item，普通页面 Item 的 z 再高
            // 也会被它压住；卡片必须同样挂到 Overlay 层并取更高的 z。
            parent: Overlay.overlay
            z: 100
            enabled: false
            // 只有编辑状态下、鼠标浮在某颗键上时才显示；关掉面板时渐隐。
            readonly property bool wantVisible:
                root.mappingPanelExpanded
                && !root.anyActionDialogOpen
                && buttonId.length > 0 && root.editorHoverScenePos.x >= 0
            // 关闭面板时 120ms 快速渐隐，让连线卡片等它完全消失后再渐显。
            opacity: wantVisible ? 1 : 0
            visible: wantVisible || opacity > 0
            Behavior on opacity {
                NumberAnimation {
                    duration: photoHoverCard.wantVisible ? 150 : 120
                    easing.type: Easing.OutCubic
                }
            }
            width: 12 + 22 + 8 + valueWidth + 12
            height: photoHoverCardColumn.implicitHeight + 20
            x: Math.max(8, Math.min(
                (parent ? parent.width : 0) - width - 8,
                cursorPoint.x + 18))
            y: Math.max(8, Math.min(
                (parent ? parent.height : 0) - height - 8,
                cursorPoint.y + 16))

            TextMetrics {
                id: singleMetrics
                font.family: root.tokens.fontFamily
                font.pixelSize: root.tokens.fontSizeMappingKey
                text: photoHoverCard.singleText.length > 0
                    ? photoHoverCard.singleText : qsTr("未设置")
            }
            TextMetrics {
                id: doubleMetrics
                font.family: root.tokens.fontFamily
                font.pixelSize: root.tokens.fontSizeMappingKey
                text: photoHoverCard.doubleText.length > 0
                    ? photoHoverCard.doubleText : qsTr("未设置")
            }
            TextMetrics {
                id: longMetrics
                font.family: root.tokens.fontFamily
                font.pixelSize: root.tokens.fontSizeMappingKey
                text: photoHoverCard.longText.length > 0
                    ? photoHoverCard.longText : qsTr("未设置")
            }

            Rectangle {
                anchors.fill: parent
                radius: 10
                color: root.tokens.surface
                // 柔和投影：RHI 下用 MultiEffect，软件渲染时直接去掉。
                layer.enabled: GraphicsInfo.shaderType === GraphicsInfo.RhiShader
                layer.effect: MultiEffect {
                    shadowEnabled: true
                    shadowColor: "#40000000"
                    shadowBlur: 0.55
                    blurMax: 12
                    shadowVerticalOffset: 2
                }
            }

            Column {
                id: photoHoverCardColumn
                x: 12
                y: 10
                spacing: 6

                Row {
                    spacing: 8
                    UiLabel {
                        width: 22
                        tokens: root.tokens
                        kind: noteKind
                        text: qsTr("单击")
                        font.pixelSize: root.tokens.fontSizeTiny
                        lineHeight: 1
                        verticalAlignment: Text.AlignVCenter
                    }
                    UiLabel {
                        tokens: root.tokens
                        kind: bodyKind
                        width: photoHoverCard.valueWidth
                        text: photoHoverCard.singleText.length > 0
                            ? photoHoverCard.singleText : qsTr("未设置")
                        color: photoHoverCard.singleText.length > 0
                            ? root.tokens.textPrimary : root.tokens.textTertiary
                        font.pixelSize: root.tokens.fontSizeMappingKey
                        lineHeight: 1
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                    }
                }
                Row {
                    spacing: 8
                    UiLabel {
                        width: 22
                        tokens: root.tokens
                        kind: noteKind
                        text: qsTr("双击")
                        font.pixelSize: root.tokens.fontSizeTiny
                        lineHeight: 1
                        verticalAlignment: Text.AlignVCenter
                    }
                    UiLabel {
                        tokens: root.tokens
                        kind: bodyKind
                        width: photoHoverCard.valueWidth
                        text: photoHoverCard.doubleText.length > 0
                            ? photoHoverCard.doubleText : qsTr("未设置")
                        color: photoHoverCard.doubleText.length > 0
                            ? root.tokens.textPrimary : root.tokens.textTertiary
                        font.pixelSize: root.tokens.fontSizeMappingKey
                        lineHeight: 1
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                    }
                }
                Row {
                    spacing: 8
                    UiLabel {
                        width: 22
                        tokens: root.tokens
                        kind: noteKind
                        text: qsTr("长按")
                        font.pixelSize: root.tokens.fontSizeTiny
                        lineHeight: 1
                        verticalAlignment: Text.AlignVCenter
                    }
                    UiLabel {
                        tokens: root.tokens
                        kind: bodyKind
                        width: photoHoverCard.valueWidth
                        text: photoHoverCard.longText.length > 0
                            ? photoHoverCard.longText : qsTr("未设置")
                        color: photoHoverCard.longText.length > 0
                            ? root.tokens.textPrimary : root.tokens.textTertiary
                        font.pixelSize: root.tokens.fontSizeMappingKey
                        lineHeight: 1
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                    }
                }
            }
        }

    }

}
