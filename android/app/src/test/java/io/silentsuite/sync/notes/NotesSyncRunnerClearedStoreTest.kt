package io.silentsuite.sync.notes

import io.silentsuite.sync.notes.edit.PendingNotesStore
import io.silentsuite.sync.notes.edit.PendingNotesStore.DiscardOutcome
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.IOException

/**
 * The push step of a Notes run over a pending store that was cleared: nothing to push once the clear finished, a
 * storage failure while a clear that stopped left files, and the fetch still runs.
 */
class NotesSyncRunnerClearedStoreTest {
    @get:Rule val tmp = TemporaryFolder()

    /** Replaces the target in one step on every platform the tests run on, Windows included. */
    private val atomicMove: (File, File) -> Unit = { from, to ->
        java.nio.file.Files.move(from.toPath(), to.toPath(),
            java.nio.file.StandardCopyOption.ATOMIC_MOVE, java.nio.file.StandardCopyOption.REPLACE_EXISTING)
    }

    @After fun reset() {
        PendingNotesStore.beforeClearForTesting = null
        PendingNotesStore.beforeRemoveForTesting = null
        PendingNotesStore.listFilesForTesting = null
        PendingNotesStore.identityForTesting = null
        PendingNotesStore.resetForTesting()
    }

    private fun storeWithOneChange(name: String = "store"): PendingNotesStore =
        PendingNotesStore.open(tmp.newFolder(name), rename = atomicMove).also {
            it.saveLocal("n1", "b1", "r1", "text".toByteArray(), isCreate = false)
        }

    @Test fun `a store whose clear stopped with a change left is a storage failure, not nothing to push`() {
        val store = storeWithOneChange()
        assertEquals(listOf("n1"), NotesSyncRunner.waitingIn(store)!!.headers.map { it.noteUid })
        PendingNotesStore.beforeClearForTesting = { throw IOException("the clear stops here") }
        assertEquals(DiscardOutcome.NotFinished, store.discardStore(store.unsynced()))
        assertTrue("the change is still on disk", "n1.note" in store.dir.list()!!)
        assertThrows(PendingNotesStore.Cleared::class.java) { store.snapshot { false } }
        assertTrue("still an IllegalStateException, as before",
            runCatching { store.read("n1") }.exceptionOrNull() is IllegalStateException)
        // The run records it as a storage failure, never a success over text still on disk, and still fetches.
        assertThrows(IOException::class.java) { NotesSyncRunner.waitingIn(store) }
        // Every opener of the directory in this process gets the same closed instance.
        val again = PendingNotesStore.open(store.dir, rename = atomicMove)
        assertSame(store, again)
        assertThrows(IOException::class.java) { NotesSyncRunner.waitingIn(again) }
    }

    @Test fun `a cleared store that cannot be counted after its clear stopped is a storage failure too`() {
        val store = storeWithOneChange()
        // A file of a kind this build does not know: the store lists, but cannot be counted.
        File(store.dir, "n2.draft").writeBytes("text".toByteArray())
        val shown = store.unsynced()
        assertTrue("could not be counted", shown.unreadable && shown.listed)
        // The clear removes the entry first and stops at that file, so what it leaves counts no change at all and
        // still cannot be counted: only that keeps the run from taking the store for empty.
        PendingNotesStore.beforeRemoveForTesting = {
            if (it.name == "n2.draft") throw IOException("the clear stops here")
        }
        assertEquals(DiscardOutcome.NotFinished, store.discardStore(shown))
        val left = store.dir.list()!!.toSet()
        assertTrue("the entry went and the file of an unknown kind stayed: $left",
            "n1.note" !in left && "n2.draft" in left)
        val now = store.unsynced()
        assertTrue("no change is counted, and the store cannot be counted: $now", now.count == 0 && now.unreadable)
        assertThrows(PendingNotesStore.Cleared::class.java) { store.snapshot { false } }
        assertThrows(IOException::class.java) { NotesSyncRunner.waitingIn(store) }
    }

    @Test fun `a store that was cleared in full has nothing to push either, also once it is dropped`() {
        val store = storeWithOneChange()
        store.clearAll()
        assertNull(NotesSyncRunner.waitingIn(store))
        val discarded = storeWithOneChange("discarded")
        assertEquals(DiscardOutcome.Done, discarded.discardStore(discarded.unsynced()))
        assertNull(NotesSyncRunner.waitingIn(discarded))
        discarded.unregister()
        assertNull("a dropped instance was cleared in full", NotesSyncRunner.waitingIn(discarded))
    }

    @Test fun `a store that cannot be listed is still a storage failure, not nothing to push`() {
        val store = storeWithOneChange()
        PendingNotesStore.listFilesForTesting = { d -> if (d == store.dir) null else d.listFiles() }
        assertThrows(IOException::class.java) { NotesSyncRunner.waitingIn(store) }
        // A sign-out confirmed for it is only acknowledged: nothing is deleted and the store is not closed, so
        // it stays a storage failure while it cannot be listed, and its change waits to be pushed once it lists.
        PendingNotesStore.identityForTesting = { PendingNotesStore.DirectoryIdentity(device = 7, inode = 1) }
        assertEquals(DiscardOutcome.Acknowledged, store.discardStore(store.unsynced()))
        assertThrows(IOException::class.java) { NotesSyncRunner.waitingIn(store) }
        PendingNotesStore.listFilesForTesting = null
        assertEquals(listOf("n1"), NotesSyncRunner.waitingIn(store)!!.headers.map { it.noteUid })
    }

    @Test fun `a run whose store was cleared skips the push and goes on to its fetch`() {
        // A run needs an account, a server and a cache, so this is checked in the runner's source: the push
        // step reads the store through waitingIn, which returns null for a store cleared in full and throws
        // IOException for one whose clear stopped with files left; the run's catch around the push step takes
        // that IOException for a storage failure; and between the push step and the fetch the run returns only
        // after a push that needs authentication, which neither is.
        val runner = File("src/main/java/io/silentsuite/sync/notes/NotesSyncRunner.kt").readText()
            .replace("\r\n", "\n")
        val push = runner.substringAfter("private fun pushPending(", "").substringBefore("\n    }\n")
        assertTrue(push.contains("val waiting = waitingIn(store) ?: return null"))
        assertEquals("the push step reads what waits only through waitingIn", 0,
            Regex("store\\.snapshot").findAll(push).count())
        val between = runner.substringAfter("push = try {", "").substringBefore("fetchEachNotebook(notebooks")
        assertTrue("a store that could not be read is a storage failure",
            between.substringAfter("} catch (e: IOException) {", "").contains("unreadableStore()"))
        assertTrue(between.contains("if (push?.ended == NotePushStep.Ended.NEEDS_AUTHENTICATION) {"))
        assertEquals("the one return before the fetch is the authentication one", 1,
            Regex("\\breturn\\b").findAll(between).count())
        assertTrue(between.substringAfter("NEEDS_AUTHENTICATION) {").contains("return true"))
    }
}
