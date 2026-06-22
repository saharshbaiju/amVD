# Arch Screen Extender — Android App

The phone side is a deliberately **thin, native client**. Its only job: read an
Annex-B H.264 stream off a TCP socket and draw it on screen with the lowest
possible latency. It holds no concept of "monitor" — that all lives on the Linux
side (see [`ARCHITECTURE.md`](./ARCHITECTURE.md)).

Everything it needs is in the **Android platform itself** — there are no
third-party media libraries (no ExoPlayer, no FFmpeg, no GStreamer). That is a
feature: every extra library adds buffering and latency. We talk directly to the
OS hardware-decode API.

---

## 1. Build setup & packages

### Toolchain

| Component        | Version         | Notes                                                    |
| ---------------- | --------------- | -------------------------------------------------------- |
| Language         | Kotlin 2.0.21   |                                                          |
| Android Gradle Plugin | 8.9.1      | Pinned in `android/build.gradle.kts`.                    |
| Gradle           | 8.11.1          | Pinned in the wrapper.                                   |
| `compileSdk` / `targetSdk` | 36    | Only platform installed (`android-36`).                  |
| `minSdk`         | 26 (Android 8.0) |                                                         |
| JDK for building | 21 (`/opt/android-studio/jbr`) | System JDK is 26 — too new for this Gradle/AGP. |

> Build from CLI with `JAVA_HOME=/opt/android-studio/jbr ./gradlew :app:assembleDebug`,
> or just open `android/` in Android Studio (it uses its bundled JDK automatically).

### Dependencies (`app/build.gradle.kts`)

```kotlin
implementation("androidx.core:core-ktx:1.13.1")     // Kotlin extensions
implementation("androidx.appcompat:appcompat:1.7.0") // AppCompatActivity, themes
```

That's the entire dependency list. **No media library at all.** Video decode is
done with `android.media.MediaCodec`, which is part of the OS.

### Platform APIs used (all built into Android)

| API                          | Package           | Role                                              |
| ---------------------------- | ----------------- | ------------------------------------------------- |
| `MediaCodec`                 | `android.media`   | Hardware H.264 decoder.                           |
| `MediaFormat`                | `android.media`   | Describes the stream (codec, SPS/PPS, low-latency flag). |
| `SurfaceView` / `SurfaceHolder` | `android.view` | A dedicated drawing surface the decoder renders into directly. |
| `Surface`                    | `android.view`    | The decoder's output target (a GPU buffer queue). |
| `Socket` / `InetSocketAddress` | `java.net`      | Plain TCP client.                                 |

### Permissions (`AndroidManifest.xml`)

```xml
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
<application android:usesCleartextTraffic="true" …>
```

`usesCleartextTraffic` is required because we use a raw TCP socket (not HTTPS) to
`127.0.0.1` / a LAN IP.

The activity is locked to landscape and configured to survive rotation/resizes:

```xml
android:screenOrientation="userLandscape"
android:configChanges="orientation|screenSize|screenLayout|keyboardHidden"
```

---

## 2. Source layout

```
app/src/main/
├── AndroidManifest.xml
├── java/com/archextender/
│   ├── MainActivity.kt     UI: fullscreen SurfaceView + connect overlay, lifecycle
│   ├── StreamSession.kt    the engine: TCP → MediaCodec → Surface
│   └── NalReader.kt        Annex-B byte stream → discrete NAL units
└── res/
    ├── layout/activity_main.xml   SurfaceView + host/port/connect overlay
    └── values/                    strings, theme (fullscreen, black background)
```

Three Kotlin files. Each has one job.

---

## 3. The components

### 3.1 `MainActivity` — UI and lifecycle

- Inflates a fullscreen `SurfaceView` (via **ViewBinding**) with a small overlay
  holding a **host** field (default `127.0.0.1` for USB), a **port** field
  (default `5000`), and a **Connect** button.
- Adds `FLAG_KEEP_SCREEN_ON` so the phone never sleeps while acting as a monitor.
- Waits for the `SurfaceHolder` callback (`surfaceCreated`) before allowing a
  connection — you can't decode into a surface that doesn't exist yet.
- On **Connect**, it constructs a `StreamSession` with the surface, host, and
  port. On success it hides the overlay and enters **immersive fullscreen**
  (status/nav bars hidden). Tapping the video brings the overlay back.
- Implements `StreamSession.Listener` to react to `onConnected` / `onStopped`
  (updates the button text and status line, restores the overlay on disconnect).
- Tears the session down in `surfaceDestroyed` and `onDestroy` so the decoder and
  socket are always released.

### 3.2 `NalReader` — Annex-B → NAL units

The TCP socket delivers an arbitrary stream of bytes; H.264 decoders want
**whole NAL units**. `NalReader` bridges that.

