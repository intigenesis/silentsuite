"""Behavioural fixtures for the self-host installer.

Every case below runs the real `self-host/install.sh` against a fabricated
release served by stub `curl`/`docker`/`openssl`/`uname` executables on PATH.
Nothing here touches the network or a container runtime.

The point is the failure cases: an installer that verifies a release bundle is
only worth anything if every malformed input stops it *before* it writes to the
operator's disk, and if it leaves no temporary files behind when it does.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from selfhost_release_contract import (  # noqa: E402
    MANIFEST_NAME,
    ReleaseIdentity,
    bundle_basename,
    bundle_prefix,
    render_manifest,
    sha256_file,
)

INSTALLER = ROOT / "self-host" / "install.sh"

TAG = "v9.9.9-beta"
COMMIT = "a" * 40
INDEX_DIGEST = "sha256:" + "1" * 64
AMD64_DIGEST = "sha256:" + "2" * 64
ARM64_DIGEST = "sha256:" + "3" * 64
SERVER_IMAGE = f"ghcr.io/silent-suite/silentsuite-server@{INDEX_DIGEST}"
IDENTITY = ReleaseIdentity(TAG, COMMIT, INDEX_DIGEST, AMD64_DIGEST, ARM64_DIGEST)

API = "https://api.github.com/repos/silent-suite/silentsuite"
DOWNLOAD = f"https://github.com/silent-suite/silentsuite/releases/download/{TAG}"
COMPARE = f"{API}/compare/{COMMIT}...{TAG}"
IDENTICAL_COMPARE = (
    '{\n  "status": "identical",\n  "ahead_by": 0,\n  "behind_by": 0,\n  "total_commits": 0,\n'
    '  "files": []\n}\n'
)

BUNDLE_NAME = bundle_basename(TAG)
CHECKSUM_NAME = f"{BUNDLE_NAME}.sha256"
PREFIX = bundle_prefix(TAG)

# GitHub pretty-prints release objects, so a top-level field sits at exactly two
# spaces. The installer anchors on that; the variants below prove it.
IMMUTABLE_TRUE = '  "immutable": true,\n'

BUNDLE_FILES = (
    ".env.example",
    "SELF-HOSTING.md",
    "close-signups.sh",
    "docker-compose.yml",
    "install.sh",
    "success.html",
    "update.sh",
    "verify.sh",
)


def url_key(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", url)


CURL_STUB = """#!/usr/bin/env bash
# Serves fixtures from $SILENTSUITE_FIXTURES keyed by a sanitised URL.
url=""
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
printf '%s\\n' "$url" >> "$SILENTSUITE_FIXTURES/requests.log"
# Race hook: plant something at the installer's target part-way through the
# download, the way another local principal could.
if [ -n "${SILENTSUITE_PLANT_ON:-}" ]; then
  case "$url" in
    *"$SILENTSUITE_PLANT_ON"*)
      case "${SILENTSUITE_PLANT_KIND:-dir}" in
        symlink) ln -sfn "$SILENTSUITE_PLANT_LINK" "$SILENTSUITE_PLANT_TARGET" ;;
        file) printf 'planted\\n' > "$SILENTSUITE_PLANT_TARGET" ;;
        *) mkdir -p "$SILENTSUITE_PLANT_TARGET"; printf 'planted\\n' > "$SILENTSUITE_PLANT_TARGET/planted.txt" ;;
      esac
      ;;
  esac
fi
key=$(printf '%s' "$url" | sed 's#[^A-Za-z0-9._-]#_#g')
src="$SILENTSUITE_FIXTURES/$key"
if [ ! -f "$src" ]; then
  exit 22
fi
if [ -n "$out" ]; then
  cp "$src" "$out"
else
  cat "$src"
fi
"""

UNAME_STUB = """#!/usr/bin/env bash
if [ "${1:-}" = "-m" ]; then
  printf '%s\\n' "${SILENTSUITE_FAKE_MACHINE:-x86_64}"
else
  printf 'Linux\\n'
