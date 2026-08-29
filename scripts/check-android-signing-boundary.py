#!/usr/bin/env python3
"""Fail closed when the release control plane or Android signing escapes it.

Two boundaries, one checker, because they are the same boundary seen from two
sides:

  * release authority is defined only by workflow code loaded from the protected
    default branch — a repository_dispatch controller and the local reusable
    workflows it calls — and never by the tag being released;
  * Android signing material lives in exactly one job of that control plane,
    which holds no repository write, no release API and no workflow token.

Everything here is structural. It parses the workflows rather than grepping
them, reviews the load-bearing jobs against exact literals, and pins the whole
of each one with a semantic digest so that any edit which is not re-reviewed
fails the check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver
from yaml.tokens import AliasToken, AnchorToken, TagToken


SIGNING_SECRETS = {
    "ANDROID_KEYSTORE_BASE64",
    "ANDROID_KEYSTORE_PASSWORD",
    "ANDROID_KEY_ALIAS",
}
WORKFLOW_DIR = Path(".github/workflows")
# The signing lane. Reachable only through workflow_call from the controller.
ROOT_WORKFLOW = WORKFLOW_DIR / "release-android.yml"
# Unprivileged Android CI. It keeps the same structural policy job and Conscrypt
# producer, and must never regain a release-producing job.
ANDROID_CI_WORKFLOW = WORKFLOW_DIR / "build-android.yml"
ANDROID_SIBLING_WORKFLOW = Path("android/.github/workflows/build.yml")
CONSCRYPT_BUILD_SCRIPT = Path("android/scripts/build-conscrypt-android-r28.sh")

CONTROLLER_WORKFLOW = WORKFLOW_DIR / "release-controller.yml"
BRIDGE_WORKFLOW = WORKFLOW_DIR / "release-bridge.yml"
SERVER_WORKFLOW = WORKFLOW_DIR / "release-server-image.yml"
READINESS_WORKFLOW = WORKFLOW_DIR / "release-readiness.yml"
COMPONENT_WORKFLOWS = (ROOT_WORKFLOW, BRIDGE_WORKFLOW, SERVER_WORKFLOW, READINESS_WORKFLOW)
CONTROL_PLANE = (CONTROLLER_WORKFLOW, *COMPONENT_WORKFLOWS)
# The hosted-production lane. It predates and is independent of the release
# control plane, is dispatch-only, and binds its own protected environment on
# every job. Named here so the "no other privileged manual lane" rule below has
# exactly one reviewed exemption rather than a silent hole.
PRODUCTION_WORKFLOW = WORKFLOW_DIR / "deploy-server.yml"
PRODUCTION_ENVIRONMENT = "server-production"

DISPATCH_EVENT_TYPE = "silentsuite_release"
ALLOWED_JOB = "build-release"
POLICY_JOB = "signing-policy"
REVALIDATION_JOB = "revalidate-signing"
CONSCRYPT_JOB = "conscrypt-r28"
ATTACHMENT_JOB = "attach-release-assets"
ADMISSION_JOB = "admit"
ENVIRONMENT_NAME = "android-release"

IDENTITY_HELPER = Path("scripts/verify-release-identity.sh")
ATTACHMENT_HELPER = Path("scripts/attach-umbrella-release-assets.sh")
READINESS_HELPER = Path("scripts/verify-umbrella-release-readiness.py")
RELEASE_ASSET_ARTIFACT = "silentsuite-android-release-assets-${{ inputs.source_sha }}"

# Every repository script whose bytes decide whether a release is admitted,
# verified, published or attached. Inside the control plane each one may be
# executed only through a checkout of the protected controller revision.
TRUSTED_HELPERS = (
    "verify-release-identity.sh",
    "attach-umbrella-release-assets.sh",
    "verify-umbrella-release-readiness.py",
    "check-android-signing-boundary.py",
    "verify-server-image-release.sh",
    "stage-bridge-release-assets.sh",
    "build-self-host-bundle.py",
    "verify-self-host-bundle.py",
    "self-host-image-smoke.sh",
)
TRUSTED_REF = "${{ github.sha }}"

# Anything that can reach the release API or a repository write. None of these
# may appear in a job that also holds signing material.
RELEASE_WRITE_MARKERS = (
    "attach-umbrella-release-assets.sh",
    "softprops/action-gh-release",
    "gh release",
    "api.github.com",
    "uploads.github.com",
    "${{ secrets.GITHUB_TOKEN }}",
)
SHA_PIN = re.compile(r"^[0-9a-f]{40}$")
UNSAFE_SECRET_EXPRESSION = re.compile(
    r"\bsecrets\s*\[|\bsecrets\s*\.\s*\*|\btojson\s*\(\s*secrets\s*\)",
    re.IGNORECASE,
)
CHECKOUT_ACTION = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON_ACTION = "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
PIP_INSTALL_POLICY_DEPENDENCY = (
    "printf '%s\\n' 'PyYAML==6.0.3 "
    "--hash=sha256:ba1cc08a7ccde2d2ec775841541641e4548226580ab850948cbfda66a1befcdc' "
    '> "$RUNNER_TEMP/android-signing-policy-requirements.txt"\n'
    "python -m pip install --disable-pip-version-check --only-binary=:all: "
    '--require-hashes -r "$RUNNER_TEMP/android-signing-policy-requirements.txt"\n'
)
# The release lane's copy pins the policy source to the protected controller
# revision; the CI copy has no admitted commit to distinguish itself from.
EXPECTED_POLICY_JOB: dict[str, Any] = {
    "name": "Enforce Android signing boundary",
    "runs-on": "ubuntu-latest",
    "permissions": {"contents": "read"},
    "steps": [
        {
            "name": "Set up Python",
            "uses": SETUP_PYTHON_ACTION,
            "with": {"python-version": "3.12"},
        },
        {
            "name": "Install signing policy dependency",
            "run": PIP_INSTALL_POLICY_DEPENDENCY,
        },
        {
            "name": "Checkout policy source",
            "uses": CHECKOUT_ACTION,
            "with": {
                "ref": TRUSTED_REF,
                "clean": "true",
                "persist-credentials": "false",
            },
        },
        {
            "name": "Enforce Android signing boundary",
            "run": 'python "$GITHUB_WORKSPACE/scripts/check-android-signing-boundary.py"',
        },
    ],
}
EXPECTED_CI_POLICY_JOB: dict[str, Any] = {
    "name": "Enforce Android signing boundary",
    "runs-on": "ubuntu-latest",
    "permissions": {"contents": "read"},
    "steps": [
        {
            "name": "Set up Python",
            "uses": SETUP_PYTHON_ACTION,
            "with": {"python-version": "3.12"},
        },
        {
            "name": "Install signing policy dependency",
            "run": PIP_INSTALL_POLICY_DEPENDENCY,
        },
        {
            "name": "Checkout policy source",
            "uses": CHECKOUT_ACTION,
            "with": {"clean": "true", "persist-credentials": "false"},
        },
        {
            "name": "Enforce Android signing boundary",
            "run": 'python "$GITHUB_WORKSPACE/scripts/check-android-signing-boundary.py"',
        },
    ],
}
EXPECTED_RELEASE_STEP_ENVIRONMENTS: dict[str, dict[str, str]] = {
    "Decode release keystore": {
        "KEYSTORE_BASE64": "${{ secrets.ANDROID_KEYSTORE_BASE64 }}",
    },
    "Build signed release APK and AAB": {
        "KSTOREPWD": "${{ secrets.ANDROID_KEYSTORE_PASSWORD }}",
        "KEY_ALIAS": "${{ secrets.ANDROID_KEY_ALIAS }}",
    },
    "Capture release dependency graph and generate signed-release splits": {
        "BUNDLETOOL_VERSION": "1.18.1",
        "BUNDLETOOL_SHA256": "675786493983787ffa11550bdb7c0715679a44e1643f3ff980a529e9c822595c",
        "KSTOREPWD": "${{ secrets.ANDROID_KEYSTORE_PASSWORD }}",
        "KEY_ALIAS": "${{ secrets.ANDROID_KEY_ALIAS }}",
    },
}
# Steps in the signed job that carry a non-secret environment. Naming them keeps
# the "a step environment is reviewed or absent" rule intact now that the
# admitted tag reaches the job as an input rather than as github.ref_name.
EXPECTED_RELEASE_PLAIN_STEP_ENVIRONMENTS: dict[str, dict[str, str]] = {
    "Rename Android artifacts for release": {"RELEASE_TAG": "${{ inputs.release_tag }}"},
    "Stage the closed release-asset set": {"RELEASE_TAG": "${{ inputs.release_tag }}"},
}
# The umbrella-draft attachment lock, shared by all three component lanes.
# Reviewed as an exact literal so a release cannot be quietly re-scoped: a
# different group would stop serializing against the sibling component lanes,
# cancel-in-progress would drop an attachment mid-upload, and anything other
# than queue: max lets the scheduler discard a pending attachment without
# failing anything.
UMBRELLA_GROUP = "umbrella-release-${{ github.event.client_payload.release_tag }}"
EXPECTED_RELEASE_CONCURRENCY: dict[str, Any] = {
    "group": UMBRELLA_GROUP,
    "cancel-in-progress": "false",
    "queue": "max",
}

# Every `secrets.NAME` the signed release job is allowed to name. The reviewed
# step environments below are the only place any of them may appear, so a new
# credential — a repository-settings reader, a release token, anything — cannot
# be introduced into the job that holds the decoded keystore.
ALLOWED_RELEASE_SECRETS = set(SIGNING_SECRETS)
SECRET_REFERENCE = re.compile(r"secrets\.\s*([A-Za-z_][A-Za-z0-9_-]*)")

# The signed job checks out the admitted commit and keeps no credential from it.
# Reviewed as an exact literal: dropping `persist-credentials: false` would hand
# a repository write token to Gradle and every build script it runs, beside the
# decoded keystore.
EXPECTED_RELEASE_CHECKOUT: dict[str, Any] = {
    "name": "Checkout",
    "uses": CHECKOUT_ACTION,
    "with": {"ref": "${{ inputs.source_sha }}", "persist-credentials": "false"},
}

# The read-only job that revalidates the live tag, its commit and both tag
# rulesets in the last moment before signing material exists on any runner. It
# is a separate job precisely so the signed job can stay free of the workflow
# token this check needs, and so the check never shares a runner with candidate
# code.
EXPECTED_REVALIDATION_JOB: dict[str, Any] = {
    "name": "Revalidate the release identity before signing",
    "needs": CONSCRYPT_JOB,
    "runs-on": "ubuntu-latest",
    "timeout-minutes": "10",
    "permissions": {"contents": "read"},
    "steps": [
        {
            "name": "Check out the trusted controller revision",
            "uses": CHECKOUT_ACTION,
            "with": {
                "ref": TRUSTED_REF,
                "clean": "true",
                "persist-credentials": "false",
            },
        },
        {
            "name": "Verify the live release identity and tag rulesets",
            "env": {
                "GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
                "RELEASE_TAG": "${{ inputs.release_tag }}",
                "SOURCE_SHA": "${{ inputs.source_sha }}",
            },
            "run": (
                "set -euo pipefail\n"
                'bash "$GITHUB_WORKSPACE/scripts/verify-release-identity.sh" \\\n'
                '  --tag "$RELEASE_TAG" \\\n'
                '  --commit "$SOURCE_SHA" \\\n'
                "  --stage android-signing\n"
            ),
        },
    ],
}

# The controller's admission job: the only place a dispatch payload becomes an
# admitted (tag, commit) pair, and the only privileged job that runs before any
# candidate code exists on a runner.
EXPECTED_CONTROLLER_ADMIT_PERMISSIONS = {"contents": "read"}
EXPECTED_CONTROLLER_CALLERS: dict[str, dict[str, Any]] = {
    "android": {
        "uses": "./.github/workflows/release-android.yml",
        "permissions": {"contents": "write"},
    },
    "bridge": {
        "uses": "./.github/workflows/release-bridge.yml",
        "permissions": {"contents": "write"},
    },
    "server": {
        "uses": "./.github/workflows/release-server-image.yml",
        "permissions": {
            "contents": "write",
            "packages": "write",
            "id-token": "write",
            "attestations": "write",
        },
    },
    "readiness": {
        "uses": "./.github/workflows/release-readiness.yml",
        "permissions": {"contents": "read"},
    },
}
EXPECTED_CALLER_INPUTS = {
    "release_tag": "${{ needs.admit.outputs.tag }}",
    "source_sha": "${{ needs.admit.outputs.commit }}",
}
EXPECTED_COMPONENT_INPUTS = {
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

EXPECTED_SECRET_STEP_SHA256 = {
    "Decode release keystore": "44c1231395b5f7347980a05fa641b0e7d866451e10ddb88958e61459f649ffba",
    "Build signed release APK and AAB": "e9e02db295ff745dfe2ead024747b996b246ddb2fc2cc0f195ed2244b1a86776",
    "Capture release dependency graph and generate signed-release splits": (
        "69ded7eab4c4ff48deff2da950aacf8e627da05373ffa507f358fbcc986a7a6a"
    ),
}
# Signing steps and the two reviewed plain-environment steps are the only steps
# in the release job permitted to carry an environment at all.
REVIEWED_RELEASE_STEP_ENVIRONMENTS: dict[str, dict[str, str]] = {
    **EXPECTED_RELEASE_STEP_ENVIRONMENTS,
    **EXPECTED_RELEASE_PLAIN_STEP_ENVIRONMENTS,
}
# Covers the whole reviewed signing job: the explicit literal checks state the
# intent, this digest makes any other edit to the job fail closed as well.
EXPECTED_RELEASE_JOB_SHA256 = "e9a9c7b42e7a698d36993d0697b3959dfe44d8112baaa43b982948d5ef507f12"
# Same treatment for the one job that can write a release. It carries the write
# credential, the attachment helper and the umbrella lock, so every byte of it
# is reviewed.
EXPECTED_ATTACHMENT_JOB_SHA256 = "1d58283e8697f63a21627dc6ea037e5d2d0e1de50d5c72d6c9eb8781599a2cca"
EXPECTED_CONTROLLER_ADMIT_SHA256 = "6c1f2a4f0f5368063dbec32c757fe3874f3157905ff7bc26f3c5826eb4930856"
# The helpers those jobs execute. Hashing the step is not enough: the step text
# is stable while the file it runs is what reaches the network and the API.
EXPECTED_IDENTITY_HELPER_SHA256 = "72f998d7ab3d524cc979e2e9b737cd3dcec1bf1a7aeb3731ef6f49bfddd0cd5c"
EXPECTED_ATTACHMENT_HELPER_SHA256 = "7a7b346ef8e40b56946b573765ba2797e1b9658de6ea34697b084c8e24581fce"
EXPECTED_READINESS_HELPER_SHA256 = "c75ebfba772c4f7bd6559161f64df3127c9c390bf1e5a81e39236ba47cc6e26f"
# The Conscrypt producer exists twice: unprivileged CI builds it from the
# triggering ref, the release lane builds it from the admitted commit. Both are
# pinned so neither can drift into an unreviewed native toolchain.
EXPECTED_CI_CONSCRYPT_JOB_SHA256 = "ed27963320252615ff159bbc388c858fdc872516fc0ea2aa5f50d905f4a5063b"
EXPECTED_RELEASE_CONSCRYPT_JOB_SHA256 = "e7d36401f4d350a09355af11917f22f1e66b7a466c87723b77aad77f978a455c"
EXPECTED_CONSCRYPT_BUILD_SCRIPT_SHA256 = (
    "0ee234f2ced343c4167bd1efad134a77853f288eb9c5210c1e1173594a014b8b"
)
ALLOWED_RELEASE_JOB_KEYS = {
    "name",
    "needs",
    "if",
    "runs-on",
    "environment",
    "permissions",
    "defaults",
    "steps",
}
ALLOWED_ATTACHMENT_JOB_KEYS = {
    "name",
    "needs",
    "if",
    "runs-on",
    "permissions",
    "concurrency",
    "env",
    "steps",
}
ALLOWED_RELEASE_STEP_KEYS = {"name", "uses", "with", "run", "env", "if"}
REQUIRED_TRIGGER_PATHS = {
    ".github/workflows/**",
    "android/.github/workflows/**",
    "runbooks/android-release.md",
    "scripts/check-android-signing-boundary.py",
    "tests/test_android_*.py",
}


class StrictBaseLoader(yaml.BaseLoader):
    """BaseLoader with parser-differential features rejected."""


def construct_unique_mapping(
    loader: StrictBaseLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


StrictBaseLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping)


def load_workflow(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        if any(isinstance(token, (AnchorToken, AliasToken, TagToken)) for token in yaml.scan(text)):
            raise ValueError("YAML anchors, aliases, and explicit tags are not allowed")
        parsed = yaml.load(text, Loader=StrictBaseLoader)
    except ValueError:
        raise
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("workflow root must be a mapping")
    return parsed


def walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from walk(key)
            yield from walk(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from walk(item)


def strings(value: Any) -> Iterator[str]:
    for item in walk(value):
        if isinstance(item, str):
            yield item


def signing_references(value: Any) -> set[str]:
    body = "\n".join(strings(value)).casefold()
    return {name for name in SIGNING_SECRETS if name.casefold() in body}


def contains_unsafe_secret_expression(value: Any) -> bool:
    return any(UNSAFE_SECRET_EXPRESSION.search(item) for item in strings(value))


def contains_secret_inheritance(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "secrets" and item == "inherit":
                return True
            if contains_secret_inheritance(item):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(contains_secret_inheritance(item) for item in value)
    return False


def environment_name(job: Mapping[str, Any]) -> str | None:
    value = job.get("environment")
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        name = value.get("name")
        return name if isinstance(name, str) else None
    return None


def permissions_read_only(value: Any) -> bool:
    if value == "read-all":
        return True
    return isinstance(value, Mapping) and all(level in {"read", "none"} for level in value.values())


def semantic_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def action_uses(value: Any) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "uses" and isinstance(item, str):
                yield item
            yield from action_uses(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from action_uses(item)


def unpinned_action(target: str) -> bool:
    if target.startswith("./"):
        return False
    if "@" not in target:
        return True
    return not SHA_PIN.fullmatch(target.rsplit("@", 1)[1])


def as_mapping(value: Any, label: str, violations: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        violations.append(f"{label} must be a mapping")
        return {}
    return value


def triggers(workflow: Mapping[str, Any]) -> dict[str, Any]:
    events = workflow.get("on")
    return events if isinstance(events, Mapping) else {}


def trigger_paths(workflow: Mapping[str, Any], event: str) -> list[str]:
    config = triggers(workflow).get(event)
    if not isinstance(config, Mapping):
        return []
    paths = config.get("paths")
    if not isinstance(paths, Sequence) or isinstance(paths, (str, bytes)):
        return []
    return [item for item in paths if isinstance(item, str)]


def job_steps(job: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, Mapping)]


def checkout_refs(job: Mapping[str, Any]) -> list[tuple[str | None, str]]:
    """Every checkout in a job as (ref, path). `None` ref means the default."""

    found: list[tuple[str | None, str]] = []
    for step in job_steps(job):
        uses = step.get("uses")
        if not isinstance(uses, str) or not uses.startswith("actions/checkout@"):
            continue
        options = step.get("with") if isinstance(step.get("with"), Mapping) else {}
        ref = options.get("ref")
        path = options.get("path")
        found.append((ref if isinstance(ref, str) else None, path if isinstance(path, str) else "."))
    return found


def trusted_helper_references(job: Mapping[str, Any]) -> list[str]:
    """Every token in a job's run scripts that invokes a trusted helper."""

    pattern = re.compile(r"[^\s'\"]*scripts/(?:" + "|".join(re.escape(h) for h in TRUSTED_HELPERS) + r")")
    found: list[str] = []
    for step in job_steps(job):
        run = step.get("run")
        if not isinstance(run, str):
            continue
        for token in pattern.findall(run):
            # A command substitution keeps its `$(` in the matched token; the
            # path being invoked is what matters, so strip it before checking.
            found.append(token[2:] if token.startswith("$(") else token)
    return found


