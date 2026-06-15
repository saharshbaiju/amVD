#!/usr/bin/env bash
# Convenience launcher for the Arch Screen Extender daemon.
#
#   ./start-extender.sh [usb|wifi] [extra extenderd args...]
#
#   usb  : USB tethering (NO USB debugging). Enable 'USB tethering' on the phone;
#          the phone then connects to this PC's tether IP (192.168.42.x).
#   wifi : just starts the daemon; connect the phone to this PC's LAN IP
set -euo pipefail

MODE="${1:-wifi}"; shift || true
PORT="${PORT:-5000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# This PC's IPv4 on Android's default USB-tether subnet (192.168.42.0/24).
tether_ip() {
  ip -j addr 2>/dev/null \
    | grep -oE '"local":"192\.168\.42\.[0-9]+"' \
    | head -n1 | grep -oE '192\.168\.42\.[0-9]+'
}

if [[ "$MODE" == "usb" ]]; then
  echo "USB tethering mode — enable 'USB tethering' on the phone (no USB debugging)."
  tip="$(tether_ip || true)"
  if [[ -n "$tip" ]]; then
    echo "Point the phone app at:  ${tip}:${PORT}"
  else
    echo "No USB-tether link yet. Enable USB tethering on the phone, then the"
    echo "phone connects to this PC's 192.168.42.x address on port ${PORT}."
  fi
elif [[ "$MODE" == "wifi" ]]; then
  ip=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
  echo "WiFi mode — point the phone app at:  ${ip:-<this-PC-LAN-IP>}:${PORT}"
else
  echo "usage: $0 [usb|wifi] [extra args]"; exit 1
fi

exec python3 "${HERE}/extenderd.py" --port "${PORT}" "$@"
