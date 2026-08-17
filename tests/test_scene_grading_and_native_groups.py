from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from heretune.analyzer import CorrectionProfile, _derive_scene_keyframes
from heretune.config import Config, public_config
from heretune.hsp import TICKS_PER_SECOND, build, inspect
from heretune.library import now, sid
from heretune.server import _scaled_correction, create_app
from heretune.vrformat import VISUAL_CALIBRATION_VERSION, _horizon_evidence
from heretune.worker import native_calibration_metadata


def _native(
    *,
    gain=(1.0, 1.0, 1.0, 1.0),
    contrast=(1.0, 1.0, 1.0, 1.0),
    saturation=(1.0, 1.0, 1.0, 1.0),
    gamma=(1.0, 1.0, 1.0, 1.0),
    offset=(0.0, 0.0, 0.0, 0.0),
    exposure=0.0,
):
    return {
        "sharpen": 2.0,
        "exposure_bias": exposure,
        "white_temp": 6500.0,
        "white_tint": 0.0,
        "saturation": saturation,
        "contrast": contrast,
        "gamma": gamma,
        "gain": gain,
        "offset": offset,
        "shadows": 0.0,
        "midtones": 1.0,
        "highlights": 1.0,
    }


def _app_with_ready_video(tmp_path: Path):
    library = tmp_path / "movies"
    library.mkdir()
    source = library / "Advanced_Grade_LR.mp4"
    source.write_bytes(b"x" * 2048)
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.library.scan_interval_seconds = 3600
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    video_id = sid(source.name)
    stamp = now()
    metadata = {
        "duration": 600.0,
        "width": 3840,
        "height": 1920,
        "projection": "perspective",
        "stereo": "sbs",
        "fov": 180.0,
        "lens": "Linear",
    }
    correction = CorrectionProfile(
        r_gain=1.02,
        g_gain=0.99,
        b_gain=1.01,
        contrast=1.04,
        scene_keyframes=[
            {"time_seconds": 0.0, "confidence": 0.9, "change_score": 0.0, "sample_count": 5, "native": _native()},
            {"time_seconds": 240.0, "confidence": 0.84, "change_score": 1.7, "sample_count": 5,
             "native": _native(contrast=(0.99, 1.01, 1.02, 1.06), exposure=0.08)},
        ],
    )
    blob = build(video_id, source.stem, 600.0, correction, metadata=metadata, source_filename=source.name)
    st = source.stat()
    app.state.db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,
           analysis_json,auto_hsp,auto_hsp_version,date_added,last_seen,last_analyzed)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            video_id, str(source), source.name, source.stem, st.st_size, st.st_mtime_ns,
            "ready", json.dumps(metadata), json.dumps(correction.dict()), blob, 7,
            stamp, stamp, stamp,
        ),
    )
    return app, cfg, video_id


