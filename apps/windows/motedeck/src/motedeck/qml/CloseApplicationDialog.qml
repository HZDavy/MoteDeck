// 「关闭应用」的配置弹窗：先选关闭方式。选「关闭指定应用」后复用
// 「启动应用」的选择器作为三级弹窗，选完程序再回填动作文本。
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import MoteDeckSettings 1.0

SettingsDialog {
    id: root
    objectName: "closeApplicationDialog"
    title: qsTr("关闭应用")
    titleNote: qsTr("选择按键触发时要关闭哪个应用。")
    preferredWidth: 560
    closeButtonObjectName: "closeApplicationCloseButton"
    standardButtons: Dialog.Cancel

    property int targetRow: -1
    property string targetTrigger: "single_click"
    // 编辑面板打开时 Qt 会吞掉弹窗里的 hover 事件，页面把窗口级鼠标位置
    // 通过这个属性送进来，下面按场景坐标自行判断悬停。
    property point hoverScenePos: Qt.point(-1, -1)

    signal currentRequested()
    signal specifiedRequested()

    readonly property var modes: [
        {
            id: "specified",
            title: qsTr("关闭指定应用"),
            desc: qsTr("选择一个应用，按键时把它关闭。")
        },
        {
            id: "current",
            title: qsTr("关闭当前应用"),
            desc: qsTr("按键时关闭当前前台的应用窗口。")
        }
    ]

    contentItem: ColumnLayout {
        spacing: root.tokens.spacingLarge

        Repeater {
            model: root.modes
            delegate: Rectangle {
                id: modeRow
                required property var modelData
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
                color: modeRow.hovered
                    ? root.tokens.buttonHover : root.tokens.surfaceMuted
                border.width: modeRow.hovered ? root.tokens.hairlineWidth : 0
                border.color: root.tokens.borderStrong
                objectName: modeRow.modelData.id === "current"
                    ? "closeApplicationCurrentCard"
                    : "closeApplicationSpecifiedCard"

                HoverHandler { id: modeRowHover }
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        if (modeRow.modelData.id === "current")
                            root.currentRequested()
                        else
                            root.specifiedRequested()
                    }
                }
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    anchors.rightMargin: 12
                    spacing: root.tokens.spacingLarge
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
    }
}
