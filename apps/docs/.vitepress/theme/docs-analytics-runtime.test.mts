import assert from 'node:assert/strict'
import test from 'node:test'

import { readFile } from 'node:fs/promises'

import { buildDocsPageviewPayload, canonicalDocsPath, canonicalizeDocsReferrer, createDocsPageviewTracker } from './public-analytics.mts'

test('registers only repository-owned canonical document paths', () => {
  assert.equal(canonicalDocsPath('/user-guide/faq/?q=private#answer'), '/user-guide/faq')
  assert.equal(canonicalDocsPath('/'), '/')
  assert.equal(canonicalDocsPath('/local-search?q=secret'), undefined)
  assert.equal(canonicalDocsPath('/not-a-document'), undefined)
})

test('sends initial and successful route pageviews without duplicate consecutive delivery', () => {
  const deliveries: string[] = []
  const tracker = createDocsPageviewTracker((path) => deliveries.push(path))

  tracker('/user-guide/faq?query=private#heading')
  tracker('/user-guide/faq/')
  tracker('/self-hosting')
  tracker('/not-a-document')
  tracker('/user-guide/faq')

  assert.deepEqual(deliveries, ['/user-guide/faq', '/self-hosting', '/user-guide/faq'])
})

test('builds an exact docs pageview with the fixed Google referrer', () => {
  assert.deepEqual(
    buildDocsPageviewPayload('/user-guide/faq', 'https://www.google.co.uk/search?q=silentsuite'),
    {
      domain: 'docs.silentsuite.io',
      name: 'pageview',
      url: 'https://docs.silentsuite.io/user-guide/faq',
      referrer: 'https://www.google.com/',
    },
  )
})

test('maps all admitted docs sources to fixed Plausible referrers', () => {
  for (const [raw, referrer] of [
    ['https://www.bing.com/search?q=silentsuite', 'https://www.bing.com/'],
    ['https://duckduckgo.com/?q=silentsuite', 'https://duckduckgo.com/'],
    ['https://search.brave.com/search?q=silentsuite', 'https://search.brave.com/'],
    ['https://www.ecosia.org/search?q=silentsuite', 'https://www.ecosia.org/'],
    ['https://twitter.com/silentsuite/status/opaque', 'https://x.com/'],
    ['https://t.co/opaque-id', 'https://x.com/'],
    ['https://new.reddit.com/r/privacy/comments/1/opaque', 'https://www.reddit.com/'],
    ['https://redd.it/opaque-id', 'https://www.reddit.com/'],
    ['https://github.com/silent-suite/silentsuite', 'https://github.com/'],
    ['https://mastodon.social/@silent/opaque', 'https://mastodon.social/'],
    ['https://bsky.app/profile/example/post/opaque', 'https://bsky.app/'],
    ['https://alternativeto.net/software/silentsuite/', 'https://alternativeto.net/'],
    ['https://www.privacyguides.org/en/tools/', 'https://www.privacyguides.org/'],
    ['https://news.ycombinator.com/item?id=1', 'https://news.ycombinator.com/'],
  ] as const) assert.equal(canonicalizeDocsReferrer(raw), referrer)
})

test('omits docs referrer for unsafe or unrecognized sources', () => {
  for (const raw of [
    '', 'not a url', 'https://google.com.evil.example/search', 'https://google.zip/search',
    'https://google.dev/search', 'https://google.co.io/search', 'https://google.ab.cd/search',
    'https://x.com.evil.example/post',
    'https://reddit.com.evil.example/r/privacy', 'https://gοogle.com/search', 'https://ｇoogle.com/search',
    'https://google。com/search', 'https://google%E3%80%82com/search', 'https://google%2ecom/search', 'https://xn--googl-fsa.com/search',
    'https:///google%2ecom/search', 'https:////google%2ecom/search',
    'https:google%2ecom/search', 'https:google%E3%80%82com/search', 'https:github.com:443/private',
    'https://user:password@github.com/private', 'https://github.com:443/private',
    ' https://github.com:443/private', 'https://github.com:443\\private', 'https://127.0.0.1/private',
    'https://localhost/private', 'ftp://github.com/private', 'javascript://github.com/private',
  ]) {
    assert.equal(canonicalizeDocsReferrer(raw), undefined)
    assert.equal('referrer' in buildDocsPageviewPayload('/user-guide/faq', raw), false)
  }
})

test('runtime passes document.referrer into docs pageview construction', async () => {
  const source = await readFile(new URL('./index.mts', import.meta.url), 'utf8')
  assert.match(source, /sendDocsPageview\(path, document\.referrer\)/)
  assert.match(source, /buildDocsPageviewPayload\(path, rawReferrer\)/)
  assert.match(source, /if \(!pageview\) return/)
  assert.match(source, /JSON\.stringify\(pageview\)/)
})

test('docs payload builder rejects paths outside the repository-owned registry', () => {
  assert.equal(buildDocsPageviewPayload('/reset/user@example.com?token=private', 'https://www.google.com/'), undefined)
})
