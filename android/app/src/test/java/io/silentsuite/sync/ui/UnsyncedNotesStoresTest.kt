package io.silentsuite.sync.ui

import io.silentsuite.sync.notes.edit.PendingCodec
import io.silentsuite.sync.notes.edit.PendingEntry
import io.silentsuite.sync.notes.edit.PendingNotesDiscardPermits
import io.silentsuite.sync.notes.edit.PendingNotesStore
import io.silentsuite.sync.notes.edit.PendingNotesStore.SaveOutcome
import io.silentsuite.sync.notes.edit.PendingNotesStore.SendOutcome
import io.silentsuite.sync.notes.edit.UnsyncedNotes
import io.silentsuite.sync.ui.UnsyncedNotesDiscardPolicy.Result
import io.silentsuite.sync.ui.UnsyncedNotesDiscardPolicy.Step
import io.silentsuite.sync.ui.UnsyncedNotesSignOutPolicy.Text
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.attribute.BasicFileAttributes

/**
 * The Settings way out for unsynced Notes, below its screen: which stores it finds and how it names them, and what a
 * confirmation in it does to a store. Real stores in a temporary no_backup folder, opened by folder name as the
 * screen opens them; the Notes job is a stand-in that records what was asked of it.
 */
class UnsyncedNotesStoresTest {
    @get:Rule val tmp = TemporaryFolder()

    private val noBackup by lazy { tmp.newFolder("no_backup") }
    private val pending get() = File(noBackup, "notes-pending")
    private val discards get() = File(noBackup, PendingNotesDiscardPermits.ROOT)

    /** Replaces the target in one step on Windows too, where these tests also run, as in PendingNotesStoreTest. */
    private val atomicMove: (File, File) -> Unit = { from, to ->
        java.nio.file.Files.move(from.toPath(), to.toPath(),
            java.nio.file.StandardCopyOption.ATOMIC_MOVE, java.nio.file.StandardCopyOption.REPLACE_EXISTING)
    }

    @After fun reset() {
        PendingNotesStore.beforeRemoveForTesting = null
        PendingNotesStore.beforeClearForTesting = null
        PendingNotesStore.listFilesForTesting = null
        PendingNotesStore.presenceForTesting = null
        PendingNotesStore.identityForTesting = null
        PendingNotesDiscardPermits.beforeRecordForTesting = null
        PendingNotesDiscardPermits.beforeDeleteForTesting = null
        PendingNotesDiscardPermits.onReadForTesting = null
        PendingNotesStore.resetForTesting()
    }

    private fun account(name: String, creationId: String = "gen-1") = ExactAccountIdentity(TYPE, name, creationId)

    private fun folderOf(owner: ExactAccountIdentity) =
        PendingNotesStore.identityDir(noBackup, owner.type, owner.name, owner.creationId).name

    private fun storeOf(owner: ExactAccountIdentity) =
        PendingNotesStore.forNoBackup(noBackup, owner.type, owner.name, owner.creationId, {}, atomicMove)

    /** The store in [folder], opened by its name as the screen opens it. */
    private fun opened(folder: String) = PendingNotesStore.forNoBackupDirectory(noBackup, folder, {}, atomicMove)

    /** Saves a note under each of [uids] in [owner]'s store, each with its notebook's copy, and gives its folder. */
    private fun saveIn(owner: ExactAccountIdentity, vararg uids: String): String {
        val store = storeOf(owner)
        for (uid in uids) {
            val outcome = store.saveLocal(uid, "b1", "r1", "text of $uid".toByteArray(), isCreate = false,
                notebookCopy = "key b1".toByteArray())
            assertTrue("$outcome", outcome is SaveOutcome.Saved)
        }
        return store.dir.name
    }

    private fun read(vararg accounts: ExactAccountIdentity, open: (String) -> PendingNotesStore = ::opened) =
        UnsyncedNotesStores.read(noBackup, accounts.toList(), open = open)

    /** What a discard asked of the Notes job and the store, in order. */
    private val calls = ArrayList<String>()

    /**
     * A discard as the screen asks for one; [ownerNow] is the account found again after a discard that finished. A
     * discard of the store that stopped in this process is read from memory, as on a device.
     */
    private fun discard(
        folder: String,
        shown: UnsyncedNotes,
        owner: ExactAccountIdentity? = null,
        retry: Boolean = false,
        running: Boolean = false,
        open: (String) -> PendingNotesStore = ::opened,
        ownerNow: () -> ExactAccountIdentity? = { owner },
    ): Result = UnsyncedNotesStores.discard(folder, owner?.name, shown, retry, owner,
        open = { calls += "open"; open(it) },
        running = { calls += "running ${it.name}"; running },
        cancel = { calls += "cancel ${it.name}" },
        ownerNow = ownerNow,
        stoppedBefore = { folder in PendingNotesStore.stoppedIn(noBackup) })

    /** The Settings screen's call of [UnsyncedNotesStores.discard], as it is wired on a device. */
    private fun deviceWiring() = File("src/main/java/io/silentsuite/sync/ui/UnsyncedNotesStores.kt").readText()
        .replace("\r\n", "\n").substringAfter("fun discard(context: Context", "").substringBefore("\n\n")

    private fun permitOf(folder: String) =
        PendingNotesDiscardPermits(discards, {}, atomicMove, PendingNotesStore.nioPresence).read(folder)

    /** The names in [d], from listings that must work: of [d], or of the folder above when that shows [d] gone. */
    private fun namesIn(d: File): Set<String> {
        val above = d.parentFile!!.list() ?: throw AssertionError("${d.parentFile!!.name} could not be listed")
        if (d.name !in above) return emptySet()
        return (d.list() ?: throw AssertionError("${d.name} could not be listed")).toSet()
    }

