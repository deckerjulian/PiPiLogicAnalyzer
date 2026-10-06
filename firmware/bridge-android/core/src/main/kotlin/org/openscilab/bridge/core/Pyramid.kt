// Copyright (C) 2026 Julian Decker
// Part of openSciLab. SPDX-License-Identifier: GPL-3.0-or-later

package org.openscilab.bridge.core

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.io.RandomAccessFile

/** How the samples of a channel are stored: analog raw bytes or the 16 bit word of D0-D15. */
enum class SampleKind(val width: Int) {
    ANALOG(1),
    DIGITAL(2);

    /** Bytes of one overview entry: `<BB` (min, max) or `<HH` (AND, OR). */
    val entryBytes: Int get() = 2 * width
}

/**
 * Reduces samples to one entry per bucket of 2^level samples and writes the entries:
 * analog (min, max) as two bytes, digital (AND, OR) as two little-endian 16 bit words.
 */
class BucketReducer(private val kind: SampleKind, level: Int, private val out: OutputStream) {
    private val size = 1L shl level
    private var filled = 0L
    private var low = 0
    private var high = 0

    init {
        reset()
    }

    fun feed(sample: Int) = feedEntry(sample, sample)

    /**
     * Adds one item to the bucket: a sample (low = high = sample) or, when building the pyramid
     * level by level with a reducer of level 1, a whole entry of the level below.
     */
    fun feedEntry(entryLow: Int, entryHigh: Int) {
        add(entryLow, entryHigh)
        if (++filled == size) flush()
    }

    private fun add(entryLow: Int, entryHigh: Int) {
        if (kind == SampleKind.ANALOG) {
            if (entryLow < low) low = entryLow
            if (entryHigh > high) high = entryHigh
        } else {
            low = low and entryLow
            high = high or entryHigh
        }
    }

    /** Writes the partly filled last bucket, if any. */
    fun finish() {
        if (filled > 0) flush()
    }

    /** Writes the current bucket and starts a new one. */
    fun flush() {
        writeEntry(out, kind, low, high)
        filled = 0
        reset()
    }

    private fun reset() {
        if (kind == SampleKind.ANALOG) {
            low = 0xFF; high = 0
        } else {
            low = 0xFFFF; high = 0
        }
    }

    companion object {
        fun writeEntry(out: OutputStream, kind: SampleKind, low: Int, high: Int) {
            if (kind == SampleKind.ANALOG) {
                out.write(low); out.write(high)
            } else {
                out.write(low and 0xFF); out.write(low ushr 8)
                out.write(high and 0xFF); out.write(high ushr 8)
            }
        }
    }
}

/** Reads samples of a raw channel file (analog bytes or little-endian 16 bit words). */
class RawSamples(private val file: File, val kind: SampleKind) {
    val points: Long get() = file.length() / kind.width

    inline fun forEach(start: Long, count: Long, crossinline action: (Int) -> Unit) {
        forEachChunk(start, count) { buffer, length ->
            if (kind == SampleKind.ANALOG) {
                for (i in 0 until length) action(buffer[i].toInt() and 0xFF)
            } else {
                var i = 0
                while (i < length) {
                    action((buffer[i].toInt() and 0xFF) or ((buffer[i + 1].toInt() and 0xFF) shl 8))
                    i += 2
                }
            }
        }
    }

    /** Calls [action] with chunks of raw bytes covering the samples [start, start + count). */
    fun forEachChunk(start: Long, count: Long, action: (ByteArray, Int) -> Unit) {
        if (count <= 0) return
        RandomAccessFile(file, "r").use { input ->
            input.seek(start * kind.width)
            var remaining = count * kind.width
            val buffer = ByteArray(CHUNK)
            while (remaining > 0) {
                val wanted = minOf(remaining, CHUNK.toLong()).toInt()
                val read = input.read(buffer, 0, wanted)
                if (read <= 0) throw IOException("${file.name} ends early")
                // Keep 16 bit words whole.
                var usable = read
                if (kind == SampleKind.DIGITAL && read % 2 != 0) {
                    input.readFully(buffer, read, 1)
                    usable++
                }
                action(buffer, usable)
                remaining -= usable
            }
        }
    }

