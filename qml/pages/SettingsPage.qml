import QtQuick 2.0
import Sailfish.Silica 1.0
import "../components"

// Secrets (PINs, passwords) are obfuscated by the backend before storage.
Dialog {
    id: dialog
    allowedOrientations: Orientation.Portrait

    property var cfg: ({})
    property var daemonStatus: ({ gps: "unknown", cmd: "unknown" })
    property string totpSecretText: qsTr("(not set)")
    property string totpUriText: ""
    property var totpQrMatrix: null
    property string backupCountText: "0"

    ListModel { id: ringToneModel }

    Component.onCompleted: {
        Bridge.call("get_settings", [], function (data) {
            cfg = data || {};
            loadFields();
        });
        refreshDaemonStatus();
        loadRingTones();
    }

    // Stop any preview that is still playing when leaving the page.
    Component.onDestruction: Bridge.call("stop_ring_preview", [], function () {})

    function refreshDaemonStatus() {
        Bridge.call("get_daemon_status", [], function (s) {
            if (s) daemonStatus = s;
        });
    }

    function loadRingTones() {
        Bridge.call("list_ring_tones", [], function (res) {
            ringToneModel.clear();
            var tones = (res && res.tones) ? res.tones : [];
            var cur = (res && res.current) ? res.current : "";
            var sel = 0;
            for (var i = 0; i < tones.length; i++) {
                ringToneModel.append({ name: tones[i].name, path: tones[i].path });
                if (tones[i].path === cur)
                    sel = i;
            }
            if (ringToneModel.count > 0)
                ringToneCombo.currentIndex = sel;
        });
    }

    function loadFields() {
        ownIdLabel.value = cfg.own_device_id || "";
        intervalField.text = "" + (cfg.gps_interval_min || "5");
        autoLocSwitch.checked = cfg.auto_enable_location === "1";

        mqttSwitch.checked = cfg.mqtt_enabled === "1";
        serverField.text = cfg.mqtt_server || "";
        tlsSwitch.checked = cfg.mqtt_tls === "1";
        mqttUserField.text = cfg.mqtt_username || "";
        mqttPassField.text = cfg.mqtt_password || "";
        portField.text = "" + (cfg.mqtt_port || "8883");
        backgroundSwitch.checked = cfg.background_enabled === "1";

        ringSwitch.checked = cfg.ring_enabled === "1";
        lockSwitch.checked = cfg.lock_enabled === "1";
        deleteSwitch.checked = cfg.delete_enabled === "1";
        pinField.text = cfg.pin || "";

        cameraSwitch.checked = cfg.camera_enabled === "1";
        webdavUrlField.text = cfg.webdav_url || "";
        webdavUserField.text = cfg.webdav_username || "";
        webdavPassField.text = cfg.webdav_password || "";

        smsRemoteSwitch.checked = cfg.sms_remote_enabled === "1";
        smsGpsSwitch.checked = cfg.sms_gps_enabled === "1";
        whitelistField.text = cfg.sms_whitelist || "";

        providerCombo.currentIndex = (cfg.tile_provider === "geoapify") ? 1 : 0;
        geoapifyKeyField.text = cfg.geoapify_key || "";

        totpSecretText = cfg.totp_secret ? cfg.totp_secret : qsTr("(not set)");
        totpUriText = cfg.totp_uri || "";
        refreshTotpQr();
        backupCountText = "" + (cfg.backup_codes_unused || 0);
    }

    function refreshTotpQr() {
        if (!totpUriText) {
            totpQrMatrix = null;
            return;
        }
        Bridge.call("qr_matrix", [totpUriText], function (m) {
            totpQrMatrix = (m && m.rows && m.rows.length > 0) ? m : null;
        });
    }

    onAccepted: {
        var values = {
            gps_interval_min: intervalField.text,
            auto_enable_location: autoLocSwitch.checked ? "1" : "0",
            mqtt_enabled: mqttSwitch.checked ? "1" : "0",
            mqtt_server: serverField.text,
            mqtt_tls: tlsSwitch.checked ? "1" : "0",
            mqtt_username: mqttUserField.text,
            mqtt_password: mqttPassField.text,
            mqtt_port: portField.text,
            background_enabled: backgroundSwitch.checked ? "1" : "0",
            ring_enabled: ringSwitch.checked ? "1" : "0",
            lock_enabled: lockSwitch.checked ? "1" : "0",
            delete_enabled: deleteSwitch.checked ? "1" : "0",
            pin: pinField.text,
            camera_enabled: cameraSwitch.checked ? "1" : "0",
            webdav_url: webdavUrlField.text,
            webdav_username: webdavUserField.text,
            webdav_password: webdavPassField.text,
            sms_remote_enabled: smsRemoteSwitch.checked ? "1" : "0",
            sms_gps_enabled: smsGpsSwitch.checked ? "1" : "0",
            sms_whitelist: whitelistField.text,
            tile_provider: providerCombo.currentIndex === 1 ? "geoapify" : "osm",
            geoapify_key: geoapifyKeyField.text
        };
        var ri = ringToneCombo.currentIndex;
        if (ri >= 0 && ri < ringToneModel.count)
            values.ring_tone = ringToneModel.get(ri).path;
        // Capture the Bridge singleton: onAccepted pops and destroys this Dialog
        // page, so by the time the save_settings callback fires an unqualified
        // 'Bridge' would resolve to undefined (the page's import scope is gone).
        var b = Bridge;
        b.call("stop_ring_preview", [], function () {});
        b.call("save_settings", [values], function () {
            b.refreshMapConfig();
        });
    }

    SilicaFlickable {
        anchors.fill: parent
        contentHeight: column.height

        VerticalScrollDecorator {}

        Column {
            id: column
            width: parent.width
            spacing: Theme.paddingSmall

            DialogHeader { title: qsTr("Settings") }

            // --- daemon status overview --------------------------------------
            SectionHeader { text: qsTr("Background services") }
            Column {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                spacing: Theme.paddingSmall

                Item {
                    width: parent.width
                    height: gpsNameLabel.implicitHeight
                    Label {
                        id: gpsNameLabel
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        text: qsTr("GPS service")
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.primaryColor
                    }
                    Label {
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        text: dialog.daemonStatus.gps
                        font.pixelSize: Theme.fontSizeSmall
                        color: dialog.daemonStatus.gps === "running"
                               ? Theme.highlightColor : Theme.secondaryColor
                    }
                }

                Item {
                    width: parent.width
                    height: cmdNameLabel.implicitHeight
                    Label {
                        id: cmdNameLabel
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        text: qsTr("Command service")
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.primaryColor
                    }
                    Label {
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        text: dialog.daemonStatus.cmd
                        font.pixelSize: Theme.fontSizeSmall
                        color: dialog.daemonStatus.cmd === "running"
                               ? Theme.highlightColor : Theme.secondaryColor
                    }
                }

                Button {
                    id: statusBtn
                    text: qsTr("Refresh")
                    highlighted: true
                    onClicked: dialog.refreshDaemonStatus()
                    anchors.horizontalCenter: parent.horizontalCenter
                }

                // Two separate labels so each line is its own translation unit and
                // the command-service note always starts on a new line.
                Label {
                    width: parent.width
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSizeExtraSmall
                    color: Theme.secondaryColor
                    text: qsTr("GPS service: Activated when you turn the switch 'Background activity' on.")
                }
                Label {
                    width: parent.width
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSizeExtraSmall
                    color: Theme.secondaryColor
                    text: qsTr("Command service: Activated when you turn min. one remote action or SMS action on.")
                }
            }

            // --- general -----------------------------------------------------
            SectionHeader { text: qsTr("General") }
            BackgroundItem {
                id: ownIdLabel
                property string value: ""
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                height: Math.max(ownIdNameLabel.implicitHeight, ownIdValueLabel.implicitHeight) + Theme.paddingMedium * 2
                onClicked: {
                    Clipboard.text = ownIdLabel.value
                }
                Label {
                    id: ownIdNameLabel
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    text: qsTr("Device-Id")
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.primaryColor
                }
                Row {
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Theme.paddingSmall
                    Label {
                        id: ownIdValueLabel
                        anchors.verticalCenter: parent.verticalCenter
                        text: ownIdLabel.value
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.highlightColor
                    }
                    Image {
                        anchors.verticalCenter: parent.verticalCenter
                        source: "image://theme/icon-s-clipboard"
                        sourceSize.width: Theme.iconSizeSmall
                        sourceSize.height: Theme.iconSizeSmall
                        opacity: 0.6
                    }
                }
            }
            TextField {
                id: intervalField
                width: parent.width
                label: qsTr("GPS query interval (minutes)")
                inputMethodHints: Qt.ImhDigitsOnly
                validator: IntValidator { bottom: 1; top: 1440 }
            }
            TextSwitch {
                id: autoLocSwitch
                text: qsTr("Auto-enable location when needed")
                description: qsTr("Lets the daemon turn on the system location "
                                + "services and accept the agreement. Opt-in.")
            }

            // --- MQTT --------------------------------------------------------
            SectionHeader { text: qsTr("MQTT") }
            TextSwitch {
                id: mqttSwitch
                text: qsTr("Publish coordinates over MQTT")
                description: qsTr("Off = the daemon stores locally but does not publish.")
            }
            TextField {
                id: serverField
                width: parent.width
                label: qsTr("MQTT server")
                placeholderText: qsTr("broker.example.com")
                inputMethodHints: Qt.ImhNoPredictiveText | Qt.ImhNoAutoUppercase
            }
            TextSwitch {
                id: tlsSwitch
                text: qsTr("Use TLS")
                onCheckedChanged: {
                    if (portField.text === "8883" || portField.text === "1883")
                        portField.text = checked ? "8883" : "1883";
                }
            }
            TextField {
                id: mqttUserField
                width: parent.width
                label: qsTr("MQTT username")
                inputMethodHints: Qt.ImhNoPredictiveText | Qt.ImhNoAutoUppercase
            }
            PasswordField {
                id: mqttPassField
                width: parent.width
                label: qsTr("MQTT password")
                inputMethodHints: Qt.ImhNoPredictiveText
            }
            TextField {
                id: portField
                width: parent.width
                label: qsTr("Port")
                inputMethodHints: Qt.ImhDigitsOnly
                validator: IntValidator { bottom: 1; top: 65535 }
            }
            TextSwitch {
                id: backgroundSwitch
                text: qsTr("Background activity")
                description: qsTr("Keep reporting the location while the app is closed "
                                + "(the daemon 'GPS service' runs).")
            }

            // --- remote actions ---------------------------------------------
            SectionHeader { text: qsTr("Remote actions") }

            PasswordField {
                id: pinField
                width: parent.width
                label: qsTr("PIN for remote access (HMAC secret)")
                inputMethodHints: Qt.ImhNoPredictiveText
            }

            TextSwitch {
                id: ringSwitch
                text: qsTr("Allow command RING")
                description: qsTr("Device will ring for 60 seconds the below defined tone.")
            }
            // Ringtone picker for the RING sound. Plays the chosen file on a loop
            ComboBox {
                id: ringToneCombo
                width: parent.width
                label: qsTr("Ringtone")
                visible: ringSwitch.checked
                menu: ContextMenu {
                    Repeater {
                        model: ringToneModel
                        MenuItem { text: model.name }
                    }
                }
            }
            Column {
                width: parent.width
                spacing: Theme.paddingSmall
                visible: ringSwitch.checked && ringToneModel.count > 0

                Row {
                    width: parent.width
                    spacing: Theme.paddingMedium
                    Button {
                        text: qsTr("Preview")
                        onClicked: {
                            var idx = ringToneCombo.currentIndex;
                            if (idx >= 0 && idx < ringToneModel.count)
                                Bridge.call("preview_ring_tone",
                                    [ringToneModel.get(idx).path], function () {});
                        }
                    }
                    Button {
                        text: qsTr("Stop")
                        onClicked: Bridge.call("stop_ring_preview", [], function () {})
                    }
                }

                Label {
                    x: Theme.horizontalPageMargin
                    width: parent.width - 2 * Theme.horizontalPageMargin
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSizeExtraSmall
                    color: Theme.secondaryColor
                    text: qsTr("Note: Preview sound will be played with the current system volume, remote command will be played with 100% volume, ignoring muted device.")
                }
            }
            TextSwitch {
                id: lockSwitch
                text: qsTr("Allow command LOCK")
                description: qsTr("If device is unlocked, it will be locked into lock screen.")
            }
            TextSwitch {
                id: deleteSwitch
                text: qsTr("Allow command DELETE (wipe)")
                description: qsTr("Will delete all userdata stored under 'home//<user>//' and reboot device afterwards.")
            }

            // --- camera ------------------------------------------------------
            SectionHeader { text: qsTr("Camera") }
            TextSwitch {
                id: cameraSwitch
                text: qsTr("Allow command CAMERA")
                description: qsTr("A photo can be captured and uploaded to the configured WebDAV server.")
            }
            TextField {
                id: webdavUrlField
                width: parent.width
                label: qsTr("WebDAV URL (full upload path) for photo upload")
                inputMethodHints: Qt.ImhNoPredictiveText | Qt.ImhNoAutoUppercase
            }
            TextField {
                id: webdavUserField
                width: parent.width
                label: qsTr("WebDAV username")
                inputMethodHints: Qt.ImhNoPredictiveText | Qt.ImhNoAutoUppercase
            }
            PasswordField {
                id: webdavPassField
                width: parent.width
                label: qsTr("WebDAV password")
                inputMethodHints: Qt.ImhNoPredictiveText
            }

            // --- SMS ---------------------------------------------------------
            SectionHeader { text: qsTr("SMS") }
            TextArea {
                id: whitelistField
                width: parent.width
                placeholderText: qsTr("+4915123456789")
                description: qsTr("Whitelist (Allowed senders) - one per line")
            }
            TextSwitch {
                id: smsRemoteSwitch
                text: qsTr("Remote control via SMS")
                description: qsTr("Turn on if you want accept SMS commands from the whitelist. "
                                + "The current TOTP code is required in SMS commands.")
            }
            TextSwitch {
                id: smsGpsSwitch
                text: qsTr("Allow command GPS")
                description: qsTr("Sends current GPS coordinates via SMS to sender. SMS will NOT be shown under sent messages but notification will be shown. ATTENTION: SMS costs may apply.")
            }

            // --- SMS two-factor (TOTP + backup codes) ------------------------
            SectionHeader { text: qsTr("SMS authentication (TOTP)") }
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSizeExtraSmall
                color: Theme.secondaryColor
                text: qsTr("Enrol this secret in a TOTP authenticator app "
                         + "on a SECOND device. The current code is required "
                         + "in SMS commands. Keep backup codes safe for use "
                         + "without an authenticator app.")
            }
            BackgroundItem {
                id: totpSecretItem
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                height: Math.max(totpNameLabel.implicitHeight, totpValueLabel.implicitHeight) + Theme.paddingMedium * 2
                enabled: dialog.totpUriText !== ""
                onClicked: Clipboard.text = dialog.totpSecretText
                Label {
                    id: totpNameLabel
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    text: qsTr("TOTP secret")
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.primaryColor
                }
                Row {
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.left: totpNameLabel.right
                    anchors.leftMargin: Theme.paddingMedium
                    spacing: Theme.paddingSmall
                    layoutDirection: Qt.RightToLeft
                    Image {
                        anchors.verticalCenter: parent.verticalCenter
                        source: "image://theme/icon-s-clipboard"
                        sourceSize.width: Theme.iconSizeSmall
                        sourceSize.height: Theme.iconSizeSmall
                        opacity: 0.6
                        visible: dialog.totpUriText !== ""
                    }
                    Label {
                        id: totpValueLabel
                        anchors.verticalCenter: parent.verticalCenter
                        width: Math.min(implicitWidth, parent.width - Theme.iconSizeSmall - Theme.paddingSmall)
                        truncationMode: TruncationMode.Fade
                        text: dialog.totpSecretText
                        font.pixelSize: Theme.fontSizeSmall
                        font.family: Theme.fontFamilyHeading
                        color: Theme.highlightColor
                    }
                }
            }

            // Scannable QR code of the otpauth:// URI for a second device.
            QrCode {
                anchors.horizontalCenter: parent.horizontalCenter
                visible: dialog.totpQrMatrix !== null
                matrix: dialog.totpQrMatrix
                dimension: Math.min(parent.width - 2 * Theme.horizontalPageMargin,
                                    Theme.itemSizeHuge * 3)
            }
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
                visible: dialog.totpQrMatrix !== null
                font.pixelSize: Theme.fontSizeExtraSmall
                color: Theme.secondaryColor
                text: qsTr("Scan with an authenticator app on another device")
            }
            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                text: qsTr("Generate new TOTP secret")
                highlighted: true
                onClicked: Bridge.call("rotate_totp_secret", [], function (r) {
                    if (r) {
                        dialog.totpSecretText = r.secret;
                        dialog.totpUriText = r.uri;
                        dialog.refreshTotpQr();
                    }
                })
            }
            SectionHeader { text: qsTr("SMS - Backup Codes") }
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSizeExtraSmall
                color: Theme.secondaryColor
                text: qsTr("If TOTP is not available, backup codes can be used for authentication. Each code can be used only once.")
            }
            Column {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                spacing: Theme.paddingSmall

                Item {
                    width: parent.width
                    height: backupCodesNameLabel.implicitHeight
                    Label {
                        id: backupCodesNameLabel
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        text: qsTr("Unused backup codes")
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.primaryColor
                    }
                    Label {
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        text: dialog.backupCountText
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.highlightColor
                    }
                }
            }

            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                text: qsTr("Regenerate backup codes")
                highlighted: true
                color: Theme.primaryColor
                onClicked: Bridge.call("regenerate_backup_codes", [], function (codes) {
                    if (codes) {
                        dialog.backupCountText = "" + codes.length;
                        pageStack.push(Qt.resolvedUrl("BackupCodesPage.qml"), { codes: codes });
                    }
                })
            }

            // --- map ---------------------------------------------------------
            SectionHeader { text: qsTr("Map") }
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSizeExtraSmall
                color: Theme.secondaryColor
                text: qsTr("The standard OpenStreetMap is not zoomable but you can create a free account " +
                            "at Geoapify to get a free API key for a zoomable map. The key is optional, but " +
                            "without it the map will not be zoomable.")
            }
            ComboBox {
                id: providerCombo
                width: parent.width
                label: qsTr("Tile provider")
                menu: ContextMenu {
                    MenuItem { text: qsTr("OpenStreetMap (no key needed)") }
                    MenuItem { text: qsTr("OpenStreetMap Geoapify") }
                }
            }
            TextField {
                id: geoapifyKeyField
                width: parent.width
                label: qsTr("Geoapify API key (optional)")
                inputMethodHints: Qt.ImhNoPredictiveText | Qt.ImhNoAutoUppercase
            }

            // ── About ────────────────────────────────────
            SectionHeader {
                text: qsTr("About")
            }

            ListItem {
                contentHeight: Theme.itemSizeMedium
                _backgroundColor: "transparent"
                highlighted: false

                Label {
                    anchors.left: parent.left
                    anchors.leftMargin: Theme.horizontalPageMargin
                    anchors.verticalCenter: parent.verticalCenter
                    text: qsTr("App Version")
                    color: Theme.primaryColor
                }
                Label {
                    anchors.right: parent.right
                    anchors.rightMargin: Theme.horizontalPageMargin
                    anchors.verticalCenter: parent.verticalCenter
                    text: Bridge.appVersion !== "" ? Bridge.appVersion : "?.?.?"
                    color: Theme.secondaryColor
                }
            }

            Separator {
                width: parent.width
                color: Theme.primaryColor
                horizontalAlignment: Qt.AlignHCenter
            }

            ListItem {
                contentHeight: Theme.itemSizeMedium

                onClicked: Qt.openUrlExternally("https://forum.sailfishos.org/t/radar-app-find-my-device/30944")

                Label {
                    anchors.left: parent.left
                    anchors.leftMargin: Theme.horizontalPageMargin
                    anchors.verticalCenter: parent.verticalCenter
                    text: qsTr("Report a bug or request a feature")
                    color: Theme.primaryColor
                }
                Image {
                    anchors.right: parent.right
                    anchors.rightMargin: Theme.horizontalPageMargin
                    anchors.verticalCenter: parent.verticalCenter
                    source: "image://theme/icon-m-right"
                    width: Theme.iconSizeSmall
                    height: Theme.iconSizeSmall
                }
            }

            Separator {
                width: parent.width
                color: Theme.primaryColor
                horizontalAlignment: Qt.AlignHCenter
            }

            ListItem {
                contentHeight: Theme.itemSizeMedium

                onClicked: Qt.openUrlExternally("https://github.com/Dominik-h-hub/harbour-find-my-device/tree/main/translations")

                Label {
                    anchors.left: parent.left
                    anchors.leftMargin: Theme.horizontalPageMargin
                    anchors.verticalCenter: parent.verticalCenter
                    text: qsTr("Add a translation")
                    color: Theme.primaryColor
                }
                Image {
                    anchors.right: parent.right
                    anchors.rightMargin: Theme.horizontalPageMargin
                    anchors.verticalCenter: parent.verticalCenter
                    source: "image://theme/icon-m-right"
                    width: Theme.iconSizeSmall
                    height: Theme.iconSizeSmall
                }
            }

            Separator {
                width: parent.width
                color: Theme.primaryColor
                horizontalAlignment: Qt.AlignHCenter
            }

            ListItem {
                contentHeight: Theme.itemSizeMedium

                onClicked: Qt.openUrlExternally("https://github.com/Dominik-h-hub/harbour-find-my-device")

                Label {
                    anchors.left: parent.left
                    anchors.leftMargin: Theme.horizontalPageMargin
                    anchors.verticalCenter: parent.verticalCenter
                    text: qsTr("Code Repository")
                    color: Theme.primaryColor
                }
                Image {
                    anchors.right: parent.right
                    anchors.rightMargin: Theme.horizontalPageMargin
                    anchors.verticalCenter: parent.verticalCenter
                    source: "image://theme/icon-m-right"
                    width: Theme.iconSizeSmall
                    height: Theme.iconSizeSmall
                }
            }

            Separator {
                width: parent.width
                color: Theme.primaryColor
                horizontalAlignment: Qt.AlignHCenter
            }

            ListItem {
                contentHeight: Theme.itemSizeMedium

                onClicked: Qt.openUrlExternally("https://github.com/Dominik-h-hub/harbour-find-my-device/blob/main/docs/USER-GUIDE.md")

                Label {
                    anchors.left: parent.left
                    anchors.leftMargin: Theme.horizontalPageMargin
                    anchors.verticalCenter: parent.verticalCenter
                    text: qsTr("User Manual")
                    color: Theme.primaryColor
                }
                Image {
                    anchors.right: parent.right
                    anchors.rightMargin: Theme.horizontalPageMargin
                    anchors.verticalCenter: parent.verticalCenter
                    source: "image://theme/icon-m-right"
                    width: Theme.iconSizeSmall
                    height: Theme.iconSizeSmall
                }
            }

            Item { width: 1; height: Theme.paddingLarge }
        }
    }
}
