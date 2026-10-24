import QtQuick
import QtQuick.Controls

// 标题栏右上角的窗口按钮（最小化 / 最大化 / 关闭）：40×32，圆角 8，16px 线条图标。
AbstractButton {
    id: root
    property var tokens
    property string kind: "minimize"

    implicitWidth: 40
    implicitHeight: 32
    hoverEnabled: true
    padding: 0
    leftInset: 0
    rightInset: 0
    topInset: 0
    bottomInset: 0

    readonly property bool isClose: kind === "close"

    background: Rectangle {
        radius: 8
        color: root.isClose && root.hovered
            ? "#c42b1c"
            : root.pressed ? root.tokens.borderStrong
                : root.hovered ? root.tokens.hover
                    : "transparent"
        Behavior on color { ColorAnimation { duration: 80 } }
    }

    contentItem: Item {
        implicitWidth: 40
        implicitHeight: 32

        SvgIcon {
            anchors.centerIn: parent
            size: 16
            name: root.kind === "minimize" ? "min"
                : root.kind === "maximize" ? "max"
                    : root.kind === "restore" ? "restore" : "close"
            color: root.isClose && root.hovered ? "#ffffff" : root.tokens.textSecondary
        }
    }
}
