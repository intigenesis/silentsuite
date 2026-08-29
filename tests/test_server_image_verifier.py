"""Behavioral registry fixtures for the closed self-host OCI index contract.

Every digest and size in these fixtures is derived from the exact bytes the
stand-in registry serves, so the tests exercise the verifier's cryptographic
binding rather than agreeing with it. A fixture that wants a mismatch produces
it by serving different real bytes — never by inventing a digest string that
belongs to nothing.
"""

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
COMMIT_REFERENCE = f"selfhost-{COMMIT}"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"

CURL_STUB = r'''#!/usr/bin/env python3
"""Minimal ghcr.io stand-in: exact fixture bytes plus an honest digest header."""
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

fixture = Path(os.environ["VERIFIER_FIXTURE"])
routes = json.loads((fixture / "routes.json").read_text())
args = sys.argv[1:]
parsed = urlparse(args[-1])

if parsed.path == "/token":
    print(json.dumps({"token": "fixture-token"}))
    raise SystemExit(0)

if "/blobs/" in parsed.path:
    digest = parsed.path.rsplit("/blobs/", 1)[1]
    sys.stdout.buffer.write((fixture / ("blob-" + digest.removeprefix("sha256:"))).read_bytes())
    raise SystemExit(0)

reference = parsed.path.rsplit("/manifests/", 1)[1]
served = routes["manifests"].get(reference)
if served is None:
    print("404", end="")
    raise SystemExit(0)

payload = (fixture / served).read_bytes()
if "-o" in args:
    Path(args[args.index("-o") + 1]).write_bytes(payload)
if "-D" in args:
    header = routes["headers"].get(reference)
    if header is None:
        header = "sha256:" + hashlib.sha256(payload).hexdigest()
    Path(args[args.index("-D") + 1]).write_text(
        "HTTP/1.1 200 OK\r\nDocker-Content-Digest: " + header + "\r\n\r\n"
    )
print("200", end="")
'''


