import QtQuick 2.0
import Sailfish.Silica 1.0
import "../components"

// Full-page confirmation for the destructive DELETE command (remote wipe + reboot).
Page {
    id: page

    property string deviceId: ""
    property string deviceLabel: ""

    SilicaFlickable {
        anchors.fill: parent
        contentHeight: column.height + Theme.paddingLarge

        VerticalScrollDecorator {}

        Column {
            id: column
            width: page.width
            spacing: Theme.paddingLarge

            PageHeader { title: qsTr("Confirm deletion") }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                visible: page.deviceLabel !== ""
                text: page.deviceLabel
                color: Theme.highlightColor
                font.pixelSize: Theme.fontSizeLarge
            }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                color: Theme.errorColor
                font.pixelSize: Theme.fontSizeMedium
                text: qsTr("WARNING: You are about to delete all user data from your "
                         + "device. This cannot be undone. After the user data has "
                         + "been deleted the device will reboot. Locating it will no "
                         + "longer be possible. Do you really want to continue?")
            }

            // Destructive button: a red chip the user must press on purpose.
            Rectangle {
                anchors.horizontalCenter: parent.horizontalCenter
                width: Math.min(parent.width - 2 * Theme.horizontalPageMargin,
                                Theme.buttonWidthLarge)
                height: Theme.itemSizeSmall
                radius: Theme.paddingSmall
                color: delMouse.pressed ? Theme.errorColor
                                        : Theme.rgba(Theme.errorColor, 0.25)
                border.width: 1
                border.color: Theme.errorColor

                Label {
                    anchors.centerIn: parent
                    text: qsTr("Delete user data now")
                    font.bold: true
                    color: delMouse.pressed ? Theme.primaryColor : Theme.errorColor
                }

                MouseArea {
                    id: delMouse
                    anchors.fill: parent
                    onClicked: {
                        var b = Bridge;
                        var id = page.deviceId;
                        b.call("send_command", [id, "DELETE", ""], function () {});
                        pageStack.pop();
                    }
                }
            }

            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                text: qsTr("Cancel")
                onClicked: pageStack.pop()
            }
        }
    }
}