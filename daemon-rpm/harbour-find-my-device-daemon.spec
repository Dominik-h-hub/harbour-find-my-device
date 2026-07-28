# Background service package for harbour-find-my-device.
#
# NOT distributed via the Jolla Store (Harbour forbids daemons, root services
# and scriptlets). Distribution channels:
#   * bundled inside the Store app RPM and sideloaded from its Settings page
#   * OpenRepos / GitHub releases (direct install)
#   * SailfishOS:Chum (built from source on OBS, installed as a normal package)
#
# Pure python + unit files -> noarch, no compilation.

%define debug_package %{nil}
# Deterministic, SFOS-compatible payload regardless of which rpmbuild builds
# this (the CI builds the noarch package outside the Sailfish SDK).
%define _binary_payload w6.xzdio

# --- version inheritance (single source of truth = app spec) ---------------
# Version/Release are read from rpm/harbour-find-my-device.spec at parse time.
# Both candidate paths are probed because %%_sourcedir differs between build
# systems (mb2: <project>/rpm; CI rpmbuild: project root). On OBS neither path
# exists at parse time -- there tar_git's set_version service rewrites
# Version/Release from the release tag (kept identical to the spec version by
# the CI consistency check), so the 0-fallback never ships.
%define fmd_ver %(cat %{_sourcedir}/rpm/harbour-find-my-device.spec %{_sourcedir}/../rpm/harbour-find-my-device.spec 2>/dev/null | sed -n 's/^Version:[[:space:]]*//p' | head -n1)
%define fmd_rel %(cat %{_sourcedir}/rpm/harbour-find-my-device.spec %{_sourcedir}/../rpm/harbour-find-my-device.spec 2>/dev/null | sed -n 's/^Release:[[:space:]]*//p' | head -n1)
%if "%{fmd_ver}" == ""
%define fmd_ver 0
%endif
%if "%{fmd_rel}" == ""
%define fmd_rel 1
%endif

Name:       harbour-find-my-device-daemon
Summary:    Background services for Radar (Find My Device)
Version:    %{fmd_ver}
Release:    %{fmd_rel}
License:    Apache-2.0
URL:        https://github.com/Dominik-h-hub/harbour-find-my-device
Source0:    %{name}-%{version}.tar.bz2
BuildArch:  noarch
Requires:   python3-base
Requires:   python3-dbus
Requires:   python3-gobject

# The python files are modules / started via "python3 <path>" (systemd units),
# never exec'd directly; keep them non-executable and suppress shebang autodeps.
# Also drop the python3dist(...) requires the dependency generator derives from
# the sources -- Sailfish OS packages do not provide those virtual names; the
# real dependencies are declared explicitly above.
%global __requires_exclude ^(/usr/bin/env.*|python3dist\\(.*\\).*)$

%description
Background services for the Radar (Find My Device) Sailfish OS app:
the GPS tracking daemon, the MQTT/SMS remote command listener and the
privileged helper (reboot / SMS / location switch). Install this package
alongside the harbour-find-my-device app to receive remote commands and
report the device location while the app is closed.

%if 0%{?_chum}
Title: Radar (Find My Device) background services
Type: generic
DeveloperName: DominikH
Categories:
 - Utility
 - System
Custom:
  Repo: https://github.com/Dominik-h-hub/harbour-find-my-device
PackageIcon: https://github.com/Dominik-h-hub/harbour-find-my-device/raw/main/icons/172x172/harbour-find-my-device.png
%endif

%prep
%setup -q -n %{name}-%{version}

%build
# nothing to build: pure python + unit files

%install
D=%{buildroot}%{_datadir}/%{name}

