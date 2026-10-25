import { describe, it, expect, beforeEach, vi } from 'vitest'
import { deserializeContact, type Contact } from '@silentsuite/core'
import { useContactStore, resetContactLifecycle } from '../use-contact-store'
import { useEtebaseStore } from '../use-etebase-store'
import { useContactListStore } from '../use-contact-list-store'
import { TEST_FINGERPRINT } from './offline-queue-store-test-utils'

// Yearless and text birthdays through the real contact store save paths (repair
// plan v2, D4/D5a): unrelated edits, Favorite, birthday edit/clear and import.
// Synthetic contacts only. BEHAVIOR messages are the regression; FIXTURE marks
// a broken harness. The text flag is optional, so these intersections stay
// type-correct before and after the field exists.
type FlaggedContact = Contact & { birthdayIsText?: boolean }

const toastMock = vi.hoisted(() => ({ showErrorToast: vi.fn() }))
vi.mock('@/app/stores/use-toast-store', () => toastMock)

vi.mock('@/app/stores/use-sync-store', () => ({
  useSyncStore: {
    getState: () => ({
      isOnline: false,
      simulateSyncCycle: vi.fn(),
    }),
  },
}))

const ID = 'remote-bday'
const APPLE = 'BDAY;X-APPLE-OMIT-YEAR=1604:1604-04-16'

const card = (version: '3.0' | '4.0', bday: string) =>
  ['BEGIN:VCARD', `VERSION:${version}`, `UID:${ID}`, 'FN:Synthetic Person', 'N:Person;Synthetic;;;', bday, 'END:VCARD'].join('\r\n')

const bdayLines = (wire: string) => wire.split('\r\n').filter((line) => /^BDAY[;:]/i.test(line))

/** Seeds the store the way a live refresh does: a contact read from item content. */
function seed(wire: string) {
  const contact = { ...deserializeContact(wire), id: ID, listId: 'contacts-1', revision: 'rev-1' }
  if (!contact.birthday) throw new Error('FIXTURE: seed card has no birthday')
  useContactStore.setState({ contacts: [contact] })
}

function onlineAccount() {
  let sequence = 0
  const updateItem = vi.fn(async (_type: string, _id: string, _content: string, options?: { onSaved?: (etag: string) => void }) => {
    options?.onSaved?.(`etag-${++sequence}`)
    return 'remote'
  })
  const createItemsBatch = vi.fn(async () => [] as string[])
  useEtebaseStore.setState({ account: {}, accountFingerprint: TEST_FINGERPRINT, itemCache: new Map([[ID, {}]]), updateItem, createItemsBatch } as any)
  return { updateItem, createItemsBatch }
}

function savedContent(updateItem: ReturnType<typeof onlineAccount>['updateItem'], call: number): string {
  const args = updateItem.mock.calls[call]
  if (!args) throw new Error(`FIXTURE: save ${call + 1} did not reach updateItem`)
  return args[2] as string
}

const stored = () => useContactStore.getState().contacts.find((contact) => contact.id === ID) as FlaggedContact | undefined

