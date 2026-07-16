import QtQuick 2.0
import Sailfish.Silica 1.0
import QtLocation 5.0
import QtPositioning 5.2
import "../components"

// The actual QtLocation map.
Map {
    id: map
    anchors.fill: parent

    property bool useGeoapify: Bridge.tileProvider === "geoapify"
                               && Bridge.geoapifyKey !== ""

    property bool allowGestures: false
    gesture.enabled: allowGestures && useGeoapify
    zoomLevel: 20                    // street level, so the road is visible
    center: QtPositioning.coordinate(0, 0)

    property var markerModel: null

    // recenter on a position; optional zoom overrides the default (e.g. a wider
    // view when several devices must fit). Clamped to the map's supported range.
    function recenter(lat, lon, zoom) {
        map.center = QtPositioning.coordinate(lat, lon);
        if (zoom !== undefined)
            map.zoomLevel = Math.max(map.minimumZoomLevel,
                                     Math.min(map.maximumZoomLevel, zoom));
    }

    // The osm plugin exposes the custom tile source as a CustomMap map type; it
    // is only fetched once it is the active type. Select it when using Geoapify.
    function applyMapType() {
        if (!useGeoapify)
            return;
        for (var i = 0; i < map.supportedMapTypes.length; i++) {
            if (map.supportedMapTypes[i].style === MapType.CustomMap) {
                map.activeMapType = map.supportedMapTypes[i];
                return;
            }
        }
    }

    Component.onCompleted: applyMapType()
    onSupportedMapTypesChanged: applyMapType()

    plugin: Plugin {
        name: "osm"
        PluginParameter { name: "osm.useragent"; value: Bridge.osmUserAgent }
        PluginParameter { name: "osm.mapping.providersrepository.disabled"; value: true }
        PluginParameter {
            name: "osm.mapping.custom.host"
            value: "https://maps.geoapify.com/v1/tile/osm-bright/%z/%x/%y.png?apiKey="
                   + Bridge.geoapifyKey
        }
    }

    MapItemView {
        model: map.markerModel
        delegate: MapQuickItem {
            coordinate: QtPositioning.coordinate(lat, lon)
            anchorPoint.x: marker.width / 2
            anchorPoint.y: marker.height
            sourceItem: Column {
                id: marker
                spacing: 0
                Rectangle {
                    anchors.horizontalCenter: parent.horizontalCenter
                    color: isOwn ? Theme.highlightColor : Theme.secondaryHighlightColor
                    radius: Theme.paddingSmall
                    width: pinLabel.width + Theme.paddingMedium
                    height: pinLabel.height + Theme.paddingSmall
                    Label {
                        id: pinLabel
                        anchors.centerIn: parent
                        text: label + (battery >= 0 ? "  " + battery + "%" : "")
                        font.pixelSize: Theme.fontSizeTiny
                        color: Theme.primaryColor
                    }
                }
                Rectangle {
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: Theme.paddingSmall
                    height: Theme.paddingSmall
                    radius: width / 2
                    color: isOwn ? Theme.highlightColor : Theme.secondaryHighlightColor
                }
            }
        }
    }
}
