# -*- coding: utf-8 -*-

# Configuration file for the iCloud bridge.
# Copy this file and fill in your values.

### iCloud
ICLOUD_USER = "your.apple.id@example.com"
ICLOUD_PASSWORD = "your-apple-id-password"
ICLOUD_COOKIE_DIR = "./icloud_session"     # folder where session cookies are saved (created by --login)

### Polling
POLL_INTERVAL = 300          # seconds: how often the location already known to iCloud is read (does not wake the devices)
LOCATE_INTERVAL = 1800       # seconds: how often the devices are additionally located actively (wakes the devices, costs battery)
                             # 0 = never locate actively. Becomes due on the next poll -> best set to a multiple of POLL_INTERVAL
GPS_FOLLOWUP = 20            # seconds after an active locate (interval or GPS command) until the follow-up query
TIMEZONE = "Europe/Berlin"   # used for timestamp_local in the payload
DEVICE_CLASSES = ["iPhone", "iPad"]        # everything else (Mac, Watch, AirPods ...) is ignored

### Devices
DEVICES_FILE = "icloud_devices.json"
NEW_DEVICES_ENABLED = True   # new devices are active immediately; blacklist can be set via "enabled": false in the JSON file

### Status / error handling
ERROR_THRESHOLD = 5          # errors before 'ERROR' is reported
SESSION_RECHECK = 1800       # seconds between login attempts while 2FA is missing / login fails

### Notification (example only, see report_status() in icloud_bridge.py)
# URL_OPENHAB = "http://192.168.1.10:8080/rest/items/iCloudBridge_Status/state"

### MQTT
MQTT_SERVER = "mqtt.example.com"
MQTT_TLS = True
MQTT_PORT = 8883             # 8883 with TLS, usually 1883 without
MQTT_USERNAME = "mqtt_user"  # "" = no authentication
MQTT_PASSWORD = "mqtt_password"

### Logging
LOGFILE = "icloud_bridge.log"
