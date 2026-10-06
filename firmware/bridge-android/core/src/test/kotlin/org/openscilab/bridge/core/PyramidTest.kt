package org.openscilab.bridge.core

import java.io.ByteArrayOutputStream
import java.nio.file.Files
import kotlin.random.Random
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class PyramidTest {
    @Test
    fun levels() {
        assertEquals(0, Pyramid.bucketCount(0, 3))
        assertEquals(1, Pyramid.bucketCount(1, 3))
        assertEquals(2, Pyramid.bucketCount(9, 3))
        assertEquals(Pyramid.BASE_LEVEL, Pyramid.topLevel(10))
        assertEquals(25, Pyramid.topLevel(31_250_000))
    }

    @Test
    fun everyLevelMatchesTheNaiveOverview() {
        val random = Random(4)
        for (kind in SampleKind.values()) {
            for (points in listOf(0, 1, 63, 64, 65, 1000, 4097, 33_333)) {
                val samples = IntArray(points) {
                    if (kind == SampleKind.ANALOG) random.nextInt(256) else (it / 7 % 5) * 0x0101 or random.nextInt(2)
                }
                val dir = Files.createTempDirectory("pyramid").toFile()
                try {
                    val raw = TestData.writeRaw(dir, "C", kind, samples)
                    Pyramid.build(raw, dir, "C")
                    for (level in 0..Pyramid.topLevel(points.toLong()) + 2) {
                        val out = ByteArrayOutputStream()
                        Pyramid.writeOverview(raw, dir, "C", level, out)
                        assertContentEquals(TestData.overview(kind, samples, level), out.toByteArray(), "$kind $points level $level")
                    }
                } finally {
                    dir.deleteRecursively()
                }
            }
        }
    }

    @Test
    fun storedLevelsAreSmall() {
        val dir = Files.createTempDirectory("pyramid").toFile()
        try {
            val raw = TestData.writeRaw(dir, "D", SampleKind.DIGITAL, IntArray(100_000) { it })
            Pyramid.build(raw, dir, "D")
            val stored = dir.listFiles()!!.filter { it.name.startsWith("D.L") }.sumOf { it.length() }
            assertTrue(stored < raw.points * 2 / 8, "pyramid $stored bytes")
        } finally {
            dir.deleteRecursively()
        }
    }
}
