import QtQuick
import QtQuick.Controls

// 稿里的 .chips：inset 底 + line 边框，padding 3，圆角 11；
// chip 高 26、左右 12、圆角 8、12px/500；选中项 panel 底 + 细描边阴影。
Rectangle {
    id: root

    property var tokens
    property var model: []
    property int currentIndex: 0
    signal activated(int index)

    implicitWidth: chipRow.implicitWidth + 6
    implicitHeight: 32
    radius: 11
    color: tokens.surfaceMuted
    border.width: tokens.hairlineWidth
    border.color: tokens.line

    Row {
        id: chipRow
        anchors.centerIn: parent
        spacing: 2

        Repeater {
            model: root.model

            delegate: AbstractButton {
                id: chip
                required property int index
                required property var modelData
                readonly property bool on: index === root.currentIndex

                implicitWidth: chipLabel.implicitWidth + 24
                implicitHeight: 26
                focusPolicy: Qt.StrongFocus
                Accessible.role: Accessible.RadioButton
                Accessible.name: modelData
                Accessible.checked: on
                onClicked: root.activated(index)

                background: Item {
                    // 0 0 0 1px line + 0 1px 2px rgba(0,0,0,.08)
                    Rectangle {
                        visible: chip.on
                        anchors.fill: parent
                        anchors.topMargin: 1
                        anchors.bottomMargin: -1
                        radius: 8
                        color: Qt.rgba(0, 0, 0, 0.08)
                    }
                    Rectangle {
                        anchors.fill: parent
                        radius: 8
                        color: chip.on ? root.tokens.surface
                                       : (chip.hovered ? root.tokens.hover : "transparent")
                        border.width: chip.on ? root.tokens.hairlineWidth : 0
                        border.color: root.tokens.line
                    }
                    Rectangle {
                        visible: chip.visualFocus
                        anchors.fill: parent
                        anchors.margins: -2
                        radius: 10
                        color: "transparent"
                        border.width: 2
                        border.color: root.tokens.accent
                    }
                }

                contentItem: Label {
                    id: chipLabel
                    text: chip.modelData
                    font.family: root.tokens.fontFamily
                    font.pixelSize: 12
                    font.weight: Font.Medium
                    color: chip.on ? root.tokens.textPrimary : root.tokens.textSecondary
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
            }
        }
    }
}
