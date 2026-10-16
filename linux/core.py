"""
core.py - shared config, detection and dependency logic for Screen Extender.

Used by the TUI (tui.py). The streaming engine (extenderd.py) stays a standalone
CLI that the TUI launches with explicit flags built from a Config here.

Supported desktops (see backends/):
    Arch      + Hyprland          -> hyprland backend
    Ubuntu    + GNOME (Wayland)   -> gnome backend
    Fedora    + GNOME (Wayland)   -> gnome backend
"""

import glob
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from urllib.parse import quote

import backends
from backends import gnome as gnome_backend
from backends import hyprland as hypr_backend

CONFIG_DIR = os.path.expanduser("~/.config/archscreenextender")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# Quality preset -> CBR bitrate handed to the encoder.
QUALITY = {"low": "4M", "medium": "8M", "high": "12M", "ultra": "20M"}

# Android USB tethering (RNDIS/NCM) is the wired transport: it needs NO USB
# debugging. The phone becomes a USB network gadget, runs a DHCP server, and
# hands the PC an address on this subnet.
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
    backend: str = "auto"          # auto / hyprland / gnome
    output: str = "EXT-1"
    side: str = "right"            # right / left / up / down
    width: int = 1920
    height: int = 1080
    fps: int = 60
    scale: float = 1.0             # desktop scale of the virtual monitor
    quality: str = "high"          # low / medium / high / ultra
    codec: str = "h264"            # the app decodes h264
    gop: int = 30                  # keyframe interval (frames)
    transport: str = "usb"         # usb / wifi (preferred address in the QR)
    port: int = 5000
    va_device: str = "/dev/dri/renderD128"

    def bitrate(self) -> str:
        return QUALITY.get(self.quality, "12M")

    def resolved_backend(self):
        return backends.detect() if self.backend == "auto" else self.backend

    def extenderd_args(self) -> list:
        """Build the explicit CLI for extenderd.py from this config."""
        return [
            "--backend", self.resolved_backend() or "auto",
            "--output", self.output,
            "--width", str(self.width),
            "--height", str(self.height),
            "--fps", str(self.fps),
            "--scale", str(self.scale),
            "--bitrate", self.bitrate(),
            "--codec", self.codec,
            "--side", self.side,
            "--gop", str(self.gop),
            "--device", self.va_device,
            "--port", str(self.port),
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
# extenderd.py; the daemon also reads side/scale from here on SIGHUP and moves
# the monitor live.
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
        "host": socket.gethostname(),
        "backend": cfg.resolved_backend() or "",
    }


