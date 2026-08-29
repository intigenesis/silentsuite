#!/usr/bin/env bash
set -euo pipefail

# Flatten the per-platform bridge build artifacts into one directory and derive
# the combined SHA256SUMS.txt the release carries.
#
# Kept out of the workflow so the attachment lanes run reviewed, syntax-checked
# repository code rather than inline YAML, and so the real-tag and manual-dispatch
# lanes cannot drift apart.
#
# Every staged name is checked against a conservative asset grammar before it is
# accepted: the attachment helper passes names straight into a URL query and a
# local path, so a stray traversal or shell-significant character has no business
# reaching it.
#
# Usage:
#   scripts/stage-bridge-release-assets.sh <downloaded-artifact-dir> <staging-dir>

SOURCE="${1:?source artifact directory is required}"
STAGING="${2:?staging directory is required}"

if [ ! -d "$SOURCE" ]; then
  echo "ERROR: '$SOURCE' is not a directory" >&2
  exit 1
fi
if [ -e "$STAGING" ]; then
  echo "ERROR: staging directory '$STAGING' already exists" >&2
  exit 1
fi
mkdir -p "$STAGING"

while IFS= read -r file; do
  name="$(basename -- "$file")"
  if ! printf '%s' "$name" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]*$'; then
    echo "ERROR: refusing to stage asset with unexpected name: $name" >&2
    exit 1
  fi
  if [ -e "$STAGING/$name" ]; then
    echo "ERROR: two build artifacts are both named '$name'" >&2
    exit 1
  fi
  mv "$file" "$STAGING/$name"
done < <(find "$SOURCE" -type f | LC_ALL=C sort)

if ! ls "$STAGING"/*.sha256 >/dev/null 2>&1; then
  echo "ERROR: no per-asset checksum files were staged" >&2
  exit 1
fi

# Deterministic order, so the manifest does not depend on artifact arrival.
( cd "$STAGING" && cat $(ls *.sha256 | LC_ALL=C sort) > SHA256SUMS.txt )

echo "== staged release assets =="
ls -l "$STAGING"
echo "== SHA256SUMS.txt =="
cat "$STAGING/SHA256SUMS.txt"
