import QtQuick 2.0
import Sailfish.Silica 1.0

// Renders a QR-code module matrix (produced by api.qr_matrix) as crisp
// black-and-white squares on a Canvas. Set `matrix` to the object returned by
// the backend: { size: <n>, rows: ["0101...", ...] } (one '0'/'1' char per
// module; the matrix already contains the white quiet-zone border).

Item {
    id: root

    // The backend matrix; assigning a new value repaints automatically.
    property var matrix: null
    // Side length of the rendered (square) code in pixels.
    property int dimension: Theme.itemSizeHuge * 3

    width: dimension
    height: dimension
    visible: matrix && matrix.rows && matrix.rows.length > 0

    Rectangle {
        anchors.fill: parent
        color: "white"
    }

    Canvas {
        id: canvas
        anchors.fill: parent
        renderStrategy: Canvas.Immediate

        onPaint: {
            var ctx = getContext("2d");
            ctx.fillStyle = "white";
            ctx.fillRect(0, 0, width, height);
            if (!root.matrix || !root.matrix.rows)
                return;
            var rows = root.matrix.rows;
            var n = rows.length;
            if (n === 0)
                return;
            var cell = width / n;
            ctx.fillStyle = "black";
            for (var y = 0; y < n; y++) {
                var rowStr = rows[y];
                for (var x = 0; x < n; x++) {
                    if (rowStr.charAt(x) === "1") {
                        // Floor the origin and ceil the size so adjacent cells
                        // overlap by <1px and never leave seams between modules.
                        ctx.fillRect(Math.floor(x * cell), Math.floor(y * cell),
                                     Math.ceil(cell), Math.ceil(cell));
                    }
                }
            }
        }
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onAvailableChanged: if (available) requestPaint()
    }

    onMatrixChanged: canvas.requestPaint()
    onVisibleChanged: if (visible) canvas.requestPaint()
}