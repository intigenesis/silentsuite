package io.silentsuite.sync.notes.edit

import io.silentsuite.sync.notes.edit.NoteMetaCodec.Merge
import io.silentsuite.sync.notes.edit.NoteMetaCodec.Peek
import io.silentsuite.sync.notes.edit.NoteMetaCodec.Why
import org.msgpack.core.MessageFormat
import org.msgpack.core.MessageFormat.*
import org.msgpack.core.MessageIntegerOverflowException
import org.msgpack.core.MessagePack
import org.msgpack.core.MessageUnpacker
import java.io.IOException
import java.nio.ByteBuffer
import java.nio.charset.CharacterCodingException
import java.nio.charset.CodingErrorAction

/*
 * Test only. merge, fresh and peek of NoteMetaCodec written a second time on the low-level API of
 * msgpack-core 0.9.12, so that the two can be compared input by input (NoteMetaCodecComparisonTest).
 * Nothing here ships: the library is a testImplementation dependency.
 *
 * What is the library's and what is the app's. The library does the format layer: which format comes
 * next, the header and typed reads, the position, and the packer's widths. Everything above that is a
 * rule of the codec and is the same code or a copy of it here: the scan loop, the two checks of a
 * count against the bytes left, the two slots, the key rules (webMayRead, regionEquals), the order of
 * the refusals and of the output, finiteToLong, wellFormed, and the strict decoder for the name.
 * Agreement between the codec and a variant therefore shows that the two format layers give the same
 * byte ranges, formats, numbers and widths. It does not test those rules, and it cannot show a
 * mistake that both sides share. The independent check of the codec's own skipping of nested values
 * is LIBRARY_SKIP on input the codec accepts: there every count is at most the size of the input,
 * far below the 2^30 at which skipValue fails, and its result has to equal the codec's.
 *
 * What this comment says about the inside of the library (how skipValue counts, when addPayload
 * copies, what readPayload(int) and readPayloadAsReference allocate, what UnpackerConfig can limit)
 * is from reading the 0.9.12 sources, not from a run. What was observed on the jar is pinned by the
 * tests of NoteMetaCodecComparisonTest that call the library directly: the buffer class, skipValue
 * on a map32 header, the widths the writers choose, packString on an unpaired surrogate, the default
 * and the strict unpacker settings, and unpackLong above Long.MAX_VALUE.
 *
 * Requirement by requirement. (a) is one direct library call, (b) is possible only by slicing the
 * original array at offsets taken from getTotalReadBytes, (c) is not possible through the library.
 *
 * R1  One map that uses every byte: (a) getNextFormat is FIXMAP, MAP16 or MAP32, then unpackMapHeader
 *     (an int; MessageSizeException for a count of 2^31 or more); (a) hasNext is false after the walk.
 *     The early check of the count against the bytes left is app code and needs the position: (b).
 * R2  Every other key and value is copied byte for byte: (b) only. The offsets before and after each
 *     key and value come from getTotalReadBytes, and that span of the original array goes to the
 *     packer with addPayload. addPayload copies the span into the packer's buffer when it fits there
 *     and is at most the flush threshold (8192 by default); any other span is added by reference.
 *     writePayload always copies. The value model (unpackValue, the method of CVE-2026-90472, then
 *     packValue) is not used and was not run: it decodes and encodes again, which is what R2 rules
 *     out.
 *     Passing over a value whose content is not needed, three ways (see Walk):
 *       LIBRARY_SKIP  skipValue on every key and value. On a map32 header that declares from 2^30
 *                     to 2^31 - 1 entries it returns normally in the middle of the input
 *                     (CVE-2026-90473); from 2^31 on it throws MessageSizeException. Not usable on
 *                     a container.
 *       LEAF_SKIP     An app loop with a long counter that is checked against the bytes left. Maps
 *                     go through unpackMapHeader, arrays through unpackArrayHeader, the reserved
 *                     byte 0xc1 (format NEVER_USED) is refused, a key in an unsigned integer format
 *                     is read with unpackLong (R6), and every other value goes through skipValue.
 *                     skipValue decrements its counter for every value; a value that is not a
 *                     container adds nothing to it, so the counter goes from 1 to 0 and the
 *                     branches that add a container count are not reached. This still calls the
 *                     method the advisory names.
 *       HEADERS_ONLY  No skipValue. NIL unpackNil; BOOLEAN unpackBoolean; POSFIXINT, NEGFIXINT,
 *                     UINT8, UINT16, UINT32 and INT8 to INT64 unpackLong; UINT64 unpackBigInteger
 *                     (a BigInteger per value, two above Long.MAX_VALUE; unpackLong throws there);
 *                     FLOAT32 and FLOAT64 unpackDouble;
 *                     FIXSTR and STR8 to STR32 unpackRawStringHeader; BIN8 to BIN32
 *                     unpackBinaryHeader; FIXEXT1 to FIXEXT16 and EXT8 to EXT32
 *                     unpackExtensionTypeHeader (one small object per ext value). The payload bytes
 *                     are read into a scratch array with readPayload(array, 0, n) after an app check
 *                     of the length against the bytes left, since the unpacker's own skip for
 *                     payload bytes is private. readPayload(int) and readPayloadAsReference are not
 *                     used: both can allocate from the declared length.
 * R3  Replace in place, or append: finding the value is (b); writing is (a): packRawStringHeader plus
 *     addPayload for the name, packLong for mtime, packString for the two appended key names.
 * R4  The map header at the smallest width for the new count: (a) packMapHeader.
 * R5  A repeated plain key: app code. The format is (a) getNextFormat; the key bytes are (b).
 * R6  Aliases. Unsigned integer keys: (a) getNextFormat is POSFIXINT or UINT8 to UINT64, and the
 *     careful variants then read the key with unpackLong on the walk's own unpacker, which allocates
 *     nothing; a value above Long.MAX_VALUE arrives as MessageIntegerOverflowException after the
 *     nine bytes are consumed, which costs one exception for such a key. (LIBRARY_SKIP has no typed
 *     read on the walk, so it decodes the key through a second unpacker, which allocates per integer
 *     key.) Binary keys: (a) the format, (b) the bytes. Malformed UTF-8 that the web reads leniently:
 *     (c) through the library; (b) with the codec's webMayRead on the raw bytes.
 * R7  Strict UTF-8 for the name that peek returns: possible as (a) with an UnpackerConfig whose two
 *     coding actions are REPORT and unpackString, but that takes a second unpacker and its decode
 *     buffer of 8192 chars for every call. Here the name is decoded as (b): the app's own strict
 *     decoder over the raw slice, as the codec does. The comparison test pins what unpackString does
 *     on both settings.
 * R8  mtime numbers: (a) unpackLong for the integer formats (MessageIntegerOverflowException gives
 *     null), unpackDouble for the two float formats, then the codec's finiteToLong. One second
 *     unpacker for each peek, not for each entry.
 * R9  Limits. Lengths and counts against the bytes left: app code with (b) the position; the library
 *     itself only fails when a read runs out of input (MessageInsufficientBufferException) or a
 *     32-bit field is 2^31 or more (MessageSizeException). No recursion: the app loop. Nothing kept
 *     for an entry: the scan holds the array, the count, the body start and two slots; what the
 *     library allocates on the way is measured in the comparison test. UnpackerConfig has one limit
 *     on a size, stringSizeLimit, which applies to unpackString and unpackValue only (bufferSize
 *     and stringDecoderBufferSize set buffer sizes, not limits); it cannot express any of these
 *     limits.
 * R10 Writer widths: (a). The U+FFFD rule stays app code in front of the writer; packString alone
 *     writes a question mark for an unpaired surrogate.
 */

