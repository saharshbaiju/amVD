#!/usr/bin/env python3
"""
tui.py - terminal UI for Arch Screen Extender.

A self-contained curses app (no third-party deps) that:
  * checks/installs required packages on first run,
  * detects your monitors, GPU VA-API encoder and any USB phone,
  * lets you configure side / resolution / fps / quality / codec / transport,
  * launches the streaming daemon and shows live status.

Run:  python3 tui.py     (or ./asext)
"""

import copy
import curses
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque

import core

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(HERE, "extenderd.py")

TITLE =  " amVD - amFOSS virtual display "


# --------------------------------------------------------------------------- #
# small curses helpers
# --------------------------------------------------------------------------- #

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title / accents
    curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)  # selection
    curses.init_pair(3, curses.COLOR_GREEN, -1)    # ok
    curses.init_pair(4, curses.COLOR_RED, -1)      # missing / stop
    curses.init_pair(5, curses.COLOR_YELLOW, -1)   # hints


def addstr(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h:
        win.addnstr(y, x, text, max(0, w - x - 1), attr)


def header(win):
    win.erase()
    addstr(win, 0, 0, TITLE, curses.color_pair(1) | curses.A_BOLD)
    h, w = win.getmaxyx()
    addstr(win, 1, 0, "─" * (w - 1), curses.color_pair(1))


def footer(win, text):
    h, w = win.getmaxyx()
    addstr(win, h - 1, 0, " " + text, curses.color_pair(5))


def run_shell(win, cmd, prompt="press Enter to return"):
    """Drop out of curses, run an interactive command, come back."""
    curses.def_prog_mode()
    curses.endwin()
    print("\n$ " + " ".join(cmd) + "\n", flush=True)
    try:
        subprocess.run(cmd)
    except Exception as e:
        print(f"error: {e}")
    input(f"\n{prompt} ")
    curses.reset_prog_mode()
    win.refresh()


# --------------------------------------------------------------------------- #
# persistent streaming daemon controller
# --------------------------------------------------------------------------- #

# Config keys that change the capture/encode pipeline: changing any of these
# needs the daemon restarted. The rest (side, scale) re-apply live via hyprctl.
RESTART_KEYS = ("output", "width", "height", "fps", "quality",
                "codec", "gop", "transport", "port")
LIVE_KEYS = ("side", "scale")


class Stream:
    """Owns the streaming daemon so it survives leaving the live view.

    The daemon is launched with --keep-output, so the virtual monitor (and the
    windows on it) persist across restarts triggered by realtime config edits.
    The monitor is only removed on an explicit disconnect.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.proc = None
        self.logs = deque(maxlen=8)
        self.state = {"live": 0, "ever": False}
        self.stop = threading.Event()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        # USB transport now rides Android USB tethering (no adb / no USB
        # debugging): the daemon binds 0.0.0.0, so it already listens on the
        # tether link — nothing to set up here. The phone connects to this PC's
        # tether IP (see live_screen / usb_ready).
        core.write_banner_file(self.cfg)        # settings shown on the phone
        # Explicit coordinates make 'side' actually work (auto-* is flaky).
        args = self.cfg.extenderd_args(core.compute_position(self.cfg))
        cmd = [sys.executable, DAEMON, "--keep-output",
               "--banner-file", core.BANNER_PATH] + args
        # stdin=DEVNULL is essential: otherwise the daemon's wf-recorder child
        # inherits this terminal's stdin and swallows our keystrokes.
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        self.stop.clear()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stderr:
            line = line.rstrip()
            self.logs.append(line)
            if "client connected" in line:
                self.state["live"] += 1
                self.state["ever"] = True
            elif "client disconnected" in line:
                self.state["live"] = max(0, self.state["live"] - 1)
        self.stop.set()

    def _stop_daemon(self):
        """Terminate the daemon; the virtual output stays (--keep-output)."""
        p = self.proc
        if p is None or p.poll() is not None:
            return
        try:
            p.terminate()
            p.wait(timeout=4)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.wait(timeout=2)
            except Exception:
                pass
        except Exception:
            pass

    def restart(self, cfg):
        """Relaunch the daemon with new cfg, keeping the monitor (and windows)."""
        self._stop_daemon()
        self.cfg = cfg
        self.logs.clear()
        self.state = {"live": 0, "ever": False}
        self.start()

    def disconnect(self):
        """Full teardown: stop the daemon and remove the virtual monitor."""
        self._stop_daemon()
        core.remove_virtual_output(self.cfg.output)

    def push_banner(self):
        """Rewrite the banner file and tell the running daemon to re-send it,
        so connected phones immediately show the new settings."""
        core.write_banner_file(self.cfg)
        if self.running():
            try:
                self.proc.send_signal(signal.SIGHUP)
            except Exception:
                pass


def apply_config(stream, new_cfg):
    """Push edited config onto a running stream. Returns what it did."""
    old = stream.cfg
    if any(getattr(new_cfg, k) != getattr(old, k) for k in RESTART_KEYS):
        stream.restart(new_cfg)        # restart re-sends a fresh banner too
        return "restart"
    geom_changed = any(getattr(new_cfg, k) != getattr(old, k) for k in LIVE_KEYS)
    stream.cfg = new_cfg
    if geom_changed:
        # reposition/resize the monitor live — no stream interruption.
        core.apply_live_monitor(new_cfg)
    stream.push_banner()               # phone shows the new settings instantly
    return "live" if geom_changed else "none"


# --------------------------------------------------------------------------- #
# main menu
# --------------------------------------------------------------------------- #

MENU = [
    ("Start extender", "start"),
    ("Configure", "configure"),
    ("Dependencies", "deps"),
    ("Detected hardware", "hardware"),
    ("Quit", "quit"),
]


def main_menu(win, cfg, stream):
    sel = 0
    running = stream is not None and stream.running()
    while True:
        header(win)
        addstr(win, 3, 2, "What do you want to do?", curses.A_BOLD)
        for i, (label, key) in enumerate(MENU):
            if key == "start" and running:
                label = "Open live view"
            mark = "▶ " if i == sel else "  "
            attr = curses.color_pair(2) if i == sel else 0
            addstr(win, 5 + i, 2, f"{mark}{label}".ljust(40), attr)

        # current stream status
        if running:
            live = stream.state["live"]
            if live > 0:
                addstr(win, 6 + len(MENU), 2,
                       f"● extender running — {live} client"
                       f"{'s' if live > 1 else ''} connected",
                       curses.color_pair(3) | curses.A_BOLD)
            else:
                addstr(win, 6 + len(MENU), 2,
                       "● extender running — waiting for phone",
                       curses.color_pair(3) | curses.A_BOLD)
        else:
            addstr(win, 6 + len(MENU), 2, "○ extender stopped",
                   curses.color_pair(5))

        # quick summary of current config
        summ = (f"side={cfg.side}  {cfg.width}x{cfg.height}@{cfg.fps}  "
                f"quality={cfg.quality}  codec={cfg.codec}  "
                f"transport={cfg.transport}  port={cfg.port}")
        addstr(win, 7 + len(MENU), 2, summ, curses.color_pair(5))
        footer(win, "↑/↓ move   Enter select   q quit")
        win.refresh()

        c = win.getch()
        if c in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(MENU)
        elif c in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(MENU)
        elif c in (ord("q"), 27):
            return "quit"
        elif c in (curses.KEY_ENTER, 10, 13):
            return MENU[sel][1]


# --------------------------------------------------------------------------- #
# dependencies screen
# --------------------------------------------------------------------------- #

def deps_screen(win):
    while True:
        header(win)
        addstr(win, 3, 2, "Required packages (pacman):", curses.A_BOLD)
        deps = core.check_dependencies()
        row = 5
        for (binary, pkg, purpose, required, present) in deps:
            if present:
                status, attr = "[ok]     ", curses.color_pair(3)
            else:
                status, attr = "[missing]", curses.color_pair(4)
            req = "required" if required else "optional"
            addstr(win, row, 2, status, attr)
            addstr(win, row, 13, f"{binary:<13} {pkg:<16} {req:<9} {purpose}")
            row += 1

        missing = core.missing_packages()
        row += 1
        if missing:
            addstr(win, row, 2,
                   f"Missing: {', '.join(missing)}", curses.color_pair(4))
            addstr(win, row + 1, 2,
                   "Press 'i' to install them with sudo pacman.",
                   curses.color_pair(5))
        else:
            addstr(win, row, 2, "All dependencies satisfied.",
                   curses.color_pair(3))
        footer(win, "i install missing   r refresh   Esc back")
        win.refresh()

        c = win.getch()
        if c in (ord("i"),) and missing:
            run_shell(win, ["sudo", "pacman", "-S", "--needed"] + missing)
        elif c in (ord("r"),):
            continue
        elif c in (27, ord("q")):
            return


# --------------------------------------------------------------------------- #
# detected hardware screen
# --------------------------------------------------------------------------- #

def hardware_screen(win):
    header(win)
    addstr(win, 2, 2, "Detecting…", curses.color_pair(5))
    win.refresh()

    mon = core.primary_monitor()
    va = core.detect_va_device()
    phone = core.detect_phone_size(landscape=True)
    has_phone = core.adb_device_present()
    tether_ip = core.usb_tether_ip()
    ip = core.lan_ip()

    header(win)
    addstr(win, 3, 2, "Detected hardware:", curses.A_BOLD)
    row = 5
    if mon:
        addstr(win, row, 2,
               f"Primary monitor : {mon['name']}  "
               f"{mon['width']}x{mon['height']}@{mon.get('refreshRate', 0):.0f}  "
               f"scale {mon.get('scale', 1)}")
    else:
        addstr(win, row, 2, "Primary monitor : (none found)", curses.color_pair(4))
    row += 1
    addstr(win, row, 2, f"VA-API encoder  : {va}")
    row += 1
    if tether_ip:
        addstr(win, row, 2,
               f"USB tethering   : up — phone connects to {tether_ip} "
               "(no USB debugging needed)", curses.color_pair(3))
    else:
        addstr(win, row, 2,
               "USB tethering   : off — enable 'USB tethering' on the phone "
               "for the wired path", curses.color_pair(5))
    row += 1
    if has_phone and phone:
        addstr(win, row, 2,
               f"Phone panel     : {phone[0]}x{phone[1]} (landscape)  — "
               f"'Match phone' available (adb)", curses.color_pair(3))
    else:
        addstr(win, row, 2,
               "Phone panel     : auto-match needs USB debugging (optional)",
               curses.color_pair(5))
    row += 1
    addstr(win, row, 2, f"LAN IP (WiFi)   : {ip or 'unknown'}")
    row += 2
    addstr(win, row, 2, "Press any key to return.", curses.color_pair(5))
    win.refresh()
    win.getch()


# --------------------------------------------------------------------------- #
# configuration editor
# --------------------------------------------------------------------------- #

def text_prompt(win, label, current):
    curses.echo()
    curses.curs_set(1)
    h, w = win.getmaxyx()
    addstr(win, h - 2, 2, f"{label}: ".ljust(w - 3), curses.color_pair(5))
    win.move(h - 2, 2 + len(label) + 2)
    try:
        raw = win.getstr(h - 2, 2 + len(label) + 2, 12).decode().strip()
    except Exception:
        raw = ""
    curses.noecho()
    curses.curs_set(0)
    return raw or str(current)


def cycle(values, current, step):
    try:
        i = values.index(current)
    except ValueError:
        i = 0
    return values[(i + step) % len(values)]


def configure_screen(win, cfg, live=False):
    """Edit a working copy of cfg.

    Returns (saved, new_cfg). On save the config is persisted to disk and the
    edited copy is returned; on cancel returns (False, None) and nothing is
    changed. `live` only affects the on-screen hint.
    """
    cfg = copy.copy(cfg)            # edit a copy so cancel truly discards
    # each entry: (label, kind, key, choices)
    rows = [
        ("Screen side", "choice", "side", ["right", "left", "up", "down"]),
        ("Resolution", "resolution", None, None),
        ("Refresh (FPS)", "choice", "fps", [30, 60, 120]),
        ("Desktop scale", "choice", "scale", [1.0, 1.25, 1.5, 2.0]),
        ("Quality", "choice", "quality", ["low", "medium", "high", "ultra"]),
        ("Codec", "choice", "codec", ["h264", "hevc"]),
        ("Keyframe interval", "choice", "gop", [15, 30, 60]),
        ("Transport", "choice", "transport", ["usb", "wifi"]),
        ("Port", "port", None, None),
    ]
    # Start the resolution cursor on the preset matching the saved size, so the
    # [label] shown next to WxH is accurate (else it always read "Match phone").
    res_idx = next((i for i, (_lbl, dims) in enumerate(core.RES_PRESETS)
                    if dims == (cfg.width, cfg.height)), 0)
    sel = 0
    while True:
        header(win)
        addstr(win, 3, 2, "Configuration  (←/→ to change a value)", curses.A_BOLD)
        for i, (label, kind, key, choices) in enumerate(rows):
            if kind == "resolution":
                preset = core.RES_PRESETS[res_idx][0]
                value = f"{cfg.width}x{cfg.height}   [{preset}]"
            elif kind == "port":
                value = str(cfg.port)
            elif key == "quality":
                value = f"{cfg.quality}  ({core.QUALITY[cfg.quality]})"
            elif key == "codec":
                extra = "  (app decodes h264)" if cfg.codec == "hevc" else ""
                value = f"{cfg.codec}{extra}"
            else:
                value = str(getattr(cfg, key))
            attr = curses.color_pair(2) if i == sel else 0
            mark = "▶ " if i == sel else "  "
            addstr(win, 5 + i, 2, f"{mark}{label:<20}".ljust(24), attr)
            addstr(win, 5 + i, 28, value.ljust(34),
                   attr if i == sel else curses.color_pair(1))

        addstr(win, 6 + len(rows), 2,
               "Tip: 'Match connected phone' sizes the monitor to your phone over USB.",
               curses.color_pair(5))
        if live:
            addstr(win, 7 + len(rows), 2,
                   "Extender is running — saving applies changes immediately "
                   "(side/scale instantly; others restart the stream).",
                   curses.color_pair(3))
        footer(win, "↑/↓ move   ←/→ change   Enter edit   s save & back   Esc cancel")
        win.refresh()

        c = win.getch()
        label, kind, key, choices = rows[sel]

        if c in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(rows)
        elif c in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(rows)
        elif c in (curses.KEY_LEFT, curses.KEY_RIGHT,
                   curses.KEY_ENTER, 10, 13):
            step = -1 if c == curses.KEY_LEFT else 1
            if kind == "choice":
                setattr(cfg, key, cycle(choices, getattr(cfg, key), step))
            elif kind == "resolution":
                res_idx = (res_idx + step) % len(core.RES_PRESETS)
                _apply_resolution(win, cfg, res_idx)
            elif kind == "port":
                raw = text_prompt(win, "Port", cfg.port)
                if raw.isdigit():
                    cfg.port = int(raw)
        elif c in (ord("s"),):
            core.save_config(cfg)
            return True, cfg
        elif c in (27, ord("q")):
            return False, None


def _apply_resolution(win, cfg, res_idx):
    label, dims = core.RES_PRESETS[res_idx]
    if dims is None:  # "match phone"
        size = core.detect_phone_size(landscape=True)
        if size:
            cfg.width, cfg.height = size
        else:
            h, w = win.getmaxyx()
            addstr(win, h - 2, 2,
                   "No USB phone detected — connect it and try again.",
                   curses.color_pair(4))
            win.refresh()
            curses.napms(1200)
    else:
        cfg.width, cfg.height = dims


# --------------------------------------------------------------------------- #
# start / live status screen
# --------------------------------------------------------------------------- #

def usb_ready(win, cfg):
    """For USB transport, verify a USB-tether link is up. Returns False + warns.

    USB tethering replaces adb reverse, so no USB debugging is needed — but the
    phone must have 'USB tethering' toggled on so this PC gets a tether IP.
    """
    if cfg.transport != "usb":
        return True
    if core.usb_tether_ip():
        return True
    header(win)
    addstr(win, 3, 2, "USB transport selected but no USB-tether link is up.",
           curses.color_pair(4))
    addstr(win, 4, 2,
           "On the phone: Settings → Connections → Mobile Hotspot and Tethering "
           "→ enable 'USB tethering'.", curses.color_pair(5))
    addstr(win, 5, 2,
           "(No USB debugging required.) Or switch transport to WiFi in Configure.",
           curses.color_pair(5))
    addstr(win, 7, 2, "Press any key to return.", curses.color_pair(5))
    win.refresh()
    win.getch()
    return False


def live_screen(win, stream):
    """Show live status for the already-running stream.

    q / Esc  -> return to the menu, leaving the stream running.
    d        -> disconnect: stop the daemon and remove the virtual monitor.
    """
    disconnect = False
    win.nodelay(True)
    try:
        while True:
            cfg = stream.cfg                 # may change after a realtime restart
            # Recompute each pass: the USB-tether IP only appears once the phone
            # has 'USB tethering' enabled, which may happen after we start.
            if cfg.transport == "usb":
                ip = core.usb_tether_ip() or "<enable USB tethering on the phone>"
                target = f"{ip}:{cfg.port}   (USB tethering)"
            else:
                ip = core.lan_ip() or "<this-PC-LAN-IP>"
                target = f"{ip}:{cfg.port}   (WiFi)"
            live = stream.state["live"]
            exited = not stream.running()
            header(win)

            if exited:
                addstr(win, 3, 2, "■ DAEMON STOPPED",
                       curses.color_pair(4) | curses.A_BOLD)
            elif live > 0:
                addstr(win, 3, 2,
                       f"● LIVE  ({live} client{'s' if live > 1 else ''})",
                       curses.color_pair(3) | curses.A_BOLD)
            elif stream.state["ever"]:
                addstr(win, 3, 2, "○ PHONE DISCONNECTED",
                       curses.color_pair(4) | curses.A_BOLD)
            else:
                addstr(win, 3, 2, "◌ WAITING FOR PHONE",
                       curses.color_pair(5) | curses.A_BOLD)

            addstr(win, 5, 2, f"Monitor   : {cfg.output}  "
                              f"{cfg.width}x{cfg.height}@{cfg.fps}  "
                              f"side={cfg.side}  scale={cfg.scale}")
            addstr(win, 6, 2, f"Encoder   : {cfg.codec} VA-API @ {cfg.bitrate()}  "
                              f"({cfg.va_device})")
            addstr(win, 7, 2, "On the phone, connect to:", curses.A_BOLD)
            addstr(win, 8, 4, target, curses.color_pair(1) | curses.A_BOLD)

            if stream.state["ever"] and live == 0 and not exited:
                addstr(win, 10, 2,
                       "Phone dropped (cable out?). It reconnects automatically "
                       "when plugged back in.", curses.color_pair(5))

            addstr(win, 12, 2,
                   "q / Esc : back to menu (keeps streaming, lets you edit config)",
                   curses.color_pair(5))
            addstr(win, 13, 2,
                   "d       : disconnect — remove the monitor, restore its windows",
                   curses.color_pair(5))

            addstr(win, 15, 2, "daemon log:", curses.color_pair(5))
            for i, line in enumerate(list(stream.logs)):
                addstr(win, 16 + i, 4, line[:win.getmaxyx()[1] - 6],
                       curses.color_pair(5))

            footer(win, "q/Esc back (keep streaming)   d disconnect & return")
            win.refresh()

            c = win.getch()
            if c in (ord("d"), ord("D")):
                disconnect = True
                break
            if c in (ord("q"), 27):
                break
            if exited:
                disconnect = True       # daemon died on its own -> clean up
                break
            curses.napms(150)
    finally:
        win.nodelay(False)
        if disconnect:
            stream.disconnect()


# --------------------------------------------------------------------------- #
# entry
# --------------------------------------------------------------------------- #

def app(stdscr):
    curses.curs_set(0)
    curses.set_escdelay(25)        # snappy, unambiguous ESC vs. arrow keys
    stdscr.keypad(True)            # translate arrow/function keys to KEY_*
    init_colors()
    cfg = core.load_config()
    stream = None

    # first-run: nudge toward dependency install if anything required is missing
    first_missing = [p for (b, p, purpose, req, present)
                     in core.check_dependencies() if req and not present]

    while True:
        if first_missing:
            deps_screen(stdscr)
            first_missing = []
            continue
        choice = main_menu(stdscr, cfg, stream)
        if choice == "quit":
            if stream and stream.running():
                stream.disconnect()
            break
        elif choice == "configure":
            running = stream is not None and stream.running()
            saved, new_cfg = configure_screen(stdscr, cfg, live=running)
            if saved:
                cfg = new_cfg
                if running:
                    apply_config(stream, cfg)   # realtime: live or restart
        elif choice == "deps":
            deps_screen(stdscr)
        elif choice == "hardware":
            hardware_screen(stdscr)
        elif choice == "start":
            if stream is None or not stream.running():
                if not usb_ready(stdscr, cfg):
                    continue
                stream = Stream(cfg)
                stream.start()
            live_screen(stdscr, stream)
            if not stream.running():        # disconnected / died
                stream = None


def main():
    if not shutil.which("hyprctl"):
        print("This tool needs Hyprland (hyprctl) running.", file=sys.stderr)
        sys.exit(1)
    try:
        curses.wrapper(app)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