fi
"""

OPENSSL_STUB = """#!/usr/bin/env bash
# Deterministic stand-in for `openssl rand -base64 N`.
printf 'fixedsecret%s\\n' "${3:-0}"
"""

DOCKER_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$SILENTSUITE_FIXTURES/docker.log"
case "$1" in
  compose)
    exit 0
    ;;
  pull)
    if [ "${SILENTSUITE_FAKE_PULL_FAILS:-0}" = "1" ]; then exit 1; fi
    # Race hook: the image pull is the longest window between the target check
    # and the target claim, so this is where a planted path would appear.
    if [ "${SILENTSUITE_PLANT_ON_PULL:-0}" = "1" ]; then
      case "${SILENTSUITE_PLANT_KIND:-dir}" in
        symlink) ln -sfn "$SILENTSUITE_PLANT_LINK" "$SILENTSUITE_PLANT_TARGET" ;;
        file) printf 'planted\\n' > "$SILENTSUITE_PLANT_TARGET" ;;
        *) mkdir -p "$SILENTSUITE_PLANT_TARGET"; printf 'planted\\n' > "$SILENTSUITE_PLANT_TARGET/planted.txt" ;;
      esac
    fi
    exit 0
    ;;
  image)
    # docker image inspect REF --format FORMAT
    format="$5"
    case "$format" in
      *revision*) printf '%s\\n' "${SILENTSUITE_FAKE_REVISION:-REVISION}" ;;
      *Architecture*) printf '%s\\n' "${SILENTSUITE_FAKE_PLATFORM:-linux/amd64}" ;;
      *RepoDigests*) printf '["%s"]\\n' "${SILENTSUITE_FAKE_REPO_DIGEST:-REPODIGEST}" ;;
      *) printf '\\n' ;;
    esac
    exit 0
    ;;
  container)
    if [ "$2" = "inspect" ] && [ "${SILENTSUITE_FAKE_EXISTING_CONTAINER:-0}" = "1" ]; then exit 0; fi
    exit 1
    ;;
  inspect)
    case "$*" in
      *State.Health.Status*) printf 'healthy\\n'; exit 0 ;;
      *) exit 1 ;;
    esac
    ;;
esac
exit 0
"""


def write_stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def build_bundle(
    archive: Path,
    manifest_text: str,
    *,
    unsafe: str | None = None,
    extra: str | None = None,
    omit: str | None = None,
) -> None:
    """Assemble a release bundle from the tracked self-host files."""

    with tarfile.open(archive, "w:gz") as handle:
        root = tarfile.TarInfo(PREFIX)
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        handle.addfile(root)
        for name in BUNDLE_FILES:
            if name == omit:
                continue
            payload = (ROOT / "self-host" / name).read_bytes()
            info = tarfile.TarInfo(f"{PREFIX}/{name}")
            info.size = len(payload)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            handle.addfile(info, io.BytesIO(payload))
        manifest = manifest_text.encode("utf-8")
        info = tarfile.TarInfo(f"{PREFIX}/{MANIFEST_NAME}")
        info.size = len(manifest)
        info.mode = 0o644
        handle.addfile(info, io.BytesIO(manifest))
        if unsafe == "symlink":
            link = tarfile.TarInfo(f"{PREFIX}/escape")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            handle.addfile(link)
        elif unsafe == "traversal":
            info = tarfile.TarInfo("../escaped.txt")
            info.size = 0
            handle.addfile(info, io.BytesIO(b""))
        if extra is not None:
            payload = b"# not part of the published inventory\n"
            info = tarfile.TarInfo(f"{PREFIX}/{extra}")
            info.size = len(payload)
            info.mode = 0o644
            handle.addfile(info, io.BytesIO(payload))


class Release:
    """A fabricated published release the stub curl can serve."""

    def __init__(self, fixtures: Path):
        self.fixtures = fixtures
        self.fixtures.mkdir(parents=True, exist_ok=True)
        self.manifest_text = render_manifest(IDENTITY)
        self.tags = [TAG]
        self.assets = [BUNDLE_NAME, CHECKSUM_NAME, MANIFEST_NAME]

    def put(self, url: str, content: bytes | str) -> None:
        path = self.fixtures / url_key(url)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_bytes(content)

    def publish(
        self,
        *,
        checksum_text: str | None = None,
        unsafe: str | None = None,
        extra: str | None = None,
        omit: str | None = None,
        compare: str | None = IDENTICAL_COMPARE,
        immutable: str | None = IMMUTABLE_TRUE,
    ) -> None:
        archive = self.fixtures / BUNDLE_NAME
        build_bundle(archive, self.manifest_text, unsafe=unsafe, extra=extra, omit=omit)
        digest = sha256_file(archive)

        if compare is not None:
            self.put(COMPARE, compare)

        self.put(f"{API}/releases?per_page=20", "".join(f'  "tag_name": "{tag}",\n' for tag in self.tags))
        asset_json = "".join(f'      "name": "{name}",\n' for name in self.assets)
        self.put(
            f"{API}/releases/tags/{TAG}",
            '{\n  "tag_name": "' + TAG + '",\n  "draft": false,\n'
            + (immutable or "")
            + '  "assets": [\n' + asset_json + "  ]\n}\n",
        )
        self.put(f"{DOWNLOAD}/{BUNDLE_NAME}", archive.read_bytes())
        self.put(f"{DOWNLOAD}/{CHECKSUM_NAME}", checksum_text if checksum_text is not None else f"{digest}  {BUNDLE_NAME}\n")
        self.put(f"{DOWNLOAD}/{MANIFEST_NAME}", self.manifest_text)


