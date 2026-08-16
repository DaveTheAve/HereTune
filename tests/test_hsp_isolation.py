from __future__ import annotations

from heretune.analyzer import CorrectionProfile
from heretune.hsp import build as build_hsp, inspect as inspect_hsp
from heretune.server import (
        _diagnostic_correction,
    _heresphere_video_path,
    _hsp_diagnostic_state,
)


def _profile() -> CorrectionProfile:
    p = CorrectionProfile(
        r_gain=1.07,
        g_gain=0.98,
        b_gain=0.95,
        contrast=1.08,
        brightness=0.0,
        gamma=0.93,
        saturation=1.06,
        sharpen=2.55,
        exposure_bias=0.12,
        white_temp=5900.0,
        white_tint=-0.035,
        shadows=0.18,
        midtones=1.12,
        highlights=0.91,
        saturation_r=1.01,
        saturation_g=0.99,
        saturation_b=1.02,
        contrast_r=1.02,
        contrast_g=0.98,
        contrast_b=1.01,
        gamma_r=0.99,
        gamma_g=1.02,
        gamma_b=0.97,
        offset_r=0.008,
        offset_g=-0.004,
        offset_b=0.006,
    )
    native = p.native_image_settings()
    scene = dict(native)
    scene.update({
        "sharpen": 2.8,
        "exposure_bias": 0.2,
        "gamma": (0.98, 1.01, 0.96, 0.90),
        "offset": (0.01, -0.005, 0.004, 0.0),
        "midtones": 1.18,
        "highlights": 0.88,
    })
    p.scene_keyframes = [{"time_seconds": 0.0, "native": scene}]
    return p


def test_vrdiag_neutralizes_only_selected_controls():
    base = _profile()
    diagnostic = _diagnostic_correction(base.dict(), {"midtones"})
    before = base.native_image_settings()
    after = diagnostic.native_image_settings()

    assert after["offset"][:3] == before["offset"][:3]
    assert after["offset"][3] == 0.0
    assert after["midtones"] == 1.0
    assert after["highlights"] == before["highlights"]
    assert after["gamma"] == before["gamma"]
    assert after["gain"] == before["gain"]
    assert after["sharpen"] == before["sharpen"]

    scene = diagnostic.scene_keyframes[0]["native"]
    assert scene["offset"][:3] == (0.01, -0.005, 0.004)
    assert scene["offset"][3] == 0.0
    assert scene["midtones"] == 1.0
    assert scene["highlights"] == 0.88
    assert scene["gamma"] == (0.98, 1.01, 0.96, 0.90)


def test_vrdiag_can_disable_rgb_and_global_components_independently():
    base = _profile()
    diagnostic = _diagnostic_correction(base.dict(), {"gamma_rgb", "offset_rgb"})
    native = diagnostic.native_image_settings()
    assert native["gamma"][:3] == (1.0, 1.0, 1.0)
    assert native["gamma"][3] == base.gamma
    assert native["offset"][:3] == (0.0, 0.0, 0.0)
    assert native["offset"][3] == 0.0


def test_vrdiag_no_scene_keyframes_keeps_whole_video_grade():
    base = _profile()
    diagnostic = _diagnostic_correction(base.dict(), {"scene_keyframes"})
    assert diagnostic.scene_keyframes == []
    parsed = inspect_hsp(build_hsp(
        "diag", "diag", 60.0, diagnostic,
        metadata={"projection": "perspective", "stereo": "mono"},
        source_filename="diag.mp4",
    ))
    assert len(parsed.image_keyframes) == 1
    image = parsed.image_keyframes[0]
    assert abs(image.midtones - base.midtones) < 1e-5
    assert abs(image.highlights - base.highlights) < 1e-5
    assert abs(image.offset[3]) < 1e-5


def test_vrdiag_scene_identity_changes_with_mask():
    baseline = {"id": "abc", "path": r"E:\\VR\\movie_LR.mp4", "layout_override": {}}
    diagnostic = {
        **baseline,
        "layout_override": {"__hsp_image_diagnostic": {"disabled": ["gamma_global"]}},
    }
    assert "/heresphere/video/abc/" in _heresphere_video_path(baseline)
    state = _hsp_diagnostic_state(diagnostic)
    assert state["enabled"] is True
    assert state["disabled"] == ["gamma_global"]
    assert f"/heresphere/diag/{state['token']}/video/abc/" in _heresphere_video_path(diagnostic)
