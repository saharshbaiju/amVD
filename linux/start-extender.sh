#!/usr/bin/env bash
# Convenience launcher for the Arch Screen Extender daemon.
#
#   ./start-extender.sh [usb|wifi] [extra extenderd args...]
#
#   usb  : also sets up `adb reverse` so the phone reaches the PC at 127.0.0.1
#   wifi : just starts the daemon; connect the phone to this PC's LAN IP
set -euo pipefail

MODE="${1:-wifi}"; shift || true
PORT="${PORT:-5000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$MODE" == "usb" ]]; then
  if ! command -v adb >/dev/null; then echo "adb not found"; exit 1; fi
  echo "waiting for USB device (enable USB debugging)…"
  adb wait-for-device
  adb reverse "tcp:${PORT}" "tcp:${PORT}"
  echo "adb reverse set: phone 127.0.0.1:${PORT} -> PC :${PORT}"
elif [[ "$MODE" == "wifi" ]]; then
  ip=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
  echo "WiFi mode — point the phone app at:  ${ip:-<this-PC-LAN-IP>}:${PORT}"
else
  echo "usage: $0 [usb|wifi] [extra args]"; exit 1
fi

exec python3 "${HERE}/extenderd.py" --port "${PORT}" "$@"
