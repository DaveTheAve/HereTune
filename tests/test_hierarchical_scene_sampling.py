from pathlib import Path

import numpy as np
import pytest

import heretune.analyzer as analyzer
from heretune.analyzer import MediaInfo, adaptive_scene_probe_interval, scene_probe_timestamps
from heretune.config import Config


def _synthetic_frame(timestamp: float, width: int, height: int) -> np.ndarray:
    # A textured, neutral-ish source with one sustained grading-state change at
    # 15 minutes. Texture keeps detail/noise paths realistic without needing a
    # real movie or face detector.
    yy, xx = np.mgrid[0:height, 0:width]
    texture = ((xx % 23) / 23.0 * 0.045 + (yy % 19) / 19.0 * 0.035)
    if timestamp < 900.0:
        base = np.array([0.34, 0.39, 0.44], dtype=np.float32)
    else:
        base = np.array([0.57, 0.43, 0.31], dtype=np.float32)
    rgb = np.clip(base[None, None, :] + texture[..., None], 0.0, 1.0)
    return np.rint(rgb * 255.0).astype(np.uint8)


def test_defaults_keep_full_analysis_at_960_and_probe_at_480():
    cfg = Config()
    assert cfg.analysis.analysis_width == 960
    assert cfg.analysis.scene_probe_width == 480
    assert cfg.analysis.scene_probe_interval_seconds == pytest.approx(10.0)
    assert cfg.analysis.scene_boundary_refine_seconds == pytest.approx(2.0)
    assert cfg.analysis.scene_full_samples_per_segment == 9
    assert cfg.analysis.scene_min_seconds == pytest.approx(30.0)


def test_adaptive_scene_probe_cadence():
    assert adaptive_scene_probe_interval(15 * 60, 10) == pytest.approx(8.0)
    assert adaptive_scene_probe_interval(45 * 60, 10) == pytest.approx(10.0)
    assert adaptive_scene_probe_interval(90 * 60, 10) == pytest.approx(15.0)
    assert adaptive_scene_probe_interval(180 * 60, 10) == pytest.approx(20.0)
    times, interval = scene_probe_timestamps(45 * 60, 10, 900)
    assert interval == pytest.approx(10.0)
    assert 260 <= len(times) <= 275


def test_dense_probe_is_wired_to_low_res_then_full_segment_analysis(monkeypatch, tmp_path: Path):
    widths: list[int] = []
    timestamps_by_width: dict[int, list[float]] = {}

    def fake_sample(_path, _ffmpeg, timestamp, width, height, _use_gpu, _gpu_device, _fallback):
        widths.append(int(width))
        timestamps_by_width.setdefault(int(width), []).append(float(timestamp))
        return _synthetic_frame(float(timestamp), int(width), int(height)), "cpu", ""

    monkeypatch.setattr(analyzer, "_sample_frame_with_fallback", fake_sample)
    monkeypatch.setattr(analyzer, "load_face_detectors", lambda: [])

    profile = analyzer.analyze_video(
        path=tmp_path / "synthetic.mp4",
        info=MediaInfo(duration=1800.0, width=1920, height=1080),
        ffmpeg="ffmpeg",
        samples=24,
        analysis_width=320,
        strength=1.0,
        white_balance=True,
        tone=True,
        saturation_correction=True,
        skin_priority=1.0,
        scene_grading=True,
        scene_max_keyframes=8,
        scene_min_seconds=40.0,
        scene_change_threshold=0.70,
        scene_min_confidence=0.20,
        scene_probe_interval_seconds=10.0,
        scene_probe_width=240,
        scene_boundary_refine_seconds=2.0,
        scene_probe_persistence=2,
        scene_full_samples_per_segment=7,
        scene_probe_max_samples=900,
        execution_mode="cpu",
        adaptive_sampling=False,
        smart_sampling=False,
        max_samples=72,
    )

    # Dense timeline work is actually low-resolution; the correction pass stays
    # at the requested full analysis width.
    assert 240 in widths
    assert 320 in widths
    assert profile.scene_probe_width == 240
    assert profile.sample_width == 320
    assert profile.scene_probe_samples >= 170
    assert profile.scene_probe_refinement_samples >= 5
    assert profile.scene_full_samples >= 14  # >= 7 full samples in each stable half

    # The 10-second probe cadence should discover and refine the sustained change
    # near 15:00, then serialize at least two stable Image grades.
    assert profile.scene_grade_boundaries >= 1
    assert len(profile.scene_keyframes) >= 2
    assert any(abs(float(kf["time_seconds"]) - 900.0) <= 15.0 for kf in profile.scene_keyframes[1:])

    probe_times = timestamps_by_width[240]
    # Refiner adds 2-second samples around the candidate, so there should be
    # tighter-than-10-second spacing near the boundary.
    local = sorted(t for t in probe_times if 870 <= t <= 930)
    assert any((b - a) <= 2.1 for a, b in zip(local, local[1:]))


def test_worker_wrapper_passes_dense_scene_config(monkeypatch, tmp_path: Path):
    captured = {}

    def fake_analyze_video(**kwargs):
        captured.update(kwargs)
        return analyzer.CorrectionProfile()

    monkeypatch.setattr(analyzer, "analyze_video", fake_analyze_video)
    cfg = Config().analysis
    profile = analyzer.analyze(
        tmp_path / "movie.mp4",
        {
            "duration": 2700.0,
            "width": 5760,
            "height": 2880,
            "bit_depth": 8,
            "is_hdr": False,
            "audio_codecs": [],
        },
        "ffmpeg",
        cfg,
    )
    assert captured["analysis_width"] == 960
    assert captured["scene_probe_interval_seconds"] == pytest.approx(10.0)
    assert captured["scene_probe_width"] == 480
    assert captured["scene_boundary_refine_seconds"] == pytest.approx(2.0)
    assert captured["scene_probe_persistence"] == 2
    assert captured["scene_full_samples_per_segment"] == 9
    assert captured["scene_probe_max_samples"] == 900
    assert profile.analysis_version == "offset-free-tone-1"


def test_settings_ui_exposes_hierarchical_scene_controls():
    root = Path(__file__).resolve().parents[1]
    html = (root / "heretune/static/index.html").read_text(encoding="utf-8")
    example = (root / "config.example.toml").read_text(encoding="utf-8")
    for field in (
        "analysis.scene_probe_interval_seconds",
        "analysis.scene_probe_width",
        "analysis.scene_boundary_refine_seconds",
        "analysis.scene_probe_persistence",
        "analysis.scene_full_samples_per_segment",
        "analysis.scene_probe_max_samples",
    ):
        assert f'name="{field}"' in html
    assert "analysis_width = 960" in example
    assert "scene_probe_width = 480" in example
    assert "scene_probe_interval_seconds = 10.0" in example
