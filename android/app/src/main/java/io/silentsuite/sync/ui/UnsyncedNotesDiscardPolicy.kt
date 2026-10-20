package io.silentsuite.sync.ui

import io.silentsuite.sync.notes.edit.PendingNotesStore.DiscardOutcome
import io.silentsuite.sync.notes.edit.UnsyncedNotes
import io.silentsuite.sync.ui.UnsyncedNotesSignOutPolicy.Text

/**
 * What the Settings way out for unsynced Notes says and does next, decided from what the store answered. Pure, so the
 * rules can be tested without a screen.
 *
 * A store that listed is offered a discard ([Step.DISCARD]). A store that could not be listed is offered only an
 * acknowledgement ([Step.ACKNOWLEDGE]): the store deletes nothing under it and records it in place of a permit
 * (DiscardOutcome.Acknowledged), so nothing on its way is called a discard or says that anything was deleted. Once
 * the store lists, the user is asked again with the real count.
 */
internal object UnsyncedNotesDiscardPolicy {
    enum class Step { DISCARD, ACKNOWLEDGE }

    /** What the screen does after a confirmation, and what it says. */
    sealed class Result {
        /** The store's folder is gone. */
        object Done : Result()

        /** The store holds something the confirmation did not cover: the dialog shows again, for [now]. */
        data class Ask(val directory: String, val label: String?, val now: UnsyncedNotes) : Result()

        /** Nothing to say: the store holds nothing a count counts now, so the screen only reads the stores again. */
        object Reload : Result()

        /** A first discard that deleted nothing: the permit was not recorded, or it failed before the store deleted. */
        object NothingDeleted : Result()

        /** A discard that may have stopped partway; Retry sends the same confirmation again ([shown]). */
        data class NotFinished(val directory: String, val label: String?, val shown: UnsyncedNotes) : Result()

        /** An acknowledgement was recorded; nothing was deleted. */
        object Acknowledged : Result()

        /** An acknowledgement could not be recorded; nothing was deleted. */
        object AcknowledgementNotRecorded : Result()
    }

    /** A confirmation that threw: before the store deleted anything, or where is not known. */
    enum class Failure { BEFORE_DISCARD, UNKNOWN }

    /** The confirmation a store's answer [shown] gets, chosen before the dialog asks. */
    fun step(shown: UnsyncedNotes): Step = if (shown.listed) Step.DISCARD else Step.ACKNOWLEDGE

    /**
     * What a row says of its store's [answer]: that it could not be read when the read threw (null) or the store could
     * not be listed, else the words of [UnsyncedNotesSignOutPolicy.text], which never shows a count of zero.
     */
    fun rowText(answer: UnsyncedNotes?): Text = answer?.let(UnsyncedNotesSignOutPolicy::text) ?: Text.Unlisted

    /**
     * What the Settings hub row says of [listing]: null, which hides the row, while nothing was found; the sum while
     * every store was counted and no other file was left unread; otherwise that not all of them could be counted.
     */
    fun hubSummary(listing: UnsyncedNotesListing): Text? {
        val flagged = listing.othersUnlisted || listing.unknownOthers
        if (listing.rows.isEmpty() && !flagged) return null
        val counts = listing.rows.map { rowText(it.answer) }
        if (flagged || counts.any { it !is Text.Counted }) return Text.Uncounted
        return Text.Counted(counts.sumOf { (it as Text.Counted).count })
    }

    /**
     * What follows a confirmation of [shown] for the store in [directory] that the store answered with [ended]. [retry]
     * is a Retry after [Result.NotFinished]. [label] is the account the row named, or null for a store not matched to
     * one.
     *
     * After a discard, a store that can no longer be listed on a Retry is a discard that is not finished: its folder
     * may be gone while the folder above it cannot be listed to show that. A Retry that records no permit deleted
     * nothing itself, but the attempt before it may have, so it is not finished either and keeps Retry. After an
     * acknowledgement, the store gives neither Done nor PermitNotRecorded, since it deletes nothing under one, and
     * NotFinished only over a store whose discard stopped, recording nothing; the screen then only reloads.
     */
    fun result(shown: UnsyncedNotes, retry: Boolean, directory: String, label: String?, ended: DiscardOutcome): Result =
        when (step(shown)) {
            Step.DISCARD -> when (ended) {
                DiscardOutcome.Done -> Result.Done
                is DiscardOutcome.Changed -> when {
                    retry && !ended.now.listed -> Result.NotFinished(directory, label, shown)
                    ended.now.anything -> Result.Ask(directory, label, ended.now)
                    else -> Result.Reload
                }
                DiscardOutcome.PermitNotRecorded ->
                    if (retry) Result.NotFinished(directory, label, shown) else Result.NothingDeleted
                DiscardOutcome.NotFinished -> Result.NotFinished(directory, label, shown)
                DiscardOutcome.Acknowledged -> Result.Acknowledged
                DiscardOutcome.AcknowledgementNotRecorded -> Result.AcknowledgementNotRecorded
            }
            Step.ACKNOWLEDGE -> when (ended) {
                DiscardOutcome.Acknowledged -> Result.Acknowledged
                DiscardOutcome.AcknowledgementNotRecorded -> Result.AcknowledgementNotRecorded
                is DiscardOutcome.Changed ->
                    if (ended.now.anything) Result.Ask(directory, label, ended.now) else Result.Reload
                DiscardOutcome.Done, DiscardOutcome.NotFinished, DiscardOutcome.PermitNotRecorded -> Result.Reload
            }
        }

    /**
     * What follows a confirmation of [shown] that threw [failed], a Retry when [retry]. A discard that failed before
     * the store deleted anything deleted nothing, unless it is a Retry, whose attempt before may have: that is not
     * finished, as is one that failed where it is not known. Nothing on the way of an acknowledgement deletes Notes
     * files, so every failure there is one that recorded nothing.
     */
    fun result(shown: UnsyncedNotes, retry: Boolean, directory: String, label: String?, failed: Failure): Result =
        when (step(shown)) {
            Step.DISCARD -> when (failed) {
                Failure.BEFORE_DISCARD ->
                    if (retry) Result.NotFinished(directory, label, shown) else Result.NothingDeleted
                Failure.UNKNOWN -> Result.NotFinished(directory, label, shown)
            }
            Step.ACKNOWLEDGE -> Result.AcknowledgementNotRecorded
        }

    /** Only a discard that did not finish is offered Retry. */
    fun offersRetry(result: Result): Boolean = result is Result.NotFinished
}