def test_sparse_scene_hsp_and_per_channel_vectors_roundtrip():
    correction = CorrectionProfile(
        r_gain=1.03,
        g_gain=0.98,
        b_gain=1.01,
        saturation_r=0.985,
        saturation_g=1.006,
        saturation_b=1.013,
        contrast_r=0.978,
        contrast_g=1.012,
        contrast_b=1.022,
        gamma_r=1.018,
        gamma_g=0.994,
        gamma_b=0.982,
        offset_r=0.008,
        offset_g=-0.004,
        offset_b=-0.006,
        scene_keyframes=[
            {
                "time_seconds": 0.0,
                "confidence": 0.94,
                "change_score": 0.0,
                "sample_count": 8,
                "native": _native(
                    gain=(1.03, 0.98, 1.01, 1.0),
                    contrast=(0.978, 1.012, 1.022, 1.04),
                    saturation=(0.985, 1.006, 1.013, 1.02),
                    gamma=(1.018, 0.994, 0.982, 0.97),
                    offset=(0.008, -0.004, -0.006, -0.01),
                ),
            },
            {
                "time_seconds": 123.5,
                "confidence": 0.88,
                "change_score": 1.65,
                "sample_count": 6,
                "native": _native(
                    gain=(0.99, 1.01, 1.02, 1.0),
                    contrast=(1.01, 0.99, 1.02, 1.08),
                    saturation=(1.00, 0.99, 1.01, 0.96),
                    gamma=(0.99, 1.01, 1.00, 1.04),
                    offset=(-0.002, 0.003, 0.006, 0.018),
                    exposure=0.11,
                ),
            },
            {
                "time_seconds": 418.0,
                "confidence": 0.81,
                "change_score": 1.42,
                "sample_count": 7,
                "native": _native(contrast=(1.0, 1.0, 1.0, 0.97), exposure=-0.07),
            },
        ],
    )
    parsed = inspect(build(
        "vid", "Scene grade", 900.0, correction,
        metadata={"projection": "perspective", "stereo": "sbs"},
        source_filename="Scene_Grade_LR.mp4",
    ))
    assert parsed.array_counts["image"] == 3
    assert [item.time_ticks for item in parsed.image_keyframes] == [
        0,
        round(123.5 * TICKS_PER_SECOND),
        round(418.0 * TICKS_PER_SECOND),
    ]
    first, second, third = parsed.image_keyframes
    assert first.contrast == pytest.approx((0.978, 1.012, 1.022, 1.04), abs=1e-5)
    assert first.gamma == pytest.approx((1.018, 0.994, 0.982, 0.97), abs=1e-5)
    assert first.offset == pytest.approx((0.008, -0.004, -0.006, -0.01), abs=1e-5)
    assert second.exposure_bias == pytest.approx(0.11, abs=1e-5)
    assert third.contrast[3] == pytest.approx(0.97, abs=1e-5)


def test_scene_detector_uses_sustained_change_hysteresis_and_cap():
    stats = []
    for index in range(18):
        if index < 9:
            p50, p05, p95, sat, means = 0.20, 0.03, 0.66, 0.18, (0.23, 0.20, 0.18)
        else:
            p50, p05, p95, sat, means = 0.55, 0.08, 0.94, 0.31, (0.50, 0.54, 0.60)
        stats.append({
            "_timestamp": index * 45.0,
            "p50": p50,
            "p05": p05,
            "p95": p95,
            "median_sat": sat,
            "channel_means": means,
        })
    profile = CorrectionProfile(
        luma_p50=0.36,
        luma_p05=0.05,
        luma_p95=0.82,
        median_saturation=0.24,
        tone_confidence=0.9,
    )
    _derive_scene_keyframes(
        profile,
        stats,
        810.0,
        enabled=True,
        minimum_seconds=90.0,
        change_threshold=1.0,
        max_keyframes=8,
        scene_strength=0.8,
        min_confidence=0.4,
    )
    # One sustained transition produces two stable segments, not a cluster of
    # adjacent micro-keyframes around the same cut.
    assert len(profile.scene_keyframes) == 2
    assert profile.scene_keyframes[0]["time_seconds"] == 0.0
    assert profile.scene_keyframes[1]["time_seconds"] >= 360.0
    assert profile.scene_grade_confidence >= 0.9
    assert profile.scene_keyframes[0]["native"] != profile.scene_keyframes[1]["native"]



def test_scene_detector_accepts_real_numpy_channel_means():
    """Regression for ambiguous NumPy truth-value handling in scene statistics."""
    stats = []
    for index in range(18):
        if index < 9:
            p50, p05, p95, sat = 0.20, 0.03, 0.66, 0.18
            means = np.asarray((0.23, 0.20, 0.18), dtype=np.float64)
        else:
            p50, p05, p95, sat = 0.55, 0.08, 0.94, 0.31
            means = np.asarray((0.50, 0.54, 0.60), dtype=np.float64)
        stats.append({
            "_timestamp": index * 45.0,
            "p50": p50,
            "p05": p05,
            "p95": p95,
            "median_sat": sat,
            "channel_means": means,
        })

    profile = CorrectionProfile(
        luma_p50=0.36, luma_p05=0.05, luma_p95=0.82,
        median_saturation=0.24, tone_confidence=0.9,
    )
    _derive_scene_keyframes(
        profile, stats, 810.0, enabled=True, minimum_seconds=90.0,
        change_threshold=1.0, max_keyframes=8, scene_strength=0.8,
        min_confidence=0.4,
    )
    assert len(profile.scene_keyframes) == 2
    assert profile.scene_keyframes[0]["time_seconds"] == 0.0


