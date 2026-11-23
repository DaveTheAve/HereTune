from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import heretune.analyzer as analyzer
import heretune.worker as worker
from heretune.config import Config
from heretune.db import DB


def _frame(width: int, height: int) -> np.ndarray:
    yy, xx = np.mgrid[0:height, 0:width]
    rgb = np.stack([
        0.25 + (xx % 17) / 120.0,
        0.35 + (yy % 13) / 130.0,
        0.45 + ((xx + yy) % 19) / 140.0,
    ], axis=-1)
    return np.rint(np.clip(rgb, 0.0, 1.0) * 255).astype(np.uint8)


def test_single_frame_statistics_exception_does_not_abort_video(monkeypatch, tmp_path: Path):
    diagnostics: list[dict[str, object]] = []

    def fake_sample(_path, _ffmpeg, _timestamp, width, height, _use_gpu, _device, _fallback):
        return _frame(int(width), int(height)), "cpu", ""

    original = analyzer._frame_statistics_thread
    calls = {"n": 0}

    def flaky_stats(frame):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("synthetic per-frame statistics failure")
        return original(frame)

    monkeypatch.setattr(analyzer, "_sample_frame_with_fallback", fake_sample)
    monkeypatch.setattr(analyzer, "_frame_statistics_thread", flaky_stats)
    monkeypatch.setattr(analyzer, "load_face_detectors", lambda: [])

    profile = analyzer.analyze_video(
        path=tmp_path / "movie.mp4",
        info=analyzer.MediaInfo(duration=60.0, width=640, height=360),
        ffmpeg="ffmpeg",
        samples=8,
        analysis_width=160,
        strength=1.0,
        white_balance=True,
        tone=True,
        saturation_correction=True,
        skin_priority=1.0,
        scene_grading=False,
        execution_mode="cpu",
        adaptive_sampling=False,
        smart_sampling=False,
        on_diagnostic=diagnostics.append,
    )

    assert profile.sampled_frames >= 6
    assert any(item.get("stage") == "frame-statistics" for item in diagnostics)
    assert any("synthetic per-frame statistics failure" in str(item.get("message")) for item in diagnostics)


def test_worker_failure_is_loud_persisted_and_queue_safe(monkeypatch, tmp_path: Path, capsys):
    cfg = Config()
    cfg.paths.data_dir = str(tmp_path / "data")
    db = DB(cfg.db_path)
    media_path = tmp_path / "Broken.mp4"
    media_path.write_bytes(b"not-used")
    now = "2026-08-14T00:00:00+00:00"
    db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,date_added,last_seen,metadata_json)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "broken", str(media_path), "Broken.mp4", "Broken", 8, 1, "queued", now, now,
            '{"duration":120,"width":1920,"height":1080,"bit_depth":8,"is_hdr":false,"audio_codecs":[]}',
        ),
    )
    row = db.get("broken")
    assert row is not None

    monkeypatch.setattr(worker, "thumbnail_is_current", lambda _row: True)

    def explode(*args, **kwargs):
        raise RuntimeError("synthetic whole-video failure")

    monkeypatch.setattr(worker, "analyze", explode)
    worker.process(db, cfg, row)

    saved = db.get("broken")
    assert saved is not None
    assert saved["status"] == "error"
    assert "synthetic whole-video failure" in saved["error"]
    assert "stage=analysis" in saved["error"]

    console = capsys.readouterr()
    combined = console.out + console.err
    assert "[HereTune Analysis] FAILED Broken" in combined
    assert "Continuing with the next queued video" in combined
    assert "RuntimeError: synthetic whole-video failure" in combined

    log = cfg.data_dir / "logs" / "analysis_failures.log"
    assert log.exists()
    text = log.read_text(encoding="utf-8")
    assert "VIDEO ANALYSIS FAILED" in text
    assert "synthetic whole-video failure" in text
    assert "Traceback" in text


def test_dashboard_surfaces_failed_video_reason_while_queue_continues():
    root = Path(__file__).resolve().parents[1]
    js = (root / "heretune" / "static" / "app.js").read_text(encoding="utf-8")
    assert "last_analysis_failure" in js
    assert "Queue continuing" in js
    assert "row-error-reason" in js


