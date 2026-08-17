from pathlib import Path

from heretune import __version__
from heretune.analyzer import ANALYSIS_VERSION


def test_public_release_polish_version_and_engine_invariant():
    assert __version__ == "0.13.1"
    assert ANALYSIS_VERSION == "offset-free-tone-1"


def test_library_uses_real_pagination_controls():
    root = Path(__file__).resolve().parents[1]
    html = (root / "heretune/static/index.html").read_text(encoding="utf-8")
    js = (root / "heretune/static/app.js").read_text(encoding="utf-8")
    # Full bottom paginator remains intact.
    for control in ("pageFirstButton", "pagePrevButton", "pageNextButton", "pageLastButton", "pageSizeSelect"):
        assert control in html
    assert "Page 1 of 1" in html
    # A separate compact pager is added above the table.
    for control in ("pagePrevTopButton", "pageStatusTop", "pageNextTopButton"):
        assert control in html
    assert "pagination-top" in html
    assert "loadMoreButton" not in html
    assert "limit: 50" in js
    assert "(state.page - 1) * state.limit" in js
    assert "goToLibraryPage" in js
    assert "pageStatusTop" in js


def test_interactive_states_and_footer_fixes_are_packaged():
    root = Path(__file__).resolve().parents[1]
    css = (root / "heretune/static/styles.css").read_text(encoding="utf-8")
    assert "a.button:link, a.button:visited" in css
    assert ":focus-visible" in css
    assert ".profile-switch button:hover:not(:disabled)" in css
    assert ".favorite-toggle:hover" in css
    assert "prefers-reduced-motion" in css
    assert "grid-template-columns:minmax(0,1fr) minmax(320px,auto) minmax(0,1fr)" in css
    assert ".site-footer-brand small { display:block; line-height:1.45; padding-bottom:2px; overflow:visible; }" in css
    assert "h1 { font-size: clamp(46px, 6vw, 82px); line-height: 1.03;" in css


def test_github_security_release_files_and_readme_screenshots():
    root = Path(__file__).resolve().parents[1]
    codeql = (root / ".github/workflows/codeql.yml").read_text(encoding="utf-8")
    release = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
    ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "github/codeql-action/init@v4" in codeql
    assert "languages: python" in codeql
    assert "languages: javascript-typescript" in codeql
    assert "actions/attest@v4" in release
    assert "attestations: write" in release
    assert "name: CI required" in ci
    assert (root / "docs/CONFIGURATION.md").is_file()
    assert (root / "docs/TROUBLESHOOTING.md").is_file()
    for name in ("dashboard.png", "native-image-grade.png", "scene-grade.png", "analysis-evidence.png", "vr-verification.png"):
        image = root / "docs/images" / name
        assert image.is_file() and image.stat().st_size > 20_000
        assert f"docs/images/{name}" in readme
    assert not (root / "docs/images/settings.png").exists()
    assert not (root / "docs/images/video-details.png").exists()
    assert "docs/images/settings.png" not in readme
    assert "docs/images/video-details.png" not in readme


def test_compact_top_pager_shares_toolbar_row():
    root = Path(__file__).resolve().parents[1]
    html = (root / "heretune/static/index.html").read_text(encoding="utf-8")
    css = (root / "heretune/static/styles.css").read_text(encoding="utf-8")
    actions_start = html.index('<div class="table-actions">')
    actions_end = html.index('<div class="selection-bar"', actions_start)
    pager = html.index('id="pagePrevTopButton"')
    sort = html.index('id="orderSelect"')
    assert actions_start < pager < actions_end
    assert actions_start < sort < actions_end
    assert pager < sort
    assert 'id="pageFirstButton"' in html
    assert 'id="pageLastButton"' in html
    assert '.pagination-top { min-height:36px; margin-left:auto;' in css
