from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import urlsplit, quote

import pytest
from fastapi.testclient import TestClient

from heretune import __version__
from heretune.analyzer import ANALYSIS_VERSION, CorrectionProfile
from heretune.config import Config, config_to_toml, load_config
from heretune.hsp import (
    HSPError,
    MAX_SAFE_IMAGE_KEYFRAMES,
    build,
    compress_proxy,
    decompress_proxy,
    inspect,
    validate_playback_safe,
)
from heretune.hsp_policy import (
    apply_auto_image_policy,
    effective_auto_image_policy,
    global_auto_image_policy,
    normalize_policy_overrides,
    policy_signature,
)
from heretune.library import now, sid
from heretune.server import create_app
from heretune.sidecar import SidecarConflict, inspect_status, remove_managed_sidecar, write_sidecar



def _hs_path(app, video_id: str) -> str:
    row = app.state.db.get(video_id)
    assert row is not None
    return f"/heresphere/video/{video_id}/{quote(Path(str(row['path'])).name, safe='')}"

def _ready_app(tmp_path: Path, *, accept_writebacks: bool = False):
    library = tmp_path / "movies"
    library.mkdir()
    source = library / "demo_MKX200.mp4"
    source.write_bytes(bytes(range(256)) * 8)

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.library.roots = [str(library)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.hsp.accept_hsp_writebacks = accept_writebacks

    app = create_app(cfg)
    db = app.state.db
    stamp = now()
    stat = source.stat()
    video_id = sid(source.name)
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
        r_gain=1.07,
        g_gain=0.98,
        b_gain=1.02,
        contrast=1.04,
        gamma=0.97,
        saturation=1.05,
        shadows=0.08,
        midtones=0.95,
        highlights=0.96,
        sharpen=2.12,
        wb_confidence=0.91,
        sampled_frames=48,
        requested_samples=48,
        analysis_version=ANALYSIS_VERSION,
    )
    auto = build(
        video_id,
        source.stem,
        120.0,
        correction,
        metadata=metadata,
        source_filename=source.name,
    )
    db.exec(
        """INSERT INTO videos(
               id,path,rel_path,library_root,title,size,mtime_ns,status,metadata_json,
               analysis_json,auto_hsp,auto_hsp_version,date_added,last_seen,last_analyzed
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            video_id,
            str(source),
            source.name,
            str(library),
            source.stem,
            stat.st_size,
            stat.st_mtime_ns,
            "ready",
            json.dumps(metadata),
            json.dumps(correction.dict()),
            auto,
            7,
            stamp,
            stamp,
            stamp,
        ),
    )
    return app, cfg, db, video_id, source


def _path_and_query(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def test_version_and_engine_invariants():
    assert __version__ == "0.13.1"
    assert ANALYSIS_VERSION == "offset-free-tone-1"


def test_global_policy_defaults_keep_offset_and_shadows_out_of_auto():
    cfg = Config()
    policy = global_auto_image_policy(cfg)
    assert policy["global_offset"] is False
    assert policy["shadows"] is False
    assert policy["midtones"] is True
    assert policy["highlights"] is True
    assert policy["global_gamma"] is True
    assert policy["global_contrast"] is True
    assert policy["rgb_offset"] is True
    assert normalize_policy_overrides({"global_offset": "on"}) == {"global_offset": "on"}
    assert effective_auto_image_policy(cfg, {"global_offset": "on"})["global_offset"] is True


def test_global_offset_force_on_is_output_only_and_bounded():
    cfg = Config()
    profile = CorrectionProfile(
        brightness=0.0,
        contrast=0.98,
        gamma=0.96,
        midtones=0.95,
        luma_p05=0.02,
        luma_p95=0.70,
        exposure_bias=0.04,
        tone_confidence=0.90,
    )
    safe = apply_auto_image_policy(profile, cfg)
    enabled = apply_auto_image_policy(profile, cfg, {"global_offset": "on"})
    assert safe.brightness == 0.0
    assert 0.0 < enabled.brightness <= 0.027
    assert enabled.gamma == profile.gamma
    assert enabled.midtones == profile.midtones
    assert enabled.contrast == profile.contrast
    assert profile.brightness == 0.0


def test_per_video_policy_can_force_shadows_on_and_gamma_off_without_mutating_evidence():
    cfg = Config()
    profile = CorrectionProfile(
        brightness=0.019,
        shadows=0.08,
        midtones=0.95,
        highlights=0.96,
        gamma=0.97,
        offset_r=-0.001,
        offset_g=0.0,
        offset_b=0.001,
        scene_keyframes=[{
            "time_seconds": 5.0,
            "native": {
                "sharpen": 2.1,
                "exposure_bias": 0.03,
                "white_temp": 6400.0,
                "white_tint": -0.01,
                "saturation": (1.0, 1.01, 1.02, 1.05),
                "contrast": (0.99, 1.0, 1.01, 1.04),
                "gamma": (0.98, 1.01, 1.02, 0.97),
                "gain": (1.06, 0.99, 0.97, 1.0),
                "offset": (-0.001, 0.0, 0.001, 0.019),
                "shadows": 0.08,
                "midtones": 0.95,
                "highlights": 0.96,
            },
        }],
    )
    default_safe = apply_auto_image_policy(profile, cfg)
    assert default_safe.brightness == 0.0
    assert default_safe.shadows == 0.0

    forced = apply_auto_image_policy(
        profile, cfg, {"shadows": "on", "global_gamma": "off"}
    )
    assert forced.brightness == 0.0
    assert forced.shadows == pytest.approx(0.08)
    assert forced.gamma == 1.0
    assert forced.scene_keyframes[0]["native"]["offset"][3] == 0.0
    assert forced.scene_keyframes[0]["native"]["shadows"] == pytest.approx(0.08)
    assert forced.scene_keyframes[0]["native"]["gamma"][:3] == (0.98, 1.01, 1.02)
    assert forced.scene_keyframes[0]["native"]["gamma"][3] == 1.0
    assert profile.shadows == pytest.approx(0.08)
    assert profile.brightness == pytest.approx(0.019)

    effective = effective_auto_image_policy(cfg, {"shadows": "on", "midtones": "off"})
    assert effective["shadows"] is True
    assert effective["midtones"] is False
    assert effective["global_offset"] is False


def test_policy_and_sidecar_settings_round_trip(tmp_path: Path):
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.hsp.auto_shadows = False
    cfg.hsp.auto_midtones = True
    cfg.hsp.write_auto_sidecars = True
    text = config_to_toml(cfg)
    assert "auto_shadows = false" in text
    assert "auto_midtones = true" in text
    assert "write_auto_sidecars = true" in text
    assert "auto_global_offset = false" in text
    assert "accept_hsp_writebacks = false" in text
    cfg.config_path.write_text(text, encoding="utf-8")
    loaded = load_config(cfg.config_path)
    assert loaded.hsp.auto_shadows is False
    assert loaded.hsp.auto_midtones is True
    assert loaded.hsp.write_auto_sidecars is True


def test_generated_scene_hsp_is_canonical_bounded_and_playback_safe():
    native = {
        "sharpen": 2.1,
        "exposure_bias": 0.01,
        "white_temp": 6500.0,
        "white_tint": 0.0,
        "saturation": (1.0, 1.0, 1.0, 1.02),
        "contrast": (1.0, 1.0, 1.0, 1.03),
        "gamma": (1.0, 1.0, 1.0, 0.98),
        "gain": (1.03, 0.99, 0.98, 1.0),
        "offset": (0.0, 0.0, 0.0, 0.0),
        "shadows": 0.0,
        "midtones": 0.98,
        "highlights": 0.97,
    }
    profile = CorrectionProfile(
        scene_keyframes=[
            {"time_seconds": float(index // 2), "native": dict(native)}
            for index in reversed(range(600))
        ]
    )
    blob = build(
        "id",
        "Title",
        400.0,
        profile,
        source_filename="movie.mp4",
    )
    parsed = validate_playback_safe(
        blob, expected_filename="movie.mp4", duration_seconds=400.0
    )
    assert 1 <= len(parsed.image_keyframes) <= MAX_SAFE_IMAGE_KEYFRAMES
    assert MAX_SAFE_IMAGE_KEYFRAMES == 64
    times = [item.time_ticks for item in parsed.image_keyframes]
    assert times[0] == 0
    assert times == sorted(set(times))
    assert times[-1] < 400 * 10_000_000


def test_unwrapped_hsp_is_rejected_before_player_or_sidecar_use():
    blob = build("id", "Title", 60.0, CorrectionProfile(), source_filename="movie.mp4")
    raw = decompress_proxy(blob)
    unsafe = compress_proxy(raw[4:])
    with pytest.raises(HSPError, match="wrapper"):
        validate_playback_safe(unsafe, duration_seconds=60.0)


def test_managed_sidecar_is_atomic_conflict_safe_and_backup_replace(tmp_path: Path):
    source = tmp_path / "movie_MKX200.mp4"
    source.write_bytes(b"video")
    blob = build(
        "id", "Title", 60.0, CorrectionProfile(), source_filename=source.name
    )
    video = {
        "id": "id",
        "path": str(source),
        "metadata": {"duration": 60.0},
        "sidecar_hsp_path": None,
        "sidecar_hsp_revision": None,
        "sidecar_hsp_managed": False,
    }
    first = write_sidecar(video, blob)
    assert first.path == source.with_suffix(".hsp")
    assert first.path.read_bytes() == blob

    managed = dict(video)
    managed.update({
        "sidecar_hsp_path": str(first.path),
        "sidecar_hsp_revision": first.revision,
        "sidecar_hsp_managed": True,
    })
    assert inspect_status(managed, blob)["current"] is True

    first.path.write_bytes(b"external-change")
    with pytest.raises(SidecarConflict):
        write_sidecar(managed, blob)
    replaced = write_sidecar(managed, blob, replace=True)
    assert replaced.backup is not None and replaced.backup.read_bytes() == b"external-change"
    assert replaced.path.read_bytes() == blob

    managed.update({
        "sidecar_hsp_path": str(replaced.path),
        "sidecar_hsp_revision": replaced.revision,
        "sidecar_hsp_managed": True,
    })
    remove_managed_sidecar(managed)
    assert not replaced.path.exists()


def test_hsp_writeback_is_ignored_by_default_without_live_reload(tmp_path: Path):
    app, _, db, video_id, _ = _ready_app(tmp_path)
    manual = build("manual", "Manual", 120.0, CorrectionProfile(r_gain=0.91))
    with TestClient(app) as client:
        advertised = client.get(_hs_path(app, video_id)).json()
        assert advertised["writeHSP"] is False
        posted = client.post(
            _hs_path(app, video_id),
            json={"needsMediaSource": True, "hsp": base64.b64encode(manual).decode("ascii")},
        )
        assert posted.status_code == 200
        payload = posted.json()
        assert payload["writeHSP"] is False
        assert "media" not in payload and "hspArray" not in payload and "hsp" not in payload
        assert db.get(video_id)["manual_hsp"] is None
        followup = client.get(_hs_path(app, video_id)).json()
        assert "hspArray" not in followup


def test_hsp_writeback_ack_is_minimal_and_defers_reload_until_close(tmp_path: Path):
    app, _, db, video_id, _ = _ready_app(tmp_path, accept_writebacks=True)
    manual = build("manual-user-id", "My Imported Look", 120.0, CorrectionProfile(r_gain=0.91))

    with TestClient(app) as client:
        client.post(f"/events/{video_id}", json={"event": 0, "time": 0})
        posted = client.post(
            _hs_path(app, video_id),
            json={
                "needsMediaSource": True,
                "hsp": base64.b64encode(manual).decode("ascii"),
            },
        )
        assert posted.status_code == 200
        ack = posted.json()
        assert ack["writeHSP"] is False
        assert "media" not in ack
        assert "hsp" not in ack
        assert "hspArray" not in ack
        assert db.get(video_id)["manual_hsp"] == manual
        assert db.get(video_id)["profile_mode"] == "auto"

        # Even if HereSphere immediately asks again during the same live import,
        # HereTune does not advertise a second HSP replacement.
        during = client.get(_hs_path(app, video_id)).json()
        assert "hspArray" not in during

        client.post(f"/events/{video_id}", json={"event": 3, "time": 0})
        after = client.post(
            _hs_path(app, video_id), json={"needsMediaSource": True}
        ).json()
        assert "/auto/7.hsp?rev=" in after["hspArray"][0]["url"]
        client.post(f"/events/{video_id}", json={"event": 3, "time": 0})

        # The imported Manual profile is structurally safe even though its UID is
        # intentionally not the media filename.
        switched = client.post(f"/api/videos/{video_id}/profile", json={"mode": "manual"})
        assert switched.status_code == 200
        manual_url = client.post(
            _hs_path(app, video_id), json={"needsMediaSource": True}
        ).json()["hspArray"][0]["url"]
        manual_response = client.get(_path_and_query(manual_url))
        assert manual_response.status_code == 200
        assert manual_response.content == manual


def test_disabled_writeback_is_acknowledged_without_parsing_or_live_reload(tmp_path: Path):
    app, _, db, video_id, _ = _ready_app(tmp_path, accept_writebacks=False)
    valid = build("manual", "Manual", 120.0, CorrectionProfile())
    unsafe = compress_proxy(decompress_proxy(valid)[4:])
    with TestClient(app) as client:
        response = client.post(
            _hs_path(app, video_id),
            json={"hsp": base64.b64encode(unsafe).decode("ascii")},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["writeHSP"] is False
        assert "media" not in payload and "hspArray" not in payload
        assert db.get(video_id)["manual_hsp"] is None


def test_active_playback_advertises_hsp_only_once_until_close(tmp_path: Path):
    app, _, _, video_id, _ = _ready_app(tmp_path)
    with TestClient(app) as client:
        first = client.post(
            _hs_path(app, video_id),
            json={"needsMediaSource": True},
        ).json()
        assert "hspArray" in first
        assert "media" in first

        # Repeated metadata/playback requests during the same open video do not
        # provoke a second native HSP replacement.
        repeated_get = client.get(_hs_path(app, video_id)).json()
        repeated_play = client.post(
            _hs_path(app, video_id),
            json={"needsMediaSource": True},
        ).json()
        assert "hspArray" not in repeated_get
        assert "hspArray" not in repeated_play

        client.post(f"/events/{video_id}", json={"event": 3, "time": 0})
        reopened = client.post(
            _hs_path(app, video_id),
            json={"needsMediaSource": True},
        ).json()
        assert "hspArray" in reopened


def test_per_video_policy_api_rebuilds_from_evidence_without_reanalysis(tmp_path: Path):
    app, _, db, video_id, _ = _ready_app(tmp_path)
    before_last_analyzed = db.get(video_id)["last_analyzed"]
    with TestClient(app) as client:
        response = client.patch(
            f"/api/videos/{video_id}/hsp-policy",
            json={"overrides": {"shadows": "on", "global_gamma": "off"}},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["policy_update"]["rebuilt"] is True
        assert payload["policy_update"]["deferred_until_close"] is False
        row = db.get(video_id)
        parsed = inspect(row["auto_hsp"])
        image = parsed.image_keyframes[0]
        assert image.shadows == pytest.approx(0.08)
        assert image.gamma[3] == pytest.approx(1.0)
        assert image.offset[3] == pytest.approx(0.0)
        assert row["last_analyzed"] == before_last_analyzed

        reset = client.patch(f"/api/videos/{video_id}/hsp-policy", json={"reset": True})
        assert reset.status_code == 200
        reset_image = inspect(db.get(video_id)["auto_hsp"]).image_keyframes[0]
        assert reset_image.shadows == pytest.approx(0.0)
        assert reset_image.gamma[3] == pytest.approx(0.97)


def test_per_video_native_policy_api_rebuilds_geometry_and_preserves_image_overrides(tmp_path: Path):
    app, _, db, video_id, _ = _ready_app(tmp_path)
    row = db.get(video_id)
    metadata = dict(row["metadata"] or {})
    metadata.update({
        "projection": "perspective",
        "stereo": "sbs",
        "stereo_alignment": {
            "frames": 4,
            "confidence": 0.94,
            "auto_applicable": True,
            "suggested_rotation": (0.40, 0.0, 0.0),
        },
        "active_image_bounds": {
            "confidence": 0.95,
            "auto_applicable": True,
            "suggested_zoom": (1.10, 1.08),
            "suggested_pan": (-0.02, 0.01),
        },
        "horizon_verification": {
            "confidence": 0.92,
            "auto_applicable": True,
            "suggested_orientation_rotation": (0.0, 0.0, 1.25),
        },
    })
    db.exec("UPDATE videos SET metadata_json=? WHERE id=?", (json.dumps(metadata), video_id))

    with TestClient(app) as client:
        response = client.patch(
            f"/api/videos/{video_id}/native-hsp-policy",
            json={"overrides": {"stereo_disparity": "off", "crop": "off", "horizon": "on"}},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["policy_update"]["rebuilt"] is True
        controls = {item["name"]: item for item in payload["native_calibration"]["output_policy"]["controls"]}
        assert controls["stereo_disparity"]["override"] == "off"
        assert controls["crop"]["override"] == "off"
        assert controls["horizon"]["override"] == "on"
        parsed = inspect(db.get(video_id)["auto_hsp"])
        assert parsed.alignment_keyframes[0].rotation == pytest.approx((0.0, 0.0, 0.0), abs=1e-5)
        assert parsed.format_keyframes[0].zoom == pytest.approx((1.0, 1.0), abs=1e-5)
        assert parsed.orientation_keyframes[0].orientation == pytest.approx((0.0, 0.0, 1.25), abs=1e-5)

        # Image-policy edits must not wipe the native choices stored in the same JSON column.
        image = client.patch(
            f"/api/videos/{video_id}/hsp-policy",
            json={"overrides": {"shadows": "on"}},
        )
        assert image.status_code == 200
        stored = db.get(video_id)["hsp_policy_override"]
        assert stored["shadows"] == "on"
        assert stored["stereo_disparity"] == "off"
        assert stored["crop"] == "off"
        assert stored["horizon"] == "on"

        reset = client.patch(f"/api/videos/{video_id}/native-hsp-policy", json={"reset": True})
        assert reset.status_code == 200
        stored = db.get(video_id)["hsp_policy_override"]
        assert stored == {"shadows": "on"}
        controls = {item["name"]: item for item in reset.json()["native_calibration"]["output_policy"]["controls"]}
        assert controls["stereo_disparity"]["override"] == "inherit"
        assert controls["stereo_disparity"]["effective"] is True
        assert controls["crop"]["effective"] is True
        assert controls["horizon"]["effective"] is False


def test_isolation_lab_can_neutralize_explicit_global_offset(tmp_path: Path):
    app, _, db, video_id, _ = _ready_app(tmp_path)
    row = db.get(video_id)
    analysis = dict(row["analysis"])
    analysis.update({
        "luma_p05": 0.02,
        "luma_p95": 0.70,
        "tone_confidence": 0.90,
        "exposure_bias": 0.04,
    })
    db.exec("UPDATE videos SET analysis_json=? WHERE id=?", (json.dumps(analysis), video_id))
    with TestClient(app) as client:
        rebuilt = client.patch(
            f"/api/videos/{video_id}/hsp-policy",
            json={"overrides": {"global_offset": "on"}},
        )
        assert rebuilt.status_code == 200
        baseline = inspect(db.get(video_id)["auto_hsp"]).image_keyframes[0]
        assert baseline.offset[3] > 0.0
        isolated = client.patch(
            f"/api/videos/{video_id}/hsp-diagnostic",
            json={"disabled": ["offset_global"]},
        )
        assert isolated.status_code == 200
        hsp_url = client.post(
            _hs_path(app, video_id), json={"needsMediaSource": True}
        ).json()["hspArray"][0]["url"]
        response = client.get(_path_and_query(hsp_url))
        assert response.status_code == 200
        assert inspect(response.content).image_keyframes[0].offset[3] == pytest.approx(0.0)


def test_sidecar_api_exports_per_video_and_blocks_live_mutation(tmp_path: Path):
    app, _, db, video_id, source = _ready_app(tmp_path)
    with TestClient(app) as client:
        exported = client.post(f"/api/videos/{video_id}/hsp-sidecar", json={})
        assert exported.status_code == 200
        target = source.with_suffix(".hsp")
        assert target.is_file()
        assert inspect_status(db.get(video_id), db.get(video_id)["auto_hsp"])["current"] is True

        client.post(f"/events/{video_id}", json={"event": 0, "time": 0})
        blocked_write = client.post(f"/api/videos/{video_id}/hsp-sidecar", json={})
        blocked_delete = client.delete(f"/api/videos/{video_id}/hsp-sidecar")
        assert blocked_write.status_code == 409
        assert blocked_delete.status_code == 409
        client.post(f"/events/{video_id}", json={"event": 3, "time": 0})
        removed = client.delete(f"/api/videos/{video_id}/hsp-sidecar")
        assert removed.status_code == 200
        assert not target.exists()



def test_global_policy_signature_stays_dirty_until_live_deferred_rebuild_closes(tmp_path: Path):
    app, cfg, db, video_id, _ = _ready_app(tmp_path)
    # Seed a real config file so the normal Settings endpoint can edit it.
    cfg.config_path.write_text(config_to_toml(cfg), encoding="utf-8")
    with TestClient(app) as client:
        # Normalize the existing helper HSP through the shipped default policy.
        reset = client.patch(f"/api/videos/{video_id}/hsp-policy", json={"reset": True})
        assert reset.status_code == 200
        assert inspect(db.get(video_id)["auto_hsp"]).image_keyframes[0].shadows == pytest.approx(0.0)

        client.post(f"/events/{video_id}", json={"event": 0, "time": 0})
        response = client.put(
            "/api/config",
            json={"hsp": {"auto_shadows": True}},
        )
        assert response.status_code == 200
        assert response.json()["hsp_policy_result"]["skipped"] == [video_id]
        assert db.get_setting("auto_hsp_policy_signature") == ""
        # The live profile remains unchanged until the player closes.
        assert inspect(db.get(video_id)["auto_hsp"]).image_keyframes[0].shadows == pytest.approx(0.0)

        client.post(f"/events/{video_id}", json={"event": 3, "time": 0})
        assert db.get_setting("auto_hsp_policy_signature") == policy_signature(cfg)
        assert not app.state.pending_hsp_rebuild
        assert inspect(db.get(video_id)["auto_hsp"]).image_keyframes[0].shadows == pytest.approx(0.08)

def test_ui_and_worker_safety_surfaces_are_packaged():
    root = Path(__file__).resolve().parents[1]
    html = (root / "heretune/static/index.html").read_text(encoding="utf-8")
    js = (root / "heretune/static/app.js").read_text(encoding="utf-8")
    css = (root / "heretune/static/styles.css").read_text(encoding="utf-8")
    worker = (root / "heretune/worker.py").read_text(encoding="utf-8")
    example = (root / "config.example.toml").read_text(encoding="utf-8")

    assert "Auto HSP output controls" in html
    assert 'name="hsp.auto_shadows"' in html
    assert 'name="hsp.auto_midtones"' in html
    assert 'name="hsp.auto_global_gamma"' in html
    assert 'name="hsp.write_auto_sidecars"' in html
    assert 'name="hsp.auto_global_offset"' in html
    assert "Global offset" in html and "OFF by default" in html
    assert 'data-image-policy-control=' in js
    assert 'data-image-policy-value="inherit"' in js
    assert 'data-image-policy-value="on"' in js
    assert 'data-image-policy-value="off"' in js
    assert "Use all global defaults" in js
    assert "Save Auto HSP beside source file" in js
    assert "No Global Offset" in js
    assert '"offset_global"' in js
    assert "setVideoImageHspPolicy" in js
    assert "writeSidecarHsp" in js
    assert ".hsp-policy-grid" in css
    assert ".sidecar-hsp-card" in css
    assert "write_sidecar(" not in worker
    assert "auto_shadows = false" in example
    assert "write_auto_sidecars = false" in example
    assert "auto_global_offset = false" in example
    assert "accept_hsp_writebacks = false" in example