    /** Every file below [d] by its path there, with its bytes, found by listing alone: a file lists as nothing. */
    private fun filesUnder(d: File): Map<String, List<Byte>> {
        val found = HashMap<String, List<Byte>>()
        fun walk(dir: File, prefix: String) {
            for (name in dir.list() ?: throw AssertionError("${dir.name} could not be listed")) {
                val child = File(dir, name)
                if (child.list() == null) found[prefix + name] = child.readBytes().toList()
                else walk(child, "$prefix$name/")
            }
        }
        walk(d, "")
        return found
    }

    /** Each listing of a folder named in [folders] fails; every other listing works. */
    private fun unlisted(vararg folders: String) {
        PendingNotesStore.listFilesForTesting = { d -> if (d.name in folders) null else d.listFiles() }
    }

    /**
     * Stand-ins for lstat's inode numbers, which the JVM cannot read on every platform these tests run on: one per
     * folder that is there, and none for a path that lists nothing, such as a folder that was never made.
     */
    private fun identifyDirectories() {
        val inodes = HashMap<String, Long>()
        PendingNotesStore.identityForTesting = { d ->
            if (d.list() == null) null
            else PendingNotesStore.DirectoryIdentity(7, inodes.getOrPut(d.path) { inodes.size + 1L })
        }
    }

    // ---- what the screen finds ----

    @Test fun `a store takes an account's name only when its creation id matches`() {
        val alice = account("alice")
        val matched = saveIn(alice, "n1")
        val replaced = saveIn(account("bob", "gen-1"), "n1")
        val blank = saveIn(account("carol"), "n1")
        val gone = saveIn(account("dave"), "n1")
        // The account list as presentAccounts reads it: bob is there under another generation, and carol's creation
        // id cannot be read, so neither identity is there.
        assertNull(UnsyncedNotesStores.identity(TYPE, "carol", null))
        assertNull(UnsyncedNotesStores.identity(TYPE, "carol", " "))
        val present = listOfNotNull(UnsyncedNotesStores.identity(TYPE, "alice", "gen-1"),
            UnsyncedNotesStores.identity(TYPE, "bob", "gen-2"), UnsyncedNotesStores.identity(TYPE, "carol", ""))
        assertEquals(listOf(alice, account("bob", "gen-2")), present)

        val rows = read(*present.toTypedArray()).rows.associateBy { it.directory }
        assertEquals(setOf(matched, replaced, blank, gone), rows.keys)
        assertEquals(alice, rows.getValue(matched).owner)
        for (unmatched in listOf(replaced, blank, gone)) assertNull(unmatched, rows.getValue(unmatched).owner)
        assertTrue(rows.values.all { it.answer == UnsyncedNotes(1, 1, false, names = setOf("n1.note")) })
    }

    @Test fun `clean stores are left out and the others keep folder-name order`() {
        val withText = listOf("x", "y", "z").map { saveIn(account(it), "n1", "n2") }
        // Pushed: only the counter and the notebook copy are left. Never written: an empty folder.
        val pushedOwner = account("pushed")
        val pushed = storeOf(pushedOwner)
        pushed.saveLocal("n1", "b1", "r1", "t".toByteArray(), isCreate = false, notebookCopy = "key".toByteArray())
        assertEquals(SendOutcome.Done, pushed.completeSend("n1", pushed.beginSend("n1")!!.version, "r1",
            "landed".toByteArray()))
        val empty = folderOf(account("empty"))
        assertTrue(File(pending, empty).mkdir())

        val listing = read(account("x"), pushedOwner)
        assertEquals(withText.sorted(), listing.rows.map { it.directory })
        assertFalse(pushed.dir.name in listing.rows.map { it.directory })
        assertFalse(listing.othersUnlisted || listing.unknownOthers)
        assertEquals(listOf(Text.Counted(2)),
            listing.rows.map { UnsyncedNotesDiscardPolicy.rowText(it.answer) }.distinct())
        assertEquals(Text.Counted(6), UnsyncedNotesDiscardPolicy.hubSummary(listing))

        // With nothing found, the screen and the hub row have nothing to show.
        for (folder in withText) {
            assertEquals(Result.Done, discard(folder, read().rows.first { it.directory == folder }.answer!!))
        }
        val none = read(account("x"), pushedOwner)
        assertEquals(emptyList<UnsyncedNotesRow>(), none.rows)
        assertNull(UnsyncedNotesDiscardPolicy.hubSummary(none))
    }

    @Test fun `a short root listing does not hide a present account's store`() {
        val p = account("p")
        val folder = saveIn(p, "n1")
        val orphan = saveIn(account("orphan"), "n1")
        // notes-pending lists, but leaves the present account's folder out, as a listing that stopped partway can.
        PendingNotesStore.listFilesForTesting = { d ->
            val all = d.listFiles()
            if (all != null && d.name == "notes-pending") all.filter { it.name != folder }.toTypedArray() else all
        }
        val listing = read(p)
        assertEquals(listOf(folder, orphan).sorted(), listing.rows.map { it.directory })
        assertEquals(p, listing.rows.single { it.directory == folder }.owner)
        assertEquals(UnsyncedNotes(1, 1, false, names = setOf("n1.note")),
            listing.rows.single { it.directory == folder }.answer)
        assertFalse(listing.othersUnlisted)
        // A present account with no store adds no row: its folder, never made, holds nothing.
        assertEquals(2, read(p, account("never")).rows.size)
        assertFalse(folderOf(account("never")) in namesIn(pending))
    }

