import QtQuick
import QtQuick.Controls

ComboBox {
    id: root

    property var tokens
    property int recommendedIndex: -1
    // Immediate-selection consumers keep currentIndex; confirmation dialogs
    // can distinguish the effective choice from a merely browsed option.
    property int effectiveIndex: currentIndex
    property bool showEffectiveMarker: false

    function decoratedText(index, rawText) {
        const value = rawText === undefined || rawText === null
            ? "" : String(rawText)
        return recommendedIndex >= 0 && index >= 0 && index === recommendedIndex
            ? qsTr("（推荐）") + " " + value
            : value
    }

    editable: false
    implicitHeight: tokens ? tokens.controlHeight : 26
    // 稿里的 .select：panel 底、line2 边框、圆角 8、左右 10、13px，右侧 16px chevron
    leftPadding: 10
    rightPadding: 10 + 16 + 8
    topPadding: 0
    bottomPadding: 0
    topInset: 0
    bottomInset: 0
    leftInset: 0
    rightInset: 0
    displayText: decoratedText(currentIndex, currentText)
    font.family: tokens ? tokens.fontFamily : "Microsoft YaHei UI"
    font.pixelSize: 13
    font.weight: Font.Normal

    contentItem: Label {
        // 稿里收起态只显示选项本身；“（推荐）”留在下拉列表与无障碍名里
        text: root.currentText
        textFormat: Text.PlainText
        color: root.enabled ? root.tokens.textPrimary : root.tokens.disabledText
        font: root.font
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    indicator: SvgIcon {
        x: root.width - width - 10
        y: (root.height - height) / 2
        size: 16
        name: "chevron"
        color: root.enabled ? root.tokens.textTertiary : root.tokens.disabledText
    }

    background: Rectangle {
        implicitHeight: 30
        radius: root.tokens.cornerRadiusControl
        color: root.hovered ? root.tokens.surfaceMuted : root.tokens.surface
        border.width: root.tokens.hairlineWidth
        border.color: root.visualFocus ? root.tokens.accent : root.tokens.borderStrong
    }

    delegate: ItemDelegate {
        objectName: root.objectName + "_option_" + index
        width: ListView.view ? ListView.view.width : root.width
        height: 28
        leftPadding: 7 + (root.showEffectiveMarker ? 18 : 0)
        rightPadding: 7
        highlighted: root.highlightedIndex === index
        IconGlyph {
            objectName: root.objectName + "_effectiveMark_" + index
            anchors.left: parent.left
            anchors.leftMargin: 7
            anchors.verticalCenter: parent.verticalCenter
            tokens: root.tokens
            glyph: "\uE73E"
            glyphSize: 12
            visible: root.showEffectiveMarker && index === root.effectiveIndex
        }
        contentItem: Label {
            text: root.decoratedText(index, root.textAt(index))
            textFormat: Text.PlainText
            color: root.tokens.textPrimary
            font.pixelSize: root.tokens.fontSizeControl
            font.weight: index === root.effectiveIndex ? Font.DemiBold : Font.Normal
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            color: parent.highlighted ? root.tokens.accentSoft : root.tokens.surface
        }
        Accessible.name: (root.showEffectiveMarker && index === root.effectiveIndex
            ? qsTr("当前使用：") : "") + contentItem.text
    }
}
