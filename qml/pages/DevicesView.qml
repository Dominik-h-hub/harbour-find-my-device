import QtQuick 2.0
import Sailfish.Silica 1.0
import "../components"

// Devices tab: lists all devices with their remote-action buttons. The own
// device exposes only RING (and cannot be edited/removed). Remote devices expose
// LOCK / RING / CAMERA / DELETE plus edit & unpair via the long-press menu.
SilicaListView {
    id: list
    anchors.fill: parent

    model: ListModel { id: deviceModel }

    header: PageHeader { title: qsTr("Devices") }

    property bool activeTab: true
    onActiveTabChanged: if (activeTab) reload()

    PullDownMenu {
        MenuItem {
            text: qsTr("Settings")
            onClicked: pageStack.push(Qt.resolvedUrl("SettingsPage.qml"))
        }
        MenuItem {
            text: qsTr("Add device")
            onClicked: pageStack.push(Qt.resolvedUrl("AddDevicePage.qml"))
        }
        MenuItem {
            text: qsTr("Refresh")
            onClicked: list.reload()
        }
    }

    function reload() {
        Bridge.call("list_devices", [], function (devs) {
            deviceModel.clear();
            if (!devs) return;
            for (var i = 0; i < devs.length; i++) {
                var d = devs[i];
                var fix = d.last_fix;
                deviceModel.append({
                    deviceId: d.device_id,
                    label: d.label,
                    isOwn: d.is_own ? 1 : 0,
                    hasPin: d.has_pin ? 1 : 0,
                    authFailed: d.auth_failed ? 1 : 0,
                    noResponse: d.no_response ? 1 : 0,
                    ringing: d.ringing ? 1 : 0,
                    lastAuthResult: d.last_auth_result,
                    actionsEnabled: d.actions_enabled ? 1 : 0,
                    cameraEnabled: d.camera_enabled ? 1 : 0,
                    ringEnabled: d.ring_enabled ? 1 : 0,
                    lastTime: fix ? (fix.timestamp_local || fix.timestamp_utc || "") : "",
                    battery: (fix && fix.battery_level !== null && fix.battery_level !== undefined)
                             ? fix.battery_level : -1
                });
            }
        });
    }

    // Render an ISO timestamp (e.g. "2026-06-26T19:09:24+02:00") as YYYY-MM-DD HH:MM Uhr.
    function formatTimestamp(iso) {
        if (!iso || iso.length < 16)
            return iso || "";
        return iso.substr(0, 10) + " " + iso.substr(11, 5) + " " + qsTr("Uhr");
    }

    function sendCommand(deviceId, cmd, arg) {
        Bridge.call("send_command", [deviceId, cmd, arg || ""], function (res) {
            if (res && !res.ok)
                feedback.show(qsTr("Could not send %1: %2").arg(cmd).arg(res.error));
            else
                feedback.show(qsTr("%1 sent").arg(cmd));
        });
    }

    Connections {
        target: Bridge
        onDevicesUpdated: list.reload()
        onCommandResult: {
            var txt = result === "ok" ? qsTr("%1 acknowledged").arg(cmd)
                    : result === "auth_failed" ? qsTr("%1 failed: wrong PIN").arg(cmd)
                    : result === "disabled" ? qsTr("%1 disabled on target").arg(cmd)
                    : result === "timeout" ? qsTr("%1: no response").arg(cmd)
                    : qsTr("%1: %2").arg(cmd).arg(result);
            feedback.show(txt);
        }
    }

    Component.onCompleted: reload()

    delegate: ListItem {
        id: item
        contentHeight: frame.height + 2 * Theme.paddingSmall
        // Own device: edit the label only. Remote devices: full menu.
        menu: (isOwn === 1) ? ownMenuComponent : contextMenuComponent

        Rectangle {
            id: frame
            x: Theme.horizontalPageMargin
            y: Theme.paddingSmall
            width: parent.width - 2 * Theme.horizontalPageMargin
            height: contentColumn.height + 2 * Theme.paddingMedium
            radius: Theme.paddingMedium
            color: item.highlighted ? Theme.rgba(Theme.highlightColor, 0.1)
                                    : Theme.rgba(Theme.primaryColor, 0.06)
            border.width: 1
            border.color: Theme.rgba(Theme.highlightColor, 0.8)

            Column {
                id: contentColumn
                x: Theme.paddingMedium
                y: Theme.paddingMedium
                width: parent.width - 2 * Theme.paddingMedium
                spacing: Theme.paddingSmall

                Label {
                    text: label + (isOwn === 1 ? "  " + qsTr("(this device)") : "")
                    color: item.highlighted ? Theme.highlightColor : Theme.primaryColor
                    font.pixelSize: Theme.fontSizeMedium
                }
                Label {
                    width: parent.width
                    text: {
                        var parts = [];
                        if (lastTime !== "")
                            parts.push(qsTr("Last GPS FIX: %1").arg(list.formatTimestamp(lastTime)));
                        if (battery >= 0) parts.push(battery + "%");
                        if (isOwn === 0 && hasPin === 0) parts.push(qsTr("no PIN set"));
                        if (authFailed === 1) parts.push(qsTr("wrong PIN"));
                        if (noResponse === 1) parts.push(qsTr("no response (check device id)"));
                        return parts.length ? parts.join("  ·  ") : qsTr("no data yet");
                    }
                    // Red when something is wrong (wrong PIN / no response)
                    color: (authFailed === 1 || noResponse === 1)
                           ? Theme.errorColor : Theme.secondaryColor
                    font.pixelSize: Theme.fontSizeExtraSmall
                    wrapMode: Text.Wrap
                }

                // --- action buttons --------------------------------------
                Label {
                    text: qsTr("Commands")
                    font.pixelSize: Theme.fontSizeExtraSmall
                    color: Theme.highlightColor
                }

                Row {
                    width: parent.width
                    spacing: Theme.paddingSmall
                    property real btnWidth: (width - spacing * 3) / 4

                    CommandButton {
                        width: parent.btnWidth
                        // While ringing the button becomes a red STOP that sends
                        // STOP_RING; otherwise it starts a RING.
                        text: ringing === 1 ? qsTr("STOP") : qsTr("RING")
                        active: ringing === 1
                        btnEnabled: ringEnabled === 1
                        onActivated: ringing === 1
                            ? list.sendCommand(deviceId, "STOP_RING", "")
                            : list.sendCommand(deviceId, "RING", "")
                    }
                    CommandButton {
                        width: parent.btnWidth
                        text: qsTr("LOCK")
                        visible: isOwn === 0
                        btnEnabled: actionsEnabled === 1
                        onActivated: list.sendCommand(deviceId, "LOCK", "")
                    }
                    CommandButton {
                        width: parent.btnWidth
                        text: qsTr("CAMERA")
                        visible: isOwn === 0
                        btnEnabled: actionsEnabled === 1
                        // Default to the back camera; front is in the long-press menu.
                        onActivated: list.sendCommand(deviceId, "CAMERA", "back")
                    }
                    CommandButton {
                        width: parent.btnWidth
                        text: qsTr("DELETE")
                        visible: isOwn === 0
                        btnEnabled: actionsEnabled === 1
                        // Confirm on a dedicated page before sending.
                        onActivated: pageStack.push(
                            Qt.resolvedUrl("ConfirmDeletePage.qml"),
                            { deviceId: deviceId, deviceLabel: label })
                    }
                }
            }
        }

        RemorseItem { id: remorse }

        Component {
            id: ownMenuComponent
            ContextMenu {
                MenuItem {
                    text: qsTr("Edit")
                    // Own device: id is read-only, no PIN field (managed in Settings).
                    onClicked: pageStack.push(Qt.resolvedUrl("AddDevicePage.qml"),
                        { editMode: true, isOwn: true, deviceId: deviceId, deviceLabel: label })
                }
            }
        }

        Component {
            id: contextMenuComponent
            ContextMenu {
                MenuItem {
                    text: qsTr("Photo (front camera)")
                    enabled: actionsEnabled === 1
                    onClicked: list.sendCommand(deviceId, "CAMERA", "front")
                }
                MenuItem {
                    text: qsTr("Edit")
                    onClicked: pageStack.push(Qt.resolvedUrl("AddDevicePage.qml"),
                        { editMode: true, deviceId: deviceId, deviceLabel: label })
                }
                MenuItem {
                    text: qsTr("Unpair device")
                    onClicked: {
                        var b = Bridge;
                        var id = deviceId;
                        remorse.execute(item, qsTr("Unpairing"),
                            function () { b.call("remove_device", [id], function () {}); });
                    }
                }
            }
        }
    }

    ViewPlaceholder {
        enabled: deviceModel.count === 0
        text: qsTr("No devices")
        hintText: qsTr("Pull down to add a device")
    }

    VerticalScrollDecorator {}

    // Simple in-app toast for command feedback (avoids system notifications here).
    Rectangle {
        id: feedback
        anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
        height: feedbackLabel.height + Theme.paddingLarge
        color: Theme.rgba(Theme.highlightDimmerColor, 0.95)
        opacity: 0
        Behavior on opacity { FadeAnimation {} }
        function show(msg) { feedbackLabel.text = msg; opacity = 1; feedbackTimer.restart(); }
        Label {
            id: feedbackLabel
            anchors.centerIn: parent
            width: parent.width - 2 * Theme.horizontalPageMargin
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.Wrap
            font.pixelSize: Theme.fontSizeSmall
            color: Theme.primaryColor
        }
        Timer { id: feedbackTimer; interval: 3000; onTriggered: feedback.opacity = 0 }
    }
}
