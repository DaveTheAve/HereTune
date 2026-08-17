from __future__ import annotations

import inspect as pyinspect
import json
import math
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .analyzer import ANALYSIS_VERSION, AnalysisError, analyze
from .analysis_cache import AnalysisCache
from .hsp import build, inspect as inspect_hsp
from .hsp_policy import (
    apply_auto_image_policy,
    effective_auto_native_policy,
    normalize_native_policy_overrides,
)
from .media import detect_layout_from_video, probe, thumbnail
from .vrformat import VISUAL_CALIBRATION_VERSION, apply_layout_override, detect

THUMBNAIL_LAYOUT_VERSION = 7


def _analysis_log_path(cfg) -> Path:
    path = Path(cfg.data_dir) / "logs" / "analysis_failures.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_analysis_log(cfg, text: str) -> None:
    """Best-effort persistent diagnostics for recoverable per-video failures."""
    try:
        path = _analysis_log_path(cfg)
        with path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(text.rstrip() + "\n")
    except Exception:
        # Logging must never turn a recoverable video failure into a server failure.
        pass


def _console_analysis_line(text: str, *, error: bool = False) -> None:
    try:
        print(text, file=sys.stderr if error else sys.stdout, flush=True)
    except Exception:
        pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def effective_metadata(row: dict[str, object], metadata: dict[str, object] | None = None) -> dict[str, object]:
    return apply_layout_override(dict(metadata or row.get("metadata") or {}), row.get("layout_override") or {})


