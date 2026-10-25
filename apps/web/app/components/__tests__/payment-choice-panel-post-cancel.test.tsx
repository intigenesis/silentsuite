import React from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import PaymentChoicePanel from '../payment-choice-panel'
import { useAuthStore } from '@/app/stores/use-auth-store'
import { bumpAccountEpoch, getAccountEpoch } from '@/app/lib/account-epoch'

vi.mock('@/app/lib/config', () => ({ BILLING_API_URL: 'https://billing.example.test', ETEBASE_SERVER_URL: 'https://server.example.test' }))
vi.mock('next/dynamic', () => ({ default: () => () => <div data-testid="stripe-payment-form" /> }))

const ACCOUNT = { id: 'account-a', email: 'a@example.invalid', planId: 'early_annual', rememberDevice: true, emailVerified: true }
const AUTHORITY_A = '3f2a1b4c-5d6e-4f70-8a91-b2c3d4e5f607'
const AUTHORITY_B = '7c1d2e3f-4a5b-4c6d-9e7f-8091a2b3c4d5'
const OFFER = '/subscription/offers/v2'
const CURRENT = '/subscription/payment-flows/current'
const CANCEL = '/subscription/payment-flows/cancel'
const SESSION = '/auth/session'
const PROFILE = 'account_confirmation_rules_v1'
const terms = {
  kind: 'charge_now', annualAmountMinor: 3600, firstChargeAmountMinor: 3600, renewalAmountMinor: 3600, monthlyEquivalentMinor: 300,
  currency: 'EUR', trialEndsAt: null, firstChargeAt: null, cancelBy: null, cancelByInclusive: false, autoRenew: true, prepaid: false,
  refundWindowDays: 30, bonusDays: 0, periodEndRule: PROFILE, renewalAt: null, entitlementEndsAt: null,
}
const baseOffer = {
  contractVersion: 2, requestId: 'e91a6d70-0d4e-4352-9bdc-426d1f76d771',
  offer: { planId: 'early_annual', customerClass: 'early', billingInterval: 'annual', annualAmountMinor: 3600, monthlyEquivalentMinor: 300,
    currency: 'EUR', providers: ['stripe', 'btcpay'], offerRevision: 1, offerToken: 'signed-offer', expiresAt: '2026-08-10T12:10:00Z' },
  accountTermsProfile: PROFILE,
}
// Crypto checkout stays disabled at module load, so only the card choice can render.
const validOffer = { ...baseOffer, accountDisclosures: { stripe: terms, btcpay: { ...terms, kind: 'prepaid', renewalAmountMinor: null, autoRenew: false, prepaid: true } } }
const flowA = {
  flowKind: 'stripe_pay_now', provider: 'stripe', status: 'provider_creation_reconciling', createdAt: '2026-08-10T12:00:00Z',
  cancellable: true, authorityId: AUTHORITY_A, disclosure: terms,
}
// A held, non-cancellable authority: its view offers only "Check payment status".
const heldFlowB = { ...flowA, authorityId: AUTHORITY_B, createdAt: '2026-08-10T12:30:00Z', status: 'provider_confirmed', cancellable: false }

// Existing client-owned copy only; #851 adds no strings.
const TERMS_UNAVAILABLE = 'Payment options are temporarily unavailable.'
const OFFER_LOAD_FAILED = 'The current annual offer could not be loaded. Retry to review current terms before continuing.'
const REFUSED = 'Payment options are not available for this account state.'
const CURRENT_FLOW_UNVERIFIED ='Could not verify whether a payment is already in progress. Retry before starting another payment.'

function barrier<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

type Reply = () => Response | Promise<Response>
type Call = { path: string; method: string; body: string | null; account: string | null; epoch: number }
let calls: Call[]
/** Each endpoint answers from its own ordered queue; the last reply repeats. */
function installTransport(queues: Record<string, Reply[]>) {
  const served: Record<string, number> = {}
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    const path = new URL(String(url)).pathname
    calls.push({ path, method: init?.method ?? 'GET', body: typeof init?.body === 'string' ? init.body : null,
      account: useAuthStore.getState().user?.id ?? null, epoch: getAccountEpoch() })
    const queue = queues[path]
    if (!queue) throw new Error(`unexpected test transport path ${path}`)
    const index = served[path] = (served[path] ?? -1) + 1
    return queue[Math.min(index, queue.length - 1)]!()
  })
}
const at = (path: string) => calls.filter(call => call.path === path)

