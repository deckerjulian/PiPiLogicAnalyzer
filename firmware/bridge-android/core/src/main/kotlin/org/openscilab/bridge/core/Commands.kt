// Copyright (C) 2026 Julian Decker
// Part of openSciLab. SPDX-License-Identifier: GPL-3.0-or-later

package org.openscilab.bridge.core

/** The `:BRIDge:*` commands of docs/protocols.md ("Bridge", protocol 1). */
sealed class BridgeCommand {
    object Version : BridgeCommand()
    data class Bench(val bytes: Long) : BridgeCommand()
    data class Snapshot(val channels: ChannelSet) : BridgeCommand()
    object CacheList : BridgeCommand()
    data class Meta(val id: Int) : BridgeCommand()
    data class Overview(val id: Int, val channel: String, val level: Int) : BridgeCommand()
    data class Tile(val id: Int, val channel: String, val start: Long, val count: Long) : BridgeCommand()
    /** [id] null: all snapshots. */
    data class CacheDelete(val id: Int?) : BridgeCommand()
    data class CacheLimit(val bytes: Long) : BridgeCommand()
    /** A `:BRIDge:` message that is not understood; answered with `ERR <message>`. */
    data class Invalid(val message: String) : BridgeCommand()
}

/** The channels of a snapshot: the digital pod (`D`, from `D0-D15`) and analog `CH1`...`CH4`. */
data class ChannelSet(val digital: List<Int>, val analog: List<Int>) {
    /** The stored channel names: `D` and `CH1`...; as used by OVERview and TILE. */
    val names: List<String> get() = (if (digital.isEmpty()) emptyList() else listOf("D")) + analog.map { "CH$it" }

    /** As in SNAPshot, normalised: `D0-D15,CH1`. */
    override fun toString(): String = (digitalRanges() + analog.map { "CH$it" }).joinToString(",")

    private fun digitalRanges(): List<String> {
        val result = ArrayList<String>()
        var index = 0
        while (index < digital.size) {
            var end = index
            while (end + 1 < digital.size && digital[end + 1] == digital[end] + 1) end++
            result.add(if (end == index) "D${digital[index]}" else "D${digital[index]}-D${digital[end]}")
            index = end + 1
        }
        return result
    }

    companion object {
        const val DIGITAL_CHANNELS = 16
        const val ANALOG_CHANNELS = 4

        /** Parses `D0-D15,CH1,CH3` (also `D3`, `D0-D7`, `CHANnel2`); throws on anything else. */
        fun parse(text: String): ChannelSet {
            val digital = sortedSetOf<Int>()
            val analog = sortedSetOf<Int>()
            for (raw in text.split(',')) {
                val part = raw.trim().trim('"', '\'').uppercase()
                if (part.isEmpty()) continue
                val range = Regex("D(\\d+)(?:-D?(\\d+))?").matchEntire(part)
                val channel = Regex("(?:CHANNEL|CHAN|CH)(\\d)").matchEntire(part)
                when {
                    range != null -> {
                        val first = range.groupValues[1].toInt()
                        val last = range.groupValues[2].ifEmpty { range.groupValues[1] }.toInt()
                        if (first > last || last >= DIGITAL_CHANNELS) throw IllegalArgumentException("bad channels $part")
                        digital.addAll(first..last)
                    }
                    channel != null -> {
                        val number = channel.groupValues[1].toInt()
                        if (number !in 1..ANALOG_CHANNELS) throw IllegalArgumentException("bad channel $part")
                        analog.add(number)
                    }
                    else -> throw IllegalArgumentException("unknown channel $part")
                }
            }
            if (digital.isEmpty() && analog.isEmpty()) throw IllegalArgumentException("no channels")
            return ChannelSet(digital.toList(), analog.toList())
        }
    }
}

object CommandParser {
    /** Whether [message] is for the bridge (its first mnemonic is `BRIDge`). */
    fun isBridge(message: String): Boolean {
        val header = message.trimStart().substringBefore(' ').substringBefore('\t').trimStart(':')
        return matches("BRIDge", header.substringBefore(':').removeSuffix("?"))
    }

