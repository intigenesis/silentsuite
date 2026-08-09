"""Static contracts for Android release artifact naming."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANDROID_BUILD_WORKFLOW = ROOT / ".github/workflows/build-android.yml"


def test_android_release_checksum_assets_do_not_look_installable():
    workflow = ANDROID_BUILD_WORKFLOW.read_text(encoding="utf-8")

    assert 'silentsuite-android-${{ github.ref_name }}-installer.sha256' in workflow
    assert 'silentsuite-android-${{ github.ref_name }}-bundle.sha256' in workflow
    assert 'silentsuite-android-${{ github.ref_name }}.apk.sha256' not in workflow
    assert 'silentsuite-android-${{ github.ref_name }}.aab.sha256' not in workflow
