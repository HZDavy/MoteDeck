import QtQuick
import QtQuick.Controls

// 开关：38×22，圆角胶囊；关闭态浅灰轨 + 白色滑块，开启态墨黑轨 + 反色滑块。
Switch {
    id: root

    property var tokens

    hoverEnabled: true
    implicitWidth: 38
    implicitHeight: 22
    padding: 0
    spacing: 0

    indicator: Rectangle {
        objectName: root.objectName + "_track"
        implicitWidth: 38
        implicitHeight: 22
        width: implicitWidth
        height: implicitHeight
        x: 0
        y: (root.height - height) / 2
        radius: height / 2
        color: !root.enabled
            ? root.tokens.surfaceMuted
            : root.checked
                ? (root.hovered && root.tokens.darkMode ? "#ffffff"
                    : root.hovered ? "#2e2e2e" : root.tokens.accent)
                : root.tokens.darkMode
                    ? (root.hovered ? "#4a4a4a" : "#3a3a3a")
                    : (root.hovered ? "#c2c2c2" : "#d6d6d6")
        opacity: root.enabled ? 1 : 0.5

        Rectangle {
            objectName: root.objectName + "_thumb"
            width: 16
            height: 16
            x: root.checked ? parent.width - width - 3 : 3
            y: 3
            radius: width / 2
            color: root.checked
                ? root.tokens.accentText
                : root.tokens.darkMode ? "#999999" : "#ffffff"

            Behavior on x {
                NumberAnimation {
                    duration: 110
                    easing.type: Easing.OutCubic
                }
            }
        }

        Behavior on color {
            ColorAnimation { duration: 90 }
        }
    }
}
