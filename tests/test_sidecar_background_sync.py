from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from heretune import __version__
from heretune.analyzer import ANALYSIS_VERSION, CorrectionProfile
from heretune.config import Config
from heretune.hsp import build
from heretune.library import now, sid
from heretune.server import create_app


def _insert_ready_video(app, root: Path, name: str = "ready.mp4") -> tuple[str, Path]:
    source = root / name
    source.write_bytes(b"video" * 128)
    stat = source.stat()
    video_id = sid(name)
    metadata = {
        "duration": 60.0,
        "width": 1920,
        "height": 960,
        "stereo": "sbs",
        "projection": "equirectangular",
    }
    correction = CorrectionProfile(analysis_version=ANALYSIS_VERSION)
    blob = build(
        video_id,
        source.stem,
        60.0,
        correction,
        metadata=metadata,
        source_filename=source.name,
    )
    stamp = now()
    app.state.db.exec(
        """INSERT INTO videos(
               id,path,rel_path,library_root,title,size,mtime_ns,status,metadata_json,
               analysis_json,auto_hsp,auto_hsp_version,date_added,last_seen,last_analyzed
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            video_id,
            str(source),
            name,
            str(root),
            source.stem,
            stat.st_size,
            stat.st_mtime_ns,
            "ready",
            json.dumps(metadata),
            json.dumps(correction.dict()),
            blob,
            7,
            stamp,
            stamp,
            stamp,
        ),
    )
    return video_id, source


def _wait_for_sync(client: TestClient, timeout: float = 5.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        payload = client.get("/api/status").json()
        last = dict(payload.get("last_sidecar_sync") or {})
        if last.get("completed_at"):
            return last
        time.sleep(0.03)
    raise AssertionError(f"sidecar sync did not complete in time; last={last!r}")


def test_version_and_engine_revision():
    assert __version__ == "0.13.0"
    assert ANALYSIS_VERSION == "offset-free-tone-1"


def test_enabling_global_sidecars_schedules_nonblocking_retroactive_sync(tmp_path: Path):
    root = tmp_path / "movies"
    root.mkdir()
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(root)]
    cfg.library.roots = [str(root)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.hsp.write_auto_sidecars = False
    app = create_app(cfg)
    _, source = _insert_ready_video(app, root)
    target = source.with_suffix(".hsp")

    with TestClient(app) as client:
        response = client.put("/api/config", json={"hsp": {"write_auto_sidecars": True}})
        assert response.status_code == 200
        body = response.json()
        assert body["sidecar_sync_scheduled"] is True
        assert "background" in body["message"].lower()
        result = _wait_for_sync(client)

    assert target.is_file()
    assert int(result.get("written") or 0) == 1
    assert int(result.get("errors") or 0) == 0


def test_disabling_global_sidecars_stops_maintenance_but_does_not_delete_files(tmp_path: Path):
    root = tmp_path / "movies"
    root.mkdir()
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(root)]
    cfg.library.roots = [str(root)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.hsp.write_auto_sidecars = False
    app = create_app(cfg)
    _, source = _insert_ready_video(app, root)
    target = source.with_suffix(".hsp")

    with TestClient(app) as client:
        assert client.put("/api/config", json={"hsp": {"write_auto_sidecars": True}}).json()["sidecar_sync_scheduled"] is True
        _wait_for_sync(client)
        assert target.is_file()
        disabled = client.put("/api/config", json={"hsp": {"write_auto_sidecars": False}})
        assert disabled.status_code == 200
        assert disabled.json()["sidecar_sync_scheduled"] is False
        time.sleep(0.08)

    assert target.is_file()


def test_sidecar_sync_progress_is_exposed_in_status_and_ui_assets():
    root = Path(__file__).resolve().parents[1]
    js = (root / "heretune/static/app.js").read_text(encoding="utf-8")
    html = (root / "heretune/static/index.html").read_text(encoding="utf-8")
    server = (root / "heretune/server.py").read_text(encoding="utf-8")
    assert '"sidecar_sync": sidecar_sync' in server
    assert 'source.addEventListener("sidecar-sync"' in js
    assert 'Syncing portable HSP sidecars' in js
    assert 'created/updated' in js
    assert 'renderSidecarStages' in js
    assert 'id="workStrip"' in html
    assert "Existing Ready videos are synchronizing portable HSP sidecars" in js
