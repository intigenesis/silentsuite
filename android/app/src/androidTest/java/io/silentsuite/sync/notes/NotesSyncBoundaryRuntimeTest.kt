package io.silentsuite.sync.notes

import android.accounts.Account
import android.accounts.AccountManager
import android.content.ContentResolver
import android.os.Bundle
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.etebase.client.Client
import com.etebase.client.ItemMetadata
import com.etebase.client.User
import io.silentsuite.sync.AccountSettings
import io.silentsuite.sync.App
import io.silentsuite.sync.Constants
import io.silentsuite.sync.EtebaseLocalCache
import io.silentsuite.sync.HttpClient
import io.silentsuite.sync.syncadapter.CollectionListRefresh
import io.silentsuite.sync.syncadapter.EXTRA_FORCE_COLLECTION_REFRESH
import io.silentsuite.sync.syncadapter.StaleSyncRunException
import io.silentsuite.sync.syncadapter.SyncStatusStore
import io.silentsuite.sync.syncadapter.requestSync
import io.silentsuite.sync.syncadapter.requestSyncDispatchOverride
import io.silentsuite.sync.ui.ExactAccountIdentity
import io.silentsuite.sync.utils.AndroidCompat
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.net.URI
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import kotlin.concurrent.thread

/**
 * The Notes job and the shared collection refresh, run for real (coordinator, runner, refresh,
 * Etebase binding, local cache, account store, status store) against an in-process stand-in for
 * the server ([FakeEtebaseServer]) that can hold a request in flight. Each case holds one request,
 * changes something while it is in flight, releases it, and checks exactly what was written.
 */
@RunWith(AndroidJUnit4::class)
class NotesSyncBoundaryRuntimeTest {
    private val context = InstrumentationRegistry.getInstrumentation().targetContext
    private val manager = AccountManager.get(context)
    private val fake = FakeEtebaseServer()
    private val names = mutableSetOf<String>()
    private val dispatched = CopyOnWriteArrayList<Bundle>()
    private var previousMasterSync = true

    /** The server side as the test sees it: the same Etebase user, talking to the same stand-in. */
    private val server by lazy {
        com.etebase.client.Account.restore(Client.create(OkHttpClient.Builder().addInterceptor(fake).build(), fake.baseUrl), session, null)
    }

    @Before fun setUp() {
        previousMasterSync = ContentResolver.getMasterSyncAutomatically()
        // No platform adapter sync may reach the stand-in server while a test holds a request.
        ContentResolver.setMasterSyncAutomatically(false)
        HttpClient.testInterceptor = fake
        requestSyncDispatchOverride = { _, _, extras -> dispatched += Bundle(extras) }
    }

    @After fun tearDown() {
        fake.releaseAll()
        for (name in names) NotesSyncCoordinator.cancelAccount(App.accountType, name)
        val drained = NotesSyncCoordinator.drainForTesting(30_000)
        // The process-wide hooks are restored whatever fails here, so one failure cannot spread to
        // the other classes that run in the same instrumentation process.
        val problems = mutableListOf<Throwable>()
        try {
            for (name in names) {
                runCatching { removeAccount(Account(name, App.accountType)) }.exceptionOrNull()?.let(problems::add)
                runCatching {
                    EtebaseLocalCache.clearUserCache(context, name)
                    forgetLastListing(name)
                }.exceptionOrNull()?.let(problems::add)
            }
        } finally {
            HttpClient.testInterceptor = null
            requestSyncDispatchOverride = null
            ContentResolver.setMasterSyncAutomatically(previousMasterSync)
        }
        assertTrue("the Notes thread finished", drained)
        problems.firstOrNull()?.let { throw it }
    }

