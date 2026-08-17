from pathlib import Path

from heretune import __version__
from heretune.analyzer import ANALYSIS_VERSION


def test_half_star_ui_only():
    assert __version__ == "0.13.1"
    root = Path(__file__).resolve().parents[1]
    app = (root / "heretune/static/app.js").read_text(encoding="utf-8")
    css = (root / "heretune/static/styles.css").read_text(encoding="utf-8")
    assert "initializeRatingPicker(video)" in app
    assert "data-star-rating" in app
    assert "--star-preview-fill" in app
    assert '.star-editor.is-preview .rating-star-glyph' in css
    assert 'var(--star-preview-fill' in css
    assert "Avg. source sample" not in app


def test_analysis_engine_is_unchanged():
    assert ANALYSIS_VERSION == "offset-free-tone-1"
