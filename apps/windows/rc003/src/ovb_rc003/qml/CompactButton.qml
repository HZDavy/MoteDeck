import QtQuick
import QtQuick.Controls

// 三种按钮外观，与高保真稿 v2 一致（高 30，圆角 8，13px，内部间距 6）：
//   flat        —— .btn-t 透明底，悬停浅底，padding 0 8，text2
//   outlined    —— .btn2 白底 line2 描边，padding 0 12，500
//   highlighted —— .btn1 墨黑实心，padding 0 14，600
// inverse：放在深色主卡上（btn-t 文字 #d4d4d4；btn2 #f2f2f2 底 #141414 字）
Button {
    id: root

    property var tokens
    property int compactMinimumWidth: 0
    property bool subduedText: false
    property bool inverse: false
    // 尾随图标（稿里 btn-t 后的 external / chevron），14px
    property string trailingIcon: ""

    readonly property int sidePadding: flat ? 8 : highlighted ? 14 : 12
    readonly property color textColor: !enabled
        ? tokens.textTertiary
        : inverse
            ? (highlighted || !flat ? "#141414" : "#d4d4d4")
            : highlighted
                ? tokens.accentText
                : flat
                    ? tokens.textSecondary
                    : subduedText ? tokens.textTertiary : tokens.textPrimary

    hoverEnabled: true
    implicitHeight: tokens ? tokens.buttonHeight : 30
    implicitWidth: Math.max(compactMinimumWidth,
        contentItem.implicitWidth + leftPadding + rightPadding)
    leftPadding: sidePadding
    rightPadding: sidePadding
    topPadding: 0
    bottomPadding: 0
    font.family: tokens ? tokens.fontFamily : "Segoe UI"
    font.pixelSize: tokens ? tokens.fontSizeBody : 13
    font.weight: highlighted ? Font.DemiBold : flat ? Font.Normal : Font.Medium

    contentItem: Item {
        implicitWidth: buttonLabel.implicitWidth
            + (trailing.visible ? trailing.width + contentRow.spacing : 0)
        implicitHeight: contentRow.implicitHeight

        Row {
            id: contentRow
            anchors.centerIn: parent
            spacing: 6

            Label {
                id: buttonLabel
                text: root.text
                color: root.textColor
                font: root.font
                // 只依赖按钮可用宽度，避免与 Row 宽度互相绑定导致宽度为 0
                width: Math.max(0, Math.min(implicitWidth, root.availableWidth
                    - (trailing.visible ? trailing.width + contentRow.spacing : 0)))
                elide: Text.ElideRight
                anchors.verticalCenter: parent.verticalCenter
            }
            SvgIcon {
                id: trailing
                visible: root.trailingIcon.length > 0
                name: root.trailingIcon
                size: 14
                color: root.inverse ? "#a3a3a3" : root.tokens.textTertiary
                anchors.verticalCenter: parent.verticalCenter
            }
        }
    }

    background: Rectangle {
        radius: root.tokens.cornerRadiusControl
        color: root.inverse
            ? (root.flat
                ? (root.down || root.hovered ? Qt.rgba(1, 1, 1, 0.08) : "transparent")
                : (root.down ? "#e8e8e8" : root.hovered ? "#ffffff" : "#f2f2f2"))
            : root.flat
                ? (root.down || root.hovered ? root.tokens.hover : "transparent")
                : root.highlighted
                    ? (root.down ? "#000000" : root.tokens.accent)
                    : (root.down || root.hovered ? root.tokens.surfaceMuted : root.tokens.surface)
        border.width: root.flat || root.highlighted ? 0 : root.tokens.hairlineWidth
        border.color: root.inverse ? "#f2f2f2" : root.tokens.borderStrong
        opacity: root.enabled ? 1 : 0.55
        Behavior on color { ColorAnimation { duration: 80 } }

        // 键盘/遥控器导航的焦点环，鼠标点击时不显示
        Rectangle {
            anchors.fill: parent
            anchors.margins: -3
            radius: parent.radius + 3
            color: "transparent"
            border.width: 1
            border.color: root.tokens.accent
            visible: root.visualFocus
        }
    }
}
