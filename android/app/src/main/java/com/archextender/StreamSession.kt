package com.archextender

import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Build
import android.util.Log
import android.view.Surface
import java.net.InetSocketAddress
import java.net.Socket
import java.nio.ByteBuffer
import org.json.JSONObject

/** Linux-side settings advertised by the daemon's banner (see extenderd.py). */
data class ServerConfig(
    val side: String,
    val width: Int,
    val height: Int,
    val fps: Int,
    val scale: Double,
    val quality: String,
    val bitrate: String,
    val codec: String,
    val gop: Int,
)

/**
 * One streaming session: connect TCP -> read Annex-B NALs -> feed a hardware
 * H.264 decoder that renders straight onto [surface].
 *
 * Latency principles:
 *   * No app-side buffering: every NAL is queued to the decoder immediately and
 *     every decoded frame is released to the Surface as soon as it's ready.
 *   * KEY_LOW_LATENCY on Android 11+ tells the decoder to skip its own reorder
 *     buffer (safe because the encoder emits no B-frames).
 *   * TCP_NODELAY so small NAL writes aren't held by Nagle.
 *
 * Runs entirely on its own thread; call [stop] to tear down.
 */
class StreamSession(
    private val host: String,
    private val port: Int,
    private val surface: Surface,
    private val listener: Listener,
) : Runnable {

    interface Listener {
        fun onConnected()
        fun onStopped(reason: String?)
        fun onStreamInfo(width: Int, height: Int)
        fun onServerConfig(config: ServerConfig)
    }

    @Volatile private var running = true
    private var socket: Socket? = null
    private var codec: MediaCodec? = null
    private var configured = false
    private var sps: ByteArray? = null
    private var pps: ByteArray? = null
    private var ptsUs = 0L
    private val info = MediaCodec.BufferInfo()
    private val au = java.io.ByteArrayOutputStream(1 shl 17)   // current access unit

    private val thread = Thread(this, "stream-session").apply { start() }

    fun stop() {
        running = false
        try { socket?.close() } catch (_: Exception) {}
    }

    fun join() = try { thread.join(1500) } catch (_: InterruptedException) {}

    override fun run() {
        var reason: String? = null
        try {
            val s = Socket()
            s.tcpNoDelay = true
            s.connect(InetSocketAddress(host, port), 5000)
            socket = s
            listener.onConnected()

            // Framed protocol: NALU = video, ASX1 = Linux settings (which can
            // arrive at any time, including live config changes mid-stream).
            val reader = FrameReader(s.getInputStream().buffered(1 shl 16))
            while (running) {
                val f = reader.next() ?: break
                when (f.type) {
                    FrameReader.TYPE_NAL -> handleNal(nalType(f.body), f.body)
                    FrameReader.TYPE_CONFIG ->
                        parseConfig(f.body)?.let { listener.onServerConfig(it) }
                    // unknown frame types are ignored for forward compatibility
                }
            }
        } catch (e: Exception) {
            reason = e.message ?: e.javaClass.simpleName
            Log.e(TAG, "session error", e)
        } finally {
            release()
            listener.onStopped(reason)
        }
    }

    /** Parse an ASX1 config frame body (JSON) into [ServerConfig]. */
    private fun parseConfig(body: ByteArray): ServerConfig? = try {
        val o = JSONObject(String(body, Charsets.UTF_8))
        ServerConfig(
            side = o.optString("side", ""),
            width = o.optInt("width", 0),
            height = o.optInt("height", 0),
            fps = o.optInt("fps", 0),
            scale = o.optDouble("scale", 0.0),
            quality = o.optString("quality", ""),
            bitrate = o.optString("bitrate", ""),
            codec = o.optString("codec", ""),
            gop = o.optInt("gop", 0),
        )
    } catch (e: Exception) {
        Log.w(TAG, "bad config frame", e)
        null
    }

    /** NAL type = header byte & 0x1F, located right after the start code. */
    private fun nalType(nal: ByteArray): Int {
        if (nal.size < 5) return -1
        val hdrIdx = if (nal[2].toInt() == 1) 3 else 4   // 00 00 01 vs 00 00 00 01
        return nal[hdrIdx].toInt() and 0x1F
    }

    private fun handleNal(type: Int, nal: ByteArray) {
        when (type) {
            NAL_SPS -> sps = nal
            NAL_PPS -> pps = nal
        }
        if (!configured) {
            val sp = sps; val pp = pps
            if (sp != null && pp != null) {
                configure(sp, pp)
                // seed the first access unit with the parameter sets so the very
                // first frame we queue is a complete SPS+PPS+IDR unit
                au.reset(); au.write(sp); au.write(pp)
            }
            return  // keep gathering until the decoder exists
        }

        // Hardware decoders (e.g. Qualcomm C2) reject partial input: they need a
        // whole *access unit* (one frame) per buffer, not one NAL at a time.
        // Accumulate NALs and flush when a VCL slice (type 1..5) ends the frame.
        au.write(nal)
        if (type in 1..5) {
            feedAndDrain(au.toByteArray())
            au.reset()
        }
    }

    private fun configure(sp: ByteArray, pp: ByteArray) {
        // Width/height are hints; the decoder derives the true size from the SPS
        // and emits INFO_OUTPUT_FORMAT_CHANGED. The Surface scales to fit.
        val fmt = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, 1920, 1080)
        fmt.setByteBuffer("csd-0", ByteBuffer.wrap(sp))
        fmt.setByteBuffer("csd-1", ByteBuffer.wrap(pp))
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            fmt.setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
        }
        val c = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
        c.configure(fmt, surface, null, 0)
        c.start()
        codec = c
        configured = true
        Log.i(TAG, "decoder configured")
    }

    private fun feedAndDrain(frame: ByteArray) {
        val c = codec ?: return
        // Get an input buffer, draining outputs while we wait so we never stall.
        var inIdx = c.dequeueInputBuffer(0)
        while (inIdx < 0 && running) {
            drain(c)
            inIdx = c.dequeueInputBuffer(2000)
        }
        if (inIdx < 0) return
        val buf: ByteBuffer = c.getInputBuffer(inIdx) ?: return
        if (frame.size > buf.capacity()) return   // guard: skip an oversized unit
        buf.clear()
        buf.put(frame)
        c.queueInputBuffer(inIdx, 0, frame.size, ptsUs, 0)
        ptsUs += 33_333   // ~30fps spacing; ordering only, no B-frames
        drain(c)
    }

    private fun drain(c: MediaCodec) {
        while (true) {
            val outIdx = c.dequeueOutputBuffer(info, 0)
            when {
                outIdx >= 0 ->
                    // render = true -> hand the frame to the Surface immediately
                    c.releaseOutputBuffer(outIdx, true)
                outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    // The decoder now knows the true resolution (from the SPS).
                    val f = c.outputFormat
                    try {
                        listener.onStreamInfo(
                            f.getInteger(MediaFormat.KEY_WIDTH),
                            f.getInteger(MediaFormat.KEY_HEIGHT),
                        )
                    } catch (_: Exception) {
                    }
                }
                else -> break // TRY_AGAIN_LATER / buffers changed
            }
        }
    }

    private fun release() {
        try { codec?.stop() } catch (_: Exception) {}
        try { codec?.release() } catch (_: Exception) {}
        codec = null
        try { socket?.close() } catch (_: Exception) {}
    }

    companion object {
        private const val TAG = "StreamSession"
        const val NAL_SPS = 7
        const val NAL_PPS = 8
    }
}
