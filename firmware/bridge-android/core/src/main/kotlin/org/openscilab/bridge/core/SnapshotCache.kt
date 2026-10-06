// Copyright (C) 2026 Julian Decker
// Part of openSciLab. SPDX-License-Identifier: GPL-3.0-or-later

package org.openscilab.bridge.core

import java.io.File
import java.io.IOException

/** A snapshot in the cache: its directory holds `meta.txt`, `<channel>.raw` and the pyramid. */
class Snapshot(val id: Int, val dir: File, val meta: Map<String, String>) {
    val time: Long get() = meta[TIME]?.toLongOrNull() ?: 0
    val points: Long get() = meta["points"]?.toLongOrNull() ?: 0
    val channels: ChannelSet get() = ChannelSet.parse(meta["channels"] ?: "")

    fun has(channel: String) = channel in channels.names

    fun raw(channel: String) = RawSamples(rawFile(dir, channel), kindOf(channel))

    /** The answer of `:BRIDge:META?`: `key=value;…` (without the internal `time`). */
    fun metaAnswer(): String = meta.filterKeys { it != TIME }.entries.joinToString(";") { "${it.key}=${it.value}" }

    fun bytes(): Long = dir.listFiles()?.sumOf { it.length() } ?: 0

    companion object {
        const val META_FILE = "meta.txt"
        const val TIME = "time"

        fun rawFile(dir: File, channel: String) = File(dir, "$channel.raw")

        fun kindOf(channel: String) = if (channel == "D") SampleKind.DIGITAL else SampleKind.ANALOG

        fun writeMeta(dir: File, meta: Map<String, String>) {
            File(dir, META_FILE).writeText(meta.entries.joinToString("") { "${it.key}=${it.value}\n" })
        }

        fun readMeta(dir: File): Map<String, String> {
            val result = LinkedHashMap<String, String>()
            File(dir, META_FILE).readLines().forEach { line ->
                val index = line.indexOf('=')
                if (index > 0) result[line.substring(0, index)] = line.substring(index + 1)
            }
            return result
        }
    }
}

/**
 * The snapshots on the instrument's storage, each in a directory named by its id. Ids grow
 * and are never reused; the oldest snapshots go when the cache grows beyond [limit].
 */
class SnapshotCache(val root: File) {
    private val lock = Any()

    init {
        root.mkdirs()
        // Snapshots that were being written when the app stopped.
        root.listFiles()?.filter { it.name.endsWith(PARTIAL) }?.forEach { it.deleteRecursively() }
    }

    var limit: Long
        get() = synchronized(lock) { File(root, LIMIT_FILE).takeIf { it.exists() }?.readText()?.trim()?.toLongOrNull() ?: DEFAULT_LIMIT }
        set(value) = synchronized(lock) {
            File(root, LIMIT_FILE).writeText("$value\n")
            enforceLimit()
        }

    /** Reserves a new id and an empty directory to write the snapshot into. */
    fun begin(): Pair<Int, File> = synchronized(lock) {
        val counter = File(root, ID_FILE)
        val existing = ids().maxOrNull() ?: 0
        val id = maxOf(counter.takeIf { it.exists() }?.readText()?.trim()?.toIntOrNull() ?: 1, existing + 1)
        counter.writeText("${id + 1}\n")
        val dir = File(root, "$id$PARTIAL")
        dir.deleteRecursively()
        if (!dir.mkdirs()) throw IOException("cannot create $dir")
        id to dir
    }

    /** Makes a written snapshot visible; then applies the limit. */
    fun commit(id: Int, dir: File) = synchronized(lock) {
        if (!dir.renameTo(File(root, id.toString()))) throw IOException("cannot store snapshot $id")
        enforceLimit()
    }

    fun abandon(dir: File) {
        dir.deleteRecursively()
    }

    /** Newest first. */
    fun list(): List<Snapshot> = synchronized(lock) { ids().sortedDescending().mapNotNull { get(it) } }

    fun get(id: Int): Snapshot? = synchronized(lock) {
        val dir = File(root, id.toString())
        if (!File(dir, Snapshot.META_FILE).isFile) return null
        Snapshot(id, dir, Snapshot.readMeta(dir))
    }

    fun delete(id: Int): Boolean = synchronized(lock) {
        val dir = File(root, id.toString())
        dir.isDirectory && dir.deleteRecursively()
    }

    fun deleteAll() = synchronized(lock) { ids().forEach { delete(it) } }

    fun totalBytes(): Long = synchronized(lock) { list().sumOf { it.bytes() } }

    /** Deletes the oldest snapshots while the cache is larger than the limit; keeps the newest. */
    fun enforceLimit() = synchronized(lock) {
        val snapshots = list().toMutableList()
        val limit = limit
        var total = snapshots.sumOf { it.bytes() }
        while (total > limit && snapshots.size > 1) {
            val oldest = snapshots.removeAt(snapshots.size - 1)
            total -= oldest.bytes()
            delete(oldest.id)
        }
    }

    private fun ids(): List<Int> = root.listFiles()?.filter { it.isDirectory }?.mapNotNull { it.name.toIntOrNull() } ?: emptyList()

    companion object {
        const val DEFAULT_LIMIT = 1L shl 30
        const val PARTIAL = ".partial"
        const val LIMIT_FILE = "limit"
        const val ID_FILE = "next_id"
    }
}
