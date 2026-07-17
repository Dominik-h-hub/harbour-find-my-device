<!-- markdownlint-disable MD041 -->
<p align="center">
    <a href="https://github.com/Dominik-h-hub/harbour-find-my-device/actions/workflows/build.yaml"><img alt="GitHub Action" src="https://github.com/Dominik-h-hub/harbour-find-my-device/actions/workflows/build.yaml/badge.svg"></a>
    <br>
    <img alt="Logo" src="icons/172x172/harbour-find-my-device.png" width="100">
    <br>
    <b>Radar App for Sailfish OS </b><br>
    <b>(Find my Device)</b>
</p>

## Introduction

Radar (Find My Device) is a native find-my-device app for Sailfish OS: see your device's last position on a map and control it remotely via MQTT or SMS — fully self-hosted, no Google or cloud account involved.
The full documentation is available under [docs/](docs/).

<p align="center">
<a href="https://openrepos.net/content/domih/radar-find-my-device"><img src="docs/images/get-it-on-logos/get-it-on-openrepos.png" alt="Get it on OpenRepos" height="55"></a>
<a href="https://github.com/Dominik-h-hub/harbour-find-my-device/releases"><img src="docs/images/get-it-on-logos/direct-rpm-download.png" alt="Direct RPM Download" height="55"></a>
<!-- <a href="https://store.jolla.com"><img src="docs/images/get-it-on-logos/get-it-on-jollaStore.png" alt="Get it on the Jolla Store" height="55"></a> -->
</p>

## Features

- Locate your device on an OpenStreetMap map (optional free Geoapify key needed for a zoomable map)
- Track other Sailfish OS devices on the same map (Radar app installed + same MQTT broker)
- Publish your device's GPS position via MQTT (optional — use your own or a free public broker)
- Remote commands, via MQTT and/or SMS:
  - RING / STOP_RING — ring the device for 60 seconds
  - LOCK — lock the device into the lock screen
  - GPS — report the current position (published via MQTT, or replied by SMS — SMS costs may apply)
  - CAMERA — take a photo (front or back camera) and upload it to your WebDAV folder
  - DELETE — wipe all user data (`/home/<defaultuser | nemo>`) and reboot — NOT a factory reset
- Everything is opt-in: each command, MQTT, SMS and background tracking can be enabled/disabled individually in the settings

<img src="docs/images/map-view.png" alt="Main view" width=200px> <img src="docs/images/devices-view.png" alt="Devices view" width=200px> <img src="docs/images/settings-view-1.png" alt="Settings view" width=200px>

For more screenshots, see [docs/images/](docs/images/) in the GitHub repository.

## Example Client App

An example client app for home use (Flask + Leaflet map + command buttons) is available under [docs/examples/](docs/examples/).

## Security Information

This is not a spy app: every remote action — even a failed one — shows a notification on the device. All commands are disabled by default; every single command must be enabled in the settings, a disabled command is never executed.

- MQTT:
  - Commands require broker authentication (username/password) plus a one-time HMAC token derived from your PIN. Example: `{"cmd": "RING", "token": "29dd05e89e5ac143"}`
- SMS:
  - Commands are only accepted from whitelisted phone numbers (configured in the settings).
  - Each command SMS requires a TOTP code (authenticator app enrolled on a second device) or a one-time backup code, both generated in the settings. Example: `RING 123456`

## Technical Information

- Qt 5.6.3 (Sailfish OS Silica UI) + Python 3 backend/daemons
- Tested on:
  - Fairphone 4 - Sailfish OS 5.0.0.62
  - Emulator - Sailfish OS 5.0.0.62, 5.1.0.11

## Contributing to the project

We are happy about any contribution to the project, whether it's bug fixes, new features, translations or documentation.

## Localization

All language/regional translations are managed here [translations/*](translations/) in the GitHub repository.
If you want to contribute translations, please submit them as pull requests against the `translations/harbour-find-my-device-{language-code}.ts` files directly.

- Go to folder translations.
- If there is a file with your language code, click on it and select the edit icon
- If not:
  - Click on harbour-find-my-device.ts file
  - Select copy icon (Copy raw file)
  - Go back, click Add file -> Create new file
  - Enter harbour-find-my-device-xx.ts replacing xx with your language code as the name. For example, de for german
  - Paste the copied file in the new file's contents
- replace:

  ```xml
  <source>Save</source>
  <translation type="unfinished"></translation>
  ```

  with the correct translation for your language (remove "type="unfinished" and add the translation in between the <translation> tags). For example, for german:

  ```xml
  <source>Save</source>
  <translation>Speichern</translation>
  ```

Thanks for your consideration and contribution!

## License

This project is licensed under the Apache License 2.0 - see [LICENSE](LICENSE).

## Trademark Disclaimer

Sailfish OS and the Sailfish OS logo are trademarks of Jolla Group Ltd.
