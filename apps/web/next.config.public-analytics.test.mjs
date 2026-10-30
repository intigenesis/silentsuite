import assert from 'node:assert/strict'
import test from 'node:test'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const nextConfig = require('./next.config.js')

function sourceMatches(source, pathname) {
  const expression = source.replace('/:path*', '(?:/.*)?').replaceAll('/', '\\/')
  return new RegExp(`^${expression}$`).test(pathname)
}

async function appliedCsp(pathname) {
  const rules = await nextConfig.headers()
  return rules
    .filter((rule) => sourceMatches(rule.source, pathname))
    .flatMap((rule) => rule.headers)
    .filter((header) => header.key === 'Content-Security-Policy')
    .at(-1)?.value
}

// These assertions check configured route intent only. Browsers also enforce the
// separate edge policy on the same document, so neither presence nor absence of a
// token here proves what a served page can actually reach.
async function connectSources(pathname) {
  const directives = (await appliedCsp(pathname)).split(';').map((directive) => directive.trim()).filter(Boolean)
  assert.equal(directives.length, 1, `${pathname} route policy must stay connect-src only`)
  const [name, ...sources] = directives[0].split(/\s+/)
  assert.equal(name, 'connect-src')
  return [...sources].sort()
}

// `https:` admits any HTTPS origin, including user-selected Etebase servers on any
// port or base path, and therefore also Plausible. Private routes no longer rely on
// CSP to deny analytics; the named hosts below only document intended destinations.
// `data:` lets libsodium 0.7.6 fetch its embedded base64 WASM without a CSP violation.
const hosted = ["'self'", 'https://api.silentsuite.io', 'https://server.silentsuite.io', 'wss://server.silentsuite.io', 'https://*.sentry.io', 'https:', 'data:']
const signup = [...hosted, 'https://plausible.silentsuite.io', 'https://api.stripe.com']
const subscription = [...hosted, 'https://plausible.silentsuite.io']

test('route connect-src admits custom HTTPS servers and keeps the named hosted sources', async () => {
  const expected = {
    '/signup': signup,
    '/signup/plan': signup,
    '/settings/subscription': subscription,
  }
  for (const path of ['/calendar', '/calendar/week', '/contacts', '/contacts/abc', '/tasks', '/notes', '/notes/abc', '/settings', '/settings/account']) {
    expected[path] = hosted
  }
  for (const [path, sources] of Object.entries(expected)) {
    assert.deepEqual(await connectSources(path), [...sources].sort(), path)
  }
})

test('route connect-src adds no plaintext, wildcard or WebSocket scheme source', async () => {
  for (const path of ['/signup', '/calendar', '/contacts', '/tasks', '/notes', '/settings', '/settings/subscription']) {
    const sources = await connectSources(path)
    for (const unintended of ['http:', '*', 'wss:', 'ws:', 'http://localhost:*', 'blob:', 'filesystem:']) {
      assert.ok(!sources.includes(unintended), `${path} must not allow ${unintended}`)
    }
  }
})

// A browser fetches a data: URL only when every enforced connect-src allows it, so
// the route policy is one necessary half. The edge policy needs the same token.
test('libsodium data: WASM fetch needs data: in both route and edge connect-src', async () => {
  const allowsData = (sources) => sources.includes('data:')
  const edgeWithout = ["'self'", 'https:']
  const edgeWith = [...edgeWithout, 'data:']
  for (const path of ['/signup', '/calendar', '/contacts', '/tasks', '/notes', '/settings', '/settings/subscription']) {
    const route = await connectSources(path)
    assert.ok(allowsData(route), `${path} route policy must allow data:`)
    assert.equal([route, edgeWithout].every(allowsData), false, `${path} stays blocked while the edge omits data:`)
    assert.equal([route, edgeWith].every(allowsData), true, `${path} is allowed once both policies admit data:`)
  }
})

test('login keeps the edge policy alone', async () => {
  assert.equal(await appliedCsp('/login'), undefined)
})

test('self-hosted builds emit no route CSP', async () => {
  const previous = process.env.NEXT_PUBLIC_SELF_HOSTED
  process.env.NEXT_PUBLIC_SELF_HOSTED = 'true'
  try {
    assert.deepEqual(await nextConfig.headers(), [])
  } finally {
    if (previous === undefined) delete process.env.NEXT_PUBLIC_SELF_HOSTED
    else process.env.NEXT_PUBLIC_SELF_HOSTED = previous
  }
})
