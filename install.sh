#!/usr/bin/env bash
# Screen Extender — PC installer (Ubuntu / Fedora / Arch).
#
#   curl -fsSL https://raw.githubusercontent.com/saharshbaiju/amVD/main/install.sh | bash
#   # or, from a checkout:  ./install.sh
#
# Installs the Linux side into ~/.local/share/amvd, puts an `amvd` command in
# ~/.local/bin (so `amvd` works in any terminal), adds an app-menu entry, and installs the system packages your
# desktop needs (asks for sudo once). Re-run any time to update.
# Uninstall: ./install.sh --uninstall
set -euo pipefail

REPO="saharshbaiju/amVD"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/amvd"
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

say()  { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -rf "$DEST" "$BIN/amvd" "$APPS/amvd.desktop"
  say "Removed Screen Extender (settings kept in ~/.config/archscreenextender)."
  exit 0
fi

command -v python3 >/dev/null || { warn "python3 is required"; exit 1; }

# ---- 1. get the files: a local checkout, else the latest release, else main
SRC=""
here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -n "$here" && -f "$here/linux/extenderd.py" ]]; then
  SRC="$here/linux"
  say "Installing from this checkout ($here)"
else
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  url="https://github.com/$REPO/releases/latest/download/amvd-linux.tar.gz"
  if ! curl -fsSL "$url" -o "$tmp/amvd.tar.gz"; then
    url="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
    say "No release found; using the main branch"
    curl -fsSL "$url" -o "$tmp/amvd.tar.gz"
  fi
  tar -xzf "$tmp/amvd.tar.gz" -C "$tmp"
  SRC="$(dirname "$(find "$tmp" -name extenderd.py -path '*linux*' | head -1)")"
  say "Downloaded $(basename "$url")"
fi

# ---- 2. copy into place (replacing an older install)
mkdir -p "$DEST" "$BIN" "$APPS"
rm -rf "$DEST/linux"
cp -r "$SRC" "$DEST/linux"
find "$DEST/linux" -name __pycache__ -prune -exec rm -rf {} +
chmod +x "$DEST/linux/amvd" "$DEST/linux/start-extender.sh"
ln -sf "$DEST/linux/amvd" "$BIN/amvd"

cat > "$APPS/amvd.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Screen Extender
Comment=Use an Android phone as a second monitor
Exec=$BIN/amvd
Icon=video-display
Terminal=true
Categories=Utility;
EOF

# ---- 3. system packages for this distro + desktop (same list the TUI checks)
cd "$DEST/linux"
read -r -a PKGS <<<"$(python3 -c 'import core; print(" ".join(core.missing_packages(core.Config().resolved_backend())))')"
if (( ${#PKGS[@]} )); then
  say "Installing packages: ${PKGS[*]}"
  if command -v apt-get >/dev/null; then sudo apt-get update -qq || true; fi
  CMD="$(python3 -c 'import core,sys; c=core.install_command(sys.argv[1:]); print(" ".join(c) if c else "")' "${PKGS[@]}")"
  if [[ -n "$CMD" ]]; then
    $CMD || warn "Package install failed — open 'amvd' → Dependencies to retry."
  else
    warn "Unknown distro: install these yourself: ${PKGS[*]}"
  fi
else
  say "All required packages are already installed"
fi

backend="$(python3 -c 'import core; print(core.Config().resolved_backend() or "")')"
case "$backend" in
  gnome)    say "Desktop: GNOME (Wayland)";;
  hyprland) say "Desktop: Hyprland";;
  *)        warn "No supported desktop detected in this session (GNOME on Wayland or Hyprland)."
            warn "On Ubuntu, pick \"Ubuntu\" (not \"Ubuntu on Xorg\") on the login screen.";;
esac

# ---- 4. firewall: the phone connects in on TCP 5000 over Wi-Fi / tethering
#         (the USB-debugging path goes through adb on 127.0.0.1 and needs nothing)
FW="$(python3 -c '
import core
f = core.firewall_check(core.Config().port)
print((f["name"] + "|" + " ".join(f["fix"])) if f else "")')"
if [[ -n "$FW" ]]; then
  warn "${FW%%|*} may block the phone on Wi-Fi. To open the port:  ${FW#*|}"
  if [[ -r /dev/tty ]]; then
    read -r -p "Open it now? [Y/n] " ans </dev/tty || ans=n
    if [[ ! "$ans" =~ ^[Nn] ]]; then
      if python3 -c 'import core, subprocess, sys
f = core.firewall_check(core.Config().port)
sys.exit(subprocess.run(f["fix"]).returncode if f else 0)'; then
        say "Port opened"
      else
        warn "Couldn't open the port"
      fi
    fi
  fi
fi

# ---- 5. make `amvd` callable from any new terminal
on_path=1; case ":$PATH:" in *":$BIN:"*) ;; *) on_path=0;; esac
if (( ! on_path )); then
  line='export PATH="$HOME/.local/bin:$PATH"   # added by amvd installer'
  for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    [[ -f "$rc" || "$rc" == "$HOME/.bashrc" ]] || continue
    grep -qs '.local/bin' "$rc" || { echo "$line" >> "$rc"; say "Added ~/.local/bin to PATH in $(basename "$rc")"; }
  done
  if [[ -d "$HOME/.config/fish" ]]; then
    mkdir -p "$HOME/.config/fish/conf.d"
    echo 'fish_add_path -g $HOME/.local/bin' > "$HOME/.config/fish/conf.d/amvd.fish"
  fi
fi
say "Done. Open a new terminal and run:  amvd"
(( on_path )) || say "(or right now:  $BIN/amvd)"
say "Phone app: https://github.com/$REPO/releases/latest  (amVD-*.apk)"
