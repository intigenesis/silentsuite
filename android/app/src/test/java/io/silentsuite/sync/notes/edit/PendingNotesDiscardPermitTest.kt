package io.silentsuite.sync.notes.edit

import io.silentsuite.sync.ui.ExactAccountIdentity
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class PendingNotesDiscardPermitTest {
    @get:Rule val tmp = TemporaryFolder()

    private val alice = ExactAccountIdentity("io.silentsuite", "alice@example.invalid", "gen-1")

    /** As in PendingNotesStoreTest: replaces the target in one step on Windows too. */
    private val atomicMove: (File, File) -> Unit = { from, to ->
        java.nio.file.Files.move(from.toPath(), to.toPath(),
            java.nio.file.StandardCopyOption.ATOMIC_MOVE, java.nio.file.StandardCopyOption.REPLACE_EXISTING)
    }

    private fun newStore() = PendingNotesStore.open(tmp.newFolder(), rename = atomicMove)

    @After fun reset() {
        PendingNotesDiscardPermit.resetForTesting()
        PendingNotesStore.resetForTesting()
    }

    @Test fun `a permit covers the generation it names with the store as the user saw it`() {
        val store = newStore()
        store.saveLocal("n1", "b1", "r1", "a".toByteArray(), isCreate = false)
        val shown = store.unsynced()
        assertFalse("nothing is covered before the confirmation", PendingNotesDiscardPermit.covers(alice, shown))
        PendingNotesDiscardPermit.grant(alice, shown)
        assertTrue(PendingNotesDiscardPermit.covers(alice, store.unsynced()))
        assertTrue("asking does not use it up", PendingNotesDiscardPermit.covers(alice, store.unsynced()))
    }

    @Test fun `a save after the confirmation ends it, also when the count is the same`() {
        val store = newStore()
        store.saveLocal("n1", "b1", "r1", "a".toByteArray(), isCreate = false)
        val shown = store.unsynced()
        PendingNotesDiscardPermit.grant(alice, shown)
        store.saveLocal("n1", "b1", "r2", "typed after the confirmation".toByteArray(), isCreate = false)
        assertEquals("the count did not change", shown.count, store.unsynced().count)
        assertFalse(PendingNotesDiscardPermit.covers(alice, store.unsynced()))
    }

    @Test fun `changes that were pushed meanwhile do not end it`() {
        val store = newStore()
        store.saveLocal("n1", "b1", "r1", "a".toByteArray(), isCreate = false)
        store.saveLocal("n2", "b1", "r1", "b".toByteArray(), isCreate = false)
        val shown = store.unsynced()
        PendingNotesDiscardPermit.grant(alice, shown)
        val sent = store.beginSend("n1")!!
        store.completeSend("n1", sent.version, "r1", "landed".toByteArray())
        val now = store.unsynced()
        assertEquals(UnsyncedNotes(1, shown.sequence, false), now)
        assertTrue(PendingNotesDiscardPermit.covers(alice, now))
    }

    @Test fun `a same-name account with another creation id is not covered, nor another name or type`() {
        val shown = UnsyncedNotes(2, 7, false)
        PendingNotesDiscardPermit.grant(alice, shown)
        assertFalse(PendingNotesDiscardPermit.covers(alice.copy(creationId = "gen-2"), shown))
        assertFalse(PendingNotesDiscardPermit.covers(alice.copy(name = "bob@example.invalid"), shown))
        assertFalse(PendingNotesDiscardPermit.covers(alice.copy(type = "io.silentsuite.other"), shown))
        assertTrue(PendingNotesDiscardPermit.covers(alice, shown))
        val bob = alice.copy(name = "bob@example.invalid")
        PendingNotesDiscardPermit.grant(bob, shown)
        PendingNotesDiscardPermit.withdraw(alice)
        assertTrue("withdrawing one identity leaves the other's permit", PendingNotesDiscardPermit.covers(bob, shown))
    }

    @Test fun `withdrawn it covers nothing, and it never covers more than was shown`() {
        val shown = UnsyncedNotes(2, 7, false)
        assertFalse(PendingNotesDiscardPermit.covers(alice, shown))
        PendingNotesDiscardPermit.grant(alice, shown)
        PendingNotesDiscardPermit.withdraw(alice)
        assertFalse(PendingNotesDiscardPermit.covers(alice, shown))
        PendingNotesDiscardPermit.grant(alice, shown)
        assertFalse("one change more than was shown", PendingNotesDiscardPermit.covers(alice, UnsyncedNotes(3, 7, false)))
        assertFalse("what was counted can no longer be counted", PendingNotesDiscardPermit.covers(alice, UnsyncedNotes(2, 7, true)))
        assertFalse("a store that holds nothing now is not covered: its answer carries sequence 0",
            PendingNotesDiscardPermit.covers(alice, UnsyncedNotes.NOTHING))

        // A permit given for a store that could not be counted: it holds for an answer that again could
        // not be counted, with no higher number of files than was shown, and for no counted answer.
        val uncounted = UnsyncedNotes(0, 0, unreadable = true)
        PendingNotesDiscardPermit.grant(alice, uncounted)
        assertTrue("an answer that could not be counted covers itself", PendingNotesDiscardPermit.covers(alice, uncounted))
        assertFalse("and not a store that is counted now", PendingNotesDiscardPermit.covers(alice, UnsyncedNotes(1, 1, false)))
        PendingNotesDiscardPermit.grant(alice, UnsyncedNotes(2, 0, unreadable = true))
        assertFalse("a higher number of files than was shown", PendingNotesDiscardPermit.covers(alice, UnsyncedNotes(3, 0, unreadable = true)))
        assertTrue("the same number of files", PendingNotesDiscardPermit.covers(alice, UnsyncedNotes(2, 0, unreadable = true)))
        assertFalse("nor a counted answer that carries sequence 0", PendingNotesDiscardPermit.covers(alice, UnsyncedNotes(1, 0, false)))
    }
}
