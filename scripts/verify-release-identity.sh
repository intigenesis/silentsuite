#!/usr/bin/env bash
set -euo pipefail

# Prove that one (tag, commit) pair is still a legitimate SilentSuite release
# source, using only evidence the candidate commit cannot produce.
#
# This is the whole trust root of the release control plane. It runs from the
# protected default-branch checkout the controller loads — never from the
# candidate tree — and it is executed twice in every lane: once in the
# controller's admission job before any candidate code runs at all, and again
# immediately before each irreversible act (Android signing, GHCR alias/index
# publication, every release-asset attachment).
#
# What it proves, all fail-closed:
#   1. the tag matches the strict SilentSuite release grammar;
#   2. the commit is a 40-hex object id;
#   3. the repository's default branch is still the protected branch we admit
#      from, and the caller is running that branch's workflow definition;
#   4. the live tag ref resolves — through an annotated tag object if there is
#      one — to exactly that commit, right now;
#   5. that commit is an ancestor of the protected default branch;
#   6. both public tag rulesets are still present, active, scoped to
#      refs/tags/v*, and carry exactly the rules that make a v* tag creatable
#      only by the owner and thereafter immutable.
#
# Ruleset reads are deliberately unprivileged. The repository is public, so the
# rulesets endpoint answers without a credential; issue #682 removed the
# repository-settings token and nothing here reintroduces one. A token is used
# only when the caller already has the workflow token, and only to raise the
# rate limit — every request falls back to an anonymous read if the token is
# refused, so a narrower token can never turn into a silent skip.
#
# Usage:
#   scripts/verify-release-identity.sh --tag vX.Y.Z --commit <40-hex> \
#     [--stage <label>] [--emit-outputs] [--evidence <path>] \
#     [--git-ancestry <repo-dir>]
#
# Requires GITHUB_REPOSITORY, curl and jq. GITHUB_TOKEN is optional.

TAG=""
COMMIT=""
STAGE="admission"
EMIT_OUTPUTS=0
EVIDENCE=""
GIT_ANCESTRY=""
ATTEMPTS=5
RETRY_DELAY=3

# The exact live rulesets this control plane depends on. Both ids, both rule
# sets, and the owner bypass principal are reviewed constants: a ruleset that
# drifts is a release blocker, not a warning.
CREATION_RULESET_ID=20051354
CREATION_RULESET_NAME="Authorize v* release tag creation"
CREATION_RULES='["creation"]'
CREATION_BYPASS_ACTOR=265568982
IMMUTABILITY_RULESET_ID=20051355
IMMUTABILITY_RULESET_NAME="Make v* release tags immutable"
IMMUTABILITY_RULES='["deletion","non_fast_forward","update"]'
RULESET_REF_PATTERN='refs/tags/v*'
PROTECTED_BRANCH="main"
TAG_GRAMMAR='^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$'

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="${2:-}"; shift 2 ;;
    --commit) COMMIT="${2:-}"; shift 2 ;;
    --stage) STAGE="${2:-}"; shift 2 ;;
    --emit-outputs) EMIT_OUTPUTS=1; shift ;;
    --evidence) EVIDENCE="${2:-}"; shift 2 ;;
    --git-ancestry) GIT_ANCESTRY="${2:-}"; shift 2 ;;
    --attempts) ATTEMPTS="${2:-}"; shift 2 ;;
    --retry-delay) RETRY_DELAY="${2:-}"; shift 2 ;;
    *) echo "ERROR: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

for tool in curl jq; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: '$tool' is required" >&2; exit 2; }
done

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must be set}"
API="${GITHUB_API_URL:-https://api.github.com}"

