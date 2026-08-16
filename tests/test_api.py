from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from heretune.analyzer import CorrectionProfile
from heretune.config import Config
from heretune.hsp import build
from heretune.library import now, sid
from heretune.server import create_app



def _hs_path(app, video_id: str) -> str:
    row = app.state.db.get(video_id)
    assert row is not None
    return f"/heresphere/video/{video_id}/{quote(Path(str(row['path'])).name, safe='')}"

def test_heresphere_api_and_range_streaming(tmp_path: Path):
    library = tmp_path / "movies"
    library.mkdir()
    source = library / "demo_MKX200.mp4"
    source.write_bytes(bytes(range(256)) * 8)

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")

    app = create_app(cfg)
    db = app.state.db
    stat = source.stat()
    video_id = sid(source.name)
    timestamp = now()
    metadata = {
        "duration": 120.0,
        "width": 3840,
        "height": 1920,
        "video_codec": "hevc",
        "projection": "fisheye",
        "stereo": "sbs",
        "fov": 200.0,
        "lens": "MKX200",
        "isEyeSwapped": False,
    }
    correction = CorrectionProfile()
    db.exec(
        """INSERT INTO videos(
               id,path,rel_path,title,size,mtime_ns,status,metadata_json,
               analysis_json,auto_hsp,auto_hsp_version,date_added,last_seen,last_analyzed
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            video_id, str(source), source.name, "demo", stat.st_size, stat.st_mtime_ns,
            "ready", json.dumps(metadata), json.dumps(correction.dict()),
            build(video_id, "demo", 120.0, correction, metadata=metadata, source_filename=source.name), 7,
            timestamp, timestamp, timestamp,
        ),
    )

    with TestClient(app) as client:
        index = client.get("/heresphere")
        assert index.status_code == 200
        assert index.headers["heresphere-json-version"] == "1"

        scan_detail = client.get(_hs_path(app, video_id)).json()
        assert "media" not in scan_detail
        assert "hspArray" not in scan_detail

        playback_detail = client.post(
            _hs_path(app, video_id), json={"needsMediaSource": True}
        ).json()
        assert playback_detail["hspArray"][0]["version"] == 7
        assert playback_detail["media"][0]["sources"][0]["url"].endswith(
            f"/media/{video_id}/{source.name}"
        )

        partial = client.get(
            f"/media/{video_id}/{source.name}", headers={"Range": "bytes=10-19"}
        )
        assert partial.status_code == 206
        assert partial.content == source.read_bytes()[10:20]

        named_partial = client.get(
            f"/media/{video_id}/{source.name}", headers={"Range": "bytes=20-29"}
        )
        assert named_partial.status_code == 206
        assert named_partial.content == source.read_bytes()[20:30]

        invalid = client.get(
            f"/media/{video_id}/{source.name}", headers={"Range": "bytes=999999-"}
        )
        assert invalid.status_code == 416
        assert invalid.headers["content-range"] == f"bytes */{stat.st_size}"
