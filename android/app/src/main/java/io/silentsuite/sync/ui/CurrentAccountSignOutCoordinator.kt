package io.silentsuite.sync.ui

import io.silentsuite.sync.notes.edit.UnsyncedNotes

/** Non-secret identity used to keep account destructive work exact and framework-independent. */
data class ExactAccountIdentity(val type: String, val name: String, val creationId: String)

data class CurrentAccountSignOutSnapshot(
    val main: ExactAccountIdentity,
    val children: List<Pair<String, String>>,
    val eligibleSiblings: List<ExactAccountIdentity>
)

data class ActiveAccountReconciliation(val succeeded: Boolean, val active: ExactAccountIdentity?)

sealed class CurrentAccountSignOutState {
    object Idle : CurrentAccountSignOutState()
    object Removing : CurrentAccountSignOutState()
    object CleaningUp : CurrentAccountSignOutState()
    data class RemovalFailed(val errorId: Long) : CurrentAccountSignOutState()
    /**
     * The sign-out did not start: the seam's check
     * ([CurrentAccountSignOutCoordinator.Seams.notesDiscardConfirmed]) answered no, or it threw
     * ([checkFailed]). This attempt canceled nothing and removed nothing. It is not a failure, and
     * the view model does not count it as a started sign-out.
     */
    data class Refused(val checkFailed: Boolean) : CurrentAccountSignOutState()
    data class CleanupFailed(val errorId: Long) : CurrentAccountSignOutState()
    data class Complete(val replacement: ExactAccountIdentity?) : CurrentAccountSignOutState()
}

/**
 * Ordered sign-out policy. The snapshot is retained for cleanup-only retry after the main row is
 * absent; a retry can therefore never broaden ownership or issue the main removal twice. A try
 * that the Notes gate refuses keeps no snapshot, and the next try has to take a new one.
 */
