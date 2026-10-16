# Screen Extender — Platforms: Arch, Ubuntu, Fedora

The daemon picks a **backend** from the running session:

| Session | Detected by | Backend |
| --- | --- | --- |
| Hyprland | `$HYPRLAND_INSTANCE_SIGNATURE` | `hyprland` |
| GNOME on Wayland (Ubuntu, Fedora Workstation, …) | `$XDG_CURRENT_DESKTOP` contains `GNOME` and `$WAYLAND_DISPLAY` is set | `gnome` |
| GNOME on Xorg | — | not supported (virtual monitors need Wayland) |
| KDE, Sway, others | — | not yet (see roadmap) |

Override with *Configure → Desktop backend* in the TUI or `--backend` on the
daemon. The TUI's **Dependencies** screen shows exactly what's missing for your
distro and backend and installs it (`i`) with pacman / apt / dnf.

---

## Arch Linux + Hyprland

```bash
sudo pacman -S --needed hyprland wf-recorder python
# hardware encoding, by the GPU Hyprland renders on (x264 on the CPU otherwise):
sudo pacman -S --needed intel-media-driver   # Intel (Broadwell+); libva-intel-driver for older
sudo pacman -S --needed libva-mesa-driver    # AMD;  NVIDIA: nvidia-utils provides NVENC
sudo pacman -S --needed libva-utils android-tools   # optional: VA-API probe, phone size via adb
```

- Hyprland **0.55+** (Lua config parser) and older versions are both supported:
  monitor and workspace changes go through `hyprctl eval` with a legacy fallback.
- The encoder runs on the GPU Hyprland renders on (first entry of
  `AQ_DRM_DEVICES`): **NVENC** if that's an NVIDIA card (needs the proprietary
  driver; NVIDIA's VA-API driver can't encode), otherwise **VA-API** on that
  GPU's render node with the matching `LIBVA_DRIVER_NAME`. Force one with
  `--encoder h264_nvenc|h264_vaapi|libx264`. If the hardware encoder can't
  start (exits, or produces nothing for 4 s), the daemon falls back to x264
  on the CPU automatically. The TUI's Dependencies screen lists the VA-API
  driver your GPU needs (`intel-media-driver` / `libva-mesa-driver`) as optional.
- Scales are snapped to the nearest value Hyprland accepts for the resolution
  (whole logical pixels), e.g. 1.5 → 1.333 at 2772×1280.
- Workspaces 1–10 stay on the laptop, 11–20 live on the phone: move a window to
  workspace 11+ with your usual keybinds, or just drag it across the edge.

## Ubuntu (22.04 and newer, GNOME)

```bash
sudo apt install python3-gi gstreamer1.0-tools gstreamer1.0-pipewire \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly
# optional hardware encoding (Intel/AMD):
sudo apt install gstreamer1.0-vaapi        # or, on 24.04+, the va plugin in plugins-bad
sudo apt install adb                       # optional: "Match connected phone"
```

- Log in with **"Ubuntu"** (Wayland), not "Ubuntu on Xorg" — gear icon on the
  login screen. NVIDIA machines sometimes default to Xorg; the TUI tells you.
- `gstreamer1.0-plugins-ugly` provides `x264enc`; with a working VA-API driver,
  `vah264enc` is used automatically instead.

## Fedora Workstation (36 and newer, GNOME)

```bash
sudo dnf install python3-gobject gstreamer1 pipewire-gstreamer \
    gstreamer1-plugins-base gstreamer1-plugins-bad-free gstreamer1-plugin-openh264
sudo dnf install android-tools             # optional
```

- `gstreamer1-plugin-openh264` comes from the **fedora-cisco-openh264** repo,
  enabled by default on Workstation. Cisco hosts those RPMs themselves, and their
  mirror is occasionally slow or unreachable — just retry
  (`sudo dnf install --setopt=retries=20 …`).
- Fedora's Mesa ships with H.264 *encoding* disabled for patent reasons, so the
  VA-API path isn't available out of the box. For hardware encoding (or x264),
  enable **RPM Fusion** and install `mesa-va-drivers-freeworld` (AMD/Intel) or
  `gstreamer1-plugins-ugly` (x264). The backend picks the best available encoder
  automatically.

---

## How the GNOME backend works

1. Open a session on Mutter's private ScreenCast D-Bus API and call
   `RecordVirtual` with `cursor-mode = embedded` (the pointer is drawn into the
   frames) and `is-platform = true` (a real extended monitor, not a mirror).
2. `Start` the session; Mutter announces a PipeWire node.
3. `gst-launch-1.0 pipewiresrc path=<node> ! video/x-raw,width=W,height=H ! …`
   connects. The **caps decide the monitor's resolution**: Mutter creates a
   W×H monitor (connector `Meta-0`, …) the moment the format is negotiated.
