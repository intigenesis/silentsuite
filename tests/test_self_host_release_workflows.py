"""Semantic contracts for the release control plane and the self-host image CI.

These assertions parse the workflows rather than grepping their source, so a
rename or a re-indent cannot quietly satisfy them. They exist to keep five
boundaries fixed:

  * release authority is defined only by workflow code loaded from the protected
    default branch — a repository_dispatch controller and the local reusable
    workflows it calls — and never by the tag being released;
  * no lane can reach hosted production credentials, environments, or
    self-hosted runners;
  * nothing publishes a mutable reference, and no partial platform alias is
    exposed before both architectures have passed their native smoke;
  * the three component lanes append to one shared draft, serialized, and none
    of them can publish it;
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
CONTROLLER_WORKFLOW = WORKFLOW_DIR / "release-controller.yml"
ANDROID_WORKFLOW = WORKFLOW_DIR / "release-android.yml"
BRIDGE_WORKFLOW = WORKFLOW_DIR / "release-bridge.yml"
BRIDGE_BUILD_WORKFLOW = WORKFLOW_DIR / "build-bridge.yml"
RELEASE_WORKFLOW = WORKFLOW_DIR / "release-server-image.yml"
READINESS_WORKFLOW = WORKFLOW_DIR / "release-readiness.yml"
CI_WORKFLOW = WORKFLOW_DIR / "ci-server.yml"
DEPLOY_WORKFLOW = WORKFLOW_DIR / "deploy-server.yml"

COMPONENT_WORKFLOWS = (ANDROID_WORKFLOW, BRIDGE_WORKFLOW, RELEASE_WORKFLOW, READINESS_WORKFLOW)
CONTROL_PLANE = (CONTROLLER_WORKFLOW, *COMPONENT_WORKFLOWS)

SHA_PINNED = re.compile(r"^[^./][^/]*/[^/@]+(?:/[^@]+)?@[0-9a-f]{40}$")
NATIVE_RUNNERS = {"ubuntu-24.04": "linux/amd64", "ubuntu-24.04-arm": "linux/arm64"}
SMOKE_SCRIPT = "scripts/self-host-image-smoke.sh"
TRUSTED_REF = "${{ github.sha }}"
UMBRELLA_GROUP = "umbrella-release-${{ github.event.client_payload.release_tag }}"

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


def checkouts(job: dict) -> list[dict]:
    return [
        step.get("with", {})
        for step in steps(job)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]


def all_workflows() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


CONTROLLER = load(CONTROLLER_WORKFLOW)
ANDROID = load(ANDROID_WORKFLOW)
BRIDGE = load(BRIDGE_WORKFLOW)
RELEASE = load(RELEASE_WORKFLOW)
READINESS = load(READINESS_WORKFLOW)
CI = load(CI_WORKFLOW)
DEPLOY = load(DEPLOY_WORKFLOW)


# ── Release authority ─────────────────────────────────────────────────


def test_no_workflow_anywhere_is_triggered_by_a_tag_push():
    """The premise of the design: no workflow code is ever loaded from a tag."""

    offenders = []
    for path in all_workflows():
        triggers = load(path).get("on") or {}
        push = triggers.get("push") if isinstance(triggers, dict) else None
        if isinstance(push, dict) and "tags" in push:
            offenders.append(path.name)
    assert offenders == []


def test_the_controller_is_the_only_repository_dispatch_workflow():
    owners = [
        path.name
        for path in all_workflows()
        if "repository_dispatch" in (load(path).get("on") or {})
    ]
    assert owners == [CONTROLLER_WORKFLOW.name]


def test_the_controller_accepts_exactly_one_event_type():
    triggers = CONTROLLER["on"]
    assert set(triggers) == {"repository_dispatch"}
    assert triggers["repository_dispatch"]["types"] == ["silentsuite_release"]
    assert CONTROLLER["permissions"] == {}
    assert CONTROLLER["concurrency"]["cancel-in-progress"] == "false"
    assert "client_payload.release_tag" in CONTROLLER["concurrency"]["group"]


def test_the_controller_admits_before_any_component_lane_runs():
    jobs = CONTROLLER["jobs"]
    assert set(jobs) == {"admit", "android", "bridge", "server", "readiness"}
    for name in ("android", "bridge", "server", "readiness"):
        needs = jobs[name]["needs"]
        assert "admit" in ([needs] if isinstance(needs, str) else needs), name
    assert set(jobs["readiness"]["needs"]) == {"admit", "android", "bridge", "server"}


def test_the_admission_job_only_reads_and_only_from_the_protected_revision():
    admit = CONTROLLER["jobs"]["admit"]
    assert admit["permissions"] == {"contents": "read"}
    assert "environment" not in admit
    assert checkouts(admit) == [
        {"ref": TRUSTED_REF, "fetch-depth": "0", "persist-credentials": "false"}
    ]


def test_the_dispatch_payload_is_validated_before_it_is_used():
    payload = step_named(CONTROLLER["jobs"]["admit"], "Validate the dispatch payload")
    assert payload["env"] == {"PAYLOAD": "${{ toJSON(github.event.client_payload) }}"}
    run = payload["run"]
    assert "keys | join(\",\")" in run
    assert '"release_tag,source_sha"' in run
    assert r"^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$" in run
    assert "^[0-9a-f]{40}$" in run
    # Payload text never reaches a shell by interpolation.
    for job in CONTROLLER["jobs"].values():
        for step in steps(job):
            assert "client_payload" not in str(step.get("run", ""))


def test_admission_proves_identity_ancestry_and_rulesets():
    identity = step_named(
        CONTROLLER["jobs"]["admit"], "Verify the live release identity and tag rulesets"
    )
    run = identity["run"]
    assert "scripts/verify-release-identity.sh" in run
    assert "--git-ancestry ." in run
    assert "--emit-outputs" in run
    assert identity["env"]["RELEASE_TAG"] == "${{ steps.payload.outputs.tag }}"
    assert identity["env"]["SOURCE_SHA"] == "${{ steps.payload.outputs.commit }}"

    verifier = (ROOT / "scripts" / "verify-release-identity.sh").read_text(encoding="utf-8")
    assert "CREATION_RULESET_ID=20051354" in verifier
    assert "IMMUTABILITY_RULESET_ID=20051355" in verifier
    assert "CREATION_BYPASS_ACTOR=265568982" in verifier
    assert "PROTECTED_BRANCH=\"main\"" in verifier
    assert "merge_base_commit" in verifier


def test_the_controller_admits_structurally_before_it_builds():
    names = step_names(CONTROLLER["jobs"]["admit"])
    assert names.index("Validate the dispatch payload") < names.index(
        "Verify the live release identity and tag rulesets"
    )
    assert names[-1] == "Enforce the release control-plane boundary"


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        ("android", {"contents": "write"}),
        ("bridge", {"contents": "write"}),
        (
            "server",
            {
                "contents": "write",
                "packages": "write",
                "id-token": "write",
                "attestations": "write",
            },
        ),
        ("readiness", {"contents": "read"}),
    ],
)
def test_each_component_lane_declares_its_permission_ceiling(job, expected):
    assert CONTROLLER["jobs"][job]["permissions"] == expected


@pytest.mark.parametrize(
    ("job", "target"),
    [
        ("android", "./.github/workflows/release-android.yml"),
        ("bridge", "./.github/workflows/release-bridge.yml"),
        ("server", "./.github/workflows/release-server-image.yml"),
        ("readiness", "./.github/workflows/release-readiness.yml"),
    ],
)
def test_each_component_lane_is_called_locally_with_the_admitted_pair(job, target):
    lane = CONTROLLER["jobs"][job]
    assert lane["uses"] == target
    assert lane["with"] == {
        "release_tag": "${{ needs.admit.outputs.tag }}",
        "source_sha": "${{ needs.admit.outputs.commit }}",
    }
    assert "secrets" not in lane, "environment secrets belong to the called job"


@pytest.mark.parametrize("path", COMPONENT_WORKFLOWS, ids=lambda p: p.name)
def test_component_lanes_are_reachable_only_by_that_call(path):
    workflow = load(path)
    assert set(workflow["on"]) == {"workflow_call"}
    assert workflow["permissions"] == {}
    assert workflow["on"]["workflow_call"]["inputs"] == {
        "release_tag": {
            "description": "The admitted immutable release tag.",
            "required": "true",
            "type": "string",
        },
        "source_sha": {
            "description": "The admitted 40-hex source commit.",
            "required": "true",
            "type": "string",
        },
    }
    assert "secrets" not in workflow["on"]["workflow_call"]


def test_the_bridge_build_is_shared_with_an_unprivileged_dispatch_lane():
    """One definition of how a bridge binary is built, callable two ways."""

    build = load(BRIDGE_BUILD_WORKFLOW)
    assert set(build["on"]) == {"workflow_call", "workflow_dispatch"}
    assert build["permissions"] == {}
    for name, job in build["jobs"].items():
        assert job["permissions"] == {"contents": "read"}, name
        assert "environment" not in job, name
    source = BRIDGE_BUILD_WORKFLOW.read_text(encoding="utf-8")
    for forbidden in ("attach-umbrella-release-assets.sh", "secrets.GITHUB_TOKEN", "gh release"):
        assert forbidden not in source, f"the build lane must not be able to {forbidden}"

    delegate = BRIDGE["jobs"]["build"]
    assert delegate["uses"] == "./.github/workflows/build-bridge.yml"
    assert delegate["permissions"] == {"contents": "read"}
    assert delegate["with"] == {
        "release_tag": "${{ inputs.release_tag }}",
        "source_sha": "${{ inputs.source_sha }}",
    }


def test_the_bridge_build_stamps_the_admitted_tag_into_the_binary():
    build = load(BRIDGE_BUILD_WORKFLOW)["jobs"]["build"]
    assert build["env"]["SILENTSUITE_BRIDGE_VERSION"] == "${{ inputs.release_tag }}"
    assert checkouts(build) == [
        {"ref": "${{ inputs.source_sha }}", "persist-credentials": "false"}
    ]
    platforms = {entry["asset-name"] for entry in build["strategy"]["matrix"]["include"]}
    assert platforms == {
        "silentsuite-bridge-linux-x86_64",
        "silentsuite-bridge-linux-arm64",
        "silentsuite-bridge-macos-x86_64",
        "silentsuite-bridge-macos-arm64",
        "silentsuite-bridge-windows-x86_64.exe",
    }


# ── Server lane: admission-gated build and smoke ──────────────────────


def test_release_jobs_are_exactly_the_build_merge_attach_chain():
    assert set(RELEASE["jobs"]) == {"build", "publish-index", "attach-release-assets"}
    assert RELEASE["jobs"]["publish-index"]["needs"] == "build"
    assert RELEASE["jobs"]["attach-release-assets"]["needs"] == "publish-index"


@pytest.mark.parametrize(
    "job_name,expected",
    [
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


@pytest.mark.parametrize("job_name", ["build", "publish-index"])
def test_the_candidate_and_the_controller_are_checked_out_separately(job_name):
    """Candidate bytes are the build input; trusted bytes verify and mutate."""

    job = RELEASE["jobs"][job_name]
    assert checkouts(job) == [
        {"ref": "${{ inputs.source_sha }}", "path": "candidate", "persist-credentials": "false"},
        {"ref": TRUSTED_REF, "path": "trusted", "persist-credentials": "false"},
    ]


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
    assert inputs["build-args"] == "VCS_REF=${{ inputs.source_sha }}"
    assert inputs["context"] == "candidate"
    assert inputs["file"] == "candidate/Dockerfile.server"
    # Buildx attestations would make the recorded child digest an intermediate
    # index instead of the platform manifest the installer verifies; provenance
    # is attested against the exact digests in publish-index instead.
    assert inputs["provenance"] == "false"
    assert inputs["sbom"] == "false"


def test_release_smoke_runs_the_trusted_script_against_the_pushed_digest():
    smoke = step_named(RELEASE["jobs"]["build"], "Smoke the exact pushed content")
    assert f"trusted/{SMOKE_SCRIPT}" in smoke["run"]
    assert 'docker pull "${IMAGE_NAME}@${CHILD_DIGEST}"' in smoke["run"]
    assert '--expect-revision "$SOURCE_SHA"' in smoke["run"]
    assert smoke["env"]["CHILD_DIGEST"] == "${{ steps.build.outputs.digest }}"
    assert smoke["env"]["SOURCE_SHA"] == "${{ inputs.source_sha }}"


def test_release_child_digest_is_only_exposed_after_its_smoke_passes():
    names = step_names(RELEASE["jobs"]["build"])
    assert names.index("Smoke the exact pushed content") < names.index("Record the smoked child digest")
    assert names.index("Record the smoked child digest") < names.index(
        "Publish the verified child digest to the merge job"
    )


# ── Server lane: index, verification, bundle ──────────────────────────


def test_registry_writes_are_serialized_by_the_admitted_tag():
    """Two runs for one tag must not race the same alias; a commit is not enough."""

    lock = RELEASE["jobs"]["publish-index"]["concurrency"]
    assert lock["group"] == "silentsuite-server-registry-${{ github.event.client_payload.release_tag }}"
    assert lock["cancel-in-progress"] == "false"
    assert lock["queue"] == "max"
    assert "github.sha" not in lock["group"]


def test_the_live_tag_is_revalidated_immediately_before_the_first_alias_write():
    names = step_names(RELEASE["jobs"]["publish-index"])
    revalidate = "Revalidate the live release identity before publishing aliases"
    assert names.index(revalidate) < names.index("Merge verified children into the release index")
    assert names[names.index(revalidate) + 1] == "Merge verified children into the release index"
    run = step_named(RELEASE["jobs"]["publish-index"], revalidate)["run"]
    assert "trusted/scripts/verify-release-identity.sh" in run
    assert "--stage server-registry-publication" in run


def test_release_merges_only_verified_children_into_immutable_references():
    run = step_named(RELEASE["jobs"]["publish-index"], "Merge verified children into the release index")["run"]
    assert '--tag "${IMAGE_NAME}:${reference}"' in run
    assert 'COMMIT_REF="selfhost-${RELEASE_COMMIT}"' in run
    assert 'publish_alias "$COMMIT_REF"' in run
    assert 'existing="$(trusted/scripts/verify-server-image-release.sh' in run
    assert '"$existing" = "absent"' in run
    assert "latest" not in run


def test_release_alias_publication_is_idempotent_but_conflict_safe():
    run = step_named(RELEASE["jobs"]["publish-index"], "Merge verified children into the release index")["run"]
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
    assert "trusted/scripts/verify-server-image-release.sh" in verify
    assert '--amd64-digest "$AMD64_DIGEST"' in verify
    assert '--arm64-digest "$ARM64_DIGEST"' in verify
    verifier = (ROOT / "scripts" / "verify-server-image-release.sh").read_text(encoding="utf-8")
    assert 'actual_config_digest="sha256:$(sha256sum' in verifier
    assert '"$actual_config_digest" != "$config_digest"' in verifier


def test_the_bundle_is_packed_by_trusted_code_from_candidate_content():
    build = step_named(RELEASE["jobs"]["publish-index"], "Build the self-host release bundle")["run"]
    assert "python3 trusted/scripts/build-self-host-bundle.py" in build
    assert "--self-host-dir candidate/self-host" in build
    verify = step_named(RELEASE["jobs"]["publish-index"], "Verify the self-host release bundle")["run"]
    assert "python3 trusted/scripts/verify-self-host-bundle.py" in verify


@pytest.mark.parametrize(
    ("step_name", "subject"),
    [
        ("Attest build provenance for the release index", "${{ steps.verify.outputs.index-digest }}"),
        ("Attest build provenance for the linux/amd64 child", "${{ steps.digests.outputs.amd64 }}"),
        ("Attest build provenance for the linux/arm64 child", "${{ steps.digests.outputs.arm64 }}"),
    ],
)
def test_provenance_is_attested_for_the_index_and_both_children(step_name, subject):
    """Evidence bound to the exact digests, and never added to the index itself."""

    attest = step_named(RELEASE["jobs"]["publish-index"], step_name)
    assert attest["uses"].startswith("actions/attest-build-provenance@")
    assert attest["with"]["push-to-registry"] == "false"
    assert attest["with"]["subject-digest"] == subject


# ── Shared umbrella draft ─────────────────────────────────────────────


ATTACHMENT_JOBS = [
    (RELEASE_WORKFLOW, "attach-release-assets", "Attach the verified assets to the shared draft release"),
    (ANDROID_WORKFLOW, "attach-release-assets", "Attach the Android assets to the shared draft release"),
    (BRIDGE_WORKFLOW, "attach-release-assets", "Attach the bridge binaries to the shared draft release"),
]
ATTACHMENT_IDS = [f"{path.stem}:{job}" for path, job, _ in ATTACHMENT_JOBS]
ATTACH_HELPER = ROOT / "scripts" / "attach-umbrella-release-assets.sh"


@pytest.mark.parametrize(("path", "job_name", "attach_step"), ATTACHMENT_JOBS, ids=ATTACHMENT_IDS)
def test_every_umbrella_attachment_job_shares_one_tag_scoped_domain(path, job_name, attach_step):
    """Three workflows write one draft, so the lock has to live outside them.

    Concurrency groups are repository scoped by name, which is the only thing
    that can serialize jobs in separate workflow files.
    """

    job = load(path)["jobs"][job_name]
    assert job["concurrency"] == {
        "group": UMBRELLA_GROUP,
        "cancel-in-progress": "false",
        "queue": "max",
    }


def test_the_umbrella_domain_is_scoped_to_the_tag_not_the_commit_or_workflow():
    for path, job_name, _ in ATTACHMENT_JOBS:
        group = load(path)["jobs"][job_name]["concurrency"]["group"]
        assert "client_payload.release_tag" in group
        for wrong in ("github.sha", "github.workflow", "github.run_id", "github.ref_name"):
            assert wrong not in group, f"{path.name}:{job_name} is scoped by {wrong}"


@pytest.mark.parametrize(("path", "job_name", "attach_step"), ATTACHMENT_JOBS, ids=ATTACHMENT_IDS)
def test_every_attachment_runs_trusted_code_and_binds_the_admitted_commit(path, job_name, attach_step):
    job = load(path)["jobs"][job_name]
    assert checkouts(job) == [
        {"ref": TRUSTED_REF, "clean": "true", "persist-credentials": "false"}
    ]
    step = step_named(job, attach_step)
    assert "uses" not in step, f"{path.name}:{job_name} must not attach with an action"
    assert ATTACH_HELPER.name in step["run"]
    assert "--expected-commit" in step["run"]
    assert step["env"]["GITHUB_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"


@pytest.mark.parametrize(("path", "job_name", "attach_step"), ATTACHMENT_JOBS, ids=ATTACHMENT_IDS)
def test_every_attachment_writes_a_draft_and_never_publishes_it(path, job_name, attach_step):
    """All three components only ever append to a draft. Publication is manual."""

    source = path.read_text(encoding="utf-8")
    for forbidden in (
        "softprops/action-gh-release",
        "draft: false",
        "gh release edit",
        "gh release create",
        "gh release upload",
        "make_latest",
        "overwrite_files",
    ):
        assert forbidden not in source, f"{path.name} can publish or overwrite a release asset"


@pytest.mark.parametrize(("path", "job_name", "attach_step"), ATTACHMENT_JOBS, ids=ATTACHMENT_IDS)
def test_queue_max_is_never_paired_with_cancellation(path, job_name, attach_step):
    """GitHub rejects that combination; asserting it keeps the pair coherent."""

    workflow = load(path)
    for declaration in (workflow.get("concurrency"), workflow["jobs"][job_name].get("concurrency")):
        if declaration and declaration.get("queue") == "max":
            assert declaration.get("cancel-in-progress") == "false"


def test_no_enclosing_concurrency_rule_can_cancel_an_attachment():
    """A workflow-level cancellation outranks the job-level umbrella lock."""

    for path in CONTROL_PLANE:
        outer = load(path).get("concurrency")
        if outer is None:
            continue
        assert outer.get("cancel-in-progress") == "false", (
            f"{path.name}: a workflow-level cancellation group can cancel its own "
            "umbrella attachment job"
        )


def test_shared_draft_helper_is_race_safe_and_fails_closed():
    helper = ATTACH_HELPER.read_text(encoding="utf-8")
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


def test_the_helper_binds_the_draft_to_the_admitted_commit_and_pages_to_exhaustion():
    helper = ATTACH_HELPER.read_text(encoding="utf-8")
    assert "target_commitish: $target" in helper
    assert "assert_release_identity" in helper
    assert "collect_pages" in helper
    assert "did not terminate within" in helper
    assert 'revalidate "pre"' in helper
    assert 'revalidate "post"' in helper


def test_the_helper_never_deletes_or_replaces_an_existing_asset():
    helper = ATTACH_HELPER.read_text(encoding="utf-8")
    assert "already present with identical bytes" in helper
    for destructive in ("-X DELETE", "-X PATCH", "-X PUT", 'releases/assets/${asset_id}" -X'):
        assert destructive not in helper, f"the helper must never {destructive}"
    verbs = set(re.findall(r"-X ([A-Z]+)", helper))
    assert verbs <= {"GET", "POST"}, verbs


def test_no_workflow_anywhere_writes_a_release_outside_the_hardened_helper():
    offenders = []
    for path in all_workflows():
        source = path.read_text(encoding="utf-8")
        for marker in (
            "softprops/action-gh-release",
            "gh release create",
            "gh release upload",
            "gh release edit",
            "gh release delete",
        ):
            if marker in source:
                offenders.append(f"{path.name}: {marker}")
    assert offenders == []


def test_the_publish_helper_only_addresses_release_objects_and_assets():
    source = ATTACH_HELPER.read_text(encoding="utf-8")
    paths = set(re.findall(r'"/repos/\$\{GITHUB_REPOSITORY\}(/[^"$]*)', source))
    assert paths, "the helper should address the releases API"
    for path in paths:
        assert path.startswith("/releases"), f"the helper reaches outside releases: {path}"


# ── Readiness gate ────────────────────────────────────────────────────


def test_the_readiness_gate_is_read_only_and_cannot_publish():
    job = READINESS["jobs"]["readiness"]
    assert job["permissions"] == {"contents": "read"}
    assert "environment" not in job
    assert checkouts(job) == [{"ref": TRUSTED_REF, "clean": "true", "persist-credentials": "false"}]
    run = step_named(job, "Prove the umbrella draft is complete")["run"]
    assert "scripts/verify-umbrella-release-readiness.py" in run
    source = READINESS_WORKFLOW.read_text(encoding="utf-8")
    for forbidden in ("draft: false", "gh release", "make_latest", "contents: write"):
        assert forbidden not in source


def test_the_readiness_gate_covers_every_component_inventory():
    contract = (ROOT / "scripts" / "umbrella_release_contract.py").read_text(encoding="utf-8")
    for component in ("android", "bridge", "self-host"):
        assert f'"{component}"' in contract
    gate = (ROOT / "scripts" / "verify-umbrella-release-readiness.py").read_text(encoding="utf-8")
    assert "expected_assets" in gate
    assert "checksum_pairs" in gate
    assert "assert_bundle_inventory" in gate
    assert "MAX_PAGES" in gate


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
            "scripts/attach-umbrella-release-assets.sh",
            "scripts/verify-release-identity.sh",
            "scripts/verify-umbrella-release-readiness.py",
            "scripts/umbrella_release_contract.py",
            "scripts/check-server-image-dependencies.py",
            "scripts/lock-server-requirements.py",
            "contracts/self-host-server-image.schema.json",
            "docs/self-hosting/**",
            "apps/docs/self-hosting/**",
            "tests/test_self_host_*.py",
            "tests/test_umbrella_release_*.py",
            "tests/test_server_image_verifier.py",
            ".github/workflows/ci-server.yml",
            ".github/workflows/release-controller.yml",
            ".github/workflows/release-android.yml",
            ".github/workflows/release-bridge.yml",
            ".github/workflows/release-server-image.yml",
            ".github/workflows/release-readiness.yml",
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


def test_ci_proves_the_hash_locked_dependencies_import_on_both_architectures():
    names = step_names(CI["jobs"]["self-host-image"])
    proof = "Prove the hash-locked dependency set imports natively"
    assert names.index(proof) < names.index("Run the self-host image smoke contract")
    run = step_named(CI["jobs"]["self-host-image"], proof)["run"]
    assert "check-server-image-dependencies.py" in run
    assert "server/requirements.txt:/requirements.txt:ro" in run


def test_ci_server_runs_the_contract_suite_and_shell_syntax_checks():
    job = CI["jobs"]["self-host-contracts"]
    contract_tests = step_named(job, "Run self-host release contract tests")["run"]
    for target in (
        "tests/test_self_host_*.py",
        "tests/test_umbrella_release_*.py",
        "tests/test_server_image_verifier.py",
    ):
        assert target in contract_tests, f"{target} is not run by the contract job"
    syntax = step_named(job, "Check release and self-host shell syntax")["run"]
    for script in (
        "self-host/install.sh",
        "self-host/update.sh",
        "scripts/self-host-image-smoke.sh",
        "scripts/self-host-compose-effective-check.sh",
        "scripts/verify-server-image-release.sh",
        "scripts/attach-umbrella-release-assets.sh",
        "scripts/verify-release-identity.sh",
    ):
        assert script in syntax
    boundary = step_named(job, "Enforce the release control-plane boundary")["run"]
    assert "check-android-signing-boundary.py" in boundary
    # The materials contract is deselected from the glob above and run once
    # under the stricter env var instead; assert both halves so the exclusion
    # can never silently drop its coverage.
    assert "--ignore=tests/test_self_host_server_image_materials.py" in contract_tests
    registry = step_named(job, "Verify the pinned base image and the hash lock against live indexes")
    assert registry["env"]["SILENTSUITE_REQUIRE_REGISTRY_CONTRACT"] == "1"
    assert "tests/test_self_host_server_image_materials.py" in registry["run"]


def test_ci_server_checks_the_effective_compose_configuration():
    """Static YAML contracts cannot see what Compose actually resolves to."""

    step = step_named(CI["jobs"]["self-host-contracts"], "Verify the effective self-host Compose configuration")
    assert step["run"].strip() == "scripts/self-host-compose-effective-check.sh"


def test_ci_server_cannot_publish_or_reach_production():
    source = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in source, "pre-release CI must not receive any secret"
    for job_name, job in CI["jobs"].items():
        assert job["permissions"] == {"contents": "read"}, job_name
        assert "environment" not in job, job_name
    assert "docker/login-action" not in source
    assert "push: true" not in source


# ── Runner and supply-chain boundaries ────────────────────────────────


@pytest.mark.parametrize("path", [*CONTROL_PLANE, BRIDGE_BUILD_WORKFLOW, CI_WORKFLOW], ids=lambda p: p.name)
def test_release_lanes_use_only_hosted_runners(path):
    workflow = load(path)
    for name, job in workflow["jobs"].items():
        runner = job.get("runs-on")
        if runner is None:
            assert "uses" in job, f"{path.name}:{name} has neither runs-on nor uses"
            continue
        if runner.startswith("${{"):
            runners = {entry["runner"] for entry in job["strategy"]["matrix"]["include"]}
        else:
            runners = {runner}
        for value in runners:
            assert "self-hosted" not in value, f"{path.name}:{name} must not use a self-hosted runner"


@pytest.mark.parametrize("path", [*CONTROL_PLANE, BRIDGE_BUILD_WORKFLOW, CI_WORKFLOW], ids=lambda p: p.name)
def test_release_lanes_declare_no_production_environment_or_marker(path):
    workflow = load(path)
    for name, job in workflow["jobs"].items():
        assert job.get("environment") != "server-production", f"{path.name}:{name}"
    source = path.read_text(encoding="utf-8")
    for marker in PRODUCTION_MARKERS:
        assert marker not in source, f"{path.name} must not reference {marker}"


def test_only_the_signed_android_job_binds_a_deployment_environment():
    owners = []
    for path in (*CONTROL_PLANE, BRIDGE_BUILD_WORKFLOW, CI_WORKFLOW):
        for job_name, job in load(path).get("jobs", {}).items():
            if job.get("environment") is not None:
                owners.append(f"{path.name}:{job_name}")
    assert owners == ["release-android.yml:build-release"]


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


ALLOWED_RELEASE_SECRETS = {
    "GITHUB_TOKEN",
    "ANDROID_KEYSTORE_BASE64",
    "ANDROID_KEYSTORE_PASSWORD",
    "ANDROID_KEY_ALIAS",
}
SECRET_REFERENCE = re.compile(r"secrets\.\s*([A-Za-z_][A-Za-z0-9_-]*)")


@pytest.mark.parametrize("path", [*CONTROL_PLANE, BRIDGE_BUILD_WORKFLOW], ids=lambda p: p.name)
def test_no_release_lane_holds_a_repository_settings_credential(path):
    """Structural rather than name-based: any *new* secret is rejected.

    That covers the deferred Administration:read token and anything introduced
    in its place — the ruleset checks are deliberately unprivileged reads.
    """

    named = set(SECRET_REFERENCE.findall(path.read_text(encoding="utf-8")))
    assert named <= ALLOWED_RELEASE_SECRETS, (
        f"{path.name} names unreviewed credentials: "
        f"{', '.join(sorted(named - ALLOWED_RELEASE_SECRETS))}"
    )


def test_no_lane_calls_a_repository_settings_endpoint_that_can_change_it():
    """The lanes read rulesets and releases; they never write a setting."""

    sources = [ATTACH_HELPER.read_text(encoding="utf-8")]
    sources += [path.read_text(encoding="utf-8") for path in (*CONTROL_PLANE, CI_WORKFLOW)]
    for source in sources:
        for endpoint in ("/immutable-releases", "/actions/permissions"):
            assert endpoint not in source, f"a repository-settings surface reappeared: {endpoint}"

    verifier = (ROOT / "scripts" / "verify-release-identity.sh").read_text(encoding="utf-8")
    assert "/rulesets" in verifier, "ruleset verification is the point of the verifier"
    assert verifier.count("-X ") == 0, "the verifier issues no mutating request"


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
        for job_name, job in load(path).get("jobs", {}).items():
            if job.get("environment") == "server-production":
                owners.append(f"{path.name}:{job_name}")
    assert owners == ["deploy-server.yml:build-and-push", "deploy-server.yml:deploy"]


def test_directly_invoked_release_helpers_are_executable():
    """The controller runs these by path, not through an interpreter.

    A lost mode bit would not change any reviewed digest, so it would survive
    every other check here and fail only at release time.
    """

    import subprocess

    listing = subprocess.run(
        ["git", "ls-files", "-s", "scripts/"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    modes = {}
    for line in listing.splitlines():
        meta, _, path = line.partition("\t")
        modes[path] = meta.split()[0]

    for helper in (
        "scripts/verify-release-identity.sh",
        "scripts/attach-umbrella-release-assets.sh",
        "scripts/verify-server-image-release.sh",
        "scripts/self-host-image-smoke.sh",
        "scripts/stage-bridge-release-assets.sh",
    ):
        assert modes.get(helper) == "100755", f"{helper} is not tracked as executable"
