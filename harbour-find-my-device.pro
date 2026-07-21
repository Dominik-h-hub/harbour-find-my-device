# NOTICE:
#
# Application name defined in TARGET has a corresponding QML filename.
# If name defined in TARGET is changed, the following needs to be done
# to match new name:
#   - corresponding QML filename must be changed
#   - desktop icon filename must be changed
#   - desktop filename must be changed
#   - icon definition filename in desktop file must be changed
#   - translation filenames have to be changed

# The name of your application
TARGET = harbour-find-my-device

CONFIG += sailfishapp_qml

SOURCES +=

OTHER_FILES += qml/harbour-find-my-device.qml \
    qml/cover/CoverPage.qml \
    qml/components/qmldir \
    qml/components/Bridge.qml \
    qml/components/CommandButton.qml \
    qml/components/QrCode.qml \
    qml/pages/MainPage.qml \
    qml/pages/MapView.qml \
    qml/pages/MapCanvas.qml \
    qml/pages/FullMapPage.qml \
    qml/pages/DevicesView.qml \
    qml/pages/SettingsPage.qml \
    qml/pages/AddDevicePage.qml \
    qml/pages/BackupCodesPage.qml \
    qml/pages/ConfirmDeletePage.qml \
    qml/utilities/api.py \
    qml/utilities/mqtt_client.py \
    qml/utilities/daemon_gps.py \
    qml/utilities/daemon_cmd.py \
    qml/utilities/notify.py \
    qml/utilities/ring_control.py \
    qml/utilities/gps_reader.py \
    qml/utilities/location_control.py \
    qml/utilities/lock_control.py \
    qml/utilities/camera_capture.py \
    qml/utilities/sms_command_listener.py \
    qml/utilities/sms_sender.py \
    qml/utilities/priv_client.py \
    qml/utilities/priv_service.py \
    qml/utilities/fmd/__init__.py \
    qml/utilities/fmd/paths.py \
    qml/utilities/fmd/obfuscation.py \
    qml/utilities/fmd/db.py \
    qml/utilities/fmd/settings.py \
    qml/utilities/fmd/devices.py \
    qml/utilities/fmd/tokens.py \
    qml/utilities/fmd/gpsstore.py \
    qml/utilities/paho/__init__.py \
    qml/utilities/paho/edl-v10 \
    qml/utilities/paho/LICENSE.txt \
    qml/utilities/paho/mqtt/__init__.py \
    qml/utilities/paho/mqtt/client.py \
    qml/utilities/paho/mqtt/matcher.py \
    qml/utilities/paho/mqtt/packettypes.py \
    qml/utilities/paho/mqtt/properties.py \
    qml/utilities/paho/mqtt/publish.py \
    qml/utilities/paho/mqtt/reasoncodes.py \
    qml/utilities/paho/mqtt/subscribe.py \
    qml/utilities/paho/mqtt/subscribeoptions.py \
    qml/utilities/qrcode/__init__.py \
    qml/utilities/qrcode/base.py \
    qml/utilities/qrcode/constants.py \
    qml/utilities/qrcode/exceptions.py \
    qml/utilities/qrcode/LICENSE \
    qml/utilities/qrcode/LUT.py \
    qml/utilities/qrcode/main.py \
    qml/utilities/qrcode/util.py \
    systemd/harbour-find-my-device-daemon-gps.service \
    systemd/harbour-find-my-device-daemon-cmd.service \
    systemd/harbour-find-my-device-priv.service \
    systemd/harbour-find-my-device-priv.path \
    systemd/tmpfiles-harbour-find-my-device.conf \
    rpm/harbour-find-my-device.changes.in \
    rpm/harbour-find-my-device.changes.run.in \
    rpm/harbour-find-my-device.spec \
    translations/*.ts \
    harbour-find-my-device.desktop

SAILFISHAPP_ICONS = 86x86 108x108 128x128 172x172

# --- background daemons + privileged action service (deployed via "make install")
# Done here (not in the .spec %install) because qmake uses source-relative paths;
# the spec %install runs in the shadow build dir where systemd/ does not exist.
# The two user daemons run as the user; the priv service is a root SYSTEM service
# triggered by the .path unit to perform the two root-only actions (reboot, send
# SMS) -- Sailfish has no sudo, so escalation goes through a spool it watches.
gps_unit.files = systemd/harbour-find-my-device-daemon-gps.service
gps_unit.path  = /usr/lib/systemd/user
cmd_unit.files = systemd/harbour-find-my-device-daemon-cmd.service
cmd_unit.path  = /usr/lib/systemd/user
priv_service_unit.files = systemd/harbour-find-my-device-priv.service
priv_service_unit.path  = /usr/lib/systemd/system
priv_path_unit.files = systemd/harbour-find-my-device-priv.path
priv_path_unit.path  = /usr/lib/systemd/system
tmpfiles_drop.files = systemd/tmpfiles-harbour-find-my-device.conf
tmpfiles_drop.path  = /usr/lib/tmpfiles.d
INSTALLS += gps_unit cmd_unit priv_service_unit priv_path_unit tmpfiles_drop

# to disable building translations every time, comment out the
# following CONFIG line
CONFIG += sailfishapp_i18n

TRANSLATIONS += translations/harbour-find-my-device-*.ts