def test_all_advanced_hsp_groups_roundtrip():
    metadata = {
        "projection": "perspective",
        "stereo": "sbs",
        "zoom": (1.12, 1.08),
        "pan": (-0.04, 0.03),
        "alignment_position": (0.10, 6.6, -0.15),
        "alignment_rotation": (0.35, -0.20, 0.45),
        "stitch_shift": (0.01, -0.012, 0.006, -0.005),
        "stitch_scale": (1.01, 0.99, 1.015, 0.985),
        "stitch_shear": (0.004, -0.003, 0.002, -0.001),
        "stitch_flare": (0.003, 0.002, -0.002, -0.003),
        "stitch_slant": (-0.004, 0.003, -0.002, 0.001),
        "orientation_rotation": (0.0, 0.0, -1.4),
        "autofocus_rotation": (0.1, -0.2, 0.3),
        "autofocus_focal_distance": 175.0,
        "autofocus_min_distance": 22.0,
        "autofocus_max_distance": 840.0,
        "autofocus_transition_speed": 1.4,
        "audio_time_offset": -0.125,
        "audio_volume_multiplier": 1.18,
        "sync_start_offset_seconds": 0.08,
        "sync_drift_seconds_per_hour": -0.17,
    }
    parsed = inspect(build(
        "vid", "Advanced", 600.0, CorrectionProfile(), metadata=metadata,
        source_filename="Advanced_LR.mp4",
    ))
    fmt = parsed.format_keyframes[0]
    assert fmt.zoom == pytest.approx((1.12, 1.08), abs=1e-5)
    assert fmt.pan == pytest.approx((-0.04, 0.03), abs=1e-5)
    assert parsed.alignment_keyframes[0].position == pytest.approx((0.10, 6.6, -0.15), abs=1e-5)
    assert parsed.alignment_keyframes[0].rotation == pytest.approx((0.35, -0.20, 0.45), abs=1e-5)
    stitch = parsed.stitch_keyframes[0]
    assert stitch.shift == pytest.approx(metadata["stitch_shift"], abs=1e-5)
    assert stitch.scale == pytest.approx(metadata["stitch_scale"], abs=1e-5)
    assert stitch.shear == pytest.approx(metadata["stitch_shear"], abs=1e-5)
    assert stitch.flare == pytest.approx(metadata["stitch_flare"], abs=1e-5)
    assert stitch.slant == pytest.approx(metadata["stitch_slant"], abs=1e-5)
    assert parsed.orientation_keyframes[0].orientation == pytest.approx((0.0, 0.0, -1.4), abs=1e-5)
    focus = parsed.autofocus_keyframes[0]
    assert focus.rotation == pytest.approx((0.1, -0.2, 0.3), abs=1e-5)
    assert focus.focal_distance_override == pytest.approx(175.0, abs=1e-5)
    assert focus.min_focus_distance == pytest.approx(22.0, abs=1e-5)
    assert focus.max_focus_distance == pytest.approx(840.0, abs=1e-5)
    assert focus.focus_transition_speed == pytest.approx(1.4, abs=1e-5)
    assert parsed.audio_keyframes[0].time_offset == pytest.approx(-0.125, abs=1e-5)
    assert parsed.audio_keyframes[0].volume_multiplier == pytest.approx(1.18, abs=1e-5)
    assert parsed.sync_keyframes[0].starting_ticks_offset == round(0.08 * TICKS_PER_SECOND)
    assert parsed.sync_keyframes[0].ticks_per_hour_offset == round(-0.17 * TICKS_PER_SECOND)