    @Test fun `a name that is not a store folder sets the unknown flag and gets no row`() {
        val folder = saveIn(account("a"), "n1")
        assertTrue(File(pending, "attachments").mkdir())
        assertTrue(File(pending, "$folder.old").createNewFile())
        val listing = read()
        assertEquals(listOf(folder), listing.rows.map { it.directory })
        assertTrue(listing.unknownOthers)
        assertFalse(listing.othersUnlisted)
        assertEquals("never a sum while something could not be read", Text.Uncounted,
            UnsyncedNotesDiscardPolicy.hubSummary(listing))
        // On its own it still keeps the hub row, with nothing to count.
        assertEquals(Result.Done, discard(folder, listing.rows.single().answer!!))
        val alone = read()
        assertEquals(emptyList<UnsyncedNotesRow>(), alone.rows)
        assertTrue(alone.unknownOthers)
        assertEquals(Text.Uncounted, UnsyncedNotesDiscardPolicy.hubSummary(alone))
    }

    @Test fun `a listed store that cannot be counted is offered as a discard that says so`() {
        // A file of a kind this build does not know, and a counter that cannot be read at all.
        val unknownKind = saveIn(account("a"), "n1")
        assertTrue(File(File(pending, unknownKind), "n2.draft").createNewFile())
        val damaged = saveIn(account("b"), "n1")
        val counter = File(File(pending, damaged), "sequence")
        assertTrue(counter.delete() && counter.mkdir())
        val rows = read().rows
        assertEquals(listOf(unknownKind, damaged).sorted(), rows.map { it.directory })
        for (row in rows) {
            val shown = row.answer!!
            assertTrue("$shown", shown.unreadable && shown.listed)
            assertEquals(Step.DISCARD, UnsyncedNotesDiscardPolicy.step(shown))
            assertEquals(Text.Uncounted, UnsyncedNotesDiscardPolicy.rowText(shown))
            // Confirmed as shown, it goes, its permit first.
            val permits = ArrayList<UnsyncedNotes?>()
            PendingNotesStore.beforeRemoveForTesting = { if (permits.isEmpty()) permits += permitOf(row.directory) }
            assertEquals(Result.Done, discard(row.directory, shown))
            assertEquals(listOf<UnsyncedNotes?>(shown), permits)
            assertFalse(row.directory in namesIn(pending))
        }
    }

    @Test fun `a store that cannot be listed is offered only as an acknowledgement, and asked again once it lists`() {
        identifyDirectories()
        val folder = saveIn(account("a"), "n1", "n2")
        val before = filesUnder(pending)
        unlisted(folder)
        val shown = read().rows.single().answer!!
        assertFalse(shown.listed)
        assertEquals(Step.ACKNOWLEDGE, UnsyncedNotesDiscardPolicy.step(shown))
        assertEquals(Text.Unlisted, UnsyncedNotesDiscardPolicy.rowText(shown))
        assertEquals(Result.Acknowledged, discard(folder, shown))
        assertEquals(setOf("$folder.acknowledgement"), namesIn(discards))

        // Once it lists, the acknowledgement goes, and the same confirmation leads to a discard with the real count.
        PendingNotesStore.listFilesForTesting = null
        val now = read().rows.single().answer!!
        assertEquals(UnsyncedNotes(2, 2, false, names = setOf("n1.note", "n2.note")), now)
        assertEquals(emptySet<String>(), namesIn(discards))
        assertEquals(Result.Ask(folder, null, now), discard(folder, shown))
        assertEquals(Step.DISCARD, UnsyncedNotesDiscardPolicy.step(now))
        assertEquals("nothing was deleted", before, filesUnder(pending))
        assertNull(permitOf(folder))
    }

    @Test fun `an acknowledgement after the store lists and counts nothing deletes nothing and only reloads`() {
        identifyDirectories()
        val owner = account("a")
        val folder = saveIn(owner, "n1")
        storeOf(owner).discard("n1")
        // Left: the counter and the notebook copy, which a count does not count.
        val left = filesUnder(pending)
        assertEquals(setOf("$folder/sequence", "$folder/b1.notebook"), left.keys)
        unlisted(folder)
        val shown = read().rows.single().answer!!
        assertFalse(shown.listed)
        PendingNotesStore.listFilesForTesting = null
        assertEquals(Result.Reload, discard(folder, shown))
        assertEquals("byte for byte", left, filesUnder(pending))
        assertNull(permitOf(folder))
        assertEquals(emptySet<String>(), namesIn(discards))
        assertEquals(emptyList<UnsyncedNotesRow>(), read().rows)
    }

    @Test fun `while notes-pending cannot be listed, present accounts' stores are found by path, the rest flagged`() {
        val p = account("p")
        val found = saveIn(p, "n1", "n2")
        saveIn(account("orphan"), "n1")
        unlisted("notes-pending")
        val listing = read(p)
        assertEquals(listOf(found), listing.rows.map { it.directory })
        assertEquals(p, listing.rows.single().owner)
        assertEquals("its own folder lists", UnsyncedNotes(2, 2, false, names = setOf("n1.note", "n2.note")),
            listing.rows.single().answer)
        assertTrue(listing.othersUnlisted)
        assertFalse(listing.unknownOthers)
        assertEquals(Text.Uncounted, UnsyncedNotesDiscardPolicy.hubSummary(listing))
        assertTrue("and without a present account, only the others are flagged", read().let {
            it.rows.isEmpty() && it.othersUnlisted
        })
        // A listing of no_backup that leaves notes-pending out does not make it one that holds nothing.
        PendingNotesStore.listFilesForTesting = { d ->
            when (d.name) {
                "notes-pending" -> null
                "no_backup" -> d.listFiles()?.filter { it.name != "notes-pending" }?.toTypedArray()
                else -> d.listFiles()
            }
        }
        assertTrue(read().othersUnlisted)
    }