function renderPanel(withoutClose = false) {
  const onCancel = vi.fn()
  const onDismissibilityChange = vi.fn()
  const { unmount } = render(<PaymentChoicePanel onSuccess={vi.fn()} onCancel={withoutClose ? undefined : onCancel} onDismissibilityChange={onDismissibilityChange} />)
  return { onCancel, unmount, dismissible: () => onDismissibilityChange.mock.calls.at(-1)?.[0] }
}
const creationButtons = () => screen.queryAllByRole('button', { name: /continue to card payment|bitcoin, lightning and monero/i })

let refresh: ReturnType<typeof vi.fn<() => Promise<boolean>>>
const originalRefresh = useAuthStore.getState().refreshSession

beforeEach(() => {
  calls = []
  bumpAccountEpoch()
  refresh = vi.fn(async () => true)
  useAuthStore.setState({ user: ACCOUNT, isAuthenticated: true, isLoading: false, error: null, pendingSignup: null,
    subscriptionStatus: 'cancelled', refreshSession: refresh })
  vi.stubGlobal('fetch', vi.fn())
})
afterEach(() => {
  useAuthStore.setState({ refreshSession: originalRefresh, user: null, isAuthenticated: false })
  vi.unstubAllGlobals()
})

/**
 * Cancels the displayed, bound authority A. The post-cancel status read proves
 * `flow: null` first; the offer reply is held until then and released with `offerReply`.
 */
async function cancelAuthorityA(offerReply: Reply, after: {
  initialOffer?: Reply; verificationCurrent?: Reply; current?: Reply[]; offer?: Reply[]; session?: Reply[]; refreshed?: true; withoutClose?: true
} = {}) {
  const verificationOffer = barrier<Response>()
  installTransport({
    ...after.session ? { [SESSION]: after.session } : {},
    [CURRENT]: [() => Response.json({ flow: flowA }), after.verificationCurrent ?? (() => Response.json({ flow: null })), ...after.current ?? []],
    // Any later re-verification receives a fresh copy of the same reply unless a test queues its own.
    [OFFER]: [after.initialOffer ?? (() => Response.json(validOffer)), () => verificationOffer.promise, ...after.offer ?? [offerReply]],
    [CANCEL]: [() => Response.json({ cancelled: true, flowKind: 'stripe_pay_now' })],
  })
  const panel = renderPanel(after.withoutClose)
  expect(await screen.findByRole('heading', { name: 'Payment already in progress' })).toBeInTheDocument()
  expect(panel.dismissible()).toBe(false)

  fireEvent.click(screen.getByRole('button', { name: 'Cancel card payment' }))
  await waitFor(() => expect(at(CURRENT)).toHaveLength(2))
  await waitFor(() => expect(at(OFFER)).toHaveLength(2))
  await act(async () => { verificationOffer.resolve(await offerReply()) })
  await waitFor(() => expect(screen.queryByText('Cancelling payment flow...')).not.toBeInTheDocument())

  // Exactly one authority-bound cancellation, all requests for the same account and epoch.
  expect(at(CANCEL).map(call => JSON.parse(call.body!))).toEqual([{ expectedAuthorityId: AUTHORITY_A }])
  expect(new Set(calls.map(call => `${call.account}@${call.epoch}`))).toEqual(new Set([`${ACCOUNT.id}@${getAccountEpoch()}`]))
  // Only a deliberately rejected initial offer may start the shared session refresh.
  if (after.refreshed) expect(refresh).toHaveBeenCalledTimes(1)
  else expect(refresh).not.toHaveBeenCalled()
  return panel
}

