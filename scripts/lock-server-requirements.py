#!/usr/bin/env python3
"""Attach PyPI hashes to the compiled server requirement pins.

`pip-compile` decides *which* versions the server image installs; this decides
which exact bytes of those versions may enter it.

Three environments install this one file, and each may select a different wheel
for the same pin, so each one's bytes have to be recorded:

  musllinux x86_64   the linux/amd64 release image (Alpine, CPython 3.12)
  musllinux aarch64  the linux/arm64 release image (Alpine, CPython 3.12)
  manylinux x86_64   the glibc CI runner that lints and tests the server

A pure-Python wheel serves all three and is recorded once. Nothing else is:
never an sdist, never an architecture beyond those three, never a `linux_*`
wheel that claims no portability standard at all, and never a wheel CPython 3.12
could not select. A `cp3X-abi3` wheel carries an older interpreter tag but is
stable-ABI, so 3.12 does select it and it is recorded.

The environments cannot reach into each other's hashes. pip's supported tags on
musl never include `manylinux`, and on glibc never include `musllinux`, so the
release image is structurally incapable of selecting the CI wheel even though
its hash is listed in the same file — see
tests/test_self_host_server_image_materials.py, which proves that by resolving
all three sets.

No sdist hash is ever recorded. Combined with `--only-binary=:all:` in
Dockerfile.server that closes the build-from-source path twice over: pip will
not choose an sdist, and could not verify one if it did.

Refuses to write anything if a pin has no acceptable wheel for any of the three
environments, because the alternative — quietly falling back to compiling from
source against live Alpine repositories — is exactly the mutable resolution this
file exists to remove.

Usage:
  scripts/lock-server-requirements.py [--requirements server/requirements.txt]
  scripts/lock-server-requirements.py --check       # verify, write nothing
  scripts/lock-server-requirements.py --report      # classify every hash as JSON
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUIREMENTS = ROOT / "server" / "requirements.txt"
PYPI = "https://pypi.org/pypi"

TARGET_PYTHON = "cp312"
PURE_TAGS = ("py3-none-any", "py2.py3-none-any")

# libc family -> the architectures that family is built for here. musl covers
# both release architectures; glibc covers only the x86_64 CI runner, because
# nothing else in this repository installs these pins on glibc.
TARGET_LIBC = {
    "musllinux": ("x86_64", "aarch64"),
    "manylinux": ("x86_64",),
}
PURE = "pure"

HEADER = """#
# Hash-locked runtime dependency set for the self-host server.
#
# Regenerate the pin list with:
#     pip-compile --output-file=requirements.txt requirements.in/base.txt
# then re-run scripts/lock-server-requirements.py to refresh the hashes.
#
# Three environments install this file and each may select a different wheel for
# the same pin, so each one's exact bytes are recorded here:
#
#   musllinux x86_64   the linux/amd64 release image  (Alpine, CPython 3.12)
#   musllinux aarch64  the linux/arm64 release image  (Alpine, CPython 3.12)
#   manylinux x86_64   the glibc CI runner that lints and tests the server
#
# A pure-Python wheel serves all three and is recorded once. Listing the CI
# wheel here does not widen the release image: pip's supported tags on musl
# never include manylinux, so the Alpine build is structurally incapable of
# selecting it, and the reverse holds on glibc. Nothing else is listed: no
# sdist, no architecture beyond those three, no unportable `linux_*` wheel, and
# no wheel CPython 3.12 could not select — a `cp3X-abi3` wheel carries an older
# interpreter tag but is stable-ABI, so 3.12 selects it and it is recorded.
# A build-from-source fallback therefore cannot be selected even if
# --only-binary were dropped. Dockerfile.server installs this file with
# --require-hashes --only-binary=:all:, so a byte that is not named here cannot
# enter the image.
#
"""


class LockError(RuntimeError):
    """A pin cannot be locked to immutable bytes."""


def classify(filename: str) -> str | None:
    """Return which environment may select this wheel, or None for "no one".

    One of `pure`, `musllinux-x86_64`, `musllinux-aarch64`, `manylinux-x86_64`.
    """

    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return None
    python_tag, abi_tag, platform_tag = parts[-3], parts[-2], parts[-1]

    if f"{python_tag}-{abi_tag}-{platform_tag}" in PURE_TAGS:
        return PURE

    if python_tag == TARGET_PYTHON and abi_tag == TARGET_PYTHON:
        pass
    else:
        # cp3X-abi3 wheels are forward compatible: any X up to the target works.
        match = re.fullmatch(r"cp3(\d+)", python_tag)
        if not (match and abi_tag == "abi3" and int(match.group(1)) <= int(TARGET_PYTHON[3:])):
            return None

    # A wheel may compress several platform tags into one dotted set. Every
    # member has to name the same libc family and architecture, so a wheel
    # cannot be admitted on the strength of one tag and installed on another.
    members = platform_tag.split(".")
    for libc, architectures in TARGET_LIBC.items():
        for architecture in architectures:
            suffix = f"_{architecture}"
            if all(member.startswith(libc) and member.endswith(suffix) for member in members):
                return f"{libc}-{architecture}"
    return None


def parse(path: Path) -> list[tuple[str, list[str]]]:
    """Split the compiled file into (pin, provenance-comment-lines) blocks."""

    entries: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        if raw.startswith("#") and current is None:
            continue
        if raw.startswith((" ", "\t")):
            stripped = raw.strip()
            if stripped.startswith("--hash="):
                continue
            if current is not None:
                current[1].append(stripped)
            continue
        if current is not None:
            entries.append(current)
        current = (raw.strip().rstrip("\\").strip(), [])
    if current is not None:
        entries.append(current)
    return entries


def wheel_hashes(requirement: str) -> list[tuple[str, str, str]]:
    """Every (filename, category, sha256) one of the three environments may use."""

    name, _, version = requirement.partition("==")
    name = name.split("[")[0]
    if not version:
        raise LockError(f"{requirement!r} is not an exact == pin")
    with urllib.request.urlopen(f"{PYPI}/{name}/{version}/json", timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))

    picked: list[tuple[str, str, str]] = []
    for item in payload["urls"]:
        if item["packagetype"] != "bdist_wheel":
            continue
        category = classify(item["filename"])
        if category is not None:
            picked.append((item["filename"], category, item["digests"]["sha256"]))
    picked.sort()

    if not picked:
        raise LockError(
            f"{requirement} publishes no pure-Python wheel and no cp312 wheel for "
            "musllinux x86_64/aarch64 or manylinux x86_64; it cannot be installed "
            "without restoring source builds"
        )
    # A pure wheel serves every environment. Otherwise each environment needs its
    # own, and a missing one would silently become an sdist build at install time.
    if not any(category == PURE for _, category, _ in picked):
        found = {category for _, category, _ in picked}
        required = {
            f"{libc}-{architecture}"
            for libc, architectures in TARGET_LIBC.items()
            for architecture in architectures
        }
        missing = sorted(required - found)
        if missing:
            raise LockError(f"{requirement} publishes no wheel for {', '.join(missing)}")
    return picked


def render(entries: list[tuple[str, list[str]]]) -> str:
    blocks: list[str] = []
    for requirement, comments in entries:
        picked = wheel_hashes(requirement)
        hashes = " \\\n    ".join(f"--hash=sha256:{digest}" for _, _, digest in picked)
        block = f"{requirement} \\\n    {hashes}"
        if comments:
            block += "\n" + "\n".join(f"    {comment}" for comment in comments)
        blocks.append(block)
    return HEADER + "\n".join(blocks) + "\n"


def report(entries: list[tuple[str, list[str]]]) -> str:
    """Classify every recorded hash, so a contract test can assert the split."""

    classified = {
        requirement: [
            {"filename": filename, "category": category, "sha256": digest}
            for filename, category, digest in wheel_hashes(requirement)
        ]
        for requirement, _ in entries
    }
    return json.dumps(classified, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the file is not already exactly what this would write",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="print every recorded hash with the environment that may select it",
    )
    arguments = parser.parse_args()

    entries = parse(arguments.requirements)
    if arguments.report:
        sys.stdout.write(report(entries))
        return 0

    rendered = render(entries)
    if arguments.check:
        if arguments.requirements.read_text(encoding="utf-8") != rendered:
            print(
                f"{arguments.requirements} is not the hash lock its pins imply; "
                "re-run scripts/lock-server-requirements.py",
                file=sys.stderr,
            )
            return 1
        print(f"{arguments.requirements} matches its published wheel hashes")
        return 0

    arguments.requirements.write_text(rendered, encoding="utf-8")
    print(f"locked {arguments.requirements}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LockError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
