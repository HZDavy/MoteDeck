import QtQuick
import QtQuick.Controls

// 侧栏底部的遥控器电量环：36px，绿色（低电量为红），中心显示百分比。
Item {
    id: root

    property var tokens
    property int level: -1
    readonly property bool available: level >= 0 && level <= 100
    readonly property bool low: available && level <= 20
    readonly property color progressColor: low
        ? tokens.errorColor : tokens.successColor

    implicitWidth: 36
    implicitHeight: 36
    activeFocusOnTab: false
    Accessible.role: Accessible.StaticText
    Accessible.name: available
        ? qsTr("小米遥控器2 Pro 电量 %1%").arg(level)
        : qsTr("小米遥控器2 Pro 电量不可用")

    Canvas {
        id: ring
        anchors.fill: parent
        antialiasing: true

        onPaint: {
            const ctx = getContext("2d")
            const strokeWidth = 3
            // 半径留 0.5px 余量：3px 描边的外沿若正好贴住 Canvas 边界，
            // 上下左右的最外圈会被裁掉，圆环看起来像被切平
            const radius = Math.min(width, height) / 2 - strokeWidth / 2 - 0.5
            const centerX = width / 2
            const centerY = height / 2

            ctx.reset()
            ctx.lineWidth = strokeWidth
            ctx.lineCap = "butt"

            ctx.beginPath()
            ctx.strokeStyle = root.tokens.borderStrong
            ctx.arc(centerX, centerY, radius, 0, Math.PI * 2, false)
            ctx.stroke()

            if (root.available && root.level > 0) {
                const start = -Math.PI / 2
                ctx.beginPath()
                ctx.strokeStyle = root.progressColor
                ctx.arc(centerX, centerY, radius, start,
                        start + Math.PI * 2 * root.level / 100, false)
                ctx.stroke()
            }
        }
    }

    // 数字 11/12 600 mono；“%” 8px / 500 text3，与数字同一行内联
    Text {
        anchors.centerIn: parent
        textFormat: Text.RichText
        text: root.available
            ? String(root.level) + "<span style=\"font-size:8px;font-weight:500;color:"
                + root.tokens.textTertiary + "\">%</span>"
            : "—"
        font.family: root.tokens.fontFamily
        font.letterSpacing: -0.22
        font.pixelSize: 11
        font.weight: Font.DemiBold
        color: root.available ? root.tokens.textPrimary : root.tokens.textTertiary
    }

    onLevelChanged: ring.requestPaint()
    onProgressColorChanged: ring.requestPaint()
    Component.onCompleted: ring.requestPaint()
}
