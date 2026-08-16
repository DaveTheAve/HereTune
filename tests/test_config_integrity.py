from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path
import sqlite3

import pytest
import tomllib

from heretune.config import Config, load_config
from heretune.db import DB, SCHEMA_VERSION


def _flatten_dataclass(obj, prefix=""):
    result = {}
    for field in fields(obj):
        if field.name == "config_path":
            continue
        value = getattr(obj, field.name)
        key = f"{prefix}.{field.name}" if prefix else field.name
        if is_dataclass(value):
            result.update(_flatten_dataclass(value, key))
        else:
            result[key] = value
    return result


def _flatten_dict(value, prefix=""):
    result = {}
    for name, item in value.items():
        key = f"{prefix}.{name}" if prefix else name
        if isinstance(item, dict):
            result.update(_flatten_dict(item, key))
        else:
            result[key] = item
    return result


def test_shipped_example_has_every_config_field_and_matches_python_defaults():
    root = Path(__file__).resolve().parents[1]
    with (root / "config.example.toml").open("rb") as handle:
        shipped = _flatten_dict(tomllib.load(handle))
    defaults = _flatten_dataclass(Config())
    assert shipped == defaults


def test_public_repo_tracks_only_one_config_template():
    root = Path(__file__).resolve().parents[1]
    assert sorted(path.name for path in root.glob("config*.toml")) == ["config.example.toml"]


def test_loaded_canonical_balanced_values_are_not_stale():
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.example.toml")
    assert (cfg.analysis.gpu_decode_workers, cfg.analysis.cpu_analysis_workers, cfg.analysis.probe_workers) == (4, 8, 8)
    assert cfg.analysis.analysis_width == 960
    assert cfg.analysis.sample_interval_seconds == 30.0
    assert cfg.analysis.max_samples == 360
    assert cfg.analysis.scene_probe_interval_seconds == 10.0
    assert cfg.analysis.scene_full_samples_per_segment == 9
    assert cfg.analysis.scene_min_seconds == 30.0
    assert cfg.analysis.scene_max_keyframes == 0
    assert cfg.analysis.vr_verification_frames == 6
    assert cfg.analysis.ffmpeg_sample_timeout_seconds == 20.0
    assert cfg.analysis.ffmpeg_aux_timeout_seconds == 45.0
    assert cfg.analysis.ffprobe_timeout_seconds == 30.0


def test_public_database_schema_starts_at_one_and_rejects_unknown_schema(tmp_path: Path):
    assert SCHEMA_VERSION == 1
    fresh = DB(tmp_path / "fresh.sqlite3")
    assert fresh.one("SELECT value FROM settings WHERE key='schema_version'")["value"] == "1"

    unsupported = tmp_path / "unsupported.sqlite3"
    with sqlite3.connect(unsupported) as connection:
        connection.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO settings(key,value) VALUES('schema_version','999')")
    with pytest.raises(RuntimeError, match="unsupported database schema"):
        DB(unsupported)
