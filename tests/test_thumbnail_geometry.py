from pathlib import Path

from heretune import __version__


def test_version_and_thumbnail_geometry():
    assert __version__ == "0.13.0"
    root = Path(__file__).resolve().parents[1]
    css = (root / "heretune/static/styles.css").read_text(encoding="utf-8")
    assert ".video-thumb { display:block; width:100%; height:100%;" in css
    assert "object-fit:contain" in css
    assert "tbody tr:hover .video-thumb-wrap" in css
    assert "tbody tr:hover .video-thumb { transform:scale" not in css
    assert ".video-thumb-wrap { width:118px; }" in css
    assert ".video-thumb-wrap { width:92px; }" in css
    assert ".video-thumb-wrap { width:76px; }" in css


def test_isolation_lab_doc_removed_but_feature_remains():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "docs/HSP_ISOLATION_LAB.md").exists()
    server = (root / "heretune/server.py").read_text(encoding="utf-8")
    app = (root / "heretune/static/app.js").read_text(encoding="utf-8")
    assert "hsp-diagnostic" in server
    assert "HSP isolation lab" in app