    @Test fun forcedRefreshAfterAnInvitationReachesNotesWhicheverJobRunsFirst() {
        val account = newAccount("gen-force")
        val identity = ExactAccountIdentity(account.type, account.name, "gen-force")
        // A first Notes sync saves the list cursor that a later listing would start from.
        NotesSyncCoordinator.request(context, account, "gen-force", NotesSyncPolicy.Trigger.MANUAL)
        awaitSettled(identity)
        assertNotNull(listCursor(account))

        // 1. Notes runs first and alone: the adapters' forced syncs are dispatched but never run.
        val first = uploadNotebook("Accepted first")
        fake.acceptedFromInvitation(first)
        val before = status(account, "gen-force")
        var mark = fake.requests.size
        requestSync(context, account, forceCollectionRefresh = true)
        awaitSettled(identity)
        assertTrue("every adapter was asked to force its refresh",
            dispatched.isNotEmpty() && dispatched.all { it.getBoolean(EXTRA_FORCE_COLLECTION_REFRESH) })
        assertTrue("Notes listed from scratch, although its last listing was seconds ago", listings(mark).any(::fromScratch))
        assertTrue(first in cachedNotebooks(account))
        assertEquals("the accepted notebook's notes were fetched", fake.itemStoken(first), notebookCursor(account, first))
        status(account, "gen-force").let {
            assertTrue("the forced run itself succeeded: $it", it.lastSuccessAt!! > before.lastSuccessAt!!)
            assertNull(it.lastFailureAt)
            assertNull(it.activeAttemptId)
        }

        // 2. The acceptance arrives while an ordinary Notes run is already listing from the cursor.
        forgetLastListing(account.name)
        val second = uploadNotebook("Accepted during a run")
        fake.acceptedFromInvitation(second)
        val listing = fake.hold("POST", LIST)
        mark = fake.requests.size
        NotesSyncCoordinator.request(context, account, "gen-force", NotesSyncPolicy.Trigger.SCREEN_OPEN)
        listing.awaitArrival()
        requestSync(context, account, forceCollectionRefresh = true)
        listing.release()
        awaitSettled(identity)
        val listed = listings(mark)
        assertFalse("the run in progress listed from its cursor", fromScratch(listed.first()))
        assertTrue("the follow-up it queued listed from scratch", listed.drop(1).any(::fromScratch))
        assertEquals(fake.itemStoken(second), notebookCursor(account, second))

        // 3. For comparison, an adapter's forced refresh runs first, then an ordinary Notes run.
        val third = uploadNotebook("Accepted, adapter first")
        fake.acceptedFromInvitation(third)
        val settings = AccountSettings(context, account)
        HttpClient.Builder(context, settings).setForeground(false).build().use {
            CollectionListRefresh.run(context, account, settings, it.okHttpClient, forceRefresh = true, creationId = "gen-force")
        }
        NotesSyncCoordinator.request(context, account, "gen-force", NotesSyncPolicy.Trigger.SCREEN)
        awaitSettled(identity)
        assertEquals(fake.itemStoken(third), notebookCursor(account, third))

        // 4. A forced refresh still applies what is pending under the saved cursor. A listing from
        // scratch never reports a lost membership, so without that a notebook this account lost
        // since its last listing would stay cached for good.
        fake.removeMembership(first)
        requestSync(context, account, forceCollectionRefresh = true)
        awaitSettled(identity)
        assertFalse("the lost notebook was dropped", first in cachedNotebooks(account))
        assertTrue(second in cachedNotebooks(account))
    }

    @Test fun aSameNameAccountThatReplacedTheOldOneGetsNothingFromTheOldRunsInFlightRequests() {
        val name = "notes-boundary-${System.nanoTime()}@example.invalid"
        val account = newAccount("gen-1", name, discoveryKey = false)
        val notebook = uploadNotebook("Shared before the swap")

        // 1. The collection list is in flight when the account is removed and a same-name one added.
        val listing = fake.hold("POST", LIST)
        NotesSyncCoordinator.request(context, account, "gen-1", NotesSyncPolicy.Trigger.MANUAL)
        listing.awaitArrival()
        replaceAccount(account, "gen-2")
        listing.release()
        assertTrue(NotesSyncCoordinator.drainForTesting(30_000))
        assertNull("no list cursor was saved", listCursor(account))
        assertTrue("no collection was cached", cachedNotebooks(account).isEmpty())
        assertNull("the replacement's discovery key was not written", AccountSettings.collectionListTypes(manager, account))
        assertTrue("no listing time was recorded", lastListingKeys(name).isEmpty())
        assertEquals("the replacement's status is untouched", SyncStatusStore.Status(), status(account, "gen-2"))
        assertClosedWithoutOutcome(status(account, "gen-1"))

        // 2. A notebook's page is in flight when the account is replaced again.
        val page = fake.hold("GET", ITEMS)
        NotesSyncCoordinator.request(context, account, "gen-2", NotesSyncPolicy.Trigger.MANUAL)
        page.awaitArrival()
        assertTrue("the listing before the page was written while its generation was current", notebook in cachedNotebooks(account))
        replaceAccount(account, "gen-3")
        page.release()
        assertTrue(NotesSyncCoordinator.drainForTesting(30_000))
        assertNull("the page and its cursor were not written", notebookCursor(account, notebook))
        assertEquals("no further page was requested", 1, fake.requests.count { it.startsWith("GET collection/$notebook/item/") })
        assertEquals(SyncStatusStore.Status(), status(account, "gen-3"))
        assertClosedWithoutOutcome(status(account, "gen-2"))

        // 3. The shared refresh as the adapters call it, with no Notes job involved.
        val adapterListing = fake.hold("POST", LIST)
        val cursorBefore = listCursor(account)
        val failure = AtomicReference<Throwable?>()
        val settings = AccountSettings(context, account)
        val adapter = thread {
            try {
                HttpClient.Builder(context, settings).setForeground(false).build().use {
                    CollectionListRefresh.run(context, account, settings, it.okHttpClient, forceRefresh = true, creationId = "gen-3")
                }
            } catch (t: Throwable) {
                failure.set(t)
            }
        }
        adapterListing.awaitArrival()
        replaceAccount(account, "gen-4")
        adapterListing.release()
        adapter.join(30_000)
        assertTrue("the old refresh stopped as stale: ${failure.get()}", failure.get() is StaleSyncRunException)
        assertEquals("the list cursor did not move", cursorBefore, listCursor(account))
        assertNull(AccountSettings.collectionListTypes(manager, account))
        assertTrue(lastListingKeys(name).none { it.endsWith("gen-4") || it.endsWith("gen-3") })
    }

