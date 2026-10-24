import QtQuick
import QtCore

// 设计变量：与高保真稿（v2/styles.css）的 CSS 变量一一对应。
// 只保留黑白灰；绿色/红色只用于电量、状态灯和语音电平。
QtObject {
    id: tokens

    property SystemPalette palette: SystemPalette {
        colorGroup: SystemPalette.Active
    }
    readonly property bool systemDark:
        palette.window.r * 0.299
        + palette.window.g * 0.587
        + palette.window.b * 0.114 < 0.5

    // 外观：0 跟随系统，1 浅色，2 深色；本地持久化
    property Settings uiSettings: Settings {
        category: "appearance"
        location: StandardPaths.writableLocation(StandardPaths.AppConfigLocation) + "/ui.ini"
        property int themeMode: 0
    }
    readonly property int themeMode: uiSettings.themeMode
    function setThemeMode(mode) { uiSettings.themeMode = mode }
    readonly property bool darkMode: themeMode === 2 || (themeMode === 0 && systemDark)

    // ── 颜色 ──────────────────────────────────────────────
    property color background: darkMode ? "#0f0f0f" : "#f5f5f5"   // --bg
    property color surface: darkMode ? "#181818" : "#ffffff"      // --panel
    property color surfaceMuted: darkMode ? "#212121" : "#f0f0f0" // --inset
    property color fieldBackground: darkMode ? "#212121" : "#f0f0f0"
    property color sidebar: darkMode ? "#181818" : "#ffffff"
    property color line: darkMode ? Qt.rgba(1, 1, 1, 0.07) : "#e6e6e6"       // --line
    property color border: line
    property color borderStrong: darkMode ? Qt.rgba(1, 1, 1, 0.14) : "#d4d4d4" // --line2
    property color cardBorder: line
    property color textPrimary: darkMode ? "#f2f2f2" : "#141414"
    property color textSecondary: darkMode ? "#b3b3b3" : "#525252"
    property color textTertiary: darkMode ? "#949494" : "#6b6b6b"
    property color disabledText: textTertiary
    property color hover: darkMode ? Qt.rgba(1, 1, 1, 0.06) : Qt.rgba(0, 0, 0, 0.05)
    property color accent: darkMode ? "#f2f2f2" : "#141414"      // --ink
    property color accentText: darkMode ? "#111111" : "#ffffff"  // --on-accent
    property color accentSoft: darkMode ? "#242424" : "#f0f0f0"  // --sel-bg
    property color accentLine: darkMode ? Qt.rgba(1, 1, 1, 0.35) : "#bdbdbd"
    property color wire: darkMode ? Qt.rgba(1, 1, 1, 0.18) : "#c8c8c8"
    property color halo: darkMode ? Qt.rgba(1, 1, 1, 0.10) : Qt.rgba(0, 0, 0, 0.08)
    property color successColor: darkMode ? "#4ade80" : "#16a34a"
    property color successText: darkMode ? "#6ee7a0" : "#15803d"
    property color successSoft: darkMode ? Qt.rgba(0.29, 0.87, 0.5, 0.12)
                                            : Qt.rgba(0.086, 0.64, 0.29, 0.10)
    property color errorColor: darkMode ? "#ff6764" : "#c42b1c"
    property color errorBackground: darkMode ? "#422421" : "#fdeceb"
    property color voiceAccent: darkMode ? "#4ade80" : "#16a34a"

    // 兼容旧页面的别名
    property color windowFrame: background
    property color windowFrameBorder: line
    property color nativeWindowBorder: darkMode ? "#4a5059" : "#a8adb4"
    property color nativeCaptionColor: sidebar
    property color nativeCaptionTextColor: textPrimary
    property color buttonBackground: surfaceMuted
    property color buttonHover: hover
    property color buttonText: textPrimary
    property color remotePhotoShadowColor: darkMode ? "#99000000" : "#40000000"
    property int remotePhotoShadowRadius: 22
    property int remotePhotoShadowOffset: 12

    // ── 字体 ──────────────────────────────────────────────
    readonly property bool hasSegoeVariable:
        Qt.platform.os === "windows"
        && Qt.fontFamilies().indexOf("Segoe UI Variable Text") >= 0
    readonly property bool hasSegoeVariableDisplay:
        Qt.platform.os === "windows"
        && Qt.fontFamilies().indexOf("Segoe UI Variable Display") >= 0
    property string fontFamily: hasSegoeVariable ? "Segoe UI Variable Text" : "Segoe UI"
    property string fontFamilyDisplay:
        hasSegoeVariableDisplay ? "Segoe UI Variable Display" : fontFamily
    property string fontFamilyMono: "Cascadia Mono"
    property real fontSizeTiny: 10
    property real fontSizeMapGesture: 10
    property real fontSizeSmall: 11
    property real fontSizeMappingKey: 12
    property real fontSizeControl: 12
    property int fontSizeBody: 13
    property int fontSizeSection: 14
    property int fontSizeTitle: 15
    property int fontSizePageTitle: 28
    property int fontSizeHeroTitle: 24

    // ── 尺寸 ──────────────────────────────────────────────
    property int cornerRadiusCard: 14
    property int cornerRadiusLarge: 14
    property int cornerRadiusControl: 8
    property int cornerRadiusSmall: 8
    property int windowClientRadius: 8
    property int windowFrameGap: 0
    property real hairlineWidth: 1
    property int structuralDividerWidth: 1
    property int spacingTiny: 4
    property int spacingSmall: 6
    property int spacingMedium: 12
    property int spacingLarge: 16

    // ── 弹窗版心 ──────────────────────────────────────────
    // 所有弹窗的标题、内容行、底部按钮左右都卡在 dialogPadding 这条线上；
    // 标题行高 dialogTitleHeight，上方留 dialogTopPadding，
    // 标题与内容之间留 dialogHeaderGap，内容与底部按钮之间留 dialogFooterGap。
    property int dialogPadding: 24
    property int dialogTopPadding: 20
    property int dialogTitleHeight: 28
    property int dialogHeaderGap: 16
    property int dialogFooterGap: 20
    // 关闭按钮是 28px 的点击区，× 字形只有约 12px；向外让出这段距离，
    // 让 × 的视觉右边缘落在版心线上，而不是点击区右边缘。
    property int dialogCloseButtonOutset: 8

    property int titleBarHeight: 72
    property int navigationWidth: 72
    property int navigationItemHeight: 56
    property int controlHeight: 30
    property int buttonHeight: controlHeight
    property int buttonWidth2Chars: 48
    property int buttonWidth4Chars: 76
    property int buttonWidth6Chars: 96
    property int buttonWidth9Chars: 124
    property int statusBarMinHeight: 0
    property int pageMaxWidth: 920
    property int pageHorizontalPadding: 20
    property int pageRightPadding: 28
    property int pageVerticalPadding: 0
    property int pageBottomPadding: 24
    property int sectionVerticalPadding: 10
}
