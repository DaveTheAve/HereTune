from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, quote

from fastapi.testclient import TestClient

from heretune.analyzer import CorrectionProfile
from heretune.config import Config
from heretune.db import DB
from heretune.hsp import build, inspect
from heretune.library import Library, now, sid
from heretune.server import create_app



def _hs_path(app, video_id: str) -> str:
    row = app.state.db.get(video_id)
    assert row is not None
    return f"/heresphere/video/{video_id}/{quote(Path(str(row['path'])).name, safe='')}"

def _insert_ready(app, source: Path, correction: CorrectionProfile, *, manual: bytes | None = None) -> str:
    db = app.state.db
    stat = source.stat()
    video_id = sid(source.name)
    stamp = now()
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
    auto = build(video_id, source.stem, 120.0, correction)
    db.exec(
        """INSERT INTO videos(
               id,path,rel_path,title,size,mtime_ns,status,metadata_json,
               analysis_json,auto_hsp,auto_hsp_version,manual_hsp,
               manual_hsp_version,date_added,last_seen,last_analyzed
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            video_id,
            str(source),
            source.name,
            source.stem,
            stat.st_size,
            stat.st_mtime_ns,
            "ready",
            json.dumps(metadata),
            json.dumps(correction.dict()),
            auto,
            7,
            manual,
            7 if manual else None,
            stamp,
            stamp,
            stamp,
        ),
    )
    return video_id


def test_auto_first_and_revisioned_urls_are_defaults():
    cfg = Config()
    assert cfg.hsp.prefer_manual_writeback is False
    assert cfg.hsp.revisioned_urls is True


def test_old_sidecar_hsp_is_ignored_by_library_scan(tmp_path: Path):
    library = tmp_path / "movies"
    library.mkdir()
    (library / "demo.mp4").write_bytes(b"video")
    (library / "demo.hsp").write_bytes(b"old-settings")
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.hsp.accept_hsp_writebacks = True
    db = DB(cfg.db_path)
    Library(db, cfg).scan()
    rows = db.videos()
    assert [row["rel_path"] for row in rows] == ["demo.mp4"]
    assert rows[0]["manual_hsp"] is None
    assert rows[0]["profile_mode"] == "auto"


def test_dashboard_profile_modes_and_auto_first_writeback(tmp_path: Path):
    library = tmp_path / "movies"
    library.mkdir()
    source = library / "demo_MKX200.mp4"
    source.write_bytes(bytes(range(256)) * 8)

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.hsp.accept_hsp_writebacks = True
    cfg.hsp.prefer_manual_writeback = False
    cfg.hsp.revisioned_urls = True

    app = create_app(cfg)
    auto_correction = CorrectionProfile(
        r_gain=1.10,
        g_gain=0.96,
        b_gain=1.04,
        contrast=1.08,
        brightness=0.02,
        gamma=0.97,
        saturation=1.05,
        wb_confidence=0.9,
        sampled_frames=48,
        requested_samples=48,
    )
    manual_blob = build(
        "manual", "Manual", 120.0,
        CorrectionProfile(r_gain=0.90, g_gain=1.0, b_gain=1.10, brightness=-0.02),
    )
    video_id = _insert_ready(app, source, auto_correction, manual=manual_blob)

    with TestClient(app) as client:
        def playback_hsp_url() -> str:
            payload = client.post(
                _hs_path(app, video_id), json={"needsMediaSource": True}
            ).json()
            url = payload["hspArray"][0]["url"]
            client.post(f"/events/{video_id}", json={"event": 3, "time": 0})
            return url

        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "Intelligent correction." in dashboard.text
        assert "Original media." in dashboard.text
        assert "Always Load HSP" in dashboard.text
        assert client.get("/static/styles.css").status_code == 200
        assert client.get("/static/app.js").status_code == 200

        auto_url = playback_hsp_url()
        assert f"/hsp/{video_id}/auto/7.hsp?rev=" in auto_url
        auto_path = urlsplit(auto_url).path + "?" + urlsplit(auto_url).query
        auto_hsp = client.get(auto_path)
        assert auto_hsp.status_code == 200
        assert "immutable" in auto_hsp.headers["cache-control"]
        assert auto_hsp.headers["x-heretune-profile"] == "auto"
        assert client.get(f"/hsp/{video_id}/auto/7.hsp?rev=stale").status_code == 404

        # A headset write-back is stored but does not steal authority from Auto.
        response = client.post(
            _hs_path(app, video_id),
            json={"needsMediaSource": False, "hsp": base64.b64encode(manual_blob).decode("ascii")},
        )
        assert response.status_code == 200
        assert response.json()["writeHSP"] is False
        assert "hspArray" not in response.json()
        assert "media" not in response.json()
        assert app.state.db.get(video_id)["profile_mode"] == "auto"

        mild = client.post(f"/api/videos/{video_id}/profile", json={"mode": "mild"})
        assert mild.status_code == 200
        mild_url = playback_hsp_url()
        assert f"/hsp/{video_id}/mild/7.hsp?rev=" in mild_url
        mild_path = urlsplit(mild_url).path + "?" + urlsplit(mild_url).query
        mild_profile = inspect(client.get(mild_path).content)
        mild_gain = mild_profile.image_keyframes[0].gain[0]
        assert 1.0 < mild_gain < auto_correction.r_gain

        original = client.post(f"/api/videos/{video_id}/profile", json={"mode": "original"})
        assert original.status_code == 200
        original_url = playback_hsp_url()
        original_path = urlsplit(original_url).path + "?" + urlsplit(original_url).query
        original_profile = inspect(client.get(original_path).content)
        image = original_profile.image_keyframes[0]
        assert image.gain == (1.0, 1.0, 1.0, 1.0)
        assert image.offset == (0.0, 0.0, 0.0, 0.0)

        manual = client.post(f"/api/videos/{video_id}/profile", json={"mode": "manual"})
        assert manual.status_code == 200
        manual_url = playback_hsp_url()
        assert f"/hsp/{video_id}/manual/7.hsp?rev=" in manual_url

        cleared = client.delete(f"/api/videos/{video_id}/manual-hsp")
        assert cleared.status_code == 200
        assert app.state.db.get(video_id)["profile_mode"] == "auto"
