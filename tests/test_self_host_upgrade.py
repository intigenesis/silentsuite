"""Behavioral contract for the checked manual self-host upgrade path."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UPGRADE = ROOT / "self-host" / "upgrade.sh"
TAG = "v10.0.0-beta"
COMMIT = "b" * 40
INDEX_DIGEST = "sha256:" + "4" * 64
AMD64_DIGEST = "sha256:" + "5" * 64
ARM64_DIGEST = "sha256:" + "6" * 64
TARGET_IMAGE = f"ghcr.io/silent-suite/silentsuite-server@{INDEX_DIGEST}"
BUNDLE_NAME = f"silentsuite-self-host-{TAG}.tar.gz"
BUNDLE_PREFIX = f"silentsuite-self-host-{TAG}"

MANAGED_FILES = (
    ".env.example",
    "SELF-HOSTING.md",
    "close-signups.sh",
    "docker-compose.yml",
    "install.sh",
    "success.html",
    "upgrade.sh",
    "update.sh",
    "verify.sh",
    "server-image.json",
    f"silentsuite-self-host-{TAG}.tar.gz.sha256",
)
OPERATOR_FILES = ("etebase-server.ini", "docker-compose.override.yml", "operator-data.txt")


def manifest() -> str:
    return (
        "{\n"
        '  "schemaVersion": 1,\n'
        f'  "tag": "{TAG}",\n'
        f'  "sourceCommit": "{COMMIT}",\n'
        '  "imageRepository": "ghcr.io/silent-suite/silentsuite-server",\n'
        f'  "indexDigest": "{INDEX_DIGEST}",\n'
        f'  "amd64Digest": "{AMD64_DIGEST}",\n'
        f'  "arm64Digest": "{ARM64_DIGEST}",\n'
        '  "platforms": [\n'
        '    "linux/amd64",\n'
        '    "linux/arm64"\n'
        "  ],\n"
        f'  "expectedRevision": "{COMMIT}"\n'
        "}\n"
    )


def build_bundle(path: Path) -> None:
    with tarfile.open(path, "w:gz") as archive:
        root = tarfile.TarInfo(BUNDLE_PREFIX)
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        for name in (
            ".env.example",
            "SELF-HOSTING.md",
            "close-signups.sh",
            "docker-compose.yml",
            "install.sh",
            "success.html",
            "upgrade.sh",
            "update.sh",
            "verify.sh",
        ):
            payload = (ROOT / "self-host" / name).read_bytes()
            info = tarfile.TarInfo(f"{BUNDLE_PREFIX}/{name}")
            info.size = len(payload)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            archive.addfile(info, io.BytesIO(payload))
        payload = manifest().encode("utf-8")
        info = tarfile.TarInfo(f"{BUNDLE_PREFIX}/server-image.json")
        info.size = len(payload)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(payload))


@pytest.fixture
def upgrade_workspace(tmp_path: Path):
    install = tmp_path / "install"
    staged = tmp_path / "staged"
    bin_dir = tmp_path / "bin"
    install.mkdir()
    staged.mkdir()
    bin_dir.mkdir()

    source_files = [
        ".env.example",
        "SELF-HOSTING.md",
        "close-signups.sh",
        "docker-compose.yml",
        "install.sh",
        "success.html",
        "upgrade.sh",
        "update.sh",
        "verify.sh",
    ]
    for name in source_files:
        source = ROOT / "self-host" / name
        shutil.copy2(source, staged / name)
        shutil.copy2(source, install / name)

    (staged / "server-image.json").write_text(manifest(), encoding="utf-8")
    bundle = staged / BUNDLE_NAME
    build_bundle(bundle)
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    (staged / f"{BUNDLE_NAME}.sha256").write_text(f"{digest}  {BUNDLE_NAME}\n", encoding="utf-8")

    (install / ".env").write_text(
        "SILENTSUITE_SERVER_IMAGE=ghcr.io/silent-suite/silentsuite-server@sha256:" + "0" * 64 + "\n"
        "DATABASE_PASSWORD=operator-password\n"
        "SUPER_PASS=operator-admin-password\n",
        encoding="utf-8",
    )
    (install / "etebase-server.ini").write_bytes(b"operator ini\x00\xff\n")
    (install / "docker-compose.override.yml").write_bytes(b"operator override\n")
    (install / "operator-data.txt").write_bytes(b"operator data\x00\xff\n")

    docker_stub = f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$UPGRADE_LOG"
case "$1" in
  compose)
    case "$*" in
      *"config --images"*) printf '%s\\n' '{TARGET_IMAGE}' 'postgres:16.9-alpine' ;;
      *) exit 0 ;;
    esac
    ;;
  pull) exit 0 ;;
  image)
    case "$5" in
      *revision*) printf '%s\\n' '{COMMIT}' ;;
      *Architecture*) printf '%s\\n' 'linux/amd64' ;;
      *RepoDigests*) printf '%s\\n' '["{TARGET_IMAGE}"]' ;;
    esac
    ;;
  inspect)
    case "$*" in
      *State.Status*) printf '%s\\n' 'running' ;;
      *State.Health.Status*) printf '%s\\n' 'healthy' ;;
    esac
    ;;
esac
"""
    (bin_dir / "docker").write_text(docker_stub, encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)
    curl_stub = "#!/usr/bin/env bash\nexit 0\n"
    (bin_dir / "curl").write_text(curl_stub, encoding="utf-8")
    (bin_dir / "curl").chmod(0o755)

    return tmp_path, install, staged, bin_dir


