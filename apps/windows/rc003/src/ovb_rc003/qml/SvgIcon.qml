import QtQuick
import QtQuick.Shapes

// 线条图标：与高保真稿 v2 的 SVG 一一对应（viewBox 20，stroke 1.5，圆头圆角）。
Item {
    id: root

    property string name: ""
    property real size: 20
    property color color: "black"
    property real strokeWidth: 1.5
    // 线条绘制进度 0..1，默认 1 为完整图标；用于切换时的描线动画
    property real drawProgress: 1

    // rect / circle 已改写为等价的 path
    readonly property var paths: ({
        "link": "M8.25 6.5H6.5a3.5 3.5 0 0 0 0 7h1.75M11.75 6.5h1.75a3.5 3.5 0 0 1 0 7h-1.75M7.25 10h5.5",
        "remote": "M10 2.5h0a3.5 3.5 0 0 1 3.5 3.5v8a3.5 3.5 0 0 1 -3.5 3.5h0a3.5 3.5 0 0 1 -3.5 -3.5v-8a3.5 3.5 0 0 1 3.5 -3.5z"
            + "M8.75 6.5a1.25 1.25 0 1 0 2.5 0a1.25 1.25 0 1 0 -2.5 0z"
            + "M8.75 13.5h2.5",
        "keys": "M5 5h10a2.5 2.5 0 0 1 2.5 2.5v5a2.5 2.5 0 0 1 -2.5 2.5h-10a2.5 2.5 0 0 1 -2.5 -2.5v-5a2.5 2.5 0 0 1 2.5 -2.5z"
            + "M6 8.5h.01M10 8.5h.01M14 8.5h.01M6.5 12h7",
        "mic": "M10 2.5a2.5 2.5 0 0 1 2.5 2.5v4a2.5 2.5 0 0 1 -5 0v-4a2.5 2.5 0 0 1 2.5 -2.5z"
            + "M4.75 9.5a5.25 5.25 0 0 0 10.5 0M10 14.75v2.75",
        "refresh": "M16 10a6 6 0 1 1 -1.76 -4.24M16 3.5v3h-3",
        "plus": "M10 4.5v11M4.5 10h11",
        "prev": "M12 5l-5 5 5 5",
        "next": "M8 5l5 5 -5 5",
        "chevron": "M6 8l4 4 4 -4",
        "chevronUp": "M6 12l4 -4 4 4",
        "close": "M5.5 5.5l9 9M14.5 5.5l-9 9",
        "min": "M5 10h10",
        "max": "M6.5 5h7a1.5 1.5 0 0 1 1.5 1.5v7a1.5 1.5 0 0 1 -1.5 1.5h-7a1.5 1.5 0 0 1 -1.5 -1.5v-7a1.5 1.5 0 0 1 1.5 -1.5z",
        "restore": "M7.5 5h6a1.5 1.5 0 0 1 1.5 1.5v6"
            + "M6.5 7.5h4.5a1.5 1.5 0 0 1 1.5 1.5v4.5a1.5 1.5 0 0 1 -1.5 1.5h-4.5a1.5 1.5 0 0 1 -1.5 -1.5v-4.5a1.5 1.5 0 0 1 1.5 -1.5z",
        "globe": "M3 10a7 7 0 1 0 14 0a7 7 0 1 0 -14 0z"
            + "M3 10h14M10 3c2 2.2 3 4.5 3 7s-1 4.8 -3 7c-2 -2.2 -3 -4.5 -3 -7s1 -4.8 3 -7z",
        "external": "M15 11v3.5a1.5 1.5 0 0 1 -1.5 1.5h-8A1.5 1.5 0 0 1 4 14.5v-8A1.5 1.5 0 0 1 5.5 5H9M12 4h4v4M16 4l-6.5 6.5",
        "check": "M5 10.5l3.2 3.2L15 7"
    })

    implicitWidth: size
    implicitHeight: size
    width: size
    height: size

    Shape {
        width: 20
        height: 20
        preferredRendererType: Shape.CurveRenderer
        transform: Scale {
            xScale: root.size / 20
            yScale: root.size / 20
        }

        ShapePath {
            strokeColor: root.color
            strokeWidth: root.strokeWidth
            fillColor: "transparent"
            capStyle: ShapePath.RoundCap
            joinStyle: ShapePath.RoundJoin
            trim.end: root.drawProgress
            PathSvg { path: root.paths[root.name] || "" }
        }
    }
}
