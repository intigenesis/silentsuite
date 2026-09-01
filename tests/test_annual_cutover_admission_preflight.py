"""Cross-file admission contract for local reusable callers with callee owner gates.

A caller job's `if` gate is not the admission signal that decides whether work
runs. When a local reusable callee gates its own jobs on a repository variable
(`vars.WEB_DEPLOY_APPROVED_SHA`, `vars.DOCS_DEPLOY_APPROVED_SHA`), the caller's
own gate can be fully satisfied while every callee job skips. GitHub then marks
the caller job `skipped`, downstream `needs` skip in turn, and the whole run
concludes `skipped`: zero jobs, zero logs, no diagnostics — while an
owner-approved exact-SHA window is burned. That is exactly how the annual Stage
B cutover produced two all-skipped runs.

Like `test_reusable_workflow_caller_permissions.py`, this mismatch lives across
two files and is invisible to `actionlint`, which validates one workflow at a
time. The contract: a caller workflow must run an unconditional preflight job
that observes every approval variable its callees gate on, must pin exactly
which callee each caller job invokes, and must end in an always()-run outcome
job that fails when any admitted job did not succeed.

The structural contract is expressed as `admission_violations()` over a parsed
document rather than as assertions against the checked-in file, so every clause
is proved by sabotaging a copy: a contract that only ever sees the correct tree
cannot show it would reject a wrong one. The outcome job's shell program is
likewise executed against result fixtures, because "the job references
`needs.*.result`" is not evidence that it refuses a run that deployed nothing.
"""

from __future__ import annotations

import copy
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"

CALLER = WORKFLOW_DIR / "annual-only-public-cutover.yml"
CI = WORKFLOW_DIR / "ci-server.yml"
PREFLIGHT = "admission"
OUTCOME = "cutover-outcome"

# Exact caller-job -> local reusable callee topology. Following whatever local
# callee a job happens to name would let a topology substitution (deploy-web
# calling deploy-docs.yml) satisfy a contract that derives its own expectations
# from the file it is meant to police.
EXPECTED_CALLEES = {
    "deploy-web": "./.github/workflows/deploy-web.yml",
    "deploy-docs": "./.github/workflows/deploy-docs.yml",
}

