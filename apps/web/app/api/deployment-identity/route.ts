import { NextResponse } from 'next/server'

export const dynamic = 'force-dynamic'

const SHA = /^[0-9a-f]{40}$/

/** Non-secret identity for post-deploy verification of externally served bytes. */
export function GET() {
  const publicSha = process.env.SILENTSUITE_PUBLIC_BUILD_SHA
  if (!publicSha || !SHA.test(publicSha)) {
    return NextResponse.json({ error: 'deployment_identity_unavailable' }, { status: 503 })
  }
  return NextResponse.json({ publicSha }, { headers: { 'Cache-Control': 'no-store, max-age=0' } })
}