refuse() {
  echo "Refusing release (${STAGE}): $*" >&2
  exit 1
}

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# One GET, retried on transport and server faults, and downgraded to an
# anonymous read whenever a supplied token is rejected. Anything else — a 404,
# a 422, a body that is not the expected JSON shape — is drift, and drift stops
# the release.
api_get() {
  local path="$1" destination="$2" attempt status
  for attempt in $(seq 1 "$ATTEMPTS"); do
    for mode in token anonymous; do
      if [ "$mode" = "token" ] && [ -z "${GITHUB_TOKEN:-}" ]; then
        continue
      fi
      if [ "$mode" = "token" ]; then
        status="$(curl -sS -o "$destination" -w '%{http_code}' \
          -H "Authorization: Bearer ${GITHUB_TOKEN:-}" \
          -H "Accept: application/vnd.github+json" \
          -H "X-GitHub-Api-Version: 2022-11-28" \
          "${API}${path}" || echo 000)"
      else
        status="$(curl -sS -o "$destination" -w '%{http_code}' \
          -H "Accept: application/vnd.github+json" \
          -H "X-GitHub-Api-Version: 2022-11-28" \
          "${API}${path}" || echo 000)"
      fi
      if [ "$status" = "200" ]; then
        return 0
      fi
      # A token that cannot see this resource must not decide the outcome; the
      # repository is public, so retry the same read with no credential.
      if [ "$mode" = "token" ] && { [ "$status" = "401" ] || [ "$status" = "403" ] || [ "$status" = "404" ]; }; then
        continue
      fi
      break
    done
    case "$status" in
      429|5??|000)
        sleep "$RETRY_DELAY"
        continue
        ;;
    esac
    break
  done
  refuse "GET ${path} answered HTTP ${status}"
}

# Read one field, failing rather than yielding an empty string. Callers always
# assign the result to a variable first: a `refuse` inside a command
# substitution would only kill the subshell, so no check may inline this.
json() {
  local file="$1" filter="$2"
  jq -er "$filter" "$file" 2>/dev/null
}

# ── 1/2. Payload grammar ──────────────────────────────────────────────

[ -n "$TAG" ] || refuse "--tag is required"
[ -n "$COMMIT" ] || refuse "--commit is required"
printf '%s' "$TAG" | grep -Eq "$TAG_GRAMMAR" \
  || refuse "'${TAG}' is not a SilentSuite release tag"
printf '%s' "$COMMIT" | grep -Eq '^[0-9a-f]{40}$' \
  || refuse "'${COMMIT}' is not a 40-hex commit id"

# ── 3. The branch this control plane is allowed to be loaded from ─────

api_get "/repos/${GITHUB_REPOSITORY}" "$WORKDIR/repo.json"
DEFAULT_BRANCH="$(json "$WORKDIR/repo.json" '.default_branch')" \
  || refuse "the repository response has no default_branch"
[ "$DEFAULT_BRANCH" = "$PROTECTED_BRANCH" ] \
  || refuse "default branch is '${DEFAULT_BRANCH}', not the protected '${PROTECTED_BRANCH}'"
if [ -n "${GITHUB_REF:-}" ] && [ "$GITHUB_REF" != "refs/heads/${PROTECTED_BRANCH}" ]; then
  refuse "this workflow was loaded from ${GITHUB_REF}, not refs/heads/${PROTECTED_BRANCH}"
fi

# ── 4. Live tag identity, dereferenced ────────────────────────────────

api_get "/repos/${GITHUB_REPOSITORY}/git/ref/tags/${TAG}" "$WORKDIR/ref.json"
REF_NAME="$(json "$WORKDIR/ref.json" '.ref')" || refuse "the tag ref response has no ref"
[ "$REF_NAME" = "refs/tags/${TAG}" ] \
  || refuse "the tag ref resolved to '${REF_NAME}', not refs/tags/${TAG}"
