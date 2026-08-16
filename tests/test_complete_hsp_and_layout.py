from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from heretune.analyzer import CorrectionProfile
from heretune.config import Config
from heretune.hsp import build, inspect
from heretune.library import now, sid
from heretune.server import create_app


COMPLETE_GROUPS = (
    "format", "lens", "stitch", "alignment", "orientation", "origin",
    "motion", "autofocus", "sync", "transition", "image", "audio", "environment",
)



def _hs_path(app, video_id: str) -> str:
    row = app.state.db.get(video_id)
    assert row is not None
    return f"/heresphere/video/{video_id}/{quote(Path(str(row['path'])).name, safe='')}"

def test_complete_v7_hsp_carries_mkx200_projection_and_identity():
    metadata = {
        "projection": "fisheye", "stereo": "sbs", "fov": 200.0,
        "lens": "MKX200", "isEyeSwapped": False,
        "width": 5760, "height": 2880,
    }
    blob = build(
        "vid", "Friendly title", 90.0, CorrectionProfile(r_gain=1.05),
        metadata=metadata, source_filename="Studio_Scene_MKX200.mp4",
    )
    parsed = inspect(blob)
    assert all(parsed.array_counts[name] == 1 for name in COMPLETE_GROUPS)
    assert parsed.unique_id == "Studio_Scene_MKX200.mp4"
    assert parsed.title == "Studio_Scene_MKX200.mp4"
    fmt = parsed.format_keyframes[0]
    assert fmt.projection_mode == 2  # Fisheye
    assert fmt.stereo_mode == 1      # SideBySide
    assert fmt.aspect_ratio_type == 4
    assert abs(fmt.aspect_ratio - 2.0) < 1e-6
    lens = parsed.lens_keyframes[0]
    assert lens.true_lens_name == "iZugar MKX200"
    assert lens.export_lens_name == "Linear"
    assert abs(lens.true_fov - 200.0) < 1e-6
    assert abs(lens.true_lens_calibration[0] - 0.5898000001907349) < 1e-6
    assert abs(lens.export_lens_calibration[0] - 0.6366199851036072) < 1e-6


def test_complete_v7_hsp_carries_top_bottom_and_eye_swap():
    parsed = inspect(build(
        "vid", "tb", 30.0, CorrectionProfile(),
        metadata={
            "projection": "equirectangular", "stereo": "tb",
            "isEyeSwapped": True, "fov": 180.0, "lens": "Linear",
        },
        source_filename="Example_BT_180.mp4",
    ))
    fmt = parsed.format_keyframes[0]
    assert fmt.projection_mode == 1
    assert fmt.stereo_mode == 2
    assert fmt.eye_swap_enabled is True
    assert fmt.aspect_ratio == 1.0


def test_heresphere_playback_url_preserves_real_filename_and_hsp_is_complete(tmp_path: Path):
    library = tmp_path / "movies"
    library.mkdir()
    source = library / "Company_Test_LR_MKX200.mp4"
    source.write_bytes(bytes(range(256)) * 4)
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    db = app.state.db
    metadata = {
        "duration": 60.0, "width": 5760, "height": 2880, "video_codec": "hevc",
        "projection": "fisheye", "stereo": "sbs", "fov": 200.0,
        "lens": "MKX200", "isEyeSwapped": False,
    }
    stamp = now(); video_id = sid(source.name); correction = CorrectionProfile(r_gain=1.04)
    blob = build(video_id, source.stem, 60.0, correction, metadata=metadata, source_filename=source.name)
    stat = source.stat()
    db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,
           analysis_json,auto_hsp,auto_hsp_version,date_added,last_seen,last_analyzed)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (video_id, str(source), source.name, source.stem, stat.st_size, stat.st_mtime_ns,
         "ready", json.dumps(metadata), json.dumps(correction.dict()), blob, 7, stamp, stamp, stamp),
    )
    with TestClient(app) as client:
        detail = client.post(_hs_path(app, video_id), json={"needsMediaSource": True}).json()
        url = detail["media"][0]["sources"][0]["url"]
        assert url.endswith(f"/media/{video_id}/{source.name}")
        assert "_LR_MKX200.mp4" in url
        hsp_url = detail["hspArray"][0]["url"]
        hsp_response = client.get(hsp_url)
        assert hsp_response.status_code == 200
        parsed = inspect(hsp_response.content)
        assert parsed.unique_id == source.name
        assert parsed.format_keyframes[0].projection_mode == 2
        assert parsed.format_keyframes[0].stereo_mode == 1
        assert parsed.image_keyframes[0].gain[0] > 1.0


def test_named_media_route_rejects_wrong_filename(tmp_path: Path):
    library = tmp_path / "movies"; library.mkdir()
    source = library / "Correct_LR.mp4"; source.write_bytes(b"abc" * 100)
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]; cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg); db = app.state.db; st = source.stat(); stamp = now(); video_id = sid(source.name)
    db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,date_added,last_seen)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (video_id, str(source), source.name, source.stem, st.st_size, st.st_mtime_ns, "queued", stamp, stamp),
    )
    with TestClient(app) as client:
        assert client.get(f"/media/{video_id}/Wrong_2D.mp4").status_code == 404


def test_layout_override_rebuilds_complete_auto_hsp_without_reanalysis(tmp_path: Path, monkeypatch):
    library = tmp_path / "movies"; library.mkdir()
    source = library / "Override_Test_LR.mp4"; source.write_bytes(b"x")
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]; cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg); db = app.state.db
    stamp = now(); video_id = sid(source.name); st = source.stat()
    metadata = {"duration": 60.0, "width": 3840, "height": 1920, "projection": "equirectangular", "stereo": "sbs", "fov": 180.0, "lens": "Linear"}
    analysis = CorrectionProfile(r_gain=1.02).dict()
    blob = build(video_id, source.stem, 60.0, CorrectionProfile(r_gain=1.02), metadata=metadata, source_filename=source.name)
    db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,
           analysis_json,auto_hsp,auto_hsp_version,date_added,last_seen,last_analyzed)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (video_id, str(source), source.name, source.stem, st.st_size, st.st_mtime_ns,
         "ready", json.dumps(metadata), json.dumps(analysis), blob, 7, stamp, stamp, stamp),
    )
    # Avoid invoking FFmpeg for a thumbnail in this API-only regression.
    monkeypatch.setattr("heretune.server.ensure_thumbnail", lambda *args, **kwargs: None)
    with TestClient(app) as client:
        response = client.patch(f"/api/videos/{video_id}/layout", json={"projection": "fisheye", "fov": 200, "lens": "MKX200"})
        assert response.status_code == 200
        updated = db.get(video_id)
        parsed = inspect(updated["auto_hsp"])
        assert parsed.format_keyframes[0].projection_mode == 2
        assert parsed.lens_keyframes[0].true_lens_name == "iZugar MKX200"
        assert updated["analysis"] == analysis
