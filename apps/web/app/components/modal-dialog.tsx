'use client'

import { useEffect, useId, useRef, type ReactNode } from 'react'
import { useFocusTrap } from '@/app/lib/use-focus-trap'

interface ModalDialogProps {
  title: string
  description: string
  children: ReactNode
  onClose?: () => void
  closeOnEscape?: boolean
  closeOnBackdrop?: boolean
  className?: string
  /**
   * Element to restore focus to on close. Required from callers that mark the
   * background inert, which blurs the opener before any effect can read it.
   */
  restoreFocusTo?: HTMLElement | null
}

/**
 * Shared semantic shell for modal content. It intentionally reuses the
 * repository focus-trap primitive and leaves payment-flow cancellation to the
 * caller, rather than treating an overlay dismissal as provider cancellation.
 */
export function ModalDialog({
  title,
  description,
  children,
  onClose,
  closeOnEscape = false,
  closeOnBackdrop = false,
  className = '',
  restoreFocusTo,
}: ModalDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  // Keep the document-level handler installed for the complete mounted
  // lifetime. Updating these during render means a parent granting dismissal
  // cannot leave a render-to-effect window in which Escape is ignored.
  const closeOnEscapeRef = useRef(closeOnEscape)
  const closeOnBackdropRef = useRef(closeOnBackdrop)
  const onCloseRef = useRef(onClose)
  const titleId = useId()
  const descriptionId = useId()

  closeOnEscapeRef.current = closeOnEscape
  closeOnBackdropRef.current = closeOnBackdrop
  onCloseRef.current = onClose

  useFocusTrap(dialogRef, true, restoreFocusTo)

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && closeOnEscapeRef.current && onCloseRef.current) {
        event.preventDefault()
        onCloseRef.current()
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [])

  const handleBackdropClick = () => {
    if (closeOnBackdropRef.current) onCloseRef.current?.()
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        aria-hidden="true"
        className="absolute inset-0 bg-[rgb(var(--background))]/80 backdrop-blur-sm"
        onClick={handleBackdropClick}
      />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        className={`relative mx-4 w-full max-w-md rounded-xl border border-[rgb(var(--border))] bg-[rgb(var(--surface))] p-6 ${className}`}
      >
        <h2 id={titleId} className="text-lg font-semibold text-[rgb(var(--foreground))]">{title}</h2>
        <p id={descriptionId} className="sr-only">{description}</p>
        {children}
      </div>
    </div>
  )
}
