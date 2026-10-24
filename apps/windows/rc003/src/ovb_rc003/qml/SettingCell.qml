import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// .split 中的一格（.h44）：标题与说明横排、基线对齐、间距 12；动作在右，间距 4。
Item {
    id: root

    property var tokens
    property string titleText: ""
    property string descriptionText: ""
    property string titleObjectName: ""
    default property alias actions: actionHolder.data

    implicitHeight: 48

    RowLayout {
        anchors.fill: parent
        spacing: 12

        RowLayout {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            spacing: 12

            Label {
                objectName: root.titleObjectName
                text: root.titleText
                font.family: root.tokens.fontFamily
                font.pixelSize: 13
                font.weight: Font.DemiBold
                lineHeight: 18
                lineHeightMode: Text.FixedHeight
                color: root.tokens.textPrimary
                Layout.alignment: Qt.AlignBaseline
            }
            Label {
                text: root.descriptionText
                font.family: root.tokens.fontFamily
                font.pixelSize: 12
                lineHeight: 16
                lineHeightMode: Text.FixedHeight
                color: root.tokens.textTertiary
                elide: Text.ElideRight
                Layout.alignment: Qt.AlignBaseline
                Layout.fillWidth: true
                Layout.minimumWidth: 0
            }
        }

        RowLayout {
            id: actionHolder
            spacing: 4
            Layout.alignment: Qt.AlignVCenter
        }
    }
}
