#!/usr/bin/env bash
set -euo pipefail

# Verify a published SilentSuite server image release directly against the
# registry API — no Docker daemon state, no build-step self-reporting.
#
# Contracts enforced:
#   * the release tag resolves to an OCI image index;
#   * the index exposes exactly one runnable linux/amd64 and one runnable
#     linux/arm64 child, with the expected child digests;
#   * attestation manifests are classified separately and never counted as
#     runnable platforms;
#   * every runnable child carries org.opencontainers.image.revision equal to
#     the exact release commit, and reports the matching os/architecture;
#   * the immutable version reference and the exact-commit reference both
#     resolve to the same index digest.
#
# Usage:
#   scripts/verify-server-image-release.sh --repository ghcr.io/OWNER/NAME \
#     --tag vX.Y.Z --commit <40-hex> \
#     --amd64-digest sha256:... --arm64-digest sha256:... \
#     [--index-digest-file PATH]
#
#   scripts/verify-server-image-release.sh --repository ... --resolve REFERENCE
#     prints the manifest digest for REFERENCE, or "absent" if it does not exist.
#
# Credentials: REGISTRY_USERNAME / REGISTRY_PASSWORD (a workflow GITHUB_TOKEN
# with packages:read is enough). Never a production or VPS credential.

REPOSITORY=""
TAG=""
COMMIT=""
AMD64_DIGEST=""
ARM64_DIGEST=""
INDEX_DIGEST_FILE=""
RESOLVE_REFERENCE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --repository) REPOSITORY="${2:-}"; shift 2 ;;
    --tag) TAG="${2:-}"; shift 2 ;;
    --commit) COMMIT="${2:-}"; shift 2 ;;
    --amd64-digest) AMD64_DIGEST="${2:-}"; shift 2 ;;
    --arm64-digest) ARM64_DIGEST="${2:-}"; shift 2 ;;
    --index-digest-file) INDEX_DIGEST_FILE="${2:-}"; shift 2 ;;
    --resolve) RESOLVE_REFERENCE="${2:-}"; shift 2 ;;
    *) echo "ERROR: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

for tool in curl jq sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: '$tool' is required" >&2; exit 2; }
done

if [ -z "$REPOSITORY" ]; then
  echo "ERROR: --repository is required" >&2
  exit 2
fi

REGISTRY="${REPOSITORY%%/*}"
IMAGE_PATH="${REPOSITORY#*/}"
if [ "$REGISTRY" != "ghcr.io" ]; then
  echo "ERROR: only ghcr.io is supported by this verifier" >&2
  exit 2
fi

ACCEPT_TYPES='application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.docker.distribution.manifest.v2+json'

registry_token() {
  curl -fsSL -u "${REGISTRY_USERNAME:-}:${REGISTRY_PASSWORD:-}" \
    "https://ghcr.io/token?service=ghcr.io&scope=repository:${IMAGE_PATH}:pull" | jq -r '.token'
}

TOKEN="$(registry_token)"
if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
  echo "ERROR: could not obtain a registry pull token for ${IMAGE_PATH}" >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# Fetch a manifest by reference. Writes the body to $2, sets MANIFEST_DIGEST to
# the digest the registry reports, and returns 1 when the reference is absent.
# Any other registry status is fatal: an unreadable registry must never be
# mistaken for "not published yet".
MANIFEST_DIGEST=""
fetch_manifest() {
  local reference="$1" body="$2" headers="$WORKDIR/headers"
  local status
  status="$(curl -sS -o "$body" -D "$headers" -w '%{http_code}' \
    -H "Authorization: Bearer $TOKEN" -H "Accept: $ACCEPT_TYPES" \
    "https://ghcr.io/v2/${IMAGE_PATH}/manifests/${reference}")"
  MANIFEST_DIGEST="$(tr -d '\r' < "$headers" | awk 'tolower($1) == "docker-content-digest:" { print $2 }' | tail -1)"
  case "$status" in
    200) return 0 ;;
    404) MANIFEST_DIGEST=""; return 1 ;;
    *) echo "ERROR: registry returned HTTP $status for ${reference}" >&2; exit 1 ;;
  esac
}

fetch_blob() {
  curl -fsSL -H "Authorization: Bearer $TOKEN" \
    "https://ghcr.io/v2/${IMAGE_PATH}/blobs/$1"
}

