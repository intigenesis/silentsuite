package io.silentsuite.sync.ui

import android.app.Application
import android.app.Dialog
import android.os.Bundle
import android.view.View
import androidx.annotation.StringRes
import androidx.fragment.app.DialogFragment
import androidx.fragment.app.Fragment
import androidx.fragment.app.viewModels
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.LiveData
import androidx.lifecycle.MutableLiveData
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.viewModelScope
import androidx.preference.Preference
import androidx.preference.PreferenceFragmentCompat
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.snackbar.Snackbar
import io.silentsuite.sync.R
import io.silentsuite.sync.notes.edit.UnsyncedNotes
import io.silentsuite.sync.ui.UnsyncedNotesDiscardPolicy.Result
import io.silentsuite.sync.ui.UnsyncedNotesDiscardPolicy.Step
import io.silentsuite.sync.ui.UnsyncedNotesSignOutPolicy.Text
import io.silentsuite.sync.ui.settings.AppPreferences
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Settings, Unsynced Notes: one row per Notes store on this device that holds unsynced text or cannot be read, a
 * present account's by its name and any other as not matched to an account, in folder-name order. A row opens the
 * confirmation for that one store ([UnsyncedNotesDiscardDialogFragment]), and the result shows in a Snackbar. No row
 * opens one while a confirmation runs, and the row of a store whose discard did not finish opens none: it goes on only
 * by Retry. It works offline and without a session: it reads the stores through [UnsyncedNotesStores], off the main
 * thread, every time it resumes. The rows say how many changes each store holds, that they could not be counted, or
 * that the store cannot be read right now, never a count of zero, and a last row says when other Notes files could
 * not be shown.
 */
class UnsyncedNotesFragment : PreferenceFragmentCompat() {
    /** Kept across a rotation and cleared once the screen is left, so a result never shows on a later visit. */
    private val model: UnsyncedNotesViewModel by viewModels()

    /** The Snackbar with Retry of a discard that did not finish, while it shows. */
    private var retryBar: Snackbar? = null

    override fun onCreatePreferences(savedInstanceState: Bundle?, rootKey: String?) {
        preferenceManager.sharedPreferencesName = AppPreferences.PREFERENCES_NAME
        preferenceScreen = preferenceManager.createPreferenceScreen(requireContext())
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        model.listing.observe(viewLifecycleOwner, ::showListing)
        model.result.observe(viewLifecycleOwner, ::showResult)
    }

    override fun onResume() {
        super.onResume()
        model.load()
    }

    override fun onDestroyView() {
        // The bar sits in the activity's content view, not in this screen's, so it would outlive the screen.
        retryBar?.dismiss()
        retryBar = null
        super.onDestroyView()
    }

    private fun showListing(listing: UnsyncedNotesListing) {
        val screen = preferenceScreen
        screen.removeAll()
        for (row in listing.rows) screen.addPreference(Preference(requireContext()).apply {
            key = KEY_ROW + row.directory
            title = row.owner?.name ?: getString(R.string.notes_store_unmatched)
            // A discard of this store that did not finish goes on only by its Retry.
            val stopped = UnsyncedNotesDiscardPolicy.notFinished(row.directory, model.result.value)
            summary = if (stopped) {
                getString(R.string.notes_discard_not_finished)
            } else when (val text = UnsyncedNotesDiscardPolicy.rowText(row.answer)) {
                is Text.Counted -> resources.getQuantityString(R.plurals.notes_store_unsynced, text.count, text.count)
                Text.Uncounted -> getString(R.string.notes_store_uncounted)
                Text.Unlisted -> getString(R.string.notes_store_unlisted)
            }
            isPersistent = false
            // A store whose read threw has nothing to confirm.
            val answer = row.answer
            isEnabled = answer != null && !stopped
            if (answer != null) setOnPreferenceClickListener {
                // Asked again at the tap: the result can change before the rows are read again.
                if (UnsyncedNotesDiscardPolicy.rowOpens(row.directory, model.result.value, model.answering)) {
                    ask(row.directory, row.owner?.name, answer)
                }
                true
            }
        })
        when {
            listing.othersUnlisted -> addNote(KEY_OTHERS, R.string.notes_stores_others_unlisted)
            listing.unknownOthers -> addNote(KEY_OTHERS, R.string.notes_stores_others_unknown)
            listing.rows.isEmpty() -> addNote(KEY_NONE, R.string.notes_stores_none)
        }
    }

