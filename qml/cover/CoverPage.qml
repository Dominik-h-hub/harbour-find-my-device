import QtQuick 2.0
import Sailfish.Silica 1.0
import "../components"

// Cover: shows the own device's last known position info and offers a quick
// "update location" action.
CoverBackground {
    id: cover

    property string lastTime: ""
    property int battery: -1

    function reload() {
        Bridge.call("get_map_data", [], function (data) {
            if (!data || !data.devices) return;
            for (var i = 0; i < data.devices.length; i++) {
                if (data.devices[i].is_own) {
                    cover.lastTime = data.devices[i].timestamp_local || "";
                    cover.battery = (data.devices[i].battery === null)
                                    ? -1 : data.devices[i].battery;
                    return;
                }
            }
        });
    }

    // Render an ISO timestamp (e.g. "2026-06-26T19:09:24+02:00") as YYYY-MM-DD HH:MM Uhr.
    function formatTimestamp(iso) {
        if (!iso || iso.length < 16)
            return iso || "";
        return iso.substr(0, 10) + " " + iso.substr(11, 5) + " " + qsTr("Uhr");
    }

    Connections {
        target: Bridge
        onMapUpdated: cover.reload()
    }
    // The cover persists across minimise/restore, so Component.onCompleted runs
    // only once and mapUpdated never fires for a background GPS-daemon fix. Re-read
    // the DB whenever the app goes inactive (i.e. the cover becomes visible) so the
    // shown values are current.
    Connections {
        target: Qt.application
        onActiveChanged: if (!Qt.application.active) cover.reload()
    }
    Component.onCompleted: reload()

    Column {
        anchors {
            left: parent.left; right: parent.right
            verticalCenter: parent.verticalCenter
            margins: Theme.paddingMedium
        }
        spacing: Theme.paddingSmall

        Label {
            width: parent.width
            text: qsTr("Find My Device")
            font.pixelSize: Theme.fontSizeMedium
            color: Theme.highlightColor
            truncationMode: TruncationMode.Fade
        }
        Label {
            width: parent.width
            text: Bridge.ownLabel !== "" ? Bridge.ownLabel : Bridge.ownDeviceId
            font.pixelSize: Theme.fontSizeSmall
            color: Theme.primaryColor
            truncationMode: TruncationMode.Fade
        }
        Label {
            width: parent.width
            visible: cover.lastTime !== ""
            text: formatTimestamp(cover.lastTime)
            font.pixelSize: Theme.fontSizeExtraSmall
            color: Theme.secondaryColor
            wrapMode: Text.Wrap
        }
        Label {
            visible: cover.battery >= 0
            text: qsTr("Battery: %1%").arg(cover.battery)
            font.pixelSize: Theme.fontSizeExtraSmall
            color: Theme.secondaryColor
        }
    }

    CoverActionList {
        CoverAction {
            iconSource: "image://theme/icon-cover-refresh"
            onTriggered: Bridge.call("refresh_location", [], function () {})
        }
    }
}
