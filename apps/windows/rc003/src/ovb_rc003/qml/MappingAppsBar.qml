import QtQuick
import QtQuick.Controls
import OvbRc003Settings 1.0

// 按键映射页顶部的应用栏：只显示图标，悬停才出名称；
// 左右各一个按钮，每次移动一格（4 格可视）。
Item {
    id: root

    property var tokens
    property var chips: []
    property bool interactive: true
    signal addRequested()
    signal removeRequested(string profileId, string name)
    // 拖动排序：两个下标都是「应用区」里的位置（全局不参与）。
    signal moveRequested(int fromIndex, int toIndex)
    // 拖动中的实时位置：dragFrom 是被拖的那一格，dragTo 是松手后落点。
    property int dragFrom: -1
    property int dragTo: -1

    readonly property real slotSize: 32
    readonly property real slotGap: 4
    readonly property real step: slotSize + slotGap
    readonly property int visibleSlots: 4
    readonly property int appCount: Math.max(0, chips.length - 1)
    readonly property int firstVisibleIndex:
        Math.round(track.contentX / step)
    // 超过 4 个应用才需要左右移动按钮
    readonly property bool hasOverflow: appCount > visibleSlots
    // 可视轨道宽度：应用少于 4 个时按实际个数收缩（0 个时只剩全局 + 加号）
    readonly property real trackWidth: appCount > 0
        ? Math.min(appCount, visibleSlots) * slotSize
            + (Math.min(appCount, visibleSlots) - 1) * slotGap + 10
        : 0

    implicitWidth: addButton.x + addButton.width
    implicitHeight: 32

    // 悬停提示出现在对应图标正下方（稿子 .tip：left = 图标中心、top = 图标底部 + 8）
    function tipFor(name, item) {
        tipLabel.text = name
        const tipWidth = tipLabel.implicitWidth
        if (item) {
            const p = item.mapToItem(root, item.width / 2, item.height)
            tipPopup.x = Math.round(p.x - tipWidth / 2)
            tipPopup.y = Math.round(p.y + 8)
        } else {
            tipPopup.x = Math.round((root.width - tipWidth) / 2)
            tipPopup.y = root.height + 8
        }
        tipPopup.open()
    }

    // 全局配置：地球图标，固定在最左侧
    Rectangle {
        id: globalButton
        objectName: "mappingChip_global"
        width: root.slotSize
        height: root.slotSize
        radius: 9
        anchors.verticalCenter: parent.verticalCenter
        color: SettingsController.mappingEditTarget === ""
            ? root.tokens.surface : globalHover.hovered ? root.tokens.hover : "transparent"
        border.width: 1
        border.color: SettingsController.mappingEditTarget === ""
            ? root.tokens.borderStrong : "transparent"
        Accessible.name: qsTr("全局")

        Rectangle {
            visible: SettingsController.mappingEditTarget === ""
            z: -1
            y: 1
            width: parent.width
            height: parent.height
            radius: parent.radius
            color: Qt.rgba(0, 0, 0, 0.08)
        }

        TapHandler {
            onTapped: SettingsController.selectMappingEditTarget("")
        }
        SvgIcon {
            anchors.centerIn: parent
            name: "globe"
            size: 18
            color: root.tokens.textPrimary
        }

        HoverHandler {
            id: globalHover
            onHoveredChanged: {
                if (hovered)
                    root.tipFor(qsTr("全局"), globalButton)
                else
                    tipPopup.close()
            }
        }
    }

    IconButton {
        id: navPrev
        objectName: "mappingAppsPrev"
        anchors.left: globalButton.right
        anchors.leftMargin: root.hasOverflow ? root.slotGap + 6 : 0
        anchors.verticalCenter: parent.verticalCenter
        width: root.hasOverflow ? implicitWidth : 0
        visible: root.hasOverflow
        tokens: root.tokens
        compact: true
        iconName: "prev"
        enabled: root.interactive && !track.atXBeginning
        Accessible.name: qsTr("向左移动一格")
        onClicked: track.stepBy(-1)
    }

    Item {
        id: trackHost
        anchors.left: navPrev.right
        // 稿子 .apps-track：宽 150，padding 6/5 + 负 margin，给移除角标留出位置
        anchors.leftMargin: root.slotGap - 5
        anchors.verticalCenter: parent.verticalCenter
        width: root.trackWidth
        height: root.slotSize + 12
        visible: root.appCount > 0
        clip: true

        // 落点占位符：拖动时右边让出来的空位画一个浅色块，松手就落在这里。
        Rectangle {
            objectName: "mappingAppsDropPlaceholder"
            visible: root.dragFrom >= 0
            width: root.slotSize
            height: root.slotSize
            y: 6
            x: 5 + root.dragTo * root.step - track.contentX
            radius: 9
            color: root.tokens.surfaceMuted
            border.width: 1
            border.color: root.tokens.borderStrong
            opacity: 0.55
            Behavior on x {
                NumberAnimation { duration: 140; easing.type: Easing.OutCubic }
            }
        }

        ListView {
            id: track
            objectName: "mappingAppsTrack"
            anchors.fill: parent
            anchors.leftMargin: 5
            anchors.rightMargin: 5
            anchors.topMargin: 6
            anchors.bottomMargin: 6
            orientation: ListView.Horizontal
            spacing: root.slotGap
            model: root.appCount
            boundsBehavior: Flickable.StopAtBounds
            flickableDirection: Flickable.HorizontalFlick
            interactive: false
            clip: false

            function stepBy(delta) {
                const target = Math.max(0, Math.min(Math.max(0, root.appCount - root.visibleSlots),
                    firstIndex + delta))
                contentX = target * root.step
            }
            readonly property int firstIndex: Math.round(contentX / root.step)
            // 应用减少时把滚动位置收回到范围内
            onCountChanged: {
                const maxIndex = Math.max(0, root.appCount - root.visibleSlots)
                if (Math.round(contentX / root.step) > maxIndex)
                    contentX = maxIndex * root.step
            }
            Behavior on contentX {
                NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
            }

            delegate: Rectangle {
                id: appCell
                required property int index
                readonly property var profile: root.chips[index + 1]
                readonly property bool active:
                    profile && SettingsController.mappingEditTarget === profile.id
                // 插件自带的条目（OpenMexia）：图标自带黑色底，且不能删。
                readonly property bool isPlugin: !!(profile && profile.plugin)
                property bool dragging: false
                property real dragX: 0
                // 拖动时其它图标往两边让位，落点空出来当占位符。
                readonly property real shiftX: {
                    if (root.dragFrom < 0 || dragging)
                        return 0
                    if (root.dragFrom < index && root.dragTo >= index)
                        return -root.step
                    if (root.dragFrom > index && root.dragTo <= index)
                        return root.step
                    return 0
                }

                objectName: "mappingChip_" + index
                width: root.slotSize
                height: root.slotSize
                radius: 9
                color: active ? root.tokens.surface
                    : cellHover.hovered ? root.tokens.hover : "transparent"
                border.width: 1
                border.color: active ? root.tokens.borderStrong : "transparent"
                Accessible.name: profile ? profile.name : ""
                z: dragging ? 10 : 0
                transform: Translate {
                    x: appCell.dragging ? appCell.dragX : appCell.shiftX
                    Behavior on x {
                        enabled: !appCell.dragging
                        NumberAnimation { duration: 140; easing.type: Easing.OutCubic }
                    }
                }

                // .app.on 阴影：0 1px 2px rgba(0,0,0,.08)
                Rectangle {
                    visible: appCell.active
                    z: -1
                    x: 0
                    y: 1
                    width: parent.width
                    height: parent.height
                    radius: parent.radius
                    color: Qt.rgba(0, 0, 0, 0.08)
                }

                HoverHandler { id: cellHover }
                TapHandler {
                    onTapped: {
                        if (appCell.profile)
                            SettingsController.selectMappingEditTarget(appCell.profile.id)
                    }
                }

                // 应用图标：优先使用系统取到的 exe 图标
                Image {
                    objectName: "mappingAppIcon_" + appCell.index
                    anchors.centerIn: parent
                    width: 20
                    height: 20
                    source: appCell.profile ? appCell.profile.icon : ""
                    visible: !appCell.isPlugin && source.toString().length > 0
                    sourceSize.width: 40
                    sourceSize.height: 40
                    fillMode: Image.PreserveAspectFit
                    smooth: true
                }
                // 插件自带的图标不绿底：黑底白色星标，和 OpenMexia 的图标一致。
                Rectangle {
                    objectName: "mappingAppPluginIcon_" + appCell.index
                    anchors.centerIn: parent
                    visible: appCell.isPlugin
                    width: 20
                    height: 20
                    radius: 5
                    color: "#111111"

                    SvgIcon {
                        anchors.centerIn: parent
                        name: "openmexia"
                        size: 14
                        strokeWidth: 1.8
                        color: "#ffffff"
                    }
                }
                Rectangle {
                    anchors.centerIn: parent
                    visible: !appCell.isPlugin
                        && (!appCell.profile || appCell.profile.icon.length === 0)
                    width: 20
                    height: 20
                    radius: 5
                    color: root.tokens.surfaceMuted
                    Label {
                        anchors.centerIn: parent
                        text: appCell.profile && appCell.profile.name.length > 0
                            ? appCell.profile.name.charAt(0) : "?"
                        font.family: root.tokens.fontFamily
                        font.pixelSize: root.tokens.fontSizeSmall
                        color: root.tokens.textSecondary
                    }
                }

                // 移除：右上角圆形 ×，悬停才出现
                Rectangle {
                    objectName: appCell.profile
                        ? "removeMappingAppButton_" + appCell.index : ""
                    width: 14
                    height: 14
                    radius: 7
                    x: parent.width - width + 5
                    y: -5
                    z: 2
                    color: root.tokens.textPrimary
                    opacity: appCell.profile && cellHover.hovered ? 1 : 0
                    visible: opacity > 0
                    Behavior on opacity { NumberAnimation { duration: 120 } }

                    SvgIcon {
                        anchors.centerIn: parent
                        name: "close"
                        size: 10
                        strokeWidth: 2.4
                        color: root.tokens.surface
                    }
                    TapHandler {
                        onTapped: {
                            if (!appCell.profile)
                                return
                            root.removeRequested(appCell.profile.id, appCell.profile.name)
                        }
                    }
                }

                // 拖动排序：按住图标左右拖，松手后换到落点那一格。
                DragHandler {
                    id: cellDrag
                    target: null
                    enabled: root.interactive && !!appCell.profile
                    yAxis.enabled: false
                    cursorShape: Qt.ClosedHandCursor
                    onActiveChanged: {
                        if (active) {
                            appCell.dragging = true
                            appCell.dragX = 0
                            root.dragFrom = appCell.index
                            root.dragTo = appCell.index
                            tipPopup.close()
                            return
                        }
                        const from = appCell.index
                        const target = root.dragTo
                        appCell.dragging = false
                        appCell.dragX = 0
                        root.dragFrom = -1
                        root.dragTo = -1
                        if (target >= 0 && target !== from)
                            root.moveRequested(from, target)
                    }
                    onTranslationChanged: {
                        if (!active)
                            return
                        appCell.dragX = translation.x
                        root.dragTo = Math.max(0, Math.min(
                            root.appCount - 1,
                            appCell.index + Math.round(appCell.dragX / root.step)))
                    }
                }

                HoverHandler {
                    onHoveredChanged: {
                        if (hovered && appCell.profile)
                            root.tipFor(appCell.profile.name, appCell)
                        else
                            tipPopup.close()
                    }
                }
            }

            onContentXChanged: root.updateBorders()
        }
    }

    IconButton {
        id: navNext
        objectName: "mappingAppsNext"
        anchors.left: trackHost.right
        anchors.leftMargin: root.slotGap - 5
        anchors.verticalCenter: parent.verticalCenter
        width: root.hasOverflow ? implicitWidth : 0
        visible: root.hasOverflow
        tokens: root.tokens
        compact: true
        iconName: "next"
        enabled: root.interactive && root.hasOverflow
            && track.firstIndex < root.appCount - root.visibleSlots
        Accessible.name: qsTr("向右移动一格")
        onClicked: track.stepBy(1)
    }

    IconButton {
        id: addButton
        objectName: "addMappingAppButton"
        anchors.left: navNext.right
        anchors.leftMargin: root.slotGap
        anchors.verticalCenter: parent.verticalCenter
        tokens: root.tokens
        compact: true
        iconName: "plus"
        text: "+"
        enabled: root.interactive
        Accessible.name: qsTr("添加应用程序")
        onClicked: root.addRequested()
        HoverHandler {
            onHoveredChanged: {
                if (hovered)
                    root.tipFor(qsTr("添加应用"), addButton)
                else
                    tipPopup.close()
            }
        }
    }

    function updateBorders() {
        // 让按钮的可用状态跟随滚动位置（atXBeginning / atXEnd）
        navPrev.enabled = root.interactive && root.hasOverflow && track.firstIndex > 0
        navNext.enabled = root.interactive && root.hasOverflow
            && track.firstIndex < Math.max(0, root.appCount - root.visibleSlots)
    }

    Popup {
        id: tipPopup
        objectName: "mappingAppToolTip"
        parent: root
        padding: 0
        leftPadding: 0
        rightPadding: 0
        topPadding: 0
        bottomPadding: 0
        leftInset: 0
        rightInset: 0
        topInset: 0
        bottomInset: 0
        // 尺寸贴合文字：内容 8+文字+8，高 4+16+4
        width: tipLabel.implicitWidth
        height: 24
        closePolicy: Popup.NoAutoClose
        background: Rectangle {
            anchors.fill: parent
            radius: 6
            color: root.tokens.darkMode ? "#f2f2f2" : "#1f1f1f"

            Rectangle {
                width: 8
                height: 8
                x: (parent.width - width) / 2
                y: -3
                rotation: 45
                color: parent.color
            }
        }
        contentItem: Label {
            id: tipLabel
            topPadding: 4
            bottomPadding: 4
            leftPadding: 8
            rightPadding: 8
            font.family: root.tokens.fontFamily
            font.pixelSize: 12
            color: root.tokens.darkMode ? "#141414" : "#ffffff"
        }
    }
}
