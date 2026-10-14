package io.silentsuite.sync.ui

import io.silentsuite.sync.notes.edit.UnsyncedNotes
import org.junit.Assert.*
import org.junit.Test

class CurrentAccountSignOutCoordinatorTest {
    private val target = ExactAccountIdentity("main", "z@example.invalid", "target-generation")
    private val sibling = ExactAccountIdentity("main", "a@example.invalid", "sibling-generation")

    private inner class Fake : CurrentAccountSignOutCoordinator.Seams {
        val events = mutableListOf<String>()
        var captured: CurrentAccountSignOutSnapshot? = CurrentAccountSignOutSnapshot(
            target, listOf("child" to "book"), listOf(target, sibling))
        var absent = false
        var cache = true
        var status = true
        var active = true
        var reconciledActive: ExactAccountIdentity? = sibling
        var children = true
        var listenerInvalidated = false
        var removeCalls = 0
        var closeCalls = 0
        var removeCallback: ((Boolean) -> Unit)? = null
        var childrenCallback: ((Boolean) -> Unit)? = null
        override fun snapshot() = captured.also { events += "snapshot" }
        var cancelThrows = false
        override fun cancelSync(identity: Pair<String, String>) {
            if (cancelThrows) throw IllegalStateException("cancel failed")
            events += "cancel:${identity.first}:${identity.second}"
        }
        // These four Notes seams record no event. clearPendingNotes, further down, records one.
        var unsynced = UnsyncedNotes.NOTHING
        var unsyncedFails = false
        var confirmed: UnsyncedNotes? = null
        /** The account of each withdrawal that was asked, in the order asked. */
        val withdrawnFor = mutableListOf<ExactAccountIdentity>()
        override fun unsyncedNotes(main: ExactAccountIdentity): UnsyncedNotes {
            if (unsyncedFails) throw IllegalStateException("cannot look")
            return unsynced
        }
        override fun confirmNotesDiscard(main: ExactAccountIdentity, shown: UnsyncedNotes) { confirmed = shown }
        override fun withdrawNotesDiscard(main: ExactAccountIdentity) { withdrawnFor += main; confirmed = null }
        var discardConfirmed = true
        var gateThrows = false
        var gateCalls = 0
        val gateAskedFor = mutableListOf<ExactAccountIdentity>()
        override fun notesDiscardConfirmed(main: ExactAccountIdentity): Boolean {
            gateCalls++; gateAskedFor += main
            if (gateThrows) throw IllegalStateException("cannot look")
            return discardConfirmed
        }
        override fun removeMain(main: ExactAccountIdentity, callback: (Boolean) -> Unit) {
            events += "remove"; removeCalls++; removeCallback = callback
        }
        override fun mainGenerationAbsent(main: ExactAccountIdentity) = absent.also { events += "absent:$it" }
        override fun clearCache(main: ExactAccountIdentity) = cache.also { events += "cache" }
        override fun clearStatus(main: ExactAccountIdentity) = status.also { events += "status" }
        var notes = true
        /** The account of each request to clear the pending Notes store, in the order asked. */
        val notesClearedFor = mutableListOf<ExactAccountIdentity>()
        var notesThrows = false
        override fun clearPendingNotes(main: ExactAccountIdentity): Boolean {
            events += "notes"; notesClearedFor += main
            if (notesThrows) throw IllegalStateException("cannot clear")
            return notes
        }
        override fun reconcileActive(main: ExactAccountIdentity, replacement: ExactAccountIdentity?) =
            ActiveAccountReconciliation(active, reconciledActive).also { events += "active:${replacement?.name}" }
        override fun removeAndVerifyChildren(snapshot: CurrentAccountSignOutSnapshot, callback: (Boolean) -> Unit) {
            events += "children"; childrenCallback = callback
        }
        override fun hasObservedMainGenerationInvalidation() = listenerInvalidated
        override fun close() { closeCalls++ }
    }

