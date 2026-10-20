package io.silentsuite.sync.ui

import android.accounts.AccountManager
import android.content.Context
import androidx.annotation.WorkerThread
import io.silentsuite.sync.AccountSettings
import io.silentsuite.sync.App
import io.silentsuite.sync.notes.NotesSyncCoordinator
import io.silentsuite.sync.notes.edit.PendingNotesStore
import io.silentsuite.sync.notes.edit.PendingNotesStore.Companion.StoreFolders
import io.silentsuite.sync.notes.edit.UnsyncedNotes
import io.silentsuite.sync.ui.UnsyncedNotesDiscardPolicy.Failure
import io.silentsuite.sync.ui.UnsyncedNotesDiscardPolicy.Result
import java.io.File
import java.io.IOException

/**
 * One Notes store the Settings way out shows: its folder in notes-pending, the present account it belongs to, if any,
 * and what it holds that has not synced. [answer] is null when reading the store threw: there is nothing to confirm.
 */
internal data class UnsyncedNotesRow(
    val directory: String,
    val owner: ExactAccountIdentity?,
    val answer: UnsyncedNotes?,
)

/**
 * Every store that holds unsynced text or cannot be read, in folder-name order. [othersUnlisted]: notes-pending could
 * not be listed, so stores other than the present accounts' could not be found. [unknownOthers]: notes-pending holds a
 * name that is not a store folder, of a kind this build cannot read.
 */
internal data class UnsyncedNotesListing(
    val rows: List<UnsyncedNotesRow>,
    val othersUnlisted: Boolean,
    val unknownOthers: Boolean,
)

/**
 * The Settings way out for unsynced Notes, below its screen: it finds every Notes store on this device and discards
 * one store that the user confirmed. It reads the account list, the store files and the discard permits only, so it
 * works offline and without a session, also for a store whose account is gone (the maintainer's condition of
 * 2026-10-18 on #709). It opens every store by its folder's name ([PendingNotesStore.forDirectory]), never keeps an
 * instance, and reads no acknowledgement as authority for anything.
 */
internal object UnsyncedNotesStores {
    /** The exact identity of an account row whose creation id [creationId] reads, or null when it does not. */
    internal fun identity(type: String, name: String, creationId: String?): ExactAccountIdentity? =
        creationId?.takeIf(String::isNotBlank)?.let { ExactAccountIdentity(type, name, it) }

    /**
     * The present accounts whose creation id reads. A read that throws gives none: the stores then show as not matched
     * to an account, and nothing is decided from that.
     */
    fun presentAccounts(context: Context): List<ExactAccountIdentity> = try {
        val manager = AccountManager.get(context)
        manager.getAccountsByType(App.accountType).mapNotNull {
            identity(it.type, it.name, manager.getUserData(it, AccountSettings.KEY_CREATION_ID))
        }
    } catch (e: Exception) {
        emptyList()
    }

    /** Every store on this device that holds anything or cannot be read. Never on the main thread. */
    @WorkerThread
    fun read(context: Context): UnsyncedNotesListing = read(context.noBackupFilesDir, presentAccounts(context),
        folders = { PendingNotesStore.storeDirectories(context) },
        open = { PendingNotesStore.forDirectory(context, it) })

