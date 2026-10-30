import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeAll, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import TasksPage from '../page'
import { renderWithIntl } from '@/src/__tests__/render-with-intl'
import messages from '@/messages/en.json'
import type { Task } from '@silentsuite/core'
import type { TaskList } from '@/app/stores/use-task-list-store'
import { useEtebaseStore } from '@/app/stores/use-etebase-store'

// Mobile reachability smoke test for the Tasks page (epic #295). jsdom does not
// evaluate CSS media queries, so these assert that the primary create action and
// the collection switcher render with their accessible labels and carry the
// expected responsive / touch-target classes, complementing manual width QA.
// Accessible names that are backed by next-intl messages are resolved from the
// message catalog so the tests do not couple to English copy.

const manageTaskLists = messages.Collections.manageTaskLists

const storeMock = vi.hoisted(() => ({
  taskState: {
    tasks: [] as Task[],
    createTask: vi.fn(),
    updateTask: vi.fn(),
    deleteTask: vi.fn(),
    toggleComplete: vi.fn(),
  },
  taskListState: {
    lists: [] as TaskList[],
    activeListId: null as string | null,
  },
  syncState: {
    isOnline: true,
  },
  authState: {
    canWrite: vi.fn(() => true),
  },
}))

vi.mock('@/app/stores/use-task-store', () => ({
  useTaskStore: (selector: (state: typeof storeMock.taskState) => unknown) => selector(storeMock.taskState),
}))

vi.mock('@/app/stores/use-task-list-store', () => ({
  useTaskListStore: (selector: (state: typeof storeMock.taskListState) => unknown) => selector(storeMock.taskListState),
}))

vi.mock('@/app/stores/use-sync-store', () => ({
  useSyncStore: (selector: (state: typeof storeMock.syncState) => unknown) => selector(storeMock.syncState),
}))

vi.mock('@/app/stores/use-auth-store', () => ({
  useAuthStore: (selector: (state: typeof storeMock.authState) => unknown) => selector(storeMock.authState),
}))

vi.mock('@/app/components/PullToRefresh', () => ({
  PullToRefresh: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}))

vi.mock('@/app/components/MobileCollectionSheet', () => ({
  MobileCollectionSheet: ({ open }: { open: boolean }) => (open ? <div data-testid="collection-sheet" /> : null),
}))

describe('TasksPage mobile reachability', () => {
  beforeEach(() => {
    useEtebaseStore.setState(useEtebaseStore.getInitialState(), true)
    storeMock.taskState.tasks = []
    storeMock.authState.canWrite.mockReturnValue(true)
  })

  it('derives its skeleton from the tasks domain and skips it for cached tasks (#747)', () => {
    const hasSkeleton = () => document.querySelector('.skeleton-shimmer') !== null
    const view = renderWithIntl(<TasksPage />)
    expect(hasSkeleton()).toBe(true)

    storeMock.taskState.tasks = [{ id: 'cached', uid: 'cached', title: 'Cached task', listId: 'default' } as Task]
    view.rerender(<TasksPage />)
    expect(hasSkeleton()).toBe(false)

    storeMock.taskState.tasks = []
    act(() => useEtebaseStore.setState((state) => ({ domainLoadState: { ...state.domainLoadState, tasks: 'loaded' } })))
    view.rerender(<TasksPage />)
    expect(hasSkeleton()).toBe(false)
  })

  it('exposes the primary create action on all widths', () => {
    renderWithIntl(<TasksPage />)
    const createButton = screen.getByRole('button', { name: 'New task' })
    expect(createButton).toBeInTheDocument()
    // Not hidden behind a desktop-only breakpoint.
    expect(createButton.className).not.toMatch(/(^|\s)hidden(\s|$)/)
    expect(createButton.className).not.toContain('md:flex')
  })

  it('exposes an inline quick-add on mobile', () => {
    renderWithIntl(<TasksPage />)
    expect(screen.getByPlaceholderText('Add a task...')).toBeInTheDocument()
  })

  it('exposes a mobile collection switcher with a 44px touch target', () => {
    renderWithIntl(<TasksPage />)
    const folderButton = screen.getByRole('button', { name: manageTaskLists })
    expect(folderButton).toBeInTheDocument()
    // Mobile-only control that meets the minimum touch target.
    expect(folderButton.className).toContain('md:hidden')
    expect(folderButton.className).toContain('touch-target')
  })
})