    @Test fun aCancelledOrSwitchedOffRunWritesNothingAfterItsInFlightRequest() {
        val account = newAccount("gen-stop")
        val identity = ExactAccountIdentity(account.type, account.name, "gen-stop")
        val notebook = uploadNotebook("Mine")

        // 1. Sign-out cancels the run while a notebook page is in flight. Like a blocking socket
        // read, the request does not stop for the interrupt; its answer arrives afterwards.
        val page = fake.hold("GET", ITEMS)
        NotesSyncCoordinator.request(context, account, "gen-stop", NotesSyncPolicy.Trigger.MANUAL)
        page.awaitArrival()
        NotesSyncCoordinator.cancelAccount(account.type, account.name)
        page.release()
        assertTrue(NotesSyncCoordinator.drainForTesting(30_000))
        assertNull("the page and its cursor were not written", notebookCursor(account, notebook))
        assertClosedWithoutOutcome(status(account, "gen-stop"))

        // The same when the network stack swallows the interrupt: then only the cancelled
        // schedule, not the thread's interrupt, can stop the write.
        val swallowed = fake.hold("GET", ITEMS, swallowInterrupt = true)
        NotesSyncCoordinator.request(context, account, "gen-stop", NotesSyncPolicy.Trigger.MANUAL)
        swallowed.awaitArrival()
        NotesSyncCoordinator.cancelAccount(account.type, account.name)
        swallowed.release()
        assertTrue(NotesSyncCoordinator.drainForTesting(30_000))
        assertNull("the page and its cursor were not written", notebookCursor(account, notebook))
        assertClosedWithoutOutcome(status(account, "gen-stop"))

        // The next run is not affected and writes normally.
        NotesSyncCoordinator.request(context, account, "gen-stop", NotesSyncPolicy.Trigger.MANUAL)
        awaitSettled(identity)
        assertEquals(fake.itemStoken(notebook), notebookCursor(account, notebook))
        val succeeded = status(account, "gen-stop").lastSuccessAt
        assertNotNull(succeeded)

        // 2. Notes is switched off while a page is in flight, with no cancellation reaching the run.
        fake.touchItems(notebook)
        forgetLastListing(account.name)
        val cursorBefore = notebookCursor(account, notebook)
        val secondPage = fake.hold("GET", ITEMS)
        NotesSyncCoordinator.request(context, account, "gen-stop", NotesSyncPolicy.Trigger.MANUAL)
        secondPage.awaitArrival()
        assertTrue(AccountSettings.writeNotesEnabled(manager, account, false))
        secondPage.release()
        awaitSettled(identity)
        assertEquals("the page was not written", cursorBefore, notebookCursor(account, notebook))
        status(account, "gen-stop").let {
            assertEquals("no success was recorded for the switched-off run", succeeded, it.lastSuccessAt)
            assertNull(it.lastFailureAt)
            assertNull(it.activeAttemptId)
        }

        // 3. Switched off the way the settings screen does it (flag, then cancel) during the listing.
        assertTrue(AccountSettings.writeNotesEnabled(manager, account, true))
        forgetLastListing(account.name)
        val another = uploadNotebook("Added elsewhere")
        val listCursorBefore = listCursor(account)
        val listing = fake.hold("POST", LIST)
        NotesSyncCoordinator.request(context, account, "gen-stop", NotesSyncPolicy.Trigger.MANUAL)
        listing.awaitArrival()
        assertTrue(AccountSettings.writeNotesEnabled(manager, account, false))
        NotesSyncCoordinator.cancel(identity)
        listing.release()
        assertTrue(NotesSyncCoordinator.drainForTesting(30_000))
        assertFalse("the listing was not written", another in cachedNotebooks(account))
        assertEquals(listCursorBefore, listCursor(account))
    }

    // ---- helpers ----

