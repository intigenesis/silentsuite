package io.silentsuite.sync.ui

import android.Manifest
import android.accounts.Account
import android.accounts.AccountManager
import android.content.Context
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.view.View
import androidx.core.view.ViewCompat
import androidx.core.view.GravityCompat
import androidx.drawerlayout.widget.DrawerLayout
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.UiDevice
import androidx.lifecycle.Lifecycle
import io.silentsuite.sync.AccountSettings
import io.silentsuite.sync.App
import io.silentsuite.sync.R
import io.silentsuite.sync.notes.NotesSyncCoordinator
import io.silentsuite.sync.notes.edit.UnsyncedNotes
import io.silentsuite.sync.resource.LocalAddressBook
import io.silentsuite.sync.syncadapter.AccountAuthenticatorService
import io.silentsuite.sync.syncadapter.SyncStatusStore
import io.silentsuite.sync.ui.setup.PostLoginSetupState
import io.silentsuite.sync.utils.AndroidCompat
import java.net.URI
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class AccountDrawerSignOutRuntimeTest {
    private fun waitUntil(description: String, timeoutMillis: Long = 10_000, predicate: () -> Boolean) {
        val deadline = SystemClock.uptimeMillis() + timeoutMillis
        while (SystemClock.uptimeMillis() < deadline) {
            if (predicate()) return
            SystemClock.sleep(25)
        }
        assertTrue("Timed out waiting for $description", predicate())
    }

    @Test fun oneAccountSwitcherExposesAddRowAndCurrentSemanticsAfterRecreation() {
        val fixture = Fixture("switcher")
        fixture.use { account ->
            ActivityScenario.launch<AccountActivity>(AccountActivity.newIntent(fixture.context, account)).use { scenario ->
                scenario.recreate()
                scenario.onActivity { activity ->
                    activity.findViewById<DrawerLayout>(R.id.drawer_layout)
                        .openDrawer(GravityCompat.START, false)
                    val header = activity.findViewById<View>(R.id.nav_account_header)
                    header.performClick()
                    val add = activity.findViewById<View>(R.id.nav_add_account_row)
                    val row = activity.findViewById<View>(AccountActivity.accountRowViewId(
                        ExactAccountIdentity(account.type, account.name, fixture.creationId)))
                    assertTrue(add.isShown)
                    assertTrue(add.isClickable)
                    assertTrue(row.isShown)
                    assertTrue(row.isSelected)
                    assertEquals(activity.getString(R.string.account_switcher_current),
                        ViewCompat.getStateDescription(row)?.toString())
                    assertEquals(activity.getString(R.string.account_switcher_expanded),
                        ViewCompat.getStateDescription(header)?.toString())
                }
            }
        }
    }

    @Test fun replacementBeforeAccountRowClickDoesNotAdoptReplacementGeneration() {
        val current = Fixture("switch-current")
        val candidate = Fixture("switch-candidate")
        try {
            val currentIdentity = ExactAccountIdentity(
                current.account.type,
                current.account.name,
                current.creationId,
            )
            assertTrue(ActiveAccountManager.setActiveAccount(current.context, currentIdentity))
            val capturedIdentity = ExactAccountIdentity(
                candidate.account.type,
                candidate.account.name,
                candidate.creationId,
            )
            ActivityScenario.launch<AccountActivity>(
                AccountActivity.newIntent(current.context, current.account, current.creationId)
            ).use { scenario ->
                scenario.onActivity { activity ->
                    activity.findViewById<DrawerLayout>(R.id.drawer_layout)
                        .openDrawer(GravityCompat.START, false)
                    activity.findViewById<View>(R.id.nav_account_header).performClick()
                    assertTrue(activity.findViewById<View>(
                        AccountActivity.accountRowViewId(capturedIdentity)
                    ).isShown)
                }

                removeAccountAndWait(candidate.manager, candidate.account)
                val replacementGeneration = "switch-candidate-replacement"
                val replacementData = Bundle().apply {
                    putString(AccountSettings.KEY_CREATION_ID, replacementGeneration)
                }
                assertTrue(candidate.manager.addAccountExplicitly(
                    candidate.account,
                    null,
                    replacementData,
                ))

                scenario.onActivity { activity ->
                    activity.findViewById<View>(
                        AccountActivity.accountRowViewId(capturedIdentity)
                    ).performClick()
                    assertFalse(activity.isFinishing)
                    assertEquals(current.account, ActiveAccountManager.getActiveAccount(current.context))
                    assertEquals(
                        current.creationId,
                        activity.intent.getStringExtra(AccountActivity.EXTRA_CREATION_ID),
                    )
                    assertEquals(
                        replacementGeneration,
                        candidate.manager.getUserData(
                            candidate.account,
                            AccountSettings.KEY_CREATION_ID,
                        ),
                    )
                }
            }
            postCommitGenerationRaceFailsClosedAndClearsOnlyTheWrittenIdentity(
                candidate,
                "switch-candidate-replacement",
            )
        } finally {
            ActiveAccountManager.afterExactSetCommitForTest = null
            candidate.close()
            current.close()
        }
    }

    private fun postCommitGenerationRaceFailsClosedAndClearsOnlyTheWrittenIdentity(
        candidate: Fixture,
        expectedGeneration: String,
    ) {
        val racedGeneration = "switch-candidate-raced-after-commit"
        ActiveAccountManager.afterExactSetCommitForTest = {
            check(AccountSettings.writeVerified(
                candidate.manager,
                candidate.account,
                AccountSettings.KEY_CREATION_ID,
                racedGeneration,
            ))
        }
        val selected = ActiveAccountManager.setActiveAccount(
            candidate.context,
            ExactAccountIdentity(
                candidate.account.type,
                candidate.account.name,
                expectedGeneration,
            ),
        )
        ActiveAccountManager.afterExactSetCommitForTest = null

        assertFalse(selected)
        assertNull(ActiveAccountManager.getActiveAccount(candidate.context))
        assertEquals(
            racedGeneration,
            candidate.manager.getUserData(
                candidate.account,
                AccountSettings.KEY_CREATION_ID,
            ),
        )
    }

    @Test fun systemBackClosesDrawerWithoutFinishing() {
        val fixture = Fixture("system-back-drawer")
        fixture.use { account ->
            ActivityScenario.launch<AccountActivity>(AccountActivity.newIntent(fixture.context, account)).use { scenario ->
                scenario.onActivity { activity ->
                    activity.findViewById<DrawerLayout>(R.id.drawer_layout)
                        .openDrawer(GravityCompat.START, false)
                    assertTrue(activity.findViewById<DrawerLayout>(R.id.drawer_layout)
                        .isDrawerOpen(GravityCompat.START))
                }
                UiDevice.getInstance(InstrumentationRegistry.getInstrumentation()).pressBack()
                waitUntil("drawer to close after system Back") {
                    var closed = false
                    scenario.onActivity { activity ->
                        closed = !activity.isFinishing && !activity.findViewById<DrawerLayout>(R.id.drawer_layout)
                            .isDrawerOpen(GravityCompat.START)
                    }
                    closed
                }
                assertEquals(Lifecycle.State.RESUMED, scenario.state)
            }
        }
    }

    @Test fun delayedRemovalFailureSurvivesRecreationAndDuplicateTapStartsOnce() {
        val fixture = Fixture("failure")
        val fake = DelayedFailureSeams(fixture.account)
        CurrentAccountSignOutViewModel.seamsFactory = { _, exact, generation ->
            assertEquals(fixture.account, exact)
            assertEquals(fixture.creationId, generation)
            fake
        }
        try {
            ActivityScenario.launch<AccountActivity>(AccountActivity.newIntent(fixture.context, fixture.account)).use { scenario ->
                scenario.onActivity { activity ->
                    val model = androidx.lifecycle.ViewModelProvider(activity)[CurrentAccountSignOutViewModel::class.java]
                    model.begin()
                    model.begin()
                    assertEquals(1, fake.removeCalls)
                }
                scenario.recreate()
                fake.callback!!(false)
                InstrumentationRegistry.getInstrumentation().waitForIdleSync()
                scenario.onActivity { activity ->
                    assertTrue(activity.findViewById<com.google.android.material.navigation.NavigationView>(R.id.nav_view)
                        .menu.findItem(R.id.nav_logout).isEnabled)
                    assertEquals(0, fake.destructiveCleanupCalls)
                }
            }
        } finally {
            CurrentAccountSignOutViewModel.seamsFactory = null
            fixture.close()
        }
    }

    @Test fun actualTwoAccountSignOutRemovesExactMainAndChildAndActivatesSibling() {
        val target = Fixture("actual-target")
        val sibling = Fixture("actual-sibling")
        val child = Account("child-${System.nanoTime()}@example.invalid", App.addressBookAccountType)
        check(target.manager.addAccountExplicitly(
            child,
            null,
            LocalAddressBook.initialUserData(target.account, SyncStatusStore(target.context).identity(target.account), "https://example.invalid/address-book"),
        ))
        try {
            assertTrue(ActiveAccountManager.setActiveAccount(target.context, target.account))
            val statusStore = SyncStatusStore(target.context)
            assertTrue(statusStore.recordSuccess(target.account, SyncStatusStore.Service.CALENDAR, 123L))
            assertEquals(123L,
                statusStore.status(target.account, SyncStatusStore.Service.CALENDAR).lastSuccessAt)
            val expectedReplacement = target.manager.getAccountsByType(App.accountType).mapNotNull { candidate ->
                target.manager.getUserData(candidate, AccountSettings.KEY_CREATION_ID)?.takeIf(String::isNotBlank)?.let {
                    ExactAccountIdentity(candidate.type, candidate.name, it)
                }
            }.let(AccountSwitcherPolicy::ordered).first { it != ExactAccountIdentity(
                target.account.type, target.account.name, target.creationId) }
            val completed = CountDownLatch(1)
            var finalState: CurrentAccountSignOutState? = null
            val coordinator = CurrentAccountSignOutCoordinator(
                AndroidCurrentAccountSignOut(target.context, target.account, target.creationId)
            ) { state ->
                if (state is CurrentAccountSignOutState.Complete) {
                    finalState = state
                    completed.countDown()
                }
            }
            coordinator.begin()
            assertTrue("sign-out did not complete", completed.await(15, TimeUnit.SECONDS))
            assertEquals(expectedReplacement, (finalState as CurrentAccountSignOutState.Complete).replacement)
            assertFalse(target.account in target.manager.getAccountsByType(target.account.type))
            assertFalse(child in target.manager.getAccountsByType(child.type))
            // Recreate the same exact generation temporarily. Any uncleared status would now be
            // visible again because SyncStatusStore keys include this creation ID.
            assertTrue(target.manager.addAccountExplicitly(target.account, null, null))
            assertTrue(AccountSettings.writeVerified(target.manager, target.account,
                AccountSettings.KEY_CREATION_ID, target.creationId))
            assertEquals(SyncStatusStore.Status(),
                statusStore.status(target.account, SyncStatusStore.Service.CALENDAR))
            assertEquals(
                Account(expectedReplacement.name, expectedReplacement.type),
                ActiveAccountManager.getActiveAccount(target.context),
            )
        } finally {
            removeAccountAndWait(target.manager, child)
            sibling.close()
            target.close()
        }
    }

    @Test fun cleanupSweepKeepsUnreadableOwnerChildAndDeletesOnlyOrphans() {
        val sibling = Fixture("sweep-sibling")
        val departed = Fixture("sweep-departed")
        val statusStore = SyncStatusStore(sibling.context)
        val owned = Account("sweep-owned-${System.nanoTime()}@example.invalid", App.addressBookAccountType)
        val unreadable = Account("sweep-unreadable-${System.nanoTime()}@example.invalid", App.addressBookAccountType)
        val orphan = Account("sweep-orphan-${System.nanoTime()}@example.invalid", App.addressBookAccountType)
        try {
            check(sibling.manager.addAccountExplicitly(owned, null,
                LocalAddressBook.initialUserData(sibling.account, statusStore.identity(sibling.account), "https://example.invalid/owned")))
            // The platform still enumerates this row but its owner metadata is unreadable: exactly
            // what the sweep observes when a sign-out removes a child between enumeration and read.
            check(sibling.manager.addAccountExplicitly(unreadable, null, null))
            check(sibling.manager.addAccountExplicitly(orphan, null,
                LocalAddressBook.initialUserData(departed.account, statusStore.identity(departed.account), "https://example.invalid/orphan")))
            removeAccountAndWait(departed.manager, departed.account)
            assertNull(LocalAddressBook(sibling.context, unreadable, null).mainAccountOrNull)

            AccountAuthenticatorService.cleanupAccounts(sibling.context)

            waitUntil("orphaned child removal") { orphan !in sibling.manager.getAccountsByType(orphan.type) }
            assertTrue("sibling-owned child was removed", owned in sibling.manager.getAccountsByType(owned.type))
            assertTrue("unreadable-owner child was removed", unreadable in sibling.manager.getAccountsByType(unreadable.type))
            assertTrue("sibling main was removed", sibling.account in sibling.manager.getAccountsByType(sibling.account.type))
        } finally {
            removeAccountAndWait(sibling.manager, owned)
            removeAccountAndWait(sibling.manager, unreadable)
            removeAccountAndWait(sibling.manager, orphan)
            departed.close()
            sibling.close()
        }
    }

    @Test fun deleteAfterChildRowVanishedDoesNotThrowAndLeavesSiblingRowsIntact() {
        val sibling = Fixture("vanish-sibling")
        val departed = Fixture("vanish-departed")
        val statusStore = SyncStatusStore(sibling.context)
        val siblingChild = Account("vanish-sibling-child-${System.nanoTime()}@example.invalid", App.addressBookAccountType)
        val vanished = Account("vanish-child-${System.nanoTime()}@example.invalid", App.addressBookAccountType)
        try {
            check(sibling.manager.addAccountExplicitly(siblingChild, null,
                LocalAddressBook.initialUserData(sibling.account, statusStore.identity(sibling.account), "https://example.invalid/sibling-child")))
            check(sibling.manager.addAccountExplicitly(vanished, null,
                LocalAddressBook.initialUserData(departed.account, statusStore.identity(departed.account), "https://example.invalid/vanished")))
            val addressBook = LocalAddressBook(sibling.context, vanished, null)
            assertEquals(departed.account, addressBook.mainAccount)
            val siblingStatusBefore = statusStore.status(sibling.account, SyncStatusStore.Service.CONTACTS)
            // The sweep decided to delete this orphan, but a concurrent sign-out removed the row first.
            removeAccountAndWait(sibling.manager, vanished)
            removeAccountAndWait(departed.manager, departed.account)
            assertNull(addressBook.mainAccountOrNull)

            addressBook.delete()

            assertFalse(vanished in sibling.manager.getAccountsByType(vanished.type))
            assertTrue("sibling child was removed", siblingChild in sibling.manager.getAccountsByType(siblingChild.type))
            assertTrue("sibling main was removed", sibling.account in sibling.manager.getAccountsByType(sibling.account.type))
            assertEquals(siblingStatusBefore, statusStore.status(sibling.account, SyncStatusStore.Service.CONTACTS))
        } finally {
            removeAccountAndWait(sibling.manager, siblingChild)
            removeAccountAndWait(sibling.manager, vanished)
            departed.close()
            sibling.close()
        }
    }

    @Test fun adapterRefusesToRemoveSameNameReplacementGeneration() {
        val fixture = Fixture("replacement")
        var adapter: AndroidCurrentAccountSignOut? = null
        try {
            val oldIdentity = ExactAccountIdentity(fixture.account.type, fixture.account.name, fixture.creationId)
            val retainedAdapter = AndroidCurrentAccountSignOut(fixture.context, fixture.account, fixture.creationId)
            adapter = retainedAdapter
            removeAccountAndWait(fixture.manager, fixture.account)
            InstrumentationRegistry.getInstrumentation().waitForIdleSync()
            val replacementGeneration = "replacement-successor-generation"
            assertNotEquals(fixture.creationId, replacementGeneration)
            val replacementData = Bundle().apply {
                putString(AccountSettings.KEY_CREATION_ID, replacementGeneration)
            }
            assertTrue(fixture.manager.addAccountExplicitly(fixture.account, null, replacementData))
            val replacementRow = fixture.manager.getAccountsByType(fixture.account.type)
                .single { it.name == fixture.account.name }
            assertEquals(replacementGeneration,
                fixture.manager.getUserData(replacementRow, AccountSettings.KEY_CREATION_ID))

            waitUntil("replacement generation visibility") {
                retainedAdapter.mainGenerationAbsent(oldIdentity)
            }
            val callbackReceived = CountDownLatch(1)
            var callback: Boolean? = null
            retainedAdapter.removeMain(oldIdentity) {
                callback = it
                callbackReceived.countDown()
            }
            assertTrue("replacement refusal callback timed out", callbackReceived.await(10, TimeUnit.SECONDS))
            assertEquals(false, callback)
            waitUntil("replacement generation visibility") {
                retainedAdapter.mainGenerationAbsent(oldIdentity)
            }
            assertTrue(fixture.account in fixture.manager.getAccountsByType(fixture.account.type))
        } finally {
            adapter?.close()
            fixture.close()
        }
    }

    @Test fun delayedSuccessfulRemovalAfterRecreationRoutesRetainedSibling() {
        val target = Fixture("retained-target")
        val sibling = Fixture("retained-sibling")
        val siblingIdentity = ExactAccountIdentity(sibling.account.type, sibling.account.name, sibling.creationId)
        val fake = DelayedSuccessSeams(target.context, target.account, target.creationId, sibling.account, siblingIdentity)
        CurrentAccountSignOutViewModel.seamsFactory = { _, _, _ -> fake }
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        try {
            ActivityScenario.launch<AccountActivity>(AccountActivity.newIntent(target.context, target.account)).use { scenario ->
                scenario.onActivity { activity ->
                    androidx.lifecycle.ViewModelProvider(activity)[CurrentAccountSignOutViewModel::class.java].begin()
                }
                // The platform row disappears while the retained coordinator still awaits its
                // callback. The recreated Activity must attach to that coordinator before trying
                // to validate its now-stale explicit route.
                removeAccountAndWait(target.manager, target.account)
                scenario.recreate()
                val monitor = instrumentation.addMonitor(AccountActivity::class.java.name, null, false)
                var launched: android.app.Activity? = null
                try {
                    // The retained ViewModel already owns its coordinator. Do not let this
                    // target-specific factory initialize the replacement sibling Activity.
                    CurrentAccountSignOutViewModel.seamsFactory = null
                    fake.absent = true
                    fake.callback!!(true)
                    launched = instrumentation.waitForMonitorWithTimeout(monitor, 10_000)
                    val replacementActivity = requireNotNull(launched) { "replacement AccountActivity was not launched" }
                    instrumentation.waitForIdleSync()
                    assertEquals(sibling.account,
                        replacementActivity.intent.getParcelableExtra<Account>(AccountActivity.EXTRA_ACCOUNT))
                    assertEquals(sibling.creationId,
                        replacementActivity.intent.getStringExtra(AccountActivity.EXTRA_CREATION_ID))
                    assertFalse("replacement AccountActivity rejected its exact route", replacementActivity.isFinishing)
                    assertEquals(1, fake.removeCalls)
                    assertEquals(sibling.account, ActiveAccountManager.getActiveAccount(target.context))
                } finally {
                    launched?.finish()
                    instrumentation.removeMonitor(monitor)
                }
            }
        } finally {
            CurrentAccountSignOutViewModel.seamsFactory = null
            sibling.close()
            target.close()
        }
    }

    @Test fun signOutWithUnsyncedNotesAsksTwiceBeforeItRemovesAnything() {
        val fixture = Fixture("unsynced")
        val fake = UnsyncedNotesSeams(fixture.account, fixture.creationId)
        CurrentAccountSignOutViewModel.seamsFactory = { _, _, _ -> fake }
        // With Notes on, Sync now asks the Notes job for a run. The job's work is replaced here, so no
        // Notes run reaches a server or a network: what is checked is whether a run was asked for,
        // and which kind. Each run is noted as: did the user start it, and its request id.
        val runs = java.util.concurrent.CopyOnWriteArrayList<Pair<Boolean, String?>>()
        NotesSyncCoordinator.runnerOverride = { _, _, _, request -> runs += request.manual to request.requestId }
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val count = UnsyncedNotesSignOutPolicy.Step.COUNT
        val discard = UnsyncedNotesSignOutPolicy.Step.DISCARD
        val positive = android.content.DialogInterface.BUTTON_POSITIVE
        val neutral = android.content.DialogInterface.BUTTON_NEUTRAL
        val negative = android.content.DialogInterface.BUTTON_NEGATIVE
        val checkFailed = fixture.context.getString(R.string.notes_sign_out_check_failed)
        fun dialogOf(activity: AccountActivity, step: UnsyncedNotesSignOutPolicy.Step): androidx.appcompat.app.AlertDialog? {
            activity.supportFragmentManager.executePendingTransactions()
            val fragment = activity.supportFragmentManager.findFragmentByTag(UnsyncedNotesDialogFragment.tag(step))
            return (fragment as? androidx.fragment.app.DialogFragment)?.dialog as? androidx.appcompat.app.AlertDialog
        }
        fun message(dialog: androidx.appcompat.app.AlertDialog) =
            dialog.findViewById<android.widget.TextView>(android.R.id.message)!!.text.toString()
        fun snackbarText(activity: AccountActivity) =
            activity.findViewById<android.widget.TextView>(com.google.android.material.R.id.snackbar_text)?.text?.toString()
        fun signOutFromDrawer(scenario: ActivityScenario<AccountActivity>) {
            scenario.onActivity { activity ->
                activity.onNavigationItemSelected(requireNotNull(
                    activity.findViewById<com.google.android.material.navigation.NavigationView>(R.id.nav_view).menu.findItem(R.id.nav_logout)))
            }
            instrumentation.waitForIdleSync()
        }
        // AlertDialog posts the button's listener and its own dismissal to the main looper, so a tap
        // waits until the main thread is idle.
        fun tap(scenario: ActivityScenario<AccountActivity>, step: UnsyncedNotesSignOutPolicy.Step, button: Int) {
            scenario.onActivity { activity -> dialogOf(activity, step)!!.getButton(button).performClick() }
            instrumentation.waitForIdleSync()
        }
        fun assertNoDialog(scenario: ActivityScenario<AccountActivity>) {
            scenario.onActivity { activity -> assertNull(dialogOf(activity, count)); assertNull(dialogOf(activity, discard)) }
        }
        try {
            ActivityScenario.launch<AccountActivity>(AccountActivity.newIntent(fixture.context, fixture.account)).use { scenario ->
                // 1. The count, with Sync now, Cancel, and going on without syncing.
                signOutFromDrawer(scenario)
                scenario.onActivity { activity ->
                    val shown = requireNotNull(dialogOf(activity, count)) { "the count dialog" }
                    assertEquals(activity.resources.getQuantityString(R.plurals.notes_sign_out_unsynced, 2, 2), message(shown))
                    assertEquals(activity.getString(R.string.account_synchronize_now), shown.getButton(positive).text.toString())
                    assertEquals(activity.getString(R.string.notes_sign_out_without_syncing), shown.getButton(neutral).text.toString())
                    assertEquals(activity.getString(android.R.string.cancel), shown.getButton(negative).text.toString())
                }

                // 2. Cancel removes nothing and confirms nothing.
                tap(scenario, count, negative)
                assertNoDialog(scenario)
                assertNull(fake.confirmed); assertEquals(0, fake.removeCalls)

                // 3. Sync now with Notes off (the fixture's state): the dialog closes, no run is asked
                // for, nothing is confirmed, and the check is not asked.
                signOutFromDrawer(scenario)
                tap(scenario, count, positive)
                assertTrue(NotesSyncCoordinator.drainForTesting(10_000))
                assertTrue("Notes is off, so no run was asked for: $runs", runs.isEmpty())
                assertNoDialog(scenario)
                assertNull(fake.confirmed); assertEquals(0, fake.removeCalls); assertEquals(0, fake.gateCalls)

                // 4. Sync now with Notes on: one run the user started, with the request id that Sync
                // now recorded for this account generation.
                check(AccountSettings.writeNotesEnabled(fixture.manager, fixture.account, true))
                signOutFromDrawer(scenario)
                tap(scenario, count, positive)
                waitUntil("the Notes run that Sync now asked for") { runs.any { it.first } }
                assertTrue(NotesSyncCoordinator.drainForTesting(10_000))
                val started = runs.filter { it.first }
                assertEquals("one run the user started: $runs", 1, started.size)
                val recorded = SyncStatusStore(fixture.context).let {
                    it.status(it.identity(fixture.account, fixture.creationId), SyncStatusStore.Service.NOTES).activeRequestId
                }
                assertNotNull("Sync now recorded a request", recorded)
                assertEquals("the run has the request id that Sync now recorded", recorded, started.single().second)
                assertNoDialog(scenario)
                assertNull(fake.confirmed); assertEquals(0, fake.removeCalls); assertEquals(0, fake.gateCalls)

                // 5. Going on without syncing leads to the separate confirmation. The count step confirmed nothing.
                signOutFromDrawer(scenario)
                tap(scenario, count, neutral)
                scenario.onActivity { activity ->
                    val shown = requireNotNull(dialogOf(activity, discard)) { "the discard confirmation" }
                    assertEquals(activity.resources.getQuantityString(R.plurals.notes_discard_text, 2, 2), message(shown))
                    assertEquals(activity.getString(R.string.notes_discard_and_sign_out), shown.getButton(positive).text.toString())
                    assertNull("the count dialog closed", dialogOf(activity, count))
                }
                assertNull(fake.confirmed); assertEquals(0, fake.removeCalls); assertEquals(0, fake.gateCalls)

                // 6. What the dialogs read moves on while the confirmation is open: the answer to it is
                // turned away, the count is shown again, and the check is not asked.
                fake.unsynced = UnsyncedNotes(2, 8, false)
                tap(scenario, discard, positive)
                scenario.onActivity { activity -> assertNotNull("the count is shown again", dialogOf(activity, count)) }
                assertEquals("the stale answer was turned away before the gate", 0, fake.gateCalls)
                assertNull(fake.confirmed); assertEquals(0, fake.removeCalls)

                // 7. What the check reads moves on after the answer: the gate refuses, the screen does not
                // treat that as a failed sign-out, and the count comes back with no tap. On API 21 the
                // dashboard shows a snackbar of its own from the start (master sync is off there), so the
                // snackbar text is read before the tap and compared after.
                tap(scenario, count, neutral)
                scenario.onActivity { activity -> assertNotNull(dialogOf(activity, discard)) }
                var before: String? = null
                scenario.onActivity { activity -> before = snackbarText(activity) }
                fake.gateSees = UnsyncedNotes(2, 9, false)
                tap(scenario, discard, positive)
                waitUntil("the count dialog after the refused sign-out") {
                    var shown = false
                    scenario.onActivity { activity -> shown = dialogOf(activity, count) != null }
                    shown
                }
                assertEquals(1, fake.gateCalls); assertEquals(0, fake.removeCalls); assertNull(fake.confirmed)
                scenario.onActivity { activity ->
                    val after = snackbarText(activity)
                    assertFalse("a refusal is not shown as a failed sign-out",
                        after == activity.getString(R.string.account_sign_out_failed))
                    assertEquals("the snackbar is the one from before the refused tap", before, after)
                }
                fake.gateSees = null
                // A sign-out that the gate refuses does not count as started. Nothing is confirmed, so
                // this begin() is refused in the same call.
                scenario.onActivity { activity ->
                    val model = androidx.lifecycle.ViewModelProvider(activity)[CurrentAccountSignOutViewModel::class.java]
                    model.begin()
                    assertFalse("a refused sign-out does not count as started", model.hasStarted())
                }
                instrumentation.waitForIdleSync()
                scenario.onActivity { activity ->
                    assertEquals("the screen acknowledged the refusal", CurrentAccountSignOutState.Idle,
                        androidx.lifecycle.ViewModelProvider(activity)[CurrentAccountSignOutViewModel::class.java].currentState())
                }

                // 8. The check itself cannot run: the sign-out is refused with a message of its own, on
                // the screen, and the two dialogs do not come back by themselves.
                fake.gateThrows = true
                tap(scenario, count, neutral)
                tap(scenario, discard, positive)
                waitUntil("the message for a check that could not run, on the screen") {
                    var shown = false
                    scenario.onActivity { activity ->
                        val view = activity.findViewById<android.widget.TextView>(com.google.android.material.R.id.snackbar_text)
                        shown = view != null && view.isShown && view.text.toString() == checkFailed
                    }
                    shown
                }
                assertNoDialog(scenario)
                assertEquals(3, fake.gateCalls); assertEquals(0, fake.removeCalls); assertNull(fake.confirmed)
                fake.gateThrows = false

                // 9. The confirmation survives a recreation, and its own button confirms and starts the
                // removal. After the recreation no count dialog is open and the snackbar is not the check
                // message.
                signOutFromDrawer(scenario)
                tap(scenario, count, neutral)
                scenario.recreate()
                instrumentation.waitForIdleSync()
                scenario.onActivity { activity ->
                    assertNull("no count dialog after the recreation", dialogOf(activity, count))
                    assertNotEquals("not the check message after the recreation", checkFailed, snackbarText(activity))
                }
                tap(scenario, discard, positive)
                assertEquals(UnsyncedNotes(2, 8, false), fake.confirmed)
                assertEquals(1, fake.removeCalls)
                assertEquals(4, fake.gateCalls)
            }
        } finally {
            // Notes goes off first, so a platform sync of this account that ends now asks for no Notes
            // run. Then what is queued is canceled and waited for, and only then is the stand-in taken
            // away.
            AccountSettings.writeNotesEnabled(fixture.manager, fixture.account, false)
            NotesSyncCoordinator.cancelAccount(fixture.account.type, fixture.account.name)
            NotesSyncCoordinator.drainForTesting(10_000)
            NotesSyncCoordinator.runnerOverride = null
            CurrentAccountSignOutViewModel.seamsFactory = null
            // The stand-in run never closes the request that Sync now recorded.
            SyncStatusStore(fixture.context).let { it.clear(it.identity(fixture.account, fixture.creationId)) }
            fixture.close()
        }
    }

    private class DelayedFailureSeams(account: Account) : CurrentAccountSignOutCoordinator.Seams {
        private val identity = ExactAccountIdentity(account.type, account.name, "failure-generation")
        var removeCalls = 0
        var destructiveCleanupCalls = 0
        var callback: ((Boolean) -> Unit)? = null
        override fun snapshot() = CurrentAccountSignOutSnapshot(identity, emptyList(), listOf(identity))
        override fun cancelSync(identity: Pair<String, String>) = Unit
        override fun removeMain(main: ExactAccountIdentity, callback: (Boolean) -> Unit) {
            removeCalls++
            this.callback = callback
        }
        override fun mainGenerationAbsent(main: ExactAccountIdentity) = false
        override fun clearCache(main: ExactAccountIdentity) = true.also { destructiveCleanupCalls++ }
        override fun clearStatus(main: ExactAccountIdentity) = true.also { destructiveCleanupCalls++ }
        override fun clearPendingNotes(main: ExactAccountIdentity) = true.also { destructiveCleanupCalls++ }
        override fun reconcileActive(main: ExactAccountIdentity, replacement: ExactAccountIdentity?) =
            ActiveAccountReconciliation(true, replacement).also { destructiveCleanupCalls++ }
        override fun removeAndVerifyChildren(snapshot: CurrentAccountSignOutSnapshot, callback: (Boolean) -> Unit) {
            destructiveCleanupCalls++
            callback(true)
        }
        override fun unsyncedNotes(main: ExactAccountIdentity) = UnsyncedNotes.NOTHING
        override fun confirmNotesDiscard(main: ExactAccountIdentity, shown: UnsyncedNotes) = Unit
        override fun withdrawNotesDiscard(main: ExactAccountIdentity) = Unit
        override fun notesDiscardConfirmed(main: ExactAccountIdentity) = true
    }

    private class DelayedSuccessSeams(
        private val context: Context,
        target: Account,
        generation: String,
        private val siblingAccount: Account,
        private val sibling: ExactAccountIdentity,
    ) : CurrentAccountSignOutCoordinator.Seams {
        private val targetIdentity = ExactAccountIdentity(target.type, target.name, generation)
        var absent = false
        var removeCalls = 0
        var callback: ((Boolean) -> Unit)? = null
        override fun snapshot() = CurrentAccountSignOutSnapshot(targetIdentity, emptyList(), listOf(targetIdentity, sibling))
        override fun cancelSync(identity: Pair<String, String>) = Unit
        override fun removeMain(main: ExactAccountIdentity, callback: (Boolean) -> Unit) {
            removeCalls++
            this.callback = callback
        }
        override fun mainGenerationAbsent(main: ExactAccountIdentity) = absent
        override fun clearCache(main: ExactAccountIdentity) = true
        override fun clearStatus(main: ExactAccountIdentity) = true
        override fun clearPendingNotes(main: ExactAccountIdentity) = true
        override fun reconcileActive(main: ExactAccountIdentity, replacement: ExactAccountIdentity?) =
            ActiveAccountReconciliation(ActiveAccountManager.setActiveAccount(context, siblingAccount), sibling)
        override fun removeAndVerifyChildren(snapshot: CurrentAccountSignOutSnapshot, callback: (Boolean) -> Unit) = callback(true)
        override fun unsyncedNotes(main: ExactAccountIdentity) = UnsyncedNotes.NOTHING
        override fun confirmNotesDiscard(main: ExactAccountIdentity, shown: UnsyncedNotes) = Unit
        override fun withdrawNotesDiscard(main: ExactAccountIdentity) = Unit
        override fun notesDiscardConfirmed(main: ExactAccountIdentity) = true
    }

    /**
     * Seams that report unsynced Notes changes and never answer the removal, so the sign-out stays
     * in progress and the screen stays. What the check reads can be set apart from what the dialogs
     * read, the check can be made to throw, and it counts how often it is asked. The cleanup steps,
     * the pending store's included, answer yes and touch nothing.
     */
    private class UnsyncedNotesSeams(account: Account, generation: String) : CurrentAccountSignOutCoordinator.Seams {
        private val identity = ExactAccountIdentity(account.type, account.name, generation)
        @Volatile var unsynced = UnsyncedNotes(2, 7, false)
        /** What the check reads in place of [unsynced], when set. */
        @Volatile var gateSees: UnsyncedNotes? = null
        @Volatile var gateThrows = false
        @Volatile var gateCalls = 0
        @Volatile var confirmed: UnsyncedNotes? = null
        @Volatile var removeCalls = 0
        override fun snapshot() = CurrentAccountSignOutSnapshot(identity, emptyList(), listOf(identity))
        override fun cancelSync(identity: Pair<String, String>) = Unit
        override fun removeMain(main: ExactAccountIdentity, callback: (Boolean) -> Unit) { removeCalls++ }
        override fun mainGenerationAbsent(main: ExactAccountIdentity) = false
        override fun clearCache(main: ExactAccountIdentity) = true
        override fun clearStatus(main: ExactAccountIdentity) = true
        override fun clearPendingNotes(main: ExactAccountIdentity) = true
        override fun reconcileActive(main: ExactAccountIdentity, replacement: ExactAccountIdentity?) =
            ActiveAccountReconciliation(true, replacement)
        override fun removeAndVerifyChildren(snapshot: CurrentAccountSignOutSnapshot, callback: (Boolean) -> Unit) = callback(true)
        override fun unsyncedNotes(main: ExactAccountIdentity) = unsynced
        override fun confirmNotesDiscard(main: ExactAccountIdentity, shown: UnsyncedNotes) { confirmed = shown }
        override fun withdrawNotesDiscard(main: ExactAccountIdentity) { confirmed = null }
        override fun notesDiscardConfirmed(main: ExactAccountIdentity): Boolean {
            gateCalls++
            if (gateThrows) throw IllegalStateException("cannot look")
            val now = gateSees ?: unsynced
            return !now.anything || confirmed?.covers(now) == true
        }
    }

    private fun removeAccountAndWait(manager: AccountManager, account: Account) {
        if (account !in manager.getAccountsByType(account.type)) return
        val removed = CountDownLatch(1)
        AndroidCompat.removeAccount(manager, account) { removed.countDown() }
        assertTrue("account removal callback timed out", removed.await(10, TimeUnit.SECONDS))
        assertFalse("account row remained after confirmed removal", account in manager.getAccountsByType(account.type))
    }

    private inner class Fixture(private val label: String) : AutoCloseable {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val manager = AccountManager.get(context)
        val account = Account("$label-${System.nanoTime()}@example.invalid", App.accountType)
        val creationId = "$label-generation"
        private val previousBootstrap = App.postLoginBootstrapSucceeded
        init {
            check(manager.addAccountExplicitly(account, null, null))
            AccountSettings.setUserData(manager, account, URI("https://example.invalid/"), account.name)
            check(AccountSettings.writeVerified(manager, account, AccountSettings.KEY_CREATION_ID, creationId))
            check(AccountSettings.writeVerified(manager, account, AccountSettings.KEY_LIMITED_INTEGRATIONS, "true"))
            check(AccountSettings.writeSetupState(manager, account, PostLoginSetupState.COMPLETE))
            App.postLoginBootstrapSucceeded = true
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                InstrumentationRegistry.getInstrumentation().uiAutomation
                    .executeShellCommand("pm grant ${context.packageName} ${Manifest.permission.POST_NOTIFICATIONS}").close()
            }
        }
        fun use(block: (Account) -> Unit) = try { block(account) } finally { close() }
        override fun close() {
            App.postLoginBootstrapSucceeded = previousBootstrap
            removeAccountAndWait(manager, account)
            ActiveAccountManager.clearActiveAccount(context)
        }
    }
}
