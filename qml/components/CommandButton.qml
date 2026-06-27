import QtQuick 2.0
import Sailfish.Silica 1.0

// Compact command button used in the Devices list.
Rectangle {
    id: root

    property string text
    property bool btnEnabled: true
    // When active, the chip uses the error/red accent (used for the RING->STOP
    // toggle so it is clear the next press stops the ringing).
    property bool active: false
    signal activated()

    // The accent colour: red while active (STOP), highlight otherwise.
    readonly property color accent: active ? Theme.errorColor : Theme.highlightColor

    height: Theme.itemSizeExtraSmall
    radius: Theme.paddingSmall
    // Clearly accented chip: tinted fill + border, filling solid while pressed.
    color: mouse.pressed && btnEnabled
           ? accent
           : Theme.rgba(accent, 0.22)
    border.width: 1
    border.color: Theme.rgba(accent, 0.9)
    opacity: btnEnabled ? 1.0 : 0.3

    Label {
        anchors {
            fill: parent
            leftMargin: Theme.paddingSmall
            rightMargin: Theme.paddingSmall
        }
        text: root.text
        font.pixelSize: Theme.fontSizeExtraSmall
        font.bold: true
        color: mouse.pressed && root.btnEnabled
               ? Theme.primaryColor : root.accent
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        truncationMode: TruncationMode.Fade
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        enabled: root.btnEnabled
        onClicked: root.activated()
    }
}