# daemon-only modules
install -d "$D"
install -m 0644 daemon-rpm/src/*.py "$D"/

# modules shared with the GUI (single source in qml/utilities/, copied at
# build time -- deliberately shipped in both packages, see the refactoring spec)
cp -r qml/utilities/fmd "$D"/fmd
cp -r qml/utilities/paho "$D"/paho
install -m 0644 qml/utilities/mqtt_client.py "$D"/

# never ship caches, keep everything non-executable
find "$D" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$D" -name '*.py' -exec chmod 0644 {} +

# version marker: daemons report this in their DB heartbeat so the app can
# detect version mismatches and offer the update flow
echo "%{version}-%{release}" > "$D"/VERSION

# systemd units + tmpfiles
install -D -m 0644 daemon-rpm/systemd/harbour-find-my-device-daemon-gps.service \
    %{buildroot}/usr/lib/systemd/user/harbour-find-my-device-daemon-gps.service
install -D -m 0644 daemon-rpm/systemd/harbour-find-my-device-daemon-cmd.service \
    %{buildroot}/usr/lib/systemd/user/harbour-find-my-device-daemon-cmd.service
install -D -m 0644 daemon-rpm/systemd/harbour-find-my-device-priv.service \
    %{buildroot}/usr/lib/systemd/system/harbour-find-my-device-priv.service
install -D -m 0644 daemon-rpm/systemd/harbour-find-my-device-priv.path \
    %{buildroot}/usr/lib/systemd/system/harbour-find-my-device-priv.path
install -D -m 0644 daemon-rpm/systemd/tmpfiles-harbour-find-my-device.conf \
    %{buildroot}/usr/lib/tmpfiles.d/tmpfiles-harbour-find-my-device.conf

%post
# Enable both user services globally so they start in every user session at
# boot. They steer themselves via the shared DB (idle when features are off),
# so no per-feature start/stop from outside is ever needed.
systemctl --global enable harbour-find-my-device-daemon-gps.service >/dev/null 2>&1 || :
systemctl --global enable harbour-find-my-device-daemon-cmd.service >/dev/null 2>&1 || :

# The static tmpfiles conf hard-codes uid 100000 as fallback. Resolve the real
# primary user (defaultuser on current SFOS, nemo on older) and write a
# corrected copy to /etc/tmpfiles.d/ (same filename = overrides the /usr/lib
# one on every boot).
FMD_USER=defaultuser
getent passwd "$FMD_USER" >/dev/null 2>&1 || FMD_USER=nemo
FMD_UID=$(getent passwd "$FMD_USER" 2>/dev/null | cut -d: -f3)
FMD_GID=$(getent passwd "$FMD_USER" 2>/dev/null | cut -d: -f4)
if [ -n "$FMD_UID" ] && [ -n "$FMD_GID" ]; then
  sed "s/100000 100000/$FMD_UID $FMD_GID/" \
      /usr/lib/tmpfiles.d/tmpfiles-harbour-find-my-device.conf \
      > /etc/tmpfiles.d/tmpfiles-harbour-find-my-device.conf 2>/dev/null || :
fi

# Create the priv-action spool now (also recreated each boot by tmpfiles) and
# enable+start the root path watcher that performs reboot / SMS on behalf of
# the (non-root) user daemon -- Sailfish has no sudo.
systemd-tmpfiles --create tmpfiles-harbour-find-my-device.conf >/dev/null 2>&1 || :
systemctl daemon-reload >/dev/null 2>&1 || :
systemctl enable --now harbour-find-my-device-priv.path >/dev/null 2>&1 || :

# Best-effort: (re)start the daemons in the already running session of the
# primary user so no reboot/relogin is needed after installing from the app's
# sideload flow. `restart` (not `start`) + user daemon-reload on purpose: on
# upgrades -- including from the old monolithic app package, whose units had
# different ExecStart paths -- stale old daemon processes may still be running
# and must be replaced by the new code.
# systemd-run (transient unit as the target user) so this works from any
# scriptlet context (rpm/PackageKit). systemctl --user talks to the user
# manager via $XDG_RUNTIME_DIR/systemd/private -- that socket is also the
# "user session is running" guard (SFOS has no /run/user/<uid>/bus). If no
# session is running the units simply start at next boot.
if [ -n "$FMD_UID" ] && [ -S "/run/user/$FMD_UID/systemd/private" ]; then
  systemd-run --uid="$FMD_UID" --gid="$FMD_GID" \
    --setenv=XDG_RUNTIME_DIR=/run/user/"$FMD_UID" \
    /usr/bin/systemctl --user daemon-reload >/dev/null 2>&1 || :
  systemd-run --uid="$FMD_UID" --gid="$FMD_GID" \
    --setenv=XDG_RUNTIME_DIR=/run/user/"$FMD_UID" \
    /usr/bin/systemctl --user restart harbour-find-my-device-daemon-gps.service \
                                      harbour-find-my-device-daemon-cmd.service \
    >/dev/null 2>&1 || :
fi

%preun
if [ "$1" = "0" ]; then
  systemctl --global disable harbour-find-my-device-daemon-gps.service >/dev/null 2>&1 || :
  systemctl --global disable harbour-find-my-device-daemon-cmd.service >/dev/null 2>&1 || :
  systemctl disable --now harbour-find-my-device-priv.path >/dev/null 2>&1 || :
  # Stop running user instances best-effort (mirrors the %%post start logic;
  # systemd-run instead of su, see there).
  FMD_USER=defaultuser
  getent passwd "$FMD_USER" >/dev/null 2>&1 || FMD_USER=nemo
  FMD_UID=$(getent passwd "$FMD_USER" 2>/dev/null | cut -d: -f3)
  FMD_GID=$(getent passwd "$FMD_USER" 2>/dev/null | cut -d: -f4)
  if [ -n "$FMD_UID" ] && [ -S "/run/user/$FMD_UID/systemd/private" ]; then
    systemd-run --uid="$FMD_UID" --gid="$FMD_GID" \
      --setenv=XDG_RUNTIME_DIR=/run/user/"$FMD_UID" \
      /usr/bin/systemctl --user stop harbour-find-my-device-daemon-gps.service \
                                     harbour-find-my-device-daemon-cmd.service \
      >/dev/null 2>&1 || :
  fi
  rm -f /etc/tmpfiles.d/tmpfiles-harbour-find-my-device.conf
fi

%files
%defattr(-,root,root,-)
%{_datadir}/%{name}
/usr/lib/systemd/user/harbour-find-my-device-daemon-gps.service
/usr/lib/systemd/user/harbour-find-my-device-daemon-cmd.service
/usr/lib/systemd/system/harbour-find-my-device-priv.service
/usr/lib/systemd/system/harbour-find-my-device-priv.path
/usr/lib/tmpfiles.d/tmpfiles-harbour-find-my-device.conf
