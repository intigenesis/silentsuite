package io.silentsuite.sync.notes

import android.accounts.Account
import android.accounts.AccountManager
import android.content.Context
import com.etebase.client.Collection
import com.etebase.client.CollectionManager
import com.etebase.client.FetchOptions
import com.etebase.client.exceptions.ConnectionException
import com.etebase.client.exceptions.NotFoundException
import com.etebase.client.exceptions.PermissionDeniedException
import com.etebase.client.exceptions.TemporaryServerErrorException
import com.etebase.client.exceptions.UnauthorizedException
import io.silentsuite.sync.AccountSettings
import io.silentsuite.sync.App
import io.silentsuite.sync.Constants
import io.silentsuite.sync.EtebaseLocalCache
import io.silentsuite.sync.HttpClient
import io.silentsuite.sync.billing.BillingManager
import io.silentsuite.sync.log.Logger
import io.silentsuite.sync.syncadapter.CollectionListRefresh
import io.silentsuite.sync.syncadapter.SyncStatusStore
import io.silentsuite.sync.syncadapter.syncConditionsAllow
import io.silentsuite.sync.ui.setup.ExactAccountRouting
import java.util.UUID
import java.util.logging.Level

/**
 * One Notes sync run: refresh the account-wide collection list, then pull every notebook's items
 * into the local cache. Read-only in this slice (no push), so a read-only notebook needs no
 * special handling here. Every private read is guarded by the exact account generation, and the
 * outcome is recorded into [SyncStatusStore] under the NOTES service exactly like an adapter would.
 */
internal object NotesSyncRunner {
    /** @property manual user-initiated runs ignore the Wi-Fi-only restriction, like manual adapter syncs. */
    data class Request(val requestId: String?, val manual: Boolean)

    fun run(context: Context, account: Account, creationId: String, request: Request) {
        val appContext = context.applicationContext
        val manager = AccountManager.get(appContext)
        fun exactGenerationStillCurrent() =
            ExactAccountRouting.validate(account, creationId, App.accountType, manager) != null
        if (!exactGenerationStillCurrent()) return

        val store = SyncStatusStore(appContext)
        val identity = store.identity(account, creationId)
        val attemptId = UUID.randomUUID().toString()
        val admission = store.beginAttemptResult(identity, SyncStatusStore.Service.NOTES, attemptId,
            System.currentTimeMillis(), request.requestId)
        if (admission == SyncStatusStore.MutationResult.REJECTED) {
            Logger.log.info("Notes sync skipped: another request owns the lifecycle")
            return
        }
        fun finishWithoutOutcome() = store.finishWithoutOutcomeResult(identity, SyncStatusStore.Service.NOTES, attemptId)
        fun recordFailure(category: SyncStatusStore.FailureCategory) = store.recordFailureResult(
            identity, SyncStatusStore.Service.NOTES, attemptId, request.requestId, category, System.currentTimeMillis())
        fun recordSuccess() = store.recordSuccessResult(
            identity, SyncStatusStore.Service.NOTES, attemptId, request.requestId, System.currentTimeMillis())

        try {
            if (!AccountSettings.notesEnabled(manager, account)) {
                Logger.log.info("Notes sync skipped: Notes is off for this account")
                finishWithoutOutcome()
                return
            }
            if (!BillingManager.getInstance().isSyncAllowed(appContext, account)) {
                Logger.log.info("Notes sync skipped: subscription inactive")
                finishWithoutOutcome()
                return
            }
            if (!exactGenerationStillCurrent()) { finishWithoutOutcome(); return }
            val settings = AccountSettings(appContext, account)
            if (!request.manual && !syncConditionsAllow(appContext, settings)) {
                finishWithoutOutcome()
                return
            }
            if (!exactGenerationStillCurrent()) { finishWithoutOutcome(); return }

            val outcome = HttpClient.Builder(appContext, settings).setForeground(false).build().use { httpClient ->
                CollectionListRefresh.run(appContext, account, settings, httpClient.okHttpClient, forceRefresh = false)
                if (!exactGenerationStillCurrent()) { finishWithoutOutcome(); return }

                val cache = EtebaseLocalCache.getInstance(appContext, account.name)
                val etebase = EtebaseLocalCache.getEtebase(appContext, httpClient.okHttpClient, settings)
                val colMgr = etebase.collectionManager
                // The fetch needs only each notebook's uid and cursor, so notebook metadata is never
                // decoded here: one notebook another app wrote in a shape this client cannot decode
                // must not stop the others from syncing.
                val notebooks = synchronized(cache) {
                    cache.collections(colMgr, type = Constants.ETEBASE_TYPE_NOTES)
                }
                fetchEachNotebook(notebooks, ::exactGenerationStillCurrent) { notebook ->
                    fetchNotebookItems(cache, colMgr, notebook)
                }
            }
            if (outcome == NotebooksOutcome.STALE || !exactGenerationStillCurrent()) { finishWithoutOutcome(); return }
            if (outcome == NotebooksOutcome.SOME_FAILED) recordFailure(SyncStatusStore.FailureCategory.UNKNOWN) else recordSuccess()
        } catch (e: InterruptedException) {
            Logger.log.info("Notes sync cancelled")
            finishWithoutOutcome()
        } catch (e: UnauthorizedException) {
            Logger.log.log(Level.WARNING, "Notes sync could not authenticate", e)
            recordFailure(SyncStatusStore.FailureCategory.AUTHENTICATION)
        } catch (e: TemporaryServerErrorException) {
            Logger.log.log(Level.WARNING, "Notes sync hit a temporary server error", e)
            recordFailure(SyncStatusStore.FailureCategory.NETWORK)
        } catch (e: ConnectionException) {
            Logger.log.log(Level.WARNING, "Notes sync could not reach the server", e)
            recordFailure(SyncStatusStore.FailureCategory.NETWORK)
        } catch (e: Exception) {
            if (Thread.currentThread().isInterrupted) {
                Logger.log.info("Notes sync cancelled")
                finishWithoutOutcome()
            } else {
                Logger.log.log(Level.SEVERE, "Notes sync failed", e)
                recordFailure(SyncStatusStore.FailureCategory.UNKNOWN)
            }
        } catch (e: Error) {
            // An OutOfMemoryError from decrypting a large notebook page must still close the
            // attempt, or the dashboard shows Notes as syncing until the interruption window expires.
            Logger.log.log(Level.SEVERE, "Notes sync failed with an error", e)
            recordFailure(SyncStatusStore.FailureCategory.UNKNOWN)
        }
    }

