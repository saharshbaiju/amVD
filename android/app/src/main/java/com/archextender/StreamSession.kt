package com.archextender

import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Build
import android.util.Log
import android.view.Surface
import java.net.InetSocketAddress
import java.net.Socket
import java.nio.ByteBuffer

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
    }

    @Volatile private var running = true
    private var socket: Socket? = null
    private var codec: MediaCodec? = null
    private var configured = false
    private var sps: ByteArray? = null
    private var pps: ByteArray? = null
    private var ptsUs = 0L
    private val info = MediaCodec.BufferInfo()

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

            val reader = NalReader(s.getInputStream().buffered(1 shl 16))
            while (running) {
                val nal = reader.next() ?: break
                handleNal(reader.nalType(nal), nal)
            }
        } catch (e: Exception) {
            reason = e.message ?: e.javaClass.simpleName
            Log.e(TAG, "session error", e)
        } finally {
            release()
            listener.onStopped(reason)
        }
    }

    private fun handleNal(type: Int, nal: ByteArray) {
        when (type) {
            NAL_SPS -> sps = nal
            NAL_PPS -> pps = nal
        }
        if (!configured) {
            val sp = sps; val pp = pps
            if (sp != null && pp != null) configure(sp, pp)
            // Until configured we only need SPS/PPS; the daemon starts us on an
            // IDR, so the IDR NAL itself arrives after both param sets.
        }
        if (configured) feedAndDrain(nal)
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

    private fun feedAndDrain(nal: ByteArray) {
        val c = codec ?: return
        // Get an input buffer, draining outputs while we wait so we never stall.
        var inIdx = c.dequeueInputBuffer(0)
        while (inIdx < 0 && running) {
            drain(c)
            inIdx = c.dequeueInputBuffer(2000)
        }
        if (inIdx < 0) return
        val buf: ByteBuffer = c.getInputBuffer(inIdx) ?: return
        buf.clear()
        buf.put(nal)
        c.queueInputBuffer(inIdx, 0, nal.size, ptsUs, 0)
        ptsUs += 1
        drain(c)
    }

    private fun drain(c: MediaCodec) {
        while (true) {
            val outIdx = c.dequeueOutputBuffer(info, 0)
            if (outIdx >= 0) {
                // render = true -> hand the frame to the Surface immediately
                c.releaseOutputBuffer(outIdx, true)
            } else {
                break // TRY_AGAIN_LATER / format or buffers changed
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
