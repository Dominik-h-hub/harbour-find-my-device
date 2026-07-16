# No compiled binaries ship (pure QML + Python), so debug info will be disabled.
%define debug_package %{nil}

Name:       harbour-find-my-device
Summary:    Radar App (Find My Device)
Version:    0.1
Release:    1
License:    Apache-2.0
URL:        https://openrepos.net/
Source0:    %{name}-%{version}.tar.bz2
Requires:   sailfishsilica-qt5 >= 0.10.9
Requires:   pyotherside-qml-plugin-python3-qt5
Requires:   libsailfishapp-launcher
Requires:   python3-base
Requires:   python3-dbus
Requires:   python3-gobject
Requires:   qt5-qtlocation
Requires:   qt5-qtdeclarative-import-location
Requires:   qt5-plugin-geoservices-osm
BuildRequires:  pkgconfig(sailfishapp) >= 1.0.2
BuildRequires:  pkgconfig(Qt5Core)
BuildRequires:  pkgconfig(Qt5Qml)
BuildRequires:  pkgconfig(Qt5Quick)
BuildRequires:  desktop-file-utils

%description
Native Sailfish OS App "Find my Device". Tracks this and other
devices on an OpenStreetMap map via MQTT, and offers remote actions: RING / LOCK / GPS /
CAMERA / DELETE over MQTT (HMAC) and SMS (TOTP / backup codes).

%prep
%setup -q -n %{name}-%{version}

%build
%qmake5
%make_build

%install
%qmake5_install

# License files live inside the app's own share dir: Harbour forbids
# /usr/share/licenses (only /usr/share/%{name} is allowed for app data).
install -D -m 0644 %{_sourcedir}/../LICENSE %{buildroot}%{_datadir}/%{name}/LICENSE
install -D -m 0644 %{_sourcedir}/../NOTICE %{buildroot}%{_datadir}/%{name}/NOTICE

# Python files are modules / started via "python3 <path>" (systemd units), never
# exec'd directly. Harbour requires them non-executable; this also drops the
# auto-generated /usr/bin/env and python3 shebang Requires.
find %{buildroot}%{_datadir}/%{name} -name '*.py' -exec chmod 0644 {} +

desktop-file-install --delete-original \
        --dir %{buildroot}%{_datadir}/applications \
        %{buildroot}%{_datadir}/applications/*.desktop

%post
# Enable both user services globally so they start in every user session at boot.
systemctl --global enable harbour-find-my-device-daemon-gps.service >/dev/null 2>&1 || :
systemctl --global enable harbour-find-my-device-daemon-cmd.service >/dev/null 2>&1 || :
# Create the priv-action spool now (also recreated each boot by tmpfiles) and
# enable+start the root path watcher that performs reboot / SMS on behalf of the
# (non-root) user daemon -- Sailfish has no sudo.
systemd-tmpfiles --create /usr/lib/tmpfiles.d/tmpfiles-harbour-find-my-device.conf >/dev/null 2>&1 || :
systemctl daemon-reload >/dev/null 2>&1 || :
systemctl enable --now harbour-find-my-device-priv.path >/dev/null 2>&1 || :

%preun
if [ "$1" = "0" ]; then
  systemctl --global disable harbour-find-my-device-daemon-gps.service >/dev/null 2>&1 || :
  systemctl --global disable harbour-find-my-device-daemon-cmd.service >/dev/null 2>&1 || :
  systemctl disable --now harbour-find-my-device-priv.path >/dev/null 2>&1 || :
fi

%files
%defattr(-,root,root,-)
%license %{_datadir}/%{name}/LICENSE
%license %{_datadir}/%{name}/NOTICE
%{_datadir}/%{name}
%{_datadir}/applications/%{name}.desktop
%{_datadir}/icons/hicolor/*/apps/%{name}.png
/usr/lib/systemd/user/harbour-find-my-device-daemon-gps.service
/usr/lib/systemd/user/harbour-find-my-device-daemon-cmd.service
/usr/lib/systemd/system/harbour-find-my-device-priv.service
/usr/lib/systemd/system/harbour-find-my-device-priv.path
/usr/lib/tmpfiles.d/tmpfiles-harbour-find-my-device.conf
