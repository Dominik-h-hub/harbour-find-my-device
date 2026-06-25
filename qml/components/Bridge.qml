pragma Singleton
import QtQuick 2.0
import io.thp.pyotherside 1.4

// Single shared PyOtherSide bridge. Declared as a QML singleton so every page
// talks to the SAME Python interpreter state and one 'received' handler relays
// the backend's async signals as QML signals.
QtObject {
    id: bridge

    property bool ready: false
    property string ownDeviceId: ""
    property string ownLabel: ""
    property string osmUserAgent: ""

    // Relayed backend signals (see api.py _emit()).
    signal logMessage(string text)
    signal mapUpdated()
    signal devicesUpdated()
    signal commandResult(string deviceId, string cmd, string result)
    signal locationFix(bool success, string message)

    // Convenience wrapper so callers don't repeat the 'api.' prefix.
    function call(func, args, callback) {
        _py.call("api." + func, args || [], callback || function () {});
    }

    property var _py: Python {
        Component.onCompleted: {
            addImportPath(Qt.resolvedUrl("../utilities"));
            importModule("api", function () {
                // NB: must be bridge.call, not call() — inside this Python
                // element an unqualified call() would resolve to Python.call()
                // (PyOtherSide's own method) and skip the "api." prefix.
                bridge.call("init_app", [], function (state) {
                    if (state) {
                        bridge.ownDeviceId = state.own_device_id;
                        bridge.ownLabel = state.own_label;
                        bridge.osmUserAgent = state.osm_user_agent;
                    }
                    bridge.ready = true;
                    bridge.devicesUpdated();
                    bridge.mapUpdated();
                });
            });
        }

        onError: console.log("fmd python error: " + traceback)

        onReceived: {
            var ev = data[0];
            if (ev === "log")
                bridge.logMessage(data[1]);
            else if (ev === "mapUpdated")
                bridge.mapUpdated();
            else if (ev === "devicesUpdated")
                bridge.devicesUpdated();
            else if (ev === "commandResult")
                bridge.commandResult(data[1], data[2], data[3]);
            else if (ev === "locationFix")
                bridge.locationFix(data[1], data[2]);
        }
    }
}