@pytest.fixture
def workspace(tmp_path):
    fixtures = tmp_path / "fixtures"
    binaries = tmp_path / "bin"
    tempdir = tmp_path / "tmp"
    home = tmp_path / "home"
    for directory in (fixtures, binaries, tempdir, home):
        directory.mkdir(parents=True, exist_ok=True)
        # The installer requires the target's parent to be a directory this user
        # owns that nobody else can write. A default umask of 0002 would make
        # these group-writable, which the installer correctly refuses.
        directory.chmod(0o755)

    write_stub(binaries, "curl", CURL_STUB)
    write_stub(binaries, "uname", UNAME_STUB)
    write_stub(binaries, "openssl", OPENSSL_STUB)
    write_stub(binaries, "docker", DOCKER_STUB)

    return {
        "root": tmp_path,
        "fixtures": fixtures,
        "bin": binaries,
        "tmp": tempdir,
        "home": home,
        "release": Release(fixtures),
    }


def run_installer(workspace, *arguments, env=None) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{workspace['bin']}:{environment['PATH']}",
            "TMPDIR": str(workspace["tmp"]),
            "SILENTSUITE_FIXTURES": str(workspace["fixtures"]),
            "SILENTSUITE_DIR": str(workspace["home"] / "silentsuite-server"),
            "SILENTSUITE_DOMAIN": "sync.example.test",
            "SILENTSUITE_PROXY_NETWORK": "",
            "SILENTSUITE_FAKE_REVISION": COMMIT,
            "SILENTSUITE_FAKE_PLATFORM": "linux/amd64",
            "SILENTSUITE_FAKE_REPO_DIGEST": SERVER_IMAGE,
        }
    )
    environment.pop("SILENTSUITE_VERSION", None)
    if env:
        environment.update(env)
    return subprocess.run(
        ["bash", str(INSTALLER), *arguments],
        capture_output=True,
        text=True,
        cwd=workspace["root"],
        env=environment,
    )


def leftover_temporaries(workspace) -> list[str]:
    return [entry.name for entry in workspace["tmp"].iterdir()]


def install_dir(workspace) -> Path:
    return workspace["home"] / "silentsuite-server"


# ── Success paths ─────────────────────────────────────────────────────


def test_the_release_tag_is_confirmed_to_point_at_the_manifest_commit(workspace):
    workspace["release"].publish()

    result = run_installer(workspace, "--stage-only", str(workspace["root"] / "staged"))

    assert result.returncode == 0, result.stderr
    assert COMPARE in (workspace["fixtures"] / "requests.log").read_text()
    assert f"confirmed to point at {COMMIT}" in result.stdout


def test_a_manifest_naming_an_untagged_commit_is_rejected(workspace):
    workspace["release"].publish(
        compare='{\n  "status": "diverged",\n  "ahead_by": 3,\n  "behind_by": 1,\n  "total_commits": 3\n}\n'
    )

    result = run_installer(workspace, "--stage-only", str(workspace["root"] / "staged"))

    assert result.returncode != 0
    assert "does not point at" in result.stderr
    assert not (workspace["root"] / "staged").exists()


def test_an_unverifiable_tag_commit_binding_fails_closed(workspace):
    workspace["release"].publish(compare=None)

    result = run_installer(workspace, "--stage-only", str(workspace["root"] / "staged"))

    assert result.returncode != 0
    assert "could not confirm" in result.stderr
    assert not (workspace["root"] / "staged").exists()


def test_a_valid_release_stages_the_exact_published_bytes(workspace):
    workspace["release"].publish()
    staged = workspace["root"] / "staged"

    result = run_installer(workspace, "--stage-only", str(staged))

    assert result.returncode == 0, result.stderr
    assert (staged / "docker-compose.yml").read_bytes() == (ROOT / "self-host" / "docker-compose.yml").read_bytes()
    assert (staged / MANIFEST_NAME).read_text() == workspace["release"].manifest_text
    assert (staged / BUNDLE_NAME).read_bytes() == (workspace["fixtures"] / BUNDLE_NAME).read_bytes()
    assert (staged / CHECKSUM_NAME).exists()
    assert sorted(entry.name for entry in staged.iterdir()) == sorted(
        [*BUNDLE_FILES, MANIFEST_NAME, BUNDLE_NAME, CHECKSUM_NAME]
    )
    assert SERVER_IMAGE in result.stdout
    assert leftover_temporaries(workspace) == []