def _canonical(document: object) -> bytes:
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode()


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class Registry:
    """A published release as bytes, with hooks to corrupt exactly one claim."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.manifests: dict[str, str] = {}
        self.headers: dict[str, str] = {}

    def write(self, name: str, payload: bytes) -> None:
        (self.directory / name).write_bytes(payload)

    def publish(self) -> None:
        (self.directory / "routes.json").write_text(
            json.dumps({"manifests": self.manifests, "headers": self.headers}),
            encoding="utf-8",
        )


def _registry_fixture(
    tmp_path: Path,
    *,
    extra_attestation: bool = False,
    amd64_variant: str | None = None,
    arm64_variant: str | None = "v8",
    amd64_platform_extra: dict | None = None,
    arm64_platform_extra: dict | None = None,
    descriptor_media_type: str | None = OCI_MANIFEST,
    amd64_size_delta: int = 0,
    lie_in_index_header: bool = False,
    swap_child_bodies: bool = False,
    alias_serves_a_different_index: bool = False,
    forge_the_index_digest: bool = False,
) -> dict:
    directory = tmp_path / "registry-fixture"
    directory.mkdir()
    registry = Registry(directory)

    children: dict[str, dict] = {}
    for architecture in ("amd64", "arm64"):
        config_bytes = _canonical(
            {
                "architecture": architecture,
                "os": "linux",
                "config": {"Labels": {"org.opencontainers.image.revision": COMMIT}},
            }
        )
        config_digest = _digest(config_bytes)
        registry.write("blob-" + config_digest.removeprefix("sha256:"), config_bytes)

        manifest_bytes = _canonical(
            {
                "schemaVersion": 2,
                "mediaType": OCI_MANIFEST,
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": config_digest,
                    "size": len(config_bytes),
                },
                "layers": [],
            }
        )
        manifest_digest = _digest(manifest_bytes)
        file_name = f"manifest-{architecture}.json"
        registry.write(file_name, manifest_bytes)
        children[architecture] = {
            "digest": manifest_digest,
            "size": len(manifest_bytes),
            "file": file_name,
        }

    amd64_platform: dict = {"os": "linux", "architecture": "amd64"}
    arm64_platform: dict = {"os": "linux", "architecture": "arm64"}
    if amd64_variant is not None:
        amd64_platform["variant"] = amd64_variant
    if arm64_variant is not None:
        arm64_platform["variant"] = arm64_variant
    if amd64_platform_extra:
        amd64_platform.update(amd64_platform_extra)
    if arm64_platform_extra:
        arm64_platform.update(arm64_platform_extra)

    descriptors = [
        {
            "mediaType": descriptor_media_type,
            "digest": children["amd64"]["digest"],
            "size": children["amd64"]["size"] + amd64_size_delta,
            "platform": amd64_platform,
        },
        {
            "mediaType": descriptor_media_type,
            "digest": children["arm64"]["digest"],
            "size": children["arm64"]["size"],
            "platform": arm64_platform,
        },
    ]
    if extra_attestation:
        # A real attestation child: its digest is the hash of real bytes too.
        attestation_bytes = _canonical({"schemaVersion": 2, "mediaType": OCI_MANIFEST, "layers": []})
        registry.write("manifest-attestation.json", attestation_bytes)
        descriptors.append(
            {
                "mediaType": OCI_MANIFEST,
                "digest": _digest(attestation_bytes),
                "size": len(attestation_bytes),
                "platform": {"os": "linux", "architecture": "amd64"},
                "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
            }
        )

    index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": descriptors}
    index_bytes = _canonical(index)
    index_digest = _digest(index_bytes)
    registry.write("index.json", index_bytes)

    registry.manifests[TAG] = "index.json"
    registry.manifests[index_digest] = "index.json"
    registry.manifests[COMMIT_REFERENCE] = "index.json"
    registry.manifests[children["amd64"]["digest"]] = children["amd64"]["file"]
    registry.manifests[children["arm64"]["digest"]] = children["arm64"]["file"]

    if swap_child_bodies:
        registry.manifests[children["amd64"]["digest"]] = children["arm64"]["file"]
    if lie_in_index_header:
        # A real digest of a real object in this same registry — the wrong one.
        registry.headers[TAG] = children["arm64"]["digest"]
    if alias_serves_a_different_index:
        alias = dict(index, annotations={"org.opencontainers.image.ref.name": TAG})
        registry.write("index-alias.json", _canonical(alias))
        registry.manifests[COMMIT_REFERENCE] = "index-alias.json"
    if forge_the_index_digest:
        # A registry that lies *consistently*: it serves the genuine index bytes
        # everywhere but reports another real object's digest as their identity,
        # including for the forged reference itself. Nothing downstream is
        # inconsistent — the only way to catch it is to hash the body, because
        # this forged value is what would be pinned into the release manifest.
        borrowed = _canonical(dict(index, annotations={"org.opencontainers.image.ref.name": TAG}))
        registry.write("index-borrowed-identity.json", borrowed)
        forged = _digest(borrowed)
        registry.manifests[forged] = "index.json"
        registry.headers[TAG] = forged
        registry.headers[COMMIT_REFERENCE] = forged
        registry.headers[forged] = forged

    registry.publish()

    curl = tmp_path / "curl"
    curl.write_text(CURL_STUB, encoding="utf-8")
    curl.chmod(0o755)

    return {
        "path": directory,
        "curl": curl,
        "amd64_digest": children["amd64"]["digest"],
        "arm64_digest": children["arm64"]["digest"],
        "index_digest": index_digest,
    }


def _run_verifier(fixture: dict) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fixture['curl'].parent}:{environment['PATH']}",
            "VERIFIER_FIXTURE": str(fixture["path"]),
            "REGISTRY_USERNAME": "fixture-user",
            "REGISTRY_PASSWORD": "fixture-password",
        }
    )
    return subprocess.run(
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
            fixture["amd64_digest"],
            "--arm64-digest",
            fixture["arm64_digest"],
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )


# ── Accepted shapes ───────────────────────────────────────────────────


def test_verifier_accepts_exact_two_descriptors_with_canonical_variants(tmp_path: Path):
    fixture = _registry_fixture(tmp_path)
    result = _run_verifier(fixture)
    assert result.returncode == 0, result.stderr
    assert "Release image verified" in result.stdout
    assert fixture["index_digest"] in result.stdout


def test_verifier_accepts_arm64_without_a_variant(tmp_path: Path):
    fixture = _registry_fixture(tmp_path, arm64_variant=None)
    result = _run_verifier(fixture)
    assert result.returncode == 0, result.stderr


# ── Descriptor and platform contract ──────────────────────────────────


@pytest.mark.parametrize(
    ("fixture_kwargs", "error"),
    [
        ({"amd64_variant": "v8"}, "exact OCI manifest/platform contract"),
        ({"arm64_variant": "v9"}, "exact OCI manifest/platform contract"),
        ({"extra_attestation": True}, "exactly two descriptors"),
        ({"descriptor_media_type": "application/vnd.docker.distribution.manifest.v2+json"}, "exact OCI manifest/platform contract"),
        ({"descriptor_media_type": None}, "exact OCI manifest/platform contract"),
        ({"amd64_platform_extra": {"os.version": "10.0.19041.1"}}, "exact OCI manifest/platform contract"),
        ({"amd64_platform_extra": {"features": ["sse4"]}}, "exact OCI manifest/platform contract"),
        ({"arm64_platform_extra": {"os.features": ["sve"]}}, "exact OCI manifest/platform contract"),
    ],
)
def test_verifier_rejects_noncanonical_or_malformed_descriptors(
    tmp_path: Path, fixture_kwargs: dict[str, object], error: str
):
    fixture = _registry_fixture(tmp_path, **fixture_kwargs)
    result = _run_verifier(fixture)
    assert result.returncode != 0
    assert error in result.stderr


# ── Bytes-to-digest binding ───────────────────────────────────────────


def test_a_descriptor_size_that_disagrees_with_the_served_child_is_rejected(tmp_path: Path):
    fixture = _registry_fixture(tmp_path, amd64_size_delta=1)
    result = _run_verifier(fixture)
    assert result.returncode != 0
    assert "index descriptor for linux/amd64 claims" in result.stderr
    assert "bytes" in result.stderr


def test_a_content_digest_header_that_disagrees_with_the_body_is_rejected(tmp_path: Path):
    fixture = _registry_fixture(tmp_path, lie_in_index_header=True)
    result = _run_verifier(fixture)
    assert result.returncode != 0
    assert "the bytes it served hash to" in result.stderr


def test_a_child_digest_served_with_different_bytes_is_rejected(tmp_path: Path):
    fixture = _registry_fixture(tmp_path, swap_child_bodies=True)
    result = _run_verifier(fixture)
    assert result.returncode != 0
    assert "for digest reference" in result.stderr


def test_a_consistently_forged_content_digest_is_rejected(tmp_path: Path):
    """A digest the verifier reports is a digest the release pins — hash it."""

    fixture = _registry_fixture(tmp_path, forge_the_index_digest=True)
    result = _run_verifier(fixture)
    assert result.returncode != 0
    assert "the bytes it served hash to" in result.stderr
    assert fixture["index_digest"] not in result.stdout


def test_an_alias_resolving_to_a_different_index_is_rejected(tmp_path: Path):
    fixture = _registry_fixture(tmp_path, alias_serves_a_different_index=True)
    result = _run_verifier(fixture)
    assert result.returncode != 0
    assert f"{REPOSITORY}:{COMMIT_REFERENCE} resolves to" in result.stderr
    assert fixture["index_digest"] in result.stderr


def test_the_verified_index_digest_is_the_hash_of_the_bytes_the_registry_served(tmp_path: Path):
    """The reported digest must be reproducible from the fixture's own bytes."""

    fixture = _registry_fixture(tmp_path)
    served = (fixture["path"] / "index.json").read_bytes()
    result = _run_verifier(fixture)
    assert result.returncode == 0, result.stderr
    assert _digest(served) == fixture["index_digest"]
    assert f"{REPOSITORY}@{fixture['index_digest']}" in result.stdout
