'use client'

import { useState } from 'react'
import { CreditCard, Clock, X } from 'lucide-react'
import { Button } from '@silentsuite/ui'

interface AddCardBannerProps {
  daysRemaining: number
  onChoosePayment: () => void
}

export default function AddCardBanner({ daysRemaining, onChoosePayment }: AddCardBannerProps) {
  const [dismissed, setDismissed] = useState(false)

  if (dismissed) return null

  return (
    <>
      <div className="flex items-center justify-between rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3">
        <div className="flex items-center gap-3">
          <Clock className="h-4 w-4 text-amber-700 dark:text-amber-400" />
          <div>
            <p className="text-sm text-amber-700 dark:text-amber-400">
              {daysRemaining} day{daysRemaining !== 1 ? 's' : ''} left in your trial
            </p>
            <p className="text-xs text-[rgb(var(--muted))]">
              Review your server-owned annual payment options and get 14 bonus days. If you do nothing, your account becomes read-only when the trial ends.
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" onClick={onChoosePayment}>
            <CreditCard className="h-3.5 w-3.5 mr-1.5" />
            Choose payment
          </Button>
          <button type="button" aria-label="Dismiss payment reminder" onClick={() => setDismissed(true)} className="text-[rgb(var(--muted))] hover:text-[rgb(var(--foreground))]">
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>
    </>
  )
}