def test_a_valid_release_installs_and_pins_the_immutable_index_digest(workspace):
    workspace["release"].publish()

    result = run_installer(workspace)

    assert result.returncode == 0, result.stderr
    target = install_dir(workspace)
    environment = (target / ".env").read_text()
    assert f"SILENTSUITE_SERVER_IMAGE={SERVER_IMAGE}" in environment
    assert re.search(r"^SILENTSUITE_SERVER_IMAGE=\S+@sha256:[0-9a-f]{64}$", environment, re.MULTILINE)
    for name in (
        "docker-compose.yml",
        "install.sh",
        "SELF-HOSTING.md",
        "update.sh",
        "verify.sh",
        "close-signups.sh",
        "success.html",
        MANIFEST_NAME,
    ):
        assert (target / name).exists(), name
    assert (target / "install.sh").stat().st_mode & 0o111
    assert (target / "etebase-server.ini").read_text().count("sync.example.test") == 1
    assert oct((target / ".env").stat().st_mode & 0o777) == "0o600"
    assert leftover_temporaries(workspace) == []


def test_installation_verifies_the_registry_identity_before_writing_anything(workspace):
    workspace["release"].publish()

    result = run_installer(workspace)

    assert result.returncode == 0, result.stderr
    docker_log = (workspace["fixtures"] / "docker.log").read_text()
    assert f"pull {SERVER_IMAGE}" in docker_log


def test_an_explicit_version_is_accepted(workspace):
    workspace["release"].publish()
    staged = workspace["root"] / "staged"

    result = run_installer(workspace, "--version", TAG, "--stage-only", str(staged))

    assert result.returncode == 0, result.stderr
    assert (staged / MANIFEST_NAME).exists()


# ── Release resolution failures ───────────────────────────────────────


def test_a_release_without_self_host_assets_is_not_installable(workspace):
    release = workspace["release"]
    release.assets = ["silentsuite-android-v9.9.9-beta.apk"]
    release.publish()

    result = run_installer(workspace, "--stage-only", str(workspace["root"] / "staged"))

    assert result.returncode != 0
    assert "no published SilentSuite release with self-host assets" in result.stderr


def test_there_is_no_branch_fallback(workspace):
    result = run_installer(workspace, "--stage-only", str(workspace["root"] / "staged"))

    assert result.returncode != 0
    assert "installing from a branch is" in result.stderr
    requests = (workspace["fixtures"] / "requests.log").read_text()
    assert "raw.githubusercontent.com" not in requests


def test_an_unpublished_explicit_version_is_rejected(workspace):
    result = run_installer(
        workspace, "--version", "v1.2.3-beta", "--stage-only", str(workspace["root"] / "staged")
    )

    assert result.returncode != 0
    assert "is not published" in result.stderr


def test_a_non_release_version_reference_is_rejected(workspace):
    result = run_installer(workspace, "--version", "main", "--stage-only", str(workspace["root"] / "staged"))

    assert result.returncode != 0
    assert "is not a SilentSuite release tag" in result.stderr


# ── Architecture ──────────────────────────────────────────────────────


def test_an_unsupported_host_architecture_stops_before_any_download(workspace):
    workspace["release"].publish()

    result = run_installer(
        workspace,
        "--stage-only",
        str(workspace["root"] / "staged"),
        env={"SILENTSUITE_FAKE_MACHINE": "riscv64"},
    )

    assert result.returncode != 0
    assert "unsupported host architecture 'riscv64'" in result.stderr
    assert not (workspace["fixtures"] / "requests.log").exists()


def test_an_arm64_host_is_supported(workspace):
    workspace["release"].publish()
    staged = workspace["root"] / "staged"

    result = run_installer(
        workspace,
        "--stage-only",
        str(staged),
        env={"SILENTSUITE_FAKE_MACHINE": "aarch64"},
    )

    assert result.returncode == 0, result.stderr
    assert "linux/arm64" in result.stdout


# ── Checksum failures ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "checksum_text,expected",
    [
        (f"{'d' * 64}  {BUNDLE_NAME}\n", "does not match its published checksum"),
        (f"{'d' * 64}  {BUNDLE_NAME}", "does not end with a newline"),
        (f"{'d' * 64}  {BUNDLE_NAME}\n{'e' * 64}  {BUNDLE_NAME}\n", "exactly one record"),
        (f"{'d' * 64}  some-other-file.tar.gz\n", "malformed or names a different file"),
        (f"{'d' * 63}  {BUNDLE_NAME}\n", "malformed or names a different file"),
        (f"{'d' * 64} {BUNDLE_NAME}\n", "malformed or names a different file"),
        ("", "is empty"),
        (f"\n{'d' * 64}  {BUNDLE_NAME}\n", "exactly one record"),
    ],
    ids=[
        "wrong-digest",
        "missing-newline",
        "two-records",
        "wrong-name",
        "short-digest",
        "single-space",
        "empty",
        "leading-blank-line",
    ],
)
def test_bad_checksum_sidecars_stop_the_install(workspace, checksum_text, expected):
    workspace["release"].publish(checksum_text=checksum_text)

    result = run_installer(workspace, "--stage-only", str(workspace["root"] / "staged"))

    assert result.returncode != 0
    assert expected in result.stderr
    assert not (workspace["root"] / "staged").exists()
    assert leftover_temporaries(workspace) == []


