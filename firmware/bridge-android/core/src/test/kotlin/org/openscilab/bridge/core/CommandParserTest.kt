package org.openscilab.bridge.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertTrue

class CommandParserTest {
    @Test
    fun recognisesBridgeMessages() {
        assertTrue(CommandParser.isBridge(":BRIDge:VERSion?"))
        assertTrue(CommandParser.isBridge("BRID:VERS?"))
        assertTrue(CommandParser.isBridge(":bridge:cache:list?"))
        assertFalse(CommandParser.isBridge("*IDN?"))
        assertFalse(CommandParser.isBridge(":BRIGhtness 3"))
        assertFalse(CommandParser.isBridge(":WAV:DATA?"))
    }

    @Test
    fun parsesEveryCommand() {
        assertEquals(BridgeCommand.Version, CommandParser.parse(":BRIDge:VERSion?"))
        assertEquals(BridgeCommand.Version, CommandParser.parse(":BRID:VERS?"))
        assertEquals(BridgeCommand.Bench(1_000_000), CommandParser.parse(":BRIDge:BENCH? 1000000"))
        assertEquals(BridgeCommand.Bench(1_000_000), CommandParser.parse(":BRIDGE:BENC? 1e6"))
        assertEquals(
            BridgeCommand.Snapshot(ChannelSet((0..15).toList(), listOf(1, 3))),
            CommandParser.parse(":BRIDge:SNAPshot D0-D15,CH1,CH3"),
        )
        assertEquals(BridgeCommand.CacheList, CommandParser.parse(":BRIDge:CACHe:LIST?"))
        assertEquals(BridgeCommand.Meta(3), CommandParser.parse(":BRIDge:META? 3"))
        assertEquals(BridgeCommand.Overview(3, "D", 10), CommandParser.parse(":BRIDge:OVERview? 3,D,10"))
        assertEquals(BridgeCommand.Overview(3, "CH2", 0), CommandParser.parse(":BRID:OVER? 3, ch2, 0"))
        assertEquals(BridgeCommand.Tile(2, "CH1", 100, 4096), CommandParser.parse(":BRIDge:TILE? 2,CH1,100,4096"))
        assertEquals(BridgeCommand.CacheDelete(4), CommandParser.parse(":BRIDge:CACHe:DELete 4"))
        assertEquals(BridgeCommand.CacheDelete(null), CommandParser.parse(":BRIDge:CACHe:DELete ALL"))
        assertEquals(BridgeCommand.CacheLimit(1L shl 31), CommandParser.parse(":BRIDge:CACHe:LIMit 2147483648"))
    }

    @Test
    fun rejectsBadMessages() {
        for (message in listOf(
            ":BRIDge:FOO?", ":BRIDge:VERSion", ":BRIDge:META? x", ":BRIDge:META? 0",
            ":BRIDge:TILE? 1,CH5,0,10", ":BRIDge:TILE? 1,D,0", ":BRIDge:TILE? 1,D,-1,4",
            ":BRIDge:OVERview? 1,D,63", ":BRIDge:SNAPshot", ":BRIDge:SNAPshot D16", ":BRIDge:SNAPshot CH0",
            ":BRIDge:BENCH? 0", ":BRIDge:CACHe:DELete", ":BRIDge:CACHe:LIMit -1",
        )) {
            assertIs<BridgeCommand.Invalid>(CommandParser.parse(message), message)
        }
    }

    @Test
    fun channelSets() {
        val set = ChannelSet.parse("D0-D7, D9,CHANnel4,ch1")
        assertEquals(listOf(0, 1, 2, 3, 4, 5, 6, 7, 9), set.digital)
        assertEquals(listOf(1, 4), set.analog)
        assertEquals("D0-D7,D9,CH1,CH4", set.toString())
        assertEquals(listOf("D", "CH1", "CH4"), set.names)
        assertEquals(listOf("CH2"), ChannelSet.parse("CH2").names)
    }
}
