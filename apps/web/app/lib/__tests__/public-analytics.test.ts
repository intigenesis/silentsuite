import { describe, expect, it } from 'vitest'

import {
  buildSignupPageviewPayload,
  buildCheckoutReturnedPayload,
  buildPlanSelectedPayload,
  buildSubscriptionManagementEntryPayload,
  canonicalizeCampaignParams,
  canonicalizeReferrer,
  classifyReferrer,
  sanitizedSignupPageUrl,
} from '../public-analytics'

describe('public analytics privacy contract', () => {
  it('canonicalizes only registered campaign parameters', () => {
    expect(
      canonicalizeCampaignParams('?utm_source=x&utm_medium=social&utm_campaign=beta-2026-q2'),
    ).toEqual({
      utm_source: 'social',
      utm_medium: 'organic',
      utm_campaign: 'beta-2026-q2',
    })
  })

  it('drops content, term, repeated, identity-shaped, and encoded values', () => {
    expect(
      canonicalizeCampaignParams(
        '?utm_source=x&utm_source=github&utm_medium=user%40example.com&utm_campaign=https%253A%252F%252Fevil.test&utm_content=secret&utm_term=private',
      ),
    ).toEqual({})
  })

  it('emits canonical fallback categories without retaining unknown raw strings', () => {
    expect(
      canonicalizeCampaignParams('?utm_source=conference&utm_medium=poster&utm_campaign=private-launch'),
    ).toEqual({
      utm_source: 'other',
      utm_medium: 'other',
      utm_campaign: 'other_campaign',
    })
  })

  it('strips all query and fragment data from signup page URLs', () => {
    expect(
      sanitizedSignupPageUrl(
        'https://app.silentsuite.io/signup?utm_source=github&utm_medium=referral&utm_content=email@example.com&returnTo=%2Fcalendar',
      ),
    ).toBe('https://app.silentsuite.io/signup')
  })

  it('uses the fixed signup registry URL instead of an arbitrary origin or nested path', () => {
    expect(sanitizedSignupPageUrl('https://previewapp.silentsuite.io/signup/plan?email=user@example.com#private')).toBe(
      'https://app.silentsuite.io/signup',
    )
    expect(sanitizedSignupPageUrl('https://evil.test/signup/customer@example.com')).toBe(
      'https://app.silentsuite.io/signup',
    )
  })

  it('classifies the referrer without retaining its path', () => {
    expect(classifyReferrer('https://github.com/silent-suite/silentsuite/issues/123?token=secret')).toBe(
      'github',
    )
    expect(classifyReferrer('https://new.reddit.com/r/privacy/comments/1/opaque')).toBe('other')
    expect(classifyReferrer('https://news.ycombinator.com/item?id=1')).toBe('other')
    expect(classifyReferrer('https://evil.test/reset/user@example.com')).toBe('other')
    expect(classifyReferrer('bad url')).toBeUndefined()
  })

  it('builds the exact identity-free signup pageview payload', () => {
    expect(buildSignupPageviewPayload(
      'https://app.silentsuite.io/signup?utm_source=github&utm_content=user@example.com&returnTo=/calendar',
      'https://github.com/silent-suite/silentsuite/issues/123?token=secret',
    )).toEqual({
      domain: 'app.silentsuite.io',
      name: 'pageview',
      url: 'https://app.silentsuite.io/signup',
      referrer: 'https://github.com/',
      props: { referrer_category: 'github', utm_source: 'github' },
    })
  })

  it('sends the fixed Google referrer for a recognized regional Google source', () => {
    expect(buildSignupPageviewPayload(
      'https://app.silentsuite.io/signup',
      'https://www.google.co.uk/search?q=silentsuite',
    )).toEqual({
      domain: 'app.silentsuite.io',
      name: 'pageview',
      url: 'https://app.silentsuite.io/signup',
      referrer: 'https://www.google.com/',
      props: { referrer_category: 'search' },
    })
  })

  it.each([
    ['https://www.bing.com/search?q=silentsuite', 'https://www.bing.com/'],
    ['https://duckduckgo.com/?q=silentsuite', 'https://duckduckgo.com/'],
    ['https://search.brave.com/search?q=silentsuite', 'https://search.brave.com/'],
    ['https://www.ecosia.org/search?q=silentsuite', 'https://www.ecosia.org/'],
    ['https://t.co/opaque-id', 'https://x.com/'],
    ['https://old.reddit.com/r/privacy/comments/1/opaque', 'https://www.reddit.com/'],
    ['https://redd.it/opaque-id', 'https://www.reddit.com/'],
    ['https://github.com/silent-suite/silentsuite', 'https://github.com/'],
    ['https://mastodon.social/@silent/opaque', 'https://mastodon.social/'],
    ['https://bsky.app/profile/example/post/opaque', 'https://bsky.app/'],
    ['https://alternativeto.net/software/silentsuite/', 'https://alternativeto.net/'],
    ['https://www.privacyguides.org/en/tools/', 'https://www.privacyguides.org/'],
    ['https://news.ycombinator.com/item?id=1', 'https://news.ycombinator.com/'],
  ])('maps recognized source %s to its fixed Plausible referrer', (raw, referrer) => {
    expect(canonicalizeReferrer(raw)).toBe(referrer)
  })

  it.each([
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
  ])('omits a standard referrer for unsafe or unrecognized input %s', (raw) => {
    expect(canonicalizeReferrer(raw)).toBeUndefined()
    expect(buildSignupPageviewPayload('https://app.silentsuite.io/signup', raw)).not.toHaveProperty('referrer')
  })

  it('creates only fixed commercial-funnel event payloads', () => {
    expect(buildPlanSelectedPayload('monthly')).toEqual({
      domain: 'app.silentsuite.io', name: 'Plan Selected', url: 'https://app.silentsuite.io/signup', props: { plan_class: 'monthly' },
    })
    expect(buildCheckoutReturnedPayload('cancelled', 'btcpay')).toEqual({
      domain: 'app.silentsuite.io', name: 'Checkout Returned', url: 'https://app.silentsuite.io/signup/cancel', props: { outcome: 'cancelled', payment_method: 'btcpay' },
    })
    expect(buildSubscriptionManagementEntryPayload()).toEqual({
      domain: 'app.silentsuite.io', name: 'Subscription Management Entry', url: 'https://app.silentsuite.io/settings/subscription', props: { surface: 'subscription_settings' },
    })
  })
})
