package org.openscilab.bridge.core

import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.net.Socket
import java.nio.file.Files
import kotlin.test.AfterTest
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/** The whole bridge on a plain JVM: client -> BridgeServer -> FakeDho. */
class BridgeServerTest {
    private val points = 10_007
    private val dho = FakeDho(points)
    private val root = Files.createTempDirectory("bridge").toFile()
    private val server = BridgeServer(
        BridgeConfig(
            version = "0.1.0", port = 0, beaconPort = 0, instrumentPort = dho.port, chunk = 1000,
            instrumentTimeoutMs = 5000,
        ),
        SnapshotCache(root),
    ).also { it.start() }
    private val socket = Socket("127.0.0.1", server.port).apply { soTimeout = 20_000 }
    private val input: InputStream = BufferedInputStream(socket.getInputStream())

    @AfterTest
    fun cleanUp() {
        socket.close()
        server.close()
        dho.close()
        root.deleteRecursively()
    }

    private fun send(message: String) {
        socket.getOutputStream().write("$message\n".toByteArray(Charsets.ISO_8859_1))
        socket.getOutputStream().flush()
    }

    /** One answer line; unlike ScpiResponse.readLine, an empty line is an answer here. */
    private fun query(message: String): String {
        send(message)
        val out = ByteArrayOutputStream()
        while (true) {
            val byte = input.read()
            if (byte < 0 || byte == '\n'.code) return out.toString("ISO-8859-1")
            out.write(byte)
        }
    }

    /** A block and the `\n` after it. */
    private fun block(message: String): ByteArray {
        send(message)
        return ScpiBlock.read(input).also { assertEquals('\n'.code, input.read()) }
    }

    @Test
    fun versionAndPassThrough() {
        assertEquals("OPENSCILAB_BRIDGE,0.1.0,1", query(":BRIDge:VERSion?"))
        assertEquals("RIGOL TECHNOLOGIES,DHO924S,DHO9TEST001,00.01.02", query("*IDN?"))
        send(":TIMebase:MAIN:SCALe 1e-3")
        assertEquals("hello", query(":TEST:ECHO? hello"))
        assertContentEquals(byteArrayOf(10, 0, 35), block(":TEST:BLOCK?"))
        assertEquals("next", query(":TEST:ECHO? next"))
        assertTrue(":TIMebase:MAIN:SCALe 1e-3" in dho.received)
        assertTrue(query(":BRIDge:NOPE?").startsWith("ERR "))
        server.updateIdentity()
        assertEquals("OPENSCILAB_BRIDGE 0.1.0 ${server.port} DHO924S DHO9TEST001", server.beaconMessage())
    }

    @Test
    fun snapshotOverviewsAndTiles() {
        val id = query(":BRIDge:SNAPshot D0-D15,CH1,CH3")
        assertEquals("1", id)
        assertEquals(0, dho.windowErrors.get())
        assertEquals(1, dho.connections.get())

        val list = query(":BRIDge:CACHe:LIST?").split(',')
        assertEquals("1", list[0])
        assertEquals("$points", list[2])

        val meta = query(":BRIDge:META? 1").split(';').associate { it.substringBefore('=') to it.substringAfter('=') }
        assertEquals("$points", meta["points"])
        assertEquals("625000000", meta["rate"])
        assertEquals("5000", meta["trigger"])
        assertEquals("D0-D15,CH1,CH3", meta["channels"])
        assertEquals("4.000000E-02", meta["CH1.yinc"])
        assertEquals("0", meta["CH3.yorig"])
        assertEquals("128", meta["CH3.yref"])

        val bits = (0..15).toList()
        val words = IntArray(points) { dho.digitalWord(it, bits) }
        val ch3 = IntArray(points) { dho.analog(3, it) }
        for (level in listOf(0, 3, 6, 9, 13, 14, 30)) {
            assertContentEquals(TestData.overview(SampleKind.DIGITAL, words, level), block(":BRIDge:OVERview? 1,D,$level"), "D $level")
            assertContentEquals(TestData.overview(SampleKind.ANALOG, ch3, level), block(":BRIDge:OVERview? 1,CH3,$level"), "CH3 $level")
        }

        assertContentEquals(words, TileCodec.decodeDigital(block(":BRIDge:TILE? 1,D,0,$points")))
        assertContentEquals(words.copyOfRange(2500, 4100), TileCodec.decodeDigital(block(":BRIDge:TILE? 1,D,2500,1600")))
        assertContentEquals(ch3.copyOfRange(9000, points), TileCodec.decodeAnalog(block(":BRIDge:TILE? 1,CH3,9000,5000")))
        assertEquals(0, block(":BRIDge:TILE? 1,CH1,$points,10").size)
        assertTrue(query(":BRIDge:TILE? 1,CH2,0,10").startsWith("ERR "))
        assertTrue(query(":BRIDge:TILE? 9,CH1,0,10").startsWith("ERR "))

        // The connection stays in sync after the blocks.
        assertEquals("hello", query(":TEST:ECHO? hello"))

        assertEquals("2", query(":BRIDge:SNAPshot CH1"))
        assertEquals(listOf("2", "1"), query(":BRIDge:CACHe:LIST?").split(';').map { it.substringBefore(',') })
        assertEquals("OK", query(":BRIDge:CACHe:DELete 1"))
        assertTrue(query(":BRIDge:CACHe:DELete 1").startsWith("ERR "))
        assertEquals("OK", query(":BRIDge:CACHe:LIMit 1000000"))
        assertEquals("OK", query(":BRIDge:CACHe:DELete ALL"))
        assertEquals("", query(":BRIDge:CACHe:LIST?"))
    }

    @Test
    fun bench() {
        val before = dho.dataQueries.get()
        val rate = query(":BRIDge:BENCH? 25000").toLong()
        assertTrue(rate > 0)
        // 10 windows of 1000 points and one of 7, then from the start again
        assertEquals(27, dho.dataQueries.get() - before)
        assertEquals(0, dho.windowErrors.get())
    }

    @Test
    fun instrumentAway() {
        // The bridge first, then the instrument away: Linux hands the port just closed to the next
        // socket bound to port 0, so a bridge started later could listen on the instrument's port
        // and pass the commands to itself.
        val away = BridgeServer(BridgeConfig(version = "0.1.0", port = 0, beaconPort = 0, instrumentPort = dho.port, instrumentTimeoutMs = 500), SnapshotCache(root))
        away.start()
        dho.close()
        Socket("127.0.0.1", away.port).use { client ->
            client.soTimeout = 5000
            client.getOutputStream().write("*IDN?\n:BRIDge:VERSion?\n".toByteArray())
            val reader = BufferedInputStream(client.getInputStream())
            assertTrue(ScpiResponse.readLine(reader).startsWith("ERR instrument"))
            assertEquals("OPENSCILAB_BRIDGE,0.1.0,1", ScpiResponse.readLine(reader))
        }
        away.close()
    }
}
