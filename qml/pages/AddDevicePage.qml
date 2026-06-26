import QtQuick 2.0
import Sailfish.Silica 1.0
import "../components"

// Add or edit a remote device.
Dialog {
    id: dialog
    allowedOrientations: Orientation.All

    property bool editMode: false
    property string deviceId: ""
    property string deviceLabel: ""

    canAccept: editMode || (idField.text.length === 10 && idValid)
    property bool idValid: /^[A-Za-z0-9]{10}$/.test(idField.text)

    onAccepted: {
        if (editMode) {
            Bridge.call("update_device",
                [deviceId, labelField.text, pinField.text], function () {});
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
                title: dialog.editMode ? qsTr("Edit device") : qsTr("Add device")
            }

            TextField {
                id: idField
                width: parent.width
                label: qsTr("Device-Id (10 letters/digits)")
                placeholderText: qsTr("Device-Id")
                text: dialog.deviceId
                readOnly: dialog.editMode
                enabled: !dialog.editMode
                inputMethodHints: Qt.ImhNoPredictiveText | Qt.ImhNoAutoUppercase
                color: (dialog.editMode || dialog.idValid || text.length === 0)
                       ? Theme.primaryColor : Theme.errorColor
                EnterKey.iconSource: "image://theme/icon-m-enter-next"
                EnterKey.onClicked: labelField.focus = true
            }

            TextField {
                id: labelField
                width: parent.width
                label: qsTr("Device label (shown on the map)")
                placeholderText: qsTr("e.g. Wife's phone")
                text: dialog.deviceLabel
                EnterKey.iconSource: "image://theme/icon-m-enter-next"
                EnterKey.onClicked: pinField.focus = true
            }

            PasswordField {
                id: pinField
                width: parent.width
                label: qsTr("PIN for remote access (HMAC secret)")
                inputMethodHints: Qt.ImhNoPredictiveText
                EnterKey.iconSource: "image://theme/icon-m-enter-accept"
                EnterKey.onClicked: dialog.accept()
            }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSizeExtraSmall
                color: Theme.secondaryColor
                text: qsTr("The PIN must match the remote device's own PIN. The same "
                         + "MQTT server as configured in Settings is used to reach it.")
            }
        }
    }
}
