// Copyright (C) 2026 Julian Decker
// Part of openSciLab. SPDX-License-Identifier: GPL-3.0-or-later

package org.openscilab.bridge.core

import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream

/** IEEE 488.2 definite-length blocks: `#<n><length><data>`. */
object ScpiBlock {
    fun header(length: Long): ByteArray {
        require(length >= 0)
        val digits = length.toString()
        require(digits.length <= 9) { "a block is limited to 9 length digits" }
        return "#${digits.length}$digits".toByteArray(Charsets.US_ASCII)
    }

    /** Reads a block whose `#` was already read; streams the data into [out]; returns its length. */
    fun readAfterHash(input: InputStream, out: OutputStream): Long {
        val digitsChar = input.read()
        if (digitsChar < 0) throw IOException("the block ends early")
        if (digitsChar !in '1'.code..'9'.code) throw IOException("not a definite-length block (#${digitsChar.toChar()})")
        val digits = ByteArray(digitsChar - '0'.code)
        readFully(input, digits, 0, digits.size)
        val text = String(digits, Charsets.US_ASCII)
        val length = text.toLongOrNull() ?: throw IOException("bad block length $text")
        val buffer = ByteArray(1 shl 16)
        var remaining = length
        while (remaining > 0) {
            val read = input.read(buffer, 0, minOf(remaining, buffer.size.toLong()).toInt())
            if (read < 0) throw IOException("the block ends early")
            out.write(buffer, 0, read)
            remaining -= read
        }
        return length
    }

    /** Reads a whole block (leading whitespace allowed) into memory. */
    fun read(input: InputStream): ByteArray {
        var first = input.read()
        while (first == ' '.code || first == '\r'.code || first == '\n'.code) first = input.read()
        if (first != '#'.code) throw IOException("expected a block, got ${if (first < 0) "the end" else first.toChar().toString()}")
        val out = ByteArrayOutputStream()
        readAfterHash(input, out)
        return out.toByteArray()
    }
}

/** Answers of an instrument: a definite-length block or a line ended by `\n`. */
object ScpiResponse {
    /**
     * Copies one answer from [input] to [out] (a block followed by `\n`, or a line with its
     * `\n`). Blank line ends left over from an earlier block are skipped.
     */
    fun copy(input: InputStream, out: OutputStream) {
        var byte = input.read()
        while (byte == '\n'.code || byte == '\r'.code) byte = input.read()
        if (byte < 0) throw IOException("the instrument closed the connection")
        if (byte == '#'.code) {
            val next = input.read()
            if (next in '1'.code..'9'.code) {
                val digits = ByteArray(next - '0'.code)
                readFully(input, digits, 0, digits.size)
                val length = String(digits, Charsets.US_ASCII).toLongOrNull()
                    ?: throw IOException("bad block length")
                out.write('#'.code)
                out.write(next)
                out.write(digits)
                copyExactly(input, out, length)
                out.write('\n'.code)
                return
            }
            out.write(byte)
            byte = next
        }
        while (byte >= 0) {
            out.write(byte)
            if (byte == '\n'.code) return
            byte = input.read()
        }
        throw IOException("the instrument closed the connection")
    }

    /** Reads one answer line (without `\r\n`). */
    fun readLine(input: InputStream): String {
        val out = ByteArrayOutputStream()
        var byte = input.read()
        while (byte == '\n'.code || byte == '\r'.code) byte = input.read()
        while (byte >= 0 && byte != '\n'.code) {
            out.write(byte)
            byte = input.read()
        }
        if (byte < 0) throw IOException("the instrument closed the connection")
        return out.toString("ISO-8859-1").trimEnd('\r')
    }

    private fun copyExactly(input: InputStream, out: OutputStream, length: Long) {
        val buffer = ByteArray(1 shl 16)
        var remaining = length
        while (remaining > 0) {
            val read = input.read(buffer, 0, minOf(remaining, buffer.size.toLong()).toInt())
            if (read < 0) throw IOException("the block ends early")
            out.write(buffer, 0, read)
            remaining -= read
        }
    }
}

/** Counts the bytes written through it. */
class CountingStream(private val target: OutputStream? = null) : OutputStream() {
    var count = 0L
        private set

    override fun write(b: Int) {
        target?.write(b)
        count++
    }

    override fun write(b: ByteArray, off: Int, len: Int) {
        target?.write(b, off, len)
        count += len
    }

    override fun flush() {
        target?.flush()
    }
}

/**
 * Splits what a client sends into program messages ended by `\n`. Quoted strings and
 * definite-length blocks (`#<n><length><data>`, e.g. an arbitrary waveform) are taken whole,
 * so a `\n` inside them does not end the message.
 */
class ProgramMessageReader(private val input: InputStream, private val limit: Int = 64 shl 20) {
    /** The next message without its `\n` (and `\r`), or null at the end of the stream. */
    fun next(): ByteArray? {
        val out = ByteArrayOutputStream()
        var quote = 0
        while (true) {
            val byte = input.read()
            if (byte < 0) return if (out.size() > 0) out.toByteArray() else null
            if (out.size() > limit) throw IOException("a message is longer than $limit bytes")
            if (quote != 0) {
                out.write(byte)
                if (byte == quote) quote = 0
                continue
            }
            when (byte) {
                '\n'.code -> {
                    val bytes = out.toByteArray()
                    val end = if (bytes.isNotEmpty() && bytes.last() == '\r'.code.toByte()) bytes.size - 1 else bytes.size
                    return bytes.copyOf(end)
                }
                '"'.code, '\''.code -> {
                    quote = byte
                    out.write(byte)
                }
                '#'.code -> {
                    out.write(byte)
                    val digits = input.read()
                    if (digits < 0) continue
                    out.write(digits)
                    if (digits in '1'.code..'9'.code) {
                        val lengthDigits = ByteArray(digits - '0'.code)
                        readFully(input, lengthDigits, 0, lengthDigits.size)
                        out.write(lengthDigits)
                        val length = String(lengthDigits, Charsets.US_ASCII).toLongOrNull()
                            ?: throw IOException("bad block length")
                        if (length > limit) throw IOException("a block is longer than $limit bytes")
                        val data = ByteArray(length.toInt())
                        readFully(input, data, 0, data.size)
                        out.write(data)
                    } else if (digits == '\n'.code) {
                        val bytes = out.toByteArray()
                        return bytes.copyOf(bytes.size - 1)
                    }
                }
                else -> out.write(byte)
            }
        }
    }

    companion object {
        /**
         * Whether a message expects an answer: a header of one of its `;`-separated commands
         * ends with `?` (e.g. `*IDN?`, `:WAV:PRE?`).
         */
        fun isQuery(message: ByteArray): Boolean {
            var inHeader = true
            var started = false
            var quote = 0
            var index = 0
            while (index < message.size) {
                val byte = message[index].toInt() and 0xFF
                index++
                if (quote != 0) {
                    if (byte == quote) quote = 0
                    continue
                }
                when {
                    byte == ';'.code -> { inHeader = true; started = false }
                    byte == ' '.code || byte == '\t'.code -> if (started) inHeader = false
                    inHeader && byte == '?'.code -> return true
                    !inHeader && (byte == '"'.code || byte == '\''.code) -> quote = byte
                    !inHeader && byte == '#'.code && index < message.size -> {
                        val digits = message[index].toInt() - '0'.code
                        if (digits in 1..9 && index + digits < message.size) {
                            val length = String(message, index + 1, digits, Charsets.US_ASCII).toLongOrNull() ?: 0
                            index += 1 + digits + length.toInt()
                        }
                    }
                    else -> started = true
                }
            }
            return false
        }
    }
}