    @Test fun `while notes-pending cannot be listed, a present account with no store is unread and records nothing`() {
        identifyDirectories()
        saveIn(account("orphan"), "n1")
        val q = account("q")
        val folder = folderOf(q)
        unlisted("notes-pending")
        val row = read(q).rows.single()
        assertEquals(folder, row.directory)
        assertEquals(q, row.owner)
        val shown = row.answer!!
        assertFalse(shown.listed)
        assertEquals(Text.Unlisted, UnsyncedNotesDiscardPolicy.rowText(shown))
        // A folder that was never made has no identity to bind an acknowledgement to.
        assertEquals(Result.AcknowledgementNotRecorded, discard(folder, shown, owner = q, running = true))
        assertEquals("nothing was recorded, and no Notes run was canceled", listOf("open"), calls)
        assertFalse("$folder.acknowledgement" in namesIn(discards))
        PendingNotesStore.listFilesForTesting = null
        assertFalse("and nothing was made", folder in namesIn(pending))
    }

    @Test fun `reading leaves notes-pending byte for byte and runs no recovery`() {
        identifyDirectories()
        val a = saveIn(account("a"), "n1")
        // A first write that never got its name: recovery would commit it.
        val stranded = PendingEntry("n9", "b1", PendingEntry.State.UPSERT, 9, "r9", false, blob = "c".toByteArray())
        File(File(pending, a), "n9.note.new").writeBytes(PendingCodec.encodeEntry(stranded))
        val b = saveIn(account("b"), "n1")
        // An acknowledgement for each, recorded while each could not be listed.
        unlisted(a, b)
        assertEquals(PendingNotesStore.DiscardOutcome.Acknowledged, opened(a).discardStore(opened(a).unsynced()))
        assertEquals(PendingNotesStore.DiscardOutcome.Acknowledged, opened(b).discardStore(opened(b).unsynced()))
        val before = filesUnder(pending)
        PendingNotesStore.resetForTesting()
        unlisted(b)
        val listing = read()
        assertEquals(UnsyncedNotes(2, 1, false, names = setOf("n1.note", "n9.note")),
            listing.rows.single { it.directory == a }.answer)
        assertFalse(listing.rows.single { it.directory == b }.answer!!.listed)
        assertEquals("the first write is still uncommitted, and nothing else changed", before, filesUnder(pending))
        // Only the store that listed withdrew its acknowledgement; a count of one that cannot be listed keeps it.
        assertEquals(setOf("$b.acknowledgement"), namesIn(discards))
    }

    @Test fun `a store whose read throws is shown with nothing to confirm`() {
        val folders = listOf("a", "b", "c").map { saveIn(account(it), "n1") }.sorted()
        val thrown = mapOf(folders[0] to IOException("canonicalPath failed"),
            folders[1] to IllegalStateException("dropped"), folders[2] to SecurityException("denied"))
        val listing = read { throw thrown.getValue(it) }
        assertEquals(folders, listing.rows.map { it.directory })
        assertTrue(listing.rows.all { it.answer == null })
        assertEquals(Text.Unlisted, UnsyncedNotesDiscardPolicy.rowText(null))
        assertEquals(Text.Uncounted, UnsyncedNotesDiscardPolicy.hubSummary(listing))
    }

    // ---- what a confirmation does ----

    @Test fun `a discard of a running present account's store cancels its run, discards, then unregisters on Done`() {
        val p = account("p")
        val folder = saveIn(p, "n1")
        PendingNotesStore.resetForTesting()
        val shown = read(p).rows.single().answer!!
        val instance = opened(folder)
        PendingNotesStore.beforeClearForTesting = { calls += "deleting" }
        assertEquals(Result.Done, discard(folder, shown, owner = p, running = true))
        assertEquals(listOf("running p", "cancel p", "open", "deleting"), calls)
        assertFalse(folder in namesIn(pending))
        // Dropped: the next opener gets a new instance, over a folder that is not there, which takes a save.
        val next = opened(folder)
        assertNotSame(instance, next)
        assertSame(next, storeOf(p))
        assertTrue(next.saveLocal("n2", "b1", "r1", "after".toByteArray(), isCreate = false) is SaveOutcome.Saved)
    }

    @Test fun `a store whose account runs nothing, an orphan, and an acknowledgement cancel nothing`() {
        identifyDirectories()
        val p = account("p")
        val idle = saveIn(p, "n1")
        assertEquals(Result.Done, discard(idle, read(p).rows.single().answer!!, owner = p, running = false))
        assertEquals(listOf("running p", "open"), calls)

        calls.clear()
        val orphan = saveIn(account("orphan"), "n1")
        assertEquals(Result.Done, discard(orphan, read().rows.single().answer!!))
        assertEquals("an orphan's run is not asked about", listOf("open"), calls)

        calls.clear()
        val q = account("q")
        val held = saveIn(q, "n1")
        unlisted(held)
        assertEquals(Result.Acknowledged, discard(held, read(q).rows.single().answer!!, owner = q, running = true))
        assertEquals("an acknowledgement deletes nothing, so it cancels nothing", listOf("open"), calls)
    }

