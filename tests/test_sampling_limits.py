from __future__ import annotations

import numpy as np

from heretune.analyzer import detect_scene_boundaries, scene_full_sample_timestamps
from heretune.config import Config, _normalize


def _stat(t: float, p50: float, rgb: tuple[float, float, float]):
    return {
        "_timestamp": t,
        "p05": max(0.01, p50 - 0.12),
        "p50": p50,
        "p95": min(0.99, p50 + 0.32),
        "median_sat": 0.2,
        "channel_means": np.asarray(rgb, dtype=np.float64),
    }


def test_user_limits_are_not_silently_clamped():
    cfg = Config()
    cfg.analysis.max_samples = 9999
    cfg.analysis.scene_max_keyframes = 9999
    _normalize(cfg)
    assert cfg.analysis.max_samples == 9999
    assert cfg.analysis.scene_max_keyframes == 9999


def test_zero_scene_limit_means_unlimited():
    cfg = Config()
    cfg.analysis.scene_max_keyframes = 0
    _normalize(cfg)
    assert cfg.analysis.scene_max_keyframes == 0


def test_detector_can_return_more_than_eight_grade_boundaries_when_unlimited():
    stats = []
    # 12 long-lived grading states, 60s each, with alternating luma/color states.
    for segment in range(12):
        base_t = segment * 60.0
        p50 = 0.22 if segment % 2 == 0 else 0.62
        rgb = (0.44, 0.42, 0.40) if segment % 2 == 0 else (0.34, 0.42, 0.50)
        for offset in (5.0, 15.0, 25.0, 35.0, 45.0, 55.0):
            stats.append(_stat(base_t + offset, p50, rgb))
    boundaries, confidence = detect_scene_boundaries(
        stats,
        720.0,
        minimum_seconds=30.0,
        change_threshold=0.8,
        max_keyframes=0,
        persistence=2,
    )
    assert len(boundaries) > 8
    assert confidence > 0.0


def test_positive_scene_limit_is_honored_exactly():
    stats = []
    for segment in range(12):
        base_t = segment * 60.0
        p50 = 0.22 if segment % 2 == 0 else 0.62
        rgb = (0.44, 0.42, 0.40) if segment % 2 == 0 else (0.34, 0.42, 0.50)
        for offset in (5.0, 15.0, 25.0, 35.0, 45.0, 55.0):
            stats.append(_stat(base_t + offset, p50, rgb))
    boundaries, _ = detect_scene_boundaries(
        stats,
        720.0,
        minimum_seconds=30.0,
        change_threshold=0.8,
        max_keyframes=5,
        persistence=2,
    )
    # max_keyframes includes the initial t=0 grade, so five grades == four boundaries.
    assert len(boundaries) == 4


def test_one_scene_limit_means_only_initial_grade():
    stats = []
    for segment in range(4):
        base_t = segment * 60.0
        p50 = 0.22 if segment % 2 == 0 else 0.62
        rgb = (0.44, 0.42, 0.40) if segment % 2 == 0 else (0.34, 0.42, 0.50)
        for offset in (5.0, 15.0, 25.0, 35.0, 45.0, 55.0):
            stats.append(_stat(base_t + offset, p50, rgb))
    boundaries, _ = detect_scene_boundaries(
        stats, 240.0, minimum_seconds=30.0, change_threshold=0.8,
        max_keyframes=1, persistence=2,
    )
    assert boundaries == []


def test_worker_plan_labels_zero_scene_limit_as_unlimited(monkeypatch, tmp_path, capsys):
    import heretune.worker as worker
    from heretune.db import DB
    cfg = Config()
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.analysis.scene_max_keyframes = 0
    db = DB(cfg.db_path)
    media_path = tmp_path / "Unlimited.mp4"
    media_path.write_bytes(b"x")
    stamp = "2026-08-14T00:00:00+00:00"
    db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,date_added,last_seen,metadata_json)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("unlimited", str(media_path), "Unlimited.mp4", "Unlimited", 1, 1, "queued", stamp, stamp,
         '{"duration":120,"width":1920,"height":1080,"bit_depth":8,"is_hdr":false,"audio_codecs":[]}'),
    )
    row = db.get("unlimited")
    monkeypatch.setattr(worker, "thumbnail_is_current", lambda _row: True)
    monkeypatch.setattr(worker, "analyze", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stop after plan")))
    worker.process(db, cfg, row)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "max grades unlimited" in combined


def test_full_sample_ceiling_is_never_exceeded_by_many_segments():
    boundaries = [float(value) for value in range(60, 1200, 60)]
    times = scene_full_sample_timestamps(1200.0, boundaries, base_target=48, per_segment=9, max_samples=100)
    assert len(times) <= 100
    assert len(times) == 100


def test_full_sample_budget_can_cover_twenty_segments_at_recommended_settings():
    boundaries = [float(value) for value in range(60, 1200, 60)]
    times = scene_full_sample_timestamps(1200.0, boundaries, base_target=48, per_segment=9, max_samples=360)
    assert len(times) == 20 * 9
