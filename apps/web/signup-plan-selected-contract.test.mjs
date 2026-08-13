import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const source = await readFile(new URL('./app/(auth)/signup/page.tsx', import.meta.url), 'utf8')
function body(start, end) { return source.slice(source.indexOf(start), source.indexOf(end)) }

test('annual Plan Selected is recorded before the chosen payment checkout starts', () => {
  assert.match(body('const handleSelectCard', 'const handleSelectBitcoin'), /trackPlanSelected\(annualOfferDetails\)/)
  assert.match(body('const handleConfirmAnnualClaim', 'const handlePlanBack'), /trackCheckoutInitiated\(annualOffer\.offer, 'stripe'\)/)
  assert.match(body('const handleConfirmAnnualClaim', 'const handlePlanBack'), /trackCheckoutInitiated\(annualOffer\.offer, 'btcpay'\)/)
  assert.doesNotMatch(source, /trackPlanSelected\('monthly'\)/)
})
