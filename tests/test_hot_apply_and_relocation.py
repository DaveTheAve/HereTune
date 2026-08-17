from __future__ import annotations

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from heretune import __version__
from heretune.analyzer import ANALYSIS_VERSION
from heretune.config import Config, config_to_toml
from heretune.db import DB
from heretune.library import Library
from heretune.server import create_app


def configured_app(tmp_path: Path) -> tuple[Config, object]:
    media = tmp_path / "media"
    media.mkdir()
    config_path = tmp_path / "config.toml"
    cfg = Config(config_path=config_path)
    cfg.library.roots = [str(media)]
    cfg.library.roots = [str(media)]
    cfg.paths.data_dir = str(tmp_path / "data")
    config_path.write_text(config_to_toml(cfg), encoding="utf-8")
    return cfg, create_app(cfg)


def test_version():
    assert __version__ == "0.13.1"


def test_settings_hot_apply_after_save_but_restart_only_fields_stay_runtime_bound(tmp_path: Path):
    cfg, app = configured_app(tmp_path)
    original_port = cfg.server.port
    assert cfg.server.show_hsp_diagnostic_tools is False
    assert cfg.server.start_paused is True

    with TestClient(app) as client:
        result = client.put(
            "/api/config",
            json={
                "server": {"show_hsp_diagnostic_tools": True, "start_paused": False},
                "analysis": {"strength": 1.25},
            },
        ).json()
        assert result["restart_required"] is False
        assert result["restart_required_for"] == []
        assert result["analysis_changes_apply_next_job"] is True
        assert result["startup_preference_applies_next_launch"] is True
        assert cfg.server.show_hsp_diagnostic_tools is True
        assert cfg.server.start_paused is False
        assert cfg.analysis.strength == 1.25
        # start_paused is a next-launch preference; Save must not unexpectedly
        # flip the current session's Pause/Resume state.
        assert app.state.work_paused is True

        port_result = client.put("/api/config", json={"server": {"port": original_port + 1}}).json()
        assert port_result["restart_required"] is True
        assert port_result["restart_required_for"] == ["server.port"]
        assert port_result["config"]["server"]["port"] == original_port + 1
        assert cfg.server.port == original_port


def test_library_root_save_hot_applies_and_requests_scan(tmp_path: Path):
    cfg, app = configured_app(tmp_path)
    second = tmp_path / "second"
    second.mkdir()
    with TestClient(app) as client:
        result = client.put("/api/config", json={"library": {"roots": [cfg.library.roots[0], str(second)]}}).json()
        assert result["restart_required"] is False
        assert cfg.library.roots == [str(tmp_path / "media"), str(second)]
        # Work starts paused by default, so the wake request remains pending until
        # Resume work; once resumed, the scanner consumes the new roots immediately.
        assert app.state.scan_wakeup.is_set()


def test_cross_volume_copy_delete_race_reconciles_to_historical_identity(tmp_path: Path):
    root = tmp_path / "library"
    old_dir = root / "old"
    new_dir = root / "new"
    old_dir.mkdir(parents=True)
    new_dir.mkdir()
    source = old_dir / "movie.mp4"
    source.write_bytes((b"HereTune relocation evidence" * 30000) + b"tail")

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(root)]
    cfg.library.roots = [str(root)]
    cfg.paths.data_dir = str(tmp_path / "data")
    db = DB(cfg.db_path)
    library = Library(db, cfg)

    library.scan()
    historical = db.videos(include_missing=True)[0]
    historical_id = historical["id"]
    db.exec(
        "UPDATE videos SET status='ready',analysis_json=?,is_favorite=1,rating=4.5 WHERE id=?",
        (f'{{"analysis_version":"{ANALYSIS_VERSION}"}}', historical_id),
    )

    destination = new_dir / source.name
    shutil.copy2(source, destination)
    second = library.scan()
    assert second["duplicate_groups"] == 1
    rows = db.videos(include_missing=True)
    assert len(rows) == 2
    transient = next(row for row in rows if row["id"] != historical_id)
    transient_id = transient["id"]
    db.exec(
        "INSERT INTO events(video_id,event,time_ms,received_at) VALUES(?,?,?,?)",
        (transient_id, 1, 1234.0, "2026-08-15T12:00:00+00:00"),
    )
    db.exec(
        "INSERT INTO analysis_runs(video_id,started_at,status) VALUES(?,?,?)",
        (transient_id, "2026-08-15T12:00:00+00:00", "ready"),
    )

    source.unlink()
    third = library.scan()
    assert third["relocation_reconciled"] == 1
    assert third["duplicate_groups"] == 0
    rows = db.videos(include_missing=True)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == historical_id
    assert Path(row["path"]) == destination
    assert row["status"] == "ready"
    assert bool(row["is_favorite"]) is True
    assert row["rating"] == 4.5
    assert db.one("SELECT video_id FROM events LIMIT 1")["video_id"] == historical_id
    assert db.one("SELECT video_id FROM analysis_runs LIMIT 1")["video_id"] == historical_id


def test_longstanding_duplicate_is_not_mistaken_for_relocation(tmp_path: Path):
    root = tmp_path / "library"
    a = root / "a"
    b = root / "b"
    a.mkdir(parents=True)
    b.mkdir()
    payload = b"same real duplicate" * 20000
    first = a / "movie.mp4"
    second = b / "movie.mp4"
    first.write_bytes(payload)
    second.write_bytes(payload)

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(root)]
    cfg.library.roots = [str(root)]
    cfg.paths.data_dir = str(tmp_path / "data")
    db = DB(cfg.db_path)
    library = Library(db, cfg)
    initial = library.scan()
    assert initial["duplicate_groups"] == 1
    first.unlink()
    result = library.scan()
    assert result["relocation_reconciled"] == 0
    rows = db.videos(include_missing=True)
    assert len(rows) == 2
    assert sum(row["status"] == "missing" for row in rows) == 1


def test_footer_and_hot_apply_copy_are_packaged(tmp_path: Path):
    cfg, app = configured_app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/").text
        script = client.get("/static/app.js").text
    assert 'class="site-footer"' in page
    assert 'id="footerVersion"' in page
    assert 'id="footerDoctorButton"' in page
    assert 'id="footerSettingsButton"' in page
    assert "No NAS existential crisis" in page
    assert "Settings saved and applied." in script
    assert "Restart HereTune to apply the new configuration" not in script