    @Test fun `confirmed removal follows destructive boundary order and routes deterministic sibling`() {
        val f = Fake(); val c = CurrentAccountSignOutCoordinator(f)
        c.begin()
        assertEquals(listOf("snapshot", "absent:false", "cancel:main:z@example.invalid", "cancel:child:book", "remove"), f.events)
        assertTrue(c.state is CurrentAccountSignOutState.Removing)
        f.absent = true; f.removeCallback!!(true)
        assertTrue("cleanup went on to the address books", c.state is CurrentAccountSignOutState.CleaningUp)
        f.childrenCallback!!(true)
        assertEquals(listOf("cache", "status", "notes", "active:a@example.invalid", "children"), f.events.takeLast(5))
        assertEquals("the store was asked for once, for the snapshot's account", listOf(target), f.notesClearedFor)
        assertEquals(CurrentAccountSignOutState.Complete(sibling), c.state)
        assertEquals(1, f.closeCalls)
    }

    @Test fun `false callback and present row preserve every destructive seam and expose one failure`() {
        val f = Fake(); val states = mutableListOf<CurrentAccountSignOutState>()
        val c = CurrentAccountSignOutCoordinator(f) { states += it }
        c.begin(); f.removeCallback!!(false)
        assertTrue(c.state is CurrentAccountSignOutState.RemovalFailed)
        assertFalse(f.events.any { it in setOf("cache", "status", "notes", "children") || it.startsWith("active:") })
        assertEquals(1, states.filterIsInstance<CurrentAccountSignOutState.RemovalFailed>().size)

        val present = Fake(); val other = CurrentAccountSignOutCoordinator(present)
        other.begin(); present.removeCallback!!(true)
        assertTrue(other.state is CurrentAccountSignOutState.RemovalFailed)
        assertFalse(present.events.contains("cache"))
    }

    @Test fun `delayed callback retains pending state and repeated tap cannot remove twice`() {
        val f = Fake(); val c = CurrentAccountSignOutCoordinator(f)
        c.begin(); c.begin()
        assertEquals(1, f.removeCalls)
        assertTrue(c.state is CurrentAccountSignOutState.Removing)
    }

    @Test fun `cleanup failure retries cleanup only and never repeats absent main removal`() {
        val f = Fake().apply { absent = true; children = false }
        val c = CurrentAccountSignOutCoordinator(f)
        c.begin(); f.childrenCallback!!(false)
        assertTrue(c.state is CurrentAccountSignOutState.CleanupFailed)
        assertEquals(0, f.removeCalls)
        f.childrenCallback = null
        c.begin(); f.childrenCallback!!(true)
        assertEquals(0, f.removeCalls)
        assertTrue(c.state is CurrentAccountSignOutState.Complete)
    }

    @Test fun `replacement generation before retry cannot trigger another main removal`() {
        val f = Fake()
        val c = CurrentAccountSignOutCoordinator(f)
        c.begin()
        f.removeCallback!!(false)
        assertEquals(1, f.removeCalls)

        // The exact old generation is absent; a same-name replacement may own the platform row.
        f.absent = true
        c.begin()
        f.childrenCallback!!(true)
        assertEquals(1, f.removeCalls)
        assertTrue(c.state is CurrentAccountSignOutState.Complete)
    }

    @Test fun `confirmed removal remains cleanup only even if presence later changes`() {
        val f = Fake()
        val c = CurrentAccountSignOutCoordinator(f)
        c.begin()
        f.absent = true
        f.removeCallback!!(true)
        f.childrenCallback!!(false)
        assertTrue(c.state is CurrentAccountSignOutState.CleanupFailed)

        f.absent = false
        c.begin()
        f.childrenCallback!!(true)
        assertEquals(1, f.removeCalls)
        assertTrue(c.state is CurrentAccountSignOutState.Complete)
    }

    @Test fun `cache status and active failures stop child cleanup and remain cleanup-only retryable`() {
        listOf("cache", "status", "notes", "active").forEach { failed ->
            val f = Fake().apply {
                absent = true; cache = failed != "cache"; status = failed != "status"; notes = failed != "notes"; active = failed != "active"
            }
            val c = CurrentAccountSignOutCoordinator(f); c.begin()
            assertTrue(c.state is CurrentAccountSignOutState.CleanupFailed)
            assertFalse(f.events.contains("children")); assertEquals(0, f.removeCalls)
            assertEquals("no step after the failed one was asked", listOf("cache", "status", "notes", "active").let { it.subList(0, it.indexOf(failed) + 1) },
                f.events.filter { it in setOf("cache", "status", "notes") || it.startsWith("active:") }.map { it.substringBefore(':') })
        }
    }

