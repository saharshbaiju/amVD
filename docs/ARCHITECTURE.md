# Arch Screen Extender — Architecture & Pipeline

This document explains the whole system, from the conceptual idea down to every
stage of the pipeline and the exact bytes that move between them.

---

## 1. The core idea

We want a phone to behave like a **real second monitor** for an Arch Linux /
Hyprland machine — drag a window "onto" the phone, watch it appear there with
near-zero lag.

A monitor, to the operating system, is just **an output the compositor can draw
into**. It does not have to be physical. So the plan is:

1. **Invent a monitor that doesn't physically exist** (a *headless output*). The
   compositor renders into it exactly like a real screen.
2. **Continuously capture** whatever the compositor draws there.
3. **Compress** those frames into a video stream (on the GPU, so it's cheap).
4. **Ship** the stream to the phone over a wire (USB) or the network (WiFi).
5. **Decode and display** the stream on the phone with **no buffering**, so what
   you see is only a few milliseconds behind reality.

The phone is a *thin client*: it never knows it's a "monitor". It just decodes a
video and draws it. All the "monitor-ness" lives on the Linux side.

```
   ┌─────────────────────────────────────────────┐        ┌──────────────┐
   │  Arch Linux + Hyprland                       │        │  Android     │
   │                                              │        │  phone       │
   │  [headless output EXT-1]  ← apps draw here   │        │              │
   │            │ frames (GPU memory)             │        │              │
   │            ▼                                 │        │              │
   │  [capture + H.264 encode]  (AMD iGPU)        │        │              │
   │            │ raw H.264 NAL units             │        │              │
   │            ▼                                 │        │              │
   │  [extenderd.py: split + broadcast]           │        │              │
   │            │ TCP bytes                       │        │              │
   └────────────┼─────────────────────────────────┘        │              │
                │  USB (adb reverse)  or  WiFi (LAN)        │              │
                └──────────────────────────────────────────▶  [MediaCodec]│
                                                            │      │       │
                                                            │      ▼       │
                                                            │  [Surface]   │
                                                            │  = your eyes │
                                                            └──────────────┘
```

---

## 2. Why the previous approach was slow (and this isn't)

An earlier version reached only ~1 second of lag. The important realization:
**capture and encoding were never the bottleneck.** The latency lived in three
avoidable places.

| Old design                         | Problem                                            | This design                                   |
| ---------------------------------- | -------------------------------------------------- | --------------------------------------------- |
| Phone played the stream with mpv / VLC | Media players add a 0.5–2 s **jitter buffer** by design — this *was* most of the second | A **dedicated app** feeds frames straight to the hardware decoder with **no buffer** |
| Stream wrapped in **mpegts** container | The player must demux 188-byte TS packets before decoding | **Raw Annex-B** H.264 — the decoder eats it directly |
| Encoded with **NVENC** (NVIDIA)    | Hyprland renders on the **AMD** iGPU, so every frame was copied AMD → NVIDIA before encoding | Encode with **VA-API on the same AMD iGPU** — zero copy |
| VKMS kernel virtual monitor        | CPU-rendered, caused session crashes               | `hyprctl output create headless` — GPU-backed, stable |

The guiding principle of the rewrite: **never let a frame sit in a buffer
waiting.** Every stage forwards its output the instant it's ready, and if a
consumer ever falls behind, we *drop* to the next keyframe instead of queuing a
backlog. Latency therefore cannot accumulate over time.

---

## 3. The hardware reality on this machine

This laptop has **two GPUs**:

```
renderD129  →  NVIDIA RTX 4060 (dGPU)
renderD128  →  AMD Radeon 890M (iGPU)   ← Hyprland renders here
```

Hyprland's environment shows `AQ_DRM_DEVICES=/dev/dri/card1:/dev/dri/card0`,
where `card1` is the AMD device. That means the compositor's framebuffers —
including the headless output — live in **AMD GPU memory**.

`vainfo` confirms the AMD iGPU has hardware **H.264/HEVC encode** entrypoints
(`VAEntrypointEncSlice`). So we encode *where the frames already are*. Choosing
NVENC would force a cross-GPU copy of every single frame; choosing VA-API on the
AMD node keeps the whole capture→encode path zero-copy via **DMA-BUF** (a Linux
mechanism for sharing GPU buffers between drivers without copying through RAM).

This is why the daemon defaults to `--device /dev/dri/renderD128`.

---

## 4. The pipeline, stage by stage

### Stage 0 — The headless output (`EXT-1`)

```bash
hyprctl output create headless EXT-1
hyprctl keyword monitor EXT-1,1920x1080@60,<pos>,<scale>
```

`hyprctl output create headless` asks Hyprland (a wlroots compositor) to create
a **virtual wlroots output**. To every application, window manager rule, and the
compositor's own rendering, `EXT-1` is indistinguishable from a plugged-in
monitor. Windows can be moved onto it, workspaces can live on it, the cursor can
travel into it. It just has no cable.

The daemon's `ensure_output()` creates it on start if missing and applies the
requested mode, and `remove_output()` deletes it on exit so you don't
accumulate phantom monitors.

> Why not VKMS? VKMS is a *kernel* virtual display rendered by the CPU. It's
> slower and, in the earlier experiments, crashed the session. A wlroots
> headless output is rendered by the same GPU path as real outputs.

### Stage 1 — Capture + encode (`wf-recorder` + VA-API)

```bash
wf-recorder -o EXT-1 -c h264_vaapi -d /dev/dri/renderD128 \
    -p bf=0 -p g=30 -p rc_mode=CBR -p b=12M -p async_depth=1 \
    -r 60 --muxer=h264 -f /dev/stdout
```

`wf-recorder` uses the **`wlr-screencopy`** Wayland protocol to grab frames from
a *named* output. (We tried `gpu-screen-recorder` first — it's excellent, but on
this setup it does not enumerate headless outputs, while `wf-recorder` does.)

The captured DMA-BUF is handed to FFmpeg's **`h264_vaapi`** encoder running on
the AMD iGPU. The flags are all about latency and clean joins:

| Flag                | Meaning                          | Why it matters for latency                       |
| ------------------- | -------------------------------- | ------------------------------------------------ |
| `bf=0`              | **no B-frames**                  | B-frames need future frames to decode → reorder delay. Zero B-frames = decode-as-you-receive. |
| `g=30`              | keyframe (IDR) every 30 frames   | At 60fps that's every 0.5 s — a fresh client can start quickly and packet loss self-heals fast. |
| `rc_mode=CBR`, `b=12M` | constant 12 Mbit bitrate      | Predictable bandwidth; no bitrate spikes to stall the link. |
| `async_depth=1`     | minimal encoder pipelining       | The encoder doesn't hold several frames in flight. |
| `--muxer=h264`      | **raw Annex-B**, no container    | Output is a bare H.264 elementary stream the decoder can read directly. |
| `-f /dev/stdout`    | write to stdout                  | The daemon reads the stream straight off the pipe. |

The output on stdout is a continuous **Annex-B byte stream**: a sequence of NAL
units, each prefixed by a start code `00 00 00 01`.

### Stage 2 — NAL parsing + broadcast (`extenderd.py`)

The daemon reads `wf-recorder`'s stdout and turns the byte stream into discrete
**NAL units** (Network Abstraction Layer units — the atomic pieces of H.264).

```
…00 00 00 01 [67 …SPS…] 00 00 00 01 [68 …PPS…] 00 00 00 01 [65 …IDR…] 00 00 00 01 [41 …P…]…
              \_______/                \_____/                \_____/                \____/
               type 7                   type 8                 type 5                 type 1
```

The NAL type is `header_byte & 0x1F`. The ones we care about:

| Type | Name     | Role                                                            |
| ---- | -------- | -------------------------------------------------------------- |
| 7    | **SPS**  | Sequence Parameter Set — resolution, profile, frame structure. |
| 8    | **PPS**  | Picture Parameter Set — slice decoding parameters.             |
| 5    | **IDR**  | A **keyframe** — a complete picture, decodable on its own.      |
| 1    | non-IDR  | A **P-frame** — only the *difference* from previous frames.     |

A decoder is useless without **SPS + PPS** (it can't know the format), and it
can't start mid-stream on a P-frame (it has no previous frame to diff against).
So the daemon implements two rules that make joining instant and correct:

1. **Cache the latest SPS and PPS** as they go by (`Broadcaster.sps/pps`).
2. **Start every new client on a keyframe.** When a phone connects, the daemon
   waits for the next IDR, then sends `SPS, PPS, IDR` back-to-back, and only then
   begins forwarding the live stream. The client's decoder is valid from its very
   first frame. (At `g=30`/60fps the wait is ≤0.5 s.)

This logic lives in `Broadcaster.feed()`:

```
for each NAL:
    if SPS → cache;  if PPS → cache
    for each connected client:
        if client not started:
            if this NAL is an IDR and we have SPS+PPS:
                send SPS, send PPS, send IDR  → client started
            else:
                skip (wait for a keyframe)
        else:
            send NAL
```

**No buffering, no latency creep.** NALs are forwarded the instant they're
parsed. Sends use `TCP_NODELAY` (Nagle's algorithm disabled) so small writes
aren't held back. If a client's socket errors (it fell behind / disconnected),
it's dropped — the daemon never grows an unbounded queue for it.

The accept loop runs on its own thread and supports **multiple clients at once**
(you could mirror to two phones).

### Stage 3 — Transport (TCP over USB or WiFi)

The wire format is just the NAL bytes over a **plain TCP** socket. TCP is used
because it works *identically* over both links the user wanted:

- **USB:** `adb reverse tcp:5000 tcp:5000` makes the phone's own `127.0.0.1:5000`
  transparently tunnel to the PC's port 5000 over the USB cable. The phone app
  connects to `127.0.0.1` and never knows the bytes crossed USB. This is the most
  deterministic, lowest-jitter path (~ single-digit ms).
- **WiFi:** the phone connects to the PC's LAN IP on port 5000 directly.

`start-extender.sh` sets up whichever mode you ask for (`usb` runs the
`adb reverse`; `wifi` prints the LAN IP to type into the app).

> TCP has one known weakness here: on lossy WiFi, a lost packet stalls
> everything behind it (head-of-line blocking), which can cause a brief freeze.
> That's why the roadmap moves WiFi to **RTP/UDP + FEC** later, while keeping TCP
> for USB where loss is essentially zero.

### Stage 4 — Decode + display (Android)

The phone reads the Annex-B stream, splits it back into NAL units, feeds them
into the hardware **`MediaCodec`** H.264 decoder, and renders each decoded frame
straight onto a `Surface` with **no buffering**. The details are in
[`ANDROID.md`](./ANDROID.md).

---

## 5. End-to-end latency budget

Approximate glass-to-glass timing for the base over USB:

| Stage                              | Typical    |
| ---------------------------------- | ---------- |
| Capture (DMA-BUF, zero-copy)       | 1–2 ms     |
| VA-API encode (no B-frames)        | 3–8 ms     |
| NAL parse + TCP send (no buffer)   | < 1 ms     |
| USB transport (adb reverse)        | 2–5 ms     |
| MediaCodec decode (low-latency)    | 5–10 ms    |
| Surface render (waits for vsync)   | 8–16 ms    |
| **Total**                          | **~25–45 ms** |

The single biggest source of *remaining* latency is display vsync on the phone,
which is fundamental. The thing that used to dominate — a player's jitter buffer
— is gone entirely.

---

## 6. Process & lifecycle summary

```
start-extender.sh ──spawns──▶ extenderd.py ──spawns──▶ wf-recorder
       │                          │  (reads wf stdout)      │ (captures EXT-1,
   (adb reverse                   │                          │  VA-API encode)
    or print IP)                  │
                                  ├─ accept thread: TCP :5000, multi-client
                                  └─ on exit: kill wf-recorder, remove EXT-1
```

- One `extenderd.py` ⟶ one `wf-recorder` child. Killing the daemon (`SIGINT`/
  `SIGTERM`) tears down capture and removes the headless output.
- If you ever see `Address already in use`, a previous daemon is still holding
  port 5000 — `pkill -f extenderd.py` and restart.

---

## 7. Where to go next

- **RTP/UDP + FEC** for lossy WiFi (keep TCP for USB).
- **HEVC** (`vah265enc`) for lower bitrate at equal quality.
- **Resolution auto-match** to the phone's panel + DPI.
- **Touch / pen input** back to the PC (uinput / virtual-pointer protocol).
- **Hotplug UX** so plugging the phone in spins everything up automatically.
- **Latency HUD** overlay for tuning.
