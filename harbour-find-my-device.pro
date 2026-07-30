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

# GUI package contents only. The background daemons, the privileged helper and
# all systemd units live in daemon-rpm/ (separate, non-Harbour RPM). Modules
# shared by both packages (fmd/, paho/, mqtt_client.py) have their single
# source here and are copied into the daemon package at build time by
# daemon-rpm/harbour-find-my-device-daemon.spec.
OTHER_FILES += qml/harbour-find-my-device.qml \
    qml/cover/CoverPage.qml \
    qml/components/qmldir \
    qml/components/Bridge.qml \
    qml/components/CommandButton.qml \
    qml/components/QrCode.qml \
    qml/components/GpsSource.qml \
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
    qml/utilities/net_watch.py \
    qml/utilities/fmd/__init__.py \
    qml/utilities/fmd/paths.py \
    qml/utilities/fmd/obfuscation.py \
    qml/utilities/fmd/db.py \
    qml/utilities/fmd/runtime.py \
    qml/utilities/fmd/settings.py \
    qml/utilities/fmd/devices.py \
    qml/utilities/fmd/tokens.py \
    qml/utilities/fmd/gpsstore.py \
    qml/utilities/paho/__init__.py \
    qml/utilities/paho/edl-v10 \
    qml/utilities/paho/epl-v20 \
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
    rpm/harbour-find-my-device.changes.in \
    rpm/harbour-find-my-device.changes.run.in \
    rpm/harbour-find-my-device.spec \
    translations/*.ts \
    harbour-find-my-device.desktop

SAILFISHAPP_ICONS = 86x86 108x108 128x128 172x172

# to disable building translations every time, comment out the
# following CONFIG line
CONFIG += sailfishapp_i18n

TRANSLATIONS += translations/harbour-find-my-device-*.ts
