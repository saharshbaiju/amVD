# Screen Extender

Turn an Android phone into a **real, low-latency second monitor** for Linux —
like spacedesk on Windows, but native and GPU-accelerated end to end.

| Desktop | Distros | How the virtual monitor is made |
| --- | --- | --- |
| **Hyprland** | Arch (and anything running Hyprland) | `hyprctl output create headless` + wf-recorder / VA-API |
| **GNOME on Wayland** | **Ubuntu 22.04+**, **Fedora 36+**, any GNOME 40+ | Mutter's virtual-monitor API + PipeWire + GStreamer |

**Touch works too:** tap, drag, scroll and pinch on the phone to control what's
on it — through a virtual touchscreen, so your mouse pointer stays where it is.
Only a phone that scanned the computer's QR code may control it. Pair by
**scanning a QR code** in the terminal; connect over a **USB cable** (adb, automatic when USB debugging is on),
**USB tethering** (no USB debugging needed) or **Wi-Fi**.

## Documentation

- [`docs/PLATFORMS.md`](docs/PLATFORMS.md) — install & setup on **Arch / Ubuntu /
  Fedora**, how the GNOME backend works, troubleshooting.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the whole pipeline, the wire
  protocol, the reliability rules, the latency budget, and the bugs fixed.
- [`docs/ANDROID.md`](docs/ANDROID.md) — the phone app: UI, components, decode
  path, developer workflow.
- [`docs/RELEASING.md`](docs/RELEASING.md) — how releases are built, signed and
  published (APK + Linux tarball on GitHub Releases).

## Install

### On the computer (Ubuntu, Fedora, Arch)

```bash
curl -fsSL https://raw.githubusercontent.com/saharshbaiju/amVD/main/install.sh | bash
```

Then open a new terminal and run **`amvd`**.

