from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heretune.analyzer import CorrectionProfile, _offset_free_gamma_midtones
from heretune.config import Config, config_to_toml, library_roots, load_config
from heretune.db import DB
from heretune.hsp_policy import apply_auto_image_policy
from heretune.library import Library
from heretune.server import create_app


def test_multi_root_same_relative_path_gets_distinct_rows(tmp_path: Path):
    a = tmp_path / "pc-a"
    b = tmp_path / "pc-b"
    a.mkdir(); b.mkdir()
    (a / "same.mp4").write_bytes(b"a")
    (b / "same.mp4").write_bytes(b"b")
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(a), str(b)]
    cfg.paths.data_dir = str(tmp_path / "data")
    db = DB(cfg.db_path)

    result = Library(db, cfg).scan()
    rows = db.videos(include_missing=True)
    assert result["roots_configured"] == 2
    assert result["roots_reachable"] == 2
    assert len(rows) == 2
    assert len({row["id"] for row in rows}) == 2
    assert {row["rel_path"] for row in rows} == {"same.mp4"}
    assert {row["library_root"] for row in rows} == {str(a), str(b)}


def test_offline_root_does_not_mark_its_videos_missing(tmp_path: Path):
    a = tmp_path / "online"
    b = tmp_path / "network"
    a.mkdir(); b.mkdir()
    (a / "a.mp4").write_bytes(b"a")
    (b / "b.mp4").write_bytes(b"b")
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(a), str(b)]
    cfg.paths.data_dir = str(tmp_path / "data")
    db = DB(cfg.db_path)
    Library(db, cfg).scan()
    b_row = next(row for row in db.videos(include_missing=True) if row["library_root"] == str(b))
    db.exec("UPDATE videos SET status='ready' WHERE id=?", (b_row["id"],))
    (b / "b.mp4").unlink()
    b.rmdir()

    result = Library(db, cfg).scan()
    assert result["partial"] is True
    assert db.get(b_row["id"])["status"] == "ready"


def test_removed_root_marks_old_rows_missing(tmp_path: Path):
    a = tmp_path / "a"; b = tmp_path / "b"
    a.mkdir(); b.mkdir()
    (a / "a.mp4").write_bytes(b"a")
    (b / "b.mp4").write_bytes(b"b")
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(a), str(b)]
    cfg.paths.data_dir = str(tmp_path / "data")
    db = DB(cfg.db_path)
    Library(db, cfg).scan()
    b_row = next(row for row in db.videos(include_missing=True) if row["library_root"] == str(b))
    cfg.library.roots = [str(a)]
    cfg.library.roots = [str(a)]
    Library(db, cfg).scan()
    assert db.get(b_row["id"])["status"] == "missing"


def test_config_serializes_public_multi_root_shape(tmp_path: Path):
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [r"D:\VR", r"\\MEDIA-PC\VR"]
    cfg.library.roots = [cfg.library.roots[0]]
    text = config_to_toml(cfg)
    assert "roots =" in text
    assert "\nroot =" not in text
    cfg.config_path.write_text(text, encoding="utf-8")
    loaded = load_config(cfg.config_path)
    assert library_roots(loaded) == cfg.library.roots


def test_auto_hsp_policy_global_offset_is_hard_neutral_guard():
    profile = CorrectionProfile(
        brightness=0.021,
        offset_r=-0.002, offset_g=0.001, offset_b=0.003,
        gamma=0.97, midtones=0.94, highlights=1.03, shadows=0.08,
        sharpen=2.3,
        scene_keyframes=[{
            "time_seconds": 0.0,
            "native": {
                "offset": (-0.002, 0.001, 0.003, 0.019),
                "gamma": (0.98, 1.01, 1.02, 0.97),
                "midtones": 0.94,
                "highlights": 1.03,
                "shadows": 0.08,
            },
        }],
    )
    safe = apply_auto_image_policy(profile, Config())
    assert safe.brightness == 0.0
    assert (safe.offset_r, safe.offset_g, safe.offset_b) == (-0.002, 0.001, 0.003)
    assert safe.gamma == 0.97
    assert safe.midtones == 0.94
    assert safe.highlights == 1.03
    assert safe.shadows == 0.0
    assert safe.sharpen == 2.3
    assert safe.scene_keyframes[0]["native"]["offset"] == (-0.002, 0.001, 0.003, 0.0)
    assert safe.scene_keyframes[0]["native"]["gamma"] == (0.98, 1.01, 1.02, 0.97)
    assert safe.scene_keyframes[0]["native"]["shadows"] == 0.0
    assert profile.brightness == 0.021  # guard does not mutate caller


def test_gamma_and_midtones_are_solved_without_global_offset_term():
    corrected_mid, gamma, midtones = _offset_free_gamma_midtones(
        ep50=0.16, contrast=0.97, strength=1.0, tone_confidence=1.0
    )
    expected_mid = 0.18 + 0.97 * (0.16 - 0.18)
    assert corrected_mid == pytest.approx(expected_mid)
    # A hypothetical positive Offset would have changed both downstream inputs;
    # the production solver deliberately has no parameter/path for that value.
    assert corrected_mid != pytest.approx(expected_mid + 0.02)
    assert gamma != 1.0
    assert midtones > 1.0


def test_new_defaults_start_paused_and_keep_hsp_lab_hidden():
    cfg = Config()
    assert cfg.server.start_paused is True
    assert cfg.server.show_hsp_diagnostic_tools is False


def test_ui_is_productionized_and_multi_root_aware(tmp_path: Path):
    root = tmp_path / "movies"; root.mkdir()
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(root)]
    cfg.library.roots = [str(root)]
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    with TestClient(app) as client:
        page = client.get("/").text
        script = client.get("/static/app.js").text
    assert "VR DIAGNOSTIC BUILD" not in page
    assert "settingsLibraryRoots" in page
    assert "Show HSP isolation lab" in page
    assert "Apply calculated Global Offset" not in page
    assert "Exact selected-HSP values · full-strength best-effort monitor approximation · HereSphere remains authoritative." in script
    assert "Test suggested Global Offset" not in script
    assert "Not generated by Auto" in script
