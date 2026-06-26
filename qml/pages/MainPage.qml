import QtQuick 2.0
import Sailfish.Silica 1.0
import "../components"

Page {
    id: mainPage
    allowedOrientations: Orientation.All

    property int currentIndex: tabView.currentIndex
    readonly property var tabTitles: [ qsTr("Map"), qsTr("Devices") ]

    Column {
        anchors.fill: parent

        // --- tab bar -------------------------------------------------------
        Item {
            id: tabBar
            width: parent.width
            height: Theme.itemSizeMedium
            z: 1

            Row {
                anchors.fill: parent
                Repeater {
                    model: mainPage.tabTitles
                    delegate: Item {
                        width: tabBar.width / 2
                        height: tabBar.height
                        Label {
                            anchors.centerIn: parent
                            text: modelData
                            color: index === mainPage.currentIndex
                                   ? Theme.highlightColor : Theme.primaryColor
                            font.pixelSize: Theme.fontSizeLarge
                        }
                        Rectangle {
                            anchors.bottom: parent.bottom
                            width: parent.width
                            height: Theme.paddingSmall / 2
                            color: Theme.highlightColor
                            visible: index === mainPage.currentIndex
                        }
                        MouseArea {
                            anchors.fill: parent
                            onClicked: tabView.positionViewAtIndex(index, ListView.SnapPosition)
                        }
                    }
                }
            }
        }

        // --- swipeable content --------------------------------------------
        ListView {
            id: tabView
            width: parent.width
            height: parent.height - tabBar.height
            clip: true
            orientation: ListView.Horizontal
            snapMode: ListView.SnapOneItem
            highlightRangeMode: ListView.StrictlyEnforceRange
            boundsBehavior: Flickable.StopAtBounds
            cacheBuffer: width * 2
            model: 2

            delegate: Loader {
                width: tabView.width
                height: tabView.height
                sourceComponent: index === 0 ? mapComponent : devicesComponent
            }
        }
    }

    Component { id: mapComponent; MapView { } }
    Component { id: devicesComponent; DevicesView { } }
}
