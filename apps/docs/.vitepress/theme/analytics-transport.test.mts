import assert from 'node:assert/strict'
import test from 'node:test'

import {
  DOCS_ANALYTICS_CONTENT_TYPE,
  deliverDocsAnalyticsPayload,
  type DocsAnalyticsTransport,
} from './analytics-transport.mts'

type BeaconCall = { url: string; body: unknown }
type FetchCall = { url: string; init: Record<string, unknown> }

function recordingTransport(sendBeacon?: (url: string, body: string) => boolean) {
  const beaconCalls: BeaconCall[] = []
  const fetchCalls: FetchCall[] = []
  const transport: DocsAnalyticsTransport = {
    sendBeacon: sendBeacon
      ? (url, body) => {
          beaconCalls.push({ url, body })
          return sendBeacon(url, body)
        }
      : undefined,
    fetch: async (url, init) => {
      fetchCalls.push({ url, init: init as Record<string, unknown> })
    },
  }
  return { beaconCalls, fetchCalls, transport }
}

const ENDPOINT = '/api/event'
const PAYLOAD = '{"domain":"docs.silentsuite.io","name":"pageview","url":"https://docs.silentsuite.io/user-guide/faq"}'

test('delivers exactly once through Beacon when the queue accepts the payload', () => {
  const { beaconCalls, fetchCalls, transport } = recordingTransport(() => true)

  deliverDocsAnalyticsPayload(ENDPOINT, PAYLOAD, transport)

  assert.deepEqual(beaconCalls, [{ url: ENDPOINT, body: PAYLOAD }])
  assert.deepEqual(fetchCalls, [])
})

test('falls back to exactly one keepalive fetch when Beacon refuses the payload', () => {
  const { beaconCalls, fetchCalls, transport } = recordingTransport(() => false)

  deliverDocsAnalyticsPayload(ENDPOINT, PAYLOAD, transport)

  assert.equal(beaconCalls.length, 1)
  assert.equal(fetchCalls.length, 1)
  assert.deepEqual(fetchCalls[0], {
    url: ENDPOINT,
    init: {
      method: 'POST',
      headers: { 'Content-Type': DOCS_ANALYTICS_CONTENT_TYPE },
      body: PAYLOAD,
      keepalive: true,
    },
  })
})

test('falls back to exactly one keepalive fetch when Beacon is unavailable', () => {
  const { beaconCalls, fetchCalls, transport } = recordingTransport()

  deliverDocsAnalyticsPayload(ENDPOINT, PAYLOAD, transport)

  assert.deepEqual(beaconCalls, [])
  assert.equal(fetchCalls.length, 1)
  assert.equal(fetchCalls[0].init.keepalive, true)
})

test('falls back to exactly one keepalive fetch when Beacon throws', () => {
  const { fetchCalls, transport } = recordingTransport(() => {
    throw new TypeError('refused')
  })

  deliverDocsAnalyticsPayload(ENDPOINT, PAYLOAD, transport)

  assert.equal(fetchCalls.length, 1)
})

test('sends a plain JSON string body on both transports, never a Blob', () => {
  for (const beaconResult of [true, false]) {
    const { beaconCalls, fetchCalls, transport } = recordingTransport(() => beaconResult)
    deliverDocsAnalyticsPayload(ENDPOINT, PAYLOAD, transport)
    for (const body of [...beaconCalls.map((call) => call.body), ...fetchCalls.map((call) => call.init.body)]) {
      assert.equal(typeof body, 'string')
      assert.equal(body, PAYLOAD)
    }
  }
})

test('uses the CORS-simple text content type rather than application/json', () => {
  assert.equal(DOCS_ANALYTICS_CONTENT_TYPE, 'text/plain;charset=UTF-8')
})

test('sends nothing when the analytics endpoint is disabled', () => {
  const { beaconCalls, fetchCalls, transport } = recordingTransport(() => true)

  deliverDocsAnalyticsPayload('', PAYLOAD, transport)

  assert.deepEqual(beaconCalls, [])
  assert.deepEqual(fetchCalls, [])
})
