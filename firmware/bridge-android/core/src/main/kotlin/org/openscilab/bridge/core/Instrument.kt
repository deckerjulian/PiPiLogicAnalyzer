// Copyright (C) 2026 Julian Decker
// Part of openSciLab. SPDX-License-Identifier: GPL-3.0-or-later

package org.openscilab.bridge.core

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.Closeable
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock

/**
 * The instrument's own SCPI server (127.0.0.1:5555 on the DHO). One connection, shared by all
 * clients of the bridge and by the snapshot; [exclusive] keeps a sequence of commands together.
 * After an error the connection is closed and opened again with the next command.
 */
class Instrument(
    private val host: String,
    private val port: Int,
    private val timeoutMs: Int = 20_000,
) : Closeable {
    private val lock = ReentrantLock()
    private var socket: Socket? = null
    private var input: InputStream? = null
    private var output: OutputStream? = null

    @Volatile
    var connected = false
        private set

    fun <T> exclusive(block: () -> T): T {
        lock.lock()
        try {
            return block()
        } finally {
            lock.unlock()
        }
    }

    /** Runs [block] if the instrument is free within [waitMs]; null otherwise. */
    fun <T> tryExclusive(waitMs: Long, block: () -> T): T? {
        if (!lock.tryLock(waitMs, TimeUnit.MILLISECONDS)) return null
        try {
            return block()
        } finally {
            lock.unlock()
        }
    }

    fun write(command: String) = exclusive {
        guarded { send(command.toByteArray(Charsets.ISO_8859_1)) }
    }

    fun query(command: String): String = exclusive {
        guarded {
            send(command.toByteArray(Charsets.ISO_8859_1))
            ScpiResponse.readLine(input!!)
        }
    }

    /** Sends a query answered with a definite-length block; streams its data into [out]. */
    fun queryBlock(command: String, out: OutputStream): Long = exclusive {
        guarded {
            send(command.toByteArray(Charsets.ISO_8859_1))
            val stream = input!!
            skipToBlock(stream)
            ScpiBlock.readAfterHash(stream, out).also { skipNewline(stream) }
        }
    }

    /** Passes a client's message on; copies the instrument's answer to [answer] for queries. */
    fun passThrough(message: ByteArray, answer: OutputStream?) = exclusive {
        guarded {
            send(message)
            if (answer != null) ScpiResponse.copy(input!!, answer)
        }
    }

    override fun close() = exclusive { disconnect() }

    private fun send(message: ByteArray) {
        if (socket == null) connect()
        val out = output!!
        out.write(message)
        out.write('\n'.code)
        out.flush()
    }

    private fun connect() {
        val created = Socket()
        try {
            created.connect(InetSocketAddress(host, port), timeoutMs)
            created.soTimeout = timeoutMs
            created.tcpNoDelay = true
        } catch (error: IOException) {
            created.close()
            throw error
        }
        socket = created
        input = BufferedInputStream(created.getInputStream(), 1 shl 16)
        output = BufferedOutputStream(created.getOutputStream(), 1 shl 16)
        connected = true
    }

    private fun disconnect() {
        try {
            socket?.close()
        } catch (_: IOException) {
        }
        socket = null
        input = null
        output = null
        connected = false
    }

    private inline fun <T> guarded(block: () -> T): T {
        try {
            return block()
        } catch (error: IOException) {
            disconnect()
            throw error
        }
    }

    private fun skipToBlock(input: InputStream) {
        while (true) {
            val byte = input.read()
            if (byte < 0) throw IOException("the instrument closed the connection")
            if (byte == '#'.code) return
            if (byte != '\n'.code && byte != '\r'.code && byte != ' '.code) {
                // An error text instead of a block: read the rest of the line for the message.
                val rest = ScpiResponse.readLine(input)
                throw IOException("expected a block, got ${byte.toChar()}$rest")
            }
        }
    }

    /** Consumes the `\n` after a block, if it is already there. */
    private fun skipNewline(input: InputStream) {
        input.mark(1)
        val available = try { input.available() } catch (_: IOException) { 0 }
        if (available > 0) {
            val byte = input.read()
            if (byte != '\n'.code) input.reset()
        }
    }
}
