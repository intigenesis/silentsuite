/**
 * jsdom implements no part of `inert`: content inside an inert subtree stays
 * focusable and keeps focus when React marks its wrapper inert. Real browsers
 * do two things that matter to focus restoration, both during the same
 * mutation phase, i.e. before React runs any layout or passive effect:
 *
 *   1. the focused element inside the newly inert subtree is blurred, and
 *   2. it cannot be re-focused while the subtree stays inert — which also
 *      silently defeats React's own post-commit focus restoration.
 *
 * Without this emulation, "focus is restored to the opener" assertions pass in
 * jsdom while production restores nothing. Install it around any test that
 * asserts focus restoration across an inert background.
 */
export type InertEmulation = {
  /** Elements whose subtree lost focus because they were marked inert. */
  readonly blurred: Element[]
  restore: () => void
}

export function emulateInert(): InertEmulation {
  const originalSetAttribute = Element.prototype.setAttribute
  const originalFocus = HTMLElement.prototype.focus
  const blurred: Element[] = []

  HTMLElement.prototype.focus = function patchedFocus(this: HTMLElement, options?: FocusOptions) {
    if (this.closest('[inert]')) return
    originalFocus.call(this, options)
  }

  Element.prototype.setAttribute = function patchedSetAttribute(this: Element, name: string, value: string) {
    originalSetAttribute.call(this, name, value)
    if (name !== 'inert') return
    const active = document.activeElement
    if (active instanceof HTMLElement && active !== this && this.contains(active)) {
      blurred.push(this)
      active.blur()
    }
  }

  return {
    blurred,
    restore: () => {
      Element.prototype.setAttribute = originalSetAttribute
      HTMLElement.prototype.focus = originalFocus
    },
  }
}
