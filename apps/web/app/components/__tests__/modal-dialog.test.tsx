import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { emulateInert } from '@/app/lib/__tests__/inert-test-utils'
import { ModalDialog } from '../modal-dialog'

afterEach(() => {
  vi.restoreAllMocks()
})

function DialogHarness() {
  const [open, setOpen] = useState(false)

  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Open dialog</button>
      {open && (
        <ModalDialog
          title="Example dialog"
          description="An example modal dialog."
          onClose={() => setOpen(false)}
          closeOnEscape
        >
          <button type="button">Inside dialog</button>
        </ModalDialog>
      )}
    </>
  )
}

/** Mirrors the production shape: the opener lives in an inerted background. */
function InertBackgroundHarness({ passOpener }: { passOpener: boolean }) {
  const [open, setOpen] = useState(false)
  const [opener, setOpener] = useState<HTMLElement | null>(null)

  return (
    <>
      <div inert={open ? true : undefined} aria-hidden={open || undefined}>
        <button
          type="button"
          onClick={(event) => {
            setOpener(event.currentTarget)
            setOpen(true)
          }}
        >
          Open dialog
        </button>
      </div>
      {open && (
        <ModalDialog
          title="Example dialog"
          description="An example modal dialog."
          onClose={() => setOpen(false)}
          closeOnEscape
          restoreFocusTo={passOpener ? opener : undefined}
        >
          <button type="button">Inside dialog</button>
        </ModalDialog>
      )}
    </>
  )
}

describe('ModalDialog', () => {
  it('uses current dismissal props immediately, without replacing its Escape listener', () => {
    const initialClose = vi.fn()
    const latestClose = vi.fn()
    const keydownAdds: EventListenerOrEventListenerObject[] = []
    const keydownRemoves: EventListenerOrEventListenerObject[] = []
    const originalAdd = document.addEventListener.bind(document)
    const originalRemove = document.removeEventListener.bind(document)

    vi.spyOn(document, 'addEventListener').mockImplementation((type, listener, options) => {
      if (type === 'keydown') keydownAdds.push(listener)
      originalAdd(type, listener, options)
    })
    vi.spyOn(document, 'removeEventListener').mockImplementation((type, listener, options) => {
      if (type === 'keydown') keydownRemoves.push(listener)
      originalRemove(type, listener, options)
    })

    const { rerender, unmount } = render(
      <ModalDialog title="Example dialog" description="An example modal dialog." onClose={initialClose}>
        <button type="button">Inside dialog</button>
      </ModalDialog>,
    )

    const initialKeydownListenerCount = keydownAdds.length
    rerender(
      <ModalDialog title="Example dialog" description="An example modal dialog." onClose={latestClose} closeOnEscape closeOnBackdrop>
        <button type="button">Inside dialog</button>
      </ModalDialog>,
    )

    // This follows the false-to-true render synchronously, before another
    // effect turn could attach a replacement listener.
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(latestClose).toHaveBeenCalledTimes(1)
    expect(initialClose).not.toHaveBeenCalled()
    expect(keydownAdds).toHaveLength(initialKeydownListenerCount)

    fireEvent.click(screen.getByRole('dialog').previousElementSibling!)
    expect(latestClose).toHaveBeenCalledTimes(2)

    unmount()
    expect(keydownRemoves).toEqual(expect.arrayContaining(keydownAdds))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(latestClose).toHaveBeenCalledTimes(2)
  })

  it('restores focus to the opener after Escape closes the dialog', async () => {
    render(<DialogHarness />)

    const opener = screen.getByRole('button', { name: 'Open dialog' })
    opener.focus()
    fireEvent.click(opener)
    await waitFor(() => expect(document.activeElement).toHaveTextContent('Inside dialog'))

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(document.activeElement).toBe(opener)
  })

  it('restores focus to an opener the browser blurred when the background became inert', async () => {
    const inert = emulateInert()
    try {
      render(<InertBackgroundHarness passOpener />)

      const opener = screen.getByRole('button', { name: 'Open dialog' })
      opener.focus()
      fireEvent.click(opener)
      await waitFor(() => expect(document.activeElement).toHaveTextContent('Inside dialog'))
      // Inerting the background really did blur the opener, so the assertion
      // below cannot pass on jsdom's missing inert support alone.
      expect(inert.blurred).not.toHaveLength(0)

      fireEvent.keyDown(document, { key: 'Escape' })
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      expect(document.activeElement).toBe(opener)
    } finally {
      inert.restore()
    }
  })

  it('cannot recover the opener from document.activeElement once the background is inert', async () => {
    // Documents why callers that inert the background must pass restoreFocusTo:
    // by the time the focus trap effect runs, the opener is already blurred.
    const inert = emulateInert()
    try {
      render(<InertBackgroundHarness passOpener={false} />)

      const opener = screen.getByRole('button', { name: 'Open dialog' })
      opener.focus()
      fireEvent.click(opener)
      await waitFor(() => expect(document.activeElement).toHaveTextContent('Inside dialog'))

      fireEvent.keyDown(document, { key: 'Escape' })
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      expect(document.activeElement).not.toBe(opener)
    } finally {
      inert.restore()
    }
  })
})
