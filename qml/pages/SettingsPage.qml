import QtQuick 2.0
import Sailfish.Silica 1.0
import "../components"

// Secrets (PINs, passwords) are obfuscated by the backend before storage.
Dialog {
    id: dialog
    allowedOrientations: Orientation.All

    property var cfg: ({})
    property var daemonStatus: ({ gps: "unknown", cmd: "unknown" })
    property string totpSecretText: qsTr("(not set)")
    property string totpUriText: ""
    property string backupCountText: "0"

    Component.onCompleted: {
        Bridge.call("get_settings", [], function (data) {
            cfg = data || {};
            loadFields();
        });
        refreshDaemonStatus();
    }

    function refreshDaemonStatus() {
        Bridge.call("get_daemon_status", [], function (s) {
            if (s) daemonStatus = s;
        });
    }

    function loadFields() {
        ownIdLabel.value = cfg.own_device_id || "";
        labelField.text = cfg.device_label || "";
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
        backupCountText = "" + (cfg.backup_codes_unused || 0);
    }

    onAccepted: {
        var values = {
            device_label_own: labelField.text,
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
        Bridge.call("save_settings", [values], function () {});
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
                    onClicked: dialog.refreshDaemonStatus()
                    anchors.horizontalCenter: parent.horizontalCenter
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
                id: labelField
                width: parent.width
                label: qsTr("Device label")
                placeholderText: qsTr("Falls leer wird die Device-Id angezeigt")
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
            TextField {
                id: mqttPassField
                width: parent.width
                label: qsTr("MQTT password")
                echoMode: TextInput.Password
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
                                + "(the GPS daemon runs).")
            }

            // --- remote actions ---------------------------------------------
            SectionHeader { text: qsTr("Remote actions") }
            TextSwitch { id: ringSwitch; text: qsTr("Allow RING") }
            TextSwitch { id: lockSwitch; text: qsTr("Allow remote LOCK") }
            TextSwitch { id: deleteSwitch; text: qsTr("Allow remote DELETE (wipe)") }
            PasswordField {
                id: pinField
                width: parent.width
                label: qsTr("PIN for remote access (HMAC secret)")
                inputMethodHints: Qt.ImhNoPredictiveText
            }

            // --- camera ------------------------------------------------------
            SectionHeader { text: qsTr("Camera") }
            TextSwitch { id: cameraSwitch; text: qsTr("Allow remote photo") }
            TextField {
                id: webdavUrlField
                width: parent.width
                label: qsTr("WebDAV URL for photo upload")
                inputMethodHints: Qt.ImhNoPredictiveText | Qt.ImhNoAutoUppercase
            }
            TextField {
                id: webdavUserField
                width: parent.width
                label: qsTr("WebDAV username")
                inputMethodHints: Qt.ImhNoPredictiveText | Qt.ImhNoAutoUppercase
            }
            TextField {
                id: webdavPassField
                width: parent.width
                label: qsTr("WebDAV password")
                echoMode: TextInput.Password
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
            TextSwitch { id: smsRemoteSwitch; text: qsTr("Remote control via SMS") }
            TextSwitch { id: smsGpsSwitch; text: qsTr("Send GPS coordinates via SMS") }

            // --- SMS two-factor (TOTP + backup codes) ------------------------
            SectionHeader { text: qsTr("SMS authentication (TOTP)") }
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSizeExtraSmall
                color: Theme.secondaryColor
                text: qsTr("Enrol this secret in an authenticator app (e.g. Aegis, "
                         + "Google Authenticator) on a SECOND device. The current "
                         + "code is required in SMS commands. Keep backup codes safe "
                         + "for use without an authenticator app.")
            }
            DetailItem { label: qsTr("TOTP secret"); value: dialog.totpSecretText }
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.WrapAnywhere
                visible: dialog.totpUriText !== ""
                font.pixelSize: Theme.fontSizeTiny
                color: Theme.secondaryHighlightColor
                text: dialog.totpUriText
            }
            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                text: qsTr("Generate new TOTP secret")
                onClicked: Bridge.call("rotate_totp_secret", [], function (r) {
                    if (r) { dialog.totpSecretText = r.secret; dialog.totpUriText = r.uri; }
                })
            }
            DetailItem { label: qsTr("Unused backup codes"); value: dialog.backupCountText }
            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                text: qsTr("Regenerate backup codes")
                onClicked: Bridge.call("regenerate_backup_codes", [], function (codes) {
                    if (codes) {
                        dialog.backupCountText = "" + codes.length;
                        pageStack.push(Qt.resolvedUrl("BackupCodesPage.qml"), { codes: codes });
                    }
                })
            }

            // --- map ---------------------------------------------------------
            SectionHeader { text: qsTr("Map") }
            ComboBox {
                id: providerCombo
                width: parent.width
                label: qsTr("Tile provider")
                menu: ContextMenu {
                    MenuItem { text: qsTr("OSM (no key needed)") }
                    MenuItem { text: qsTr("Geoapify") }
                }
            }
            TextField {
                id: geoapifyKeyField
                width: parent.width
                label: qsTr("Geoapify API key (optional)")
                inputMethodHints: Qt.ImhNoPredictiveText | Qt.ImhNoAutoUppercase
            }

            Item { width: 1; height: Theme.paddingLarge }
        }
    }
}
