from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from heretune import __version__
from heretune.config import Config
from heretune.server import create_app


def test_version_and_favicon_assets(tmp_path: Path):
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    assert __version__ == "0.13.0"
    with TestClient(app) as client:
        ico = client.get("/favicon.ico")
        assert ico.status_code == 200
        assert ico.headers["content-type"].startswith("image/x-icon")
        svg = client.get("/favicon.svg")
        assert svg.status_code == 200
        assert svg.headers["content-type"].startswith("image/svg+xml")


def test_openapi_operation_ids_are_unique(tmp_path: Path):
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.paths.data_dir = str(tmp_path / "data")
    app = create_app(cfg)
    schema = app.openapi()
    operation_ids: list[str] = []
    for item in schema.get("paths", {}).values():
        for operation in item.values():
            operation_id = operation.get("operationId")
            if operation_id:
                operation_ids.append(operation_id)
    assert len(operation_ids) == len(set(operation_ids))
