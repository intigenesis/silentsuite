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
OLD_TAG = "v9.9.9"
COMMIT = "b" * 40
INDEX_DIGEST = "sha256:" + "4" * 64
AMD64_DIGEST = "sha256:" + "5" * 64
ARM64_DIGEST = "sha256:" + "6" * 64
TARGET_IMAGE = f"ghcr.io/silent-suite/silentsuite-server@{INDEX_DIGEST}"
UTILITY_IMAGE = "ghcr.io/silent-suite/silentsuite-server@sha256:" + "a" * 64
BUNDLE_NAME = f"silentsuite-self-host-{TAG}.tar.gz"
BUNDLE_PREFIX = f"silentsuite-self-host-{TAG}"
OLD_CHECKSUM_NAME = f"silentsuite-self-host-{OLD_TAG}.tar.gz.sha256"
OLD_BUNDLE_NAME = f"silentsuite-self-host-{OLD_TAG}.tar.gz"

MANAGED_FILES = (
    ".env.example",
    "SELF-HOSTING.md",
    "backup-restore.sh",
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
            "backup-restore.sh",
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
        "backup-restore.sh",
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
    (install / "server-image.json").write_bytes(b"previous manifest bytes\n")
    (install / OLD_CHECKSUM_NAME).write_text(
        f"{'a' * 64}  {OLD_BUNDLE_NAME}\n", encoding="utf-8"
    )

    docker_stub = f"""#!/usr/bin/env bash
if [ "$1" = compose ]; then
  case "$*" in
    *"stop server"*)
      if [ -d "${{UPGRADE_INSTALL_DIR:-}}/.silentsuite-upgrade-backups" ]; then
        printf '%s\\n' 'backup-present-at-stop' >> "$UPGRADE_LOG"
      fi
      ;;
  esac
fi
printf '%s\\n' "$*" >> "$UPGRADE_LOG"
case "$1" in
  compose)
    case "$*" in
      *"config --images"*) printf '%s\\n' '{TARGET_IMAGE}' 'postgres:16.9-alpine' ;;
      *"pull server"*)
        if [ "${{FAIL_COMPOSE_PULL:-0}}" = 1 ]; then exit 41; fi
        ;;
      *"run --rm --no-deps"*)
        if [ "${{FAIL_MIGRATE:-0}}" = 1 ]; then exit 42; fi
        ;;
      *"up -d"*)
        if [ "${{FAIL_FIRST_UP:-0}}" = 1 ] && [ ! -e "${{FAIL_FIRST_UP_MARKER:-}}" ]; then
          touch "$FAIL_FIRST_UP_MARKER"
          exit 43
        fi
        ;;
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
      *State.Running*) printf '%s\\n' 'true' ;;
      *Config.Image*) printf '%s\\n' '{UTILITY_IMAGE}' ;;
      *Mounts*)
        case "$*" in
          *silentsuite-postgres*) printf 'volume|%s|/var/lib/docker/volumes/%s/_data\\n' "${{UPGRADE_POSTGRES_VOLUME:-default-project_pgdata}}" "${{UPGRADE_POSTGRES_VOLUME:-default-project_pgdata}}" ;;
          *silentsuite-server*) printf 'volume|%s|/var/lib/docker/volumes/%s/_data\\n' "${{UPGRADE_SERVER_VOLUME:-default-project_server_data}}" "${{UPGRADE_SERVER_VOLUME:-default-project_server_data}}" ;;
        esac
        ;;
      *State.Status*) printf '%s\\n' 'running' ;;
      *State.Health.Status*) printf '%s\\n' 'healthy' ;;
    esac
    ;;
  volume)
    if [ "${2:-}" = inspect ]; then
      if [ -n "${{FAIL_VOLUME_INSPECT:-}}" ]; then exit 45; fi
      volume_name="${{@: -1}}"
      if [[ "$volume_name" == *pgdata* ]]; then
        driver="${{UPGRADE_POSTGRES_VOLUME_DRIVER-local}}"
        options="${{UPGRADE_POSTGRES_VOLUME_OPTIONS-null}}"
      else
        driver="${{UPGRADE_SERVER_VOLUME_DRIVER-local}}"
        options="${{UPGRADE_SERVER_VOLUME_OPTIONS-null}}"
      fi
      if [[ "$*" == *"{{.Driver}}"* ]]; then
        printf '%s\n' "$driver"
      elif [[ "$*" == *"{{json .Options}}"* ]]; then
        printf '%s\n' "$options"
      else
        printf '{{}}\n'
      fi
    fi
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
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "UPGRADE_LOG": str(log),
            "UPGRADE_INSTALL_DIR": str(install),
        }
    )

    before_operator = {name: (install / name).read_bytes() for name in OPERATOR_FILES}
    before_env = (install / ".env").read_text(encoding="utf-8")
    assert not (install / f"{BUNDLE_NAME}.sha256").exists()

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
    assert "operator-password" not in result.stdout + result.stderr

    backup_line = next(line for line in result.stdout.splitlines() if line.startswith("Durable previous cohort backup: "))
    backup = Path(backup_line.split(": ", 1)[1])
    assert backup.parent == install / ".silentsuite-upgrade-backups"
    assert (backup / "files" / ".env").read_text(encoding="utf-8") == before_env
    assert (backup / "files" / "server-image.json").read_bytes() == b"previous manifest bytes\n"
    assert (install / OLD_CHECKSUM_NAME).read_text(encoding="utf-8").startswith("a" * 64)
    assert (install / f"{BUNDLE_NAME}.sha256").is_file()
    assert (backup / "files" / OLD_CHECKSUM_NAME).read_text(encoding="utf-8").startswith("a" * 64)
    metadata = (backup / "metadata").read_text(encoding="utf-8")
    assert "postgresVolume=default-project_pgdata\n" in metadata
    assert "serverDataVolume=default-project_server_data\n" in metadata
    assert f"utilityImage={UTILITY_IMAGE}\n" in metadata
    assert metadata.count("schemaVersion=1\n") == 1
    assert (backup / "metadata").stat().st_mode & 0o777 == 0o600
    assert "previousImage=ghcr.io/silent-suite/silentsuite-server@sha256:" + "0" * 64 in metadata
    assert f"targetChecksumName={BUNDLE_NAME}.sha256" in metadata
    assert f"previousChecksumName={OLD_CHECKSUM_NAME}" in metadata
    assert (backup / "restore-previous-cohort.sh").stat().st_mode & 0o111

    commands = log.read_text(encoding="utf-8").splitlines()
    admission = next(index for index, command in enumerate(commands) if "config --images" in command)
    pull = next(index for index, command in enumerate(commands) if command.startswith("pull"))
    backup_at_stop = commands.index("backup-present-at-stop")
    stop = next(index for index, command in enumerate(commands) if "stop server" in command)
    migrate = next(index for index, command in enumerate(commands) if "run --rm --no-deps" in command)
    restart = next(index for index, command in enumerate(commands) if " up -d" in command)
    assert admission < pull < backup_at_stop < stop < migrate < restart
    assert not any("stop" in command and "postgres" in command for command in commands)


@pytest.mark.parametrize(
    "failure_env",
    [
        {"UPGRADE_POSTGRES_VOLUME_DRIVER": "nfs"},
        {"UPGRADE_SERVER_VOLUME_OPTIONS": '{"device":"secret-endpoint"}'},
    ],
    ids=["custom-driver", "custom-options"],
)
def test_upgrade_rejects_custom_volume_recreation_before_durable_backup(upgrade_workspace, failure_env):
    tmp_path, install, staged, bin_dir = upgrade_workspace
    log = tmp_path / "docker.log"
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "UPGRADE_LOG": str(log),
            "UPGRADE_INSTALL_DIR": str(install),
            **failure_env,
        }
    )

    result = subprocess.run(
        ["bash", str(UPGRADE), "--staged", str(staged), "--install-dir", str(install)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "custom driver/options unsupported" in result.stderr
    assert "secret-endpoint" not in result.stderr
    assert not (install / ".silentsuite-upgrade-backups").exists()
    commands = log.read_text(encoding="utf-8").splitlines()
    assert not any("stop server" in command or "run --rm --no-deps" in command for command in commands)


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


def test_pre_migration_failure_restores_the_previous_cohort_without_stopping_server(upgrade_workspace):
    tmp_path, install, staged, bin_dir = upgrade_workspace
    old_managed = b"previous managed cohort\n"
    (install / "SELF-HOSTING.md").write_bytes(old_managed)
    old_env = (install / ".env").read_bytes()
    log = tmp_path / "docker.log"
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "UPGRADE_LOG": str(log),
            "UPGRADE_INSTALL_DIR": str(install),
            "FAIL_COMPOSE_PULL": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(UPGRADE), "--staged", str(staged), "--install-dir", str(install)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert (install / "SELF-HOSTING.md").read_bytes() == old_managed
    assert (install / ".env").read_bytes() == old_env
    assert (install / "server-image.json").read_bytes() == b"previous manifest bytes\n"
    assert (install / OLD_CHECKSUM_NAME).read_text(encoding="utf-8").startswith("a" * 64)
    assert not (install / f"{BUNDLE_NAME}.sha256").exists()
    commands = log.read_text(encoding="utf-8").splitlines()
    assert any("pull server" in command for command in commands)
    assert not any("stop server" in command for command in commands)
    assert "previous cohort restored" in result.stderr
    assert "operator-password" not in result.stdout + result.stderr


@pytest.mark.parametrize("failure_env", [{"FAIL_MIGRATE": "1"}, {"FAIL_FIRST_UP": "1"}])
def test_post_migration_failure_restores_previous_cohort_and_truthfully_reports_database_state(
    upgrade_workspace, failure_env
):
    tmp_path, install, staged, bin_dir = upgrade_workspace
    old_env = (install / ".env").read_bytes()
    log = tmp_path / "docker.log"
    marker = tmp_path / "first-up.failed"
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "UPGRADE_LOG": str(log),
            "UPGRADE_INSTALL_DIR": str(install),
            "FAIL_FIRST_UP_MARKER": str(marker),
            **failure_env,
        }
    )

    result = subprocess.run(
        ["bash", str(UPGRADE), "--staged", str(staged), "--install-dir", str(install)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert (install / ".env").read_bytes() == old_env
    assert "Django migrations remain forward-applied" in result.stderr
    assert "previous image/cohort restored and its service is healthy" in result.stderr
    assert "operator-password" not in result.stdout + result.stderr
    commands = log.read_text(encoding="utf-8").splitlines()
    stop = next(index for index, command in enumerate(commands) if "stop server" in command)
    migrate = next(index for index, command in enumerate(commands) if "run --rm --no-deps" in command)
    assert stop < migrate
    assert any(index > migrate and "up -d" in command for index, command in enumerate(commands))


def test_same_version_retry_restores_existing_target_sidecar(upgrade_workspace):
    tmp_path, install, staged, bin_dir = upgrade_workspace
    target_checksum = install / f"{BUNDLE_NAME}.sha256"
    target_checksum.write_bytes(b"previous target checksum bytes\n")
    log = tmp_path / "docker.log"
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "UPGRADE_LOG": str(log),
            "UPGRADE_INSTALL_DIR": str(install),
            "FAIL_COMPOSE_PULL": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(UPGRADE), "--staged", str(staged), "--install-dir", str(install)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert target_checksum.read_bytes() == b"previous target checksum bytes\n"
    assert (install / OLD_CHECKSUM_NAME).is_file()
    backup_line = next(line for line in result.stdout.splitlines() if line.startswith("Durable previous cohort backup: "))
    backup = Path(backup_line.split(": ", 1)[1])
    assert (backup / "files" / OLD_CHECKSUM_NAME).is_file()
    assert (backup / "files" / target_checksum.name).read_bytes() == b"previous target checksum bytes\n"


def test_symlinked_managed_destination_is_rejected_without_touching_its_referent(upgrade_workspace):
    tmp_path, install, staged, bin_dir = upgrade_workspace
    outside = tmp_path / "outside-managed-file"
    outside.write_bytes(b"must remain unchanged\n")
    (install / "docker-compose.yml").unlink()
    (install / "docker-compose.yml").symlink_to(outside)
    log = tmp_path / "docker.log"
    environment = dict(os.environ)
    environment.update({"PATH": f"{bin_dir}:{environment['PATH']}", "UPGRADE_LOG": str(log)})

    result = subprocess.run(
        ["bash", str(UPGRADE), "--staged", str(staged), "--install-dir", str(install)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "symlink" in result.stderr
    assert outside.read_bytes() == b"must remain unchanged\n"
    assert not (install / ".silentsuite-upgrade-backups").exists()
    assert not log.exists()


def test_symlinked_release_checksum_sidecar_is_rejected_without_touching_its_referent(upgrade_workspace):
    tmp_path, install, staged, bin_dir = upgrade_workspace
    outside = tmp_path / "outside-checksum"
    outside.write_bytes(b"must remain unchanged\n")
    (install / OLD_CHECKSUM_NAME).unlink()
    (install / OLD_CHECKSUM_NAME).symlink_to(outside)
    log = tmp_path / "docker.log"
    environment = dict(os.environ)
    environment.update({"PATH": f"{bin_dir}:{environment['PATH']}", "UPGRADE_LOG": str(log)})

    result = subprocess.run(
        ["bash", str(UPGRADE), "--staged", str(staged), "--install-dir", str(install)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "checksum sidecar" in result.stderr
    assert outside.read_bytes() == b"must remain unchanged\n"
    assert not (install / ".silentsuite-upgrade-backups").exists()
    assert not log.exists()
