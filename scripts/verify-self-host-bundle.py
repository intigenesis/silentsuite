#!/usr/bin/env python3
"""Strictly verify a built self-host release bundle before it is published.

Run by the release workflow against the artefacts it just produced, and by the
contract tests against fixtures. It re-derives everything from the files on
disk — it never trusts the builder's own report.

Checks:
  * checksum sidecar grammar (exactly one record, exact basename, one newline)
  * bundle bytes match the recorded checksum
  * manifest validates against contracts/self-host-server-image.schema.json
  * manifest matches the expected tag / commit / digests / platforms / revision
  * every archive member is a safe relative path under the bundle root
  * the bundle inventory is exactly the expected file set
  * the manifest inside the bundle is byte-identical to the published sidecar
"""

from __future__ import annotations

import argparse
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from selfhost_release_contract import (  # noqa: E402
    MANIFEST_NAME,
    ContractError,
    ReleaseIdentity,
    assert_archive_members_safe,
    assert_bundle_inventory,
    assert_manifest_matches,
    bundle_basename,
    bundle_prefix,
    parse_checksum_file,
    parse_manifest,
    sha256_file,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, help="directory holding the three artefacts")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--index-digest", required=True)
    parser.add_argument("--amd64-digest", required=True)
    parser.add_argument("--arm64-digest", required=True)
    arguments = parser.parse_args()

    directory = Path(arguments.directory)
    basename = bundle_basename(arguments.tag)
    archive = directory / basename
    checksum = directory / f"{basename}.sha256"
    manifest = directory / MANIFEST_NAME

    for path in (archive, checksum, manifest):
        if not path.is_file():
            raise ContractError(f"missing release artefact {path}")

    expected_digest = parse_checksum_file(checksum.read_text(encoding="utf-8"), basename)
    actual_digest = sha256_file(archive)
    if expected_digest != actual_digest:
        raise ContractError(f"bundle digest {actual_digest} does not match sidecar {expected_digest}")

    identity = ReleaseIdentity(
        tag=arguments.tag,
        source_commit=arguments.source_commit,
        index_digest=arguments.index_digest,
        amd64_digest=arguments.amd64_digest,
        arm64_digest=arguments.arm64_digest,
    )
    manifest_text = manifest.read_text(encoding="utf-8")
    assert_manifest_matches(parse_manifest(manifest_text), identity)

    names = assert_archive_members_safe(archive, arguments.tag)
    assert_bundle_inventory(names, arguments.tag)

    with tarfile.open(archive, "r:gz") as handle:
        member = handle.extractfile(f"{bundle_prefix(arguments.tag)}/{MANIFEST_NAME}")
        if member is None:
            raise ContractError("bundle does not contain a readable manifest")
        embedded = member.read().decode("utf-8")
    if embedded != manifest_text:
        raise ContractError("manifest inside the bundle differs from the published manifest")

    print(f"verified {archive} ({actual_digest})")
    print(f"verified {manifest} for {arguments.tag} @ {arguments.source_commit}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ContractError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
