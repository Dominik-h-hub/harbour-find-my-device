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

# Captures done by this process -- tells whether failures only start after an
# earlier capture in the same (long-running) daemon process.
_capture_count = 0


def _dbus_call(dest, path, iface, method):
    import dbus
    obj = dbus.SystemBus().get_object(dest, path)
    return getattr(dbus.Interface(obj, iface), method)()


def _camera_processes():
    """Names of running processes that look like camera users."""
    found = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == os.getpid():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as fh:
                    argv0 = fh.read().split(b"\0", 1)[0].decode("utf-8", "replace")
            except OSError:
                continue
            name = os.path.basename(argv0)
            if "camera" in name.lower() or name == "minimediaservice":
                found.append("%s[%s]" % (name, pid))
    except Exception as exc:
        return ["unknown (%s)" % exc]
    return found


def capture_environment():
    """One-line description of the device state relevant for camera access."""
    parts = []
    for label, method in (("display", "get_display_status"),
                          ("tklock", "get_tklock_mode")):
        try:
            parts.append("%s=%s" % (label, _dbus_call(
                "com.nokia.mce", "/com/nokia/mce/request",
                "com.nokia.mce.request", method)))
        except Exception as exc:
            parts.append("%s=? (%s)" % (label, exc.__class__.__name__))
    try:
        parts.append("devicelock=%s" % int(_dbus_call(
            "org.nemomobile.devicelock", "/devicelock",
            "org.nemomobile.lipstick.devicelock", "state")))
    except Exception as exc:
        parts.append("devicelock=? (%s)" % exc.__class__.__name__)
    parts.append("camera_procs=%s" % (",".join(_camera_processes()) or "none"))
    return " ".join(parts)


def _drain_bus_errors(bus):
    """Pop queued ERROR/WARNING messages from the bus as readable strings."""
    msgs = []
    while True:
        msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.WARNING)
        if msg is None:
            return msgs
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            kind = "ERROR"
        else:
            err, dbg = msg.parse_warning()
            kind = "WARNING"
        src_name = msg.src.get_name() if msg.src is not None else "?"
        msgs.append("%s from %s: %s (%s)" % (kind, src_name, err.message, dbg))


def _start_pipeline(pipeline, bus):
    """Walk NULL -> READY -> PAUSED -> PLAYING one step at a time, logging each
    result. READY is where droidcamsrc opens the camera HAL. Returns None on
    success or an error string carrying the GStreamer bus messages.

    The bus must not have a signal watch yet, otherwise the messages explaining
    a failure would be dispatched elsewhere before we can read them."""
    for target in (Gst.State.READY, Gst.State.PAUSED, Gst.State.PLAYING):
        t = time.monotonic()
        ret = pipeline.set_state(target)
        log.info("set_state %s -> %s (%.2fs)", target.value_nick,
                 ret.value_nick, time.monotonic() - t)
        if ret == Gst.StateChangeReturn.FAILURE:
            details = _drain_bus_errors(bus)
            for d in details:
                log.error("gst: %s", d)
            return "could not set %s: %s" % (
                target.value_nick.upper(),
                "; ".join(details) or "no bus message")
    for d in _drain_bus_errors(bus):
        log.warning("gst: %s", d)
    return None


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

    global _capture_count
    Gst.init(None)  # idempotent
    camera_device = _DEVICE_INDEX[which]
    _capture_count += 1
    t0 = time.monotonic()
    log.info("capture start: camera=%s (%d) -> %s", which, camera_device, out_path)
    log.info("capture #%d in pid %d, %s, %s", _capture_count, os.getpid(),
             Gst.version_string(), capture_environment())

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
        log.info("ready-for-capture after %.2fs (preview frames=%d)",
                 st["ready_at"] - t0, st["vf"])
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

    def on_img_buffer(_pad, info):
        st["captured"] += 1
        buf = info.get_buffer()
        log.info("image buffer received after %.2fs (%d bytes)",
                 time.monotonic() - t0, buf.get_size() if buf else -1)
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

    def on_timeout():
        if not st["captured"]:
            st["error"] = st["error"] or "timeout (vf_frames=%d)" % st["vf"]
            loop.quit()
        return False

    bus = pipeline.get_bus()
    start_error = _start_pipeline(pipeline, bus)
    if start_error:
        pipeline.set_state(Gst.State.NULL)
        log.error("capture FAILED after %.2fs: %s", time.monotonic() - t0,
                  start_error)
        return CaptureResult(False, out_path, 0, start_error, 0)
    # Only now: messages posted meanwhile stay queued and are delivered here.
    bus.add_signal_watch()
    bus.connect("message", on_bus)

    GLib.timeout_add(int(timeout * 1000), on_timeout)
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
        bus.remove_signal_watch()

    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    success = (st["error"] is None) and st["captured"] > 0 and size > 0
    elapsed = time.monotonic() - t0
    if success:
        log.info("capture OK after %.2fs: %s (%d bytes, preview frames=%d)",
                 elapsed, out_path, size, st["vf"])
    else:
        log.error("capture FAILED after %.2fs: %s (triggered=%s, image buffers=%d, "
                  "file size=%d, preview frames=%d)", elapsed, st["error"],
                  st["triggered"], st["captured"], size, st["vf"])
    return CaptureResult(success, out_path, size, st["error"], st["vf"])


def upload_webdav(local_path, url, username, password, timeout=30):
    """Upload a file to WebDAV via HTTP PUT (stdlib only, no extra deps).

    `url` should be the full target URL including the filename, e.g.
    https://dav.example.com/fmd/myphone_20260616_back.jpg
    Returns True on 2xx.
    """
    import base64
    import urllib.error
    import urllib.request

    safe_url = _safe_url(url)
    with open(local_path, "rb") as fh:
        data = fh.read()
    req = urllib.request.Request(url, data=data, method="PUT")
    token = base64.b64encode("{}:{}".format(username, password).encode()).decode()
    req.add_header("Authorization", "Basic " + token)
    req.add_header("Content-Type", "image/jpeg")
    log.info("webdav upload start: %s (%d bytes, timeout %ss)",
             safe_url, len(data), timeout)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
            log.info("webdav upload %s -> HTTP %s %s (%.2fs)", safe_url,
                     resp.status, resp.reason, time.monotonic() - t0)
            return ok
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(200).decode("utf-8", "replace")
        except Exception:
            body = "?"
        log.error("webdav upload %s failed after %.2fs: HTTP %s %s, body: %r",
                  safe_url, time.monotonic() - t0, exc.code, exc.reason, body)
        return False
    except urllib.error.URLError as exc:
        log.error("webdav upload %s failed after %.2fs: URLError %r",
                  safe_url, time.monotonic() - t0, exc.reason)
        return False
    except Exception as exc:
        log.error("webdav upload %s failed after %.2fs: %s: %s", safe_url,
                  time.monotonic() - t0, exc.__class__.__name__, exc)
        return False


def _safe_url(url):
    """URL for logging: scheme://host[:port]/path, never user:pass@ or query."""
    import urllib.parse
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = "%s:%d" % (host, parts.port)
        return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))
    except Exception:
        return "<unparsable url>"


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
