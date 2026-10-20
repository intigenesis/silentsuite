package io.silentsuite.sync.ui

import io.silentsuite.sync.R
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

    /** The text of the string resource [name] in the default resources. */
    private fun stringText(name: String): String =
        Regex("<string name=\"$name\">(.*?)</string>").find(File("src/main/res/values/strings.xml").readText())
            ?.groupValues?.get(1) ?: throw AssertionError("no string $name")

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
        assertEquals("a store that could not be listed can be acknowledged, for the same answer",
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
            ".setPositiveButton(confirm) { _, _ ->\n" +
                "                    send(UnsyncedNotesSignOutPolicy.Answer.DISCARD)\n                }"))
        assertTrue("whose words are those of a discard only for a store that lists", fragment.contains(
            "val confirm = if (unlisted) R.string.notes_acknowledge_continue else R.string.notes_discard_and_sign_out"))
        val activity = source("AccountActivity.kt")
        assertEquals("the screen gives the confirmation in one place", 1,
            Regex("confirmNotesDiscard\\(").findAll(activity).count())
        val branch = activity.substringAfter("is UnsyncedNotesSignOutPolicy.Next.ConfirmAndSignOut -> {", "")
            .substringBefore("}")
        assertTrue("the accepted discard confirms what its dialog showed, then asks for the sign-out",
            branch.contains("signOutModel.confirmNotesDiscard(next.shown)") &&
                branch.indexOf("confirmNotesDiscard") < branch.indexOf("signOutModel.begin()"))
        assertEquals("a sign-out is asked for in three places: the plain question, the accepted discard, Retry", 3,
            Regex("signOutModel\\.begin\\(\\)").findAll(activity).count())
    }

    @Test fun `a refused sign-out asks again, and a sign-out stopped after a clear says what may be gone`() {
        val activity = source("AccountActivity.kt")
        val render = body(activity, "private fun renderSignOutState(state: CurrentAccountSignOutState) {")
        val refusal = body(render, "if (state is CurrentAccountSignOutState.Refused) {", "        ")
        assertTrue("a refusal is acknowledged, then the question is asked again, and nothing more is rendered",
            refusal.trimEnd().endsWith(
                "signOutModel.acknowledgeRefusal()\n            confirmLogout()\n            return"))
        assertTrue("a refusal is handled before the failure messages",
            render.indexOf("if (state is CurrentAccountSignOutState.Refused) {") in
                0 until render.indexOf("val errorId = when (state) {"))
        assertTrue("a clear that stopped has a failure message with Retry",
            render.contains("is CurrentAccountSignOutState.NotesClearFailed -> state.errorId"))
        assertTrue("the message is the policy's", render.contains(
            "val message = UnsyncedNotesSignOutPolicy.failureMessage(state)\n            signOutErrorSnackbar = " +
                "Snackbar.make(findViewById(R.id.coordinator), message, Snackbar.LENGTH_INDEFINITE)"))
        assertEquals("a clear of confirmed changes that stopped says they may be gone", R.string.notes_sign_out_stopped,
            UnsyncedNotesSignOutPolicy.failureMessage(CurrentAccountSignOutState.NotesClearFailed(1, true)))
        assertEquals("one with no confirmed changes says only that the account was kept",
            R.string.notes_sign_out_stopped_kept,
            UnsyncedNotesSignOutPolicy.failureMessage(CurrentAccountSignOutState.NotesClearFailed(1, false)))
        assertEquals("so does a removal that failed after confirmed changes were cleared",
            R.string.notes_sign_out_stopped, UnsyncedNotesSignOutPolicy.failureMessage(
                CurrentAccountSignOutState.RemovalFailed(1, notesDiscarded = true, notesCleared = true)))
        assertEquals("one that failed after a clear of no confirmed changes says only that the account was kept",
            R.string.notes_sign_out_stopped_kept,
            UnsyncedNotesSignOutPolicy.failureMessage(CurrentAccountSignOutState.RemovalFailed(1, notesCleared = true)))
        assertEquals("only a removal that failed before any clear keeps the usual words",
            R.string.account_sign_out_failed,
            UnsyncedNotesSignOutPolicy.failureMessage(CurrentAccountSignOutState.RemovalFailed(1)))
        assertEquals(R.string.account_sign_out_cleanup_failed,
            UnsyncedNotesSignOutPolicy.failureMessage(CurrentAccountSignOutState.CleanupFailed(1)))
        val closed = body(activity, "internal fun onSignOutDialogClosedWithoutSignOut() {")
        assertTrue("a dialog that ends after a clear that stopped shows that message again",
            closed.contains("state is CurrentAccountSignOutState.NotesClearFailed"))
        assertTrue("when a dialog ends on a retained screen that is idle, the activity is created again",
            closed.contains("} else if (signOutOnly && state is CurrentAccountSignOutState.Idle) {") &&
                closed.indexOf("recreate()") > closed.indexOf("signOutOnly && state is"))

        val model = source("AndroidCurrentAccountSignOut.kt")
        assertTrue("a refusal does not count as a started sign-out", model.contains(
            "fun hasStarted() = coordinator?.state?.let {\n        it !is CurrentAccountSignOutState.Idle && " +
                "it !is CurrentAccountSignOutState.Refused\n    } == true"))
        assertTrue("the screen's acknowledgment reaches the coordinator",
            model.contains("internal fun acknowledgeRefusal() { coordinator?.acknowledgeRefusal() }"))
        assertTrue("the screen's confirmation reaches the coordinator for the owner's exact account",
            model.contains("exactAccount?.let { coordinator?.confirmNotesDiscard(it, shown) }"))
        // Text checks of the Android seams, which no unit test builds: the clear and the reopening go to the
        // store of the exact generation, and the clear passes the confirmation on.
        assertTrue(model.contains(
            "override fun discardPendingNotes(main: ExactAccountIdentity, confirmed: UnsyncedNotes?) =\n" +
                "        pendingNotes(main).discardStore(confirmed)"))
        assertTrue(model.contains(
            "override fun reopenPendingNotes(main: ExactAccountIdentity) = pendingNotes(main).unregister()"))
        assertTrue(model.contains("PendingNotesStore.forIdentity(context, main.type, main.name, main.creationId)"))
    }

    @Test fun `a sign-out stopped at a store that could not be listed says nothing was deleted and never discard`() {
        for (acknowledgement in CurrentAccountSignOutState.NotesClearFailed.Acknowledgement.values()) {
            for (discarded in listOf(false, true)) {
                val state = CurrentAccountSignOutState.NotesClearFailed(1, discarded, acknowledgement)
                assertEquals("$state", R.string.notes_sign_out_stopped_nothing_deleted,
                    UnsyncedNotesSignOutPolicy.failureMessage(state))
            }
        }
        val text = stringText("notes_sign_out_stopped_nothing_deleted")
        assertTrue(text, text.contains("Nothing was deleted") && text.contains("may remain on this device") &&
            text.contains("may no longer sync"))
        assertFalse("never called a discard: $text", text.contains("discard", ignoreCase = true))
        assertFalse(text, text.contains("may already be deleted"))
        assertTrue("the words of a clear that stopped are another string",
            stringText("notes_sign_out_stopped").contains("may already be deleted"))
    }

    @Test fun `a store that could not be listed gets words of its own in both steps, never those of a discard`() {
        for (count in listOf(0, 2)) for (unreadable in listOf(true, false)) {
            for (names in listOf(emptySet<String>(), two.names)) {
                val shown = UnsyncedNotes(count, 7, unreadable, listed = false, names = names)
                assertEquals("$shown", Text.Unlisted, UnsyncedNotesSignOutPolicy.text(shown))
            }
        }
        assertEquals("one that lists and could not be counted keeps its words", Text.Uncounted,
            UnsyncedNotesSignOutPolicy.text(UnsyncedNotes(0, 7, unreadable = true)))
        val fragment = source("UnsyncedNotesDialogFragment.kt")
        assertTrue(fragment.contains("val unlisted = text == UnsyncedNotesSignOutPolicy.Text.Unlisted\n"))
        // The message of each step, by the policy's result: only a store that lists gets the words of a discard.
        for ((counted, uncounted, notListed) in listOf(
            Triple("notes_sign_out_unsynced", "notes_sign_out_unsynced_unreadable", "notes_sign_out_unsynced_unlisted"),
            Triple("notes_discard_text", "notes_discard_unreadable", "notes_acknowledge_text"),
        )) {
            assertTrue(notListed, fragment.contains("is UnsyncedNotesSignOutPolicy.Text.Counted ->\n" +
                "                        resources.getQuantityString(R.plurals.$counted, text.count, text.count)\n" +
                "                    UnsyncedNotesSignOutPolicy.Text.Uncounted -> getString(R.string.$uncounted)\n" +
                "                    UnsyncedNotesSignOutPolicy.Text.Unlisted -> getString(R.string.$notListed)\n"))
        }
        // The action of step 1 that goes on, and the title and button of step 2.
        val choices = Regex("val (\\w+) = if \\(unlisted\\) R\\.string\\.(\\w+) else R\\.string\\.(\\w+)\n")
            .findAll(fragment).associate { it.groupValues[1] to (it.groupValues[2] to it.groupValues[3]) }
        assertEquals("for a store that could not be listed, and for one that lists", mapOf(
            "goOn" to ("notes_continue_without_syncing" to "notes_sign_out_without_syncing"),
            "confirmTitle" to ("notes_acknowledge_title" to "notes_discard_title"),
            "confirm" to ("notes_acknowledge_continue" to "notes_discard_and_sign_out"),
        ), choices)
        assertTrue(fragment.contains(".setNeutralButton(goOn) { _, _ ->\n"))
        assertTrue(fragment.contains(".setTitle(confirmTitle)\n"))
        // Each word of a discard is named once, so only in the places above.
        for (name in listOf("string.notes_sign_out_unsynced_unreadable", "string.notes_sign_out_without_syncing",
            "string.notes_discard_title", "string.notes_discard_unreadable", "string.notes_discard_and_sign_out",
            "plurals.notes_sign_out_unsynced", "plurals.notes_discard_text")) {
            assertEquals(name, 1, Regex("R\\.${name.replace(".", "\\.")}\\b").findAll(fragment).count())
        }
    }

    @Test fun `the words for a store that could not be listed say that nothing will be deleted, never discard`() {
        val messages = listOf("notes_sign_out_unsynced_unlisted", "notes_acknowledge_text").map { stringText(it) }
        val labels = listOf("notes_continue_without_syncing", "notes_acknowledge_title", "notes_acknowledge_continue")
            .map { stringText(it) }
        for (text in messages + labels) {
            assertFalse("never called a discard: $text", text.contains("discard", ignoreCase = true))
            assertFalse("nothing is said to be removed: $text", text.contains("remove", ignoreCase = true))
            assertFalse(text, text.contains("cannot be undone") || text.contains("cannot be recovered"))
            val rest = text.replace("Nothing will be deleted", "").replace("deletes nothing", "")
                .replace("without deleting", "")
            assertFalse("nothing is said to be deleted: $text", rest.contains("delet", ignoreCase = true))
        }
        for (text in labels) {
            assertFalse("no title or button names a sign-out: $text", text.contains("sign", ignoreCase = true))
        }
        for (text in messages) {
            assertTrue(text, text.contains("your account stays signed in") &&
                text.contains("may remain on this device") && text.contains("may no longer sync"))
        }
        assertTrue(messages[0], messages[0].contains("deletes nothing"))
        assertTrue(messages[1], messages[1].contains("Nothing will be deleted"))
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
        assertTrue("before the removal Retry reads the store, and it starts at once only when nothing is there",
            retry.contains("if (failed is CurrentAccountSignOutState.CleanupFailed) UnsyncedNotes.NOTHING\n" +
                "            else signOutModel.unsyncedNotes()") &&
                retry.contains("if (unsynced.anything) showUnsyncedNotes(UnsyncedNotesSignOutPolicy.Step.COUNT, " +
                    "unsynced)\n        else signOutModel.begin()"))
    }
}