OBJECT_TYPE="$(json "$WORKDIR/ref.json" '.object.type')" || refuse "the tag ref names no object type"
OBJECT_SHA="$(json "$WORKDIR/ref.json" '.object.sha')" || refuse "the tag ref names no object sha"
case "$OBJECT_TYPE" in
  commit)
    TAG_COMMIT="$OBJECT_SHA"
    ;;
  tag)
    api_get "/repos/${GITHUB_REPOSITORY}/git/tags/${OBJECT_SHA}" "$WORKDIR/tagobj.json"
    TAG_OBJECT_TYPE="$(json "$WORKDIR/tagobj.json" '.object.type')" \
      || refuse "annotated tag ${TAG} names no target type"
    [ "$TAG_OBJECT_TYPE" = "commit" ] \
      || refuse "annotated tag ${TAG} points at a '${TAG_OBJECT_TYPE}', not a commit"
    TAG_COMMIT="$(json "$WORKDIR/tagobj.json" '.object.sha')" \
      || refuse "annotated tag ${TAG} names no target sha"
    ;;
  *)
    refuse "tag ${TAG} points at a '${OBJECT_TYPE}' object"
    ;;
esac
[ "$TAG_COMMIT" = "$COMMIT" ] \
  || refuse "${TAG} currently points at ${TAG_COMMIT}, not the admitted ${COMMIT}"

# ── 5. Reachability from the protected branch ─────────────────────────

api_get "/repos/${GITHUB_REPOSITORY}/compare/${PROTECTED_BRANCH}...${COMMIT}" "$WORKDIR/compare.json"
COMPARE_STATUS="$(json "$WORKDIR/compare.json" '.status')" \
  || refuse "the compare response has no status"
MERGE_BASE="$(json "$WORKDIR/compare.json" '.merge_base_commit.sha')" \
  || refuse "the compare response has no merge base"
case "$COMPARE_STATUS" in
  identical|behind) ;;
  *) refuse "${COMMIT} is '${COMPARE_STATUS}' relative to ${PROTECTED_BRANCH}, so it is not on the protected branch" ;;
esac
[ "$MERGE_BASE" = "$COMMIT" ] \
  || refuse "${COMMIT} is not its own merge base with ${PROTECTED_BRANCH}"

# The controller's admission job also has a full clone, so the same claim is
# re-derived locally from the object graph rather than from one API answer.
if [ -n "$GIT_ANCESTRY" ]; then
  command -v git >/dev/null 2>&1 || refuse "--git-ancestry needs git"
  git -C "$GIT_ANCESTRY" fetch --no-tags --quiet origin \
    "+refs/heads/${PROTECTED_BRANCH}:refs/remotes/origin/${PROTECTED_BRANCH}" \
    || refuse "could not fetch ${PROTECTED_BRANCH} to re-derive ancestry"
  git -C "$GIT_ANCESTRY" fetch --no-tags --quiet origin "+refs/tags/${TAG}:refs/tags/${TAG}" \
    || refuse "could not fetch ${TAG} to re-derive its commit"
  LOCAL_TAG_COMMIT="$(git -C "$GIT_ANCESTRY" rev-list -n 1 "refs/tags/${TAG}")"
  [ "$LOCAL_TAG_COMMIT" = "$COMMIT" ] \
    || refuse "the fetched ${TAG} resolves to ${LOCAL_TAG_COMMIT}, not ${COMMIT}"
  git -C "$GIT_ANCESTRY" merge-base --is-ancestor "$COMMIT" "origin/${PROTECTED_BRANCH}" \
    || refuse "${COMMIT} is not reachable from protected ${PROTECTED_BRANCH}"
fi

# ── 6. Live tag rulesets ──────────────────────────────────────────────

api_get "/repos/${GITHUB_REPOSITORY}/rulesets" "$WORKDIR/rulesets.json"
jq -e 'type == "array"' "$WORKDIR/rulesets.json" >/dev/null 2>&1 \
  || refuse "the rulesets endpoint did not return a list"

BYPASS_OBSERVED="unobservable"

