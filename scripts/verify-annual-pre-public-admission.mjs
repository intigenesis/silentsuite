#!/usr/bin/env node
import { createHash, createHmac, timingSafeEqual } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

// Stage A v2: `privateSha`/`producerRun` are the producer source identity of the
// private run that uploaded the admission, and are bound to that run's head and
// artifact below. `deployedRuntime` is the separately bound deployed identity the
// private producer observed twice through its authenticated runtime endpoint; it
// may name an older private SHA than the producer when a non-mutating
// re-attestation ran from a later private main. The retired v1 wire is rejected
// explicitly and never reinterpreted.
const topLevelKeys = ['schemaVersion', 'predicateType', 'privateSha', 'expectedPublicSha', 'billingImageDigest', 'rollbackImageDigest', 'buildAttestationDigest', 'qaAttestationDigest', 'providerRegistryDigest', 'providerAdmission', 'disclosureDigest', 'producerRun', 'deployedRuntime', 'publicReview', 'signature']
const deployedRuntimeKeys = ['privateSha', 'imageDigest', 'phase', 'deployedAt', 'observedAt', 'reobservedAt']
export const stageAPredicate = 'https://silentsuite.io/attestations/annual-only-pre-public-admission/v2'
const retiredStageAPredicate = 'https://silentsuite.io/attestations/annual-only-pre-public-admission/v1'
const sha = /^[0-9a-f]{40}$/
const digest = /^sha256:[0-9a-f]{64}$/
const signature = /^[0-9a-f]{64}$/
const timestamp = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/
const assert = (value, message) => { if (!value) throw new Error(message) }
const exactKeys = (value, keys) => Boolean(value) && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key))
const bytesDigest = (value) => `sha256:${createHash('sha256').update(value).digest('hex')}`
const signed = (key, value) => createHmac('sha256', key).update(bytesDigest(JSON.stringify(value))).digest('hex')
const equal = (left, right) => { const actual = Buffer.from(left ?? ''); const expected = Buffer.from(right ?? ''); return actual.length === expected.length && timingSafeEqual(actual, expected) }
const positive = (value, name) => assert(Number.isInteger(value) && value > 0, `${name} is invalid`)
const utcSecond = (value, name) => { assert(typeof value === 'string' && timestamp.test(value) && new Date(value).toISOString() === value.replace('Z', '.000Z'), `${name} is invalid`); return Date.parse(value) }

function verifyRun(value, name) {
  assert(exactKeys(value, ['runId', 'runAttempt']), `${name} has missing or unknown fields`)
  positive(value.runId, `${name}.runId`); positive(value.runAttempt, `${name}.runAttempt`)
}
function verifyPublicReview(value, expectedPublicSha, publicReviewHmacKey) {
  assert(exactKeys(value, ['schemaVersion', 'predicateType', 'repository', 'publicSha', 'runId', 'runAttempt', 'disclosureDigest', 'signature']), 'Pre-public signed v2 public review has missing or unknown fields')
  assert(value.schemaVersion === 2 && value.predicateType === 'https://silentsuite.io/attestations/annual-only-public-review/v2', 'Pre-public signed v2 public review predicate is invalid')
  assert(value.repository === 'silent-suite/silentsuite', 'Pre-public signed v2 public review repository is invalid')
  assert(value.publicSha === expectedPublicSha, 'Pre-public signed v2 public review does not admit the exact public SHA')
  positive(value.runId, 'Pre-public signed v2 public review run ID'); positive(value.runAttempt, 'Pre-public signed v2 public review run attempt')
  assert(digest.test(value.disclosureDigest), 'Pre-public signed v2 public review disclosure digest is invalid')
  assert(typeof publicReviewHmacKey === 'string' && publicReviewHmacKey.length > 0, 'Public review HMAC key is missing')
  const unsigned = Object.fromEntries(Object.entries(value).filter(([key]) => key !== 'signature'))
  assert(typeof value.signature === 'string' && signature.test(value.signature) && equal(value.signature, signed(publicReviewHmacKey, unsigned)), 'Pre-public signed v2 public review signature is invalid')
}
function verifyExpectedSourceArtifact(value) {
  assert(exactKeys(value, ['repository', 'runId', 'runAttempt', 'artifactId', 'name']), 'Expected private source artifact is malformed')
  assert(value.repository === 'silent-suite/silentsuite-internal', 'Expected private source artifact repository is invalid')
  positive(value.runId, 'Expected private source artifact run ID'); positive(value.runAttempt, 'Expected private source artifact run attempt'); positive(value.artifactId, 'Expected private source artifact ID')
  assert(value.name === `annual-only-pre-public-admission-${value.runId}-${value.runAttempt}`, 'Expected private source artifact name is invalid')
}
export function verifyDeployedRuntime(value, billingImageDigest) {
  assert(exactKeys(value, deployedRuntimeKeys), 'Pre-public deployed runtime has missing or unknown fields')
  assert(sha.test(value.privateSha) && digest.test(value.imageDigest), 'Pre-public deployed runtime identity is invalid')
  assert(value.phase === 'additive', 'Pre-public deployed runtime phase must be additive')
  const deployedAt = utcSecond(value.deployedAt, 'Pre-public deployed runtime deployedAt'); const observedAt = utcSecond(value.observedAt, 'Pre-public deployed runtime observedAt'); const reobservedAt = utcSecond(value.reobservedAt, 'Pre-public deployed runtime reobservedAt')
  assert(deployedAt <= observedAt && observedAt <= reobservedAt, 'Pre-public deployed runtime observation order is invalid')
  assert(value.imageDigest === billingImageDigest, 'Pre-public deployed runtime image is not the admitted image')
  return value
}

