import QtQuick 2.0
import Sailfish.Silica 1.0

// Compact command button used in the Devices list.
Rectangle {
    id: root

    property string text
    property bool btnEnabled: true
    signal activated()

    height: Theme.itemSizeExtraSmall
    radius: Theme.paddingSmall
    // Clearly accented chip: tinted highlight fill + highlight border, filling
    // solid while pressed. Much more visible than the previous faint primary tint.
    color: mouse.pressed && btnEnabled
           ? Theme.highlightColor
           : Theme.rgba(Theme.highlightColor, 0.22)
    border.width: 1
    border.color: Theme.rgba(Theme.highlightColor, 0.9)
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
               ? Theme.primaryColor : Theme.highlightColor
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