    /**
     * [read] for the stores in [noBackup], with [accounts] present, the folders of notes-pending listed by [folders]
     * and each store opened by [open]. The folders are those notes-pending lists plus every present account's own,
     * so a listing that comes back short cannot hide a present account's store; a folder that is not there counts
     * nothing and is not made. While notes-pending cannot be listed and is not shown gone, only the present accounts'
     * folders are read. A store is kept while it holds anything or its read throws. The counts run no recovery and
     * write nothing in a store, though a count of a store that lists withdraws its acknowledgement
     * ([PendingNotesStore.unsynced]).
     */
    internal fun read(
        noBackup: File,
        accounts: List<ExactAccountIdentity>,
        folders: () -> StoreFolders = { PendingNotesStore.storeDirectories(noBackup) },
        open: (String) -> PendingNotesStore,
    ): UnsyncedNotesListing {
        val byDirectory = accounts.associateBy { directoryOf(noBackup, it) }
        val listed = try {
            folders()
        } catch (e: IOException) {
            null
        }
        val directories = (listed?.stores.orEmpty() + byDirectory.keys).distinct().sorted()
        val rows = directories.mapNotNull { directory ->
            val answer = try {
                open(directory).unsynced()
            } catch (e: Exception) {
                null
            }
            if (answer == null || answer.anything) UnsyncedNotesRow(directory, byDirectory[directory], answer) else null
        }
        return UnsyncedNotesListing(rows, othersUnlisted = listed == null, unknownOthers = listed?.unknown == true)
    }

    /**
     * The user's confirmation of [shown] for the store in [directory], a Retry when [retry]. Never on the main thread.
     * The store's account, when it is present, is found again now, and its Notes run is canceled first when it runs or
     * waits to.
     */
    @WorkerThread
    fun discard(context: Context, directory: String, label: String?, shown: UnsyncedNotes, retry: Boolean): Result {
        val noBackup = context.noBackupFilesDir
        val owner = presentAccounts(context).firstOrNull { directoryOf(noBackup, it) == directory }
        return discard(directory, label, shown, retry, owner,
            open = { PendingNotesStore.forDirectory(context, it) },
            running = { NotesSyncCoordinator.isActive(it) || NotesSyncCoordinator.isPending(it) },
            cancel = NotesSyncCoordinator::cancel)
    }

    /**
     * [discard], with the store opened by [open] and the Notes job asked through [running] and [cancel]:
     * 1. For a discard of a store whose account [owner] is present, that exact generation's Notes run is canceled
     *    when [running] says it runs or waits to. Only then, since a cancel always tells the job's listeners, and an
     *    acknowledgement deletes nothing, so it cancels nothing.
     * 2. The store is opened. A throw in 1 or 2 is a failure before this call deleted anything.
     * 3. The store discards what [shown] covers ([PendingNotesStore.discardStore]), under its own lock: it counts
     *    again, records the permit before any file goes, and refuses anything it was not shown. An
     *    IllegalStateException there is an instance that was dropped, before this call deleted anything; any other
     *    throw may come from anywhere.
     * 4. Only once the folder is gone, the instance is dropped, so the next opener gets a new one that takes saves.
     */
    internal fun discard(
        directory: String,
        label: String?,
        shown: UnsyncedNotes,
        retry: Boolean,
        owner: ExactAccountIdentity?,
        open: (String) -> PendingNotesStore,
        running: (ExactAccountIdentity) -> Boolean,
        cancel: (ExactAccountIdentity) -> Unit,
    ): Result {
        val store = try {
            if (shown.listed && owner != null && running(owner)) cancel(owner)
            open(directory)
        } catch (e: Exception) {
            return UnsyncedNotesDiscardPolicy.result(shown, retry, directory, label, Failure.BEFORE_DISCARD)
        }
        val ended = try {
            store.discardStore(shown)
        } catch (e: IllegalStateException) {
            return UnsyncedNotesDiscardPolicy.result(shown, retry, directory, label, Failure.BEFORE_DISCARD)
        } catch (e: Exception) {
            return UnsyncedNotesDiscardPolicy.result(shown, retry, directory, label, Failure.UNKNOWN)
        }
        if (ended == PendingNotesStore.DiscardOutcome.Done) store.unregister()
        return UnsyncedNotesDiscardPolicy.result(shown, retry, directory, label, ended)
    }

    private fun directoryOf(noBackup: File, account: ExactAccountIdentity) =
        PendingNotesStore.identityDir(noBackup, account.type, account.name, account.creationId).name
}
