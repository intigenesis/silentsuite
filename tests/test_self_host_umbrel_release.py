"""Effective Umbrel stack contracts exercised without starting containers."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "self-host" / "umbrel" / "silentsuite"
RENDERER = ROOT / "scripts" / "umbrel-candidate-effective-compose.py"
SOURCE_SHA = "a" * 40
WEB_IMAGE = f"127.0.0.1:5000/silentsuite-web:dev-{SOURCE_SHA}"
SERVER_IMAGE = f"127.0.0.1:5000/silentsuite-server:dev-{SOURCE_SHA}"
EXPECTED_IMAGES = {"web": WEB_IMAGE, "init": SERVER_IMAGE, "server": SERVER_IMAGE}
POISONED_IMAGES = {
    service: f"example.invalid/poison-{service}:old@sha256:{number * 64}"
    for service, number in (("web", "1"), ("init", "2"), ("server", "3"))
}


@pytest.fixture
def candidate(tmp_path):
    package = tmp_path / "candidate"
    shutil.copytree(PACKAGE, package, symlinks=True)
    # Referenced hostile inputs exist, so rejection cannot be a missing-file error.
    (package / "candidate.env").write_text("FIXTURE_ONLY=value\n", encoding="utf-8")
    (package / "compose-base.yml").write_text(
        "services:\n  web:\n    image: example.invalid/base:fixture\n",
        encoding="utf-8",
    )
    document = yaml.safe_load((package / "docker-compose.yml").read_text(encoding="utf-8"))
    return package, document


def render(candidate):
    package, document = candidate
    source = package / "docker-compose.yml"
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    original = source.read_bytes()
    destination = package.parent / "effective.yml"
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "APP_DATA_DIR": str(package),
        "APP_PASSWORD": "4" * 64,
        "APP_SILENTSUITE_REGISTRATION_SIGNING": "5" * 64,
        "APP_SILENTSUITE_DB_PASSWORD": "6" * 64,
        "APP_SILENTSUITE_WEB_IMAGE": WEB_IMAGE,
        "APP_SILENTSUITE_SERVER_IMAGE": SERVER_IMAGE,
    }
    # Match the current smoke caller: source, destination and router port only.
    result = subprocess.run(
        [sys.executable, str(RENDERER), str(source), str(destination), "18080"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert "ModuleNotFoundError" not in result.stderr, result.stderr
    assert "ImportError" not in result.stderr, result.stderr
    assert "unrecognized arguments" not in result.stderr, result.stderr
    assert source.read_bytes() == original, "renderer modified the candidate package"
    return result, destination


def test_valid_canonical_package_preserves_the_smoke_stack(candidate):
    result, destination = render(candidate)
    assert result.returncode == 0, result.stderr
    services = yaml.safe_load(destination.read_text(encoding="utf-8"))["services"]
    assert set(services) == {"router", "web", "init", "server", "postgres"}
    assert services["router"]["ports"] == ["127.0.0.1:18080:8080"]
    assert all(
        service["container_name"] == f"silentsuite_{name}_1"
        for name, service in services.items()
    )
    assert all("ports" not in services[name] for name in services if name != "router")
    assert services["init"]["user"] == "0:0"
    assert services["init"]["network_mode"] == "none"
    assert services["init"]["restart"] == "no"
    assert services["server"]["volumes"] == candidate[1]["services"]["server"]["volumes"]
    assert services["postgres"]["image"] == candidate[1]["services"]["postgres"]["image"]


@pytest.mark.parametrize("literal_pins", [False, True], ids=["canonical", "poisoned-pins"])
def test_smoke_uses_exact_just_built_image_pair(candidate, literal_pins):
    if literal_pins:
        for service, image in POISONED_IMAGES.items():
            candidate[1]["services"][service]["image"] = image
    result, destination = render(candidate)
    assert result.returncode == 0, result.stderr
    services = yaml.safe_load(destination.read_text(encoding="utf-8"))["services"]
    actual = {service: services[service]["image"] for service in EXPECTED_IMAGES}
    assert actual == EXPECTED_IMAGES, (
        "smoke must use its supplied just-built images, never canonical selectors; "
        f"got {actual}"
    )
    assert services["init"]["image"] == services["server"]["image"]
    rendered = destination.read_text(encoding="utf-8")
    assert all(image not in rendered for image in POISONED_IMAGES.values())


@pytest.mark.parametrize(
    "service,patch",
    [
        pytest.param("server", {"volumes": ["/:/data:ro"]}, id="host-root-bind"),
        pytest.param(
            "server",
            {"volumes": ["/var/run/docker.sock:/var/run/docker.sock"]},
            id="docker-socket-bind",
        ),
        pytest.param(
            "server",
            {"volumes": [{"type": "bind", "source": "/run/docker.sock", "target": "/socket"}]},
            id="docker-socket-long-bind",
        ),
        pytest.param("web", {"privileged": True}, id="privileged"),
        pytest.param("web", {"network_mode": "host"}, id="host-network"),
        pytest.param("web", {"pid": "host"}, id="host-pid"),
        pytest.param("web", {"ipc": "host"}, id="host-ipc"),
        pytest.param("web", {"uts": "host"}, id="host-uts"),
        pytest.param("web", {"userns_mode": "host"}, id="host-userns"),
        pytest.param("web", {"env_file": ["candidate.env"]}, id="environment-file"),
        pytest.param("web", {"devices": ["/dev/kmsg:/dev/kmsg"]}, id="device"),
        pytest.param("web", {"cap_add": ["SYS_ADMIN"]}, id="added-capability"),
        pytest.param("web", {"build": {"context": "."}}, id="candidate-build"),
        pytest.param(
            "web",
            {"extends": {"file": "compose-base.yml", "service": "web"}},
            id="compose-extends",
        ),
        pytest.param(None, {"include": ["compose-base.yml"]}, id="compose-include"),
        pytest.param(
            None,
            {"networks": {"default": {"external": True, "name": "outside"}}},
            id="external-network",
        ),
    ],
)
def test_renderer_rejects_candidate_host_access(candidate, service, patch):
    document = candidate[1]
    target = document if service is None else document["services"][service]
    target.update(patch)
    result, destination = render(candidate)
    assert result.returncode != 0, f"unsafe Compose input was accepted: {patch}"
    assert not destination.exists(), "rejected input must not produce runnable Compose"


@pytest.mark.parametrize(
    "source",
    [
        "${APP_DATA_DIR}/data/../../outside",
        "${APP_DATA_DIR}/data/../private",
        "${APP_DATA_DIR}/data-other/server",
    ],
    ids=["parent-escape", "outside-data-root", "data-prefix-lookalike"],
)
def test_renderer_rejects_bind_path_escape(candidate, source):
    package, document = candidate
    host_path = Path(source.replace("${APP_DATA_DIR}", str(package)))
    host_path.mkdir(parents=True, exist_ok=True)
    document["services"]["server"]["volumes"][0] = f"{source}:/data"
    result, destination = render(candidate)
    assert result.returncode != 0, f"escaping bind source was accepted: {source}"
    assert not destination.exists()


@pytest.mark.parametrize("kind", ["directory", "bootstrap-file"])
def test_renderer_rejects_bind_source_symlink_escape(candidate, kind):
    package, _ = candidate
    outside = package.parent / "outside"
    outside.mkdir()
    if kind == "directory":
        link = package / "data" / "server"
        shutil.rmtree(link)
        link.symlink_to(outside, target_is_directory=True)
    else:
        target = outside / "init.sh"
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        link = package / "data" / "bootstrap" / "init.sh"
        link.unlink()
        link.symlink_to(target)
    result, destination = render(candidate)
    assert result.returncode != 0, f"escaping {kind} symlink was accepted"
    assert not destination.exists()
