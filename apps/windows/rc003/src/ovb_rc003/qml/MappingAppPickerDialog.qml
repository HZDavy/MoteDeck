// "选择应用程序" dialog: pick from the running windows, type a process name, or
// browse for an .exe. The button page's "+" chip opens it; picking an
// application gives it its own button layout, mirroring Logi Options+.
// SettingsController is a QML singleton - see main.qml.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import OvbRc003Settings 1.0

SettingsDialog {
    id: root
    objectName: "mappingAppPickerDialog"
    title: qsTr("选择应用程序")
    titleNote: qsTr("为常用应用程序自定义按键，提高效率。")
    preferredWidth: 520
    closeButtonObjectName: "mappingAppPickerCloseButton"
    standardButtons: Dialog.Ok | Dialog.Cancel
    property string errorText: ""
    property var checked: ({})
    // Set when "OK" fails, so the dialog we re-open keeps showing why.
    property bool keepError: false

    function isChecked(process) {
        return checked[process] === true
    }
    function toggle(process) {
        const next = {}
        for (const key in checked)
            if (checked[key] && key !== process) next[key] = true
        if (!checked[process]) next[process] = true
        checked = next
    }
    function runningName(process) {
        const rows = SettingsController.runningApplications
        for (let i = 0; i < rows.length; ++i)
            if (rows[i].process === process) return rows[i].name
        return ""
    }
    function addChecked() {
        let failure = ""
        for (const process in checked) {
            if (!checked[process]) continue
            const result = SettingsController.addMappingAppProfile(
                process, runningName(process))
            if (!result.ok) failure = result.message
        }
        if (failure.length > 0) {
            errorText = failure
            return false
        }
        checked = ({})
        SettingsController.refreshMappingApplications()
        return true
    }
    function addOne(value) {
        const result = SettingsController.addMappingAppProfile(value, "")
        errorText = result.ok ? "" : result.message
        if (result.ok)
            SettingsController.refreshMappingApplications()
        return result.ok
    }
    onAboutToShow: {
        checked = ({})
        // A failed "OK" re-opens this dialog; keep its message visible
        // instead of wiping it before the user can read it.
        if (!keepError)
            errorText = ""
        keepError = false
        SettingsController.refreshMappingApplications()
    }
    onAccepted: {
        if (!addChecked()) {
            keepError = true
            Qt.callLater(function() { root.open() })
        }
    }

    contentItem: ColumnLayout {
        spacing: root.tokens.spacingLarge
        RowLayout {
            Layout.fillWidth: true
            spacing: root.tokens.spacingSmall
            CompactTextField {
                id: processField
                objectName: "mappingAppProcessField"
                tokens: root.tokens
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                maximumLength: 128
                placeholderText: qsTr("输入进程名，例如 chrome.exe")
                onAccepted: root.addOne(text)
            }
            CompactButton {
                objectName: "addMappingAppProcessButton"
                tokens: root.tokens
                compactMinimumWidth: tokens.buttonWidth4Chars
                text: qsTr("添加")
                onClicked: {
                    if (root.addOne(processField.text))
                        processField.text = ""
                }
            }
            CompactButton {
                objectName: "browseMappingAppButton"
                tokens: root.tokens
                compactMinimumWidth: tokens.buttonWidth6Chars
                text: qsTr("浏览…")
                onClicked: appFileDialog.open()
            }
        }
        RowLayout {
            Layout.fillWidth: true
            UiLabel {
                tokens: root.tokens
                kind: bodyKind
                Layout.fillWidth: true
                text: qsTr("正在运行的应用程序")
            }
            CompactButton {
                objectName: "refreshRunningAppsButton"
                tokens: root.tokens
                compactMinimumWidth: tokens.buttonWidth6Chars
                text: qsTr("刷新列表")
                onClicked: SettingsController.refreshMappingApplications()
            }
        }
        ScrollView {
            id: runningList
            objectName: "runningAppList"
            Layout.fillWidth: true
            Layout.preferredHeight: 180
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ScrollBar.vertical.policy: ScrollBar.AsNeeded

            ColumnLayout {
                objectName: "runningAppListContent"
                width: runningList.availableWidth
                spacing: 2
                Repeater {
                    model: SettingsController.runningApplications
                    delegate: Rectangle {
                        id: runningRow
                        required property var modelData
                        required property int index
                        readonly property bool added: modelData.selected
                        readonly property bool checked:
                            root.isChecked(modelData.process)

                        objectName: "runningAppRow_" + index
                        Layout.fillWidth: true
                        implicitHeight: 36
                        radius: root.tokens.cornerRadiusControl
                        opacity: added ? 0.55 : 1
                        color: checked && !added ? root.tokens.accentSoft
                            : runningRowHover.hovered && !added
                                ? root.tokens.buttonHover : "transparent"
                        border.width: checked && !added
                            ? root.tokens.structuralDividerWidth : 0
                        border.color: root.tokens.accent
                        Accessible.name: modelData.name

                        HoverHandler { id: runningRowHover }
                        MouseArea {
                            anchors.fill: parent
                            enabled: !runningRow.added
                            onClicked: root.toggle(runningRow.modelData.process)
                        }
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 8
                            anchors.rightMargin: 8
                            spacing: root.tokens.spacingSmall
                            Item {
                                Layout.preferredWidth: 20
                                Layout.preferredHeight: 20
                                Layout.alignment: Qt.AlignVCenter
                                Image {
                                    anchors.fill: parent
                                    visible: runningRow.modelData.icon.length > 0
                                    source: runningRow.modelData.icon
                                    sourceSize.width: 20
                                    sourceSize.height: 20
                                    fillMode: Image.PreserveAspectFit
                                    smooth: true
                                }
                                Label {
                                    anchors.centerIn: parent
                                    visible: runningRow.modelData.icon.length === 0
                                    text: runningRow.modelData.name.length > 0
                                        ? runningRow.modelData.name.charAt(0) : "?"
                                    font.family: root.tokens.fontFamily
                                    font.pixelSize: root.tokens.fontSizeControl
                                    color: root.tokens.textSecondary
                                }
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                                spacing: 0
                                UiLabel {
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    tokens: root.tokens
                                    kind: bodyKind
                                    text: runningRow.modelData.name
                                    elide: Text.ElideRight
                                }
                                UiLabel {
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    tokens: root.tokens
                                    kind: noteKind
                                    text: runningRow.modelData.process
                                    elide: Text.ElideRight
                                }
                            }
                            UiLabel {
                                Layout.alignment: Qt.AlignVCenter
                                visible: runningRow.added
                                tokens: root.tokens
                                kind: noteKind
                                text: qsTr("已添加")
                            }
                            SvgIcon {
                                Layout.alignment: Qt.AlignVCenter
                                visible: runningRow.checked && !runningRow.added
                                name: "check"
                                size: 16
                                color: root.tokens.accent
                            }
                        }
                    }
                }
            }
        }
        UiLabel {
            objectName: "mappingAppPickerMessage"
            tokens: root.tokens
            kind: noteKind
            Layout.fillWidth: true
            visible: text.length > 0
            text: root.errorText
            textFormat: Text.PlainText
            wrapMode: Text.WrapAnywhere
        }
    }

    FileDialog {
        id: appFileDialog
        objectName: "mappingAppFileDialog"
        title: qsTr("选择应用程序")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("应用程序 (*.exe)")]
        onAccepted: root.addOne(selectedFile.toString())
    }
}
