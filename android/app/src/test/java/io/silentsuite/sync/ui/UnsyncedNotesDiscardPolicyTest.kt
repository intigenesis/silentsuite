package io.silentsuite.sync.ui

import io.silentsuite.sync.notes.edit.PendingNotesStore.DiscardOutcome
import io.silentsuite.sync.notes.edit.UnsyncedNotes
import io.silentsuite.sync.ui.UnsyncedNotesDiscardPolicy.Failure
import io.silentsuite.sync.ui.UnsyncedNotesDiscardPolicy.Result
import io.silentsuite.sync.ui.UnsyncedNotesDiscardPolicy.Step
import io.silentsuite.sync.ui.UnsyncedNotesSignOutPolicy.Text
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class UnsyncedNotesDiscardPolicyTest {
    private val two = UnsyncedNotes(2, 7, false, names = setOf("a.note", "b.note"))
    private val uncounted = UnsyncedNotes(0, 7, unreadable = true)
    private val unlisted = UnsyncedNotes(0, 7, unreadable = true, listed = false)
    private val directory = "0123456789abcdef".repeat(4)

    private fun row(answer: UnsyncedNotes?, owner: ExactAccountIdentity? = null) =
        UnsyncedNotesRow(directory, owner, answer)

    private fun listing(vararg answers: UnsyncedNotes?, othersUnlisted: Boolean = false,
                        unknownOthers: Boolean = false) =
        UnsyncedNotesListing(answers.map { row(it) }, othersUnlisted, unknownOthers)

    private fun result(shown: UnsyncedNotes, ended: DiscardOutcome, retry: Boolean = false, label: String? = "alice") =
        UnsyncedNotesDiscardPolicy.result(shown, retry, directory, label, ended)

    private fun failed(shown: UnsyncedNotes, failure: Failure, retry: Boolean = false, label: String? = "alice") =
        UnsyncedNotesDiscardPolicy.result(shown, retry, directory, label, failure)

    @Test fun `the confirmation is a discard only for a store that listed`() {
        for (shown in listOf(two, uncounted, UnsyncedNotes.NOTHING, UnsyncedNotes(3, 9, unreadable = true))) {
            assertEquals("$shown", Step.DISCARD, UnsyncedNotesDiscardPolicy.step(shown))
        }
        val never = UnsyncedNotes(0, 0, unreadable = true, listed = false)
        for (shown in listOf(unlisted, two.copy(listed = false), never)) {
            assertEquals("$shown", Step.ACKNOWLEDGE, UnsyncedNotesDiscardPolicy.step(shown))
        }
    }

    @Test fun `a count is shown or said to be uncountable, never 0`() {
        // A row: the count, or that it could not be counted, or that the store could not be read at all.
        assertEquals(Text.Counted(2), UnsyncedNotesDiscardPolicy.rowText(two))
        assertEquals(Text.Counted(1), UnsyncedNotesDiscardPolicy.rowText(UnsyncedNotes(1, 3, false)))
        assertEquals(Text.Uncounted, UnsyncedNotesDiscardPolicy.rowText(uncounted))
        assertEquals("files counted by name are not a count of changes", Text.Uncounted,
            UnsyncedNotesDiscardPolicy.rowText(UnsyncedNotes(3, 9, unreadable = true)))
        assertEquals("never a count of zero", Text.Uncounted, UnsyncedNotesDiscardPolicy.rowText(UnsyncedNotes.NOTHING))
        assertEquals(Text.Unlisted, UnsyncedNotesDiscardPolicy.rowText(unlisted))
        assertEquals("whatever else an answer of a store that could not be listed holds", Text.Unlisted,
            UnsyncedNotesDiscardPolicy.rowText(two.copy(listed = false)))
        assertEquals("a read that threw", Text.Unlisted, UnsyncedNotesDiscardPolicy.rowText(null))

        // The dialog of a discard takes its words from the same rule, so it never says 0 either.
        for (shown in listOf(two, uncounted, UnsyncedNotes.NOTHING, UnsyncedNotes(0, 0, unreadable = true))) {
            val text = UnsyncedNotesSignOutPolicy.text(shown)
            assertFalse("$shown", text is Text.Counted && text.count <= 0)
            assertEquals("$shown", text, UnsyncedNotesDiscardPolicy.rowText(shown))
        }

        // The hub: hidden while nothing is found, a sum only while every store was counted, else that not all of
        // them could be counted.
        assertNull(UnsyncedNotesDiscardPolicy.hubSummary(listing()))
        assertEquals(Text.Counted(3), UnsyncedNotesDiscardPolicy.hubSummary(listing(two, UnsyncedNotes(1, 3, false))))
        for (mixed in listOf(
            listing(two, unlisted), listing(two, uncounted), listing(two, null),
            listing(two, UnsyncedNotes(0, 0, unreadable = true)), listing(unlisted), listing(null),
            listing(two, othersUnlisted = true), listing(two, unknownOthers = true),
            listing(othersUnlisted = true), listing(unknownOthers = true),
        )) {
            assertEquals("$mixed", Text.Uncounted, UnsyncedNotesDiscardPolicy.hubSummary(mixed))
        }
    }

    @Test fun `every outcome maps by the step that was confirmed`() {
        val more = UnsyncedNotes(3, 8, false, names = two.names + "c.note")
        val nowUnlisted = UnsyncedNotes(0, 8, unreadable = true, listed = false)
        for (retry in listOf(false, true)) {
            // A discard, confirmed for a store that listed.
            for (shown in listOf(two, uncounted)) {
                assertEquals(Result.Done, result(shown, DiscardOutcome.Done, retry))
                assertEquals("the dialog shows again for what is there now", Result.Ask(directory, "alice", more),
                    result(shown, DiscardOutcome.Changed(more), retry))
                assertEquals("nothing is there now", Result.Reload,
                    result(shown, DiscardOutcome.Changed(UnsyncedNotes.NOTHING), retry))
                val noLongerListed = if (retry) Result.NotFinished(directory, "alice", shown)
                else Result.Ask(directory, "alice", nowUnlisted)
                assertEquals("a store that can no longer be listed", noLongerListed,
                    result(shown, DiscardOutcome.Changed(nowUnlisted), retry))
                // A Retry that records no permit, or fails before the store deletes, deleted nothing itself, but the
                // attempt before it may have: it is not finished either, and keeps Retry.
                val nothingNow = if (retry) Result.NotFinished(directory, "alice", shown) else Result.NothingDeleted
                assertEquals(nothingNow, result(shown, DiscardOutcome.PermitNotRecorded, retry))
                assertEquals(Result.NotFinished(directory, "alice", shown),
                    result(shown, DiscardOutcome.NotFinished, retry))
                assertEquals(Result.Acknowledged, result(shown, DiscardOutcome.Acknowledged, retry))
                assertEquals(Result.AcknowledgementNotRecorded,
                    result(shown, DiscardOutcome.AcknowledgementNotRecorded, retry))
                assertEquals(nothingNow, failed(shown, Failure.BEFORE_DISCARD, retry))
                assertEquals("where it stopped is unknown", Result.NotFinished(directory, "alice", shown),
                    failed(shown, Failure.UNKNOWN, retry))
                assertEquals("a store not matched to an account keeps that", Result.NotFinished(directory, null, shown),
                    failed(shown, Failure.UNKNOWN, retry, label = null))
            }

            // An acknowledgement, confirmed for a store that could not be listed: nothing on its way says discard,
            // deleted or not finished.
            assertEquals(Result.Acknowledged, result(unlisted, DiscardOutcome.Acknowledged, retry))
            assertEquals(Result.AcknowledgementNotRecorded,
                result(unlisted, DiscardOutcome.AcknowledgementNotRecorded, retry))
            assertEquals("once it lists, a fresh discard with the real count", Result.Ask(directory, "alice", more),
                result(unlisted, DiscardOutcome.Changed(more), retry))
            assertEquals(Result.Ask(directory, "alice", nowUnlisted),
                result(unlisted, DiscardOutcome.Changed(nowUnlisted), retry))
            assertEquals(Result.Reload, result(unlisted, DiscardOutcome.Changed(UnsyncedNotes.NOTHING), retry))
            for (failure in Failure.values()) {
                assertEquals("$failure", Result.AcknowledgementNotRecorded, failed(unlisted, failure, retry))
            }
            // After an acknowledgement: no Done or PermitNotRecorded, and NotFinished only after a stop; each reloads.
            for (outcome in listOf(DiscardOutcome.Done, DiscardOutcome.NotFinished, DiscardOutcome.PermitNotRecorded)) {
                assertEquals("$outcome", Result.Reload, result(unlisted, outcome, retry))
            }
        }
    }

    @Test fun `only a discard that did not finish offers Retry`() {
        val all = listOf(
            Result.Done, Result.Ask(directory, null, two), Result.Reload, Result.NothingDeleted,
            Result.NotFinished(directory, null, two), Result.Acknowledged, Result.AcknowledgementNotRecorded,
        )
        assertEquals(listOf<Result>(Result.NotFinished(directory, null, two)),
            all.filter(UnsyncedNotesDiscardPolicy::offersRetry))
        assertTrue(UnsyncedNotesDiscardPolicy.offersRetry(Result.NotFinished(directory, "alice", uncounted)))
    }
}
