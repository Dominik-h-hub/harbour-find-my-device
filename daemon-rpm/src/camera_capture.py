#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
camera_capture.py -- Headless capture for harbour-find-my-device (Sailfish OS).

KEY FINDINGS BAKED IN (see CAMERA_NOTES.md for the full story):
  * droidcamsrc (package: gstreamer1.0-droid) exposes THREE always-pads
    (vfsrc/imgsrc/vidsrc); ALL must be linked or the stream dies "not-linked".
  * The preview MUST stream continuously, otherwise takePicture() times out
    after ~10s with "error 0x1 from camera HAL". The fix: release gralloc
    buffers immediately -> fakesink enable-last-sample=false + leaky queues.
  * The still resolution must be set from image-capture-supported-caps via a
    capsfilter on the image branch.
  * imgsrc delivers image/jpeg directly -> filesink writes the JPEG (q=90).
  * 3A: flash OFF and focus INFINITY avoid headless AF/flash hangs.

USAGE (as a library):
    from camera_capture import capture_to_file, CAMERA_BACK
    res = capture_to_file(CAMERA_BACK, "/path/<device-id>_<ts>_back.jpg")
    if res.success: ...

USAGE (as a subprocess -- recommended from the daemon, see CAMERA_NOTES.md):
    python3 camera_capture.py --which back --device-id myphone --out-dir /tmp
    # prints a final machine-readable line:  RESULT success=1 path=... size=...
"""

import argparse
import collections
import logging
import os
import sys
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

log = logging.getLogger("fmd.camera")

# --- camera selection -------------------------------------------------------
CAMERA_BACK = "back"
CAMERA_FRONT = "front"
_DEVICE_INDEX = {CAMERA_BACK: 0, CAMERA_FRONT: 1}

# --- proven droidcamsrc settings (do not change without re-testing) ----------
MODE_IMAGE = 1            # GstCameraBin2Mode: still image
FLASH_OFF = 1            # GstPhotographyFlashMode
FLASH_AUTO = 0
FOCUS_INFINITY = 3       # GstPhotographyFocusMode: no blocking AF (default)
FOCUS_CONTINUOUS = 6     # better for faces; may hang headless -> test first

# Result returned to the caller (the daemon turns this into the cmd/ack reply).
CaptureResult = collections.namedtuple(
    "CaptureResult", "success path size error vf_frames")


def build_filename(device_id, which):
    """Filename per spec: <device-id>_<timestamp>_<front|back>.jpg"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    return "{}_{}_{}.jpg".format(device_id, ts, which)


def _max_dim(structure, field):
    """Max value of a caps field; never raises (handles int / list / range)."""
    try:
        ok, v = structure.get_int(field)
        if ok:
            return v
        val = structure.get_value(field)
        try:
            return max(int(x) for x in val)
        except Exception:
            pass
        for attr in ("stop", "high", "end"):
            if hasattr(val, attr):
                return int(getattr(val, attr))
    except Exception:
        pass
    return None


def _pick_capture_caps(supported, want_w, want_h):
    if want_w and want_h:
        return Gst.Caps.from_string(
            "image/jpeg,width={},height={}".format(want_w, want_h))
    if supported is None or supported.is_empty():
        return None
    best = None
    for i in range(supported.get_size()):
        s = supported.get_structure(i)
        w = _max_dim(s, "width")
        h = _max_dim(s, "height")
        if w and h and (best is None or w * h > best[1] * best[2]):
            best = (s.get_name(), w, h)
    return Gst.Caps.from_string("{},width={},height={}".format(*best)) if best else None


def _make_fakesink(name):
    s = Gst.ElementFactory.make("fakesink", name)
    s.set_property("sync", False)
    s.set_property("async", False)
    s.set_property("enable-last-sample", False)  # critical: return buffers now
    return s


def _make_leaky_queue(name):
    q = Gst.ElementFactory.make("queue", name)
    q.set_property("max-size-buffers", 2)
    q.set_property("leaky", 2)  # downstream
    return q


def _link_static(src_elem, pad_name, dst_elem):
    srcpad = src_elem.get_static_pad(pad_name)
    if srcpad is None:
        log.error("no static pad '%s' on droidcamsrc", pad_name)
        return False
    return srcpad.link(dst_elem.get_static_pad("sink")) == Gst.PadLinkReturn.OK


