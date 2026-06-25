# No compiled binaries ship (pure QML + Python + shell helper), so debug info will be disabled.
%define debug_package %{nil}

Name:       harbour-find-my-device
Summary:    Find My Device for Sailfish OS
Version:    0.1
Release:    1
License:    EPL-2.0
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

desktop-file-install --delete-original \
        --dir %{buildroot}%{_datadir}/applications \
        %{buildroot}%{_datadir}/applications/*.desktop

%post
# Enable both user services globally so they start in every user session at boot.
systemctl --global enable harbour-find-my-device-daemon-gps.service >/dev/null 2>&1 || :
systemctl --global enable harbour-find-my-device-daemon-cmd.service >/dev/null 2>&1 || :

%preun
if [ "$1" = "0" ]; then
  systemctl --global disable harbour-find-my-device-daemon-gps.service >/dev/null 2>&1 || :
  systemctl --global disable harbour-find-my-device-daemon-cmd.service >/dev/null 2>&1 || :
fi

%files
%defattr(-,root,root,-)
%{_datadir}/%{name}
%{_datadir}/applications/%{name}.desktop
%{_datadir}/icons/hicolor/*/apps/%{name}.png
/usr/lib/systemd/user/harbour-find-my-device-daemon-gps.service
/usr/lib/systemd/user/harbour-find-my-device-daemon-cmd.service
%attr(0755,root,root) %{_bindir}/harbour-find-my-device-priv-helper
%attr(0440,root,root) %{_sysconfdir}/sudoers.d/sudoers-harbour-find-my-device