    @Test fun `Changed and PermitNotRecorded delete nothing and keep the instance registered`() {
        val owner = account("a")
        val folder = saveIn(owner, "n1")
        val shown = read().rows.single().answer!!
        val instance = opened(folder)
        // A save after the dialog was shown: the dialog shows again, for what is there now.
        saveIn(owner, "n2")
        val now = read().rows.single().answer!!
        val before = filesUnder(pending)
        assertEquals(Result.Ask(folder, "a", now), discard(folder, shown, owner = owner))
        assertEquals(before, filesUnder(pending))
        assertNull(permitOf(folder))
        assertSame(instance, opened(folder))

        PendingNotesDiscardPermits.beforeRecordForTesting = { throw IOException("could not record the permit") }
        assertEquals(Result.NothingDeleted, discard(folder, now, owner = owner))
        PendingNotesDiscardPermits.beforeRecordForTesting = null
        assertEquals(before, filesUnder(pending))
        assertNull(permitOf(folder))
        assertSame(instance, opened(folder))
        assertTrue("the store stays open", instance.scan().entries.size == 2)
    }

    @Test fun `NotFinished keeps the permit and the instance, and the same confirmation finishes it later`() {
        val folder = saveIn(account("a"), "n1", "n2")
        val shown = read().rows.single().answer!!
        val instance = opened(folder)
        val before = filesUnder(pending)
        // The first note goes, then the removal stops.
        var removals = 0
        PendingNotesStore.beforeRemoveForTesting = { if (removals++ == 1) throw IOException("the removal stops here") }
        assertEquals(Result.NotFinished(folder, null, shown), discard(folder, shown))
        assertEquals("only the first note went", before - "$folder/n1.note", filesUnder(pending))
        assertEquals("the permit stays", shown, permitOf(folder))
        assertSame("the closed instance stays registered", instance, opened(folder))
        assertEquals(UnsyncedNotes(1, 2, false, names = setOf("n2.note")), read().rows.single().answer)

        // A Retry that cannot record its permit, or cannot open the store, deleted nothing itself, but the attempt
        // before it did: it is not finished, with Retry, and never says that nothing was deleted.
        PendingNotesDiscardPermits.beforeRecordForTesting = { throw IOException("could not record the permit") }
        assertEquals(Result.NotFinished(folder, null, shown), discard(folder, shown, retry = true))
        PendingNotesDiscardPermits.beforeRecordForTesting = null
        assertEquals(Result.NotFinished(folder, null, shown),
            discard(folder, shown, retry = true, open = { throw IOException("could not open") }))
        assertEquals("the permit stays", shown, permitOf(folder))
        assertSame(instance, opened(folder))

        assertEquals(Result.Done, discard(folder, shown, retry = true))
        assertFalse(folder in namesIn(pending))
        assertNull(permitOf(folder))
        assertSame("with no present account, the finished instance stays, closed", instance, opened(folder))
        assertTrue(instance.clearedInFull())
    }

    @Test fun `an acknowledgement deletes nothing, records no permit, stays recorded and keeps the store open`() {
        identifyDirectories()
        val folder = saveIn(account("a"), "n1")
        PendingNotesStore.resetForTesting()
        val before = filesUnder(pending)
        unlisted(folder)
        val shown = read().rows.single().answer!!
        val instance = opened(folder)
        assertEquals(Result.Acknowledged, discard(folder, shown))
        assertEquals(before, filesUnder(pending))
        assertNull(permitOf(folder))
        assertEquals(setOf("$folder.acknowledgement"), namesIn(discards))
        assertSame(instance, opened(folder))
        assertTrue(instance.acknowledged())
        PendingNotesStore.listFilesForTesting = null
        assertEquals("not closed: it reads and lists again", 1, instance.scan().entries.size)
    }

    @Test fun `with notes-pending unlistable a discard does not finish, nor a Retry, until the folder lists`() {
        val p = account("p")
        val folder = saveIn(p, "n1")
        unlisted("notes-pending")
        val shown = read(p).rows.single().answer!!
        assertTrue(shown.listed)
        val notFinished = Result.NotFinished(folder, "p", shown)
        assertEquals(notFinished, discard(folder, shown, owner = p))
        assertEquals("the permit stays", shown, permitOf(folder))
        // Gone, but not shown gone while the folder above cannot be listed: it cannot be read right now.
        val after = read(p).rows.single().answer!!
        assertFalse(after.listed)
        assertEquals(Text.Unlisted, UnsyncedNotesDiscardPolicy.rowText(after))
        assertEquals("a Retry is not finished either, and is not turned into an acknowledgement", notFinished,
            discard(folder, shown, owner = p, retry = true))
        assertEquals(shown, permitOf(folder))

        PendingNotesStore.listFilesForTesting = null
        assertFalse(folder in namesIn(pending))
        assertEquals(Result.Done, discard(folder, shown, owner = p, retry = true))
        assertNull(permitOf(folder))
        assertTrue(storeOf(p).saveLocal("n2", "b1", "r1", "new".toByteArray(), isCreate = false) is SaveOutcome.Saved)
    }

    @Test fun `failures before the discard delete nothing, and failures during it are not finished`() {
        identifyDirectories()
        val owner = account("a")
        val folder = saveIn(owner, "n1")
        val shown = read().rows.single().answer!!
        val before = filesUnder(pending)
        // Opening throws (canonicalPath can).
        val throwing: (String) -> PendingNotesStore = { throw IOException("could not open") }
        assertEquals(Result.NothingDeleted, discard(folder, shown, open = throwing))
        assertEquals(before, filesUnder(pending))

        // An instance dropped after a discard that finished, while a new one owns the folder again.
        val dropped = opened(folder)
        assertEquals(Result.Done, discard(folder, shown, owner = owner))
        saveIn(owner, "n1")
        val again = read().rows.single().answer!!
        val refilled = filesUnder(pending)
        assertEquals(Result.NothingDeleted, discard(folder, again, open = { dropped }))
        assertEquals(refilled, filesUnder(pending))
        assertNull(permitOf(folder))

        // Anything else from the store is a discard that may have stopped anywhere.
        PendingNotesStore.beforeClearForTesting = { throw RuntimeException("stopped after closing") }
        assertEquals(Result.NotFinished(folder, null, again), discard(folder, again))
        PendingNotesStore.beforeClearForTesting = null
        assertEquals(refilled, filesUnder(pending))
        assertEquals("the permit stays", again, permitOf(folder))

        // After an acknowledgement, every failure records nothing and says nothing was deleted.
        val other = saveIn(account("b"), "n1")
        unlisted(other)
        val unlistedShown = read().rows.single { it.directory == other }.answer!!
        assertEquals(Result.AcknowledgementNotRecorded, discard(other, unlistedShown, open = throwing))
        assertEquals(Result.AcknowledgementNotRecorded, discard(other, unlistedShown, open = { dropped }))
        PendingNotesDiscardPermits.beforeRecordForTesting = { throw RuntimeException("stopped while recording") }
        assertEquals(Result.AcknowledgementNotRecorded, discard(other, unlistedShown))
        PendingNotesDiscardPermits.beforeRecordForTesting = null
        assertFalse("$other.acknowledgement" in namesIn(discards))
    }