/** Test only: merge, fresh and peek of [NoteMetaCodec] on msgpack-core's low-level API, to compare the two. */
internal class MsgpackCoreNoteMeta(private val walk: Walk) {
    enum class Walk {
        /** skipValue() on every key and value: the library's own skip, the method of CVE-2026-90473. */
        LIBRARY_SKIP,

        /** Containers through header reads and a checked counter; skipValue() only on values that are not containers. */
        LEAF_SKIP,

        /** No skipValue() at all: every format through its header read or typed read. */
        HEADERS_ONLY,
    }

    private class NotOneMap(message: String) : Exception(message)

    private class Slot(val key: String) {
        var plain = 0
        var aliases = 0
        var valueStart = -1
        var valueEnd = -1
        val trusted get() = plain == 1 && aliases == 0
    }

    private class Scan(val raw: ByteArray, val count: Int, val bodyStart: Int) {
        val name = Slot("name")
        val mtime = Slot("mtime")
    }

    private enum class KeyKind { OTHER, NAME, MTIME, NAME_ALIAS, MTIME_ALIAS }

    private val scratch = ByteArray(4096)

    fun merge(raw: ByteArray?, name: String, mtime: Long): Merge {
        val scan = try {
            scan(raw)
        } catch (e: NotOneMap) {
            return Merge.NotAMap(e.message ?: "malformed")
        } catch (e: RuntimeException) {
            // Fail closed: every MessagePackException is one, and so is anything else the library or its
            // buffer class may throw on input it did not expect. The reason names the class, so the
            // comparison can list which ones occurred.
            return Merge.NotAMap(e.javaClass.simpleName)
        } catch (e: IOException) {
            // Declared by every unpacker method; an array input has no reason to throw it.
            return Merge.NotAMap(e.javaClass.simpleName)
        }
        for (slot in arrayOf(scan.name, scan.mtime)) {
            if (slot.aliases > 0) return Merge.Refused(slot.key, Why.ALIASED)
            if (slot.plain > 1) return Merge.Refused(slot.key, Why.REPEATED)
        }
        // From here on the offsets are the scan's. An exception below would be a defect, and it is left to
        // reach the comparison, which reports it as a line.
        val written = NoteMetaCodec.wellFormed(name)
        val nameBytes = written.toByteArray(Charsets.UTF_8)
        val packer = PACKER.newBufferPacker()
        fun copy(from: Int, to: Int) {
            if (to > from) packer.addPayload(scan.raw, from, to - from)
        }
        fun writeName() {
            packer.packRawStringHeader(nameBytes.size)
            if (nameBytes.isNotEmpty()) packer.addPayload(nameBytes)
        }
        packer.packMapHeader(scan.count + (if (scan.name.plain == 0) 1 else 0) + (if (scan.mtime.plain == 0) 1 else 0))
        var from = scan.bodyStart
        for (slot in listOf(scan.name, scan.mtime).filter { it.plain == 1 }.sortedBy { it.valueStart }) {
            copy(from, slot.valueStart)
            if (slot === scan.name) writeName() else packer.packLong(mtime)
            from = slot.valueEnd
        }
        copy(from, scan.raw.size)
        if (scan.name.plain == 0) {
            packer.packString("name")
            writeName()
        }
        if (scan.mtime.plain == 0) {
            packer.packString("mtime")
            packer.packLong(mtime)
        }
        return Merge.Merged(packer.toByteArray(), written)
    }