// C3b-R12/R13 (PLAN U6, U7, U8). The store boundary is the existing mock; each create is a
// controlled deferred promise. The store contract itself (TaskSaveError.unconfirmed) is proven
// in use-task-store.test.ts and the node SDK integration test.
describe('C3b task drafts survive an unsaved or unconfirmed create', () => {
  let TaskSaveError: typeof import('@/app/stores/use-task-store').TaskSaveError
  const unconfirmedMessage = /(couldn't|could not) confirm/i
  const definiteMessage = /Task not saved/i

  beforeAll(async () => {
    ;({ TaskSaveError } = await vi.importActual<typeof import('@/app/stores/use-task-store')>('@/app/stores/use-task-store'))
  })

  beforeEach(() => {
    useEtebaseStore.setState(useEtebaseStore.getInitialState(), true)
    storeMock.taskState.tasks = []
    storeMock.authState.canWrite.mockReturnValue(true)
    storeMock.taskState.createTask.mockReset()
  })

  /**
   * The next createTask stays pending until the test settles it. Teardown resolves it, never
   * rejects it, so a handler that does not await it can never surface an unhandled rejection.
   */
  function deferCreate() {
    let resolve!: (task: Task) => void
    let reject!: (reason: unknown) => void
    const pending = new Promise<Task>((resolvePending, rejectPending) => { resolve = resolvePending; reject = rejectPending })
    storeMock.taskState.createTask.mockImplementationOnce(() => pending)
    onTestFinished(() => resolve({ id: 'settled-at-test-end', title: 'settled' } as Task))
    return { resolve, reject }
  }

  const dialog = () => screen.queryByRole('dialog', { name: 'New Task' })
  const backdrop = () => document.querySelector('div[aria-hidden="true"][class*="bg-black/40"]')

  async function openDraft() {
    renderWithIntl(<TasksPage />)
    fireEvent.click(screen.getByRole('button', { name: 'New task' }))
    fireEvent.change(screen.getByPlaceholderText('Task title'), { target: { value: 'Draft title' } })
    fireEvent.change(screen.getByPlaceholderText('Add notes...'), { target: { value: 'Draft notes' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(storeMock.taskState.createTask).toHaveBeenCalledTimes(1))
    expect(storeMock.taskState.createTask).toHaveBeenCalledWith(expect.objectContaining({ title: 'Draft title', description: 'Draft notes' }))
  }

  it.each([
    ['unconfirmed', true, unconfirmedMessage, definiteMessage],
    ['known unsaved', false, definiteMessage, unconfirmedMessage],
  ] as const)('C3b-R12 dialog: a pending create keeps the draft open and guarded, and an %s outcome keeps every field', async (_label, unconfirmed, shown, notShown) => {
    const create = deferCreate()
    await openDraft()

    // Pending: the draft stays mounted, Create cannot dispatch again, and nothing can discard it.
    expect(dialog()).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.click(backdrop()!)
    expect(dialog()).not.toBeNull()
    expect(storeMock.taskState.createTask).toHaveBeenCalledTimes(1)

    await act(async () => { create.reject(new TaskSaveError(unconfirmed)) })

    const message = await screen.findByText(shown)
    if (unconfirmed) expect(message.textContent).toMatch(/(appears|already)/i)
    expect(screen.queryByText(notShown)).toBeNull()
    expect(dialog()).not.toBeNull()
    expect(screen.getByPlaceholderText('Task title')).toHaveValue('Draft title')
    expect(screen.getByPlaceholderText('Add notes...')).toHaveValue('Draft notes')
    expect(screen.getByRole('button', { name: 'Create' })).toBeEnabled()
  })

  it('C3b-R12 dialog control: an acknowledged create closes the dialog without an outcome message', async () => {
    const create = deferCreate()
    await openDraft()

    await act(async () => { create.resolve({ id: 'remote-task-1', title: 'Draft title' } as Task) })

    await waitFor(() => expect(dialog()).toBeNull())
    expect(screen.queryByText(unconfirmedMessage)).toBeNull()
    expect(screen.queryByText(definiteMessage)).toBeNull()
  })

  /** Every native control in the open dialog: its header actions and all draft fields, nested editors included. */
  const draftControls = () => Array.from(dialog()!.querySelectorAll<HTMLElement>('input, textarea, select, button'))
  const describeControl = (control: HTMLElement) =>
    `${control.tagName.toLowerCase()} ${control.getAttribute('aria-label') ?? control.getAttribute('placeholder') ?? control.textContent?.trim() ?? ''}`

  it('C3b-R12 dialog: while a create is pending no draft control accepts input, so the acknowledged create closes on exactly the submitted draft', async () => {
    const create = deferCreate()
    await openDraft()

    // Pending: native disabled semantics refuse every edit, so no newer input can exist to be discarded.
    const controls = draftControls()
    expect(controls.length).toBeGreaterThan(4)
    for (const control of controls) expect(control, describeControl(control)).toBeDisabled()
    expect(screen.getByPlaceholderText('Task title')).toHaveValue('Draft title')
    expect(screen.getByPlaceholderText('Add notes...')).toHaveValue('Draft notes')

    await act(async () => { create.resolve({ id: 'remote-task-1', title: 'Draft title' } as Task) })

    await waitFor(() => expect(dialog()).toBeNull())
    expect(storeMock.taskState.createTask).toHaveBeenCalledTimes(1)
  })

  it('C3b-R12 dialog: a rejected create makes the submitted draft editable again', async () => {
    const create = deferCreate()
    renderWithIntl(<TasksPage />)
    fireEvent.click(screen.getByRole('button', { name: 'New task' }))
    fireEvent.change(screen.getByPlaceholderText('Task title'), { target: { value: 'Draft title' } })
    fireEvent.change(screen.getByPlaceholderText('Add notes...'), { target: { value: 'Draft notes' } })
    // Which controls accept input before submitting; the same ones must accept it again after a rejection.
    const editableBefore = draftControls().map((control) => !control.matches(':disabled'))
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(storeMock.taskState.createTask).toHaveBeenCalledTimes(1))

    for (const control of draftControls()) expect(control, describeControl(control)).toBeDisabled()

    await act(async () => { create.reject(new TaskSaveError(true)) })
    await screen.findByText(unconfirmedMessage)

    const controls = draftControls()
    expect(controls).toHaveLength(editableBefore.length)
    controls.forEach((control, index) => {
      if (editableBefore[index]) expect(control, describeControl(control)).toBeEnabled()
      else expect(control, describeControl(control)).toBeDisabled()
    })
    expect(screen.getByPlaceholderText('Task title')).toHaveValue('Draft title')
    expect(screen.getByPlaceholderText('Add notes...')).toHaveValue('Draft notes')
    fireEvent.change(screen.getByPlaceholderText('Task title'), { target: { value: 'Draft title, revised' } })
    expect(screen.getByPlaceholderText('Task title')).toHaveValue('Draft title, revised')
  })

  async function submitQuickAdd(title: string) {
    renderWithIntl(<TasksPage />)
    const input = screen.getByPlaceholderText('Add a task...')
    fireEvent.change(input, { target: { value: title } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(storeMock.taskState.createTask).toHaveBeenCalledTimes(1))
    return input
  }

  it.each([
    ['unconfirmed', true, unconfirmedMessage, definiteMessage],
    ['known unsaved', false, definiteMessage, unconfirmedMessage],
  ] as const)('C3b-R13 quick add: a pending create keeps the submitted title read-only, and an %s outcome leaves it editable', async (_label, unconfirmed, shown, notShown) => {
    const create = deferCreate()
    const input = await submitQuickAdd('First')

    // Pending: the submitted title stays, the field cannot be edited, and Add (with it Enter) cannot submit again.
    expect(input).toHaveValue('First')
    expect(input).toHaveAttribute('readonly')
    const add = screen.getByRole('button', { name: 'Add' })
    expect(add).toBeDisabled()
    fireEvent.click(add)
    expect(storeMock.taskState.createTask).toHaveBeenCalledTimes(1)

    await act(async () => { create.reject(new TaskSaveError(unconfirmed)) })

    const message = await screen.findByText(shown)
    if (unconfirmed) expect(message.textContent).toMatch(/(appears|already)/i)
    expect(screen.queryByText(notShown)).toBeNull()
    expect(input).toHaveValue('First')
    expect(input).not.toHaveAttribute('readonly')
    expect(screen.getByRole('button', { name: 'Add' })).toBeEnabled()
  })

  it('C3b-R13 quick add: an acknowledged create clears the title only once it resolves', async () => {
    const create = deferCreate()
    const input = await submitQuickAdd('First')

    expect(input).toHaveValue('First')

    await act(async () => { create.resolve({ id: 'remote-task-1', title: 'First' } as Task) })

    await waitFor(() => expect(input).toHaveValue(''))
    expect(input).not.toHaveAttribute('readonly')
    expect(screen.queryByText(unconfirmedMessage)).toBeNull()
    expect(screen.queryByText(definiteMessage)).toBeNull()
  })

  it('C3b-R13 quick add control: an acknowledged create sends the title and leaves the field empty', async () => {
    storeMock.taskState.createTask.mockResolvedValueOnce({ id: 'remote-task-1', title: 'Quick' } as Task)
    const input = await submitQuickAdd('Quick')

    expect(storeMock.taskState.createTask).toHaveBeenCalledWith(expect.objectContaining({ title: 'Quick' }))
    await waitFor(() => expect(input).toHaveValue(''))
    expect(screen.queryByText(unconfirmedMessage)).toBeNull()
    expect(screen.queryByText(definiteMessage)).toBeNull()
  })
})
