#!/usr/bin/env bash
set -euo pipefail

# Attach one component's verified artefacts to the shared draft umbrella release
# for an admitted (tag, commit) pair.
#
# This is the *only* sanctioned way any lane writes a release asset. The Android,
# Bridge and self-host server lanes all call it, all append to the same draft,
# and all execute this copy of it from the protected default-branch checkout —
# never from the candidate tree. It is deliberately defensive:
#   * revalidates the live tag, its commit, and both tag rulesets immediately
#     before every write it makes — the draft-creation POST and each individual
#     asset-upload POST — and once more after the last one;
#   * pages the releases list to exhaustion rather than assuming the newest few
#     hundred releases contain every draft;
#   * bounded idempotent lookup-or-create of the draft for this exact tag,
#     created with target_commitish set to the admitted commit;
#   * fails closed if more than one release claims the tag, if the release is not
#     a draft, if it targets a different commit, or if a same-named asset already
#     exists with different bytes;
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
#   scripts/attach-umbrella-release-assets.sh --tag vX.Y.Z --expected-commit SHA \
#     --directory DIR --asset NAME [--asset NAME ...]
#
# Credential, exactly one:
#   GITHUB_TOKEN   contents:write — every release read/write here
# Also requires GITHUB_REPOSITORY, curl and jq. The token is never printed.

TAG=""
EXPECTED_COMMIT=""
DIRECTORY=""
ASSETS=()
ATTEMPTS=6
RETRY_DELAY=5
# 200 pages of 100 is two orders of magnitude beyond this repository's release
# count. Reaching it means the listing is not converging, which is refused
# rather than silently truncated — a missed duplicate draft is exactly the
# failure this bound exists to prevent.
MAX_PAGES=200
# The one branch a draft may legitimately name when GitHub ignores the
# target_commitish this helper sends; see assert_release_identity.
PROTECTED_BRANCH="main"

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
IDENTITY_SCRIPT="${IDENTITY_SCRIPT:-$SCRIPT_DIR/verify-release-identity.sh}"

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="${2:-}"; shift 2 ;;
    --expected-commit) EXPECTED_COMMIT="${2:-}"; shift 2 ;;
    --directory) DIRECTORY="${2:-}"; shift 2 ;;
    --asset) ASSETS+=("${2:-}"); shift 2 ;;
    --attempts) ATTEMPTS="${2:-}"; shift 2 ;;
    --retry-delay) RETRY_DELAY="${2:-}"; shift 2 ;;
    --max-pages) MAX_PAGES="${2:-}"; shift 2 ;;
    *) echo "ERROR: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

for tool in curl jq sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: '$tool' is required" >&2; exit 2; }
done

if [ -z "$TAG" ] || [ -z "$EXPECTED_COMMIT" ] || [ -z "$DIRECTORY" ] || [ "${#ASSETS[@]}" -eq 0 ]; then
  echo "ERROR: --tag, --expected-commit, --directory and at least one --asset are required" >&2
  exit 2
fi
if ! printf '%s' "$EXPECTED_COMMIT" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "ERROR: --expected-commit '${EXPECTED_COMMIT}' is not a 40-hex commit id" >&2
  exit 2
fi
if [ ! -x "$IDENTITY_SCRIPT" ] && [ ! -f "$IDENTITY_SCRIPT" ]; then
  echo "ERROR: the trusted identity verifier ${IDENTITY_SCRIPT} is missing" >&2
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

# The live tag, its commit and both tag rulesets, checked against the pair this
# workflow was admitted for.
#
# Called immediately before every irreversible request this script makes — the
# draft-creation POST and each individual asset-upload POST — and again after
# the last one. A single check at the top would leave a whole series of uploads,
# each of them minutes long for a release APK, running on an identity that was
# only true when the script started. The trailing check stays: it is what
# catches a tag that moved during the final upload, which no pre-check can.
revalidate() {
  bash "$IDENTITY_SCRIPT" \
    --tag "$TAG" \
    --commit "$EXPECTED_COMMIT" \
    --stage "attachment:$1"
}

api() {
  local method="$1" path="$2"
  shift 2
  curl -sS -X "$method" \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$@" "${API}${path}"
}