    @Test fun `last account completes with login destination`() {
        val f = Fake().apply {
            captured = CurrentAccountSignOutSnapshot(target, emptyList(), listOf(target)); reconciledActive = null
        }
        val c = CurrentAccountSignOutCoordinator(f); c.begin(); f.absent = true; f.removeCallback!!(true); f.childrenCallback!!(true)
        assertEquals(CurrentAccountSignOutState.Complete(null), c.state)
    }

    @Test fun `concurrently active unrelated sibling is preserved as route`() {
        val other = ExactAccountIdentity("main", "b@example.invalid", "other-generation")
        val f = Fake().apply { reconciledActive = other }
        val c = CurrentAccountSignOutCoordinator(f); c.begin(); f.absent = true
        f.removeCallback!!(true); f.childrenCallback!!(true)
        assertEquals(CurrentAccountSignOutState.Complete(other), c.state)
    }

    @Test fun `monotonic listener invalidation is exposed independently of exact routing`() {
        val f = Fake()
        val c = CurrentAccountSignOutCoordinator(f)
        assertFalse(c.hasObservedMainGenerationInvalidation())

        f.listenerInvalidated = true
        assertTrue(c.hasObservedMainGenerationInvalidation())
        // The seam remains monotonic even if a same-name replacement makes a later direct
        // routing read look present again.
        f.absent = false
        assertTrue(c.hasObservedMainGenerationInvalidation())
    }

    @Test fun `the count comes through the seam, and a seam that cannot look says something may be there`() {
        val f = Fake().apply { unsynced = UnsyncedNotes(2, 7, false) }
        val c = CurrentAccountSignOutCoordinator(f)
        assertEquals(UnsyncedNotes(2, 7, false), c.unsyncedNotes(target))
        f.unsyncedFails = true
        assertEquals(UnsyncedNotes(0, 0, unreadable = true), c.unsyncedNotes(target))
        assertTrue(c.unsyncedNotes(target).anything)
        assertEquals("asking starts nothing", emptyList<String>(), f.events)
    }

    @Test fun `the discard confirmation does not outlive the attempt it was given for`() {
        val shown = UnsyncedNotes(2, 7, false)
        // A removal that Android refuses.
        var f = Fake(); var c = CurrentAccountSignOutCoordinator(f)
        c.confirmNotesDiscard(target, shown)
        c.begin()
        assertEquals("kept while the removal is asked", shown, f.confirmed)
        f.removeCallback!!(false)
        assertNull(f.confirmed)
        assertEquals("the withdrawal was asked once, for the account the confirmation was given for", listOf(target), f.withdrawnFor)
        f.gateThrows = true; c.begin()
        assertEquals("a retry after a failed removal comes to the gate again, and a check that cannot run refuses it", CurrentAccountSignOutState.Refused(checkFailed = true), c.state)
        // A removal that fails before it is asked.
        f = Fake().apply { cancelThrows = true }; c = CurrentAccountSignOutCoordinator(f)
        c.confirmNotesDiscard(target, shown); c.begin()
        assertTrue(c.state is CurrentAccountSignOutState.RemovalFailed)
        assertNull(f.confirmed)
        // A cleanup that fails.
        f = Fake().apply { children = false }; c = CurrentAccountSignOutCoordinator(f)
        c.confirmNotesDiscard(target, shown); c.begin(); f.absent = true; f.removeCallback!!(true); f.childrenCallback!!(false)
        assertTrue(c.state is CurrentAccountSignOutState.CleanupFailed)
        assertNull(f.confirmed)
        // A sign-out that completes.
        f = Fake(); c = CurrentAccountSignOutCoordinator(f)
        c.confirmNotesDiscard(target, shown); c.begin(); f.absent = true; f.removeCallback!!(true); f.childrenCallback!!(true)
        assertTrue(c.state is CurrentAccountSignOutState.Complete)
        assertNull(f.confirmed)
        // No snapshot could be taken: the withdrawal is asked for the account the confirmation was given for.
        f = Fake().apply { captured = null }; c = CurrentAccountSignOutCoordinator(f)
        c.confirmNotesDiscard(target, shown); c.begin()
        assertTrue(c.state is CurrentAccountSignOutState.RemovalFailed)
        assertNull(f.confirmed)
        assertEquals(listOf(target), f.withdrawnFor)
        // A confirmation given for another account than the snapshot's: the withdrawal is asked for both.
        f = Fake(); c = CurrentAccountSignOutCoordinator(f)
        c.confirmNotesDiscard(sibling, shown); c.begin(); f.removeCallback!!(false)
        assertEquals(listOf(sibling, target), f.withdrawnFor)
        // The withdrawal is asked before the end is reported.
        val seams = Fake(); var atEnd: UnsyncedNotes? = shown
        c = CurrentAccountSignOutCoordinator(seams) { if (it is CurrentAccountSignOutState.RemovalFailed) atEnd = seams.confirmed }
        c.confirmNotesDiscard(target, shown); c.begin(); seams.removeCallback!!(false)
        assertNull("the confirmation is withdrawn before the end is reported", atEnd)
    }

