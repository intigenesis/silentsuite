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
import json
import os
import shutil
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


# ── Draft creation media type ─────────────────────────────────────────
#
# v0.5.4-beta produced no GitHub release at all: six draft-creation POSTs across
# the self-host and Bridge lanes were rejected and none returned an `.id`. The
# body was JSON, but curl's `-d` declares application/x-www-form-urlencoded and
# the `Accept` header only describes the response, so GitHub parsed the document
# as form fields. These cases pin both halves — the old request must fail, the
# new one must succeed — and the diagnostics that make the next such failure
# legible without leaking anything.


def create_release_directly(stub: GitHubStub, *, content_type: str | None) -> tuple[int, str]:
    """Replay one draft-creation POST with a chosen media type, via curl."""

    body = json.dumps(
        {
            "tag_name": TAG,
            "target_commitish": COMMIT,
            "name": f"SilentSuite {TAG}",
            "draft": True,
        }
    )
    command = [
        "curl", "-sS", "-o", "/dev/stdout", "-w", "\\n%{http_code}", "-X", "POST",
        "-H", "Authorization: Bearer test-token",
        "-H", "Accept: application/vnd.github+json",
        "-H", "X-GitHub-Api-Version: 2022-11-28",
    ]
    if content_type is not None:
        command += ["-H", f"Content-Type: {content_type}"]
    command += ["-d", body, f"{stub.url}/repos/{stub.state['repository']}/releases"]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    payload, _, status = result.stdout.rpartition("\n")
    return int(status), payload


def test_the_pre_fix_form_encoded_request_is_rejected(stub):
    """curl's default media type is what broke the release."""

    status, payload = create_release_directly(stub, content_type=None)

    assert status == 400
    assert "id" not in json.loads(payload)
    assert stub.state["releases"] == []
    assert stub.state["rejected_media_types"] == ["application/x-www-form-urlencoded"]


def test_an_explicit_json_media_type_creates_the_draft(stub):
    status, payload = create_release_directly(stub, content_type="application/json")

    assert status == 201
    assert json.loads(payload)["tag_name"] == TAG
    assert stub.state["rejected_media_types"] == []


@pytest.mark.parametrize(
    "content_type",
    ["application/x-www-form-urlencoded", "text/plain", "application/vnd.github+json"],
)
def test_only_the_exact_json_media_type_is_accepted(stub, content_type: str):
    """`application/vnd.github+json` describes the response, not the request."""

    status, _ = create_release_directly(stub, content_type=content_type)

    assert status == 400
    assert stub.state["releases"] == []


def test_the_helper_declares_the_json_media_type_on_the_creation_post(stub, assets: Path):
    assert attach(stub, assets).returncode == 0

    assert stub.state["rejected_media_types"] == [], "the helper sent a body GitHub cannot parse"
    assert len(stub.state["releases"]) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 422, 500, 502, 503])
def test_a_failed_creation_reports_the_status_and_the_sanitized_message(
    stub, assets: Path, status: int
):
    stub.state["fail"]["create_release"] = status
    stub.state["create_release_body"] = {"message": "Validation Failed", "errors": ["private"]}

    result = attach(stub, assets)

    assert result.returncode != 0
    assert f"HTTP {status}: Validation Failed" in result.stderr
    assert "could not resolve a single draft release" in result.stderr
    # Only `.message` — never another response field, never a header.
    assert "private" not in result.stderr
    assert "errors" not in result.stderr
    assert stub.state["releases"] == []


def test_an_unparseable_response_is_labelled_rather_than_dumped(stub, assets: Path):
    stub.state["fail"]["create_release"] = 502
    stub.state["create_release_body"] = "<html><body>bad gateway</body></html>"

    result = attach(stub, assets)

    assert result.returncode != 0
    assert "HTTP 502: unparseable response" in result.stderr
    assert "html" not in result.stderr.lower()


def test_a_hostile_api_message_is_bounded_and_stripped(stub, assets: Path):
    stub.state["fail"]["create_release"] = 422
    stub.state["create_release_body"] = {"message": "\x1b[31mboom\x1b[0m " + "A" * 500}

    result = attach(stub, assets)

    assert result.returncode != 0
    assert "\x1b" not in result.stderr, "terminal escapes must not reach the log"
    longest = max(len(line) for line in result.stderr.splitlines())
    assert longest < 320, f"an unbounded message reached the log ({longest} chars)"


def test_no_diagnostic_ever_prints_the_token(stub, assets: Path):
    stub.state["fail"]["create_release"] = 401
    stub.state["create_release_body"] = {"message": "Bad credentials"}

    result = attach(stub, assets)

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "test-token" not in combined
    assert "Authorization" not in combined
    assert "Bearer" not in combined


def test_a_sibling_that_wins_the_creation_race_is_joined_not_duplicated(stub, assets: Path):
    """The loser's 422 is a retry signal, not a second draft."""

    winner = stub.add_release(TAG, target=COMMIT)

    result = attach(stub, assets)

    assert result.returncode == 0, result.stderr
    assert [r["id"] for r in stub.state["releases"]] == [winner["id"]]
    assert set(uploaded(stub, winner["id"])) == {
        "server-image.json",
        f"silentsuite-self-host-{TAG}.tar.gz",
    }


