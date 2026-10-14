package io.silentsuite.sync.ui

import io.silentsuite.sync.notes.edit.UnsyncedNotes
import io.silentsuite.sync.ui.UnsyncedNotesSignOutPolicy.Answer
import io.silentsuite.sync.ui.UnsyncedNotesSignOutPolicy.Next
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class UnsyncedNotesSignOutPolicyTest {
    private val two = UnsyncedNotes(2, 7, false)
    private val uncounted = UnsyncedNotes(0, 0, unreadable = true)

    private fun source(name: String) = File("src/main/java/io/silentsuite/sync/ui/$name").readText().replace("\r\n", "\n")

    @Test fun `with nothing unsynced the plain question is asked, as before`() {
        assertEquals(Next.AskPlain, UnsyncedNotesSignOutPolicy.start(UnsyncedNotes.NOTHING))
    }

    @Test fun `with something unsynced the count comes first`() {
        assertEquals(Next.ShowCount(two), UnsyncedNotesSignOutPolicy.start(two))
        assertEquals("changes that could not be counted are something", Next.ShowCount(uncounted),
            UnsyncedNotesSignOutPolicy.start(uncounted))
    }

    @Test fun `sync now asks for a Notes sync and confirms nothing`() {
        assertEquals(Next.SyncNotes, UnsyncedNotesSignOutPolicy.answered(Answer.SYNC_NOW, two, two))
    }

    @Test fun `going on without syncing leads to the separate confirmation for what the store holds then`() {
        assertEquals(Next.ShowDiscard(two), UnsyncedNotesSignOutPolicy.answered(Answer.GO_ON_WITHOUT_SYNCING, two, two))
        val more = UnsyncedNotes(3, 8, false)
        assertEquals("counted again: step 2 shows what the store holds then", Next.ShowDiscard(more),
            UnsyncedNotesSignOutPolicy.answered(Answer.GO_ON_WITHOUT_SYNCING, two, more))
        assertEquals("everything synced meanwhile", Next.AskPlain,
            UnsyncedNotesSignOutPolicy.answered(Answer.GO_ON_WITHOUT_SYNCING, two, UnsyncedNotes.NOTHING))
        assertEquals("a store that could not be counted gets the separate confirmation too", Next.ShowDiscard(uncounted),
            UnsyncedNotesSignOutPolicy.answered(Answer.GO_ON_WITHOUT_SYNCING, uncounted, uncounted))
    }

    @Test fun `only the discard answer confirms, and only for what its dialog showed`() {
        assertEquals(Next.ConfirmAndSignOut(two), UnsyncedNotesSignOutPolicy.answered(Answer.DISCARD, two, two))
        assertEquals("a change that was pushed meanwhile does not ask again", Next.ConfirmAndSignOut(two),
            UnsyncedNotesSignOutPolicy.answered(Answer.DISCARD, two, UnsyncedNotes(1, 7, false)))
        assertEquals("nor does everything having synced", Next.ConfirmAndSignOut(two),
            UnsyncedNotesSignOutPolicy.answered(Answer.DISCARD, two, UnsyncedNotes.NOTHING))
        assertEquals("the discard of a store that could not be counted can be confirmed", Next.ConfirmAndSignOut(uncounted),
            UnsyncedNotesSignOutPolicy.answered(Answer.DISCARD, uncounted, uncounted))
        for (answer in listOf(Answer.SYNC_NOW, Answer.GO_ON_WITHOUT_SYNCING)) {
            for (now in listOf(two, uncounted, UnsyncedNotes.NOTHING)) {
                assertFalse("$answer with $now", UnsyncedNotesSignOutPolicy.answered(answer, two, now) is Next.ConfirmAndSignOut)
            }
        }
        assertFalse(UnsyncedNotesSignOutPolicy.start(two) is Next.ConfirmAndSignOut)
    }

    @Test fun `a save between the confirmation dialog and its answer shows the count again`() {
        val saved = UnsyncedNotes(2, 8, false)
        assertEquals(Next.ShowCount(saved), UnsyncedNotesSignOutPolicy.answered(Answer.DISCARD, two, saved))
        val another = UnsyncedNotes(3, 7, false)
        assertEquals("more than was shown", Next.ShowCount(another), UnsyncedNotesSignOutPolicy.answered(Answer.DISCARD, two, another))
        assertEquals("what was counted can no longer be counted", Next.ShowCount(uncounted),
            UnsyncedNotesSignOutPolicy.answered(Answer.DISCARD, two, uncounted))
    }

    @Test fun `the screen records a discard confirmation in one place only, the branch the policy names`() {
        val activity = source("AccountActivity.kt")
        assertEquals(1, Regex("confirmNotesDiscard\\(").findAll(activity).count())
        val branch = activity.substringAfter("is UnsyncedNotesSignOutPolicy.Next.ConfirmAndSignOut -> {").substringBefore("}")
        assertTrue(branch.contains("signOutModel.confirmNotesDiscard(next.shown)"))
        assertTrue(branch.indexOf("confirmNotesDiscard") < branch.indexOf("signOutModel.begin()"))
        assertEquals("a sign-out starts in three places: the plain question, the accepted discard, Retry", 3,
            Regex("signOutModel\\.begin\\(\\)").findAll(activity).count())
        val fragment = source("UnsyncedNotesDialogFragment.kt")
        assertEquals("one button sends the discard answer", 1,
            Regex("send\\(UnsyncedNotesSignOutPolicy\\.Answer\\.DISCARD\\)").findAll(fragment).count())
        assertTrue("and it is the button of step 2", fragment.contains(
            ".setPositiveButton(R.string.notes_discard_and_sign_out) { _, _ ->\n                    send(UnsyncedNotesSignOutPolicy.Answer.DISCARD)\n                }"))
    }

    @Test fun `every end of a sign-out dialog without a sign-out reaches the one function the screen has for it`() {
        val hook = "onSignOutDialogClosedWithoutSignOut()"
        val fragment = source("UnsyncedNotesDialogFragment.kt")
        assertEquals("Cancel on both steps", 2,
            fragment.split(".setNegativeButton(android.R.string.cancel) { _, _ -> closedWithoutSignOut() }").size - 1)
        assertFalse("no Cancel without a listener", fragment.contains("setNegativeButton(android.R.string.cancel, null)"))
        assertTrue(fragment.substringAfter("fun closedWithoutSignOut() {", "").substringBefore("\n        }\n").contains(hook))
        assertTrue("Back and a tap outside",
            fragment.substringAfter("override fun onCancel(dialog: DialogInterface) {", "").substringBefore("\n    }\n").contains(hook))

        val activity = source("AccountActivity.kt")
        assertFalse("the plain question reports its Cancel too", activity.contains("setNegativeButton(android.R.string.cancel, null)"))
        assertTrue(activity.contains(".setNegativeButton(android.R.string.cancel) { _, _ -> $hook }"))
        assertTrue(activity.contains(".setOnCancelListener { $hook }"))
        val sync = activity.substringAfter("private fun syncNotesBeforeSignOut(identity: ExactAccountIdentity) {", "").substringBefore("\n    }\n")
        assertTrue("Sync now reports its end on every return", sync.trimEnd().endsWith("} finally {\n            $hook\n        }"))
        val closed = activity.substringAfter("internal fun $hook {", "").substringBefore("\n    }\n")
        assertTrue("the failure message with Retry is shown again",
            closed.contains("renderedSignOutErrorId = 0") && closed.contains("renderSignOutState(state)"))
        assertTrue("Retry asks before it signs out", activity.contains(".setAction(R.string.retry) { retrySignOut(state) }"))

        // Text checks for the path a sign-out takes while nothing is unsynced: confirmLogout still leads
        // to the plain question, the plain question keeps the string ids of its title and its text, and
        // its positive button holds the call that starts the sign-out.
        val confirm = activity.substringAfter("private fun confirmLogout() {", "").substringBefore("\n    }\n")
        assertTrue("with nothing unsynced the plain question is asked", confirm.contains("else -> confirmPlainLogout()"))
        val plain = activity.substringAfter("private fun confirmPlainLogout() {", "").substringBefore("\n    }\n")
        assertTrue("the plain question keeps its title and its text",
            plain.contains(".setTitle(R.string.account_delete_confirmation_title)") &&
                plain.contains(".setMessage(R.string.account_delete_confirmation_text)"))
        assertTrue("the plain question's own button starts the sign-out",
            plain.contains(".setPositiveButton(R.string.navigation_drawer_logout) { _, _ ->\n                signOutModel.begin()\n            }"))
        val answer = activity.substringAfter("internal fun onUnsyncedNotesAnswer(", "").substringBefore("\n    }\n")
        assertTrue("an answer for another account is reported as an end without a sign-out",
            answer.contains("if (signOutModel.identity() != identity) {\n            $hook\n            return\n        }"))
        val show = activity.substringAfter("private fun showUnsyncedNotes(", "").substringBefore("\n    }\n")
        assertTrue("a dialog that cannot be shown is reported too",
            show.contains("if (identity == null || supportFragmentManager.isStateSaved) {\n            $hook\n            return\n        }"))
        assertTrue("the sign-out message goes before the question is asked",
            confirm.indexOf("signOutErrorSnackbar?.dismiss()") in 0 until confirm.indexOf("UnsyncedNotesSignOutPolicy.start("))
        val retry = activity.substringAfter("private fun retrySignOut(failed: CurrentAccountSignOutState) {", "").substringBefore("\n    }\n")
        assertTrue("after a failed removal Retry reads the store, and it starts at once only when nothing is there",
            retry.contains("if (failed is CurrentAccountSignOutState.RemovalFailed) signOutModel.unsyncedNotes() else UnsyncedNotes.NOTHING") &&
                retry.contains("if (unsynced.anything) showUnsyncedNotes(UnsyncedNotesSignOutPolicy.Step.COUNT, unsynced) else signOutModel.begin()"))
    }
}
