import assert from 'node:assert/strict'
import test from 'node:test'
import { checkAnnualBillingV2Contract } from './check-annual-billing-v2-contract.mjs'

test('the public annual client, persistence, and authenticated UI stay compatible with the pinned closed v2 wire contract', () => {
  assert.doesNotThrow(() => checkAnnualBillingV2Contract())
})
