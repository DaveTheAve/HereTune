from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from heretune import __version__
from heretune.analyzer import CorrectionProfile
from heretune.analysis_cache import AnalysisCache
from heretune.config import Config
from heretune.db import DB
from heretune.hsp import build
from heretune.library import now
from heretune.media import PREVIEW_RENDER_VERSION, _scene_correction_at, apply_correction_preview
from heretune.server import _selected_hsp_preview_correction, create_app


def test_release_copy_and_selected_app_surfaces():
    root = Path(__file__).resolve().parents[1]
    html = (root / "heretune/static/index.html").read_text(encoding="utf-8")
    js = (root / "heretune/static/app.js").read_text(encoding="utf-8")
    css = (root / "heretune/static/styles.css").read_text(encoding="utf-8")
    player = (root / "heretune/static/player.js").read_text(encoding="utf-8")
    assert __version__ == "0.13.0"
    assert "Adaptive HSP Calibration" in html
    assert "YOUR HERE" in html and "SPHERE" in html and "COMPANION" in html
    assert "Intelligent correction." in html and "Original media." in html
    assert "No NAS filing for emotional damages." in html
    assert "Analysis inspector" in js
    assert "Rejected sample gallery" in js
    assert "Checkpoint cache" in js
    assert "loadLibraryHealth" in js
    assert "analysis-timeline" in css
    assert "srgbToLinear" in player and "linearToSrgb" in player
    assert "updateGradeIndicator" in player
    assert "Playback diagnostics" in (root / "heretune/static/player.html").read_text(encoding="utf-8")


def test_config_enables_isolation_and_checkpoint_cache():
    cfg = Config()
    assert cfg.analysis.isolate_analysis_worker is True
    assert cfg.analysis.analysis_cache_enabled is True
    assert cfg.analysis.analysis_cache_max_gb == 20.0
    assert cfg.analysis.analysis_width == 960


def test_analysis_cache_round_trips_numpy_statistics_without_frames(tmp_path: Path):
    cache = AnalysisCache(tmp_path / "cache", "video-fingerprint", max_gb=1)
    stat = {
        "channel_means": np.asarray([0.3, 0.4, 0.5], dtype=np.float64),
        "signature": np.asarray([0.1, 0.2, 0.3], dtype=np.float32),
        "p50": np.float64(0.44),
    }
    cache.put_stat("full", 12.345, 960, stat)
    cache.close()
    reopened = AnalysisCache(tmp_path / "cache", "video-fingerprint", max_gb=1)
    restored = reopened.get_stat("full", 12.345, 960)
    assert restored is not None
    assert np.allclose(restored["channel_means"], stat["channel_means"])
    assert np.allclose(restored["signature"], stat["signature"])
    assert not hasattr(reopened, "put_frame")
    assert not hasattr(reopened, "existing_frame")


def test_database_run_history_backup_and_integrity(tmp_path: Path):
    db = DB(tmp_path / "data" / "heretune.sqlite3")
    run = db.start_analysis_run("video-a", "6.2")
    db.update_analysis_run(run, stage="full-sampling", planned=110, details={"resumed": 20})
    db.finish_analysis_run(run, status="ready", stage="complete", used=101, rejected=9, duration=85.0)
    history = db.analysis_history("video-a")
    assert history[0]["status"] == "ready"
    assert history[0]["used_samples"] == 101
    assert history[0]["rejected_samples"] == 9
    assert db.integrity_check() == ["ok"]
    backup = db.backup_to(tmp_path / "backups" / "one.sqlite3")
    assert backup.is_file() and backup.stat().st_size > 0


def test_nested_scene_preview_uses_native_scene_values():
    correction = {
        "gain_r": 1.0,
        "gain_g": 1.0,
        "gain_b": 1.0,
        "scene_keyframes": [
            {"time_seconds": 0, "native": {"gain_r": 1.0}},
            {"time_seconds": 30, "native": {"gain_r": 1.08, "exposure_bias": 0.04}},
        ],
    }
    selected = _scene_correction_at(correction, 45)
    assert selected["gain_r"] == 1.08
    assert selected["exposure_bias"] == 0.04
    assert "scene_keyframes" not in selected