check_ruleset() {
  local id="$1" name="$2" rules="$3" bypass="$4" file="$WORKDIR/ruleset-$1.json"
  local listed actual
  listed="$(jq -r --argjson id "$id" '[.[] | select(.id == $id)] | length' "$WORKDIR/rulesets.json")"
  [ "$listed" = "1" ] || refuse "ruleset ${id} (${name}) is not published exactly once"

  api_get "/repos/${GITHUB_REPOSITORY}/rulesets/${id}" "$file"
  expect_field "$file" '.id' "$id" "ruleset ${id} reports a different id"
  expect_field "$file" '.name' "$name" "ruleset ${id} is no longer named '${name}'"
  expect_field "$file" '.target' "tag" "ruleset ${id} no longer targets tags"
  expect_field "$file" '.enforcement' "active" "ruleset ${id} is not actively enforced"
  expect_field "$file" '.source_type' "Repository" "ruleset ${id} is not owned by this repository"
  expect_field "$file" '.source' "$GITHUB_REPOSITORY" "ruleset ${id} is inherited from another source"
  expect_field "$file" '.conditions.ref_name.include | @json' "[\"${RULESET_REF_PATTERN}\"]" \
    "ruleset ${id} no longer includes exactly ${RULESET_REF_PATTERN}"
  expect_field "$file" '.conditions.ref_name.exclude | @json' "[]" \
    "ruleset ${id} excludes refs from its own pattern"
  actual="$(json "$file" '[.rules[].type] | sort | @json')" \
    || refuse "ruleset ${id} publishes no rule list"
  [ "$actual" = "$rules" ] || refuse "ruleset ${id} rules are ${actual}, expected ${rules}"

  # bypass_actors is only served to a reader with repository administration
  # rights. Issue #682 deliberately leaves this lane without such a credential,
  # so the field is normally absent: enforce it exactly whenever it is visible,
  # and report it as unobservable when it is not. Never treat absent as pass.
  if jq -e 'has("bypass_actors")' "$file" >/dev/null 2>&1; then
    BYPASS_OBSERVED="observed"
    actual="$(json "$file" '[.bypass_actors[] | {actor_id, actor_type, bypass_mode}] | sort_by(.actor_id) | @json')" \
      || refuse "ruleset ${id} publishes an unreadable bypass list"
    [ "$actual" = "$bypass" ] \
      || refuse "ruleset ${id} bypass actors are ${actual}, expected ${bypass}"
  fi
}

expect_field() {
  local file="$1" filter="$2" expected="$3" message="$4" actual
  actual="$(json "$file" "$filter")" || refuse "${message} (field is absent)"
  [ "$actual" = "$expected" ] || refuse "${message} (found '${actual}')"
}

check_ruleset "$CREATION_RULESET_ID" "$CREATION_RULESET_NAME" "$CREATION_RULES" \
  "[{\"actor_id\":${CREATION_BYPASS_ACTOR},\"actor_type\":\"User\",\"bypass_mode\":\"always\"}]"
check_ruleset "$IMMUTABILITY_RULESET_ID" "$IMMUTABILITY_RULESET_NAME" "$IMMUTABILITY_RULES" "[]"

# ── Result ───────────────────────────────────────────────────────────

if [ "$EMIT_OUTPUTS" -eq 1 ] && [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "tag=$TAG"
    echo "commit=$COMMIT"
    echo "bypass-actors=$BYPASS_OBSERVED"
  } >> "$GITHUB_OUTPUT"
fi

if [ -n "$EVIDENCE" ]; then
  jq -n \
    --arg stage "$STAGE" \
    --arg tag "$TAG" \
    --arg commit "$COMMIT" \
    --arg branch "$PROTECTED_BRANCH" \
    --arg compare "$COMPARE_STATUS" \
    --arg bypass "$BYPASS_OBSERVED" \
    '{stage: $stage, tag: $tag, commit: $commit, protectedBranch: $branch,
      compareStatus: $compare, bypassActors: $bypass}' > "$EVIDENCE"
fi

echo "Release identity verified (${STAGE}): ${TAG} -> ${COMMIT}"
echo "  protected branch: ${PROTECTED_BRANCH} (${COMPARE_STATUS})"
echo "  rulesets ${CREATION_RULESET_ID} and ${IMMUTABILITY_RULESET_ID}: active for ${RULESET_REF_PATTERN}"
if [ "$BYPASS_OBSERVED" = "observed" ]; then
  echo "  bypass actors: verified exactly"
else
  echo "  bypass actors: not served to this reader (no administration:read credential by design)"
fi
