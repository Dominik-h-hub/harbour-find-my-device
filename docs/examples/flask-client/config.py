# -*- coding: utf-8 -*-
"""Configuration for the Find My Device example client.

Fill in your MQTT broker credentials and the PIN of every device you want to
send commands to. Locations are shown for ALL devices publishing on fmd/#,
commands only work for devices whose PIN is configured here.
"""

# --- MQTT broker (same broker the phones publish to) -----------------------
MQTT_SERVER = "your-broker.example.com"
MQTT_TLS = True
MQTT_PORT = 8883            # 8883 with TLS, usually 1883 without
MQTT_USERNAME = ""
MQTT_PASSWORD = ""

# --- device PINs -----------------------------------------------------------
# device_id -> PIN, as shown/configured in the app on each phone.
# The PIN signs the command token; without it a device is view-only.
DEVICE_PINS = {
    # "IdqgCdghr": "123456",
}

# --- map -------------------------------------------------------------------
# Tile server for the interactive Leaflet map. Tiles are loaded by the
# browser, which caches them; any {z}/{x}/{y} tile server works.
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# --- local web server ------------------------------------------------------
# Localhost only by design: the app has no auth/CSRF/HTTPS. Do not expose it.
HTTP_HOST = "127.0.0.1"
HTTP_PORT = 5000
