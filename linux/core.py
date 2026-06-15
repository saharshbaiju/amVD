"""
core.py - shared config, detection and dependency logic for Arch Screen Extender.

Used by the TUI (tui.py). The streaming engine (extenderd.py) stays a standalone
CLI that the TUI launches with explicit flags built from a Config here.
"""

import glob
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass

CONFIG_DIR = os.path.expanduser("~/.config/archscreenextender")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# Quality preset -> CBR bitrate handed to the VA-API encoder.
QUALITY = {"low": "4M", "medium": "8M", "high": "12M", "ultra": "20M"}

# User-facing "side" -> Hyprland auto-placement keyword.
SIDES = {
    "right": "auto-right",
    "left": "auto-left",
    "up": "auto-up",
    "down": "auto-down",
}

# (binary, pacman package, purpose, required?)
DEPS = [
    ("hyprctl", "hyprland", "Wayland compositor (must already be running)", True),
    ("wf-recorder", "wf-recorder", "headless capture + VA-API H.264 encode", True),
    ("vainfo", "libva-utils", "VA-API capability detection (optional)", False),
    ("adb", "android-tools", "USB phone size auto-match (optional, needs USB debugging)", False),
]

# Android USB tethering (RNDIS/NCM) is the wired transport: it needs NO USB
# debugging. The phone becomes a USB network gadget, runs a DHCP server, and
# hands the PC an address on this subnet (phone = gateway, .129 by default).
# The phone then reaches the daemon at the PC's address on that link.
USB_TETHER_PREFIX = "192.168.42."

RES_PRESETS = [
    ("Match connected phone (USB)", None),
    ("1280x720", (1280, 720)),
    ("1920x1080", (1920, 1080)),
    ("2560x1440", (2560, 1440)),
    ("3840x2160", (3840, 2160)),
]


@dataclass
class Config:
    output: str = "EXT-1"
    side: str = "right"            # right / left / up / down
    width: int = 1920
    height: int = 1080
    fps: int = 60
    scale: float = 1.0             # Hyprland scale for the virtual monitor
    quality: str = "high"          # low / medium / high / ultra
    codec: str = "h264"            # h264 / hevc
    gop: int = 30                  # keyframe interval (frames)
    transport: str = "usb"         # usb / wifi
    port: int = 5000
    va_device: str = "/dev/dri/renderD128"

    def bitrate(self) -> str:
        return QUALITY.get(self.quality, "12M")

    def position(self) -> str:
        return SIDES.get(self.side, "auto-right")

    def extenderd_args(self, position: str = None) -> list:
        """Build the explicit CLI for extenderd.py from this config.

        `position` overrides the placement passed to the daemon (e.g. an
        explicit "XxY" computed from the live monitor layout). Falls back to
        the Hyprland auto-* keyword for this side.
        """
        return [
            "--output", self.output,
            "--width", str(self.width),
            "--height", str(self.height),
            "--fps", str(self.fps),
            "--scale", str(self.scale),
            "--bitrate", self.bitrate(),
            "--codec", self.codec,
            "--position", position or self.position(),
            "--gop", str(self.gop),
            "--device", self.va_device,
            "--port", str(self.port),
            "--side", self.side,
            "--quality", self.quality,
        ]


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def load_config() -> Config:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
            fields = Config.__dataclass_fields__
            return Config(**{k: v for k, v in data.items() if k in fields})
        except Exception:
            pass
    return Config()


def save_config(cfg: Config) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(asdict(cfg), f, indent=2)


# Settings advertised to the phone. Keys must match build_banner_payload() in
# extenderd.py so the daemon can frame this JSON verbatim.
BANNER_PATH = os.path.join(CONFIG_DIR, "daemon-banner.json")


def banner_payload(cfg: Config) -> dict:
    return {
        "side": cfg.side,
        "width": cfg.width,
        "height": cfg.height,
        "fps": cfg.fps,
        "scale": cfg.scale,
        "quality": cfg.quality,
        "bitrate": cfg.bitrate(),
        "codec": cfg.codec,
        "gop": cfg.gop,
    }