    @Test fun `unsynced Notes text without a confirmed discard refuses the sign-out before anything is canceled or removed`() {
        val f = Fake().apply { discardConfirmed = false; confirmed = UnsyncedNotes(2, 7, false) }
        val states = mutableListOf<CurrentAccountSignOutState>()
        val c = CurrentAccountSignOutCoordinator(f) { states += it }
        c.begin()
        assertEquals(CurrentAccountSignOutState.Refused(checkFailed = false), c.state)
        assertEquals("nothing was canceled and nothing removed", listOf("snapshot", "absent:false"), f.events)
        assertEquals(0, f.removeCalls)
        assertEquals("the gate was asked once", 1, f.gateCalls)
        assertEquals("the gate was asked about the snapshot's account", listOf(target), f.gateAskedFor)
        assertNull("a confirmation that does not cover what is there now is not kept", f.confirmed)
        assertEquals("the withdrawal was asked for the snapshot's account", listOf(target), f.withdrawnFor)
        f.discardConfirmed = true
        c.begin()
        assertEquals(1, f.removeCalls)
        assertEquals("a try after a refusal comes to the gate again", 2, f.gateCalls)
        assertTrue(c.state is CurrentAccountSignOutState.Removing)
        assertEquals(1, states.count { it is CurrentAccountSignOutState.Refused })
    }

    @Test fun `a check that cannot run refuses, and the refusal says that it could not run`() {
        val f = Fake().apply { gateThrows = true }
        val c = CurrentAccountSignOutCoordinator(f)
        c.confirmNotesDiscard(target, UnsyncedNotes(2, 7, false))
        c.begin()
        assertEquals(CurrentAccountSignOutState.Refused(checkFailed = true), c.state)
        assertEquals(0, f.removeCalls)
        assertNull("a refusal ends a confirmation that was given through the coordinator", f.confirmed)
        c.begin()
        assertEquals("a second try while the check still cannot run is refused as well", CurrentAccountSignOutState.Refused(checkFailed = true), c.state)
        assertEquals(0, f.removeCalls)
    }

    @Test fun `an account that is already gone is cleaned up, not refused`() {
        val f = Fake().apply { absent = true; children = false; discardConfirmed = false }
        val c = CurrentAccountSignOutCoordinator(f)
        c.begin()
        assertTrue("the row was already gone: cleanup only, nothing to refuse", c.state is CurrentAccountSignOutState.CleaningUp)
        f.childrenCallback!!(false)
        assertTrue(c.state is CurrentAccountSignOutState.CleanupFailed)
        f.childrenCallback = null
        c.begin(); f.childrenCallback!!(true)
        assertTrue(c.state is CurrentAccountSignOutState.Complete)
        assertEquals(0, f.removeCalls)
    }

    @Test fun `an acknowledged refusal returns to idle and the next try takes a new snapshot`() {
        val f = Fake().apply { discardConfirmed = false }
        val states = mutableListOf<CurrentAccountSignOutState>()
        val c = CurrentAccountSignOutCoordinator(f) { states += it }
        c.begin(); c.acknowledgeRefusal()
        assertEquals(CurrentAccountSignOutState.Idle, c.state)
        assertEquals("the refusal and the return to idle are both reported",
            listOf(CurrentAccountSignOutState.Idle, CurrentAccountSignOutState.Refused(checkFailed = false), CurrentAccountSignOutState.Idle), states)
        f.discardConfirmed = true
        c.begin()
        assertEquals("a refused try keeps no snapshot", 2, f.events.count { it == "snapshot" })
        // Acknowledging while a removal is under way changes nothing.
        c.acknowledgeRefusal()
        assertTrue(c.state is CurrentAccountSignOutState.Removing)
        f.removeCallback!!(false); c.acknowledgeRefusal()
        assertTrue("acknowledging after a failed removal changes nothing", c.state is CurrentAccountSignOutState.RemovalFailed)
    }

