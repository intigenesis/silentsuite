#!/usr/bin/env python3
"""Prove the built server image contains exactly the hash-locked dependency set.

Runs *inside* the image, on the architecture that built it, so it answers the
two questions a static reading of Dockerfile.server cannot:

  1. does every hash-locked distribution import — including the native ones,
     whose musllinux wheels have to vendor libpq, libffi and libsodium now that
     the image installs no Alpine packages at all;
  2. is the installed set exactly the pinned set — no extra distribution
     resolved at build time, no pinned distribution missing or at a different
     version.

Usage, from the repository root of a checkout that also has the image:

  docker run --rm --entrypoint python3 \\
    -v "$PWD/scripts/check-server-image-dependencies.py:/check.py:ro" \\
    -v "$PWD/server/requirements.txt:/requirements.txt:ro" \\
    <image> /check.py --requirements /requirements.txt
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import re
import sys
from pathlib import Path

# Distributions the base image ships. Everything else must be pinned.
BASE_IMAGE_DISTRIBUTIONS = {"pip", "setuptools", "wheel"}

# Distribution name -> the module whose import actually loads native code.
NATIVE_IMPORTS = {
    "cffi": "cffi",
    "httptools": "httptools",
    "msgpack": "msgpack",
    "psycopg2-binary": "psycopg2",
    "pydantic-core": "pydantic_core",
    "pynacl": "nacl.bindings",
    "pyyaml": "yaml",
    "uvloop": "uvloop",
    "watchfiles": "watchfiles",
    "websockets": "websockets",
}
REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)(?:\[[^\]]*\])?==(?P<version>[^\s\\]+)")


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(path: Path) -> dict[str, str]:
    pinned: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", " ", "\t")):
            continue
        match = REQUIREMENT.match(line.strip())
        if match is None:
            raise SystemExit(f"requirements line is not an exact pin: {line!r}")
        pinned[normalise(match.group("name"))] = match.group("version")
    if not pinned:
        raise SystemExit("no pinned requirements were found")
    return pinned


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=Path("/requirements.txt"))
    arguments = parser.parse_args()

    pinned = parse_requirements(arguments.requirements)
    installed = {
        normalise(dist.metadata["Name"]): dist.version
        for dist in importlib.metadata.distributions()
        if dist.metadata["Name"]
    }

    problems: list[str] = []
    for name, version in sorted(pinned.items()):
        if name not in installed:
            problems.append(f"{name} is pinned but not installed")
        elif installed[name] != version:
            problems.append(f"{name} is {installed[name]}, pinned at {version}")

    unexpected = sorted(set(installed) - set(pinned) - BASE_IMAGE_DISTRIBUTIONS)
    if unexpected:
        problems.append(f"unpinned distributions were installed: {unexpected}")

    for distribution, module in sorted(NATIVE_IMPORTS.items()):
        if normalise(distribution) not in pinned:
            problems.append(f"{distribution} is no longer pinned; update NATIVE_IMPORTS")
            continue
        try:
            importlib.import_module(module)
        except Exception as error:  # noqa: BLE001 - any import failure is fatal here
            problems.append(f"{module} ({distribution}) failed to import: {error!r}")

    # The application itself has to load, not just its dependencies.
    try:
        importlib.import_module("django")
        importlib.import_module("fastapi")
    except Exception as error:  # noqa: BLE001
        problems.append(f"the application framework failed to import: {error!r}")

    if problems:
        print("Server image dependency check failed:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1

    print(
        f"Server image dependency check passed on {sys.platform} "
        f"{sys.implementation.name} {sys.version.split()[0]}: "
        f"{len(pinned)} pinned distributions installed, "
        f"{len(NATIVE_IMPORTS)} native extensions imported"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