def write_banner_file(cfg: Config, path: str = BANNER_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(banner_payload(cfg), f)
    os.replace(tmp, path)          # atomic: the daemon never reads half a file


# --------------------------------------------------------------------------- #
# Distro + dependencies
# --------------------------------------------------------------------------- #

def distro() -> str:
    """'arch' / 'debian' (Ubuntu, Mint, Pop…) / 'fedora' / 'unknown'."""
    ids = ""
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith(("ID=", "ID_LIKE=")):
                    ids += " " + line.split("=", 1)[1].strip().strip('"').lower()
    except OSError:
        pass
    for key, name in (("arch", "arch"), ("fedora", "fedora"), ("rhel", "fedora"),
                      ("ubuntu", "debian"), ("debian", "debian")):
        if key in ids.split():
            return name
    return "unknown"


INSTALL_CMD = {
    "arch": ["sudo", "pacman", "-S", "--needed"],
    "debian": ["sudo", "apt", "install", "-y"],
    "fedora": ["sudo", "dnf", "install", "-y"],
}


def _bin(name):
    return lambda: shutil.which(name) is not None


def _gst(element):
    return lambda: bool(shutil.which("gst-inspect-1.0")
                        and gnome_backend.gst_inspect(element))


def _va_encode_driver():
    """(VA driver name, {distro: package}) Hyprland would encode with, or None.

    Only for Intel/AMD compositor GPUs; NVIDIA uses NVENC from its driver.
    """
    card = hypr_backend.compositor_card()
    node = hypr_backend.render_node(card) if card else None
    va = hypr_backend.va_driver_for(node) if node else None
    pkgs = {"iHD": {"arch": "intel-media-driver",
                    "debian": "intel-media-va-driver",
                    "fedora": "libva-intel-media-driver"},
            "i965": {"arch": "libva-intel-driver", "debian": "i965-va-driver",
                     "fedora": "libva-intel-driver"},
            "radeonsi": {"arch": "libva-mesa-driver",
                         "debian": "mesa-va-drivers",
                         "fedora": "mesa-va-drivers"}}.get(va)
    return (va, pkgs) if pkgs else None


def _va_driver_present(name):
    return lambda: hypr_backend.va_installed(name)


def _any_h264_encoder():
    return bool(shutil.which("gst-inspect-1.0") and gnome_backend.pick_encoder())


# (label, check, {distro: package}, required, purpose)
DEPS = {
    "hyprland": [
        ("hyprctl", _bin("hyprctl"), {"arch": "hyprland"}, True,
         "Hyprland compositor (must be running)"),
        ("wf-recorder", _bin("wf-recorder"),
         {"arch": "wf-recorder", "debian": "wf-recorder", "fedora": "wf-recorder"},
         True, "headless capture + VA-API H.264 encode"),
        ("vainfo", _bin("vainfo"),
         {"arch": "libva-utils", "debian": "vainfo", "fedora": "libva-utils"},
         False, "VA-API capability detection"),
    ] + ([("VA-API driver", _va_driver_present(_VA[0]), _VA[1], False,
           "hardware H.264 encode on Intel/AMD (else x264 on the CPU)")]
         if (_VA := _va_encode_driver()) else []),
    "gnome": [
        ("gst-launch-1.0", _bin("gst-launch-1.0"),
         {"arch": "gstreamer", "debian": "gstreamer1.0-tools",
          "fedora": "gstreamer1"}, True, "runs the capture/encode pipeline"),
        ("python gi", lambda: importlib.util.find_spec("gi") is not None,
         {"arch": "python-gobject", "debian": "python3-gi",
          "fedora": "python3-gobject"}, True, "talks to Mutter over D-Bus"),
        ("pipewiresrc", _gst("pipewiresrc"),
         {"arch": "gst-plugin-pipewire", "debian": "gstreamer1.0-pipewire",
          "fedora": "pipewire-gstreamer"}, True, "reads the virtual monitor"),
        ("videoconvert", _gst("videoconvert"),
         {"arch": "gst-plugins-base", "debian": "gstreamer1.0-plugins-base",
          "fedora": "gstreamer1-plugins-base"}, True, "pixel format conversion"),
        ("h264parse", _gst("h264parse"),
         {"arch": "gst-plugins-bad", "debian": "gstreamer1.0-plugins-bad",
          "fedora": "gstreamer1-plugins-bad-free"}, True,
         "H.264 framing (+ VA-API encoders)"),
        ("H.264 encoder", _any_h264_encoder,
         {"arch": "gst-plugins-ugly", "debian": "gstreamer1.0-plugins-ugly",
          "fedora": "gstreamer1-plugin-openh264"}, True,
         "x264 / openh264 (VA-API used automatically if present)"),
    ],
    "common": [
        ("adb", _bin("adb"),
         {"arch": "android-tools", "debian": "adb", "fedora": "android-tools"},
         False, "phone size auto-match (needs USB debugging)"),
    ],
}


def check_dependencies(backend=None) -> list:
    """[(label, package, purpose, required, present), ...] for this backend."""
    d = distro()
    rows = []
    for label, check, pkgs, req, purpose in (DEPS.get(backend, [])
                                             + DEPS["common"]):
        try:
            present = bool(check())
        except Exception:
            present = False
        rows.append((label, pkgs.get(d, next(iter(pkgs.values()))), purpose,
                     req, present))
    return rows


def missing_packages(backend=None, required_only=False) -> list:
    pkgs = []
    for _l, pkg, _p, req, present in check_dependencies(backend):
        if not present and (req or not required_only) and pkg not in pkgs:
            pkgs.append(pkg)
    return pkgs


def install_command(packages) -> list:
    base = INSTALL_CMD.get(distro())
    return base + packages if base else None


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def primary_monitor_desc(backend) -> str:
    """One-line description of the main display, for the hardware screen."""
    if backend == "hyprland":
        m = hypr_backend.primary_for("EXT-1")
        if m:
            return (f"{m['name']}  {m['width']}x{m['height']}"
                    f"@{m.get('refreshRate', 0):.0f}  scale {m.get('scale', 1)}")
    elif backend == "gnome":
        try:
            from gi.repository import Gio
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            _s, _mons, logicals, _p = gnome_backend.display_state(bus)
            for x, y, sc, _t, primary, specs, _lp in logicals:
                if primary:
                    return f"{specs[0][0]}  at {x},{y}  scale {sc:g}"
        except Exception:
            pass
    return "(none found)"


def remove_virtual_output(cfg: Config) -> None:
    """Hyprland: move windows off EXT and remove it (the TUI launches the daemon
    with --keep-output so the monitor survives restarts). GNOME's virtual
    monitor disappears by itself when the daemon stops."""
    if cfg.resolved_backend() == "hyprland":
        try:
            hypr_backend.remove_output(cfg.output)
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
        return any(l.strip().endswith("device")
                   for l in r.stdout.splitlines()[1:])
    except Exception:
        return False


_adb_reverse_cache = {}


def adb_reverse(port: int) -> bool:
    """Forward the phone's 127.0.0.1:port to ours over the USB cable via adb.

    Works whenever the phone is plugged in with USB debugging on, even when it
    is on mobile data and can't see the PC on Wi-Fi. Re-run at most every 10 s
    (it's cheap, but the TUI asks often); True if at least one phone has it.
    """
    now = time.time()
    hit = _adb_reverse_cache.get(port)
    if hit and now - hit[0] < 10:
        return hit[1]
    ok = False
    if shutil.which("adb"):
        try:
            r = subprocess.run(["adb", "devices"], capture_output=True,
                               text=True, timeout=5)
            serials = [l.split()[0] for l in r.stdout.splitlines()[1:]
                       if l.strip().endswith("device")]
            for sn in serials:
                rr = subprocess.run(["adb", "-s", sn, "reverse",
                                     f"tcp:{port}", f"tcp:{port}"],
                                    capture_output=True, timeout=5)
                ok = ok or rr.returncode == 0
        except Exception:
            ok = False
    _adb_reverse_cache[port] = (now, ok)
    return ok


def detect_phone_size(landscape: bool = True):
    """(w, h) of a USB phone's panel, oriented for the (landscape) app."""
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


# Interfaces that are never a path to the phone (containers, VMs, VPN tunnels).
_VIRTUAL_IF = re.compile(r"^(docker|br-|veth|virbr|vboxnet|vmnet|tun|tap|wg|"
                         r"lxc|lxd|podman|cni|flannel|zt)")


def _ip_addrs():
    """[(ifname, ipv4), ...] for every up, non-loopback, non-virtual interface."""
    try:
        out = subprocess.run(["ip", "-j", "addr"], capture_output=True,
                             text=True, timeout=5).stdout
        links = json.loads(out)
    except Exception:
        return []
    res = []
    for link in links:
        name = link.get("ifname", "")
        if name == "lo" or _VIRTUAL_IF.match(name):
            continue
        if "UP" not in link.get("flags", []):
            continue
        for a in link.get("addr_info", []):
            if a.get("family") == "inet" and a.get("local"):
                res.append((name, a["local"]))
    return res


def lan_ip():
    try:
        r = subprocess.run(["ip", "route", "get", "1.1.1.1"],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", r.stdout)
        return m.group(1) if m else None
    except Exception:
        return None


def usb_tether_ip():
    """This PC's IPv4 on the USB-tether link, or None if no phone is tethered.

    Prefers Android's default 192.168.42.0/24 subnet; failing that (vendor with
    a non-standard subnet) a USB-gadget-looking interface (usb0, enp0s20f0u1…).
    """
    addrs = _ip_addrs()
    for _name, ip in addrs:
        if ip.startswith(USB_TETHER_PREFIX):
            return ip
    for name, ip in addrs:
        if name.startswith(("usb", "rndis", "ncm")) or re.search(r"u\d", name):
            return ip
    return None


def candidate_ips(cfg: Config) -> list:
    """Every address the phone might reach us on, preferred transport first."""
    tether, lan = usb_tether_ip(), lan_ip()
    adb = "127.0.0.1" if adb_reverse(cfg.port) else None
    first = ([adb, tether, lan] if cfg.transport == "usb"
             else [lan, tether, adb])
    ips = []
    for ip in first + [ip for _n, ip in _ip_addrs()]:
        if ip and ip not in ips:
            ips.append(ip)
    return ips[:5]


def connect_uri(cfg: Config) -> str:
    """What the pairing QR encodes. The app tries every host in parallel and
    keeps the first that answers, so USB and Wi-Fi both just work:

        asext://c?h=192.168.42.129,10.0.0.5&p=5000&n=archbox&v=1
    """
    hosts = ",".join(candidate_ips(cfg)) or "0.0.0.0"
    name = quote(socket.gethostname()[:24], safe="")
    return f"asext://c?h={hosts}&p={cfg.port}&n={name}&v=1"
