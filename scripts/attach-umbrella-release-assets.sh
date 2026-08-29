#!/usr/bin/env bash
set -euo pipefail

# Attach one component's verified artefacts to the shared draft umbrella release
# for a release tag.
#
# This is the *only* sanctioned way any lane writes a release asset. The Android,
# Bridge and self-host server lanes all call it, all append to the same draft,
# and can run concurrently, so it is deliberately defensive:
#   * bounded idempotent lookup-or-create of the draft for this exact tag;
#   * fails closed if more than one release claims the tag, if the release is not
#     a draft, or if a same-named asset already exists with different bytes;
#   * never deletes or replaces an asset: a byte-identical re-run is a no-op and
#     anything else is refused, so a rerun can never damage an existing release;
#   * never writes the release body, name of an existing release, or publishes it;
#   * records sibling asset names before uploading and re-asserts them after;
#   * reads every uploaded asset back and compares its bytes to the local file.
#
# Why not a marketplace release action: the widely used ones treat `draft: true`
# as "keep an existing draft a draft" rather than "refuse a published release",
# and default to deleting a colliding asset before re-uploading it. Either
# behaviour can damage a published umbrella release on a delayed or repeated run.
#
# What it does not claim: nothing here freezes a published release. GitHub
# immutable releases are deferred while the repository has a single direct admin
# (issue #682), so an administrator can still replace an asset after publication.
# The readback below proves the bytes GitHub accepted are the bytes this workflow
# built; it is not a guarantee about the release's future.
#
# Usage:
#   scripts/attach-umbrella-release-assets.sh --tag vX.Y.Z --directory DIR \
#     --asset NAME [--asset NAME ...]
#
# Credential, exactly one:
#   GITHUB_TOKEN   contents:write — every release read/write here
# Also requires GITHUB_REPOSITORY, curl and jq. The token is never printed.

TAG=""
DIRECTORY=""
ASSETS=()
ATTEMPTS=6
RETRY_DELAY=5

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="${2:-}"; shift 2 ;;
    --directory) DIRECTORY="${2:-}"; shift 2 ;;
    --asset) ASSETS+=("${2:-}"); shift 2 ;;
    --attempts) ATTEMPTS="${2:-}"; shift 2 ;;
    --retry-delay) RETRY_DELAY="${2:-}"; shift 2 ;;
    *) echo "ERROR: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

for tool in curl jq sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: '$tool' is required" >&2; exit 2; }
done

if [ -z "$TAG" ] || [ -z "$DIRECTORY" ] || [ "${#ASSETS[@]}" -eq 0 ]; then
  echo "ERROR: --tag, --directory and at least one --asset are required" >&2
  exit 2
fi
: "${GITHUB_TOKEN:?GITHUB_TOKEN must be set}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must be set}"
API="${GITHUB_API_URL:-https://api.github.com}"
UPLOADS="${GITHUB_UPLOAD_URL_BASE:-https://uploads.github.com}"

for asset in "${ASSETS[@]}"; do
  if [ ! -f "$DIRECTORY/$asset" ]; then
    echo "ERROR: asset $DIRECTORY/$asset does not exist" >&2
    exit 1
  fi
done

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

api() {
  local method="$1" path="$2"
  shift 2
  curl -sS -X "$method" \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$@" "${API}${path}"
}

local_digest() {
  sha256sum "$1" | cut -d' ' -f1
}

# Draft releases have no git tag yet, so /releases/tags/<tag> cannot find them.
# Listing releases is the only reliable lookup for a draft.
find_release() {
  local page found
  : > "$WORKDIR/matches.json"
  for page in 1 2 3; do
    api GET "/repos/${GITHUB_REPOSITORY}/releases?per_page=100&page=${page}" > "$WORKDIR/page.json"
    if ! jq -e 'type == "array"' "$WORKDIR/page.json" >/dev/null 2>&1; then
      echo "ERROR: unexpected releases response: $(head -c 300 "$WORKDIR/page.json")" >&2
      exit 1
    fi
    jq -c --arg tag "$TAG" '.[] | select(.tag_name == $tag)' "$WORKDIR/page.json" >> "$WORKDIR/matches.json"
    if [ "$(jq 'length' "$WORKDIR/page.json")" -lt 100 ]; then
      break
    fi
  done
  found="$(wc -l < "$WORKDIR/matches.json" | tr -d ' ')"
  if [ "$found" -gt 1 ]; then
    echo "ERROR: ${found} releases claim tag ${TAG}; refusing to guess which draft to append to" >&2
    exit 1
  fi
  [ "$found" -eq 1 ]
}

