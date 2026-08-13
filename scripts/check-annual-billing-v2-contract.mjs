#!/usr/bin/env node
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

const PIN_FILE = 'contracts/annual-only-billing-v2.schema.sha256'
const SCHEMA_FILE = 'contracts/annual-only-billing-v2.schema.json'
const CLIENT_FILE = 'apps/web/app/lib/billing-v2.ts'
const AUTH_STORE_FILE = 'apps/web/app/stores/use-auth-store.ts'
const PAYMENT_PANEL_FILE = 'apps/web/app/components/payment-choice-panel.tsx'
const SIGNUP_PAGE_FILE = 'apps/web/app/(auth)/signup/page.tsx'
const PENDING_PAYMENT_FILE = 'apps/web/app/(auth)/signup/pending-payment/page.tsx'
const OFFER_PRESENTATION_FILE = 'apps/web/app/lib/annual-offer-presentation.ts'
const PUBLIC_ANALYTICS_FILE = 'apps/web/app/lib/public-analytics.ts'
const SHA256 = /^[0-9a-f]{64}$/
const assert = (condition, message) => { if (!condition) throw new Error(message) }

const canonicalPaths = {
  emailProofRequest: '/auth/signup-email-verifications/v2',
  emailProofConsume: '/auth/signup-email-verifications/v2/consume',
  anonymousOffer: '/auth/offers/v2',
  anonymousActivate: '/auth/offers/v2/activate',
  authenticatedOffer: '/subscription/offers/v2',
  authenticatedActivate: '/subscription/offers/v2/activate',
  provision: '/auth/provision/v2',
  paymentSession: '/auth/signup/payment-session/v2',
  finalize: '/auth/signup/finalize-payment/v2',
  authenticatedPaymentFlow: '/subscription/payment-flows/v2',
  paymentSessionCurrent: '/auth/signup/payment-session/v2/current',
  paymentSessionReconcile: '/auth/signup/payment-session/v2/reconcile',
  paymentSessionCancel: '/auth/signup/payment-session/v2/cancel',
}

function functionBlock(source, name) {
  const start = source.indexOf(`function ${name}(`)
  assert(start >= 0, `Missing ${name} exact-response guard`)
  const end = source.indexOf('\n}\n', start)
  assert(end >= 0, `Could not delimit ${name} exact-response guard`)
  return source.slice(start, end + 2)
}

