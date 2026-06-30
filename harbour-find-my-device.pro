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
    qml/utilities/gps_reader.py \
    qml/utilities/location_control.py \
    qml/utilities/lock_control.py \
    qml/utilities/camera_capture.py \
    qml/utilities/sms_command_listener.py \
    qml/utilities/sms_sender.py \
    qml/utilities/fmd/__init__.py \
    qml/utilities/fmd/paths.py \
    qml/utilities/fmd/obfuscation.py \
    qml/utilities/fmd/db.py \
    qml/utilities/fmd/settings.py \
    qml/utilities/fmd/devices.py \
    qml/utilities/fmd/tokens.py \
    qml/utilities/fmd/gpsstore.py \
    systemd/harbour-find-my-device-daemon-gps.service \
    systemd/harbour-find-my-device-daemon-cmd.service \
    systemd/harbour-find-my-device-priv-helper \
    systemd/sudoers-harbour-find-my-device \
    rpm/harbour-find-my-device.changes.in \
    rpm/harbour-find-my-device.changes.run.in \
    rpm/harbour-find-my-device.spec \
    translations/*.ts \
    harbour-find-my-device.desktop

SAILFISHAPP_ICONS = 86x86 108x108 128x128 172x172

# --- background daemons + privilege helper (deployed via "make install") ----
# Done here (not in the .spec %install) because qmake uses source-relative paths;
# the spec %install runs in the shadow build dir where systemd/ does not exist.
# Final file modes are fixed with %attr in the .spec %files section.
gps_unit.files = systemd/harbour-find-my-device-daemon-gps.service
gps_unit.path  = /usr/lib/systemd/user
cmd_unit.files = systemd/harbour-find-my-device-daemon-cmd.service
cmd_unit.path  = /usr/lib/systemd/user
priv_helper.files = systemd/harbour-find-my-device-priv-helper
priv_helper.path  = /usr/bin
sudoers_drop.files = systemd/sudoers-harbour-find-my-device
sudoers_drop.path  = /etc/sudoers.d
INSTALLS += gps_unit cmd_unit priv_helper sudoers_drop

# to disable building translations every time, comment out the
# following CONFIG line
CONFIG += sailfishapp_i18n

# German translation is enabled as an example. If you aren't
# planning to localize your app, remember to comment out the
# following TRANSLATIONS line. And also do not forget to
# modify the localized app name in the the .desktop file.
TRANSLATIONS += translations/harbour-find-my-device-de.ts
