#!/usr/bin/env node
import { createHash, createHmac, timingSafeEqual } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const topLevelKeys = ['schemaVersion', 'predicateType', 'privateSha', 'expectedPublicSha', 'billingImageDigest', 'rollbackImageDigest', 'buildAttestationDigest', 'qaAttestationDigest', 'providerRegistryDigest', 'disclosureDigest', 'privateDeploymentRun', 'publicReview', 'signature']
const sha = /^[0-9a-f]{40}$/
const digest = /^sha256:[0-9a-f]{64}$/
const signature = /^[0-9a-f]{64}$/
const repository = /^silent-suite\/[a-z0-9._-]+$/
const assert = (value, message) => { if (!value) throw new Error(message) }
const exactKeys = (value, keys) => Boolean(value) && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key))
const bytesDigest = (value) => `sha256:${createHash('sha256').update(value).digest('hex')}`
const signed = (key, value) => createHmac('sha256', key).update(bytesDigest(JSON.stringify(value))).digest('hex')
const equal = (left, right) => { const actual = Buffer.from(left ?? ''); const expected = Buffer.from(right ?? ''); return actual.length === expected.length && timingSafeEqual(actual, expected) }
const positive = (value, name) => assert(Number.isInteger(value) && value > 0, `${name} is invalid`)

function verifyRun(value, name) {
  assert(exactKeys(value, ['runId', 'runAttempt']), `${name} has missing or unknown fields`)
  positive(value.runId, `${name}.runId`); positive(value.runAttempt, `${name}.runAttempt`)
}
function verifyArtifact(value, name) {
  assert(exactKeys(value, ['repository', 'runId', 'runAttempt', 'artifactId']), `${name} has missing or unknown fields`)
  assert(typeof value.repository === 'string' && repository.test(value.repository), `${name} repository is invalid`)
  positive(value.runId, `${name}.runId`); positive(value.runAttempt, `${name}.runAttempt`); positive(value.artifactId, `${name}.artifactId`)
}
function verifyExpectedSourceArtifact(value) {
  assert(exactKeys(value, ['repository', 'runId', 'runAttempt', 'artifactId', 'name']), 'Expected private source artifact is malformed')
  assert(value.repository === 'silent-suite/silentsuite-internal', 'Expected private source artifact repository is invalid')
  positive(value.runId, 'Expected private source artifact run ID'); positive(value.runAttempt, 'Expected private source artifact run attempt'); positive(value.artifactId, 'Expected private source artifact ID')
  assert(value.name === `annual-only-pre-public-admission-${value.runId}-${value.runAttempt}`, 'Expected private source artifact name is invalid')
}

