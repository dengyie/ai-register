"""Regression: repo-root turnstilePatch must stay MV3 (MV2 crashes Chrome 149+)."""
import json
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MANIFEST = _ROOT / "turnstilePatch" / "manifest.json"


def test_root_turnstile_patch_is_mv3():
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3, "MV2 crashes the renderer on Chrome 149+"
    assert "<all_urls>" in manifest["host_permissions"], "MV3 needs host_permissions for all_urls"
    assert manifest["content_scripts"][0]["all_frames"] is True
