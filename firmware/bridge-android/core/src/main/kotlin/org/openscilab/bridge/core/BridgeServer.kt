// Copyright (C) 2026 Julian Decker
// Part of openSciLab. SPDX-License-Identifier: GPL-3.0-or-later

package org.openscilab.bridge.core

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.Closeable
import java.io.IOException
import java.io.OutputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

/** The protocol version in `:BRIDge:VERSion?` (docs/protocols.md, "Bridge (protocol 1)"). */
const val BRIDGE_PROTOCOL = 1

data class BridgeConfig(
    /** The app's version, in VERSion? and the beacon. */
    val version: String,
    val port: Int = 5560,
    /** 0: no beacon. */
    val beaconPort: Int = 5561,
    val beaconIntervalMs: Long = 2000,
    val instrumentHost: String = "127.0.0.1",
    val instrumentPort: Int = 5555,
    val instrumentTimeoutMs: Int = 20_000,
    /** Points per `:WAVeform:DATA?`; Rigol documents 250000 for BYTE reads (to measure on the DHO). */
    val chunk: Int = 250_000,
    /** Model and serial for the beacon until `*IDN?` answered. */
    val fallbackModel: String = "DHO",
    val fallbackSerial: String = "unknown",
)

/** Answers the `:BRIDge:*` commands. */
class BridgeHandler(
    private val config: BridgeConfig,
    private val instrument: Instrument,
    val cache: SnapshotCache,
) {
    @Volatile
    var lastBench: Long? = null
        private set

    fun handle(command: BridgeCommand, out: OutputStream) {
        try {
            when (command) {
                BridgeCommand.Version -> line(out, "OPENSCILAB_BRIDGE,${config.version},$BRIDGE_PROTOCOL")
                is BridgeCommand.Bench -> line(out, Bench(instrument, config.chunk).run(command.bytes).also { lastBench = it }.toString())
                is BridgeCommand.Snapshot -> line(out, SnapshotWriter(instrument, cache, config.chunk).take(command.channels).toString())
                BridgeCommand.CacheList -> line(out, cache.list().joinToString(";") { "${it.id},${it.time},${it.points}" })
                is BridgeCommand.Meta -> line(out, snapshot(command.id).metaAnswer())
                is BridgeCommand.Overview -> overview(command, out)
                is BridgeCommand.Tile -> tile(command, out)
                is BridgeCommand.CacheDelete -> {
                    if (command.id == null) cache.deleteAll()
                    else if (!cache.delete(command.id)) throw BridgeError("no snapshot ${command.id}")
                    line(out, "OK")
                }
                is BridgeCommand.CacheLimit -> {
                    cache.limit = command.bytes
                    line(out, "OK")
                }
                is BridgeCommand.Invalid -> throw BridgeError(command.message)
            }
        } catch (error: BridgeError) {
            line(out, "ERR ${error.message}")
        } catch (error: IOException) {
            line(out, "ERR ${error.message ?: error.javaClass.simpleName}")
        } catch (error: IllegalArgumentException) {
            line(out, "ERR ${error.message}")
        }
    }

    private fun snapshot(id: Int) = cache.get(id) ?: throw BridgeError("no snapshot $id")

    private fun overview(command: BridgeCommand.Overview, out: OutputStream) {
        val snapshot = snapshot(command.id)
        if (!snapshot.has(command.channel)) throw BridgeError("snapshot ${command.id} has no channel ${command.channel}")
        val raw = snapshot.raw(command.channel)
        val length = Pyramid.overviewLength(raw.kind, raw.points, command.level)
        out.write(ScpiBlock.header(length))
        Pyramid.writeOverview(raw, snapshot.dir, command.channel, command.level, out)
        out.write('\n'.code)
    }

    /** Encodes twice: once to learn the length for the block header, once into the answer. */
    private fun tile(command: BridgeCommand.Tile, out: OutputStream) {
        val snapshot = snapshot(command.id)
        if (!snapshot.has(command.channel)) throw BridgeError("snapshot ${command.id} has no channel ${command.channel}")
        val raw = snapshot.raw(command.channel)
        if (command.start > raw.points) throw BridgeError("start ${command.start} is behind the ${raw.points} points")
        val count = minOf(command.count, raw.points - command.start)
        val counter = CountingStream()
        encodeTile(raw, command.start, count, counter)
        out.write(ScpiBlock.header(counter.count))
        encodeTile(raw, command.start, count, out)
        out.write('\n'.code)
    }

    private fun encodeTile(raw: RawSamples, start: Long, count: Long, out: OutputStream) {
        if (raw.kind == SampleKind.DIGITAL) {
            val encoder = DigitalEncoder(out)
            raw.forEach(start, count) { encoder.feed(it) }
            encoder.finish()
        } else {
            val encoder = AnalogEncoder(out)
            raw.forEach(start, count) { encoder.feed(it) }
        }
    }

    private fun line(out: OutputStream, text: String) {
        out.write(text.toByteArray(Charsets.ISO_8859_1))
        out.write('\n'.code)
    }
}

