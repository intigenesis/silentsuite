"""Behavioural contract for the shared umbrella attachment helper.

`scripts/attach-umbrella-release-assets.sh` is the only sanctioned way any lane
writes a release asset, and all three components call it concurrently against one
draft. Reading it cannot show what it does when a twin draft exists beyond the
first three pages, when the release was published between two runs, or when the
tag moves while assets are uploading — so every case here runs the real script
against a live stand-in for the GitHub API in exactly those states.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from github_api_stub import GitHubStub, default_state

ROOT = Path(__file__).resolve().parents[1]
ATTACH = ROOT / "scripts" / "attach-umbrella-release-assets.sh"

TAG = "v1.2.3"
COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40


@pytest.fixture
def stub():
    server = GitHubStub(default_state(TAG, COMMIT))
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def assets(tmp_path: Path) -> Path:
    directory = tmp_path / "release-assets"
    directory.mkdir()
    (directory / "server-image.json").write_text('{"schemaVersion": 1}\n', encoding="utf-8")
    (directory / f"silentsuite-self-host-{TAG}.tar.gz").write_bytes(b"bundle-bytes")
    return directory


def attach(
    stub: GitHubStub,
    directory: Path,
    *,
    tag: str = TAG,
    commit: str = COMMIT,
    names: list[str] | None = None,
    extra: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    names = names if names is not None else sorted(p.name for p in directory.iterdir())
    arguments: list[str] = []
    for name in names:
        arguments += ["--asset", name]
    environment = {
        **os.environ,
        **stub.environment(),
        "GITHUB_TOKEN": "test-token",
    }
    environment.pop("GITHUB_REF", None)
    return subprocess.run(
        [
            "bash",
            str(ATTACH),
            "--tag",
            tag,
            "--expected-commit",
            commit,
            "--directory",
            str(directory),
            "--attempts",
            "2",
            "--retry-delay",
            "0",
            *arguments,
            *(extra or []),
        ],
        capture_output=True,
        text=True,
        env=environment,
    )


def uploaded(stub: GitHubStub, release_id: int) -> dict[str, bytes]:
    return {
        asset["name"]: stub.state["asset_bytes"][asset["id"]]
        for asset in stub.state["assets"].get(release_id, [])
    }


# ── The path that succeeds ────────────────────────────────────────────


def test_a_new_draft_is_created_against_the_admitted_commit(stub, assets: Path):
    result = attach(stub, assets)

    assert result.returncode == 0, result.stderr
    assert len(stub.state["releases"]) == 1
    release = stub.state["releases"][0]
    assert release["draft"] is True
    assert release["tag_name"] == TAG
    assert release["target_commitish"] == COMMIT
    assert uploaded(stub, release["id"]) == {
        "server-image.json": b'{"schemaVersion": 1}\n',
        f"silentsuite-self-host-{TAG}.tar.gz": b"bundle-bytes",
    }
    assert "publication remains manual" in result.stdout


def test_a_second_component_appends_to_the_existing_draft(stub, assets: Path, tmp_path: Path):
    release = stub.add_release(TAG, target=COMMIT)
    stub.add_asset(release["id"], f"silentsuite-android-{TAG}.apk", b"apk-bytes")

    result = attach(stub, assets)

    assert result.returncode == 0, result.stderr
    assert len(stub.state["releases"]) == 1
    names = set(uploaded(stub, release["id"]))
    assert f"silentsuite-android-{TAG}.apk" in names, "a sibling component's asset was lost"
    assert "server-image.json" in names


def test_an_identical_rerun_is_a_no_op(stub, assets: Path):
    assert attach(stub, assets).returncode == 0
    before = dict(stub.state["asset_bytes"])

    result = attach(stub, assets)

    assert result.returncode == 0, result.stderr
    assert stub.state["asset_bytes"] == before
    assert "already present with identical bytes" in result.stdout


def test_a_draft_targeting_the_default_branch_is_accepted(stub, assets: Path):
    """GitHub ignores target_commitish when the git tag already exists."""

    stub.add_release(TAG, target="main")

    assert attach(stub, assets).returncode == 0


# ── Everything that must fail closed ──────────────────────────────────


def test_a_published_release_is_never_altered(stub, assets: Path):
    release = stub.add_release(TAG, draft=False, target=COMMIT)

    result = attach(stub, assets)

    assert result.returncode != 0
    assert "already published; refusing to alter a published release" in result.stderr
    assert uploaded(stub, release["id"]) == {}


def test_a_rerun_after_publication_fails_without_touching_the_release(stub, assets: Path):
    assert attach(stub, assets).returncode == 0
    release = stub.state["releases"][0]
    before = dict(uploaded(stub, release["id"]))
    release["draft"] = False

    result = attach(stub, assets)

    assert result.returncode != 0
    assert "refusing to alter a published release" in result.stderr
    assert uploaded(stub, release["id"]) == before


def test_a_draft_targeting_another_commit_is_refused(stub, assets: Path):
    stub.add_release(TAG, target=OTHER_COMMIT)

    result = attach(stub, assets)

    assert result.returncode != 0
    assert f"not the admitted {COMMIT}" in result.stderr


def test_two_releases_claiming_the_tag_are_refused(stub, assets: Path):
    stub.add_release(TAG, target=COMMIT)
    stub.add_release(TAG, target=COMMIT)

    result = attach(stub, assets)

    assert result.returncode != 0
    assert "releases claim tag" in result.stderr


def test_a_same_named_asset_with_different_bytes_is_never_clobbered(stub, assets: Path):
    release = stub.add_release(TAG, target=COMMIT)
    stub.add_asset(release["id"], "server-image.json", b"other-bytes")

    result = attach(stub, assets)

    assert result.returncode != 0
    assert "refusing to clobber" in result.stderr
    assert uploaded(stub, release["id"])["server-image.json"] == b"other-bytes"


def test_the_matching_draft_is_found_beyond_the_first_three_pages(stub, assets: Path):
    """The old helper stopped at 300 releases; a twin past that hid from it."""

    for index in range(350):
        stub.add_release(f"v0.0.{index}", target=COMMIT)
    target = stub.add_release(TAG, target=COMMIT)

    result = attach(stub, assets)

    assert result.returncode == 0, result.stderr
    assert set(uploaded(stub, target["id"])) == {
        "server-image.json",
        f"silentsuite-self-host-{TAG}.tar.gz",
    }
    assert len(stub.state["releases"]) == 351, "a duplicate draft was created past page 3"


def test_a_duplicate_draft_beyond_page_three_is_still_detected(stub, assets: Path):
    stub.add_release(TAG, target=COMMIT)
    for index in range(350):
        stub.add_release(f"v0.0.{index}", target=COMMIT)
    stub.add_release(TAG, target=COMMIT)

    result = attach(stub, assets)

    assert result.returncode != 0
    assert "releases claim tag" in result.stderr


def test_a_release_list_that_never_terminates_is_refused(stub, assets: Path):
    for index in range(250):
        stub.add_release(f"v0.0.{index}", target=COMMIT)
    stub.add_release(TAG, target=COMMIT)

    result = attach(stub, assets, extra=["--max-pages", "2"])

    assert result.returncode != 0
    assert "did not terminate within 2 pages" in result.stderr


def test_a_moved_tag_stops_the_attachment_before_any_upload(stub, assets: Path):
    stub.state["tags"][TAG] = {"type": "commit", "sha": OTHER_COMMIT}

    result = attach(stub, assets)

    assert result.returncode != 0
    assert "attachment:pre" in result.stderr
    assert stub.state["releases"] == [], "a draft was created for an unadmitted tag"


# One tag read per identity check, so `tag_moves_after` selects which write the
# tag moves in front of. With two assets the happy path reads the tag five
# times: pre, before-create, before-upload x2, post.
#
# The point of these cases is that every one of them stops *before* the write it
# guards, not merely afterwards: a post-hoc detection cannot un-upload an asset.
@pytest.mark.parametrize(
    ("moves_after", "stage", "expected_uploads"),
    [
        (0, "attachment:pre", 0),
        (1, "attachment:before-create", 0),
        (2, "attachment:before-upload:server-image.json", 0),
        (3, f"attachment:before-upload:silentsuite-self-host-{TAG}.tar.gz", 1),
    ],
    ids=("pre", "before-create", "before-first-upload", "before-second-upload"),
)
def test_a_tag_that_moves_is_caught_before_the_next_write(
    stub, assets: Path, moves_after, stage, expected_uploads
):
    stub.state["tag_moves_after"] = moves_after

    result = attach(stub, assets)

    assert result.returncode != 0
    assert stage in result.stderr, result.stderr
    written = sum(len(v) for v in stub.state["assets"].values())
    assert written == expected_uploads, (
        f"{written} assets were uploaded after the tag moved; expected {expected_uploads}"
    )


def test_a_tag_that_moves_after_the_last_upload_still_fails_the_post_check(stub, assets: Path):
    """No pre-check can see a move that happens during the final upload."""

    stub.state["tag_moves_after"] = 4

    result = attach(stub, assets)

    assert result.returncode != 0
    assert "attachment:post" in result.stderr


def test_a_disabled_ruleset_stops_the_attachment(stub, assets: Path):
    stub.state["rulesets"][1]["enforcement"] = "disabled"

    result = attach(stub, assets)

    assert result.returncode != 0
    assert "is not actively enforced" in result.stderr
    assert stub.state["releases"] == []


@pytest.mark.parametrize(
    ("arguments", "needle"),
    [
        (["--tag", TAG, "--directory", "."], "--expected-commit"),
        (
            ["--tag", TAG, "--expected-commit", "not-a-sha", "--directory", ".", "--asset", "x"],
            "is not a 40-hex commit id",
        ),
        (["--expected-commit", COMMIT, "--directory", ".", "--asset", "x"], "--tag"),
    ],
)
def test_the_helper_refuses_an_incomplete_invocation(stub, arguments, needle):
    environment = {**os.environ, **stub.environment(), "GITHUB_TOKEN": "test-token"}
    result = subprocess.run(
        ["bash", str(ATTACH), *arguments],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode != 0
    assert needle in result.stderr


def test_a_missing_local_asset_is_refused_before_any_request(stub, assets: Path):
    result = attach(stub, assets, names=["not-there.txt"])

    assert result.returncode != 0
    assert "does not exist" in result.stderr
    assert stub.state["requests"] == []


def test_the_uploaded_bytes_are_read_back_and_compared(stub, assets: Path):
    result = attach(stub, assets)

    assert result.returncode == 0, result.stderr
    for name, payload in uploaded(stub, stub.state["releases"][0]["id"]).items():
        local = (assets / name).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == hashlib.sha256(local).hexdigest()
    assert "digest verified" in result.stdout


# ── Call ordering at every mutation boundary ──────────────────────────


def request_log(stub: GitHubStub) -> list[str]:
    """The stub records every request; identity reads mark a revalidation."""

    log = []
    for method, path in stub.state["requests"]:
        if method == "GET" and "/git/ref/tags/" in path:
            log.append("revalidate")
        elif method == "POST" and path.endswith("/releases"):
            log.append("create-draft")
        elif method == "POST" and "/assets" in path:
            log.append("upload-asset")
    return log


def test_every_write_is_immediately_preceded_by_a_revalidation(stub, assets: Path):
    """Ordering, not the presence of a call: each write's predecessor is a check.

    A revalidation that ran only at the start of the script would satisfy any
    "does it call the verifier" assertion while leaving every write unguarded.
    """

    assert attach(stub, assets).returncode == 0

    log = request_log(stub)
    writes = [index for index, event in enumerate(log) if event != "revalidate"]
    assert writes, "the run performed no write at all"
    for index in writes:
        assert index > 0 and log[index - 1] == "revalidate", (
            f"{log[index]} at position {index} was not immediately preceded by a "
            f"revalidation; sequence was {log}"
        )


def test_the_run_revalidates_once_per_write_plus_the_bracketing_pair(stub, assets: Path):
    assert attach(stub, assets).returncode == 0

    log = request_log(stub)
    writes = [event for event in log if event != "revalidate"]
    checks = [event for event in log if event == "revalidate"]
    # one draft creation + two asset uploads
    assert writes == ["create-draft", "upload-asset", "upload-asset"]
    # one per write, plus the opening and closing checks
    assert len(checks) == len(writes) + 2
    assert log[0] == "revalidate"
    assert log[-1] == "revalidate"


def test_an_identical_rerun_revalidates_but_performs_no_write(stub, assets: Path):
    """Idempotency must not be mistaken for a mutation that needs guarding."""

    assert attach(stub, assets).returncode == 0
    stub.state["requests"].clear()

    assert attach(stub, assets).returncode == 0

    log = request_log(stub)
    assert [event for event in log if event != "revalidate"] == []
    assert log.count("revalidate") == 2, "an unchanged rerun still brackets its reads"
