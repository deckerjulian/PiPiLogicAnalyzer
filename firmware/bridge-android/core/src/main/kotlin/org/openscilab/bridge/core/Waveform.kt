// Copyright (C) 2026 Julian Decker
// Part of openSciLab. SPDX-License-Identifier: GPL-3.0-or-later

package org.openscilab.bridge.core

import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.FileOutputStream
import java.io.IOException
import java.io.OutputStream
import java.math.BigDecimal
import java.math.MathContext
import kotlin.math.roundToLong

/**
 * `:WAVeform:PREamble?`: format, type, points, count, xincrement, xorigin, xreference,
 * yincrement, yorigin, yreference. The y values are kept as the instrument wrote them.
 */
data class Preamble(
    val points: Long,
    val xIncrement: Double,
    val xOrigin: Double,
    val xReference: Double,
    val yIncrement: String,
    val yOrigin: String,
    val yReference: String,
) {
    /** Samples per second. */
    val rate: Double get() = if (xIncrement > 0) 1.0 / xIncrement else 0.0

    /** The sample at time 0 (the trigger): time = (i - xreference) * xincrement + xorigin. */
    val trigger: Long get() = if (xIncrement > 0) (xReference - xOrigin / xIncrement).roundToLong() else 0

    companion object {
        fun parse(text: String): Preamble {
            val fields = text.trim().split(',').map { it.trim() }
            if (fields.size < 10) throw IOException("unexpected preamble: $text")
            fun number(index: Int) = fields[index].toDoubleOrNull() ?: throw IOException("unexpected preamble: $text")
            return Preamble(
                points = number(2).toLong(),
                xIncrement = number(4),
                xOrigin = number(5),
                xReference = number(6),
                yIncrement = fields[7],
                yOrigin = fields[8],
                yReference = fields[9],
            )
        }
    }
}

/**
 * Reads waveform memory from the instrument block by block (`:WAVeform:STARt/STOP/DATA?`,
 * RAW mode, BYTE format). [chunk] is the most points one `:WAVeform:DATA?` returns.
 */
class WaveformReader(private val instrument: Instrument, private val chunk: Int) {
    private var start: Long? = null
    private var stop: Long? = null

    /** Selects a source (`CHANnel1`, `D0` …) for RAW BYTE reads and returns its preamble. */
    fun select(source: String): Preamble {
        prepare()
        instrument.write(":WAVeform:SOURce $source")
        return Preamble.parse(instrument.query(":WAVeform:PREamble?"))
    }

    /** RAW mode, BYTE format, for the current source. */
    fun prepare() {
        instrument.write(":WAVeform:MODE RAW")
        instrument.write(":WAVeform:FORMat BYTE")
    }

    fun switchSource(source: String) = instrument.write(":WAVeform:SOURce $source")

    fun preamble(): Preamble = Preamble.parse(instrument.query(":WAVeform:PREamble?"))

    /** Streams the samples [first, first + count) (0-based) of the current source into [out]. */
    fun read(first: Long, count: Long, out: OutputStream) {
        var position = first
        val end = first + count
        while (position < end) {
            val length = minOf(chunk.toLong(), end - position)
            readWindow(position, length, out)
            position += length
        }
    }

    /** One `:WAVeform:DATA?` of up to [chunk] points. */
    fun readWindow(first: Long, length: Long, out: OutputStream) {
        setWindow(first + 1, first + length)
        val received = instrument.queryBlock(":WAVeform:DATA?", out)
        if (received != length) throw IOException("asked for $length points from ${first + 1}, got $received")
    }

    /**
     * Sets STARt and STOP (1-based, inclusive) in an order the instrument accepts: STARt may not
     * lie behind STOP.
     */
    private fun setWindow(newStart: Long, newStop: Long) {
        val oldStop = stop
        if (oldStop != null && newStart > oldStop) {
            instrument.write(":WAVeform:STOP $newStop")
            instrument.write(":WAVeform:STARt $newStart")
        } else if (oldStop != null || newStart == 1L) {
            instrument.write(":WAVeform:STARt $newStart")
            instrument.write(":WAVeform:STOP $newStop")
        } else {
            instrument.write(":WAVeform:STARt 1")
            instrument.write(":WAVeform:STOP $newStop")
            instrument.write(":WAVeform:STARt $newStart")
        }
        start = newStart
        stop = newStop
    }
}

