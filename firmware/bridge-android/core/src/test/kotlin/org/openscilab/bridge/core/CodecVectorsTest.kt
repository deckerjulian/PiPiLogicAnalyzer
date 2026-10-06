package org.openscilab.bridge.core

import java.io.ByteArrayOutputStream
import java.nio.file.Files
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals

/** The Kotlin codec against tests/fixtures/bridge_codec.json, shared with the Python driver. */
class CodecVectorsTest {
    @Suppress("UNCHECKED_CAST")
    private val vectors: Map<String, Any?> = Json.parse(
        javaClass.getResource("/bridge_codec.json")!!.readText()
    ) as Map<String, Any?>

    @Suppress("UNCHECKED_CAST")
    private fun cases(name: String) = vectors[name] as List<Map<String, Any?>>

    @Suppress("UNCHECKED_CAST")
    private fun ints(value: Any?) = (value as List<Long>).map { it.toInt() }.toIntArray()

    @Suppress("UNCHECKED_CAST")
    private fun pairs(value: Any?) = (value as List<List<Long>>).flatMap { it.map(Long::toInt) }.toIntArray()

    private fun hex(text: Any?): ByteArray {
        val s = text as String
        return ByteArray(s.length / 2) { s.substring(2 * it, 2 * it + 2).toInt(16).toByte() }
    }

    @Test
    fun varint() {
        for (case in cases("varint")) {
            val value = case["value"] as Long
            assertContentEquals(hex(case["encoded"]), Varint.encode(value), "varint $value")
            assertEquals(value, Varint.read(hex(case["encoded"]), 0).first)
        }
    }

    @Test
    fun zigzag() {
        for (case in cases("zigzag")) {
            val value = case["value"] as Long
            assertEquals(case["encoded"] as Long, zigzag(value), "zigzag $value")
            assertEquals(value, unzigzag(case["encoded"] as Long))
        }
    }

    @Test
    fun digitalTiles() {
        assertEquals(4, cases("digital").size)
        for (case in cases("digital")) {
            val levels = ints(case["levels"])
            assertContentEquals(hex(case["encoded"]), TileCodec.encodeDigital(levels))
            assertContentEquals(levels, TileCodec.decodeDigital(hex(case["encoded"])))
        }
    }

    @Test
    fun analogTiles() {
        assertEquals(4, cases("analog").size)
        for (case in cases("analog")) {
            val samples = ints(case["samples"])
            assertContentEquals(hex(case["encoded"]), TileCodec.encodeAnalog(samples))
            assertContentEquals(samples, TileCodec.decodeAnalog(hex(case["encoded"])))
        }
    }

    @Test
    fun overviews() {
        for (case in cases("overview_analog")) {
            val samples = ints(case["samples"])
            val level = (case["level"] as Long).toInt()
            val expected = pairs(case["minmax"]).map { it.toByte() }.toByteArray()
            assertContentEquals(expected, reduce(SampleKind.ANALOG, samples, level))
            assertContentEquals(expected, fromFile(SampleKind.ANALOG, samples, level))
        }
        for (case in cases("overview_digital")) {
            val levels = ints(case["levels"])
            val level = (case["level"] as Long).toInt()
            val out = ByteArrayOutputStream()
            pairs(case["and_or"]).forEach { out.write(it and 0xFF); out.write(it ushr 8) }
            assertContentEquals(out.toByteArray(), reduce(SampleKind.DIGITAL, levels, level))
            assertContentEquals(out.toByteArray(), fromFile(SampleKind.DIGITAL, levels, level))
        }
    }

    private fun reduce(kind: SampleKind, samples: IntArray, level: Int): ByteArray {
        val out = ByteArrayOutputStream()
        val reducer = BucketReducer(kind, level, out)
        samples.forEach(reducer::feed)
        reducer.finish()
        return out.toByteArray()
    }

    private fun fromFile(kind: SampleKind, samples: IntArray, level: Int): ByteArray {
        val dir = Files.createTempDirectory("pyramid").toFile()
        try {
            val raw = TestData.writeRaw(dir, "X", kind, samples)
            Pyramid.build(raw, dir, "X")
            val out = ByteArrayOutputStream()
            Pyramid.writeOverview(raw, dir, "X", level, out)
            assertEquals(Pyramid.overviewLength(kind, raw.points, level), out.size().toLong())
            return out.toByteArray()
        } finally {
            dir.deleteRecursively()
        }
    }
}