4. The daemon notices the new connector in `DisplayConfig.GetCurrentState` and
   calls `ApplyMonitorsConfig` (temporary) to put it on the configured side of
   the primary and set its scale. Live side/scale edits from the TUI repeat
   this step without restarting the stream.
5. H.264 from the encoder goes through `h264parse` (SPS/PPS before every
   keyframe) into a pipe; from there it's the same daemon as on Hyprland.

**Tested:** Ubuntu 24.04 (Mutter 46, headless, in Docker): the virtual monitor
appears as `Meta-1`, is placed on the configured side, and H.264 (x264enc)
reaches the client and decodes. Fedora shares the same code path (only package
names differ); a full Fedora run is still pending.

When the daemon exits for any reason, its D-Bus connection closes and Mutter
removes the monitor and moves its windows back — nothing to clean up.

`pipewiresrc keepalive-time=1000` resends the last frame every second when the
desktop is idle (Mutter only sends frames on damage), and the daemon's GOP
replay gives a phone that connects to an idle desktop a picture immediately.

**Display settings:** while the extender runs, the virtual monitor appears in
*Settings → Displays* like any other; you can drag it there too. Placement made
by the extender is temporary — it never touches `~/.config/monitors.xml`.

---

## USB cable with USB debugging (all distros)

If the phone has **USB debugging** on and `adb` is installed, plugging in the
cable is enough: the TUI / `start-extender.sh` runs
`adb reverse tcp:5000 tcp:5000` for every connected phone and puts `127.0.0.1`
first in the QR. The phone then reaches the PC over the cable even when it's on
mobile data and not on your Wi-Fi.

## USB tethering (all distros)

The wired path uses **Android USB tethering**, not `adb`, so **no developer mode
or USB debugging** is needed: *Settings → Connections → Mobile Hotspot and
Tethering → USB tethering*. The phone becomes a USB network adapter and gives
the PC an address on `192.168.42.0/24` (vendor-dependent); the pairing QR then
includes that address.

> While tethering is on, NetworkManager may route internet through the phone.
> If that's unwanted, lower that connection's priority or switch off mobile data
> on the phone — the stream only needs the local link.

---

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Phone: *"reachable, but the extender isn't running"* | Start it in the TUI (`amvd` → Start extender); check the daemon log in the live view |
| Phone: *"Couldn't reach …"* | The live view's **connection checks** say why. *Firewall … press f*: opens TCP 5000 (ufw / firewalld / nftables / iptables are detected without root — note Arch's stock `/etc/nftables.conf` drops everything but SSH once `nftables.service` is enabled; `f` adds a runtime rule, add `tcp dport 5000 accept` to the file to keep it). *USB cable*: needs adb installed, USB debugging on, and "Allow USB debugging" accepted on the phone — then rescan the QR. Same Wi-Fi but still unreachable: guest/campus/hotel Wi-Fi isolates clients — use the cable or the phone's hotspot |
| Phone: *"Connected, but no video arrived"* | Encoder failed — the live view's daemon log shows `gst:` / wf-recorder errors; `python3 extenderd.py … </dev/null` prints everything |
| TUI: *"GNOME is running on X11"* | Log in to the Wayland session |
| GNOME: `could not create the virtual monitor` | GNOME < 40, or Mutter's ScreenCast interface not available (`gdbus introspect --session -d org.gnome.Mutter.ScreenCast -o /org/gnome/Mutter/ScreenCast`) |
| GNOME: monitor appears but on the wrong side | The log shows `could not place virtual monitor (…)`; drag it in *Settings → Displays* — the stream keeps working |
| Hyprland: resolution/scale ignored | Hyprland ≥ 0.55 needs `hyprctl eval`; the daemon uses it automatically — check `hyprctl eval 'return 1'` prints `ok` |
| Hyprland: `Unsupported format: bgra` / `Cannot create hw frames context` | Encoder on a different GPU than Hyprland; with `--encoder auto` it follows the compositor — check the daemon's `encoder:` log line |
| Hyprland: *"invalid scale"* or *"EXT-1 overlaps"* popup | Fixed: scale is snapped to a valid value; if you still see it, two daemons were running — the new one now waits for the old one to exit |
| Hyprland on Intel: connects, black screen | Install `intel-media-driver` (`vainfo` should list `VAProfileH264… VAEntrypointEncSlice*`); without it the daemon now falls back to x264 after 4 s — the log shows `encoder: …` lines for each attempt |
| Stutter on Wi-Fi | Use 5 GHz, lower *Quality*, or use USB tethering |
| Fedora: `openh264` download fails | Cisco mirror hiccup — retry, or use RPM Fusion's x264 (`gstreamer1-plugins-ugly`) |