def test_comparator_reads_exact_selected_hsp_values():
    correction = CorrectionProfile(
        r_gain=1.07, g_gain=0.98, b_gain=1.02,
        contrast=1.03, brightness=-0.005, gamma=0.99, saturation=1.04,
    )
    blob = build("selected", "Selected", 60.0, correction)
    video = {
        "id": "selected", "title": "Selected", "path": "/tmp/selected.mp4",
        "rel_path": "selected.mp4", "status": "ready", "profile_mode": "auto",
        "auto_hsp": blob, "auto_hsp_version": 7,
        "metadata": {"duration": 60.0}, "analysis": {},
    }
    payload = _selected_hsp_preview_correction(video, Config())
    assert payload["preview_source"] == "selected-hsp"
    assert abs(payload["gain_r"] - 1.07) < 1e-5
    assert abs(payload["gain_g"] - 0.98) < 1e-5
    assert abs(payload["contrast"] - 1.03) < 1e-5
    assert abs(payload["offset"] + 0.005) < 1e-5
    assert abs(payload["saturation"] - 1.04) < 1e-5
    assert PREVIEW_RENDER_VERSION == 8

def test_comparator_mild_grade_does_not_crush_midtones():
    grad = np.linspace(24, 232, 256, dtype=np.uint8)
    frame = np.repeat(grad[None, :, None], 48, axis=0)
    frame = np.repeat(frame, 3, axis=2)
    correction = {
        "gain_r": 1.02, "gain_g": 0.99, "gain_b": 1.01,
        "contrast": 1.015, "gamma": 0.995, "saturation": 1.02,
        "offset": -0.004, "exposure_bias": 0.02,
        "white_temp": 6300, "white_tint": -0.01,
        "shadows": 0.02, "midtones": 1.01, "highlights": 0.99,
        "sharpen": 2.1,
    }
    after = apply_correction_preview(frame, correction)
    # A mild native grade may shift the picture, but it should not collapse a
    # healthy midrange in the monitor approximation.
    before_mid = frame[:, 64:192].astype(np.float64).mean()
    after_mid = after[:, 64:192].astype(np.float64).mean()
    assert 0.93 <= after_mid / before_mid <= 1.07
    assert len(np.unique(after[0, :, 0])) > 160


def test_bulk_priority_is_fifo_even_if_old_client_requests_front(tmp_path: Path):
    root = tmp_path / "movies"; root.mkdir()
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(root)]; cfg.library.scan_interval_seconds = 9999
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    with TestClient(app) as client:
        db = app.state.db; stamp = now()
        for idx in range(3):
            path = root / f"q{idx}.mp4"; path.write_bytes(b"x")
            st = path.stat()
            db.exec(
                "INSERT OR REPLACE INTO videos(id,path,rel_path,title,size,mtime_ns,status,date_added,last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"q{idx}", str(path), path.name, path.stem, st.st_size, st.st_mtime_ns, "queued", stamp, stamp),
            )
        assert client.post("/api/videos/q0/priority", json={"priority": True}).status_code == 200
        response = client.post("/api/videos/bulk-priority", json={"ids": ["q1", "q2"], "priority": True, "position": "front"})
        assert response.status_code == 200
        queue = client.get("/api/queue").json()["items"]
        assert [item["id"] for item in queue[:3]] == ["q0", "q1", "q2"]


def test_narrow_layout_and_explicit_row_action_are_packaged():
    root = Path(__file__).resolve().parents[1] / "heretune/static"
    css = (root / "styles.css").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    assert '@media (max-width: 390px)' in css
    assert 'grid-template-areas:' in css and '"confidence"' in css
    assert '$$("#videoRows .row-action")' in js
    assert 'position: "back"' in js


def test_profile_note_and_pipeline_chip_polish_are_packaged():
    root = Path(__file__).resolve().parents[1] / "heretune/static"
    js = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "styles.css").read_text(encoding="utf-8")
    assert "Versioned HSP URLs make HereSphere fetch the newest HereTune profile" in js
    assert "Always Load HSP for Web API media" in js
    assert "Auto Create HSP" not in js
    assert "Auto Update HSP" not in js
    assert "flex-wrap:wrap" in css and "border-radius:999px" in css
    assert ".work-progress{grid-column:1 / -1; margin:2px 0 0 40px}" in css


def test_layout_polish_packaged():
    root = Path(__file__).resolve().parents[1] / "heretune/static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "styles.css").read_text(encoding="utf-8")
    assert "<h1><span>Intelligent correction.</span><em>Original media.</em></h1>" in html
    assert "pipeline-stage-mark" in js
    assert ".library-panel .section-heading" in css
    assert "@media (max-width: 1480px)" in css