def native_calibration_metadata(
    row: dict[str, object],
    metadata: dict[str, object] | None,
    cfg,
    correction: object | dict[str, object] | None = None,
) -> dict[str, object]:
    """Resolve effective metadata plus bounded, evidence-backed native HSP controls.

    Analysis decides what evidence to measure. Global + per-video HSP output
    policy independently decides which automatic native-calibration suggestions
    may reach Auto HSPs. Global Auto-ON controls remain confidence-gated. An
    explicit per-video Force On is a user decision and therefore bypasses the
    confidence threshold while retaining hard structural/range/projection safety
    checks. Stereo disparity and safe encoded-border crop preserve the public
    v0.13.0 global ON defaults; newly exposed controls stay OFF by default.
    Explicit numeric layout overrides are applied first and remain authoritative.
    """
    result = effective_metadata(row, metadata)
    override = row.get("layout_override") or {}
    if not isinstance(override, dict):
        override = {}

    def number(value: object, fallback: float = 0.0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return fallback
        return parsed if math.isfinite(parsed) else fallback

    def triple(value: object, fallback=(0.0, 0.0, 0.0)) -> tuple[float, float, float]:
        try:
            parts = tuple(float(item) for item in value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return tuple(fallback)
        if len(parts) != 3 or not all(math.isfinite(item) for item in parts):
            return tuple(fallback)
        return parts

    def vec4(value: object, fallback) -> tuple[float, float, float, float]:
        try:
            parts = tuple(float(item) for item in value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return tuple(fallback)
        if len(parts) != 4 or not all(math.isfinite(item) for item in parts):
            return tuple(fallback)
        return parts

    def correction_value(name: str, fallback: float = 0.0) -> float:
        if correction is None:
            return fallback
        if isinstance(correction, dict):
            return number(correction.get(name), fallback)
        return number(getattr(correction, name, fallback), fallback)

    def overridden(*keys: str) -> bool:
        for key in keys:
            if key not in override:
                continue
            value = override.get(key)
            if value is not None and value != "" and value != "auto":
                return True
        return False

    raw_native_overrides = normalize_native_policy_overrides(
        row.get("hsp_policy_override") or {}
    )
    native_output_policy = effective_auto_native_policy(cfg, raw_native_overrides)

    def output_enabled(name: str) -> bool:
        return bool(native_output_policy.get(name, False))

    def forced_on(name: str) -> bool:
        # Explicit per-video On means "apply the measured suggestion for this
        # title even if it is below the library-wide automatic confidence gate."
        # Hard evidence validity, projection, range and manual-override checks
        # remain in force; only confidence is bypassed.
        return raw_native_overrides.get(name) == "on"

    def confidence_allows(name: str, value: float, threshold: float) -> bool:
        return forced_on(name) or value >= threshold

    applied: dict[str, bool] = {
        "vertical_alignment": False,
        "stereo_roll": False,
        "eye_scale_shear": False,
        "stitch": False,
        "crop": False,
        "horizon": False,
        "orientation_180": False,
        "eye_swap": False,
        "lens_fov": False,
        "autofocus": False,
        "audio_volume": False,
        "audio_sync": False,
        "sync_drift": False,
    }

    # --- Stereo alignment / roll / eye scale-shear / stitch --------------
    alignment = result.get("stereo_alignment") or {}
    if not isinstance(alignment, dict):
        alignment = {}
    confidence = number(alignment.get("confidence"), 0.0)
    minimum = number(getattr(cfg.analysis, "stereo_alignment_min_confidence", 0.72), 0.72)
    frames = int(number(alignment.get("frames"), 0.0))
    base_position = triple(result.get("alignment_position"), (0.0, 6.5, 0.0))
    base_rotation = list(triple(result.get("alignment_rotation"), (0.0, 0.0, 0.0)))

    alignment_cap = abs(number(getattr(cfg.analysis, "stereo_alignment_max_degrees", 0.75), 0.75))
    suggested = triple(alignment.get("suggested_rotation"), (0.0, 0.0, 0.0))
    vertical_disparity = number(alignment.get("median_vertical_disparity_px"), 0.0)
    vertical_stability = number(alignment.get("vertical_stability_px"), 999.0)
    vertical_sign_consistency = number(alignment.get("vertical_sign_consistency"), 0.0)
    stereo_structure_valid = bool(
        frames >= 2
        and abs(vertical_disparity) >= 0.75
        and vertical_sign_consistency >= 0.75
        and vertical_stability <= max(2.5, abs(vertical_disparity) * 0.65)
        and abs(suggested[0]) > 0.005
    )
    stereo_candidate = bool(alignment.get("auto_applicable")) or (
        forced_on("stereo_disparity") and stereo_structure_valid
    )
    if (
        bool(getattr(cfg.analysis, "stereo_alignment", True))
        and output_enabled("stereo_disparity")
        and not overridden("alignment_rotation", "alignment_position")
        and stereo_candidate
        and frames >= 2
        and confidence_allows("stereo_disparity", confidence, minimum)
    ):
        base_rotation[0] = max(-alignment_cap, min(alignment_cap, suggested[0]))
        result["alignment_position"] = base_position
        applied["vertical_alignment"] = abs(base_rotation[0]) > 1e-5

    roll_value = abs(number(alignment.get("median_roll_deg"), 0.0))
    roll_stability = number(alignment.get("roll_stability_deg"), 999.0)
    roll_structure_valid = bool(
        frames >= 2
        and 0.03 <= roll_value <= max(0.10, alignment_cap * 2.0)
        and roll_stability <= max(0.18, roll_value * 0.45)
    )
    roll_candidate = bool(alignment.get("roll_auto_applicable")) or (
        forced_on("stereo_roll") and roll_structure_valid
    )
    if (
        bool(getattr(cfg.analysis, "stereo_roll_correction", True))
        and output_enabled("stereo_roll")
        and not overridden("alignment_rotation")
        and roll_candidate
        and frames >= 2
        and confidence_allows("stereo_roll", confidence, max(0.68, minimum * 0.92))
    ):
        suggested_roll = triple(alignment.get("suggested_roll_rotation"), (0.0, 0.0, 0.0))[2]
        roll_cap = min(1.25, alignment_cap)
        base_rotation[2] = max(-roll_cap, min(roll_cap, suggested_roll))
        applied["stereo_roll"] = abs(base_rotation[2]) > 1e-5

    if applied["vertical_alignment"] or applied["stereo_roll"]:
        result["alignment_rotation"] = tuple(base_rotation)
        result["stereo_alignment_confidence"] = confidence

    suggested_stitch = alignment.get("suggested_stitch") or {}
    if not isinstance(suggested_stitch, dict):
        suggested_stitch = {}
    stitch_confidence = number(alignment.get("stitch_confidence"), confidence)
    eye_geometry_opt_in = bool(getattr(cfg.analysis, "stereo_scale_correction", True)) and output_enabled("eye_scale_shear")
    stitch_opt_in = bool(getattr(cfg.analysis, "stitch_correction", True)) and output_enabled("stitch_vectors")
    if (
        frames >= 2
        and (eye_geometry_opt_in or stitch_opt_in)
        and not overridden("stitch_shift", "stitch_scale", "stitch_shear", "stitch_flare", "stitch_slant")
    ):
        neutral_shift = (0.0, 0.0, 0.0, 0.0)
        neutral_scale = (1.0, 1.0, 1.0, 1.0)
        neutral_other = (0.0, 0.0, 0.0, 0.0)
        shift = vec4(suggested_stitch.get("shift"), neutral_shift)
        scale = vec4(suggested_stitch.get("scale"), neutral_scale)
        shear = vec4(suggested_stitch.get("shear"), neutral_other)
        flare = vec4(suggested_stitch.get("flare"), neutral_other)
        slant = vec4(suggested_stitch.get("slant"), neutral_other)
        max_shift = max(0.0, number(getattr(cfg.analysis, "max_stitch_shift", 0.025), 0.025))
        max_scale = max(0.0, number(getattr(cfg.analysis, "max_stitch_scale_delta", 0.030), 0.030))
        shift = tuple(max(-max_shift, min(max_shift, item)) for item in shift)
        scale = tuple(max(1.0 - max_scale, min(1.0 + max_scale, item)) for item in scale)
        shear = tuple(max(-0.04, min(0.04, item)) for item in shear)

        scale_x = number(alignment.get("median_scale_x"), 1.0)
        scale_y = number(alignment.get("median_scale_y"), 1.0)
        scale_delta = max(abs(scale_x - 1.0), abs(scale_y - 1.0))
        scale_stability = number(alignment.get("scale_stability"), 999.0)
        measured_shear = number(alignment.get("median_shear"), 0.0)
        scale_structure_valid = bool(
            0.0025 <= scale_delta <= 0.030
            and scale_stability <= 0.008
            and abs(measured_shear) <= 0.030
        )
        stitch_structure_valid = bool(
            scale_structure_valid
            or abs(shift[1]) >= 0.0015
            or abs(shift[3]) >= 0.0015
            or abs(shear[2]) >= 0.0015
            or abs(shear[3]) >= 0.0015
        )
        stitch_gate = max(0.72, minimum)
        eye_geometry_allowed = bool(
            eye_geometry_opt_in
            and (
                (bool(alignment.get("scale_auto_applicable")) and stitch_confidence >= stitch_gate)
                or (forced_on("eye_scale_shear") and scale_structure_valid)
            )
        )
        stitch_allowed = bool(
            stitch_opt_in
            and (
                (bool(alignment.get("stitch_auto_applicable")) and stitch_confidence >= stitch_gate)
                or (forced_on("stitch_vectors") and stitch_structure_valid)
            )
        )

        if eye_geometry_allowed:
            result["stitch_scale"] = scale
            result["stitch_shear"] = shear
            applied["eye_scale_shear"] = (
                any(abs(item - 1.0) > 1e-5 for item in scale)
                or any(abs(item) > 1e-5 for item in shear)
            )
        if stitch_allowed:
            result.update({
                "stitch_shift": shift,
                "stitch_scale": scale,
                "stitch_shear": shear,
                "stitch_flare": flare,
                "stitch_slant": slant,
            })
            applied["stitch"] = any(abs(item) > 1e-5 for item in (*shift, *shear, *flare, *slant)) or any(
                abs(item - 1.0) > 1e-5 for item in scale
            )
        if eye_geometry_allowed or stitch_allowed:
            result["stitch_confidence"] = stitch_confidence

    # --- Eye-order and 180-degree orientation suggestions -----------------
    explicit_filename_stereo = bool(result.get("layout_filename_explicit_stereo"))
    eye_swap_confidence = number(alignment.get("eye_swap_confidence"), 0.0)
    if (
        output_enabled("eye_swap")
        and not overridden("isEyeSwapped")
        and not explicit_filename_stereo
        and bool(alignment.get("eye_swap_suspected"))
        and confidence_allows("eye_swap", eye_swap_confidence, 0.86)
    ):
        result["isEyeSwapped"] = not bool(result.get("isEyeSwapped"))
        applied["eye_swap"] = True

    orientation = result.get("orientation_verification") or {}
    if not isinstance(orientation, dict):
        orientation = {}
    orientation_confidence = number(orientation.get("confidence"), 0.0)
    rotated_faces = int(number(orientation.get("rotated_faces"), 0.0))
    upright_faces = int(number(orientation.get("upright_faces"), 0.0))
    rotated_frames = int(number(orientation.get("rotated_frames"), 0.0))
    orientation_structure_valid = bool(
        rotated_faces >= upright_faces + 3
        and rotated_faces >= 3
        and rotated_frames >= 1
    )
    orientation_candidate = bool(orientation.get("rotate_180_suspected")) or (
        forced_on("orientation_180") and orientation_structure_valid
    )
    if (
        bool(getattr(cfg.analysis, "orientation_verification", True))
        and output_enabled("orientation_180")
        and not overridden("flip_left_right", "flip_up_down")
        and orientation_candidate
        and confidence_allows("orientation_180", orientation_confidence, 0.82)
    ):
        result["flip_left_right"] = True
        result["flip_up_down"] = True
        applied["orientation_180"] = True

    # --- Persistent encoded-border crop via native Format Zoom/Pan --------
    bounds = result.get("active_image_bounds") or {}
    if not isinstance(bounds, dict):
        bounds = {}
    projection = str(result.get("projection") or "perspective").casefold()
    crop_allowed_projection = projection not in {"fisheye", "cubemap", "equiangularcubemap"}
    crop_confidence = number(bounds.get("confidence"), 0.0)
    try:
        zoom = tuple(float(value) for value in bounds.get("suggested_zoom") or (1.0, 1.0))
        pan = tuple(float(value) for value in bounds.get("suggested_pan") or (0.0, 0.0))
    except (TypeError, ValueError):
        zoom, pan = (1.0, 1.0), (0.0, 0.0)
    max_zoom = max(1.0, number(getattr(cfg.analysis, "auto_crop_max_zoom", 1.32), 1.32))
    crop_vectors_valid = bool(
        len(zoom) == 2
        and len(pan) == 2
        and all(math.isfinite(value) for value in (*zoom, *pan))
        and max(zoom) <= max_zoom
        and min(zoom) >= 1.0
        and max(abs(value) for value in pan) <= 0.35
    )
    crop_structure_valid = bool(
        crop_vectors_valid
        and number(bounds.get("largest_border_fraction"), 0.0) >= 0.025
        and number(bounds.get("symmetry"), 0.0) >= 0.72
    )
    crop_candidate = bool(bounds.get("auto_applicable")) or (
        forced_on("crop") and crop_structure_valid
    )
    if (
        output_enabled("crop")
        and not overridden("zoom", "pan")
        and crop_allowed_projection
        and crop_candidate
        and confidence_allows(
            "crop", crop_confidence,
            number(getattr(cfg.analysis, "auto_crop_min_confidence", 0.82), 0.82),
        )
        and crop_vectors_valid
    ):
        result["zoom"] = tuple(max(1.0, min(max_zoom, value)) for value in zoom)
        result["pan"] = tuple(max(-0.35, min(0.35, value)) for value in pan)
        applied["crop"] = any(abs(value - 1.0) > 1e-4 for value in result["zoom"]) or any(
            abs(value) > 1e-4 for value in result["pan"]
        )
        result["crop_confidence"] = crop_confidence

    # --- Camera horizon leveling through Orientation rotation -------------
    horizon = result.get("horizon_verification") or {}
    if not isinstance(horizon, dict):
        horizon = {}
    horizon_confidence = number(horizon.get("confidence"), 0.0)
    horizon_frames = int(number(horizon.get("frames"), 0.0))
    horizon_roll = abs(number(horizon.get("median_roll_deg", horizon.get("roll_deg")), 0.0))
    horizon_stability = number(horizon.get("stability_deg"), 999.0)
    horizon_measurements = horizon.get("measurements") or []
    horizon_sign_support = 1.0
    if isinstance(horizon_measurements, list):
        meaningful_rolls = [
            number(item.get("roll_deg"), 0.0)
            for item in horizon_measurements
            if isinstance(item, dict) and abs(number(item.get("roll_deg"), 0.0)) >= 0.18
        ]
        if meaningful_rolls:
            positive = sum(value > 0 for value in meaningful_rolls) / len(meaningful_rolls)
            negative = sum(value < 0 for value in meaningful_rolls) / len(meaningful_rolls)
            horizon_sign_support = max(positive, negative)
    horizon_structure_valid = bool(
        horizon_frames >= 2
        and 0.20 <= horizon_roll <= 5.0
        and horizon_stability <= max(0.35, horizon_roll * 0.45)
        and horizon_sign_support >= 0.72
    )
    horizon_candidate = bool(horizon.get("auto_applicable")) or (
        forced_on("horizon") and horizon_structure_valid
    )
    if (
        bool(getattr(cfg.analysis, "horizon_leveling", True))
        and output_enabled("horizon")
        and not overridden("orientation_rotation")
        and horizon_candidate
        and confidence_allows(
            "horizon", horizon_confidence,
            number(getattr(cfg.analysis, "horizon_min_confidence", 0.78), 0.78),
        )
    ):
        rotation = list(triple(result.get("orientation_rotation"), (0.0, 0.0, 0.0)))
        suggestion = triple(horizon.get("suggested_orientation_rotation"), (0.0, 0.0, 0.0))
        max_degrees = abs(number(getattr(cfg.analysis, "horizon_max_degrees", 3.0), 3.0))
        rotation[2] = max(-max_degrees, min(max_degrees, suggestion[2]))
        result["orientation_rotation"] = tuple(rotation)
        result["horizon_confidence"] = horizon_confidence
        applied["horizon"] = abs(rotation[2]) > 1e-5

    # --- Lens/FOV verification --------------------------------------------
    lens_verification = result.get("lens_verification") or {}
    if not isinstance(lens_verification, dict):
        lens_verification = {}
    lens_fov_confidence = number(lens_verification.get("fov_confidence"), 0.0)
    suggested_fov = number(lens_verification.get("suggested_fov"), 0.0)
    if (
        bool(getattr(cfg.analysis, "lens_verification", True))
        and output_enabled("lens_fov")
        and not overridden("fov", "lens")
        and not bool(result.get("layout_filename_explicit_projection"))
        and suggested_fov > 0.0
        and confidence_allows(
            "lens_fov", lens_fov_confidence,
            number(getattr(cfg.analysis, "lens_fov_min_confidence", 0.80), 0.80),
        )
    ):
        result["fov"] = suggested_fov
        applied["lens_fov"] = True
    result["lens_fov_applied"] = applied["lens_fov"]
    result["lens_fov_confidence"] = lens_fov_confidence

    # --- Autofocus range recommendation from measured stereo disparity -----
    recommendation = alignment.get("autofocus_recommendation") or {}
    if not isinstance(recommendation, dict):
        recommendation = {}
    autofocus_confidence = number(recommendation.get("confidence"), 0.0)
    if (
        bool(getattr(cfg.analysis, "autofocus_tuning", True))
        and output_enabled("autofocus")
        and not overridden(
            "autofocus_rotation", "autofocus_focal_distance",
            "autofocus_min_distance", "autofocus_max_distance",
            "autofocus_transition_speed",
        )
        and confidence_allows("autofocus", autofocus_confidence, 0.72)
        and frames >= 2
    ):
        minimum_focus = max(1.0, min(1000.0, number(recommendation.get("min_distance"), 10.0)))
        maximum_focus = max(minimum_focus + 1.0, min(5000.0, number(recommendation.get("max_distance"), 1000.0)))
        result["autofocus_min_distance"] = minimum_focus
        result["autofocus_max_distance"] = maximum_focus
        result["autofocus_transition_speed"] = max(0.05, min(10.0, number(recommendation.get("transition_speed"), 1.0)))
        result["autofocus_confidence"] = autofocus_confidence
        applied["autofocus"] = True

    # --- HSP Audio and Sync keyframes --------------------------------------
    volume = correction_value("audio_volume_multiplier", 1.0)
    volume_confidence = correction_value("audio_loudness_confidence", 0.0)
    if (
        bool(getattr(cfg.analysis, "audio_correction", True))
        and bool(getattr(cfg.analysis, "loudness_normalization", True))
        and not overridden("audio_volume_multiplier")
        and volume_confidence >= 0.30
    ):
        result["audio_volume_multiplier"] = max(0.25, min(4.0, volume))
        applied["audio_volume"] = abs(result["audio_volume_multiplier"] - 1.0) >= 0.005
        result["audio_loudness_confidence"] = volume_confidence

    audio_offset = correction_value("suggested_audio_time_offset", correction_value("audio_time_offset", 0.0))
    drift = correction_value("suggested_sync_drift_seconds_per_hour", correction_value("sync_drift_seconds_per_hour", 0.0))
    audio_sync_confidence = correction_value("av_sync_confidence", 0.0)
    drift_confidence = correction_value("sync_drift_confidence", 0.0)
    if output_enabled("audio_sync"):
        if (
            not overridden("audio_time_offset", "sync_start_offset_seconds")
            and confidence_allows("audio_sync", audio_sync_confidence, 0.68)
            and abs(audio_offset) <= 0.50
            and abs(audio_offset) > 1e-5
        ):
            result["audio_time_offset"] = max(-2.0, min(2.0, audio_offset))
            applied["audio_sync"] = True
        if (
            not overridden("sync_drift_seconds_per_hour")
            and confidence_allows("audio_sync", drift_confidence, 0.72)
            and abs(drift) > 1e-5
        ):
            result["sync_drift_seconds_per_hour"] = max(-2.0, min(2.0, drift))
            applied["sync_drift"] = True

    result["native_calibration_applied"] = applied
    result["native_calibration_output_policy"] = {
        name: output_enabled(name)
        for name in (
            "stereo_disparity", "stereo_roll", "eye_scale_shear", "stitch_vectors",
            "crop", "horizon", "orientation_180", "eye_swap", "lens_fov",
            "autofocus", "audio_sync",
        )
    }
    result["stereo_alignment_applied"] = applied["vertical_alignment"] or applied["stereo_roll"]
    result["stereo_roll_applied"] = applied["stereo_roll"]
    result["stereo_scale_applied"] = applied["eye_scale_shear"]
    result["stitch_applied"] = applied["stitch"]
    result["auto_crop_applied"] = applied["crop"]
    result["auto_crop_confidence"] = crop_confidence
    result["horizon_leveling_applied"] = applied["horizon"]
    result["horizon_confidence"] = horizon_confidence
    result["autofocus_applied"] = applied["autofocus"]
    result["audio_volume_applied"] = applied["audio_volume"]
    result["audio_sync_applied"] = applied["audio_sync"] or applied["sync_drift"]
    result["stereo_alignment_policy"] = (
        "auto stereo disparity + roll" if applied["stereo_roll"] and applied["vertical_alignment"]
        else "auto stereo disparity" if applied["vertical_alignment"]
        else "manual override" if overridden("alignment_rotation", "alignment_position")
        else "diagnostic only"
    )
    return result


def probe_metadata(db, cfg, row) -> dict[str, object] | None:
    """Probe and visually inspect layout once, then cache the result."""
    existing = dict(row.get("metadata") or {})
    if existing and int(existing.get("visual_calibration_version") or 0) >= VISUAL_CALIBRATION_VERSION:
        return existing
    try:
        metadata = existing or probe(Path(row["path"]), cfg.paths.ffprobe, float(getattr(cfg.analysis, "ffprobe_timeout_seconds", 30.0)))
        # Multi-frame visual verification is independent from filename
        # inference and stores stereo-alignment/lens/orientation evidence even
        # when confidence is too low to auto-apply a change.
        detect_layout_from_video(
            Path(row["path"]), cfg.paths.ffmpeg, metadata, cfg.analysis
        )
        db.exec(
            "UPDATE videos SET metadata_json=?, error=NULL WHERE id=?",
            (json.dumps(metadata), row["id"]),
        )
        return metadata
    except Exception:
        return None


def thumbnail_destination(cfg, video_id: str) -> Path:
    return cfg.thumbs_dir / f"{video_id}-preview2d-v{THUMBNAIL_LAYOUT_VERSION}.jpg"


def thumbnail_is_current(row: dict[str, object]) -> bool:
    value = str(row.get("thumb_path") or "")
    if not value:
        return False
    path = Path(value)
    return path.exists() and path.name.endswith(f"-preview2d-v{THUMBNAIL_LAYOUT_VERSION}.jpg")


def thumbnail_browse_metadata(row: dict[str, object], metadata: dict[str, object]) -> dict[str, object]:
    """Resolve thumbnail layout, retaining the proven VR-first last resort.

    HereTune first uses filename + visual detection + explicit override. Only if all
    of those still say mono and the filename is not explicitly 2D do we retain
    the conservative SBS browse fallback used for this VR-first library.
    """
    browse = effective_metadata(row, metadata)
    filename = Path(str(row["path"])).name.casefold()
    explicit_flat = any(token in filename for token in (
        "_mono", "-mono", ".mono", " mono",
        "_2d", "-2d", ".2d", " 2d",
        "_flat", "-flat", ".flat", " flat",
        "_perspective", "-perspective", ".perspective", " perspective",
    ))
    if browse.get("stereo") == "mono" and not explicit_flat and not row.get("layout_override"):
        browse["stereo"] = "sbs"
        browse["thumbnail_stereo_inferred"] = True
        browse["thumbnail_stereo_reason"] = "untagged-vr-default-sbs"
    return browse


def ensure_thumbnail(db, cfg, row, *, force: bool = False) -> str | None:
    if not force and thumbnail_is_current(row):
        return str(row["thumb_path"])
    metadata = row.get("metadata") or probe_metadata(db, cfg, row)
    if not metadata:
        return str(row.get("thumb_path") or "") or None
    browse_metadata = thumbnail_browse_metadata(row, metadata)
    destination = thumbnail_destination(cfg, str(row["id"]))
    try:
        thumbnail(
            Path(row["path"]), destination, cfg.paths.ffmpeg,
            float(metadata.get("duration") or 0.0), browse_metadata,
        )
        db.exec("UPDATE videos SET thumb_path=? WHERE id=?", (str(destination), row["id"]))
        return str(destination)
    except Exception:
        return str(row.get("thumb_path") or "") or None


def process(db, cfg, row, progress=None, worker_plan_provider=None) -> None:
    video_id = row["id"]
    title = str(row.get("title") or video_id)
    path_text = str(row.get("path") or "")
    runtime = {"stage": "starting", "completed": 0, "total": 0, "elapsed_seconds": 0.0}
    diagnostic_counts: dict[str, int] = {}
    # Stage callbacks originate inside the analyzer and used to omit timing.
    # Because those precise stage events are the last progress event published,
    # they replaced the generic event that did contain ETA/elapsed values. Keep
    # clocks here at the single progress-report boundary so every stage is
    # consistently enriched before it reaches the API/UI.
    analysis_clock_started: float | None = None
    stage_clock_name = "starting"
    stage_clock_started = time.monotonic()
    run_id = db.start_analysis_run(str(video_id), ANALYSIS_VERSION)
    cache = AnalysisCache(
        cfg.analysis_cache_dir,
        f"{row.get('content_fingerprint') or path_text}|{row.get('size') or 0}|{row.get('mtime_ns') or 0}",
        enabled=bool(getattr(cfg.analysis, "analysis_cache_enabled", True)),
        max_gb=float(getattr(cfg.analysis, "analysis_cache_max_gb", 20.0)),
    )

    def report(stage: str, **extra) -> None:
        nonlocal stage_clock_name, stage_clock_started
        now_mono = time.monotonic()
        if stage != stage_clock_name:
            stage_clock_name = stage
            stage_clock_started = now_mono

        enriched = dict(extra)
        if analysis_clock_started is not None and "elapsed_seconds" not in enriched:
            enriched["elapsed_seconds"] = max(0.0, now_mono - analysis_clock_started)

        # Count-based stages (scene scan, boundary refinement, sampling, etc.)
        # can always provide an ETA once at least one unit has completed. Use the
        # stage-local rate so a long earlier stage does not distort the current
        # stage's remaining-time estimate. Explicit ETA values still win.
        if "eta_seconds" not in enriched:
            try:
                completed = int(enriched.get("completed") or 0)
                total = int(enriched.get("total") or 0)
            except (TypeError, ValueError):
                completed = total = 0
            if total > 0 and completed >= total:
                enriched["eta_seconds"] = 0.0
            elif total > 0 and completed > 0:
                stage_elapsed = max(0.0, now_mono - stage_clock_started)
                enriched["eta_seconds"] = (stage_elapsed / completed) * max(0, total - completed)
            elif total > 0:
                enriched["eta_seconds"] = None

        runtime["stage"] = stage
        runtime.update(enriched)
        try:
            db.update_analysis_run(run_id, stage=stage, planned=int(enriched.get("total") or 0) if "total" in enriched else None, details={"completed": int(enriched.get("completed") or 0), **{k: v for k, v in enriched.items() if k not in {"completed", "total"}}})
        except Exception:
            pass
        if progress:
            progress({"video_id": video_id, "id": video_id, "title": title, "stage": stage, **enriched})

    def diagnostic(payload: dict[str, object]) -> None:
        stage = str(payload.get("stage") or runtime.get("stage") or "analysis")
        timestamp = payload.get("timestamp")
        severity = str(payload.get("severity") or "warning").upper()
        message = str(payload.get("message") or payload.get("error") or "analysis diagnostic")
        kind = str(payload.get("kind") or "")
        ts_text = ""
        try:
            if timestamp is not None:
                ts_text = f" @ {float(timestamp):.2f}s"
        except (TypeError, ValueError):
            pass
        signature = f"{stage}|{kind}|{message}"
        count = diagnostic_counts.get(signature, 0) + 1
        diagnostic_counts[signature] = count
        line = f"[HereTune Analysis] {severity} {title} · {stage}{ts_text}{f' · {kind}' if kind else ''} · {message}"
        # Persist every diagnostic. Console-print the first few identical failures
        # and periodic repeats so a systemic decoder failure is visible without
        # producing thousands of identical lines on a long movie.
        stamp = now()
        detail = str(payload.get("traceback") or "")
        _append_analysis_log(cfg, f"[{stamp}] {line}\n{detail}" if detail else f"[{stamp}] {line}")
        if count <= 3 or count % 10 == 0:
            repeat = f" (repeat #{count})" if count > 1 else ""
            _console_analysis_line(line + repeat, error=severity in {"ERROR", "FATAL"})

    db.exec(
        "UPDATE videos SET status='analyzing', error=NULL, manual_priority=0, priority_requested_at=NULL, priority_rank=NULL WHERE id=?",
        (video_id,),
    )
    _console_analysis_line(f"[HereTune Analysis] START {title} · {path_text}")
    scene_grade_limit = int(getattr(cfg.analysis, "scene_max_keyframes", 0))
    scene_grade_text = "unlimited" if scene_grade_limit == 0 else str(scene_grade_limit)
    max_full = int(getattr(cfg.analysis, "max_samples", 360))
    max_full_text = "unlimited" if max_full == 0 else str(max_full)
    _console_analysis_line(
        f"[HereTune Analysis] PLAN {title} · full {int(getattr(cfg.analysis, 'analysis_width', 960))}px "
        f"· base ≥{int(getattr(cfg.analysis, 'samples', 48))} · full cadence ≤{float(getattr(cfg.analysis, 'sample_interval_seconds', 30.0)):.1f}s "
        f"· max full {max_full_text} "
        f"· scene probes ~{float(getattr(cfg.analysis, 'scene_probe_interval_seconds', 10.0)):.1f}s/{int(getattr(cfg.analysis, 'scene_probe_width', 480))}px "
        f"· refine {float(getattr(cfg.analysis, 'scene_boundary_refine_seconds', 2.0)):.1f}s "
        f"· {int(getattr(cfg.analysis, 'scene_full_samples_per_segment', 9))} full/segment "
        f"· max grades {scene_grade_text} "
        f"· workers GPU {int(getattr(cfg.analysis, 'gpu_decode_workers', 4))}/CPU {int(getattr(cfg.analysis, 'cpu_analysis_workers', 8))}"
    )
    report("metadata")
    try:
        metadata = row.get("metadata") or probe_metadata(db, cfg, row)
        if not metadata:
            metadata = probe(Path(row["path"]), cfg.paths.ffprobe, float(getattr(cfg.analysis, "ffprobe_timeout_seconds", 30.0)))
            detect_layout_from_video(
                Path(row["path"]), cfg.paths.ffmpeg, metadata, cfg.analysis
            )
        row = dict(row)
        row["metadata"] = metadata

        # A readiness job is a per-video bundle: browse thumbnail first, then
        # analysis/HSP generation for that same video.
        thumb_path = str(row.get("thumb_path") or "") or None
        if not thumbnail_is_current(row):
            report("thumbnail", completed=0, total=1, elapsed_seconds=0.0, eta_seconds=None)
            thumbnail_started = time.monotonic()
            thumb_path = ensure_thumbnail(db, cfg, row, force=True) or thumb_path
            report(
                "thumbnail", completed=1, total=1,
                elapsed_seconds=max(0.0, time.monotonic() - thumbnail_started),
                eta_seconds=0.0,
            )
            row["thumb_path"] = thumb_path

        analysis_started = time.monotonic()
        analysis_clock_started = analysis_started
        report("analysis", completed=0, total=0, elapsed_seconds=0.0, eta_seconds=None)

        def on_sample(completed: int, total: int) -> None:
            elapsed = max(0.0, time.monotonic() - analysis_started)
            eta = (elapsed / completed * max(0, total - completed)) if completed > 0 else None
            stage = str(runtime.get("stage") or "frame-analysis")
            if stage in {"analysis", "starting", "metadata", "thumbnail", "scene-probe", "boundary-refinement", "full-sampling"}:
                stage = "frame-analysis"
            # Keep the long-standing generic progress event for API consumers,
            # then publish the precise pipeline stage. The detailed event is last
            # so the dashboard never regresses to an opaque "analysis" label.
            if progress:
                progress({
                    "video_id": video_id, "id": video_id, "title": title,
                    "stage": "analysis", "completed": completed, "total": total,
                    "elapsed_seconds": elapsed, "eta_seconds": eta,
                })
            report(stage, completed=completed, total=total, elapsed_seconds=elapsed, eta_seconds=eta)

        def on_stage(stage: str, details: dict[str, object]) -> None:
            report(stage, **details)

        try:
            analyze_kwargs = {"on_sample": on_sample}
            signature = pyinspect.signature(analyze).parameters
            if "on_diagnostic" in signature:
                analyze_kwargs["on_diagnostic"] = diagnostic
            if (
                worker_plan_provider
                and bool(getattr(cfg.analysis, "adaptive_workers", True))
                and "worker_plan_provider" in signature
            ):
                analyze_kwargs["worker_plan_provider"] = worker_plan_provider
            if "analysis_cache" in signature:
                analyze_kwargs["analysis_cache"] = cache
            if "on_stage" in signature:
                analyze_kwargs["on_stage"] = on_stage
            correction = analyze(
                Path(row["path"]), metadata, cfg.paths.ffmpeg, cfg.analysis,
                **analyze_kwargs,
            )
            correction.analysis_seconds = max(0.0, time.monotonic() - analysis_started)
        except AnalysisError as exc:
            if bool(metadata.get("is_hdr")) or int(metadata.get("bit_depth") or 8) > 8:
                db.exec(
                    """UPDATE videos SET status='skipped', metadata_json=?, analysis_json=?,
                       thumb_path=?, last_analyzed=?, error=?, profile_mode='auto' WHERE id=?""",
                    (json.dumps(metadata), json.dumps({"reason": str(exc)}), thumb_path, now(), str(exc)[:4000], video_id),
                )
                _console_analysis_line(f"[HereTune Analysis] SKIP {title} · {exc}")
                db.finish_analysis_run(run_id, status="skipped", stage=str(runtime.get("stage") or "analysis"), duration=max(0.0, time.monotonic() - analysis_started), error=str(exc))
                cache.close()
                return
            raise

        report("finalizing")
        # Store the complete analyzer result, then serialize Auto through the
        # final HSP invariant guard. Global Offset is already zero here because
        # the Auto tone solver no longer estimates it at all.
        row["analysis"] = correction.dict()
        hsp_correction = apply_auto_image_policy(correction, cfg, row.get("hsp_policy_override"))
        blob = build(
            video_id, row["title"], float(metadata["duration"]), hsp_correction,
            metadata=native_calibration_metadata(row, metadata, cfg, hsp_correction),
            source_filename=Path(str(row["path"])).name,
            hsp_version=int(cfg.hsp.generated_version),
        )
        parsed = inspect_hsp(blob)
        db.exec(
            """UPDATE videos SET status='ready', metadata_json=?, analysis_json=?,
               auto_hsp=?, auto_hsp_version=?, thumb_path=?, last_analyzed=?, error=NULL,
               profile_mode='auto' WHERE id=?""",
            (json.dumps(metadata), json.dumps(correction.dict()), blob, parsed.version, thumb_path, now(), video_id),
        )
        report(
            "ready", completed=int(correction.sampled_frames), total=int(correction.target_samples),
            elapsed_seconds=float(correction.analysis_seconds), eta_seconds=0.0,
        )
        _console_analysis_line(
            f"[HereTune Analysis] READY {title} · {int(correction.sampled_frames)} usable frames · "
            f"{float(correction.analysis_seconds):.1f}s"
        )
        db.finish_analysis_run(
            run_id, status="ready", stage="ready", used=int(correction.sampled_frames),
            rejected=int(correction.rejected_total), duration=float(correction.analysis_seconds),
            details={"scene_grades": len(correction.scene_keyframes), "cache_resume_samples": int(getattr(correction, "cache_resume_samples", 0))},
        )
        cache.close()
    except Exception as exc:
        # A failed video MUST NOT stop the queue, but it also must never fail
        # silently. Preserve a concise reason in SQLite/UI and write the full
        # traceback to a persistent per-analysis log while printing it to the
        # server console immediately.
        stage = str(runtime.get("stage") or "analysis")
        completed = int(runtime.get("completed") or 0)
        total = int(runtime.get("total") or 0)
        summary = f"{type(exc).__name__}: {exc}"
        progress_text = f" · frame {completed}/{total}" if total else ""
        persisted = f"{summary} · stage={stage}{progress_text}"
        db.exec(
            "UPDATE videos SET status='error', error=? WHERE id=?",
            (persisted[:4000], video_id),
        )
        formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        stamp = now()
        block = (
            f"\n[{stamp}] VIDEO ANALYSIS FAILED\n"
            f"Title: {title}\nVideo ID: {video_id}\nPath: {path_text}\n"
            f"Stage: {stage}\nProgress: {completed}/{total}\n"
            f"Error: {summary}\n\n{formatted.rstrip()}\n"
        )
        _append_analysis_log(cfg, block)
        _console_analysis_line(
            f"[HereTune Analysis] FAILED {title} · stage {stage}{progress_text} · {summary}",
            error=True,
        )
        # Print the traceback too: when the user is watching the console they
        # should not have to go hunting through SQLite to discover the cause.
        _console_analysis_line(formatted.rstrip(), error=True)
        _console_analysis_line(
            f"[HereTune Analysis] Continuing with the next queued video. Full failure log: {_analysis_log_path(cfg)}",
            error=True,
        )
        db.finish_analysis_run(
            run_id, status="error", stage=stage, used=completed, duration=float(runtime.get("elapsed_seconds") or 0.0),
            error=persisted, details={"diagnostics": sum(diagnostic_counts.values())},
        )
        cache.close()
        report("error", completed=completed, total=total, error=persisted, eta_seconds=0.0)
