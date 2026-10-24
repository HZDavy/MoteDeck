import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 设置行（语音页用，稿里的 .h60 .row）：
//   行高 64；左侧 .lbl 标题 13/18 600 + 说明 12/16 text3（间距 2，超长省略）；
//   其后是 .pill 状态（gap 12），最右 .act 编辑控件和动作（gap 4）；行尾 1px line。
Item {
    id: root

    property var tokens
    property string titleText: ""
    property int titleFontWeight: Font.DemiBold
    property string descriptionText: ""
    property string descriptionObjectName: ""
    property bool descriptionNeverElide: false
    property string stateText: ""
    property color stateColor: tokens.textSecondary
    // 状态是否以 pill 展示（语音键行把状态写进说明里，不显示 pill）
    property bool statePillVisible: true
    readonly property bool stateOk: Qt.colorEqual(stateColor, tokens.successColor)
    readonly property bool stateNeutral: Qt.colorEqual(stateColor, tokens.textSecondary)
        || Qt.colorEqual(stateColor, tokens.disabledText)
    // 兼容旧调用
    property int titleWidth: 72
    property real editorColumnWidth: 0
    property bool editorColumnVisible: editorRow.children.length > 0
    property int stateColumnWidth: 0
    property int actionColumnWidth: 0
    property bool showDivider: true
    property alias editorData: editorRow.data
    default property alias actionData: actionRow.data

    implicitHeight: 64
    Layout.fillWidth: true

    RowLayout {
        anchors.fill: parent
        spacing: 12

        Column {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.alignment: Qt.AlignVCenter
            spacing: 2

            Label {
                id: titleLabel
                objectName: root.objectName.length > 0
                    ? root.objectName + "_titleLabel" : ""
                width: Math.min(implicitWidth, parent.width)
                text: root.titleText
                font.family: root.tokens.fontFamily
                font.pixelSize: 13
                font.weight: root.titleFontWeight
                lineHeight: 18
                lineHeightMode: Text.FixedHeight
                verticalAlignment: Text.AlignVCenter
                color: root.tokens.textPrimary
                elide: Text.ElideRight
                HoverHandler { id: titleHover }
                CompactToolTip {
                    tokens: root.tokens
                    active: titleHover.hovered && titleLabel.truncated
                    text: root.titleText
                }
            }

            Label {
                id: descriptionLabel
                objectName: root.descriptionObjectName.length > 0
                    ? root.descriptionObjectName
                    : root.objectName.length > 0
                        ? root.objectName + "_descriptionLabel" : ""
                visible: root.descriptionText.length > 0
                width: parent.width
                text: root.descriptionText
                font.family: root.tokens.fontFamily
                font.pixelSize: 12
                lineHeight: 16
                lineHeightMode: Text.FixedHeight
                verticalAlignment: Text.AlignVCenter
                color: root.tokens.textTertiary
                wrapMode: Text.NoWrap
                elide: root.descriptionNeverElide ? Text.ElideNone : Text.ElideRight
                HoverHandler { id: descriptionHover }
                CompactToolTip {
                    tokens: root.tokens
                    active: descriptionHover.hovered && descriptionLabel.truncated
                    text: root.descriptionText
                }
            }
        }

        StatusPill {
            objectName: root.objectName.length > 0
                ? root.objectName + "_stateLabel" : ""
            property string text: root.stateText
            visible: root.statePillVisible && root.stateText.length > 0
            tokens: root.tokens
            label: root.stateText
            ok: root.stateOk
            dotColor: root.stateOk || root.stateNeutral ? "transparent" : root.stateColor
            Layout.alignment: Qt.AlignVCenter
        }

        RowLayout {
            spacing: 4
            Layout.alignment: Qt.AlignRight | Qt.AlignVCenter

            RowLayout {
                id: editorRow
                objectName: root.objectName.length > 0
                    ? root.objectName + "_editorColumn" : ""
                visible: root.editorColumnVisible
                spacing: 4
                Layout.alignment: Qt.AlignVCenter
            }

            RowLayout {
                id: actionRow
                objectName: root.objectName.length > 0
                    ? root.objectName + "_actionColumn" : ""
                visible: children.length > 0
                spacing: 4
                Layout.alignment: Qt.AlignVCenter
            }
        }
    }

    SettingsRowDivider {
        objectName: root.objectName + "_divider"
        tokens: root.tokens
        visible: root.showDivider
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
    }
}
