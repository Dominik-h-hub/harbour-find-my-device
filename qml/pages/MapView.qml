import QtQuick 2.0
import Sailfish.Silica 1.0
import "../components"
// NOTE: QtLocation/QtPositioning are intentionally NOT imported here. The map is
// in MapCanvas.qml and loaded via a Loader, so a missing QtLocation module on the
// device degrades gracefully (fallback below) instead of breaking the whole app.

SilicaFlickable {
    id: root
    anchors.fill: parent
    contentHeight: height

    property var mapData: ({ devices: [], network_online: true, gps_available: true })
    property bool mapReady: false

    // True while this is the visible tab. Re-query the DB when the user swipes
    // here so background GPS-daemon fixes show without a manual refresh.
    property bool activeTab: true
    onActiveTabChanged: if (activeTab) reload()

    // Interactive exploration is only meaningful with Geoapify (OSM stays static
    // to limit tile requests). Mirrors MapCanvas.useGeoapify.
    property bool canExplore: Bridge.tileProvider === "geoapify" && Bridge.geoapifyKey !== ""

    // Last computed view, handed to FullMapPage so it opens where the tab is.
    property real centerLat: 0
    property real centerLon: 0
    property real centerZoom: 13

    PullDownMenu {
        MenuItem {
            text: qsTr("Settings")
            onClicked: pageStack.push(Qt.resolvedUrl("SettingsPage.qml"))
        }
        MenuItem {
            text: qsTr("Update map")
            onClicked: root.refreshLocation()
        }
    }

    function reload() {
        Bridge.call("get_map_data", [], function (data) {
            if (!data) return;
            root.mapData = data;
            root.rebuildMarkers();
            root.mapReady = true;
        });
    }

    function refreshLocation() {
        busyIndicator.running = true;
        Bridge.call("refresh_location", [], function () { /* result via signals */ });
    }

    function rebuildMarkers() {
        deviceModel.clear();
        var devs = root.mapData.devices || [];
        var sumLat = 0, sumLon = 0, n = 0;
        for (var i = 0; i < devs.length; i++) {
            var d = devs[i];
            deviceModel.append({
                deviceId: d.device_id,
                label: d.label,
                lat: d.lat,
                lon: d.lon,
                timestampLocal: d.timestamp_local || "",
                battery: d.battery === null ? -1 : d.battery,
                isOwn: d.is_own ? 1 : 0
            });
            sumLat += d.lat; sumLon += d.lon; n++;
        }
        if (n > 0) {
            root.centerLat = sumLat / n;
            root.centerLon = sumLon / n;
            root.centerZoom = n === 1 ? 20 : 13;
            if (mapLoader.item && mapLoader.status === Loader.Ready)
                mapLoader.item.recenter(root.centerLat, root.centerLon, root.centerZoom);
        }
    }

    ListModel { id: deviceModel }

    function reloadMap() {
        mapLoader.active = false;
        mapLoader.active = true;
    }

    Connections {
        target: Bridge
        onMapUpdated: root.reload()
        onTileProviderChanged: root.reloadMap()
        onGeoapifyKeyChanged: root.reloadMap()
        onLocationFix: {
            busyIndicator.running = false;
            if (!success)
                statusBanner.show(message ? message : qsTr("No GPS fix"));
        }
    }

    Component.onCompleted: reload()

    // --- the map (static; isolated in MapCanvas.qml) ----------------------
    Loader {
        id: mapLoader
        anchors.fill: parent
        source: Qt.resolvedUrl("MapCanvas.qml")
        onStatusChanged: {
            if (status === Loader.Ready && item) {
                item.markerModel = deviceModel;
                root.rebuildMarkers();
            }
        }
    }

    MouseArea {
        anchors.fill: mapLoader
        enabled: root.canExplore && mapLoader.status === Loader.Ready
        onClicked: pageStack.push(Qt.resolvedUrl("FullMapPage.qml"), {
            markerModel: deviceModel,
            startLat: root.centerLat,
            startLon: root.centerLon,
            startZoom: root.centerZoom
        })
    }

    Label {
        anchors {
            bottom: parent.bottom
            horizontalCenter: parent.horizontalCenter
            bottomMargin: Theme.paddingLarge
        }
        visible: root.canExplore && mapLoader.status === Loader.Ready
        text: qsTr("Tap the map to explore")
        font.pixelSize: Theme.fontSizeTiny
        color: Theme.primaryColor
        style: Text.Outline
        styleColor: Theme.overlayBackgroundColor
    }

    // Fallback shown when the QtLocation module is not installed on the device.
    Column {
        anchors.centerIn: parent
        width: parent.width - 2 * Theme.horizontalPageMargin
        spacing: Theme.paddingMedium
        visible: mapLoader.status === Loader.Error
        Label {
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.Wrap
            text: qsTr("Map module not available")
            color: Theme.highlightColor
            font.pixelSize: Theme.fontSizeLarge
        }
        Label {
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.Wrap
            text: qsTr("Install QtLocation on the device to show the map. "
                     + "Device positions are still listed on the Devices tab.")
            color: Theme.secondaryColor
            font.pixelSize: Theme.fontSizeSmall
        }
    }

    // --- attribution (visible when the map is up) --------------------------
    Label {
        anchors { bottom: parent.bottom; right: parent.right; margins: Theme.paddingSmall }
        visible: mapLoader.status === Loader.Ready
        text: "© OpenStreetMap contributors"
        font.pixelSize: Theme.fontSizeTiny
        color: Theme.highlightColor
        style: Text.Outline
        styleColor: Theme.overlayBackgroundColor
    }

    // --- status banner (offline / no GPS) ---------------------------------
    Rectangle {
        id: statusBanner
        anchors { top: parent.top; left: parent.left; right: parent.right }
        height: bannerLabel.height + Theme.paddingMedium
        color: Theme.rgba(Theme.highlightDimmerColor, 0.9)
        visible: !root.mapData.network_online || !root.mapData.gps_available || statusBanner._sticky
        property bool _sticky: false
        function show(msg) { bannerLabel.overrideText = msg; _sticky = true; bannerTimer.restart(); }
        Label {
            id: bannerLabel
            property string overrideText: ""
            anchors.centerIn: parent
            font.pixelSize: Theme.fontSizeSmall
            color: Theme.primaryColor
            text: overrideText !== "" ? overrideText
                  : (!root.mapData.network_online ? qsTr("Network offline")
                  : (!root.mapData.gps_available ? qsTr("GPS not available") : ""))
        }
        Timer { id: bannerTimer; interval: 4000; onTriggered: { statusBanner._sticky = false; bannerLabel.overrideText = ""; } }
    }

    BusyIndicator {
        id: busyIndicator
        anchors.centerIn: parent
        size: BusyIndicatorSize.Large
        running: false
    }

    ViewPlaceholder {
        enabled: root.mapReady && deviceModel.count === 0
                 && mapLoader.status !== Loader.Error
        text: qsTr("No location yet")
        hintText: qsTr("Pull down and tap 'Update map'")
    }
}
