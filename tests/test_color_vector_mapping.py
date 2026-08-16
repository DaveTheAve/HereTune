from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heretune.analyzer import CorrectionProfile
from heretune.config import Config
from heretune.hsp import build, inspect
from heretune.library import now, sid
from heretune.server import create_app


def test_whole_image_controls_use_native_global_w_with_neutral_rgb():
    correction = CorrectionProfile(
        r_gain=1.041,
        g_gain=0.983,
        b_gain=0.998,
        contrast=1.084,
        brightness=0.029,
        gamma=0.972,
        saturation=1.017,
    )
    parsed = inspect(build("vid", "Movie", 60.0, correction, source_filename="Movie.mp4"))
    image = parsed.image_keyframes[0]

    assert image.saturation == pytest.approx((1.0, 1.0, 1.0, 1.017), abs=1e-5)
    assert image.contrast == pytest.approx((1.0, 1.0, 1.0, 1.084), abs=1e-5)
    assert image.gamma == pytest.approx((1.0, 1.0, 1.0, 0.972), abs=1e-5)
    assert image.gain == pytest.approx((1.041, 0.983, 0.998, 1.0), abs=1e-5)
    assert image.offset == pytest.approx((0.0, 0.0, 0.0, 0.029), abs=1e-5)


def test_debug_endpoint_reports_full_native_vectors(tmp_path: Path):
    library = tmp_path / "movies"
    library.mkdir()
    source = library / "Mapping_Test_LR_180.mp4"
    source.write_bytes(b"mapping-test")

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.library.scan_interval_seconds = 600
    app = create_app(cfg)
    db = app.state.db

    correction = CorrectionProfile(
        r_gain=1.041,
        g_gain=0.983,
        b_gain=0.998,
        contrast=1.084,
        gamma=0.972,
        saturation=1.017,
    )
    metadata = {
        "duration": 60.0,
        "width": 5760,
        "height": 2880,
        "projection": "equirectangular",
        "stereo": "sbs",
        "fov": 180.0,
        "lens": "Linear",
        "isEyeSwapped": False,
    }
    video_id = sid(f"{library.resolve()}::{source.name}")
    stamp = now()
    st = source.stat()
    blob = build(video_id, source.stem, 60.0, correction, metadata=metadata, source_filename=source.name)
    db.exec(
        """INSERT INTO videos(id,path,rel_path,library_root,title,size,mtime_ns,status,metadata_json,
           analysis_json,auto_hsp,auto_hsp_version,date_added,last_seen,last_analyzed)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            video_id, str(source), source.name, str(library), source.stem, st.st_size, st.st_mtime_ns,
            "ready", json.dumps(metadata), json.dumps(correction.dict()), blob, 7, stamp, stamp, stamp,
        ),
    )

    with TestClient(app) as client:
        payload = client.get(f"/api/videos/{video_id}/heresphere-debug").json()
    hsp = payload["hsp"]
    assert hsp["color_vector_mapping"] == "global-w-with-per-channel-rgb-gain"
    assert hsp["contrast"] == pytest.approx([1.0, 1.0, 1.0, 1.084], abs=1e-5)
    assert hsp["saturation"] == pytest.approx([1.0, 1.0, 1.0, 1.017], abs=1e-5)
    assert hsp["offset"] == pytest.approx([0.0, 0.0, 0.0, 0.0], abs=1e-5)
