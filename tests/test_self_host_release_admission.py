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

import hashlib
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


def stage(tmp_path: Path, names: dict[str, str | bytes]):
    source = tmp_path / "artifacts"
    for relative, content in names.items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    return subprocess.run(
        ["bash", str(STAGE), str(source), str(tmp_path / "flat")],
        capture_output=True,
        text=True,
    )


BRIDGE_PAYLOADS = (
    "silentsuite-bridge-linux-arm64",
    "silentsuite-bridge-linux-x86_64",
    "silentsuite-bridge-macos-arm64",
    "silentsuite-bridge-macos-x86_64",
    "silentsuite-bridge-windows-x86_64.exe",
)


def valid_bridge_tree(*, windows_binary_marker: bool = False) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for index, name in enumerate(BRIDGE_PAYLOADS):
        payload = f"payload-{index}-{name}\n".encode()
        digest = hashlib.sha256(payload).hexdigest()
        marker = "*" if windows_binary_marker and name.endswith(".exe") else " "
        files[f"artifact-{index}/{name}"] = payload
        files[f"artifact-{index}/{name}.sha256"] = f"{digest.upper()} {marker}{name}\n".encode()
    return files


def test_staging_verifies_all_platforms_and_builds_a_deterministic_manifest(tmp_path: Path):
    result = stage(tmp_path, valid_bridge_tree(windows_binary_marker=True))

    assert result.returncode == 0, result.stderr
    flat = tmp_path / "flat"
    records = [(flat / f"{name}.sha256").read_text() for name in sorted(BRIDGE_PAYLOADS)]
    assert (flat / "SHA256SUMS.txt").read_text() == "".join(records)
    assert all(record.split()[0].islower() for record in records)
    assert records[-1].endswith("  silentsuite-bridge-windows-x86_64.exe\n")


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (lambda digest, name: f"{digest}  {name}", "malformed checksum record"),
        (lambda digest, name: f"{digest}  {name}\r\n", "unsafe top-level name"),
        (lambda digest, name: f"{digest}  {name}\n{digest}  {name}\n", "malformed checksum record"),
        (lambda digest, name: f"{digest}   {name}\n", "unsafe top-level name"),
        (lambda digest, name: f"{digest}  ../{name}\n", "unsafe top-level name"),
        (lambda digest, name: f"{digest}  wrong-name\n", "wrong filename"),
        (lambda digest, name: f"{'0' * 64}  {name}\n", "checksum mismatch"),
        (lambda digest, name: f"{digest}\t {name}\n", "malformed checksum record"),
        (lambda digest, name: f"{digest}  {name}\x00\n", "unsafe top-level name"),
    ],
)
def test_staging_refuses_malformed_or_untrusted_checksum_records(tmp_path, replacement, message):
    files = valid_bridge_tree()
    name = BRIDGE_PAYLOADS[0]
    payload = files[f"artifact-0/{name}"]
    files[f"artifact-0/{name}.sha256"] = replacement(
        hashlib.sha256(payload).hexdigest(), name
    ).encode()
    result = stage(tmp_path, files)

    assert result.returncode != 0
    assert message in result.stderr


def test_staging_refuses_an_asset_name_that_could_escape_its_directory(tmp_path: Path):
    files = valid_bridge_tree()
    files["a/.hidden-asset"] = b"x\n"
    result = stage(tmp_path, files)

    assert result.returncode != 0
    assert "unexpected name" in result.stderr


def test_staging_refuses_two_artifacts_with_the_same_name(tmp_path: Path):
    files = valid_bridge_tree()
    files["duplicate/silentsuite-bridge-linux-x86_64"] = b"two\n"
    result = stage(tmp_path, files)

    assert result.returncode != 0
    assert "both named" in result.stderr


def test_staging_refuses_missing_and_orphan_assets(tmp_path: Path):
    files = valid_bridge_tree()
    del files["artifact-0/silentsuite-bridge-linux-arm64.sha256"]
    files["orphan.sha256"] = b"0" * 64 + b"  orphan\n"
    result = stage(tmp_path, files)

    assert result.returncode != 0
    assert "inventory mismatch" in result.stderr


def test_staging_refuses_to_reuse_an_existing_directory(tmp_path: Path):
    (tmp_path / "flat").mkdir()
    result = stage(tmp_path, valid_bridge_tree())

    assert result.returncode != 0
    assert "already exists" in result.stderr