def test_a_tampered_bundle_is_rejected(workspace):
    release = workspace["release"]
    release.publish()
    tampered = (release.fixtures / url_key(f"{DOWNLOAD}/{BUNDLE_NAME}"))
    tampered.write_bytes(tampered.read_bytes() + b"tamper")

    result = run_installer(workspace, "--stage-only", str(workspace["root"] / "staged"))

    assert result.returncode != 0
    assert "does not match its published checksum" in result.stderr


# ── Manifest failures ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda text: text.replace(f'"tag": "{TAG}"', '"tag": "v0.0.1-beta"'), "tag mismatch"),
        (lambda text: text.replace('"schemaVersion": 1', '"schemaVersion": 2'), "unsupported schema version"),
        (
            lambda text: text.replace(
                '"imageRepository": "ghcr.io/silent-suite/silentsuite-server"',
                '"imageRepository": "ghcr.io/attacker/silentsuite-server"',
            ),
            "image repository",
        ),
        (lambda text: text.replace(f'"indexDigest": "{INDEX_DIGEST}"', '"indexDigest": "latest"'), "index digest"),
        (
            lambda text: text.replace(f'"expectedRevision": "{COMMIT}"', f'"expectedRevision": "{"b" * 40}"'),
            "revision does not match",
        ),
        (lambda text: text.replace('    "linux/arm64"', '    "linux/amd64"'), "platform list"),
        (lambda text: text + "\n", "unexpected length"),
        (lambda text: text.replace('"platforms": [', '"platforms2": ['), "platform list"),
        (lambda text: text.replace('  "tag":', '  "tag ":'), "tag mismatch"),
    ],
    ids=[
        "tag-mismatch",
        "schema-version",
        "repository",
        "mutable-index-reference",
        "revision-mismatch",
        "duplicate-platform",
        "extra-line",
        "renamed-platforms-field",
        "renamed-tag-field",
    ],
)
def test_bad_manifests_stop_the_install(workspace, mutate, expected):
    release = workspace["release"]
    release.manifest_text = mutate(release.manifest_text)
    release.publish()

    result = run_installer(workspace, "--stage-only", str(workspace["root"] / "staged"))

    assert result.returncode != 0
    assert expected in result.stderr
    assert not (workspace["root"] / "staged").exists()


def test_a_manifest_that_disagrees_with_the_bundle_copy_is_rejected(workspace):
    release = workspace["release"]
    release.publish()
    # The bundle keeps the correct manifest; the separately published one is
    # swapped for a different but internally valid document.
    other = ReleaseIdentity(TAG, COMMIT, INDEX_DIGEST, AMD64_DIGEST, "sha256:" + "4" * 64)
    release.put(f"{DOWNLOAD}/{MANIFEST_NAME}", render_manifest(other))

    result = run_installer(workspace, "--stage-only", str(workspace["root"] / "staged"))

    assert result.returncode != 0
    assert "differs from the published manifest" in result.stderr


def test_a_manifest_without_the_host_platform_is_rejected(workspace):
    release = workspace["release"]
    release.manifest_text = release.manifest_text.replace('    "linux/arm64"', '    "linux/amd64"')
    release.publish()

    result = run_installer(
        workspace,
        "--stage-only",
        str(workspace["root"] / "staged"),
        env={"SILENTSUITE_FAKE_MACHINE": "aarch64"},
    )

    assert result.returncode != 0


# ── Archive safety ────────────────────────────────────────────────────


@pytest.mark.parametrize("unsafe,expected", [("symlink", "links or special files"), ("traversal", "unsafe path")])
def test_unsafe_archive_members_stop_the_install(workspace, unsafe, expected):
    workspace["release"].publish(unsafe=unsafe)

    result = run_installer(workspace, "--stage-only", str(workspace["root"] / "staged"))

    assert result.returncode != 0
    assert expected in result.stderr
    assert not (workspace["root"] / "staged").exists()
    assert leftover_temporaries(workspace) == []


def test_an_extra_archive_member_stops_the_install(workspace):
    # Path-safe but not published: the inventory is closed, not a lower bound.
    workspace["release"].publish(extra="extra-payload.sh")

    result = run_installer(workspace, "--stage-only", str(workspace["root"] / "staged"))

    assert result.returncode != 0
    assert "does not contain the expected set of files" in result.stderr
    assert "unexpected: extra-payload.sh" in result.stderr
    assert not (workspace["root"] / "staged").exists()
    assert leftover_temporaries(workspace) == []


def test_a_missing_archive_member_stops_the_install(workspace):
    workspace["release"].publish(omit="verify.sh")

    result = run_installer(workspace, "--stage-only", str(workspace["root"] / "staged"))

    assert result.returncode != 0
    assert "does not contain the expected set of files" in result.stderr
    assert "missing:    verify.sh" in result.stderr
    assert not (workspace["root"] / "staged").exists()
    assert leftover_temporaries(workspace) == []