The installer puts the program in `~/.local/share/amvd`, adds the `amvd` command
(and adds `~/.local/bin` to your shell's PATH if needed), creates a *Screen
Extender* app-menu entry, and installs the packages your distro and desktop need
with `apt` / `dnf` / `pacman` (it asks for your password once). Re-run the same
line to update; `./install.sh --uninstall` removes it. From a git checkout,
`./install.sh` installs that checkout instead.

> **Ubuntu / Fedora:** log in to the GNOME **Wayland** session (the default; on
> Ubuntu it's "Ubuntu", not "Ubuntu on Xorg"). **Fedora:** the H.264 encoder
> comes from the `fedora-cisco-openh264` repo, enabled by default on Workstation.

### On the phone (Android 8+)

Download **`amVD-<version>.apk`** from the
[latest release](https://github.com/saharshbaiju/amVD/releases/latest) on the
phone, open it, and allow "install unknown apps" for your browser when asked.

> **"App not installed" / "package conflicts with an existing package"?** The
> phone still has an old *Screen Extender* that was signed with a different key
> (a test build from before 0.4.0). Android never lets one key's app replace
> another's, so uninstall *Screen Extender* once, then install the APK again.
> Later updates install over it normally. (The `-debug.apk` is a separate app,
> *Screen Extender (debug)*, which is why it installed when the normal one didn't.)

### Connect

1. Run `amvd` → **Start extender**. A QR code appears.
2. Pick how the phone reaches the computer:
   - **USB cable** (lowest latency): enable *USB debugging* on the phone and plug
     it in — `amvd` sets up `adb reverse` by itself. Works even when the phone is
     on mobile data.
   - **USB tethering**: *Settings → Hotspot & tethering → USB tethering* (no
     developer options needed).
   - **Wi-Fi**: phone and computer on the same network. If a firewall is in
     the way, the live view says so — press **f** to open port 5000.

   While it waits for the phone, the live view shows **connection checks**
   (USB debugging authorised? firewall? Wi-Fi) explaining why a phone can't
   connect yet.
3. In the app tap **Scan QR code**, scan the terminal, tap **Connect**. Ripples
   spread from your finger, then the screen dissolves into your extended desktop.
4. Touch the phone's screen to use it. On **Hyprland** touch needs a one-time
   permission for `/dev/uinput` — the installer offers it, or press **t** in the
   live view. On **GNOME** there's nothing to set up.

Pointing the phone's **stock camera** at the QR works too — it's an `asext://`
link that opens the app with your computer filled in.

Without the TUI: `~/.local/share/amvd/linux/start-extender.sh usb` prints the
same QR and runs the daemon in the foreground.

## How it works

```
virtual monitor (Hyprland headless output / Mutter virtual monitor)
        │  zero-copy capture
   H.264 encode on the compositor's GPU (NVENC / VA-API, x264 fallback;
                GStreamer VA / x264 / openh264 on GNOME)
        │  no B-frames, one slice per frame, CBR
   extenderd.py   splits NAL units, caches the current GOP, one sender
        │         thread per phone, 1 s heartbeats, resync on keyframes
   TCP  — USB tethering, Wi-Fi, or adb reverse (every QR address tried in parallel)
        │                                        ▲
   Android app → MediaCodec → Surface            │ touches (paired phones only)
                hardware decode, rendered on arrival
                                                 │
   phone_input.py → virtual touchscreen: uinput (Hyprland) / Mutter RemoteDesktop (GNOME)
```

Target glass-to-glass latency: **~25–45 ms** over USB with hardware encoding.

### Why it's fast

The old approach (~1 s lag) used a media player with a jitter buffer, an mpegts
container, and encoded on the wrong GPU. This one feeds raw H.264 straight into
the phone's hardware decoder with no buffering, and encodes on the GPU the
compositor already renders on. Details in
[`ARCHITECTURE.md` §2](docs/ARCHITECTURE.md#2-why-the-previous-approach-was-slow-and-this-isnt).

### Why it stays connected

The previous build "connected, then disconnected by itself". The causes — a
session race on the phone, frames truncated mid-send on resync, no heartbeat,
undersized decoder buffers, and broken `hyprctl` commands on Hyprland 0.55+ — are
all fixed; see the table in
[`ARCHITECTURE.md` §7](docs/ARCHITECTURE.md#7-bugs-fixed-in-this-revision-and-why-they-happened).
If the link drops after streaming (cable pulled, Wi-Fi roam, settings change),
the app shows *Reconnecting…* over the last frame and resumes by itself.

## The terminal UI

```bash
amvd
```

- **Start extender** — launches the daemon and opens the live view: status, the
  pairing **QR code**, the plain addresses, touch status, and the daemon log. `q` returns to the
  menu *keeping the stream running*; `d` disconnects and removes the monitor.
- **Configure** — desktop backend (auto / hyprland / gnome), screen **side**
  (left / right / up / down), **resolution** (presets or *Match connected phone*
  via adb), **FPS**, desktop **scale**, **quality** (bitrate), keyframe interval,
  **preferred link** (which address goes first in the QR), **port**, **touch
  input** (on / off), and **paired phones → forget all** (new pairing key; every
  phone must rescan the QR to touch again). Saving while
  streaming applies side/scale instantly; other changes restart the stream.
  Stored in `~/.config/archscreenextender/config.json`.
- **Dependencies** — per distro (pacman / apt / dnf) and per backend; `i`
  installs what's missing.
- **Detected hardware** — desktop, primary monitor, VA-API node, USB tether
  link, phone panel size (adb), LAN IP.

### Daemon directly (advanced / scripting)

```bash
cd linux
./start-extender.sh usb|wifi [flags]     # prints the QR, runs in the foreground
python3 extenderd.py --help
```

```
--backend auto|hyprland|gnome            # auto-detected from the session
--width 2400 --height 1080 --fps 60      # virtual monitor mode
--side right|left|up|down --scale 1.5    # placement (applied live on SIGHUP)
--bitrate 12M --gop 30                   # encoder
--encoder auto|vah264enc|x264enc|…       # GNOME: GStreamer element to use
--encoder auto|h264_nvenc|h264_vaapi|libx264  # Hyprland: compositor GPU, x264 fallback
--device /dev/dri/renderD128             # Hyprland: VA-API node (h264_vaapi)
--port 5000
--pairing-key-file ~/.config/archscreenextender/pairing-key   # enables touch for phones with this key
--no-touch                               # view only
```

## The Android app

Users install the APK from GitHub Releases (above). To build it yourself:

```bash
cd android
JAVA_HOME=/opt/android-studio/jbr ./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

(Or open `android/` in Android Studio and press Run.) The CLI build needs JDK
17–21; point `JAVA_HOME` at Android Studio's bundled JBR if your system JDK is
newer. Requires Android 8.0+; the QR scanner uses Google Play services (no
camera permission). Material You colors on Android 12+, the shader-based water
transition on Android 13+ (a canvas version on older phones).

## Developing

```bash
python3 linux/testsrc.py --width 2400 --height 1080   # fake desktop, real daemon code
python3 linux/testsrc.py --log-touch                  # …and print the touches it receives
adb shell am start -a android.intent.action.VIEW -d "'asext://c?h=10.0.2.2&p=5000'"   # emulator
adb logcat -s StreamSession
```

`testsrc.py` streams a test pattern through the real broadcaster without creating
a monitor, so the app can be developed on any machine (the emulator reaches the
PC at `10.0.2.2`). More in [`ANDROID.md` §7](docs/ANDROID.md#7-developer-install--debugging).

## Project layout

```
linux/
  amvd                  TUI launcher (installed as the `amvd` command)
  python.sh             picks the system /usr/bin/python3 (not conda/pyenv) for
                        amvd, start-extender.sh and install.sh
  tui.py                curses UI: deps, detection, config, live view + QR
  core.py               config, distro/dependency detection, addresses, QR URI
  qr.py                 dependency-free QR encoder (half-block terminal output)
  extenderd.py          daemon: backend → NAL parse → GOP cache → TCP broadcast
  phone_input.py        what phones send back: pairing hello, touches → slots
  uinput_touch.py       virtual multi-touch touchscreen via /dev/uinput (Hyprland)
  backends/hyprland.py  headless output, hyprctl Lua API, wf-recorder, touch mapping
  backends/gnome.py     Mutter ScreenCast virtual monitor, DisplayConfig, GStreamer,
                        RemoteDesktop touch
  start-extender.sh     no-TUI launcher (prints the QR)
  testsrc.py            test-pattern server for app development
android/app/src/main/java/com/archextender/
  MainActivity.kt       Material 3 UI, scanner, transitions, side sheet
  StreamSession.kt      parallel connect, framed reader, MediaCodec → Surface
  RippleRevealLayout.kt water ripple + reveal (AGSL)
  ConnectTarget.kt      asext:// QR / deep link parsing (incl. pairing key)
  FrameReader.kt        NALU / ASX1 / PING / INPT framing
  FrameWriter.kt        HELO / TUCH (phone → computer)
  TouchForwarder.kt     MotionEvent → touch frames
  AspectRatioSurfaceView.kt  letterboxing
docs/                   PLATFORMS, ARCHITECTURE, ANDROID, RELEASING
install.sh              one-line installer (any distro), adds the `amvd` command
```

## Roadmap

- RTP/UDP + FEC for lossy Wi-Fi (keep TCP for USB)
- HEVC decode in the app
- KDE Plasma backend
- Pen input (pressure, hover) and typing from the phone
- TLS with the certificate pinned in the QR
- Latency HUD