# POST a JSON document and return GitHub's HTTP status on stdout.
#
# The media type is the whole point of this function existing. curl sends `-d`
# as application/x-www-form-urlencoded unless told otherwise, and the Accept
# header only describes the response, so a JSON body sent through the plain
# `api` helper reaches GitHub as form fields. That is what happened to
# v0.5.4-beta: six draft-creation POSTs across two lanes were rejected, none
# returned an `.id`, and the tag ended up with no release at all.
#
# The body goes through a file rather than argv: it is not secret, but a request
# document belongs on disk under this run's private WORKDIR, not in a process
# listing. The token stays in a header, as everywhere else in this script.
# Exactly three digits on stdout, always. On a transport failure curl both
# writes `000` through `-w` *and* exits non-zero, so a `|| printf '000'`
# fallback appends a second one and the caller reports `HTTP 000000`. The status
# is therefore taken from the write-out alone and then validated; anything that
# is not three digits — including the empty string from a curl that died before
# writing — becomes the single canonical `000`.
api_post_json() {
  local path="$1" body="$2" out="$3" request="$WORKDIR/request.json" status
  printf '%s' "$body" > "$request"
  # curl does not create the -o file when it never connects, and the caller's
  # diagnostic reads it either way. An empty file yields the fixed
  # "unparseable response" label instead of a missing-path error.
  : > "$out"
  status="$(curl -sS -o "$out" -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "Content-Type: application/json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    --data-binary "@${request}" \
    "${API}${path}")" || true
  if ! printf '%s' "$status" | grep -Eq '^[0-9]{3}$'; then
    status="000"
  fi
  printf '%s' "$status"
}

# What a failed creation is allowed to say out loud.
#
# GitHub's error bodies carry a short human `.message` ("Validation Failed",
# "Not Found", "Bad credentials"), which is exactly the diagnostic the last run
# needed and did not have. Nothing else is echoed: no response headers, no other
# response field, no request. The message is stripped of control characters and
# truncated, so a hostile or malformed body cannot flood the log or smuggle
# terminal escapes into it.
sanitized_api_message() {
  local body="$1" message
  if ! message="$(jq -er '.message | select(type == "string")' "$body" 2>/dev/null)"; then
    printf 'unparseable response'
    return 0
  fi
  printf '%s' "$message" | tr -c '[:print:]' ' ' | cut -c1-200
}

local_digest() {
  sha256sum "$1" | cut -d' ' -f1
}

# Page a list endpoint to exhaustion into one JSON array. Stopping at a fixed
# page count would let a second release claiming this tag hide beyond the
# horizon, which is the one thing the sole-draft check must never miss.
collect_pages() {
  local path="$1" destination="$2" page items
  : > "$WORKDIR/pages.ndjson"
  page=1
  while [ "$page" -le "$MAX_PAGES" ]; do
    api GET "${path}?per_page=100&page=${page}" > "$WORKDIR/page.json"
    if ! jq -e 'type == "array"' "$WORKDIR/page.json" >/dev/null 2>&1; then
      echo "ERROR: unexpected response for ${path}: $(head -c 300 "$WORKDIR/page.json")" >&2
      exit 1
    fi
    jq -c '.[]' "$WORKDIR/page.json" >> "$WORKDIR/pages.ndjson"
    items="$(jq 'length' "$WORKDIR/page.json")"
    if [ "$items" -lt 100 ]; then
      jq -s '.' "$WORKDIR/pages.ndjson" > "$destination"
      return 0
    fi
    page=$((page + 1))
  done
  echo "ERROR: ${path} did not terminate within ${MAX_PAGES} pages; refusing to guess" >&2
  exit 1
}

# Draft releases have no git tag yet, so /releases/tags/<tag> cannot find them.
# Listing every release is the only reliable lookup for a draft.
find_release() {
  local found
  collect_pages "/repos/${GITHUB_REPOSITORY}/releases" "$WORKDIR/releases.json"
  jq -c --arg tag "$TAG" '.[] | select(.tag_name == $tag)' "$WORKDIR/releases.json" \
    > "$WORKDIR/matches.json"
  found="$(wc -l < "$WORKDIR/matches.json" | tr -d ' ')"
  if [ "$found" -gt 1 ]; then
    echo "ERROR: ${found} releases claim tag ${TAG}; refusing to guess which draft to append to" >&2
    exit 1
  fi
  [ "$found" -eq 1 ]
}

