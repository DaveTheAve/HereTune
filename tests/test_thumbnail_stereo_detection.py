from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import cv2
import pytest
from fastapi.testclient import TestClient

from heretune.config import Config
from heretune.analyzer import CorrectionProfile
from heretune.server import create_app
from heretune.worker import THUMBNAIL_LAYOUT_VERSION, thumbnail_browse_metadata
from heretune.library import now


def test_thumbnail_uses_vr_default_sbs_for_untagged_files():
    assert THUMBNAIL_LAYOUT_VERSION >= 5
    row = {"path": r"X:\\VR\\ordinary_name_without_layout_tag.mp4"}
    metadata = {"width": 3840, "height": 1920, "projection": "perspective", "stereo": "mono"}
    browse = thumbnail_browse_metadata(row, metadata)
    assert browse["stereo"] == "sbs"
    assert browse["thumbnail_stereo_reason"] == "untagged-vr-default-sbs"


def test_thumbnail_respects_explicit_2d_or_mono_filename():
    metadata = {"width": 1920, "height": 1080, "projection": "perspective", "stereo": "mono"}
    for filename in ("movie_2d.mp4", "movie_mono.mp4", "movie_flat.mp4", "movie_perspective.mp4"):
        browse = thumbnail_browse_metadata({"path": filename}, metadata)
        assert browse["stereo"] == "mono"
        assert "thumbnail_stereo_inferred" not in browse


def test_http_thumbnail_crops_untagged_double_eye_video(tmp_path: Path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")

    library = tmp_path / "movies"
    library.mkdir()
    # Deliberately no SBS token: this exercises the VR-first fallback heuristic.
    source = library / "untagged_vr_movie.mp4"
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

    stat = source.stat()
    video_id = "untagged"
    metadata = {
        "duration": 1.0, "width": 640, "height": 320, "video_codec": "h264",
        "projection": "perspective", "stereo": "mono", "fov": 0.0,
        "lens": "Linear", "isEyeSwapped": False,
    }
    analysis = CorrectionProfile(sampled_frames=1, target_samples=1).dict()
    app.state.db.exec(
        """INSERT INTO videos(id,path,rel_path,title,size,mtime_ns,status,metadata_json,analysis_json,date_added,last_seen)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (video_id, str(source), source.name, source.stem, stat.st_size, stat.st_mtime_ns,
         "ready", json.dumps(metadata), json.dumps(analysis), now(), now()),
    )

    with TestClient(app) as client:
        response = client.get(f"/thumb/{video_id}.jpg?rev=5")
        assert response.status_code == 200
        out = tmp_path / "thumb.jpg"
        out.write_bytes(response.content)
        image = cv2.imread(str(out), cv2.IMREAD_COLOR)
        assert image is not None
        blue, _green, red = image.mean(axis=(0, 1))
        # If both eyes were present, red and blue would be similar. Legacy behavior
        # must choose the left/red eye before making the 16:9 card.
        assert red > blue * 3
        row = app.state.db.get(video_id)
        assert f"preview2d-v{THUMBNAIL_LAYOUT_VERSION}" in Path(row["thumb_path"]).name
