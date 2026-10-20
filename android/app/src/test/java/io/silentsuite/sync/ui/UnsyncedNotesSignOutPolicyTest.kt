package io.silentsuite.sync.ui

import io.silentsuite.sync.notes.edit.UnsyncedNotes
import io.silentsuite.sync.ui.UnsyncedNotesSignOutPolicy.Answer
import io.silentsuite.sync.ui.UnsyncedNotesSignOutPolicy.Next
import io.silentsuite.sync.ui.UnsyncedNotesSignOutPolicy.Text
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class UnsyncedNotesSignOutPolicyTest {
    private val two = UnsyncedNotes(2, 7, false, names = setOf("a.note", "b.note"))
    private val uncounted = UnsyncedNotes(0, 0, unreadable = true)
    private val unlisted = UnsyncedNotes(0, 7, unreadable = true, listed = false)

    private fun answered(answer: Answer, shown: UnsyncedNotes, now: UnsyncedNotes) =
        UnsyncedNotesSignOutPolicy.answered(answer, shown, now)

    private fun source(name: String) =
        File("src/main/java/io/silentsuite/sync/ui/$name").readText().replace("\r\n", "\n")

    /** What follows [start] in [text], up to the first closing brace at [indent]: one function or block. */
    private fun body(text: String, start: String, indent: String = "    ") =
        text.substringAfter(start, "").substringBefore("\n$indent}\n")

    @Test fun `with nothing unsynced the plain question is asked, as before`() {
        assertEquals(Next.AskPlain, UnsyncedNotesSignOutPolicy.start(UnsyncedNotes.NOTHING))
    }

    @Test fun `with something unsynced the count comes first`() {
        assertEquals(Next.ShowCount(two), UnsyncedNotesSignOutPolicy.start(two))
        assertEquals("changes that could not be counted are something", Next.ShowCount(uncounted),
            UnsyncedNotesSignOutPolicy.start(uncounted))
    }

    @Test fun `sync now asks for a Notes sync and confirms nothing`() {
        assertEquals(Next.SyncNotes, answered(Answer.SYNC_NOW, two, two))
    }

    @Test fun `going on without syncing leads to the separate confirmation for what the store holds then`() {
        assertEquals(Next.ShowDiscard(two), answered(Answer.GO_ON_WITHOUT_SYNCING, two, two))
        val more = UnsyncedNotes(3, 8, false, names = two.names + "c.note")
        assertEquals("counted again: step 2 shows what the store holds then", Next.ShowDiscard(more),
            answered(Answer.GO_ON_WITHOUT_SYNCING, two, more))
        assertEquals("everything synced meanwhile", Next.AskPlain,
            answered(Answer.GO_ON_WITHOUT_SYNCING, two, UnsyncedNotes.NOTHING))
        assertEquals("a store that could not be counted gets the separate confirmation too",
            Next.ShowDiscard(uncounted), answered(Answer.GO_ON_WITHOUT_SYNCING, uncounted, uncounted))
    }

    @Test fun `only the discard answer confirms, and only for what its dialog showed`() {
        assertEquals(Next.ConfirmAndSignOut(two), answered(Answer.DISCARD, two, two))
        assertEquals("a change that was pushed meanwhile does not ask again", Next.ConfirmAndSignOut(two),
            answered(Answer.DISCARD, two, UnsyncedNotes(1, 7, false, names = setOf("a.note"))))
        assertEquals("nor does everything having synced", Next.ConfirmAndSignOut(two),
            answered(Answer.DISCARD, two, UnsyncedNotes.NOTHING))
        assertEquals("the discard of a store that could not be counted can be confirmed",
            Next.ConfirmAndSignOut(uncounted), answered(Answer.DISCARD, uncounted, uncounted))
        assertEquals("and so can that of a store that could not be listed, for the same answer",
            Next.ConfirmAndSignOut(unlisted), answered(Answer.DISCARD, unlisted, unlisted))
        for (answer in listOf(Answer.SYNC_NOW, Answer.GO_ON_WITHOUT_SYNCING)) {
            for (now in listOf(two, uncounted, UnsyncedNotes.NOTHING)) {
                assertFalse("$answer with $now", answered(answer, two, now) is Next.ConfirmAndSignOut)
            }
        }
        assertFalse(UnsyncedNotesSignOutPolicy.start(two) is Next.ConfirmAndSignOut)
    }

    @Test fun `a save between the confirmation dialog and its answer shows the count again`() {
        val saved = UnsyncedNotes(2, 8, false, names = two.names)
        assertEquals(Next.ShowCount(saved), answered(Answer.DISCARD, two, saved))
        val another = UnsyncedNotes(3, 7, false, names = two.names + "c.note")
        assertEquals("more than was shown", Next.ShowCount(another), answered(Answer.DISCARD, two, another))
        val unnamed = UnsyncedNotes(2, 7, false, names = setOf("a.note", "c.note"))
        assertEquals("a note the dialog did not name", Next.ShowCount(unnamed), answered(Answer.DISCARD, two, unnamed))
        assertEquals("what was counted can no longer be counted", Next.ShowCount(uncounted),
            answered(Answer.DISCARD, two, uncounted))
        val listedNow = UnsyncedNotes(0, 7, unreadable = true)
        assertEquals("a store that could not be listed lists now", Next.ShowCount(listedNow),
            answered(Answer.DISCARD, unlisted, listedNow))
        assertEquals("a store that listed can no longer be listed", Next.ShowCount(unlisted),
            answered(Answer.DISCARD, listedNow, unlisted))
    }

    @Test fun `the dialogs keep all of the answer they showed, the names it was counted from included`() {
        val fragment = source("UnsyncedNotesDialogFragment.kt")
        for (field in listOf("count", "sequence", "unreadable", "listed", "names")) {
            val arg = "ARG_" + field.uppercase()
            assertTrue("$field is kept", Regex("put\\w+\\($arg, [^\\n]*shown\\.$field\\b").containsMatchIn(fragment))
            assertTrue("$field is read back", Regex("args\\.get\\w+\\($arg\\)").containsMatchIn(fragment))
        }
        val shown = fragment.substringAfter("val shown = UnsyncedNotes(", "").substringBefore("\n        fun ")
        assertTrue("the answer is built from all five", shown.contains("args.getBoolean(ARG_LISTED)") &&
            shown.contains("args.getStringArrayList(ARG_NAMES)!!.toSet()"))
    }

    @Test fun `a count gives the number, changes that could not be counted say so, and zero is never shown`() {
        assertEquals(Text.Counted(2), UnsyncedNotesSignOutPolicy.text(two))
        assertEquals(Text.Counted(1), UnsyncedNotesSignOutPolicy.text(UnsyncedNotes(1, 3, false)))
        assertEquals(Text.Uncounted, UnsyncedNotesSignOutPolicy.text(uncounted))
        assertEquals("files counted by name are not a count of changes", Text.Uncounted,
            UnsyncedNotesSignOutPolicy.text(UnsyncedNotes(3, 9, unreadable = true)))
        assertEquals("never a count of zero", Text.Uncounted, UnsyncedNotesSignOutPolicy.text(UnsyncedNotes.NOTHING))
        val fragment = source("UnsyncedNotesDialogFragment.kt")
        assertEquals("both dialogs take their words from the policy", 1,
            Regex("UnsyncedNotesSignOutPolicy\\.text\\(shown\\)").findAll(fragment).count())
        assertFalse("no dialog formats the count itself", fragment.contains("shown.count, shown.count"))
    }

    @Test fun `the discard answer comes from one button, the one of step 2`() {
        val fragment = source("UnsyncedNotesDialogFragment.kt")
        assertEquals("one button sends the discard answer", 1,
            Regex("send\\(UnsyncedNotesSignOutPolicy\\.Answer\\.DISCARD\\)").findAll(fragment).count())
        assertTrue("and it is the button of step 2", fragment.contains(
            ".setPositiveButton(R.string.notes_discard_and_sign_out) { _, _ ->\n" +
                "                    send(UnsyncedNotesSignOutPolicy.Answer.DISCARD)\n                }"))
        val activity = source("AccountActivity.kt")
        val answer = body(activity, "internal fun onUnsyncedNotesAnswer(")
        assertTrue("the accepted discard asks for the sign-out",
            answer.contains("is UnsyncedNotesSignOutPolicy.Next.ConfirmAndSignOut -> signOutModel.begin()"))
        assertEquals("a sign-out is asked for in three places: the plain question, the accepted discard, Retry", 3,
            Regex("signOutModel\\.begin\\(\\)").findAll(activity).count())
    }

    @Test fun `every end of a sign-out dialog without a sign-out reaches the one function the screen has for it`() {
        val hook = "onSignOutDialogClosedWithoutSignOut()"
        val fragment = source("UnsyncedNotesDialogFragment.kt")
        assertEquals("Cancel on both steps", 2,
            fragment.split(".setNegativeButton(android.R.string.cancel) { _, _ -> closedWithoutSignOut() }").size - 1)
        assertFalse("no Cancel without a listener",
            fragment.contains("setNegativeButton(android.R.string.cancel, null)"))
        assertTrue(body(fragment, "fun closedWithoutSignOut() {", "        ").contains(hook))
        assertTrue("Back and a tap outside",
            body(fragment, "override fun onCancel(dialog: DialogInterface) {").contains(hook))

        val activity = source("AccountActivity.kt")
        assertFalse("the plain question reports its Cancel too",
            body(activity, "private fun confirmPlainLogout() {").contains("setNegativeButton(android.R.string.cancel, null)"))
        assertTrue(activity.contains(".setNegativeButton(android.R.string.cancel) { _, _ -> $hook }"))
        assertTrue(activity.contains(".setOnCancelListener { $hook }"))
        val sync = body(activity, "private fun syncNotesBeforeSignOut(identity: ExactAccountIdentity) {")
        assertTrue("Sync now reports its end on every return",
            sync.trimEnd().endsWith("} finally {\n            $hook\n        }"))
        val closed = body(activity, "internal fun $hook {")
        assertTrue("the failure message with Retry is shown again",
            closed.contains("renderedSignOutErrorId = 0") && closed.contains("renderSignOutState(state)"))
        assertTrue("Retry asks before it signs out",
            activity.contains(".setAction(R.string.retry) { retrySignOut(state) }"))

        // Text checks for the path a sign-out takes while nothing is unsynced: confirmLogout still leads
        // to the plain question, the plain question keeps the string ids of its title and its text, and
        // its positive button holds the call that asks for the sign-out.
        val confirm = body(activity, "private fun confirmLogout() {")
        assertTrue("with nothing unsynced the plain question is asked",
            confirm.contains("else -> confirmPlainLogout()"))
        assertTrue("the sign-out message goes before the question is asked",
            confirm.indexOf("signOutErrorSnackbar?.dismiss()") in
                0 until confirm.indexOf("UnsyncedNotesSignOutPolicy.start("))
        val plain = body(activity, "private fun confirmPlainLogout() {")
        assertTrue("the plain question keeps its title and its text",
            plain.contains(".setTitle(R.string.account_delete_confirmation_title)") &&
                plain.contains(".setMessage(R.string.account_delete_confirmation_text)"))
        assertTrue("the plain question's own button asks for the sign-out", plain.contains(
            ".setPositiveButton(R.string.navigation_drawer_logout) { _, _ ->\n                signOutModel.begin()\n"))
        val answer = body(activity, "internal fun onUnsyncedNotesAnswer(")
        assertTrue("an answer for another account is reported as an end without a sign-out", answer.contains(
            "if (signOutModel.identity() != identity) {\n            $hook\n            return\n        }"))
        val show = body(activity, "private fun showUnsyncedNotes(")
        assertTrue("a dialog that cannot be shown is reported too", show.contains(
            "if (identity == null || supportFragmentManager.isStateSaved) {\n            $hook\n            return\n"))
        val retry = body(activity, "private fun retrySignOut(failed: CurrentAccountSignOutState) {")
        assertTrue("after a failed removal Retry reads the store, and it starts at once only when nothing is there",
            retry.contains("if (failed is CurrentAccountSignOutState.RemovalFailed) signOutModel.unsyncedNotes()") &&
                retry.contains("if (unsynced.anything) showUnsyncedNotes(UnsyncedNotesSignOutPolicy.Step.COUNT, " +
                    "unsynced)\n        else signOutModel.begin()"))
    }
}