    fun parse(message: String): BridgeCommand {
        val text = message.trim()
        val split = text.indexOfFirst { it == ' ' || it == '\t' }
        val header = if (split < 0) text else text.substring(0, split)
        val argument = if (split < 0) "" else text.substring(split + 1).trim()
        val query = header.endsWith("?")
        val path = header.removeSuffix("?").trimStart(':').split(':')
        if (path.isEmpty() || !matches("BRIDge", path[0])) return BridgeCommand.Invalid("not a bridge command")
        val rest = path.drop(1)
        val args = if (argument.isEmpty()) emptyList() else argument.split(',').map { it.trim() }
        return try {
            when {
                rest.size == 1 && matches("VERSion", rest[0]) && query -> BridgeCommand.Version
                rest.size == 1 && matches("BENCh", rest[0]) && query ->
                    BridgeCommand.Bench(number(args, 0, "bytes").also { if (it <= 0) bad("bytes must be positive") })
                rest.size == 1 && matches("SNAPshot", rest[0]) -> BridgeCommand.Snapshot(ChannelSet.parse(argument))
                rest.size == 1 && matches("META", rest[0]) && query -> BridgeCommand.Meta(id(args, 0))
                rest.size == 1 && matches("OVERview", rest[0]) && query -> {
                    expect(args, 3)
                    BridgeCommand.Overview(id(args, 0), channel(args[1]), number(args, 2, "level").toInt()
                        .also { if (it !in 0..Pyramid.MAX_LEVEL) bad("level must be 0..${Pyramid.MAX_LEVEL}") })
                }
                rest.size == 1 && matches("TILE", rest[0]) && query -> {
                    expect(args, 4)
                    val start = number(args, 2, "start")
                    val count = number(args, 3, "count")
                    if (start < 0 || count < 0) bad("start and count must not be negative")
                    BridgeCommand.Tile(id(args, 0), channel(args[1]), start, count)
                }
                rest.size == 2 && matches("CACHe", rest[0]) && matches("LIST", rest[1]) && query -> BridgeCommand.CacheList
                rest.size == 2 && matches("CACHe", rest[0]) && matches("DELete", rest[1]) && !query -> {
                    expect(args, 1)
                    if (args[0].equals("ALL", ignoreCase = true)) BridgeCommand.CacheDelete(null)
                    else BridgeCommand.CacheDelete(id(args, 0))
                }
                rest.size == 2 && matches("CACHe", rest[0]) && matches("LIMit", rest[1]) && !query ->
                    BridgeCommand.CacheLimit(number(args, 0, "bytes").also { if (it < 0) bad("bytes must not be negative") })
                else -> BridgeCommand.Invalid("unknown bridge command ${header}")
            }
        } catch (error: IllegalArgumentException) {
            BridgeCommand.Invalid(error.message ?: "bad arguments")
        }
    }

    /**
     * SCPI mnemonic matching: the short form (the upper-case part of [pattern]) or the long form,
     * in any case.
     */
    fun matches(pattern: String, token: String): Boolean {
        val short = pattern.filter { it.isUpperCase() || it.isDigit() }
        return token.equals(pattern, ignoreCase = true) || token.equals(short, ignoreCase = true)
    }

    private fun channel(text: String): String {
        val name = text.trim('"', '\'').uppercase()
        if (name == "D") return name
        val match = Regex("(?:CHANNEL|CHAN|CH)([1-4])").matchEntire(name) ?: bad("unknown channel $text (D or CH1...CH4)")
        return "CH${match.groupValues[1]}"
    }

    private fun expect(args: List<String>, count: Int) {
        if (args.size != count) bad("expected $count arguments, got ${args.size}")
    }

    private fun number(args: List<String>, index: Int, name: String): Long {
        if (index >= args.size) bad("missing $name")
        return parseNumber(args[index]) ?: bad("bad $name ${args[index]}")
    }

    /** Integers, also written as SCPI decimals such as `1e6` or `2.0E+07`. */
    private fun parseNumber(text: String): Long? {
        text.toLongOrNull()?.let { return it }
        val value = text.toDoubleOrNull() ?: return null
        if (value != Math.floor(value) || value > Long.MAX_VALUE.toDouble()) return null
        return value.toLong()
    }

    private fun id(args: List<String>, index: Int): Int {
        val value = number(args, index, "id")
        if (value !in 1..Int.MAX_VALUE.toLong()) bad("bad id ${args[index]}")
        return value.toInt()
    }

    private fun bad(message: String): Nothing = throw IllegalArgumentException(message)
}
