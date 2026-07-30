# Radar App - Technical Infos

This document describes the technical internals of the Radar App (Find my Device) for developers and power users: the background daemons, how to read their logs on the device, the MQTT topics and payloads, and how the command authentication (HMAC token) works.

For a feature overview see the [README](../README.md), for the app usage see the [User Guide](USER-GUIDE.md).

## Architecture Overview

Since v1.2 the project ships as **two RPM packages**:

| Package | Content | Distribution |
|---|---|---|
| `harbour-find-my-device` | Sandboxed GUI app (Sailjail ON, stock permissions) | Jolla Store / OpenRepos / Chum |
| `harbour-find-my-device-daemon` | GPS daemon, command daemon, privileged root helper, systemd units | Bundled inside the app RPM (in-app sideload install), OpenRepos, Chum |

This split exists because Harbour (the Jolla Store QA) forbids systemd units,
RPM scriptlets and disabled sandboxing in store packages. The app is fully
usable without the daemon package (map, foreground tracking while open,
settings, TOTP); remote commands, SMS control and background tracking need the
daemon package, which the app offers to install from its Settings page
(Harbour-approved sideload flow: the bundled RPM is copied to `~/Downloads`
and opened with the system installer).

All parts share one SQLite database:

```text
+---------------------------+       +--------------------------------------+
| GUI app (Silica QML +     |       | GPS daemon (systemd user service)    |
| Python via PyOtherSide)   |       | periodic GPS fix -> DB -> MQTT       |
| SANDBOXED (Sailjail)      |       | unsandboxed, package: -daemon        |
+------------+--------------+       +------------------+-------------------+
             |                                         |
             v                                         v
   +----------------------------------------------------------------+
   | SQLite DB                                                      |
   | ~/.local/share/harbour-find-my-device/harbour-find-my-device/  |
   | (settings, devices, GPS fixes, daemon heartbeats, generation)  |
   +----------------------------------------------------------------+
             ^                                         ^
             |                                         |
+------------+--------------+       +------------------+-------------------+
| Command daemon (systemd   |       | Privileged action processor          |
| user service)             | ----> | (systemd SYSTEM service, root)       |
| MQTT + SMS remote control |       | reboot / send SMS / location switch  |
+---------------------------+       +--------------------------------------+
```

- The GUI is started via `sailfish-qml` (QML-only app, Python backend loaded
  through PyOtherSide) and runs **sandboxed** with the stock Sailjail
  permissions `Internet;Location;Audio;Downloads`. It never talks to systemd,
  never imports dbus/gi and never touches the privileged spool.
- GUI <-> daemon coordination happens exclusively through the shared DB:
  - the GUI bumps a **settings generation counter** after every save; the
    daemons poll it and reconfigure themselves,
  - the daemons write **heartbeats** (timestamp, active/idle state, package
    version) every 30 s; the Settings page derives the daemon status purely
    from these records,
  - privileged wishes of the GUI (e.g. "enable system location") are queued in
    the DB and forwarded by the command daemon to the root helper.
- Every feature is opt-in; a daemon idles (connections down, short poll timer)
  when its features are disabled - it is never started/stopped from outside.

## The Daemons

All daemon files are installed by the `harbour-find-my-device-daemon` package
under `/usr/share/harbour-find-my-device-daemon/`.

### GPS daemon - `harbour-find-my-device-daemon-gps.service`

`ExecStart: python3 /usr/share/harbour-find-my-device-daemon/daemon_gps.py`

- Runs as systemd USER service, permanently enabled; it actively polls GPS
  only while "Background activity" is switched on in the settings and idles
  otherwise.
- Periodically obtains a GPS fix (via geoclue on the D-Bus session), stores it in the SQLite DB and - if MQTT is enabled - publishes it retained to `fmd/<device-id>`.
- The poll interval is the "GPS query interval (minutes)" from the settings; while the background switch is off the daemon idles and the running GUI app polls instead via QtPositioning (positions keep being published until you close the app).
- Optionally switches the system location services on before a fix ("Auto-enable location when needed" setting, opt-in) - this goes through the privileged action processor.

### Command daemon - `harbour-find-my-device-daemon-cmd.service`

`ExecStart: python3 /usr/share/harbour-find-my-device-daemon/daemon_cmd.py`

- Runs as systemd USER service, permanently enabled; it listens only while at
  least one remote action or SMS action is switched on in the settings and
  idles otherwise. Settings changes are picked up automatically (generation
  counter), no restart needed.
- MQTT channel: subscribes `fmd/<own-id>/cmd`, verifies the HMAC token (see below), executes the command and publishes the result to `fmd/<own-id>/cmd/ack`.
- SMS channel: listens to incoming SMS via ofono (D-Bus). A command SMS must come from a whitelisted number AND carry a valid TOTP code or one-time backup code.
- Also forwards the sandboxed GUI's queued "enable system location" requests to the privileged helper.
- Every executed action - even a failed one - posts a notification on the device (this is not a spy app).

