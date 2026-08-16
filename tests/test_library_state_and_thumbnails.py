from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from heretune.analyzer import CorrectionProfile
from heretune.config import Config
from heretune.db import DB
from heretune.hsp import build
from heretune.library import now, sid
from heretune.processes import run_capture
from heretune.server import create_app
from heretune.vrformat import detect
from heretune.worker import THUMBNAIL_LAYOUT_VERSION, thumbnail_destination



def _hs_path(app, video_id: str) -> str:
    row = app.state.db.get(video_id)
    assert row is not None
    return f"/heresphere/video/{video_id}/{quote(Path(str(row['path'])).name, safe='')}"

def _ready_video(app, source: Path) -> str:
    db = app.state.db
    stat = source.stat()
    video_id = sid(source.name)
    timestamp = now()
    metadata = {
        "duration": 120.0,
        "width": 5760,
        "height": 2880,
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
            build(video_id, "demo", 120.0, correction), 7,
            timestamp, timestamp, timestamp,
        ),
    )
    return video_id


def test_heresphere_favorite_and_rating_round_trip(tmp_path: Path):
    library = tmp_path / "movies"
    library.mkdir()
    source = library / "demo_MKX200_SBS.mp4"
    source.write_bytes(b"not-a-real-video")
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    video_id = _ready_video(app, source)

    with TestClient(app) as client:
        initial = client.get(_hs_path(app, video_id)).json()
        assert initial["isFavorite"] is False
        assert initial["writeFavorite"] is True
        assert initial["writeRating"] is True
        assert "rating" not in initial

        changed = client.post(
            _hs_path(app, video_id),
            json={"needsMediaSource": False, "isFavorite": True, "rating": 4.5},
        )
        assert changed.status_code == 200
        payload = changed.json()
        assert payload["isFavorite"] is True
        assert payload["rating"] == 4.5

        scan = client.get("/heresphere/scan").json()["scanData"][0]
        assert scan["isFavorite"] is True
        assert scan["rating"] == 4.5

        api = client.get(f"/api/videos/{video_id}").json()
        assert api["is_favorite"] is True
        assert api["rating"] == 4.5


def test_dashboard_can_edit_and_filter_library_state(tmp_path: Path):
    library = tmp_path / "movies"
    library.mkdir()
    source = library / "demo_MKX200_SBS.mp4"
    source.write_bytes(b"video")
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    video_id = _ready_video(app, source)

    with TestClient(app) as client:
        updated = client.patch(
            f"/api/videos/{video_id}/library-state",
            json={"is_favorite": True, "rating": 5},
        )
        assert updated.status_code == 200
        assert updated.json()["is_favorite"] is True
        assert updated.json()["rating"] == 5.0
        assert client.get("/api/videos?favorite=favorite").json()["total"] == 1
        assert client.get("/api/videos?favorite=not_favorite").json()["total"] == 0
        assert client.get("/api/videos?rating=5").json()["total"] == 1
        assert client.get("/api/videos?rating=unrated").json()["total"] == 0
        page = client.get("/").text
        assert "favoriteFilter" in page
        assert "ratingFilter" in page
        assert "accelerationText" in page


def test_stereo_does_not_automatically_mean_equirectangular():
    metadata = detect("plain_SBS.mp4", 5760, 2880)
    assert metadata["stereo"] == "sbs"
    assert metadata["projection"] == "perspective"
    fisheye = detect("scene_RF52_SBS.mp4", 5760, 2880)
    assert fisheye["projection"] == "fisheye"
    assert fisheye["fov"] == 190.0


def test_thumbnail_layout_is_bumped_to_force_regeneration(tmp_path: Path):
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.paths.data_dir = str(tmp_path / "data")
    assert THUMBNAIL_LAYOUT_VERSION >= 3
    assert f"preview2d-v{THUMBNAIL_LAYOUT_VERSION}" in thumbnail_destination(cfg, "abc").name


def test_raw_process_capture_handles_byte_undefined_in_cp1252():
    result = run_capture([
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(b'before\\x9dafter')",
    ], timeout=5)
    assert result.returncode == 0
    assert result.stdout == b"before\x9dafter"


def test_interrupted_analyzing_rows_requeue_on_db_open(tmp_path: Path):
    path = tmp_path / "db.sqlite3"
    db = DB(path)
    db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,date_added,last_seen)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        ("v", "x", "x", "x", 1, 1, "analyzing", now(), now()),
    )
    reopened = DB(path)
    assert reopened.get("v")["status"] == "queued"


def test_http_thumbnail_route_generates_only_one_sbs_eye(tmp_path: Path):
    import shutil
    import subprocess
    import cv2

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return
    library = tmp_path / "movies"
    library.mkdir()
    source = library / "synthetic_SBS.mp4"
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=red:s=320x320:d=1",
        "-f", "lavfi", "-i", "color=c=blue:s=320x320:d=1",
        "-filter_complex", "hstack=inputs=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "1", "-y", str(source),
    ], check=True)

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    video_id = _ready_video(app, source)
    # Replace the helper's MKX metadata with ordinary SBS/perspective metadata.
    app.state.db.exec(
        "UPDATE videos SET metadata_json=?, thumb_path=NULL WHERE id=?",
        (json.dumps({
            "duration": 1.0, "width": 640, "height": 320, "video_codec": "h264",
            "projection": "perspective", "stereo": "sbs", "fov": 0.0,
            "lens": "Linear", "isEyeSwapped": False,
        }), video_id),
    )
    with TestClient(app) as client:
        response = client.get(f"/thumb/{video_id}.jpg")
        assert response.status_code == 200
        jpg = tmp_path / "thumb.jpg"
        jpg.write_bytes(response.content)
        image = cv2.imread(str(jpg), cv2.IMREAD_COLOR)
        assert image is not None
        # OpenCV is BGR. A single left/red eye should have far more red than blue.
        blue, _green, red = image.mean(axis=(0, 1))
        assert red > blue * 3


def test_terminate_all_processes_interrupts_owned_child():
    import threading
    import time
    from heretune.processes import active_process_count, terminate_all_processes

    result = {}

    def run_long_child():
        result["completed"] = run_capture([
            sys.executable, "-c", "import time; time.sleep(30)"
        ]).returncode

    thread = threading.Thread(target=run_long_child)
    thread.start()
    deadline = time.time() + 3
    while active_process_count() == 0 and time.time() < deadline:
        time.sleep(0.02)
    assert active_process_count() >= 1
    assert terminate_all_processes() >= 1
    thread.join(timeout=3)
    assert not thread.is_alive()
