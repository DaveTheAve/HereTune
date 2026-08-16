from pathlib import Path


def test_compact_filter_toolbar_and_reset_are_packaged():
    root = Path(__file__).resolve().parents[1]
    html = (root / "heretune/static/index.html").read_text(encoding="utf-8")
    css = (root / "heretune/static/styles.css").read_text(encoding="utf-8")
    js = (root / "heretune/static/app.js").read_text(encoding="utf-8")
    assert 'class="filter-cluster"' in html
    assert 'id="resetFiltersButton"' in html
    assert 'class="compact-filter sort-filter"' in html
    assert '.filter-cluster' in css
    assert 'function resetLibraryFilters()' in js
    assert 'updateResetFiltersState' in js


def test_hero_is_compacted_for_above_fold_content():
    root = Path(__file__).resolve().parents[1]
    css = (root / "heretune/static/styles.css").read_text(encoding="utf-8")
    assert 'min-height: 305px' in css
    assert 'padding: 38px 0 40px' in css


def test_thumbnail_stage_is_visible_to_ui():
    root = Path(__file__).resolve().parents[1]
    js = (root / "heretune/static/app.js").read_text(encoding="utf-8")
    assert 'thumbnail: "Creating monitor-friendly browse preview…"' in js
