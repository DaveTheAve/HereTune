from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from heretune.analyzer import CorrectionProfile
from heretune.config import Config
from heretune.hsp import build
from heretune.library import now, sid
from heretune.server import create_app


def _ready_app(tmp_path: Path):
    library = tmp_path / "movies"
    library.mkdir()
    source = library / "Forensic_Test_LR_MKX200.mp4"
    source.write_bytes(bytes(range(256)) * 4)
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.hsp.accept_hsp_writebacks = True
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
    correction = CorrectionProfile(
        r_gain=1.041,
        g_gain=0.983,
        b_gain=0.998,
        contrast=1.084,
        brightness=0.029,
        gamma=0.972,
        saturation=1.017,
    )
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
    return app, source, video_id, blob


def test_video_post_logs_even_without_hsp(tmp_path: Path, capsys):
    app, source, video_id, _ = _ready_app(tmp_path)
    path = (
        f"/heresphere/video/{video_id}/"
        f"{quote(source.name, safe='')}"
    )
    with TestClient(app) as client:
        response = client.post(path, json={"needsMediaSource": True})
        assert response.status_code == 200
    output = capsys.readouterr().out
    assert "[HereTune HereSphere] VIDEO POST" in output
    assert "keys=needsMediaSource" in output
    assert "hsp=no" in output


def test_accepts_form_encoded_hsp_writeback_and_logs_it(tmp_path: Path, capsys):
    app, source, video_id, blob = _ready_app(tmp_path)
    path = (
        f"/heresphere/video/{video_id}/"
        f"{quote(source.name, safe='')}"
    )
    with TestClient(app) as client:
        response = client.post(path, data={"hsp": base64.b64encode(blob).decode("ascii")})
        assert response.status_code == 200
        row = app.state.db.get(video_id)
        assert row["manual_hsp"] == blob
    output = capsys.readouterr().out
    assert "application/x-www-form-urlencoded" in output
    assert "keys=hsp" in output
    assert "hsp=yes" in output
    assert "HSP WRITEBACK" in output


def test_debug_exposes_authoritative_hsp_url_and_image_time(tmp_path: Path):
    app, _, video_id, _ = _ready_app(tmp_path)
    with TestClient(app) as client:
        payload = client.get(f"/api/videos/{video_id}/heresphere-debug").json()
    assert payload["write_hsp_advertised"] is True
    assert payload["hsp_url"].endswith(payload["hsp"]["revision"])
    assert payload["hsp"]["image_time_ticks"] == 0
    counts = payload["hsp"]["array_counts"]
    assert counts["tags"] == 0
    assert all(value == 1 for key, value in counts.items() if key != "tags")
