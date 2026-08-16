from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import urlparse, quote

from fastapi.testclient import TestClient

from heretune.analyzer import ANALYSIS_VERSION, CorrectionProfile
from heretune.config import Config
from heretune.db import DB
from heretune.hsp import build, inspect
from heretune.library import now, sid
from heretune.server import create_app



def _hs_path(app, video_id: str) -> str:
    row = app.state.db.get(video_id)
    assert row is not None
    return f"/heresphere/video/{video_id}/{quote(Path(str(row['path'])).name, safe='')}"

def _ready_app(tmp_path: Path):
    library = tmp_path / "movies"
    library.mkdir()
    source = library / "demo_MKX200.mp4"
    source.write_bytes(bytes(range(256)) * 8)

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.hsp.accept_hsp_writebacks = True

    app = create_app(cfg)
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
        "bit_depth": 8,
        "is_hdr": False,
    }
    correction = CorrectionProfile(
        r_gain=1.10,
        g_gain=0.98,
        b_gain=1.02,
        contrast=1.05,
        brightness=0.02,
        gamma=0.99,
        saturation=1.03,
        wb_confidence=0.80,
        sampled_frames=48,
        requested_samples=48,
        analysis_version=ANALYSIS_VERSION,
    )
    db.exec(
        """INSERT INTO videos(
               id,path,rel_path,title,size,mtime_ns,status,metadata_json,
               analysis_json,auto_hsp,auto_hsp_version,date_added,last_seen,last_analyzed
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            video_id,
            str(source),
            source.name,
            "demo",
            stat.st_size,
            stat.st_mtime_ns,
            "ready",
            json.dumps(metadata),
            json.dumps(correction.dict()),
            build(video_id, "demo", 120.0, correction),
            7,
            stamp,
            stamp,
            stamp,
        ),
    )
    return app, cfg, db, video_id


def _path_and_query(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def test_branch_dashboard_is_served_from_static_assets(tmp_path: Path):
    app, _, _, _ = _ready_app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Intelligent correction." in page.text
        assert "Original media." in page.text
        assert "Always Load HSP" in page.text
        assert "/static/styles.css" in page.text
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/styles.css").status_code == 200


def test_auto_is_authoritative_and_hsp_urls_are_revisioned(tmp_path: Path):
    app, cfg, db, video_id = _ready_app(tmp_path)
    assert cfg.hsp.prefer_manual_writeback is False

    manual = build(video_id, "manual", 120.0, CorrectionProfile(r_gain=0.90))
    db.exec(
        "UPDATE videos SET manual_hsp=?, manual_hsp_version=? WHERE id=?",
        (manual, 7, video_id),
    )

    with TestClient(app) as client:
        detail = client.post(
            _hs_path(app, video_id), json={"needsMediaSource": True}
        ).json()
        url = detail["hspArray"][0]["url"]
        assert f"/hsp/{video_id}/auto/7.hsp?rev=" in url

        response = client.get(_path_and_query(url))
        assert response.status_code == 200
        assert "immutable" in response.headers["cache-control"]
        assert response.headers["x-heretune-profile"] == "auto"
        assert response.headers["x-heretune-revision"] in url

        dashboard = client.get(f"/api/videos/{video_id}").json()
        assert dashboard["profile_mode"] == "auto"
        assert dashboard["manual_hsp"] is True
        assert dashboard["selected_hsp_kind"] == "auto"


def test_headset_writeback_is_stored_but_does_not_replace_auto(tmp_path: Path):
    app, _, db, video_id = _ready_app(tmp_path)
    manual = build(video_id, "manual", 120.0, CorrectionProfile(r_gain=0.88))

    with TestClient(app) as client:
        posted = client.post(
            _hs_path(app, video_id),
            json={"needsMediaSource": False, "hsp": base64.b64encode(manual).decode("ascii")},
        )
        assert posted.status_code == 200
        row = db.get(video_id)
        assert row["manual_hsp"] == manual
        assert row["profile_mode"] == "auto"
        acknowledgement = posted.json()
        assert acknowledgement["writeHSP"] is False
        assert "hspArray" not in acknowledgement
        assert "hsp" not in acknowledgement
        assert "media" not in acknowledgement


def test_explicit_profile_switches_use_valid_native_hsp_variants(tmp_path: Path):
    app, _, db, video_id = _ready_app(tmp_path)
    manual = build(video_id, "manual", 120.0, CorrectionProfile(r_gain=0.88))
    db.exec(
        "UPDATE videos SET manual_hsp=?, manual_hsp_version=? WHERE id=?",
        (manual, 7, video_id),
    )

    with TestClient(app) as client:
        def playback_hsp_url() -> str:
            payload = client.post(
                _hs_path(app, video_id), json={"needsMediaSource": True}
            ).json()
            url = payload["hspArray"][0]["url"]
            client.post(f"/events/{video_id}", json={"event": 3, "time": 0})
            return url

        auto_url = playback_hsp_url()

        original = client.post(
            f"/api/videos/{video_id}/profile", json={"mode": "original"}
        )
        assert original.status_code == 200
        original_url = playback_hsp_url()
        original_hsp = inspect(client.get(_path_and_query(original_url)).content)
        assert original_hsp.image_keyframes[0].gain[:3] == (1.0, 1.0, 1.0)

        strong = client.post(
            f"/api/videos/{video_id}/profile", json={"mode": "strong"}
        )
        assert strong.status_code == 200
        strong_url = playback_hsp_url()
        strong_hsp = inspect(client.get(_path_and_query(strong_url)).content)
        assert strong_hsp.image_keyframes[0].gain[0] > 1.10
        assert strong_url != auto_url
        assert strong_url != original_url

        manual_mode = client.post(
            f"/api/videos/{video_id}/profile", json={"mode": "manual"}
        )
        assert manual_mode.status_code == 200
        manual_url = playback_hsp_url()
        assert f"/hsp/{video_id}/manual/7.hsp?rev=" in manual_url
        assert client.get(_path_and_query(manual_url)).content == manual

        auto = client.post(f"/api/videos/{video_id}/profile", json={"mode": "auto"})
        assert auto.status_code == 200
        assert db.get(video_id)["profile_mode"] == "auto"


def test_existing_same_name_hsp_sidecar_is_not_imported(tmp_path: Path):
    from heretune.library import Library

    root = tmp_path / "movies"
    root.mkdir()
    source = root / "existing.mp4"
    source.write_bytes(b"video-placeholder")
    (root / "existing.hsp").write_bytes(b"user-profile-that-must-be-ignored")

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(root)]
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.hsp.accept_hsp_writebacks = True
    db = DB(cfg.db_path)
    result = Library(db, cfg).scan()
    videos = db.videos()

    assert result["seen"] == 1
    assert len(videos) == 1
    assert videos[0]["rel_path"] == "existing.mp4"
    assert videos[0]["manual_hsp"] is None
    assert videos[0]["profile_mode"] == "auto"