def test_profile_strength_scales_sparse_scenes_channels_and_audio():
    correction = CorrectionProfile(
        contrast_r=1.04,
        gamma_b=0.96,
        offset_g=0.012,
        audio_volume_multiplier=1.30,
        audio_time_offset=0.20,
        sync_drift_seconds_per_hour=-0.10,
        scene_keyframes=[
            {"time_seconds": 0.0, "native": _native(contrast=(1.04, 1.0, 1.0, 1.10))},
            {"time_seconds": 120.0, "native": _native(gamma=(1.0, 1.0, 0.96, 0.94), offset=(0.0, 0.012, 0.0, 0.02))},
        ],
    )
    scaled = _scaled_correction(correction.dict(), 0.5)
    assert 1.0 < scaled.contrast_r < 1.04
    assert 0.96 < scaled.gamma_b < 1.0
    assert scaled.offset_g == pytest.approx(0.006, abs=1e-7)
    assert 1.0 < scaled.audio_volume_multiplier < 1.30
    assert scaled.audio_time_offset == pytest.approx(0.10)
    assert scaled.sync_drift_seconds_per_hour == pytest.approx(-0.05)
    assert [item["time_seconds"] for item in scaled.scene_keyframes] == [0.0, 120.0]
    assert scaled.scene_keyframes[0]["native"]["contrast"][0] < 1.04
    assert scaled.scene_keyframes[1]["native"]["offset"][1] == pytest.approx(0.006)


