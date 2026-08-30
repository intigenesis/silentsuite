#!/usr/bin/env bash
set -euo pipefail

# Prove the decoded Android release keystore is the one this project signs with,
# before Gradle is allowed to open it.
#
# v0.5.4-beta's signed build decoded the environment secret and then died inside
# `:app:packageRelease` with `KeytoolException: Failed to read key from
# .../silentsuite-release.jks: Tag number over 30 is not supported` — a DER
# parse failure, i.e. the bytes on disk were not a readable JKS at all. That was
# discovered after twenty minutes of native build work, from a Gradle stack
# trace, with no statement of what the store actually contained. This runs
# immediately after the decode and answers the question directly.
#
# What it proves, all fail-closed:
#   1. the keystore path is a non-empty regular file;
#   2. the store opens with the configured password;
#   3. the configured alias exists in it;
#   4. that alias is a PrivateKeyEntry, not a trusted certificate;
#   5. its leaf certificate is exactly the reviewed developer upload key.
#
# Secret handling, deliberate:
#   * the store password is read by keytool from the environment through
#     `-storepass:env`, so it is never an argument and never on disk;
#   * the alias is read by awk from ENVIRON, and matched in this script, so it
#     never reaches any child process's argument vector either — `ps` on a
#     shared runner shows arguments, not environments;
#   * keytool's own output is captured into a shell variable, never written to
#     disk and never echoed — recognised failures are reported as fixed labels;
#   * the only values printed are the expected and observed SHA-256 certificate
#     fingerprints, which are public: they are in every published APK.
#
# Why the expected fingerprint is an argument and not an environment variable:
# an earlier step in the signed job writes to `$GITHUB_ENV`, so a variable is
# reachable by candidate build code running before this check. A step's `run`
# text is byte-pinned by scripts/check-android-signing-boundary.py, so an
# argument is not. The reviewed workflow passes none, and the compiled-in
# constant is used.
#
# Usage:
#   scripts/verify-android-release-keystore.sh [--expect-sha256 <64-hex>]
#
# Environment:
#   KEYSTORE_PATH  path to the decoded store (not secret)
#   KSTOREPWD      store password (never printed, never in any argv)
#   KEY_ALIAS      expected alias (never printed, never in any argv)

# The developer upload certificate. Changing this is changing which key the
# project ships under, so it is a reviewed constant, pinned by
# scripts/check-android-signing-boundary.py.
EXPECTED_CERT_SHA256="8035a4ff1511e2045c579c905d26e93af6009b239e741ef78542ae04e7a7ca79"

# Deterministic, machine-readable keytool output. Without these the labels this
# script parses ("Alias name:", "Entry type:", "SHA256:") are translated — a
# German-locale runner prints "Keystore-Typ" and nothing matches. Both flags are
# ordinary non-secret JVM options.
KEYTOOL_LOCALE=(-J-Duser.language=en -J-Duser.country=US)

while [ $# -gt 0 ]; do
  case "$1" in
    --expect-sha256) EXPECTED_CERT_SHA256="${2:-}"; shift 2 ;;
    *) echo "ERROR: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

: "${KEYSTORE_PATH:?KEYSTORE_PATH must be set}"
: "${KSTOREPWD:?KSTOREPWD must be set}"
: "${KEY_ALIAS:?KEY_ALIAS must be set}"

# keytool reads the password from the environment and awk reads the alias from
# it; both must therefore actually be exported, not merely set in this shell.
export KSTOREPWD KEY_ALIAS

command -v keytool >/dev/null 2>&1 || {
  echo "ERROR: keytool is required to verify the release keystore" >&2
  exit 2
}

refuse() {
  echo "Refusing to sign: $*" >&2
  exit 1
}

