# iCloud Find My → MQTT Bridge

A small Python daemon that reads the locations of your Apple devices (including your
Family Sharing group) from Apple iCloud "Find My" and publishes them to an MQTT broker.

The topics and payloads follow the format of the Jolla / Sailfish OS app
**harbour-find-my-device**, so Apple devices show up there (and in any other client speaking
the same protocol) just like a Jolla phone. Remote commands `RING` and `GPS` are supported and
authenticated with the same HMAC token scheme.

Built on [pyicloud](https://github.com/picklepete/pyicloud) and
[paho-mqtt](https://github.com/eclipse/paho.mqtt.python).

## Features

- Polls iCloud every `POLL_INTERVAL` seconds (reads the last known location, does not wake devices)
- Optional active locate every `LOCATE_INTERVAL` seconds (fresher position, costs battery)
- Publishes **retained** location messages to `fmd/<device_id>` (QoS 1), only when the location changed
- Handles commands on `fmd/<device_id>/cmd` and replies on `fmd/<device_id>/cmd/ack`
- New devices are picked up automatically and get a random `device_id` and PIN
- Per-device blacklist in `icloud_devices.json`, applied without restart
- Survives expired iCloud sessions: waits for a new `--login` instead of spamming 2FA prompts
- Status hook `report_status()` (`OK`, `2FA_REQUIRED`, `LOGIN_FAILED`, `ERROR`) for your own notification,
  with a commented-out [openHAB](https://www.openhab.org/) example

## Requirements

- Python **≥ 3.10** (required by pyicloud 2026.x)
- An Apple ID with **"Access iCloud Data on the Web"** enabled
  (Settings → [your name] → iCloud). Accounts that require a hardware security key (FIDO2)
  or use the legacy two-step verification (2SA) are not supported.
- An MQTT broker

## First login

```sh
./icloud_bridge.sh login     # uses the password from the config, then asks for the 2FA code
./icloud_bridge.sh list      # check which devices are found
./icloud_bridge.sh start
```

The session cookies are stored in `icloud_session/`. After the first poll, all iPhones/iPads are
written to `icloud_devices.json` with their generated `device_id` and `pin` (also logged).

Enter the `device_id` and PIN in your client app (e.g. add the device as a remote device in the
Jolla app) to view the location and send commands.

### Command line

| Command | Description |
|---|---|
| `python icloud_bridge.py` | run the daemon |
| `python icloud_bridge.py --login` | interactive login incl. 2FA |
| `python icloud_bridge.py --list` | list all devices in the account |
| `python icloud_bridge.py --list --raw` | … including all raw fields returned by iCloud |
| `python icloud_bridge.py --list --locate` | … after an active locate |

## Operation

| Task | How |
|---|---|
| Start / stop | `./icloud_bridge.sh start` / `stop` / `restart` |
| Log | `icloud_bridge.log` (rotated at 1 MB, 3 backups) |
| Disable a device | set `"enabled": false` in `icloud_devices.json`; applied on the next poll without restart, the retained location is removed from the broker |
| iCloud session expired | status `2FA_REQUIRED` (log / your notification) → run `./icloud_bridge.sh login`; the running bridge picks up the new session on the next poll |

For running it as a service, a simple systemd unit calling
`<venv>/bin/python /path/to/icloud_bridge.py` works fine as well (the daemon handles `SIGTERM`).

## Status notification

The bridge cannot renew an expired iCloud session on its own – you have to run `--login` and enter
a 2FA code. To get notified, add your own code to `report_status()` in `icloud_bridge.py`. It is
called whenever the status changes:

| Status | Meaning |
|---|---|
| `OK` | polling works |
| `2FA_REQUIRED` | session expired → run `./icloud_bridge.sh login` |
| `LOGIN_FAILED` | Apple ID or password rejected (retried every `SESSION_RECHECK` seconds) |
| `ERROR` | `ERROR_THRESHOLD` failed polls in a row (network, iCloud outage, …) |

By default the change is only logged. A commented-out example pushes the status to an openHAB item via
its REST API (uncomment it and set `URL_OPENHAB` in the config).

## Device file

`icloud_devices.json` is created and maintained by the bridge. It may be edited by hand
(`enabled`, `device_id`); manual changes are never overwritten.

```json
{
  "<apple-device-id>": {
    "device_id": "aB3dE5fG7h",
    "pin": "123456",
    "enabled": true,
    "name": "Jane's iPhone",
    "model": "iPhone 15",
    "first_seen": "2026-11-01T10:00:00Z"
  }
}
```

The `device_id` consists of exactly 10 characters `[A-Za-z0-9]` (as expected by the Jolla app),
the PIN of 6 digits.

## MQTT protocol

### Location – `fmd/<device_id>` (retained, QoS 1)

```json
{
  "device_id": "aB3dE5fG7h",
  "timestamp_utc": "2026-11-01T09:27:09Z",
  "timestamp_local": "2026-11-01T10:27:09+01:00",
  "lat": 48.137154,
  "lon": 11.576124,
  "alt": 0.0,
  "speed": 0.0,
  "accuracy": 35.0,
  "battery": 71,
  "source": "icloud",
  "name": "Jane's iPhone"
}
```

`battery` is `null` if iCloud does not report it. An empty retained message is published when a
device gets disabled, so the broker forgets its last location.

### Command – `fmd/<device_id>/cmd`

```json
{"cmd": "RING", "arg": null, "token": "0123456789abcdef"}
```

`token` = first 16 hex characters of `HMAC-SHA256(key=PIN, msg="CMD:arg:bucket")` where `CMD`
is upper case, `arg` lower case (empty string if none) and `bucket = floor(unix_time / 30)`.
The current bucket ±1 is accepted.

Example for a client:

```python
import hashlib, hmac, time

def make_token(pin, cmd, arg=None):
    bucket = int(time.time() // 30)
    msg = "{}:{}:{}".format(cmd.upper(), (arg or "").lower(), bucket).encode()
    return hmac.new(str(pin).encode(), msg, hashlib.sha256).hexdigest()[:16]
```

| Command | Action | Ack |
|---|---|---|
| `RING` | play a sound on the device | `ok` / `error` |
| `GPS` | active locate, fresh location is published after `GPS_FOLLOWUP` seconds | `ok` / `error` |
| `STOP_RING`, `LOCK`, `CAMERA`, `DELETE` | not supported by iCloud bridge | `disabled` |
| unknown command | – | `error` |
| invalid token | – | `auth_failed` |

Commands for unknown or disabled devices are ignored without an ack.

### Ack – `fmd/<device_id>/cmd/ack` (QoS 1, not retained)

```json
{"cmd": "RING", "result": "ok"}
```

## Security notes

- `bridge_config.py`, `icloud_session/` and `icloud_devices.json` contain credentials, session
  cookies and PINs. Never commit them (add them to your `.gitignore`) and `chmod 600` them.
- Anyone with access to the MQTT broker can read the locations. Use TLS.
- Every password login may trigger a 2FA prompt on all your Apple devices; the bridge therefore
  does not retry on its own while 2FA is pending.

## Disclaimer

This project is not affiliated with or endorsed by Apple Inc. It uses the unofficial iCloud web
API via pyicloud, which may change or break at any time. Use at your own risk.
