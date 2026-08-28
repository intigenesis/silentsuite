"""Behavioral registry fixtures for the closed self-host OCI index contract."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify-server-image-release.sh"
REPOSITORY = "ghcr.io/silent-suite/silentsuite-server"
TAG = "v10.0.0-beta"
COMMIT = "b" * 40
INDEX_DIGEST = "sha256:" + "4" * 64
AMD64_DIGEST = "sha256:" + "5" * 64
ARM64_DIGEST = "sha256:" + "6" * 64


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _registry_fixture(tmp_path: Path, *, extra_attestation: bool) -> tuple[Path, Path]:
    fixture = tmp_path / "registry-fixture"
    fixture.mkdir()
    manifests: dict[str, object] = {}
    blobs: dict[str, bytes] = {}

    for architecture, child_digest in (("amd64", AMD64_DIGEST), ("arm64", ARM64_DIGEST)):
        config = {
            "architecture": architecture,
            "os": "linux",
            "config": {"Labels": {"org.opencontainers.image.revision": COMMIT}},
        }
        config_bytes = (json.dumps(config, separators=(",", ":")) + "\n").encode()
        config_digest = _digest(config_bytes)
        blobs[config_digest] = config_bytes
        manifests[child_digest] = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": config_digest,
                "size": len(config_bytes),
            },
            "layers": [],
        }

    descriptors = [
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": AMD64_DIGEST,
            "size": 1,
            "platform": {"os": "linux", "architecture": "amd64"},
        },
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": ARM64_DIGEST,
            "size": 1,
            "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"},
        },
    ]
    if extra_attestation:
        descriptors.append(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:" + "7" * 64,
                "size": 1,
                "platform": {"os": "linux", "architecture": "amd64"},
                "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
            }
        )

    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": descriptors,
    }
    (fixture / "index.json").write_text(json.dumps(index), encoding="utf-8")
    for digest, document in manifests.items():
        (fixture / f"manifest-{digest.removeprefix('sha256:')}.json").write_text(
            json.dumps(document), encoding="utf-8"
        )
    for digest, payload in blobs.items():
        (fixture / f"blob-{digest.removeprefix('sha256:')}").write_bytes(payload)

    references = {
        TAG: "index.json",
        f"selfhost-{COMMIT}": "index.json",
        INDEX_DIGEST: "index.json",
        AMD64_DIGEST: f"manifest-{AMD64_DIGEST.removeprefix('sha256:')}.json",
        ARM64_DIGEST: f"manifest-{ARM64_DIGEST.removeprefix('sha256:')}.json",
    }
    (fixture / "references.json").write_text(json.dumps(references), encoding="utf-8")

    curl = tmp_path / "curl"
    curl.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

fixture = Path(os.environ['VERIFIER_FIXTURE'])
references = json.loads((fixture / 'references.json').read_text())
args = sys.argv[1:]
url = args[-1]
parsed = urlparse(url)

if parsed.path == '/token':
    print('{\\"token\\":\\"fixture-token\\"}')
    raise SystemExit(0)

if '/blobs/' in parsed.path:
    digest = parsed.path.rsplit('/blobs/', 1)[1]
    sys.stdout.buffer.write((fixture / ('blob-' + digest.removeprefix('sha256:'))).read_bytes())
    raise SystemExit(0)

reference = parsed.path.rsplit('/manifests/', 1)[1]
name = references.get(reference)
if name is None:
    print('404', end='')
    raise SystemExit(0)

body = fixture / name
if '-o' in args:
    output = Path(args[args.index('-o') + 1])
    output.write_bytes(body.read_bytes())
if '-D' in args:
    headers = Path(args[args.index('-D') + 1])
    headers.write_text('Docker-Content-Digest: ' + (reference if reference.startswith('sha256:') else ('sha256:' + '4' * 64)) + '\\r\\n')
print('200', end='')
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    return fixture, curl


@pytest.mark.parametrize("extra_attestation", [False, True])
def test_verifier_requires_closed_two_descriptor_oci_index(tmp_path: Path, extra_attestation: bool):
    fixture, curl = _registry_fixture(tmp_path, extra_attestation=extra_attestation)
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{curl.parent}:{environment['PATH']}",
            "VERIFIER_FIXTURE": str(fixture),
            "REGISTRY_USERNAME": "fixture-user",
            "REGISTRY_PASSWORD": "fixture-password",
        }
    )
    result = subprocess.run(
        [
            "bash",
            str(VERIFIER),
            "--repository",
            REPOSITORY,
            "--tag",
            TAG,
            "--commit",
            COMMIT,
            "--amd64-digest",
            AMD64_DIGEST,
            "--arm64-digest",
            ARM64_DIGEST,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    if extra_attestation:
        assert result.returncode != 0
        assert "exactly two descriptors" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "Release image verified" in result.stdout
