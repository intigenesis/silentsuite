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
 *
 * [listed] is false only when the store's directory could not be listed and is not shown gone: nothing was
 * counted, and a confirmation of such an answer is only an acknowledgement, which deletes nothing
 * ([PendingNotesStore.DiscardOutcome.Acknowledged]). A caller chooses from it, before it asks, between the
 * wording of a discard and that of an acknowledgement.
 *
 * [names] are what the answer was counted from that may hold unsynced text: each note it counted by its entry
 * file's name ("<uid>.note", which a first write takes when recovery commits it), and in an answer that could
 * not be counted, each entry file and first write by that name and each file of a kind this build does not
 * know by its own. A listing can come back short, so a count can be low (see [PendingNotesStore.unsynced]);
 * a confirmation covers only what it names all the same.
 */
internal data class UnsyncedNotes(
    val count: Int,
    val sequence: Long,
    val unreadable: Boolean,
    val listed: Boolean = true,
    val names: Set<String> = emptySet(),
) {
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
     * though, no new version can be handed out. A recorded permit does not rest on this comparison
     * alone: a store removes its permit before it hands out a new version ([PendingNotesDiscardPermits]).
     *
     * Either way, [now] must name nothing that this does not ([names]), so what a short listing missed is not
     * covered once a later listing shows it. And an answer from a store that could not be listed covers only
     * another such answer, while no answer from a listed store covers one ([listed]): a confirmation given as
     * an acknowledgement never becomes one for a discard, nor the other way round.
     */
    fun covers(now: UnsyncedNotes): Boolean = sequence == now.sequence && unreadable == now.unreadable &&
        listed == now.listed && now.count <= count && names.containsAll(now.names)

    companion object {
        val NOTHING = UnsyncedNotes(0, 0, false)
    }
}
