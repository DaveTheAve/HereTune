from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest
from fastapi.testclient import TestClient

from heretune.analyzer import CorrectionProfile
from heretune.config import Config
from heretune.hsp import build, inspect
from heretune.library import now, sid
from heretune.server import create_app


UNICODE_FILENAMES = [
    "Teacher’s Test_LR_180.mp4",
    "Test ｜ Another_LR_180.mp4",
    "Café_LR_180.mp4",
    "日本語_LR_180.mp4",
    "Movie ❤️ Test_LR_180.mp4",
]


def _hs_path(video_id: str, filename: str) -> str:
    return f"/heresphere/video/{video_id}/{quote(filename, safe='')}"


def _path_and_query(url: str) -> str:
    split = urlsplit(url)
    return split.path + (f"?{split.query}" if split.query else "")


def _assert_latin1_headers(response) -> None:
    for name, value in response.headers.items():
        name.encode("latin-1")
        value.encode("latin-1")


@pytest.mark.parametrize("filename", UNICODE_FILENAMES)
def test_unicode_media_and_hsp_filenames_never_escape_into_http_headers(tmp_path: Path, filename: str):
    library = tmp_path / "movies"
    library.mkdir()
    source = library / filename
    source.write_bytes(bytes(range(64)) * 16)

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")

    app = create_app(cfg)
    db = app.state.db
    stat = source.stat()
    video_id = sid(filename)
    timestamp = now()
    metadata = {
        "duration": 90.0,
        "width": 3840,
        "height": 1920,
        "video_codec": "hevc",
        "projection": "equirectangular",
        "stereo": "sbs",
        "fov": 180.0,
        "lens": "Linear",
        "isEyeSwapped": False,
    }
    correction = CorrectionProfile()
    blob = build(
        video_id,
        source.stem,
        90.0,
        correction,
        metadata=metadata,
        source_filename=filename,
    )
    db.exec(
        """INSERT INTO videos(
               id,path,rel_path,title,size,mtime_ns,status,metadata_json,
               analysis_json,auto_hsp,auto_hsp_version,date_added,last_seen,last_analyzed
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            video_id,
            str(source),
            filename,
            source.stem,
            stat.st_size,
            stat.st_mtime_ns,
            "ready",
            json.dumps(metadata),
            json.dumps(correction.dict()),
            blob,
            7,
            timestamp,
            timestamp,
            timestamp,
        ),
    )

    with TestClient(app) as client:
        playback = client.post(
            _hs_path(video_id, filename),
            json={"needsMediaSource": True},
        )
        assert playback.status_code == 200
        detail = playback.json()

        media_url = detail["media"][0]["sources"][0]["url"]
        media = client.get(_path_and_query(media_url), headers={"Range": "bytes=0-31"})
        assert media.status_code == 206
        assert media.content == source.read_bytes()[:32]
        _assert_latin1_headers(media)
        disposition = media.headers["content-disposition"]
        assert "filename*=UTF-8''" in disposition
        assert filename not in disposition

        hsp_url = detail["hspArray"][0]["url"]
        hsp = client.get(_path_and_query(hsp_url))
        assert hsp.status_code == 200
        parsed = inspect(hsp.content)
        assert parsed.unique_id == filename
        _assert_latin1_headers(hsp)
        assert "x-heretune-hsp-filename" not in hsp.headers
        assert "filename*=UTF-8''" in hsp.headers["content-disposition"]
        assert filename.replace(".mp4", ".hsp") not in hsp.headers["content-disposition"]
