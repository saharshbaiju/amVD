#!/usr/bin/env python3
"""
extenderd - Arch Screen Extender daemon (Hyprland / wlroots)

Pipeline:
    Hyprland headless output (EXT-1, GPU-backed secondary monitor)
        -> wf-recorder  (wlr-screencopy capture + VA-API H.264 encode, zero-copy on the iGPU)
        -> raw Annex-B H.264 on stdout (no container, no B-frames)
        -> this daemon parses NAL units, caches SPS/PPS, and broadcasts over TCP
        -> phone connects, feeds NALs straight into MediaCodec -> Surface

Design goals (why this beats the old mpv-over-mpegts approach):
  * No container: clean Annex-B start codes the phone can split trivially.
  * No jitter buffer anywhere: each NAL is forwarded the instant it's parsed.
  * Latency can't creep: if a client's socket backs up, we drop everything
    until the next keyframe instead of queuing a growing backlog.
  * Fast join: SPS/PPS are cached and re-sent to every new client, and a client
    only starts streaming at an IDR, so the picture is correct from frame one.

Transport is plain TCP, which works identically over:
  * USB  -> `adb reverse tcp:<port> tcp:<port>`  then phone connects to 127.0.0.1
  * WiFi -> phone connects to this machine's LAN IP

This is the low-latency BASE. RTP/FEC over UDP for lossy WiFi comes later.
"""

import argparse
import os
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

# H.264 NAL unit types (nal_unit_header & 0x1F)
NAL_SPS = 7
NAL_PPS = 8
NAL_IDR = 5          # keyframe slice
NAL_NON_IDR = 1      # P/B slice