    // ---- a discard that stopped, once the screen was left ----

    @Test fun `a stopped discard keeps its row and Retry with nothing left to count, and the Retry finishes it`() {
        val folder = saveIn(account("a"), "n1")
        val shown = read().rows.single().answer!!
        PendingNotesStore.resetForTesting()
        // Opened first, so its functions are the ones kept: the sync right after n1.note's unlink fails once, as an
        // EIO from fsync can. Every sync after that is recorded.
        var unlinked = false
        var failed = false
        val synced = ArrayList<String>()
        PendingNotesStore.beforeRemoveForTesting = { if (it.name == "n1.note") unlinked = true }
        val instance = PendingNotesStore.forNoBackupDirectory(noBackup, folder, { d ->
            if (failed) {
                synced += d.name
            } else if (unlinked && d.name == folder) {
                failed = true
                throw IOException("could not sync the store folder")
            }
        }, atomicMove)
        assertEquals(Result.NotFinished(folder, null, shown), discard(folder, shown))
        assertEquals("the text went, the rest stays", setOf("b1.notebook", "sequence"), namesIn(File(pending, folder)))
        assertEquals("the closed instance counts nothing", UnsyncedNotes.NOTHING, instance.unsynced())
        assertEquals("the permit stays", shown, permitOf(folder))

        // The screen was left, so it holds no result: the store's own state keeps the row, and the hub row.
        val listing = read()
        assertEquals(listOf(UnsyncedNotesRow(folder, null, UnsyncedNotes.NOTHING, stopped = true)), listing.rows)
        assertEquals(Text.Uncounted, UnsyncedNotesDiscardPolicy.hubSummary(listing))

        // Its Retry, an answer of nothing, finishes it, and syncs the store folder and the folder above.
        assertEquals(Result.Done, discard(folder, UnsyncedNotes.NOTHING, retry = true))
        assertTrue("$synced", folder in synced && "notes-pending" in synced)
        assertFalse(folder in namesIn(pending))
        assertNull(permitOf(folder))
        assertFalse(instance.clearStopped())
        val after = read()
        assertEquals(emptyList<UnsyncedNotesRow>(), after.rows)
        assertNull(UnsyncedNotesDiscardPolicy.hubSummary(after))
    }

    @Test fun `a stopped discard that still holds text asks again for it, and is never said to have deleted nothing`() {
        identifyDirectories()
        val folder = saveIn(account("a"), "n1", "n2")
        val shown = read().rows.single().answer!!
        PendingNotesStore.beforeRemoveForTesting = { if (it.name == "n2.note") throw IOException("the removal stops") }
        assertEquals(Result.NotFinished(folder, null, shown), discard(folder, shown))
        PendingNotesStore.beforeRemoveForTesting = null
        val left = UnsyncedNotes(1, 2, false, names = setOf("n2.note"))
        assertEquals(listOf(UnsyncedNotesRow(folder, null, left, stopped = true)), read().rows)

        // The row's Retry, an answer of nothing, deletes nothing while text is left: the dialog shows again for it.
        val before = filesUnder(pending)
        assertEquals(Result.Ask(folder, null, left), discard(folder, UnsyncedNotes.NOTHING, retry = true))
        assertEquals(before, filesUnder(pending))
        assertEquals("the permit stays", shown, permitOf(folder))
        // That dialog's confirmation, whose permit cannot be recorded, deleted nothing itself, but the attempt before
        // it did: not finished, never that nothing was deleted.
        PendingNotesDiscardPermits.beforeRecordForTesting = { throw IOException("could not record the permit") }
        assertEquals(Result.NotFinished(folder, null, left), discard(folder, left))
        PendingNotesDiscardPermits.beforeRecordForTesting = null
        assertEquals(before, filesUnder(pending))
        // Nor when it fails before the store is reached: a discard of the store stopped in this process, which is
        // read from memory, on a device too.
        assertEquals(Result.NotFinished(folder, null, left),
            discard(folder, left, open = { throw IOException("could not open") }))
        assertEquals(before, filesUnder(pending))
        assertTrue(deviceWiring().contains(
            "stoppedBefore = { directory in PendingNotesStore.stoppedIn(context.noBackupFilesDir) })"))

        // While the folder cannot be listed, the row is still the Retry, which is not finished again, and an
        // acknowledgement over it records nothing and says nothing.
        unlisted(folder)
        val row = read().rows.single()
        assertTrue("$row", row.stopped && row.answer?.listed == false)
        assertEquals(Result.NotFinished(folder, null, UnsyncedNotes.NOTHING),
            discard(folder, UnsyncedNotes.NOTHING, retry = true))
        assertEquals(Result.Reload, discard(folder, row.answer!!))
        assertFalse("$folder.acknowledgement" in namesIn(discards))
        assertEquals(before, filesUnder(pending))
        // Settings reloads before the store call, so a call that would throw never says that nothing was deleted.
        PendingNotesStore.listFilesForTesting = { d ->
            if (d.name == folder) throw SecurityException("the listing is refused") else d.listFiles()
        }
        assertEquals(Result.Reload, discard(folder, row.answer!!))
        assertFalse("$folder.acknowledgement" in namesIn(discards))

        // Once it lists, the confirmation of what is left finishes it.
        PendingNotesStore.listFilesForTesting = null
        assertEquals(Result.Done, discard(folder, left))
        assertFalse(folder in namesIn(pending))
        assertNull(permitOf(folder))
    }

