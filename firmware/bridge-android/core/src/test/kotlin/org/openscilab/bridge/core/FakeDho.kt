package org.openscilab.bridge.core

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.Closeable
import java.io.IOException
import java.net.ServerSocket
import java.util.concurrent.atomic.AtomicInteger

/**
 * A stand-in for the DHO's SCPI server on 5555, as far as the bridge uses it: `*IDN?`,
 * `:WAVeform:SOURce/MODE/FORMat/STARt/STOP/PREamble?/DATA?` (RAW BYTE, as assumed until step
 * 3a measures the real formats), plus two test queries. Rejects STARt behind STOP like Rigol.
 */
class FakeDho(val points: Int) : Closeable {
    private val server = ServerSocket(0)
    val port: Int get() = server.localPort
    val windowErrors = AtomicInteger()
    val dataQueries = AtomicInteger()
    val connections = AtomicInteger()
    val received = java.util.Collections.synchronizedList(ArrayList<String>())

    private var source = "CHANNEL1"
    private var start = 1L
    private var stop = 1000L

    init {
        Thread({ serve() }, "fake-dho").apply { isDaemon = true }.start()
    }

    fun analog(channel: Int, index: Int) = ((index * 7 + channel * 13) xor (index shr 5)) and 0xFF

    fun digitalBit(bit: Int, index: Int) = ((index / 3 + bit * 5) shr (bit % 4)) and 1

    fun digitalWord(index: Int, bits: List<Int>) = bits.fold(0) { word, bit -> word or (digitalBit(bit, index) shl bit) }

    private fun serve() {
        while (!server.isClosed) {
            val socket = try { server.accept() } catch (_: IOException) { return }
            connections.incrementAndGet()
            Thread {
                socket.use {
                    val reader = ProgramMessageReader(BufferedInputStream(it.getInputStream()))
                    val out = BufferedOutputStream(it.getOutputStream())
                    try {
                        while (true) {
                            val line = String(reader.next() ?: break, Charsets.ISO_8859_1).trim()
                            received.add(line)
                            answer(line, out)
                            out.flush()
                        }
                    } catch (_: IOException) {
                    } catch (error: RuntimeException) {
                        error.printStackTrace()
                    }
                }
            }.apply { isDaemon = true }.start()
        }
    }

    @Synchronized
    private fun answer(line: String, out: java.io.OutputStream) {
        val header = line.substringBefore(' ').uppercase()
        val argument = line.substringAfter(' ', "").trim()
        fun text(value: String) = out.write("$value\n".toByteArray(Charsets.ISO_8859_1))
        when (header) {
            "*IDN?" -> text("RIGOL TECHNOLOGIES,DHO924S,DHO9TEST001,00.01.02")
            ":WAVEFORM:SOURCE" -> source = argument.uppercase()
            ":WAVEFORM:MODE", ":WAVEFORM:FORMAT" -> {}
            ":WAVEFORM:STARt".uppercase() -> {
                val value = argument.toLong()
                if (value > stop) windowErrors.incrementAndGet() else start = value
            }
            ":WAVEFORM:STOP" -> {
                val value = argument.toLong()
                if (value < start) windowErrors.incrementAndGet() else stop = value
            }
            ":WAVEFORM:PREAMBLE?" -> text("0,2,$points,1,1.600000E-09,-8.000000E-06,0,4.000000E-02,0,128")
            ":WAVEFORM:DATA?" -> {
                dataQueries.incrementAndGet()
                val first = (start - 1).toInt()
                val last = minOf(stop.toInt(), points)
                val data = ByteArray(maxOf(0, last - first)) { i ->
                    val index = first + i
                    if (source.startsWith("CHANNEL")) analog(source.removePrefix("CHANNEL").toInt(), index).toByte()
                    else digitalBit(source.removePrefix("D").toInt(), index).toByte()
                }
                out.write(ScpiBlock.header(data.size.toLong()))
                out.write(data)
                out.write('\n'.code)
            }
            ":TEST:ECHO?" -> text(argument)
            ":TEST:BLOCK?" -> {
                out.write(ScpiBlock.header(3)); out.write(byteArrayOf(10, 0, 35)); out.write('\n'.code)
            }
            else -> if (header.endsWith("?")) text("0")
        }
    }

    override fun close() = server.close()
}
