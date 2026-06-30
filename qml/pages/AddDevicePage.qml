import QtQuick 2.0
import Sailfish.Silica 1.0
import "../components"

// Add or edit a remote device.
Dialog {
    id: dialog
    allowedOrientations: Orientation.Portrait

    property bool editMode: false
    property bool isOwn: false           // editing this device: id read-only, no PIN
    property string deviceId: ""
    property string deviceLabel: ""

    // The own device's id is fixed (only the label is editable); for remote
    // devices the id is editable and must be valid to accept.
    canAccept: isOwn || (idField.text.length === 10 && idValid)
    property bool idValid: /^[A-Za-z0-9]{10}$/.test(idField.text)

    Component.onCompleted: {
        // The own device has no editable PIN here (it lives in Settings).
        if (editMode && !isOwn && deviceId !== "") {
            Bridge.call("get_device_pin", [deviceId], function (pin) {
                pinField.text = pin || "";
            });
        }
    }

    onAccepted: {
        if (isOwn) {
            // Capture Bridge before the async boundary (the dialog is destroyed on
            // pop) and reflect the new label in the cover immediately.
            var b = Bridge;
            var newLabel = labelField.text;
            b.call("set_own_label", [newLabel], function () {});
            b.ownLabel = newLabel;
        } else if (editMode) {
            Bridge.call("update_device",
                [dialog.deviceId, labelField.text, pinField.text, idField.text],
                function () {});
        } else {
            Bridge.call("add_device",
                [idField.text, labelField.text, pinField.text], function (res) {
                });
        }
    }

    SilicaFlickable {
        anchors.fill: parent
        contentHeight: column.height

        Column {
            id: column
            width: parent.width
            spacing: Theme.paddingMedium

            DialogHeader {
                title: dialog.isOwn ? qsTr("Edit this device")
                     : dialog.editMode ? qsTr("Edit device") : qsTr("Add device")
            }

            Item {
                width: parent.width
                height: idField.height

                TextField {
                    id: idField
                    anchors.left: parent.left
                    // Leave room for the copy button on the own device.
                    anchors.right: dialog.isOwn ? copyIdButton.left : parent.right
                    // The own device's id is its fixed identity -> read-only.
                    readOnly: dialog.isOwn
                    label: dialog.isOwn ? qsTr("Device-Id")
                                        : qsTr("Device-Id (10 letters/digits)")
                    placeholderText: qsTr("Device-Id")
                    text: dialog.deviceId
                    inputMethodHints: Qt.ImhNoPredictiveText | Qt.ImhNoAutoUppercase
                    color: (dialog.isOwn || dialog.idValid || text.length === 0)
                           ? Theme.primaryColor : Theme.errorColor
                    EnterKey.iconSource: "image://theme/icon-m-enter-next"
                    EnterKey.onClicked: labelField.focus = true
                }

                IconButton {
                    id: copyIdButton
                    visible: dialog.isOwn
                    anchors.right: parent.right
                    anchors.rightMargin: Theme.horizontalPageMargin
                    // Align with the text input row, not the field's top label.
                    anchors.bottom: idField.bottom
                    anchors.bottomMargin: Theme.paddingMedium
                    icon.source: "image://theme/icon-m-clipboard"
                    onClicked: Clipboard.text = idField.text
                }
            }

            TextField {
                id: labelField
                width: parent.width
                label: qsTr("Device label (shown on the map)")
                placeholderText: qsTr("e.g. My Jolla Phone")
                text: dialog.deviceLabel
                EnterKey.iconSource: dialog.isOwn ? "image://theme/icon-m-enter-accept"
                                                  : "image://theme/icon-m-enter-next"
                EnterKey.onClicked: dialog.isOwn ? dialog.accept() : (pinField.focus = true)
            }

            PasswordField {
                id: pinField
                width: parent.width
                // The own device's remote-access PIN is managed in Settings.
                visible: !dialog.isOwn
                label: qsTr("PIN for remote access (HMAC secret)")
                inputMethodHints: Qt.ImhNoPredictiveText
                EnterKey.iconSource: "image://theme/icon-m-enter-accept"
                EnterKey.onClicked: dialog.accept()
            }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                visible: !dialog.isOwn
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSizeExtraSmall
                color: Theme.secondaryColor
                text: qsTr("The PIN must match the remote device's own PIN. The same "
                         + "MQTT server as configured in Settings is used to reach it.")
            }
        }
    }
}
