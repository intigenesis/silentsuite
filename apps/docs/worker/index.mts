// Production docs Worker: a first-party same-origin relay in front of the shared
// analytics upstream, with every other request falling through to the static assets
// binding. The browser never learns the upstream host, and the relay never trusts a
// browser-supplied measurement identity: admitted requests are rebuilt from the closed
// route, referrer, event, and property vocabularies that the docs theme already owns.
import {
  DOCS_ANALYTICS_DOMAIN,
  DOCS_ANALYTICS_ORIGIN,
  DOCS_CANONICAL_REFERRERS,
  REGISTERED_DOCS_PATHS,
  classifyDocsOutboundEvent,
  type DocsCanonicalReferrer,
} from '../.vitepress/theme/public-analytics.mts'

export const DOCS_RELAY_PATH = '/api/event'
export const DOCS_PLAUSIBLE_ENDPOINT = 'https://plausible.silentsuite.io/api/event'
export const DOCS_RELAY_MAX_BODY_BYTES = 1024

const ADMITTED_CONTENT_TYPE = 'text/plain;charset=utf-8'
const CANONICAL_EVENT_URL = `${DOCS_ANALYTICS_ORIGIN}/`

export type DocsWorkerEnv = {
  ASSETS: { fetch: (request: Request) => Promise<Response> }
}

export type DocsWorkerContext = {
  waitUntil: (promise: Promise<unknown>) => void
}

export type DocsUpstreamFetch = (url: string, init: RequestInit) => Promise<Response>

type JsonRecord = Record<string, unknown>

type DocsRelayPayload =
  | { domain: typeof DOCS_ANALYTICS_DOMAIN; name: 'pageview'; url: string; referrer?: DocsCanonicalReferrer }
  | { domain: typeof DOCS_ANALYTICS_DOMAIN; name: string; url: string; props: Record<string, string> }

function isPlainRecord(value: unknown): value is JsonRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function hasExactKeys(record: JsonRecord, required: readonly string[], optional: readonly string[] = []): boolean {
  const keys = Object.keys(record)
  return required.every((key) => keys.includes(key))
    && keys.every((key) => required.includes(key) || optional.includes(key))
}

function rebuildPageview(payload: JsonRecord): DocsRelayPayload | undefined {
  if (!hasExactKeys(payload, ['domain', 'name', 'url'], ['referrer'])) return undefined
  const url = payload.url
  if (typeof url !== 'string' || !url.startsWith(`${DOCS_ANALYTICS_ORIGIN}/`)) return undefined
  const path = url.slice(DOCS_ANALYTICS_ORIGIN.length)
  if (!REGISTERED_DOCS_PATHS.has(path)) return undefined

  const rebuilt: DocsRelayPayload = {
    domain: DOCS_ANALYTICS_DOMAIN,
    name: 'pageview',
    url: `${DOCS_ANALYTICS_ORIGIN}${path}`,
  }
  if (!('referrer' in payload)) return rebuilt

  const referrer = DOCS_CANONICAL_REFERRERS.find((candidate) => candidate === payload.referrer)
  return referrer ? { ...rebuilt, referrer } : undefined
}

function rebuildOutboundEvent(payload: JsonRecord): DocsRelayPayload | undefined {
  if (!hasExactKeys(payload, ['domain', 'name', 'path', 'href'])) return undefined
  if (payload.name !== 'outbound' || typeof payload.path !== 'string' || typeof payload.href !== 'string') return undefined
  const signature = classifyDocsOutboundEvent(payload.href, payload.path)
  if (!signature) return undefined

  return {
    domain: DOCS_ANALYTICS_DOMAIN,
    name: signature.event,
    url: CANONICAL_EVENT_URL,
    props: { ...signature.props },
  }
}

export function rebuildDocsRelayPayload(rawBody: string): string | undefined {
  let parsed: unknown
  try {
    parsed = JSON.parse(rawBody)
  } catch {
    return undefined
  }
  if (!isPlainRecord(parsed)) return undefined
  if (parsed.domain !== DOCS_ANALYTICS_DOMAIN || typeof parsed.name !== 'string') return undefined

  const rebuilt = parsed.name === 'pageview' ? rebuildPageview(parsed) : rebuildOutboundEvent(parsed)
  return rebuilt ? JSON.stringify(rebuilt) : undefined
}

function hasAdmittedContentType(request: Request): boolean {
  const contentType = request.headers.get('Content-Type')
  return contentType !== null && contentType.replace(/\s+/g, '').toLowerCase() === ADMITTED_CONTENT_TYPE
}

async function readCappedBody(request: Request): Promise<string | undefined> {
  const declaredLength = request.headers.get('Content-Length')
  if (declaredLength !== null
    && (!/^\d+$/.test(declaredLength) || Number(declaredLength) > DOCS_RELAY_MAX_BODY_BYTES)) return undefined

  const stream = request.body
  if (!stream) return undefined
  const reader = stream.getReader()
  const chunks: Uint8Array[] = []
  let size = 0
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > DOCS_RELAY_MAX_BODY_BYTES) {
        await reader.cancel()
        return undefined
      }
      chunks.push(value)
    }
    const body = new Uint8Array(size)
    let offset = 0
    for (const chunk of chunks) {
      body.set(chunk, offset)
      offset += chunk.byteLength
    }
    return new TextDecoder('utf-8', { fatal: true }).decode(body)
  } catch {
    return undefined
  }
}

async function admitRelayPayload(request: Request, url: URL): Promise<string | undefined> {
  if (url.protocol !== 'https:' || url.host !== DOCS_ANALYTICS_DOMAIN) return undefined
  if (request.headers.get('Origin') !== DOCS_ANALYTICS_ORIGIN) return undefined
  if (!hasAdmittedContentType(request)) return undefined

  const rawBody = await readCappedBody(request)
  return rawBody === undefined ? undefined : rebuildDocsRelayPayload(rawBody)
}

// Only the edge-observed client address reaches the upstream, under the header it reads
// for visitor attribution. Every other request header is dropped rather than relayed.
async function forwardAdmittedPayload(
  upstreamFetch: DocsUpstreamFetch,
  payload: string,
  request: Request,
): Promise<void> {
  const headers = new Headers({ 'Content-Type': 'application/json' })
  const clientAddress = request.headers.get('CF-Connecting-IP')
  if (clientAddress) headers.set('X-Plausible-IP', clientAddress)
  const userAgent = request.headers.get('User-Agent')
  if (userAgent) headers.set('User-Agent', userAgent)

  try {
    await upstreamFetch(DOCS_PLAUSIBLE_ENDPOINT, { method: 'POST', headers, body: payload })
  } catch {
    // Upstream outcomes are never surfaced, retried, or recorded.
  }
}

function relayResponse(status: 204 | 405): Response {
  return new Response(null, { status, headers: { 'Cache-Control': 'no-store' } })
}

export function createDocsRelayWorker(
  upstreamFetch: DocsUpstreamFetch = (url, init) => fetch(url, init),
) {
  return {
    async fetch(request: Request, env: DocsWorkerEnv, ctx?: DocsWorkerContext): Promise<Response> {
      const url = new URL(request.url)
      if (url.pathname !== DOCS_RELAY_PATH || url.search !== '') return env.ASSETS.fetch(request)
      if (request.method !== 'POST') return relayResponse(405)

      const payload = await admitRelayPayload(request, url)
      if (payload) {
        const forwarding = forwardAdmittedPayload(upstreamFetch, payload, request)
        if (ctx) ctx.waitUntil(forwarding)
        else await forwarding
      }
      return relayResponse(204)
    },
  }
}

export default createDocsRelayWorker()
