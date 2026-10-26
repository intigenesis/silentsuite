"""Contracts for the Android CI speed changes in build-android.yml.

* superseded pull_request runs are cancelled per workflow and PR; every other run
  has its own group and is never cancelled;
* the static contract suite also runs early in its own read-only job, while
  build-pr keeps running it as before;
* Etebase is built once per run by one producer, and every native consumer admits
  that same-run artifact before its first Gradle or emulator step, keeping its
  NDK provisioning and every later gate.

The checker runs against a byte copy of the predecessor workflow (immutable base
f867267e) to show the exact violations it rejects, against the candidate, which
must have none, and against candidate mutations, which must each be caught.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/build-android.yml"
BASE_FIXTURE = ROOT / "tests/fixtures/build-android-base-f867267e.yml"
BASE_FIXTURE_SHA256 = "c429134d85718a265b5366c0cbe4aef32298c0addb0d7487a50b6854e2ffa4d8"

PRODUCER = "etebase-16kb"
STATIC_JOB = "static-contracts"
CONSUMERS = ("build-pr", "account-recreation-runtime", "color-parity-evidence", "registry-process-boundary")
UNCHANGED_JOBS = ("signing-policy", "conscrypt-r28", "conscrypt-runtime")
ARTIFACT = "etebase-16kb-${{ github.sha }}"
STAGING = "${{ runner.temp }}/etebase-16kb-artifact"
BUILD_SCRIPT = "scripts/build-etebase-client-16kb.sh"
RECORD = 'python3 scripts/etebase-run-artifact.py record --staging "$RUNNER_TEMP/etebase-16kb-artifact"'
ADMIT = 'python3 scripts/etebase-run-artifact.py verify --staging "$RUNNER_TEMP/etebase-16kb-artifact"'
NATIVE_VERIFY = (
    "scripts/verify-native-16kb.py",
    "--require-lib libetebase_android.so",
    "--require-android-ndk-major libetebase_android.so=28",
    "app/libs/client-2.3.2-16kb.aar",
)
STATIC_RUN = "python -m pytest ../tests/test_android_*.py -q"
NDK = "ndk;28.2.13676358"
UPLOAD = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
DOWNLOAD = "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
UPLOAD_WITH = {
    "name": ARTIFACT,
    "path": STAGING,
    "if-no-files-found": "error",
    "retention-days": 1,
    "overwrite": True,
}
CONCURRENCY = {
    "group": (
        "${{ github.workflow }}-${{ github.event_name == 'pull_request' && "
        "format('pr-{0}', github.event.pull_request.number) || format('run-{0}', github.run_id) }}"
    ),
    "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
}
ADMISSION_STEP_NAMES = (
    "Download same-run Etebase 16 KB AAR",
    "Admit same-run Etebase 16 KB AAR",
    "Verify admitted Etebase 16 KB AAR",
)

CONCURRENCY_VIOLATION = "concurrency: must cancel only superseded pull_request runs in workflow-and-PR groups"
EXPECTED_BASE_VIOLATIONS = sorted(
    [
        CONCURRENCY_VIOLATION,
        f"{STATIC_JOB}: missing early static job",
        f"{PRODUCER}: missing producer job",
        *(f"{job}: builds Etebase itself" for job in CONSUMERS),
        *(f"{job}: installs Rust" for job in CONSUMERS),
        *(f"{job}: must need conscrypt-r28 and etebase-16kb" for job in CONSUMERS),
        *(f"{job}: missing Etebase admission" for job in CONSUMERS),
    ]
)


def _run(step: dict) -> str:
    return str(step.get("run", ""))


def _uses(step: dict) -> str:
    return str(step.get("uses", ""))


def _index(steps: list, predicate):
    return next((index for index, step in enumerate(steps) if predicate(step)), None)


def _first_build(steps: list) -> int:
    found = _index(
        steps,
        lambda step: "./gradlew" in _run(step)
        or _uses(step).startswith("ReactiveCircus/android-emulator-runner@"),
    )
    return len(steps) if found is None else found


def _is_native_verify(step: dict) -> bool:
    return all(part in _run(step) for part in NATIVE_VERIFY)


def ci_speed_violations(workflow: dict) -> list[str]:
    jobs = workflow.get("jobs") or {}
    violations = []

    if workflow.get("concurrency") != CONCURRENCY:
        violations.append(CONCURRENCY_VIOLATION)
    for name, job in jobs.items():
        if "concurrency" in job:
            violations.append(f"{name}: must not override concurrency")
        if name == PRODUCER:
            continue
        steps = job.get("steps") or []
        if any(BUILD_SCRIPT in _run(step) for step in steps):
            violations.append(f"{name}: builds Etebase itself")
        if any(_uses(step).startswith("dtolnay/rust-toolchain@") for step in steps):
            violations.append(f"{name}: installs Rust")

    static = jobs.get(STATIC_JOB)
    if static is None:
        violations.append(f"{STATIC_JOB}: missing early static job")
    else:
        if "needs" in static:
            violations.append(f"{STATIC_JOB}: must not depend on another job")
        if static.get("permissions") != {"contents": "read"} or "environment" in static:
            violations.append(f"{STATIC_JOB}: must be read-only")
        if not any(_run(step).strip() == STATIC_RUN for step in static.get("steps") or []):
            violations.append(f"{STATIC_JOB}: must run the exact Android static contract suite")
    if not any(_run(step).strip() == STATIC_RUN for step in (jobs.get("build-pr") or {}).get("steps") or []):
        violations.append("build-pr: static contract suite removed")

    producer = jobs.get(PRODUCER)
    if producer is None:
        violations.append(f"{PRODUCER}: missing producer job")
    else:
        steps = producer.get("steps") or []
        if producer.get("needs") != "signing-policy":
            violations.append(f"{PRODUCER}: producer must need only signing-policy")
        if producer.get("permissions") != {"contents": "read"} or "environment" in producer:
            violations.append(f"{PRODUCER}: producer must be read-only")
        if not any(NDK in _run(step) for step in steps):
            violations.append(f"{PRODUCER}: producer must provision NDK r28")
        build = _index(steps, lambda step: _run(step).strip() == BUILD_SCRIPT)
        native = _index(steps, _is_native_verify)
        record = _index(steps, lambda step: _run(step).strip() == RECORD)
        upload = _index(steps, lambda step: _uses(step) == UPLOAD)
        if None in (build, native, record, upload) or not build < native < record < upload == len(steps) - 1:
            violations.append(f"{PRODUCER}: producer must build, verify, record, then upload last")
        last = steps[-1] if steps else {}
        if _uses(last) != UPLOAD or last.get("with") != UPLOAD_WITH:
            violations.append(f"{PRODUCER}: last step must be the pinned overwrite upload of the fixed staging folder")

    for name in CONSUMERS:
        job = jobs.get(name)
        if job is None:
            violations.append(f"{name}: missing consumer job")
            continue
        needs = job.get("needs")
        needs = [needs] if isinstance(needs, str) else list(needs or [])
        if sorted(needs) != ["conscrypt-r28", PRODUCER]:
            violations.append(f"{name}: must need conscrypt-r28 and etebase-16kb")
        steps = job.get("steps") or []
        first = _first_build(steps)
        download = _index(
            steps, lambda step: _uses(step) == DOWNLOAD and step.get("with") == {"name": ARTIFACT, "path": STAGING}
        )
        admit = _index(steps, lambda step: _run(step).strip() == ADMIT)
        native = _index(steps, _is_native_verify)
        if download is None or admit is None:
            violations.append(f"{name}: missing Etebase admission")
        elif native is None:
            violations.append(f"{name}: missing consumer 16 KB verification")
        elif not download < admit < native < first:
            violations.append(f"{name}: Etebase admission after first Gradle or emulator step")
        if not any(NDK in _run(step) for step in steps[:first]):
            violations.append(f"{name}: NDK provisioning removed")
    return sorted(violations)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict):
    # PyYAML reads the bare `on` key as boolean True.
    return workflow.get("on", workflow.get(True))


def _without_native_compile(job: dict) -> dict:
    """The predecessor consumer minus only its Rust toolchain and Etebase compile."""
    job = copy.deepcopy(job)
    steps = []
    for step in job.get("steps") or []:
        if _uses(step).startswith("dtolnay/rust-toolchain@"):
            continue
        run = step.get("run")
        if isinstance(run, str) and BUILD_SCRIPT in run:
            kept = "".join(line for line in run.splitlines(keepends=True) if line.strip() != BUILD_SCRIPT)
            if not kept.strip():
                continue
            step = dict(step, run=kept)
            if step.get("name") == "Make gradlew executable and build native client":
                step["name"] = "Make gradlew executable"
        steps.append(step)
    job["steps"] = steps
    job.pop("needs", None)
    return job


def _without_admission(job: dict) -> dict:
    job = copy.deepcopy(job)
    job["steps"] = [step for step in job.get("steps") or [] if step.get("name") not in ADMISSION_STEP_NAMES]
    job.pop("needs", None)
    return job


@pytest.fixture(scope="module")
def base() -> dict:
    assert hashlib.sha256(BASE_FIXTURE.read_bytes()).hexdigest() == BASE_FIXTURE_SHA256
    return _load(BASE_FIXTURE)


@pytest.fixture(scope="module")
def candidate() -> dict:
    return _load(WORKFLOW)


def test_predecessor_workflow_is_rejected_with_exactly_the_expected_violations(base):
    assert ci_speed_violations(base) == EXPECTED_BASE_VIOLATIONS


def test_candidate_workflow_satisfies_every_speed_contract(candidate):
    assert ci_speed_violations(candidate) == []


def test_candidate_changes_nothing_else_relative_to_the_predecessor(base, candidate):
    assert candidate["name"] == base["name"]
    assert _triggers(candidate) == _triggers(base)
    assert set(candidate["jobs"]) == set(base["jobs"]) | {STATIC_JOB, PRODUCER}
    for name in UNCHANGED_JOBS:
        assert candidate["jobs"][name] == base["jobs"][name], name
    # Consumers lose only the Rust toolchain and the Etebase compile, and gain only
    # the admission steps: NDK, build-tools, JDKs, matrices, runtime scripts,
    # ledgers, producer manifests and every final gate are otherwise identical.
    for name in CONSUMERS:
        assert _without_admission(candidate["jobs"][name]) == _without_native_compile(base["jobs"][name]), name


def test_static_job_runs_the_same_suite_as_build_pr(candidate):
    static = candidate["jobs"][STATIC_JOB]["steps"]
    build_pr = candidate["jobs"]["build-pr"]["steps"]

    def same(name):
        left = next(step for step in static if step.get("name") == name)
        right = next(step for step in build_pr if step.get("name") == name)
        return left == right

    assert same("Set up Python for static Android contracts")
    assert same("Install Android static contract dependencies")
    assert same("Run Android static Python contracts")
    assert candidate["jobs"][STATIC_JOB]["defaults"] == {"run": {"working-directory": "android"}}


def _step_named(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def _remove_step(job: dict, predicate) -> None:
    job["steps"] = [step for step in job["steps"] if not predicate(step)]


def _move_to_end(job: dict, name: str) -> None:
    step = _step_named(job, name)
    job["steps"].remove(step)
    job["steps"].append(step)


def _mutation(workflow: dict, case: str) -> None:
    jobs = workflow["jobs"]
    if case == "remove-admission":
        _remove_step(jobs["account-recreation-runtime"], lambda step: step.get("name") == ADMISSION_STEP_NAMES[1])
    elif case == "remove-download":
        _remove_step(jobs["registry-process-boundary"], lambda step: step.get("name") == ADMISSION_STEP_NAMES[0])
    elif case == "reorder-admission":
        _move_to_end(jobs["color-parity-evidence"], ADMISSION_STEP_NAMES[1])
    elif case == "reorder-native-verify":
        _move_to_end(jobs["build-pr"], ADMISSION_STEP_NAMES[2])
    elif case == "remove-native-verify":
        _remove_step(jobs["registry-process-boundary"], lambda step: step.get("name") == ADMISSION_STEP_NAMES[2])
    elif case == "remove-ndk":
        _remove_step(jobs["build-pr"], lambda step: NDK in _run(step))
    elif case == "rebuild-etebase":
        jobs["account-recreation-runtime"]["steps"].insert(0, {"name": "Rebuild", "run": BUILD_SCRIPT})
    elif case == "consumer-skips-producer":
        jobs["color-parity-evidence"]["needs"] = "conscrypt-r28"
    elif case == "cancel-every-event":
        workflow["concurrency"]["cancel-in-progress"] = True
    elif case == "non-unique-push-group":
        workflow["concurrency"]["group"] = "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}"
    elif case == "job-level-concurrency":
        jobs["build-pr"]["concurrency"] = {"group": "x", "cancel-in-progress": True}
    elif case == "upload-without-overwrite":
        jobs[PRODUCER]["steps"][-1]["with"]["overwrite"] = False
    elif case == "upload-not-last":
        jobs[PRODUCER]["steps"].append({"name": "After upload", "run": "true"})
    elif case == "producer-needs-conscrypt":
        jobs[PRODUCER]["needs"] = ["signing-policy", "conscrypt-r28"]
    elif case == "static-job-waits":
        jobs[STATIC_JOB]["needs"] = "conscrypt-r28"
    elif case == "build-pr-drops-static-suite":
        _remove_step(jobs["build-pr"], lambda step: _run(step).strip() == STATIC_RUN)
    else:
        raise AssertionError(case)


MUTATIONS = {
    "remove-admission": "account-recreation-runtime: missing Etebase admission",
    "remove-download": "registry-process-boundary: missing Etebase admission",
    "reorder-admission": "color-parity-evidence: Etebase admission after first Gradle or emulator step",
    "reorder-native-verify": "build-pr: Etebase admission after first Gradle or emulator step",
    "remove-native-verify": "registry-process-boundary: missing consumer 16 KB verification",
    "remove-ndk": "build-pr: NDK provisioning removed",
    "rebuild-etebase": "account-recreation-runtime: builds Etebase itself",
    "consumer-skips-producer": "color-parity-evidence: must need conscrypt-r28 and etebase-16kb",
    "cancel-every-event": CONCURRENCY_VIOLATION,
    "non-unique-push-group": CONCURRENCY_VIOLATION,
    "job-level-concurrency": "build-pr: must not override concurrency",
    "upload-without-overwrite": f"{PRODUCER}: last step must be the pinned overwrite upload of the fixed staging folder",
    "upload-not-last": f"{PRODUCER}: producer must build, verify, record, then upload last",
    "producer-needs-conscrypt": f"{PRODUCER}: producer must need only signing-policy",
    "static-job-waits": f"{STATIC_JOB}: must not depend on another job",
    "build-pr-drops-static-suite": "build-pr: static contract suite removed",
}


@pytest.mark.parametrize(("case", "expected"), MUTATIONS.items(), ids=MUTATIONS.keys())
def test_candidate_mutations_are_rejected(candidate, case, expected):
    mutated = copy.deepcopy(candidate)
    _mutation(mutated, case)
    assert expected in ci_speed_violations(mutated)
