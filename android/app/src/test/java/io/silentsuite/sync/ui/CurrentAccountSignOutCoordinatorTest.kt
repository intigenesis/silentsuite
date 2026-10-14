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
        // The Notes seams record no event, so the event lists of the older tests stand.
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
        f.absent = true; f.removeCallback!!(true); f.childrenCallback!!(true)
        assertEquals(listOf("cache", "status", "active:a@example.invalid", "children"), f.events.takeLast(4))
        assertEquals(CurrentAccountSignOutState.Complete(sibling), c.state)
        assertEquals(1, f.closeCalls)
    }

    @Test fun `false callback and present row preserve every destructive seam and expose one failure`() {
        val f = Fake(); val states = mutableListOf<CurrentAccountSignOutState>()
        val c = CurrentAccountSignOutCoordinator(f) { states += it }
        c.begin(); f.removeCallback!!(false)
        assertTrue(c.state is CurrentAccountSignOutState.RemovalFailed)
        assertFalse(f.events.any { it in setOf("cache", "status", "children") || it.startsWith("active:") })
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
        listOf("cache", "status", "active").forEach { failed ->
            val f = Fake().apply {
                absent = true; cache = failed != "cache"; status = failed != "status"; active = failed != "active"
            }
            val c = CurrentAccountSignOutCoordinator(f); c.begin()
            assertTrue(c.state is CurrentAccountSignOutState.CleanupFailed)
            assertFalse(f.events.contains("children")); assertEquals(0, f.removeCalls)
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
}