VAR_REFERENCE = re.compile(r"vars\.([A-Za-z_][A-Za-z0-9_]*)")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def needs_of(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def callee_gate_variables(relative: str) -> set[str]:
    path = ROOT / relative[2:] if relative.startswith("./") else ROOT / relative
    names: set[str] = set()
    for job in load(path).get("jobs", {}).values():
        names.update(VAR_REFERENCE.findall(str(job.get("if", ""))))
    return names


def admission_violations(document: dict) -> list[str]:
    """Every way a cutover caller can silently execute no admitted work."""
    problems: list[str] = []
    jobs = document.get("jobs", {})

    preflight = jobs.get(PREFLIGHT)
    if preflight is None:
        return [f"missing `{PREFLIGHT}` preflight job"]
    if "if" in preflight:
        problems.append(
            f"`{PREFLIGHT}` is gated; a preflight that can skip reproduces the silent "
            "zero-job cutover it exists to diagnose"
        )

    observed = set(preflight.get("env", {}))
    required = set(VAR_REFERENCE.findall(str(preflight.get("env", {}))))

    local_callers = {
        name: job["uses"]
        for name, job in jobs.items()
        if isinstance(job.get("uses"), str) and job["uses"].startswith("./.github/workflows/")
    }
    if local_callers != EXPECTED_CALLEES:
        problems.append(
            f"caller-to-callee topology is {local_callers}, expected {EXPECTED_CALLEES}"
        )
    for name, callee in sorted(local_callers.items()):
        if EXPECTED_CALLEES.get(name) == callee:
            required |= callee_gate_variables(callee)
        if PREFLIGHT not in needs_of(jobs[name]):
            problems.append(f"caller job `{name}` does not require `{PREFLIGHT}`")

    for name in sorted(required - observed):
        problems.append(
            f"`{PREFLIGHT}` does not observe `vars.{name}`: a callee gates its jobs on it, "
            "so an unarmed value silently skips every job in this cutover"
        )

    outcome = jobs.get(OUTCOME)
    if outcome is None:
        problems.append(f"missing `{OUTCOME}` job")
        return problems
    if str(outcome.get("if", "")).strip() != "always()":
        problems.append(f"`{OUTCOME}` must run with `always()`")
    observed_needs = set(needs_of(outcome))
    if observed_needs != set(jobs) - {OUTCOME}:
        problems.append(f"`{OUTCOME}` must observe every other job, observes {sorted(observed_needs)}")
    rendered = yaml.safe_dump(outcome)
    for name in sorted(observed_needs):
        if f"needs['{name}'].result" not in rendered:
            problems.append(f"`{OUTCOME}` does not read the result of `{name}`")
    return problems


def test_the_checked_in_cutover_satisfies_the_admission_contract() -> None:
    assert admission_violations(load(CALLER)) == []


def sabotage(mutate) -> dict:
    document = copy.deepcopy(load(CALLER))
    mutate(document)
    return document


def swap_callees(document: dict) -> None:
    jobs = document["jobs"]
    jobs["deploy-web"]["uses"], jobs["deploy-docs"]["uses"] = (
        jobs["deploy-docs"]["uses"],
        jobs["deploy-web"]["uses"],
    )


@pytest.mark.parametrize(
    "label,mutate,expected",
    [
        ("preflight removed", lambda d: d["jobs"].pop(PREFLIGHT), "missing `admission` preflight job"),
        (
            "preflight gated behind the same variable it must diagnose",
            lambda d: d["jobs"][PREFLIGHT].update({"if": "vars.ANNUAL_PUBLIC_CUTOVER_APPROVED_SHA == inputs.expected_sha"}),
            "is gated",
        ),
        (
            "callee approval variable no longer observed",
            lambda d: d["jobs"][PREFLIGHT]["env"].pop("WEB_DEPLOY_APPROVED_SHA"),
            "does not observe `vars.WEB_DEPLOY_APPROVED_SHA`",
        ),
        (
            "docs approval variable no longer observed",
            lambda d: d["jobs"][PREFLIGHT]["env"].pop("DOCS_DEPLOY_APPROVED_SHA"),
            "does not observe `vars.DOCS_DEPLOY_APPROVED_SHA`",
        ),
        (
            "caller no longer requires the preflight",
            lambda d: d["jobs"]["deploy-web"].pop("needs"),
            "caller job `deploy-web` does not require `admission`",
        ),
        ("callee topology substituted", swap_callees, "caller-to-callee topology"),
        (
            "unexpected extra local callee wired in",
            lambda d: d["jobs"].update({"deploy-server": {"uses": "./.github/workflows/deploy-server.yml"}}),
            "caller-to-callee topology",
        ),
        ("outcome removed", lambda d: d["jobs"].pop(OUTCOME), "missing `cutover-outcome` job"),
        (
            "outcome no longer always()",
            lambda d: d["jobs"][OUTCOME].update({"if": "success()"}),
            "must run with `always()`",
        ),
        (
            "outcome stops observing a deploying job",
            lambda d: d["jobs"][OUTCOME].update({"needs": [PREFLIGHT, "deploy-web", "attest"]}),
            "must observe every other job",
        ),
        (
            "outcome stops reading a result it observes",
            lambda d: d["jobs"][OUTCOME]["env"].pop("DEPLOY_DOCS_RESULT"),
            "does not read the result of `deploy-docs`",
        ),
    ],
)
def test_contract_rejects_each_way_a_cutover_can_execute_nothing(label, mutate, expected) -> None:
    problems = admission_violations(sabotage(mutate))
    assert any(expected in problem for problem in problems), f"{label} was accepted: {problems}"


SHA = "3" * 40
OTHER = "9" * 40


def program_of(job_name: str) -> str:
    job = load(CALLER)["jobs"][job_name]
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def run_shell(program: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", program],
        env={"PATH": os.environ.get("PATH", ""), **env},
        capture_output=True,
        text=True,
    )


def run_preflight(**overrides: str) -> subprocess.CompletedProcess:
    env = {
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": SHA,
        "EXPECTED_SHA": SHA,
        "ANNUAL_PUBLIC_CUTOVER_APPROVED_SHA": SHA,
        "WEB_DEPLOY_APPROVED_SHA": SHA,
        "DOCS_DEPLOY_APPROVED_SHA": SHA,
    }
    env.update(overrides)
    return run_shell(program_of(PREFLIGHT), env)


def test_preflight_declares_every_identity_and_approval_it_must_assert() -> None:
    program = program_of(PREFLIGHT)
    assert "refs/heads/main" in program
    assert "EXPECTED_SHA" in program
    assert "GITHUB_SHA" in program
    for name in ("ANNUAL_PUBLIC_CUTOVER_APPROVED_SHA", "WEB_DEPLOY_APPROVED_SHA", "DOCS_DEPLOY_APPROVED_SHA"):
        assert name in program, f"`{PREFLIGHT}` must assert `{name}` matches the exact expected SHA"


def test_preflight_admits_a_fully_armed_exact_live_main_cutover() -> None:
    result = run_preflight()
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"


@pytest.mark.parametrize(
    "override",
    [
        {"GITHUB_REF": "refs/heads/dev"},
        {"GITHUB_SHA": OTHER},
        {"EXPECTED_SHA": "not-a-sha"},
        {"EXPECTED_SHA": SHA[:7]},
        {"ANNUAL_PUBLIC_CUTOVER_APPROVED_SHA": ""},
        {"ANNUAL_PUBLIC_CUTOVER_APPROVED_SHA": OTHER},
        {"WEB_DEPLOY_APPROVED_SHA": ""},
        {"WEB_DEPLOY_APPROVED_SHA": OTHER},
        {"DOCS_DEPLOY_APPROVED_SHA": ""},
        {"DOCS_DEPLOY_APPROVED_SHA": OTHER},
    ],
)
def test_preflight_refuses_and_diagnoses_every_unarmed_or_mismatched_identity(override: dict) -> None:
    result = run_preflight(**override)
    assert result.returncode != 0, f"preflight admitted {override}: {result.stdout}{result.stderr}"
    assert "::error::" in f"{result.stdout}{result.stderr}", (
        f"preflight refused {override} without an explicit diagnostic"
    )


RESULT_VARIABLES = ("ADMISSION_RESULT", "DEPLOY_WEB_RESULT", "DEPLOY_DOCS_RESULT", "ATTEST_RESULT")


def run_outcome(**overrides: str) -> subprocess.CompletedProcess:
    env = {name: "success" for name in RESULT_VARIABLES}
    env.update(overrides)
    return run_shell(program_of(OUTCOME), env)


def test_outcome_accepts_only_a_cutover_where_every_admitted_job_succeeded() -> None:
    result = run_outcome()
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    for job in ("admission", "deploy-web", "deploy-docs", "attest"):
        assert job in result.stdout, f"outcome must report `{job}` even on success"


@pytest.mark.parametrize("variable", RESULT_VARIABLES)
@pytest.mark.parametrize("conclusion", ["failure", "skipped", "cancelled", ""])
def test_outcome_refuses_any_job_that_did_not_succeed(variable: str, conclusion: str) -> None:
    result = run_outcome(**{variable: conclusion})
    combined = f"{result.stdout}{result.stderr}"
    assert result.returncode != 0, f"outcome accepted {variable}={conclusion!r}: {combined}"
    assert "::error::" in combined, f"outcome refused {variable}={conclusion!r} without a diagnostic"


def test_outcome_refuses_the_exact_all_skipped_no_op_that_burned_two_dispatches() -> None:
    # The observed live failure: every job skipped, run conclusion `skipped`,
    # zero mutations, no logs. That must now be a red run with diagnostics.
    result = run_outcome(**{name: "skipped" for name in RESULT_VARIABLES})
    combined = f"{result.stdout}{result.stderr}"
    assert result.returncode != 0, f"outcome accepted an all-skipped no-op: {combined}"
    for job in ("admission", "deploy-web", "deploy-docs", "attest"):
        assert f"::error::{job} concluded skipped" in combined, f"missing diagnostic for `{job}`"
    assert "ANNUAL_PUBLIC_CUTOVER_APPROVED_SHA" in combined, (
        "a refused cutover must tell the operator to clear the armed approval variables"
    )


def test_ci_triggers_on_the_contract_that_guards_the_cutover() -> None:
    triggers = load(CI)[True]
    for event in ("push", "pull_request"):
        paths = triggers[event]["paths"]
        assert "tests/test_annual_cutover_admission_preflight.py" in paths, (
            f"ci-server `{event}.paths` omits this contract, so editing it alone runs no CI"
        )


def test_no_deployment_gate_was_weakened() -> None:
    text = CALLER.read_text(encoding="utf-8")
    gate = "github.ref == 'refs/heads/main' && github.sha == inputs.expected_sha && vars.ANNUAL_PUBLIC_CUTOVER_APPROVED_SHA == inputs.expected_sha"
    assert text.count(gate) == 3, "each deploying job must keep the exact owner one-use SHA gate"
    for callee in ("deploy-web.yml", "deploy-docs.yml"):
        for job in load(WORKFLOW_DIR / callee)["jobs"].values():
            assert "APPROVED_SHA == inputs.expected_sha" in str(job.get("if", ""))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
