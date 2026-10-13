package io.silentsuite.sync.notes.edit

import io.silentsuite.sync.ui.ExactAccountIdentity

/**
 * The user's separate confirmation that the unsynced Notes text of one exact account generation will
 * be discarded, kept as data so that the code that removes the account can check it. It lives in
 * memory only, in the app's process: if the process dies between the confirmation and the removal,
 * the permit is gone and covers nothing. In a living process it stays until it is withdrawn.
 *
 * A permit is valid for the same generation and for what the user saw ([UnsyncedNotes.covers]). A
 * permit for a counted store holds for a counted answer with the same sequence and no more changes
 * than were shown; the same sequence means that nothing was saved since only if the counter file
 * read the whole time in between. An answer of nothing carries sequence 0, so a permit for a higher
 * sequence does not cover it: a caller has to ask [UnsyncedNotes.anything] first. A permit given
 * for a store that could not be counted holds only for an answer that again could not be counted,
 * with no higher number of files than was shown; it cannot tell whether text changed or one note
 * took the place of another in between.
 *
 * Nothing is called while its own small lock is held. A caller has to ask the store first and pass
 * the answer in; nothing here checks that.
 */
internal object PendingNotesDiscardPermit {
    private val lock = Any()
    private val exact = HashMap<ExactAccountIdentity, UnsyncedNotes>()

    /**
     * For the second confirmation only, with exactly what that dialog showed. A second grant for
     * the same identity replaces the first.
     */
    fun grant(identity: ExactAccountIdentity, shown: UnsyncedNotes) {
        synchronized(lock) { exact[identity] = shown }
    }

    /** Whether a confirmation exists for [identity] that still holds for what its store holds [now]. Asking does not use it up. */
    fun covers(identity: ExactAccountIdentity, now: UnsyncedNotes): Boolean = synchronized(lock) {
        exact[identity]?.covers(now) == true
    }

    /** Ends the confirmation for [identity] and for no other. Without one, nothing happens. */
    fun withdraw(identity: ExactAccountIdentity) {
        synchronized(lock) { exact.remove(identity) }
    }

    internal fun resetForTesting() {
        synchronized(lock) { exact.clear() }
    }
}