describe('PaymentChoicePanel joint post-cancellation proof (#851)', () => {
  it.each([
    ['R1: HTTP 200 offer whose signed account terms are invalid', () => Response.json({ ...baseOffer, accountDisclosures: {} }), TERMS_UNAVAILABLE],
    ['R1: HTTP 200 offer with null account terms', () => Response.json({ ...baseOffer, accountDisclosures: null }), TERMS_UNAVAILABLE],
    ['R2: offer 503', () => Response.json({ type: 'https://api.silentsuite.io/errors/offer-unavailable', status: 503 }, { status: 503 }), OFFER_LOAD_FAILED],
  ] as const)('after cancelling A, flow:null plus %s neither dismisses nor offers creation', async (_label, offerReply, copy) => {
    const { onCancel, dismissible } = await cancelAuthorityA(offerReply)

    expect(screen.getAllByText(copy).length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'Retry current annual offer' })).toBeInTheDocument()
    expect(creationButtons()).toHaveLength(0)
    // No implicit dismissal: the parent must not be told the chooser may close.
    expect(dismissible()).toBe(false)
    // The chooser close is not the plain dismissal shortcut while the proof is incomplete.
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument()
    const close = screen.getByRole('button', { name: 'Cancel any payment in progress and close' })
    fireEvent.click(close)
    // Settlement witness: the close re-ran the joint proof and it published its failure again.
    await waitFor(() => expect(at(OFFER)).toHaveLength(3))
    expect(at(CURRENT)).toHaveLength(3)
    expect(await screen.findByRole('button', { name: 'Cancel any payment in progress and close' })).toBeEnabled()
    expect(screen.getAllByText(copy).length).toBeGreaterThan(0)
    expect(onCancel).not.toHaveBeenCalled()
    expect(at(CANCEL)).toHaveLength(1)
    expect(creationButtons()).toHaveLength(0)
    expect(dismissible()).toBe(false)
  })

  it('B3: chooser close after an incomplete proof re-verifies without another cancellation and closes once on success', async () => {
    const invalid = () => Response.json({ ...baseOffer, accountDisclosures: null })
    const { onCancel, dismissible } = await cancelAuthorityA(invalid, { offer: [() => Response.json(validOffer)] })
    expect(dismissible()).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: 'Cancel any payment in progress and close' }))

    await waitFor(() => expect(onCancel).toHaveBeenCalledTimes(1))
    expect(at(CURRENT)).toHaveLength(3)
    expect(at(OFFER)).toHaveLength(3)
    expect(at(CANCEL)).toHaveLength(1)
  })

  it.each([
    ['offer half (invalid signed terms)', 'Retry current annual offer', TERMS_UNAVAILABLE,
      () => Response.json({ ...baseOffer, accountDisclosures: null }), {}],
    ['current-flow half (status 503)', 'Retry payment status', CURRENT_FLOW_UNVERIFIED,
      () => Response.json(validOffer), { verificationCurrent: () => Response.json({}, { status: 503 }), current: [() => Response.json({ flow: null })] }],
  ] as const)('R3/R4: Retry after a failed %s repeats the joint flow and offer proof without cancelling again', async (_half, retry, copy, offerReply, after) => {
    const { onCancel, dismissible } = await cancelAuthorityA(offerReply, { ...after, offer: [() => Response.json(validOffer)] })
    expect(screen.getAllByText(copy).length).toBeGreaterThan(0)
    expect(creationButtons()).toHaveLength(0)
    expect(dismissible()).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: retry }))

    expect(await screen.findByRole('button', { name: /continue to card payment/i })).toBeEnabled()
    expect(at(CURRENT)).toHaveLength(3)
    expect(at(OFFER)).toHaveLength(3)
    expect(at(CANCEL)).toHaveLength(1)
    expect(dismissible()).toBe(true)
    expect(screen.queryByText(copy)).not.toBeInTheDocument()
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('B2: a newly observed flow B is shown despite a failed offer and stays deliberately cancellable by its own authority', async () => {
    const flowB = { ...flowA, authorityId: AUTHORITY_B, createdAt: '2026-08-10T12:30:00Z' }
    const offer503 = () => Response.json({ type: 'https://api.silentsuite.io/errors/offer-unavailable', status: 503 }, { status: 503 })
    const { onCancel, dismissible } = await cancelAuthorityA(offer503, {
      verificationCurrent: () => Response.json({ flow: flowB }),
      current: [() => Response.json({ flow: null })],
      offer: [() => Response.json(validOffer)],
    })
    expect(screen.getByRole('heading', { name: 'Payment already in progress' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Resume card payment' })).toBeEnabled()
    expect(creationButtons()).toHaveLength(0)
    expect(dismissible()).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: 'Cancel card payment' }))

    expect(await screen.findByRole('button', { name: /continue to card payment/i })).toBeEnabled()
    expect(at(CANCEL).map(call => JSON.parse(call.body!))).toEqual([{ expectedAuthorityId: AUTHORITY_A }, { expectedAuthorityId: AUTHORITY_B }])
    expect(dismissible()).toBe(true)
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('B2: "Check payment status" on a held flow B repeats the joint proof without cancelling', async () => {
    const heldB = { ...flowA, authorityId: AUTHORITY_B, status: 'provider_confirmed', cancellable: false }
    const { dismissible } = await cancelAuthorityA(() => Response.json(validOffer), {
      verificationCurrent: () => Response.json({ flow: heldB }),
      current: [() => Response.json({ flow: null })],
      offer: [() => Response.json(validOffer)],
    })
    expect(screen.getByRole('button', { name: 'Cancel card payment' })).toBeDisabled()
    expect(dismissible()).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: 'Check payment status' }))

    expect(await screen.findByRole('button', { name: /continue to card payment/i })).toBeEnabled()
    expect(at(CURRENT)).toHaveLength(3)
    expect(at(OFFER)).toHaveLength(3)
    expect(at(CANCEL)).toHaveLength(1)
    expect(dismissible()).toBe(true)
  })

  it.each([
    ['stale success showing the held flow again', () => Response.json({ flow: heldFlowB })],
    ['stale failure', () => Response.json({}, { status: 503 })],
  ] as const)('R6: an older status read (%s) never overwrites a newer flow:null proof', async (_label, olderReply) => {
    const older = barrier<Response>()
    const newer = barrier<Response>()
    installTransport({
      [CURRENT]: [() => Response.json({ flow: heldFlowB }), () => older.promise, () => newer.promise],
      [OFFER]: [() => Response.json(validOffer)],
    })
    const { dismissible } = renderPanel()
    const check = await screen.findByRole('button', { name: 'Check payment status' })
    fireEvent.click(check)
    fireEvent.click(screen.getByRole('button', { name: 'Check payment status' }))
    await waitFor(() => expect(at(CURRENT)).toHaveLength(3))

    await act(async () => { newer.resolve(Response.json({ flow: null })) })
    expect(await screen.findByRole('button', { name: /continue to card payment/i })).toBeEnabled()
    await act(async () => { older.resolve(await olderReply()) })
    // Settlement witness: the older reply was consumed by the panel's own reader.
    await act(async () => { await older.promise })

    expect(screen.queryByRole('heading', { name: 'Payment already in progress' })).not.toBeInTheDocument()
    expect(screen.queryByText(CURRENT_FLOW_UNVERIFIED)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /continue to card payment/i })).toBeEnabled()
    expect(dismissible()).toBe(true)
    expect(at(CANCEL)).toHaveLength(0)
  })

  it.each([
    ['/auth/session 503 (retryable recovery)', 503, { flow: heldFlowB }],
    ['/auth/session 401 (sign-in required)', 401, { flow: null }],
  ] as const)('R7: an older verification\'s recovery error after %s never replaces a newer successful verification', async (_label, sessionStatus, newerFlow) => {
    const refreshGate = barrier<boolean>()
    refresh.mockImplementation(() => refreshGate.promise)
    const { onCancel } = await cancelAuthorityA(() => Response.json(validOffer), {
      verificationCurrent: () => Response.json({ flow: heldFlowB }),
      current: [() => Response.json({ flow: heldFlowB }), () => Response.json(newerFlow)],
      offer: [() => Response.json({ type: 'invalid-access-token' }, { status: 401 }), () => Response.json(validOffer)],
      session: [() => Response.json({}, { status: sessionStatus })],
    })
    // Older verification: its offer read is rejected and waits on the shared refresh.
    fireEvent.click(screen.getByRole('button', { name: 'Check payment status' }))
    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1))
    // Newer verification succeeds while the older one is still recovering.
    fireEvent.click(screen.getByRole('button', { name: 'Check payment status' }))
    await waitFor(() => expect(at(OFFER)).toHaveLength(4))
    if (newerFlow.flow) expect(await screen.findByRole('button', { name: 'Check payment status' })).toBeEnabled()
    else expect(await screen.findByRole('button', { name: /continue to card payment/i })).toBeEnabled()

    await act(async () => { refreshGate.resolve(true) })
    await waitFor(() => expect(at(SESSION)).toHaveLength(1))
    // Settlement witness: the older verification finished and was superseded.
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })

    expect(screen.queryByRole('link', { name: /sign in/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry current annual offer' })).not.toBeInTheDocument()
    if (newerFlow.flow) expect(screen.getByRole('heading', { name: 'Payment already in progress' })).toBeInTheDocument()
    else expect(screen.getByRole('button', { name: /continue to card payment/i })).toBeEnabled()
    expect(at(CANCEL)).toHaveLength(1)
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('R7: the initial offer load\'s late sign-in-required error never replaces a newer successful post-cancel verification', async () => {
    const refreshGate = barrier<boolean>()
    refresh.mockImplementation(() => refreshGate.promise)
    const { onCancel, dismissible } = await cancelAuthorityA(() => Response.json(validOffer), {
      initialOffer: () => Response.json({ type: 'invalid-access-token' }, { status: 401 }),
      refreshed: true,
      session: [() => Response.json({}, { status: 401 })],
    })
    expect(await screen.findByRole('button', { name: /continue to card payment/i })).toBeEnabled()

    await act(async () => { refreshGate.resolve(true) })
    await waitFor(() => expect(at(SESSION)).toHaveLength(1))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })

    expect(screen.queryByRole('link', { name: /sign in/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /continue to card payment/i })).toBeEnabled()
    expect(dismissible()).toBe(true)
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('R7: an older status read\'s late sign-in-required error never replaces a newer flow:null proof', async () => {
    const refreshGate = barrier<boolean>()
    refresh.mockImplementation(() => refreshGate.promise)
    installTransport({
      [CURRENT]: [() => Response.json({ flow: heldFlowB }), () => Response.json({ type: 'invalid-access-token' }, { status: 401 }), () => Response.json({ flow: null })],
      [OFFER]: [() => Response.json(validOffer)],
      [SESSION]: [() => Response.json({}, { status: 401 })],
    })
    const { dismissible } = renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'Check payment status' }))
    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: 'Check payment status' }))
    expect(await screen.findByRole('button', { name: /continue to card payment/i })).toBeEnabled()

    await act(async () => { refreshGate.resolve(true) })
    await waitFor(() => expect(at(SESSION)).toHaveLength(1))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })

    expect(screen.queryByRole('link', { name: /sign in/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /continue to card payment/i })).toBeEnabled()
    expect(dismissible()).toBe(true)
  })

  it.each([
    ['newer failure, stale success', 'fail', 'ok'],
    ['newer success, stale failure', 'ok', 'fail'],
  ] as const)('R6: overlapping "Check payment status" verifications publish only the newest verdict (%s)', async (_label, newest, stale) => {
    const olderCurrent = barrier<Response>(); const olderOffer = barrier<Response>()
    const newerCurrent = barrier<Response>(); const newerOffer = barrier<Response>()
    const { onCancel, dismissible } = await cancelAuthorityA(() => Response.json(validOffer), {
      verificationCurrent: () => Response.json({ flow: heldFlowB }),
      current: [() => olderCurrent.promise, () => newerCurrent.promise],
      offer: [() => olderOffer.promise, () => newerOffer.promise],
    })
    fireEvent.click(screen.getByRole('button', { name: 'Check payment status' }))
    fireEvent.click(screen.getByRole('button', { name: 'Check payment status' }))
    await waitFor(() => expect(at(OFFER)).toHaveLength(4))
    const settle = async (current: ReturnType<typeof barrier<Response>>, offer: ReturnType<typeof barrier<Response>>, kind: 'ok' | 'fail') => {
      await act(async () => {
        current.resolve(kind === 'ok' ? Response.json({ flow: null }) : Response.json({}, { status: 503 }))
        offer.resolve(Response.json(validOffer))
        await Promise.all([current.promise, offer.promise])
      })
    }
    await settle(newerCurrent, newerOffer, newest)
    if (newest === 'ok') expect(await screen.findByRole('button', { name: /continue to card payment/i })).toBeEnabled()
    else expect(await screen.findByText(CURRENT_FLOW_UNVERIFIED)).toBeInTheDocument()
    await settle(olderCurrent, olderOffer, stale)
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })

    if (newest === 'ok') {
      expect(screen.getByRole('button', { name: /continue to card payment/i })).toBeEnabled()
      expect(screen.queryByText(CURRENT_FLOW_UNVERIFIED)).not.toBeInTheDocument()
      expect(dismissible()).toBe(true)
    } else {
      expect(screen.getByText(CURRENT_FLOW_UNVERIFIED)).toBeInTheDocument()
      expect(creationButtons()).toHaveLength(0)
      expect(dismissible()).toBe(false)
    }
    expect(at(CANCEL)).toHaveLength(1)
    expect(onCancel).not.toHaveBeenCalled()
  })

  it.each([
    ['older success', () => Response.json({ ...validOffer, offer: { ...baseOffer.offer, planId: 'standard_annual', customerClass: 'standard', annualAmountMinor: 4800, monthlyEquivalentMinor: 400, providers: ['stripe'] }, accountDisclosures: { stripe: { ...terms, annualAmountMinor: 4800, firstChargeAmountMinor: 4800, renewalAmountMinor: 4800, monthlyEquivalentMinor: 400 } } })],
    ['invalid signed terms', () => Response.json({ ...baseOffer, accountDisclosures: null })],
    ['error', () => Response.json({ type: 'https://api.silentsuite.io/errors/offer-unavailable', status: 503 }, { status: 503 })],
  ] as const)('guard: a late initial offer (%s) never replaces the joint verdict', async (_label, late) => {
    const initial = barrier<Response>()
    const { dismissible } = await cancelAuthorityA(() => Response.json(validOffer), { initialOffer: () => initial.promise })
    expect(await screen.findByRole('button', { name: /continue to card payment/i })).toBeEnabled()

    await act(async () => { initial.resolve(await late()); await initial.promise })
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })

    expect(screen.getByRole('button', { name: /continue to card payment/i })).toBeEnabled()
    expect(screen.getByText(/^€36\.00\/year/)).toBeInTheDocument()
    expect(screen.queryByText(/€48\.00/)).not.toBeInTheDocument()
    expect(screen.queryByText(TERMS_UNAVAILABLE)).not.toBeInTheDocument()
    expect(screen.queryByText(OFFER_LOAD_FAILED)).not.toBeInTheDocument()
    expect(dismissible()).toBe(true)
  })

  it.each([
    ['unmount', (panel: { unmount: () => void }) => panel.unmount()],
    ['logout', () => useAuthStore.setState({ user: null, isAuthenticated: false })],
    ['account replacement', () => useAuthStore.setState({ user: { ...ACCOUNT, id: 'account-b', email: 'b@example.invalid' } })],
    ['same-account epoch change', () => { bumpAccountEpoch() }],
  ] as const)('guard: %s during a chooser-close verification publishes nothing and never dismisses', async (label, boundary) => {
    const closeCurrent = barrier<Response>(); const closeOffer = barrier<Response>()
    const panel = await cancelAuthorityA(() => Response.json({ ...baseOffer, accountDisclosures: null }), {
      current: [() => closeCurrent.promise], offer: [() => closeOffer.promise],
    })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel any payment in progress and close' }))
    await waitFor(() => expect(at(OFFER)).toHaveLength(3))

    act(() => { boundary(panel) })
    await act(async () => {
      closeCurrent.resolve(Response.json({ flow: null }))
      closeOffer.resolve(Response.json(validOffer))
      await Promise.all([closeCurrent.promise, closeOffer.promise])
      await new Promise(resolve => setTimeout(resolve, 0))
    })

    expect(panel.onCancel).not.toHaveBeenCalled()
    expect(creationButtons()).toHaveLength(0)
    expect(at(CANCEL)).toHaveLength(1)
    if (label === 'logout' || label === 'account replacement') expect(screen.getByText(/Payment account changed/)).toBeInTheDocument()
  })

  it('guard: a double chooser close dismisses at most once and never cancels again', async () => {
    const { onCancel } = await cancelAuthorityA(() => Response.json({ ...baseOffer, accountDisclosures: null }), { offer: [() => Response.json(validOffer)] })
    const close = screen.getByRole('button', { name: 'Cancel any payment in progress and close' })
    fireEvent.click(close)
    fireEvent.click(close)
    await waitFor(() => expect(onCancel).toHaveBeenCalledTimes(1))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(at(OFFER)).toHaveLength(3)
    expect(at(CANCEL)).toHaveLength(1)
  })

  it.each([
    ['refused', () => Response.json({ type: 'https://api.silentsuite.io/errors/plan-not-purchasable' }, { status: 409 }), {}, REFUSED, false, true],
    ['refused without onCancel', () => Response.json({ type: 'https://api.silentsuite.io/errors/plan-not-purchasable' }, { status: 409 }), { withoutClose: true }, REFUSED, false, false],
    ['retryable session recovery', () => Response.json({ type: 'invalid-access-token' }, { status: 401 }), { refreshResult: false }, OFFER_LOAD_FAILED, true, true],
    ['transient without onCancel', () => Response.json({}, { status: 503 }), { withoutClose: true }, OFFER_LOAD_FAILED, true, false],
  ] as const)('B4: a %s offer verdict renders only its existing controls', async (_label, offerReply, options, copy, retry, close) => {
    if ('refreshResult' in options) refresh.mockImplementation(async () => options.refreshResult)
    const { onCancel, dismissible } = await cancelAuthorityA(offerReply, {
      withoutClose: 'withoutClose' in options ? true : undefined,
      refreshed: 'refreshResult' in options ? true : undefined,
    })
    expect(screen.getAllByText(copy).length).toBeGreaterThan(0)
    expect(screen.queryAllByRole('button', { name: 'Retry current annual offer' })).toHaveLength(retry ? 1 : 0)
    expect(screen.queryAllByRole('button', { name: 'Cancel any payment in progress and close' })).toHaveLength(close ? 1 : 0)
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument()
    expect(creationButtons()).toHaveLength(0)
    expect(dismissible()).toBe(false)
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('B4: a sign-in-required verification shows only the existing sign-in screen', async () => {
    const { onCancel, dismissible } = await cancelAuthorityA(() => Response.json({ type: 'invalid-access-token' }, { status: 401 }), {
      session: [() => Response.json({}, { status: 401 })],
      refreshed: true,
    })
    expect(screen.getByRole('link', { name: /sign in again/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancel any payment in progress and close' })).not.toBeInTheDocument()
    expect(creationButtons()).toHaveLength(0)
    expect(dismissible()).toBe(false)
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('control: after cancelling A, flow:null plus a valid offer restores creation and dismissal', async () => {
    const { onCancel, dismissible } = await cancelAuthorityA(() => Response.json(validOffer))

    expect(await screen.findByRole('button', { name: /continue to card payment/i })).toBeEnabled()
    expect(dismissible()).toBe(true)
    expect(screen.queryByText(TERMS_UNAVAILABLE)).not.toBeInTheDocument()
    expect(screen.queryByText(OFFER_LOAD_FAILED)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(at(CANCEL)).toHaveLength(1)
  })
})