def check_trusted_helper_sources(
    relative: Path, job_name: str, job: Mapping[str, Any], violations: list[str]
) -> None:
    """No control-plane job may run a helper out of the candidate checkout."""

    references = trusted_helper_references(job)
    if not references:
        return
    checkouts = checkout_refs(job)
    trusted_paths = {path for ref, path in checkouts if ref == TRUSTED_REF}
    candidate_paths = {path for ref, path in checkouts if ref is not None and ref != TRUSTED_REF}
    if not trusted_paths:
        violations.append(
            f"{relative} job {job_name} runs {sorted(set(references))} without checking out the "
            f"protected controller revision (ref: {TRUSTED_REF})"
        )
        return
    if candidate_paths & trusted_paths:
        violations.append(
            f"{relative} job {job_name} checks the candidate and the controller out at the same "
            "path, so a helper's source is ambiguous"
        )
        return

    allowed: set[str] = set()
    for helper in TRUSTED_HELPERS:
        for path in trusted_paths:
            if path in {".", ""}:
                prefixes = ("", "./", "$GITHUB_WORKSPACE/", "${GITHUB_WORKSPACE}/")
            else:
                prefixes = (f"{path}/", f"./{path}/", f"$GITHUB_WORKSPACE/{path}/")
            allowed |= {f"{prefix}scripts/{helper}" for prefix in prefixes}

    for reference in references:
        if reference not in allowed:
            violations.append(
                f"{relative} job {job_name} runs '{reference}', which is not inside a checkout of "
                f"the protected controller revision ({sorted(trusted_paths)})"
            )


