package io.silentsuite.sync.ui

import android.app.Activity
import android.view.Menu
import android.view.MenuItem
import io.silentsuite.sync.R

/**
 * The overflow item that opens Settings, Unsynced Notes from the sign-in and setup screens, so the way out for
 * unsynced Notes is there also when no account can reach its dashboard. It opens the screen as the root of Settings
 * with no account ([AppSettingsActivity.newUnsyncedNotesIntent]), with no hub, so Back returns to the screen it came
 * from.
 */
internal object UnsyncedNotesEntry {
    /** Not 0, which Help's item has, and below the resource ids and android.R.id.home (0x01000000 and above). */
    const val ITEM_ID = 0x00554e53

    fun addTo(menu: Menu) {
        menu.add(Menu.NONE, ITEM_ID, Menu.NONE, R.string.settings_unsynced_notes)
            .setShowAsAction(MenuItem.SHOW_AS_ACTION_NEVER)
    }

    /** Opens Unsynced Notes for this item and answers true; false for any other item. */
    fun opens(activity: Activity, item: MenuItem): Boolean {
        if (item.itemId != ITEM_ID) return false
        activity.startActivity(AppSettingsActivity.newUnsyncedNotesIntent(activity))
        return true
    }
}
