from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from heretune import __version__
from heretune.config import Config, config_to_toml, load_config
from heretune.library import now, sid
from heretune.server import _alpha_library_label, _compact_library_groups, create_app


def test_version_and_alpha_labels():
    assert __version__ == "0.13.0"
    assert [_alpha_library_label(i) for i in (0, 1, 25, 26, 27, 51, 52)] == ["A", "B", "Z", "AA", "AB", "AZ", "BA"]
    groups = [
        {"name": "Favorites", "list": ["favorite"]},
        {"name": "All Movies", "list": ["all"]},
        *[{"name": f"Folder {i}", "list": [str(i)]} for i in range(28)],
    ]
    compact = _compact_library_groups(groups)
    assert compact[0]["name"] == "♥"
    assert compact[1]["name"] == "All"
    assert compact[2]["name"] == "A"
    assert compact[27]["name"] == "Z"
    assert compact[28]["name"] == "AA"
    assert groups[0]["name"] == "Favorites"  # presentation-only; input is not mutated
    assert groups[2]["name"] == "Folder 0"


def test_compact_library_setting_round_trips(tmp_path: Path):
    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.server.compact_heresphere_library_labels = True
    cfg.config_path.write_text(config_to_toml(cfg), encoding="utf-8")
    loaded = load_config(cfg.config_path)
    assert loaded.server.compact_heresphere_library_labels is True


def test_heresphere_compact_groups_are_dynamic_and_do_not_touch_db(tmp_path: Path):
    library = tmp_path / "movies"
    (library / "Long Folder Alpha").mkdir(parents=True)
    (library / "Long Folder Beta").mkdir(parents=True)
    source_a = library / "Long Folder Alpha" / "hello.mp4"
    source_b = library / "Long Folder Beta" / "testing.mp4"
    source_a.write_bytes(b"a")
    source_b.write_bytes(b"b")

    cfg = Config(config_path=tmp_path / "config.toml")
    cfg.library.roots = [str(library)]
    cfg.library.roots = [str(library)]
    cfg.library.scan_interval_seconds = 600
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.server.compact_heresphere_library_labels = True
    app = create_app(cfg)
    db = app.state.db
    stamp = now()
    for source in (source_a, source_b):
        stat = source.stat()
        rel = str(source.relative_to(library))
        db.exec(
            """INSERT INTO videos(id,path,rel_path,library_root,title,size,mtime_ns,status,metadata_json,date_added,last_seen)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (sid(rel), str(source), rel, str(library), source.stem, stat.st_size, stat.st_mtime_ns, "pending", json.dumps({"duration": 60.0}), stamp, stamp),
        )

    # This test is about presentation, not ready-only filtering.
    cfg.server.heresphere_ready_only = False
    db.exec("UPDATE videos SET is_favorite=1 WHERE id=?", (sid(str(source_a.relative_to(library))),))

    with TestClient(app) as client:
        payload = client.get("/heresphere").json()
    names = [item["name"] for item in payload["library"]]
    assert names[:2] == ["♥", "All"]
    assert names[2:] == [_alpha_library_label(i) for i in range(len(names) - 2)]
    assert not any("Long Folder" in name for name in names)
    rows = db.all("SELECT rel_path FROM videos ORDER BY rel_path")
    assert rows[0]["rel_path"].startswith("Long Folder Alpha")
    assert rows[1]["rel_path"].startswith("Long Folder Beta")


def test_copy_button_uses_sync_first_and_has_visual_feedback():
    root = Path(__file__).resolve().parents[1]
    js = (root / "heretune/static/app.js").read_text(encoding="utf-8")
    css = (root / "heretune/static/styles.css").read_text(encoding="utf-8")
    html = (root / "heretune/static/index.html").read_text(encoding="utf-8")
    function = js[js.index("async function copyText"):js.index("async function refreshStatus")]
    assert function.index('document.execCommand?.("copy")') < function.index("navigator.clipboard?.writeText")
    assert 'button.textContent = "Copied!"' in js
    assert ".endpoint-copy-button:hover:not(:disabled)" in css
    assert 'name="server.compact_heresphere_library_labels"' in html
