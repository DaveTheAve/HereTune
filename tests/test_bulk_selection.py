from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from heretune.config import Config
from heretune.server import create_app, now


def _cfg(tmp_path: Path) -> Config:
    library = tmp_path / "library"
    library.mkdir(exist_ok=True)
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.analysis.execution_mode = "cpu"
    cfg.library.scan_interval_seconds = 3600
    return cfg


def _insert(app, source: Path, video_id: str, status: str = "queued") -> None:
    metadata = {
        "duration": 10.0, "width": 5760, "height": 2880,
        "projection": "equirectangular", "stereo": "sbs",
        "video_codec": "hevc", "audio_codecs": ["aac"],
        "bit_depth": 8, "is_hdr": False,
    }
    app.state.db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,date_added,last_seen)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (video_id, str(source), source.name, source.stem, source.stat().st_size,
         source.stat().st_mtime_ns, status, json.dumps(metadata), now(), now()),
    )


def test_bulk_select_ids_and_prioritize_front(tmp_path: Path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    app.state.work_paused = True
    for index, name in enumerate(["StudioX One.mp4", "StudioX Two.mp4", "Other.mp4"]):
        source = Path(cfg.library.roots[0]) / name
        source.write_bytes(name.encode())
        _insert(app, source, f"v{index}")
    with TestClient(app) as client:
        ids = client.get("/api/video-ids", params={"q": "StudioX"}).json()["ids"]
        assert ids == ["v0", "v1"]
        response = client.post("/api/videos/bulk-priority", json={"ids": ids, "priority": True, "position": "front"})
        assert response.status_code == 200
        assert response.json()["changed"] == 2
        queue = client.get("/api/queue").json()["items"]
        assert [item["id"] for item in queue[:2]] == ["v0", "v1"]


def test_bulk_priority_skips_ready_and_requeues_error(tmp_path: Path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    app.state.work_paused = True
    rows = [("queued", "q"), ("error", "e"), ("ready", "r")]
    ids = []
    for index, (status, prefix) in enumerate(rows):
        source = Path(cfg.library.roots[0]) / f"{prefix}.mp4"
        source.write_bytes(prefix.encode())
        vid = f"v{index}"
        ids.append(vid)
        _insert(app, source, vid, status=status)
        if status == "error":
            app.state.db.exec("UPDATE videos SET error='old failure' WHERE id=?", (vid,))
    with TestClient(app) as client:
        payload = client.post("/api/videos/bulk-priority", json={"ids": ids, "priority": True}).json()
        assert payload["changed"] == 2
        assert payload["skipped"] == 1
        assert app.state.db.get("v1")["status"] == "queued"
        assert app.state.db.get("v1")["error"] is None


def test_player_has_source_attempt_guards_and_console_diagnostics():
    root = Path(__file__).resolve().parents[1]
    js = (root / "heretune/static/player.js").read_text(encoding="utf-8")
    assert "sourceAttempt" in js
    assert "sourceErrorHandledAttempt" in js
    assert "reportPlaybackEvent('media-error'" in js
    assert "[HereTune Web Player]" in js
    assert "setTimeout(() => handleMediaFailure(attempt), 500)" in js


def test_dashboard_has_bulk_selection_controls():
    root = Path(__file__).resolve().parents[1]
    html = (root / "heretune/static/index.html").read_text(encoding="utf-8")
    js = (root / "heretune/static/app.js").read_text(encoding="utf-8")
    assert 'id="selectAllButton"' in html
    assert 'id="selectVisibleCheckbox"' in html
    assert 'id="prioritizeSelectionButton"' in html
    assert "/api/video-ids" in js
    assert "/api/videos/bulk-priority" in js