/** Reads the stopped acquisition into the cache: `:BRIDge:SNAPshot`. */
class SnapshotWriter(
    private val instrument: Instrument,
    private val cache: SnapshotCache,
    private val chunk: Int,
    private val clock: () -> Long = { System.currentTimeMillis() / 1000 },
) {
    fun take(channels: ChannelSet): Int = instrument.exclusive {
        val (id, dir) = cache.begin()
        try {
            val reader = WaveformReader(instrument, chunk)
            val analog = channels.analog.associateWith { reader.select(analogSource(it)) }
            val digital = channels.digital.firstOrNull()?.let { reader.select(digitalSource(it)) }
            val first = digital ?: analog.values.first()
            val points = (analog.values + listOfNotNull(digital)).minOf { it.points }

            val meta = LinkedHashMap<String, String>()
            meta["points"] = points.toString()
            meta["rate"] = formatNumber(first.rate)
            meta["trigger"] = first.trigger.toString()
            meta["channels"] = channels.toString()
            for ((channel, preamble) in analog) {
                meta["CH$channel.yinc"] = preamble.yIncrement
                meta["CH$channel.yorig"] = preamble.yOrigin
                meta["CH$channel.yref"] = preamble.yReference
            }
            meta[Snapshot.TIME] = clock().toString()

            for (channel in channels.analog) {
                reader.switchSource(analogSource(channel))
                BufferedOutputStream(FileOutputStream(Snapshot.rawFile(dir, "CH$channel")), 1 shl 16).use {
                    reader.read(0, points, it)
                }
            }
            if (channels.digital.isNotEmpty()) {
                BufferedOutputStream(FileOutputStream(Snapshot.rawFile(dir, "D")), 1 shl 16).use {
                    readDigital(reader, channels.digital, points, it)
                }
            }
            for (name in channels.names) {
                Pyramid.build(RawSamples(Snapshot.rawFile(dir, name), Snapshot.kindOf(name)), dir, name)
            }
            Snapshot.writeMeta(dir, meta)
            cache.commit(id, dir)
            id
        } catch (error: Exception) {
            cache.abandon(dir)
            throw error
        }
    }

    /**
     * The instrument gives every digital channel on its own (one byte per sample, not 0 = high);
     * they are combined into one little-endian 16 bit word per sample, chunk by chunk.
     */
    private fun readDigital(reader: WaveformReader, bits: List<Int>, points: Long, out: OutputStream) {
        var position = 0L
        val words = IntArray(chunk)
        val buffer = ByteArrayOutputStream(chunk)
        val bytes = ByteArray(chunk * 2)
        while (position < points) {
            val length = minOf(chunk.toLong(), points - position).toInt()
            words.fill(0, 0, length)
            for (bit in bits) {
                reader.switchSource(digitalSource(bit))
                buffer.reset()
                reader.readWindow(position, length.toLong(), buffer)
                val data = buffer.toByteArray()
                for (i in 0 until length) if (data[i].toInt() != 0) words[i] = words[i] or (1 shl bit)
            }
            for (i in 0 until length) {
                bytes[2 * i] = words[i].toByte()
                bytes[2 * i + 1] = (words[i] ushr 8).toByte()
            }
            out.write(bytes, 0, 2 * length)
            position += length
        }
    }

    companion object {
        fun analogSource(channel: Int) = "CHANnel$channel"
        fun digitalSource(bit: Int) = "D$bit"
    }
}

/** `:BRIDge:BENCH? <bytes>`: reads at least [bytes] of waveform data and times it. */
class Bench(private val instrument: Instrument, private val chunk: Int) {
    fun run(bytes: Long): Long = instrument.exclusive {
        val reader = WaveformReader(instrument, chunk)
        reader.prepare()
        val points = reader.preamble().points
        if (points <= 0) throw IOException("the instrument has no waveform data")
        val counter = CountingStream()
        var position = 0L
        val begin = System.nanoTime()
        while (counter.count < bytes) {
            val length = minOf(chunk.toLong(), points - position)
            reader.readWindow(position, length, counter)
            position = (position + length) % points
        }
        val elapsed = maxOf(System.nanoTime() - begin, 1)
        (counter.count.toDouble() * 1e9 / elapsed).roundToLong()
    }
}

/** A number for META: an integer when it is one, otherwise ten significant digits. */
fun formatNumber(value: Double): String {
    if (value.isNaN() || value.isInfinite()) return "0"
    val rounded = BigDecimal(value).round(MathContext(10))
    return rounded.stripTrailingZeros().let { if (it.scale() < 0) it.setScale(0) else it }.toPlainString()
}