- Reads from the socket's `InputStream` in 64 KB chunks into a growable buffer.
- Scans for start codes (`00 00 01` or `00 00 00 01`) using a *search cursor* so
  it doesn't re-scan bytes it has already examined.
- A NAL is only emitted once the **start of the next** NAL is seen — guaranteeing
  each unit handed to the decoder is complete. The remaining partial NAL stays in
  the buffer until more bytes arrive.
- Returns each NAL **with its leading start code intact**, because that's exactly
  what `MediaCodec` expects for Annex-B input and for the `csd-` parameter sets.
- `nalType(nal)` returns `header_byte & 0x1F` so the session can recognise SPS
  (7), PPS (8), IDR (5), etc.

This mirrors the daemon's parser on the Linux side — both ends speak the same
NAL-unit language.

### 3.3 `StreamSession` — the decode engine

This is the heart of the client. One object owns one streaming session and runs
on its **own thread**.

**Flow:**

```
connect TCP (tcpNoDelay = true)
   │
NalReader.next()  ──loop──▶  handleNal(type, nal)
                                 │
              type 7 (SPS) ──────┤ cache sps
              type 8 (PPS) ──────┤ cache pps
                                 │
              once sps & pps known → configure() the decoder
                                 │
              if configured → feedAndDrain(nal)
```

**`configure(sps, pps)`** builds a `MediaFormat` for `video/avc` and attaches the
parameter sets as **codec-specific data**:

```kotlin
val fmt = MediaFormat.createVideoFormat(MIMETYPE_VIDEO_AVC, 1920, 1080)
fmt.setByteBuffer("csd-0", ByteBuffer.wrap(sps))   // SPS
fmt.setByteBuffer("csd-1", ByteBuffer.wrap(pps))   // PPS
if (SDK_INT >= R) fmt.setInteger(KEY_LOW_LATENCY, 1)  // Android 11+
codec = MediaCodec.createDecoderByType(MIMETYPE_VIDEO_AVC)
codec.configure(fmt, surface, null, 0)             // decode straight to Surface
codec.start()
```

Key points:

- The `1920×1080` here is only a **hint**. The decoder reads the true resolution
  from the SPS and emits `INFO_OUTPUT_FORMAT_CHANGED`; the `Surface` scales to
  fit. So the same app handles any virtual-monitor resolution without changes.
- **`KEY_LOW_LATENCY`** (Android 11+) tells the decoder to skip its internal
  reorder/look-ahead buffer. This is safe *only* because the encoder emits no
  B-frames — frames already arrive in display order.
- `configure(fmt, surface, …)` binds the decoder's output directly to the
  on-screen `Surface`. Decoded frames are **never copied into app memory**; the
  GPU hands them straight to the display compositor.

**`feedAndDrain(nal)`** — the zero-buffer pump:

```kotlin
// 1. get an input buffer (drain outputs while waiting so we never stall)
var inIdx = codec.dequeueInputBuffer(0)
while (inIdx < 0 && running) { drain(codec); inIdx = codec.dequeueInputBuffer(2000) }

// 2. copy this NAL in and queue it
codec.getInputBuffer(inIdx).put(nal)
codec.queueInputBuffer(inIdx, 0, nal.size, ptsUs++, 0)

// 3. push out every ready frame immediately
drain(codec)
```

**`drain(codec)`** releases every available decoded frame to the screen at once,
with `render = true`:

```kotlin
while (true) {
    val out = codec.dequeueOutputBuffer(info, 0)
    if (out >= 0) codec.releaseOutputBuffer(out, /* render = */ true)
    else break
}
```

`releaseOutputBuffer(out, true)` is what actually paints the frame onto the
`Surface`. There is **no queue, no scheduling, no "presentation time" pacing** on
the app side — a frame is shown the moment it's decoded. Display vsync is the only
thing that gates it, and that's unavoidable.

**Teardown** (`release()`): stop and release the codec, close the socket. Called
from the session thread's `finally` so it always runs, on error or normal stop.

---

## 4. Why each choice keeps latency low

| Decision                                   | Effect                                                     |
| ------------------------------------------ | ---------------------------------------------------------- |
| No media library (raw `MediaCodec`)        | No hidden jitter/demux buffer — the #1 fix vs. mpv/VLC.    |
| Decode **directly to a `Surface`**         | Zero frame copies into app memory; GPU → display path.     |
| `KEY_LOW_LATENCY = 1`                      | Decoder skips its reorder buffer.                          |
| Encoder sends **no B-frames**              | Frames arrive in display order → can render on arrival.    |
| `releaseOutputBuffer(idx, true)` instantly | No app-side presentation scheduling.                       |
| `tcpNoDelay = true`                        | Small NAL writes aren't delayed by Nagle's algorithm.      |
| Daemon starts client on a **keyframe**     | Valid picture from the first frame; no decode errors/flicker on connect. |

---

## 5. How a frame travels through the app

