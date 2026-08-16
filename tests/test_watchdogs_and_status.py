from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import heretune.analyzer as analyzer
from heretune import __version__
from heretune.config import Config
from heretune.db import DB
from heretune.library import Library
from heretune.processes import active_process_count, run_capture
from heretune.server import _status_payload


def test_release_version_and_watchdog_defaults():
    cfg = Config()
    assert __version__ == "0.13.0"
    assert cfg.analysis.analysis_width == 960
    assert cfg.analysis.scene_probe_width == 480
    assert cfg.analysis.ffmpeg_sample_timeout_seconds == 20.0
    assert cfg.analysis.ffmpeg_aux_timeout_seconds == 45.0
    assert cfg.analysis.ffprobe_timeout_seconds == 30.0


def test_run_capture_timeout_cannot_leave_owned_child_stuck():
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_capture(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=0.15,
        )
    assert time.monotonic() - started < 3.0
    assert active_process_count() == 0


def test_sample_timeout_becomes_recoverable_sampling_error(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0] if args else ["ffmpeg"], 20)

    monkeypatch.setattr(analyzer, "run_capture", timeout)
    frame, error = analyzer._run_sample_command(["ffmpeg"], 16, 8, timeout_seconds=20)
    assert frame is None
    assert "timed out" in error.lower()
    assert "terminated" in error.lower()


def test_status_payload_never_materializes_full_video_rows(tmp_path: Path, monkeypatch):
    cfg = Config()
    cfg.paths.data_dir = str(tmp_path / "data")
    db = DB(tmp_path / "data" / "heretune.sqlite3")
    db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,date_added,last_seen,
           metadata_json,analysis_json,auto_hsp,auto_hsp_version)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "v1", str(tmp_path / "demo.mp4"), "demo.mp4", "Demo", 123, 1, "ready",
            "2026-08-14T00:00:00+00:00", "2026-08-14T00:00:00+00:00",
            '{"duration":1800,"width":5760,"height":2880,"video_codec":"hevc"}',
            '{"analysis_seconds":120,"sampled_frames":72,"requested_samples":72,"wb_confidence":0.8}',
            b"x" * (4 * 1024 * 1024), 7,
        ),
    )
    monkeypatch.setattr(db, "videos", lambda *a, **k: (_ for _ in ()).throw(AssertionError("SELECT * path used")))
    app = SimpleNamespace(state=SimpleNamespace(
        analysis_progress=None,
        analysis_progress_updated_at=None,
        scan_running=False,
        work_paused=False,
        worker_plan={},
    ))
    payload = _status_payload(db, cfg, app, "cpu", None)
    assert payload["stats"]["total"] == 1
    assert payload["stats"]["ready"] == 1
    assert payload["analysis"]["active_child_processes"] == 0


def test_library_scan_uses_projected_rows_not_hsp_blobs(tmp_path: Path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    cfg = Config()
    cfg.library.roots = [str(root)]
    cfg.paths.data_dir = str(tmp_path / "data")
    db = DB(tmp_path / "data" / "heretune.sqlite3")
    monkeypatch.setattr(db, "videos", lambda *a, **k: (_ for _ in ()).throw(AssertionError("full videos() used")))
    result = Library(db, cfg).scan()
    assert result["seen"] == 0


def test_dashboard_reports_server_loss_instead_of_stale_progress():
    text = (Path(__file__).parents[1] / "heretune" / "static" / "app.js").read_text(encoding="utf-8")
    assert "HereTune server connection lost" in text
    assert "waiting on decoder" in text
    assert "Server unavailable" in text