    @Test fun `a pending Notes store that cannot be cleared fails cleanup, and the retry clears it without another removal`() {
        val f = Fake().apply { notes = false }
        val c = CurrentAccountSignOutCoordinator(f)
        c.begin()
        assertTrue("the gate let the removal start", c.state is CurrentAccountSignOutState.Removing)
        f.absent = true; f.removeCallback!!(true)
        assertTrue("a store that cannot be cleared fails cleanup", c.state is CurrentAccountSignOutState.CleanupFailed)
        assertFalse("the address books are not touched while the store could not be cleared", f.events.contains("children"))
        assertFalse("the active account is not reconciled while the store could not be cleared", f.events.any { it.startsWith("active:") })
        f.notes = true
        c.begin()
        assertTrue("the retry went on to the address books", c.state is CurrentAccountSignOutState.CleaningUp)
        f.childrenCallback!!(true)
        assertEquals("the retry asked for no second removal", 1, f.removeCalls)
        assertEquals("the retry asked for the store again", 2, f.events.count { it == "notes" })
        assertEquals("both tries asked for the snapshot's account", listOf(target, target), f.notesClearedFor)
        assertEquals("the cleanup retry did not ask the check again", 1, f.gateCalls)
        assertTrue(c.state is CurrentAccountSignOutState.Complete)
        // A store step that throws fails cleanup as well.
        val thrower = Fake().apply { absent = true; notesThrows = true }
        val t = CurrentAccountSignOutCoordinator(thrower)
        t.begin()
        assertTrue("a store step that throws fails cleanup", t.state is CurrentAccountSignOutState.CleanupFailed)
        assertFalse("and the address books are not touched", thrower.events.contains("children"))

        // A text check for the Android seam, which no unit test builds: its body holds the line that
        // answers no while the generation has its row, and it ends with the line that answers
        // whether the store's clearAll went through. BT runs the seam on a device.
        val clear = java.io.File("src/main/java/io/silentsuite/sync/ui/AndroidCurrentAccountSignOut.kt").readText().replace("\r\n", "\n")
            .substringAfter("override fun clearPendingNotes(main: ExactAccountIdentity): Boolean {", "").substringBefore("\n    }\n")
        assertTrue("the Android seam clears nothing while the generation has its row",
            clear.contains("if (row != null && manager.getUserData(row, AccountSettings.KEY_CREATION_ID) == main.creationId) return false"))
        assertTrue("and it answers whether the store's clearAll went through",
            clear.trimEnd().endsWith("return runCatching { pendingNotes(main).clearAll() }.isSuccess"))
    }

