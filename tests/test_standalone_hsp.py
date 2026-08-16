from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

import pytest
from urllib.parse import quote

from fastapi.testclient import TestClient

from heretune.analyzer import CorrectionProfile
from heretune.config import Config
from heretune.hsp import (
    HSPError,
    STANDALONE_HSP_PREFIX,
    build,
    compress_proxy,
    decompress_proxy,
    inspect,
    version,
)
from heretune.library import now, sid
from heretune.server import create_app


def test_generated_standalone_hsp_has_native_1024_prefix():
    blob = build(
        "vid", "Avatar", 60.0,
        CorrectionProfile(contrast=1.075060606),
        metadata={"projection": "fisheye", "stereo": "sbs", "fov": 200.0, "lens": "MKX200"},
        source_filename="Avatar (2160).mp4",
    )
    raw = decompress_proxy(blob)
    assert struct.unpack_from("<i", raw, 0)[0] == STANDALONE_HSP_PREFIX == 1024
    assert struct.unpack_from("<i", raw, 4)[0] == 7
    assert version(blob) == 7
    assert inspect(blob).container_prefix == 1024
    parsed = inspect(blob)
    assert parsed.container_prefix == 1024
    assert abs(parsed.image_keyframes[0].contrast[3] - 1.075060606) < 1e-6


def test_unwrapped_embedded_payload_is_rejected_as_standalone():
    blob = build("id", "Title", 60.0, CorrectionProfile())
    raw = decompress_proxy(blob)
    embedded_style = compress_proxy(raw[4:])
    assert version(embedded_style) is None
    with pytest.raises(HSPError, match="wrapper is missing"):
        inspect(embedded_style)


def test_native_wrapped_writeback_is_v7_not_1024(tmp_path: Path, capsys):
    library = tmp_path / "movies"
    library.mkdir()
    source = library / "Avatar (2160).mp4"
    source.write_bytes(b"x" * 1024)
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
    metadata = {"duration": 60.0, "projection": "fisheye", "stereo": "sbs", "fov": 200.0, "lens": "MKX200"}
    correction = CorrectionProfile(contrast=1.075060606)
    blob = build(video_id, source.stem, 60.0, correction, metadata=metadata, source_filename=source.name)
    db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,
           analysis_json,auto_hsp,auto_hsp_version,date_added,last_seen,last_analyzed)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (video_id, str(source), source.name, source.stem, st.st_size, st.st_mtime_ns,
         "ready", json.dumps(metadata), json.dumps(correction.dict()), blob, 7, stamp, stamp, stamp),
    )
    path = f"/heresphere/video/{video_id}/{quote(source.name, safe='')}"
    with TestClient(app) as client:
        response = client.post(path, json={"hsp": base64.b64encode(blob).decode("ascii")})
        assert response.status_code == 200
        row = app.state.db.get(video_id)
        assert row["manual_hsp"] == blob
        assert row["manual_hsp_version"] == 7
    output = capsys.readouterr().out
    assert "HSP WRITEBACK" in output
    assert "v7" in output
    assert "contrast=1.000/1.000/1.000/1.075" in output


