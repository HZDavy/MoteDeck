import QtQuick
import QtQuick.Controls

// 稿里的 .btn-i：30×30 透明底图标按钮，图标 18（compact 时 28 / 16）。
Button {
    id: root

    property var tokens
    property string iconName: ""
    property bool inverse: false
    property bool compact: false

    implicitWidth: compact ? 28 : 30
    implicitHeight: compact ? 28 : 30
    padding: 0
    hoverEnabled: true

    contentItem: Item {
        SvgIcon {
            anchors.centerIn: parent
            name: root.iconName
            size: root.compact ? 16 : 18
            color: root.inverse ? "#d4d4d4" : root.tokens.textSecondary
        }
    }

    background: Rectangle {
        radius: 8
        color: root.down || root.hovered
            ? (root.inverse ? Qt.rgba(1, 1, 1, 0.08) : root.tokens.hover)
            : "transparent"
        Behavior on color { ColorAnimation { duration: 80 } }

        Rectangle {
            anchors.fill: parent
            anchors.margins: -2
            radius: parent.radius + 2
            color: "transparent"
            border.width: 1
            border.color: root.tokens.accent
            visible: root.visualFocus
        }
    }
    opacity: enabled ? 1 : 0.3
}
