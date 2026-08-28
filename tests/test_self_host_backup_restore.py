"""Behavioral tests for recorded self-host volume identity."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "self-host" / "backup-restore.sh"
DEFAULT_UTILITY_IMAGE = "ghcr.io/silent-suite/silentsuite-server@sha256:" + "a" * 64


DOCKER_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$DOCKER_LOG"

if [ "$1" = compose ] && [ "${2:-}" = version ]; then
  exit 0
fi

case "$1" in
  inspect)
    case "$*" in
      *State.Running*) printf 'true\n' ;;
      *Config.Image*) printf '%s\n' "${FAKE_SERVER_IMAGE-''' + DEFAULT_UTILITY_IMAGE + r'''}" ;;
      *State.Health.Status*) printf 'healthy\n' ;;
      *Mounts*)
        if [[ "$*" == *silentsuite-postgres* ]]; then
          count="${FAKE_POSTGRES_MATCHES:-1}"
          for _ in $(seq 1 "$count"); do
            printf '%s|%s|%s\n' "${FAKE_POSTGRES_TYPE-volume}" "${FAKE_POSTGRES_NAME-silentsuite-server_pgdata}" /host/postgres
          done
        else
          count="${FAKE_SERVER_MATCHES:-1}"
          for _ in $(seq 1 "$count"); do
            printf '%s|%s|%s\n' "${FAKE_SERVER_TYPE-volume}" "${FAKE_SERVER_NAME-silentsuite-server_server_data}" /host/server
          done
        fi
        ;;
    esac
    ;;
  exec)
    case "$*" in
      *pg_dump*) printf 'fixture dump\n' ;;
      *psql*) exit 0 ;;
    esac
    ;;
  run)
    if [ -n "${FAIL_TAR:-}" ]; then
      printf 'partial archive\n'
      exit 42
    fi
    printf 'archive fixture\n'
    ;;
  pull)
    if [ -n "${FAIL_UTILITY_PULL:-}" ]; then
      exit 44
    fi
    ;;
  volume)
    if [ "${2:-}" = inspect ]; then
      if [ -n "${FAIL_VOLUME_INSPECT:-}" ]; then
        exit 45
      fi
      volume_name="${@: -1}"
      if [[ "$volume_name" == *pgdata* || "$volume_name" == *database* ]]; then
        driver="${FAKE_POSTGRES_VOLUME_DRIVER-local}"
        options="${FAKE_POSTGRES_VOLUME_OPTIONS-null}"
      else
        driver="${FAKE_SERVER_VOLUME_DRIVER-local}"
        options="${FAKE_SERVER_VOLUME_OPTIONS-null}"
      fi
      if [[ "$*" == *"{{.Driver}}"* ]]; then
        printf '%s\n' "$driver"
      elif [[ "$*" == *"{{json .Options}}"* ]]; then
        printf '%s\n' "$options"
      else
        printf '{}\n'
      fi
    fi
    ;;
  compose)
    ;;
esac
'''


@pytest.fixture
def helper_workspace(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(DOCKER_STUB, encoding="utf-8")
    docker.chmod(0o755)
    install = tmp_path / "install"
    install.mkdir()
    (install / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    log = tmp_path / "docker.log"
    return tmp_path, bin_dir, install, log


def run_helper(
    workspace,
    command: str,
    *arguments: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    tmp_path, bin_dir, install, log = workspace
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "DOCKER_LOG": str(log),
        }
    )
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        ["bash", str(HELPER), command, *arguments, "--install-dir", str(install)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )


def write_backup(backup_dir: Path, *, env: str | None = None) -> None:
    backup_dir.mkdir()
    (backup_dir / "metadata").write_text(
        "schemaVersion=1\npostgresVolume=customproject_database_1\n"
        "postgresVolumeRecreationContract=local-empty-options\n"
        "serverDataVolume=customproject_application_data\n"
        "serverDataVolumeRecreationContract=local-empty-options\n"
        f"utilityImage={DEFAULT_UTILITY_IMAGE}\n",
        encoding="utf-8",
    )
    (backup_dir / "metadata").chmod(0o600)
    (backup_dir / "database.sql").write_text("dump\n", encoding="utf-8")
    (backup_dir / "server-data.tar.gz").write_bytes(b"archive")
    if env is not None:
        (backup_dir / ".env.backup").write_text(env, encoding="utf-8")
        (backup_dir / ".env.backup").chmod(0o600)
    names = sorted(
        path.name
        for path in backup_dir.iterdir()
        if path.name in {".env.backup", "database.sql", "server-data.tar.gz"}
    )
    checksums = "".join(
        f"{hashlib.sha256((backup_dir / name).read_bytes()).hexdigest()}  {name}\n"
        for name in names
    )
    (backup_dir / "checksums").write_text(checksums, encoding="utf-8")
    (backup_dir / "checksums").chmod(0o600)


def assert_no_restore_mutation(log: Path) -> None:
    commands = log.read_text(encoding="utf-8").splitlines()
    assert not any(" down" in command for command in commands)
    assert not any(command.startswith("volume rm ") for command in commands)
    assert not any(command.startswith("volume create ") for command in commands)


@pytest.mark.parametrize(
    "extra_env",
    [
        {},
        {
            "FAKE_POSTGRES_NAME": "customproject_database_1",
            "FAKE_SERVER_NAME": "customproject_application_data",
        },
    ],
    ids=["default-project-style", "custom-compose-project"],
)
def test_backup_records_the_exact_live_volume_names_before_data_operations(helper_workspace, extra_env):
    tmp_path, _bin_dir, _install, log = helper_workspace
    backup_dir = tmp_path / "backup"
    result = run_helper(
        helper_workspace,
        "backup",
        "--backup-dir",
        str(backup_dir),
        extra_env=extra_env,
    )
    assert result.returncode == 0, result.stderr
    metadata = (backup_dir / "metadata").read_text(encoding="utf-8")
    expected_postgres = extra_env.get("FAKE_POSTGRES_NAME", "silentsuite-server_pgdata")
    expected_server = extra_env.get("FAKE_SERVER_NAME", "silentsuite-server_server_data")
    assert f"postgresVolume={expected_postgres}\n" in metadata
    assert f"serverDataVolume={expected_server}\n" in metadata
    assert f"utilityImage={DEFAULT_UTILITY_IMAGE}\n" in metadata
    assert (backup_dir / "metadata").stat().st_mode & 0o777 == 0o600
    assert (backup_dir / "database.sql").is_file()
    assert (backup_dir / "server-data.tar.gz").is_file()
    assert (backup_dir / "checksums").stat().st_mode & 0o777 == 0o600
    checksum_lines = (backup_dir / "checksums").read_text(encoding="utf-8").splitlines()
    assert [line.split("  ", 1)[1] for line in checksum_lines] == [
        "database.sql",
        "server-data.tar.gz",
    ]
    backup_commands = log.read_text(encoding="utf-8")
    assert "type=bind" not in backup_commands
    assert "czf - -C /data ." in backup_commands
    assert "--network none" in backup_commands
    assert "--entrypoint tar" in backup_commands
    assert DEFAULT_UTILITY_IMAGE in backup_commands
    assert " alpine " not in backup_commands
    assert "volume rm" not in log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "extra_env",
    [
        {"FAKE_POSTGRES_NAME": ""},
        {"FAKE_SERVER_NAME": ""},
        {"FAKE_POSTGRES_TYPE": "bind"},
        {"FAKE_SERVER_TYPE": "bind"},
        {"FAKE_POSTGRES_MATCHES": "2"},
        {"FAKE_SERVER_MATCHES": "2"},
    ],
)
def test_invalid_live_mount_fails_before_backup_or_mutation(helper_workspace, extra_env):
    tmp_path, _bin_dir, _install, log = helper_workspace
    backup_dir = tmp_path / "must-not-be-created"
    result = run_helper(
        helper_workspace,
        "backup",
        "--backup-dir",
        str(backup_dir),
        extra_env=extra_env,
    )
    assert result.returncode != 0
    assert "custom backup" in result.stderr
    assert "no empty replacement volume" in result.stderr
    assert not backup_dir.exists()
    commands = log.read_text(encoding="utf-8").splitlines()
    assert not any(command.startswith("exec ") or command.startswith("run ") or command.startswith("volume rm ") or command.startswith("volume create ") for command in commands)


@pytest.mark.parametrize(
    "extra_env",
    [
        {"FAKE_POSTGRES_VOLUME_DRIVER": "nfs"},
        {"FAKE_SERVER_VOLUME_DRIVER": "azurefile"},
        {"FAKE_POSTGRES_VOLUME_OPTIONS": '{"type":"nfs","device":"secret-endpoint"}'},
        {"FAKE_SERVER_VOLUME_OPTIONS": '{"o":"addr=secret.example"}'},
        {"FAIL_VOLUME_INSPECT": "1"},
        {"FAKE_POSTGRES_VOLUME_DRIVER": "local\nlocal"},
        {"FAKE_POSTGRES_VOLUME_OPTIONS": "{}\n"},
        {"FAKE_SERVER_VOLUME_OPTIONS": "not-json"},
    ],
    ids=["postgres-custom-driver", "server-custom-driver", "postgres-options", "server-options", "inspect-failure", "ambiguous-driver", "ambiguous-options", "malformed-options"],
)
def test_custom_or_untrusted_volume_contract_fails_before_backup_artifacts(helper_workspace, extra_env):
    tmp_path, _bin_dir, _install, log = helper_workspace
    backup_dir = tmp_path / "must-not-be-created"

    result = run_helper(
        helper_workspace,
        "backup",
        "--backup-dir",
        str(backup_dir),
        extra_env=extra_env,
    )

    assert result.returncode != 0
    assert "custom driver/options unsupported" in result.stderr
    assert "secret-endpoint" not in result.stderr
    assert "secret.example" not in result.stderr
    assert not backup_dir.exists()
    commands = log.read_text(encoding="utf-8").splitlines()
    assert not any(command.startswith("exec ") or command.startswith("run ") or command.startswith("volume rm ") or command.startswith("volume create ") for command in commands)


def test_empty_local_volume_options_are_admitted(helper_workspace):
    tmp_path, _bin_dir, _install, _log = helper_workspace
    backup_dir = tmp_path / "backup"

    result = run_helper(
        helper_workspace,
        "backup",
        "--backup-dir",
        str(backup_dir),
        extra_env={"FAKE_POSTGRES_VOLUME_OPTIONS": "{}", "FAKE_SERVER_VOLUME_OPTIONS": "{}"},
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/silent-suite/silentsuite-server:latest",
        "sha256:" + "b" * 64,
        "",
        "ghcr.io/silent-suite/silentsuite-server@sha256:" + "B" * 64,
        DEFAULT_UTILITY_IMAGE + "\n" + DEFAULT_UTILITY_IMAGE,
    ],
)
def test_mutable_or_ambiguous_running_image_fails_before_backup_artifacts(helper_workspace, image):
    tmp_path, _bin_dir, _install, log = helper_workspace
    backup_dir = tmp_path / "must-not-be-created"

    result = run_helper(
        helper_workspace,
        "backup",
        "--backup-dir",
        str(backup_dir),
        extra_env={"FAKE_SERVER_IMAGE": image},
    )

    assert result.returncode != 0
    assert "canonical immutable digest" in result.stderr
    assert not backup_dir.exists()
    commands = log.read_text(encoding="utf-8").splitlines()
    assert not any(command.startswith("exec ") or command.startswith("run ") for command in commands)


def test_custom_immutable_running_image_is_recorded(helper_workspace):
    tmp_path, _bin_dir, _install, _log = helper_workspace
    image = "ghcr.io/silent-suite/silentsuite-server@sha256:" + "c" * 64
    backup_dir = tmp_path / "backup"

    result = run_helper(
        helper_workspace,
        "backup",
        "--backup-dir",
        str(backup_dir),
        extra_env={"FAKE_SERVER_IMAGE": image},
    )

    assert result.returncode == 0, result.stderr
    assert f"utilityImage={image}\n" in (backup_dir / "metadata").read_text(encoding="utf-8")


def test_restore_uses_recorded_names_without_recomputing_current_mounts(helper_workspace):
    tmp_path, _bin_dir, _install, log = helper_workspace
    backup_dir = tmp_path / "backup"
    write_backup(backup_dir)

    result = run_helper(helper_workspace, "restore", "--backup-dir", str(backup_dir))
    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8")
    assert "Mounts" not in commands
    assert "volume rm -- customproject_database_1" in commands
    assert "volume rm -- customproject_application_data" in commands
    assert "volume create -- customproject_database_1" in commands
    assert "volume create -- customproject_application_data" in commands
    assert "source=customproject_application_data,target=/data" in commands
    assert "xzf - -C /data" in commands
    assert "--network none" in commands
    assert "--entrypoint tar" in commands
    assert DEFAULT_UTILITY_IMAGE in commands
    assert "alpine" not in commands
    assert "type=bind" not in commands


def test_restore_pulls_recorded_utility_image_before_compose_down(helper_workspace):
    tmp_path, _bin_dir, _install, log = helper_workspace
    write_backup(tmp_path / "backup")

    result = run_helper(helper_workspace, "restore", "--backup-dir", str(tmp_path / "backup"))

    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8").splitlines()
    pull = next(index for index, command in enumerate(commands) if command == f"pull {DEFAULT_UTILITY_IMAGE}")
    down = next(index for index, command in enumerate(commands) if " down" in command)
    assert pull < down


@pytest.mark.parametrize(
    "metadata_change",
    [
        lambda text: text.replace("postgresVolumeRecreationContract=local-empty-options\n", ""),
        lambda text: text.replace("serverDataVolumeRecreationContract=local-empty-options", "serverDataVolumeRecreationContract=custom-options"),
        lambda text: text + "postgresVolumeRecreationContract=local-empty-options\n",
    ],
    ids=["missing-proof", "unsupported-proof", "duplicate-proof"],
)
def test_tampered_volume_recreation_proof_fails_before_restore_mutation(helper_workspace, metadata_change):
    tmp_path, _bin_dir, _install, log = helper_workspace
    backup_dir = tmp_path / "backup"
    write_backup(backup_dir)
    metadata = backup_dir / "metadata"
    metadata.write_text(metadata_change(metadata.read_text(encoding="utf-8")), encoding="utf-8")
    metadata.chmod(0o600)

    result = run_helper(helper_workspace, "restore", "--backup-dir", str(backup_dir))

    assert result.returncode != 0
    assert "metadata" in result.stderr or "recreation contract" in result.stderr
    assert_no_restore_mutation(log)
    commands = log.read_text(encoding="utf-8").splitlines()
    assert not any(command.startswith("pull ") for command in commands)


def test_utility_image_pull_failure_prevents_restore_mutation(helper_workspace):
    tmp_path, _bin_dir, _install, log = helper_workspace
    write_backup(tmp_path / "backup")

    result = run_helper(
        helper_workspace,
        "restore",
        "--backup-dir",
        str(tmp_path / "backup"),
        extra_env={"FAIL_UTILITY_PULL": "1"},
    )

    assert result.returncode != 0
    assert "will not modify the stack" in result.stderr
    assert_no_restore_mutation(log)


@pytest.mark.parametrize(
    "metadata_change",
    [
        lambda text: text.replace(f"utilityImage={DEFAULT_UTILITY_IMAGE}\n", ""),
        lambda text: text + f"utilityImage={DEFAULT_UTILITY_IMAGE}\n",
        lambda text: text.replace(f"utilityImage={DEFAULT_UTILITY_IMAGE}", "utilityImage=alpine"),
        lambda text: text.replace(f"utilityImage={DEFAULT_UTILITY_IMAGE}", "utilityImage=" + "d" * 64),
        lambda text: text.replace(f"utilityImage={DEFAULT_UTILITY_IMAGE}", "utilityImage=" + DEFAULT_UTILITY_IMAGE + "\nutilityImage=" + DEFAULT_UTILITY_IMAGE),
        lambda text: text.replace(f"utilityImage={DEFAULT_UTILITY_IMAGE}", "utilityImage=" + DEFAULT_UTILITY_IMAGE + "\nunknown=field"),
    ],
    ids=["missing", "duplicate", "mutable", "local-id", "duplicate-field", "unknown-field"],
)
def test_invalid_utility_metadata_fails_before_restore_mutation(helper_workspace, metadata_change):
    tmp_path, _bin_dir, _install, log = helper_workspace
    backup_dir = tmp_path / "backup"
    write_backup(backup_dir)
    metadata = backup_dir / "metadata"
    metadata.write_text(metadata_change(metadata.read_text(encoding="utf-8")), encoding="utf-8")
    metadata.chmod(0o600)

    result = run_helper(helper_workspace, "restore", "--backup-dir", str(backup_dir))

    assert result.returncode != 0
    assert "metadata" in result.stderr or "utility image" in result.stderr
    assert_no_restore_mutation(log)


def test_restore_uses_custom_operator_override_for_every_compose_command(helper_workspace):
    tmp_path, _bin_dir, install, log = helper_workspace
    write_backup(tmp_path / "backup")
    (install / "docker-compose.override.yml").write_text(
        "services:\n  postgres:\n    restart: always\n", encoding="utf-8"
    )

    result = run_helper(helper_workspace, "restore", "--backup-dir", str(tmp_path / "backup"))

    assert result.returncode == 0, result.stderr
    restore_commands = [
        line for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith("compose ") and "version" not in line
    ]
    assert len(restore_commands) == 3
    for command in restore_commands:
        assert "-f docker-compose.yml -f docker-compose.override.yml -f /" in command
    assert restore_commands[0].endswith(" down")
    assert restore_commands[1].endswith(" up -d postgres")
    assert restore_commands[2].endswith(" up -d")


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_unsafe_operator_override_fails_before_restore_mutation(helper_workspace, kind):
    tmp_path, _bin_dir, install, log = helper_workspace
    write_backup(tmp_path / "backup")
    override = install / "docker-compose.override.yml"
    if kind == "symlink":
        override.symlink_to(tmp_path / "operator-override.yml")
    else:
        override.mkdir()

    result = run_helper(helper_workspace, "restore", "--backup-dir", str(tmp_path / "backup"))

    assert result.returncode != 0
    assert "override" in result.stderr
    assert_no_restore_mutation(log)


@pytest.mark.parametrize("artifact", ["database.sql", "server-data.tar.gz"])
def test_corrupt_artifact_fails_before_restore_mutation(helper_workspace, artifact):
    tmp_path, _bin_dir, _install, log = helper_workspace
    backup_dir = tmp_path / "backup"
    write_backup(backup_dir)
    (backup_dir / artifact).write_bytes(b"corrupted")

    result = run_helper(helper_workspace, "restore", "--backup-dir", str(backup_dir))

    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert_no_restore_mutation(log)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda path: path.write_text(path.read_text() + "deadbeef  extra\n", encoding="utf-8"),
        lambda path: path.write_text("\n".join(path.read_text().splitlines(True)[:1]), encoding="utf-8"),
        lambda path: path.write_text(path.read_text().replace("  database.sql\n", "  wrong.sql\n"), encoding="utf-8"),
        lambda path: path.write_text("0" * 64 + path.read_text()[64:], encoding="utf-8"),
        lambda path: path.write_text(path.read_text().rstrip("\n"), encoding="utf-8"),
        lambda path: path.write_text("not a checksum\n", encoding="utf-8"),
    ],
    ids=["extra-record", "missing-record", "wrong-name", "wrong-digest", "missing-newline", "malformed"],
)
def test_corrupt_checksum_manifest_fails_before_restore_mutation(helper_workspace, mutate):
    tmp_path, _bin_dir, _install, log = helper_workspace
    backup_dir = tmp_path / "backup"
    write_backup(backup_dir)
    mutate(backup_dir / "checksums")

    result = run_helper(helper_workspace, "restore", "--backup-dir", str(backup_dir))

    assert result.returncode != 0
    assert "checksum" in result.stderr
    assert_no_restore_mutation(log)


@pytest.mark.parametrize("artifact_kind", ["symlink", "directory"])
def test_nonregular_artifact_fails_before_restore_mutation(helper_workspace, artifact_kind):
    tmp_path, _bin_dir, _install, log = helper_workspace
    backup_dir = tmp_path / "backup"
    write_backup(backup_dir)
    artifact = backup_dir / "server-data.tar.gz"
    original = backup_dir / "server-data.original"
    artifact.rename(original)
    if artifact_kind == "symlink":
        artifact.symlink_to(original)
    else:
        artifact.mkdir()

    result = run_helper(helper_workspace, "restore", "--backup-dir", str(backup_dir))

    assert result.returncode != 0
    assert "server-data.tar.gz" in result.stderr
    assert_no_restore_mutation(log)


def test_failed_archive_stream_never_reports_a_complete_backup(helper_workspace):
    tmp_path, _bin_dir, _install, _log = helper_workspace
    backup_dir = tmp_path / "backup"

    result = run_helper(
        helper_workspace,
        "backup",
        "--backup-dir",
        str(backup_dir),
        extra_env={"FAIL_TAR": "1"},
    )

    assert result.returncode != 0
    assert "Backup complete" not in result.stdout
    assert not (backup_dir / "checksums").exists()


def test_env_backup_is_bound_in_sorted_manifest_and_never_printed(helper_workspace):
    tmp_path, _bin_dir, install, log = helper_workspace
    secret = "PASSWORD=do-not-print\n"
    (install / ".env").write_text(secret, encoding="utf-8")
    backup_dir = tmp_path / "backup"
    result = run_helper(helper_workspace, "backup", "--backup-dir", str(backup_dir))

    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout + result.stderr + log.read_text(encoding="utf-8")
    checksum_lines = (backup_dir / "checksums").read_text(encoding="utf-8").splitlines()
    assert [line.split("  ", 1)[1] for line in checksum_lines] == [
        ".env.backup",
        "database.sql",
        "server-data.tar.gz",
    ]
    assert (backup_dir / ".env.backup").stat().st_mode & 0o777 == 0o600


def test_all_backup_guides_reject_legacy_fixed_volume_names():
    guide_paths = [
        ROOT / "self-host" / "SELF-HOSTING.md",
        ROOT / "apps/docs/self-hosting/backup-and-restore.md",
        ROOT / "docs/self-hosting/backup-and-restore.md",
    ]
    legacy_names = ("self-host" + "_pgdata", "self-host" + "_server_data")
    for path in guide_paths:
        source = path.read_text(encoding="utf-8")
        assert "backup-restore.sh" in source
        assert all(legacy_name not in source for legacy_name in legacy_names)
