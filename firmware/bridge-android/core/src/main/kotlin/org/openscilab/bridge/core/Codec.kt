// Copyright (C) 2026 Julian Decker
// Part of openSciLab. SPDX-License-Identifier: GPL-3.0-or-later

package org.openscilab.bridge.core

import java.io.ByteArrayOutputStream
import java.io.OutputStream

/**
 * The compression of the bridge's tiles (docs/protocols.md, "Bridge"); the Python counterpart is
 * openscilab/driver/rigoldho/codec.py, both are checked against tests/fixtures/bridge_codec.json.
 *
 * Digital channel `D` (16 bits per sample): runs of a varint count and the 16 bit level (`<H`).
 * Analog channels (raw 8 bit samples): the first sample, then zigzag varint differences.
 */
object Varint {
    /** LEB128: 7 bits per byte, the lowest first, the top bit set while more follow. */
    fun write(value: Long, out: OutputStream) {
        require(value >= 0) { "a varint is not negative" }
        var rest = value
        while (true) {
            val byte = (rest and 0x7F).toInt()
            rest = rest ushr 7
            if (rest != 0L) {
                out.write(byte or 0x80)
            } else {
                out.write(byte)
                return
            }
        }
    }

    fun encode(value: Long): ByteArray = ByteArrayOutputStream().also { write(value, it) }.toByteArray()

    /** Reads a varint at [offset]; returns the value and the offset after it. */
    fun read(data: ByteArray, offset: Int): Pair<Long, Int> {
        var value = 0L
        var shift = 0
        var position = offset
        while (true) {
            if (position >= data.size) throw IllegalArgumentException("a varint is cut off")
            if (shift > 63) throw IllegalArgumentException("a varint is too long")
            val byte = data[position++].toInt() and 0xFF
            value = value or ((byte and 0x7F).toLong() shl shift)
            if (byte and 0x80 == 0) return value to position
            shift += 7
        }
    }
}

/** 0, -1, 1, -2 ... -> 0, 1, 2, 3 ... */
fun zigzag(value: Long): Long = (value shl 1) xor (value shr 63)

fun unzigzag(value: Long): Long = (value ushr 1) xor -(value and 1)

/** Run-length encoder for 16 bit levels; feed samples, then [finish]. */
class DigitalEncoder(private val out: OutputStream) {
    private var level = -1
    private var count = 0L

    fun feed(sample: Int) {
        val value = sample and 0xFFFF
        if (value == level) {
            count++
            return
        }
        flush()
        level = value
        count = 1
    }

    fun finish() = flush().also { level = -1; count = 0 }

    private fun flush() {
        if (count == 0L) return
        Varint.write(count, out)
        out.write(level and 0xFF)
        out.write(level ushr 8)
    }
}

/** Delta encoder for raw 8 bit samples; feed samples (0..255). */
class AnalogEncoder(private val out: OutputStream) {
    private var previous = -1

    fun feed(sample: Int) {
        val value = sample and 0xFF
        if (previous < 0) out.write(value) else Varint.write(zigzag((value - previous).toLong()), out)
        previous = value
    }
}

object TileCodec {
    fun encodeDigital(levels: IntArray): ByteArray {
        val out = ByteArrayOutputStream()
        val encoder = DigitalEncoder(out)
        levels.forEach(encoder::feed)
        encoder.finish()
        return out.toByteArray()
    }

    fun decodeDigital(data: ByteArray): IntArray {
        val result = ArrayList<Int>()
        var offset = 0
        while (offset < data.size) {
            val (count, next) = Varint.read(data, offset)
            if (next + 2 > data.size) throw IllegalArgumentException("a run is cut off")
            val level = (data[next].toInt() and 0xFF) or ((data[next + 1].toInt() and 0xFF) shl 8)
            repeat(count.toInt()) { result.add(level) }
            offset = next + 2
        }
        return result.toIntArray()
    }

    fun encodeAnalog(samples: IntArray): ByteArray {
        val out = ByteArrayOutputStream()
        val encoder = AnalogEncoder(out)
        samples.forEach(encoder::feed)
        return out.toByteArray()
    }

    fun decodeAnalog(data: ByteArray): IntArray {
        if (data.isEmpty()) return IntArray(0)
        val result = ArrayList<Int>()
        result.add(data[0].toInt() and 0xFF)
        var offset = 1
        while (offset < data.size) {
            val (encoded, next) = Varint.read(data, offset)
            result.add(((result.last() + unzigzag(encoded)).toInt()) and 0xFF)
            offset = next
        }
        return result.toIntArray()
    }
}
