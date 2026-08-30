#!/usr/bin/env bash
set -euo pipefail

# Flatten the five per-platform Bridge artifacts, verify every producer checksum,
# and rewrite accepted GNU text/binary records into the one canonical release
# representation consumed by the readiness gate.
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
mkdir -m 0755 "$STAGING"

# Artifact directories are expected, but every leaf must be a regular file.
# In particular, do not let find silently omit producer-created symlinks.
while IFS= read -r entry; do
  if [ -d "$entry" ] && [ ! -L "$entry" ]; then
    continue
  fi
  if [ ! -f "$entry" ] || [ -L "$entry" ]; then
    echo "ERROR: refusing non-regular or symlink artifact: $entry" >&2
    exit 1
  fi

  name="$(basename -- "$entry")"
  if ! printf '%s' "$name" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]*$'; then
    echo "ERROR: refusing to stage asset with unexpected name: $name" >&2
    exit 1
  fi
  if [ -e "$STAGING/$name" ]; then
    echo "ERROR: two build artifacts are both named '$name'" >&2
    exit 1
  fi
  mv "$entry" "$STAGING/$name"
done < <(find "$SOURCE" -mindepth 1 -print | LC_ALL=C sort)

# Python is already a pinned runner dependency in the release job. Use byte
# parsing here so NULs, CRLF, missing final LF, and extra records cannot be
# normalized accidentally by shell text handling.
python3 - "$STAGING" <<'PY'
from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
from pathlib import Path

staging = Path(sys.argv[1])
payloads = (
    "silentsuite-bridge-linux-arm64",
    "silentsuite-bridge-linux-x86_64",
    "silentsuite-bridge-macos-arm64",
    "silentsuite-bridge-macos-x86_64",
    "silentsuite-bridge-windows-x86_64.exe",
)
expected = {name for payload in payloads for name in (payload, f"{payload}.sha256")}
actual = {entry.name for entry in staging.iterdir()}
if actual != expected:
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    raise SystemExit(
        f"ERROR: bridge payload/sidecar inventory mismatch; missing={missing}, extra={extra}"
    )

safe_name = re.compile(rb"[A-Za-z0-9][A-Za-z0-9._-]*")
record = re.compile(rb"([0-9A-Fa-f]{64}) ([ *])(.+)\n")
canonical: list[bytes] = []

for payload_name in sorted(payloads):
    payload = staging / payload_name
    sidecar = staging / f"{payload_name}.sha256"
    for path, kind in ((payload, "payload"), (sidecar, "sidecar")):
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise SystemExit(f"ERROR: {kind} is not a regular non-symlink file: {path.name}")
    if payload.stat().st_size == 0:
        raise SystemExit(f"ERROR: payload is empty: {payload_name}")

    raw = sidecar.read_bytes()
    match = record.fullmatch(raw)
    if match is None:
        raise SystemExit(f"ERROR: malformed checksum record: {sidecar.name}")
    recorded_name = match.group(3)
    if safe_name.fullmatch(recorded_name) is None:
        raise SystemExit(f"ERROR: checksum records an unsafe top-level name: {sidecar.name}")
    if recorded_name.decode("ascii") != payload_name:
        raise SystemExit(
            f"ERROR: {sidecar.name} records the wrong filename: {recorded_name!r}"
        )

    actual_digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    if match.group(1).decode("ascii").lower() != actual_digest:
        raise SystemExit(f"ERROR: checksum mismatch for {payload_name}")
    rendered = f"{actual_digest}  {payload_name}\n".encode("ascii")

    temporary = sidecar.with_name(f".{sidecar.name}.canonical.{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(rendered)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, sidecar)
        os.chmod(sidecar, 0o644)
    finally:
        if temporary.exists():
            temporary.unlink()
    canonical.append(rendered)

manifest = staging / "SHA256SUMS.txt"
temporary = staging / f".{manifest.name}.tmp.{os.getpid()}"
descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
try:
    with os.fdopen(descriptor, "wb") as output:
        output.write(b"".join(canonical))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, manifest)
    os.chmod(manifest, 0o644)
finally:
    if temporary.exists():
        temporary.unlink()

covered = {
    line.split(b"  ", 1)[1].removesuffix(b"\n").decode("ascii")
    for line in manifest.read_bytes().splitlines(keepends=True)
}
if covered != set(payloads):
    raise SystemExit("ERROR: SHA256SUMS.txt does not cover exactly the Bridge payloads")
PY

echo "== staged release assets =="
ls -l "$STAGING"
echo "== SHA256SUMS.txt =="
cat "$STAGING/SHA256SUMS.txt"
