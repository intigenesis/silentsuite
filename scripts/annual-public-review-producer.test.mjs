import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('the protected public review producer is a dispatch-only exact-main closed v2 producer', () => {
  const workflow = readFileSync('.github/workflows/annual-only-public-review.yml', 'utf8')
  assert.match(workflow, /workflow_dispatch:/)
  assert.match(workflow, /expected_sha:/)
  assert.match(workflow, /github\.ref == 'refs\/heads\/main'/)
  assert.match(workflow, /github\.sha == inputs\.expected_sha/)
  assert.match(workflow, /environment: annual-public-review/)
  assert.match(workflow, /ANNUAL_PUBLIC_REVIEW_HMAC_KEY/)
  assert.match(workflow, /persist-credentials: false/)
  assert.match(workflow, /git fetch --no-tags origin \+refs\/heads\/main/)
  assert.doesNotMatch(workflow, /deploy|cloudflare|ssh-action|create-github-app-token/i)
  assert.match(workflow, /annual-only-public-review-\$\{\{ github\.run_id \}\}-\$\{\{ github\.run_attempt \}\}/)
})

test('the signed v2 review does not contain an artifact id and emits exactly two files', () => {
  const producer = readFileSync('scripts/sign-annual-public-review.mjs', 'utf8')
  assert.match(producer, /schemaVersion: 2/)
  assert.match(producer, /annual-only-public-review\/v2/)
  assert.match(producer, /disclosureDigest/)
  assert.doesNotMatch(producer, /artifactId|process\.argv|console\.log\([^)]*KEY/i)
  assert.match(producer, /annual-only-public-review\.json/)
  assert.match(producer, /annual-only-public-disclosure\.json/)
})
