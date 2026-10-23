#!/usr/bin/env bash
# Convenience launcher for the Screen Extender daemon (no TUI).
#
#   ./start-extender.sh [usb|wifi] [extra extenderd args...]
#
# Prints the pairing QR code (scan it with the phone app), then runs the daemon
# in the foreground with its logs on stderr. The desktop backend (Hyprland /
# GNOME) is auto-detected; force one with --backend.
#
#   usb  : USB tethering (NO USB debugging) — the tether address goes first in
#          the QR. Enable 'USB tethering' on the phone before scanning.
#   wifi : the LAN address goes first in the QR.
set -euo pipefail

MODE="${1:-usb}"; shift || true
PORT="${PORT:-5000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/python.sh"   # the system Python, not whatever python3 is on PATH
PY="$(amvd_python)" || { echo "python3 is required" >&2; exit 1; }

if [[ "$MODE" != "usb" && "$MODE" != "wifi" ]]; then
  echo "usage: $0 [usb|wifi] [extra extenderd args]"; exit 1
fi

ASEXT_HERE="$HERE" "$PY" - "$MODE" "$PORT" <<'EOF'
import os, sys
sys.path.insert(0, os.environ["ASEXT_HERE"])
import core, qr
cfg = core.load_config()
cfg.transport, cfg.port = sys.argv[1], int(sys.argv[2])
uri = core.connect_uri(cfg)
# black modules on a white field, whatever the terminal theme is
for line in qr.render_halfblocks(qr.encode(uri, "M"), quiet=2):
    print("  \033[30;47m" + line + "\033[0m")
print(f"\n  Scan with the Screen Extender app, or connect to one of:")
for ip in core.candidate_ips(cfg):
    print(f"    {ip}:{cfg.port}")
if cfg.transport == "usb" and not core.usb_tether_ip():
    print("\n  (no USB-tether link yet: enable 'USB tethering' on the phone,"
          " then rerun so its address is in the QR)")
print()
EOF

# The QR carries the pairing key (core.connect_uri creates it on first use); the
# daemon reads the same file so phones that scanned the QR may use touch.
KEY_FILE="$HOME/.config/archscreenextender/pairing-key"   # core.PAIRING_KEY_PATH
exec "$PY" "${HERE}/extenderd.py" --port "${PORT}" \
  --pairing-key-file "${KEY_FILE}" "$@" </dev/null
