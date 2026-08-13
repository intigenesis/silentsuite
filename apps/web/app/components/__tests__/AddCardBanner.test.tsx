import React from 'react'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import AddCardBanner from '../add-card-banner'

vi.mock('@silentsuite/ui', () => ({
  Button: ({ children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) => <button {...props}>{children}</button>,
}))

vi.mock('lucide-react', () => ({
  CreditCard: () => <svg />,
  Clock: () => <svg />,
  X: () => <svg />,
  Crown: () => <svg />,
  Lock: () => <svg />,
  Zap: () => <svg />,
}))

describe('AddCardBanner', () => {
  it('forwards its no-card CTA to the page-owned payment chooser without mounting a second overlay', () => {
    const onChoosePayment = vi.fn()
    render(<AddCardBanner daysRemaining={3} onChoosePayment={onChoosePayment} />)

    fireEvent.click(screen.getByRole('button', { name: /choose payment/i }))

    expect(onChoosePayment).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText('Pay now + 14 bonus days')).not.toBeInTheDocument()
  })

  it('keeps only the banner dismissal local', () => {
    render(<AddCardBanner daysRemaining={1} onChoosePayment={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: /dismiss payment reminder/i }))

    expect(screen.queryByRole('button', { name: /choose payment/i })).not.toBeInTheDocument()
  })
})