def check_control_plane(
    loaded: Mapping[Path, dict[str, Any]], violations: list[str]
) -> None:
    """The release authority: where it is loaded from, and what it may reach."""

    # 1. Nothing anywhere is triggered by a tag push. This is what makes "no
    #    tag-sourced workflow code can obtain a privilege" a structural fact
    #    rather than a per-job argument.
    for relative, workflow in loaded.items():
        push = triggers(workflow).get("push")
        if isinstance(push, Mapping) and "tags" in push:
            violations.append(
                f"{relative} declares a tag-push trigger; release authority must come from the "
                "protected default branch, never from a tag"
            )
        if "repository_dispatch" in triggers(workflow) and relative != CONTROLLER_WORKFLOW:
            violations.append(f"{relative} must not define a second release control plane")

    # 2. The controller is loaded from the default branch by construction, and
    #    it is the only workflow that may be.
    controller = loaded.get(CONTROLLER_WORKFLOW)
    if controller is None:
        violations.append(f"missing release controller: {CONTROLLER_WORKFLOW}")
        return
    controller_triggers = triggers(controller)
    if set(controller_triggers) != {"repository_dispatch"}:
        violations.append(
            f"{CONTROLLER_WORKFLOW} must declare exactly one trigger, repository_dispatch"
        )
    dispatch = controller_triggers.get("repository_dispatch")
    if not isinstance(dispatch, Mapping) or dispatch.get("types") != [DISPATCH_EVENT_TYPE]:
        violations.append(
            f"{CONTROLLER_WORKFLOW} must accept exactly the {DISPATCH_EVENT_TYPE} event type"
        )
    if controller.get("permissions") != {}:
        violations.append(f"{CONTROLLER_WORKFLOW} must grant no default permissions")
    outer = controller.get("concurrency")
    if not isinstance(outer, Mapping) or outer.get("cancel-in-progress") != "false":
        violations.append(
            f"{CONTROLLER_WORKFLOW} must serialize releases without cancelling one in flight"
        )

    controller_jobs = as_mapping(controller.get("jobs"), f"{CONTROLLER_WORKFLOW} jobs", violations)
    expected_jobs = {ADMISSION_JOB, *EXPECTED_CONTROLLER_CALLERS}
    if set(controller_jobs) != expected_jobs:
        violations.append(
            f"{CONTROLLER_WORKFLOW} jobs must be exactly {sorted(expected_jobs)}"
        )

    admit = controller_jobs.get(ADMISSION_JOB)
    if not isinstance(admit, Mapping):
        violations.append(f"{CONTROLLER_WORKFLOW} must define the {ADMISSION_JOB} job")
    else:
        if admit.get("permissions") != EXPECTED_CONTROLLER_ADMIT_PERMISSIONS:
            violations.append(f"{ADMISSION_JOB} must declare exactly contents: read")
        if environment_name(admit) is not None:
            violations.append(f"{ADMISSION_JOB} must not bind any deployment environment")
        if signing_references(admit):
            violations.append(f"{ADMISSION_JOB} must never reference Android signing secrets")
        refs = checkout_refs(admit)
        if refs != [(TRUSTED_REF, ".")]:
            violations.append(
                f"{ADMISSION_JOB} must check out exactly the protected controller revision"
            )
        if semantic_sha256(admit) != EXPECTED_CONTROLLER_ADMIT_SHA256:
            violations.append(f"{ADMISSION_JOB} must match its exact reviewed digest")

    # 3. The payload is data, never code: it may only ever reach a script
    #    through the environment.
    for job_name, job in controller_jobs.items():
        if not isinstance(job, Mapping):
            continue
        for step in job_steps(job):
            run = step.get("run")
            if isinstance(run, str) and "client_payload" in run:
                violations.append(
                    f"{CONTROLLER_WORKFLOW} job {job_name} interpolates the dispatch payload into "
                    "a script; it must be passed through the environment"
                )

    # 4. Each component lane is called from this protected revision, with the
    #    admitted pair, under a declared permission ceiling, and with no secret
    #    handed across the call boundary.
    for job_name, expected in EXPECTED_CONTROLLER_CALLERS.items():
        job = controller_jobs.get(job_name)
        if not isinstance(job, Mapping):
            violations.append(f"{CONTROLLER_WORKFLOW} must define the {job_name} lane")
            continue
        if job.get("uses") != expected["uses"]:
            violations.append(
                f"{CONTROLLER_WORKFLOW} job {job_name} must call {expected['uses']} from this "
                "protected revision"
            )
        if job.get("permissions") != expected["permissions"]:
            violations.append(
                f"{CONTROLLER_WORKFLOW} job {job_name} must declare exactly "
                f"{expected['permissions']}"
            )
        if job.get("with") != EXPECTED_CALLER_INPUTS:
            violations.append(
                f"{CONTROLLER_WORKFLOW} job {job_name} must pass exactly the admitted tag and commit"
            )
        if "secrets" in job:
            violations.append(
                f"{CONTROLLER_WORKFLOW} job {job_name} must not pass secrets across the call "
                "boundary; environment secrets belong to the called job that binds the environment"
            )
        needs = job.get("needs")
        needs = [needs] if isinstance(needs, str) else (needs or [])
        if ADMISSION_JOB not in needs:
            violations.append(f"{CONTROLLER_WORKFLOW} job {job_name} can run unadmitted")

    readiness = controller_jobs.get("readiness")
    if isinstance(readiness, Mapping):
        needs = readiness.get("needs")
        needs = [needs] if isinstance(needs, str) else (needs or [])
        if set(needs) != {ADMISSION_JOB, "android", "bridge", "server"}:
            violations.append(
                f"{CONTROLLER_WORKFLOW} readiness must wait for every component lane"
            )

    # 5. Component workflows are reachable only by that call.
    for relative in COMPONENT_WORKFLOWS:
        workflow = loaded.get(relative)
        if workflow is None:
            violations.append(f"missing release component workflow: {relative}")
            continue
        component_triggers = triggers(workflow)
        if set(component_triggers) != {"workflow_call"}:
            violations.append(
                f"{relative} must declare exactly one trigger, workflow_call; any other trigger "
                "lets a selected ref supply its own definition of a privileged lane"
            )
        call = component_triggers.get("workflow_call")
        declared = call.get("inputs") if isinstance(call, Mapping) else None
        if declared != EXPECTED_COMPONENT_INPUTS:
            violations.append(f"{relative} must accept exactly the admitted tag and commit")
        if isinstance(call, Mapping) and "secrets" in call:
            violations.append(f"{relative} must not declare callable secrets")
        if workflow.get("permissions") != {}:
            violations.append(f"{relative} must grant no default permissions")

    # 5b. Anything a component lane calls in turn is on the release path too.
    #     It may build the candidate, but it may not hold a write permission or
    #     bind an environment, and it must be a local workflow so that it
    #     resolves at this same protected revision.
    for relative in COMPONENT_WORKFLOWS:
        workflow = loaded.get(relative)
        if workflow is None:
            continue
        for job_name, job in (workflow.get("jobs") or {}).items():
            if not isinstance(job, Mapping):
                continue
            called_ref = job.get("uses")
            if not isinstance(called_ref, str):
                continue
            if not called_ref.startswith("./"):
                violations.append(
                    f"{relative} job {job_name} calls {called_ref}, which is not resolved at this "
                    "protected revision"
                )
                continue
            if not permissions_read_only(job.get("permissions")):
                violations.append(
                    f"{relative} job {job_name} delegates a build and must declare read-only "
                    "permissions"
                )
            called = loaded.get(Path(called_ref[2:]))
            if called is None:
                violations.append(f"{relative} job {job_name} calls a missing workflow {called_ref}")
                continue
            if called.get("permissions") != {}:
                violations.append(f"{called_ref} must grant no default permissions")
            for sub_name, sub_job in (called.get("jobs") or {}).items():
                if not isinstance(sub_job, Mapping):
                    continue
                if not permissions_read_only(sub_job.get("permissions")):
                    violations.append(
                        f"{called_ref} job {sub_name} is on the release path and must declare "
                        "read-only permissions"
                    )
                if environment_name(sub_job) is not None:
                    violations.append(
                        f"{called_ref} job {sub_name} is on the release path and must not bind a "
                        "deployment environment"
                    )

    # 6. Trusted helpers execute from a checkout of this protected revision.
    for relative in CONTROL_PLANE:
        workflow = loaded.get(relative)
        if workflow is None:
            continue
        for job_name, job in (workflow.get("jobs") or {}).items():
            if isinstance(job, Mapping):
                check_trusted_helper_sources(relative, job_name, job, violations)

    # 7. Outside the control plane, the release helpers may only be named by a
    #    read-only job — a syntax check or a contract test, never a writer.
    for relative, workflow in loaded.items():
        if relative in CONTROL_PLANE:
            continue
        for job_name, job in (workflow.get("jobs") or {}).items():
            if not isinstance(job, Mapping):
                continue
            body = "\n".join(strings(job))
            named = [
                helper
                for helper in (ATTACHMENT_HELPER.name, IDENTITY_HELPER.name, READINESS_HELPER.name)
                if helper in body
            ]
            if named and not permissions_read_only(job.get("permissions")):
                violations.append(
                    f"{relative} job {job_name} names {sorted(named)} outside the release control "
                    "plane while holding a write permission"
                )

    # 8. The umbrella lock is one repository-wide domain, declared identically by
    #    all three attachment jobs.
    attachment_owners = []
    for relative in (ROOT_WORKFLOW, BRIDGE_WORKFLOW, SERVER_WORKFLOW):
        workflow = loaded.get(relative)
        if workflow is None:
            continue
        job = (workflow.get("jobs") or {}).get(ATTACHMENT_JOB)
        if not isinstance(job, Mapping):
            violations.append(f"{relative} must define the {ATTACHMENT_JOB} job")
            continue
        attachment_owners.append(relative)
        if job.get("concurrency") != EXPECTED_RELEASE_CONCURRENCY:
            violations.append(
                f"{relative} {ATTACHMENT_JOB} must declare exactly the reviewed umbrella-release "
                f"concurrency {EXPECTED_RELEASE_CONCURRENCY}"
            )
        if job.get("permissions") != {"contents": "write"}:
            violations.append(f"{relative} {ATTACHMENT_JOB} permissions must be exactly contents: write")
        if environment_name(job) is not None:
            violations.append(f"{relative} {ATTACHMENT_JOB} must not bind a deployment environment")
        if signing_references(job):
            violations.append(
                f"{relative} {ATTACHMENT_JOB} must never reference Android signing secrets; it "
                "holds the release write credential"
            )
        if ATTACHMENT_HELPER.name not in "\n".join(strings(job)):
            violations.append(f"{relative} {ATTACHMENT_JOB} must attach through {ATTACHMENT_HELPER}")
    if len(attachment_owners) != 3:
        violations.append("all three component lanes must own an umbrella attachment job")

    # 9. Only the reviewed production lane may combine a manual trigger with a
    #    protected environment; nothing else may bind server-production.
    for relative, workflow in loaded.items():
        for job_name, job in (workflow.get("jobs") or {}).items():
            if not isinstance(job, Mapping):
                continue
            if environment_name(job) == PRODUCTION_ENVIRONMENT and relative != PRODUCTION_WORKFLOW:
                violations.append(
                    f"{relative} job {job_name} binds {PRODUCTION_ENVIRONMENT}, which belongs "
                    f"exclusively to {PRODUCTION_WORKFLOW}"
                )
        if relative in CONTROL_PLANE and PRODUCTION_WORKFLOW.name in json.dumps(workflow):
            violations.append(f"{relative} must not reference the hosted-production workflow")


