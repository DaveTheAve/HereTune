from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

import heretune.accel as accel
from heretune.accel import WorkerPlan
from heretune.config import Config
from heretune.live import LiveHub
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


def _insert(app, source: Path, video_id: str = "v") -> None:
    metadata = {
        "duration": 10.0,
        "width": 3840,
        "height": 1920,
        "projection": "equirectangular",
        "stereo": "sbs",
        "lens": "Linear",
        "fov": 180.0,
        "video_codec": "h264",
        "isEyeSwapped": True,
    }
    app.state.db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,
           analysis_json,date_added,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            video_id, str(source), source.name, source.stem, source.stat().st_size,
            source.stat().st_mtime_ns, "ready", json.dumps(metadata),
            json.dumps({"sampled_frames": 48, "target_samples": 48}), now(), now(),
        ),
    )


def test_adaptive_worker_plan_backs_off_under_pressure(monkeypatch, tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.analysis.adaptive_workers = True
    cfg.analysis.gpu_decode_workers = 4
    cfg.analysis.cpu_analysis_workers = 8
    cfg.analysis.probe_workers = 6
    accel._RESOURCE_PLAN_CACHE.clear()
    monkeypatch.setattr(accel, "_host_telemetry", lambda: (92.0, 94.0))
    monkeypatch.setattr(accel, "gpu_telemetry", lambda *a, **k: {
        "gpu_utilization": 95.0,
        "decoder_utilization": 88.0,
    })
    monkeypatch.setattr(accel, "_nas_latency_ms", lambda root: 180.0)
    plan = accel.resource_worker_plan(cfg.analysis, cfg.library.roots[0], max_age=0)
    assert plan.gpu_workers == 1
    assert plan.cpu_workers == 1
    assert plan.probe_workers == 1
    assert plan.adaptive is True
    assert any("CPU" in reason for reason in plan.reasons)
    assert any("NAS" in reason for reason in plan.reasons)


def test_live_hub_sends_hello_and_published_event():
    async def scenario():
        hub = LiveHub()
        hub.bind(asyncio.get_running_loop())
        stream = hub.stream()
        hello = await anext(stream)
        assert "event: hello" in hello
        hub.publish("library", reason="ready", video_id="v")
        await asyncio.sleep(0)
        message = await asyncio.wait_for(anext(stream), timeout=1)
        assert "event: library" in message
        assert '"video_id":"v"' in message
        await stream.aclose()
    asyncio.run(scenario())


def test_web_player_and_direct_full_quality_stream(tmp_path: Path):
    cfg = _cfg(tmp_path)
    source = Path(cfg.library.roots[0]) / "demo.mp4"
    source.write_bytes(b"0123456789abcdef")
    app = create_app(cfg)
    app.state.work_paused = True
    _insert(app, source)
    with TestClient(app) as client:
        page = client.get("/watch/v")
        assert page.status_code == 200
        assert "2D VR View" in page.text
        assert "Compare" in page.text
        assert "FULL QUALITY" in page.text
        direct = client.get("/play/v/demo.mp4", headers={"Range": "bytes=3-7"})
        assert direct.status_code == 206
        assert direct.content == b"34567"
        assert direct.headers["x-heretune-original"] == "1"
        assert direct.headers["x-heretune-transcoded"] == "0"
        detail = client.get("/api/videos/v").json()
        assert detail["isEyeSwapped"] is True


def test_dashboard_v06_card_cleanup_and_priority_placement():
    root = Path(__file__).resolve().parents[1]
    html = (root / "heretune/static/index.html").read_text(encoding="utf-8")
    js = (root / "heretune/static/app.js").read_text(encoding="utf-8")
    css = (root / "heretune/static/styles.css").read_text(encoding="utf-8")
    assert 'id="statPending"' in html
    assert 'id="statQueueDetail"' in html
    assert 'id="statGpu"' not in html
    assert 'id="statManual"' not in html
    assert 'id="statQueueEta"' not in html
    assert "Not analyzed" in js
    assert "correction-priority" in js
    assert ".not-analyzed-cell" in css
    assert 'new EventSource("/api/live")' in js
    assert 'id="handoffDialog"' not in html


def test_player_has_live_comparison_and_vr_2d_controls():
    root = Path(__file__).resolve().parents[1]
    html = (root / "heretune/static/player.html").read_text(encoding="utf-8")
    js = (root / "heretune/static/player.js").read_text(encoding="utf-8")
    assert 'data-color="split"' in html
    assert 'id="splitRange"' in html
    assert 'data-view="vr2d"' in html
    assert 'data-view="eye"' in html
    assert 'id="nativePlayerLink"' in html
    assert "uProjection" in js
    assert "uSplit" in js
    assert "zero transcode" in js