export function checkAnnualBillingV2Contract(root = process.cwd()) {
  const schemaBytes = readFileSync(resolve(root, SCHEMA_FILE))
  const pinnedSha = readFileSync(resolve(root, PIN_FILE), 'utf8').trim()
  assert(SHA256.test(pinnedSha), `${PIN_FILE} must contain one lowercase SHA-256`)
  assert(createHash('sha256').update(schemaBytes).digest('hex') === pinnedSha, `${SCHEMA_FILE} does not match its pinned canonical SHA-256`)

  const schema = JSON.parse(schemaBytes)
  const definitions = schema?.$defs
  assert(definitions && typeof definitions === 'object', 'Canonical annual v2 schema lacks definitions')
  assert(JSON.stringify(Object.fromEntries(Object.entries(definitions.Paths.properties).map(([name, shape]) => [name, shape.const]))) === JSON.stringify(canonicalPaths), 'Canonical annual v2 endpoint map drifted')
  for (const name of ['OfferRequest', 'OfferResponse', 'EmailProofRequest', 'EmailProofConsume', 'EmailProofResponse', 'ActivateRequest', 'ActivateResponse', 'ProvisionRequest', 'ProvisionResponse', 'PaymentSessionRequest', 'StripePaymentSessionResponse', 'BtcpayPaymentSessionResponse', 'FinalizeRequest', 'FinalizeResponse', 'AuthenticatedFlowRequest', 'AuthenticatedStripeFlowResponse', 'AuthenticatedBtcpayFlowResponse', 'PaymentSessionRecoveryRequest', 'PaymentSessionRecoveryResponse']) {
    assert(definitions[name]?.additionalProperties === false, `${name} must remain a closed canonical object`)
  }
  assert(JSON.stringify(definitions.Disclosure.properties.periodEndRule.enum) === JSON.stringify(['activation_plus_trial', 'first_charge_plus_1_utc_calendar_year', 'confirmation_plus_1_utc_calendar_year', 'confirmation_bonus_then_1_utc_calendar_year']), 'Canonical disclosure period-end rules drifted')
  assert(JSON.stringify(definitions.EmailProofResponse.required) === JSON.stringify(['contractVersion', 'emailOwnershipToken', 'expiresAt']), 'Canonical email proof response drifted')
  assert(JSON.stringify(definitions.StripePaymentSessionResponse.required) === JSON.stringify(['contractVersion', 'kind', 'clientSecret', 'paymentSessionToken']), 'Canonical Stripe session response drifted')
  assert(JSON.stringify(definitions.BtcpayPaymentSessionResponse.required) === JSON.stringify(['contractVersion', 'kind', 'cryptoCheckoutUrl', 'cryptoInvoiceId', 'cryptoInvoiceLookupToken', 'paymentSessionToken']), 'Canonical BTCPay session response drifted')
  assert(JSON.stringify(definitions.AuthenticatedStripeFlowResponse.required) === JSON.stringify(['contractVersion', 'kind', 'authorityId', 'clientSecret']), 'Canonical authenticated Stripe flow drifted')
  assert(JSON.stringify(definitions.AuthenticatedBtcpayFlowResponse.required) === JSON.stringify(['contractVersion', 'kind', 'authorityId', 'checkoutUrl', 'invoiceId', 'invoiceLookupToken']), 'Canonical authenticated BTCPay flow drifted')
  assert(definitions.PaymentSessionRequest.properties.returnUrl?.$ref === '#/$defs/HttpUrl' && definitions.AuthenticatedFlowRequest.properties.returnUrl?.$ref === '#/$defs/HttpUrl', 'Payment return URLs must share the absolute HTTP(S) definition')
  assert(definitions.HttpUrl?.pattern === '^https?://', 'Payment return URL definition must be absolute HTTP(S)')

  const client = readFileSync(resolve(root, CLIENT_FILE), 'utf8')
  const authStore = readFileSync(resolve(root, AUTH_STORE_FILE), 'utf8')
  const publicV2Callers = `${client}\n${authStore}`
  for (const [name, route] of Object.entries(canonicalPaths)) {
    if (name === 'paymentSessionCurrent' || name === 'paymentSessionReconcile' || name === 'paymentSessionCancel') continue
    assert(publicV2Callers.includes(route), `Public v2 callers do not use canonical route ${route}`)
  }
  assert(client.includes('/auth/signup/payment-session/v2${path}') && client.includes("'/current'") && client.includes("'/reconcile'") && client.includes("'/cancel'"), 'Public v2 client does not use every canonical anonymous recovery route')
  for (const forbidden of ['/auth/email-ownership/v2', 'paymentSessionId', 'recoveryToken', 'cryptoLookupToken', 'confirmation_plus_365_days', 'trial_end_plus_365_days', 'payment_plus_365_days']) assert(!client.includes(forbidden), `Public v2 client contains non-canonical ${forbidden}`)
  assert(!/checkoutIntentToken: params\.checkoutIntentToken,[\s\S]{0,300}provider: params\.provider/.test(client), 'Public v2 payment request sends a caller-selected provider after activation froze authority')
  assert(client.includes('paymentSessionToken') && client.includes('cryptoInvoiceLookupToken') && client.includes('authorityId') && client.includes('checkoutUrl') && client.includes('invoiceId') && client.includes('invoiceLookupToken'), 'Public v2 client does not validate every canonical payment authority shape')

  const noCard = functionBlock(authStore, 'isExactNoCardProvision')
  const finalized = functionBlock(authStore, 'isExactV2PaidFinalization')
  assert(noCard.includes('earlyAdopter') && !noCard.includes('isAdmin'), 'No-card finalization must require earlyAdopter and must not invent isAdmin')
  assert(finalized.includes('earlyAdopter') && finalized.includes('isAdmin'), 'Paid finalization must require the exact canonical earlyAdopter and isAdmin fields')
  assert(!authStore.includes('payment.recoveryToken') && !authStore.includes('payment.cryptoLookupToken'), 'Signup persistence still uses non-canonical payment response fields')

  const paymentPanel = readFileSync(resolve(root, PAYMENT_PANEL_FILE), 'utf8')
  assert(!paymentPanel.includes('data.cryptoCheckoutUrl') && !paymentPanel.includes('data.cryptoInvoiceId') && !paymentPanel.includes('data.cryptoLookupToken'), 'Authenticated payment UI still expects signup-only BTCPay response fields')

  // The signed offer is the single public presentation authority.  These
  // byte-level controls make a pricing/provider regression noisy even before
  // component tests run against the standard offer.
  const signup = readFileSync(resolve(root, SIGNUP_PAGE_FILE), 'utf8')
  const pendingPayment = readFileSync(resolve(root, PENDING_PAYMENT_FILE), 'utf8')
  const presentation = readFileSync(resolve(root, OFFER_PRESENTATION_FILE), 'utf8')
  const analytics = readFileSync(resolve(root, PUBLIC_ANALYTICS_FILE), 'utf8')
  for (const source of [signup, paymentPanel]) {
    assert(source.includes('annualOfferPlanLabel') && source.includes('annualOfferAnnualLabel') && source.includes('annualOfferRenewalCopy'), 'Public annual UI must derive class, amount, and renewal copy from the canonical offer')
    assert(source.includes("isAnnualOfferProviderAvailable") && source.includes("'stripe'") && source.includes("'btcpay'"), 'Public annual UI must gate Stripe and BTCPay with canonical offer providers')
  }
  assert(signup.includes('annualOffer={annualOffer}'), 'Signup must pass the signed annual offer through StepChoosePlan')
  assert(pendingPayment.includes("isAnnualOfferProviderAvailable(offer.offer, 'btcpay', CRYPTO_CHECKOUT_ENABLED)"), 'Bitcoin restart must fail closed when a refreshed offer does not authorize BTCPay')
  for (const forbiddenPresentationConstant of [/&euro;36/, /€36(?:\.00)?(?:\/year)?/, /€3(?:\.00)?(?:\/month)?/, /Early Adopter(?: Plan)?/]) {
    assert(!forbiddenPresentationConstant.test(signup) && !forbiddenPresentationConstant.test(paymentPanel), `Public annual UI contains reintroduced fixed offer copy: ${forbiddenPresentationConstant}`)
  }
  assert(presentation.includes('annualAmountMinor') && presentation.includes('monthlyEquivalentMinor') && presentation.includes('customerClass') && presentation.includes('planId') && presentation.includes('providers'), 'Annual presentation helpers must use canonical class, plan, amount, and provider fields')
  assert(analytics.includes('annualOfferAnalyticsDimensions(offer)') && presentation.includes('plan_id') && presentation.includes('customer_class') && presentation.includes('annual_amount_minor') && presentation.includes('monthly_equivalent_minor'), 'Signup analytics must use non-identifying dimensions from the canonical offer')
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  try { checkAnnualBillingV2Contract() } catch (error) { process.stderr.write(`Annual billing v2 contract guard rejected: ${error instanceof Error ? error.message : 'unknown error'}\n`); process.exitCode = 1 }
}