def test_staging_refuses_empty_payloads_and_symlinks(tmp_path: Path):
    files = valid_bridge_tree()
    name = BRIDGE_PAYLOADS[0]
    files[f"artifact-0/{name}"] = b""
    assert "payload is empty" in stage(tmp_path, files).stderr

    source = tmp_path / "symlink-artifacts"
    source.mkdir()
    (source / "target").write_bytes(b"payload")
    (source / "link").symlink_to("target")
    result = subprocess.run(
        ["bash", str(STAGE), str(source), str(tmp_path / "symlink-flat")],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "symlink artifact" in result.stderr


# ── Bridge artifact acquisition: the real mixed run ───────────────────
#
# Controller run 33269466888 held ten artifacts at once. The Bridge attachment
# job's `actions/download-artifact` named none of them and matched no pattern,
# so it took all ten; the staging helper refused a foreign file and the lane
# attached nothing. These cases reproduce that run's artifact set and prove the
# acquisition filter — not a change to the helper — is what fixes it.

import fnmatch  # noqa: E402
import re as _re  # noqa: E402

import yaml as _yaml  # noqa: E402

BRIDGE_WORKFLOW = ROOT / ".github" / "workflows" / "release-bridge.yml"
BRIDGE_PATTERN = "silentsuite-bridge-*"

# Artifact name -> the files that artifact contains, as the run produced them.
# `merge-multiple: false` gives each artifact its own directory, so this is the
# tree the staging helper is handed.
REAL_RUN_ARTIFACTS: dict[str, dict[str, str]] = {
    "silentsuite-bridge-linux-x86_64": {
        "silentsuite-bridge-linux-x86_64": "linux-x86_64-binary\n",
        "silentsuite-bridge-linux-x86_64.sha256": "aaa  silentsuite-bridge-linux-x86_64\n",
    },
    "silentsuite-bridge-linux-arm64": {
        "silentsuite-bridge-linux-arm64": "linux-arm64-binary\n",
        "silentsuite-bridge-linux-arm64.sha256": "bbb  silentsuite-bridge-linux-arm64\n",
    },
    "silentsuite-bridge-macos-x86_64": {
        "silentsuite-bridge-macos-x86_64": "macos-x86_64-binary\n",
        "silentsuite-bridge-macos-x86_64.sha256": "ccc  silentsuite-bridge-macos-x86_64\n",
    },
    "silentsuite-bridge-macos-arm64": {
        "silentsuite-bridge-macos-arm64": "macos-arm64-binary\n",
        "silentsuite-bridge-macos-arm64.sha256": "ddd  silentsuite-bridge-macos-arm64\n",
    },
    "silentsuite-bridge-windows-x86_64.exe": {
        "silentsuite-bridge-windows-x86_64.exe": "windows-binary\n",
        "silentsuite-bridge-windows-x86_64.exe.sha256": "eee  silentsuite-bridge-windows-x86_64.exe\n",
    },
    # Sibling lanes' artifacts, present in the same run.
    "server-image-digest-amd64": {"amd64.digest": "sha256:" + "1" * 64 + "\n"},
    "server-image-digest-arm64": {"arm64.digest": "sha256:" + "2" * 64 + "\n"},
    # Buildx build records. The upstream file name is not asserted anywhere here;
    # what matters is that a foreign artifact's contents reach staging at all.
    "build-amd64-dockerbuild": {"Build and push linux (amd64).dockerbuild": "record\n"},
    "build-arm64-dockerbuild": {"Build and push linux (arm64).dockerbuild": "record\n"},
    "conscrypt-r28-abc123": {
        "org/conscrypt/conscrypt-android/2.6.3-r28/conscrypt-android-2.6.3-r28.aar": "aar\n",
    },
}

# Keep the historical tree shape, but make its producer sidecars cryptographically
# valid now that staging verifies rather than blindly concatenating them.
for _artifact_files in REAL_RUN_ARTIFACTS.values():
    for _relative_name in tuple(_artifact_files):
        if _relative_name.endswith(".sha256"):
            _payload_name = _relative_name.removesuffix(".sha256")
            _payload = _artifact_files[_payload_name].encode()
            _artifact_files[_relative_name] = (
                f"{hashlib.sha256(_payload).hexdigest()}  {_payload_name}\n"
            )


def workflow_expected_inventory() -> list[str]:
    """The closed name set the attachment job asserts, read from the workflow."""

    workflow = _yaml.safe_load(BRIDGE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["attach-release-assets"]["steps"]
    run = next(s for s in steps if s.get("name") == "Re-assert the closed asset inventory")["run"]
    block = run.split("printf '%s\\n' \\", 1)[1].split("| LC_ALL=C sort)", 1)[0]
    return sorted(_re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]*", block))


def materialise(root: Path, artifacts: dict[str, dict[str, str]]) -> Path:
    """Lay artifacts out the way download-artifact does with merge-multiple: false."""

    source = root / "release-assets"
    for artifact, files in artifacts.items():
        for relative, content in files.items():
            target = source / artifact / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    return source


def run_staging(source: Path, staging: Path):
    return subprocess.run(
        ["bash", str(STAGE), str(source), str(staging)],
        capture_output=True,
        text=True,
    )


def filtered(artifacts: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    return {
        name: files
        for name, files in artifacts.items()
        if fnmatch.fnmatch(name, BRIDGE_PATTERN)
    }


def test_the_real_run_held_ten_artifacts_and_the_filter_admits_exactly_five():
    assert len(REAL_RUN_ARTIFACTS) == 10
    admitted = sorted(filtered(REAL_RUN_ARTIFACTS))
    assert admitted == [
        "silentsuite-bridge-linux-arm64",
        "silentsuite-bridge-linux-x86_64",
        "silentsuite-bridge-macos-arm64",
        "silentsuite-bridge-macos-x86_64",
        "silentsuite-bridge-windows-x86_64.exe",
    ]
    rejected = sorted(set(REAL_RUN_ARTIFACTS) - set(admitted))
    assert rejected == [
        "build-amd64-dockerbuild",
        "build-arm64-dockerbuild",
        "conscrypt-r28-abc123",
        "server-image-digest-amd64",
        "server-image-digest-arm64",
    ]


def test_the_unfiltered_acquisition_stops_the_lane_before_anything_is_attached(tmp_path: Path):
    """The observed failure, reproduced: staging is handed foreign artifacts."""

    source = materialise(tmp_path, REAL_RUN_ARTIFACTS)
    staging = tmp_path / "flat"

    result = run_staging(source, staging)

    assert result.returncode != 0, "the helper accepted a run it should have refused"
    assert not (staging / "SHA256SUMS.txt").exists(), "a manifest was built from foreign inputs"


def test_the_filtered_acquisition_stages_exactly_the_closed_bridge_inventory(tmp_path: Path):
    """With the pattern applied, only the five producers reach the helper."""

    source = materialise(tmp_path, filtered(REAL_RUN_ARTIFACTS))
    staging = tmp_path / "flat"

    result = run_staging(source, staging)

    assert result.returncode == 0, result.stderr
    staged = sorted(entry.name for entry in staging.iterdir())
    assert staged == workflow_expected_inventory()
    assert len(staged) == 11, staged
    # The manifest is the per-asset sidecars concatenated in C-locale order.
    manifest = (staging / "SHA256SUMS.txt").read_text(encoding="utf-8")
    sidecars = sorted(name for name in staged if name.endswith(".sha256"))
    assert manifest == "".join(
        (staging / name).read_text(encoding="utf-8") for name in sidecars
    )


def test_a_missing_bridge_producer_fails_the_closed_inventory(tmp_path: Path):
    artifacts = filtered(REAL_RUN_ARTIFACTS)
    del artifacts["silentsuite-bridge-macos-arm64"]
    source = materialise(tmp_path, artifacts)
    staging = tmp_path / "flat"

    result = run_staging(source, staging)
    assert result.returncode != 0
    assert "inventory mismatch" in result.stderr


def test_a_duplicated_bridge_producer_is_refused_by_the_helper(tmp_path: Path):
    """Two artifacts carrying one name must not silently overwrite each other."""

    artifacts = dict(filtered(REAL_RUN_ARTIFACTS))
    artifacts["silentsuite-bridge-linux-x86_64-rerun"] = {
        "silentsuite-bridge-linux-x86_64": "a-different-binary\n",
    }
    source = materialise(tmp_path, artifacts)

    result = run_staging(source, tmp_path / "flat")

    assert result.returncode != 0
    assert "both named" in result.stderr


def test_an_unexpected_extra_bridge_asset_fails_the_closed_inventory(tmp_path: Path):
    artifacts = dict(filtered(REAL_RUN_ARTIFACTS))
    artifacts["silentsuite-bridge-freebsd-x86_64"] = {
        "silentsuite-bridge-freebsd-x86_64": "unexpected\n",
        "silentsuite-bridge-freebsd-x86_64.sha256": "fff  silentsuite-bridge-freebsd-x86_64\n",
    }
    source = materialise(tmp_path, artifacts)
    staging = tmp_path / "flat"

    # It matches the acquisition pattern, so the filter alone does not stop it;
    # the closed inventory is what does.
    assert fnmatch.fnmatch("silentsuite-bridge-freebsd-x86_64", BRIDGE_PATTERN)
    result = run_staging(source, staging)
    assert result.returncode != 0
    assert "inventory mismatch" in result.stderr
