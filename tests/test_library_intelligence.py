from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from heretune.analyzer import ANALYSIS_VERSION, _smart_select_statistics
from heretune.config import Config
from heretune.db import DB
from heretune.library import Library
from heretune.server import create_app, now
from heretune.vrformat import apply_layout_override, detect_visual_layout


def _cfg(tmp_path: Path) -> Config:
    library = tmp_path / "library"
    library.mkdir(exist_ok=True)
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.analysis.execution_mode = "cpu"
    cfg.library.scan_interval_seconds = 3600
    return cfg


def _insert_video(app, source: Path, video_id: str, *, status: str = "queued", duration: float = 100.0):
    metadata = {
        "duration": duration,
        "width": 1920,
        "height": 960,
        "projection": "perspective",
        "stereo": "sbs",
        "lens": "Linear",
        "video_codec": "h264",
    }
    app.state.db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,
           date_added,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (video_id, str(source), source.name, source.stem, source.stat().st_size,
         source.stat().st_mtime_ns, status, json.dumps(metadata), now(), now()),
    )


def test_visual_layout_detects_untagged_sbs_pair():
    yy, xx = np.mgrid[:180, :180]
    left = np.zeros((180, 180, 3), dtype=np.uint8)
    left[..., 0] = ((xx // 15) % 2 * 180 + 40).astype(np.uint8)
    left[..., 1] = ((yy // 18) % 2 * 160 + 50).astype(np.uint8)
    left[..., 2] = (((xx + yy) // 20) % 2 * 140 + 60).astype(np.uint8)
    right = left.copy()
    frame = np.concatenate([left, right], axis=1)
    detected = detect_visual_layout(frame, "ordinary_movie.mp4", 3600, 1800)
    assert detected["resolved"]["stereo"] == "sbs"
    assert detected["stereo_confidence"] >= 0.50
    assert "similarity" in detected["stereo_reason"]


def test_layout_override_is_explicit_and_reversible():
    base = {"stereo": "sbs", "projection": "fisheye", "fov": 180.0, "lens": "Linear"}
    changed = apply_layout_override(base, {"stereo": "tb", "projection": "perspective", "fov": 90.0})
    assert changed["stereo"] == "tb"
    assert changed["projection"] == "perspective"
    assert changed["fov"] == 90.0
    assert changed["layout_override_active"] is True
    assert apply_layout_override(base, {}) == base


def test_smart_sampling_rejects_bad_and_duplicate_candidates():
    sig_a = np.zeros((16,), dtype=np.float32)
    sig_b = np.ones((16,), dtype=np.float32) * 0.4
    rows = [
        {"_timestamp": 1.0, "p50": .02, "p95": .04, "luma_spread": .02, "black_fraction": .97, "median_sat": .02, "edge_density": .01, "signature": sig_a},
        {"_timestamp": 2.0, "p50": .45, "p95": .8, "luma_spread": .5, "black_fraction": .03, "median_sat": .3, "edge_density": .08, "signature": sig_a},
        {"_timestamp": 3.0, "p50": .46, "p95": .81, "luma_spread": .5, "black_fraction": .03, "median_sat": .3, "edge_density": .08, "signature": sig_a + .002},
        {"_timestamp": 4.0, "p50": .50, "p95": .85, "luma_spread": .5, "black_fraction": .02, "median_sat": .32, "edge_density": .07, "signature": sig_b},
        {"_timestamp": 5.0, "p50": .52, "p95": .86, "luma_spread": .5, "black_fraction": .02, "median_sat": .34, "edge_density": .07, "signature": sig_b + .2},
    ]
    selected, rejected = _smart_select_statistics(rows, 3)
    assert len(selected) == 3
    assert rejected["black_fade"] == 1
    assert rejected["duplicate"] >= 1


def test_fingerprint_preserves_identity_across_rename(tmp_path: Path):
    cfg = _cfg(tmp_path)
    source = Path(cfg.library.roots[0]) / "before.mp4"
    source.write_bytes((b"abcdef" * 100000) + b"tail")
    db = DB(cfg.db_path)
    library = Library(db, cfg)
    first = library.scan()
    assert first["added"] == 1
    original = db.videos()[0]
    original_id = original["id"]
    db.exec(
        "UPDATE videos SET is_favorite=1,rating=4.5,watch_position_ms=12345,last_watched=?,status='ready',analysis_json=? WHERE id=?",
        (now(), json.dumps({"analysis_version": ANALYSIS_VERSION}), original_id),
    )
    renamed = source.with_name("after rename.mp4")
    source.rename(renamed)
    result = library.scan()
    assert result["renamed"] == 1
    row = db.videos()[0]
    assert row["id"] == original_id
    assert row["rel_path"] == renamed.name
    assert row["is_favorite"] == 1
    assert row["rating"] == 4.5
    assert row["watch_position_ms"] == 12345


def test_duplicate_detection_groups_identical_files(tmp_path: Path):
    cfg = _cfg(tmp_path)
    payload = b"same-media" * 90000
    (Path(cfg.library.roots[0]) / "a.mp4").write_bytes(payload)
    (Path(cfg.library.roots[0]) / "b.mp4").write_bytes(payload)
    db = DB(cfg.db_path)
    result = Library(db, cfg).scan()
    assert result["duplicate_groups"] == 1
    rows = db.videos()
    assert len(rows) == 2
    assert rows[0]["content_fingerprint"] == rows[1]["content_fingerprint"]
    assert rows[0]["duplicate_group"] == rows[1]["duplicate_group"]


def test_playback_events_persist_continue_watching_and_completion(tmp_path: Path):
    cfg = _cfg(tmp_path)
    source = Path(cfg.library.roots[0]) / "watch.mp4"
    source.write_bytes(b"x")
    app = create_app(cfg)
    _insert_video(app, source, "watch", status="ready", duration=100.0)
    with TestClient(app) as client:
        response = client.post("/events/watch", json={"event": 0, "time": 0, "speed": 1})
        assert response.status_code == 200
        client.post("/events/watch", json={"event": 2, "time": 50000, "speed": 1})
        detail = client.get("/api/videos/watch").json()
        assert detail["watch"]["play_count"] == 1
        assert detail["watch"]["position_ms"] == 50000
        assert detail["watch"]["completed"] is False
        cont = client.get("/api/videos?watch=continue").json()
        assert [item["id"] for item in cont["items"]] == ["watch"]
        client.post("/events/watch", json={"event": 3, "time": 95000, "speed": 1})
        detail = client.get("/api/videos/watch").json()
        assert detail["watch"]["completed"] is True
        assert client.get("/api/videos?watch=continue").json()["items"] == []
        assert [item["id"] for item in client.get("/api/videos?watch=completed").json()["items"]] == ["watch"]


def test_priority_queue_can_be_drag_reordered_via_api(tmp_path: Path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    app.state.work_paused = True
    for n in range(3):
        source = Path(cfg.library.roots[0]) / f"q{n}.mp4"
        source.write_bytes(bytes([n + 1]))
        _insert_video(app, source, f"q{n}")
    with TestClient(app) as client:
        for n in range(3):
            assert client.post(f"/api/videos/q{n}/priority", json={"priority": True}).status_code == 200
        ids = [item["id"] for item in client.get("/api/queue").json()["items"]]
        assert ids == ["q0", "q1", "q2"]
        response = client.put("/api/queue", json={"ids": ["q2", "q0", "q1"]})
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["items"]] == ["q2", "q0", "q1"]


def test_ui_contains_v05_controls_and_real_priority_button():
    root = Path(__file__).resolve().parents[1]
    html = (root / "heretune/static/index.html").read_text(encoding="utf-8")
    js = (root / "heretune/static/app.js").read_text(encoding="utf-8")
    css = (root / "heretune/static/styles.css").read_text(encoding="utf-8")
    assert 'id="queueButton"' in html
    assert 'id="watchFilter"' in html
    assert 'id="duplicateFilter"' in html
    assert 'analysis.smart_sampling' in html
    assert 'queue-priority-button' in js
    assert 'queue-priority-button' in css
    assert 'thumbnail calibration' in js.lower()
    assert 'Before / after preview' in js
