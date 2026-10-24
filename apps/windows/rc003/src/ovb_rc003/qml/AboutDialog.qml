import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

// 关于：产品标识 + 版本，下面是作者 / 官网 / 使用说明三条外链。
SettingsDialog {
    id: root
    objectName: "aboutDialog"
    title: qsTr("关于")
    closeButtonObjectName: "closeAboutDialogButton"
    preferredWidth: 400

    component LinkRow: AbstractButton {
        id: row
        property string label: ""
        property string value: ""
        Layout.fillWidth: true
        implicitHeight: 40
        hoverEnabled: true
        // 文字与图标卡在版心线上；悬停底色用负 inset 向两侧外扩，
        // 不再靠内缩 padding 留出底色空间。
        padding: 0
        leftInset: -10
        rightInset: -10
        Accessible.name: label + " " + value

        contentItem: RowLayout {
            spacing: root.tokens.spacingSmall
            UiLabel {
                tokens: root.tokens
                kind: bodyKind
                text: row.label
                color: root.tokens.textSecondary
            }
            Item { Layout.fillWidth: true }
            UiLabel {
                tokens: root.tokens
                kind: bodyKind
                text: row.value
                color: root.tokens.textPrimary
                elide: Text.ElideRight
                Layout.minimumWidth: 0
            }
            SvgIcon {
                name: "external"
                size: 14
                color: root.tokens.textTertiary
            }
        }

        background: Rectangle {
            radius: root.tokens.cornerRadiusControl
            color: row.down || row.hovered ? root.tokens.hover : "transparent"
            Behavior on color { ColorAnimation { duration: 80 } }
            Rectangle {
                anchors.fill: parent
                anchors.margins: -2
                radius: parent.radius + 2
                color: "transparent"
                border.width: 1
                border.color: root.tokens.accent
                visible: row.visualFocus
            }
        }
    }

    contentItem: ColumnLayout {
        spacing: root.tokens.spacingLarge

        RowLayout {
            Layout.fillWidth: true
            spacing: 14

            // 与安装包 / 托盘图标一致的 liquid glass 产品标识
            Image {
                objectName: "aboutBrandIcon"
                source: SettingsController.brandIconSource
                sourceSize.width: 80
                sourceSize.height: 80
                width: 40
                height: 40
                smooth: true
                fillMode: Image.PreserveAspectFit
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                Item {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 24
                    MoteDeckWordmark {
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        markHeight: 14
                        color: root.tokens.textPrimary
                    }
                }
                UiLabel {
                    objectName: "aboutVersionLabel"
                    tokens: root.tokens
                    kind: noteKind
                    text: qsTr("版本 %1").arg(SettingsController.applicationVersion)
                }
            }
        }

        UiLabel {
            tokens: root.tokens
            kind: bodyKind
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: root.tokens.textSecondary
            text: qsTr("开源免费的蓝牙遥控器驱动，用电视遥控器控制电脑。")
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 2

            LinkRow {
                objectName: "aboutAuthorLink"
                label: qsTr("作者")
                value: "HZDavy"
                onClicked: SettingsController.openAuthorPage()
            }
            LinkRow {
                objectName: "aboutWebsiteLink"
                label: qsTr("官网")
                value: "hzdavy.github.io/MoteDeck"
                onClicked: SettingsController.openWebsite()
            }
            LinkRow {
                objectName: "aboutUsageLink"
                label: qsTr("使用方法")
                value: qsTr("查看说明")
                onClicked: SettingsController.openUsageGuide()
            }
        }
    }
}
