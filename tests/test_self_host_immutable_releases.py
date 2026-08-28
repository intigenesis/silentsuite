"""Behavioral contract for the release-immutability publication gate.

The guard is the only thing standing between a release lane and publishing
assets that can be rewritten afterwards, so it is exercised against a stand-in
GitHub API rather than read statically. Every case asserts the guard fails
closed: an unreadable, ambiguous, or negative answer must never look like
"enabled".
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "require-immutable-releases.sh"
REPOSITORY = "silent-suite/silentsuite"
READ_TOKEN = "fixture-admin-read-token-not-a-secret"
WORKFLOW_TOKEN = "fixture-workflow-token-not-a-secret"

CURL_STUB = r'''#!/usr/bin/env bash
# Stand-in for the single curl invocation the guard makes.
raw="$*"
printf '%s\n' "$raw" >> "$GUARD_FIXTURES/curl.log"
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -H|-w|-X) shift 2 ;;
    -*) shift ;;
    *) shift ;;
  esac
done
if [ -n "$out" ]; then
  cp "$GUARD_FIXTURES/body" "$out"
fi
printf '%s' "$(cat "$GUARD_FIXTURES/status")"
'''


def _fixture(tmp_path: Path, body: str, status: str = "200") -> Path:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(exist_ok=True)
    (fixtures / "body").write_text(body, encoding="utf-8")
    (fixtures / "status").write_text(status, encoding="utf-8")
    stub = tmp_path / "curl"
    stub.write_text(CURL_STUB, encoding="utf-8")
    stub.chmod(0o755)
    return fixtures


def _run(tmp_path: Path, fixtures: Path, **overrides) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{tmp_path}:{environment['PATH']}",
            "GUARD_FIXTURES": str(fixtures),
            "IMMUTABLE_RELEASES_READ_TOKEN": READ_TOKEN,
            # Present but unusable for this endpoint: the guard must never
            # reach for it, even when it is sitting right there.
            "GITHUB_TOKEN": WORKFLOW_TOKEN,
            "GITHUB_REPOSITORY": REPOSITORY,
        }
    )
    environment.update(overrides)
    return subprocess.run(
        ["bash", str(GUARD)], cwd=ROOT, env=environment, capture_output=True, text=True
    )


def test_an_enabled_setting_admits_the_lane(tmp_path: Path):
    fixtures = _fixture(tmp_path, json.dumps({"enabled": True, "enforced_by_owner": False}))
    result = _run(tmp_path, fixtures)
    assert result.returncode == 0, result.stderr
    assert "Release immutability is enabled" in result.stdout


def test_the_live_disabled_setting_blocks_the_lane(tmp_path: Path):
    """The value this repository actually returns today must fail closed."""

    fixtures = _fixture(tmp_path, json.dumps({"enabled": False, "enforced_by_owner": False}))
    result = _run(tmp_path, fixtures)
    assert result.returncode == 1
    assert "release immutability is disabled" in result.stderr
    assert "only reads the setting; it never changes it" in result.stderr


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ('{"enforced_by_owner": false}', "missing key"),
        ('{"enabled": "true"}', "string, not boolean"),
        ('{"enabled": null}', "null"),
        ('{"enabled": 1}', "number"),
        ('[{"enabled": true}]', "array response"),
        ("not json at all", "unparseable"),
        ("", "empty body"),
    ],
)
def test_an_ambiguous_answer_is_not_an_enabled_answer(tmp_path: Path, body: str, reason: str):
    fixtures = _fixture(tmp_path, body)
    result = _run(tmp_path, fixtures)
    assert result.returncode != 0, reason
    assert "unambiguous boolean" in result.stderr


@pytest.mark.parametrize("status", ["401", "403", "404", "500"])
def test_an_unreadable_setting_is_not_a_passing_setting(tmp_path: Path, status: str):
    fixtures = _fixture(tmp_path, json.dumps({"enabled": True}), status=status)
    result = _run(tmp_path, fixtures)
    assert result.returncode == 1
    assert f"HTTP {status}" in result.stderr
    assert "Refusing to create or attach" in result.stderr


def test_the_guard_only_reads_and_never_leaks_either_token(tmp_path: Path):
    fixtures = _fixture(tmp_path, json.dumps({"enabled": False}))
    result = _run(tmp_path, fixtures)
    for secret in (READ_TOKEN, WORKFLOW_TOKEN):
        assert secret not in result.stdout
        assert secret not in result.stderr
    calls = (fixtures / "curl.log").read_text().strip().split("\n")
    assert len(calls) == 1, "the guard should make exactly one request"
    assert f"/repos/{REPOSITORY}/immutable-releases" in calls[0]
    for mutation in ("-X POST", "-X PATCH", "-X PUT", "-X DELETE", "--data"):
        assert mutation not in calls[0], f"the guard must not {mutation} anything"


def test_the_settings_read_uses_the_dedicated_token_not_the_workflow_token(tmp_path: Path):
    """The endpoint needs repository Administration: read.

    No workflow `permissions:` block can grant that, so the guard authenticates
    with a separate read-only credential. Proving *which* token went on the wire
    is the whole point: sending the workflow token would fail in production and
    would tempt someone to widen the publisher's credential instead.
    """

    fixtures = _fixture(tmp_path, json.dumps({"enabled": True}))
    result = _run(tmp_path, fixtures)
    assert result.returncode == 0, result.stderr
    call = (fixtures / "curl.log").read_text()
    assert f"Authorization: Bearer {READ_TOKEN}" in call
    assert WORKFLOW_TOKEN not in call


def test_there_is_no_fallback_to_the_workflow_token(tmp_path: Path):
    """A missing read token must fail closed, never silently degrade."""

    fixtures = _fixture(tmp_path, json.dumps({"enabled": True}))
    result = _run(tmp_path, fixtures, IMMUTABLE_RELEASES_READ_TOKEN="")
    assert result.returncode != 0
    assert "IMMUTABLE_RELEASES_READ_TOKEN" in result.stderr
    assert not (fixtures / "curl.log").exists(), "no request may be attempted"


def test_the_workflow_token_is_never_expanded_by_the_guard():
    """Static companion to the behavioural no-fallback test."""

    source = GUARD.read_text(encoding="utf-8")
    for expansion in ("$GITHUB_TOKEN", "${GITHUB_TOKEN"):
        assert expansion not in source, f"the guard must not expand {expansion}"


def test_a_missing_environment_fails_before_any_request(tmp_path: Path):
    fixtures = _fixture(tmp_path, json.dumps({"enabled": True}))
    result = _run(tmp_path, fixtures, IMMUTABLE_RELEASES_READ_TOKEN="", GITHUB_TOKEN="")
    assert result.returncode != 0
    assert not (fixtures / "curl.log").exists()


def test_a_malformed_repository_is_rejected(tmp_path: Path):
    fixtures = _fixture(tmp_path, json.dumps({"enabled": True}))
    result = _run(tmp_path, fixtures, GITHUB_REPOSITORY="not-a-repo-pair")
    assert result.returncode == 2
    assert "<owner>/<name>" in result.stderr
    assert not (fixtures / "curl.log").exists()


def test_the_guard_never_enables_the_setting():
    """Turning immutability on is an owner action, never a repository action."""

    source = GUARD.read_text(encoding="utf-8")
    for mutation in ("-X POST", "-X PUT", "-X PATCH", "-X DELETE", "--data", "-d "):
        assert mutation not in source