    @Test fun `a discard that stopped once its folder was gone keeps its row, though no listing finds the folder`() {
        val folder = saveIn(account("orphan"), "n1")
        val shown = read().rows.single().answer!!
        PendingNotesStore.resetForTesting()
        // The sync of notes-pending fails, as an EIO from fsync can, after the folder is gone.
        var failAbove = true
        PendingNotesStore.forNoBackupDirectory(noBackup, folder, { d ->
            if (failAbove && d.name == "notes-pending") throw IOException("could not sync notes-pending")
        }, atomicMove)
        assertEquals(Result.NotFinished(folder, null, shown), discard(folder, shown))
        assertFalse(folder in namesIn(pending))
        assertEquals("the permit stays", shown, permitOf(folder))

        val listing = read()
        assertEquals(listOf(UnsyncedNotesRow(folder, null, UnsyncedNotes.NOTHING, stopped = true)), listing.rows)
        assertEquals(Text.Uncounted, UnsyncedNotesDiscardPolicy.hubSummary(listing))
        failAbove = false
        assertEquals(Result.Done, discard(folder, UnsyncedNotes.NOTHING, retry = true))
        assertNull(permitOf(folder))
        assertEquals(emptyList<UnsyncedNotesRow>(), read().rows)
    }

    @Test fun `an answer of nothing over a discard not stopped deletes nothing, Retry or not, and cancels no run`() {
        val p = account("p")
        val folder = saveIn(p, "n1")
        // Pushed and cleared from the store: only the notebook copy and the counter are left, which hold no text.
        storeOf(p).discard("n1")
        val before = filesUnder(pending)
        PendingNotesStore.beforeClearForTesting = { calls += "deleting" }
        // Only a row's Retry sends one, but the rule does not rest on that.
        for (retry in listOf(true, false)) {
            calls.clear()
            assertEquals("retry $retry", Result.Reload,
                discard(folder, UnsyncedNotes.NOTHING, owner = p, retry = retry, running = true))
            assertEquals("retry $retry", before, filesUnder(pending))
            assertEquals("no run is canceled for an answer of nothing", listOf("open"), calls)
        }
        assertFalse(storeOf(p).clearStopped())

        // One that another call finished, as the sign-out does: Done, and the instance it keeps stays.
        val q = account("q")
        val finished = saveIn(q, "n1")
        val kept = storeOf(q)
        assertEquals(PendingNotesStore.DiscardOutcome.Done, kept.discardStore(kept.unsynced()))
        calls.clear()
        assertEquals(Result.Done, discard(finished, UnsyncedNotes.NOTHING, owner = q, retry = true, running = true))
        assertEquals(listOf("open"), calls)
        assertSame(kept, storeOf(q))
    }

    @Test fun `Settings drops an instance only when its discard alone finished it and an account owns the folder`() {
        val p = account("p")
        val folder = saveIn(p, "n1", "n2")
        val shown = read(p).rows.single().answer!!
        val instance = storeOf(p)
        PendingNotesStore.beforeRemoveForTesting = { if (it.name == "n2.note") throw IOException("the removal stops") }
        assertEquals(Result.NotFinished(folder, "p", shown), discard(folder, shown, owner = p))
        PendingNotesStore.beforeRemoveForTesting = null
        assertSame(instance, storeOf(p))
        // The sign-out finishes it and keeps it, closed, for its account, which it removes next.
        assertEquals(PendingNotesStore.DiscardOutcome.Done, storeOf(p).discardStore(storeOf(p).unsynced()))
        // A Settings Retry finishes it as well, whether the account is still found or not: it stays.
        for (now in listOf(null, p)) {
            assertEquals(Result.Done, discard(folder, shown, owner = p, retry = true, ownerNow = { now }))
            assertSame("$now", instance, storeOf(p))
        }
        assertTrue(runCatching { storeOf(p).saveLocal("n3", "b1", "r1", "late".toByteArray(), isCreate = false) }
            .exceptionOrNull() is PendingNotesStore.Cleared)
        assertFalse(folder in namesIn(pending))

        // The sign-out finishes the instance between Settings' Done and its drop: it stays.
        val r = account("r")
        val raced = saveIn(r, "n1")
        val racedInstance = storeOf(r)
        assertEquals(Result.Done, discard(raced, read(r).rows.single().answer!!, owner = r, ownerNow = {
            assertEquals(PendingNotesStore.DiscardOutcome.Done, storeOf(r).discardStore(storeOf(r).unsynced()))
            r
        }))
        assertSame(racedInstance, storeOf(r))

        // An account removed while Settings' own discard ran, and an account read that throws: each keeps its
        // finished instance, closed.
        for ((name, ownerNow) in listOf<Pair<String, () -> ExactAccountIdentity?>>(
            "q" to { null }, "s" to { throw SecurityException("denied") })) {
            val owner = account(name)
            val other = saveIn(owner, "n1")
            val opened = storeOf(owner)
            assertEquals(name, Result.Done, discard(other, read(owner).rows.single().answer!!, owner = owner,
                ownerNow = ownerNow))
            assertSame(name, opened, storeOf(owner))
            assertTrue(name, opened.clearedInFull())
        }

        // On a device, the account is found again after Done.
        assertTrue(deviceWiring().contains("ownerNow = { ownerOf(context, directory) },"))
    }

