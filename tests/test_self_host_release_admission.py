"""Behavioural contract for the trusted release-identity verifier.

`scripts/verify-release-identity.sh` is the trust root of the whole release
control plane: the controller runs it before any candidate code exists on a
runner, and it runs again immediately before Android signing, before the GHCR
alias writes, and on both sides of every release-asset upload. So it is
exercised against a live HTTP stand-in for the GitHub API and against real git
repositories, not read statically.

Every case asserts it fails closed: a payload outside the grammar, a tag that
points somewhere else, a commit that never reached protected `main`, a ruleset
that has been disabled, relaxed, or given a bypass actor.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from github_api_stub import GitHubStub, default_state

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "scripts" / "verify-release-identity.sh"
STAGE = ROOT / "scripts" / "stage-bridge-release-assets.sh"

TAG = "v1.2.3"
COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.test",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.test",
}


@pytest.fixture
def stub():
    server = GitHubStub(default_state(TAG, COMMIT))
    try:
        yield server
    finally:
        server.close()


def verify(
    stub: GitHubStub,
    *,
    tag: str = TAG,
    commit: str = COMMIT,
    extra: list[str] | None = None,
    environment: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **GIT_ENV, **stub.environment()}
    # A real controller run has neither of these unless the job supplies them,
    # so drop any ambient value before the case adds its own.
    env.pop("GITHUB_REF", None)
    env.pop("GITHUB_TOKEN", None)
    env.update(environment or {})
    return subprocess.run(
        [
            "bash",
            str(VERIFY),
            "--tag",
            tag,
            "--commit",
            commit,
            "--attempts",
            "1",
            "--retry-delay",
            "0",
            *(extra or []),
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


# ── The one path that succeeds ────────────────────────────────────────


def test_an_immutable_tag_on_protected_main_is_verified(stub, tmp_path: Path):
    output = tmp_path / "github-output"
    result = verify(
        stub,
        extra=["--emit-outputs"],
        environment={"GITHUB_OUTPUT": str(output), "GITHUB_REF": "refs/heads/main"},
    )

    assert result.returncode == 0, result.stderr
    assert f"{TAG} -> {COMMIT}" in result.stdout
    recorded = output.read_text(encoding="utf-8")
    assert f"tag={TAG}" in recorded
    assert f"commit={COMMIT}" in recorded
    # bypass_actors is not served without administration:read, and the verifier
    # says so rather than pretending it checked.
    assert "bypass-actors=unobservable" in recorded
    # The run must say it did not prove this, not merely that it could not read it.
    assert "UNOBSERVABLE — not proven by this run." in result.stdout
    assert "Owner-only release authority comes" in result.stdout
    assert "verified exactly" not in result.stdout


def test_an_annotated_tag_is_dereferenced_to_its_commit(stub):
    stub.state["tags"][TAG] = {"type": "tag", "sha": "c" * 40}
    stub.state["annotated"]["c" * 40] = {"type": "commit", "sha": COMMIT}

    assert verify(stub).returncode == 0


@pytest.mark.parametrize("tag", ["v0.1.0", "v10.20.30", "v1.2.3-beta", "v1.2.3-rc.1"])
def test_the_release_tag_grammar_accepts_the_published_shapes(stub, tag: str):
    stub.state["tags"] = {tag: {"type": "commit", "sha": COMMIT}}

    assert verify(stub, tag=tag).returncode == 0


def test_a_visible_bypass_list_is_enforced_exactly(stub):
    """A privileged reader must not get a weaker check than an anonymous one."""

    stub.state["rulesets"][0]["bypass_actors"] = [
        {"actor_id": 265568982, "actor_type": "User", "bypass_mode": "always"}
    ]
    stub.state["rulesets"][1]["bypass_actors"] = []

    result = verify(stub)
    assert result.returncode == 0, result.stderr
    assert "bypass actors: verified exactly" in result.stdout


# ── Everything that must fail closed ──────────────────────────────────


@pytest.mark.parametrize(
    "tag",
    ["nightly", "v1.2", "1.2.3", "v1.2.3.4", "v1.2.3-beta_1", "release-v1.2.3", "v1.2.3 "],
)
def test_a_tag_outside_the_release_grammar_is_refused(stub, tag: str):
    result = verify(stub, tag=tag)

    assert result.returncode != 0
    assert "is not a SilentSuite release tag" in result.stderr


@pytest.mark.parametrize("commit", ["not-a-sha", "abc", "A" * 40, "a" * 39, "a" * 41])
def test_a_commit_outside_the_40_hex_grammar_is_refused(stub, commit: str):
    result = verify(stub, commit=commit)

    assert result.returncode != 0
    assert "is not a 40-hex commit id" in result.stderr


def test_a_tag_that_does_not_exist_is_refused(stub):
    result = verify(stub, tag="v9.9.9")

    assert result.returncode != 0
    assert "answered HTTP 404" in result.stderr


def test_a_tag_pointing_at_a_different_commit_is_refused(stub):
    """The moved-tag case, even though ruleset 20051355 forbids the move."""

    stub.state["tags"][TAG] = {"type": "commit", "sha": OTHER_COMMIT}

    result = verify(stub)

    assert result.returncode != 0
    assert f"{TAG} currently points at {OTHER_COMMIT}" in result.stderr


def test_a_tag_deleted_between_admission_and_mutation_is_refused(stub):
    stub.state["tags"].clear()

    result = verify(stub, extra=["--stage", "server-registry-publication"])

    assert result.returncode != 0
    assert "server-registry-publication" in result.stderr


def test_a_tag_pointing_at_a_tree_is_refused(stub):
    stub.state["tags"][TAG] = {"type": "tree", "sha": "d" * 40}

    result = verify(stub)

    assert result.returncode != 0
    assert "points at a 'tree' object" in result.stderr


def test_an_annotated_tag_that_does_not_target_a_commit_is_refused(stub):
    stub.state["tags"][TAG] = {"type": "tag", "sha": "c" * 40}
    stub.state["annotated"]["c" * 40] = {"type": "tree", "sha": "d" * 40}

    result = verify(stub)

    assert result.returncode != 0
    assert "points at a 'tree', not a commit" in result.stderr


def test_a_commit_that_never_reached_protected_main_is_refused(stub):
    """The original blocker: a release tag created at an unreviewed commit."""

    stub.state["compare"] = {}

    result = verify(stub)

    assert result.returncode != 0
    assert "is not on the protected branch" in result.stderr


def test_a_commit_ahead_of_protected_main_is_refused(stub):
    stub.state["compare"][COMMIT] = ("ahead", COMMIT)

    result = verify(stub)

    assert result.returncode != 0
    assert "is 'ahead' relative to main" in result.stderr


def test_a_commit_that_is_not_its_own_merge_base_is_refused(stub):
    stub.state["compare"][COMMIT] = ("behind", OTHER_COMMIT)

    result = verify(stub)

    assert result.returncode != 0
    assert "is not its own merge base" in result.stderr


def test_a_repository_whose_default_branch_moved_is_refused(stub):
    stub.state["default_branch"] = "trunk"

    result = verify(stub)

    assert result.returncode != 0
    assert "default branch is 'trunk'" in result.stderr


def test_running_from_a_ref_other_than_the_protected_branch_is_refused(stub):
    """repository_dispatch always loads main; anything else is a broken premise."""

    result = verify(stub, environment={"GITHUB_REF": "refs/tags/v1.2.3"})

    assert result.returncode != 0
    assert "was loaded from refs/tags/v1.2.3" in result.stderr


@pytest.mark.parametrize("index", [0, 1])
def test_a_disabled_tag_ruleset_is_refused(stub, index: int):
    stub.state["rulesets"][index]["enforcement"] = "disabled"

    result = verify(stub)

    assert result.returncode != 0
    assert "is not actively enforced" in result.stderr


@pytest.mark.parametrize("index", [0, 1])
def test_a_missing_tag_ruleset_is_refused(stub, index: int):
    removed = stub.state["rulesets"].pop(index)

    result = verify(stub)

    assert result.returncode != 0
    assert f"ruleset {removed['id']}" in result.stderr


def test_a_relaxed_immutability_ruleset_is_refused(stub):
    """Dropping the deletion rule would make the tag movable again."""

    stub.state["rulesets"][1]["rules"] = [{"type": "update"}, {"type": "non_fast_forward"}]

    result = verify(stub)

    assert result.returncode != 0
    assert "ruleset 20051355 rules are" in result.stderr


def test_a_ruleset_that_no_longer_covers_release_tags_is_refused(stub):
    stub.state["rulesets"][0]["conditions"]["ref_name"]["include"] = ["refs/tags/release-*"]

    result = verify(stub)

    assert result.returncode != 0
    assert "no longer includes exactly refs/tags/v*" in result.stderr


def test_a_ruleset_that_excludes_some_release_tags_is_refused(stub):
    stub.state["rulesets"][1]["conditions"]["ref_name"]["exclude"] = ["refs/tags/v1.*"]

    result = verify(stub)

    assert result.returncode != 0
    assert "excludes refs from its own pattern" in result.stderr


def test_a_bypass_actor_on_the_immutability_ruleset_is_refused(stub):
    """Visible drift must fail; an unobservable field must never be assumed."""

    stub.state["rulesets"][1]["bypass_actors"] = [
        {"actor_id": 1, "actor_type": "User", "bypass_mode": "always"}
    ]

    result = verify(stub)

    assert result.returncode != 0
    assert "ruleset 20051355 bypass actors are" in result.stderr


def test_an_unexpected_creation_bypass_actor_is_refused(stub):
    stub.state["rulesets"][0]["bypass_actors"] = [
        {"actor_id": 999, "actor_type": "User", "bypass_mode": "always"}
    ]

    result = verify(stub)

    assert result.returncode != 0
    assert "ruleset 20051354 bypass actors are" in result.stderr


def test_a_ruleset_inherited_from_an_organisation_is_refused(stub):
    stub.state["rulesets"][0]["source_type"] = "Organization"

    result = verify(stub)

    assert result.returncode != 0
    assert "is not owned by this repository" in result.stderr


def test_a_duplicated_ruleset_id_is_refused(stub):
    stub.state["rulesets"].append(dict(stub.state["rulesets"][0]))

    result = verify(stub)

    assert result.returncode != 0
    assert "is not published exactly once" in result.stderr


def test_an_unavailable_ruleset_api_is_refused(stub):
    """Fail closed on the API, never open."""

    stub.state["fail"]["rulesets"] = 500

    result = verify(stub)

    assert result.returncode != 0
    assert "answered HTTP 500" in result.stderr


def test_a_token_that_cannot_read_rulesets_falls_back_to_the_public_read(stub):
    """The lane holds no administration credential and must not need one."""

    result = verify(stub, environment={"GITHUB_TOKEN": "unprivileged"})

    assert result.returncode == 0, result.stderr


def test_a_missing_repository_environment_fails_before_any_request(stub):
    environment = {**os.environ, **stub.environment()}
    environment.pop("GITHUB_REPOSITORY")
    result = subprocess.run(
        ["bash", str(VERIFY), "--tag", TAG, "--commit", COMMIT],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode != 0
    assert "GITHUB_REPOSITORY" in result.stderr


def test_the_verifier_never_writes_to_the_repository():
    """It reads. It never pushes, tags, commits, or issues a mutating request."""

    source = VERIFY.read_text(encoding="utf-8")
    for mutation in ("git push", "git tag ", "git commit", "git update-ref", "-X POST", "-X PATCH", "-X DELETE"):
        assert mutation not in source, f"the verifier must not {mutation!r}"


# ── Local ancestry re-derivation ──────────────────────────────────────


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def commit_file(repo: Path, message: str) -> str:
    (repo / "file.txt").write_text(message + "\n", encoding="utf-8")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def lane(tmp_path: Path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    base = commit_file(origin, "base")

    def clone_at(ref: str) -> Path:
        work = tmp_path / f"work-{ref[:12]}"
        subprocess.run(
            ["git", "clone", "-q", str(origin), str(work)],
            check=True,
            capture_output=True,
            env={**os.environ, **GIT_ENV},
        )
        git(work, "checkout", "-q", "--detach", ref)
        return work

    return {"origin": origin, "base": base, "clone_at": clone_at}


def test_local_ancestry_confirms_the_api_answer(lane, tmp_path: Path):
    sha = lane["base"]
    git(lane["origin"], "tag", TAG)
    work = lane["clone_at"](sha)
    server = GitHubStub(default_state(TAG, sha))
    try:
        result = verify(server, commit=sha, extra=["--git-ancestry", str(work)])
    finally:
        server.close()

    assert result.returncode == 0, result.stderr


def test_local_ancestry_catches_a_tag_the_api_still_reports_correctly(lane):
    """Two independent answers; disagreement stops the release."""

    first = lane["base"]
    second = commit_file(lane["origin"], "second")
    git(lane["origin"], "tag", TAG, second)
    work = lane["clone_at"](first)
    server = GitHubStub(default_state(TAG, first))
    try:
        result = verify(server, commit=first, extra=["--git-ancestry", str(work)])
    finally:
        server.close()

    assert result.returncode != 0
    assert f"the fetched {TAG} resolves to {second}" in result.stderr


def test_local_ancestry_refuses_a_commit_off_protected_main(lane):
    git(lane["origin"], "checkout", "-q", "-b", "side")
    unreviewed = commit_file(lane["origin"], "unreviewed")
    git(lane["origin"], "tag", TAG, unreviewed)
    git(lane["origin"], "checkout", "-q", "main")
    work = lane["clone_at"](unreviewed)
    server = GitHubStub(default_state(TAG, unreviewed))
    try:
        result = verify(server, commit=unreviewed, extra=["--git-ancestry", str(work)])
    finally:
        server.close()

    assert result.returncode != 0
    assert "is not reachable from protected main" in result.stderr


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
