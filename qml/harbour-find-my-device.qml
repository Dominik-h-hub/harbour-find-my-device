import QtQuick 2.0
import Sailfish.Silica 1.0
import "pages"
import "components"

ApplicationWindow {
    id: appWindow

    // Touch the singleton early so the Python backend initializes at startup.
    Component.onCompleted: Bridge.ready

    // Foreground GPS provider (QtPositioning). In a Loader so a missing
    // QtPositioning module degrades gracefully: fix requests then time out
    // instead of the whole app failing to start.
    Loader { source: Qt.resolvedUrl("components/GpsSource.qml") }

    initialPage: Component { MainPage { } }
    cover: Qt.resolvedUrl("cover/CoverPage.qml")
    allowedOrientations: defaultAllowedOrientations
}
