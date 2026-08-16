from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import heretune.server as server
import heretune.worker as worker
from heretune.analyzer import CorrectionProfile
from heretune.config import Config
from heretune.db import DB
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


def _metadata(codec: str = "hevc") -> dict[str, object]:
    return {
        "duration": 10.0,
        "width": 5760,
        "height": 2880,
        "projection": "equirectangular",
        "stereo": "sbs",
        "lens": "Linear",
        "fov": 180.0,
        "video_codec": codec,
        "audio_codecs": ["aac"],
        "bit_depth": 8,
        "is_hdr": False,
    }


def _insert(app, source: Path, video_id: str, status: str) -> None:
    app.state.db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,date_added,last_seen)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            video_id, str(source), source.name, source.stem, source.stat().st_size,
            source.stat().st_mtime_ns, status, json.dumps(_metadata()), now(), now(),
        ),
    )


def test_readiness_process_generates_thumbnail_before_analysis(monkeypatch, tmp_path: Path):
    db = DB(tmp_path / "worker.sqlite3")
    cfg = _cfg(tmp_path)
    db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,date_added,last_seen)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("v", "demo.mp4", "demo.mp4", "demo", 1, 1, "queued", json.dumps(_metadata("h264")), now(), now()),
    )

    order: list[str] = []
    thumbnail_path = tmp_path / "thumb.jpg"

    def fake_thumbnail(*args, **kwargs):
        order.append("thumbnail")
        thumbnail_path.write_bytes(b"jpeg")
        db.exec("UPDATE videos SET thumb_path=? WHERE id=?", (str(thumbnail_path), "v"))
        return str(thumbnail_path)

    def fake_analyze(path, metadata, ffmpeg, analysis_cfg, on_sample=None, **kwargs):
        order.append("analysis")
        if on_sample:
            on_sample(1, 2)
            on_sample(2, 2)
        return CorrectionProfile(sampled_frames=2, requested_samples=2, target_samples=2)

    monkeypatch.setattr(worker, "thumbnail_is_current", lambda row: False)
    monkeypatch.setattr(worker, "ensure_thumbnail", fake_thumbnail)
    monkeypatch.setattr(worker, "analyze", fake_analyze)
    events: list[dict[str, object]] = []
    worker.process(db, cfg, db.get("v"), lambda payload: events.append(dict(payload)))
    row = db.get("v")
    assert row["status"] == "ready"
    assert row["thumb_path"] == str(thumbnail_path)
    assert order == ["thumbnail", "analysis"]
    assert any(event.get("stage") == "thumbnail" for event in events)
    assert any(event.get("stage") == "analysis" for event in events)


def test_thumbnail_request_is_deferred_while_analysis_queue_has_work(tmp_path: Path):
    cfg = _cfg(tmp_path)
    ready = Path(cfg.library.roots[0]) / "ready.mp4"
    queued = Path(cfg.library.roots[0]) / "queued.mp4"
    ready.write_bytes(b"ready")
    queued.write_bytes(b"queued")
    app = create_app(cfg)
    app.state.work_paused = True
    _insert(app, ready, "ready", "ready")
    _insert(app, queued, "queued", "queued")
    with TestClient(app) as client:
        response = client.get("/thumb/ready.jpg")
        assert response.status_code == 200
        assert response.headers.get("x-heretune-preview-deferred") == "1"
        assert response.headers["content-type"].startswith("image/svg+xml")
        assert app.state.db.get("queued")["status"] == "queued"
        assert app.state.db.get("ready")["status"] == "ready"


def test_browser_compatibility_commands_prefer_lossless_remux_then_h264(monkeypatch, tmp_path: Path):
    cfg = _cfg(tmp_path)
    video = {"path": str(Path(cfg.library.roots[0]) / "movie.mkv"), "metadata": _metadata("hevc")}
    remux, label = server._web_compat_command(video, cfg, mode="remux", start=12.5)
    assert "copy" in remux
    assert "hvc1" in remux
    assert "-ss" in remux
    assert "lossless-video" in label

    monkeypatch.setattr(server, "_web_nvenc_works", lambda ffmpeg: False)
    transcode, label = server._web_compat_command(video, cfg, mode="transcode", start=0)
    assert "libx264" in transcode
    assert "scale=3840:-2:flags=lanczos" in transcode
    assert "H.264 compatibility stream" in label


def test_web_compatibility_route_streams_mp4(monkeypatch, tmp_path: Path):
    cfg = _cfg(tmp_path)
    source = Path(cfg.library.roots[0]) / "demo.mkv"
    source.write_bytes(b"fake")
    app = create_app(cfg)
    app.state.work_paused = True
    _insert(app, source, "v", "ready")
    monkeypatch.setattr(server, "_web_compat_command", lambda *a, **k: (["fake-ffmpeg"], "test stream"))
    monkeypatch.setattr(server, "_stream_ffmpeg", lambda command, **kwargs: iter([b"ftyp", b"moof", b"mdat"]))
    with TestClient(app) as client:
        response = client.get("/webmedia/v.mp4?mode=transcode&start=3")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("video/mp4")
        assert response.content == b"ftypmoofmdat"


def test_web_player_has_automatic_three_step_fallback():
    root = Path(__file__).resolve().parents[1]
    js = (root / "heretune/static/player.js").read_text(encoding="utf-8")
    html = (root / "heretune/static/player.html").read_text(encoding="utf-8")
    assert "compatibilityFirstStep" in js
    assert "setVideoSource('transcode'" in js
    assert "/webmedia/" in js
    assert "Original video quality" in js
    assert 'id="sourcePillLabel"' in html


def test_worker_adaptive_provider_uses_python_inspect_and_completes(monkeypatch, tmp_path: Path):
    """Regression for the Python inspect-module/HSP-inspect name collision.

    The real server always passes worker_plan_provider. The production server passes it, so
    inspect.signature(analyze) was never reached even though production was.
    """
    db = DB(tmp_path / "adaptive.sqlite3")
    cfg = _cfg(tmp_path)
    db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,date_added,last_seen)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("v", "demo.mp4", "demo.mp4", "demo", 1, 1, "queued", json.dumps(_metadata("h264")), now(), now()),
    )

    def fake_analyze(path, metadata, ffmpeg, analysis_cfg, on_sample=None, worker_plan_provider=None):
        assert callable(worker_plan_provider)
        if on_sample:
            on_sample(1, 1)
        return CorrectionProfile(sampled_frames=1, requested_samples=1, target_samples=1)

    monkeypatch.setattr(worker, "analyze", fake_analyze)
    worker.process(db, cfg, db.get("v"), worker_plan_provider=lambda: {"cpu_workers": 1})
    row = db.get("v")
    assert row["status"] == "ready"
    assert row["error"] is None