def test_hybrid_per_sample_gpu_failure_uses_three_value_cpu_fallback(monkeypatch, tmp_path: Path):
    """Regression for the three-value per-sample GPU fallback contract."""
    diagnostics: list[dict[str, object]] = []
    gpu_calls = {"n": 0}

    monkeypatch.setattr(analyzer, "resolve_execution_mode", lambda cfg, ffmpeg: ("hybrid", None))
    monkeypatch.setattr(analyzer, "load_face_detectors", lambda: [])

    def fake_gpu(_path, _ffmpeg, _timestamp, width, height, _device, _strategy):
        gpu_calls["n"] += 1
        # Preflight must pass so the video remains in Hybrid. Every real sample
        # then exercises the CPU fallback branch used by production.
        if gpu_calls["n"] == 1:
            return _frame(int(width), int(height)), ""
        return None, "synthetic GPU seek failure"

    def fake_fallback(_path, _ffmpeg, _timestamp, width, height, _prefer_gpu, _device, _fallback):
        return _frame(int(width), int(height)), "cpu", ""

    monkeypatch.setattr(analyzer, "_sample_frame_gpu_strategy", fake_gpu)
    monkeypatch.setattr(analyzer, "_sample_frame_with_fallback", fake_fallback)

    profile = analyzer.analyze_video(
        path=tmp_path / "movie.mp4",
        info=analyzer.MediaInfo(duration=60.0, width=640, height=360),
        ffmpeg="ffmpeg",
        samples=6,
        analysis_width=160,
        strength=1.0,
        white_balance=True,
        tone=True,
        saturation_correction=True,
        skin_priority=1.0,
        scene_grading=False,
        execution_mode="hybrid",
        adaptive_sampling=False,
        smart_sampling=False,
        gpu_decode_workers=2,
        cpu_analysis_workers=2,
        gpu_fallback_to_cpu=True,
        on_diagnostic=diagnostics.append,
    )

    assert profile.sampled_frames >= 4
    assert profile.gpu_fallback_samples >= 4
    assert not any("too many values to unpack" in str(item.get("message")) for item in diagnostics)


def test_worker_prints_effective_sampling_plan(monkeypatch, tmp_path: Path, capsys):
    cfg = Config()
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.analysis.analysis_width = 960
    cfg.analysis.samples = 48
    cfg.analysis.sample_interval_seconds = 30.0
    cfg.analysis.max_samples = 192
    cfg.analysis.scene_probe_interval_seconds = 10.0
    cfg.analysis.scene_probe_width = 480
    cfg.analysis.scene_boundary_refine_seconds = 2.0
    cfg.analysis.scene_full_samples_per_segment = 9
    cfg.analysis.scene_max_keyframes = 8
    cfg.analysis.gpu_decode_workers = 4
    cfg.analysis.cpu_analysis_workers = 8
    db = DB(cfg.db_path)
    media_path = tmp_path / "Plan.mp4"
    media_path.write_bytes(b"not-used")
    stamp = "2026-08-14T00:00:00+00:00"
    db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,date_added,last_seen,metadata_json)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "plan", str(media_path), "Plan.mp4", "Plan", 8, 1, "queued", stamp, stamp,
            '{"duration":120,"width":1920,"height":1080,"bit_depth":8,"is_hdr":false,"audio_codecs":[]}',
        ),
    )
    row = db.get("plan")
    assert row is not None
    monkeypatch.setattr(worker, "thumbnail_is_current", lambda _row: True)
    monkeypatch.setattr(worker, "analyze", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stop after plan")))
    worker.process(db, cfg, row)
    console = capsys.readouterr()
    combined = console.out + console.err
    assert "[HereTune Analysis] PLAN Plan" in combined
    assert "full 960px" in combined
    assert "full cadence ≤30.0s" in combined
    assert "scene probes ~10.0s/480px" in combined
    assert "9 full/segment" in combined
    assert "max grades 8" in combined
    assert "workers GPU 4/CPU 8" in combined


def test_sdr_bit_depth_is_analyzed_not_skipped_as_hdr():
    """Regression: the auto-tune skip gate keys on true-HDR (``is_hdr``), not on
    bit depth. 10-bit SDR (yuv420p10le / bt709) is the bulk of the library and
    must be analyzed, not skipped as if it were HDR. A true-HDR source is still
    skipped. We probe the gate in isolation: metadata with no duration reaches the
    "Video duration is missing" check only if the HDR gate let it through."""
    # true HDR (PQ/HLG) -> skipped by the gate
    with pytest.raises(analyzer.AnalysisError) as hdr:
        analyzer.analyze(Path("hdr.mp4"), {"is_hdr": True, "bit_depth": 8}, "ffmpeg", None)
    assert "HDR" in str(hdr.value)
    # 10-bit SDR -> passes the gate (reaches the duration check, a different error)
    with pytest.raises(analyzer.AnalysisError) as sdr10:
        analyzer.analyze(Path("sdr10.mp4"), {"is_hdr": False, "bit_depth": 10}, "ffmpeg", None)
    assert "duration" in str(sdr10.value).lower()
    # 8-bit SDR -> also analyzed
    with pytest.raises(analyzer.AnalysisError) as sdr8:
        analyzer.analyze(Path("sdr8.mp4"), {"is_hdr": False, "bit_depth": 8}, "ffmpeg", None)
    assert "duration" in str(sdr8.value).lower()
