package org.openscilab.bridge.core

/** A small JSON reader for the shared test vectors (no library needed). */
class Json private constructor(private val text: String) {
    private var index = 0

    companion object {
        fun parse(text: String): Any? = Json(text).run { value().also { skip(); check(index == text.length) { "trailing data" } } }
    }

    private fun value(): Any? {
        skip()
        return when (val c = text[index]) {
            '{' -> obj()
            '[' -> array()
            '"' -> string()
            't' -> literal("true", true)
            'f' -> literal("false", false)
            'n' -> literal("null", null)
            else -> if (c == '-' || c.isDigit()) number() else error("unexpected $c at $index")
        }
    }

    private fun obj(): Map<String, Any?> {
        val result = LinkedHashMap<String, Any?>()
        index++
        skip()
        if (text[index] == '}') { index++; return result }
        while (true) {
            skip()
            val key = string()
            skip(); expect(':')
            result[key] = value()
            skip()
            if (text[index] == ',') { index++; continue }
            expect('}')
            return result
        }
    }

    private fun array(): List<Any?> {
        val result = ArrayList<Any?>()
        index++
        skip()
        if (text[index] == ']') { index++; return result }
        while (true) {
            result.add(value())
            skip()
            if (text[index] == ',') { index++; continue }
            expect(']')
            return result
        }
    }

    private fun string(): String {
        expect('"')
        val out = StringBuilder()
        while (true) {
            val c = text[index++]
            when (c) {
                '"' -> return out.toString()
                '\\' -> {
                    when (val e = text[index++]) {
                        'n' -> out.append('\n'); 't' -> out.append('\t'); 'r' -> out.append('\r')
                        'b' -> out.append('\b'); 'f' -> out.append('\u000c')
                        'u' -> { out.append(text.substring(index, index + 4).toInt(16).toChar()); index += 4 }
                        else -> out.append(e)
                    }
                }
                else -> out.append(c)
            }
        }
    }

    private fun number(): Any {
        val start = index
        while (index < text.length && (text[index].isDigit() || text[index] in "+-.eE")) index++
        val token = text.substring(start, index)
        return token.toLongOrNull() ?: token.toDouble()
    }

    private fun literal(word: String, value: Any?): Any? {
        check(text.startsWith(word, index)) { "bad literal at $index" }
        index += word.length
        return value
    }

    private fun expect(c: Char) {
        check(text[index] == c) { "expected $c at $index, got ${text[index]}" }
        index++
    }

    private fun skip() {
        while (index < text.length && text[index].isWhitespace()) index++
    }
}
