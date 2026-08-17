import { readdir, readFile } from 'node:fs/promises'
import path from 'node:path'

const UPSTREAM = 'plausible.silentsuite.io'
const REQUIRED_ENABLED_TEXT = ['docs.silentsuite.io', 'pageview', 'Hosted App Click', 'Android Download Click', 'GitHub Click']
const PROHIBITED_TEXT = ['utm_content', 'utm_term', 'referrer_category']

function normalizeResolvableLiteralText(text) {
  let normalized = text
    .replace(/\\(?:x2[fF]|u002[fF]|\/)/g, '/')
    .replace(/(?:&sol;|&#(?:0*47|x0*2[fF]);)/gi, '/')
  for (let attempt = 0; attempt < 2; attempt += 1) {
    normalized = normalized.replace(/(?:%[\da-f]{2})+/gi, (encoded) => {
      try { return decodeURIComponent(encoded) } catch { return encoded }
    })
  }
  return normalized
}

function approvedEndpointOccurrences(text) {
  // The enabled artifact contract is a quoted string literal containing exactly the
  // same-origin relay path. This deliberately cannot match an absolute or encoded URL.
  return [...text.matchAll(/(["'`])\/api\/event\1/g)]
}

async function artifactText(directory) {
  let text = ''
  let files = 0
  async function collect(current) {
    for (const entry of await readdir(current, { withFileTypes: true })) {
      const fullPath = path.join(current, entry.name)
      if (entry.isDirectory()) await collect(fullPath)
      else if (/\.(?:js|html)$/.test(entry.name)) {
        files += 1
        text += await readFile(fullPath, 'utf8')
      }
    }
  }
  await collect(directory)
  if (!files) throw new Error('docs analytics artifact contains no emitted JS or HTML')
  return text
}

export async function verifyDocsAnalyticsBuild(directory, mode) {
  if (mode !== 'enabled' && mode !== 'disabled') throw new Error('mode must be enabled or disabled')
  const text = await artifactText(directory)
  if (text.includes('__SILENTSUITE_DOCS_ANALYTICS_ENDPOINT__')) throw new Error('docs artifact contains unresolved analytics compile constant')
  const literalText = normalizeResolvableLiteralText(text)
  const eventOccurrences = [...literalText.matchAll(/\/api\/event/g)]
  if (literalText.includes(UPSTREAM)) throw new Error('docs artifact contains direct upstream analytics endpoint')
  if (mode === 'disabled') {
    if (eventOccurrences.length) throw new Error('disabled docs artifact contains analytics endpoint')
    return
  }
  const approvedOccurrences = approvedEndpointOccurrences(text)
  if (!approvedOccurrences.length) {
    throw new Error('enabled docs artifact is missing analytics endpoint')
  }
  if (eventOccurrences.length !== approvedOccurrences.length) {
    throw new Error('docs artifact contains an unapproved event endpoint')
  }
  for (const required of REQUIRED_ENABLED_TEXT) {
    if (!text.includes(required)) throw new Error(`enabled docs artifact is missing required contract: ${required}`)
  }
  for (const prohibited of PROHIBITED_TEXT) {
    if (text.includes(prohibited)) throw new Error(`docs artifact contains prohibited analytics property: ${prohibited}`)
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const args = process.argv.slice(2)
  const directory = args[args.indexOf('--dir') + 1]
  const mode = args[args.indexOf('--mode') + 1]
  if (!directory || !mode) throw new Error('usage: verify-docs-analytics-build.mjs --dir <directory> --mode enabled|disabled')
  await verifyDocsAnalyticsBuild(directory, mode)
  console.log(`Docs analytics artifact verified (${mode})`)
}