    @Test fun `the store of an account that is already gone is cleared only when the check answers yes, and a cleanup retry keeps that answer`() {
        // The check answers no: cleanup goes on and the store is not cleared.
        val kept = Fake().apply { absent = true; discardConfirmed = false }
        val c = CurrentAccountSignOutCoordinator(kept)
        c.begin()
        assertTrue("cleanup went on to the address books", c.state is CurrentAccountSignOutState.CleaningUp)
        kept.childrenCallback!!(true)
        assertTrue(c.state is CurrentAccountSignOutState.Complete)
        assertEquals(0, kept.removeCalls)
        assertFalse("the check said no, so the store is not cleared", kept.events.contains("notes"))
        assertEquals(listOf("cache", "status", "active:a@example.invalid", "children"), kept.events.takeLast(4))
        assertEquals("the check was asked once, about the snapshot's account", listOf(target), kept.gateAskedFor)
        // The check answers yes: the store is cleared after the status.
        val cleared = Fake().apply { absent = true }
        val d = CurrentAccountSignOutCoordinator(cleared)
        d.begin()
        assertTrue("cleanup went on to the address books", d.state is CurrentAccountSignOutState.CleaningUp)
        cleared.childrenCallback!!(true)
        assertTrue(d.state is CurrentAccountSignOutState.Complete)
        assertEquals(listOf("cache", "status", "notes", "active:a@example.invalid", "children"), cleared.events.takeLast(5))
        assertEquals(listOf(target), cleared.notesClearedFor)
        // The check throws: the store is not cleared either.
        val blind = Fake().apply { absent = true; gateThrows = true }
        val e = CurrentAccountSignOutCoordinator(blind)
        e.begin()
        assertTrue("cleanup went on to the address books", e.state is CurrentAccountSignOutState.CleaningUp)
        blind.childrenCallback!!(true)
        assertTrue(e.state is CurrentAccountSignOutState.Complete)
        assertFalse("the check threw, so the store is not cleared", blind.events.contains("notes"))
        // The gate let a removal start and the removal failed. By the next try the account is gone
        // and the check answers no: the yes of the earlier try does not clear the store.
        val later = Fake()
        val h = CurrentAccountSignOutCoordinator(later)
        h.begin()
        assertTrue("the gate let the removal start", h.state is CurrentAccountSignOutState.Removing)
        later.removeCallback!!(false)
        later.absent = true; later.discardConfirmed = false
        h.begin()
        assertTrue("cleanup went on to the address books", h.state is CurrentAccountSignOutState.CleaningUp)
        later.childrenCallback!!(true)
        assertTrue(h.state is CurrentAccountSignOutState.Complete)
        assertEquals(1, later.removeCalls)
        assertFalse("a yes for a try whose removal failed does not clear the store on a later try", later.events.contains("notes"))
        // The same when the check cannot run on that later try: the earlier yes does not stand in for it.
        val unsure = Fake()
        val u = CurrentAccountSignOutCoordinator(unsure)
        u.begin()
        assertTrue("the gate let the removal start", u.state is CurrentAccountSignOutState.Removing)
        unsure.removeCallback!!(false)
        unsure.absent = true; unsure.gateThrows = true
        u.begin()
        assertTrue("cleanup went on to the address books", u.state is CurrentAccountSignOutState.CleaningUp)
        unsure.childrenCallback!!(true)
        assertFalse("a yes for a try whose removal failed does not stand in for a check that cannot run on a later try", unsure.events.contains("notes"))
        // The same with a refused retry in between, which drops the snapshot. This fake still hands
        // out a snapshot for an account that is gone.
        val stale = Fake()
        val g = CurrentAccountSignOutCoordinator(stale)
        g.begin()
        assertTrue("the gate let the removal start", g.state is CurrentAccountSignOutState.Removing)
        stale.removeCallback!!(false)
        stale.discardConfirmed = false
        g.begin()
        assertEquals(CurrentAccountSignOutState.Refused(checkFailed = false), g.state)
        stale.absent = true
        g.begin()
        assertTrue("cleanup went on to the address books", g.state is CurrentAccountSignOutState.CleaningUp)
        stale.childrenCallback!!(true)
        assertTrue(g.state is CurrentAccountSignOutState.Complete)
        assertEquals(1, stale.removeCalls)
        assertEquals("the check was asked at each of the three tries", 3, stale.gateCalls)
        assertFalse("a yes from before a refused retry does not clear the store either", stale.events.contains("notes"))
        // A no is kept for a cleanup retry: the check is not asked again, whatever it would answer.
        val firm = Fake().apply { absent = true; discardConfirmed = false }
        val k = CurrentAccountSignOutCoordinator(firm)
        k.begin()
        assertTrue("cleanup went on to the address books", k.state is CurrentAccountSignOutState.CleaningUp)
        firm.childrenCallback!!(false)
        assertTrue(k.state is CurrentAccountSignOutState.CleanupFailed)
        firm.discardConfirmed = true
        k.begin()
        assertTrue("the cleanup retry went on to the address books", k.state is CurrentAccountSignOutState.CleaningUp)
        firm.childrenCallback!!(true)
        assertTrue(k.state is CurrentAccountSignOutState.Complete)
        assertEquals("the cleanup retry did not ask the check again", 1, firm.gateCalls)
        assertFalse("the no from the try that entered cleanup holds for the retry", firm.events.contains("notes"))
    }
}
