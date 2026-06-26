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

    PullDownMenu {
        MenuItem {
            text: qsTr("Settings")
            onClicked: pageStack.push(Qt.resolvedUrl("SettingsPage.qml"))
        }
        MenuItem {
            text: qsTr("Add device")
            onClicked: pageStack.push(Qt.resolvedUrl("AddDevicePage.qml"))
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
                    : qsTr("%1: %2").arg(cmd).arg(result);
            feedback.show(txt);
        }
    }

    Component.onCompleted: reload()

    delegate: ListItem {
        id: item
        contentHeight: contentColumn.height + Theme.paddingMedium
        menu: (isOwn === 1) ? null : contextMenuComponent

        Column {
            id: contentColumn
            x: Theme.horizontalPageMargin
            width: parent.width - 2 * Theme.horizontalPageMargin
            y: Theme.paddingSmall
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
                    if (lastTime !== "") parts.push("Last update from: " + lastTime);
                    if (battery >= 0) parts.push(battery + "%");
                    if (isOwn === 0 && hasPin === 0) parts.push(qsTr("no PIN set"));
                    if (authFailed === 1) parts.push(qsTr("wrong PIN"));
                    return parts.length ? parts.join("  ·  ") : qsTr("no data yet");
                }
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
                truncationMode: TruncationMode.Fade
            }

            // --- action buttons ------------------------------------------
            Flow {
                width: parent.width
                spacing: Theme.paddingSmall

                Button {
                    text: qsTr("RING")
                    enabled: ringEnabled === 1
                    onClicked: list.sendCommand(deviceId, "RING", "")
                }
                Button {
                    text: qsTr("LOCK")
                    visible: isOwn === 0
                    enabled: actionsEnabled === 1
                    onClicked: list.sendCommand(deviceId, "LOCK", "")
                }
                Button {
                    text: qsTr("CAMERA")
                    visible: isOwn === 0
                    enabled: cameraEnabled === 1
                    // Default to the back camera; front is in the long-press menu.
                    onClicked: list.sendCommand(deviceId, "CAMERA", "back")
                }
                Button {
                    text: qsTr("DELETE")
                    visible: isOwn === 0
                    enabled: actionsEnabled === 1
                    onClicked: remorse.execute(item, qsTr("Wiping remote device"),
                        function () { list.sendCommand(deviceId, "DELETE", ""); })
                }
            }
        }

        RemorseItem { id: remorse }

        Component {
            id: contextMenuComponent
            ContextMenu {
                MenuItem {
                    text: qsTr("Photo (front camera)")
                    enabled: cameraEnabled === 1
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
