package io.silentsuite.sync.ui

import android.app.Dialog
import android.content.DialogInterface
import android.os.Bundle
import androidx.fragment.app.DialogFragment
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import io.silentsuite.sync.R
import io.silentsuite.sync.notes.edit.UnsyncedNotes

/**
 * The two dialogs the account screen shows before a sign-out while Notes changes have not synced.
 * Step 1 shows how many changes have not synced, or that they could not be counted, with Sync now,
 * Cancel and a third action to go on without syncing. Step 2 asks for the separate confirmation of
 * the discard. The dialog keeps the exact account generation and what it showed in its arguments
 * and sends both with every answer, so the screen can turn away an answer for another account. It
 * decides nothing itself: an answer goes to the account screen that hosts it, and so do Cancel and
 * a canceled dialog ([onCancel]).
 */
class UnsyncedNotesDialogFragment : DialogFragment() {
    override fun onCreateDialog(savedInstanceState: Bundle?): Dialog {
        val args = requireArguments()
        val identity = ExactAccountIdentity(args.getString(ARG_TYPE)!!, args.getString(ARG_NAME)!!, args.getString(ARG_CREATION_ID)!!)
        val shown = UnsyncedNotes(args.getInt(ARG_COUNT), args.getLong(ARG_SEQUENCE), args.getBoolean(ARG_UNREADABLE))
        fun send(answer: UnsyncedNotesSignOutPolicy.Answer) {
            (activity as? AccountActivity)?.onUnsyncedNotesAnswer(identity, answer, shown)
        }
        fun closedWithoutSignOut() {
            (activity as? AccountActivity)?.onSignOutDialogClosedWithoutSignOut()
        }
        val builder = MaterialAlertDialogBuilder(requireContext())
        return when (UnsyncedNotesSignOutPolicy.Step.valueOf(args.getString(ARG_STEP)!!)) {
            UnsyncedNotesSignOutPolicy.Step.COUNT -> builder
                .setTitle(R.string.notes_sign_out_unsynced_title)
                .setMessage(if (shown.unreadable) getString(R.string.notes_sign_out_unsynced_unreadable)
                    else resources.getQuantityString(R.plurals.notes_sign_out_unsynced, shown.count, shown.count))
                .setNegativeButton(android.R.string.cancel) { _, _ -> closedWithoutSignOut() }
                .setNeutralButton(R.string.notes_sign_out_without_syncing) { _, _ ->
                    send(UnsyncedNotesSignOutPolicy.Answer.GO_ON_WITHOUT_SYNCING)
                }
                .setPositiveButton(R.string.account_synchronize_now) { _, _ ->
                    send(UnsyncedNotesSignOutPolicy.Answer.SYNC_NOW)
                }
                .create()
            UnsyncedNotesSignOutPolicy.Step.DISCARD -> builder
                .setTitle(R.string.notes_discard_title)
                .setMessage(if (shown.unreadable) getString(R.string.notes_discard_unreadable)
                    else resources.getQuantityString(R.plurals.notes_discard_text, shown.count, shown.count))
                .setNegativeButton(android.R.string.cancel) { _, _ -> closedWithoutSignOut() }
                .setPositiveButton(R.string.notes_discard_and_sign_out) { _, _ ->
                    send(UnsyncedNotesSignOutPolicy.Answer.DISCARD)
                }
                .create()
        }
    }

    /** The dialog was canceled: it ends without an answer, and the account screen that hosts it is told. The platform cancels a dialog for Back and for a tap outside it. */
    override fun onCancel(dialog: DialogInterface) {
        super.onCancel(dialog)
        (activity as? AccountActivity)?.onSignOutDialogClosedWithoutSignOut()
    }

    companion object {
        private const val ARG_TYPE = "unsyncedNotes.type"
        private const val ARG_NAME = "unsyncedNotes.name"
        private const val ARG_CREATION_ID = "unsyncedNotes.creationId"
        private const val ARG_COUNT = "unsyncedNotes.count"
        private const val ARG_SEQUENCE = "unsyncedNotes.sequence"
        private const val ARG_UNREADABLE = "unsyncedNotes.unreadable"
        private const val ARG_STEP = "unsyncedNotes.step"

        /** One tag per step. The screen looks the tag up before it shows a step and shows none while it finds one. */
        fun tag(step: UnsyncedNotesSignOutPolicy.Step) = "unsynced-notes-" + step.name

        fun newInstance(identity: ExactAccountIdentity, step: UnsyncedNotesSignOutPolicy.Step, shown: UnsyncedNotes) =
            UnsyncedNotesDialogFragment().apply {
                arguments = Bundle(7).apply {
                    putString(ARG_TYPE, identity.type)
                    putString(ARG_NAME, identity.name)
                    putString(ARG_CREATION_ID, identity.creationId)
                    putInt(ARG_COUNT, shown.count)
                    putLong(ARG_SEQUENCE, shown.sequence)
                    putBoolean(ARG_UNREADABLE, shown.unreadable)
                    putString(ARG_STEP, step.name)
                }
            }
    }
}