def log(*a):
    print("[extenderd]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Hyprland headless output management
# --------------------------------------------------------------------------- #

def hyprctl(*args):
    return subprocess.run(["hyprctl", *args],
                          capture_output=True, text=True)


def ensure_output(name, width, height, fps, scale):
    """Create the headless output if missing and apply the requested mode."""
    mons = hyprctl("monitors").stdout
    if f"Monitor {name} " not in mons:
        log(f"creating headless output {name}")
        hyprctl("output", "create", "headless", name)
        time.sleep(0.5)
    mode = f"{width}x{height}@{fps}"
    pos = "auto"  # let Hyprland place it; user can re-place with hyprctl later
    rule = f"{name},{mode},{pos},{scale}"
    r = hyprctl("keyword", "monitor", rule)
    if r.returncode != 0:
        log(f"warning: could not apply mode {mode} ({r.stderr.strip()}); "
            f"using whatever mode the output reports")
    log(f"output {name} ready ({mode}, scale {scale})")


def remove_output(name):
    hyprctl("output", "remove", name)


# --------------------------------------------------------------------------- #
# Capture / encode process
# --------------------------------------------------------------------------- #

def build_wf_cmd(output, device, bitrate, gop, fps):
    """wf-recorder -> raw Annex-B H.264 on stdout, tuned for low latency."""
    return [
        "wf-recorder",
        "-o", output,
        "-c", "h264_vaapi",
        "-d", device,
        "-p", "bf=0",                 # no B-frames -> no reorder delay
        "-p", f"g={gop}",             # keyframe interval (fast join + recovery)
        "-p", "rc_mode=CBR",
        "-p", f"b={bitrate}",
        "-p", "async_depth=1",        # minimal encoder pipelining
        "-r", str(fps),
        "--muxer=h264",               # raw Annex-B, no container
        "-f", "/dev/stdout",
    ]


# --------------------------------------------------------------------------- #
# NAL broadcaster
# --------------------------------------------------------------------------- #

class Client:
    __slots__ = ("sock", "addr", "started", "alive")

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.started = False   # becomes True once we've sent an IDR
        self.alive = True


class Broadcaster:
    """Owns the client set; forwards NAL units with no buffering."""

    def __init__(self):
        self._clients = []
        self._lock = threading.Lock()
        self.sps = None
        self.pps = None

    def add(self, sock, addr):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self._lock:
            self._clients.append(Client(sock, addr))
        log(f"client connected: {addr} (total {len(self._clients)})")

    def _drop(self, c):
        c.alive = False
        try:
            c.sock.close()
        except OSError:
            pass

    def _send(self, c, data):
        """Blocking send; on failure the client is marked dead."""
        try:
            c.sock.sendall(data)
            return True
        except OSError:
            self._drop(c)
            return False

    def feed(self, nal_type, nal_bytes):
        """Called per NAL by the parser thread."""
        if nal_type == NAL_SPS:
            self.sps = nal_bytes
        elif nal_type == NAL_PPS:
            self.pps = nal_bytes

        is_idr = nal_type == NAL_IDR

        with self._lock:
            if not self._clients:
                return
            survivors = []
            for c in self._clients:
                if not c.alive:
                    continue
                if not c.started:
                    # A fresh client must begin on a keyframe, prefixed with
                    # the cached parameter sets, so the decoder is valid at once.
                    if is_idr and self.sps and self.pps:
                        if (self._send(c, self.sps)
                                and self._send(c, self.pps)
                                and self._send(c, nal_bytes)):
                            c.started = True
                            log(f"client {c.addr} started on keyframe")
                        else:
                            continue
                    # else: skip non-keyframe NALs until the first IDR
                else:
                    if not self._send(c, nal_bytes):
                        continue
                if c.alive:
                    survivors.append(c)
            self._clients = survivors


# --------------------------------------------------------------------------- #
# Annex-B parser: split wf-recorder stdout into NAL units
# --------------------------------------------------------------------------- #

def parse_annexb(stream, broadcaster, stop_evt):
    """
    Read the raw H.264 byte stream and emit one (type, bytes-with-startcode)
    per NAL unit. Start codes are 00 00 01 or 00 00 00 01.
    """
    buf = bytearray()
    READ = 65536
    while not stop_evt.is_set():
        chunk = stream.read(READ)
        if not chunk:
            break
        buf += chunk

        # Find start-code positions, emit every complete NAL (i.e. up to the
        # byte before the next start code). Keep the trailing partial NAL.
        starts = _find_start_codes(buf)
        if len(starts) < 2:
            continue
        # Emit NALs between consecutive start codes; retain the last one
        # (it may still be growing).
        for i in range(len(starts) - 1):
            s = starts[i]
            e = starts[i + 1]
            nal = bytes(buf[s:e])
            _emit(nal, broadcaster)
        # Drop everything before the last start code.
        last = starts[-1]
        del buf[:last]


def _find_start_codes(buf):
    """Return indices where an Annex-B start code begins."""
    positions = []
    i = 0
    n = len(buf)
    while i < n - 3:
        if buf[i] == 0 and buf[i + 1] == 0:
            if buf[i + 2] == 1:
                positions.append(i)
                i += 3
                continue
            if buf[i + 2] == 0 and i + 3 < n and buf[i + 3] == 1:
                positions.append(i)
                i += 4
                continue
        i += 1
    return positions


def _emit(nal, broadcaster):
    # Skip the start code to read the NAL header byte.
    if nal[2] == 1:
        hdr = nal[3]
    else:  # 00 00 00 01
        hdr = nal[4]
    nal_type = hdr & 0x1F
    broadcaster.feed(nal_type, nal)


# --------------------------------------------------------------------------- #
# TCP accept loop
# --------------------------------------------------------------------------- #

def accept_loop(host, port, broadcaster, stop_evt):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(8)
    srv.settimeout(0.5)
    log(f"listening on {host}:{port}")
    while not stop_evt.is_set():
        try:
            sock, addr = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        broadcaster.add(sock, addr)
    srv.close()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="Arch Screen Extender daemon")
    ap.add_argument("--output", default="EXT-1", help="headless output name")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--bitrate", default="12M")
    ap.add_argument("--gop", type=int, default=30,
                    help="keyframe interval in frames (lower = faster join)")
    ap.add_argument("--device", default="/dev/dri/renderD128",
                    help="VA-API render node (AMD iGPU that Hyprland renders on)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--keep-output", action="store_true",
                    help="don't remove the headless output on exit")
    args = ap.parse_args()

    for tool in ("hyprctl", "wf-recorder"):
        if not shutil.which(tool):
            log(f"error: '{tool}' not found in PATH")
            sys.exit(1)

    ensure_output(args.output, args.width, args.height, args.fps, args.scale)

    broadcaster = Broadcaster()
    stop_evt = threading.Event()

    cmd = build_wf_cmd(args.output, args.device, args.bitrate, args.gop, args.fps)
    log("capture cmd: " + " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, bufsize=0)

    acc = threading.Thread(target=accept_loop,
                           args=(args.host, args.port, broadcaster, stop_evt),
                           daemon=True)
    acc.start()

    def shutdown(*_):
        if stop_evt.is_set():
            return
        log("shutting down")
        stop_evt.set()
        try:
            proc.terminate()
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        parse_annexb(proc.stdout, broadcaster, stop_evt)
    finally:
        shutdown()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not args.keep_output:
            remove_output(args.output)
        log("stopped")


if __name__ == "__main__":
    main()