    /** A last row with no action. */
    private fun addNote(key: String, @StringRes text: Int) {
        preferenceScreen.addPreference(Preference(requireContext()).apply {
            this.key = key
            setTitle(text)
            isSingleLineTitle = false
            isEnabled = false
            isPersistent = false
        })
    }

    /** Shows the confirmation of [shown] for one store, unless that store's dialog of that step shows already. */
    private fun ask(directory: String, label: String?, shown: UnsyncedNotes): Boolean {
        val tag = UnsyncedNotesDiscardDialogFragment.tag(UnsyncedNotesDiscardPolicy.step(shown), directory)
        if (childFragmentManager.isStateSaved) return false
        if (childFragmentManager.findFragmentByTag(tag) == null) {
            UnsyncedNotesDiscardDialogFragment.newInstance(directory, label, shown).show(childFragmentManager, tag)
        }
        return true
    }

    private fun showResult(result: Result?) {
        retryBar?.dismiss()
        retryBar = null
        val view = view ?: return
        when (result) {
            null -> Unit
            is Result.Ask -> if (ask(result.directory, result.label, result.now)) model.consume()
            Result.Reload -> model.consume()
            // Kept until Retry is tapped, so a rotation shows it again.
            is Result.NotFinished -> retryBar =
                Snackbar.make(view, R.string.notes_discard_not_finished, Snackbar.LENGTH_INDEFINITE)
                    .setAction(R.string.retry) { model.retry(result) }
                    .also { it.show() }
            else -> {
                UnsyncedNotesDiscardPolicy.message(result)?.let { message ->
                    Snackbar.make(view, message, Snackbar.LENGTH_LONG).show()
                }
                model.consume()
            }
        }
    }

    companion object {
        private const val KEY_ROW = "unsynced_notes:"
        internal const val KEY_OTHERS = "unsynced_notes_others"
        internal const val KEY_NONE = "unsynced_notes_none"

        internal fun rowKey(directory: String) = KEY_ROW + directory

        /**
         * Reads the stores off the main thread and shows the Settings hub's [row] only while some store holds anything
         * or cannot be read, with how many changes there are, or that not all of them could be counted. [fragment]
         * calls it whenever it resumes, so a return from the screen shows what is there now.
         */
        internal fun refreshHubRow(fragment: Fragment, row: Preference) {
            val app = fragment.requireContext().applicationContext
            fragment.viewLifecycleOwner.lifecycleScope.launch {
                val listing = withContext(Dispatchers.IO) { UnsyncedNotesStores.read(app) }
                if (!fragment.isAdded) return@launch
                val summary = UnsyncedNotesDiscardPolicy.hubSummary(listing)
                row.isVisible = summary != null
                row.summary = when (summary) {
                    null -> null
                    is Text.Counted -> fragment.resources
                        .getQuantityString(R.plurals.settings_unsynced_notes_summary, summary.count, summary.count)
                    else -> fragment.getString(R.string.settings_unsynced_notes_uncounted)
                }
            }
        }
    }
}

/**
 * The confirmation for one store of [UnsyncedNotesFragment]. For a store that listed, a discard: how many changes go,
 * or that they could not be counted, with Discard and Cancel. For a store that could not be listed, an
 * acknowledgement, in words of its own: it never says discard, says that nothing is deleted, and that unsynced Notes
 * text may remain on this device and may no longer sync. The dialog keeps all of the answer it showed in its arguments
 * and sends exactly that with its one confirming button. Cancel, Back and a tap outside send nothing.
 */
class UnsyncedNotesDiscardDialogFragment : DialogFragment() {
    private val model: UnsyncedNotesViewModel by viewModels(ownerProducer = { requireParentFragment() })

