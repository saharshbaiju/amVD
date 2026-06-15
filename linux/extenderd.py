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

Transport is plain TCP (we bind 0.0.0.0, so every link works the same):
  * USB  -> Android USB tethering (no USB debugging): the phone connects to this
            PC's tether IP on the 192.168.42.0/24 link the phone hands out.
  * WiFi -> phone connects to this machine's LAN IP

This is the low-latency BASE. RTP/FEC over UDP for lossy WiFi comes later.
"""

import argparse
import json
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


def kill_stale(output):
    """Kill leftover daemon / capture processes from a previous run.

    Avoids 'Address already in use' and orphaned wf-recorder holding the GPU.
    We inspect each process's real argv via /proc (not a loose substring match)
    so we only ever target an actual `python … extenderd.py` or a `wf-recorder`
    for *our* output — never the parent shell/TUI. Own PID and parent PID are
    always excluded.
    """
    me, parent = os.getpid(), os.getppid()
    killed = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in (me, parent):
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = [a.decode("utf-8", "replace")
                        for a in f.read().split(b"\x00") if a]
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if not args:
            continue
        is_daemon = (any(a.endswith("extenderd.py") for a in args)
                     and any("python" in a for a in args))
        is_wf = (args[0].endswith("wf-recorder") and output in " ".join(args))
        if is_daemon or is_wf:
            try:
                os.kill(pid, signal.SIGTERM)
                log(f"killed stale pid {pid} ({os.path.basename(args[0])})")
                killed += 1
            except (ProcessLookupError, PermissionError):
                pass
    if killed:
        time.sleep(0.4)  # let the OS release the socket / GPU handles


def _monitors_json():
    try:
        return json.loads(hyprctl("monitors", "-j").stdout)
    except Exception:
        return []


def ensure_output(name, width, height, fps, scale, position="auto-right"):
    """Create the headless output if missing and apply the requested mode.

    If the output already exists (e.g. left over from a previous run) we reuse
    it instead of erroring. `position` accepts Hyprland placement keywords
    (auto-right / auto-left / auto-up / auto-down) or an explicit "x y" string.
    """
    exists = any(m.get("name") == name for m in _monitors_json())
    if exists:
        log(f"reusing existing headless output {name}")
    else:
        log(f"creating headless output {name}")
        hyprctl("output", "create", "headless", name)
        time.sleep(0.5)
    mode = f"{width}x{height}@{fps}"
    rule = f"{name},{mode},{position},{scale}"
    r = hyprctl("keyword", "monitor", rule)
    if r.returncode != 0:
        log(f"warning: could not apply mode {mode} ({r.stderr.strip()}); "
            f"using whatever mode the output reports")
    log(f"output {name} ready ({mode}, scale {scale})")


def remove_output(name):
    """Move any windows off the virtual output, then remove it.

    Without this, windows living on EXT-1 would vanish with the output. We move
    every workspace that sits on EXT-1 onto the primary monitor first, so those
    windows reappear on the main screen.
    """
    mons = _monitors_json()
    if not any(m.get("name") == name for m in mons):
        return  # already gone

    primary = (next((m for m in mons
                     if m.get("name") != name and m.get("focused")), None)
               or next((m for m in mons if m.get("name") != name), None))

    if primary:
        try:
            workspaces = json.loads(hyprctl("workspaces", "-j").stdout)
            moved = False
            for w in workspaces:
                if w.get("monitor") == name:
                    hyprctl("dispatch", "moveworkspacetomonitor",
                            f"{w['id']} {primary['name']}")
                    moved = True
            if moved:
                log(f"moved windows from {name} back to {primary['name']}")
        except Exception as e:
            log(f"warning: could not reflow windows ({e})")

    hyprctl("output", "remove", name)
    log(f"removed headless output {name}")


# --------------------------------------------------------------------------- #
# Capture / encode process
# --------------------------------------------------------------------------- #

def build_wf_cmd(output, device, bitrate, gop, fps, codec="h264"):
    """wf-recorder -> raw Annex-B on stdout, tuned for low latency.

    codec: "h264" (default, decodable by the current app) or "hevc".
    """
    encoder = "hevc_vaapi" if codec == "hevc" else "h264_vaapi"
    muxer = "hevc" if codec == "hevc" else "h264"
    return [
        "wf-recorder",
        "-o", output,
        "-c", encoder,
        "-d", device,
        "-p", "bf=0",                 # no B-frames -> no reorder delay
        "-p", f"g={gop}",             # keyframe interval (fast join + recovery)
        "-p", "rc_mode=CBR",
        "-p", f"b={bitrate}",
        "-p", "async_depth=1",        # minimal encoder pipelining
        "-r", str(fps),
        f"--muxer={muxer}",           # raw Annex-B, no container
        "-f", "/dev/stdout",
    ]


# --------------------------------------------------------------------------- #
# Framed wire protocol
# --------------------------------------------------------------------------- #
#
# Every message on the wire is:  4-byte type + 4-byte big-endian length + body.
#   NALU -> body is one Annex-B NAL (with its start code) -> straight to decoder
#   ASX1 -> body is JSON of the active Linux settings -> shown on the phone
#
# Framing (instead of a one-shot header then raw Annex-B) lets us push a fresh
# config frame to already-connected phones whenever the settings change, so the
# app's readout always matches Linux. The 'ASX1' tag also can't be mistaken for
# a NAL (Annex-B start codes begin with 0x00).

FRAME_NAL = b"NALU"
FRAME_BANNER = b"ASX1"


def frame(ftype, body):
    return ftype + len(body).to_bytes(4, "big") + body


def build_banner_payload(args):
    """JSON body describing the active settings."""
    info = {
        "side": args.side,
        "width": args.width,
        "height": args.height,
        "fps": args.fps,
        "scale": args.scale,
        "quality": args.quality,
        "bitrate": args.bitrate,
        "codec": args.codec,
        "gop": args.gop,
    }
    return json.dumps(info).encode("utf-8")


def load_banner_payload(args):
    """Current banner body: the live --banner-file if present, else from args.

    The TUI keeps --banner-file up to date and signals SIGHUP so we re-read it
    for realtime config changes that don't restart the daemon (side / scale).
    """
    if args.banner_file:
        try:
            with open(args.banner_file, "rb") as f:
                data = f.read()
            if data.strip():
                return data
        except OSError:
            pass
    return build_banner_payload(args)


# --------------------------------------------------------------------------- #
# NAL broadcaster
# --------------------------------------------------------------------------- #

# If a client's unsent backlog exceeds this, it's too slow: we drop the backlog
# and resync it at the next keyframe instead of stalling everyone else.
MAX_PENDING = 4 * 1024 * 1024


class Client:
    __slots__ = ("sock", "addr", "started", "alive", "pending")

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.started = False        # becomes True once we've sent an IDR
        self.alive = True
        self.pending = b""          # bytes queued but not yet flushed


class Broadcaster:
    """Owns the client set; forwards NAL units with no buffering.

    Sends are non-blocking and happen OUTSIDE the lock, so one slow or
    half-open client can never stall the parser thread or other clients. A
    client that falls too far behind is resynced at the next keyframe.
    """

    def __init__(self, banner_payload=b""):
        self._clients = []
        self._lock = threading.Lock()
        self.sps = None
        self.pps = None
        self.banner_payload = banner_payload   # current config JSON body
        self._banner_dirty = False             # set by set_banner, sent by feed

    def set_banner(self, payload):
        """Replace the config body and schedule a push to all clients.

        The actual send happens in the parser thread (feed) so all socket
        writes stay single-threaded — never from the signal handler.
        """
        with self._lock:
            self.banner_payload = payload
            self._banner_dirty = True

    def add(self, sock, addr):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Send the current config frame first, while the socket is still
        # blocking, so the phone can show the Linux-side settings immediately.
        # It's tiny; a 2s timeout guards against a dead peer.
        with self._lock:
            payload = self.banner_payload
        if payload:
            try:
                sock.settimeout(2.0)
                sock.sendall(frame(FRAME_BANNER, payload))
                sock.settimeout(None)
            except OSError:
                try:
                    sock.close()
                except OSError:
                    pass
                log(f"client {addr} dropped before banner sent")
                return
        sock.setblocking(False)
        with self._lock:
            self._clients.append(Client(sock, addr))
            n = len(self._clients)
        log(f"client connected: {addr} (total {n})")

    def _drop(self, c):
        c.alive = False
        try:
            c.sock.close()
        except OSError:
            pass

    def _queue(self, c, data):
        """Append data and flush as much as the socket will take, non-blocking."""
        c.pending += data
        while c.pending:
            try:
                sent = c.sock.send(c.pending)
                c.pending = c.pending[sent:]
            except BlockingIOError:
                break                       # socket buffer full for now
            except OSError:
                self._drop(c)               # broken / closed connection
                return
        if len(c.pending) > MAX_PENDING:    # consumer too slow -> resync
            c.pending = b""
            c.started = False

    def feed(self, nal_type, nal_bytes):
        """Called per NAL by the parser thread."""
        if nal_type == NAL_SPS:
            self.sps = nal_bytes
        elif nal_type == NAL_PPS:
            self.pps = nal_bytes

        is_idr = nal_type == NAL_IDR

        with self._lock:
            clients = list(self._clients)
            banner_frame = None
            if self._banner_dirty:
                banner_frame = frame(FRAME_BANNER, self.banner_payload)
                self._banner_dirty = False

        # Push an updated config frame to every connected client (realtime
        # settings change). Independent of the video, so it's safe any time.
        if banner_frame:
            for c in clients:
                if c.alive:
                    self._queue(c, banner_frame)

        if not clients:
            return

        nal_frame = frame(FRAME_NAL, nal_bytes)
        for c in clients:
            if not c.alive:
                continue
            if not c.started:
                # A fresh (or resyncing) client must begin on a keyframe,
                # prefixed with the cached parameter sets, so its decoder is
                # valid from the first frame.
                if is_idr and self.sps and self.pps:
                    c.pending = b""
                    self._queue(c, frame(FRAME_NAL, self.sps))
                    self._queue(c, frame(FRAME_NAL, self.pps))
                    self._queue(c, nal_frame)
                    if c.alive:
                        c.started = True
                        log(f"client {c.addr} started on keyframe")
                # else: skip non-keyframe NALs until the first IDR
            else:
                self._queue(c, nal_frame)

        # prune any clients dropped during this pass
        dead = [c for c in clients if not c.alive]
        if dead:
            with self._lock:
                self._clients = [c for c in self._clients if c.alive]
                remaining = len(self._clients)
            for c in dead:
                log(f"client disconnected: {c.addr} (total {remaining})")


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
    # Retry the bind briefly in case a just-killed daemon hasn't released the
    # port yet (kill_stale ran, but the kernel may need a moment).
    for attempt in range(10):
        try:
            srv.bind((host, port))
            break
        except OSError:
            if stop_evt.is_set():
                return
            if attempt == 9:
                log(f"error: port {port} still in use after retries")
                stop_evt.set()
                return
            time.sleep(0.3)
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
    ap.add_argument("--codec", default="h264", choices=("h264", "hevc"),
                    help="video codec (the app currently decodes h264)")
    ap.add_argument("--side", default="right",
                    help="placement side, for reporting to the phone only")
    ap.add_argument("--quality", default="high",
                    help="quality preset label, for reporting to the phone only")
    ap.add_argument("--banner-file", default=None,
                    help="JSON file with the settings to advertise; re-read on "
                         "SIGHUP so live config changes reach connected phones")
    ap.add_argument("--position", default="auto-right",
                    help="placement: auto-right/auto-left/auto-up/auto-down or 'x y'")
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

    kill_stale(args.output)
    ensure_output(args.output, args.width, args.height, args.fps, args.scale,
                  args.position)

    broadcaster = Broadcaster(load_banner_payload(args))
    stop_evt = threading.Event()

    cmd = build_wf_cmd(args.output, args.device, args.bitrate, args.gop,
                       args.fps, args.codec)
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

    def reload_banner(*_):
        broadcaster.set_banner(load_banner_payload(args))
        log("config banner reloaded (live settings change)")

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGHUP, reload_banner)

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