    fun fresh(name: String, mtime: Long): Merge.Merged = merge(byteArrayOf(0x80.toByte()), name, mtime) as Merge.Merged

    fun peek(raw: ByteArray?): Peek? {
        val scan = try {
            scan(raw)
        } catch (e: NotOneMap) {
            return null
        } catch (e: RuntimeException) {
            return null // fail closed, as in merge
        } catch (e: IOException) {
            return null
        }
        // As in merge, the two reads below work on offsets of the scan and are outside the catch: anything
        // thrown there reaches the comparison, which reports it as a line.
        val name = scan.name.takeIf { it.trusted }?.let { stringValue(scan.raw, it.valueStart, it.valueEnd) }
        val mtime = scan.mtime.takeIf { it.trusted }?.let { numberValue(scan.raw, it.valueStart, it.valueEnd) }
        return Peek(name, mtime)
    }

    /** Exactly one map that uses every byte of [raw], read in one pass. */
    private fun scan(raw: ByteArray?): Scan {
        if (raw == null || raw.isEmpty()) throw NotOneMap("empty")
        // Over a byte array the position of the unpacker is the index of the next unread byte.
        val u = STRICT.newUnpacker(raw)
        val top = u.nextFormat
        if (top != FIXMAP && top != MAP16 && top != MAP32) throw NotOneMap("the top level is not a map")
        val count = u.unpackMapHeader()
        val scan = Scan(raw, count, u.totalReadBytes.toInt())
        if (2L * count > raw.size - scan.bodyStart) throw NotOneMap("a container count runs past the end of the input")
        for (i in 0 until count) {
            val keyStart = u.totalReadBytes.toInt()
            val keyFormat = u.nextFormat
            val index = passKey(u, raw, keyFormat, keyStart)
            val keyEnd = u.totalReadBytes.toInt()
            pass(u, raw)
            val valueEnd = u.totalReadBytes.toInt()
            val kind = keyKind(raw, keyFormat, keyStart, keyEnd, index)
            val slot = when (kind) {
                KeyKind.OTHER -> continue
                KeyKind.NAME, KeyKind.NAME_ALIAS -> scan.name
                KeyKind.MTIME, KeyKind.MTIME_ALIAS -> scan.mtime
            }
            if (kind == KeyKind.NAME || kind == KeyKind.MTIME) {
                if (slot.plain++ == 0) {
                    slot.valueStart = keyEnd
                    slot.valueEnd = valueEnd
                }
            } else {
                slot.aliases++
            }
        }
        if (u.hasNext()) throw NotOneMap("trailing bytes after the map")
        return scan
    }

