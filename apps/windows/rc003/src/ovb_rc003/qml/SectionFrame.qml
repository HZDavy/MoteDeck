import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 一组设置（稿里的 .sec-title + .card）：
//   标题 20 高、12px / 600 text3，后接 1px 细线，细线右侧可放 .link（titleActions）；
//   标题下 8px 是白底圆角 14 卡片，左右 padding 16。
ColumnLayout {
    id: root

    property var tokens
    property color fillColor: tokens.surface
    property int horizontalPadding: 16
    property int verticalPadding: 0
    property int contentSpacing: 0
    property string titleText: ""
    property string titleObjectName: ""
    default property alias contentData: sectionContent.data
    property alias titleActions: titleActionHolder.data

    Layout.fillWidth: true
    spacing: 8

    RowLayout {
        Layout.fillWidth: true
        Layout.preferredHeight: 20
        spacing: 8
        visible: root.titleText.length > 0

        Label {
            objectName: root.titleObjectName
            text: root.titleText
            color: root.tokens.textTertiary
            font.family: root.tokens.fontFamily
            font.pixelSize: 12
            font.weight: Font.DemiBold
            verticalAlignment: Text.AlignVCenter
            Layout.preferredHeight: 20
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignVCenter
            Layout.preferredHeight: 1
            color: root.tokens.line
        }

        RowLayout {
            id: titleActionHolder
            spacing: 4
            visible: children.length > 0
            Layout.preferredHeight: 20
        }
    }

    Rectangle {
        id: card
        Layout.fillWidth: true
        implicitHeight: sectionContent.implicitHeight + root.verticalPadding * 2
        radius: root.tokens.cornerRadiusCard
        color: root.fillColor
        border.width: root.tokens.hairlineWidth
        border.color: root.tokens.line

        ColumnLayout {
            id: sectionContent
            anchors.fill: parent
            anchors.leftMargin: root.horizontalPadding
            anchors.rightMargin: root.horizontalPadding
            anchors.topMargin: root.verticalPadding
            anchors.bottomMargin: root.verticalPadding
            spacing: root.contentSpacing
        }
    }
}
