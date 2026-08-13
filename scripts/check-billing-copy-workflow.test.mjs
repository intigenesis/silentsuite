import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('public CI invokes the non-noop annual billing copy guard', () => {
  const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
  assert.equal(pkg.scripts['check:billing-copy'], 'node --test scripts/check-billing-copy.test.mjs scripts/check-billing-copy-workflow.test.mjs && node scripts/check-billing-copy.mjs')
  const workflow = readFileSync(new URL('../.github/workflows/ci.yml', import.meta.url), 'utf8')
  assert.match(workflow, /pnpm run check:billing-copy/)
})

test('Web and Docs preview/deploy workflows enforce the root guard and preview filters cover its inputs', () => {
  const root = new URL('../', import.meta.url)
  for (const name of ['preview-web.yml', 'deploy-web.yml', 'preview-docs.yml', 'deploy-docs.yml']) {
    const workflow = readFileSync(new URL(`.github/workflows/${name}`, root), 'utf8')
    assert.match(workflow, /pnpm run check:billing-copy/, `${name} must run the root copy guard`)
  }
  for (const name of ['preview-web.yml', 'preview-docs.yml']) {
    const workflow = readFileSync(new URL(`.github/workflows/${name}`, root), 'utf8')
    for (const path of ["'README.md'", "'docs/**'", "'apps/docs/**'", "'android/**'", "'scripts/check-billing-copy*.mjs'", "'package.json'", "'pnpm-lock.yaml'", "'.github/workflows/**'"]) {
      assert.match(workflow, new RegExp(path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')), `${name} must trigger on ${path}`)
    }
  }
})

test('the annual billing guard inventories all production source and copy roots', () => {
  const source = readFileSync(new URL('./check-billing-copy.mjs', import.meta.url), 'utf8')
  for (const root of ['apps/web', 'apps/docs', 'docs', 'android/app/src', 'README.md', 'runbooks', 'scripts', '.github/workflows']) {
    assert.match(source, new RegExp(root.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')), `${root} must be part of the inventory`)
  }
  assert.match(source, /tests and fixtures are only/i)
  assert.match(source, /production-imports-excluded-test-module/)
  assert.match(source, /production-imports-prohibited-fixture/)
})