    companion object {
        const val CHUNK = 1 shl 20
    }
}

/**
 * The min/max (analog) and AND/OR (digital) pyramid of a channel, stored next to its raw file as
 * `<name>.L<level>` from [BASE_LEVEL] up to the level with a single bucket. Lower levels are
 * computed from the raw samples when asked for.
 */
object Pyramid {
    const val BASE_LEVEL = 6
    const val MAX_LEVEL = 62

    fun bucketCount(points: Long, level: Int): Long =
        if (points <= 0) 0 else ((points - 1) shr level) + 1

    /** The lowest level (at least [BASE_LEVEL]) with no more than one bucket. */
    fun topLevel(points: Long): Int {
        var level = BASE_LEVEL
        while (bucketCount(points, level) > 1) level++
        return level
    }

    fun levelFile(dir: File, name: String, level: Int) = File(dir, "$name.L$level")

    fun overviewLength(kind: SampleKind, points: Long, level: Int): Long =
        bucketCount(points, level) * kind.entryBytes

    /** Writes the stored levels of [raw] into [dir]. */
    fun build(raw: RawSamples, dir: File, name: String) {
        val points = raw.points
        if (points == 0L) return
        val kind = raw.kind
        writeFile(levelFile(dir, name, BASE_LEVEL)) { out ->
            val reducer = BucketReducer(kind, BASE_LEVEL, out)
            raw.forEach(0, points) { reducer.feed(it) }
            reducer.finish()
        }
        for (level in BASE_LEVEL + 1..topLevel(points)) {
            writeFile(levelFile(dir, name, level)) { out ->
                val reducer = BucketReducer(kind, 1, out)
                readEntries(levelFile(dir, name, level - 1), kind) { low, high ->
                    reducer.feedEntry(low, high)
                }
                reducer.finish()
            }
        }
    }

    /** Streams the overview of [raw] at [level] (exactly [overviewLength] bytes) into [out]. */
    fun writeOverview(raw: RawSamples, dir: File, name: String, level: Int, out: OutputStream) {
        require(level in 0..MAX_LEVEL) { "level must be 0..$MAX_LEVEL" }
        val points = raw.points
        if (points == 0L) return
        if (level < BASE_LEVEL) {
            val reducer = BucketReducer(raw.kind, level, out)
            raw.forEach(0, points) { reducer.feed(it) }
            reducer.finish()
            return
        }
        val stored = levelFile(dir, name, minOf(level, topLevel(points)))
        FileInputStream(stored).use { copy(it, out) }
    }

    private inline fun readEntries(file: File, kind: SampleKind, action: (Int, Int) -> Unit) {
        BufferedInputStream(FileInputStream(file), 1 shl 16).use { input ->
            val entry = ByteArray(kind.entryBytes)
            while (true) {
                val first = input.read()
                if (first < 0) return
                entry[0] = first.toByte()
                readFully(input, entry, 1, entry.size - 1)
                if (kind == SampleKind.ANALOG) {
                    action(entry[0].toInt() and 0xFF, entry[1].toInt() and 0xFF)
                } else {
                    action(
                        (entry[0].toInt() and 0xFF) or ((entry[1].toInt() and 0xFF) shl 8),
                        (entry[2].toInt() and 0xFF) or ((entry[3].toInt() and 0xFF) shl 8),
                    )
                }
            }
        }
    }

    private inline fun writeFile(file: File, body: (OutputStream) -> Unit) {
        BufferedOutputStream(FileOutputStream(file), 1 shl 16).use(body)
    }
}

fun readFully(input: InputStream, buffer: ByteArray, offset: Int, length: Int) {
    var done = 0
    while (done < length) {
        val read = input.read(buffer, offset + done, length - done)
        if (read < 0) throw IOException("the data ends early")
        done += read
    }
}

/** Copies [input] to [out] (InputStream.transferTo needs API 33). */
fun copy(input: InputStream, out: OutputStream): Long {
    val buffer = ByteArray(1 shl 16)
    var total = 0L
    while (true) {
        val read = input.read(buffer)
        if (read < 0) return total
        out.write(buffer, 0, read)
        total += read
    }
}