# Normalise then validate, before the value can reach a log line. An expected
# fingerprint carrying control characters or the wrong length is a broken
# invocation, not something to compare against and print.
EXPECTED_CERT_SHA256="$(printf '%s' "$EXPECTED_CERT_SHA256" | tr '[:upper:]' '[:lower:]')"
printf '%s' "$EXPECTED_CERT_SHA256" | grep -Eq '^[0-9a-f]{64}$' \
  || refuse "the expected certificate fingerprint is not 64 hexadecimal characters"

# ── 1. The file itself ────────────────────────────────────────────────

[ -f "$KEYSTORE_PATH" ] || refuse "the keystore path is not a regular file"
[ -s "$KEYSTORE_PATH" ] || refuse "the decoded keystore is empty"

# ── 2. The store opens ────────────────────────────────────────────────
#
# Captured, never echoed. keytool does not print the password back, but its
# diagnostics are still mapped to fixed labels rather than forwarded verbatim,
# so no future keytool release can surprise this log.

LISTING=""
if ! LISTING="$(keytool "${KEYTOOL_LOCALE[@]}" -list -v \
    -keystore "$KEYSTORE_PATH" -storepass:env KSTOREPWD 2>&1)"; then
  case "$LISTING" in
    *"password was incorrect"*|*"Keystore was tampered with"*)
      refuse "the store password was rejected by the keystore" ;;
    *"Tag number over"*|*"Invalid keystore format"*|*"DerInputStream"*|*"Short read"*\
    |*"EOFException"*|*"not a valid"*|*"Unrecognized keystore"*)
      # This is the v0.5.4-beta shape: a truncated or otherwise mangled base64
      # decode produces bytes that are not DER, and Gradle only says so from
      # inside :app:packageRelease twenty minutes later.
      refuse "the decoded bytes are not a readable keystore (DER/format error)" ;;
    *)
      refuse "keytool could not read the keystore" ;;
  esac
fi

# ── 3/4. The configured alias, and what kind of entry it is ───────────
#
# `keytool -alias "$KEY_ALIAS"` and `awk -v alias="$KEY_ALIAS"` would both put
# the alias into a child process's argument vector. Reading it from ENVIRON
# keeps it out of every argv while still matching exactly. Only the matching
# block is examined; no alias is ever printed.

ENTRY="$(
  printf '%s\n' "$LISTING" | awk '
    BEGIN { alias = ENVIRON["KEY_ALIAS"] }
    /^Alias name: / {
      capturing = (substr($0, 13) == alias)
      if (capturing) { print; next }
    }
    /^Alias name: / { next }
    capturing { print }
  '
)"

[ -n "$ENTRY" ] || refuse "the configured signing alias is not present in the keystore"

case "$ENTRY" in
  *"Entry type: PrivateKeyEntry"*) ;;
  *) refuse "the configured signing alias is not a PrivateKeyEntry" ;;
esac

# ── 5. The leaf certificate ───────────────────────────────────────────
#
# The first SHA256 fingerprint in the entry is Certificate[1], the signing leaf.

OBSERVED="$(
  printf '%s\n' "$ENTRY" \
    | grep -m1 -oE 'SHA256:[[:space:]]*([0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}' \
    | tr -d ' \t' \
    | sed 's/^SHA256://' \
    | tr -d ':' \
    | tr '[:upper:]' '[:lower:]' || true
)"

printf '%s' "$OBSERVED" | grep -Eq '^[0-9a-f]{64}$' \
  || refuse "no SHA-256 certificate fingerprint was found for the signing alias"

if [ "$OBSERVED" != "$EXPECTED_CERT_SHA256" ]; then
  echo "Refusing to sign: the signing certificate is not the reviewed upload key" >&2
  echo "  expected SHA-256: ${EXPECTED_CERT_SHA256}" >&2
  echo "  observed SHA-256: ${OBSERVED}" >&2
  exit 1
fi

echo "Release keystore verified before Gradle:"
echo "  store opens, configured alias present, entry is a PrivateKeyEntry"
echo "  certificate SHA-256: ${OBSERVED}"
