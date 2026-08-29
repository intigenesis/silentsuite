#!/usr/bin/env bash
set -euo pipefail

# Admit — or refuse — the source a release lane is about to build from.
#
# Every component lane (Android, Bridge, self-host server image) runs this in a
# read-only job before anything is signed, pushed or attached. One definition,
# so the three lanes cannot drift into three different ideas of what a release
# commit is.
#
# What it proves, all of it fail-closed:
#   1. the workflow was triggered by a tag push, not a branch or a dispatch;
#   2. the tag matches the strict SilentSuite release grammar;
#   3. GITHUB_SHA is a 40-hex commit id;
#   4. the checked-out tree is exactly that commit — not a branch tip that has
#      moved, and not a re-pointed tag;
#   5. the tag object resolves to exactly that commit;
#   6. that commit is reachable from protected `main`, so a tag pushed at an
#      unreviewed commit cannot produce a release.
#
# Caller contract: check out with `ref: ${{ github.sha }}`, `fetch-depth: 0`
# and `persist-credentials: false`. This script fetches from `origin` over the
# checkout's anonymous remote; it needs no credential and is never given one.
#
# Usage:
#   scripts/admit-release-source.sh            # writes tag/commit to GITHUB_OUTPUT
#
# Requires GITHUB_REF, GITHUB_SHA and git. Writes `tag=` and `commit=` to
# GITHUB_OUTPUT when that variable is set, and echoes both either way.

: "${GITHUB_REF:?GITHUB_REF must be set}"
: "${GITHUB_SHA:?GITHUB_SHA must be set}"

TAG="${GITHUB_REF#refs/tags/}"
if [ "$TAG" = "$GITHUB_REF" ]; then
  echo "Refusing release: this lane only admits tag pushes, not ${GITHUB_REF}" >&2
  exit 1
fi
if ! printf '%s' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$'; then
  echo "Refusing release: '$TAG' is not a SilentSuite release tag" >&2
  exit 1
fi
if ! printf '%s' "$GITHUB_SHA" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "Refusing release: tag commit is not a 40-hex SHA" >&2
  exit 1
fi

git fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main
git fetch --no-tags origin "+refs/tags/${TAG}:refs/tags/${TAG}"

TAG_COMMIT="$(git rev-list -n 1 "refs/tags/${TAG}")"
if [ "$(git rev-parse HEAD)" != "$GITHUB_SHA" ] || [ "$TAG_COMMIT" != "$GITHUB_SHA" ]; then
  echo "Refusing release: the checked-out tree is not the exact tag commit" >&2
  exit 1
fi
if ! git merge-base --is-ancestor "$GITHUB_SHA" origin/main; then
  echo "Refusing release: ${GITHUB_SHA} is not reachable from protected main" >&2
  exit 1
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "tag=$TAG" >> "$GITHUB_OUTPUT"
  echo "commit=$GITHUB_SHA" >> "$GITHUB_OUTPUT"
fi
echo "Admitted ${TAG} at ${GITHUB_SHA} (reachable from origin/main)"