### Privileged action processor - `harbour-find-my-device-priv.service`

`ExecStart: python3 /usr/share/harbour-find-my-device-daemon/priv_service.py`

- Sailfish OS has no `sudo`, so the user daemons cannot escalate directly. Instead they drop a small JSON request file into the spool directory `/run/harbour-find-my-device/spool`; a systemd SYSTEM service (running as root) is started by a `.path` unit whenever the spool is non-empty.
- This is the whole privilege boundary - it can ONLY reboot the device, send an SMS (raw ofono) and toggle the system location switch. Every request file is deleted after processing.

### Managing the daemons manually

The daemons are enabled at install time (`systemctl --global enable`) and are
never started/stopped by the app - they steer themselves from the settings.
For debugging you can drive them by hand (as `defaultuser`):

```bash
systemctl --user status harbour-find-my-device-daemon-gps.service
systemctl --user restart harbour-find-my-device-daemon-cmd.service
systemctl --user stop harbour-find-my-device-daemon-gps.service
```

The settings page shows the state of both daemons (`running` / `idle` /
`not installed or not running`) derived purely from the heartbeat records in
the DB - a heartbeat younger than 90 s counts as alive.

## Reading Logs on the Device

The app and both daemons write logs to **rotating files under `/tmp/`** and additionally to **stdout** (visible in journalctl and the IDE debugger). The rotating files survive network changes and are easier to tail over SSH.

### Log Files (recommended for debugging)

SSH into the device (as `defaultuser` or `root`), then:

```bash
# Watch all three logs live (GPS daemon, command daemon, QML app)
tail -f /tmp/fmd-gps.log /tmp/fmd-cmd.log /tmp/fmd-app.log

# Or individually
tail -f /tmp/fmd-gps.log
tail -f /tmp/fmd-cmd.log
tail -f /tmp/fmd-app.log

# Read the last 50 lines
tail -n 50 /tmp/fmd-gps.log
```

Each log rotates at 1 MB (max. 3 backups: `.log`, `.log.1`, `.log.2`, `.log.3`).

### journalctl (alternative, requires root)

Sailfish OS keeps the journal volatile (in RAM, lost on reboot) and there is no separate user journal - everything lands in the system journal, readable as root only. SSH into the device, become root with `devel-su`, then:

```bash
# everything from the app + both daemons, live
journalctl -f | grep -i find-my-device

# one specific daemon (user units are filtered via this field, not via -u)
journalctl -f _SYSTEMD_USER_UNIT=harbour-find-my-device-daemon-gps.service
journalctl -f _SYSTEMD_USER_UNIT=harbour-find-my-device-daemon-cmd.service

# the root helper is a system unit, so -u works here
journalctl -f -u harbour-find-my-device-priv.service

# looking back instead of live: drop -f, e.g.
journalctl --since "30 min ago" --no-pager _SYSTEMD_USER_UNIT=harbour-find-my-device-daemon-gps.service
```

**Note:** journalctl may miss entries for user services on some SailfishOS versions - the log files are more reliable.

## Data Locations

| Path                                                       | Content                                    |
| ---------------------------------------------------------- | ------------------------------------------ |
| `~/.local/share/harbour-find-my-device/harbour-find-my-device/findmydevice.db` | SQLite DB: settings, devices, GPS fixes, daemon heartbeats |
| `~/.local/share/harbour-find-my-device/harbour-find-my-device/photos/` | camera captures before WebDAV upload |
| `/run/harbour-find-my-device/spool/`                       | privileged action request spool (volatile) |
| `/tmp/fmd-gps.log`, `/tmp/fmd-cmd.log`, `/tmp/fmd-app.log`| rotating log files (max 1 MB, 3 backups)  |
| `/usr/share/harbour-find-my-device/`                       | installed app files (QML + Python + bundled daemon RPM under `daemon/`) |
| `/usr/share/harbour-find-my-device-daemon/`                | installed daemon files (Python + VERSION)  |

The nested data directory is the Sailjail layout
(`~/.local/share/<OrganizationName>/<ApplicationName>/`): inside the sandbox it
is the app's real data dir, and the unsandboxed daemons use exactly the same
absolute path. A pre-1.2 DB in the old flat location
(`~/.local/share/harbour-find-my-device/findmydevice.db`) is migrated
automatically on first start.

## MQTT Topics

All traffic uses QoS 1. `<device-id>` is the id shown in the settings.

| Topic                     | Direction        | Retain | Payload                     |
| ------------------------- | ---------------- | ------ | --------------------------- |
| `fmd/<device-id>`         | device publishes | yes    | last known location         |
| `fmd/<device-id>/cmd`     | you publish      | no     | command `{cmd, arg, token}` |
| `fmd/<device-id>/cmd/ack` | device publishes | no     | result `{cmd, result}`      |

Because the location is retained, a subscriber immediately receives the last known position of every device on connect - that is how the app (and the example client) discovers devices via a single `fmd/#` subscription.