RELEASE_ID=""
for attempt in $(seq 1 "$ATTEMPTS"); do
  if find_release; then
    RELEASE_ID="$(jq -r '.id' "$WORKDIR/matches.json")"
    break
  fi
  # No release yet. Create the draft; a sibling workflow may win this race, in
  # which case the next lookup finds theirs and we append to it instead.
  api POST "/repos/${GITHUB_REPOSITORY}/releases" \
    -d "$(jq -n --arg tag "$TAG" '{tag_name: $tag, name: ("SilentSuite " + $tag), draft: true}')" \
    > "$WORKDIR/created.json" || true
  if jq -e '.id? // empty' "$WORKDIR/created.json" >/dev/null 2>&1; then
    RELEASE_ID="$(jq -r '.id' "$WORKDIR/created.json")"
    break
  fi
  echo "release lookup/create attempt ${attempt} did not settle; retrying in ${RETRY_DELAY}s" >&2
  sleep "$RETRY_DELAY"
done

if [ -z "$RELEASE_ID" ] || ! printf '%s' "$RELEASE_ID" | grep -Eq '^[0-9]+$'; then
  echo "ERROR: could not resolve a single draft release for ${TAG} within ${ATTEMPTS} attempts" >&2
  exit 1
fi

# A sibling workflow can create a competing draft between our lookup and create.
# Let the API settle, then require this ID to be the sole release claiming the
# tag before any asset is uploaded.
sleep "$RETRY_DELAY"
if ! find_release; then
  echo "ERROR: release ${RELEASE_ID} disappeared while resolving ${TAG}" >&2
  exit 1
fi
CANONICAL_RELEASE_ID="$(jq -r '.id' "$WORKDIR/matches.json")"
if [ "$CANONICAL_RELEASE_ID" != "$RELEASE_ID" ]; then
  echo "ERROR: release ${RELEASE_ID} is not the sole draft claiming ${TAG}" >&2
  exit 1
fi

api GET "/repos/${GITHUB_REPOSITORY}/releases/${RELEASE_ID}" > "$WORKDIR/release.json"
RELEASE_TAG="$(jq -r '.tag_name // ""' "$WORKDIR/release.json")"
RELEASE_DRAFT="$(jq -r '.draft // false' "$WORKDIR/release.json")"
if [ "$RELEASE_TAG" != "$TAG" ]; then
  echo "ERROR: release ${RELEASE_ID} targets '${RELEASE_TAG}', not '${TAG}'" >&2
  exit 1
fi
if [ "$RELEASE_DRAFT" != "true" ]; then
  echo "ERROR: release ${RELEASE_ID} for ${TAG} is already published; refusing to alter a published release" >&2
  exit 1
fi
echo "Appending to draft release ${RELEASE_ID} for ${TAG}"

list_assets() {
  api GET "/repos/${GITHUB_REPOSITORY}/releases/${RELEASE_ID}/assets?per_page=100" > "$WORKDIR/assets.json"
  if ! jq -e 'type == "array"' "$WORKDIR/assets.json" >/dev/null 2>&1; then
    echo "ERROR: unexpected assets response: $(head -c 300 "$WORKDIR/assets.json")" >&2
    exit 1
  fi
}

# Two-step download: the asset endpoint answers 302 to a signed URL that must be
# fetched without the API Authorization header.
download_asset() {
  local asset_id="$1" destination="$2" location
  curl -sS -o /dev/null -D "$WORKDIR/asset-headers" \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/octet-stream" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${API}/repos/${GITHUB_REPOSITORY}/releases/assets/${asset_id}"
  location="$(tr -d '\r' < "$WORKDIR/asset-headers" | awk 'tolower($1) == "location:" { print $2 }' | tail -1)"
  if [ -z "$location" ]; then
    echo "ERROR: release asset ${asset_id} did not redirect to downloadable content" >&2
    exit 1
  fi
  curl -fsSL -o "$destination" "$location"
}

list_assets
SIBLING_ASSETS="$(jq -r '.[].name' "$WORKDIR/assets.json" | LC_ALL=C sort)"

