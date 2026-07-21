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
        var minLat = 90, maxLat = -90, minLon = 180, maxLon = -180, n = 0;
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
            if (d.lat < minLat) minLat = d.lat;
            if (d.lat > maxLat) maxLat = d.lat;
            if (d.lon < minLon) minLon = d.lon;
            if (d.lon > maxLon) maxLon = d.lon;
            n++;
        }
        if (n > 0) {
            // Centre on the bounding box; with several devices pick a zoom that fits
            // them all in view (with a buffer), otherwise a close single-device zoom.
            root.centerLat = (minLat + maxLat) / 2;
            root.centerLon = (minLon + maxLon) / 2;
            if (n === 1 || root.width < 100 || root.height < 100)
                root.centerZoom = n === 1 ? 20 : 13;
            else
                root.centerZoom = root.fitZoom(minLat, minLon, maxLat, maxLon,
                                               root.width, root.height);
            if (mapLoader.item && mapLoader.status === Loader.Ready)
                mapLoader.item.recenter(root.centerLat, root.centerLon, root.centerZoom);
        }
    }

    // Function to fit map zoom for multiple devices.
    function fitZoom(minLat, minLon, maxLat, maxLon, w, h) {
        var TILE = 256;
        var PAD = 64;                       // px buffer per side
        var ZOOM_MARGIN = 0.5; // adjust to 0.7 if its too close to border or 0.3 if too far away from borders.
        var usableW = Math.max(32, w - 2 * PAD);
        var usableH = Math.max(32, h - 2 * PAD);
        var latFrac = Math.abs(_latMerc(maxLat) - _latMerc(minLat)) / (2 * Math.PI);
        var lonFrac = Math.abs(maxLon - minLon) / 360;
        var EPS = 1e-9;
        var latZoom = latFrac > EPS ? Math.log(usableH / TILE / latFrac) / Math.LN2 : 20;
        var lonZoom = lonFrac > EPS ? Math.log(usableW / TILE / lonFrac) / Math.LN2 : 20;
        var z = Math.min(latZoom, lonZoom) - ZOOM_MARGIN;
        return isFinite(z) ? z : 13;
    }

    // Mercator-projected latitude (radians), used by fitZoom.
    function _latMerc(lat) {
        var s = Math.sin(lat * Math.PI / 180);
        s = Math.max(-0.9999, Math.min(0.9999, s));
        return Math.log((1 + s) / (1 - s)) / 2;
    }

    ListModel { id: deviceModel }

    function reloadMap() {
        mapLoader.active = false;
        mapLoader.active = true;
    }

    // Localize a backend GPS error code (see api.py _fix_error_code) for the status banner.
    function fixErrorText(code) {
        switch (code) {
        case "no_fix": return qsTr("No GPS fix yet — Retry at next refresh time");
        case "gps_unavailable": return qsTr("GPS not available on this device");
        case "gps_disabled": return qsTr("GPS is disabled");
        case "gps_reader_unavailable": return qsTr("GPS reader unavailable");
        case "error": return qsTr("GPS error");
        default: return qsTr("No GPS fix");
        }
    }

    Connections {
        target: Bridge
        onMapUpdated: root.reload()
        onTileProviderChanged: root.reloadMap()
        onGeoapifyKeyChanged: root.reloadMap()
        onLocationFix: {
            busyIndicator.running = false;
            if (!success)
                statusBanner.show(root.fixErrorText(message));
            else
                statusBanner.clear();
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
        // A GPS-failure banner stays until a fix succeeds (clear()); it must not
        // vanish after a few seconds while there is still no location.
        function show(msg) { bannerLabel.overrideText = msg; _sticky = true; }
        function clear() { _sticky = false; bannerLabel.overrideText = ""; }
        Label {
            id: bannerLabel
            property string overrideText: ""
            anchors.centerIn: parent
            font.pixelSize: Theme.fontSizeSmall
            color: Theme.primaryColor
            text: overrideText !== "" ? overrideText
                  : (!root.mapData.network_online ? qsTr("Internet connection not available")
                  : (!root.mapData.gps_available ? qsTr("GPS not available") : ""))
        }
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