class BridgeError(message: String) : Exception(message)

/** What the status screen shows. */
data class BridgeStatus(
    val port: Int,
    val clients: Int,
    val instrumentConnected: Boolean,
    val model: String,
    val serial: String,
    val beacons: Long,
    val lastBench: Long?,
    val lastError: String?,
)

/**
 * The TCP server (port 5560): `:BRIDge:*` messages are answered here, everything else goes to
 * the instrument and its answers back. Also sends the UDP beacon.
 */
class BridgeServer(private val config: BridgeConfig, cache: SnapshotCache) : Closeable {
    val instrument = Instrument(config.instrumentHost, config.instrumentPort, config.instrumentTimeoutMs)
    val handler = BridgeHandler(config, instrument, cache)
    private var server: ServerSocket? = null
    private val sessions = HashSet<Socket>()
    private val clients = AtomicInteger()
    private val beacons = AtomicLong()
    private var beaconThread: Thread? = null

    @Volatile
    private var running = false

    @Volatile
    private var lastError: String? = null

    @Volatile
    private var identity: Pair<String, String>? = null

    /** The bound port (useful with port 0 in tests). */
    val port: Int get() = server?.localPort ?: config.port

    fun start() {
        val socket = ServerSocket()
        socket.reuseAddress = true
        socket.bind(InetSocketAddress(config.port))
        server = socket
        running = true
        Thread({ acceptLoop(socket) }, "bridge-accept").apply { isDaemon = true }.start()
        if (config.beaconPort > 0) {
            beaconThread = Thread({ beaconLoop() }, "bridge-beacon").apply { isDaemon = true; start() }
        }
    }

    override fun close() {
        running = false
        try {
            server?.close()
        } catch (_: IOException) {
        }
        synchronized(sessions) { sessions.toList() }.forEach {
            try {
                it.close()
            } catch (_: IOException) {
            }
        }
        beaconThread?.interrupt()
        instrument.close()
    }

    fun status(): BridgeStatus {
        val (model, serial) = identity ?: (config.fallbackModel to config.fallbackSerial)
        return BridgeStatus(port, clients.get(), instrument.connected, model, serial, beacons.get(), handler.lastBench, lastError)
    }

    /** `OPENSCILAB_BRIDGE <version> <tcp port> <model> <serial>` */
    fun beaconMessage(): String {
        val (model, serial) = identity ?: (config.fallbackModel to config.fallbackSerial)
        return "OPENSCILAB_BRIDGE ${config.version} $port $model $serial"
    }

    /** Asks the instrument for model and serial (`*IDN?`) unless known; skipped while busy. */
    fun updateIdentity() {
        if (identity != null) return
        try {
            val answer = instrument.tryExclusive(100) { instrument.query("*IDN?") } ?: return
            parseIdentity(answer)?.let { identity = it }
        } catch (error: IOException) {
            lastError = "instrument: ${error.message}"
        }
    }

