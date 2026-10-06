package org.openscilab.bridge.core

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class ScpiTest {
    private fun bytes(text: String) = text.toByteArray(Charsets.ISO_8859_1)

    @Test
    fun blockHeader() {
        assertEquals("#10", String(ScpiBlock.header(0)))
        assertEquals("#3123", String(ScpiBlock.header(123)))
        assertEquals("#9000001200".length - 2, String(ScpiBlock.header(123_456_789)).length - 2)
        assertEquals("#9123456789", String(ScpiBlock.header(123_456_789)))
    }

    @Test
    fun readsBlocks() {
        val data = bytes("\n#15a\nb\u0000c\n")
        assertContentEquals(bytes("a\nb\u0000c"), ScpiBlock.read(ByteArrayInputStream(data)))
        assertFailsWith<java.io.IOException> { ScpiBlock.read(ByteArrayInputStream(bytes("#15ab"))) }
        assertFailsWith<java.io.IOException> { ScpiBlock.read(ByteArrayInputStream(bytes("1,2\n"))) }
    }

    @Test
    fun copiesAnswers() {
        val input = ByteArrayInputStream(bytes("RIGOL,DHO924S\n#13a\nb\n\n0.5\n"))
        val out = ByteArrayOutputStream()
        ScpiResponse.copy(input, out)
        assertEquals("RIGOL,DHO924S\n", out.toString("ISO-8859-1"))
        out.reset()
        ScpiResponse.copy(input, out)
        assertEquals("#13a\nb\n", out.toString("ISO-8859-1"))
        out.reset()
        ScpiResponse.copy(input, out)
        assertEquals("0.5\n", out.toString("ISO-8859-1"))
    }

    @Test
    fun splitsMessagesKeepingBlocksAndStrings() {
        val reader = ProgramMessageReader(ByteArrayInputStream(bytes(
            "*IDN?\r\n:SOUR:TRAC:DATA #14a\nb\n,1\n:DISP:TEXT \"x\ny\"\n:LAST"
        )))
        assertEquals("*IDN?", String(reader.next()!!))
        assertEquals(":SOUR:TRAC:DATA #14a\nb\n,1", String(reader.next()!!, Charsets.ISO_8859_1))
        assertEquals(":DISP:TEXT \"x\ny\"", String(reader.next()!!))
        assertEquals(":LAST", String(reader.next()!!))
        assertNull(reader.next())
    }

    @Test
    fun recognisesQueries() {
        assertTrue(ProgramMessageReader.isQuery(bytes("*IDN?")))
        assertTrue(ProgramMessageReader.isQuery(bytes(":WAV:PRE?")))
        assertTrue(ProgramMessageReader.isQuery(bytes(":TIM:SCAL 1e-3;:TIM:SCAL?")))
        assertTrue(ProgramMessageReader.isQuery(bytes("  :MEAS:ITEM? VMAX,CHAN1")))
        assertFalse(ProgramMessageReader.isQuery(bytes(":TIM:SCAL 1e-3")))
        assertFalse(ProgramMessageReader.isQuery(bytes(":DISP:TEXT \"what?\"")))
        assertFalse(ProgramMessageReader.isQuery(bytes(":SOUR:TRAC:DATA #13?;?")))
        assertFalse(ProgramMessageReader.isQuery(bytes(":SING")))
    }
}