class CurrentAccountSignOutCoordinator(
    private val seams: Seams,
    private val onStateChanged: (CurrentAccountSignOutState) -> Unit = {}
) {
    interface Seams {
        fun snapshot(): CurrentAccountSignOutSnapshot?
        fun cancelSync(identity: Pair<String, String>)
        fun removeMain(main: ExactAccountIdentity, callback: (Boolean) -> Unit)
        fun mainGenerationAbsent(main: ExactAccountIdentity): Boolean
        fun clearCache(main: ExactAccountIdentity): Boolean
        fun clearStatus(main: ExactAccountIdentity): Boolean
        fun reconcileActive(main: ExactAccountIdentity, replacement: ExactAccountIdentity?): ActiveAccountReconciliation
        fun removeAndVerifyChildren(snapshot: CurrentAccountSignOutSnapshot, callback: (Boolean) -> Unit)
        /** What the pending Notes store of [main] holds that has not synced, which a removal of the account would lose. */
        fun unsyncedNotes(main: ExactAccountIdentity): UnsyncedNotes
        /** Called for the user's separate confirmation that the unsynced Notes text of [main] may be discarded. [shown] is the answer it was given for. */
        fun confirmNotesDiscard(main: ExactAccountIdentity, shown: UnsyncedNotes)
        /**
         * Ends that confirmation. The coordinator asks for it at each end of an attempt (a refusal,
         * a failed removal, a failed cleanup, completion), for the account a confirmation was last
         * given for and for the snapshot's account. No end follows completion, so a confirmation
         * passed on after it is not withdrawn.
         */
        fun withdrawNotesDiscard(main: ExactAccountIdentity)
        /**
         * Whether [main] may be removed as far as its unsynced Notes text goes: there is none, or
         * the separate discard confirmation still holds for what is there now ([UnsyncedNotes.covers],
         * with its limits). The coordinator asks it in [begin], after the branches for a cleanup-only
         * retry and for an account that is already gone, and before it cancels or removes anything.
         */
        fun notesDiscardConfirmed(main: ExactAccountIdentity): Boolean
        /** Monotonic platform-listener observation; production defaults to no invalidation. */
        fun hasObservedMainGenerationInvalidation(): Boolean = false
        fun close() = Unit
    }

    var state: CurrentAccountSignOutState = CurrentAccountSignOutState.Idle
        private set
    private var snapshot: CurrentAccountSignOutSnapshot? = null
    private var mainRemovalConfirmed = false
    private var nextErrorId = 1L

    /**
     * Who the separate discard confirmation was last given for since the last withdrawal, if anyone.
     * Only the last one is kept. Kept apart from the snapshot: an attempt that cannot take its
     * snapshot fails without one, and the withdrawal is asked for this account as well.
     */
    private var discardConfirmedFor: ExactAccountIdentity? = null

    init { onStateChanged(state) }

    fun begin() {
        if (state is CurrentAccountSignOutState.Removing ||
            state is CurrentAccountSignOutState.CleaningUp ||
            state is CurrentAccountSignOutState.Complete) return

        val captured = snapshot ?: runCatching { seams.snapshot() }.getOrNull()?.also { snapshot = it }
        if (captured == null) {
            failRemoval()
            return
        }
        if (mainRemovalConfirmed || state is CurrentAccountSignOutState.CleanupFailed) {
            cleanup(captured)
            return
        }
        if (runCatching { seams.mainGenerationAbsent(captured.main) }.getOrDefault(false)) {
            mainRemovalConfirmed = true
            cleanup(captured)
            return
        }

        // The gate. The removal starts only when the seam's check answers yes, which by its contract
        // means that the account's unsynced Notes text is nothing or that a discard confirmation
        // still holds for it. A check that throws refuses as well, so the gate fails closed. It
        // stands before anything is canceled, so a refused sign-out stops no sync, and after the two
        // branches above, which do not remove the main account.
        val allowed = runCatching { seams.notesDiscardConfirmed(captured.main) }
        if (!allowed.getOrDefault(false)) {
            // This try started nothing, so nothing is kept for the next one: the seam is asked to
            // withdraw the confirmation and the snapshot is dropped, also one kept from an earlier
            // try whose removal failed. The next try has to take a new one.
            withdrawNotesDiscard()
            snapshot = null
            transition(CurrentAccountSignOutState.Refused(checkFailed = allowed.isFailure))
            return
        }

        transition(CurrentAccountSignOutState.Removing)
        try {
            seams.cancelSync(captured.main.type to captured.main.name)
            captured.children.forEach(seams::cancelSync)
            seams.removeMain(captured.main) { confirmed ->
                if (state !is CurrentAccountSignOutState.Removing) return@removeMain
                if (confirmed && runCatching { seams.mainGenerationAbsent(captured.main) }.getOrDefault(false)) {
                    mainRemovalConfirmed = true
                    cleanup(captured)
                } else failRemoval()
            }
        } catch (_: Exception) {
            failRemoval()
        }
    }

    private fun cleanup(captured: CurrentAccountSignOutSnapshot) {
        transition(CurrentAccountSignOutState.CleaningUp)
        val replacement = AccountSwitcherPolicy.replacement(captured.main, captured.eligibleSiblings)
        val essentialCleanup = runCatching {
            if (!seams.clearCache(captured.main) || !seams.clearStatus(captured.main)) null
            else seams.reconcileActive(captured.main, replacement)
        }.getOrNull()
        if (essentialCleanup?.succeeded != true) {
            failCleanup()
            return
        }
        try {
            seams.removeAndVerifyChildren(captured) { success ->
                if (state !is CurrentAccountSignOutState.CleaningUp) return@removeAndVerifyChildren
                if (success) {
                    transition(CurrentAccountSignOutState.Complete(essentialCleanup.active))
                    close()
                } else failCleanup()
            }
        } catch (_: Exception) {
            failCleanup()
        }
    }

    private fun failRemoval() = transition(CurrentAccountSignOutState.RemovalFailed(nextErrorId++))
    private fun failCleanup() = transition(CurrentAccountSignOutState.CleanupFailed(nextErrorId++))
    private fun transition(next: CurrentAccountSignOutState) {
        state = next
        // At each of these three ends of an attempt, withdrawNotesDiscard() asks the seam to end the
        // separate discard confirmation of unsynced Notes text, for the account it was given for and
        // for the snapshot's account, so that none is left standing for a later attempt. That call
        // comes before the state is reported, so a listener hears of the end after it.
        if (next is CurrentAccountSignOutState.RemovalFailed || next is CurrentAccountSignOutState.CleanupFailed ||
            next is CurrentAccountSignOutState.Complete) withdrawNotesDiscard()
        onStateChanged(next)
    }
    fun close() { runCatching { seams.close() } }

    fun isMainGenerationPresent(main: ExactAccountIdentity) =
        !runCatching { seams.mainGenerationAbsent(main) }.getOrDefault(true)

    /** The seam's answer. When the seam throws, the answer is that the changes could not be counted, never nothing. */
    fun unsyncedNotes(main: ExactAccountIdentity): UnsyncedNotes =
        runCatching { seams.unsyncedNotes(main) }.getOrDefault(UnsyncedNotes(0, 0, unreadable = true))

    fun confirmNotesDiscard(main: ExactAccountIdentity, shown: UnsyncedNotes) {
        discardConfirmedFor = main
        runCatching { seams.confirmNotesDiscard(main, shown) }
    }

    /**
     * Takes a refusal back to idle and reports that. In any other state it does nothing. Made for
     * the screen to call when it has taken note of a refusal.
     */
    fun acknowledgeRefusal() {
        if (state is CurrentAccountSignOutState.Refused) transition(CurrentAccountSignOutState.Idle)
    }

    private fun withdrawNotesDiscard() {
        val given = setOfNotNull(discardConfirmedFor, snapshot?.main)
        discardConfirmedFor = null
        given.forEach { runCatching { seams.withdrawNotesDiscard(it) } }
    }

    fun hasObservedMainGenerationInvalidation() =
        runCatching { seams.hasObservedMainGenerationInvalidation() }.getOrDefault(true)
}

object AccountSwitcherPolicy {
    fun ordered(accounts: List<ExactAccountIdentity>): List<ExactAccountIdentity> =
        accounts.distinct().sortedWith(compareBy<ExactAccountIdentity>({ it.name }, { it.creationId }, { it.type }))

    fun replacement(removed: ExactAccountIdentity, eligible: List<ExactAccountIdentity>): ExactAccountIdentity? =
        ordered(eligible).firstOrNull { it != removed }

    fun canExpand(accountCount: Int) = accountCount >= 1
}

sealed class ActiveAccountReplacementDecision {
    object Preserve : ActiveAccountReplacementDecision()
    data class Replace(val identity: ExactAccountIdentity?) : ActiveAccountReplacementDecision()
}

object ActiveAccountReplacementPolicy {
    fun decide(savedName: String?, savedGeneration: String?, expected: ExactAccountIdentity,
               replacement: ExactAccountIdentity?): ActiveAccountReplacementDecision =
        if (savedName == expected.name && savedGeneration == expected.creationId)
            ActiveAccountReplacementDecision.Replace(replacement)
        else ActiveAccountReplacementDecision.Preserve
}
