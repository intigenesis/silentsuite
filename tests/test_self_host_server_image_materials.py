"""Contracts for the immutable build materials of the self-host server image.

The reviewers' remaining materials finding was that the image was reproducible
in name only: mutable `FROM` tags, `apk add` against live Alpine repositories,
and pip requirements with no hashes. These tests pin what replaced that.

Two halves. The static half reads Dockerfile.server and server/requirements.txt
and is offline. The registry half re-derives the pinned base index and both
runnable platform descriptors from Docker Hub, and re-derives every wheel hash
from PyPI; it skips when the network is unavailable and is required in CI by
setting SILENTSUITE_REQUIRE_REGISTRY_CONTRACT=1.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile.server"
REQUIREMENTS = ROOT / "server" / "requirements.txt"
LOCK_SCRIPT = ROOT / "scripts" / "lock-server-requirements.py"

BASE_IMAGE = "python:3.12-alpine"
BASE_INDEX_DIGEST = "sha256:d09d15e60962ca365d1cd544a48773bac9d33f2fb1b00f2aa0deec78ade7dc31"
BASE_CHILDREN = {
    ("linux", "amd64", None): "sha256:285a71327884a4d50efbea30104473b0fa43ecefa499458899670ca30dae76e5",
    ("linux", "arm64", "v8"): "sha256:c95cd47204b8f236725fc8cf94726abe3f32755a062393597efadd9a5d24fbe1",
}
OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
REGISTRY = "https://registry-1.docker.io"
AUTH = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull"

# Distributions with compiled extensions: each needs one musllinux wheel per
# release architecture, so each must carry at least two recorded hashes.
NATIVE_DISTRIBUTIONS = {
    "cffi",
    "httptools",
    "msgpack",
    "psycopg2-binary",
    "pydantic-core",
    "pynacl",
    "pyyaml",
    "uvloop",
    "watchfiles",
    "websockets",
}
PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)(?:\[[a-z0-9,._-]+\])?==(?P<version>[^\s\\]+) \\$")
HASH = re.compile(r"^ {4}--hash=sha256:(?P<digest>[0-9a-f]{64})( \\)?$")


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements() -> dict[str, list[str]]:
    """name -> recorded hashes, refusing any line shape the lock does not use."""

    pins: dict[str, list[str]] = {}
    current: str | None = None
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith("    #"):
            continue
        match = PIN.match(line)
        if match:
            current = normalise(match.group("name"))
            assert current not in pins, f"{current} is pinned twice"
            pins[current] = []
            continue
        hashed = HASH.match(line)
        assert hashed, f"unrecognised requirements line: {line!r}"
        assert current, "a hash appeared before any pin"
        pins[current].append(hashed.group("digest"))
    return pins


# ── Static: the Dockerfile ────────────────────────────────────────────


def test_both_stages_pin_the_same_approved_base_index():
    """Two stages resolving two base generations is the drift being removed."""

    froms = re.findall(r"^FROM (\S+)", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert len(froms) == 2, froms
    assert froms == [f"{BASE_IMAGE}@{BASE_INDEX_DIGEST}"] * 2


def test_the_image_installs_no_alpine_package():
    """No apk means no live repository resolution in the release image at all."""

    text = DOCKERFILE.read_text(encoding="utf-8")
    assert not re.search(r"^\s*RUN\b.*\bapk\b", text, re.MULTILINE)
    assert "apk add" not in text


def test_pip_installs_only_hash_locked_wheels():
    text = DOCKERFILE.read_text(encoding="utf-8")
    install = [line for line in text.splitlines() if "pip install" in line]
    assert len(install) == 1, install
    assert "--require-hashes" in install[0]
    assert "--only-binary=:all:" in install[0]
    assert "-r /requirements.txt" in install[0]


def test_the_build_revision_label_is_still_stamped():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "ARG VCS_REF" in text
    assert "LABEL org.opencontainers.image.revision=$VCS_REF" in text


# ── Static: the hash lock ─────────────────────────────────────────────


def test_every_requirement_is_pinned_and_hashed():
    pins = parse_requirements()
    assert pins, "the requirements file records no pins"
    for name, digests in sorted(pins.items()):
        assert digests, f"{name} has no recorded hash"


def test_every_native_distribution_records_a_wheel_per_release_architecture():
    pins = parse_requirements()
    missing = sorted(NATIVE_DISTRIBUTIONS - set(pins))
    assert missing == [], f"native distributions vanished from the lock: {missing}"
    for name in sorted(NATIVE_DISTRIBUTIONS):
        assert len(pins[name]) >= 2, (
            f"{name} compiles native code, so it needs a musllinux wheel hash for both "
            f"linux/amd64 and linux/arm64; only {len(pins[name])} recorded"
        )


def test_no_hash_is_recorded_twice():
    digests = [digest for digests in parse_requirements().values() for digest in digests]
    assert len(digests) == len(set(digests))


def test_the_lock_documents_how_to_regenerate_it():
    header = REQUIREMENTS.read_text(encoding="utf-8").split("aiofiles")[0]
    assert "scripts/lock-server-requirements.py" in header
    assert "--require-hashes --only-binary=:all:" in header
    assert "No sdist hash is listed" in header


def test_the_native_import_check_covers_every_native_distribution():
    checker = (ROOT / "scripts" / "check-server-image-dependencies.py").read_text(encoding="utf-8")
    for name in sorted(NATIVE_DISTRIBUTIONS):
        assert f'"{name}"' in checker, f"{name} is not import-checked inside the image"


def test_ci_proves_the_native_imports_on_both_architectures():
    workflow = (ROOT / ".github/workflows/ci-server.yml").read_text(encoding="utf-8")
    assert "check-server-image-dependencies.py" in workflow
    assert "ubuntu-24.04-arm" in workflow


# ── Registry: the live base index ─────────────────────────────────────


def registry_get(path: str, accept: str) -> tuple[dict, str]:
    token_request = urllib.request.Request(AUTH)
    with urllib.request.urlopen(token_request, timeout=30) as response:
        token = json.loads(response.read().decode("utf-8"))["token"]
    request = urllib.request.Request(f"{REGISTRY}{path}")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", accept)
    with urllib.request.urlopen(request, timeout=60) as response:
        return (
            json.loads(response.read().decode("utf-8")),
            response.headers.get("Docker-Content-Digest", ""),
        )


def require_network(error: Exception) -> None:
    if os.environ.get("SILENTSUITE_REQUIRE_REGISTRY_CONTRACT") == "1":
        raise AssertionError(f"the registry contract is required in CI: {error}")
    pytest.skip(f"registry unreachable: {error}")


@pytest.fixture(scope="module")
def base_index():
    try:
        return registry_get(
            f"/v2/library/python/manifests/{BASE_INDEX_DIGEST}", OCI_INDEX_MEDIA_TYPE
        )
    except (urllib.error.URLError, OSError) as error:  # pragma: no cover - network shape
        require_network(error)


def test_the_pinned_base_reference_is_the_index_it_names(base_index):
    document, content_digest = base_index
    assert content_digest == BASE_INDEX_DIGEST
    assert document["mediaType"] == OCI_INDEX_MEDIA_TYPE


def test_both_release_platforms_resolve_to_the_reviewed_child_descriptors(base_index):
    """Attestation manifests are classified apart from runnable ones.

    The upstream index carries one `unknown/unknown` attestation manifest per
    architecture. They are evidence about a runnable child, not something a
    runtime can select, so they must never be counted as a platform.
    """

    document, _ = base_index
    runnable: dict[tuple[str, str, str | None], str] = {}
    attestations: dict[str, str] = {}
    for descriptor in document["manifests"]:
        platform = descriptor.get("platform", {})
        annotations = descriptor.get("annotations") or {}
        if platform.get("os") == "unknown" or platform.get("architecture") == "unknown":
            assert annotations.get("vnd.docker.reference.type") == "attestation-manifest"
            attestations[descriptor["digest"]] = annotations["vnd.docker.reference.digest"]
            continue
        runnable[
            (platform["os"], platform["architecture"], platform.get("variant"))
        ] = descriptor["digest"]

    for key, digest in BASE_CHILDREN.items():
        assert runnable.get(key) == digest, f"{key} resolved to {runnable.get(key)}"
    for attested in attestations.values():
        assert attested in runnable.values(), (
            "an attestation manifest points at something that is not a runnable child"
        )


def test_both_reviewed_children_are_single_platform_manifests(base_index):
    _, _ = base_index
    for (operating_system, architecture, variant), digest in BASE_CHILDREN.items():
        try:
            document, content_digest = registry_get(
                f"/v2/library/python/manifests/{digest}",
                "application/vnd.oci.image.manifest.v1+json",
            )
        except (urllib.error.URLError, OSError) as error:  # pragma: no cover
            require_network(error)
        assert content_digest == digest
        assert document["mediaType"] == "application/vnd.oci.image.manifest.v1+json"
        assert "manifests" not in document, (
            f"{architecture} child is an index, not the platform manifest the build records"
        )


def test_the_recorded_wheel_hashes_are_the_published_ones():
    """The lock is exactly what the generator would write from PyPI today."""

    result = subprocess.run(
        [sys.executable, str(LOCK_SCRIPT), "--check"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "urlopen error" in result.stderr:  # pragma: no cover
        require_network(RuntimeError(result.stderr.strip()))
    assert result.returncode == 0, result.stdout + result.stderr
