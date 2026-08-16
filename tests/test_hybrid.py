from __future__ import annotations

from pathlib import Path
import os

from heretune import accel
from heretune.analyzer import build_sample_command, effective_sample_count
from heretune.config import Config


def _library_id(root: Path, relative: str) -> str:
    from heretune.library import sid
    root_key = os.path.normcase(os.path.normpath(str(root))).casefold()
    return sid(f"{root_key}::{relative}")


def test_auto_defaults_to_hybrid_preference_for_long_vr():
    cfg = Config()
    assert cfg.analysis.execution_mode == "auto"
    assert cfg.analysis.auto_preference == "hybrid"
    assert cfg.analysis.samples == 48
    assert cfg.analysis.analysis_width == 960
    assert cfg.analysis.gpu_decode_workers == 4
    assert cfg.analysis.cpu_analysis_workers == 8
    assert cfg.analysis.probe_workers == 8


def test_adaptive_sample_count():
    assert effective_sample_count(30 * 60, 48, True, 45.0, 72) == 48
    assert effective_sample_count(60 * 60, 48, True, 45.0, 72) == 72
    assert effective_sample_count(10.0, 48, True, 45.0, 72) == 40


def test_cuda_sample_command_keeps_resize_on_gpu():
    command = build_sample_command(
        Path(r"C:\\movies\\demo.mp4"), "ffmpeg", 123.5, 960, 480, "gpu", 0
    )
    joined = " ".join(command)
    assert "-hwaccel cuda" in joined
    assert "-hwaccel_output_format cuda" in joined
    assert "scale_cuda=960:480" in joined
    assert "hwdownload" in joined
    assert command[-1] == "pipe:1"


def test_auto_resolves_to_hybrid_when_cuda_is_usable(monkeypatch):
    cfg = Config().analysis
    caps = accel.CudaCapabilities(True, True, True, True, True, "ok")
    monkeypatch.setattr(accel, "detect_cuda", lambda ffmpeg, device: caps)
    mode, found = accel.resolve_execution_mode(cfg, "ffmpeg")
    assert mode == "hybrid"
    assert found is caps


def test_auto_falls_back_to_cpu_when_cuda_is_unavailable(monkeypatch):
    cfg = Config().analysis
    caps = accel.CudaCapabilities(True, True, False, True, False, "no scale_cuda")
    monkeypatch.setattr(accel, "detect_cuda", lambda ffmpeg, device: caps)
    mode, found = accel.resolve_execution_mode(cfg, "ffmpeg")
    assert mode == "cpu"
    assert found is caps


def test_stale_analysis_is_requeued_without_touching_manual_hsp(tmp_path):
    import json
    from heretune.db import DB
    from heretune.library import Library, now

    root = tmp_path / "movies"
    root.mkdir()
    source = root / "old.mp4"
    source.write_bytes(b"not-a-real-video")

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(root)]
    cfg.paths.data_dir = str(tmp_path / "data")
    db = DB(cfg.db_path)
    stat = source.stat()
    video_id = _library_id(root, source.name)
    stamp = now()
    db.exec(
        """INSERT INTO videos(
               id,path,rel_path,title,size,mtime_ns,status,analysis_json,
               auto_hsp,auto_hsp_version,manual_hsp,manual_hsp_version,
               date_added,last_seen
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            video_id, str(source), source.name, "old", stat.st_size, stat.st_mtime_ns,
            "ready", json.dumps({"analysis_version": "2.3-native-hsp"}),
            b"old-auto", 7, b"manual-stays", 7, stamp, stamp,
        ),
    )
    result = Library(db, cfg).scan()
    row = db.get(video_id)
    assert result["stale_requeued"] == 1
    assert row["status"] == "queued"
    assert row["auto_hsp"] is None
    assert row["manual_hsp"] == b"manual-stays"


def test_skipped_video_is_not_requeued_every_scan(tmp_path):
    import json
    from heretune.db import DB
    from heretune.library import Library, now

    root = tmp_path / "movies"
    root.mkdir()
    source = root / "hdr.mp4"
    source.write_bytes(b"not-a-real-video")

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(root)]
    cfg.paths.data_dir = str(tmp_path / "data")
    db = DB(cfg.db_path)
    stat = source.stat()
    video_id = _library_id(root, source.name)
    stamp = now()
    db.exec(
        """INSERT INTO videos(
               id,path,rel_path,title,size,mtime_ns,status,analysis_json,
               date_added,last_seen
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            video_id, str(source), source.name, "hdr", stat.st_size, stat.st_mtime_ns,
            "skipped", json.dumps({"reason": "HDR safety skip"}), stamp, stamp,
        ),
    )
    result = Library(db, cfg).scan()
    row = db.get(video_id)
    assert result["stale_requeued"] == 0
    assert row["status"] == "skipped"
