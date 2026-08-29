#!/usr/bin/env bash
set -euo pipefail

# Verify a published SilentSuite server image release directly against the
# registry API — no Docker daemon state, no build-step self-reporting.
#
# Contracts enforced:
#   * every digest this verifier trusts is the SHA-256 of bytes it actually
#     read: index, child manifest, and image config bodies are hashed and
#     matched against the registry's content-digest header and the digest that
#     was requested, and descriptor sizes are matched against the exact byte
#     counts served;
#   * the release tag resolves to an OCI image index;
#   * the index exposes exactly two descriptors: one linux/amd64 and one
#     linux/arm64 child, with the expected child digests, sizes, and a closed
#     platform key set (arm64 may add only a canonical "v8" variant);
#   * provenance is outside the registry index, so no attestation descriptor
#     (especially one with a concrete runnable platform) is admitted;
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
#   scripts/verify-server-image-release.sh --repository ... --verify-reference REFERENCE \
#     --commit <40-hex> --amd64-digest sha256:... --arm64-digest sha256:... \
#     [--expected-index-digest sha256:...] [--index-digest-file PATH]
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
VERIFY_REFERENCE=""
EXPECTED_INDEX_DIGEST=""

while [ $# -gt 0 ]; do
  case "$1" in
    --repository) REPOSITORY="${2:-}"; shift 2 ;;
    --tag) TAG="${2:-}"; shift 2 ;;
    --commit) COMMIT="${2:-}"; shift 2 ;;
    --amd64-digest) AMD64_DIGEST="${2:-}"; shift 2 ;;
    --arm64-digest) ARM64_DIGEST="${2:-}"; shift 2 ;;
    --index-digest-file) INDEX_DIGEST_FILE="${2:-}"; shift 2 ;;
    --resolve) RESOLVE_REFERENCE="${2:-}"; shift 2 ;;
    --verify-reference) VERIFY_REFERENCE="${2:-}"; shift 2 ;;
    --expected-index-digest) EXPECTED_INDEX_DIGEST="${2:-}"; shift 2 ;;
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
# the SHA-256 of the exact bytes received and MANIFEST_BYTES to their exact
# length, and returns 1 when the reference is absent. Any other registry status
# is fatal: an unreadable registry must never be mistaken for "not published
# yet".
#
# The Docker-Content-Digest header is a claim about the response, not proof of
# it. Every digest this verifier goes on to trust is the hash of bytes it read:
# the header must agree with that hash, and a digest reference must be served
# bytes that hash to the digest that was asked for. A registry that answers a
# digest request with different content is rejected here rather than believed.
MANIFEST_DIGEST=""
MANIFEST_BYTES=""
fetch_manifest() {
  local reference="$1" body="$2" headers="$WORKDIR/headers"
  local status header_digest
  MANIFEST_DIGEST=""
  MANIFEST_BYTES=""
  : > "$body"
  status="$(curl -sS -o "$body" -D "$headers" -w '%{http_code}' \
    -H "Authorization: Bearer $TOKEN" -H "Accept: $ACCEPT_TYPES" \
    "https://ghcr.io/v2/${IMAGE_PATH}/manifests/${reference}")"
  case "$status" in
    200) ;;
    404) return 1 ;;
    *) echo "ERROR: registry returned HTTP $status for ${reference}" >&2; exit 1 ;;
  esac

  MANIFEST_DIGEST="sha256:$(sha256sum "$body" | cut -d' ' -f1)"
  MANIFEST_BYTES="$(wc -c < "$body" | tr -d ' ')"

  header_digest="$(tr -d '\r' < "$headers" | awk 'tolower($1) == "docker-content-digest:" { print $2 }' | tail -1)"
  if [ -z "$header_digest" ]; then
    echo "ERROR: registry reported no content digest for ${reference}" >&2
    exit 1
  fi
  if [ "$header_digest" != "$MANIFEST_DIGEST" ]; then
    echo "ERROR: registry reports ${reference} as ${header_digest}, but the bytes it served hash to ${MANIFEST_DIGEST}" >&2
    exit 1
  fi
  case "$reference" in
    sha256:*)
      if [ "$MANIFEST_DIGEST" != "$reference" ]; then
        echo "ERROR: registry served bytes hashing to ${MANIFEST_DIGEST} for digest reference ${reference}" >&2
        exit 1
      fi
      ;;
  esac
  return 0
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

if [ -n "$VERIFY_REFERENCE" ]; then
  TAG="$VERIFY_REFERENCE"
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

