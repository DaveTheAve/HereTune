from __future__ import annotations

import json
import struct
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from heretune.analyzer import CorrectionProfile
from heretune.config import Config
from heretune.hsp import build, decompress_proxy, inspect
from heretune.library import now, sid
from heretune.server import create_app



def _hs_path(app, video_id: str) -> str:
    row = app.state.db.get(video_id)
    assert row is not None
    return f"/heresphere/video/{video_id}/{quote(Path(str(row['path'])).name, safe='')}"

def _ready_app(tmp_path: Path, name: str = "Studio_Test_LR_MKX200.mp4"):
    library = tmp_path / "movies"
    library.mkdir()
    source = library / name
    source.write_bytes(bytes(range(256)) * 4)
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    db = app.state.db
    stamp = now()
    video_id = sid(source.name)
    st = source.stat()
    metadata = {
        "duration": 60.0,
        "width": 5760,
        "height": 2880,
        "video_codec": "hevc",
        "projection": "fisheye",
        "stereo": "sbs",
        "fov": 200.0,
        "lens": "MKX200",
        "isEyeSwapped": False,
    }
    correction = CorrectionProfile(r_gain=1.04, b_gain=0.98, contrast=1.03)
    blob = build(
        video_id,
        source.stem,
        60.0,
        correction,
        metadata=metadata,
        source_filename=source.name,
    )
    db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,
           analysis_json,auto_hsp,auto_hsp_version,date_added,last_seen,last_analyzed)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            video_id,
            str(source),
            source.name,
            source.stem,
            st.st_size,
            st.st_mtime_ns,
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
    return app, source, video_id


def test_heresphere_advertises_fresh_filename_bearing_scene_identity(tmp_path: Path):
    app, source, video_id = _ready_app(tmp_path)
    encoded = quote(source.name, safe="")
    expected_path = f"/heresphere/video/{video_id}/{encoded}"
    with TestClient(app) as client:
        library = client.get("/heresphere").json()
        links = [url for group in library["library"] for url in group["list"]]
        assert any(url.endswith(expected_path) for url in links)

        scan = client.post("/heresphere/scan", json={}).json()
        assert any(item["link"].endswith(expected_path) for item in scan["scanData"])

        fresh = client.post(expected_path, json={"needsMediaSource": True})
        assert fresh.status_code == 200
        payload = fresh.json()
        assert payload["projection"] == "fisheye"
        assert payload["stereo"] == "sbs"
        assert payload["fov"] == 200.0
        assert payload["lens"] == "MKX200"
        assert payload["cameraIPD"] == 6.5
        assert payload["media"][0]["sources"][0]["url"].endswith(
            f"/media/{video_id}/{encoded}"
        )

        assert client.get(_hs_path(app, video_id)).status_code == 200
        assert client.get(
            f"/heresphere/video/{video_id}/Wrong_LR.mp4"
        ).status_code == 404


def test_generated_hsp_uses_canonical_empty_fstrings_and_description():
    blob = build(
        "vid",
        "Friendly",
        60.0,
        CorrectionProfile(r_gain=1.03),
        metadata={
            "projection": "equirectangular",
            "stereo": "sbs",
            "fov": 180.0,
            "lens": "Linear",
        },
        source_filename="Example_LR_180.mp4",
    )
    parsed = inspect(blob)
    assert parsed.description == ""

    raw = decompress_proxy(blob)
    assert struct.unpack_from("<i", raw, 0)[0] == 1024
    assert struct.unpack_from("<i", raw, 4)[0] == 7
    pos = 8  # standalone prefix + HSP version
    for _ in range(2):  # UniqueID, Title
        count = struct.unpack_from("<i", raw, pos)[0]
        assert count > 0
        pos += 4 + count
    # HereSphere's own presets serialize an empty Description as FString count 0.
    assert struct.unpack_from("<i", raw, pos)[0] == 0


def test_heresphere_reconciles_stale_cached_geometry_from_actual_filename(tmp_path: Path):
    library = tmp_path / "movies"; library.mkdir()
    source = library / "Studio_OldCache_LR_MKX200.mp4"; source.write_bytes(b"x" * 2048)
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]; cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg); db = app.state.db
    stamp = now(); video_id = sid(source.name); st = source.stat()
    stale = {
        "duration": 60.0, "width": 5760, "height": 2880, "video_codec": "hevc",
        "projection": "perspective", "stereo": "mono", "fov": 0.0,
        "lens": "Linear", "isEyeSwapped": False,
        # Deliberately model metadata cached before current filename rules.
        "vrformat_version": 1,
    }
    analysis = CorrectionProfile(r_gain=1.06, b_gain=0.97).dict()
    stale_hsp = build(video_id, source.stem, 60.0, CorrectionProfile(r_gain=1.06, b_gain=0.97), metadata=stale, source_filename=source.name)
    db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,
           analysis_json,auto_hsp,auto_hsp_version,date_added,last_seen,last_analyzed)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (video_id, str(source), source.name, source.stem, st.st_size, st.st_mtime_ns,
         "ready", json.dumps(stale), json.dumps(analysis), stale_hsp, 7, stamp, stamp, stamp),
    )
    encoded = quote(source.name, safe="")
    fresh_path = f"/heresphere/video/{video_id}/{encoded}"
    with TestClient(app) as client:
        payload = client.post(fresh_path, json={"needsMediaSource": True}).json()
        assert payload["projection"] == "fisheye"
        assert payload["stereo"] == "sbs"
        assert payload["fov"] == 200.0
        assert payload["lens"] == "MKX200"
        debug = client.get(f"/api/videos/{video_id}/heresphere-debug").json()
        assert "filename-stereo" in debug["geometry_source"]
        assert "filename-projection" in debug["geometry_source"]
        assert debug["stored_geometry"]["projection"] == "perspective"
        assert debug["served_geometry"]["projection"] == "fisheye"
        assert debug["scene_url"].endswith(fresh_path)

        # Startup reconciliation is HSP-only: saved analysis survives untouched,
        # while the native Format/Lens keyframes now match the filename geometry.
        updated = db.get(video_id)
        parsed = inspect(updated["auto_hsp"])
        assert parsed.format_keyframes[0].projection_mode == 2
        assert parsed.format_keyframes[0].stereo_mode == 1
        assert parsed.lens_keyframes[0].true_lens_name == "iZugar MKX200"
        assert updated["analysis"] == analysis


def test_heresphere_explicit_layout_override_beats_filename_rule(tmp_path: Path):
    app, source, video_id = _ready_app(tmp_path, "OverrideWins_LR_MKX200.mp4")
    app.state.db.exec(
        "UPDATE videos SET layout_override_json=? WHERE id=?",
        (json.dumps({"projection": "perspective", "stereo": "mono", "fov": 90.0, "lens": "Linear"}), video_id),
    )
    encoded = quote(source.name, safe="")
    fresh_path = f"/heresphere/video/{video_id}/{encoded}"
    with TestClient(app) as client:
        payload = client.post(fresh_path, json={"needsMediaSource": True}).json()
        assert payload["projection"] == "perspective"
        assert payload["stereo"] == "mono"
        assert payload["fov"] == 90.0
        assert payload["lens"] == "Linear"
        debug = client.get(f"/api/videos/{video_id}/heresphere-debug").json()
        assert debug["geometry_source"].startswith("override:")
        parsed = inspect(app.state.db.get(video_id)["auto_hsp"])
        assert parsed.format_keyframes[0].projection_mode == 0
        assert parsed.format_keyframes[0].stereo_mode == 0
