package io.silentsuite.sync.notes

import android.os.Looper
import io.silentsuite.sync.notes.edit.PendingNotesStore
import java.io.File
import java.util.concurrent.atomic.AtomicInteger
import org.junit.Assert.assertTrue

/**
 * Records every call that reaches the pending Notes store's listing seam ([PendingNotesStore.listFilesForTesting]) or
 * its removal seam ([PendingNotesStore.beforeRemoveForTesting]) on the main thread, so a device case can show that a
 * screen reaches the store only off it: every listing the store makes, of its own folder, the folders above it and the
 * permits' root, and every file it removes, passes one of the two. It only records: the seams run inside a store call
 * on a worker thread, which rethrows anything, so an assertion there would end the app process. The test thread asks
 * [assertNoneOnMain].
 *
 * While it is installed, the probe holds both seams. A case that sets its own listing or removal sets [listing] or
 * [removal], never the store's seam, and the probe applies it as it is: a listing that answers null is one that
 * failed, and is never replaced by File.listFiles().
 */
internal class MainThreadStoreProbe {
    /** What a listing of the directory it is given answers in place of File.listFiles(); null is one that failed. */
    @Volatile var listing: ((File) -> Array<File>?)? = null

    /** Called with the file before each removal, once the probe has seen it. */
    @Volatile var removal: ((File) -> Unit)? = null

    /** How many listings and removals reached the probe, on any thread. */
    private val seen = AtomicInteger()

    /** What reached it on the main thread, in order. */
    private val onMain = ArrayList<String>()

    fun install() {
        PendingNotesStore.listFilesForTesting = { d ->
            record("a listing of ${d.name}")
            listing.let { if (it != null) it(d) else d.listFiles() }
        }
        PendingNotesStore.beforeRemoveForTesting = { file ->
            record("a removal of ${file.name}")
            removal?.invoke(file)
        }
    }

    /** Resets both seams of the store, and the case's own listing and removal. */
    fun uninstall() {
        PendingNotesStore.listFilesForTesting = null
        PendingNotesStore.beforeRemoveForTesting = null
        listing = null
        removal = null
    }

    /** Counts every call, and keeps the ones made on the main looper's thread. */
    private fun record(what: String) {
        seen.incrementAndGet()
        if (Thread.currentThread() === Looper.getMainLooper().thread) synchronized(onMain) { onMain += what }
    }

    /** Fails, naming each call, while any reached the store on the main thread, or while none reached the probe. */
    fun assertNoneOnMain() {
        val found = synchronized(onMain) { onMain.toList() }
        assertTrue("store calls on the main thread: $found", found.isEmpty())
        assertTrue("no listing or removal reached the probe, so it saw nothing", seen.get() > 0)
    }
}
