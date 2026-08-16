from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pytest
from fastapi.testclient import TestClient

from heretune.analyzer import CorrectionProfile
from heretune.config import Config, public_config
from heretune.hsp import build, inspect
from heretune.library import now, sid
from heretune.server import (
        _normalize_half_star_rating,
    create_app,
)
from heretune.vrformat import VISUAL_CALIBRATION_VERSION, detect_visual_layout_sequence
from heretune.worker import native_calibration_metadata


def _app_with_video(tmp_path: Path, *, rating: float | None = None):
    library = tmp_path / "movies"
    library.mkdir()
    source = library / "Calibration_LR_FISHEYE.mp4"
    source.write_bytes(b"x" * 2048)
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.library.scan_interval_seconds = 3600
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    video_id = sid(source.name)
    stamp = now()
    metadata = {
        "duration": 60.0,
        "width": 480,
        "height": 240,
        "projection": "fisheye",
        "stereo": "sbs",
        "fov": 180.0,
        "lens": "Linear",
    }
    correction = CorrectionProfile()
    hsp = build(
        video_id,
        source.stem,
        60.0,
        correction,
        metadata=metadata,
        source_filename=source.name,
    )
    stat = source.stat()
    app.state.db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,
           analysis_json,auto_hsp,auto_hsp_version,rating,date_added,last_seen,last_analyzed)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            video_id,
            str(source),
            source.name,
            source.stem,
            stat.st_size,
            stat.st_mtime_ns,
            "ready",
            json.dumps(metadata),
            json.dumps(correction.dict()),
            hsp,
            7,
            rating,
            stamp,
            stamp,
            stamp,
        ),
    )
    return app, cfg, video_id, source