    private fun acceptLoop(socket: ServerSocket) {
        while (running) {
            val client = try {
                socket.accept()
            } catch (error: IOException) {
                if (running) lastError = "accept: ${error.message}"
                return
            }
            synchronized(sessions) { sessions.add(client) }
            Thread({ session(client) }, "bridge-client").apply { isDaemon = true }.start()
        }
    }

    private fun session(client: Socket) {
        clients.incrementAndGet()
        try {
            client.tcpNoDelay = true
            val reader = ProgramMessageReader(BufferedInputStream(client.getInputStream(), 1 shl 16))
            val out = BufferedOutputStream(client.getOutputStream(), 1 shl 16)
            while (running) {
                val message = reader.next() ?: break
                val text = String(message, Charsets.ISO_8859_1)
                if (text.isBlank()) continue
                if (CommandParser.isBridge(text)) {
                    handler.handle(CommandParser.parse(text), out)
                } else {
                    val query = ProgramMessageReader.isQuery(message)
                    try {
                        instrument.passThrough(message, if (query) out else null)
                    } catch (error: IOException) {
                        lastError = "instrument: ${error.message}"
                        if (query) out.write("ERR instrument: ${error.message}\n".toByteArray(Charsets.ISO_8859_1))
                    }
                }
                out.flush()
            }
        } catch (error: IOException) {
            if (running) lastError = "client: ${error.message}"
        } finally {
            clients.decrementAndGet()
            synchronized(sessions) { sessions.remove(client) }
            try {
                client.close()
            } catch (_: IOException) {
            }
        }
    }

    private fun beaconLoop() {
        val socket = try {
            DatagramSocket().apply { broadcast = true }
        } catch (error: IOException) {
            lastError = "beacon: ${error.message}"
            return
        }
        socket.use {
            while (running) {
                updateIdentity()
                val data = beaconMessage().toByteArray(Charsets.US_ASCII)
                for (address in broadcastAddresses()) {
                    try {
                        socket.send(DatagramPacket(data, data.size, address, config.beaconPort))
                    } catch (_: IOException) {
                        // An interface without a route: the others still get it.
                    }
                }
                beacons.incrementAndGet()
                try {
                    Thread.sleep(config.beaconIntervalMs)
                } catch (_: InterruptedException) {
                    return
                }
            }
        }
    }

    companion object {
        /** `RIGOL TECHNOLOGIES,DHO924S,DHO9A0000000,00.01.02` -> model, serial (no spaces). */
        fun parseIdentity(answer: String): Pair<String, String>? {
            val fields = answer.split(',').map { it.trim() }
            if (fields.size < 3 || fields[1].isEmpty()) return null
            fun clean(text: String) = text.replace(Regex("\\s+"), "_").ifEmpty { "unknown" }
            return clean(fields[1]) to clean(fields[2])
        }

        /** The broadcast address of every network that is up, and 255.255.255.255. */
        fun broadcastAddresses(): List<InetAddress> {
            val result = LinkedHashSet<InetAddress>()
            try {
                for (network in NetworkInterface.getNetworkInterfaces()?.toList() ?: emptyList()) {
                    if (!network.isUp || network.isLoopback) continue
                    network.interfaceAddresses.mapNotNullTo(result) { it.broadcast }
                }
            } catch (_: IOException) {
            }
            result.add(InetAddress.getByName("255.255.255.255"))
            return result.toList()
        }

        /** The IPv4 addresses of this device, for the status screen. */
        fun localAddresses(): List<String> = try {
            NetworkInterface.getNetworkInterfaces()?.toList().orEmpty()
                .filter { it.isUp && !it.isLoopback }
                .flatMap { network -> network.inetAddresses.toList().filter { it.address.size == 4 }.map { it.hostAddress ?: "" } }
                .filter { it.isNotEmpty() }
        } catch (_: IOException) {
            emptyList()
        }
    }
}