# ── Registry identity ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"SILENTSUITE_FAKE_REVISION": "c" * 40}, "reports revision"),
        ({"SILENTSUITE_FAKE_PLATFORM": "linux/arm64"}, "is linux/arm64, expected linux/amd64"),
        ({"SILENTSUITE_FAKE_REPO_DIGEST": "ghcr.io/silent-suite/silentsuite-server@sha256:" + "9" * 64}, "is not"),
        ({"SILENTSUITE_FAKE_PULL_FAILS": "1"}, "could not pull"),
    ],
    ids=["wrong-revision", "wrong-architecture", "wrong-digest", "pull-fails"],
)
def test_registry_identity_mismatches_stop_before_the_install_directory_exists(workspace, env, expected):
    workspace["release"].publish()

    result = run_installer(workspace, env=env)

    assert result.returncode != 0
    assert expected in result.stderr
    assert not install_dir(workspace).exists()
    assert leftover_temporaries(workspace) == []


# ── Existing installations ────────────────────────────────────────────


def test_an_existing_installation_is_never_modified(workspace):
    workspace["release"].publish()
    target = install_dir(workspace)
    target.mkdir(parents=True)
    (target / ".env").write_text("SILENTSUITE_SERVER_IMAGE=ghcr.io/silent-suite/silentsuite-server@sha256:0\n")
    (target / "docker-compose.yml").write_text("operator edited\n")
    before = {path.name: path.read_bytes() for path in target.iterdir()}

    result = run_installer(workspace)

    assert result.returncode != 0
    assert "target directory" in result.stderr
    after = {path.name: path.read_bytes() for path in target.iterdir()}
    assert after == before
    assert not (workspace["fixtures"] / "requests.log").exists()
    assert leftover_temporaries(workspace) == []


def test_existing_named_containers_are_never_stopped_or_replaced(workspace):
    result = run_installer(workspace, env={"SILENTSUITE_FAKE_EXISTING_CONTAINER": "1"})

    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert "will not stop or replace" in result.stderr
    assert not install_dir(workspace).exists()
    assert not (workspace["fixtures"] / "requests.log").exists()
    docker_log = (workspace["fixtures"] / "docker.log").read_text()
    assert "stop" not in docker_log
    assert "rm" not in docker_log
    assert leftover_temporaries(workspace) == []


def test_a_non_empty_staging_directory_is_refused(workspace):
    workspace["release"].publish()
    staged = workspace["root"] / "staged"
    staged.mkdir()
    (staged / "keep.txt").write_text("existing\n")

    result = run_installer(workspace, "--stage-only", str(staged))

    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert (staged / "keep.txt").read_text() == "existing\n"
    assert not (workspace["fixtures"] / "requests.log").exists()
    assert leftover_temporaries(workspace) == []


@pytest.mark.parametrize("stage_only", [False, True], ids=["install", "stage-only"])
def test_a_pre_existing_empty_target_is_refused(workspace, stage_only):
    """An empty directory is not proof it will still be empty, or a directory."""

    workspace["release"].publish()
    if stage_only:
        target = workspace["root"] / "staged"
        target.mkdir()
        arguments = ("--stage-only", str(target))
    else:
        target = install_dir(workspace)
        target.mkdir(parents=True)
        arguments = ()

    result = run_installer(workspace, *arguments)

    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert sorted(entry.name for entry in target.iterdir()) == []
    assert not (workspace["fixtures"] / "requests.log").exists()
    assert leftover_temporaries(workspace) == []


def test_a_foreign_non_empty_install_directory_is_refused_before_any_download(workspace):
    target = install_dir(workspace)
    target.mkdir(parents=True)
    (target / "operator-file").write_bytes(b"not a SilentSuite install\n")
    (target / "nested").mkdir()
    (target / "nested" / "data").write_bytes(b"operator data\x00\xff")
    before = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }

    result = run_installer(workspace)

    assert result.returncode != 0
    assert "target directory" in result.stderr
    after = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (workspace["fixtures"] / "requests.log").exists()
    docker_log = (workspace["fixtures"] / "docker.log").read_text()
    assert "container inspect" not in docker_log
    assert "pull" not in docker_log
    assert "compose up" not in docker_log
    assert leftover_temporaries(workspace) == []


# ── Immutable release admission ───────────────────────────────────────


def _downloaded_assets(workspace) -> list[str]:
    log = workspace["fixtures"] / "requests.log"
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if "/releases/download/" in line]


