package io.silentsuite.sync.resource

import at.bitfire.vcard4android.Contact
import at.bitfire.vcard4android.GroupMethod
import ezvcard.VCardVersion
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.io.StringReader
import java.text.SimpleDateFormat
import java.util.Locale

/**
 * Yearless birthdays through the sync parser (repair plan v2, D6). The app module hosts this
 * because CI runs app:testDebugUnitTest, not the vendored library's unit tests.
 *
 * Synthetic cards only, parsed by the real Contact.fromReader the sync adapter uses. Behavioral
 * failures are assertion failures prefixed "BEHAVIOR"; setup problems are IllegalStateException
 * prefixed "FIXTURE" and must not be read as a reproduction. ANNIVERSARY is out of scope.
 */
class YearlessBirthdayVCardTest {

    @Test fun `legacy vCard 3 partial birthdays written by the web are kept without a year`() {
        for (value in listOf("--0416", "--04-16")) {
            val contact = parse(card("3.0", "BDAY:$value"))
            assertMonthDay("BEHAVIOR legacy v3 $value", 4, 16, contact)
            assertNoBdayInUnknown("legacy v3 $value", contact)
        }
    }

    @Test fun `legacy vCard 3 leap-day partial birthday is kept`() {
        assertMonthDay("BEHAVIOR legacy v3 leap day", 2, 29, parse(card("3.0", "BDAY:--0229")))
    }

    @Test fun `recovered legacy birthday is written as a vCard 4 partial date and reparses`() {
        val recovered = parse(card("3.0", "BDAY:--0416"))
        val os = ByteArrayOutputStream()
        recovered.write(VCardVersion.V4_0, GroupMethod.GROUP_VCARDS, os)
        val written = os.toString("UTF-8")
        assertTrue("BEHAVIOR v4 write keeps a partial BDAY line:\n$written",
            Regex("(?im)^BDAY(;[^:]*)?:--04-?16\\r?$").containsMatchIn(written))
        assertMonthDay("BEHAVIOR v4 reparse", 4, 16, parse(written))
    }

    @Test fun `invalid month-day partials are still dropped without throwing`() {
        for (value in listOf("--0230", "--0431", "--1301", "--0016", "--04-1", "--04")) {
            val contact = parse(card("3.0", "BDAY:$value"))
            assertNull("BEHAVIOR invalid $value is not recovered", contact.birthDay)
            assertNoBdayInUnknown("invalid $value", contact)
        }
    }

    @Test fun `vCard 3 text-typed birthday is not recovered as a date`() {
        for (line in listOf("BDAY;VALUE=text:--0416", "BDAY;VALUE=TEXT:--04-16")) {
            val contact = parse(card("3.0", line))
            assertNull("BEHAVIOR $line keeps text semantics (no date recovery)", contact.birthDay?.partialDate)
            assertNull("BEHAVIOR $line is not a full date", contact.birthDay?.date)
        }
    }

    @Test fun `a typed full date wins over a raw partial in either order`() {
        for (lines in listOf(listOf("BDAY:1990-04-16", "BDAY:--0416"), listOf("BDAY:--0416", "BDAY:1990-04-16"))) {
            val contact = parse(card("3.0", *lines.toTypedArray()))
            assertEquals("BEHAVIOR typed precedence $lines", "1990-04-16", fullDate(contact))
            assertNull("BEHAVIOR typed precedence $lines has no partial", contact.birthDay?.partialDate)
        }
    }

    @Test fun `fully dated vCard 3 and vCard 4 birthdays are unchanged`() {
        for ((version, value) in listOf("3.0" to "1990-04-16", "4.0" to "1990-04-16", "3.0" to "19900416", "4.0" to "19900416")) {
            assertEquals("BEHAVIOR full date v$version $value", "1990-04-16", fullDate(parse(card(version, "BDAY:$value"))))
        }
    }

    @Test fun `vCard 4 partial birthday control`() {
        assertMonthDay("BEHAVIOR v4 basic", 4, 16, parse(card("4.0", "BDAY:--0416")))
        assertMonthDay("BEHAVIOR v4 extended", 4, 16, parse(card("4.0", "BDAY:--04-16")))
    }

    @Test fun `the marked 1604 form the web will write reads back without a year`() {
        // Exact lines the core generator emits under VERSION:3.0 for --04-16 and --02-29.
        assertMonthDay("BEHAVIOR core marked form", 4, 16,
            parse(card("3.0", "BDAY;X-APPLE-OMIT-YEAR=1604:1604-04-16")))
        assertMonthDay("BEHAVIOR core marked leap day", 2, 29,
            parse(card("3.0", "BDAY;X-APPLE-OMIT-YEAR=1604:1604-02-29")))
        assertEquals("BEHAVIOR mismatched marker keeps the full date", "1990-04-16",
            fullDate(parse(card("3.0", "BDAY;X-APPLE-OMIT-YEAR=1604:1990-04-16"))))
    }

    @Test fun `a card without a birthday has none`() {
        assertNull("BEHAVIOR no BDAY", parse(card("3.0")).birthDay)
    }

    private fun card(version: String, vararg lines: String): String =
        (listOf("BEGIN:VCARD", "VERSION:$version", "UID:synthetic-bday", "FN:Synthetic Person", "N:Person;Synthetic;;;") +
            lines + "END:VCARD").joinToString("\r\n") + "\r\n"

    private fun parse(text: String): Contact {
        val contacts = Contact.fromReader(StringReader(text), null)
        if (contacts.size != 1) throw IllegalStateException("FIXTURE: expected one synthetic contact, got ${contacts.size}")
        return contacts[0]
    }

    private fun assertMonthDay(message: String, month: Int, day: Int, contact: Contact) {
        val birthday = contact.birthDay
        assertNotNull("$message: birthday present", birthday)
        assertNull("$message: no full date (no invented year)", birthday!!.date)
        val partial = birthday.partialDate
        assertNotNull("$message: partial date", partial)
        assertEquals("$message: month", month, partial!!.month)
        assertEquals("$message: day", day, partial.date)
        assertNull("$message: no year", partial.year)
    }

    private fun assertNoBdayInUnknown(label: String, contact: Contact) {
        assertFalse("BEHAVIOR $label: BDAY must not leak into unknown properties",
            contact.unknownProperties?.contains(Regex("(?im)^BDAY[;:]")) == true)
    }

    private fun fullDate(contact: Contact): String? =
        contact.birthDay?.date?.let { SimpleDateFormat("yyyy-MM-dd", Locale.ROOT).format(it) }
}
