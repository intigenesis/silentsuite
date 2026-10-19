package io.silentsuite.sync.ui.etebase

import io.silentsuite.sync.Constants
import org.junit.Assert.assertEquals
import org.junit.Test

class ItemPresentationTest {
    private data class Entry(val name: String, val deleted: Boolean)

    private val entries = listOf(
        Entry("live-1", false),
        Entry("deleted-1", true),
        Entry("live-2", false),
        Entry("deleted-2", true)
    )

    @Test
    fun ordinaryAddressBookListLeavesDeletedContactsOut() {
        assertEquals(
            listOf("live-1", "live-2"),
            ItemPresentation.ORDINARY.select(Constants.ETEBASE_TYPE_ADDRESS_BOOK, entries) { it.deleted }.map { it.name }
        )
    }

    @Test
    fun recoveryListShowsOnlyDeletedContactsInOrder() {
        assertEquals(
            listOf("deleted-1", "deleted-2"),
            ItemPresentation.DELETED_CONTACTS.select(Constants.ETEBASE_TYPE_ADDRESS_BOOK, entries) { it.deleted }.map { it.name }
        )
    }

    @Test
    fun calendarsTasksAndNotesKeepEveryEntryAndHaveNoContactsRecoveryList() {
        listOf(Constants.ETEBASE_TYPE_CALENDAR, Constants.ETEBASE_TYPE_TASKS, Constants.ETEBASE_TYPE_NOTES).forEach { type ->
            assertEquals(type, entries, ItemPresentation.ORDINARY.select(type, entries) { it.deleted })
            assertEquals(type, emptyList<Entry>(), ItemPresentation.DELETED_CONTACTS.select(type, entries) { it.deleted })
        }
    }
}