def test_queue_table_and_sample_fallback_packaged():
    root = Path(__file__).resolve().parents[1] / "heretune/static"
    js = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "styles.css").read_text(encoding="utf-8")
    assert "function analysisSampleUrl" in js
    assert '["ready", "Ready"]' not in js
    assert ".table-wrap table { min-width: 1500px; }" in css or "min-width:1680px !important" in css
    assert "Keep a real scrollable table instead of variable-height card rows" in css


def test_stage_table_and_scene_stability_polish():
    root = Path(__file__).resolve().parents[1]
    js = (root / "heretune/static/app.js").read_text(encoding="utf-8")
    css = (root / "heretune/static/styles.css").read_text(encoding="utf-8")
    html = (root / "heretune/static/index.html").read_text(encoding="utf-8")
    server = (root / "heretune/server.py").read_text(encoding="utf-8")
    assert '["finalizing", "HSP"]' in js
    assert '["ready", "Ready"]' not in js
    assert 'font-weight:500 !important' in css
    assert 'class="table-scroll-proxy" id="tableScrollProxy"' in html
    assert 'function initializeFloatingTableScrollbar' in js
    assert 'requestAnimationFrame(refreshFloatingTableScrollbar)' in js
    assert 'video?.thumbnail_path' in js
    assert 'def _video_analysis_cache' in server
    assert 'sceneCount || 0) <= 1' in js
    assert 'Auto Create HSP' not in html
    assert 'Auto Update HSP' not in html


def test_inspector_sample_endpoint_falls_back_to_real_thumbnail(tmp_path: Path, monkeypatch):
    import heretune.server as server_module

    root = tmp_path / "movies"
    root.mkdir()
    source = root / "sample.mp4"
    source.write_bytes(b"not-a-real-video")
    thumb = tmp_path / "thumb.jpg"
    thumb_bytes = b"\xff\xd8\xff\xdbHereTuneThumb\xff\xd9"
    thumb.write_bytes(thumb_bytes)

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(root)]
    cfg.library.scan_interval_seconds = 9999
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)

    def fail_preview(*_args, **_kwargs):
        raise RuntimeError("synthetic sample decode failure")

    monkeypatch.setattr(server_module, "preview_frame", fail_preview)
    with TestClient(app) as client:
        db = app.state.db
        stamp = now()
        st = source.stat()
        db.exec(
            "INSERT OR REPLACE INTO videos(id,path,rel_path,title,size,mtime_ns,status,date_added,last_seen,thumb_path,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "sample-video", str(source), source.name, source.stem,
                st.st_size, st.st_mtime_ns, "ready", stamp, stamp, str(thumb),
                json.dumps({"duration": 60.0, "width": 1920, "height": 1080, "stereo": "mono", "projection": "perspective"}),
            ),
        )
        response = client.get("/api/videos/sample-video/analysis-sample.jpg?kind=full&time_seconds=12.0&width=640")
        assert response.status_code == 200
        assert response.content == thumb_bytes


def test_analysis_cache_exposes_statistics_only(tmp_path: Path):
    cache = AnalysisCache(tmp_path / "cache", "no-images", max_gb=1)
    assert not hasattr(cache, "put_frame")
    assert not hasattr(cache, "existing_frame")
    assert not any(path.name.casefold() == "frames" for path in (tmp_path / "cache").rglob("*"))


