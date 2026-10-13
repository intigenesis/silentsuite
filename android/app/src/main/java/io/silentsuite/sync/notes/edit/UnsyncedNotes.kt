package io.silentsuite.sync.notes.edit

/**
 * What a pending store holds that a removal of its account would lose. In a counted answer with a
 * count above zero, [sequence] is the last version the store handed out. Every save, rebase, release
 * from holding and conflict copy moves it on, so two such answers with the same sequence are about
 * the same text or less of it, while the counter file reads. An answer of nothing and an answer
 * that could not be counted ([unreadable]) carry sequence 0, which says nothing about the text.
 */
data class UnsyncedNotes(val count: Int, val sequence: Long, val unreadable: Boolean) {
    /** Something would be lost, or that cannot be ruled out. */
    val anything: Boolean get() = count > 0 || unreadable

    companion object {
        val NOTHING = UnsyncedNotes(0, 0, false)
    }
}
