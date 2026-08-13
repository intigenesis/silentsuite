import { useEffect, useRef, type RefObject } from 'react'

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/**
 * Trap keyboard focus within a container element.
 * When the dialog is open, Tab and Shift+Tab cycle through focusable elements
 * inside the container, preventing focus from escaping to the background.
 *
 * `restoreFocusTo` is the element to return focus to on cleanup. Callers that
 * inert the background must pass the opener captured in their own click
 * handler: React applies `inert`/`aria-hidden` during the mutation phase, and
 * the browser blurs the focused opener before this passive effect ever runs,
 * so `document.activeElement` read from here is already `document.body`.
 */
export function useFocusTrap(
  ref: RefObject<HTMLElement | null>,
  active: boolean = true,
  restoreFocusTo?: HTMLElement | null,
) {
  const restoreFocusToRef = useRef(restoreFocusTo)
  restoreFocusToRef.current = restoreFocusTo

  useEffect(() => {
    if (!active) return
    const container = ref.current
    if (!container) return

    // Store the previously focused element to restore on cleanup
    const previouslyFocused = restoreFocusToRef.current ?? (document.activeElement as HTMLElement | null)

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key !== 'Tab' || !container) return

      const focusable = Array.from(
        container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ).filter((el) => {
        // `offsetParent` is null for fixed-position controls, so using it
        // alone lets focus escape otherwise valid modal dialogs. Preserve the
        // normal visibility test while accepting rendered fixed descendants.
        if (el.closest('[hidden], [aria-hidden="true"]')) return false
        if (el.offsetParent !== null) return true
        const style = window.getComputedStyle(el)
        return style.display !== 'none' && style.visibility !== 'hidden'
      })

      if (focusable.length === 0) {
        e.preventDefault()
        return
      }

      const first = focusable[0]!
      const last = focusable[focusable.length - 1]!

      if (e.shiftKey) {
        // Shift+Tab: if focus is on first element, wrap to last
        if (document.activeElement === first) {
          e.preventDefault()
          last.focus()
        }
      } else {
        // Tab: if focus is on last element, wrap to first
        if (document.activeElement === last) {
          e.preventDefault()
          first.focus()
        }
      }
    }

    // Auto-focus the first focusable element inside the container
    requestAnimationFrame(() => {
      if (!container) return
      const focusable = container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)
      // Try to focus the first input/textarea, or fall back to first focusable
      const firstInput = container.querySelector<HTMLElement>(
        'input:not([disabled]), textarea:not([disabled])',
      )
      if (firstInput) {
        firstInput.focus()
      } else if (focusable.length > 0) {
        focusable[0]!.focus()
      }
    })

    document.addEventListener('keydown', handleKeyDown)

    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      // Restore focus to the previously focused element
      previouslyFocused?.focus()
    }
  }, [ref, active])
}