@pytest.mark.parametrize(
    ("immutable", "reason"),
    [
        ('  "immutable": false,\n', "explicitly mutable"),
        (None, "no immutability recorded at all (a pre-setting legacy release)"),
        ('  "immutable": true,\n  "immutable": false,\n', "duplicated declaration"),
        ('  "immutable": "true",\n', "quoted, not a boolean"),
        ('  "immutable": null,\n', "null"),
        ('  "immutable":true,\n', "malformed spacing"),
        ('      "immutable": true,\n', "nested at asset depth, not top level"),
    ],
)
def test_a_release_that_is_not_immutable_is_never_downloaded(workspace, immutable, reason):
    """The bundle checksum lives on the release it authenticates.

    That only proves something if the release cannot be rewritten afterwards, so
    a release without an unambiguous top-level immutable:true is refused before
    a single asset byte is fetched.
    """

    workspace["release"].publish(immutable=immutable)

    result = run_installer(workspace)

    assert result.returncode != 0, reason
    assert "not published as an immutable GitHub release" in result.stderr
    assert _downloaded_assets(workspace) == []
    assert not install_dir(workspace).exists()
    assert leftover_temporaries(workspace) == []


def test_an_immutable_release_is_admitted_and_reported(workspace):
    workspace["release"].publish()

    result = run_installer(workspace, "--stage-only", str(workspace["root"] / "staged"))

    assert result.returncode == 0, result.stderr
    assert "(immutable release)" in result.stdout
    assert _downloaded_assets(workspace) != []


# ── Target claim: parent trust and the download-window race ───────────


def test_a_group_or_world_writable_parent_is_refused_before_any_download(workspace):
    parent = workspace["root"] / "shared"
    parent.mkdir()
    parent.chmod(0o777)
    workspace["release"].publish()

    result = run_installer(workspace, env={"SILENTSUITE_DIR": str(parent / "silentsuite-server")})

    assert result.returncode != 0
    assert "group- or world-writable" in result.stderr
    assert not (parent / "silentsuite-server").exists()
    assert not (workspace["fixtures"] / "requests.log").exists()
    assert leftover_temporaries(workspace) == []


def test_a_missing_parent_is_refused_before_any_download(workspace):
    workspace["release"].publish()
    target = workspace["root"] / "absent" / "silentsuite-server"

    result = run_installer(workspace, env={"SILENTSUITE_DIR": str(target)})

    assert result.returncode != 0
    assert "does not exist" in result.stderr
    assert not target.parent.exists()
    assert not (workspace["fixtures"] / "requests.log").exists()


def test_a_private_parent_directory_is_accepted(workspace):
    parent = workspace["root"] / "private"
    parent.mkdir()
    parent.chmod(0o700)
    workspace["release"].publish()

    result = run_installer(workspace, env={"SILENTSUITE_DIR": str(parent / "silentsuite-server")})

    assert result.returncode == 0, result.stderr
    installed = parent / "silentsuite-server"
    assert (installed / ".env").exists()
    assert oct(installed.stat().st_mode & 0o777) == "0o750"


@pytest.mark.parametrize("kind", ["dir", "file"])
def test_a_target_planted_during_the_image_pull_is_never_written_through(workspace, kind):
    """The window Sol identified: absent at check time, occupied at write time."""

    workspace["release"].publish()
    target = install_dir(workspace)

    result = run_installer(
        workspace,
        env={
            "SILENTSUITE_PLANT_ON_PULL": "1",
            "SILENTSUITE_PLANT_TARGET": str(target),
            "SILENTSUITE_PLANT_KIND": kind,
        },
    )

    assert result.returncode != 0
    assert "appeared while the release was being verified" in result.stderr
    if kind == "dir":
        assert sorted(entry.name for entry in target.iterdir()) == ["planted.txt"]
    else:
        assert target.is_file() and target.read_text() == "planted\n"
    docker_log = (workspace["fixtures"] / "docker.log").read_text()
    assert "compose up" not in docker_log
    assert leftover_temporaries(workspace) == []


def test_a_target_symlink_planted_during_the_image_pull_is_never_followed(workspace):
    workspace["release"].publish()
    target = install_dir(workspace)
    elsewhere = workspace["root"] / "attacker-controlled"
    elsewhere.mkdir()

    result = run_installer(
        workspace,
        env={
            "SILENTSUITE_PLANT_ON_PULL": "1",
            "SILENTSUITE_PLANT_TARGET": str(target),
            "SILENTSUITE_PLANT_KIND": "symlink",
            "SILENTSUITE_PLANT_LINK": str(elsewhere),
        },
    )

    assert result.returncode != 0
    assert "appeared while the release was being verified" in result.stderr
    assert target.is_symlink()
    # Nothing was written through the link, and its mode was never changed.
    assert sorted(entry.name for entry in elsewhere.iterdir()) == []
    assert oct(elsewhere.stat().st_mode & 0o777) != "0o750"
    docker_log = (workspace["fixtures"] / "docker.log").read_text()
    assert "compose up" not in docker_log


