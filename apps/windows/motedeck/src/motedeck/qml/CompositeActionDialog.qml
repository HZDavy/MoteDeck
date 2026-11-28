// 「组合动作」编辑弹窗：用户把多个简单动作排成一列，按键时从上到下执行，
// 例如「启动应用 → 聚焦输入框」。拖动手柄的样式与语音键配置保持一致。
// SettingsController is a QML singleton - see main.qml.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import MoteDeckSettings 1.0

SettingsDialog {
    id: root
    objectName: "compositeActionDialog"
    title: qsTr("组合动作")
    titleNote: qsTr("按键时按从上到下的顺序执行。")
    preferredWidth: 540
    closeButtonObjectName: "compositeActionCloseButton"
    standardButtons: Dialog.Ok | Dialog.Cancel

    property var steps: []
    property var catalog: []
    property string errorText: ""
    property bool keepError: false
    readonly property int maxSteps: 20
    readonly property int stepRowHeight: 64
    // 拖动排序：正在拖的行、落点行，以及行高（其它行按它让位）。
    property int dragFrom: -1
    property int dragTarget: -1
    property real dragRowHeight: 0
    // 「启动应用」步骤用三级弹窗选程序，不在这里手填路径。
    property int pendingAppIndex: -1
    // 「聚焦输入框」步骤复用聚焦输入框配置弹窗（三级弹窗）。
    property int pendingFocusIndex: -1
    readonly property var catalogLabels: {
        const labels = []
        for (let i = 0; i < catalog.length; ++i)
            labels.push(String(catalog[i].label || ""))
        return labels
    }

    signal stepsAccepted(string text)
    signal applicationPickRequested()
    signal focusConfigRequested()

    Component.onCompleted: {
        catalog = SettingsController.compositeStepCatalog()
    }

    function entryFor(kind) {
        for (let i = 0; i < catalog.length; ++i) {
            if (String(catalog[i].kind) === String(kind))
                return catalog[i]
        }
        return null
    }

    function openFor(text) {
        errorText = ""
        keepError = false
        const parsed = []
        let raw = []
        try {
            raw = JSON.parse(SettingsController.compositeStepsJson(String(text || "")))
        } catch (error) {
            raw = []
        }
        if (Array.isArray(raw)) {
            for (let i = 0; i < raw.length; ++i) {
                const item = raw[i] || {}
                const entry = entryFor(String(item.kind || ""))
                if (!entry)
                    continue
                parsed.push({
                    kind: String(entry.kind),
                    label: String(entry.label),
                    param: String(entry.param),
                    target: String(item.target || ""),
                    hotkey: Array.isArray(item.keys) ? item.keys.join("+") : "",
                    // 聚焦输入框的完整配置（方式与学习数据）原样带走。
                    focusPayload: JSON.stringify({
                        mode: String(item.focus_mode || "auto"),
                        signature: String(item.focus_signature || ""),
                        keys: Array.isArray(item.focus_keys) ? item.focus_keys : [],
                        point: Array.isArray(item.focus_point) ? item.focus_point : []
                    })
                })
            }
        }
        steps = parsed
        addCombo.currentIndex = 0
        open()
    }

    function addStep(kind) {
        if (steps.length >= maxSteps)
            return
        const entry = entryFor(kind)
        if (!entry)
            return
        const next = steps.slice()
        next.push({
            kind: String(entry.kind),
            label: String(entry.label),
            param: String(entry.param),
            target: "",
            hotkey: "",
            focusPayload: '{"mode":"auto"}'
        })
        steps = next
        errorText = ""
    }

    function removeStep(index) {
        const next = steps.slice()
        next.splice(index, 1)
        steps = next
        errorText = ""
    }

    function moveStep(index, delta) {
        const target = index + delta
        if (index < 0 || target < 0 || target >= steps.length)
            return
        const next = steps.slice()
        const moved = next.splice(index, 1)[0]
        next.splice(target, 0, moved)
        steps = next
    }

    function finishDrag() {
        const from = dragFrom
        const target = dragTarget
        dragFrom = -1
        dragTarget = -1
        dragRowHeight = 0
        if (from < 0 || target < 0 || target === from)
            return
        // 延后重建列表，避免在拖动把手自己的信号里销毁它。
        Qt.callLater(function() { root.moveStep(from, target - from) })
    }

    function updateField(index, key, value) {
        if (index < 0 || index >= steps.length)
            return
        steps[index][key] = value
        errorText = ""
    }

    function focusValue(payloadJson) {
        let data = { mode: "auto" }
        try {
            data = JSON.parse(String(payloadJson || ""))
        } catch (error) {
            data = { mode: "auto" }
        }
        const mode = String(data.mode || "auto")
        if (mode === "element")
            return qsTr("学习输入框")
        if (mode === "shortcut")
            return qsTr("快捷键 %1").arg((data.keys || []).join("+"))
        if (mode === "point") {
            const point = Array.isArray(data.point) ? data.point : [0.5, 0.5]
            return qsTr("记录位置 %1% / %2%")
                .arg(Math.round(Number(point[0]) * 100))
                .arg(Math.round(Number(point[1]) * 100))
        }
        return qsTr("自动选择")
    }

    function requestFocusConfig(index) {
        if (index < 0 || index >= steps.length)
            return
        pendingFocusIndex = index
        focusConfigRequested()
    }

    function pendingFocusPayload() {
        if (pendingFocusIndex < 0 || pendingFocusIndex >= steps.length)
            return '{"mode":"auto"}'
        return String(steps[pendingFocusIndex].focusPayload || '{"mode":"auto"}')
    }

    function setFocusConfig(payloadJson) {
        const index = pendingFocusIndex
        pendingFocusIndex = -1
        if (index < 0 || index >= steps.length)
            return
        steps[index].focusPayload = String(payloadJson || '{"mode":"auto"}')
        steps = steps.slice()
        errorText = ""
    }

    function requestApplication(index) {
        if (index < 0 || index >= steps.length)
            return
        pendingAppIndex = index
        applicationPickRequested()
    }

    function pendingApplicationTarget() {
        if (pendingAppIndex < 0 || pendingAppIndex >= steps.length)
            return ""
        return String(steps[pendingAppIndex].target || "")
    }

    function setApplicationTarget(target) {
        const index = pendingAppIndex
        pendingAppIndex = -1
        if (index < 0 || index >= steps.length)
            return
        steps[index].target = String(target || "")
        steps = steps.slice()
        errorText = ""
    }

    function commit() {
        if (steps.length === 0) {
            errorText = qsTr("请先添加至少一个动作")
            return false
        }
        const payload = []
        for (let i = 0; i < steps.length; ++i) {
            const step = steps[i]
            const item = { kind: String(step.kind) }
            if (step.kind === "focus_input") {
                let data = { mode: "auto" }
                try {
                    data = JSON.parse(String(step.focusPayload || ""))
                } catch (error) {
                    data = { mode: "auto" }
                }
                const mode = String(data.mode || "auto")
                item.focus_mode = mode
                if (mode === "element") {
                    if (String(data.signature || "").length === 0) {
                        errorText = qsTr("第 %1 步的聚焦输入框还没学习，请点「配置」").arg(i + 1)
                        return false
                    }
                    item.focus_signature = String(data.signature)
                } else if (mode === "shortcut") {
                    const keys = Array.isArray(data.keys) ? data.keys : []
                    if (keys.length === 0) {
                        errorText = qsTr("第 %1 步的聚焦输入框还没填快捷键").arg(i + 1)
                        return false
                    }
                    item.focus_keys = keys
                } else if (mode === "point") {
                    item.focus_point = Array.isArray(data.point)
                        ? data.point : [0.5, 0.5]
                }
            } else if (step.param === "application") {
                const target = String(step.target || "").trim()
                if (target.length === 0) {
                    errorText = qsTr("第 %1 步还没填要启动的程序").arg(i + 1)
                    return false
                }
                item.target = target
            } else if (step.param === "hotkey") {
                const tokens = String(step.hotkey || "").split("+")
                    .map(function(token) { return token.trim().toLowerCase() })
                    .filter(function(token) { return token.length > 0 })
                if (tokens.length === 0) {
                    errorText = qsTr("第 %1 步还没填快捷键").arg(i + 1)
                    return false
                }
                item.keys = tokens
            }
            payload.push(item)
        }
        const text = SettingsController.compositeActionTextFromJson(
            JSON.stringify(payload))
        if (text.length === 0) {
            errorText = qsTr("组合动作里有不支持的步骤，请检查后重试")
            return false
        }
        errorText = ""
        root.stepsAccepted(text)
        return true
    }

    onAboutToShow: {
        if (!keepError)
            errorText = ""
        keepError = false
        dragFrom = -1
        dragTarget = -1
        dragRowHeight = 0
        pendingAppIndex = -1
    }
    onAccepted: {
        if (!root.commit()) {
            keepError = true
            Qt.callLater(function() { root.open() })
        }
    }

    contentItem: ColumnLayout {
        spacing: root.tokens.spacingLarge

        UiLabel {
            tokens: root.tokens
            kind: noteKind
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: qsTr("可以添加多个动作，拖动每行右侧的把手调整顺序。启动应用后会自动留出窗口打开的时间。")
        }

        ScrollView {
            id: stepScroll
            objectName: "compositeStepList"
            Layout.fillWidth: true
            Layout.preferredHeight: 220
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ScrollBar.vertical.policy: ScrollBar.AsNeeded

            Item {
                id: stepHost
                width: stepScroll.availableWidth
                implicitHeight: stepColumn.implicitHeight
                height: implicitHeight

                ColumnLayout {
                    id: stepColumn
                    objectName: "compositeStepColumn"
                    width: stepHost.width
                    spacing: root.tokens.spacingSmall

                    Repeater {
                        id: stepRepeater
                        model: root.steps
                        delegate: Rectangle {
                            id: stepRow
                            required property var modelData
                            required property int index

                            property real dragOffset: 0
                            readonly property bool dragging: root.dragFrom === index
                            // 拖动时其它行实时让位，落点那一格空出来当占位符。
                            readonly property real shiftOffset: {
                                if (root.dragFrom < 0 || dragging)
                                    return 0
                                const from = root.dragFrom
                                const to = root.dragTarget
                                if (from < 0 || to < 0)
                                    return 0
                                if (from < to && index > from && index <= to)
                                    return -root.dragRowHeight
                                if (from > to && index >= to && index < from)
                                    return root.dragRowHeight
                                return 0
                            }

                            Layout.fillWidth: true
                            implicitHeight: root.stepRowHeight
                            radius: root.tokens.cornerRadiusControl
                            color: root.tokens.surface
                            z: dragging ? 1 : 0
                            transform: Translate {
                                y: stepRow.dragging
                                    ? stepRow.dragOffset : stepRow.shiftOffset
                                Behavior on y {
                                    enabled: !stepRow.dragging
                                    NumberAnimation {
                                        duration: 140
                                        easing.type: Easing.OutCubic
                                    }
                                }
                            }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 10
                                anchors.rightMargin: 8
                                spacing: root.tokens.spacingSmall

                                UiLabel {
                                    tokens: root.tokens
                                    kind: noteKind
                                    Layout.preferredWidth: 18
                                    text: String(stepRow.index + 1)
                                }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    Layout.fillHeight: false
                                    Layout.alignment: Qt.AlignVCenter
                                    spacing: 2
                                    UiLabel {
                                        tokens: root.tokens
                                        kind: bodyKind
                                        Layout.fillWidth: true
                                        text: String(stepRow.modelData.label)
                                        elide: Text.ElideRight
                                    }
                                    // 启动应用：显示已选程序，点右侧「选择」开三级弹窗。
                                    UiLabel {
                                        visible: stepRow.modelData.param === "application"
                                        tokens: root.tokens
                                        kind: noteKind
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 0
                                        elide: Text.ElideMiddle
                                        color: String(stepRow.modelData.target || "").length > 0
                                            ? root.tokens.textSecondary
                                            : root.tokens.textTertiary
                                        text: String(stepRow.modelData.target || "").length > 0
                                            ? String(stepRow.modelData.target)
                                            : qsTr("未选择应用")
                                    }
                                    // 聚焦输入框：显示当前方式，点右侧「配置」开三级弹窗。
                                    UiLabel {
                                        visible: stepRow.modelData.kind === "focus_input"
                                        tokens: root.tokens
                                        kind: noteKind
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 0
                                        elide: Text.ElideRight
                                        text: root.focusValue(
                                            stepRow.modelData.focusPayload)
                                    }
                                    // 快捷键：仍然手填（例如 ctrl+shift+a）。
                                    CompactTextField {
                                        visible: stepRow.modelData.param === "hotkey"
                                        Layout.fillWidth: true
                                        tokens: root.tokens
                                        maximumLength: 64
                                        placeholderText: qsTr("输入快捷键，例如 ctrl+shift+a")
                                        Component.onCompleted: {
                                            text = String(stepRow.modelData.hotkey || "")
                                        }
                                        onTextEdited: root.updateField(
                                            stepRow.index, "hotkey", text)
                                    }
                                }
                                CompactButton {
                                    visible: stepRow.modelData.param === "application"
                                    tokens: root.tokens
                                    flat: true
                                    compactMinimumWidth: 56
                                    text: qsTr("选择")
                                    onClicked: root.requestApplication(stepRow.index)
                                }
                                CompactButton {
                                    visible: stepRow.modelData.kind === "focus_input"
                                    tokens: root.tokens
                                    flat: true
                                    compactMinimumWidth: 56
                                    text: qsTr("配置")
                                    onClicked: root.requestFocusConfig(stepRow.index)
                                }
                                // 拖动排序把手：按住上下拖动，其它行让位，
                                // 落点露出占位符，松手后落到新位置。
                                Item {
                                    Layout.preferredWidth: 28
                                    Layout.preferredHeight: 28
                                    Layout.alignment: Qt.AlignVCenter
                                    HoverHandler {
                                        id: dragHover
                                        cursorShape: Qt.OpenHandCursor
                                    }
                                    Rectangle {
                                        anchors.fill: parent
                                        radius: root.tokens.cornerRadiusControl
                                        color: dragHover.hovered
                                            ? root.tokens.buttonHover : "transparent"
                                    }
                                    SvgIcon {
                                        anchors.centerIn: parent
                                        name: "dragHandle"
                                        size: 16
                                        strokeWidth: 2
                                        color: stepRow.dragging || dragHover.hovered
                                            ? root.tokens.textPrimary
                                            : root.tokens.textSecondary
                                    }
                                    DragHandler {
                                        id: dragHandler
                                        target: null
                                        xAxis.enabled: false
                                        cursorShape: Qt.OpenHandCursor
                                        onActiveChanged: {
                                            if (active) {
                                                root.dragFrom = stepRow.index
                                                root.dragRowHeight = stepRow.height
                                                root.dragTarget = stepRow.index
                                                stepRow.dragOffset = 0
                                                return
                                            }
                                            root.finishDrag()
                                        }
                                        onTranslationChanged: {
                                            if (!active)
                                                return
                                            stepRow.dragOffset = translation.y
                                            // 落点取中心点最近的兄弟行。
                                            const center = stepRow.y
                                                + stepRow.height / 2 + translation.y
                                            let best = -1
                                            let bestDistance = Infinity
                                            for (let i = 0; i < stepRepeater.count; ++i) {
                                                const item = stepRepeater.itemAt(i)
                                                if (!item)
                                                    continue
                                                const distance = Math.abs(
                                                    center - (item.y + item.height / 2))
                                                if (distance < bestDistance) {
                                                    bestDistance = distance
                                                    best = i
                                                }
                                            }
                                            if (best >= 0)
                                                root.dragTarget = best
                                        }
                                    }
                                }
                                CompactButton {
                                    tokens: root.tokens
                                    flat: true
                                    compactMinimumWidth: 26
                                    text: qsTr("✕")
                                    onClicked: root.removeStep(stepRow.index)
                                }
                            }

                            // 拖动时描一圈深色边框，单独一层避免被内容压住。
                            Rectangle {
                                anchors.fill: parent
                                visible: stepRow.dragging
                                radius: stepRow.radius
                                color: "transparent"
                                border.width: 1
                                border.color: root.tokens.textPrimary
                                z: 100
                            }
                        }
                    }
                }

                // 拖动落点的占位符：其它行让开的位置画一块浅色空位，
                // 松手就落在这一格，拖动过程中能马上看到结果。
                Rectangle {
                    objectName: "compositeStepDropPlaceholder"
                    visible: root.dragFrom >= 0
                    readonly property var fromRow: stepRepeater.itemAt(
                        Math.max(0, root.dragFrom))
                    readonly property var toRow: stepRepeater.itemAt(
                        Math.max(0, root.dragTarget))
                    z: -1
                    x: 0
                    width: stepColumn.width
                    height: fromRow ? fromRow.height : 0
                    radius: root.tokens.cornerRadiusControl
                    color: root.tokens.surfaceMuted
                    border.width: 1
                    border.color: root.tokens.borderStrong
                    opacity: 0.6
                    // 往下拖时落点在目标行的下方，往上拖时在目标行的上方。
                    y: {
                        if (!toRow)
                            return 0
                        if (root.dragFrom < root.dragTarget)
                            return toRow.y + toRow.height - height
                        return toRow.y
                    }
                    Behavior on y {
                        NumberAnimation {
                            duration: 140
                            easing.type: Easing.OutCubic
                        }
                    }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: root.tokens.spacingSmall
            SelectionComboBox {
                id: addCombo
                objectName: "compositeStepKindCombo"
                tokens: root.tokens
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                model: root.catalogLabels
                enabled: root.steps.length < root.maxSteps
            }
            CompactButton {
                objectName: "compositeAddStepButton"
                tokens: root.tokens
                compactMinimumWidth: root.tokens.buttonWidth4Chars
                text: qsTr("添加")
                enabled: root.steps.length < root.maxSteps
                onClicked: {
                    if (addCombo.currentIndex < 0 || addCombo.currentIndex >= root.catalog.length)
                        return
                    root.addStep(root.catalog[addCombo.currentIndex].kind)
                }
            }
            UiLabel {
                tokens: root.tokens
                kind: noteKind
                text: root.steps.length + " / " + root.maxSteps
            }
        }

        UiLabel {
            objectName: "compositeActionMessage"
            tokens: root.tokens
            kind: noteKind
            Layout.fillWidth: true
            visible: text.length > 0
            text: root.errorText
            color: root.tokens.errorColor
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
        }
    }
}