    private fun newAccount(generation: String, name: String = "notes-boundary-${System.nanoTime()}@example.invalid",
                           discoveryKey: Boolean = true): Account {
        val account = Account(name, App.accountType)
        names += name
        check(manager.addAccountExplicitly(account, null, null))
        AccountSettings.setUserData(manager, account, URI(fake.baseUrl), account.name)
        check(AccountSettings.writeVerified(manager, account, AccountSettings.KEY_CREATION_ID, generation))
        check(AccountSettings.writeNotesEnabled(manager, account, true))
        AccountSettings(context, account).etebaseSession = session
        // With the discovery key in place a listing starts from the saved cursor, as it does on a
        // device that has synced before; without it every listing starts from scratch.
        if (discoveryKey) check(AccountSettings.writeCollectionListTypes(manager, account, CollectionListRefresh.discoveryTypesKey))
        return account
    }

    /** Removes the account and adds a same-name one with a new generation, as a sign-out and sign-in would. */
    private fun replaceAccount(account: Account, generation: String) {
        removeAccount(account)
        newAccount(generation, account.name, discoveryKey = false)
    }

    private fun removeAccount(account: Account) {
        if (account !in manager.getAccountsByType(account.type)) return
        val removed = CountDownLatch(1)
        var confirmed = false
        AndroidCompat.removeAccount(manager, account) {
            confirmed = it
            removed.countDown()
        }
        assertTrue("account removal callback timed out", removed.await(10, TimeUnit.SECONDS))
        assertTrue("account removal was not confirmed", confirmed)
    }

    private fun uploadNotebook(name: String): String {
        val colMgr = server.collectionManager
        val notebook = colMgr.create(Constants.ETEBASE_TYPE_NOTES, ItemMetadata().apply { this.name = name }, "")
        colMgr.upload(notebook)
        return notebook.uid
    }

    private fun cache(account: Account) = EtebaseLocalCache.getInstance(context, account.name)

    private fun listCursor(account: Account): String? = cache(account).let { synchronized(it) { it.loadStoken() } }

    private fun notebookCursor(account: Account, uid: String): String? = cache(account).let { synchronized(it) { it.collectionLoadStoken(uid) } }

    private fun cachedNotebooks(account: Account): Set<String> = cache(account).let { cache ->
        synchronized(cache) { cache.collections(server.collectionManager, type = Constants.ETEBASE_TYPE_NOTES).mapTo(HashSet()) { it.uid } }
    }

    private fun status(account: Account, generation: String): SyncStatusStore.Status = SyncStatusStore(context).let {
        it.status(it.identity(account, generation), SyncStatusStore.Service.NOTES)
    }

    private fun assertClosedWithoutOutcome(status: SyncStatusStore.Status) {
        assertNull("no success: $status", status.lastSuccessAt)
        assertNull("no failure: $status", status.lastFailureAt)
        assertNull("no attempt left open: $status", status.activeAttemptId)
    }

    private fun listings(since: Int) = fake.requests.drop(since).filter { it.startsWith("POST collection/list_multi/") }

    private fun fromScratch(request: String) = !request.contains("stoken=")

    private fun lastListingKeys(name: String) = CollectionListRefresh.collectionLastFetchMap.keys.filter { it.startsWith("$name\u0000") }

    private fun forgetLastListing(name: String) {
        lastListingKeys(name).forEach { CollectionListRefresh.collectionLastFetchMap.remove(it) }
    }

    private fun awaitSettled(identity: ExactAccountIdentity) {
        waitUntil("Notes sync settled") { !NotesSyncCoordinator.isActive(identity) && !NotesSyncCoordinator.isPending(identity) }
        assertTrue(NotesSyncCoordinator.drainForTesting(30_000))
    }

    private fun waitUntil(description: String, timeoutMillis: Long = 30_000, predicate: () -> Boolean) {
        val deadline = android.os.SystemClock.uptimeMillis() + timeoutMillis
        while (android.os.SystemClock.uptimeMillis() < deadline) {
            if (predicate()) return
            android.os.SystemClock.sleep(50)
        }
        throw AssertionError("Timed out waiting for $description")
    }

    companion object {
        private val LIST = Regex("collection/list_multi/")
        private val ITEMS = Regex("collection/[^/]+/item/")

        /**
         * One Etebase signup for the whole class: its key derivation takes seconds, and the session
         * works against any stand-in server, which keeps no state about users.
         */
        private val session: String by lazy {
            val fake = FakeEtebaseServer()
            val client = Client.create(OkHttpClient.Builder().addInterceptor(fake).build(), fake.baseUrl)
            com.etebase.client.Account.signup(client, User("notes-boundary-${System.nanoTime()}", "notes-boundary@example.invalid"),
                "stand-in-server-only").save(null)
        }
    }
}
