#!/usr/bin/env python3
"""
testsrc.py - stream a synthetic test pattern with the real daemon code.

Exercises the exact wire protocol, broadcaster, heartbeats and GOP replay of
extenderd.py, but the video comes from GStreamer's videotestsrc instead of a
virtual monitor — so you can test the phone app (or the emulator, which
reaches this PC at 10.0.2.2) without touching your desktop layout.

    python3 testsrc.py [--width 2400 --height 1080 --fps 60 --port 5000]
    python3 testsrc.py --static      # one frame then silence: tests GOP replay

Needs gst-launch-1.0 with x264enc or openh264enc (see backends/gnome.py).
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extenderd  # noqa: E402
from backends import gnome  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--pattern", default="smpte",
                    help="videotestsrc pattern (smpte, ball, bar, …)")
    ap.add_argument("--static", action="store_true",
                    help="send 2 seconds of video, then keep the stream idle")
    args = ap.parse_args()

    enc = gnome.pick_encoder()
    if not enc:
        sys.exit("no GStreamer H.264 encoder (install gst-plugins-ugly or openh264)")
    r, w = os.pipe()
    frames = f"num-buffers={args.fps * 2} " if args.static else ""
    desc = (f"videotestsrc is-live=true {frames}pattern={args.pattern} "
            f"! video/x-raw,width={args.width},height={args.height},"
            f"framerate={args.fps}/1 "
            f"! timeoverlay FONT "
            f"! {gnome.encoder_chain(enc, 8000, 30)} "
            f"! h264parse config-interval=-1 "
            f"! video/x-h264,stream-format=byte-stream,alignment=au "
            f"! fdsink fd={w} sync=false")
    # gst-launch joins argv with spaces and re-parses, so a quoted value with a
    # space must travel as one argv element.
    cmd = ["gst-launch-1.0", "-q"] + [
        'font-desc="Sans 40"' if part == "FONT" else part for part in desc.split()]
    if args.static:
        # keep the pipe open after gst exits so the stream goes quiet, not EOF
        _keep_open = os.dup(w)  # noqa: F841
    proc = subprocess.Popen(cmd, pass_fds=(w,), stdin=subprocess.DEVNULL)
    os.close(w)

    banner = json.dumps({
        "side": "right", "width": args.width, "height": args.height,
        "fps": args.fps, "scale": 1.0, "quality": "test", "bitrate": "8M",
        "codec": "h264", "gop": 30, "host": socket.gethostname(),
        "backend": "testsrc"}).encode()
    b = extenderd.Broadcaster(banner)
    stop = threading.Event()
    threading.Thread(target=extenderd.accept_loop,
                     args=("0.0.0.0", args.port, b, stop), daemon=True).start()
    extenderd.log(f"test pattern {args.width}x{args.height}@{args.fps} via {enc}"
                  f"{' (static)' if args.static else ''}; Ctrl+C to stop")
    def shutdown(*_):
        stop.set()
        proc.terminate()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        extenderd.parse_annexb(os.fdopen(r, "rb", buffering=0), b, stop)
    finally:
        stop.set()
        proc.terminate()
        b.close_all()


if __name__ == "__main__":
    main()
