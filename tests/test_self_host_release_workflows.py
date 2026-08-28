"""Semantic contracts for the self-host image CI and release workflows.

These assertions parse the workflows rather than grepping their source, so a
rename or a re-indent cannot quietly satisfy them. They exist to keep four
boundaries fixed:

  * the release lane is reachable only from an immutable tag that is proven to
    be on protected main;
  * neither lane can reach hosted production credentials, environments, or
    self-hosted runners;
  * nothing publishes a mutable reference, and no partial platform alias is
    exposed before both architectures have passed their native smoke;
  * the production deploy workflow stays isolated — not called, not modified,
    and not callable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
RELEASE_WORKFLOW = WORKFLOW_DIR / "release-server-image.yml"
CI_WORKFLOW = WORKFLOW_DIR / "ci-server.yml"
DEPLOY_WORKFLOW = WORKFLOW_DIR / "deploy-server.yml"

SHA_PINNED = re.compile(r"^[^./][^/]*/[^/@]+(?:/[^@]+)?@[0-9a-f]{40}$")
NATIVE_RUNNERS = {"ubuntu-24.04": "linux/amd64", "ubuntu-24.04-arm": "linux/arm64"}
SMOKE_SCRIPT = "scripts/self-host-image-smoke.sh"

PRODUCTION_MARKERS = (
    "server-production",
    "VPS_HOST",
    "VPS_USER",
    "VPS_SSH_KEY",
    "SERVER_DEPLOY_APPROVED_SHA",
    "appleboy/ssh-action",
    "deploy-server.yml",
    "server.silentsuite.io",
)


class StrictLoader(yaml.BaseLoader):
    """BaseLoader that refuses duplicate keys and YAML merge keys."""


def _construct_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise AssertionError("YAML merge keys are not allowed in workflows")
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise AssertionError(f"duplicate workflow key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


StrictLoader.add_constructor("tag:yaml.org,2002:map", _construct_mapping)


def load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=StrictLoader)


def steps(job: dict) -> list[dict]:
    return job.get("steps", [])


def step_names(job: dict) -> list[str]:
    return [step.get("name", "") for step in steps(job)]


def step_named(job: dict, name: str) -> dict:
    matches = [step for step in steps(job) if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}, found {len(matches)}"
    return matches[0]


def all_workflows() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


RELEASE = load(RELEASE_WORKFLOW)
CI = load(CI_WORKFLOW)
DEPLOY = load(DEPLOY_WORKFLOW)


# ── Release lane: admission ───────────────────────────────────────────


def test_release_workflow_triggers_only_on_immutable_tag_pushes():
    triggers = RELEASE["on"]
    assert set(triggers) == {"push"}
    assert set(triggers["push"]) == {"tags"}
    assert triggers["push"]["tags"] == ["v*"]
    assert RELEASE["concurrency"]["cancel-in-progress"] == "false"


def test_release_workflow_grants_no_default_permissions():
    assert RELEASE["permissions"] == {}


def test_release_jobs_are_exactly_the_admission_build_merge_attach_chain():
    assert set(RELEASE["jobs"]) == {"admit", "build", "publish-index", "attach-release-assets"}
    assert RELEASE["jobs"]["build"]["needs"] == "admit"
    assert RELEASE["jobs"]["publish-index"]["needs"] == ["admit", "build"]
    assert RELEASE["jobs"]["attach-release-assets"]["needs"] == ["admit", "publish-index"]


@pytest.mark.parametrize(
    "job_name,expected",
    [
        ("admit", {"contents": "read"}),
        ("build", {"contents": "read", "packages": "write"}),
        (
            "publish-index",
            {"contents": "read", "packages": "write", "id-token": "write", "attestations": "write"},
        ),
        ("attach-release-assets", {"contents": "write"}),
    ],
)
def test_release_jobs_request_minimum_permissions(job_name, expected):
    assert RELEASE["jobs"][job_name]["permissions"] == expected


def test_only_the_asset_attachment_job_can_write_repository_contents():
    writers = [
        name
        for name, job in RELEASE["jobs"].items()
        if job.get("permissions", {}).get("contents") == "write"
    ]
    assert writers == ["attach-release-assets"]


def test_release_admission_requires_a_tag_reachable_from_protected_main():
    run = step_named(RELEASE["jobs"]["admit"], "Require an immutable tag reachable from protected main")["run"]
    assert r"^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$" in run
    assert "git merge-base --is-ancestor \"$GITHUB_SHA\" origin/main" in run
    assert "+refs/heads/main:refs/remotes/origin/main" in run
    assert '"$(git rev-parse HEAD)" != "$GITHUB_SHA"' in run
    assert '"$TAG_COMMIT" != "$GITHUB_SHA"' in run


def test_release_admission_runs_before_anything_is_built():
    for name, job in RELEASE["jobs"].items():
        if name == "admit":
            continue
        needs = job["needs"]
        assert "admit" in ([needs] if isinstance(needs, str) else needs)


# ── Release lane: per-platform build and smoke ────────────────────────


def test_release_builds_natively_on_both_architectures():
    matrix = RELEASE["jobs"]["build"]["strategy"]["matrix"]["include"]
    assert [(entry["platform"], entry["runner"]) for entry in matrix] == [
        ("linux/amd64", "ubuntu-24.04"),
        ("linux/arm64", "ubuntu-24.04-arm"),
    ]
    assert RELEASE["jobs"]["build"]["strategy"]["fail-fast"] == "true"


def test_release_build_pushes_by_digest_without_any_version_alias():
    build = step_named(RELEASE["jobs"]["build"], "Build and push ${{ matrix.platform }} by digest")
    inputs = build["with"]
    assert "push-by-digest=true" in inputs["outputs"]
    assert "name-canonical=true" in inputs["outputs"]
    assert "tags" not in inputs, "a per-architecture build must not create a release alias"
    assert inputs["build-args"] == "VCS_REF=${{ github.sha }}"
    # Attestations would make the recorded child digest an intermediate index
    # instead of the platform manifest the installer verifies.
    assert inputs["provenance"] == "false"
    assert inputs["sbom"] == "false"


def test_release_smoke_runs_the_shared_script_against_the_pushed_digest():
    job = RELEASE["jobs"]["build"]
    smoke = step_named(job, "Smoke the exact pushed content")
    assert SMOKE_SCRIPT in smoke["run"]
    assert 'docker pull "${IMAGE_NAME}@${CHILD_DIGEST}"' in smoke["run"]
    assert '--expect-revision "$GITHUB_SHA"' in smoke["run"]
    assert smoke["env"]["CHILD_DIGEST"] == "${{ steps.build.outputs.digest }}"


def test_release_child_digest_is_only_exposed_after_its_smoke_passes():
    names = step_names(RELEASE["jobs"]["build"])
    assert names.index("Smoke the exact pushed content") < names.index("Record the smoked child digest")
    assert names.index("Record the smoked child digest") < names.index(
        "Publish the verified child digest to the merge job"
    )


# ── Release lane: index, verification, bundle ─────────────────────────


def test_release_merges_only_verified_children_into_immutable_references():
    run = step_named(RELEASE["jobs"]["publish-index"], "Merge verified children into the release index")["run"]
    assert '--tag "${IMAGE_NAME}:${reference}"' in run
    assert 'COMMIT_REF="selfhost-${RELEASE_COMMIT}"' in run
    assert 'publish_alias "$COMMIT_REF"' in run
    assert 'existing="$(scripts/verify-server-image-release.sh' in run
    assert '"$existing" = "absent"' in run
    assert "latest" not in run


def test_release_alias_publication_is_idempotent_but_conflict_safe():
    run = step_named(RELEASE["jobs"]["publish-index"], "Merge verified children into the release index")["run"]
    assert RELEASE["concurrency"]["group"] == "release-server-image-${{ github.sha }}"
    assert "--verify-reference" in run
    assert "--expected-index-digest" in run
    assert "ALIAS_DIR" in run
    assert "expected_index" in run
    assert "aliases resolve to different verified indexes" in run
    assert run.count("docker buildx imagetools create") == 1
    assert "one alias at a" in run
    assert "not a registry-level atomicity claim" in run
    assert "Both aliases are checked again" in run


def test_release_verifier_can_validate_an_existing_alias_identity():
    verifier = (ROOT / "scripts" / "verify-server-image-release.sh").read_text(encoding="utf-8")
    assert "--verify-reference" in verifier
    assert "--expected-index-digest" in verifier
    assert "verify_index_reference" in verifier
    assert "index children do not match the verified per-platform digests" in verifier
    assert "revision label is" in verifier


def test_release_never_pushes_or_moves_latest():
    source = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert ":latest" not in source
    assert "Assert latest was not moved" in step_names(RELEASE["jobs"]["publish-index"])
    guard = step_named(RELEASE["jobs"]["publish-index"], "Assert latest was not moved")["run"]
    assert '"$AFTER" != "$LATEST_BEFORE"' in guard


def test_release_verifies_the_registry_before_building_the_bundle():
    names = step_names(RELEASE["jobs"]["publish-index"])
    assert names.index("Verify the published release image") < names.index("Build the self-host release bundle")
    assert names.index("Build the self-host release bundle") < names.index(
        "Verify the self-host release bundle"
    )
    assert names.index("Verify the self-host release bundle") < names.index(
        "Upload the verified self-host assets"
    )
    verify = step_named(RELEASE["jobs"]["publish-index"], "Verify the published release image")["run"]
    assert "scripts/verify-server-image-release.sh" in verify
    assert '--amd64-digest "$AMD64_DIGEST"' in verify
    assert '--arm64-digest "$ARM64_DIGEST"' in verify
    verifier = (ROOT / "scripts" / "verify-server-image-release.sh").read_text(encoding="utf-8")
    assert 'actual_config_digest="sha256:$(sha256sum' in verifier
    assert '"$actual_config_digest" != "$config_digest"' in verifier


def test_release_attests_provenance_without_mutating_the_index():
    attest = step_named(RELEASE["jobs"]["publish-index"], "Attest build provenance for the release index")
    assert attest["uses"].startswith("actions/attest-build-provenance@")
    assert attest["with"]["push-to-registry"] == "false"
    assert attest["with"]["subject-digest"] == "${{ steps.verify.outputs.index-digest }}"


# ── Release lane: shared draft release ────────────────────────────────


def test_release_attaches_only_self_host_assets_to_the_shared_draft():
    job = RELEASE["jobs"]["attach-release-assets"]
    names = step_names(job)
    assert names.index("Re-verify the assets before they leave the workflow") < names.index(
        "Attach the verified assets to the shared draft release"
    )
    attach = step_named(job, "Attach the verified assets to the shared draft release")["run"]
    assert "scripts/publish-self-host-release-assets.sh" in attach
    for asset in (
        '--asset "silentsuite-self-host-${RELEASE_TAG}.tar.gz"',
        '--asset "silentsuite-self-host-${RELEASE_TAG}.tar.gz.sha256"',
        "--asset server-image.json",
    ):
        assert asset in attach


def test_release_never_publishes_the_umbrella_release():
    source = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    for forbidden in ("draft: false", "gh release edit", "gh release create", "make_latest"):
        assert forbidden not in source


def test_shared_draft_helper_is_race_safe_and_fails_closed():
    helper = (ROOT / "scripts" / "publish-self-host-release-assets.sh").read_text(encoding="utf-8")
    assert "refusing to guess which draft to append to" in helper
    assert "refusing to clobber" in helper
    assert "refusing to alter a published release" in helper
    assert "assets that existed before this upload are now missing" in helper
    assert "read back with a different digest" in helper
    assert helper.count("if ! find_release; then") >= 2
    assert "sole draft claiming" in helper
    # A release is only ever created as a draft, and never edited afterwards.
    assert "draft: true" in helper
    assert "-X PATCH" not in helper
    assert "draft=false" not in helper


# ── Pre-release CI ────────────────────────────────────────────────────


def test_ci_server_covers_every_surface_that_can_change_the_image():
    triggers = CI["on"]
    for event in ("push", "pull_request"):
        paths = triggers[event]["paths"]
        for required in (
            "server/**",
            "Dockerfile.server",
            "self-host/**",
            "scripts/self-host-image-smoke.sh",
            "scripts/self-host-image-smoke-probe.py",
            "scripts/self-host-compose-effective-check.sh",
            "scripts/build-self-host-bundle.py",
            "scripts/verify-self-host-bundle.py",
            "scripts/selfhost_release_contract.py",
            "scripts/verify-server-image-release.sh",
            "scripts/publish-self-host-release-assets.sh",
            "contracts/self-host-server-image.schema.json",
            "tests/test_self_host_*.py",
            ".github/workflows/ci-server.yml",
            ".github/workflows/release-server-image.yml",
        ):
            assert required in paths, f"{event}: {required} is not a CI trigger path"


def test_ci_server_builds_and_smokes_both_native_architectures():
    matrix = CI["jobs"]["self-host-image"]["strategy"]["matrix"]["include"]
    assert [(entry["platform"], entry["runner"]) for entry in matrix] == [
        ("linux/amd64", "ubuntu-24.04"),
        ("linux/arm64", "ubuntu-24.04-arm"),
    ]
    build = step_named(CI["jobs"]["self-host-image"], "Build the server image for ${{ matrix.platform }}")
    assert build["with"]["push"] == "false"
    assert build["with"]["load"] == "true"
    assert "tags" in build["with"] and "ghcr.io" not in build["with"]["tags"]
    smoke = step_named(CI["jobs"]["self-host-image"], "Run the self-host image smoke contract")
    assert SMOKE_SCRIPT in smoke["run"]


def test_ci_server_runs_the_contract_suite_and_shell_syntax_checks():
    job = CI["jobs"]["self-host-contracts"]
    assert "python -m pytest tests/test_self_host_*.py -q" in step_named(
        job, "Run self-host release contract tests"
    )["run"]
    syntax = step_named(job, "Check release and self-host shell syntax")["run"]
    for script in (
        "self-host/install.sh",
        "self-host/update.sh",
        "self-host/upgrade.sh",
        "self-host/backup-restore.sh",
        "scripts/self-host-image-smoke.sh",
        "scripts/self-host-compose-effective-check.sh",
        "scripts/verify-server-image-release.sh",
        "scripts/publish-self-host-release-assets.sh",
    ):
        assert script in syntax


def test_ci_server_checks_the_effective_compose_configuration():
    """Static YAML contracts cannot see what Compose actually resolves to."""

    job = CI["jobs"]["self-host-contracts"]
    step = step_named(job, "Verify the effective self-host Compose configuration")
    assert step["run"].strip() == "scripts/self-host-compose-effective-check.sh"


def test_ci_server_cannot_publish_or_reach_production():
    source = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in source, "pre-release CI must not receive any secret"
    for job_name, job in CI["jobs"].items():
        assert job["permissions"] == {"contents": "read"}, job_name
        assert "environment" not in job, job_name
    assert "docker/login-action" not in source
    assert "push: true" not in source


# ── Both lanes: runner and supply-chain boundaries ────────────────────


@pytest.mark.parametrize("path", [RELEASE_WORKFLOW, CI_WORKFLOW], ids=lambda p: p.name)
def test_self_host_lanes_use_only_hosted_native_runners(path):
    workflow = load(path)
    for name, job in workflow["jobs"].items():
        runner = job["runs-on"]
        if runner.startswith("${{"):
            matrix = job["strategy"]["matrix"]["include"]
            runners = {entry["runner"] for entry in matrix}
        else:
            runners = {runner}
        for value in runners:
            assert "self-hosted" not in value, f"{path.name}:{name} must not use a self-hosted runner"
            assert value in {"ubuntu-latest", *NATIVE_RUNNERS}, f"{path.name}:{name} runner {value}"


@pytest.mark.parametrize("path", [RELEASE_WORKFLOW, CI_WORKFLOW], ids=lambda p: p.name)
def test_self_host_lanes_declare_no_environment_and_no_production_markers(path):
    workflow = load(path)
    for name, job in workflow["jobs"].items():
        assert "environment" not in job, f"{path.name}:{name} must not use a deployment environment"
    source = path.read_text(encoding="utf-8")
    for marker in PRODUCTION_MARKERS:
        assert marker not in source, f"{path.name} must not reference {marker}"


@pytest.mark.parametrize("path", [RELEASE_WORKFLOW, CI_WORKFLOW], ids=lambda p: p.name)
def test_self_host_lanes_introduce_no_job_level_reusable_workflow(path):
    workflow = load(path)
    for name, job in workflow["jobs"].items():
        assert "uses" not in job, f"{path.name}:{name} must not delegate to a reusable workflow"


def test_every_workflow_action_is_pinned_to_an_immutable_commit():
    violations = []
    for path in all_workflows():
        workflow = load(path)
        for job_name, job in workflow.get("jobs", {}).items():
            job_uses = job.get("uses")
            if isinstance(job_uses, str) and not job_uses.startswith("./"):
                violations.append(f"{path.name}:{job_name}: third-party reusable workflow {job_uses}")
            for step in job.get("steps", []):
                uses = step.get("uses")
                if not isinstance(uses, str) or uses.startswith("./"):
                    continue
                if not SHA_PINNED.match(uses):
                    violations.append(f"{path.name}:{job_name}: {uses} is not pinned to a 40-hex commit")
    assert violations == []


# ── Production deploy isolation ───────────────────────────────────────


def test_production_deploy_workflow_is_not_callable_and_not_called():
    assert set(DEPLOY["on"]) == {"workflow_dispatch"}
    assert "workflow_call" not in DEPLOY["on"]
    assert set(DEPLOY["jobs"]) == {"build-and-push", "deploy"}
    for job in DEPLOY["jobs"].values():
        assert job["environment"] == "server-production"

    for path in all_workflows():
        if path.name == "deploy-server.yml":
            continue
        assert "deploy-server.yml" not in path.read_text(encoding="utf-8"), (
            f"{path.name} must not reference the production deploy workflow"
        )


def test_production_deploy_remains_the_only_lane_running_in_its_environment():
    owners = []
    for path in all_workflows():
        workflow = load(path)
        for job_name, job in workflow.get("jobs", {}).items():
            if job.get("environment") == "server-production":
                owners.append(f"{path.name}:{job_name}")
    assert owners == ["deploy-server.yml:build-and-push", "deploy-server.yml:deploy"]
