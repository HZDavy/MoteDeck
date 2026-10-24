import QtQuick
import QtQuick.Controls

// 稿里的 .link：12px / 600，accent-text 色，padding 0 4，圆角 6，悬停浅底，无下划线。
CompactButton {
    id: root

    flat: true
    implicitHeight: 20
    implicitWidth: contentItem.implicitWidth + 8
    leftPadding: 4
    rightPadding: 4
    font.pixelSize: 12
    font.weight: Font.DemiBold

    contentItem: Label {
        text: root.text
        font: root.font
        color: root.enabled ? root.tokens.textPrimary : root.tokens.textTertiary
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
    }

    background: Rectangle {
        radius: 6
        color: root.hovered || root.down ? root.tokens.hover : "transparent"
    }

    HoverHandler {
        cursorShape: root.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
    }
}
