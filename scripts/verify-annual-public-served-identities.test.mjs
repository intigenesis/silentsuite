import assert from 'node:assert/strict'
import test from 'node:test'
import { verifyAnnualPublicServedIdentities } from './verify-annual-public-served-identities.mjs'

const expectedPublicSha = 'b'.repeat(40)
const web = 'https://app.silentsuite.io/api/deployment-identity'
const docs = 'https://docs.silentsuite.io/deployment-identity.json'
const response = (body, status = 200) => ({ status, json: async () => body })

test('derives clientServedAt only after fresh Web then Docs identity probes on the post-approval state', async () => {
  let servedSha = expectedPublicSha
  const calls = []
  const result = await verifyAnnualPublicServedIdentities({
    expectedPublicSha,
    fetcher: async (url, options) => {
      calls.push([url, options])
      if (url === web) {
        // Models an approval delay followed by a later deployment mutation.
        servedSha = expectedPublicSha
        return response({ publicSha: expectedPublicSha })
      }
      assert.equal(url, docs)
      return response({ publicSha: servedSha })
    },
    now: () => '2026-08-11T12:00:05Z',
  })
  assert.deepEqual(result, { clientServedAt: '2026-08-11T12:00:05Z' })
  assert.deepEqual(calls.map(([url]) => url), [web, docs])
  for (const [, options] of calls) assert.deepEqual(options, { method: 'GET', cache: 'no-store', redirect: 'error', headers: { 'cache-control': 'no-cache', pragma: 'no-cache' } })
})

test('fails closed for approval-delay mutation, network/status failure, and absent, malformed, or mismatched one-surface evidence', async () => {
  const failure = async (webReply, docsReply) => assert.rejects(
    verifyAnnualPublicServedIdentities({
      expectedPublicSha,
      fetcher: async (url) => {
        const reply = url === web ? webReply : docsReply
        if (reply instanceof Error) throw reply
        return reply
      },
    }),
    /Fresh (Web|Docs) served-identity/,
  )
  let servedSha = expectedPublicSha
  await assert.rejects(verifyAnnualPublicServedIdentities({
    expectedPublicSha,
    fetcher: async (url) => {
      if (url === web) return response({ publicSha: servedSha })
      servedSha = 'a'.repeat(40) // Models a deployment mutation after approval/Web verification.
      return response({ publicSha: servedSha })
    },
  }), /Fresh Docs served-identity/)
  await failure(new Error('network unavailable'), response({ publicSha: expectedPublicSha }))
  await failure(response({ publicSha: expectedPublicSha }, 503), response({ publicSha: expectedPublicSha }))
  await failure(response({ publicSha: expectedPublicSha }), response({}))
  await failure(response({ publicSha: expectedPublicSha }), response({ publicSha: expectedPublicSha, stale: false }))
})
