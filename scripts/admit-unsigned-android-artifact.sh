#!/usr/bin/env bash
set -euo pipefail

# Admit — or refuse — the unsigned Android build handed over by the candidate
# producer job, before any signing material exists on this runner.
#
# The signing job never checks out or executes candidate code. What it does
# consume is candidate *data*: an APK, an AAB and their evidence, produced on a
# different runner by candidate Gradle. This is the boundary that data crosses,
# so everything about the incoming directory is treated as hostile until proven
# otherwise:
#
#   * exactly the closed inventory, no more and no fewer files;
#   * every entry a regular file — no symlink, no directory, no device node,
#     nothing that could redirect a later read or write outside the tree;
#   * no name that could traverse, hide, or collide;
#   * nothing executable: this job runs none of it, and a file that need not be
#     executable must not be;
#   * no empty file;
#   * a producer-side SHA256SUMS covering exactly the payload, rechecked here;
#   * the admitted source commit, matching the one the controller admitted.
#
# What this cannot prove, stated plainly: GitHub Actions artifacts carry no
# signature or provenance a consumer job can verify, so "these bytes came from
# the producer job in this run" rests on the platform's artifact scoping, not on
# cryptography. What it does prove is that the bytes are internally consistent,
# match the checksums the producer recorded, and name the commit the controller
# admitted — which is protected-main ancestry, already verified.
#
# Usage:
#   scripts/admit-unsigned-android-artifact.sh --directory DIR --source-sha SHA

DIRECTORY=""
SOURCE_SHA=""

# The producer promises exactly these names. Payload first, then the two
# metadata files, which are verified rather than checksummed by themselves.
PAYLOAD=(
  "app-release-unsigned.apk"
  "app-release.aab"
  "mapping.txt"
  "native-debug-symbols.zip"
  "release-runtime-dependencies.txt"
  "tracker-scan-summary.json"
)
CHECKSUM_FILE="SHA256SUMS"
SOURCE_FILE="source-sha"

while [ $# -gt 0 ]; do
  case "$1" in
    --directory) DIRECTORY="${2:-}"; shift 2 ;;
    --source-sha) SOURCE_SHA="${2:-}"; shift 2 ;;
    *) echo "ERROR: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

[ -n "$DIRECTORY" ] || { echo "ERROR: --directory is required" >&2; exit 2; }
[ -n "$SOURCE_SHA" ] || { echo "ERROR: --source-sha is required" >&2; exit 2; }
printf '%s' "$SOURCE_SHA" | grep -Eq '^[0-9a-f]{40}$' \
  || { echo "ERROR: --source-sha is not a 40-hex commit id" >&2; exit 2; }
command -v sha256sum >/dev/null 2>&1 || { echo "ERROR: sha256sum is required" >&2; exit 2; }

refuse() {
  echo "Refusing the unsigned build: $*" >&2
  exit 1
}

[ -d "$DIRECTORY" ] || refuse "the artifact directory does not exist"

# ── 1. Exactly the closed inventory ───────────────────────────────────
#
# `find -mindepth 1` sees every entry at any depth, so a nested directory or a
# stray file anywhere under the tree is caught, not just at the top level.

EXPECTED="$(printf '%s\n' "${PAYLOAD[@]}" "$CHECKSUM_FILE" "$SOURCE_FILE" | LC_ALL=C sort)"
ACTUAL="$(find "$DIRECTORY" -mindepth 1 -printf '%P\n' | LC_ALL=C sort)"
if [ "$ACTUAL" != "$EXPECTED" ]; then
  echo "Refusing the unsigned build: the artifact is not the closed inventory" >&2
  echo "  expected:" >&2
  printf '%s\n' "$EXPECTED" | sed 's/^/    /' >&2
  echo "  actual:" >&2
  printf '%s\n' "$ACTUAL" | sed 's/^/    /' >&2
  exit 1
fi

# ── 2. What each entry is ─────────────────────────────────────────────

while IFS= read -r name; do
  [ -n "$name" ] || continue
  path="$DIRECTORY/$name"
  case "$name" in
    */*|.*|*..*) refuse "'${name}' is not a plain top-level file name" ;;
  esac
  [ -L "$path" ] && refuse "'${name}' is a symbolic link"
  [ -f "$path" ] || refuse "'${name}' is not a regular file"
  [ -s "$path" ] || refuse "'${name}' is empty"
  mode="$(stat -c '%a' -- "$path")"
  [ "$mode" = "644" ] \
    || refuse "'${name}' has mode ${mode}, expected exactly 644 for data-only input"
done <<< "$ACTUAL"

# ── 3. The commit the producer says it built ──────────────────────────

RECORDED_SHA="$(tr -d ' \t\r\n' < "$DIRECTORY/$SOURCE_FILE")"
[ "$RECORDED_SHA" = "$SOURCE_SHA" ] \
  || refuse "the artifact records source ${RECORDED_SHA}, not the admitted ${SOURCE_SHA}"

# ── 4. The producer's checksums, over exactly the payload ─────────────
#
# The manifest must name the payload and nothing else: a checksum line for a
# file that is not in the inventory, or a payload file with no line, both mean
# the manifest and the tree disagree.

MANIFEST_NAMES="$(sed -n 's/^[0-9a-f]\{64\}  \(.*\)$/\1/p' "$DIRECTORY/$CHECKSUM_FILE" | LC_ALL=C sort)"
PAYLOAD_NAMES="$(printf '%s\n' "${PAYLOAD[@]}" | LC_ALL=C sort)"
[ "$MANIFEST_NAMES" = "$PAYLOAD_NAMES" ] \
  || refuse "${CHECKSUM_FILE} does not cover exactly the payload files"
[ "$(wc -l < "$DIRECTORY/$CHECKSUM_FILE")" -eq "${#PAYLOAD[@]}" ] \
  || refuse "${CHECKSUM_FILE} contains malformed or duplicate records"

( cd "$DIRECTORY" && sha256sum --check --strict --quiet "$CHECKSUM_FILE" ) \
  || refuse "the artifact bytes do not match the checksums the producer recorded"

echo "Unsigned build admitted for ${SOURCE_SHA}:"
echo "  ${#PAYLOAD[@]} payload files, closed inventory, checksums verified"
