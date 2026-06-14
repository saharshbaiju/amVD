package com.archextender

import java.io.InputStream

/**
 * Incrementally splits an Annex-B H.264 byte stream into NAL units.
 *
 * Each returned NAL includes its leading start code (00 00 00 01), which is the
 * form MediaCodec expects for csd / Annex-B input buffers. We read greedily and
 * only emit a NAL once we've seen the *start* of the following one, so every NAL
 * handed to the decoder is complete.
 */
class NalReader(input: InputStream, private val chunk: Int = 65536) {

    private val src = input
    private val buf = ByteArrayQueue()
    private val tmp = ByteArray(chunk)
    private var eof = false

    /** NAL type = header byte & 0x1F, located right after the start code. */
    fun nalType(nal: ByteArray): Int {
        // start code is 00 00 01 (3) or 00 00 00 01 (4)
        val hdrIdx = if (nal[2].toInt() == 1) 3 else 4
        return nal[hdrIdx].toInt() and 0x1F
    }

    /** Blocks until a full NAL is available; returns null at end of stream. */
    fun next(): ByteArray? {
        while (true) {
            val nal = buf.extractNal()
            if (nal != null) return nal
            if (eof) {
                // flush any trailing bytes as a final NAL
                return buf.drainRemaining()
            }
            val n = src.read(tmp)
            if (n < 0) {
                eof = true
                continue
            }
            if (n > 0) buf.append(tmp, n)
        }
    }
}

/**
 * Growable byte buffer that knows how to pop a single Annex-B NAL.
 * Keeps a search cursor so we don't rescan the whole buffer each call.
 */
private class ByteArrayQueue {
    private var data = ByteArray(1 shl 16)
    private var size = 0
    private var firstStart = -1   // index of the first start code found

    fun append(src: ByteArray, len: Int) {
        ensure(size + len)
        System.arraycopy(src, 0, data, size, len)
        size += len
    }

    private fun ensure(cap: Int) {
        if (cap <= data.size) return
        var n = data.size
        while (n < cap) n = n shl 1
        data = data.copyOf(n)
    }

    private fun startCodeLen(i: Int): Int {
        if (i + 3 <= size && data[i].toInt() == 0 && data[i + 1].toInt() == 0 &&
            data[i + 2].toInt() == 1
        ) return 3
        if (i + 4 <= size && data[i].toInt() == 0 && data[i + 1].toInt() == 0 &&
            data[i + 2].toInt() == 0 && data[i + 3].toInt() == 1
        ) return 4
        return 0
    }

    /** Returns a complete NAL (with start code) or null if not yet available. */
    fun extractNal(): ByteArray? {
        if (firstStart < 0) {
            firstStart = findStart(0)
            if (firstStart < 0) {
                // no start code at all yet; keep the tail (could be a partial code)
                compactKeepingTail(3)
                return null
            }
        }
        // find the next start code after the first one
        val next = findStart(firstStart + 3)
        if (next < 0) return null // current NAL still growing

        val nal = data.copyOfRange(firstStart, next)
        // shift everything from `next` to the front
        System.arraycopy(data, next, data, 0, size - next)
        size -= next
        firstStart = 0
        return nal
    }

    fun drainRemaining(): ByteArray? {
        if (firstStart < 0 || firstStart >= size) return null
        val nal = data.copyOfRange(firstStart, size)
        size = 0
        firstStart = -1
        return nal.takeIf { it.size > 3 }
    }

    private fun findStart(from: Int): Int {
        var i = if (from < 0) 0 else from
        while (i + 3 <= size) {
            if (startCodeLen(i) > 0) return i
            i++
        }
        return -1
    }

    private fun compactKeepingTail(keep: Int) {
        if (size > keep) {
            System.arraycopy(data, size - keep, data, 0, keep)
            size = keep
        }
    }
}
