package org.openscilab.bridge.core

import java.io.File
import java.nio.file.Files
import kotlin.test.AfterTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class SnapshotCacheTest {
    private val root = Files.createTempDirectory("cache").toFile()

    @AfterTest
    fun cleanUp() {
        root.deleteRecursively()
    }

    private fun add(cache: SnapshotCache, bytes: Int, time: Long): Int {
        val (id, dir) = cache.begin()
        File(dir, "CH1.raw").writeBytes(ByteArray(bytes))
        Snapshot.writeMeta(dir, linkedMapOf("points" to "$bytes", "channels" to "CH1", Snapshot.TIME to "$time"))
        cache.commit(id, dir)
        return id
    }

    @Test
    fun listsNewestFirstAndDeletes() {
        val cache = SnapshotCache(root)
        assertEquals(1, add(cache, 10, 100))
        assertEquals(2, add(cache, 20, 200))
        assertEquals(listOf(2, 1), cache.list().map { it.id })
        assertEquals(20, cache.get(2)!!.points)
        assertEquals(200, cache.get(2)!!.time)
        assertEquals("points=20;channels=CH1", cache.get(2)!!.metaAnswer())
        assertTrue(cache.delete(2))
        assertFalse(cache.delete(2))
        // Ids are not reused.
        assertEquals(3, add(cache, 5, 300))
        cache.deleteAll()
        assertTrue(cache.list().isEmpty())
        assertEquals(4, add(SnapshotCache(root), 5, 400))
    }

    @Test
    fun limitDropsTheOldest() {
        val cache = SnapshotCache(root)
        add(cache, 1000, 1); add(cache, 1000, 2); add(cache, 1000, 3)
        cache.limit = 2500
        assertEquals(listOf(3, 2), cache.list().map { it.id })
        assertEquals(2500, SnapshotCache(root).limit)
        cache.limit = 0
        assertEquals(listOf(3), cache.list().map { it.id })
    }

    @Test
    fun partialSnapshotsAreRemoved() {
        val cache = SnapshotCache(root)
        val (_, dir) = cache.begin()
        assertTrue(dir.exists())
        SnapshotCache(root)
        assertFalse(dir.exists())
        assertNull(cache.get(1))
    }
}
