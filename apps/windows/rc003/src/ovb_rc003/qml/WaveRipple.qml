import QtQuick

// 语音输入电平：4px 像素格阵列，逐项照搬设计稿 v2/index.html 的 canvas 脚本。
// 绿色亮格是"中心向两侧扩散"的波形（ripple + noise 动画），按实时 level 缩放；
// 灰色底格自下而上渐隐；左右两侧按 8% 渐隐（对应稿子里的 mask）。
// level 为 0 时只画灰色格子，动画也停下。
Canvas {
    id: root
    property var tokens
    property real level: 0
    readonly property bool darkMode: tokens ? tokens.darkMode : false

    antialiasing: false
    renderTarget: Canvas.FramebufferObject

    readonly property int cell: 4
    readonly property int gap: 2
    readonly property int step: cell + gap
    property int columns: 0
    property int rows: 0
    // 动画时间（秒），对应稿子里的 now / 1000。
    property real seconds: 0

    function refit() {
        columns = Math.floor((width + gap) / step)
        rows = Math.max(1, Math.floor((height + gap) / step))
    }

    function greenColor(t) {
        return Qt.hsla((142 - t * 14) / 360, (62 + t * 26) / 100, (28 + t * 34) / 100, 1)
    }

    function edgeMask(x, width) {
        const fade = Math.max(1, width * 0.08)
        const left = Math.min(1, x / fade)
        const right = Math.min(1, (width - x) / fade)
        return Math.max(0, Math.min(left, right))
    }

    onPaint: {
        const ctx = getContext("2d")
        if (columns === 0 || rows === 0) refit()
        ctx.reset()
        if (columns === 0) return
        const width = root.width
        const height = root.height
        const originX = (width - (columns * step - gap)) / 2
        const originY = height - (rows * step - gap)
        const mid = (columns - 1) / 2
        const grey = darkMode ? Qt.rgba(1, 1, 1, 0.16) : Qt.rgba(0, 0, 0, 0.10)
        const level = Math.max(0, Math.min(1, root.level))
        const s = seconds
        for (let i = 0; i < columns; i++) {
            const mask = edgeMask(originX + i * step + cell / 2, width)
            if (mask <= 0) continue
            const d = mid > 0 ? Math.abs(i - mid) / mid : 0
            let lit = 0
            if (level > 0) {
                const ripple = Math.sin(d * 14 - s * 5) * 0.5 + 0.5
                const noise = Math.sin(i * 1.7 + s * 9) * 0.5 + 0.5
                const env = Math.pow(1 - d, 1.4)
                lit = Math.round(rows * Math.min(1, env * (0.35 + ripple * 0.5 + noise * 0.25) * level))
            }
            for (let j = 0; j < rows; j++) {
                const on = j < lit
                const t = j / Math.max(1, rows - 1)
                ctx.fillStyle = on ? greenColor(t) : grey
                ctx.globalAlpha = (on ? 0.55 + t * 0.45 : Math.pow(1 - t, 1.2)) * mask
                ctx.fillRect(originX + i * step, originY + (rows - 1 - j) * step, cell, cell)
            }
        }
        ctx.globalAlpha = 1
    }

    // 有电平时按帧推进波形；电平回到 0 就停，不空转。
    FrameAnimation {
        running: root.visible && root.level > 0
        onTriggered: {
            root.seconds += frameTime
            root.requestPaint()
        }
    }

    onWidthChanged: { refit(); requestPaint() }
    onHeightChanged: { refit(); requestPaint() }
    onLevelChanged: if (level === 0) requestPaint()
    onDarkModeChanged: requestPaint()
    Component.onCompleted: { refit(); requestPaint() }
}
