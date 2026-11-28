// 「聚焦输入框」的完整配置弹窗：四种方式（自动选择 / 学习输入框 /
// 应用聚焦快捷键 / 记录点击位置），以及学习时的 3 秒倒计时。
// SettingsController is a QML singleton - see main.qml.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import MoteDeckSettings 1.0

SettingsDialog {
    id: root
    objectName: "focusInputDialog"
    title: qsTr("聚焦输入框")
    titleNote: qsTr("选择按键时把光标送进输入框的方式。")
    preferredWidth: 580
    closeButtonObjectName: "focusInputCloseButton"
    standardButtons: Dialog.Ok | Dialog.Cancel

    property string mode: "auto"
    property string elementSignature: ""
    property string shortcutText: ""
    property real pointX: 0.5
    property real pointY: 0.5
    property bool hasPoint: false
    property string errorText: ""
    property bool keepError: false
    property string pendingKind: ""
    property int countdown: 0
    // 编辑面板打开时 Qt 会吞掉弹窗里的 hover 事件，页面把窗口级鼠标位置
    // 通过这个属性送进来，下面按场景坐标自行判断悬停。
    property point hoverScenePos: Qt.point(-1, -1)

    readonly property var modes: [
        {
            id: "auto",
            title: qsTr("自动选择"),
            desc: qsTr("扫描前台窗口，自动挑最像输入框的控件。")
        },
        {
            id: "element",
            title: qsTr("学习输入框"),
            desc: qsTr("浏览一次目标输入框并记住它，位置变了也能认回来。")
        },
        {
            id: "point",
            title: qsTr("记录点击位置"),
            desc: qsTr("记住窗口里一个相对位置直接点击，适合读不到控件树的软件。")
        }
    ]

    signal focusConfigured(string text)

    function openFor(text) {
        let payload = '{"mode":"auto"}'
        try {
            payload = SettingsController.focusActionPayload(String(text || ""))
        } catch (error) {
            payload = '{"mode":"auto"}'
        }
        openForPayload(payload)
    }

    // 组合动作里的步骤直接把手上的配置塞进来，跳过按键页文本这一层。
    function openForPayload(payloadJson) {
        errorText = ""
        keepError = false
        countdown = 0
        pendingKind = ""
        captureTimer.stop()
        let data = { mode: "auto" }
        try {
            data = JSON.parse(String(payloadJson || ""))
        } catch (error) {
            data = { mode: "auto" }
        }
        mode = String(data.mode || "auto")
        elementSignature = String(data.signature || "")
        shortcutText = Array.isArray(data.keys) ? data.keys.join("+") : ""
        const point = Array.isArray(data.point) ? data.point : []
        hasPoint = point.length === 2
        pointX = hasPoint ? Number(point[0]) : 0.5
        pointY = hasPoint ? Number(point[1]) : 0.5
        open()
    }

    function startCapture(kind) {
        pendingKind = kind
        countdown = 3
        errorText = ""
        captureTimer.restart()
    }

    function finishCapture() {
        if (pendingKind === "element") {
            const payload = SettingsController.captureFocusElement()
            if (!payload) {
                errorText = qsTr("没找到可学习的输入框，请确认目标应用在前台并把光标点进输入框")
                pendingKind = ""
                return
            }
            elementSignature = payload
        } else if (pendingKind === "point") {
            const payload = SettingsController.captureFocusPoint()
            let point = null
            try {
                point = JSON.parse(payload)
            } catch (error) {
                point = null
            }
            if (!point) {
                errorText = qsTr("没有取到鼠标位置，请重试")
                pendingKind = ""
                return
            }
            pointX = Number(point.x)
            pointY = Number(point.y)
            hasPoint = true
        }
        pendingKind = ""
    }

    function describePoint() {
        return Math.round(pointX * 100) + "% / " + Math.round(pointY * 100) + "%"
    }

    function commit() {
        const payload = { mode: mode }
        if (mode === "element") {
            if (elementSignature.length === 0) {
                errorText = qsTr("请先点击「记录输入框」学习一次")
                return false
            }
            payload.signature = elementSignature
        } else if (mode === "shortcut") {
            const keys = String(shortcutText).split("+")
                .map(function(token) { return token.trim().toLowerCase() })
                .filter(function(token) { return token.length > 0 })
            if (keys.length === 0) {
                errorText = qsTr("请输入快捷键，例如 ctrl+f")
                return false
            }
            payload.keys = keys
        } else if (mode === "point") {
            if (!hasPoint) {
                errorText = qsTr("请先点击「记录位置」记录一次鼠标位置")
                return false
            }
            payload.point = [pointX, pointY]
        }
        const text = SettingsController.focusActionTextFromPayload(
            mode, JSON.stringify(payload))
        if (text.length === 0) {
            errorText = qsTr("配置不完整，请检查后重试")
            return false
        }
        errorText = ""
        root.focusConfigured(text)
        return true
    }

    onAboutToShow: {
        if (!keepError)
            errorText = ""
        keepError = false
    }
    onClosed: {
        captureTimer.stop()
        countdown = 0
        pendingKind = ""
    }
    onAccepted: {
        if (!root.commit()) {
            keepError = true
            Qt.callLater(function() { root.open() })
        }
    }

    Timer {
        id: captureTimer
        interval: 1000
        repeat: true
        onTriggered: {
            root.countdown -= 1
            if (root.countdown > 0)
                return
            // 这里不能用 root.captureTimer（id 不是 root 的属性，取到
            // undefined 会抛错、Timer 停不下来），否则倒计时永远停在负数，
            // 「记录位置」按钮就一直是灰的、点了也没反应。
            captureTimer.stop()
            root.countdown = 0
            root.finishCapture()
        }
    }

    contentItem: ColumnLayout {
        spacing: root.tokens.spacingLarge

        Repeater {
            model: root.modes
            delegate: Rectangle {
                id: modeRow
                required property var modelData

                readonly property bool selected: root.mode === modelData.id
                readonly property bool hovered: {
                    if (modeRowHover.hovered)
                        return true
                    if (root.hoverScenePos.x < 0 || root.hoverScenePos.y < 0)
                        return false
                    const p = modeRow.mapFromItem(
                        null, root.hoverScenePos.x, root.hoverScenePos.y)
                    return p.x >= 0 && p.y >= 0
                        && p.x <= modeRow.width && p.y <= modeRow.height
                }

                Layout.fillWidth: true
                implicitHeight: 54
                radius: root.tokens.cornerRadiusControl
                color: selected ? root.tokens.accentSoft
                    : modeRow.hovered
                        ? root.tokens.buttonHover : root.tokens.surfaceMuted
                border.width: (selected || modeRow.hovered)
                    ? root.tokens.structuralDividerWidth : 0
                border.color: selected
                    ? root.tokens.accent : root.tokens.borderStrong

                HoverHandler { id: modeRowHover }
                MouseArea {
                    anchors.fill: parent
                    onClicked: {
                        root.mode = modeRow.modelData.id
                        root.errorText = ""
                    }
                }
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    anchors.rightMargin: 12
                    spacing: root.tokens.spacingLarge
                    Rectangle {
                        Layout.alignment: Qt.AlignVCenter
                        width: 14
                        height: 14
                        radius: 7
                        color: "transparent"
                        border.width: root.tokens.hairlineWidth
                        border.color: modeRow.selected
                            ? root.tokens.accent : root.tokens.border
                        Rectangle {
                            anchors.centerIn: parent
                            width: 8
                            height: 8
                            radius: 4
                            color: root.tokens.accent
                            visible: modeRow.selected
                        }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        spacing: 1
                        UiLabel {
                            tokens: root.tokens
                            kind: bodyKind
                            text: modeRow.modelData.title
                        }
                        UiLabel {
                            tokens: root.tokens
                            kind: noteKind
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            text: modeRow.modelData.desc
                            elide: Text.ElideRight
                        }
                    }
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: root.tokens.spacingSmall
            visible: root.mode === "element"
            RowLayout {
                Layout.fillWidth: true
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    Layout.fillWidth: true
                    text: root.elementSignature.length > 0
                        ? qsTr("已记住目标输入框，按键时会按特征找回来")
                        : qsTr("还没有学习输入框")
                }
                CompactButton {
                    objectName: "focusLearnButton"
                    tokens: root.tokens
                    compactMinimumWidth: root.tokens.buttonWidth6Chars
                    enabled: root.countdown === 0
                    text: root.countdown > 0
                        ? qsTr("%1 秒后记录…").arg(root.countdown)
                        : qsTr("记录输入框")
                    onClicked: {
                        root.pendingKind = "element"
                        root.startCapture("element")
                    }
                }
            }
            UiLabel {
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                visible: root.countdown > 0
                text: qsTr("请在三秒内切换到目标应用，并把光标点进要记住的输入框。")
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: root.tokens.spacingSmall
            visible: root.mode === "shortcut"
            UiLabel {
                tokens: root.tokens
                kind: bodyKind
                text: qsTr("快捷键")
            }
            CompactTextField {
                objectName: "focusShortcutField"
                tokens: root.tokens
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                maximumLength: 64
                text: root.shortcutText
                placeholderText: qsTr("例如 ctrl+f")
                onTextEdited: root.shortcutText = text
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: root.tokens.spacingSmall
            visible: root.mode === "point"
            RowLayout {
                Layout.fillWidth: true
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    Layout.fillWidth: true
                    text: root.hasPoint
                        ? qsTr("已记录位置：%1").arg(root.describePoint())
                        : qsTr("还没有记录点击位置")
                }
                CompactButton {
                    objectName: "focusRecordPointButton"
                    tokens: root.tokens
                    compactMinimumWidth: root.tokens.buttonWidth6Chars
                    enabled: root.countdown === 0
                    text: root.countdown > 0
                        ? qsTr("%1 秒后记录…").arg(root.countdown)
                        : qsTr("记录位置")
                    onClicked: {
                        root.pendingKind = "point"
                        root.startCapture("point")
                    }
                }
            }
            UiLabel {
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                visible: root.countdown > 0
                text: qsTr("请切换到目标应用，把鼠标放到输入框里，倒计时结束时会记录这个位置。")
            }
        }

        UiLabel {
            objectName: "focusInputMessage"
            tokens: root.tokens
            kind: noteKind
            Layout.fillWidth: true
            visible: text.length > 0
            text: root.errorText
            color: root.tokens.errorColor
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
        }
    }
}
