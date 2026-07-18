# Radar App - Technical Infos

This document describes the technical internals of the Radar App (Find my Device) for developers and power users: the background daemons, how to read their logs on the device, the MQTT topics and payloads, and how the command authentication (HMAC token) works.

For a feature overview see the [README](../README.md), for the app usage see the [User Guide](USER-GUIDE.md).

## Architecture Overview

The app consists of four parts which all share one SQLite database:

```text
+---------------------------+       +--------------------------------------+
| GUI app (Silica QML +     |       | GPS daemon (systemd user service)    |
| Python via PyOtherSide)   |       | periodic GPS fix -> DB -> MQTT       |
+------------+--------------+       +------------------+-------------------+
             |                                         |
             v                                         v
      +-------------------------------------------------------+
      | SQLite DB  ~/.local/share/harbour-find-my-device/     |
      +-------------------------------------------------------+
             ^                                         ^
             |                                         |
+------------+--------------+       +------------------+-------------------+
| Command daemon (systemd   |       | Privileged action processor          |
| user service)             | ----> | (systemd SYSTEM service, root)       |
| MQTT + SMS remote control |       | reboot / send SMS / location switch  |
+---------------------------+       +--------------------------------------+
```

- The GUI is started via `sailfish-qml` (QML-only app, Python backend loaded through PyOtherSide). Sailjail sandboxing is disabled because the app shares the SQLite DB with the unsandboxed daemons, starts/stops them via systemd and talks to the privileged root helper.
- The daemons read their switches from the DB - every feature is opt-in, a daemon idles (or is stopped) when its feature is disabled in the settings.

## The Daemons

### GPS daemon - `harbour-find-my-device-daemon-gps.service`

`ExecStart: python3 /usr/share/harbour-find-my-device/qml/utilities/daemon_gps.py`

- Runs as systemd USER service, enabled when you switch on "Background activity" in the settings.
- Periodically obtains a GPS fix (via geoclue on the D-Bus session), stores it in the SQLite DB and - if MQTT is enabled - publishes it retained to `fmd/<device-id>`.
- The poll interval is the "GPS query interval (minutes)" from the settings; while the background switch is off the daemon is stopped and the running GUI app polls instead (positions keep being published until you close the app).
- Optionally switches the system location services on before a fix ("Auto-enable location when needed" setting, opt-in) - this goes through the privileged action processor.

### Command daemon - `harbour-find-my-device-daemon-cmd.service`

`ExecStart: python3 /usr/share/harbour-find-my-device/qml/utilities/daemon_cmd.py`

- Runs as systemd USER service, enabled when at least one remote action or SMS action is switched on in the settings.
- MQTT channel: subscribes `fmd/<own-id>/cmd`, verifies the HMAC token (see below), executes the command and publishes the result to `fmd/<own-id>/cmd/ack`.
- SMS channel: listens to incoming SMS via ofono (D-Bus). A command SMS must come from a whitelisted number AND carry a valid TOTP code or one-time backup code.
- Every executed action - even a failed one - posts a notification on the device (this is not a spy app).

### Privileged action processor - `harbour-find-my-device-priv.service`

`ExecStart: python3 /usr/share/harbour-find-my-device/qml/utilities/priv_service.py`

- Sailfish OS has no `sudo`, so the user daemons cannot escalate directly. Instead they drop a small JSON request file into the spool directory `/run/harbour-find-my-device/spool`; a systemd SYSTEM service (running as root) is started by a `.path` unit whenever the spool is non-empty.
- This is the whole privilege boundary - it can ONLY reboot the device, send an SMS (raw ofono) and toggle the system location switch. Every request file is deleted after processing.

### Managing the daemons manually

The app starts/stops the user daemons itself whenever you save the settings. For debugging you can drive them by hand (as `defaultuser`):

```bash
systemctl --user status harbour-find-my-device-daemon-gps.service
systemctl --user restart harbour-find-my-device-daemon-cmd.service
systemctl --user stop harbour-find-my-device-daemon-gps.service
```

The settings page shows the live state of both daemons (running / deactivated / failed) straight from `systemctl --user is-active`.

## Reading Logs on the Device

Sailfish OS keeps the journal volatile (in RAM, lost on reboot) and there is no separate user journal - everything lands in the system journal, readable as root only. So: SSH into the device, become root with `devel-su`, then:

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

## Data Locations

| Path                                                       | Content                                    |
| ---------------------------------------------------------- | ------------------------------------------ |
| `~/.local/share/harbour-find-my-device/findmydevice.db`    | SQLite DB: settings, devices, GPS fixes    |
| `~/.local/share/harbour-find-my-device/photos/`            | camera captures before WebDAV upload       |
| `/run/harbour-find-my-device/spool/`                       | privileged action request spool (volatile) |
| `/usr/share/harbour-find-my-device/`                       | installed app files (QML + Python)         |

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