def test_inspector_images_are_live_source_seeks_not_cache(tmp_path: Path, monkeypatch):
    import cv2
    import heretune.server as server_module

    root = tmp_path / "movies"
    root.mkdir()
    source = root / "sample.mp4"
    source.write_bytes(b"source")
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(root)]
    cfg.library.scan_interval_seconds = 9999
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)

    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    frame[..., 0] = 230
    monkeypatch.setattr(server_module, "preview_frame", lambda *args, **kwargs: frame.copy())
    with TestClient(app) as client:
        db = app.state.db
        stamp = now()
        st = source.stat()
        db.exec(
            "INSERT OR REPLACE INTO videos(id,path,rel_path,title,size,mtime_ns,status,date_added,last_seen,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("live", str(source), source.name, source.stem, st.st_size, st.st_mtime_ns, "ready", stamp, stamp,
             json.dumps({"duration": 60.0, "width": 1920, "height": 1080, "stereo": "mono", "projection": "perspective"})),
        )
        response = client.get("/api/videos/live/analysis-sample.jpg?kind=full&time_seconds=12.0&width=640")
        assert response.status_code == 200
        assert response.headers["x-heretune-frame-source"] == "live-source-seek"
        assert "no-store" in response.headers["cache-control"]
        decoded = cv2.imdecode(np.frombuffer(response.content, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert decoded is not None
        decoded_rgb = cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)
        assert float(decoded_rgb[..., 0].mean()) > 180
        assert not any(cfg.analysis_cache_dir.rglob("*.jpg"))


def test_precise_scene_stage_keeps_elapsed_and_eta(monkeypatch, tmp_path: Path):
    import json
    import heretune.worker as worker

    db = DB(tmp_path / "eta.sqlite3")
    metadata = {
        "duration": 600.0, "width": 1920, "height": 960,
        "projection": "perspective", "stereo": "sbs", "lens": "Linear",
        "bit_depth": 8, "is_hdr": False,
    }
    db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,date_added,last_seen)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("eta", "eta.mp4", "eta.mp4", "eta", 1, 1, "queued", json.dumps(metadata), now(), now()),
    )
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.paths.data_dir = str(tmp_path / "data")
    monkeypatch.setattr(worker, "ensure_thumbnail", lambda *args, **kwargs: None)

    ticks = iter([0.0, 0.0, 1.0, 1.0, 2.0, 6.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0, 21.0, 22.0, 23.0, 24.0, 25.0])
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(ticks, 26.0))

    def fake_analyze(path, metadata, ffmpeg, analysis_cfg, on_sample=None, on_stage=None, **kwargs):
        if on_stage:
            on_stage("scene-probe", {"width": 480})
            on_stage("scene-probe", {"completed": 25, "total": 100, "width": 480})
            on_stage("scene-probe", {"completed": 50, "total": 100, "width": 480})
            on_stage("boundary-refinement", {"completed": 2, "total": 10, "width": 480})
        if on_sample:
            on_sample(1, 2)
            on_sample(2, 2)
        return CorrectionProfile(sampled_frames=2, requested_samples=2, target_samples=2)

    monkeypatch.setattr(worker, "analyze", fake_analyze)
    events = []
    worker.process(db, cfg, db.get("eta"), lambda payload: events.append(dict(payload)))
    scene = [event for event in events if event.get("stage") == "scene-probe" and event.get("completed") == 50][0]
    assert scene["elapsed_seconds"] > 0
    assert scene["eta_seconds"] is not None
    assert scene["eta_seconds"] > 0
    refine = [event for event in events if event.get("stage") == "boundary-refinement" and event.get("completed") == 2][0]
    assert refine["elapsed_seconds"] > 0
    assert refine["eta_seconds"] is not None


def test_live_evidence_ui_and_settings_are_data_only():
    root = Path(__file__).resolve().parents[1] / "heretune/static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    assert "Cache inspector frames" not in html
    assert "No analysis images are stored" in html
    assert "function initializeLiveSampleImages" in js
    assert "Data only—inspector images are decoded live" in js
    assert "statistics only" in js


def test_doctor_duplicate_table_and_stage_polish_packaged():
    root = Path(__file__).resolve().parents[1] / "heretune/static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "styles.css").read_text(encoding="utf-8")
    assert 'id="healthButton"' not in html
    assert 'data-doctor-tab="health"' in html
    assert 'class="col-status"' in html and 'class="col-confidence"' in html
    assert 'function splitRelativePath' in js
    assert 'Current file' in js or 'CURRENT FILE' in js
    assert 'redundantWarningPrefixes' in js
    assert '.table-actions .sort-filter { order:99; margin-left:0;' in css
    assert '.pagination-top { min-height:36px; margin-left:auto;' in css
    assert '.pipeline-stage-mark { display:inline-flex !important;' in css


def test_compact_table_proportions_and_pending_stage_marker():
    root = Path(__file__).resolve().parents[1] / "heretune/static"
    css = (root / "styles.css").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    assert '#videoTable .col-status { width:100px !important; }' in css
    assert '#videoTable .col-corrections { width:420px !important; }' in css
    assert '#videoTable .col-confidence { width:105px !important; }' in css
    assert '#videoTable .col-video { width:auto !important; }' in css
    assert '.shell { width:min(1560px, calc(100% - clamp(30px, 7vw, 110px))) !important; }' in css
    assert 'min-width:1360px !important' in css
    assert '"○"' in js
    assert 'if (wrap.scrollLeft !== 0) wrap.scrollLeft = 0;' in js