    /**
     * Passes over one key. A key in an unsigned integer format is a field index to the typed decoder, so
     * its value is returned; every other key gives [NO_INDEX].
     */
    private fun passKey(u: MessageUnpacker, raw: ByteArray, f: MessageFormat, start: Int): Long {
        val unsigned = f == POSFIXINT || f == UINT8 || f == UINT16 || f == UINT32 || f == UINT64
        if (walk == Walk.LIBRARY_SKIP) {
            u.skipValue()
            if (!unsigned) return NO_INDEX
            // This variant has no typed read on the walk, so the key is decoded a second time, through an
            // unpacker of its own: several small objects for every integer key.
            return try {
                STRICT.newUnpacker(raw, start, u.totalReadBytes.toInt() - start).unpackLong()
            } catch (e: MessageIntegerOverflowException) {
                NO_INDEX
            }
        }
        if (!unsigned) {
            pass(u, raw)
            return NO_INDEX
        }
        return try {
            u.unpackLong()
        } catch (e: MessageIntegerOverflowException) {
            // Above Long.MAX_VALUE, so not a field index. unpackLong throws after it has consumed the nine
            // bytes; the walk goes on only if the position says so.
            if (u.totalReadBytes != start + 9L) throw NotOneMap("the position after an integer overflow is not the end of the value")
            NO_INDEX
        }
    }

    /** Passes over one value, containers included. */
    private fun pass(u: MessageUnpacker, raw: ByteArray) {
        if (walk == Walk.LIBRARY_SKIP) {
            u.skipValue()
            return
        }
        var pending = 1L
        while (pending > 0) {
            pending--
            when (val f = u.nextFormat) {
                FIXMAP, MAP16, MAP32 -> pending += 2L * u.unpackMapHeader()
                FIXARRAY, ARRAY16, ARRAY32 -> pending += u.unpackArrayHeader()
                NEVER_USED -> throw NotOneMap("reserved type 0xc1")
                else -> if (walk == Walk.LEAF_SKIP) u.skipValue() else leaf(u, f, raw)
            }
            // Every value takes at least one byte, so more values than bytes left cannot be there.
            if (pending > raw.size - u.totalReadBytes) throw NotOneMap("a container count runs past the end of the input")
        }
    }

    private fun leaf(u: MessageUnpacker, f: MessageFormat, raw: ByteArray) {
        when (f) {
            NIL -> u.unpackNil()
            BOOLEAN -> u.unpackBoolean()
            UINT64 -> u.unpackBigInteger() // unpackLong throws above Long.MAX_VALUE
            POSFIXINT, NEGFIXINT, UINT8, UINT16, UINT32, INT8, INT16, INT32, INT64 -> u.unpackLong()
            FLOAT32, FLOAT64 -> u.unpackDouble()
            FIXSTR, STR8, STR16, STR32 -> payload(u, u.unpackRawStringHeader(), raw)
            BIN8, BIN16, BIN32 -> payload(u, u.unpackBinaryHeader(), raw)
            FIXEXT1, FIXEXT2, FIXEXT4, FIXEXT8, FIXEXT16, EXT8, EXT16, EXT32 -> payload(u, u.unpackExtensionTypeHeader().length, raw)
            else -> error("containers and 0xc1 are handled by the caller")
        }
    }

    /**
     * MessageUnpacker's skip for payload bytes is private, so they are read into a scratch array. The
     * length is checked against the bytes left first; nothing is allocated from it.
     */
    private fun payload(u: MessageUnpacker, length: Int, raw: ByteArray) {
        if (length < 0 || length > raw.size - u.totalReadBytes) throw NotOneMap("a length runs past the end of the input")
        var left = length
        while (left > 0) {
            val n = minOf(left, scratch.size)
            u.readPayload(scratch, 0, n)
            left -= n
        }
    }