    override fun onCreateDialog(savedInstanceState: Bundle?): Dialog {
        val args = requireArguments()
        val directory = args.getString(ARG_DIRECTORY)!!
        val label = args.getString(ARG_LABEL)
        // All of the answer, the names it was counted from included: a confirmation covers only what it names.
        val shown = UnsyncedNotes(args.getInt(ARG_COUNT), args.getLong(ARG_SEQUENCE), args.getBoolean(ARG_UNREADABLE),
            args.getBoolean(ARG_LISTED), args.getStringArrayList(ARG_NAMES)!!.toSet())
        val name = label ?: getString(R.string.notes_store_unmatched_name)
        val text = UnsyncedNotesSignOutPolicy.text(shown)
        fun confirm() {
            model.answer(directory, label, shown, retry = false)
        }
        val builder = MaterialAlertDialogBuilder(requireContext())
            .setNegativeButton(android.R.string.cancel, null)
        return when (UnsyncedNotesDiscardPolicy.step(shown)) {
            Step.DISCARD -> builder
                .setTitle(R.string.notes_discard_title)
                .setMessage(if (text is Text.Counted) {
                    resources.getQuantityString(R.plurals.notes_discard_store_text, text.count, text.count, name)
                } else {
                    getString(R.string.notes_discard_store_uncounted, name)
                })
                .setPositiveButton(R.string.notes_discard) { _, _ -> confirm() }
                .create()
            Step.ACKNOWLEDGE -> builder
                .setTitle(R.string.notes_acknowledge_title)
                .setMessage(getString(R.string.notes_store_acknowledge_text, name))
                .setPositiveButton(R.string.notes_acknowledge_continue) { _, _ -> confirm() }
                .create()
        }
    }

    companion object {
        private const val ARG_DIRECTORY = "unsyncedNotes.directory"
        private const val ARG_LABEL = "unsyncedNotes.label"
        private const val ARG_COUNT = "unsyncedNotes.count"
        private const val ARG_SEQUENCE = "unsyncedNotes.sequence"
        private const val ARG_UNREADABLE = "unsyncedNotes.unreadable"
        private const val ARG_LISTED = "unsyncedNotes.listed"
        private const val ARG_NAMES = "unsyncedNotes.names"

        /**
         * One tag per store and step. The screen looks the tag up before it shows a step for a store, and shows none
         * while it finds one, so a dialog for one store never stands in for another store's.
         */
        internal fun tag(step: Step, directory: String) = "unsynced-notes-store-" + step.name + ":" + directory

        internal fun newInstance(directory: String, label: String?, shown: UnsyncedNotes) =
            UnsyncedNotesDiscardDialogFragment().apply {
                arguments = Bundle(7).apply {
                    putString(ARG_DIRECTORY, directory)
                    putString(ARG_LABEL, label)
                    putInt(ARG_COUNT, shown.count)
                    putLong(ARG_SEQUENCE, shown.sequence)
                    putBoolean(ARG_UNREADABLE, shown.unreadable)
                    putBoolean(ARG_LISTED, shown.listed)
                    putStringArrayList(ARG_NAMES, ArrayList(shown.names.sorted()))
                }
            }
    }
}

/**
 * The stores [UnsyncedNotesFragment] shows, and the result of the last confirmation until the screen has shown it.
 * Every store call runs off the main thread. While a confirmation runs, the screen opens no other, and another one
 * that comes is ignored. When the model is cleared while one runs, the store still finishes it, and only its result is
 * dropped.
 */
internal class UnsyncedNotesViewModel(app: Application) : AndroidViewModel(app) {
    private val mutableListing = MutableLiveData<UnsyncedNotesListing>()
    val listing: LiveData<UnsyncedNotesListing> = mutableListing
    private val mutableResult = MutableLiveData<Result?>()
    val result: LiveData<Result?> = mutableResult

    /** How many reads were started; only the last one started is shown. */
    private var loads = 0

    /** Whether a confirmation runs: set by [answer], and cleared once its result is set. On the main thread only. */
    var answering = false
        private set

    fun load() {
        val load = ++loads
        viewModelScope.launch {
            val read = withContext(Dispatchers.IO) { UnsyncedNotesStores.read(getApplication()) }
            if (load == loads) mutableListing.value = read
        }
    }

    /** The user confirmed [shown] for the store in [directory]; [retry] after a discard that did not finish. */
    fun answer(directory: String, label: String?, shown: UnsyncedNotes, retry: Boolean) {
        if (answering) return
        answering = true
        mutableResult.value = null
        viewModelScope.launch {
            try {
                mutableResult.value = withContext(Dispatchers.IO) {
                    UnsyncedNotesStores.discard(getApplication(), directory, label, shown, retry)
                }
            } finally {
                answering = false
            }
            load()
        }
    }

    /** Retry sends the confirmation the user gave again; the store counts again and refuses what it does not cover. */
    fun retry(notFinished: Result.NotFinished) =
        answer(notFinished.directory, notFinished.label, notFinished.shown, retry = true)

    /** The screen has shown the result. */
    fun consume() {
        mutableResult.value = null
    }
}
