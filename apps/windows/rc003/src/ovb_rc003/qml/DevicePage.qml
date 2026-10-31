import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import QtQuick.Effects
import QtQuick.Shapes
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens
    property var backTabTarget: null
    property var tabTarget: null
    readonly property var firstFocusItem: openBluetoothSettingsButton
    readonly property var lastFocusItem: closeBehaviorSwitch
    readonly property int deviceStateColumnWidth: 96
    readonly property int deviceActionColumnWidth: tokens.buttonWidth4Chars
    signal openButtonsRequested()
    signal enableHidHelperRequested()
    signal removeHidHelperRequested()

    function restoreLogExportFocus() {
        Qt.callLater(function() {
            if (root.Window.window)
                root.Window.window.requestActivate()
            if (deviceOpenLogButton.enabled)
                deviceOpenLogButton.forceActiveFocus()
            else
                diagnosticTraceSwitch.forceActiveFocus()
        })
    }

    FileDialog {
        id: logExportDialog
        objectName: "logExportDialog"
        title: qsTr("导出日志")
        fileMode: FileDialog.SaveFile
        nameFilters: [qsTr("ZIP 压缩文件 (*.zip)")]
        defaultSuffix: "zip"
        onAccepted: {
            SettingsController.exportLogs(selectedFile)
            root.restoreLogExportFocus()
        }
        onRejected: root.restoreLogExportFocus()
    }

    function checkResult(checkId) {
        const rows = DiagnosticsController.checkResults
        for (var i = 0; i < rows.length; i++) {
            if (String(rows[i].checkId) === checkId)
                return rows[i]
        }
        return null
    }

    function currentDeviceStateCode() {
        if (!SettingsController.activeRemoteKey)
            return "unselected"
        if (!SettingsController.activeRemoteReady)
            return "pending"
        if (!SettingsController.isRc003Device) {
            if (SettingsController.bridgeConnected)
                return "paired"
            return SettingsController.activeRemotePairingState
        }
        // 服务已经连上遥控器时以实连为准：启动时那次探测失败，不能把已连上
        // 的链路显示成「点击“重新检查”」。
        if (SettingsController.bridgeConnected)
            return "paired"
        if (DiagnosticsController.isRefreshing)
            return "checking"
        const row = checkResult("ble_candidate")
        if (!row)
            return "unchecked"
        const resultCode = String(row.resultCode || "")
        if (resultCode === "selected_missing")
            return "selected_missing"
        if (resultCode === "ambiguous")
            return "conflict"
        if (resultCode === "no_candidate")
            return "unpaired"
        if (row.status === "pass")
            return "paired"
        if (row.status === "manual")
            return "waiting"
        return "error"
    }

    function currentDeviceStateText() {
        switch (currentDeviceStateCode()) {
        case "unselected": return qsTr("请选择设备")
        case "pending": return qsTr("接收尚未接入")
        case "selected_missing": return qsTr("所选设备未连接")
        case "checking": return qsTr("正在检查")
        case "paired": return qsTr("已连接")
        case "waiting": return qsTr("正在确认")
        case "service_stopped": return qsTr("等待服务启动")
        case "receiver_failed": return qsTr("接收失败")
        case "conflict": return qsTr("设备识别异常")
        case "unpaired": return qsTr("点击“蓝牙设置”")
        case "error": return qsTr("点击“重新检查”")
        default: return qsTr("未检查")
        }
    }

    function currentDeviceDetailText() {
        return SettingsController.activeRemoteLabel
    }

    function currentDeviceStateColor() {
        const code = currentDeviceStateCode()
        if (code === "paired")
            return tokens.successColor
        if (code === "checking" || code === "waiting")
            return tokens.voiceAccent
        if (code === "unchecked" || code === "pending" || code === "service_stopped")
            return tokens.disabledText
        return tokens.errorColor
    }

    function valueIn(value, choices) {
        return choices.indexOf(String(value)) >= 0
    }

    function rawInputReady() {
        return SettingsController.rawInputState === "ready"
    }

    function hidTapReady() {
        return valueIn(SettingsController.hidTapState, [
            "attached_waiting_for_hid_io", "ready"
        ])
    }

    function rawInputWaitsForRemote() {
        return valueIn(SettingsController.rawInputState, [
            "no_device", "device_removed"
        ])
    }

    function hidTapWaitsForRemote() {
        return valueIn(SettingsController.hidTapState, [
            "waiting_for_rc003_host", "waiting_for_gadget_connection"
        ])
    }

    function buttonReceiverWaitsForRemote() {
        return hidTapWaitsForRemote()
            && (rawInputWaitsForRemote() || rawInputReady())
    }

    function rawInputChecking() {
        return valueIn(SettingsController.rawInputState, [
            "unknown", "starting", "recovering"
        ])
    }

    function hidTapChecking() {
        return valueIn(SettingsController.hidTapState, [
            "unknown", "verified_not_started", "starting", "injecting", "recovering"
        ])
    }

    function voiceKeyChecking() {
        return valueIn(SettingsController.voiceKeyPhysicalizerState, [
            "unknown", "starting", "recovering"
        ])
    }

    function voiceKeyFailed() {
        return valueIn(SettingsController.voiceKeyPhysicalizerState, [
            "failed", "stopped"
        ])
    }

    function buttonReceiverStateCode() {
        if (!SettingsController.activeRemoteKey)
            return "unselected"
        if (!SettingsController.activeRemoteReady)
            return "pending"
        if (SettingsController.hidHelperRepairBusy)
            return "checking"
        if (SettingsController.hidHelperIssueVisible) {
            if (SettingsController.hidHelperAccountUnsupported)
                return "account"
            if (SettingsController.hidHelperCleanupPending)
                return "cleanup"
            if (!SettingsController.hidHelperRepairVisible)
                return "version"
            return SettingsController.hidHelperSetupRequired
                ? "disabled" : "permission"
        }
        if (SettingsController.bridgeRunning) {
            if (SettingsController.hidTapState === "authorization_cancelled")
                return "source_authorization_cancelled"
            if (SettingsController.hidTapState === "authorization_failed")
                return "source_authorization_failed"
            if (SettingsController.hidTapState === "unavailable_gadget_not_verified")
                return "component_unavailable"
            if (SettingsController.hidTapState === "selected_device_shared_host")
                return "shared_host"
            if (SettingsController.hidTapState === "restart_required")
                return "restart_computer"
            if (SettingsController.bridgeLaunchBusy
                    || SettingsController.bridgeReconnectBusy)
                return "checking"
            if (SettingsController.rawInputState === "ambiguous")
                return "conflict"
            const rawReady = rawInputReady()
            const tapReady = hidTapReady()
            const deviceCode = currentDeviceStateCode()
            if (SettingsController.buttonReceiverUsable)
                return "ready"
            if (rawReady && tapReady) {
                if (voiceKeyFailed())
                    return "voice_error"
                if (voiceKeyChecking())
                    return "checking"
                return "checking"
            }
            if (buttonReceiverWaitsForRemote()) {
                if (deviceCode === "unpaired")
                    return "unpaired"
                if (deviceCode === "conflict")
                    return "conflict"
                return "waiting_remote"
            }
            if (rawReady)
                return hidTapChecking() || hidTapWaitsForRemote()
                    ? "checking" : "original_only"
            if (tapReady)
                return rawInputWaitsForRemote() ? "recovering"
                    : rawInputChecking() ? "checking" : "custom_only"
            if (rawInputChecking() || hidTapChecking())
                return "checking"
            return "error"
        }
        if (DiagnosticsController.isRefreshing)
            return "checking"
        const osRow = checkResult("os_version")
        if (osRow && (osRow.status === "fail" || osRow.status === "unsupported"))
            return "system_error"
        const rawRow = checkResult("raw_input")
        if (!rawRow)
            return "unchecked"
        const rawCode = String(rawRow.resultCode || "")
        if (rawCode === "ambiguous")
            return "conflict"
        if (rawCode === "no_device") {
            const deviceCode = currentDeviceStateCode()
            if (deviceCode === "unpaired")
                return "unpaired"
            if (deviceCode === "conflict")
                return "conflict"
            return "waiting_remote"
        }
        if (rawRow.status === "pass")
            return "service_stopped"
        return "error"
    }

    function buttonReceiverStateText() {
        switch (buttonReceiverStateCode()) {
        case "unselected": return qsTr("请先选择设备")
        case "pending": return qsTr("接收尚未接入")
        case "source_authorization_cancelled": return qsTr("授权已取消")
        case "source_authorization_failed": return qsTr("授权未完成")
        case "checking": return qsTr("正在检查")
        case "cleanup": return qsTr("点击“完成清理”")
        case "recovering": return qsTr("正在恢复")
        case "account": return qsTr("请更换账号")
        case "disabled": return qsTr("点击“启用改键”")
        case "permission": return qsTr("点击“修复权限”")
        case "version": return qsTr("请安装兼容版")
        case "component_unavailable": return qsTr("按键组件不完整")
        case "restart_computer": return qsTr("请重启电脑一次")
        case "shared_host": return qsTr("按键来源待区分")
        case "waiting_remote": return qsTr("未连接")
        case "unpaired": return qsTr("请先配对遥控器")
        case "conflict": return qsTr("设备识别异常")
        case "ready": return qsTr("正常")
        case "service_stopped": return qsTr("等待服务启动")
        case "custom_only": return qsTr("点击“重启服务”")
        case "original_only": return qsTr("点击“重启服务”")
        case "voice_error": return qsTr("点击“重启服务”")
        case "system_error": return qsTr("请升级系统")
        case "error": return SettingsController.bridgeRunning
            ? qsTr("点击“重启服务”") : qsTr("点击“重新检查”")
        default: return qsTr("未检查")
        }
    }

    function buttonReceiverStateColor() {
        const code = buttonReceiverStateCode()
        if (code === "ready")
            return tokens.successColor
        if (code === "checking" || code === "waiting_remote" || code === "recovering"
                || code === "custom_only"
                || code === "original_only")
            return tokens.voiceAccent
        if (code === "cleanup" || code === "unchecked" || code === "pending"
                || code === "service_stopped")
            return tokens.disabledText
        return tokens.errorColor
    }

    function bridgeStateText() {
        if (sourceAuthorizationRetryAvailable())
            return qsTr("等待按键授权")
        if (SettingsController.activeRemoteKey && !SettingsController.activeRemoteReady)
            return qsTr("接收尚未接入")
        if (SettingsController.bridgeReconnectBusy)
            return qsTr("连接中")
        if (SettingsController.bridgeLaunchPhase === "stopping")
            return qsTr("正在停止")
        if (SettingsController.bridgeLaunchBusy)
            return qsTr("启动中")
        if (bridgeNeedsRestartAction())
            return qsTr("点击“重启服务”")
        const receiverCode = buttonReceiverStateCode()
        if (SettingsController.bridgeConnected)
            return qsTr("已连接")
        if (SettingsController.bridgeRunning) {
            const connectionCode = String(SettingsController.bridgeConnectionState)
            if (connectionCode === "retry_wait")
                return qsTr("点击“重新连接”")
            if (connectionCode === "connecting")
                return qsTr("正在连接遥控器")
            if (receiverCode === "waiting_remote")
                return qsTr("正在等待连接")
            if (receiverCode === "unpaired" || receiverCode === "conflict")
                return qsTr("请先处理设备")
            if (bridgeReconnectActionAvailable())
                return qsTr("点击“重新连接”")
            return qsTr("正在等待连接")
        }
        if (SettingsController.bridgeLaunchPhase === "unknown"
                || SettingsController.bridgeLaunchPhase === "failed")
            return qsTr("启动失败")
        return qsTr("点击“启动服务”")
    }

    function bridgeReconnectActionAvailable() {
        if (!SettingsController.bridgeReconnectAvailable)
            return false
        const code = currentDeviceStateCode()
        if (code === "unpaired" || code === "conflict" || code === "error")
            return false
        const connectionCode = String(SettingsController.bridgeConnectionState)
        if (connectionCode === "retry_wait")
            return true
        if (connectionCode !== "waiting_for_device")
            return false
        return SettingsController.buttonReceiverUsable
    }

    function bridgeNeedsRestartAction() {
        if (sourceAuthorizationRetryAvailable())
            return false
        if (!SettingsController.bridgeRunning)
            return false
        if (SettingsController.hidTapState === "restart_required")
            return false
        if (SettingsController.isRc003Device
                && SettingsController.hidTapState === "unavailable_gadget_not_verified")
            return false
        if (SettingsController.bridgeRestartRecommended)
            return true
        const code = buttonReceiverStateCode()
        return code === "custom_only" || code === "original_only"
            || code === "voice_error" || code === "error"
    }

    function bridgeActionVisible() {
        if (sourceAuthorizationRetryAvailable())
            return true
        if (!SettingsController.bridgeRunning)
            return true
        if (SettingsController.bridgeLaunchPhase === "stopping")
            return false
        if (SettingsController.bridgeLaunchBusy
                || SettingsController.bridgeReconnectBusy)
            return true
        if (bridgeNeedsRestartAction())
            return true
        if (SettingsController.bridgeConnected)
            return false
        return bridgeReconnectActionAvailable()
    }

    function bridgeActionText() {
        if (SettingsController.bridgeReconnectBusy)
            return qsTr("连接中…")
        if (SettingsController.bridgeLaunchBusy)
            return qsTr("启动中…")
        if (!SettingsController.bridgeRunning)
            return qsTr("启动服务")
        if (sourceAuthorizationRetryAvailable())
            return qsTr("重新授权")
        if (bridgeNeedsRestartAction())
            return qsTr("重启服务")
        return qsTr("重新连接")
    }

    function bridgeStateColor() {
        if (sourceAuthorizationRetryAvailable())
            return tokens.errorColor
        if (bridgeNeedsRestartAction())
            return tokens.errorColor
        if (SettingsController.bridgeConnectionState === "retry_wait")
            return tokens.errorColor
        if (SettingsController.bridgeConnected)
            return tokens.successColor
        const receiverCode = buttonReceiverStateCode()
        if (receiverCode === "unpaired" || receiverCode === "conflict")
            return tokens.errorColor
        if (SettingsController.bridgeLaunchPhase === "unknown"
                || SettingsController.bridgeLaunchPhase === "failed")
            return tokens.errorColor
        if (SettingsController.bridgeReconnectBusy
                || SettingsController.bridgeRunning
                || SettingsController.bridgeLaunchBusy
                || !SettingsController.bridgeRunning)
            return tokens.voiceAccent
        return tokens.disabledText
    }

    function sourceAuthorizationRetryAvailable() {
        return SettingsController.isRc003Device && SettingsController.bridgeRunning
            && valueIn(SettingsController.hidTapState, ["authorization_cancelled", "authorization_failed"])
    }

    RemoteDeviceDialog {
        id: remoteDeviceDialog
        tokens: root.tokens
        anchors.centerIn: parent
    }

    AboutDialog {
        id: aboutDialog
        tokens: root.tokens
    }

    SettingsDialog {
        id: stopBridgeConfirmDialog
        objectName: "stopBridgeConfirmDialog"
        tokens: root.tokens
        preferredWidth: 390
        title: qsTr("关闭遥控器服务？")
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: SettingsController.stopBridge()

        contentItem: UiLabel {
            tokens: root.tokens
            kind: bodyKind
            wrapMode: Text.WordWrap
            text: qsTr("关闭后遥控器将无法接收按键和语音，可随时再打开。")
        }
    }

    ScrollView {
        id: deviceScroll
        objectName: "deviceScroll"
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: ScrollBar.AsNeeded

        ColumnLayout {
            id: devicePageContent
            objectName: "devicePageContent"
            width: deviceScroll.availableWidth
            spacing: 0

            // ── 页面标题 ──
            Item {
                id: devicePageHeader
                objectName: "devicePageHeader"
                Layout.fillWidth: true
                Layout.preferredHeight: 60

                RowLayout {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    height: 40
                    spacing: 12

                    Label {
                        objectName: "devicePageTitle"
                        text: qsTr("设备")
                        font.family: tokens.fontFamilyDisplay
                        font.pixelSize: tokens.fontSizePageTitle
                        font.weight: Font.DemiBold
                        color: tokens.textPrimary
                        Layout.alignment: Qt.AlignBaseline
                    }
                    Label {
                        objectName: "devicePageSubtitle"
                        text: qsTr("遥控器、服务与启动行为")
                        font.family: tokens.fontFamily
                        font.pixelSize: tokens.fontSizeBody
                        color: tokens.textTertiary
                        Layout.alignment: Qt.AlignBaseline
                        Layout.fillWidth: true
                    }
                }
            }

            // ── 墨黑主卡：实物图 + 当前设备 + 右侧操作 ──
            Rectangle {
                id: deviceHero
                objectName: "currentDeviceRow"
                Layout.fillWidth: true
                Layout.preferredHeight: 132
                property string stateText: root.currentDeviceStateText()
                property string descriptionText: SettingsController.activeRemoteLabel
                property color stateColor: root.currentDeviceStateColor()
                radius: root.tokens.cornerRadiusCard
                clip: true
                color: "transparent"

                // linear-gradient(135deg, …)：Rectangle.gradient 只能横/竖，
                // 斜向底色用 Shape 的 LinearGradient 画。CSS 135° 的渐变线过中心、
                // 方向 (1,1)，长度 (W+H)/√2，起止点即中心 ∓ (W+H)/4。
                Shape {
                    id: heroBackdrop
                    anchors.fill: parent
                    preferredRendererType: Shape.CurveRenderer
                    readonly property real reach: (width + height) / 4
                    ShapePath {
                        strokeColor: "transparent"
                        fillGradient: LinearGradient {
                            x1: heroBackdrop.width / 2 - heroBackdrop.reach
                            y1: heroBackdrop.height / 2 - heroBackdrop.reach
                            x2: heroBackdrop.width / 2 + heroBackdrop.reach
                            y2: heroBackdrop.height / 2 + heroBackdrop.reach
                            GradientStop {
                                position: 0.0
                                color: root.tokens && root.tokens.darkMode ? "#2a2a2a" : "#262626"
                            }
                            GradientStop {
                                position: 0.45
                                color: root.tokens && root.tokens.darkMode ? "#1c1c1c" : "#161616"
                            }
                            GradientStop {
                                position: 1.0
                                color: root.tokens && root.tokens.darkMode ? "#141414" : "#0c0c0c"
                            }
                        }
                        PathRectangle {
                            width: deviceHero.width
                            height: deviceHero.height
                            radius: deviceHero.radius
                        }
                    }
                }

                // 设计稿的两层径向柔光：
                //   radial-gradient(120% 140% at 12% 0%, rgba(255,255,255,.13), transparent 55%)
                //   radial-gradient(80% 120% at 100% 100%, rgba(255,255,255,.05), transparent 60%)
                // 用 HeroGlow 还原成真正的椭圆径向渐变，亮部从左上斜向衰减，
                // 不能再用整宽的竖向渐变（那会让卡片看起来是「上亮下暗」的直上直下）。
                HeroGlow {
                    hero: deviceHero
                    centerXFactor: 0.12
                    centerYFactor: 0.0
                    radiusXFactor: 1.2
                    radiusYFactor: 1.4
                    edgePosition: 0.55
                    glowColor: "#21ffffff"
                }

                HeroGlow {
                    hero: deviceHero
                    centerXFactor: 1.0
                    centerYFactor: 1.0
                    radiusXFactor: 0.8
                    radiusYFactor: 1.2
                    edgePosition: 0.6
                    glowColor: "#0dffffff"
                }

                // .hero-pic：112 宽，实物图高 280、top 14 只露上半截，
                // 从 60% 处开始透明渐隐（mask-image）；遥控器带投影，
                // 看上去像插在卡槽里，而不是压在卡片边缘上。
                Item {
                    id: heroPhoto
                    objectName: "deviceHeroPhoto"
                    x: 12
                    width: 112
                    height: parent.height
                    clip: true

                    Image {
                        id: heroPhotoImage
                        objectName: "deviceHeroPhotoImage"
                        y: 14
                        // 谷歌实物图是 453×1443 的细长机身，按 280 高放大会盖满卡槽；
                        // 只把谷歌档案放小，小米仍按设计稿的 280。
                        height: SettingsController.isRc003Device ? 280 : 230
                        width: sourceSize.height > 0
                            ? height * sourceSize.width / sourceSize.height : height * 2 / 3
                        x: (parent.width - width) / 2
                        source: SettingsController.photoAvailable
                            ? SettingsController.photoSource : ""
                        fillMode: Image.PreserveAspectFit
                        smooth: true
                        mipmap: true
                        layer.enabled: source.toString().length > 0
                        layer.effect: MultiEffect {
                            shadowEnabled: true
                            shadowColor: "#73000000"
                            shadowVerticalOffset: 10
                            shadowBlur: 0.9
                            blurMax: 18
                            autoPaddingEnabled: true
                        }
                    }

                    // 真正的 alpha 蒙版：linear-gradient(#000 60%, transparent)。
                    // 实物图到卡片底边时已完全透明，不会从圆角处露出，
                    // 也不会因斜向底色而出现一块颜色对不上的方块。
                    layer.enabled: heroPhotoImage.source.toString().length > 0
                    layer.effect: MultiEffect {
                        maskEnabled: true
                        maskSource: heroPhotoMask
                        // MultiEffect 掩码 = smoothstep(min·(1+s)−s, min·(1+s), α)；
                        // min 0.5、s 1 即 smoothstep(0, 1, α)，渐隐连续不截断。
                        maskThresholdMin: 0.5
                        maskSpreadAtMin: 1.0
                    }
                }

                Rectangle {
                    id: heroPhotoMask
                    objectName: "deviceHeroPhotoFade"
                    width: heroPhoto.width
                    height: heroPhoto.height
                    visible: false
                    layer.enabled: true
                    gradient: Gradient {
                        GradientStop { position: 0.0; color: "#ffffffff" }
                        GradientStop { position: 0.6; color: "#ffffffff" }
                        GradientStop { position: 1.0; color: "#00ffffff" }
                    }
                }

                // .hero-body：两行（当前设备 + pill / 设备名），垂直居中
                Column {
                    anchors.left: heroPhoto.right
                    anchors.leftMargin: 20
                    anchors.right: heroActions.left
                    anchors.rightMargin: 16
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 6

                    Row {
                        spacing: 10
                        height: 22

                        Label {
                            text: qsTr("当前设备")
                            font.family: tokens.fontFamily
                            font.pixelSize: 12
                            font.weight: Font.Medium
                            color: "#a3a3a3"
                            anchors.verticalCenter: parent.verticalCenter
                        }
                        StatusPill {
                            tokens: root.tokens
                            onHero: true
                            ok: root.currentDeviceStateCode() === "paired"
                            label: root.currentDeviceStateText()
                            dotColor: ok ? "transparent" : root.currentDeviceStateColor()
                            visible: label.length > 0
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }

                    Label {
                        objectName: "currentDeviceNameLabel"
                        width: parent.width
                        text: SettingsController.currentRemoteModelName.length > 0
                            ? SettingsController.currentRemoteModelName
                            : SettingsController.activeRemoteLabel
                        font.family: tokens.fontFamilyDisplay
                        font.pixelSize: tokens.fontSizeHeroTitle
                        font.weight: Font.DemiBold
                        font.letterSpacing: -0.24
                        lineHeight: 32
                        lineHeightMode: Text.FixedHeight
                        color: "#f2f2f2"
                        elide: Text.ElideRight
                    }
                }

                // .hero .act：贴底，margin-bottom 20，gap 4
                Row {
                    id: heroActions
                    anchors.right: parent.right
                    anchors.rightMargin: 20
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: 20
                    spacing: 4

                    IconButton {
                        id: refreshDeviceChecksButton
                        objectName: "refreshDeviceChecksButton"
                        tokens: root.tokens
                        inverse: true
                        iconName: "refresh"
                        text: qsTr("重新检查")
                        enabled: !DiagnosticsController.isRefreshing
                            && !DiagnosticsController.vbCableTestRunning
                            && !SettingsController.configurationReadOnly
                        Accessible.name: DiagnosticsController.isRefreshing
                            ? qsTr("检查中…") : qsTr("重新检查")
                        onClicked: {
                            DiagnosticsController.refreshDiagnostics()
                            if (!SettingsController.isRc003Device)
                                SettingsController.refreshRemoteDevices()
                        }
                        KeyNavigation.backtab: root.backTabTarget
                        KeyNavigation.tab: openBluetoothSettingsButton
                    }

                    CompactButton {
                        id: openBluetoothSettingsButton
                        objectName: "openBluetoothSettingsButton"
                        tokens: root.tokens
                        inverse: true
                        flat: true
                        trailingIcon: "external"
                        text: qsTr("蓝牙设置")
                        Accessible.name: text
                        onClicked: SettingsController.openBluetoothSettings()
                        KeyNavigation.backtab: refreshDeviceChecksButton
                        KeyNavigation.tab: selectRemoteButton
                    }

                    CompactButton {
                        id: selectRemoteButton
                        objectName: "selectRemoteButton"
                        enabled: !SettingsController.configurationReadOnly
                        tokens: root.tokens
                        inverse: true
                        text: qsTr("选择设备")
                        onClicked: remoteDeviceDialog.open()
                        KeyNavigation.backtab: openBluetoothSettingsButton
                        KeyNavigation.tab: buttonReceiverPrimaryButton
                    }
                }

                // 1px 描边：放在最上层，避免被斜向底色 Shape 盖住；
                // 深色界面下靠它区分卡片和页面背景。
                Rectangle {
                    objectName: "deviceHeroBorder"
                    anchors.fill: parent
                    radius: parent.radius
                    color: "transparent"
                    border.width: 1
                    border.color: "#14ffffff"
                }
            }

            // ── 三块功能瓦片 ──
            RowLayout {
                id: deviceTiles
                objectName: "deviceTiles"
                Layout.fillWidth: true
                Layout.preferredHeight: 119
                Layout.topMargin: 16
                spacing: 12

                DeviceTile {
                    objectName: "buttonReceiverRow"
                    Layout.fillWidth: true
                    Layout.preferredWidth: 0
                    tokens: root.tokens
                    iconName: "keys"
                    titleText: qsTr("按键接收")
                    descriptionText: root.buttonReceiverStateCode() === "source_authorization_cancelled"
                        ? qsTr("未完成管理员授权，点击“重新授权”")
                        : root.buttonReceiverStateCode() === "source_authorization_failed"
                        ? qsTr("未取得按键权限，点击“重新授权”")
                        : root.buttonReceiverStateCode() === "restart_computer"
                        ? qsTr("旧按键组件未释放，请重启电脑")
                        : root.buttonReceiverStateCode() === "component_unavailable"
                        ? qsTr("组件缺失或校验失败，补齐后重开程序")
                        : root.buttonReceiverStateCode() === "recovering"
                        ? qsTr("按键设备尚未就绪，正在自动重试")
                        : qsTr("让遥控器按键在电脑上生效")
                    stateText: root.buttonReceiverStateText()
                    stateColor: root.buttonReceiverStateColor()

                    CompactButton {
                        objectName: "repairHidHelperButton"
                        visible: SettingsController.hidHelperRepairVisible
                        tokens: root.tokens
                        flat: true
                        text: SettingsController.hidHelperRepairBusy
                            ? qsTr("处理中…")
                            : (SettingsController.hidHelperSetupRequired
                                ? qsTr("启用改键")
                                : SettingsController.hidHelperCleanupPending
                                ? qsTr("完成清理") : qsTr("修复权限"))
                        highlighted: false
                        enabled: !SettingsController.hidHelperRepairBusy
                            && !SettingsController.bridgeLaunchBusy
                            && !SettingsController.configurationReadOnly
                        onClicked: root.enableHidHelperRequested()
                    }
                    CompactButton {
                        objectName: "removeHidHelperButton"
                        visible: SettingsController.hidHelperRemovalVisible
                        tokens: root.tokens
                        flat: true
                        text: qsTr("移除权限")
                        enabled: !SettingsController.hidHelperRepairBusy
                            && !SettingsController.bridgeLaunchBusy
                            && !SettingsController.configurationReadOnly
                        onClicked: root.removeHidHelperRequested()
                    }
                    CompactButton {
                        id: buttonReceiverPrimaryButton
                        objectName: "openButtonSettingsButton"
                        tokens: root.tokens
                        flat: true
                        text: qsTr("按键设置")
                        onClicked: root.openButtonsRequested()
                    }
                }

                DeviceTile {
                    objectName: "remoteServiceRow"
                    Layout.fillWidth: true
                    Layout.preferredWidth: 0
                    tokens: root.tokens
                    focusTile: root.bridgeActionVisible()
                    iconName: "remote"
                    titleText: qsTr("遥控器服务")
                    descriptionText: qsTr("连接遥控器，接收按键和语音")
                    stateText: root.bridgeStateText()
                    stateColor: root.bridgeStateColor()

                    CompactButton {
                        objectName: "stopBridgeButton"
                        visible: SettingsController.bridgeRunning
                            && !SettingsController.bridgeReconnectBusy
                            && (SettingsController.bridgeLaunchPhase === "stopping"
                                || !SettingsController.bridgeLaunchBusy)
                        tokens: root.tokens
                        flat: true
                        text: SettingsController.bridgeLaunchPhase === "stopping"
                            ? qsTr("停止中…") : qsTr("关闭服务")
                        enabled: SettingsController.bridgeLaunchPhase !== "stopping"
                            && !SettingsController.bridgeLaunchBusy
                            && !SettingsController.configurationReadOnly
                            && !SettingsController.hidHelperRepairBusy
                            && !DiagnosticsController.vbCableTestRunning
                            && !DiagnosticsController.driverActionRunning
                            && !SettingsController.voiceHotkeyBusy
                            && !SettingsController.endpointPreflightBusy
                        onClicked: stopBridgeConfirmDialog.open()
                    }

                    CompactButton {
                        objectName: "bridgeActionButton"
                        visible: root.bridgeActionVisible()
                        tokens: root.tokens
                        highlighted: true
                        text: root.bridgeActionText()
                        enabled: !SettingsController.bridgeLaunchBusy
                            && !SettingsController.configurationReadOnly
                            && (SettingsController.bridgeRunning || !SettingsController.activeRemoteKey
                                || SettingsController.activeRemoteReady)
                            && !SettingsController.bridgeReconnectBusy
                            && !SettingsController.hidHelperRepairBusy
                            && !DiagnosticsController.vbCableTestRunning
                            && !DiagnosticsController.driverActionRunning
                            && !SettingsController.voiceHotkeyBusy
                            && !SettingsController.endpointPreflightBusy
                        onClicked: {
                            if (!SettingsController.bridgeRunning)
                                SettingsController.startBridge()
                            else if (root.sourceAuthorizationRetryAvailable() || root.bridgeNeedsRestartAction())
                                SettingsController.restartBridge()
                            else if (root.bridgeReconnectActionAvailable())
                                SettingsController.reconnectBridgeNow()
                        }
                    }
                }

                DeviceTile {
                    objectName: "runtimeLogRow"
                    Layout.fillWidth: true
                    Layout.preferredWidth: 0
                    tokens: root.tokens
                    iconName: "refresh"
                    titleText: qsTr("检查更新")
                    descriptionText: qsTr("有新版本时在这里提示")
                    stateText: SettingsController.applicationUpdateCanDownload
                        ? qsTr("有新版本") : ""
                    stateColor: root.tokens.accent

                    CompactButton {
                        id: deviceUsageLink
                        objectName: "deviceUsageLink"
                        tokens: root.tokens
                        flat: true
                        text: qsTr("关于")
                        KeyNavigation.tab: checkApplicationUpdateButton
                        onClicked: aboutDialog.open()
                    }
                    CompactButton {
                        id: checkApplicationUpdateButton
                        objectName: "checkApplicationUpdateButton"
                        tokens: root.tokens
                        text: SettingsController.applicationUpdateCheckBusy
                            ? qsTr("检查中…")
                            : (SettingsController.applicationUpdateCanDownload
                                ? qsTr("查看更新") : qsTr("检查更新"))
                        enabled: !SettingsController.applicationUpdateBusy
                        KeyNavigation.backtab: deviceUsageLink
                        KeyNavigation.tab: launchAtLoginSwitch
                        onClicked: {
                            if (SettingsController.applicationUpdateCanDownload)
                                SettingsController.showApplicationUpdate()
                            else
                                SettingsController.checkForApplicationUpdate()
                        }
                    }
                }
            }

            // ── 通用设置 ──
            SectionFrame {
                objectName: "desktopBehaviorSection"
                tokens: root.tokens
                Layout.fillWidth: true
                Layout.topMargin: 20
                titleText: qsTr("通用设置")
                titleObjectName: "desktopBehaviorSectionTitle"
                titleActions: CompactLink {
                    objectName: "deviceRetrySaveLink"
                    tokens: root.tokens
                    text: qsTr("重试保存")
                    visible: SettingsController.mappingAutoSaveRetryAvailable
                    onClicked: SettingsController.retryMappingAutoSave()
                }

                Row {
                    id: generalDualRow
                    Layout.fillWidth: true
                    Layout.preferredHeight: 48

                    Item {
                        width: Math.floor((generalDualRow.width - 1) / 2)
                        height: generalDualRow.height

                        SettingCell {
                            objectName: "launchAtLoginRow"
                            anchors.fill: parent
                            anchors.rightMargin: 16
                            tokens: root.tokens
                            titleText: qsTr("随 Windows 启动")
                            descriptionText: qsTr("登录后后台运行")

                            CompactSwitch {
                                id: launchAtLoginSwitch
                                objectName: "launchAtLoginSwitch"
                                tokens: root.tokens
                                checked: SettingsController.launchAtLogin
                                Accessible.name: qsTr("随 Windows 启动")
                                onToggled: {
                                    if (checked !== SettingsController.launchAtLogin)
                                        SettingsController.setLaunchAtLogin(checked)
                                }
                            }
                        }
                    }

                    Rectangle {
                        width: 1
                        height: parent.height
                        color: root.tokens.border
                    }

                    Item {
                        width: Math.ceil((generalDualRow.width - 1) / 2)
                        height: generalDualRow.height

                        SettingCell {
                            objectName: "launchBridgeOnAppStartRow"
                            anchors.fill: parent
                            anchors.leftMargin: 16
                            tokens: root.tokens
                            titleText: qsTr("启动程序时自动运行服务")
                            descriptionText: qsTr("自动连接遥控器")
                            enabled: !SettingsController.configurationReadOnly

                            CompactSwitch {
                                id: launchBridgeOnAppStartSwitch
                                objectName: "launchBridgeOnAppStartSwitch"
                                tokens: root.tokens
                                checked: SettingsController.launchBridgeOnAppStart
                                Accessible.name: qsTr("启动程序时自动运行遥控器服务")
                                onToggled: {
                                    if (checked !== SettingsController.launchBridgeOnAppStart)
                                        SettingsController.setLaunchBridgeOnAppStart(checked)
                                }
                            }
                        }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: tokens.hairlineWidth
                    color: root.tokens.border
                }

                Row {
                    id: generalSecondRow
                    Layout.fillWidth: true
                    Layout.preferredHeight: 48

                    Item {
                        width: Math.floor((generalSecondRow.width - 1) / 2)
                        height: generalSecondRow.height

                        SettingCell {
                            objectName: "closeBehaviorRow"
                            anchors.fill: parent
                            anchors.rightMargin: 16
                            tokens: root.tokens
                            titleText: qsTr("关闭窗口时隐藏到任务栏")
                            descriptionText: qsTr("保留右下角图标")

                            CompactSwitch {
                                id: closeBehaviorSwitch
                                objectName: "closeBehaviorSwitch"
                                tokens: root.tokens
                                checked: SettingsController.closeBehavior !== "quit"
                                Accessible.name: qsTr("关闭窗口时隐藏到任务栏")
                                onToggled: SettingsController.setCloseBehaviorIndex(checked ? 0 : 1)
                                KeyNavigation.tab: root.tabTarget
                            }
                        }
                    }

                    Rectangle {
                        width: 1
                        height: parent.height
                        color: root.tokens.border
                    }

                    Item {
                        width: Math.ceil((generalSecondRow.width - 1) / 2)
                        height: generalSecondRow.height

                        SettingCell {
                            objectName: "appearanceRow"
                            anchors.fill: parent
                            anchors.leftMargin: 16
                            tokens: root.tokens
                            titleText: qsTr("外观")
                            descriptionText: qsTr("界面主题")

                            ChipGroup {
                                objectName: "appearanceChips"
                                tokens: root.tokens
                                model: [qsTr("跟随系统"), qsTr("浅色"), qsTr("深色")]
                                currentIndex: root.tokens.themeMode
                                onActivated: function(index) { root.tokens.setThemeMode(index) }
                            }
                        }
                    }
                }
            }

            // ── 软件协助 ──
            SectionFrame {
                objectName: "softwareAssistSection"
                tokens: root.tokens
                Layout.fillWidth: true
                Layout.topMargin: 12
                titleText: qsTr("软件协助")
                titleObjectName: "softwareAssistSectionTitle"

                SettingRow {
                    objectName: "diagnosticTraceRow"
                    tokens: root.tokens
                    Layout.fillWidth: true
                    inlineLabel: true
                    showDivider: false
                    titleText: qsTr("诊断日志")
                    descriptionText: qsTr("记录按键与语音排查信息，本地保存")

                    CompactButton {
                        id: deviceLogLocationLink
                        objectName: "deviceLogLocationLink"
                        tokens: root.tokens
                        flat: true
                        text: qsTr("打开日志位置")
                        KeyNavigation.backtab: launchBridgeOnAppStartSwitch
                        KeyNavigation.tab: deviceOpenLogButton
                        onClicked: SettingsController.openLogLocation()
                    }
                    CompactButton {
                        id: deviceOpenLogButton
                        objectName: "deviceOpenLogButton"
                        tokens: root.tokens
                        flat: true
                        text: SettingsController.logExportBusy ? qsTr("导出中…") : qsTr("导出日志")
                        enabled: !SettingsController.logExportBusy
                        KeyNavigation.backtab: deviceLogLocationLink
                        KeyNavigation.tab: diagnosticTraceSwitch
                        onClicked: {
                            logExportDialog.selectedFile = SettingsController.logExportDefaultFile()
                            logExportDialog.open()
                        }
                    }
                    CompactSwitch {
                        id: diagnosticTraceSwitch
                        objectName: "diagnosticTraceSwitch"
                        enabled: !SettingsController.configurationReadOnly
                        tokens: root.tokens
                        checked: SettingsController.diagnosticTraceEnabled
                        Accessible.name: qsTr("诊断日志")
                        KeyNavigation.backtab: deviceOpenLogButton
                        onToggled: {
                            if (checked !== SettingsController.diagnosticTraceEnabled)
                                SettingsController.setDiagnosticTraceEnabled(checked)
                        }
                    }
                }
            }
        }
    }

    // 功能瓦片：图标 + 右上操作 + 标题 + 说明
    component DeviceTile: Rectangle {
        id: tile

        property var tokens
        property string iconName: ""
        property string titleText: ""
        property string descriptionText: ""
        property string stateText: ""
        property color stateColor: tokens.successColor
        property bool focusTile: false
        default property alias actions: tileActions.data

        implicitHeight: 119
        radius: tokens.cornerRadiusCard
        color: focusTile ? tokens.accentSoft : tokens.surface
        border.width: 1
        border.color: focusTile ? tokens.accentLine : tokens.border

        // .tile：padding 14 14 12 16；.tile-top 32 高，两端对齐
        ColumnLayout {
            anchors.fill: parent
            anchors.leftMargin: 16
            anchors.rightMargin: 14
            anchors.topMargin: 14
            anchors.bottomMargin: 12
            spacing: 0

            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 32
                spacing: 4

                Rectangle {
                    Layout.preferredWidth: 32
                    Layout.preferredHeight: 32
                    radius: 10
                    color: tile.focusTile ? tokens.accent : tokens.surfaceMuted

                    SvgIcon {
                        anchors.centerIn: parent
                        name: tile.iconName
                        size: 18
                        color: tile.focusTile ? tokens.accentText : tokens.textSecondary
                    }
                }

                Item { Layout.fillWidth: true }

                RowLayout {
                    id: tileActions
                    spacing: 4
                }
            }

            Label {
                objectName: tile.objectName.length > 0
                    ? tile.objectName + "_titleLabel" : ""
                Layout.topMargin: 10
                Layout.fillWidth: true
                text: tile.titleText
                font.family: tokens.fontFamily
                font.pixelSize: tokens.fontSizeSection
                font.weight: Font.DemiBold
                lineHeight: 20
                lineHeightMode: Text.FixedHeight
                color: tokens.textPrimary
                elide: Text.ElideRight
            }
            // 稿里瓦片不显示状态字，仅保留给辅助功能/测试读取
            Label {
                objectName: tile.objectName.length > 0
                    ? tile.objectName + "_stateLabel" : ""
                visible: false
                text: tile.stateText
            }

            Label {
                objectName: tile.objectName.length > 0
                    ? tile.objectName + "_descriptionLabel" : ""
                lineHeight: 16
                lineHeightMode: Text.FixedHeight
                text: tile.descriptionText
                font.family: tokens.fontFamily
                font.pixelSize: 12
                color: tokens.textTertiary
                elide: Text.ElideRight
                Layout.fillWidth: true
            }

            Item { Layout.fillHeight: true }
        }
    }

    // CSS radial-gradient 的椭圆径向渐变：Qt Shape 的 RadialGradient 只能画正圆，
    // 这里先把形状按半径比例画成正圆，再对 Shape 做纵向缩放，渐变随之变成椭圆。
    component HeroGlow: Shape {
        id: heroGlow
        required property Rectangle hero
        // 渐变中心相对卡片宽/高的比例（对应 CSS 的 at x% y%）
        required property real centerXFactor
        required property real centerYFactor
        // 椭圆半径相对卡片宽/高的比例（对应 CSS 的 120% 140%）
        required property real radiusXFactor
        required property real radiusYFactor
        // 相对半径的衰减位置（对应 CSS 的 transparent 55%）
        property real edgePosition: 0.55
        property color glowColor: "#21ffffff"

        preferredRendererType: Shape.CurveRenderer
        readonly property real radiusX: hero.width * radiusXFactor
        readonly property real radiusY: hero.height * radiusYFactor
        readonly property real yScale: radiusX > 0 ? radiusY / radiusX : 1
        readonly property real cornerX: hero.radius
        readonly property real cornerY: yScale > 0 ? hero.radius / yScale : hero.radius

        width: hero.width
        height: hero.height / yScale
        transform: Scale {
            origin.x: 0
            origin.y: 0
            yScale: heroGlow.yScale
        }

        ShapePath {
            strokeColor: "transparent"
            fillGradient: RadialGradient {
                centerX: heroGlow.width * heroGlow.centerXFactor
                centerY: heroGlow.height * heroGlow.centerYFactor
                centerRadius: heroGlow.radiusX
                // QtQuick.Shapes 的 RadialGradient 焦点默认在 (0,0) 而不是圆心，
                // 不显式对齐的话渐变会变成左上角顶出来的一道锥形。
                focalX: centerX
                focalY: centerY
                focalRadius: 0
                GradientStop { position: 0.0; color: heroGlow.glowColor }
                GradientStop { position: heroGlow.edgePosition; color: "#00ffffff" }
            }
            // 柔光层必须和卡片同样圆角，否则亮部所在的左上/右下会露出直角。
            // Shape 之后会纵向缩放 yScale，所以竖直方向的圆角半径要先除以 yScale。
            startX: heroGlow.cornerX
            startY: 0
            PathLine { x: heroGlow.width - heroGlow.cornerX; y: 0 }
            PathArc {
                x: heroGlow.width; y: heroGlow.cornerY
                radiusX: heroGlow.cornerX; radiusY: heroGlow.cornerY
            }
            PathLine { x: heroGlow.width; y: heroGlow.height - heroGlow.cornerY }
            PathArc {
                x: heroGlow.width - heroGlow.cornerX; y: heroGlow.height
                radiusX: heroGlow.cornerX; radiusY: heroGlow.cornerY
            }
            PathLine { x: heroGlow.cornerX; y: heroGlow.height }
            PathArc {
                x: 0; y: heroGlow.height - heroGlow.cornerY
                radiusX: heroGlow.cornerX; radiusY: heroGlow.cornerY
            }
            PathLine { x: 0; y: heroGlow.cornerY }
            PathArc {
                x: heroGlow.cornerX; y: 0
                radiusX: heroGlow.cornerX; radiusY: heroGlow.cornerY
            }
        }
    }
}
