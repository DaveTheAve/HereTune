from pathlib import Path

from heretune import __version__


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_webgl_video_texture_is_uploaded_upright():
    js = (_root() / "heretune/static/player.js").read_text(encoding="utf-8")
    # DOM video/image sources have a top-left origin; WebGL texture coordinates
    # are bottom-left. Without this, the complete rendered player is upside down.
    assert "gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);" in js
    assert "textureYFlip: true" in js


def test_top_bottom_stereo_keeps_top_eye_as_eye_zero_after_y_flip():
    js = (_root() / "heretune/static/player.js").read_text(encoding="utf-8")
    # Once the texture is flipped at upload, physical source top occupies
    # texture-v .5..1, so eye zero must map into that upper texture half.
    assert "if (uStereo == 2) return vec2(uv.x, (uv.y + float(1 - uEye)) * .5);" in js


def test_web_player_has_session_orientation_escape_hatch_without_touching_raw():
    root = _root()
    js = (root / "heretune/static/player.js").read_text(encoding="utf-8")
    html = (root / "heretune/static/player.html").read_text(encoding="utf-8")
    assert 'id="orientationGroup"' in html
    for value in ("normal", "rotate180", "mirror", "flipv"):
        assert f'data-orientation="{value}"' in html
    assert "if (uView != 0) local = orientLocal(local);" in js
    assert "if (orientationGroup) orientationGroup.hidden = viewMode === 'raw';" in js


def test_player_assets_are_cache_busted():
    html = (_root() / "heretune/static/player.html").read_text(encoding="utf-8")
    js = (_root() / "heretune/static/player.js").read_text(encoding="utf-8")
    assert f"const PLAYER_BUILD = '{__version__}';" in js
    assert f"/static/player.css?v={__version__}" in html
    assert f"/static/player.js?v={__version__}" in html
