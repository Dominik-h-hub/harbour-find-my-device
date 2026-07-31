# No compiled binaries ship (pure QML + Python), so debug info will be disabled.
%define debug_package %{nil}

# --- dependency filtering (Harbour FAQ 2.6.0) ------------------------------
# qml(QtMultimedia): Harbour rejects any declared dependency on QtMultimedia,
#   while the QML import itself (ringtone preview in SettingsPage.qml) is
#   allowed. The dependency generator derives it from that import on its own,
#   so filtering here is what keeps it out -- dropping the explicit Requires
#   alone would not. Anchored on QtMultimedia only: qml(QtPositioning) is on
#   the allowed list and has to survive.
# python3dist(...): derived from the imports in the bundled python sources.
#   Sailfish OS packages do not provide those virtual names (same exclusion as
#   in the daemon spec); the real dependency is pyotherside-qml-plugin-*, which
#   pulls in python3 including the sqlite3 stdlib module.
%global __requires_exclude ^(qml\\(QtMultimedia\\)|python3dist\\(.*\\))

# --- optional daemon bundling ----------------------------------------------
# Store/OpenRepos builds embed the separately built daemon RPM so the app can
# offer the sideload install flow from its Settings page. The CI builds the
# daemon RPM into daemon-rpm/RPMS/ first; if it is there, it gets bundled.
# On OBS (Chum) the directory does not exist -> no bundling, users install
# harbour-find-my-device-daemon from the same repository instead.
# Both candidate paths are probed because %%_sourcedir differs between build
# systems (mb2: <project>/rpm; plain rpmbuild in CI: project root).
%define daemon_rpm_file %(ls -1 %{_sourcedir}/../daemon-rpm/RPMS/harbour-find-my-device-daemon-*.noarch.rpm %{_sourcedir}/../daemon-rpm/RPMS/noarch/harbour-find-my-device-daemon-*.noarch.rpm %{_sourcedir}/daemon-rpm/RPMS/harbour-find-my-device-daemon-*.noarch.rpm %{_sourcedir}/daemon-rpm/RPMS/noarch/harbour-find-my-device-daemon-*.noarch.rpm 2>/dev/null | head -n1)

Name:       harbour-find-my-device
Summary:    Radar App (Find My Device)
Version:    2.0
Release:    1
# Own code is Apache-2.0. The BSD-3-Clause part covers the vendored
# qml/utilities/paho (EDL-1.0, which SPDX expresses as BSD-3-Clause) and
# qml/utilities/qrcode -- see NOTICE. Both ship inside this package, so the
# tag has to name them.
License:    Apache-2.0 AND BSD-3-Clause
URL:        https://github.com/Dominik-h-hub/harbour-find-my-device
Source0:    %{name}-%{version}.tar.bz2
Requires:   sailfishsilica-qt5 >= 0.10.9
Requires:   pyotherside-qml-plugin-python3-qt5
Requires:   libsailfishapp-launcher
Requires:   qt5-qtlocation
Requires:   qt5-plugin-geoservices-osm
# Foreground GPS fix (GpsSource.qml)
Requires:   qml(QtPositioning)
# No QtMultimedia dependency: Harbour does not allow it (see
# __requires_exclude above). The ringtone preview degrades gracefully if the
# gstreamer mediaservice plugin is missing -- it ships with the device image,
# but a bare emulator may need
# qt5-qtmultimedia-plugin-mediaservice-gstmediaplayer installed by hand.
BuildRequires:  pkgconfig(sailfishapp) >= 1.0.2
BuildRequires:  pkgconfig(Qt5Core)
BuildRequires:  pkgconfig(Qt5Qml)
BuildRequires:  pkgconfig(Qt5Quick)
BuildRequires:  desktop-file-utils

%description
Native Sailfish OS App "Find my Device". Tracks this and other
devices on an OpenStreetMap map via MQTT, and offers remote actions: RING /
LOCK / GPS / CAMERA / DELETE over MQTT (HMAC) and SMS (TOTP / backup codes).
Remote commands and background tracking while the app is closed require the
companion background service package (harbour-find-my-device-daemon),
installable from the app's Settings page.

%if 0%{?_chum}
Title: Radar (Find My Device)
Type: desktop-application
DeveloperName: DominikH
Categories:
 - Utility
 - System
Custom:
  Repo: https://github.com/Dominik-h-hub/harbour-find-my-device
PackageIcon: https://github.com/Dominik-h-hub/harbour-find-my-device/raw/main/icons/172x172/harbour-find-my-device.png
Screenshots:
 - https://github.com/Dominik-h-hub/harbour-find-my-device/raw/main/docs/images/map-view.png
 - https://github.com/Dominik-h-hub/harbour-find-my-device/raw/main/docs/images/devices-view.png
 - https://github.com/Dominik-h-hub/harbour-find-my-device/raw/main/docs/images/settings-view-1.png
Links:
  Homepage: https://github.com/Dominik-h-hub/harbour-find-my-device
  Help: https://forum.sailfishos.org/t/radar-app-find-my-device/30944
  Bugtracker: https://github.com/Dominik-h-hub/harbour-find-my-device/issues
%endif

%prep
%setup -q -n %{name}-%{version}

%build
%qmake5
%make_build

%install
%qmake5_install

# License files live inside the app's own share dir: Harbour forbids
# /usr/share/licenses (only /usr/share/%{name} is allowed for app data).
# cwd differs between build systems: mb2 runs %%install in the shadow build
# dir (files reachable via %%_sourcedir/..), OBS in the extracted source tree.
for f in LICENSE NOTICE; do
  if [ -f "$f" ]; then
    install -D -m 0644 "$f" %{buildroot}%{_datadir}/%{name}/$f
  else
    install -D -m 0644 %{_sourcedir}/../$f %{buildroot}%{_datadir}/%{name}/$f
  fi
done

# Version marker read by the app at runtime (Settings "App Version" row and
# the daemon update check). rpm(1) is not callable inside the sandbox.
echo "%{version}-%{release}" > %{buildroot}%{_datadir}/%{name}/VERSION

# Python files are modules loaded via PyOtherSide, never exec'd directly.
# Harbour requires them non-executable; this also drops the auto-generated
# /usr/bin/env and python3 shebang Requires.
find %{buildroot}%{_datadir}/%{name} -name '*.py' -exec chmod 0644 {} +
# Stray local caches must never ship.
find %{buildroot}%{_datadir}/%{name} -name '__pycache__' -type d -prune -exec rm -rf {} +

# Bundle the daemon RPM for the Settings sideload flow (see top of this spec).
%if "%{daemon_rpm_file}" != ""
install -d %{buildroot}%{_datadir}/%{name}/daemon
cp "%{daemon_rpm_file}" %{buildroot}%{_datadir}/%{name}/daemon/
%endif

desktop-file-install --delete-original \
        --dir %{buildroot}%{_datadir}/applications \
        %{buildroot}%{_datadir}/applications/*.desktop

%files
%defattr(-,root,root,-)
%license %{_datadir}/%{name}/LICENSE
%license %{_datadir}/%{name}/NOTICE
%{_datadir}/%{name}
%{_datadir}/applications/%{name}.desktop
%{_datadir}/icons/hicolor/*/apps/%{name}.png
