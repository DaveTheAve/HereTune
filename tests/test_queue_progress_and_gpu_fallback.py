from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from heretune.analyzer import CorrectionProfile, MediaInfo
from heretune.config import Config
from heretune.db import DB
from heretune.library import now
from heretune.server import create_app
from heretune.worker import THUMBNAIL_LAYOUT_VERSION


def _library_id(root: Path, relative: str) -> str:
    from heretune.library import sid
    root_key = os.path.normcase(os.path.normpath(str(root))).casefold()
    return sid(f"{root_key}::{relative}")


def _insert(db: DB, video_id: str, status: str = "queued", priority: int = 0, when: str | None = None):
    db.exec(
        """INSERT INTO videos(
               id,path,rel_path,title,size,mtime_ns,status,manual_priority,priority_requested_at,date_added,last_seen
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (video_id, video_id, video_id, video_id, 1, 1, status, priority, when, now(), now()),
    )


def test_thumbnail_cache_revision_is_current():
    assert THUMBNAIL_LAYOUT_VERSION >= 5


def test_manual_do_next_queue_precedes_normal_and_preserves_promotion_order(tmp_path: Path):
    db = DB(tmp_path / "queue.sqlite3")
    _insert(db, "normal")
    _insert(db, "next-a", priority=1, when="2026-08-13T20:00:00+00:00")
    _insert(db, "next-b", priority=1, when="2026-08-13T20:01:00+00:00")
    assert db.queued()["id"] == "next-a"
    db.exec("UPDATE videos SET status='analyzing', manual_priority=0, priority_requested_at=NULL WHERE id='next-a'")
    assert db.queued()["id"] == "next-b"
    db.exec("UPDATE videos SET status='analyzing', manual_priority=0, priority_requested_at=NULL WHERE id='next-b'")
    assert db.queued()["id"] == "normal"


def test_pause_resume_api_and_runtime_progress(tmp_path: Path):
    library = tmp_path / "movies"
    library.mkdir()
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    with TestClient(app) as client:
        paused = client.post("/api/work/pause", json={"paused": True})
        assert paused.status_code == 200
        assert paused.json()["paused"] is True
        assert client.post("/api/scan", json={}).status_code == 409
        status = client.get("/api/status").json()
        assert status["work"]["paused"] is True

        app.state.analysis_progress = {
            "video_id": "abc", "title": "Demo", "stage": "analysis",
            "completed": 18, "total": 72, "elapsed_seconds": 24.0, "eta_seconds": 72.0,
        }
        status = client.get("/api/status").json()
        assert status["active"][0]["completed"] == 18
        assert status["active"][0]["eta_seconds"] == 72.0

        resumed = client.post("/api/work/pause", json={"paused": False})
        assert resumed.status_code == 200
        assert resumed.json()["paused"] is False


def test_priority_api_promotes_pending_video(tmp_path: Path):
    library = tmp_path / "movies"
    library.mkdir()
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    with TestClient(app) as client:
        client.post("/api/work/pause", json={"paused": True})
        _insert(app.state.db, "v")
        response = client.post("/api/videos/v/priority", json={"priority": True})
        assert response.status_code == 200
        assert response.json()["manual_priority"] is True
        assert app.state.db.queued()["id"] == "v"
        response = client.post("/api/videos/v/priority", json={"priority": False})
        assert response.status_code == 200
        assert response.json()["manual_priority"] is False


def test_gpu_scale_failure_can_still_use_nvdec_compatibility_path(monkeypatch, tmp_path: Path):
    import heretune.analyzer as analyzer

    monkeypatch.setattr(analyzer, "resolve_execution_mode", lambda cfg, ffmpeg: ("hybrid", None))

    def fake_run(command, width, height):
        # Full CUDA output/scale path fails, but CUDA decode with software resize succeeds.
        if "-hwaccel_output_format" in command:
            return None, "scale_cuda surface conversion rejected"
        if "-hwaccel" in command and "cuda" in command:
            return np.full((height, width, 3), 128, dtype=np.uint8), ""
        return np.full((height, width, 3), 128, dtype=np.uint8), ""

    monkeypatch.setattr(analyzer, "_run_sample_command", fake_run)
    profile = analyzer.analyze_video(
        tmp_path / "demo.mp4",
        MediaInfo(duration=30.0, width=1920, height=960),
        "ffmpeg",
        samples=4,
        analysis_width=320,
        strength=1.0,
        white_balance=True,
        tone=True,
        saturation_correction=True,
        skin_priority=1.0,
        execution_mode="auto",
        auto_preference="hybrid",
        gpu_fallback_to_cpu=True,
        adaptive_sampling=False,
        max_samples=4,
    )
    assert profile.execution_mode == "hybrid"
    assert profile.gpu_preflight_fallback is False
    assert profile.gpu_compat_mode is True
    assert profile.gpu_samples == 4
    assert profile.gpu_compat_samples == 4
    assert profile.cpu_samples == 0


def test_dashboard_contains_live_refresh_progress_priority_and_pause_controls(tmp_path: Path):
    library = tmp_path / "movies"
    library.mkdir()
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    with TestClient(app) as client:
        page = client.get("/").text
        script = client.get("/static/app.js").text
        assert "pauseWorkButton" in page
        assert "workProgressBar" in page
        assert "lastStatusSignature" in script
        assert "reloadRequested" in script
        assert "data-priority-video" in script
        assert 'metric("Analysis time"' in script
        assert "eta_seconds" in script


def test_video_view_exposes_analysis_time_and_thumbnail_revision(tmp_path: Path):
    library = tmp_path / "movies"
    library.mkdir()
    source = library / "demo.mp4"
    source.write_bytes(b"x")
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    thumb = cfg.thumbs_dir / "v-preview2d-v5.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"jpg")
    metadata = {"duration": 10.0, "width": 640, "height": 320, "projection": "perspective", "stereo": "sbs", "lens": "Linear"}
    analysis = CorrectionProfile(analysis_seconds=12.5, sampled_frames=4, target_samples=4).dict()
    app.state.db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,analysis_json,thumb_path,date_added,last_seen,last_analyzed)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("v", str(source), source.name, "demo", source.stat().st_size, source.stat().st_mtime_ns, "ready", json.dumps(metadata), json.dumps(analysis), str(thumb), now(), now(), now()),
    )
    with TestClient(app) as client:
        video = client.get("/api/videos/v").json()
        assert video["analysis"]["analysis_seconds"] == 12.5
        from heretune.worker import THUMBNAIL_LAYOUT_VERSION
        assert video["thumbnail_revision"].startswith(f"{THUMBNAIL_LAYOUT_VERSION}-")

def test_worker_reports_live_progress_and_persists_analysis_time(monkeypatch, tmp_path: Path):
    import heretune.worker as worker

    db = DB(tmp_path / "worker.sqlite3")
    metadata = {
        "duration": 60.0, "width": 1920, "height": 960,
        "projection": "perspective", "stereo": "sbs", "lens": "Linear",
        "bit_depth": 8, "is_hdr": False,
    }
    db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,date_added,last_seen)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("v", "demo.mp4", "demo.mp4", "demo", 1, 1, "queued", json.dumps(metadata), now(), now()),
    )
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.paths.data_dir = str(tmp_path / "data")
    monkeypatch.setattr(worker, "ensure_thumbnail", lambda *args, **kwargs: None)

    def fake_analyze(path, metadata, ffmpeg, analysis_cfg, on_sample=None):
        if on_sample:
            on_sample(1, 4)
            on_sample(2, 4)
            on_sample(4, 4)
        return CorrectionProfile(sampled_frames=4, requested_samples=4, target_samples=4)

    monkeypatch.setattr(worker, "analyze", fake_analyze)
    events = []
    worker.process(db, cfg, db.get("v"), lambda payload: events.append(dict(payload)))
    row = db.get("v")
    assert row["status"] == "ready"
    assert row["analysis"]["analysis_seconds"] >= 0.0
    analysis_events = [event for event in events if event["stage"] == "analysis"]
    assert any(event.get("completed") == 2 and event.get("total") == 4 for event in analysis_events)
    assert events[-1]["stage"] == "ready"

def test_library_selectively_requeues_only_gpu_preflight_false_negative(tmp_path: Path):
    from heretune.analyzer import ANALYSIS_VERSION
    from heretune.library import Library

    library = tmp_path / "movies"
    library.mkdir()
    source_a = library / "gpu-fallback.mp4"
    source_b = library / "healthy.mp4"
    source_a.write_bytes(b"a")
    source_b.write_bytes(b"b")
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.paths.data_dir = str(tmp_path / "data")
    db = DB(tmp_path / "db.sqlite3")
    stamp = now()
    for video_id, source, analysis in (
        ("a", source_a, {"analysis_version": ANALYSIS_VERSION, "gpu_preflight_fallback": True}),
        ("b", source_b, {"analysis_version": ANALYSIS_VERSION, "gpu_preflight_fallback": False}),
    ):
        stat = source.stat()
        db.exec(
            """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,analysis_json,date_added,last_seen)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (video_id, str(source), source.name, source.stem, stat.st_size, stat.st_mtime_ns, "ready", json.dumps(analysis), stamp, stamp),
        )
    # IDs used by the library are path hashes, so update rows to those exact IDs.
    from heretune.library import sid
    db.exec("DELETE FROM videos")
    for source, analysis in (
        (source_a, {"analysis_version": ANALYSIS_VERSION, "gpu_preflight_fallback": True}),
        (source_b, {"analysis_version": ANALYSIS_VERSION, "gpu_preflight_fallback": False}),
    ):
        stat = source.stat()
        db.exec(
            """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,analysis_json,date_added,last_seen)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (_library_id(library, source.name), str(source), source.name, source.stem, stat.st_size, stat.st_mtime_ns, "ready", json.dumps(analysis), stamp, stamp),
        )
    result = Library(db, cfg).scan()
    assert result["stale_requeued"] == 1
    assert db.get(_library_id(library, source_a.name))["status"] == "queued"
    assert db.get(_library_id(library, source_b.name))["status"] == "ready"