def _textured_eye(seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    eye = (rng.random((240, 320, 3)) * 255).astype(np.uint8)
    # Strong deterministic corners/features keep the feature matcher stable.
    yy, xx = np.mgrid[:240, :320]
    eye[..., 0] = (eye[..., 0] // 2 + ((xx // 17) % 2) * 110).astype(np.uint8)
    eye[..., 1] = (eye[..., 1] // 2 + ((yy // 19) % 2) * 105).astype(np.uint8)
    return eye


def test_native_image_stack_roundtrips_every_new_control():
    correction = CorrectionProfile(
        r_gain=1.04,
        g_gain=0.99,
        b_gain=1.01,
        contrast=1.06,
        brightness=0.018,
        gamma=0.97,
        saturation=1.03,
        sharpen=2.65,
        exposure_bias=0.14,
        white_temp=5250.0,
        white_tint=-0.075,
        shadows=0.055,
        midtones=1.035,
        highlights=0.955,
    )
    parsed = inspect(
        build(
            "vid",
            "Native Grade",
            60.0,
            correction,
            metadata={
                "projection": "fisheye",
                "stereo": "sbs",
                "lens": "Linear",
                "fov": 190.0,
                "orientation_type": "down",
                "aspect_ratio_type": "custom",
                "aspect_ratio": 1.85,
            },
            source_filename="Native Grade_LR_FISHEYE.mp4",
        )
    )
    image = parsed.image_keyframes[0]
    assert image.sharpen == pytest.approx(2.65, abs=1e-5)
    assert image.exposure_bias == pytest.approx(0.14, abs=1e-5)
    assert image.white_temp == pytest.approx(5250.0, abs=1e-3)
    assert image.white_tint == pytest.approx(-0.075, abs=1e-5)
    assert image.shadows == pytest.approx(0.055, abs=1e-5)
    assert image.midtones == pytest.approx(1.035, abs=1e-5)
    assert image.highlights == pytest.approx(0.955, abs=1e-5)
    assert image.contrast[3] == pytest.approx(1.06, abs=1e-5)
    assert image.gain[:3] == pytest.approx((1.04, 0.99, 1.01), abs=1e-5)
    fmt = parsed.format_keyframes[0]
    assert fmt.orientation_type == 3
    assert fmt.aspect_ratio_type == 4
    assert fmt.aspect_ratio == pytest.approx(1.85, abs=1e-5)


def test_half_star_validation_filters_and_heresphere_writeback(tmp_path: Path):
    assert _normalize_half_star_rating(4.5) == 4.5
    assert _normalize_half_star_rating(3.50000001) == 3.5
    with pytest.raises(ValueError, match="0.5-star increments"):
        _normalize_half_star_rating(4.3)

    app, _cfg, video_id, source = _app_with_video(tmp_path)
    hs_path = (
        f"/heresphere/video/{video_id}/"
        f"{quote(source.name, safe='')}"
    )
    with TestClient(app) as client:
        response = client.patch(
            f"/api/videos/{video_id}/library-state", json={"rating": 4.5}
        )
        assert response.status_code == 200
        assert response.json()["rating"] == 4.5
        assert [item["id"] for item in client.get("/api/videos?rating=4.5plus").json()["items"]] == [video_id]
        assert client.get("/api/videos?rating=5").json()["items"] == []
        assert client.patch(
            f"/api/videos/{video_id}/library-state", json={"rating": 4.3}
        ).status_code == 422

        response = client.post(hs_path, json={"rating": 3.5})
        assert response.status_code == 200
        assert client.get(f"/api/videos/{video_id}").json()["rating"] == 3.5


def test_multiframe_stereo_alignment_is_vertical_only_and_capped():
    left = _textured_eye()
    frames: list[np.ndarray] = []
    for shift in (4, 4, 5):
        right = np.zeros_like(left)
        right[shift:] = left[:-shift]
        frames.append(np.concatenate([left, right], axis=1))

    result = detect_visual_layout_sequence(
        frames,
        "Alignment_Test_LR.mp4",
        640,
        240,
        orientation_verification=False,
        lens_verification=False,
        stereo_alignment=True,
        stereo_alignment_max_degrees=0.75,
    )
    alignment = result["alignment"]
    assert result["resolved"]["stereo"] == "sbs"
    assert alignment["frames"] == 3
    assert alignment["auto_applicable"] is True
    assert abs(float(alignment["median_vertical_disparity_px"])) >= 3.5
    assert alignment["suggested_rotation"][1:] == (0.0, 0.0)
    assert abs(float(alignment["suggested_rotation"][0])) <= 0.75

    cfg = Config()
    row = {"metadata": {"stereo_alignment": alignment}, "layout_override": {}}
    native = native_calibration_metadata(row, row["metadata"], cfg)
    assert native["stereo_alignment_applied"] is True
    assert native["alignment_rotation"][1:] == (0.0, 0.0)


def test_content_verification_reports_borders_and_fisheye_family():
    rng = np.random.default_rng(7)
    bordered: list[np.ndarray] = []
    for _ in range(3):
        frame = np.zeros((240, 400, 3), dtype=np.uint8)
        frame[24:216, 40:360] = (rng.random((192, 320, 3)) * 200 + 30).astype(np.uint8)
        bordered.append(frame)
    flat = detect_visual_layout_sequence(
        bordered,
        "Encoded_Borders_2D.mp4",
        400,
        240,
        orientation_verification=False,
        stereo_alignment=False,
        lens_verification=False,
    )
    bounds = flat["active_bounds"]
    assert bounds["significant_borders"] is True
    assert bounds["auto_applied"] is False
    assert bounds["border_fractions"]["left"] == pytest.approx(0.10, abs=0.015)
    assert bounds["border_fractions"]["top"] == pytest.approx(0.10, abs=0.015)

    fisheye_frames: list[np.ndarray] = []
    yy, xx = np.mgrid[:240, :240]
    mask = (xx - 120) ** 2 + (yy - 120) ** 2 < 100 ** 2
    for _ in range(3):
        eye = np.zeros((240, 240, 3), dtype=np.uint8)
        texture = (rng.random((240, 240, 3)) * 180 + 50).astype(np.uint8)
        eye[mask] = texture[mask]
        fisheye_frames.append(np.concatenate([eye, eye], axis=1))
    fisheye = detect_visual_layout_sequence(
        fisheye_frames,
        "Lens_Test_LR_FISHEYE.mp4",
        480,
        240,
        orientation_verification=False,
        stereo_alignment=False,
        lens_verification=True,
    )
    lens = fisheye["lens_verification"]
    assert lens["confidence"] >= 0.75
    assert lens["suggested_fov"] in {180.0, 190.0, 200.0, 220.0}
    assert lens["fov_is_heuristic"] is True
    assert lens["auto_apply_recommended"] is False


def test_config_and_ui_expose_new_grade_calibration_and_half_stars():
    cfg = Config()
    public = public_config(cfg)
    analysis = public["analysis"]
    for name in (
        "advanced_tone",
        "sharpening",
        "temperature_tint",
        "vr_verification",
        "eye_swap_auto_apply",
        "stereo_alignment",
        "lens_verification",
    ):
        assert name in analysis
    assert VISUAL_CALIBRATION_VERSION >= 4

    root = Path(__file__).resolve().parents[1]
    html = (root / "heretune/static/index.html").read_text(encoding="utf-8")
    js = (root / "heretune/static/app.js").read_text(encoding="utf-8")
    css = (root / "heretune/static/styles.css").read_text(encoding="utf-8")
    example = (root / "config.example.toml").read_text(encoding="utf-8")
    assert 'value="4.5plus"' in html
    assert 'name="analysis.advanced_tone"' in html
    assert 'name="analysis.stereo_alignment_auto_apply"' in html
    assert 'name="analysis.lens_fov_auto_apply"' in html
    assert 'data-star-rating="${half.toFixed(1)}"' in js
    assert "Native image grade" in js
    assert "VR verification & native calibration" in js
    assert ".rating-half-left" in css
    assert ".calibration-summary-grid" in css
    assert "temperature_tint_strength = 0.45" in example
    assert "stereo_alignment_auto_apply = true" in example
