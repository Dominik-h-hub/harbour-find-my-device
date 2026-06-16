import QtQuick 2.0
import Sailfish.Silica 1.0
import io.thp.pyotherside 1.4

CoverBackground {
    Label {
        id: label
        anchors.centerIn: parent
        text: qsTr("My Cover")
    }

    CoverActionList {
        id: coverAction

        CoverAction {
            iconSource: "image://theme/icon-cover-next"
            onTriggered: python.call('coveractions.action_next', [], function(newstring) {
                label.text = newstring;
            });
        }

        CoverAction {
            iconSource: "image://theme/icon-cover-pause"
            onTriggered: python.call('coveractions.action_pause', [], function(newstring) {
                label.text = newstring;
            });
        }
    }

    Python {
        id: python

        Component.onCompleted: {
            addImportPath(Qt.resolvedUrl('.'));
            importModule('coveractions', function () {});
        }

        onError: {
            console.log('python error: ' + traceback);
        }
    }
}