def write_banner_file(cfg: Config, path: str = BANNER_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(banner_payload(cfg), f)


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def detect_monitors() -> list:
    try:
        out = subprocess.run(["hyprctl", "monitors", "-j"],
                             capture_output=True, text=True, timeout=5).stdout
        return json.loads(out)
    except Exception:
        return []


def primary_monitor():
    mons = [m for m in detect_monitors() if not m.get("name", "").startswith("EXT-")]
    for m in mons:
        if m.get("focused"):
            return m
    return mons[0] if mons else None


def _hyprctl(*args):
    return subprocess.run(["hyprctl", *args], capture_output=True,
                          text=True, timeout=5)


def compute_position(cfg) -> str:
    """Explicit "XxY" Hyprland placement for the virtual output.

    Hyprland's auto-left/right/up/down keywords are unreliable across versions,
    so we place the secondary monitor at concrete logical coordinates relative
    to the focused/primary monitor. Falls back to the auto-* keyword if we
    can't read the layout. Coordinates are in logical pixels (mode / scale).
    """
    primary = primary_monitor()
    if not primary:
        return cfg.position()
    try:
        pscale = primary.get("scale", 1) or 1
        px = int(primary.get("x", 0))
        py = int(primary.get("y", 0))
        pw = int(primary["width"] / pscale)   # primary logical size
        ph = int(primary["height"] / pscale)
        cscale = cfg.scale or 1
        ew = int(cfg.width / cscale)           # extender logical size
        eh = int(cfg.height / cscale)
    except Exception:
        return cfg.position()

    if cfg.side == "left":
        x, y = px - ew, py
    elif cfg.side == "up":
        x, y = px, py - eh
    elif cfg.side == "down":
        x, y = px, py + ph
    else:  # right (default)
        x, y = px + pw, py
    return f"{x}x{y}"


def apply_live_monitor(cfg) -> bool:
    """Re-apply the virtual monitor's mode/position/scale to a running output.

    Used for realtime config changes (side, scale) that don't require
    restarting the capture/encode pipeline. Returns True on success.
    """
    pos = compute_position(cfg)
    rule = f"{cfg.output},{cfg.width}x{cfg.height}@{cfg.fps},{pos},{cfg.scale}"
    try:
        return _hyprctl("keyword", "monitor", rule).returncode == 0
    except Exception:
        return False


def remove_virtual_output(name: str) -> None:
    """Move windows off the virtual output and remove it (restore windows).

    Mirrors the daemon's own cleanup so the TUI can tear the monitor down when
    the daemon was launched with --keep-output (so it survives restarts).
    """
    try:
        mons = detect_monitors()
    except Exception:
        mons = []
    if not any(m.get("name") == name for m in mons):
        return  # already gone

    primary = (next((m for m in mons
                     if m.get("name") != name and m.get("focused")), None)
               or next((m for m in mons if m.get("name") != name), None))
    if primary:
        try:
            workspaces = json.loads(_hyprctl("workspaces", "-j").stdout)
            for w in workspaces:
                if w.get("monitor") == name:
                    _hyprctl("dispatch", "moveworkspacetomonitor",
                             f"{w['id']} {primary['name']}")
        except Exception:
            pass
    try:
        _hyprctl("output", "remove", name)
    except Exception:
        pass


def detect_va_device() -> str:
    """Find a render node that advertises H.264 VA-API *encode*."""
    if not shutil.which("vainfo"):
        return "/dev/dri/renderD128"
    for node in sorted(glob.glob("/dev/dri/renderD*")):
        try:
            r = subprocess.run(["vainfo", "--display", "drm", "--device", node],
                               capture_output=True, text=True, timeout=5)
            if "VAEntrypointEncSlice" in r.stdout and "VAProfileH264" in r.stdout:
                return node
        except Exception:
            continue
    return "/dev/dri/renderD128"


def adb_device_present() -> bool:
    if not shutil.which("adb"):
        return False
    try:
        r = subprocess.run(["adb", "devices"], capture_output=True,
                           text=True, timeout=5)
        rows = [l for l in r.stdout.splitlines()[1:]
                if l.strip() and l.strip().endswith("device")]
        return len(rows) > 0
    except Exception:
        return False


def detect_phone_size(landscape: bool = True):
    """(w, h) of a USB phone's panel, oriented for the (landscape) app. None if absent."""
    if not adb_device_present():
        return None
    try:
        r = subprocess.run(["adb", "shell", "wm", "size"],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r"(\d+)x(\d+)", r.stdout)
        if not m:
            return None
        a, b = int(m.group(1)), int(m.group(2))
        w, h = (max(a, b), min(a, b)) if landscape else (min(a, b), max(a, b))
        return (w - w % 2, h - h % 2)   # encoders want even dimensions
    except Exception:
        return None


def lan_ip():
    try:
        r = subprocess.run(["ip", "route", "get", "1.1.1.1"],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", r.stdout)
        return m.group(1) if m else None
    except Exception:
        return None


def _ip_addrs():
    """[(ifname, ipv4), ...] for every up, non-loopback interface."""
    try:
        out = subprocess.run(["ip", "-j", "addr"], capture_output=True,
                             text=True, timeout=5).stdout
        links = json.loads(out)
    except Exception:
        return []
    res = []
    for link in links:
        name = link.get("ifname", "")
        if name == "lo":
            continue
        for a in link.get("addr_info", []):
            if a.get("family") == "inet" and a.get("local"):
                res.append((name, a["local"]))
    return res


def usb_tether_ip():
    """This PC's IPv4 on the USB-tether link, or None if no phone is tethered.

    USB tethering needs no USB debugging: the phone is a USB network gadget that
    assigns the PC an address on Android's default 192.168.42.0/24 subnet, and
    the phone reaches the daemon at the PC's address on that link. We prefer an
    address on that subnet; failing that (vendor with a non-standard subnet) we
    fall back to a USB-gadget-looking interface (usb0, enp0s20f0u1, rndis…).
    """
    addrs = _ip_addrs()
    for _name, ip in addrs:
        if ip.startswith(USB_TETHER_PREFIX):
            return ip
    for name, ip in addrs:
        if (name.startswith(("usb", "rndis", "ncm")) or re.search(r"u\d", name)):
            return ip
    return None


def check_dependencies() -> list:
    """[(binary, package, purpose, required, present), ...]"""
    return [(b, p, purpose, req, shutil.which(b) is not None)
            for (b, p, purpose, req) in DEPS]


def missing_packages() -> list:
    return [p for (_b, p, _purpose, _req, present) in check_dependencies()
            if not present]