Location payload example:

```json
{
  "device_id": "IdqgCUotmY",
  "timestamp_utc": "2026-07-17T12:00:00Z",
  "timestamp_local": "2026-07-17T14:00:00+02:00",
  "lat": 52.520008,
  "lon": 13.404954,
  "alt": 40.0,
  "speed": 0.0,
  "accuracy": 12.0,
  "battery": 78
}
```

Command payload example (see the token section below):

```json
{"cmd": "CAMERA", "arg": "front", "token": "29dd05e89e5ac143"}
```

Ack payload example - `result` is one of `ok`, `disabled` (feature switched off on the device), `error`, `auth_failed` (wrong token/PIN):

```json
{"cmd": "CAMERA", "result": "ok"}
```

## Remote Command Reference

| Command     | `arg`             | Action                                                        |
| ----------- | ----------------- | ------------------------------------------------------------- |
| `RING`      | -                 | ring the device for 60 seconds                                |
| `STOP_RING` | -                 | stop a running ring                                           |
| `LOCK`      | -                 | lock the device into the lock screen                          |
| `GPS`       | -                 | one-off fix: store, publish via MQTT (or reply by SMS)        |
| `CAMERA`    | `front` or `back` | take a photo and upload it to the configured WebDAV folder    |
| `DELETE`    | -                 | wipe all user data and reboot - NOT a factory reset           |

## Command Authentication (HMAC Token)

MQTT commands are signed with a short one-time token derived from the target device's PIN. The token is bound to the command (a `LOCK` token cannot be replayed as `DELETE`) and only valid for a short time window:

- message: `"<CMD>:<arg>:<timebucket>"` - command uppercased, arg lowercased (empty string if none), `timebucket = unix-time // 30`
- token: `HMAC-SHA256(secret = PIN, message)`, hex, truncated to 16 chars
- the device accepts the current bucket ±1, so client and device clocks may drift up to ~30 seconds

Reference implementation (matches `qml/utilities/fmd/tokens.py`):

```python
import hashlib
import hmac
import time


def make_command_token(pin, cmd, arg=None):
    bucket = int(time.time() // 30)
    msg = "{}:{}:{}".format(cmd.upper(), (arg or "").lower(), bucket)
    return hmac.new(str(pin).encode(), msg.encode(),
                    hashlib.sha256).hexdigest()[:16]
```

Sending a command from the shell:

```bash
TOKEN=$(python3 -c "import hashlib,hmac,time;print(hmac.new(b'123456',('RING::%d'%(time.time()//30)).encode(),hashlib.sha256).hexdigest()[:16])")
mosquitto_pub -h your-broker.example.com -p 8883 --capath /etc/ssl/certs \
  -u mqttuser -P mqttpassword \
  -t "fmd/IdqgCUotmY/cmd" -m "{\"cmd\": \"RING\", \"token\": \"$TOKEN\"}"
```

## SMS Command Format

A command SMS is `KEYWORD [front|back] CODE`, e.g. `RING 123456` or `CAMERA front 123456`. The camera arg defaults to `back` if not given.

- The sender must be on the whitelist (numbers are compared on their last 9 digits, so national and international formats of the same number match).
- `CODE` is a 6-digit TOTP code (authenticator app enrolled on a second device) or a one-time backup code generated in the settings.
- SMS replies (for `GPS`) are sent via raw ofono and do NOT show up in the Messages app history - the notification on the device records them instead.

## Example Client

A minimal Flask client (interactive Leaflet map, device list, command buttons, HMAC token generation) lives under [examples/flask-client/](examples/flask-client/) - it demonstrates the full protocol above in ~150 lines of Python and is meant as a starting point for your own home client.

## Building

The app is built with the Sailfish SDK (`harbour-find-my-device.pro`, spec under `rpm/`). CI builds run on GitHub Actions using the [CODeRUS Sailfish OS Platform SDK docker images](https://github.com/CODeRUS/github-sfos-build) - see [.github/workflows/build.yaml](../.github/workflows/build.yaml).

### Versioning

The version is maintained **only** in `rpm/harbour-find-my-device.spec` (`Version:` / `Release:`); the daemon spec inherits it at build time and both packages write a `VERSION` file that app and daemons read at runtime. When building locally with the Sailfish SDK, disable the SDK's automatic git-describe versioning so the spec version applies: `sfdk config no-fix-version` (or `mb2 --no-fix-version`). Release tags must match the spec version (`v<Version>-<Release>-release`) - the release CI enforces this.

### Build order of the two packages

The noarch daemon RPM is built first (plain `rpmbuild`, no compilation), then each app build picks it up from `daemon-rpm/RPMS/` and embeds it under `/usr/share/harbour-find-my-device/daemon/`. The app RPM is gated by the official [sdk-harbour-rpmvalidator](https://github.com/sailfishos/sdk-harbour-rpmvalidator).
