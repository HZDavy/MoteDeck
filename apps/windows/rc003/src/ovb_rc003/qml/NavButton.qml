import QtQuick
import QtQuick.Controls

// 左侧导航项：72×56，图标 20 在上、文字 11/14 在下（间距 4）；选中竖条见 main.qml navigationIndicator。
AbstractButton {
    id: root

    property var tokens
    property string iconName: ""

    checkable: false
    hoverEnabled: true
    implicitWidth: 72
    implicitHeight: tokens.navigationItemHeight
    padding: 0
    leftInset: 0
    rightInset: 0
    topInset: 0
    bottomInset: 0

    readonly property color ink: checked ? tokens.accent : tokens.textTertiary

    contentItem: Item {
        Column {
            anchors.centerIn: parent
            spacing: 4

            SvgIcon {
                id: navIcon
                anchors.horizontalCenter: parent.horizontalCenter
                name: root.iconName
                size: 20
                color: root.ink
            }
            Label {
                anchors.horizontalCenter: parent.horizontalCenter
                text: root.text
                color: root.ink
                font.family: root.tokens.fontFamily
                font.pixelSize: 11
                font.weight: root.checked ? Font.DemiBold : Font.Normal
                lineHeight: 14
                lineHeightMode: Text.FixedHeight
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
        }
    }

    background: Rectangle {
        objectName: root.objectName + "_background"
        color: root.hovered ? root.tokens.hover : "transparent"
        Behavior on color { ColorAnimation { duration: 80 } }
    }

    // 选中竖条由 main.qml 中共享的 navigationIndicator 绘制（可在各项间滑动）；
    // 这里只负责新选中图标的描线动画。
    onCheckedChanged: if (checked) drawAnimation.restart()

    NumberAnimation {
        id: drawAnimation
        target: navIcon
        property: "drawProgress"
        from: 0
        to: 1
        duration: 520
        easing.type: Easing.InOutCubic
    }
}
