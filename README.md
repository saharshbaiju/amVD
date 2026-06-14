# Arch Screen Extender

Turn an Android phone into a **real low-latency secondary monitor** for an Arch
Linux / Hyprland (wlroots) machine — like spacedesk on Windows, but native and
GPU-accelerated end to end.

This is the **low-latency base**: display only, no touch input yet.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the whole system from the
  conceptual idea down to every pipeline stage, the two-GPU reasoning, the NAL
  flow, and the end-to-end latency budget.
- [`docs/ANDROID.md`](docs/ANDROID.md) — the mobile app: packages, platform APIs,
  the three source files, and how a frame travels from TCP byte to your eyes.

## How it works

```
Hyprland headless output (EXT-1)        a genuine GPU-backed secondary monitor;
        │                               drag windows onto it like any display
        │  wlr-screencopy, DMA-BUF (zero copy)
   wf-recorder + VA-API H.264           hardware encode on the AMD iGPU that
        │  (no B-frames, CBR)           Hyprland already renders on -> no GPU copy
        │
   extenderd.py                         splits raw Annex-B into NAL units,
        │  caches SPS/PPS, no buffering  starts each client on a keyframe
        │
   TCP  (USB via adb reverse, or WiFi LAN)
        │
   Android app  →  MediaCodec  →  Surface   hardware decode, rendered on arrival,
                                             KEY_LOW_LATENCY, zero jitter buffer
```

### Why this is fast (and the old approach wasn't)

The previous attempt topped out around **~1 second** of lag. The capture and
encode were never the problem — the latency lived in two places:

1. **A generic player on the phone (mpv/VLC).** Media players build a large
   jitter/demux buffer by design; that buffer *was* most of the second. The fix
   is a dedicated app that feeds NAL units straight into `MediaCodec` with no
   buffering — what this app does.
2. **A container (mpegts) + the wrong GPU.** We drop the container entirely
   (raw Annex-B) and encode with VA-API on the **AMD iGPU** that Hyprland renders
   on, so frames never get copied to the NVIDIA GPU.

Target end-to-end latency for this base: **well under 50 ms** on USB / good 5 GHz.

## Requirements (Arch)

```bash
sudo pacman -S wf-recorder libva-mesa-driver python   # capture + VA-API + daemon
# adb (for USB):  sudo pacman -S android-tools
```

Hyprland must be the running compositor (it provides the headless output).

## Run — Linux side

USB (most deterministic, recommended for the base):

```bash
cd linux
./start-extender.sh usb            # sets `adb reverse` + starts the daemon
```

WiFi:

```bash
cd linux
./start-extender.sh wifi           # prints the LAN IP to enter on the phone
```

Useful flags (forwarded to `extenderd.py`):

```
--width 1920 --height 1080 --fps 60     # virtual monitor mode
--bitrate 12M --gop 30                   # encoder bitrate / keyframe interval
--device /dev/dri/renderD128             # VA-API node (AMD iGPU; the default)
--port 5000
```

The headless output `EXT-1` is created on start and removed on exit. Place it
relative to your real screen with Hyprland as usual, e.g.:

```bash
hyprctl keyword monitor EXT-1,1920x1080@60,2560x0,1
```

## Run — Android side

Open `android/` in Android Studio and Run, **or** build from the CLI:

```bash
cd android
JAVA_HOME=/opt/android-studio/jbr ./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

> The CLI build needs JDK 17–21. The system JDK here is 26 (too new for the
> Gradle/AGP versions), so point `JAVA_HOME` at Android Studio's bundled JBR.

In the app:
- **USB:** host `127.0.0.1`, port `5000`, Connect. (Run the `usb` launcher first.)
- **WiFi:** host = the PC LAN IP the launcher printed, port `5000`, Connect.

Tap the video to bring the connection overlay back.

## Project layout

```
linux/
  extenderd.py        capture -> encode -> NAL parse -> TCP broadcast
  start-extender.sh   USB/WiFi launcher
android/
  app/src/main/java/com/archextender/
    MainActivity.kt   fullscreen SurfaceView + connect overlay
    StreamSession.kt  TCP + MediaCodec decode -> Surface (no buffering)
    NalReader.kt      Annex-B -> NAL unit splitter
```

## Roadmap (after the base proves out)

- **RTP/UDP + FEC** transport for lossy WiFi (TCP head-of-line blocking hurts
  there); keep TCP for USB.
- **HEVC** option (`vah265enc`) for lower bitrate at the same quality.
- **Resolution auto-match** to the phone's panel + DPI.
- **Touch / pen input** back to the PC (uinput / virtual-pointer protocol).
- A small **system tray / hotplug** UX so plugging the phone "just works".
- Latency HUD (capture→display timestamp overlay) for tuning.
```
