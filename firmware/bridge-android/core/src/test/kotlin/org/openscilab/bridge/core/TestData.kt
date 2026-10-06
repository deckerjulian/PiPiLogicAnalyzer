package org.openscilab.bridge.core

import java.io.ByteArrayOutputStream
import java.io.File

object TestData {
    fun writeRaw(dir: File, name: String, kind: SampleKind, samples: IntArray): RawSamples {
        val out = ByteArrayOutputStream()
        for (sample in samples) {
            out.write(sample and 0xFF)
            if (kind == SampleKind.DIGITAL) out.write((sample ushr 8) and 0xFF)
        }
        val file = File(dir, "$name.raw")
        file.writeBytes(out.toByteArray())
        return RawSamples(file, kind)
    }

    /** min/max or AND/OR per bucket, written the naive way. */
    fun overview(kind: SampleKind, samples: IntArray, level: Int): ByteArray {
        val out = ByteArrayOutputStream()
        val size = 1 shl level
        var start = 0
        while (start < samples.size) {
            val block = samples.copyOfRange(start, minOf(samples.size, start + size))
            if (kind == SampleKind.ANALOG) {
                out.write(block.min()); out.write(block.max())
            } else {
                val and = block.reduce { a, b -> a and b }
                val or = block.reduce { a, b -> a or b }
                out.write(and and 0xFF); out.write(and ushr 8); out.write(or and 0xFF); out.write(or ushr 8)
            }
            start += size
        }
        return out.toByteArray()
    }
}