def test_native_calibration_safe_defaults_and_opt_in_groups():
    metadata = {
        "projection": "perspective",
        "stereo": "sbs",
        "stereo_alignment": {
            "frames": 3,
            "confidence": 0.91,
            "auto_applicable": True,
            "suggested_rotation": (0.42, 0.0, 0.0),
            "roll_auto_applicable": True,
            "suggested_roll_rotation": (0.0, 0.0, -0.35),
            "scale_auto_applicable": True,
            "stitch_auto_applicable": True,
            "stitch_confidence": 0.88,
            "suggested_stitch": {
                "shift": (0.01, -0.01, 0.006, -0.006),
                "scale": (1.01, 0.99, 1.006, 0.994),
                "shear": (0.004, -0.004, 0.002, -0.002),
                "flare": (0.002, 0.0, -0.002, 0.0),
                "slant": (0.001, -0.001, 0.001, -0.001),
            },
            "autofocus_recommendation": {
                "confidence": 0.86,
                "min_distance": 18.0,
                "max_distance": 920.0,
                "transition_speed": 1.25,
            },
        },
        "active_image_bounds": {
            "confidence": 0.93,
            "auto_applicable": True,
            "suggested_zoom": (1.14, 1.10),
            "suggested_pan": (-0.03, 0.02),
        },
        "horizon_verification": {
            "confidence": 0.90,
            "auto_applicable": True,
            "suggested_orientation_rotation": (0.0, 0.0, 1.7),
        },
    }
    correction = CorrectionProfile(
        audio_volume_multiplier=1.20,
        audio_loudness_confidence=0.9,
        suggested_audio_time_offset=0.16,
        av_sync_confidence=0.90,
        suggested_sync_drift_seconds_per_hour=-0.08,
        sync_drift_confidence=0.90,
    )
    cfg = Config()
    base = native_calibration_metadata({"metadata": metadata, "layout_override": {}}, metadata, cfg, correction)
    # Preserve v0.13.0 behavior: confidence-gated vertical stereo alignment and
    # encoded-border crop remain enabled by default; newly exposed native
    # calibration outputs stay opt-in.
    assert base["stereo_alignment_applied"] is True
    assert base["auto_crop_applied"] is True
    assert base["audio_volume_applied"] is True
    assert base["audio_volume_multiplier"] == pytest.approx(1.20)
    assert base["stereo_roll_applied"] is False
    assert base["stereo_scale_applied"] is False
    assert base["stitch_applied"] is False
    assert base["horizon_leveling_applied"] is False
    assert base["autofocus_applied"] is False
    assert base["audio_sync_applied"] is False
    assert base["native_calibration_output_policy"]["stereo_disparity"] is True
    assert base["native_calibration_output_policy"]["crop"] is True
    assert all(
        value is False
        for name, value in base["native_calibration_output_policy"].items()
        if name not in {"stereo_disparity", "crop"}
    )

    cfg.hsp.auto_stereo_roll = True
    cfg.hsp.auto_eye_scale_shear = True
    cfg.hsp.auto_stitch_vectors = True
    cfg.hsp.auto_crop = True
    cfg.hsp.auto_horizon = True
    cfg.hsp.auto_autofocus = True
    cfg.hsp.auto_audio_sync = True
    applied = native_calibration_metadata({"metadata": metadata, "layout_override": {}}, metadata, cfg, correction)
    assert applied["stereo_alignment_applied"] is True
    assert applied["alignment_rotation"][0] == pytest.approx(0.42)
    assert applied["auto_crop_applied"] is True
    assert applied["zoom"] == pytest.approx((1.14, 1.10))
    assert applied["stereo_roll_applied"] is True
    assert applied["stitch_applied"] is True
    assert applied["horizon_leveling_applied"] is True
    assert applied["autofocus_applied"] is True
    assert applied["audio_sync_applied"] is True
    assert applied["audio_time_offset"] == pytest.approx(0.16)
    assert applied["sync_drift_seconds_per_hour"] == pytest.approx(-0.08)

    manual = {"zoom": [1.03, 1.03], "pan": [0.0, 0.0], "alignment_rotation": [0.0, 0.0, 0.0]}
    protected = native_calibration_metadata({"metadata": metadata, "layout_override": manual}, metadata, cfg, correction)
    assert protected["zoom"] == [1.03, 1.03]
    assert protected["alignment_rotation"] == [0.0, 0.0, 0.0]

    fisheye = dict(metadata, projection="fisheye")
    no_crop = native_calibration_metadata({"metadata": fisheye, "layout_override": {}}, fisheye, Config(), correction)
    assert no_crop["auto_crop_applied"] is False


def test_horizon_estimator_is_stable_and_bounded():
    frames = []
    for angle in (2.0, 2.1, 1.9):
        image = np.zeros((240, 400, 3), dtype=np.uint8)
        for y in (80, 120, 160):
            cv2.line(image, (30, y), (370, y), (255, 255, 255), 3)
        matrix = cv2.getRotationMatrix2D((200, 120), angle, 1.0)
        frames.append(cv2.warpAffine(image, matrix, (400, 240)))
    result = _horizon_evidence(frames, "mono", False, "perspective")
    assert result["confidence"] >= 0.8
    assert result["auto_applicable"] is True
    assert abs(float(result["median_roll_deg"])) == pytest.approx(2.0, abs=0.3)
    assert abs(float(result["suggested_orientation_rotation"][2])) <= 3.0


