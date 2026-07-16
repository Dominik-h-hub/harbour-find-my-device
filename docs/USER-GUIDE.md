# Radar App - User Guide

## Introduction
The Radar App (Find my Device) is a native Sailfish OS application that allows you to locate your device on a map and send commands to it remotely. The app provides various features for device tracking and management, ensuring security and convenience for users.

## Map View (Main Page)
The main page of the Radar App displays a map view where you can see the location of your device and other devices which you can add in the tab "Devices". 
The default map is NOT interactive. To get a scrollable map you can add your own Openstreetmap-tileserver key for a scrollable map (optional - free account at geoapify.com needed) within the settings.

### Pull-Down Menu
If you pull down the map view, you will see a menu with the following options:
- Settings: Opens the settings page where you can configure various options for the app.
- Update Map: Refreshes (also gets a new GPS fix for your device) the map view from database to show the latest location of your device and any other devices you have added.

<div class="row">
  <img src="images/map-view.png" alt="Map View" width="200">
</div>

## Devices Page
The devices page allows you to manage the devices you want to track. You can add, edit, unpair devices except of your own device.

You can do the following actions (if PIN was set on device-configuration):
- RING / STOP_RING: Makes the device ring for 60 seconds.
- LOCK: Locks the device into lock-screen.
- CAMERA: Takes a picture with the back camera of the device and uploads it to the preconfigured webdav upload folder. Works only if command enabled on other device.
- DELETE: Wipes the userdata from the device (/home/<defaultuser | nemo>) - This is NOT a factory reset. Afterwards, device will reboot and cannot be tracked anymore.

<div class="row">
  <img src="images/devices-view.png" alt="Devices View" width="200">
</div>

### Long-press context menu
If you long-press on a device in the list, you will see a context menu with the following options:
- Photo (Front Camera): Takes a picture with the front camera of the device and uploads it to the preconfigured webdav upload folder. Works only if command enabled on other device.
- Edit: Opens a dialog to edit the device's details.
- Unpair: Removes the device from your list.

<div class="row">
  <img src="images/devices-view-kontext-menu.png" alt="Devices View Context Menu" width="200">
</div>

### Pull-Down Menu
If you pull down the devices page, you will see a menu with the following options:
- Settings: Opens the settings page where you can configure various options for the app.
- Add Devices: Opens a dialog page to add a new device. Enter the device-id (Radar app needs to be installed on other device) and remote pin if you are allowed to send remote actions to the other device. Note: The other device needs to configure the same MQTT server as you did in the settings.
- Update: Refreshes the devices list from database and update "Last GPS FIX" time and date.

<div class="row">
  <img src="images/devices-view.png" alt="Devices View" width="200">
</div>


## Cover Actions