    // ---- a store folder that is a link ----

    /** A symbolic link at [at] to [target], or a skip where this machine cannot make one. */
    private fun linked(at: File, target: File) {
        val made = try {
            Files.createSymbolicLink(at.toPath(), target.toPath())
            true
        } catch (e: IOException) {
            false
        } catch (e: UnsupportedOperationException) {
            false
        }
        assumeTrue("this machine cannot make a symbolic link", made)
    }

    /**
     * Every path at and below [root] as lstat shows it, no link followed: a link by what it points to, a directory as
     * null, a file by its bytes. A path that is not there gives nothing.
     */
    private fun treeOf(root: File): Map<String, List<Byte>?> {
        val tree = HashMap<String, List<Byte>?>()
        fun walk(f: File, name: String) {
            val path = f.toPath()
            when {
                Files.isSymbolicLink(path) -> tree[name] = "-> ${Files.readSymbolicLink(path)}".toByteArray().toList()
                Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS) -> {
                    tree[name] = null
                    for (child in f.list()!!) walk(File(f, child), "$name/$child")
                }
                Files.exists(path, LinkOption.NOFOLLOW_LINKS) -> tree[name] = f.readBytes().toList()
            }
        }
        walk(root, root.name)
        return tree
    }

    /**
     * Stand-ins for lstat's inode numbers of any path that is there, read without following a link, so a link has one
     * of its own; none for a path that is not there.
     */
    private fun identifyWithoutFollowing() {
        val inodes = HashMap<String, Long>()
        PendingNotesStore.identityForTesting = { d ->
            val attributes = try {
                Files.readAttributes(d.toPath(), BasicFileAttributes::class.java, LinkOption.NOFOLLOW_LINKS)
            } catch (e: IOException) {
                null
            }
            attributes?.let {
                val key = (if (it.isSymbolicLink) "link " else "") + d.path
                PendingNotesStore.DirectoryIdentity(7, inodes.getOrPut(key) { inodes.size + 1L })
            }
        }
    }

    /**
     * A present account's store folder A made a link to [target], given store B beside it, which holds a saved note.
     * The screen shows A as a store that could not be counted and is not listed, and B with its own count; A's instance
     * is not B's. A's confirmation is only an acknowledgement, under A's own name, which cancels nothing and deletes
     * nothing. notes-pending, the link, B's files and count, and the link's target stay as they were, byte for byte,
     * and no permit or record is read or written but A's own acknowledgement.
     */
    private fun assertLinkOnlyAcknowledged(target: (b: File) -> File) {
        identifyWithoutFollowing()
        val b = saveIn(account("b"), "n1")
        val counted = UnsyncedNotes(1, 1, false, names = setOf("n1.note"))
        assertEquals(counted, opened(b).unsynced())
        val owner = account("a")
        val a = folderOf(owner)
        val to = target(File(pending, b))
        linked(File(pending, a), to)
        val before = treeOf(pending)
        val targetBefore = treeOf(to)
        val records = ArrayList<String>()
        PendingNotesDiscardPermits.onReadForTesting = { records += it.name }

        val listing = read(owner)
        assertEquals(listOf(a, b).sorted(), listing.rows.map { it.directory })
        assertFalse(listing.othersUnlisted || listing.unknownOthers)
        val row = listing.rows.single { it.directory == a }
        assertEquals(owner, row.owner)
        val shown = row.answer!!
        assertEquals("could not be counted, and not listed", UnsyncedNotes(0, 0, unreadable = true, listed = false),
            shown)
        assertEquals(Step.ACKNOWLEDGE, UnsyncedNotesDiscardPolicy.step(shown))
        assertEquals(Text.Unlisted, UnsyncedNotesDiscardPolicy.rowText(shown))
        assertEquals("B's own count", counted, listing.rows.single { it.directory == b }.answer)
        assertNotSame("a link never shares its target's instance", opened(b), opened(a))
        assertEquals("the link's own name", a, opened(a).dir.name)

        assertEquals(Result.Acknowledged, discard(a, shown, owner = owner, running = true))
        assertEquals("an acknowledgement cancels nothing", listOf("open"), calls)
        assertTrue("bound to the link's own identity", opened(a).acknowledged())
        assertEquals("a record under the link's own name, none under B's or the target's", setOf("$a.acknowledgement"),
            namesIn(discards))
        assertEquals("no permit or record read but A's own", setOf("$a.acknowledgement"), records.toSet())

        assertEquals("notes-pending, the link and B's files", before, treeOf(pending))
        assertEquals("the link's target", targetBefore, treeOf(to))
        assertEquals("B's count", counted, opened(b).unsynced())
    }

    @Test fun `a store folder that is a link to another store is only acknowledged, and nothing behind it changes`() =
        assertLinkOnlyAcknowledged { b -> b }

    @Test fun `a store folder that is a link to a file is only acknowledged, and the file stays as it was`() =
        assertLinkOnlyAcknowledged { tmp.newFile("plain").also { it.writeBytes("not a store".toByteArray()) } }

    @Test fun `a store folder that is a link to nothing is only acknowledged, and nothing is made there`() =
        assertLinkOnlyAcknowledged { File(tmp.root, "missing") }

    private companion object {
        const val TYPE = "io.silentsuite"
    }
}
