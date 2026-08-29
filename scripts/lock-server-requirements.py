#!/usr/bin/env python3
"""Attach PyPI hashes to the compiled server requirement pins.

`pip-compile` decides *which* versions the server image installs; this decides
which exact bytes of those versions may enter it. For every pin it records the
sha256 of each distribution CPython 3.12 could select on the two architectures
the release image is built for — the pure-Python wheel where one exists, and the
cp312/abi3 musllinux wheels for x86_64 and aarch64 otherwise.

No sdist hash is ever recorded. Combined with `--only-binary=:all:` in
Dockerfile.server that closes the build-from-source path twice over: pip will
not choose an sdist, and could not verify one if it did.

Refuses to write anything if a pin has no acceptable wheel on either
architecture, because the alternative — quietly falling back to compiling from
source against live Alpine repositories — is exactly the mutable resolution
this file exists to remove.

Usage:
  scripts/lock-server-requirements.py [--requirements server/requirements.txt]
  scripts/lock-server-requirements.py --check       # verify, write nothing
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
TARGET_PLATFORMS = ("x86_64", "aarch64")
PURE_TAGS = ("py3-none-any", "py2.py3-none-any")

HEADER = """#
# Hash-locked runtime dependency set for the self-host server image.
#
# Regenerate the pin list with:
#     pip-compile --output-file=requirements.txt requirements.in/base.txt
# then re-run scripts/lock-server-requirements.py to refresh the hashes.
#
# Every entry carries the sha256 of each distribution that CPython 3.12 on
# linux/amd64 and linux/arm64 musl may select: the pure-Python wheel where one
# exists, and the cp312/abi3 musllinux wheels for x86_64 and aarch64 otherwise.
# No sdist hash is listed, so a build-from-source fallback cannot be selected
# even if --only-binary were dropped. Dockerfile.server installs this file with
# --require-hashes --only-binary=:all:, so a byte that is not named here cannot
# enter the release image.
#
"""


class LockError(RuntimeError):
    """A pin cannot be locked to immutable bytes."""


def acceptable(filename: str) -> bool:
    stem = filename[:-4] if filename.endswith(".whl") else filename
    parts = stem.split("-")
    if len(parts) < 5:
        return False
    python_tag, abi_tag, platform_tag = parts[-3], parts[-2], parts[-1]
    if f"{python_tag}-{abi_tag}-{platform_tag}" in PURE_TAGS:
        return True
    if "musllinux" not in platform_tag:
        return False
    if not any(platform_tag.endswith(f"_{arch}") for arch in TARGET_PLATFORMS):
        return False
    if python_tag == TARGET_PYTHON and abi_tag == TARGET_PYTHON:
        return True
    # cp3X-abi3 wheels are forward compatible: any X up to the target works.
    match = re.fullmatch(r"cp3(\d+)", python_tag)
    return bool(match and abi_tag == "abi3" and int(match.group(1)) <= int(TARGET_PYTHON[3:]))


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


def wheel_hashes(requirement: str) -> list[tuple[str, str]]:
    name, _, version = requirement.partition("==")
    name = name.split("[")[0]
    if not version:
        raise LockError(f"{requirement!r} is not an exact == pin")
    with urllib.request.urlopen(f"{PYPI}/{name}/{version}/json", timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    picked = sorted(
        (item["filename"], item["digests"]["sha256"])
        for item in payload["urls"]
        if item["packagetype"] == "bdist_wheel" and acceptable(item["filename"])
    )
    if not picked:
        raise LockError(
            f"{requirement} publishes no pure-Python or cp312 musllinux wheel for "
            f"{', '.join(TARGET_PLATFORMS)}; the image cannot install it without "
            "restoring source builds"
        )
    return picked


def render(entries: list[tuple[str, list[str]]]) -> str:
    blocks: list[str] = []
    for requirement, comments in entries:
        picked = wheel_hashes(requirement)
        hashes = " \\\n    ".join(f"--hash=sha256:{digest}" for _, digest in picked)
        block = f"{requirement} \\\n    {hashes}"
        if comments:
            block += "\n" + "\n".join(f"    {comment}" for comment in comments)
        blocks.append(block)
    return HEADER + "\n".join(blocks) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the file is not already exactly what this would write",
    )
    arguments = parser.parse_args()

    rendered = render(parse(arguments.requirements))
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
