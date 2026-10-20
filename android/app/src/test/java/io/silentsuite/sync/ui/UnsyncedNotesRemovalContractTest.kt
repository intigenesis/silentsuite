package io.silentsuite.sync.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Text checks of the guard of account removals for unsynced Notes: what it reads, and what it must never read, take or
 * write (the maintainer's rules of 2026-10-18 and 2026-10-20 on #709).
 */
class UnsyncedNotesRemovalContractTest {
    private val ui = File("src/main/java/io/silentsuite/sync/ui")

    /** [file]'s code, without comments, so a KDoc that names something does not count. */
    private fun code(file: File) = file.readText().replace("\r\n", "\n")
        .replace(Regex("(?s)/\\*.*?\\*/"), "").replace(Regex("//[^\n]*"), "")

    private val guard get() = code(File(ui, "PendingNotesRemovalGuard.kt"))

    @Test fun `the guard reads creation ids only through getUserData, and builds no account settings`() {
        // The AccountSettings constructor takes its class monitor and may write user data.
        assertFalse(Regex("""\bAccountSettings\(""").containsMatchIn(guard))
        val reads = Regex("""KEY_CREATION_ID""").findAll(guard).count()
        assertTrue(reads > 0)
        assertEquals("every read of it is a getUserData", reads,
            Regex("""getUserData\([^()]*AccountSettings\.KEY_CREATION_ID\)""").findAll(guard).count())
        for (word in listOf("setUserData", "writeVerified", "presentAccounts(", "removeAccount", "addAccount")) {
            assertFalse(word, guard.contains(word))
        }
    }

    @Test fun `the guard names no Notes setting, cache, session or network, and decides nothing from exists`() {
        for (word in listOf("notes_enabled", "notesEnabled", "NotesFeature", "EtebaseLocalCache", "getEtebase",
                "etebaseSession", "HttpClient", "okhttp", "java.net", "requestSync", "clearAll", "discardStore",
                "coverForTesting")) {
            assertFalse(word, guard.contains(word, ignoreCase = true))
        }
        assertFalse(Regex("""\b(exists|notExists|isFile|isDirectory)\b""").containsMatchIn(guard))
    }

    @Test fun `only the guard asks a removal check, and the files that act on its verdict delete nothing`() {
        val main = File("src/main/java").walk().filter { it.isFile && it.extension in setOf("kt", "java") }
            .associate { it.name to code(it) }
        fun naming(word: Regex) = main.filterValues { word.containsMatchIn(it) }.keys.sorted()
        // An absent answer is one check, which a removal of that account alone may take as nothing to lose: no other
        // code asks a removal check or names that answer, so no clear, discard or sweep can take it for gone.
        val asking = listOf("PendingNotesRemovalGuard.kt", "PendingNotesStore.kt")
        assertEquals(asking, naming(Regex("""\bremovalCheck\(""")))
        assertEquals(asking, naming(Regex("""\bUnlistedAbsent\b""")))
        // The files that ask for the guard's verdict, or compare with an allowed one, are these alone, and none of them
        // clears, discards, unregisters or deletes, or writes a permit.
        val acting = naming(Regex("""\bPendingNotesRemovalGuard\.(decide|decision|allows)\b|\bVerdict\.ALLOW\b"""))
        assertEquals(listOf("PendingNotesRemovalGuard.kt"), acting)
        for (file in acting) {
            for (word in listOf("""\bdiscardStore\(""", """\bclearAll\b""", """\bunregister\(""",
                    """\bdeleteRecursively\b""", """\.delete\(""", """\bdeleteIfExists\b""", """\bFiles\.delete\b""")) {
                assertFalse("$file: $word", Regex(word).containsMatchIn(main.getValue(file)))
            }
            if (file != "PendingNotesRemovalGuard.kt") {
                assertFalse(file, main.getValue(file).contains("PendingNotesDiscardPermits"))
            }
        }
        // The guard names the permit records only for a reader of notes-discard that lists: its rename throws.
        assertEquals(listOf("import io.silentsuite.sync.notes.edit.PendingNotesDiscardPermits",
            "PendingNotesDiscardPermits(File(noBackup, PendingNotesDiscardPermits.ROOT), syncDirectory = {},"),
            guard.lines().map { it.trim() }.filter { "PendingNotesDiscardPermits" in it })
        assertEquals(1, Regex("""\n\s*rename = \{ _, _ -> throw IOException\("this reader only lists"\) \}, """ +
            """presence = presence\)\.listNames\(\)\n""").findAll(guard).count())
    }

    @Test fun `the guard's log line holds its verdict and a count only`() {
        val logs = guard.lines().filter { "Logger." in it }.map { it.trim() }
        assertEquals(listOf("Logger.log.info(\"Notes removal guard: \$verdict after \$checks store checks\")"), logs)
    }
}
