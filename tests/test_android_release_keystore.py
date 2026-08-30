"""Behavioural contract for the pre-Gradle Android keystore verifier.

v0.5.4-beta's signed build decoded the environment secret and then died inside
`:app:packageRelease` with `KeytoolException: Failed to read key from
.../silentsuite-release.jks: Tag number over 30 is not supported`. The bytes on
disk were not a readable keystore, and nothing said so until Gradle had already
spent twenty minutes building. `scripts/verify-android-release-keystore.sh` is
the check that now runs between the decode and Gradle.

Reading it proves very little, so every case here builds a real keystore with
`keytool` and runs the real script against it: a good store, a wrong password, a
missing alias, a certificate-only entry, an unexpected certificate, and a
truncated store — the shape the release actually hit.

No secret is embedded anywhere. Passwords are generated per test, passed only
through the environment, and asserted absent from both the output and the
argument vector the script hands to keytool.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify-android-release-keystore.sh"
ANDROID_RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release-android.yml"

# The reviewed developer upload certificate. Nothing here can produce it — these
# tests generate throwaway keys — so the fingerprint is overridden per case and
# the constant is asserted to be what the shipped helper and policy pin.
EXPECTED_UPLOAD_CERT_SHA256 = "8035a4ff1511e2045c579c905d26e93af6009b239e741ef78542ae04e7a7ca79"

ALIAS = "silentsuite-upload"
FINGERPRINT = re.compile(r"SHA256:\s*((?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2})")

keytool_required = pytest.mark.skipif(
    shutil.which("keytool") is None, reason="keytool (JDK) is not installed"
)


def keytool(*arguments: str, password: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["keytool", *arguments],
        capture_output=True,
        text=True,
        env={**os.environ, "SILENTSUITE_TEST_STOREPASS": password},
    )


def make_keystore(path: Path, password: str, alias: str = ALIAS) -> None:
    result = keytool(
        "-genkeypair",
        "-keystore", str(path),
        "-storetype", "JKS",
        "-alias", alias,
        "-keyalg", "RSA",
        "-keysize", "2048",
        "-validity", "1",
        "-dname", "CN=SilentSuite Test Upload",
        "-storepass:env", "SILENTSUITE_TEST_STOREPASS",
        "-keypass:env", "SILENTSUITE_TEST_STOREPASS",
        password=password,
    )
    assert result.returncode == 0, result.stderr
    assert path.is_file() and path.stat().st_size > 0


def fingerprint_of(path: Path, password: str) -> str:
    result = keytool(
        "-list", "-v", "-keystore", str(path),
        "-storepass:env", "SILENTSUITE_TEST_STOREPASS",
        password=password,
    )
    assert result.returncode == 0, result.stderr
    match = FINGERPRINT.search(result.stdout)
    assert match, result.stdout
    return match.group(1).replace(":", "").lower()


def verify(
    keystore: Path,
    password: str,
    *,
    alias: str = ALIAS,
    expected: str | None = None,
    path_override: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "KEYSTORE_PATH": path_override if path_override is not None else str(keystore),
        "KSTOREPWD": password,
        "KEY_ALIAS": alias,
        **(extra_env or {}),
    }
    command = ["bash", str(VERIFIER)]
    if expected is not None:
        command += ["--expect-sha256", expected]
    return subprocess.run(command, capture_output=True, text=True, env=environment)


@pytest.fixture
def store(tmp_path: Path):
    password = secrets.token_urlsafe(24)
    path = tmp_path / "silentsuite-release.jks"
    make_keystore(path, password)
    return {"path": path, "password": password, "fingerprint": fingerprint_of(path, password)}


# ── The one path that signs ───────────────────────────────────────────


@keytool_required
def test_a_matching_keystore_is_accepted(store):
    result = verify(store["path"], store["password"], expected=store["fingerprint"])

    assert result.returncode == 0, result.stderr
    assert "Release keystore verified before Gradle" in result.stdout
    assert "PrivateKeyEntry" in result.stdout
    assert store["fingerprint"] in result.stdout


# ── Everything that must fail before Gradle ───────────────────────────


@keytool_required
def test_a_truncated_store_is_refused(store, tmp_path: Path):
    """The v0.5.4-beta shape: a mangled decode that is not DER at all."""

    broken = tmp_path / "broken.jks"
    broken.write_bytes(store["path"].read_bytes()[:400])

    result = verify(broken, store["password"], expected=store["fingerprint"])

    assert result.returncode == 1
    assert "Refusing to sign" in result.stderr
    assert "keystore" in result.stderr


@keytool_required
def test_random_bytes_are_refused_as_a_der_format_error(store, tmp_path: Path):
    garbage = tmp_path / "garbage.jks"
    garbage.write_bytes(secrets.token_bytes(4096))

    result = verify(garbage, store["password"], expected=store["fingerprint"])

    assert result.returncode == 1
    assert "not a readable keystore" in result.stderr


@keytool_required
def test_a_wrong_store_password_is_refused(store):
    result = verify(store["path"], secrets.token_urlsafe(24), expected=store["fingerprint"])

    assert result.returncode == 1
    assert "store password was rejected" in result.stderr


@keytool_required
def test_a_missing_alias_is_refused(store):
    result = verify(
        store["path"], store["password"], alias="not-present", expected=store["fingerprint"]
    )

    assert result.returncode == 1
    assert "alias is not present" in result.stderr


@keytool_required
def test_a_certificate_only_entry_is_refused(store, tmp_path: Path):
    """A trustedCertEntry cannot sign, and must not be mistaken for a key."""

    certificate = tmp_path / "upload.cer"
    export = keytool(
        "-exportcert", "-alias", ALIAS, "-keystore", str(store["path"]),
        "-storepass:env", "SILENTSUITE_TEST_STOREPASS", "-file", str(certificate),
        password=store["password"],
    )
    assert export.returncode == 0, export.stderr

    cert_only = tmp_path / "cert-only.jks"
    imported = keytool(
        "-importcert", "-noprompt", "-alias", ALIAS, "-file", str(certificate),
        "-keystore", str(cert_only), "-storetype", "JKS",
        "-storepass:env", "SILENTSUITE_TEST_STOREPASS",
        password=store["password"],
    )
    assert imported.returncode == 0, imported.stderr

    result = verify(cert_only, store["password"], expected=store["fingerprint"])

    assert result.returncode == 1
    assert "not a PrivateKeyEntry" in result.stderr


@keytool_required
def test_an_unexpected_certificate_is_refused_and_both_fingerprints_reported(store):
    other = "b" * 64

    result = verify(store["path"], store["password"], expected=other)

    assert result.returncode == 1
    assert "not the reviewed upload key" in result.stderr
    assert f"expected SHA-256: {other}" in result.stderr
    assert f"observed SHA-256: {store['fingerprint']}" in result.stderr


@keytool_required
def test_an_empty_store_is_refused_before_keytool_runs(store, tmp_path: Path):
    empty = tmp_path / "empty.jks"
    empty.touch()

    result = verify(empty, store["password"], expected=store["fingerprint"])

    assert result.returncode == 1
    assert "decoded keystore is empty" in result.stderr


@keytool_required
def test_a_missing_store_is_refused(store, tmp_path: Path):
    result = verify(
        store["path"],
        store["password"],
        expected=store["fingerprint"],
        path_override=str(tmp_path / "absent.jks"),
    )

    assert result.returncode == 1
    assert "not a regular file" in result.stderr


@pytest.mark.parametrize("missing", ["KEYSTORE_PATH", "KSTOREPWD", "KEY_ALIAS"])  # noqa: PT006
def test_a_missing_input_fails_closed(tmp_path: Path, missing: str):
    environment = {
        **os.environ,
        "KEYSTORE_PATH": str(tmp_path / "x.jks"),
        "KSTOREPWD": "unused",
        "KEY_ALIAS": ALIAS,
    }
    environment.pop(missing)
    result = subprocess.run(
        ["bash", str(VERIFIER)], capture_output=True, text=True, env=environment
    )

    assert result.returncode != 0
    assert missing in result.stderr


# ── Secret handling ───────────────────────────────────────────────────


@keytool_required
@pytest.mark.parametrize(
    "case", ["good", "wrong-password", "wrong-alias", "wrong-certificate", "corrupt"]
)
def test_no_outcome_ever_prints_the_password_or_alias(store, tmp_path: Path, case: str):
    password = store["password"]
    keystore = store["path"]
    expected = store["fingerprint"]
    alias = ALIAS
    if case == "wrong-password":
        password = secrets.token_urlsafe(24)
    elif case == "wrong-alias":
        alias = "some-other-alias"
    elif case == "wrong-certificate":
        expected = "c" * 64
    elif case == "corrupt":
        keystore = tmp_path / "corrupt.jks"
        keystore.write_bytes(store["path"].read_bytes()[:200])

    result = verify(keystore, password, alias=alias, expected=expected)
    combined = result.stdout + result.stderr

    assert password not in combined, f"{case}: the store password reached the log"
    assert store["password"] not in combined, f"{case}: the real password reached the log"
    assert alias not in combined, f"{case}: the signing alias reached the log"
    assert ALIAS not in combined, f"{case}: the real alias reached the log"


@keytool_required
def test_the_password_and_alias_never_reach_the_keytool_argument_vector(store, tmp_path: Path):
    """A shim records exactly what the verifier hands to keytool, then execs it.

    `ps` on a shared runner shows the argument vector of every process, so
    "the password is not an argument" has to be proven, not asserted in prose.
    """

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    record = tmp_path / "argv.log"
    real = shutil.which("keytool")
    (shim_dir / "keytool").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> {record}\n'
        f'exec {real} "$@"\n',
        encoding="utf-8",
    )
    (shim_dir / "keytool").chmod(0o755)

    result = verify(
        store["path"],
        store["password"],
        expected=store["fingerprint"],
        extra_env={"PATH": f"{shim_dir}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stderr
    recorded = record.read_text(encoding="utf-8")
    assert recorded, "the shim captured no keytool invocation"
    assert store["password"] not in recorded, "the store password was passed as an argument"
    assert ALIAS not in recorded, "the signing alias was passed as an argument"
    # The password is named, not spelled: keytool reads it from the environment.
    assert "-storepass:env" in recorded
    assert "KSTOREPWD" in recorded


def executable_text(path: Path) -> str:
    """Source with comments stripped: these rules are about what runs.

    The comments deliberately name the two constructs being avoided.
    """

    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_the_verifier_never_writes_key_material_or_passwords_to_disk():
    source = executable_text(VERIFIER)
    assert "-storepass:env KSTOREPWD" in source
    assert "-storepass " not in source, "a literal password argument would land in `ps`"
    assert "-keypass" not in source
    assert "-alias " not in source, "the alias is matched in-shell, not passed to keytool"
    # keytool's own output is held in a variable and never persisted or echoed.
    # keytool's output is held in a shell variable, matched against fixed
    # patterns, and never echoed or persisted.
    assert 'LISTING="$(keytool' in source
    for leak in ('echo "$LISTING"', 'echo "${LISTING}"', '"$LISTING" >', '"$LISTING" >>'):
        assert leak not in source, f"keytool output must not be forwarded: {leak}"
    assert 'EXPECTED_CERT_SHA256:-' not in source, (
        "the expected fingerprint must not fall back to an ambient variable"
    )
    # The only place it is consumed is the failure classifier and the awk split.
    assert 'case "$LISTING" in' in source
    assert 'printf \'%s\\n\' "$LISTING" | awk' in source


def test_the_verifier_pins_the_reviewed_upload_certificate():
    source = VERIFIER.read_text(encoding="utf-8")
    assert EXPECTED_UPLOAD_CERT_SHA256 in source
    policy = (ROOT / "scripts" / "check-android-signing-boundary.py").read_text(encoding="utf-8")
    assert EXPECTED_UPLOAD_CERT_SHA256 in policy, "the policy must pin the same certificate"


# ── Wiring: decode, then verify, then Gradle ──────────────────────────


def build_release_steps() -> list[dict]:
    workflow = yaml.load(
        ANDROID_RELEASE_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    return workflow["jobs"]["sign-release"]["steps"]


def test_the_verifier_runs_between_the_decode_and_signing():
    """Nothing may sit between the store being written and it being proven."""

    names = [step.get("name") for step in build_release_steps()]
    decode = names.index("Decode release keystore")
    verify_index = names.index("Verify the release keystore before signing")
    signing = names.index("Sign the admitted APK and AAB")

    assert verify_index == decode + 1, names[decode : signing + 1]
    assert signing == verify_index + 1, names[decode : signing + 1]


def test_the_verifier_runs_trusted_code_with_only_the_signing_secrets():
    step = next(
        s for s in build_release_steps() if s.get("name") == "Verify the release keystore before signing"
    )
    assert step["env"] == {
        "KSTOREPWD": "${{ secrets.ANDROID_KEYSTORE_PASSWORD }}",
        "KEY_ALIAS": "${{ secrets.ANDROID_KEY_ALIAS }}",
    }
    assert '"$GITHUB_WORKSPACE/scripts/verify-android-release-keystore.sh"' in step["run"]
    assert "unsigned/" not in step["run"], "the verifier is trusted code, not candidate data"


def test_the_decode_step_is_hardened_and_fails_closed():
    step = next(s for s in build_release_steps() if s.get("name") == "Decode release keystore")
    run = step["run"]
    assert "umask 077" in run, "the store must not be world-readable on a shared runner"
    assert "printf '%s'" in run, "echo mangles a value with backslashes or a leading dash"
    assert "echo \"$KEYSTORE_BASE64\"" not in run
    assert "set -euo pipefail" in run
    # A failing decode must not leave a truncated file behind unremarked.
    assert "is not valid base64" in run
    assert "-s \"$KEYSTORE_FILE\"" in run
    assert "-f \"$KEYSTORE_FILE\"" in run


def test_the_keystore_is_still_cleaned_up():
    steps = build_release_steps()
    cleanup = next(s for s in steps if s.get("name") == "Cleanup keystore")
    assert cleanup["if"] == "always()"
    assert 'rm -rf "$RUNNER_TEMP/keystore"' in cleanup["run"]
    assert [s.get("name") for s in steps][-1] == "Cleanup keystore"


# ── The expected fingerprint is not environment-controllable ──────────


@keytool_required
def test_an_ambient_environment_variable_cannot_relax_the_expected_certificate(store):
    """An earlier step in the signed job writes `$GITHUB_ENV`.

    That makes any environment variable reachable by candidate build code
    running before this check, while a reviewed step's `run` text is byte-pinned
    by the signing-boundary policy. So the override is an argument, and an
    ambient variable of the same name must have no effect at all.
    """

    result = verify(
        store["path"],
        store["password"],
        extra_env={"EXPECTED_CERT_SHA256": store["fingerprint"]},
    )

    assert result.returncode == 1
    assert f"expected SHA-256: {EXPECTED_UPLOAD_CERT_SHA256}" in result.stderr
    assert f"observed SHA-256: {store['fingerprint']}" in result.stderr


def test_the_reviewed_workflow_supplies_no_override():
    step = next(
        s for s in build_release_steps() if s.get("name") == "Verify the release keystore before signing"
    )
    assert "--expect-sha256" not in step["run"]


@keytool_required
@pytest.mark.parametrize(
    "value",
    ["", "abc", "g" * 64, "a" * 63, "a" * 65, "a" * 63 + "\n", "\x1b[31m" + "a" * 58],
)
def test_a_malformed_expected_fingerprint_is_refused_before_it_is_logged(store, value: str):
    result = verify(store["path"], store["password"], expected=value)

    assert result.returncode == 1
    assert "not 64 hexadecimal characters" in result.stderr
    assert "\x1b" not in result.stderr
    assert "observed SHA-256" not in result.stderr, "nothing is compared or printed"


@keytool_required
def test_an_uppercase_expected_fingerprint_is_normalised(store):
    result = verify(store["path"], store["password"], expected=store["fingerprint"].upper())

    assert result.returncode == 0, result.stderr


# ── Deterministic keytool output ──────────────────────────────────────


@keytool_required
def test_a_hostile_default_jvm_locale_cannot_break_the_parser(store):
    """keytool translates its labels; the parser matches English ones.

    `JAVA_TOOL_OPTIONS` sets the JVM's default locale from the environment, so
    this is the real failure mode on a non-English runner. The explicit `-J-D`
    flags come later on the command line and win.
    """

    german = {"JAVA_TOOL_OPTIONS": "-Duser.language=de -Duser.country=DE"}

    # Without the flags, the labels this script parses do not appear at all.
    raw = keytool(
        "-list", "-v", "-keystore", str(store["path"]),
        "-storepass:env", "SILENTSUITE_TEST_STOREPASS",
        password=store["password"],
    )
    localised = subprocess.run(
        ["keytool", "-list", "-v", "-keystore", str(store["path"]),
         "-storepass:env", "SILENTSUITE_TEST_STOREPASS"],
        capture_output=True, text=True,
        env={**os.environ, "SILENTSUITE_TEST_STOREPASS": store["password"], **german},
    )
    assert "Alias name:" in raw.stdout
    if "Alias name:" in localised.stdout:
        pytest.skip("this JDK has no German resource bundle to translate with")

    result = verify(
        store["path"], store["password"], expected=store["fingerprint"], extra_env=german
    )

    assert result.returncode == 0, result.stderr
    assert store["fingerprint"] in result.stdout


def test_the_verifier_pins_keytools_locale():
    source = VERIFIER.read_text(encoding="utf-8")
    assert "-J-Duser.language=en" in source
    assert "-J-Duser.country=US" in source
    assert 'KEYTOOL_LOCALE=(' in source
    assert '"${KEYTOOL_LOCALE[@]}"' in source


# ── Nothing secret reaches any child argument vector ──────────────────


def spawned_commands(source: str) -> set[str]:
    """External commands the verifier can execute, from its executable text."""

    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    found = set()
    for name in ("keytool", "awk", "grep", "sed", "tr", "cut", "printf", "cat", "head"):
        if re.search(rf"(?:^|[|(\s$]){name}\s", code, re.MULTILINE):
            found.add(name)
    return found


@keytool_required
def test_no_process_the_verifier_spawns_receives_the_alias_or_password(store, tmp_path: Path):
    """Every PATH-resolved command it runs is shimmed and its argv recorded.

    `ps` shows arguments to any user on the machine, so "the alias is not an
    argument" has to be observed across every child, not just keytool. Shell
    builtins cannot leak to an argv at all, so PATH coverage is the whole
    external surface.
    """

    names = spawned_commands(VERIFIER.read_text(encoding="utf-8"))
    assert "keytool" in names and "awk" in names, names

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    record = tmp_path / "argv.log"
    shimmed = {}
    for name in sorted(names):
        real = shutil.which(name)
        if real is None:  # a builtin such as printf may have no binary
            continue
        shimmed[name] = real
        (shim_dir / name).write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "{name}" "$@" >> {record}\n'
            f'exec {real} "$@"\n',
            encoding="utf-8",
        )
        (shim_dir / name).chmod(0o755)
    assert "keytool" in shimmed and "awk" in shimmed, shimmed

    result = verify(
        store["path"],
        store["password"],
        expected=store["fingerprint"],
        extra_env={"PATH": f"{shim_dir}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stderr
    recorded = record.read_text(encoding="utf-8")
    assert recorded, "no child process was observed"
    observed = {line for line in recorded.splitlines() if line in shimmed}
    assert "keytool" in observed and "awk" in observed, sorted(observed)
    assert store["password"] not in recorded, "the store password reached a child argv"
    assert ALIAS not in recorded, "the signing alias reached a child argv"
    # The password is named, not spelled; the alias is not mentioned at all.
    assert "-storepass:env" in recorded
    assert "KSTOREPWD" in recorded
    assert "-v alias=" not in recorded


def test_the_alias_is_read_from_the_environment_not_an_argument():
    code = "\n".join(
        line for line in VERIFIER.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert 'ENVIRON["KEY_ALIAS"]' in code
    for argv_leak in ("-v alias=", "-v ALIAS=", "-alias "):
        assert argv_leak not in code, f"the alias must not be passed as {argv_leak.strip()}"
    # Both values must actually be exported for keytool and awk to see them.
    assert "export KSTOREPWD KEY_ALIAS" in code