verify_child() {
  local expected_arch="$1" digest="$2" descriptor_size="$3"
  local body="$WORKDIR/child-${expected_arch}.json"
  if ! fetch_manifest "$digest" "$body"; then
    echo "ERROR: child manifest ${digest} is not retrievable" >&2
    exit 1
  fi
  if [ "$MANIFEST_DIGEST" != "$digest" ]; then
    echo "ERROR: registry returned ${MANIFEST_DIGEST} for child ${digest}" >&2
    exit 1
  fi
  # The descriptor's size is part of the index's claim about this child, so it
  # is checked against the bytes actually served, not merely well-formedness.
  if [ "$MANIFEST_BYTES" != "$descriptor_size" ]; then
    echo "ERROR: index descriptor for linux/${expected_arch} claims ${descriptor_size} bytes, but child ${digest} is ${MANIFEST_BYTES} bytes" >&2
    exit 1
  fi
  local child_media_type
  child_media_type="$(jq -r '.mediaType // ""' "$body")"
  if [ "$child_media_type" != "application/vnd.oci.image.manifest.v1+json" ]; then
    echo "ERROR: child ${digest} is a ${child_media_type:-<unset>}, expected an OCI image manifest" >&2
    exit 1
  fi
  local config_digest
  config_digest="$(jq -r '.config.digest // ""' "$body")"
  if ! printf '%s' "$config_digest" | grep -Eq '^sha256:[0-9a-f]{64}$'; then
    echo "ERROR: child ${digest} has no image config" >&2
    exit 1
  fi
  fetch_blob "$config_digest" > "$WORKDIR/config-${expected_arch}.json"
  local actual_config_digest actual_config_size claimed_config_size
  actual_config_digest="sha256:$(sha256sum "$WORKDIR/config-${expected_arch}.json" | cut -d' ' -f1)"
  if [ "$actual_config_digest" != "$config_digest" ]; then
    echo "ERROR: child ${digest} config bytes hash to ${actual_config_digest}, expected ${config_digest}" >&2
    exit 1
  fi
  claimed_config_size="$(jq -r '.config.size // ""' "$body")"
  actual_config_size="$(wc -c < "$WORKDIR/config-${expected_arch}.json" | tr -d ' ')"
  if [ "$claimed_config_size" != "$actual_config_size" ]; then
    echo "ERROR: child ${digest} claims a ${claimed_config_size:-<unset>}-byte config, but the blob is ${actual_config_size} bytes" >&2
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

verify_index_reference() {
  local reference="$1"
  echo "== Resolving ${REPOSITORY}:${reference} =="
  if ! fetch_manifest "$reference" "$WORKDIR/index.json"; then
    echo "ERROR: ${reference} is not published in the registry" >&2
    exit 1
  fi
  INDEX_DIGEST="$MANIFEST_DIGEST"
  if ! printf '%s' "$INDEX_DIGEST" | grep -Eq '^sha256:[0-9a-f]{64}$'; then
    echo "ERROR: registry did not report an immutable index digest for ${reference}" >&2
    exit 1
  fi
  if [ -n "$EXPECTED_INDEX_DIGEST" ] && [ "$INDEX_DIGEST" != "$EXPECTED_INDEX_DIGEST" ]; then
    echo "ERROR: ${REPOSITORY}:${reference} resolves to ${INDEX_DIGEST}, expected ${EXPECTED_INDEX_DIGEST}" >&2
    exit 1
  fi

  local media_type
  media_type="$(jq -r '.mediaType // ""' "$WORKDIR/index.json")"
  if [ "$media_type" != "application/vnd.oci.image.index.v1+json" ]; then
    echo "ERROR: ${reference} is a ${media_type:-<unset>}, expected an OCI image index" >&2
    exit 1
  fi

  local descriptor_count
  descriptor_count="$(jq -r '(.manifests // []) | length' "$WORKDIR/index.json")"
  if [ "$descriptor_count" != "2" ]; then
    echo "ERROR: release index must contain exactly two descriptors (linux/amd64 and linux/arm64); found ${descriptor_count}" >&2
    exit 1
  fi

  # Provenance is deliberately published outside this index. An annotation is
  # not a security boundary: a concrete-platform attestation descriptor can be
  # selected by a normal OCI resolver. Reject the annotation before comparing
  # the two allowed child descriptors.
  local attestation_descriptors
  attestation_descriptors="$(jq -r '
    [ .manifests[]
      | select((.annotations["vnd.docker.reference.type"] // "") == "attestation-manifest")
      | "\(.platform.os // "unknown")/\(.platform.architecture // "unknown") \(.digest // "<missing-digest>")"
    ] | .[]
  ' "$WORKDIR/index.json")"
  if [ -n "$attestation_descriptors" ]; then
    echo "ERROR: release index contains an attestation-manifest descriptor; provenance must remain outside the release index" >&2
    printf '%s\n' "$attestation_descriptors" | sed 's/^/  /' >&2
    exit 1
  fi

  # Descriptor identity is closed as well as counted, and the platform object is
  # a closed key set rather than a prefix match: linux/amd64 carries exactly
  # {os, architecture}, linux/arm64 the same plus at most a canonical "v8"
  # variant. Any other key — os.version, os.features, features, a stray
  # annotation-style field — is a platform this release was never verified
  # against, so it is rejected instead of ignored. The descriptor media type,
  # digest, platform, and OCI size are all validated before the normalized
  # runnable list is compared with the release inputs.
  local descriptor_contract_ok
  descriptor_contract_ok="$(jq -r --arg amd64 "$AMD64_DIGEST" --arg arm64 "$ARM64_DIGEST" '
    all(.manifests[];
      (.mediaType == "application/vnd.oci.image.manifest.v1+json") and
      ((.digest // "") | test("^sha256:[0-9a-f]{64}$")) and
      ((.size // -1) | type == "number") and
      ((.size // -1) >= 0) and
      (((.size // -1) | floor) == (.size // -1)) and
      ((.platform | type) == "object") and
      (
        (.platform | keys) as $keys |
        (
          (.platform.os == "linux" and
           .platform.architecture == "amd64" and
           .digest == $amd64 and
           $keys == ["architecture", "os"]) or
          (.platform.os == "linux" and
           .platform.architecture == "arm64" and
           .digest == $arm64 and
           ($keys == ["architecture", "os"] or
            ($keys == ["architecture", "os", "variant"] and .platform.variant == "v8")))
        )
      )
    )
  ' "$WORKDIR/index.json")"
  if [ "$descriptor_contract_ok" != "true" ]; then
    echo "ERROR: index descriptors do not match the exact OCI manifest/platform contract" >&2
    exit 1
  fi

  jq -r '
    [ .manifests[]
      | {os: (.platform.os // ""), architecture: (.platform.architecture // ""), digest: (.digest // "")}
    ] | sort_by([.os, .architecture]) | .[]
    | "\(.os)/\(.architecture) \(.digest)"
  ' "$WORKDIR/index.json" > "$WORKDIR/runnable.txt"

  echo "runnable children:"
  sed 's/^/  /' "$WORKDIR/runnable.txt"

  local expected_runnable
  expected_runnable="$(printf 'linux/amd64 %s\nlinux/arm64 %s\n' "$AMD64_DIGEST" "$ARM64_DIGEST" | LC_ALL=C sort)"
  if [ "$(cat "$WORKDIR/runnable.txt")" != "$expected_runnable" ]; then
    echo "ERROR: index children do not match the verified per-platform digests" >&2
    echo "expected:" >&2
    printf '%s\n' "$expected_runnable" | sed 's/^/  /' >&2
    exit 1
  fi

  echo "== Verifying child identity =="
  local amd64_size arm64_size
  amd64_size="$(jq -r --arg d "$AMD64_DIGEST" '[.manifests[] | select(.digest == $d) | .size] | if length == 1 then (.[0] | tostring) else "" end' "$WORKDIR/index.json")"
  arm64_size="$(jq -r --arg d "$ARM64_DIGEST" '[.manifests[] | select(.digest == $d) | .size] | if length == 1 then (.[0] | tostring) else "" end' "$WORKDIR/index.json")"
  if [ -z "$amd64_size" ] || [ -z "$arm64_size" ]; then
    echo "ERROR: index does not carry exactly one descriptor per verified platform digest" >&2
    exit 1
  fi
  verify_child amd64 "$AMD64_DIGEST" "$amd64_size"
  verify_child arm64 "$ARM64_DIGEST" "$arm64_size"
  if ! fetch_manifest "$INDEX_DIGEST" "$WORKDIR/by-digest.json"; then
    echo "ERROR: index digest ${INDEX_DIGEST} is not retrievable" >&2
    exit 1
  fi
  if [ "$MANIFEST_DIGEST" != "$INDEX_DIGEST" ]; then
    echo "ERROR: digest reference is not self-consistent" >&2
    exit 1
  fi
}

if [ -n "$VERIFY_REFERENCE" ]; then
  verify_index_reference "$VERIFY_REFERENCE"
  if [ -n "$INDEX_DIGEST_FILE" ]; then
    printf '%s\n' "$INDEX_DIGEST" > "$INDEX_DIGEST_FILE"
  fi
  echo "Reference verified: ${REPOSITORY}:${VERIFY_REFERENCE} -> ${INDEX_DIGEST}"
  exit 0
fi

EXPECTED_INDEX_DIGEST=""
verify_index_reference "$TAG"

echo "== Verifying immutable references =="
COMMIT_REFERENCE="selfhost-${COMMIT}"
EXPECTED_INDEX_DIGEST="$INDEX_DIGEST"
verify_index_reference "$COMMIT_REFERENCE"
COMMIT_REF_DIGEST="$INDEX_DIGEST"

echo "  ${REPOSITORY}:${TAG}    -> ${INDEX_DIGEST}"
echo "  ${REPOSITORY}:${COMMIT_REFERENCE} -> ${COMMIT_REF_DIGEST}"

if [ -n "$INDEX_DIGEST_FILE" ]; then
  printf '%s\n' "$INDEX_DIGEST" > "$INDEX_DIGEST_FILE"
fi

echo ""
echo "Release image verified: ${REPOSITORY}@${INDEX_DIGEST}"
