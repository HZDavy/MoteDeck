import QtQuick
import QtQuick.Controls

// 稿里的 .pill：高 22，padding 0 9，12px / 500。
//   默认：inset 底、text2 字、currentColor 圆点（.7）
//   ok：ok-soft 底、ok-text 字、ok 圆点 + 3px ok-soft 光环
//   onHero：深色主卡上的 ok 变体
Rectangle {
    id: root

    property var tokens
    property string label: ""
    property bool ok: false
    property bool onHero: false
    // 兼容旧接口：错误等状态用自定义圆点色
    property color dotColor: "transparent"
    property bool quiet: false

    readonly property color textColor: onHero
        ? (ok ? "#86efac" : "#d4d4d4")
        : ok ? tokens.successText : tokens.textSecondary
    readonly property color dot: dotColor.a > 0 ? dotColor
        : onHero ? (ok ? "#4ade80" : "#d4d4d4")
            : ok ? tokens.successColor : textColor
    readonly property color softColor: onHero
        ? (ok ? Qt.rgba(74 / 255, 222 / 255, 128 / 255, 0.14) : Qt.rgba(1, 1, 1, 0.10))
        : ok ? tokens.successSoft : tokens.surfaceMuted

    implicitHeight: 22
    implicitWidth: row.implicitWidth + 18
    radius: height / 2
    color: quiet ? "transparent" : softColor

    Row {
        id: row
        anchors.centerIn: parent
        spacing: 6

        Item {
            width: 6
            height: 6
            anchors.verticalCenter: parent.verticalCenter

            Rectangle {
                visible: root.ok
                anchors.centerIn: parent
                width: 12
                height: 12
                radius: 6
                color: root.onHero ? Qt.rgba(74 / 255, 222 / 255, 128 / 255, 0.18)
                    : root.tokens.successSoft
            }
            Rectangle {
                anchors.fill: parent
                radius: 3
                color: root.dot
                opacity: root.ok || root.dotColor.a > 0 ? 1 : 0.7
            }
        }
        Label {
            text: root.label
            font.family: root.tokens.fontFamily
            font.pixelSize: 12
            font.weight: Font.Medium
            color: root.textColor
            anchors.verticalCenter: parent.verticalCenter
        }
    }
}