describe('contact store birthday saves', () => {
  beforeEach(() => {
    resetContactLifecycle()
    toastMock.showErrorToast.mockReset()
    useContactStore.setState({ contacts: [], syncStatus: 'synced', searchQuery: '', contactSaveStates: {} })
    useEtebaseStore.setState(useEtebaseStore.getInitialState(), true)
    useContactListStore.setState({ lists: [{ id: 'contacts-1', name: 'Contacts', color: '#fff', visible: true, accessLevel: 2 }], activeListId: 'contacts-1' })
  })

  it('writes a legacy yearless birthday in the marked form on an unrelated edit and on Favorite', async () => {
    const { updateItem } = onlineAccount()
    seed(card('3.0', 'BDAY:--0416'))

    await useContactStore.getState().updateContact(ID, { organization: 'Synthetic Org' })
    expect(bdayLines(savedContent(updateItem, 0)), 'BEHAVIOR: unrelated edit keeps the yearless birthday Android can read').toEqual([APPLE])

    await useContactStore.getState().setContactFavorite(ID, true)
    const favorite = savedContent(updateItem, 1)
    expect(favorite, 'FIXTURE: favorite save').toContain('X-SILENTSUITE-FAVORITE:1')
    expect(bdayLines(favorite), 'BEHAVIOR: Favorite keeps the yearless birthday').toEqual([APPLE])
  })

  for (const [label, version, line, value] of [
    ['date-shaped basic', '4.0', 'BDAY;VALUE=text:--0416', '--0416'],
    ['date-shaped extended', '4.0', 'BDAY;VALUE=text:--04-16', '--04-16'],
    ['ordinary', '4.0', 'BDAY;VALUE=text:circa 1800', 'circa 1800'],
  ] as const) {
    it(`keeps a ${label} text birthday as text through unrelated edits, Favorite and reload`, async () => {
      const { updateItem } = onlineAccount()
      seed(card(version, line))
      const textLine = `BDAY;VALUE=text:${value}`

      await useContactStore.getState().updateContact(ID, { notes: 'first' })
      await useContactStore.getState().updateContact(ID, { notes: 'second' })
      await useContactStore.getState().setContactFavorite(ID, true)

      for (const call of [0, 1, 2]) {
        expect(bdayLines(savedContent(updateItem, call)), `BEHAVIOR: save ${call + 1} keeps text`).toEqual([textLine])
      }
      expect(stored()?.birthdayIsText, 'BEHAVIOR: store keeps the text type').toBe(true)
      const reloaded = deserializeContact(savedContent(updateItem, 2)) as FlaggedContact
      expect([reloaded.birthday, reloaded.birthdayIsText], 'BEHAVIOR: reload keeps string and type').toEqual([value, true])
    })
  }

  it('clears a stale text type when the birthday itself is edited', async () => {
    const { updateItem } = onlineAccount()
    seed(card('4.0', 'BDAY;VALUE=text:--0416'))

    await useContactStore.getState().updateContact(ID, { birthday: '1990-04-16' })
    expect(bdayLines(savedContent(updateItem, 0)), 'BEHAVIOR: edited birthday is a date').toEqual(['BDAY:1990-04-16'])
    expect(stored()?.birthdayIsText === true, 'BEHAVIOR: stale text type cleared').toBe(false)

    await useContactStore.getState().updateContact(ID, { notes: 'after edit' })
    expect(bdayLines(savedContent(updateItem, 1)), 'BEHAVIOR: later unrelated save stays a date').toEqual(['BDAY:1990-04-16'])
  })

  it('clears a stale text type when the birthday is cleared', async () => {
    const { updateItem } = onlineAccount()
    seed(card('4.0', 'BDAY;VALUE=text:--0416'))

    await useContactStore.getState().updateContact(ID, { birthday: null })
    expect(bdayLines(savedContent(updateItem, 0)), 'BEHAVIOR: cleared birthday writes no line').toEqual([])
    expect(stored()?.birthdayIsText === true, 'BEHAVIOR: text type cleared with the value').toBe(false)

    await useContactStore.getState().updateContact(ID, { birthday: '2000-01-02' })
    expect(bdayLines(savedContent(updateItem, 1)), 'BEHAVIOR: new birthday is a date').toEqual(['BDAY:2000-01-02'])
  })

  it('keeps a full date unchanged through unrelated edits (control)', async () => {
    const { updateItem } = onlineAccount()
    seed(card('3.0', 'BDAY:1990-04-16'))
    await useContactStore.getState().updateContact(ID, { title: 'Synthetic' })
    await useContactStore.getState().setContactFavorite(ID, true)
    expect(bdayLines(savedContent(updateItem, 0)), 'BEHAVIOR: full date control').toEqual(['BDAY:1990-04-16'])
    expect(bdayLines(savedContent(updateItem, 1)), 'BEHAVIOR: full date control on Favorite').toEqual(['BDAY:1990-04-16'])
  })

  it('imports text-typed, yearless and unflagged birthdays into the right wire forms', async () => {
    const { createItemsBatch } = onlineAccount()
    // Shapes ContactImport builds; the flag is optional on the input type.
    const text = { displayName: 'Text', birthday: '--0416', birthdayIsText: true, listId: 'contacts-1' }
    const yearless = { displayName: 'Yearless', birthday: '--04-16', listId: 'contacts-1' }
    const legacyBasic = { displayName: 'Legacy', birthday: '--0416', listId: 'contacts-1' }
    const full = { displayName: 'Full', birthday: '1990-04-16', listId: 'contacts-1' }

    await useContactStore.getState().importContacts([text, yearless, legacyBasic, full])

    const batch = createItemsBatch.mock.calls[0] as unknown[] | undefined
    if (!batch) throw new Error('FIXTURE: import did not reach createItemsBatch')
    const contents = (batch[1] as Array<{ content: string }>).map((entry) => entry.content)
    expect(contents.map(bdayLines), 'BEHAVIOR: imported birthday wire forms').toEqual([
      ['BDAY;VALUE=text:--0416'], [APPLE], [APPLE], ['BDAY:1990-04-16'],
    ])
    const imported = useContactStore.getState().contacts.find((contact) => contact.displayName === 'Text') as FlaggedContact | undefined
    expect(imported?.birthdayIsText, 'BEHAVIOR: imported record keeps the text type').toBe(true)
  })
})
