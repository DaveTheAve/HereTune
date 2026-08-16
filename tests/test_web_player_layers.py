from pathlib import Path


def test_player_hidden_layers_are_actually_hidden_and_assets_cache_busted():
    root = Path(__file__).resolve().parents[1]
    html = (root / "heretune/static/player.html").read_text(encoding="utf-8")
    css = (root / "heretune/static/player.css").read_text(encoding="utf-8")
    assert "[hidden]{display:none !important}" in css
    assert '/static/player.css?v=' in html
    assert '/static/player.js?v=' in html
    assert 'id="stageError" hidden' in html
    assert 'id="fallbackVideo"' in html and 'hidden' in html


def test_player_explicitly_supports_2d_full_frame_correction_and_vr_to_2d():
    root = Path(__file__).resolve().parents[1]
    js = (root / "heretune/static/player.js").read_text(encoding="utf-8")
    html = (root / "heretune/static/player.html").read_text(encoding="utf-8")
    assert "function isOrdinary2D()" in js
    assert "2D Full Frame" in js
    assert "ordinary2D" in js
    assert "colorMode === 'original' ? 0 : colorMode === 'corrected' ? 1 : 2" in js
    assert "if (corrected) c = grade(c);" in js
    assert "Normal 2D videos use the full frame" in html
    assert "VR videos add a 2D projection or single-eye step" in html


def test_player_bootstrap_diagnostics_fire_before_media_attempts():
    root = Path(__file__).resolve().parents[1]
    js = (root / "heretune/static/player.js").read_text(encoding="utf-8")
    bootstrap = js.index("reportPlaybackEvent('player-bootstrap'")
    first_source = js.index("setVideoSource('original'")
    assert bootstrap < first_source
    assert "[HereTune Web Player]" in js
    assert "player-metadata" in js
    assert "playback source ready" in js
