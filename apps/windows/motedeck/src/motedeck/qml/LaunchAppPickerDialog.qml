// "选择应用程序" 的单选版本：按键弹窗里「启动应用」那一行点「选择」时打开。
// 只能选一个程序，选中的进程交回按键弹窗；不会创建多应用布局，所以和
// MappingAppPickerDialog（多选、可一次添加多个应用）互不影响。
// SettingsController is a QML singleton - see main.qml.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import MoteDeckSettings 1.0

SettingsDialog {
    id: root
    objectName: "launchAppPickerDialog"
    title: qsTr("选择应用程序")
    titleNote: root.purpose === "close"
        ? qsTr("选中后作为「关闭应用」要关闭的程序。")
        : qsTr("选中后作为「启动应用」要打开的程序。")
    preferredWidth: 520
    closeButtonObjectName: "launchAppPickerCloseButton"
    standardButtons: Dialog.Ok | Dialog.Cancel
    // launch：给「启动应用」挑程序；close：给「关闭应用」挑要关的程序。
    property string purpose: "launch"
    property string errorText: ""
    // 这一行已经选过的程序；打开时自动勾上。它只来自这一行的动作文本，
    // 这一行改成别的动作后文本被覆盖，勾选记忆也就没了。
    property string selectedProcess: ""
    property string chosen: ""
    // Set when "OK" fails, so the dialog we re-open keeps showing why.
    property bool keepError: false
    signal applicationSelected(string process)

    function baseName(value) {
        const text = String(value || "").trim()
        if (text.length === 0)
            return ""
        const parts = text.split(/[\\/]/)
        return parts[parts.length - 1]
    }
    function isChosen(process) {
        return chosen === process
    }
    function choose(process) {
        // 一次只能选一个：点已选中的行取消，点别的行直接替换。
        chosen = chosen === process ? "" : process
    }
    function resetFromSelected() {
        chosen = ""
        const wanted = baseName(selectedProcess).toLowerCase()
        if (wanted.length === 0)
            return
        const rows = SettingsController.runningApplications
        for (let i = 0; i < rows.length; ++i) {
            const process = String(rows[i].process || "")
            if (process.toLowerCase() !== wanted)
                continue
            chosen = process
            return
        }
    }
    function pick(value) {
        const text = String(value || "").trim()
        if (text.length === 0) {
            errorText = qsTr("请选择应用程序")
            return false
        }
        errorText = ""
        root.applicationSelected(text)
        Qt.callLater(function() { root.close() })
        return true
    }
    onAboutToShow: {
        if (!keepError)
            errorText = ""
        keepError = false
        // 枚举窗口和抽图标是同步阻塞的（刚启动时首次调用最慢），放在弹窗
        // 显示之后再刷新，避免点了「启动应用」却迟迟看不到窗口。
        Qt.callLater(function() {
            SettingsController.refreshMappingApplications()
            resetFromSelected()
        })
    }
    onAccepted: {
        if (chosen.length === 0) {
            errorText = qsTr("请选择应用程序")
            keepError = true
            Qt.callLater(function() { root.open() })
            return
        }
        root.applicationSelected(chosen)
    }

    contentItem: ColumnLayout {
        spacing: root.tokens.spacingLarge
        RowLayout {
            Layout.fillWidth: true
            spacing: root.tokens.spacingSmall
            CompactTextField {
                id: processField
                objectName: "launchAppProcessField"
                tokens: root.tokens
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                maximumLength: 128
                placeholderText: qsTr("输入进程名，例如 chrome.exe")
                onAccepted: root.pick(text)
            }
            CompactButton {
                objectName: "pickLaunchAppProcessButton"
                tokens: root.tokens
                compactMinimumWidth: tokens.buttonWidth4Chars
                text: qsTr("添加")
                onClicked: root.pick(processField.text)
            }
            CompactButton {
                objectName: "browseLaunchAppButton"
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
                objectName: "refreshLaunchAppsButton"
                tokens: root.tokens
                compactMinimumWidth: tokens.buttonWidth6Chars
                text: qsTr("刷新列表")
                onClicked: SettingsController.refreshMappingApplications()
            }
        }
        ScrollView {
            id: runningList
            objectName: "launchAppList"
            Layout.fillWidth: true
            Layout.preferredHeight: 180
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ScrollBar.vertical.policy: ScrollBar.AsNeeded

            ColumnLayout {
                objectName: "launchAppListContent"
                width: runningList.availableWidth
                spacing: 2
                Repeater {
                    model: SettingsController.runningApplications
                    delegate: Rectangle {
                        id: runningRow
                        required property var modelData
                        required property int index
                        readonly property bool checked:
                            root.isChosen(modelData.process)

                        objectName: "launchAppRow_" + index
                        Layout.fillWidth: true
                        implicitHeight: 36
                        radius: root.tokens.cornerRadiusControl
                        color: checked ? root.tokens.accentSoft
                            : runningRowHover.hovered
                                ? root.tokens.buttonHover : "transparent"
                        border.width: checked
                            ? root.tokens.structuralDividerWidth : 0
                        border.color: root.tokens.accent
                        Accessible.name: modelData.name

                        HoverHandler { id: runningRowHover }
                        MouseArea {
                            anchors.fill: parent
                            onClicked: root.choose(runningRow.modelData.process)
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
                            SvgIcon {
                                Layout.alignment: Qt.AlignVCenter
                                visible: runningRow.checked
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
            objectName: "launchAppPickerMessage"
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
        objectName: "launchAppFileDialog"
        title: qsTr("选择应用程序")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("应用程序 (*.exe)")]
        onAccepted: root.pick(selectedFile.toString())
    }
}