def test_advanced_layout_patch_rebuilds_native_hsp(tmp_path: Path):
    app, _cfg, video_id = _app_with_ready_video(tmp_path)
    patch = {
        "zoom": "1.12,1.08",
        "pan": [-0.04, 0.02],
        "alignment_rotation": "0.3,0,-0.2",
        "stitch_shift": [0.01, -0.01, 0.005, -0.005],
        "stitch_scale": [1.01, 0.99, 1.005, 0.995],
        "stitch_shear": [0.004, -0.004, 0.0, 0.0],
        "orientation_rotation": [0.0, 0.0, 1.2],
        "autofocus_min_distance": 15.0,
        "autofocus_max_distance": 900.0,
        "audio_time_offset": -0.1,
        "audio_volume_multiplier": 1.15,
        "sync_drift_seconds_per_hour": 0.12,
    }
    with TestClient(app) as client:
        response = client.patch(f"/api/videos/{video_id}/layout", json=patch)
        assert response.status_code == 200, response.text
    row = app.state.db.get(video_id)
    assert row["layout_override"]["zoom"] == pytest.approx([1.12, 1.08])
    parsed = inspect(row["auto_hsp"])
    assert parsed.format_keyframes[0].zoom == pytest.approx((1.12, 1.08), abs=1e-5)
    assert parsed.format_keyframes[0].pan == pytest.approx((-0.04, 0.02), abs=1e-5)
    assert parsed.alignment_keyframes[0].rotation == pytest.approx((0.3, 0.0, -0.2), abs=1e-5)
    assert parsed.stitch_keyframes[0].shift == pytest.approx((0.01, -0.01, 0.005, -0.005), abs=1e-5)
    assert parsed.orientation_keyframes[0].orientation == pytest.approx((0.0, 0.0, 1.2), abs=1e-5)
    assert parsed.autofocus_keyframes[0].min_focus_distance == pytest.approx(15.0)
    assert parsed.autofocus_keyframes[0].max_focus_distance == pytest.approx(900.0)
    assert parsed.audio_keyframes[0].time_offset == pytest.approx(-0.1, abs=1e-5)
    assert parsed.audio_keyframes[0].volume_multiplier == pytest.approx(1.15, abs=1e-5)
    assert parsed.sync_keyframes[0].ticks_per_hour_offset == round(0.12 * TICKS_PER_SECOND)
    assert parsed.array_counts["image"] == 2


def test_config_ui_comparator_responsive_table_and_drag_direction():
    cfg = Config()
    public = public_config(cfg)
    for key in (
        "per_channel_color", "scene_grading", "horizon_leveling",
        "stereo_roll_correction", "stereo_scale_correction", "stitch_correction",
        "autofocus_tuning", "audio_correction", "loudness_normalization",
        "audio_sync_analysis",
    ):
        assert key in public["analysis"]
    assert "web_player_invert_drag" in public["server"]
    assert VISUAL_CALIBRATION_VERSION >= 5

    root = Path(__file__).resolve().parents[1]
    html = (root / "heretune/static/index.html").read_text(encoding="utf-8")
    js = (root / "heretune/static/app.js").read_text(encoding="utf-8")
    css = (root / "heretune/static/styles.css").read_text(encoding="utf-8")
    player_html = (root / "heretune/static/player.html").read_text(encoding="utf-8")
    player_js = (root / "heretune/static/player.js").read_text(encoding="utf-8")
    example = (root / "config.example.toml").read_text(encoding="utf-8")

    assert 'name="analysis.scene_max_keyframes" type="number" min="0"' in html
    assert 'name="analysis.per_channel_color"' in html
    assert 'name="hsp.auto_stitch_vectors"' in html
    assert 'name="hsp.auto_audio_sync"' in html
    assert 'name="analysis.stitch_auto_apply"' not in html
    assert 'name="analysis.audio_sync_auto_apply"' not in html
    assert "diagonal-compare" in js
    assert js.count('id="diagonalCompare"') == 1
    assert "compare-after-mask" in js
    assert "sceneTimelineHtml" in js
    assert "channel-vector-table" in js
    assert '@media (max-width: 1200px)' in css
    assert 'tr.video-row' in css
    assert 'grid-template-areas:' in css
    assert '.compare-divider' in css
    assert 'data-col="corrections"' in js
    assert 'id="reverseDragToggle"' in player_html
    assert "yaw += dx * .004 * direction" in player_js
    assert "pitch + dy * .004 * direction" in player_js
    assert "localStorage.setItem('heretune.reverseVrDrag'" in player_js
    assert "Legacy cache-bust regression" not in player_html + player_js
    assert "scene_max_keyframes = 0" in example
    assert "web_player_invert_drag = false" in example