    /** What one notebook's failed fetch means for the rest of the run. */
    internal enum class NotebookFailure {
        /**
         * Credentials, connectivity, an unavailable server, or a permission denial: every other
         * notebook would fail the same way. The server answers a read with 403 only for the whole
         * account (for example a user no longer in the LDAP directory), never for one notebook.
         */
        ABORT_RUN,

        /** The notebook was deleted or unshared after the list refresh (the server answers 404); the next refresh drops it. */
        LOST_ACCESS,

        /** A problem with this notebook alone: the others still sync, and the run is recorded as failed. */
        NOTEBOOK_FAILED,
    }

    internal fun notebookFailure(e: Exception): NotebookFailure = when (e) {
        is UnauthorizedException, is PermissionDeniedException, is ConnectionException,
        is TemporaryServerErrorException -> NotebookFailure.ABORT_RUN
        is NotFoundException -> NotebookFailure.LOST_ACCESS
        else -> NotebookFailure.NOTEBOOK_FAILED
    }

    internal enum class NotebooksOutcome { ALL_FETCHED, SOME_FAILED, STALE }

    /**
     * Fetches each notebook in turn so one notebook cannot stop the others: only failures that
     * would hit every notebook, and cancellation, end the run by propagating. STALE means the exact
     * account generation went away between notebooks.
     */
    internal fun <T> fetchEachNotebook(notebooks: List<T>, stillCurrent: () -> Boolean, fetch: (T) -> Unit): NotebooksOutcome {
        var failed = 0
        for (notebook in notebooks) {
            if (Thread.interrupted()) throw InterruptedException()
            if (!stillCurrent()) return NotebooksOutcome.STALE
            try {
                fetch(notebook)
            } catch (e: InterruptedException) {
                throw e
            } catch (e: Exception) {
                when (notebookFailure(e)) {
                    NotebookFailure.ABORT_RUN -> throw e
                    NotebookFailure.LOST_ACCESS ->
                        Logger.log.info("Skipping a notebook this account can no longer read: ${e.javaClass.name}")
                    NotebookFailure.NOTEBOOK_FAILED -> {
                        // A cancellation can surface as any exception; let the run record it as one.
                        if (Thread.currentThread().isInterrupted) throw e
                        Logger.log.log(Level.WARNING, "A notebook could not be synced; continuing with the others", e)
                        failed++
                    }
                }
            }
        }
        return if (failed > 0) NotebooksOutcome.SOME_FAILED else NotebooksOutcome.ALL_FETCHED
    }

    /**
     * Mirrors the adapters' item fetch: skip when the notebook's cursor is unchanged, else page until
     * done. The cached copy is compared by revision only and never decoded, so a note whose metadata
     * this client cannot decode cannot fail the page and pin the cursor.
     */
    private fun fetchNotebookItems(cache: EtebaseLocalCache, colMgr: CollectionManager, notebook: Collection) {
        val colUid = notebook.uid
        val itemMgr = colMgr.getItemManager(notebook)
        var stoken = synchronized(cache) { cache.collectionLoadStoken(colUid) }
        if (notebook.stoken == stoken) {
            Logger.log.fine("Notebook unchanged; skipping item fetch")
            return
        }
        do {
            if (Thread.interrupted()) throw InterruptedException()
            val itemList = itemMgr.list(FetchOptions().stoken(stoken))
            synchronized(cache) {
                for (item in itemList.data) {
                    if (cache.itemEtag(itemMgr, colUid, item.uid) != item.etag) {
                        cache.itemSet(itemMgr, colUid, item)
                    }
                }
                itemList.stoken?.let { cache.collectionSaveStoken(colUid, it) }
            }
            stoken = itemList.stoken
        } while (!itemList.isDone)
    }
}
