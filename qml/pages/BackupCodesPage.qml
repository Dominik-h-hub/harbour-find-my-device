import QtQuick 2.0
import Sailfish.Silica 1.0

// One-time display of freshly generated backup codes. They are stored only as
// hashes, so this is the single chance to copy them down.
Page {
    id: page
    allowedOrientations: Orientation.All

    property var codes: []

    SilicaListView {
        anchors.fill: parent
        header: Column {
            width: parent.width
            PageHeader { title: qsTr("Backup codes") }
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.secondaryColor
                text: qsTr("Write these down now. Each code works once and is shown "
                         + "only here. They replace the TOTP code in an SMS command "
                         + "when you have no authenticator app.")
            }
            Item { width: 1; height: Theme.paddingLarge }
        }
        model: page.codes
        delegate: ListItem {
            Label {
                anchors.centerIn: parent
                text: modelData
                font.pixelSize: Theme.fontSizeLarge
                font.family: "monospace"
                color: Theme.highlightColor
            }
            onClicked: Clipboard.text = modelData
        }
        footer: Button {
            anchors.horizontalCenter: parent.horizontalCenter
            text: qsTr("Copy all")
            onClicked: Clipboard.text = page.codes.join("\n")
        }
        VerticalScrollDecorator {}
    }
}
