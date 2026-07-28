import QtQuick 2.0
import QtPositioning 5.2
// Explicit self-directory import: required so the qmldir singleton (Bridge)
// resolves -- the implicit directory import does not register singletons.
import "."

// Foreground GPS fix provider for the sandboxed app. api.py cannot use
// dbus/gi (Harbour rules), so it emits 'requestGpsFix' and this element
// answers with api.qml_fix_result(seq, ok, data) once QtPositioning delivers
// a usable position (or the timeout hits). Loaded once from the
// ApplicationWindow; non-visual.
Item {
    id: root

    property int seq: 0
    property bool fixing: false
    // Positions older than this are treated as stale cache, not a live fix.
    readonly property int maxFixAgeMs: 120 * 1000
    property double requestedAt: 0

    Connections {
        target: Bridge
        onGpsFixRequested: {
            root.seq = requestSeq;
            root.requestedAt = Date.now();
            timeoutTimer.interval = Math.max(5000, timeoutMs);
            timeoutTimer.restart();
            root.fixing = true;   // activates the PositionSource
        }
    }

    function finish(ok, data) {
        if (!fixing)
            return;
        fixing = false;
        timeoutTimer.stop();
        Bridge.call("qml_fix_result", [seq, ok, data], function () {});
    }

    PositionSource {
        id: src
        active: root.fixing
        updateInterval: 1000

        onPositionChanged: {
            if (!root.fixing)
                return;
            var p = position;
            if (!p.latitudeValid || !p.longitudeValid)
                return;
            // Reject the stale last-known position geoclue replays on start.
            if (p.timestamp && root.requestedAt - p.timestamp.getTime() > root.maxFixAgeMs)
                return;
            root.finish(true, {
                lat: p.coordinate.latitude,
                lon: p.coordinate.longitude,
                alt: p.altitudeValid ? p.coordinate.altitude : null,
                speed: p.speedValid ? p.speed : null,
                accuracy: p.horizontalAccuracyValid ? p.horizontalAccuracy : null
            });
        }

        onSourceErrorChanged: {
            if (!root.fixing || sourceError === PositionSource.NoError)
                return;
            root.finish(false, sourceError === PositionSource.AccessError
                        ? "gps_unavailable (access denied)"
                        : "gps_unavailable (source error " + sourceError + ")");
        }
    }

    Timer {
        id: timeoutTimer
        repeat: false
        onTriggered: root.finish(false, "no fix within timeout")
    }
}
