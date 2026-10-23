import QtQuick 2.0
import Sailfish.Silica 1.0
import "../components"

// Main page with two swipeable tabs (Map, Devices). The tab bar is pinned to
// the bottom of the page so the labels stay clear of the device notch; the
// tab contents keep a Theme.itemSizeSmall gap at the top for the same reason
// (same layout as harbour-ferry).
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

    // --- swipeable content ------------------------------------------------
    ListView {
        id: tabView
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: daemonBanner.top
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

    // --- background-service hint banner (above the tab bar) ---------------
    Rectangle {
        id: daemonBanner
        anchors.bottom: tabDivider.top
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

    // Thin divider so content scrolling up to the bar stays readable.
    Rectangle {
        id: tabDivider
        anchors.bottom: tabBar.top
        width: parent.width
        height: 1
        z: 1
        color: Theme.secondaryColor
        opacity: 0.4
    }

    // --- tab bar ----------------------------------------------------------
    Row {
        id: tabBar
        anchors.bottom: parent.bottom
        width: parent.width
        height: Theme.itemSizeMedium
        z: 1

        Repeater {
            model: mainPage.tabTitles

            delegate: BackgroundItem {
                width: tabBar.width / 2
                height: tabBar.height
                onClicked: tabView.positionViewAtIndex(index, ListView.SnapPosition)

                Label {
                    anchors.centerIn: parent
                    text: modelData
                    font.pixelSize: Theme.fontSizeLarge
                    color: mainPage.currentIndex === index ? Theme.highlightColor
                                                           : Theme.secondaryColor
                }

                Rectangle {
                    anchors.top: parent.top
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: parent.width - 2 * Theme.paddingLarge
                    height: Math.round(Theme.paddingSmall / 2) + 1
                    radius: height / 2
                    color: Theme.highlightColor
                    visible: mainPage.currentIndex === index
                }
            }
        }
    }

    Component { id: mapComponent; MapView { activeTab: mainPage.currentIndex === 0 } }
    Component { id: devicesComponent; DevicesView { activeTab: mainPage.currentIndex === 1 } }
}