# GitHub documents target_commitish as unused when the git tag already exists,
# which is always the case here: the owner creates the immutable tag first and
# only then dispatches the release. So the field can come back either as the
# commit this helper asked for or as the repository default branch, and both are
# accepted. Any *other* value — a different commit, a different branch — means
# the draft was created against something nobody admitted, and is refused. The
# authoritative binding is not this field but the live tag identity `revalidate`
# proves on both sides of every write, backed by ruleset 20051355.
assert_release_identity() {
  local stage="$1" tag draft target
  api GET "/repos/${GITHUB_REPOSITORY}/releases/${RELEASE_ID}" > "$WORKDIR/release.json"
  tag="$(jq -r '.tag_name // ""' "$WORKDIR/release.json")"
  draft="$(jq -r '.draft // false' "$WORKDIR/release.json")"
  target="$(jq -r '.target_commitish // ""' "$WORKDIR/release.json")"
  if [ "$tag" != "$TAG" ]; then
    echo "ERROR (${stage}): release ${RELEASE_ID} targets '${tag}', not '${TAG}'" >&2
    exit 1
  fi
  if [ "$draft" != "true" ]; then
    echo "ERROR (${stage}): release ${RELEASE_ID} for ${TAG} is already published; refusing to alter a published release" >&2
    exit 1
  fi
  case "$target" in
    "$EXPECTED_COMMIT"|"$PROTECTED_BRANCH") ;;
    *)
      echo "ERROR (${stage}): release ${RELEASE_ID} targets '${target}', not the admitted ${EXPECTED_COMMIT}" >&2
      exit 1
      ;;
  esac
}

revalidate "pre"

RELEASE_ID=""
CREATE_STATUS="none"
for attempt in $(seq 1 "$ATTEMPTS"); do
  if find_release; then
    RELEASE_ID="$(jq -r '.id' "$WORKDIR/matches.json")"
    break
  fi
  # No release yet. Create the draft bound to the admitted commit; a sibling
  # workflow may win this race, in which case the next lookup finds theirs and
  # we append to it instead. The listing above can page through thousands of
  # releases, so the identity is re-proved here rather than inherited from the
  # check at the top of the script.
  revalidate "before-create"
  CREATE_STATUS="$(api_post_json "/repos/${GITHUB_REPOSITORY}/releases" \
    "$(jq -n --arg tag "$TAG" --arg target "$EXPECTED_COMMIT" \
      '{tag_name: $tag, target_commitish: $target, name: ("SilentSuite " + $tag), draft: true}')" \
    "$WORKDIR/created.json")"
  if jq -e '.id? // empty' "$WORKDIR/created.json" >/dev/null 2>&1; then
    RELEASE_ID="$(jq -r '.id' "$WORKDIR/created.json")"
    break
  fi
  # A losing sibling in the create/create race sees 422 here and finds the
  # winner's draft on the next lookup, so this is a retry, not a failure — but
  # it says why, which the run that produced no draft at all could not.
  echo "draft creation attempt ${attempt} did not settle: HTTP ${CREATE_STATUS}: $(sanitized_api_message "$WORKDIR/created.json")" >&2
  echo "retrying in ${RETRY_DELAY}s" >&2
  sleep "$RETRY_DELAY"
done

if [ -z "$RELEASE_ID" ] || ! printf '%s' "$RELEASE_ID" | grep -Eq '^[0-9]+$'; then
  echo "ERROR: could not resolve a single draft release for ${TAG} within ${ATTEMPTS} attempts" >&2
  echo "       last creation attempt: HTTP ${CREATE_STATUS}: $(sanitized_api_message "$WORKDIR/created.json")" >&2
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

assert_release_identity "before upload"
echo "Appending to draft release ${RELEASE_ID} for ${TAG} at ${EXPECTED_COMMIT}"

list_assets() {
  collect_pages "/repos/${GITHUB_REPOSITORY}/releases/${RELEASE_ID}/assets" "$WORKDIR/assets.json"
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

  # Per asset, not per run: an umbrella draft takes eleven bridge binaries, six
  # Android artefacts and three self-host files, and each upload is its own
  # irreversible write.
  revalidate "before-upload:${asset}"
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

assert_release_identity "after upload"

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

revalidate "post"

echo ""
echo "Assets attached to the draft release for ${TAG}; publication remains manual."