if [ -n "$RESOLVE_REFERENCE" ]; then
  if fetch_manifest "$RESOLVE_REFERENCE" "$WORKDIR/resolve.json"; then
    printf '%s\n' "$MANIFEST_DIGEST"
  else
    printf 'absent\n'
  fi
  exit 0
fi

for required in "--tag:$TAG" "--commit:$COMMIT" "--amd64-digest:$AMD64_DIGEST" "--arm64-digest:$ARM64_DIGEST"; do
  if [ -z "${required#*:}" ]; then
    echo "ERROR: ${required%%:*} is required" >&2
    exit 2
  fi
done
printf '%s' "$COMMIT" | grep -Eq '^[0-9a-f]{40}$' || { echo "ERROR: --commit must be a 40-hex commit SHA" >&2; exit 2; }
for digest in "$AMD64_DIGEST" "$ARM64_DIGEST"; do
  printf '%s' "$digest" | grep -Eq '^sha256:[0-9a-f]{64}$' || { echo "ERROR: child digests must be sha256:<64 hex>" >&2; exit 2; }
done
if [ "$AMD64_DIGEST" = "$ARM64_DIGEST" ]; then
  echo "ERROR: the two platform children must be distinct manifests" >&2
  exit 2
fi

echo "== Resolving ${REPOSITORY}:${TAG} =="
if ! fetch_manifest "$TAG" "$WORKDIR/index.json"; then
  echo "ERROR: release tag ${TAG} is not published in the registry" >&2
  exit 1
fi
INDEX_DIGEST="$MANIFEST_DIGEST"
if ! printf '%s' "$INDEX_DIGEST" | grep -Eq '^sha256:[0-9a-f]{64}$'; then
  echo "ERROR: registry did not report an immutable index digest for ${TAG}" >&2
  exit 1
fi

MEDIA_TYPE="$(jq -r '.mediaType // ""' "$WORKDIR/index.json")"
if [ "$MEDIA_TYPE" != "application/vnd.oci.image.index.v1+json" ]; then
  echo "ERROR: ${TAG} is a ${MEDIA_TYPE:-<unset>}, expected an OCI image index" >&2
  exit 1
fi

