import QtQuick 2.0
import Sailfish.Silica 1.0
import "../components"

Page {
    id: mainPage
    allowedOrientations: Orientation.Portrait

    property int currentIndex: tabView.currentIndex
    readonly property var tabTitles: [ qsTr("Map"), qsTr("Devices") ]

    // First-start hint that remote features need the background service
    // package (degraded mode). Shown once until dismissed; disappears on its
    // own as soon as the daemon heartbeats (checked every 10 s while shown).
    property bool daemonBannerVisible: false

    function checkDaemonBanner() {
        Bridge.call("get_daemon_status", [], function (s) {
            daemonBannerVisible = !!(s && s.banner_needed);
        });
    }

    Component.onCompleted: if (Bridge.ready) checkDaemonBanner()
    Connections {
        target: Bridge
        onReadyChanged: if (Bridge.ready) mainPage.checkDaemonBanner()
    }
    Timer {
        interval: 10000
        repeat: true
        running: mainPage.daemonBannerVisible
        onTriggered: mainPage.checkDaemonBanner()
    }

    Column {
        anchors.fill: parent

        // --- background-service hint banner --------------------------------
        Rectangle {
            id: daemonBanner
            width: parent.width
            height: mainPage.daemonBannerVisible
                    ? bannerRow.implicitHeight + Theme.paddingMedium : 0
            visible: mainPage.daemonBannerVisible
            color: Theme.rgba(Theme.highlightDimmerColor, 0.9)
            z: 2

            Row {
                id: bannerRow
                anchors.verticalCenter: parent.verticalCenter
                x: Theme.horizontalPageMargin
                width: parent.width - x - Theme.paddingSmall
                spacing: Theme.paddingSmall

                Label {
                    width: parent.width - bannerClose.width - Theme.paddingSmall
                    anchors.verticalCenter: parent.verticalCenter
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSizeExtraSmall
                    color: Theme.primaryColor
                    text: qsTr("Remote commands and background tracking need "
                             + "the background service. Tap to set it up in "
                             + "the Settings.")
                    MouseArea {
                        anchors.fill: parent
                        onClicked: pageStack.push(Qt.resolvedUrl("SettingsPage.qml"))
                    }
                }
                IconButton {
                    id: bannerClose
                    anchors.verticalCenter: parent.verticalCenter
                    icon.source: "image://theme/icon-m-clear"
                    onClicked: {
                        mainPage.daemonBannerVisible = false;
                        Bridge.call("dismiss_daemon_banner", [], function () {});
                    }
                }
            }
        }

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
                    - (mainPage.daemonBannerVisible ? daemonBanner.height : 0)
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

    Component { id: mapComponent; MapView { activeTab: mainPage.currentIndex === 0 } }
    Component { id: devicesComponent; DevicesView { activeTab: mainPage.currentIndex === 1 } }
}
