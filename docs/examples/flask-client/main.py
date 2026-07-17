#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""main.py -- Minimal Flask example client for Find My Device.

Shows every device publishing on fmd/# of your MQTT broker: an interactive
Leaflet/OpenStreetMap map with all positions on top, the device list with
hard-coded command buttons below. Reload via the Refresh button.

LOCALHOST ONLY: no auth, no CSRF, no HTTPS -- do not expose this app.

Run:  python main.py   then open  http://127.0.0.1:5000
"""

from flask import Flask, redirect, render_template, request, url_for

import config
import modules

app = Flask(__name__)


@app.route("/")
def index():
    devices = modules.all_devices()
    positioned = [d for d in devices if d.get("lat") is not None
                  and d.get("lon") is not None]
    return render_template("index.html", devices=devices,
                           positioned=positioned, has_fix=bool(positioned),
                           tile_url=config.TILE_URL)


@app.route("/cmd", methods=["POST"])
def cmd():
    modules.send_command(request.form["device_id"],
                         request.form["cmd"],
                         request.form.get("arg") or None)
    return redirect(url_for("index"))


if __name__ == "__main__":
    modules.start_mqtt()
    app.run(host=config.HTTP_HOST, port=config.HTTP_PORT)