export function verifyPrePublicAdmission({ admissionBytes, expectedAdmissionDigest, expectedPublicSha, expectedSourcePrivateSha, expectedSourceArtifact, expectedPublicRepository, hmacKey, publicReviewHmacKey }) {
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
  assert(admission && typeof admission === 'object' && !Array.isArray(admission), 'Pre-public admission must be an object')
  assert(admission.schemaVersion !== 1 && admission.predicateType !== retiredStageAPredicate, 'Pre-public admission v1 is retired; Stage B requires the v2 admission that separates the producer source from the observed deployed runtime')
  assert(exactKeys(admission, topLevelKeys), 'Pre-public admission must use the exact closed schema')
  assert(admission.schemaVersion === 2 && admission.predicateType === stageAPredicate, 'Pre-public admission predicate is invalid')
  assert(sha.test(admission.privateSha) && sha.test(admission.expectedPublicSha), 'Pre-public admission SHA binding is invalid')
  for (const key of ['billingImageDigest', 'rollbackImageDigest', 'buildAttestationDigest', 'qaAttestationDigest', 'providerRegistryDigest', 'disclosureDigest']) assert(typeof admission[key] === 'string' && digest.test(admission[key]), `Pre-public admission ${key} is invalid`)
  assert(exactKeys(admission.providerAdmission, ['artifactId', 'archiveDigest', 'statementDigest', 'runId', 'runAttempt']), 'Pre-public provider admission has missing or unknown fields')
  positive(admission.providerAdmission.artifactId, 'Pre-public provider admission artifact ID'); positive(admission.providerAdmission.runId, 'Pre-public provider admission run ID'); positive(admission.providerAdmission.runAttempt, 'Pre-public provider admission run attempt')
  assert(digest.test(admission.providerAdmission.archiveDigest) && digest.test(admission.providerAdmission.statementDigest), 'Pre-public provider admission digest is invalid')
  verifyRun(admission.producerRun, 'Pre-public producer run'); verifyDeployedRuntime(admission.deployedRuntime, admission.billingImageDigest); verifyPublicReview(admission.publicReview, expectedPublicSha, publicReviewHmacKey)
  assert(expectedPublicRepository === admission.publicReview.repository, 'Pre-public admission review repository is not this public repository')
  assert(admission.publicReview.disclosureDigest === admission.disclosureDigest, 'Pre-public admission disclosure digest is not the reviewed disclosure')
  assert(admission.producerRun.runId === expectedSourceArtifact.runId && admission.producerRun.runAttempt === expectedSourceArtifact.runAttempt, 'Pre-public admission producer run does not match its immutable source artifact')
  assert(admission.privateSha === expectedSourcePrivateSha, 'Pre-public admission private SHA does not match its immutable source run')
  assert(typeof admission.signature === 'string' && signature.test(admission.signature), 'Pre-public admission signature is malformed')
  const unsigned = Object.fromEntries(Object.entries(admission).filter(([key]) => key !== 'signature'))
  assert(equal(admission.signature, signed(hmacKey, unsigned)), 'Pre-public admission signature is invalid')
  assert(admission.expectedPublicSha === expectedPublicSha, 'Pre-public admission does not admit this exact public SHA')
  return { privateSha: admission.privateSha, expectedPublicSha: admission.expectedPublicSha, billingImageDigest: admission.billingImageDigest, disclosureDigest: admission.disclosureDigest, producerRun: admission.producerRun, deployedRuntime: admission.deployedRuntime, privateAdmissionDigest: expectedAdmissionDigest }
}
const positiveEnv = (value) => typeof value === 'string' && /^[1-9][0-9]*$/.test(value) ? Number(value) : undefined
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  try {
    const env = process.env; const admissionBytes = await readFile(env.ANNUAL_PRE_PUBLIC_ADMISSION ?? '')
    const result = verifyPrePublicAdmission({ admissionBytes, expectedAdmissionDigest: env.ANNUAL_PRIVATE_ADMISSION_DIGEST, expectedPublicSha: env.EXPECTED_PUBLIC_SHA, expectedSourcePrivateSha: env.ANNUAL_PRIVATE_ADMISSION_SOURCE_SHA, expectedSourceArtifact: { repository: env.ANNUAL_PRIVATE_ADMISSION_REPOSITORY, runId: positiveEnv(env.ANNUAL_PRIVATE_ADMISSION_RUN_ID), runAttempt: positiveEnv(env.ANNUAL_PRIVATE_ADMISSION_RUN_ATTEMPT), artifactId: positiveEnv(env.ANNUAL_PRIVATE_ADMISSION_ARTIFACT_ID), name: env.ANNUAL_PRIVATE_ADMISSION_ARTIFACT_NAME }, expectedPublicRepository: env.GITHUB_REPOSITORY, hmacKey: env.ANNUAL_PRIVATE_ADMISSION_HMAC_KEY, publicReviewHmacKey: env.ANNUAL_PUBLIC_REVIEW_HMAC_KEY })
    process.stdout.write(`Pre-public admission verified for private ${result.privateSha} (deployed ${result.deployedRuntime.privateSha}) and exact public ${result.expectedPublicSha}\n`)
  } catch (error) { process.stderr.write(`Pre-public admission rejected: ${error instanceof Error ? error.message : 'unknown error'}\n`); process.exitCode = 1 }
}
