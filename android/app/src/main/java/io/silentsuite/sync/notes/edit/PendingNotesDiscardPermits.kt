package io.silentsuite.sync.notes.edit

import java.io.File
import java.io.FileOutputStream
import java.io.IOException

/**
 * The user's confirmed discards of whole pending stores, one permit per store directory, recorded before
 * anything in a store that may hold unsynced text is deleted. A permit holds the answer of
 * [PendingNotesStore.unsynced] that the discard deletes, and covers the store for as long as
 * [UnsyncedNotes.covers] holds for what the store holds now. So a discard that stopped halfway, also across
 * a restart, can be finished under the same confirmation while the permit still covers what is left;
 * otherwise the user is asked again.
 *
 * Permits live in a root of their own (noBackupFilesDir/notes-discard, one "<store>.permit" per store
 * directory name). Not in the store, which the discard deletes, and not beside the stores in
 * notes-pending, so a permit stays readable when that directory cannot be listed.
 *
 * A permit never covers text saved after it: before a store hands out a new version it removes its
 * permit, and a removal that cannot be shown fails that write. Nothing here takes a lock; each store's
 * permit is recorded and removed under that store's lock.
 *
 * Whether a file is there is decided as in the store: by a read that worked, a deletion that answered
 * true, a listing that worked, or the presence check, which answers absent only when the system says the
 * path does not exist. File.exists() decides nothing.
 */
internal class PendingNotesDiscardPermits(
    val root: File,
    private val syncDirectory: (File) -> Unit,
    /** Replaces the target in one step or throws, as for the store. */
    private val rename: (File, File) -> Unit,
    /** The store's presence check: whether a path is there, [PendingNotesStore.Presence.UNKNOWN] when it fails. */
    private val presence: (File) -> PendingNotesStore.Presence,
) {
    /**
     * The permit of the store in directory [store], or null when there is none. A permit that is cut
     * short, fails its checksum, is in a newer format, names another store or cannot be read is no permit,
     * so the store it was for is not covered.
     */
    fun read(store: String): UnsyncedNotes? {
        val bytes = try {
            file(store).readBytes()
        } catch (e: IOException) {
            return null
        }
        val (named, shown) = (PendingCodec.decodePermit(bytes) as? PendingCodec.Decoded.Ok)?.value ?: return null
        return shown.takeIf { named == store }
    }

    /**
     * Records [shown] as the permit of the store in directory [store], in place of any earlier one. It is
     * written to "<name>.new", synced, renamed over "<name>" in one step ([rename]), and the root is synced,
     * so after a power loss the permit is there in full or not at all. The root is created on the way.
     * Throws when any step fails, and removes the temporary file it wrote; a failure after the rename leaves
     * the new permit in place.
     */
    fun record(store: String, shown: UnsyncedNotes) {
        val target = file(store)
        val tmp = File(root, target.name + NEW)
        try {
            createRoot()
            FileOutputStream(tmp).use { out ->
                out.write(PendingCodec.encodePermit(store, shown))
                out.fd.sync()
            }
            beforeRecordForTesting?.invoke(target)
            rename(tmp, target)
            syncDirectory(root)
        } catch (e: Throwable) {
            tmp.delete()
            throw e
        }
    }

    /**
     * Removes the permit of the store in directory [store], and returns only once it is shown not to be
     * there: removed with the root synced, or shown absent by two checks that agree, its presence check and
     * a listing of the root that does not hold it (or the root itself shown not to be there,
     * [PendingNotesStore.knownAbsent]). File.exists() is not asked: it answers false for a check that failed
     * as well. Anything else throws IOException, a root that cannot be listed included, so no caller takes a
     * permit that may still be there for gone.
     */
    fun remove(store: String) {
        val target = file(store)
        if (target.delete()) {
            syncDirectory(root)
            return
        }
        // Not there, or not removed: its own check and a listing tell which, and must agree.
        if (presenceOf(target) != PendingNotesStore.Presence.ABSENT) {
            throw IOException("could not show the discard permit of $store gone")
        }
        val listed = PendingNotesStore.listDirectory(root)
        if (listed == null) {
            if (PendingNotesStore.knownAbsent(root)) return
            throw IOException("could not list the discard permits")
        }
        if (listed.any { it.name == target.name }) throw IOException("could not remove the discard permit of $store")
    }

    private fun file(store: String): File {
        require(NAME.matches(store)) { "not a store directory name" }
        return File(root, store + PERMIT)
    }

    private fun presenceOf(file: File) = PendingNotesStore.presenceForTesting?.invoke(file) ?: presence(file)

    /**
     * Creates the root and any missing parent, one level at a time, and syncs the parent of each new level
     * and the root's own parent, so a permit committed in it cannot outlive, after a power loss, the
     * entries that lead to it. The root's parent is synced on every record, since a record before this one
     * may have created the root and then failed that sync. As in the store, a level whose check fails is
     * made like a missing one, and mkdir, or a directory that is there already, decides.
     */
    private fun createRoot() {
        val missing = generateSequence(root) { it.parentFile }
            .takeWhile { presenceOf(it) != PendingNotesStore.Presence.PRESENT }.toList().asReversed()
        for (level in missing) {
            if (!level.mkdir() && !level.isDirectory) throw IOException("could not create the discard permits")
        }
        (missing.map { it.parentFile } + listOfNotNull(root.parentFile)).distinct().forEach(syncDirectory)
    }

    companion object {
        /** The root's name, beside notes-pending in noBackupFilesDir. */
        const val ROOT = "notes-discard"
        private const val PERMIT = ".permit"
        private const val NEW = ".new"

        /** A store directory's name: one path level, never "." or "..". */
        private val NAME = Regex("^[A-Za-z0-9_-]{1,128}$")

        /** Test seam: a crash point after a new permit is written and synced, before its rename. */
        @Volatile internal var beforeRecordForTesting: ((File) -> Unit)? = null
    }
}
