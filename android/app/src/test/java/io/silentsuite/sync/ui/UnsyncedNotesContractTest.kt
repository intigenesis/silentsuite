package io.silentsuite.sync.ui

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Text checks of the Settings way out for unsynced Notes, which must work offline and without a session (the
 * maintainer's condition of 2026-10-18 on #709) and decides nothing from File.exists().
 */
class UnsyncedNotesContractTest {
    private val ui = File("src/main/java/io/silentsuite/sync/ui")

    /** Every source file of the way out and of the sign-out dialogs it shares its words with. */
    private val files: List<File>
        get() = ui.listFiles()!!.filter { it.name.startsWith("UnsyncedNotes") && it.name.endsWith(".kt") }
            .sortedBy { it.name }

    /** [file]'s code, without comments, so a KDoc that names something does not count. */
    private fun code(file: File) = file.readText().replace("\r\n", "\n")
        .replace(Regex("(?s)/\\*.*?\\*/"), "").replace(Regex("//[^\n]*"), "")

    @Test fun `the Unsynced Notes files name no network, session or sync start`() {
        assertTrue(files.map { it.name }.containsAll(listOf("UnsyncedNotesStores.kt", "UnsyncedNotesDiscardPolicy.kt",
            "UnsyncedNotesFragment.kt")))
        val forbidden = listOf("HttpClient", "EtebaseLocalCache", "getEtebase", "etebaseSession", "KEY_ETEBASE_SESSION",
            "okhttp", "java.net", "NotesSyncRunner", "requestSync", ".request(", "clearAll")
        for (file in files) {
            val code = code(file)
            for (word in forbidden) assertFalse("${file.name} names $word", code.contains(word, ignoreCase = true))
        }
    }

    @Test fun `the Unsynced Notes files decide nothing from exists, notExists, isFile or isDirectory`() {
        // File.exists() answers false for a check that failed as well as for a file that is not there.
        for (file in files) {
            assertFalse(file.name, Regex("""\b(exists|notExists|isFile|isDirectory)\b""").containsMatchIn(code(file)))
        }
    }

    @Test fun `the Settings discard opens stores only through forDirectory`() {
        val stores = code(File(ui, "UnsyncedNotesStores.kt"))
        assertTrue(stores.contains("PendingNotesStore.forDirectory(context"))
        for (other in listOf("forNoBackup", "forIdentity", "PendingNotesStore.open(")) {
            assertFalse(other, stores.contains(other))
        }
    }
}