# Runnable children are image manifests with a concrete linux platform.
# Attestation manifests (unknown/unknown, or carrying the attestation reference
# annotation) are classified separately and must never satisfy a platform.
jq -r '
  [ .manifests[]
    | select((.platform.os // "unknown") != "unknown")
    | select((.platform.architecture // "unknown") != "unknown")
    | select((.annotations["vnd.docker.reference.type"] // "") != "attestation-manifest")
    | "\(.platform.os)/\(.platform.architecture) \(.digest)"
  ] | sort | .[]
' "$WORKDIR/index.json" > "$WORKDIR/runnable.txt"

# A surprise CPU variant would silently change what an operator runs, so only
# the canonical arm64 variant spelling is tolerated.
UNEXPECTED_VARIANTS="$(jq -r '
  [ .manifests[]
    | select((.platform.variant // "") != "")
    | select((.platform.variant // "") != "v8")
    | "\(.digest) variant=\(.platform.variant)"
  ] | .[]
' "$WORKDIR/index.json")"
if [ -n "$UNEXPECTED_VARIANTS" ]; then
  echo "ERROR: index contains children with unexpected CPU variants:" >&2
  printf '%s\n' "$UNEXPECTED_VARIANTS" | sed 's/^/  /' >&2
  exit 1
fi

jq -r '
  [ .manifests[]
    | select(((.platform.os // "unknown") == "unknown")
             or ((.platform.architecture // "unknown") == "unknown")
             or ((.annotations["vnd.docker.reference.type"] // "") == "attestation-manifest"))
    | "\(.digest) \(.annotations["vnd.docker.reference.type"] // "unclassified")"
  ] | .[]
' "$WORKDIR/index.json" > "$WORKDIR/attestations.txt"

echo "runnable children:"
sed 's/^/  /' "$WORKDIR/runnable.txt"
if [ -s "$WORKDIR/attestations.txt" ]; then
  echo "non-runnable (attestation) manifests:"
  sed 's/^/  /' "$WORKDIR/attestations.txt"
fi

EXPECTED_RUNNABLE="$(printf 'linux/amd64 %s\nlinux/arm64 %s\n' "$AMD64_DIGEST" "$ARM64_DIGEST" | LC_ALL=C sort)"
if [ "$(cat "$WORKDIR/runnable.txt")" != "$EXPECTED_RUNNABLE" ]; then
  echo "ERROR: index children do not match the verified per-platform digests" >&2
  echo "expected:" >&2
  printf '%s\n' "$EXPECTED_RUNNABLE" | sed 's/^/  /' >&2
  exit 1
fi

echo "== Verifying child identity =="
verify_child() {
  local expected_arch="$1" digest="$2"
  local body="$WORKDIR/child-${expected_arch}.json"
  if ! fetch_manifest "$digest" "$body"; then
    echo "ERROR: child manifest ${digest} is not retrievable" >&2
    exit 1
  fi
  if [ "$MANIFEST_DIGEST" != "$digest" ]; then
    echo "ERROR: registry returned ${MANIFEST_DIGEST} for child ${digest}" >&2
    exit 1
  fi
  local config_digest
  config_digest="$(jq -r '.config.digest // ""' "$body")"
  if ! printf '%s' "$config_digest" | grep -Eq '^sha256:[0-9a-f]{64}$'; then
    echo "ERROR: child ${digest} has no image config" >&2
    exit 1
  fi
  fetch_blob "$config_digest" > "$WORKDIR/config-${expected_arch}.json"
  local actual_config_digest
  actual_config_digest="sha256:$(sha256sum "$WORKDIR/config-${expected_arch}.json" | cut -d' ' -f1)"
  if [ "$actual_config_digest" != "$config_digest" ]; then
    echo "ERROR: child ${digest} config bytes hash to ${actual_config_digest}, expected ${config_digest}" >&2
    exit 1
  fi
  local architecture os revision
  architecture="$(jq -r '.architecture // ""' "$WORKDIR/config-${expected_arch}.json")"
  os="$(jq -r '.os // ""' "$WORKDIR/config-${expected_arch}.json")"
  revision="$(jq -r '.config.Labels["org.opencontainers.image.revision"] // ""' "$WORKDIR/config-${expected_arch}.json")"
  if [ "$os" != "linux" ] || [ "$architecture" != "$expected_arch" ]; then
    echo "ERROR: child ${digest} reports ${os}/${architecture}, expected linux/${expected_arch}" >&2
    exit 1
  fi
  if [ "$revision" != "$COMMIT" ]; then
    echo "ERROR: child ${digest} revision label is '${revision}', expected '${COMMIT}'" >&2
    exit 1
  fi
  echo "  linux/${expected_arch} ${digest} revision=${revision}"
}

verify_child amd64 "$AMD64_DIGEST"
verify_child arm64 "$ARM64_DIGEST"

echo "== Verifying immutable references =="
if ! fetch_manifest "$COMMIT" "$WORKDIR/commit.json"; then
  echo "ERROR: exact-commit reference ${COMMIT} is not published" >&2
  exit 1
fi
COMMIT_REF_DIGEST="$MANIFEST_DIGEST"
if [ "$COMMIT_REF_DIGEST" != "$INDEX_DIGEST" ]; then
  echo "ERROR: ${REPOSITORY}:${COMMIT} resolves to ${COMMIT_REF_DIGEST}, expected ${INDEX_DIGEST}" >&2
  exit 1
fi
if ! fetch_manifest "$INDEX_DIGEST" "$WORKDIR/by-digest.json"; then
  echo "ERROR: index digest ${INDEX_DIGEST} is not retrievable" >&2
  exit 1
fi
DIGEST_REF_DIGEST="$MANIFEST_DIGEST"
if [ "$DIGEST_REF_DIGEST" != "$INDEX_DIGEST" ]; then
  echo "ERROR: digest reference is not self-consistent" >&2
  exit 1
fi

echo "  ${REPOSITORY}:${TAG}    -> ${INDEX_DIGEST}"
echo "  ${REPOSITORY}:${COMMIT} -> ${COMMIT_REF_DIGEST}"

if [ -n "$INDEX_DIGEST_FILE" ]; then
  printf '%s\n' "$INDEX_DIGEST" > "$INDEX_DIGEST_FILE"
fi

echo ""
echo "Release image verified: ${REPOSITORY}@${INDEX_DIGEST}"
