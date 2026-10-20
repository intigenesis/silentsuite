package io.silentsuite.sync.notes.edit

/**
 * What a pending store holds that a removal of its account would lose. In a counted answer with a
 * count above zero, [sequence] is the last version the store handed out. Every save, rebase, release
 * from holding and conflict copy moves it on, so two such answers with the same sequence are about
 * the same text or less of it, if the counter file read the whole time in between. An answer that
 * could not be counted ([unreadable]) carries the sequence too whenever it can be read, as a new
 * version reads it, so a save moves it on there as well. It carries 0 only while the sequence cannot
 * be read, and then no new version can be handed out. An answer of nothing carries sequence 0, which
 * says nothing about the text.
 */
internal data class UnsyncedNotes(val count: Int, val sequence: Long, val unreadable: Boolean) {
    /** Something would be lost, or that cannot be ruled out. */
    val anything: Boolean get() = count > 0 || unreadable

    /**
     * Whether a confirmation given for this still holds for [now]. A confirmation for a counted
     * answer holds for a counted answer with the same sequence and no more changes than were shown.
     * The same sequence means that nothing was saved since only if the counter file read the whole
     * time in between. An answer of nothing carries sequence 0, so a confirmation for a higher
     * sequence does not cover it: ask [anything] first. A confirmation for an answer that could not
     * be counted holds only for an answer that again could not be counted, with the same sequence and
     * no higher number of files than was shown. Its sequence moves on with every save, as a counted
     * one does. Between two that carry 0 only the number of files is compared, which cannot tell
     * whether text changed or one note took the place of another; while the sequence cannot be read,
     * though, no new version can be handed out.
     */
    fun covers(now: UnsyncedNotes): Boolean =
        sequence == now.sequence && unreadable == now.unreadable && now.count <= count

    companion object {
        val NOTHING = UnsyncedNotes(0, 0, false)
    }
}