def test_a_second_creation_for_the_same_tag_is_refused_by_the_api(stub):
    """The stub enforces GitHub's one-release-per-tag rule, so a duplicate
    draft cannot be manufactured by a test and pass unnoticed."""

    assert create_release_directly(stub, content_type="application/json")[0] == 201
    status, payload = create_release_directly(stub, content_type="application/json")

    assert status == 422
    assert json.loads(payload)["message"] == "Validation Failed"
    assert len(stub.state["releases"]) == 1


def test_the_request_body_never_reaches_the_process_arguments(stub, assets: Path):
    """The document goes through a file under the run's private workdir."""

    helper = ATTACH.read_text(encoding="utf-8")
    assert '--data-binary "@${request}"' in helper
    assert '-H "Content-Type: application/json"' in helper
    # And the only other POST body — an asset — is already a file upload.
    assert '--data-binary "@${DIRECTORY}/${asset}"' in helper


def test_a_transport_failure_reports_exactly_one_canonical_status(assets: Path, tmp_path: Path):
    """curl writes `000` through -w *and* exits non-zero on a dead endpoint.

    A `|| printf '000'` fallback therefore concatenated, and the helper reported
    `HTTP 000000`. The status is now taken from the write-out and validated to
    exactly three digits.
    """

    # Port 1 on loopback refuses immediately: no listener, no DNS, no timeout.
    dead = "http://127.0.0.1:1"
    environment = {
        **os.environ,
        "GITHUB_API_URL": dead,
        "GITHUB_UPLOAD_URL_BASE": dead,
        "GITHUB_REPOSITORY": "silent-suite/silentsuite",
        "GITHUB_TOKEN": "test-token",
    }
    environment.pop("GITHUB_REF", None)
    result = subprocess.run(
        [
            "bash", str(ATTACH),
            "--tag", TAG,
            "--expected-commit", COMMIT,
            "--directory", str(assets),
            "--attempts", "1",
            "--retry-delay", "0",
            "--asset", "server-image.json",
        ],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "000000" not in combined, "the status was concatenated again"
    for impossible in ("HTTP 0000", "HTTP  ", "HTTP :"):
        assert impossible not in combined
    assert "test-token" not in combined
    assert "Bearer" not in combined


def test_a_transport_failure_inside_the_creation_step_reports_http_000(
    stub, assets: Path, tmp_path: Path
):
    """Identity checks pass, then the creation POST cannot connect.

    A curl stand-in fails only the release-creation call, so the run reaches the
    diagnostic under test instead of stopping at the first identity read.
    """

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    real_curl = shutil.which("curl")
    (shim_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        "for argument in \"$@\"; do\n"
        "  if [[ \"$argument\" == *'/releases' && \"$*\" == *'-X POST'* ]]; then\n"
        "    for previous in \"$@\"; do\n"
        "      if [[ \"$previous\" == '%{http_code}' ]]; then printf '000'; fi\n"
        "    done\n"
        "    exit 7\n"
        "  fi\n"
        "done\n"
        f"exec {real_curl} \"$@\"\n",
        encoding="utf-8",
    )
    (shim_dir / "curl").chmod(0o755)

    environment = {
        **os.environ,
        **stub.environment(),
        "GITHUB_TOKEN": "test-token",
        "PATH": f"{shim_dir}:{os.environ['PATH']}",
    }
    environment.pop("GITHUB_REF", None)
    result = subprocess.run(
        [
            "bash", str(ATTACH),
            "--tag", TAG,
            "--expected-commit", COMMIT,
            "--directory", str(assets),
            "--attempts", "1",
            "--retry-delay", "0",
            "--asset", "server-image.json",
        ],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode != 0
    assert "HTTP 000: unparseable response" in result.stderr, result.stderr
    assert "000000" not in result.stderr
    assert "test-token" not in result.stdout + result.stderr
    assert stub.state["releases"] == [], "nothing was created"


def executable_lines(path: Path) -> str:
    """Source with comments removed: these rules are about what runs."""

    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


@pytest.mark.parametrize(
    "script",
    ["attach-umbrella-release-assets.sh", "verify-release-identity.sh"],
)
def test_no_curl_status_is_taken_from_a_concatenating_fallback(script: str):
    """Both helpers read `%{http_code}`; both had the same defect."""

    code = executable_lines(ROOT / "scripts" / script)
    assert "'^[0-9]{3}$'" in code, "the write-out must be validated to three digits"
    assert 'status="000"' in code
    for fallback in ("|| printf '000'", "|| echo 000"):
        assert fallback not in code, f"{script}: {fallback} concatenates with curl's own 000"


def test_the_creation_response_file_always_exists_for_the_diagnostic():
    code = executable_lines(ATTACH)
    assert ': > "$out"' in code, "curl does not create -o when it never connects"
