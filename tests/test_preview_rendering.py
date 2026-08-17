from pathlib import Path

import numpy as np
import pytest

import heretune.media as media
from heretune import __version__


def test_release_and_preview_revision():
    assert __version__ == "0.13.1"
    assert media.PREVIEW_RENDER_VERSION >= 3
    assert media.PREVIEW_CARD_WIDTH == 960
    assert media.PREVIEW_CARD_HEIGHT == 540
    assert media.PREVIEW_JPEG_QUALITY >= 92


def test_comparison_prefers_stable_high_confidence_scene():
    correction = {
        "scene_keyframes": [
            {"time_seconds": 0.0, "confidence": 0.25},
            {"time_seconds": 120.0, "confidence": 0.94},
            {"time_seconds": 500.0, "confidence": 0.35},
        ]
    }
    # The strong 120..500s segment wins and the frame is taken from its center.
    assert media._comparison_preview_timestamp(correction, 600.0) == pytest.approx(310.0)


def test_comparison_timestamp_falls_back_without_scenes():
    assert media._comparison_preview_timestamp({}, 1000.0) == pytest.approx(350.0)
    assert media._comparison_preview_timestamp(None, 10.0) == pytest.approx(3.5)


def test_sbs_comparison_preserves_full_eye_resolution(monkeypatch, tmp_path: Path):
    seen = {}

    def fake_decode(path, ffmpeg, duration, source_width, source_height, max_width=640, timestamp_ratio=0.35):
        seen["max_width"] = max_width
        seen["ratio"] = timestamp_ratio
        # Return a packed SBS image at the requested width.
        h = max(2, max_width // 2)
        return np.zeros((h, max_width, 3), dtype=np.uint8)

    monkeypatch.setattr(media, "_decode_rgb_frame", fake_decode)
    result = media.preview_frame(
        tmp_path / "movie.mp4",
        "ffmpeg",
        {"duration": 100.0, "width": 3840, "height": 1920, "stereo": "sbs"},
        "final",
        timestamp_seconds=40.0,
        card_width=960,
        card_height=540,
    )
    assert seen["max_width"] == 1920
    assert seen["ratio"] == pytest.approx(0.4)
    assert result.shape == (540, 960, 3)


def test_neutral_static_grade_is_effectively_identity():
    rng = np.random.default_rng(83)
    frame = rng.integers(0, 256, size=(72, 128, 3), dtype=np.uint8)
    corrected = media.apply_correction_preview(frame, {})
    delta = np.abs(frame.astype(np.int16) - corrected.astype(np.int16))
    assert int(delta.max()) <= 1
    assert float(delta.mean()) < 0.05


def test_static_grade_damps_unverified_regional_controls():
    # Use a gradient so the intentionally subtle regional approximation is
    # measurable without requiring a one-code-value change on a flat patch.
    grad = np.linspace(32, 224, 64, dtype=np.uint8)
    frame = np.repeat(grad[None, :, None], 32, axis=0)
    frame = np.repeat(frame, 3, axis=2)
    base = media.apply_correction_preview(frame, {})
    changed = media.apply_correction_preview(frame, {"midtones": 1.08})
    delta = changed.astype(np.int16) - base.astype(np.int16)
    assert float(delta.mean()) >= 0.0
    assert int(np.abs(delta).max()) <= 8


def test_ui_cache_busts_static_comparison_and_keeps_labels():
    root = Path(__file__).resolve().parents[1]
    app_js = (root / "heretune/static/app.js").read_text(encoding="utf-8")
    assert "video.preview_revision" in app_js
    assert 'class="compare-label before"' in app_js
    assert 'class="compare-label after"' in app_js
    assert ">BEFORE<" not in app_js  # labels are templated, not renamed to new wording


def test_live_shader_uses_full_strength_regional_preview_and_no_duplicate_declaration():
    root = Path(__file__).resolve().parents[1]
    player_js = (root / "heretune/static/player.js").read_text(encoding="utf-8")
    assert "1.0 * (uShadows * sw" in player_js
    assert "srgbToLinear" in player_js
    assert "linearToSrgb" in player_js
    declaration = "bool corrected = uColor == 1 || (uColor == 2 && vUv.x >= uSplit);"
    assert player_js.count(declaration) == 1
