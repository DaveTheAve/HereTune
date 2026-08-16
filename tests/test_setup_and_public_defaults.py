from __future__ import annotations

import base64
from pathlib import Path

from fastapi.testclient import TestClient

from heretune import __version__
from heretune.config import Config, analysis_presets
from heretune.server import create_app


def _auth(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_public_defaults_and_presets():
    cfg = Config()
    assert __version__ == "0.13.0"
    assert cfg.library.roots == []
    assert cfg.analysis.analysis_width == 960
    presets = analysis_presets()
    assert set(presets) == {"balanced", "high_coverage", "fast", "low_resource"}
    assert presets["balanced"]["values"]["analysis_width"] == 960
    assert presets["high_coverage"]["values"]["max_samples"] == 0


def test_first_run_setup_can_hot_apply_library_and_preset(tmp_path: Path):
    media = tmp_path / "media"; media.mkdir()
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    with TestClient(app) as client:
        status = client.get("/api/setup/status").json()
        assert status["required"] is True
        assert "acceleration" in status and "resolved_mode" in status["acceleration"]
        assert status["presets"]["balanced"]["values"]["analysis_width"] == 960
        test = client.post("/api/setup/test-library", json={"root": str(media)}).json()
        assert test["ok"] is True
        result = client.post("/api/setup/complete", json={"library_roots": [str(media)], "preset": "low_resource"})
        assert result.status_code == 200
        assert cfg.library.roots == [str(media)]
        assert cfg.analysis.analysis_width == 640
        assert (tmp_path / "config.toml").exists()


def test_optional_dashboard_basic_auth(tmp_path: Path):
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.server.dashboard_username = "test"
    cfg.server.dashboard_password = "secret"
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get("/").status_code == 401
        assert client.get("/api/status").status_code == 401
        assert client.get("/", headers=_auth("test", "secret")).status_code == 200
        # HereSphere API auth is deliberately separate from dashboard Basic auth.
        assert client.get("/heresphere").status_code == 200


def test_public_repo_files_and_guides():
    root = Path(__file__).resolve().parents[1]
    assert sorted(path.name for path in root.glob("config*.toml")) == ["config.example.toml"]
    for name in (
        "LICENSE", "COMMERCIAL_USE.md", "SECURITY.md", "CONTRIBUTING.md",
        "CONTRIBUTOR_LICENSE_AGREEMENT.md", "CODE_OF_CONDUCT.md", "AGENTS.md", "THIRD_PARTY_NOTICES.md",
    ):
        assert (root / name).is_file(), name
    for name in (
        "HERESPHERE_API_QUICK_REFERENCE.md",
        "HERESPHERE_API_REFERENCE.md",
        "HERESPHERE_MODDING_AND_CUSTOMIZATION_GUIDE.md",
    ):
        assert (root / "docs" / name).is_file(), name


def test_first_run_and_settings_preset_ui_packaged():
    root = Path(__file__).resolve().parents[1] / "heretune" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    assert 'id="setupDialog"' in html
    assert 'id="setupPresetCards"' in html
    assert 'id="settingsPresetCards"' in html
    assert 'id="tourDialog"' in html
    assert 'id="setupAccelText"' in html
    assert "initializeFirstRun" in js
    assert "startGuidedTour" in js
    assert "applyPresetToSettings" in js
