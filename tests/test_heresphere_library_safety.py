from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from heretune import __version__
from heretune.analyzer import CorrectionProfile
from heretune.config import Config, config_to_toml, load_config
from heretune.hsp import build
from heretune.library import now, sid
from heretune.server import _heresphere_library_where, create_app



def _hs_path(app, video_id: str) -> str:
    row = app.state.db.get(video_id)
    assert row is not None
    return f"/heresphere/video/{video_id}/{quote(Path(str(row['path'])).name, safe='')}"

def _insert_video(db, source: Path, root: Path, *, status: str, with_hsp: bool) -> str:
    stat = source.stat()
    rel = str(source.relative_to(root))
    video_id = sid(rel)
    metadata = {"duration": 60.0, "width": 1920, "height": 960, "stereo": "sbs", "projection": "equirectangular"}
    auto_hsp = build(video_id, source.stem, 60.0, CorrectionProfile(), metadata=metadata, source_filename=source.name) if with_hsp else None
    stamp = now()
    db.exec(
        """INSERT INTO videos(
               id,path,rel_path,library_root,title,size,mtime_ns,status,metadata_json,
               analysis_json,auto_hsp,auto_hsp_version,date_added,last_seen,last_analyzed
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            video_id, str(source), rel, str(root), source.stem, stat.st_size, stat.st_mtime_ns,
            status, json.dumps(metadata), json.dumps(CorrectionProfile().dict()) if status == "ready" else None,
            auto_hsp, 7 if auto_hsp else None, stamp, stamp, stamp if status == "ready" else None,
        ),
    )
    return video_id


def test_version_and_ready_only_default():
    assert __version__ == "0.13.0"
    cfg = Config()
    assert cfg.server.heresphere_ready_only is True
    assert "status='ready'" in _heresphere_library_where(cfg)


def test_ready_only_setting_round_trips(tmp_path: Path):
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.server.heresphere_ready_only = False
    cfg.config_path.write_text(config_to_toml(cfg), encoding="utf-8")
    loaded = load_config(cfg.config_path)
    assert loaded.server.heresphere_ready_only is False


def test_heresphere_library_and_scan_hide_unready_items(tmp_path: Path):
    root = tmp_path / "movies"
    root.mkdir()
    ready = root / "ready.mp4"
    pending = root / "pending.mp4"
    ready_without_hsp = root / "ready_without_hsp.mp4"
    for path in (ready, pending, ready_without_hsp):
        path.write_bytes(b"video")

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(root)]
    cfg.library.roots = [str(root)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.server.heresphere_ready_only = True
    app = create_app(cfg)
    db = app.state.db
    ready_id = _insert_video(db, ready, root, status="ready", with_hsp=True)
    pending_id = _insert_video(db, pending, root, status="pending", with_hsp=False)
    no_hsp_id = _insert_video(db, ready_without_hsp, root, status="ready", with_hsp=False)

    with TestClient(app) as client:
        library_payload = client.get("/heresphere").json()
        scan_payload = client.get("/heresphere/scan").json()

    links = [link for group in library_payload["library"] for link in group["list"]]
    scan_links = [item["link"] for item in scan_payload["scanData"]]
    assert any(ready_id in link for link in links)
    assert any(ready_id in link for link in scan_links)
    assert not any(pending_id in link for link in links + scan_links)
    assert not any(no_hsp_id in link for link in links + scan_links)


def test_disabling_ready_only_restores_pending_but_never_missing(tmp_path: Path):
    root = tmp_path / "movies"
    root.mkdir()
    pending = root / "pending.mp4"
    missing = root / "missing.mp4"
    pending.write_bytes(b"pending")
    missing.write_bytes(b"missing")

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(root)]
    cfg.library.roots = [str(root)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.server.heresphere_ready_only = False
    app = create_app(cfg)
    db = app.state.db
    pending_id = _insert_video(db, pending, root, status="pending", with_hsp=False)
    missing_id = _insert_video(db, missing, root, status="missing", with_hsp=False)

    with TestClient(app) as client:
        payload = client.get("/heresphere").json()
    links = [link for group in payload["library"] for link in group["list"]]
    assert any(pending_id in link for link in links)
    assert not any(missing_id in link for link in links)


def test_ready_only_toggle_is_exposed_in_settings_ui():
    root = Path(__file__).resolve().parents[1]
    html = (root / "heretune/static/index.html").read_text(encoding="utf-8")
    assert 'name="server.heresphere_ready_only"' in html
    assert "Show only correction-ready videos in HereSphere" in html


def test_library_browsing_does_not_advertise_hsp_until_playback(tmp_path: Path):
    root = tmp_path / "movies"
    root.mkdir()
    source = root / "ready.mp4"
    source.write_bytes(b"video")

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(root)]
    cfg.library.roots = [str(root)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    video_id = _insert_video(app.state.db, source, root, status="ready", with_hsp=True)

    with TestClient(app) as client:
        metadata = client.get(_hs_path(app, video_id)).json()
        assert "media" not in metadata
        assert "hspArray" not in metadata

        playback = client.post(
            _hs_path(app, video_id),
            json={"needsMediaSource": True},
        ).json()
        assert "media" in playback
        assert "hspArray" in playback
