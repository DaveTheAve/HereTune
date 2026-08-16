from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from heretune.cli import _network_kind
from heretune.config import Config, load_config, save_config_patch
from heretune.media import _decode_output, thumbnail_filters
from heretune.server import create_app


def test_external_tool_output_never_uses_windows_charmap():
    # 0x9d is undefined in cp1252 and was the byte triggering Windows reader-thread crashes.
    text = _decode_output(b"before\x9dafter")
    assert text.startswith("before")
    assert "after" in text
    assert "\ufffd" in text


def test_sbs_fisheye_thumbnail_uses_proven_single_eye_center_preview():
    filters = thumbnail_filters({
        "projection": "fisheye",
        "stereo": "sbs",
        "fov": 200.0,
        "isEyeSwapped": False,
    })
    joined = ",".join(filters)
    assert joined.startswith("crop=")
    assert "v360=" not in joined
    assert "scale=640:360:force_original_aspect_ratio=increase" in joined
    assert "crop=640:360" in joined


def test_vr180_thumbnail_uses_single_eye_center_preview_without_guessing_lens_math():
    joined = ",".join(thumbnail_filters({
        "projection": "equirectangular",
        "stereo": "sbs",
        "fov": 180.0,
    }))
    assert joined.startswith("crop=")
    assert "v360=" not in joined
    assert "crop=640:360" in joined


def test_perspective_stereo_thumbnail_still_crops_to_one_eye():
    joined = ",".join(thumbnail_filters({
        "projection": "perspective",
        "stereo": "sbs",
    }))
    assert joined.startswith("crop=")
    assert "v360=" not in joined
    assert "crop=640:360" in joined


def test_frames_ui_attempt_count_can_exceed_base_without_being_wrong(tmp_path: Path):
    # API-facing accounting is covered by the UI implementation; this ensures the
    # config itself exposes the current high-coverage 48 -> 360 adaptive plan.
    cfg = Config(config_path=tmp_path / "config.toml")
    assert cfg.analysis.samples == 48
    assert cfg.analysis.max_samples == 360


def test_settings_patch_rewrites_real_toml_and_preserves_auto_policy(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("[analysis]\nsamples = 48\n\n[hsp]\nprefer_manual_writeback = false\n", encoding="utf-8")
    saved = save_config_patch(config_path, {
        "library": {"roots": [r"\\server\share\movies"], "scan_interval_seconds": 90},
        "analysis": {"execution_mode": "auto", "auto_preference": "hybrid", "samples": 56, "max_samples": 80},
        "hsp": {"prefer_manual_writeback": False, "revisioned_urls": True, "generated_version": 7},
    })
    assert saved.analysis.samples == 56
    loaded = load_config(config_path)
    assert loaded.library.roots == [r"\\server\share\movies"]
    assert loaded.analysis.max_samples == 80
    assert loaded.hsp.prefer_manual_writeback is False
    assert "[server]" in config_path.read_text(encoding="utf-8")


def test_settings_api_and_badges_removed(tmp_path: Path):
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(tmp_path)]
    cfg.paths.data_dir = str(tmp_path / "data")
    # Write an actual file so GET /api/config reads the same settings.
    save_config_patch(cfg.config_path, {
        "library": {"roots": [str(tmp_path)], "scan_interval_seconds": 600},
        "paths": {"data_dir": str(tmp_path / "data")},
    })
    app = create_app(load_config(cfg.config_path))
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "settingsButton" in page.text
        assert "settingsDialog" in page.text
        assert "analysisSummary" not in page.text
        assert "profilePolicy" not in page.text
        current = client.get("/api/config")
        assert current.status_code == 200
        assert current.json()["analysis"]["auto_preference"] == "hybrid"
        changed = client.put("/api/config", json={"analysis": {"samples": 52, "max_samples": 76}})
        assert changed.status_code == 200
        assert changed.json()["restart_required"] is False
        assert load_config(cfg.config_path).analysis.samples == 52


def test_network_classification_prefers_lan_and_recognizes_tailscale():
    assert _network_kind("192.168.0.237", "Ethernet")[1] == "LAN / Quest"
    assert _network_kind("100.104.61.14", "Tailscale")[1] == "Tailscale"
    assert _network_kind("172.31.208.1", "vEthernet (WSL)")[1] == "Virtual"


def test_hybrid_gpu_preflight_failure_switches_whole_file_to_cpu(monkeypatch, tmp_path: Path):
    import numpy as np
    import heretune.analyzer as analyzer

    monkeypatch.setattr(analyzer, "resolve_execution_mode", lambda cfg, ffmpeg: ("hybrid", None))
    monkeypatch.setattr(analyzer, "_run_sample_command", lambda command, width, height: (None, "NVDEC says nope"))
    calls = []

    def fake_sample(path, ffmpeg, timestamp, width, height, prefer_gpu, gpu_device, fallback_to_cpu):
        calls.append(prefer_gpu)
        return np.full((height, width, 3), 128, dtype=np.uint8), "cpu", ""

    monkeypatch.setattr(analyzer, "_sample_frame_with_fallback", fake_sample)
    info = analyzer.MediaInfo(duration=30.0, width=1920, height=960)
    profile = analyzer.analyze_video(
        tmp_path / "demo.mp4",
        info,
        "ffmpeg",
        samples=4,
        analysis_width=320,
        strength=1.0,
        white_balance=True,
        tone=True,
        saturation_correction=True,
        skin_priority=1.0,
        execution_mode="auto",
        auto_preference="hybrid",
        gpu_fallback_to_cpu=True,
        adaptive_sampling=False,
        max_samples=4,
    )
    assert profile.execution_mode == "cpu"
    assert profile.gpu_preflight_fallback is True
    assert profile.gpu_fallback_samples == 0
    assert calls and all(value is False for value in calls)


def test_clean_startup_banner_groups_networks(monkeypatch, capsys):
    from heretune import cli

    cfg = Config()
    monkeypatch.setattr(cli, "_discover_addresses", lambda: [
        ("192.168.0.237", "Ethernet", "LAN / Quest"),
        ("100.104.61.14", "Tailscale", "Tailscale"),
        ("172.31.208.1", "vEthernet (WSL)", "Virtual"),
    ])
    cli._print_startup_banner(cfg)
    output = capsys.readouterr().out
    assert "QUEST / LAN  [recommended]" in output
    assert "http://127.0.0.1:8777/ui" in output
    assert "http://192.168.0.237:8777/heresphere" in output
    assert "Tailscale" in output
    assert "Uvicorn" not in output
