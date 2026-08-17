// Beacon keeps the request out of the unload race, but a refused queue returns false
// instead of throwing, so the result must be inspected before the fallback is skipped.
// The body stays a plain string with a CORS-simple content type so the same-origin
// relay admits both transports under identical rules.
export const DOCS_ANALYTICS_CONTENT_TYPE = 'text/plain;charset=UTF-8'

export type DocsAnalyticsTransport = {
  sendBeacon?: (url: string, body: string) => boolean
  fetch: (url: string, init: Record<string, unknown>) => Promise<unknown>
}

function browserTransport(): DocsAnalyticsTransport {
  return {
    sendBeacon: typeof navigator !== 'undefined' && typeof navigator.sendBeacon === 'function'
      ? (url, body) => navigator.sendBeacon(url, body)
      : undefined,
    fetch: (url, init) => fetch(url, init as RequestInit),
  }
}

function queuedThroughBeacon(endpoint: string, payload: string, transport: DocsAnalyticsTransport): boolean {
  if (!transport.sendBeacon) return false
  try {
    return transport.sendBeacon(endpoint, payload) === true
  } catch {
    return false
  }
}

export function deliverDocsAnalyticsPayload(
  endpoint: string,
  payload: string,
  transport: DocsAnalyticsTransport = browserTransport(),
): void {
  if (!endpoint) return
  if (queuedThroughBeacon(endpoint, payload, transport)) return
  void transport.fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': DOCS_ANALYTICS_CONTENT_TYPE },
    body: payload,
    keepalive: true,
  })
}