@pytest.mark.parametrize("kind", ["dir", "symlink"])
def test_a_stage_target_planted_during_the_download_is_never_written_through(workspace, kind):
    workspace["release"].publish()
    staged = workspace["root"] / "staged"
    elsewhere = workspace["root"] / "attacker-controlled"
    elsewhere.mkdir()

    result = run_installer(
        workspace,
        "--stage-only",
        str(staged),
        env={
            "SILENTSUITE_PLANT_ON": "server-image.json",
            "SILENTSUITE_PLANT_TARGET": str(staged),
            "SILENTSUITE_PLANT_KIND": kind,
            "SILENTSUITE_PLANT_LINK": str(elsewhere),
        },
    )

    assert result.returncode != 0
    assert "appeared while the release was being verified" in result.stderr
    if kind == "dir":
        assert sorted(entry.name for entry in staged.iterdir()) == ["planted.txt"]
    else:
        assert staged.is_symlink()
        assert sorted(entry.name for entry in elsewhere.iterdir()) == []
    assert leftover_temporaries(workspace) == []


def test_a_symlinked_parent_resolving_to_a_private_directory_is_accepted(workspace):
    """A symlinked path is fine; the installer just stops addressing it that way."""

    real_parent = workspace["root"] / "real-home"
    real_parent.mkdir()
    real_parent.chmod(0o700)
    link = workspace["root"] / "link-home"
    link.symlink_to(real_parent)
    workspace["release"].publish()

    result = run_installer(workspace, env={"SILENTSUITE_DIR": str(link / "silentsuite-server")})

    assert result.returncode == 0, result.stderr
    installed = real_parent / "silentsuite-server"
    assert (installed / ".env").exists()
    assert not installed.is_symlink()
    # The claim is announced against the canonical path, not the lexical one.
    assert f"Creating install directory: {installed}" in result.stdout


def test_a_symlinked_parent_repointed_during_the_pull_cannot_redirect_the_claim(workspace):
    """Canonicalisation is what makes the claim un-redirectable.

    The lexical parent is a symlink that gets re-pointed at an attacker-owned
    directory during the image pull. Because the target was canonicalised right
    after the parent was vetted, the mkdir never traverses that symlink again.
    """

    real_parent = workspace["root"] / "real-home"
    real_parent.mkdir()
    real_parent.chmod(0o700)
    decoy = workspace["root"] / "decoy-home"
    decoy.mkdir()
    decoy.chmod(0o700)
    link = workspace["root"] / "link-home"
    link.symlink_to(real_parent)
    workspace["release"].publish()

    result = run_installer(
        workspace,
        env={
            "SILENTSUITE_DIR": str(link / "silentsuite-server"),
            "SILENTSUITE_PLANT_ON_PULL": "1",
            "SILENTSUITE_PLANT_TARGET": str(link),
            "SILENTSUITE_PLANT_KIND": "symlink",
            "SILENTSUITE_PLANT_LINK": str(decoy),
        },
    )

    assert result.returncode == 0, result.stderr
    # The re-point really happened...
    assert link.readlink() == decoy
    # ...and changed nothing: the install is in the directory that was vetted.
    assert (real_parent / "silentsuite-server" / ".env").exists()
    assert sorted(entry.name for entry in decoy.iterdir()) == []


def test_the_target_is_claimed_only_after_the_registry_image_is_verified(workspace):
    """A pull that fails must leave the target absent, not partially created."""

    workspace["release"].publish()

    result = run_installer(workspace, env={"SILENTSUITE_FAKE_PULL_FAILS": "1"})

    assert result.returncode != 0
    assert not install_dir(workspace).exists()

# ── Static guarantees ─────────────────────────────────────────────────


def test_installer_never_falls_back_to_branch_sources():
    source = INSTALLER.read_text(encoding="utf-8")
    assert "raw.githubusercontent.com/silent-suite/silentsuite/main/self-host/docker-compose.yml" not in source
    assert "GITHUB_RAW_BASE" not in source
    assert 'echo "main"' not in source


def test_installer_does_not_require_a_json_tool():
    source = INSTALLER.read_text(encoding="utf-8")
    assert not re.search(r"\bjq\b", source)
    assert "python3 -c" not in source


def test_installer_cleans_up_its_temporary_workspace_on_every_exit():
    source = INSTALLER.read_text(encoding="utf-8")
    assert "trap cleanup EXIT" in source
    assert "trap 'cleanup; exit 130' INT" in source
    assert "trap 'cleanup; exit 143' TERM" in source
    assert 'mktemp -d "${TMPDIR:-/tmp}/silentsuite-install.XXXXXXXX"' in source


def test_installer_bundle_inventory_is_closed_in_both_directions():
    source = INSTALLER.read_text(encoding="utf-8")
    assert "does not contain the expected set of files" in source
    assert 'ACTUAL_MEMBERS" != "$EXPECTED_MEMBERS' in source