def check(root: Path) -> list[str]:
    workflow_dir = root / WORKFLOW_DIR
    violations: list[str] = []
    root_path = root / ROOT_WORKFLOW
    sibling_path = root / ANDROID_SIBLING_WORKFLOW

    if not root_path.is_file():
        return [f"missing required workflow: {ROOT_WORKFLOW}"]
    if not sibling_path.is_file():
        return [f"missing Android sibling workflow: {ANDROID_SIBLING_WORKFLOW}"]

    loaded: dict[Path, dict[str, Any]] = {}
    workflow_paths = sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")))
    for path in workflow_paths:
        relative = path.relative_to(root)
        try:
            workflow = load_workflow(path)
        except ValueError as exc:
            violations.append(f"{relative}: {exc}")
            continue
        loaded[relative] = workflow
        jobs = as_mapping(workflow.get("jobs"), f"{relative} jobs", violations)
        workflow_scope = {key: value for key, value in workflow.items() if key != "jobs"}

        scope_refs = signing_references(workflow_scope)
        if scope_refs:
            violations.append(
                f"{relative} references Android signing secrets outside a job: "
                f"{', '.join(sorted(scope_refs))}"
            )

        if contains_secret_inheritance(workflow):
            violations.append(f"{relative} must not use reusable-workflow secrets: inherit")
        if contains_unsafe_secret_expression(workflow):
            violations.append(f"{relative} must not use dynamic or whole-context secret expressions")

        for job_name, raw_job in jobs.items():
            if not isinstance(raw_job, Mapping):
                violations.append(f"{relative} job {job_name} must be a mapping")
                continue
            refs = signing_references(raw_job)
            env = environment_name(raw_job)
            is_allowed = relative == ROOT_WORKFLOW and job_name == ALLOWED_JOB
            if refs and not is_allowed:
                violations.append(
                    f"{relative} job {job_name} references Android signing secrets: "
                    f"{', '.join(sorted(refs))}"
                )
            if env and env.casefold() == ENVIRONMENT_NAME.casefold() and not is_allowed:
                violations.append(f"{relative} job {job_name} binds {ENVIRONMENT_NAME} outside {ALLOWED_JOB}")
            if env and "${{" in env and not is_allowed:
                violations.append(f"{relative} job {job_name} uses a dynamic environment outside {ALLOWED_JOB}")

    check_control_plane(loaded, violations)

    root_workflow = loaded.get(ROOT_WORKFLOW)
    if root_workflow is None:
        return violations
    jobs = as_mapping(root_workflow.get("jobs"), f"{ROOT_WORKFLOW} jobs", violations)
    policy = jobs.get(POLICY_JOB)
    if not isinstance(policy, Mapping):
        violations.append(f"{ROOT_WORKFLOW} is missing mapping job {POLICY_JOB}")
        return violations
    release = jobs.get(ALLOWED_JOB)
    if not isinstance(release, Mapping):
        violations.append(f"{ROOT_WORKFLOW} is missing mapping job {ALLOWED_JOB}")
        return violations

    # The Conscrypt producer, reviewed in both the release lane and CI.
    for relative, job_digest in (
        (ROOT_WORKFLOW, EXPECTED_RELEASE_CONSCRYPT_JOB_SHA256),
        (ANDROID_CI_WORKFLOW, EXPECTED_CI_CONSCRYPT_JOB_SHA256),
    ):
        workflow = loaded.get(relative)
        conscrypt_job = (workflow or {}).get("jobs", {}).get(CONSCRYPT_JOB)
        if not isinstance(conscrypt_job, Mapping):
            violations.append(f"{relative} is missing mapping job {CONSCRYPT_JOB}")
        elif semantic_sha256(conscrypt_job) != job_digest:
            violations.append(
                f"{relative} {CONSCRYPT_JOB} must match the exact reviewed producer specification"
            )

    conscrypt_script = root / CONSCRYPT_BUILD_SCRIPT
    if not conscrypt_script.is_file():
        violations.append(f"{CONSCRYPT_BUILD_SCRIPT} is missing")
    elif hashlib.sha256(conscrypt_script.read_bytes()).hexdigest() != EXPECTED_CONSCRYPT_BUILD_SCRIPT_SHA256:
        violations.append(f"{CONSCRYPT_BUILD_SCRIPT} must match its exact reviewed digest")

    # The helpers this control plane executes. Their bytes are what actually
    # admit a source commit, what writes a release asset, and what decides that
    # a draft is complete.
    for helper, expected_digest in (
        (IDENTITY_HELPER, EXPECTED_IDENTITY_HELPER_SHA256),
        (ATTACHMENT_HELPER, EXPECTED_ATTACHMENT_HELPER_SHA256),
        (READINESS_HELPER, EXPECTED_READINESS_HELPER_SHA256),
    ):
        path = root / helper
        if not path.is_file():
            violations.append(f"{helper} is missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected_digest:
            violations.append(f"{helper} must match its exact reviewed digest")

    revalidation = jobs.get(REVALIDATION_JOB)
    if not isinstance(revalidation, Mapping):
        violations.append(f"{ROOT_WORKFLOW} must define the {REVALIDATION_JOB} job")
    elif revalidation != EXPECTED_REVALIDATION_JOB:
        violations.append(
            f"{REVALIDATION_JOB} must match the exact reviewed pre-signing revalidation job"
        )

    attachment = jobs.get(ATTACHMENT_JOB)
    if not isinstance(attachment, Mapping):
        violations.append(f"{ROOT_WORKFLOW} must define the {ATTACHMENT_JOB} job")
    else:
        unexpected_keys = set(attachment) - ALLOWED_ATTACHMENT_JOB_KEYS
        if unexpected_keys:
            violations.append(
                f"{ATTACHMENT_JOB} has unreviewed job keys: {', '.join(sorted(unexpected_keys))}"
            )
        if attachment.get("needs") != ALLOWED_JOB:
            violations.append(f"{ATTACHMENT_JOB} must require a successful {ALLOWED_JOB}")
        attachment_body = "\n".join(strings(attachment))
        if "softprops/action-gh-release" in attachment_body:
            violations.append(
                f"{ATTACHMENT_JOB} must not use a marketplace release action; it can overwrite "
                "assets on an already-published release"
            )
        if RELEASE_ASSET_ARTIFACT not in attachment_body:
            violations.append(
                f"{ATTACHMENT_JOB} must consume the closed release-asset artifact "
                f"{RELEASE_ASSET_ARTIFACT}"
            )
        if "--expected-commit" not in attachment_body:
            violations.append(
                f"{ATTACHMENT_JOB} must bind the attachment to the admitted commit"
            )
        for target in action_uses(attachment):
            if target.startswith("./"):
                violations.append(f"{ATTACHMENT_JOB} must not invoke local actions")
            elif unpinned_action(target):
                violations.append(f"{ATTACHMENT_JOB} action {target} must be SHA-pinned")
        if semantic_sha256(attachment) != EXPECTED_ATTACHMENT_JOB_SHA256:
            violations.append(
                f"{ATTACHMENT_JOB} must match the exact reviewed attachment-job specification"
            )

    # The load-bearing separation: nothing that can write a release may sit in a
    # job that also holds signing material, in this workflow or any other.
    for relative, workflow in loaded.items():
        for job_name, raw_job in (workflow.get("jobs") or {}).items():
            if not isinstance(raw_job, Mapping) or not signing_references(raw_job):
                continue
            body = "\n".join(strings(raw_job))
            reachable = [marker for marker in RELEASE_WRITE_MARKERS if marker in body]
            if reachable:
                violations.append(
                    f"{relative} job {job_name} holds signing material and can also write a "
                    f"release: {', '.join(sorted(reachable))}"
                )
            if not permissions_read_only(raw_job.get("permissions")):
                violations.append(
                    f"{relative} job {job_name} holds signing material and must declare "
                    "read-only permissions"
                )

    for relative in (ROOT_WORKFLOW, ANDROID_CI_WORKFLOW):
        workflow = loaded.get(relative)
        if workflow is None:
            continue
        top_permissions = workflow.get("permissions")
        if top_permissions is not None and not permissions_read_only(top_permissions):
            if not (relative == ROOT_WORKFLOW and top_permissions == {}):
                violations.append(
                    f"{relative} must not grant dynamic or write permissions at workflow scope"
                )
        for inherited_key in ("defaults", "env"):
            if inherited_key in workflow:
                violations.append(
                    f"{relative} must not define workflow-level {inherited_key} that can alter "
                    f"{POLICY_JOB}"
                )

    for job_name, raw_job in jobs.items():
        if not isinstance(raw_job, Mapping):
            continue
        permissions = raw_job.get("permissions")
        if job_name == ATTACHMENT_JOB:
            if permissions != {"contents": "write"}:
                violations.append(f"{ATTACHMENT_JOB} permissions must be exactly contents: write")
        elif permissions is None:
            violations.append(f"{ROOT_WORKFLOW} job {job_name} must declare explicit read-only permissions")
        elif not permissions_read_only(permissions):
            violations.append(
                f"{ROOT_WORKFLOW} job {job_name} has dynamic or write permissions outside "
                f"{ATTACHMENT_JOB}"
            )

    # Unprivileged Android CI keeps the same policy gate and may hold nothing else.
    ci_workflow = loaded.get(ANDROID_CI_WORKFLOW)
    if ci_workflow is None:
        violations.append(f"missing Android CI workflow: {ANDROID_CI_WORKFLOW}")
    else:
        ci_jobs = as_mapping(ci_workflow.get("jobs"), f"{ANDROID_CI_WORKFLOW} jobs", violations)
        if ci_jobs.get(POLICY_JOB) != EXPECTED_CI_POLICY_JOB:
            violations.append(
                f"{ANDROID_CI_WORKFLOW} {POLICY_JOB} must match the exact fail-closed job specification"
            )
        for job_name, raw_job in ci_jobs.items():
            if not isinstance(raw_job, Mapping):
                continue
            if not permissions_read_only(raw_job.get("permissions")):
                violations.append(
                    f"{ANDROID_CI_WORKFLOW} job {job_name} must declare read-only permissions"
                )
            if environment_name(raw_job) is not None:
                violations.append(
                    f"{ANDROID_CI_WORKFLOW} job {job_name} must not bind a deployment environment"
                )
        for event in ("push", "pull_request"):
            paths = trigger_paths(ci_workflow, event)
            missing_paths = REQUIRED_TRIGGER_PATHS - set(paths)
            if missing_paths:
                violations.append(
                    f"{ANDROID_CI_WORKFLOW} {event}.paths is missing: {', '.join(sorted(missing_paths))}"
                )
            if any(path.startswith("!") for path in paths):
                violations.append(
                    f"{ANDROID_CI_WORKFLOW} {event}.paths must not contain negative patterns"
                )

    if policy != EXPECTED_POLICY_JOB:
        violations.append(f"{POLICY_JOB} must match the exact fail-closed job specification")

    missing_refs = SIGNING_SECRETS - signing_references(release)
    if missing_refs:
        violations.append(f"{ALLOWED_JOB} is missing signing references: {', '.join(sorted(missing_refs))}")
    if "if" in release:
        violations.append(
            f"{ALLOWED_JOB} must not carry an event guard; this lane is reachable only through "
            "the protected controller"
        )
    if release.get("needs") != [POLICY_JOB, CONSCRYPT_JOB, REVALIDATION_JOB]:
        violations.append(
            f"{ALLOWED_JOB} must require successful {POLICY_JOB}, {CONSCRYPT_JOB} and "
            f"{REVALIDATION_JOB}"
        )
    if release.get("permissions") != {"contents": "read"}:
        violations.append(
            f"{ALLOWED_JOB} permissions must be exactly contents: read; the release write "
            f"belongs to {ATTACHMENT_JOB}"
        )
    release_steps_raw = release.get("steps")
    checkout_steps = [
        step
        for step in (release_steps_raw if isinstance(release_steps_raw, list) else [])
        if isinstance(step, Mapping) and str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    if len(checkout_steps) != 1 or checkout_steps[0] != EXPECTED_RELEASE_CHECKOUT:
        violations.append(
            f"{ALLOWED_JOB} must check out the admitted commit exactly once with "
            "persist-credentials: false"
        )
    named_secrets = {
        name for item in strings(release) for name in SECRET_REFERENCE.findall(item)
    }
    unreviewed_secrets = named_secrets - ALLOWED_RELEASE_SECRETS
    if unreviewed_secrets:
        violations.append(
            f"{ALLOWED_JOB} must not carry any credential beyond the reviewed signing "
            f"secrets: {', '.join(sorted(unreviewed_secrets))}"
        )
    if release.get("environment") != ENVIRONMENT_NAME:
        violations.append(f"{ALLOWED_JOB} must bind the {ENVIRONMENT_NAME} environment")
    if release.get("runs-on") != "ubuntu-latest":
        violations.append(f"{ALLOWED_JOB} must run exactly on GitHub-hosted ubuntu-latest")
    if release.get("defaults") != {"run": {"working-directory": "android"}}:
        violations.append(f"{ALLOWED_JOB} must use the exact Android working-directory defaults")
    # The umbrella lock belongs on the attachment job. Leaving it here would put
    # the signing job back in the shared write domain it no longer belongs to.
    if "concurrency" in release:
        violations.append(
            f"{ALLOWED_JOB} must not declare concurrency; the umbrella lock belongs to "
            f"{ATTACHMENT_JOB}"
        )
    if semantic_sha256(release) != EXPECTED_RELEASE_JOB_SHA256:
        violations.append(f"{ALLOWED_JOB} must match the exact reviewed release-job specification")
    for forbidden_key in ("container", "services", "strategy", "env", "continue-on-error"):
        if forbidden_key in release:
            violations.append(f"{ALLOWED_JOB} must not define job-level {forbidden_key}")
    unexpected_job_keys = set(release) - ALLOWED_RELEASE_JOB_KEYS
    if unexpected_job_keys:
        violations.append(
            f"{ALLOWED_JOB} has unreviewed job keys: {', '.join(sorted(unexpected_job_keys))}"
        )
    if "uses" in release:
        violations.append(f"{ALLOWED_JOB} must not delegate to a reusable workflow")
    if any(target.startswith("./") for target in action_uses(release)):
        violations.append(f"{ALLOWED_JOB} must not invoke local actions")

    release_steps = release.get("steps")
    if not isinstance(release_steps, list):
        violations.append(f"{ALLOWED_JOB} steps must be a sequence")
    else:
        for step in release_steps:
            if not isinstance(step, Mapping):
                violations.append(f"{ALLOWED_JOB} steps must contain only mappings")
                continue
            step_name = step.get("name")
            unexpected_step_keys = set(step) - ALLOWED_RELEASE_STEP_KEYS
            if unexpected_step_keys:
                violations.append(
                    f"{ALLOWED_JOB} step {step_name!r} has unreviewed keys: "
                    f"{', '.join(sorted(unexpected_step_keys))}"
                )
            if ("run" in step) == ("uses" in step):
                violations.append(
                    f"{ALLOWED_JOB} step {step_name!r} must define exactly one of run or uses"
                )
            for forbidden_key in ("shell", "working-directory", "continue-on-error"):
                if forbidden_key in step:
                    violations.append(
                        f"{ALLOWED_JOB} step {step_name!r} must not define {forbidden_key}"
                    )
            if "env" in step:
                expected_env = REVIEWED_RELEASE_STEP_ENVIRONMENTS.get(str(step_name))
                if step.get("env") != expected_env:
                    violations.append(
                        f"{ALLOWED_JOB} step {step_name!r} must use its exact reviewed environment"
                    )

        for step_name, expected_env in EXPECTED_RELEASE_STEP_ENVIRONMENTS.items():
            matching_steps = [
                step
                for step in release_steps
                if isinstance(step, Mapping) and step.get("name") == step_name
            ]
            if len(matching_steps) != 1 or matching_steps[0].get("env") != expected_env:
                violations.append(
                    f"{ALLOWED_JOB} must contain exactly one {step_name!r} step with reviewed environment"
                )
            elif semantic_sha256(matching_steps[0]) != EXPECTED_SECRET_STEP_SHA256[step_name]:
                violations.append(
                    f"{ALLOWED_JOB} step {step_name!r} must match its exact reviewed execution"
                )
        release_without_step_env = dict(release)
        release_without_step_env["steps"] = [
            {key: value for key, value in step.items() if key != "env"}
            if isinstance(step, Mapping)
            else step
            for step in release_steps
        ]
        refs_outside_reviewed_env = signing_references(release_without_step_env)
        if refs_outside_reviewed_env:
            violations.append(
                f"{ALLOWED_JOB} references signing secrets outside reviewed step environments: "
                f"{', '.join(sorted(refs_outside_reviewed_env))}"
            )

    for relative in (ROOT_WORKFLOW, ANDROID_CI_WORKFLOW, ANDROID_SIBLING_WORKFLOW):
        path = root / relative
        try:
            workflow = load_workflow(path)
        except ValueError:
            continue
        for target in action_uses(workflow):
            if unpinned_action(target):
                violations.append(f"{relative} action must be pinned to a full commit SHA: {target}")

    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()

    violations = check(args.root.resolve())
    if violations:
        print("Release control-plane / Android signing boundary check failed:")
        for violation in sorted(set(violations)):
            print(f"- {violation}")
        return 1

    print("Release control-plane and Android signing boundary check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
