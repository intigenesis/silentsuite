"""Behavioural contract for the shared release-source admission helper.

Every component lane (Android, Bridge, self-host server image) runs
`scripts/admit-release-source.sh` in a read-only job before anything is signed,
pushed or attached, so it is exercised against real git repositories rather than
read statically. Every case asserts it fails closed: a branch push, a stray tag,
a re-pointed tag, a moved checkout, or a commit that never reached protected
`main` must never look like an admitted release.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIT = ROOT / "scripts" / "admit-release-source.sh"
STAGE = ROOT / "scripts" / "stage-bridge-release-assets.sh"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.test",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.test",
}


def git(repo: Path, *args: str) -> str:
    environment = {**os.environ, **GIT_ENV}
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()


def commit(repo: Path, message: str) -> str:
    (repo / "file.txt").write_text(message + "\n", encoding="utf-8")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def lane(tmp_path: Path):
    """An `origin` with protected main, and a checkout that mimics the runner."""

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    base = commit(origin, "base")

    def clone_at(ref: str) -> Path:
        work = tmp_path / f"work-{ref.replace('/', '_')}"
        subprocess.run(
            ["git", "clone", "-q", str(origin), str(work)],
            check=True,
            capture_output=True,
            env={**os.environ, **GIT_ENV},
        )
        git(work, "checkout", "-q", "--detach", ref)
        return work

    return {"origin": origin, "base": base, "clone_at": clone_at, "tmp": tmp_path}


def run_admit(workdir: Path, ref: str, sha: str, output: Path | None = None):
    environment = {**os.environ, **GIT_ENV, "GITHUB_REF": ref, "GITHUB_SHA": sha}
    if output is not None:
        environment["GITHUB_OUTPUT"] = str(output)
    else:
        environment.pop("GITHUB_OUTPUT", None)
    return subprocess.run(
        ["bash", str(ADMIT)],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=environment,
    )


# ── Admission: the one path that succeeds ─────────────────────────────


def test_a_release_tag_on_protected_main_is_admitted(lane, tmp_path: Path):
    sha = lane["base"]
    git(lane["origin"], "tag", "v1.2.3")
    work = lane["clone_at"](sha)
    output = tmp_path / "github-output"

    result = run_admit(work, "refs/tags/v1.2.3", sha, output)

    assert result.returncode == 0, result.stderr
    assert f"Admitted v1.2.3 at {sha}" in result.stdout
    assert f"tag=v1.2.3" in output.read_text()
    assert f"commit={sha}" in output.read_text()


@pytest.mark.parametrize("tag", ["v0.1.0", "v10.20.30", "v1.2.3-beta", "v1.2.3-rc.1"])
def test_the_release_tag_grammar_accepts_the_published_shapes(lane, tag: str):
    sha = lane["base"]
    git(lane["origin"], "tag", tag)
    work = lane["clone_at"](sha)

    assert run_admit(work, f"refs/tags/{tag}", sha).returncode == 0


# ── Admission: everything that must fail closed ───────────────────────


def test_a_branch_push_is_refused(lane):
    work = lane["clone_at"](lane["base"])

    result = run_admit(work, "refs/heads/main", lane["base"])

    assert result.returncode != 0
    assert "only admits tag pushes" in result.stderr


@pytest.mark.parametrize(
    "tag",
    ["nightly", "v1.2", "1.2.3", "v1.2.3.4", "v1.2.3-beta_1", "release-v1.2.3", "v1.2.3 "],
)
def test_a_tag_outside_the_release_grammar_is_refused(lane, tag: str):
    sha = lane["base"]
    work = lane["clone_at"](sha)

    result = run_admit(work, f"refs/tags/{tag}", sha)

    assert result.returncode != 0
    assert "is not a SilentSuite release tag" in result.stderr


def test_a_non_hex_commit_is_refused(lane):
    git(lane["origin"], "tag", "v1.2.3")
    work = lane["clone_at"](lane["base"])

    result = run_admit(work, "refs/tags/v1.2.3", "not-a-sha")

    assert result.returncode != 0
    assert "not a 40-hex SHA" in result.stderr


def test_a_tag_pointing_at_a_different_commit_than_the_checkout_is_refused(lane):
    """The tag object and the checked-out tree must be the same commit."""

    first = lane["base"]
    second = commit(lane["origin"], "second")
    git(lane["origin"], "tag", "v1.2.3", second)
    work = lane["clone_at"](first)

    result = run_admit(work, "refs/tags/v1.2.3", first)

    assert result.returncode != 0
    assert "not the exact tag commit" in result.stderr


def test_a_tag_repointed_after_the_checkout_is_refused(lane):
    """The re-point is caught even though GITHUB_SHA matches the checkout."""

    first = lane["base"]
    git(lane["origin"], "tag", "v1.2.3", first)
    work = lane["clone_at"](first)
    second = commit(lane["origin"], "second")
    git(lane["origin"], "tag", "-f", "v1.2.3", second)

    result = run_admit(work, "refs/tags/v1.2.3", first)

    assert result.returncode != 0
    assert "not the exact tag commit" in result.stderr


def test_a_tag_on_a_commit_that_never_reached_protected_main_is_refused(lane):
    """The blocker case: a release tag pushed at an unreviewed commit."""

    git(lane["origin"], "checkout", "-q", "-b", "side")
    unreviewed = commit(lane["origin"], "unreviewed")
    git(lane["origin"], "tag", "v9.9.9", unreviewed)
    git(lane["origin"], "checkout", "-q", "main")
    work = lane["clone_at"](unreviewed)

    result = run_admit(work, "refs/tags/v9.9.9", unreviewed)

    assert result.returncode != 0
    assert "is not reachable from protected main" in result.stderr


def test_a_checkout_that_is_not_the_named_commit_is_refused(lane):
    """`ref: ${{ github.sha }}` is a caller contract; this enforces it anyway."""

    first = lane["base"]
    second = commit(lane["origin"], "second")
    git(lane["origin"], "tag", "v1.2.3", second)
    work = lane["clone_at"](first)

    result = run_admit(work, "refs/tags/v1.2.3", second)

    assert result.returncode != 0
    assert "not the exact tag commit" in result.stderr


def test_a_missing_environment_fails_before_any_git_work(tmp_path: Path):
    result = subprocess.run(
        ["bash", str(ADMIT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k not in {"GITHUB_REF", "GITHUB_SHA"}},
    )
    assert result.returncode != 0
    assert "GITHUB_REF" in result.stderr


def test_the_helper_never_writes_to_the_repository():
    """Admission reads. It fetches, and it never pushes, tags or commits."""

    source = ADMIT.read_text(encoding="utf-8")
    for mutation in ("git push", "git tag", "git commit", "git update-ref", "-X POST", "curl "):
        assert mutation not in source, f"admission must not {mutation!r}"


def test_the_helper_needs_no_credential():
    """It runs after a `persist-credentials: false` checkout, so it has none."""

    source = ADMIT.read_text(encoding="utf-8")
    for token in ("GITHUB_TOKEN", "secrets.", "Authorization"):
        assert token not in source


# ── Bridge asset staging ──────────────────────────────────────────────


def stage(tmp_path: Path, names: dict[str, str]):
    source = tmp_path / "artifacts"
    for relative, content in names.items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return subprocess.run(
        ["bash", str(STAGE), str(source), str(tmp_path / "flat")],
        capture_output=True,
        text=True,
    )


def test_staging_flattens_and_builds_a_deterministic_manifest(tmp_path: Path):
    result = stage(
        tmp_path,
        {
            "b/silentsuite-bridge-macos-arm64": "m\n",
            "b/silentsuite-bridge-macos-arm64.sha256": "bbb  silentsuite-bridge-macos-arm64\n",
            "a/silentsuite-bridge-linux-x86_64": "l\n",
            "a/silentsuite-bridge-linux-x86_64.sha256": "aaa  silentsuite-bridge-linux-x86_64\n",
        },
    )

    assert result.returncode == 0, result.stderr
    manifest = (tmp_path / "flat" / "SHA256SUMS.txt").read_text()
    assert manifest == (
        "aaa  silentsuite-bridge-linux-x86_64\nbbb  silentsuite-bridge-macos-arm64\n"
    ), "the manifest order must not depend on artifact arrival"


def test_staging_refuses_an_asset_name_that_could_escape_its_directory(tmp_path: Path):
    result = stage(
        tmp_path,
        {
            "a/.hidden-asset": "x\n",
            "a/silentsuite-bridge-linux-x86_64.sha256": "aaa  silentsuite-bridge-linux-x86_64\n",
        },
    )

    assert result.returncode != 0
    assert "unexpected name" in result.stderr


def test_staging_refuses_two_artifacts_with_the_same_name(tmp_path: Path):
    result = stage(
        tmp_path,
        {
            "a/silentsuite-bridge-linux-x86_64": "one\n",
            "b/silentsuite-bridge-linux-x86_64": "two\n",
            "a/silentsuite-bridge-linux-x86_64.sha256": "aaa  silentsuite-bridge-linux-x86_64\n",
        },
    )

    assert result.returncode != 0
    assert "both named" in result.stderr


def test_staging_refuses_to_produce_an_empty_checksum_manifest(tmp_path: Path):
    result = stage(tmp_path, {"a/silentsuite-bridge-linux-x86_64": "l\n"})

    assert result.returncode != 0
    assert "no per-asset checksum files" in result.stderr


def test_staging_refuses_to_reuse_an_existing_directory(tmp_path: Path):
    (tmp_path / "flat").mkdir()
    result = stage(
        tmp_path,
        {"a/silentsuite-bridge-linux-x86_64.sha256": "aaa  silentsuite-bridge-linux-x86_64\n"},
    )

    assert result.returncode != 0
    assert "already exists" in result.stderr