    private fun keyKind(raw: ByteArray, f: MessageFormat, start: Int, end: Int, index: Long): KeyKind = when (f) {
        FIXSTR, STR8, STR16, STR32 -> {
            val from = start + headerSize(f)
            when {
                regionEquals(raw, from, end, NAME_BYTES) -> KeyKind.NAME
                regionEquals(raw, from, end, MTIME_BYTES) -> KeyKind.MTIME
                webMayRead(raw, from, end, NAME_BYTES) -> KeyKind.NAME_ALIAS
                webMayRead(raw, from, end, MTIME_BYTES) -> KeyKind.MTIME_ALIAS
                else -> KeyKind.OTHER
            }
        }
        BIN8, BIN16, BIN32 -> {
            val from = start + headerSize(f)
            when {
                regionEquals(raw, from, end, NAME_BYTES) -> KeyKind.NAME_ALIAS
                regionEquals(raw, from, end, MTIME_BYTES) -> KeyKind.MTIME_ALIAS
                else -> KeyKind.OTHER
            }
        }
        // Unsigned formats only: the typed decoder reads them as field indexes (1 name, 2 mtime); a signed 1 is not one.
        POSFIXINT, UINT8, UINT16, UINT32, UINT64 -> when (index) {
            1L -> KeyKind.NAME_ALIAS
            2L -> KeyKind.MTIME_ALIAS
            else -> KeyKind.OTHER
        }
        else -> KeyKind.OTHER
    }

    private fun headerSize(f: MessageFormat): Int = when (f) {
        STR8, BIN8 -> 2
        STR16, BIN16 -> 3
        STR32, BIN32 -> 5
        else -> 1
    }

    /**
     * The string in [start, end), decoded as strict UTF-8 with the app's own decoder, or null when the
     * value is not a string or not valid. The format comes from the library, the bytes from the array.
     */
    private fun stringValue(raw: ByteArray, start: Int, end: Int): String? {
        val f = MessageFormat.valueOf(raw[start])
        if (f != FIXSTR && f != STR8 && f != STR16 && f != STR32) return null
        val from = start + headerSize(f)
        return try {
            Charsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(raw, from, end - from))
                .toString()
        } catch (e: CharacterCodingException) {
            null
        }
    }

    private fun numberValue(raw: ByteArray, start: Int, end: Int): Long? {
        val u = STRICT.newUnpacker(raw, start, end - start)
        return when (u.nextFormat) {
            FLOAT32, FLOAT64 -> finiteToLong(u.unpackDouble())
            POSFIXINT, NEGFIXINT, UINT8, UINT16, UINT32, UINT64, INT8, INT16, INT32, INT64 -> try {
                u.unpackLong()
            } catch (e: MessageIntegerOverflowException) {
                null // above Long.MAX_VALUE: not a usable mtime
            }
            else -> null
        }
    }

    // ---- copied unchanged from NoteMetaCodec, where they are private: rules of the app, not of the format ----

    private fun webMayRead(b: ByteArray, from: Int, end: Int, target: ByteArray): Boolean {
        var offset = from
        var n = 0
        while (offset < end) {
            if (n == target.size) return false
            val byte1 = b[offset].toInt() and 0xff
            val extra = when {
                byte1 and 0x80 == 0 -> 0
                byte1 and 0xe0 == 0xc0 -> 1
                byte1 and 0xf0 == 0xe0 -> 2
                byte1 and 0xf8 == 0xf0 -> 3
                else -> 0 // a stray continuation byte or 0xf8..0xff is kept as it is
            }
            if (offset + extra >= end) {
                n++ // cut off: its value depends on what follows the key
                break
            }
            var unit = when (extra) {
                0 -> byte1
                3 -> byte1 and 0x07
                else -> byte1 and 0x1f
            }
            for (k in 1..extra) unit = (unit shl 6) or (b[offset + k].toInt() and 0x3f)
            if (unit != (target[n].toInt() and 0xff)) return false
            n++
            offset += extra + 1
        }
        return n == target.size
    }

    private fun regionEquals(b: ByteArray, from: Int, end: Int, target: ByteArray): Boolean {
        if (end - from != target.size) return false
        for (i in target.indices) if (b[from + i] != target[i]) return false
        return true
    }

    private fun finiteToLong(d: Double): Long? =
        if (d.isFinite() && d >= Long.MIN_VALUE.toDouble() && d < Long.MAX_VALUE.toDouble()) d.toLong() else null

    companion object {
        private const val NO_INDEX = -1L
        private val NAME_BYTES = "name".toByteArray(Charsets.UTF_8)
        private val MTIME_BYTES = "mtime".toByteArray(Charsets.UTF_8)

        /** Both coding actions REPORT and no reading of one raw family as the other. */
        internal val STRICT: MessagePack.UnpackerConfig = MessagePack.UnpackerConfig()
            .withActionOnMalformedString(CodingErrorAction.REPORT)
            .withActionOnUnmappableString(CodingErrorAction.REPORT)
            .withAllowReadingBinaryAsString(false)
            .withAllowReadingStringAsBinary(false)
        private val PACKER = MessagePack.PackerConfig().withBufferSize(512)
    }
}
