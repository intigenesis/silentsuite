#!/usr/bin/env node
import { appendFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const SHA = /^[0-9a-f]{40}$/
const TIMESTAMP = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/
const SERVED_IDENTITIES = Object.freeze([
  ['Web', 'https://app.silentsuite.io/api/deployment-identity'],
  ['Docs', 'https://docs.silentsuite.io/deployment-identity.json'],
])
const assert = (value, message) => { if (!value) throw new Error(message) }
const utcSecond = () => new Date().toISOString().replace(/\.\d{3}Z$/, 'Z')
const validTimestamp = (value) => typeof value === 'string' && TIMESTAMP.test(value) && !Number.isNaN(Date.parse(value)) && new Date(value).toISOString().replace(/\.000Z$/, 'Z') === value

function exactIdentity(value, expectedPublicSha) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === 1
    && Object.hasOwn(value, 'publicSha')
    && value.publicSha === expectedPublicSha
}

export async function verifyAnnualPublicServedIdentities({ expectedPublicSha, fetcher = fetch, now = utcSecond }) {
  assert(typeof expectedPublicSha === 'string' && SHA.test(expectedPublicSha), 'Expected public SHA is malformed')
  assert(typeof fetcher === 'function', 'Fresh served-identity fetcher is unavailable')
  for (const [surface, endpoint] of SERVED_IDENTITIES) {
    let response
    try {
      response = await fetcher(endpoint, {
        method: 'GET',
        cache: 'no-store',
        redirect: 'error',
        headers: { 'cache-control': 'no-cache', pragma: 'no-cache' },
      })
    } catch {
      throw new Error(`Fresh ${surface} served-identity probe failed`)
    }
    assert(response?.status === 200, `Fresh ${surface} served-identity status is invalid`)
    let body
    try { body = await response.json() } catch { throw new Error(`Fresh ${surface} served-identity body is invalid`) }
    assert(exactIdentity(body, expectedPublicSha), `Fresh ${surface} served-identity does not exactly match the expected public SHA`)
  }
  const clientServedAt = now()
  assert(validTimestamp(clientServedAt), 'Fresh public served client timestamp is invalid')
  return { clientServedAt }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  try {
    const { clientServedAt } = await verifyAnnualPublicServedIdentities({ expectedPublicSha: process.env.EXPECTED_PUBLIC_SHA })
    assert(typeof process.env.GITHUB_OUTPUT === 'string' && process.env.GITHUB_OUTPUT.length > 0, 'GitHub output path is missing')
    await appendFile(process.env.GITHUB_OUTPUT, `client_served_at=${clientServedAt}\n`, { mode: 0o600 })
  } catch (error) {
    process.stderr.write(`Fresh public served identity verification rejected: ${error instanceof Error ? error.message : 'unknown error'}\n`)
    process.exitCode = 1
  }
}
