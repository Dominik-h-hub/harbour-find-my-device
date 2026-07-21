# Find My Device – Flask example client

Minimal example of a home client for
[harbour-find-my-device](https://github.com/Dominik-h-hub/harbour-find-my-device):
a small Flask app that subscribes to `fmd/#` on your MQTT broker, shows all
devices on an interactive OpenStreetMap map (Leaflet) and offers the remote
commands as plain buttons. Bootstrap and Leaflet are loaded from their CDNs,
so the page needs an internet connection to look right.

**Localhost only.** The app has no authentication, no CSRF protection and no
HTTPS – it binds to `127.0.0.1` on purpose. Do not expose it to a network.

## Setup

```bash
cd docs/examples/flask-client
pip install -r requirements.txt   # flask, paho-mqtt
```

Edit `config.py`:

- `MQTT_*` – the same broker your phones publish to (hostname only, no
  `https://` prefix).
- `DEVICE_PINS` – `device_id -> PIN` for every device you want to command
  (the PIN signs the command token; devices without a PIN are view-only).
- `TILE_URL` – map tile server, any `{z}/{x}/{y}` scheme works. Tiles are
  loaded and cached by the browser (the normal, policy-friendly way to use
  OSM tiles).

## Run

```bash
python main.py
```

Open <http://127.0.0.1:5000>. Because locations are published retained, the
last known position of every device appears right after connecting. Use the
Refresh button to reload positions and command acks.

## How it works

| File                   | Purpose                                              |
| ---------------------- | ---------------------------------------------------- |
| `main.py`              | Flask routes: page and command POST                  |
| `modules.py`           | MQTT subscribe/publish, device store, HMAC token     |
| `config.py`            | broker credentials, device PINs, tile server         |
| `templates/index.html` | the one and only page (Leaflet map, Bootstrap style) |

Protocol in short (matches the app's `qml/utilities/mqtt_client.py` and
`qml/utilities/fmd/tokens.py`):

- `fmd/<device-id>` – location JSON, retained, QoS 1
- `fmd/<device-id>/cmd` – command JSON `{"cmd", "arg", "token"}`, QoS 1
- `fmd/<device-id>/cmd/ack` – result JSON `{"cmd", "result"}`, QoS 1

Commands: `RING`, `STOP_RING`, `LOCK`, `GPS`, `CAMERA` (arg `front`/`back`),
`DELETE`. The token is `HMAC-SHA256(secret=PIN, "CMD:arg:timebucket")`
truncated to 16 hex chars, with 30-second time buckets (the phone accepts
±1 bucket, so tolerate up to ~30 s clock drift between client and phone).
