#!/usr/bin/env bash
set -euo pipefail

# Refuse to create a release draft or upload a release asset unless GitHub
# release immutability is enabled for this repository.
#
# Why this gate exists: the self-host installer authenticates a downloaded
# bundle against a checksum sidecar that lives on the same release. An actor
# holding contents:write can replace both together, so the sidecar only proves
# integrity if the release itself cannot be rewritten after publication.
# Immutable releases are what supplies that property.
#
# GitHub applies the setting to releases published *after* it is enabled, never
# retroactively. That is why this runs before anything creates a draft or
# attaches an asset, rather than as a post-publication audit.
#
# This script never enables the setting and never writes anything: turning
# immutability on is an owner decision, made outside this repository's code.
#
# Usage:
#   scripts/require-immutable-releases.sh
#
# Requires GITHUB_TOKEN (contents:read is sufficient), GITHUB_REPOSITORY, curl
# and jq. Nothing derived from the token is ever printed.

for tool in curl jq; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: '$tool' is required" >&2; exit 2; }
done

: "${GITHUB_TOKEN:?GITHUB_TOKEN must be set}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must be set}"
API="${GITHUB_API_URL:-https://api.github.com}"

if ! printf '%s' "$GITHUB_REPOSITORY" | grep -Eq '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$'; then
  echo "ERROR: GITHUB_REPOSITORY must be an <owner>/<name> pair" >&2
  exit 2
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
BODY="$WORKDIR/immutable-releases.json"

STATUS="$(curl -sS -o "$BODY" -w '%{http_code}' \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "${API}/repos/${GITHUB_REPOSITORY}/immutable-releases")"

# An unreadable setting is not a passing setting. The response body is never
# echoed, so a misrouted request cannot print anything privileged into a log.
if [ "$STATUS" != "200" ]; then
  echo "ERROR: could not read the release-immutability setting (HTTP ${STATUS})." >&2
  echo "       Refusing to create or attach release assets without that proof." >&2
  exit 1
fi

# The answer has to be an unambiguous JSON boolean. A string "true", a missing
# key, a null, or an array response are all rejected rather than coerced.
if ! jq -e 'type == "object" and has("enabled") and (.enabled | type == "boolean")' "$BODY" >/dev/null 2>&1; then
  echo "ERROR: the release-immutability response has no unambiguous boolean 'enabled' field." >&2
  exit 1
fi

if [ "$(jq -r '.enabled' "$BODY")" != "true" ]; then
  echo "ERROR: release immutability is disabled for ${GITHUB_REPOSITORY}." >&2
  echo "       Published assets could be replaced after an operator verifies them," >&2
  echo "       so this lane refuses to create a draft or upload an asset." >&2
  echo "       A repository owner must enable immutable releases first. This script" >&2
  echo "       never changes the setting, and GitHub does not apply it to releases" >&2
  echo "       that were published before it was enabled." >&2
  exit 1
fi

echo "Release immutability is enabled for ${GITHUB_REPOSITORY}."