```
TCP bytes
   │  java.net.Socket  (tcpNoDelay)
   ▼
NalReader  ── splits on 00 00 00 01 ──▶  one complete NAL (with start code)
   │
   ▼
StreamSession.handleNal
   │   SPS/PPS → MediaFormat csd-0 / csd-1 (decoder configured once)
   │   IDR / P-frame ↓
   ▼
MediaCodec input buffer ──▶ [hardware H.264 decode] ──▶ MediaCodec output buffer
   │
   ▼
releaseOutputBuffer(index, render=true)
   │
   ▼
Surface (SurfaceView)  ──▶  display compositor  ──▶  your eyes
```

---

## 6. Running it

In the app: enter host (the PC tether IP `192.168.42.x` for USB tethering, or the
PC's LAN IP for WiFi) and port `5000`, then **Connect**. The overlay disappears
and your `EXT-1` desktop appears. Tap the screen to bring the overlay back to
disconnect.

> **Plain end users need neither `adb` nor developer mode.** They install the APK
> and use USB tethering (see the README). `adb` below is only for *developing /
> debugging* the app.

---

## 7. Developer install & debugging

This section is for iterating on the app itself — installing debug builds,
reading logs, and driving it from the command line. It needs **USB debugging**
(Settings → Developer options → USB debugging) — which the end-user path does not.

### 7.1 Build + install over USB

```bash
cd android

# one-step build-and-install onto the connected device
JAVA_HOME=/opt/android-studio/jbr ./gradlew installDebug

# …or build the APK and push it yourself
JAVA_HOME=/opt/android-studio/jbr ./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

> The CLI build needs **JDK 17–21**. The system JDK here is 26 (too new for this
> Gradle 8.11 / AGP 8.9), so always point `JAVA_HOME` at Android Studio's bundled
> JBR (JDK 21). Opening `android/` in Android Studio and pressing **Run** uses
> that JBR automatically and skips all of this.

### 7.2 Launch + drive from the command line

```bash
# plain launch
adb shell am start -n com.archextender/.MainActivity

# launch AND connect immediately (uses the host/port last saved in the app)
adb shell am start -n com.archextender/.MainActivity --ez autoconnect true
```

The only recognised intent extra is `autoconnect` (boolean). Host and port are
**not** settable by intent — the app reads them from its own EditText fields and
persists them in `SharedPreferences`, so set them once in the UI and
`autoconnect` reuses the saved pair on later launches.

> **This device blocks `adb` input injection** (`input tap` / `input keyevent` →
> `SecurityException`). You cannot script taps — use the `--ez autoconnect true`
> intent above to reach the connected state instead of tapping **Connect**.

### 7.3 Logs

```bash
adb logcat --pid=$(adb shell pidof com.archextender)   # only this app's logs
adb logcat *:E                                          # all errors, system-wide
adb logcat -c                                           # clear the buffer first
```

### 7.4 Wireless debugging (no cable)

Run everything above over Wi-Fi — install, `am start`, `logcat` all work
identically once the device is connected wirelessly. The phone and PC must be on
the **same network**.

**Android 11+ — pairing (no USB ever needed):**

On the phone: Settings → Developer options → **Wireless debugging** → on →
**Pair device with pairing code**. Then on the PC:

```bash
adb pair <phone-ip>:<pair-port>          # type the 6-digit code shown on the phone
adb connect <phone-ip>:<connect-port>    # IP:port from the *main* Wireless-debugging
                                         # screen — a DIFFERENT port than pairing
adb devices -l                           # confirm it shows as "device"
```

**Android 10 or older — bootstrap over USB once:**

```bash
adb -d tcpip 5555                  # with the phone plugged in
# unplug USB
adb connect <phone-ip>:5555
```

Then build/install/launch/log exactly as in 7.1–7.3 — `adb` no longer cares that
the transport is Wi-Fi.

> **Wireless `adb` ≠ the wireless video path.** `adb` debugging over Wi-Fi only
> ferries dev commands. The *stream* still connects straight to the PC's LAN IP on
> port 5000 (the WiFi transport) — there is no `adb reverse`/tunnel involved on the
> WiFi path. Wireless `adb` also drops when the phone sleeps or roams; just re-run
> `adb connect`.

---

## 8. Current limitations (base) & next steps

- **TCP only.** Great over USB; on lossy WiFi a dropped packet can briefly freeze
  the picture (head-of-line blocking). Planned: RTP/UDP + FEC for WiFi.
- **No input.** Display only — touch/pen events aren't sent back yet.
- **Manual connect.** Host/port typed by hand; no discovery or auto-reconnect.
- **H.264 only.** HEVC (`vah265enc` + `MIMETYPE_VIDEO_HEVC`) would cut bitrate.
- **Fixed format hint.** Works for any resolution today; could read the panel size
  and ask the daemon to match the virtual monitor to the phone exactly.