def test_upgrade_advances_managed_files_and_preserves_operator_files(upgrade_workspace):
    tmp_path, install, staged, bin_dir = upgrade_workspace
    log = tmp_path / "docker.log"
    environment = dict(os.environ)
    environment.update({"PATH": f"{bin_dir}:{environment['PATH']}", "UPGRADE_LOG": str(log)})

    before_operator = {name: (install / name).read_bytes() for name in OPERATOR_FILES}
    before_env = (install / ".env").read_text(encoding="utf-8")

    result = subprocess.run(
        ["bash", str(UPGRADE), "--staged", str(staged), "--install-dir", str(install)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    for name in MANAGED_FILES:
        assert (install / name).read_bytes() == (staged / name).read_bytes(), name
    assert {name: (install / name).read_bytes() for name in OPERATOR_FILES} == before_operator

    expected_env = before_env.replace(
        before_env.split("SILENTSUITE_SERVER_IMAGE=", 1)[1].splitlines()[0], TARGET_IMAGE
    )
    assert (install / ".env").read_text(encoding="utf-8") == expected_env
    assert f"SILENTSUITE_SERVER_IMAGE={TARGET_IMAGE}" in (install / ".env").read_text()

    commands = log.read_text(encoding="utf-8").splitlines()
    admission = next(index for index, command in enumerate(commands) if "config --images" in command)
    pull = next(index for index, command in enumerate(commands) if command.startswith("pull"))
    migrate = next(index for index, command in enumerate(commands) if "run --rm --no-deps" in command)
    restart = next(index for index, command in enumerate(commands) if " up -d" in command)
    assert admission < pull < migrate < restart


@pytest.mark.parametrize("tamper", ["staged", "archive"])
def test_upgrade_rejects_tampering_before_operator_state_mutation(upgrade_workspace, tamper):
    tmp_path, install, staged, bin_dir = upgrade_workspace
    if tamper == "staged":
        (staged / "docker-compose.yml").open("ab").write(b"\n# staged release marker\n")
    else:
        bundle = staged / BUNDLE_NAME
        bundle.write_bytes(bundle.read_bytes() + b"tampered")

    log = tmp_path / "docker.log"
    environment = dict(os.environ)
    environment.update({"PATH": f"{bin_dir}:{environment['PATH']}", "UPGRADE_LOG": str(log)})
    before_install = {
        path.name: path.read_bytes()
        for path in install.iterdir()
        if path.is_file()
    }
    before_operator = {name: (install / name).read_bytes() for name in OPERATOR_FILES}

    result = subprocess.run(
        ["bash", str(UPGRADE), "--staged", str(staged), "--install-dir", str(install)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "checksum" in result.stderr or "differs from the verified archive" in result.stderr
    assert {path.name: path.read_bytes() for path in install.iterdir() if path.is_file()} == before_install
    assert {name: (install / name).read_bytes() for name in OPERATOR_FILES} == before_operator
    assert not log.exists()