export function verifyPrePublicAdmission({ admissionBytes, expectedAdmissionDigest, expectedPublicSha, expectedSourcePrivateSha, expectedSourceArtifact, expectedPublicRepository, hmacKey }) {
  const bytes = Buffer.isBuffer(admissionBytes) ? admissionBytes : Buffer.from(admissionBytes ?? '')
  assert(bytes.length > 0, 'Pre-public admission is missing')
  assert(typeof expectedAdmissionDigest === 'string' && digest.test(expectedAdmissionDigest), 'Pre-public admission digest is malformed')
  assert(equal(bytesDigest(bytes), expectedAdmissionDigest), 'Pre-public admission digest does not match exact downloaded bytes')
  assert(typeof expectedPublicSha === 'string' && sha.test(expectedPublicSha), 'Expected public SHA is malformed')
  assert(typeof expectedSourcePrivateSha === 'string' && sha.test(expectedSourcePrivateSha), 'Expected private source SHA is malformed')
  verifyExpectedSourceArtifact(expectedSourceArtifact)
  assert(expectedPublicRepository === 'silent-suite/silentsuite', 'Expected public repository is invalid')
  assert(typeof hmacKey === 'string' && hmacKey.length > 0, 'Private admission HMAC key is missing')
  let admission
  try { admission = JSON.parse(bytes) } catch { throw new Error('Pre-public admission is not valid JSON') }
  assert(exactKeys(admission, topLevelKeys), 'Pre-public admission must use the exact closed schema')
  assert(admission.schemaVersion === 1 && admission.predicateType === 'https://silentsuite.io/attestations/annual-only-pre-public-admission/v1', 'Pre-public admission predicate is invalid')
  assert(sha.test(admission.privateSha) && sha.test(admission.expectedPublicSha), 'Pre-public admission SHA binding is invalid')
  for (const key of ['billingImageDigest', 'rollbackImageDigest', 'buildAttestationDigest', 'qaAttestationDigest', 'providerRegistryDigest', 'disclosureDigest']) assert(typeof admission[key] === 'string' && digest.test(admission[key]), `Pre-public admission ${key} is invalid`)
  verifyRun(admission.privateDeploymentRun, 'Pre-public private deployment run'); verifyArtifact(admission.publicReview, 'Pre-public public review')
  assert(admission.publicReview.repository === expectedPublicRepository, 'Pre-public admission review repository is not this public repository')
  assert(admission.privateDeploymentRun.runId === expectedSourceArtifact.runId && admission.privateDeploymentRun.runAttempt === expectedSourceArtifact.runAttempt, 'Pre-public admission private deployment run does not match its immutable source artifact')
  assert(admission.privateSha === expectedSourcePrivateSha, 'Pre-public admission private SHA does not match its immutable source run')
  assert(typeof admission.signature === 'string' && signature.test(admission.signature), 'Pre-public admission signature is malformed')
  const unsigned = Object.fromEntries(Object.entries(admission).filter(([key]) => key !== 'signature'))
  assert(equal(admission.signature, signed(hmacKey, unsigned)), 'Pre-public admission signature is invalid')
  assert(admission.expectedPublicSha === expectedPublicSha, 'Pre-public admission does not admit this exact public SHA')
  return { privateSha: admission.privateSha, expectedPublicSha: admission.expectedPublicSha, billingImageDigest: admission.billingImageDigest, disclosureDigest: admission.disclosureDigest, privateDeploymentRun: admission.privateDeploymentRun, privateAdmissionDigest: expectedAdmissionDigest }
}
const positiveEnv = (value) => typeof value === 'string' && /^[1-9][0-9]*$/.test(value) ? Number(value) : undefined
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  try {
    const env = process.env; const admissionBytes = await readFile(env.ANNUAL_PRE_PUBLIC_ADMISSION ?? '')
    const result = verifyPrePublicAdmission({ admissionBytes, expectedAdmissionDigest: env.ANNUAL_PRIVATE_ADMISSION_DIGEST, expectedPublicSha: env.EXPECTED_PUBLIC_SHA, expectedSourcePrivateSha: env.ANNUAL_PRIVATE_ADMISSION_SOURCE_SHA, expectedSourceArtifact: { repository: env.ANNUAL_PRIVATE_ADMISSION_REPOSITORY, runId: positiveEnv(env.ANNUAL_PRIVATE_ADMISSION_RUN_ID), runAttempt: positiveEnv(env.ANNUAL_PRIVATE_ADMISSION_RUN_ATTEMPT), artifactId: positiveEnv(env.ANNUAL_PRIVATE_ADMISSION_ARTIFACT_ID), name: env.ANNUAL_PRIVATE_ADMISSION_ARTIFACT_NAME }, expectedPublicRepository: env.GITHUB_REPOSITORY, hmacKey: env.ANNUAL_PRIVATE_ADMISSION_HMAC_KEY })
    process.stdout.write(`Pre-public admission verified for private ${result.privateSha} and exact public ${result.expectedPublicSha}\n`)
  } catch (error) { process.stderr.write(`Pre-public admission rejected: ${error instanceof Error ? error.message : 'unknown error'}\n`); process.exitCode = 1 }
}
