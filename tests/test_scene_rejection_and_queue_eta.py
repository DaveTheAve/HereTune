from __future__ import annotations

import numpy as np

from heretune.analyzer import _smart_select_scene_statistics


def _row(ts: float, signature: np.ndarray, *, black: bool = False) -> dict:
    if black:
        return {
            "_timestamp": ts,
            "p50": .02,
            "p95": .04,
            "luma_spread": .02,
            "black_fraction": .97,
            "median_sat": .02,
            "edge_density": .01,
            "signature": signature,
        }
    return {
        "_timestamp": ts,
        "p50": .46,
        "p95": .82,
        "luma_spread": .50,
        "black_fraction": .03,
        "median_sat": .30,
        "edge_density": .08,
        "signature": signature,
    }


def test_dense_scene_smart_rejection_counts_bad_full_samples_per_segment():
    # Five candidates per segment means rejecting one bad frame still leaves
    # enough evidence that the selector does not invoke its safety rollback.
    rows = [
        _row(5, np.zeros(16, np.float32), black=True),
        _row(10, np.full(16, .10, np.float32)),
        _row(20, np.full(16, .25, np.float32)),
        _row(30, np.full(16, .40, np.float32)),
        _row(40, np.full(16, .55, np.float32)),
        _row(65, np.full(16, .70, np.float32), black=True),
        _row(70, np.full(16, .12, np.float32)),
        _row(80, np.full(16, .28, np.float32)),
        _row(90, np.full(16, .44, np.float32)),
        _row(100, np.full(16, .60, np.float32)),
    ]
    selected, rejected = _smart_select_scene_statistics(rows, [60.0])
    assert len(selected) == 8
    assert rejected == {"black_fade": 2, "title_like": 0, "duplicate": 0, "coverage_trim": 0}


def test_dense_scene_duplicate_rejection_does_not_cross_scene_boundary():
    # The same visual signature in another stable scene must not be rejected
    # merely because it resembles a frame from the previous scene.
    rows = [
        _row(10, np.full(16, .10, np.float32)),
        _row(20, np.full(16, .25, np.float32)),
        _row(30, np.full(16, .40, np.float32)),
        _row(40, np.full(16, .55, np.float32)),
        _row(50, np.full(16, .70, np.float32)),
        _row(70, np.full(16, .10, np.float32)),
        _row(80, np.full(16, .30, np.float32)),
        _row(90, np.full(16, .45, np.float32)),
        _row(100, np.full(16, .60, np.float32)),
        _row(110, np.full(16, .75, np.float32)),
    ]
    selected, rejected = _smart_select_scene_statistics(rows, [60.0])
    assert len(selected) == 10
    assert rejected["duplicate"] == 0


def test_dense_scene_rejection_safety_restores_segment_when_too_little_evidence():
    rows = [
        _row(5, np.zeros(16, np.float32), black=True),
        _row(10, np.full(16, .10, np.float32)),
        _row(20, np.full(16, .1005, np.float32)),  # near duplicate
        _row(30, np.full(16, .101, np.float32)),   # near duplicate
        _row(40, np.full(16, .1015, np.float32)), # near duplicate
    ]
    selected, rejected = _smart_select_scene_statistics(rows, [])
    # Fewer than four clean frames -> reliability fallback restores candidates,
    # therefore these were screened but not actually rejected from the grade.
    assert len(selected) == 5
    assert rejected == {"black_fade": 0, "title_like": 0, "duplicate": 0, "coverage_trim": 0}



def test_dashboard_summary_copy_explains_metrics_and_eta_basis():
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "heretune" / "static"
    html = (package / "index.html").read_text(encoding="utf-8")
    app = (package / "app.js").read_text(encoding="utf-8")

    assert '<span>Analyzed</span>' in html
    assert '<small>corrections ready</small>' in html
    assert '<span>Avg. WB confidence</span>' in html
    assert '<small>neutral + face/skin evidence</small>' in html
    assert ' · learned' not in app
    assert 'based on ${fmtNumber(etaHistoryCount)} recent' not in app
    assert 'baseline estimate' not in app


def test_queue_eta_api_exposes_explicit_estimate_source(tmp_path):
    from types import SimpleNamespace

    from heretune.db import DB
    from heretune.server import _queue_eta_model

    db = DB(tmp_path / "eta.sqlite")
    cfg = SimpleNamespace(analysis=SimpleNamespace(
        samples=48,
        adaptive_sampling=True,
        sample_interval_seconds=30.0,
        max_samples=360,
        candidate_multiplier=1.35,
        smart_sampling=True,
    ))
    model = _queue_eta_model(db, cfg)
    assert model["history_count"] == 0
    assert model["estimate_source"] == "baseline"
