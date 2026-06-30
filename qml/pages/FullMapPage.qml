import QtQuick 2.0
import Sailfish.Silica 1.0
import "../components"
// Full-screen interactive map. Reached by tapping the static map on the Map tab
// (Geoapify only). There is deliberately NO PullDownMenu here, so every gesture
// belongs to the map (pan/pinch/fling). The map also swallows the page-stack
// edge-swipe, hence the explicit back button.
Page {
    id: page
    allowedOrientations: Orientation.All

    property var markerModel: null
    property real startLat: 0
    property real startLon: 0
    property real startZoom: 13

    Loader {
        id: mapLoader
        anchors.fill: parent
        source: Qt.resolvedUrl("MapCanvas.qml")
        onStatusChanged: {
            if (status === Loader.Ready && item) {
                item.allowGestures = true;
                item.markerModel = page.markerModel;
                item.recenter(page.startLat, page.startLon, page.startZoom);
            }
        }
    }

    // back button:
    IconButton {
        anchors {
            top: parent.top
            left: parent.left
            margins: Theme.paddingMedium
        }
        icon.source: "image://theme/icon-m-back"
        onClicked: pageStack.pop()

        Rectangle {
            anchors.centerIn: parent
            width: parent.width + Theme.paddingSmall
            height: width
            z: -1
            radius: width / 2
            color: Theme.rgba(Theme.overlayBackgroundColor, 0.6)
        }
    }

    Label {
        anchors { bottom: parent.bottom; right: parent.right; margins: Theme.paddingSmall }
        visible: mapLoader.status === Loader.Ready
        text: "© OpenStreetMap contributors"
        font.pixelSize: Theme.fontSizeTiny
        color: Theme.highlightColor
        style: Text.Outline
        styleColor: Theme.overlayBackgroundColor
    }

    // Fallback if the QtLocation module is missing on the device.
    Label {
        anchors.centerIn: parent
        width: parent.width - 2 * Theme.horizontalPageMargin
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.Wrap
        visible: mapLoader.status === Loader.Error
        text: qsTr("Map module not available")
        color: Theme.highlightColor
        font.pixelSize: Theme.fontSizeLarge
    }
}