for asset in "${ASSETS[@]}"; do
  existing_count="$(jq --arg name "$asset" '[.[] | select(.name == $name)] | length' "$WORKDIR/assets.json")"
  if [ "$existing_count" -gt 1 ]; then
    echo "ERROR: ${existing_count} assets already named '${asset}' on ${TAG}" >&2
    exit 1
  fi
  if [ "$existing_count" -eq 1 ]; then
    existing_id="$(jq -r --arg name "$asset" '.[] | select(.name == $name) | .id' "$WORKDIR/assets.json")"
    download_asset "$existing_id" "$WORKDIR/existing-$asset"
    if [ "$(local_digest "$WORKDIR/existing-$asset")" = "$(local_digest "$DIRECTORY/$asset")" ]; then
      echo "  ${asset}: already present with identical bytes"
      continue
    fi
    echo "ERROR: '${asset}' already exists on ${TAG} with different bytes; refusing to clobber" >&2
    exit 1
  fi

  echo "  ${asset}: uploading"
  curl -sS -X POST \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    -H "Content-Type: application/octet-stream" \
    --data-binary "@${DIRECTORY}/${asset}" \
    "${UPLOADS}/repos/${GITHUB_REPOSITORY}/releases/${RELEASE_ID}/assets?name=${asset}" \
    > "$WORKDIR/upload-$asset.json"
  if ! jq -e '.id? // empty' "$WORKDIR/upload-$asset.json" >/dev/null 2>&1; then
    echo "ERROR: upload of '${asset}' failed: $(head -c 300 "$WORKDIR/upload-$asset.json")" >&2
    exit 1
  fi
done

echo "== Reading uploaded assets back =="
list_assets
for asset in "${ASSETS[@]}"; do
  count="$(jq --arg name "$asset" '[.[] | select(.name == $name)] | length' "$WORKDIR/assets.json")"
  if [ "$count" != "1" ]; then
    echo "ERROR: expected exactly one '${asset}' on ${TAG}, found ${count}" >&2
    exit 1
  fi
  asset_id="$(jq -r --arg name "$asset" '.[] | select(.name == $name) | .id' "$WORKDIR/assets.json")"
  asset_size="$(jq -r --arg name "$asset" '.[] | select(.name == $name) | .size' "$WORKDIR/assets.json")"
  asset_state="$(jq -r --arg name "$asset" '.[] | select(.name == $name) | .state' "$WORKDIR/assets.json")"
  local_size="$(wc -c < "$DIRECTORY/$asset" | tr -d ' ')"
  if [ "$asset_state" != "uploaded" ]; then
    echo "ERROR: '${asset}' is in state '${asset_state}'" >&2
    exit 1
  fi
  if [ "$asset_size" != "$local_size" ]; then
    echo "ERROR: '${asset}' is ${asset_size} bytes on the release, ${local_size} locally" >&2
    exit 1
  fi
  download_asset "$asset_id" "$WORKDIR/readback-$asset"
  if [ "$(local_digest "$WORKDIR/readback-$asset")" != "$(local_digest "$DIRECTORY/$asset")" ]; then
    echo "ERROR: '${asset}' read back with a different digest" >&2
    exit 1
  fi
  echo "  ${asset}: ${asset_size} bytes, digest verified"
done

MISSING_SIBLINGS=""
if [ -n "$SIBLING_ASSETS" ]; then
  MISSING_SIBLINGS="$(comm -23 <(printf '%s\n' "$SIBLING_ASSETS") <(jq -r '.[].name' "$WORKDIR/assets.json" | LC_ALL=C sort))"
fi
if [ -n "$MISSING_SIBLINGS" ]; then
  echo "ERROR: assets that existed before this upload are now missing:" >&2
  printf '%s\n' "$MISSING_SIBLINGS" | sed 's/^/  /' >&2
  exit 1
fi

api GET "/repos/${GITHUB_REPOSITORY}/releases/${RELEASE_ID}" > "$WORKDIR/release-after.json"
if [ "$(jq -r '.draft' "$WORKDIR/release-after.json")" != "true" ] \
   || [ "$(jq -r '.tag_name' "$WORKDIR/release-after.json")" != "$TAG" ]; then
  echo "ERROR: release identity or draft state changed while assets were being attached" >&2
  exit 1
fi

# Recheck tag ownership after uploads as well. This catches a slower sibling
# create/create race before this workflow reports a usable shared draft.
if ! find_release; then
  echo "ERROR: no release claims ${TAG} after asset upload" >&2
  exit 1
fi
if [ "$(jq -r '.id' "$WORKDIR/matches.json")" != "$RELEASE_ID" ]; then
  echo "ERROR: release ownership for ${TAG} changed during asset upload" >&2
  exit 1
fi

echo ""
echo "Assets attached to the draft release for ${TAG}; publication remains manual."