def capture_to_file(which=CAMERA_BACK, out_path=None,
                    focus_mode=FOCUS_INFINITY, flash_mode=FLASH_OFF,
                    width=0, height=0,
                    min_preview_frames=15, warmup_extra=1.0,
                    max_warmup=6.0, timeout=20.0):
    """Capture a single still photo headless. Blocking; runs its own GLib loop.

    Returns a CaptureResult. Does NOT raise on capture failure -- inspect
    result.success / result.error. Intended to be called from a worker thread
    or, preferably, a subprocess (GStreamer + GLib own their main loop).
    """
    if which not in _DEVICE_INDEX:
        return CaptureResult(False, out_path, 0, "invalid camera '%s'" % which, 0)
    if not out_path:
        return CaptureResult(False, None, 0, "out_path required", 0)

    Gst.init(None)  # idempotent
    camera_device = _DEVICE_INDEX[which]
    log.info("capture start: camera=%s (%d) -> %s", which, camera_device, out_path)

    pipeline = Gst.Pipeline.new("fmd-cam")
    src = Gst.ElementFactory.make("droidcamsrc", "src")
    if src is None:
        return CaptureResult(False, out_path, 0,
                             "droidcamsrc missing (install gstreamer1.0-droid)", 0)

    src.set_property("camera-device", camera_device)
    src.set_property("mode", MODE_IMAGE)
    try:
        src.set_property("flash-mode", flash_mode)
        src.set_property("focus-mode", focus_mode)
    except Exception as exc:
        log.warning("could not set 3A props: %s", exc)

    vf_queue = _make_leaky_queue("vf_queue")
    vf_sink = _make_fakesink("vf_sink")
    vid_queue = _make_leaky_queue("vid_queue")
    vid_sink = _make_fakesink("vid_sink")
    img_queue = Gst.ElementFactory.make("queue", "img_queue")  # never leak images
    img_caps = Gst.ElementFactory.make("capsfilter", "img_caps")
    filesink = Gst.ElementFactory.make("filesink", "img_sink")
    filesink.set_property("location", out_path)

    for el in [src, vf_queue, vf_sink, vid_queue, vid_sink, img_queue, img_caps, filesink]:
        pipeline.add(el)

    if not (vf_queue.link(vf_sink) and vid_queue.link(vid_sink)
            and img_queue.link(img_caps) and img_caps.link(filesink)):
        return CaptureResult(False, out_path, 0, "internal link failed", 0)

    if not (_link_static(src, "vfsrc", vf_queue)
            and _link_static(src, "imgsrc", img_queue)
            and _link_static(src, "vidsrc", vid_queue)):
        return CaptureResult(False, out_path, 0, "source pad link failed", 0)

    st = {"triggered": False, "captured": 0, "error": None,
          "ready_seen": False, "vf": 0, "ready_at": 0.0}
    loop = GLib.MainLoop()

    def on_vf_buffer(_pad, _info):
        st["vf"] += 1
        return Gst.PadProbeReturn.OK

    vfp = vf_queue.get_static_pad("src")
    if vfp:
        vfp.add_probe(Gst.PadProbeType.BUFFER, on_vf_buffer)

    def do_capture():
        if st["triggered"]:
            return False
        st["triggered"] = True
        log.info("triggering start-capture (preview frames=%d)", st["vf"])
        try:
            src.emit("start-capture")
        except TypeError:
            st["error"] = "start-capture signal unavailable"
            loop.quit()
        return False

    def maybe_capture():
        # Fire once the preview is genuinely streaming (+ a little settle time),
        # with a hard fallback so we never wait forever.
        if st["triggered"]:
            return False
        elapsed = time.monotonic() - st["ready_at"]
        if st["vf"] >= min_preview_frames and elapsed >= warmup_extra:
            do_capture()
            return False
        if elapsed >= max_warmup:
            log.warning("preview slow (frames=%d); capturing anyway", st["vf"])
            do_capture()
            return False
        return True  # keep polling

    def on_ready(_e, _p):
        if not (src.get_property("ready-for-capture") and not st["ready_seen"]):
            return
        st["ready_seen"] = True
        st["ready_at"] = time.monotonic()
        try:
            caps = _pick_capture_caps(
                src.get_property("image-capture-supported-caps"), width, height)
            if caps is not None:
                img_caps.set_property("caps", caps)
                log.info("capture resolution: %s", caps.to_string())
        except Exception as exc:
            log.warning("caps handling failed: %s", exc)
        GLib.timeout_add(200, maybe_capture)

    src.connect("notify::ready-for-capture", on_ready)

    def on_img_buffer(_pad, _info):
        st["captured"] += 1
        GLib.timeout_add(500, loop.quit)  # let filesink flush
        return Gst.PadProbeReturn.OK

    fpad = filesink.get_static_pad("sink")
    if fpad:
        fpad.add_probe(Gst.PadProbeType.BUFFER, on_img_buffer)

    def on_bus(_b, message):
        if message.type == Gst.MessageType.ERROR:
            err, _dbg = message.parse_error()
            st["error"] = err.message
            loop.quit()
        elif message.type == Gst.MessageType.EOS:
            loop.quit()
        return True

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_bus)

    def on_timeout():
        if not st["captured"]:
            st["error"] = st["error"] or "timeout (vf_frames=%d)" % st["vf"]
            loop.quit()
        return False

    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        pipeline.set_state(Gst.State.NULL)
        return CaptureResult(False, out_path, 0, "could not set PLAYING", 0)

    GLib.timeout_add(int(timeout * 1000), on_timeout)
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)

    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    success = (st["error"] is None) and st["captured"] > 0 and size > 0
    if success:
        log.info("capture OK: %s (%d bytes)", out_path, size)
    else:
        log.error("capture FAILED: %s", st["error"])
    return CaptureResult(success, out_path, size, st["error"], st["vf"])


