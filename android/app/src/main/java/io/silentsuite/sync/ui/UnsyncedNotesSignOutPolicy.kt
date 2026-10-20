package io.silentsuite.sync.ui

import io.silentsuite.sync.notes.edit.UnsyncedNotes

/**
 * What the sign-out screen does next, decided from the answer to a dialog and from what the caller
 * passes as the store's answer at that moment. Pure, so the rule can be tested without a screen: of
 * the three answers only DISCARD, the answer to the separate discard confirmation, gives
 * [Next.ConfirmAndSignOut].
 */
internal object UnsyncedNotesSignOutPolicy {
    enum class Step { COUNT, DISCARD }
    enum class Answer { SYNC_NOW, GO_ON_WITHOUT_SYNCING, DISCARD }

    sealed class Next {
        /** Nothing is unsynced: the plain sign-out question, with the words it had before. */
        object AskPlain : Next()
        /** Step 1: the count, with Sync now, Cancel, and going on without syncing. */
        data class ShowCount(val unsynced: UnsyncedNotes) : Next()
        /** Step 2: the separate discard confirmation, shown for [unsynced]. */
        data class ShowDiscard(val unsynced: UnsyncedNotes) : Next()
        object SyncNotes : Next()
        /** The DISCARD answer was accepted. [shown] is what its dialog showed. */
        data class ConfirmAndSignOut(val shown: UnsyncedNotes) : Next()
    }

    /** What a dialog says about the changes it was shown for. */
    sealed class Text {
        /** How many changes have not synced. */
        data class Counted(val count: Int) : Text()
        /** That the changes could not be counted. */
        object Uncounted : Text()
    }

    /** The user asked to sign out: the count when [now] holds anything, else the plain question. */
    fun start(now: UnsyncedNotes): Next = if (now.anything) Next.ShowCount(now) else Next.AskPlain

    /**
     * The user answered [answer] in a dialog that showed [shown]. [now] is what the caller passes as
     * the store's answer at this moment. SYNC_NOW gives [Next.SyncNotes]. GO_ON_WITHOUT_SYNCING gives
     * step 2 for [now], or the plain question when [now] holds nothing. DISCARD is accepted only when
     * [now] holds nothing, or when [UnsyncedNotes.covers] says that a confirmation for [shown] still
     * holds for [now]; see its limits there. Otherwise the count is shown again, for [now].
     */
    fun answered(answer: Answer, shown: UnsyncedNotes, now: UnsyncedNotes): Next = when (answer) {
        Answer.SYNC_NOW -> Next.SyncNotes
        Answer.GO_ON_WITHOUT_SYNCING -> if (now.anything) Next.ShowDiscard(now) else Next.AskPlain
        Answer.DISCARD -> if (!now.anything || shown.covers(now)) Next.ConfirmAndSignOut(shown) else Next.ShowCount(now)
    }

    /**
     * What the dialogs say for [shown]: the number of changes while they were counted, else that they could
     * not be counted. A count of zero is never shown: the dialogs are asked only for an answer that holds
     * anything, and one that holds no counted change could not be counted.
     */
    fun text(shown: UnsyncedNotes): Text =
        if (shown.unreadable || shown.count <= 0) Text.Uncounted else Text.Counted(shown.count)
}
