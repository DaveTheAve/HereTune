from __future__ import annotations

from pathlib import Path

from heretune.analyzer import effective_sample_count, scene_full_sample_timestamps
from heretune.config import Config, _normalize, load_config


def test_zero_max_samples_survives_normalization_as_unlimited():
    cfg = Config()
    cfg.analysis.samples = 48
    cfg.analysis.max_samples = 0
    _normalize(cfg)
    assert cfg.analysis.max_samples == 0


def test_zero_max_samples_loaded_from_toml_stays_zero(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[analysis]\nsamples = 48\nadaptive_sampling = true\n"
        "sample_interval_seconds = 30.0\nmax_samples = 0\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.analysis.max_samples == 0


def test_55_minute_video_unlimited_uses_over_one_hundred_full_samples():
    # 55 minutes / 30 seconds = 110 expensive full-resolution targets.
    assert effective_sample_count(
        55 * 60,
        48,
        adaptive_sampling=True,
        sample_interval_seconds=30.0,
        max_samples=0,
    ) == 110


def test_positive_max_samples_remains_a_real_ceiling():
    assert effective_sample_count(
        55 * 60,
        48,
        adaptive_sampling=True,
        sample_interval_seconds=30.0,
        max_samples=80,
    ) == 80


def test_unlimited_scene_redistribution_can_exceed_base_target():
    boundaries = [float(value) for value in range(150, 3000, 150)]
    times = scene_full_sample_timestamps(
        55 * 60,
        boundaries,
        base_target=110,
        per_segment=9,
        max_samples=0,
    )
    assert len(times) > 110
    assert len(times) == 20 * 9


def test_positive_scene_redistribution_ceiling_is_still_honored():
    boundaries = [float(value) for value in range(150, 3000, 150)]
    times = scene_full_sample_timestamps(
        55 * 60,
        boundaries,
        base_target=110,
        per_segment=9,
        max_samples=120,
    )
    assert len(times) == 120


def test_settings_ui_allows_zero_for_unlimited_max_samples():
    root = Path(__file__).resolve().parents[1]
    html = (root / "heretune" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'name="analysis.max_samples" type="number" min="0"' in html
    assert "0 = unlimited" in html


def test_analyze_wrapper_fallbacks_match_current_canonical_defaults(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import heretune.analyzer as analyzer

    captured = {}
    def fake_analyze_video(**kwargs):
        captured.update(kwargs)
        return analyzer.CorrectionProfile()

    monkeypatch.setattr(analyzer, "analyze_video", fake_analyze_video)
    cfg = SimpleNamespace(
        samples=48, analysis_width=960, strength=1.0, white_balance=True,
        tone=True, saturation=True, skin_priority=1.25,
        min_rgb_gain=0.80, max_rgb_gain=1.24, audio_correction=False,
    )
    metadata = {
        "duration": 3300.0, "width": 5760, "height": 2880,
        "bit_depth": 8, "is_hdr": False,
    }
    analyzer.analyze(tmp_path / "movie.mp4", metadata, "ffmpeg", cfg)
    assert captured["scene_min_seconds"] == 30.0
    assert captured["scene_full_samples_per_segment"] == 9
    assert captured["gpu_decode_workers"] == 4
    assert captured["cpu_analysis_workers"] == 8
    assert captured["sample_interval_seconds"] == 30.0
    assert captured["max_samples"] == 360


def test_worker_plan_labels_zero_max_samples_as_unlimited(monkeypatch, tmp_path, capsys):
    import heretune.worker as worker
    from heretune.db import DB

    cfg = Config()
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.analysis.max_samples = 0
    db = DB(cfg.db_path)
    media_path = tmp_path / "UnlimitedSamples.mp4"
    media_path.write_bytes(b"x")
    stamp = "2026-08-14T00:00:00+00:00"
    db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,date_added,last_seen,metadata_json)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("unlimited-samples", str(media_path), "UnlimitedSamples.mp4", "UnlimitedSamples",
         1, 1, "queued", stamp, stamp,
         '{"duration":3300,"width":1920,"height":1080,"bit_depth":8,"is_hdr":false,"audio_codecs":[]}'),
    )
    row = db.get("unlimited-samples")
    monkeypatch.setattr(worker, "thumbnail_is_current", lambda _row: True)
    monkeypatch.setattr(worker, "analyze", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stop after plan")))
    worker.process(db, cfg, row)
    captured_output = capsys.readouterr()
    combined = captured_output.out + captured_output.err
    assert "max full unlimited" in combined