def upload_webdav(local_path, url, username, password, timeout=30):
    """Upload a file to WebDAV via HTTP PUT (stdlib only, no extra deps).

    `url` should be the full target URL including the filename, e.g.
    https://dav.example.com/fmd/myphone_20260616_back.jpg
    Returns True on 2xx.
    """
    import base64
    import urllib.request

    with open(local_path, "rb") as fh:
        data = fh.read()
    req = urllib.request.Request(url, data=data, method="PUT")
    token = base64.b64encode("{}:{}".format(username, password).encode()).decode()
    req.add_header("Authorization", "Basic " + token)
    req.add_header("Content-Type", "image/jpeg")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
            log.info("webdav upload %s -> HTTP %s", url, resp.status)
            return ok
    except Exception as exc:
        log.error("webdav upload failed: %s", exc)
        return False


def _main(argv=None):
    ap = argparse.ArgumentParser(description="FMD headless camera capture.")
    ap.add_argument("--which", choices=[CAMERA_BACK, CAMERA_FRONT], default=CAMERA_BACK)
    ap.add_argument("--device-id", default="device")
    ap.add_argument("--out-dir", default="/tmp")
    ap.add_argument("--out", default=None, help="explicit path (overrides out-dir).")
    ap.add_argument("--focus-mode", type=int, default=FOCUS_INFINITY)
    ap.add_argument("--flash-mode", type=int, default=FLASH_OFF)
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=20.0)
    # Optional WebDAV upload after capture:
    ap.add_argument("--webdav-url", default=None, help="base URL (dir), file appended.")
    ap.add_argument("--webdav-user", default=None)
    ap.add_argument("--webdav-pass", default=None)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    fname = build_filename(args.device_id, args.which)
    out_path = args.out or os.path.join(args.out_dir, fname)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    res = capture_to_file(args.which, out_path,
                          focus_mode=args.focus_mode, flash_mode=args.flash_mode,
                          width=args.width, height=args.height, timeout=args.timeout)

    if res.success and args.webdav_url:
        dav_url = args.webdav_url.rstrip("/") + "/" + os.path.basename(out_path)
        upload_webdav(out_path, dav_url, args.webdav_user or "", args.webdav_pass or "")

    # Machine-readable final line for a subprocess caller (the daemon).
    print("RESULT success={} path={} size={} error={}".format(
        1 if res.success else 0, res.path, res.size, res.error))
    return 0 if res.success else 1


if __name__ == "__main__":
    sys.exit(_main())
