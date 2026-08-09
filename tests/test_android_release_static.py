"""Static contracts for Android release artifact naming."""

import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ANDROID_BUILD_WORKFLOW = ROOT / ".github/workflows/build-android.yml"


def release_steps() -> dict[str, dict[str, object]]:
    workflow = yaml.load(
        ANDROID_BUILD_WORKFLOW.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["build-release"]["steps"]
    return {step["name"]: step for step in steps}


def checksum_outputs(run: str) -> dict[str, str]:
    lines = run.splitlines()
    outputs: dict[str, str] = {}
    for index, line in enumerate(lines):
        command = line.strip()
        if not command.startswith("sha256sum "):
            continue
        source_match = re.fullmatch(
            r'sha256sum "([^"\n]+)" ' + re.escape("\\"),
            command,
        )
        if source_match is None or index + 1 >= len(lines):
            raise ValueError(f"invalid sha256sum command: {command}")
        redirect = lines[index + 1].strip()
        target_match = re.fullmatch(r'> "([^"\n]+)"', redirect)
        if target_match is None:
            raise ValueError(f"invalid sha256sum redirect: {redirect}")
        outputs[source_match.group(1)] = target_match.group(1)
    return outputs


@pytest.mark.parametrize(
    "run",
    [
        'sha256sum "app.apk"\n# > "safe.sha256"',
        'sha256sum "app.apk" \\\nprintf "> \\\"safe.sha256\\\""',
        'sha256sum "app.apk" \\\n"safe.sha256"',
    ],
)
def test_checksum_output_parser_rejects_non_redirect_lookalikes(run: str):
    with pytest.raises(ValueError):
        checksum_outputs(run)


def test_android_release_checksum_generation_matches_uploads():
    steps = release_steps()
    tag = "${{ github.ref_name }}"
    apk = f"silentsuite-android-{tag}.apk"
    aab = f"silentsuite-android-{tag}.aab"
    installer_checksum = f"silentsuite-android-{tag}-installer.sha256"
    bundle_checksum = f"silentsuite-android-{tag}-bundle.sha256"

    rename_run = steps["Rename Android artifacts for release"]["run"]
    assert checksum_outputs(rename_run) == {
        apk: installer_checksum,
        aab: bundle_checksum,
    }

    uploaded = steps["Attach Android artifacts to umbrella GitHub Release"]["with"][
        "files"
    ].splitlines()
    assert uploaded == [
        f"android/app/build/outputs/apk/release/{apk}",
        f"android/app/build/outputs/apk/release/{installer_checksum}",
        f"android/app/build/outputs/bundle/release/{aab}",
        f"android/app/build/outputs/bundle/release/{bundle_checksum}",
    ]


def test_android_release_checksum_sidecars_do_not_match_orion_apk_filter():
    steps = release_steps()
    rename_run = steps["Rename Android artifacts for release"]["run"]
    sidecars = checksum_outputs(rename_run).values()

    def looks_installable(name: str) -> bool:
        lowered_name = name.lower()
        lowered_url = f"https://github.com/example/releases/{name}".lower()
        return (
            lowered_name.endswith(".apk")
            or ".apk" in lowered_url
            or lowered_name == "apk"
            or "apk" in lowered_name
        )

    assert all(not looks_installable(sidecar) for sidecar in sidecars)
