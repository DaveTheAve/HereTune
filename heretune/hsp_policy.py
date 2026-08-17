from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict
from typing import Any

# These switches control what Auto is allowed to serialize. Analysis evidence is
# intentionally retained even when a control is globally disabled, so changing
# policy or applying a per-video override never requires another source decode.
AUTO_HSP_POLICY_VERSION = 3
AUTO_IMAGE_CONTROLS: "OrderedDict[str, dict[str, Any]]" = OrderedDict([
    ("sharpen", {"label": "Sharpen", "neutral": 2.0, "default": True}),
    ("exposure", {"label": "Exposure", "neutral": 0.0, "default": True}),
    ("temperature", {"label": "Temperature", "neutral": 6500.0, "default": True}),
    ("tint", {"label": "Tint", "neutral": 0.0, "default": True}),
    ("rgb_gain", {"label": "RGB gain", "neutral": (1.0, 1.0, 1.0), "default": True}),
    ("global_offset", {
        "label": "Global offset",
        "neutral": 0.0,
        "default": False,
        "warning": "Unsafe by default: headset testing linked positive Global Offset to black-floor lift and haze.",
    }),
    ("rgb_offset", {"label": "RGB offset", "neutral": (0.0, 0.0, 0.0), "default": True}),
    ("global_gamma", {"label": "Global gamma", "neutral": 1.0, "default": True}),
    ("rgb_gamma", {"label": "RGB gamma", "neutral": (1.0, 1.0, 1.0), "default": True}),
    ("global_contrast", {"label": "Global contrast", "neutral": 1.0, "default": True}),
    ("rgb_contrast", {"label": "RGB contrast", "neutral": (1.0, 1.0, 1.0), "default": True}),
    ("global_saturation", {"label": "Global saturation", "neutral": 1.0, "default": True}),
    ("rgb_saturation", {"label": "RGB saturation", "neutral": (1.0, 1.0, 1.0), "default": True}),
    # Headset A/B testing identified these as unsafe defaults for this library.
    # They remain analyzed and can be Force On for an individual video.
    ("shadows", {"label": "Shadows", "neutral": 0.0, "default": False}),
    ("midtones", {"label": "Midtones", "neutral": 1.0, "default": True}),
    ("highlights", {"label": "Highlights", "neutral": 1.0, "default": True}),
    ("scene_keyframes", {"label": "Scene keyframes", "neutral": None, "default": True}),
])

# Native geometry/player calibration output controls are deliberately separate
# from analysis evidence. They support the same Inherit / Force On / Force Off
# per-video policy as Image controls. Preserve the public v0.13.0 defaults for
# confidence-gated vertical stereo alignment and encoded-border crop; newly
# exposed native-calibration outputs remain opt-in. Explicit numeric layout
# overrides still win over automatic policy.
AUTO_NATIVE_CONTROLS: "OrderedDict[str, dict[str, Any]]" = OrderedDict([
    ("stereo_disparity", {"label": "Stereo disparity", "default": True}),
    ("stereo_roll", {"label": "Stereo roll", "default": False}),
    ("eye_scale_shear", {"label": "Eye scale / shear", "default": False}),
    ("stitch_vectors", {"label": "Stitch vectors", "default": False}),
    ("crop", {"label": "Native border crop", "default": True}),
    ("horizon", {"label": "Horizon leveling", "default": False}),
    ("orientation_180", {"label": "180° orientation correction", "default": False}),
    ("eye_swap", {"label": "Eye swap", "default": False}),
    ("lens_fov", {"label": "Generic lens/FOV suggestion", "default": False}),
    ("autofocus", {"label": "Autofocus range", "default": False}),
    ("audio_sync", {"label": "Audio timing / drift", "default": False}),
])

_OVERRIDE_VALUES = {"inherit", "on", "off"}


def _cfg_enabled(cfg, control: str) -> bool:
    hsp = getattr(cfg, "hsp", None)
    spec = AUTO_IMAGE_CONTROLS[control]
    if hsp is None:
        return bool(spec["default"])
    return bool(getattr(hsp, f"auto_{control}", spec["default"]))


def _native_cfg_enabled(cfg, control: str) -> bool:
    hsp = getattr(cfg, "hsp", None)
    spec = AUTO_NATIVE_CONTROLS[control]
    if hsp is None:
        return bool(spec["default"])
    return bool(getattr(hsp, f"auto_{control}", spec["default"]))


def global_auto_native_policy(cfg=None) -> dict[str, bool]:
    return {name: _native_cfg_enabled(cfg, name) for name in AUTO_NATIVE_CONTROLS}


def normalize_native_policy_overrides(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, raw in value.items():
        name = str(key or "").strip()
        state = str(raw or "inherit").strip().lower()
        if name not in AUTO_NATIVE_CONTROLS or state not in _OVERRIDE_VALUES:
            continue
        if state != "inherit":
            result[name] = state
    return result


def effective_auto_native_policy(cfg=None, overrides: object = None) -> dict[str, bool]:
    result = global_auto_native_policy(cfg)
    for name, state in normalize_native_policy_overrides(overrides).items():
        result[name] = state == "on"
    return result


def native_policy_payload(cfg=None, overrides: object = None) -> dict[str, object]:
    normalized = normalize_native_policy_overrides(overrides)
    effective = effective_auto_native_policy(cfg, normalized)
    return {
        "version": AUTO_HSP_POLICY_VERSION,
        "controls": [
            {
                "name": name,
                "label": str(spec["label"]),
                "global": bool(_native_cfg_enabled(cfg, name)),
                "override": normalized.get(name, "inherit"),
                "effective": bool(effective[name]),
                # Backward-compatible alias for the first v0.13.1 drawer.
                "enabled": bool(effective[name]),
            }
            for name, spec in AUTO_NATIVE_CONTROLS.items()
        ],
    }


def normalize_policy_overrides(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, raw in value.items():
        name = str(key or "").strip()
        state = str(raw or "inherit").strip().lower()
        if name not in AUTO_IMAGE_CONTROLS or state not in _OVERRIDE_VALUES:
            continue
        if state != "inherit":
            result[name] = state
    return result


def global_auto_image_policy(cfg=None) -> dict[str, bool]:
    return {name: _cfg_enabled(cfg, name) for name in AUTO_IMAGE_CONTROLS}


def effective_auto_image_policy(cfg=None, overrides: object = None) -> dict[str, bool]:
    result = global_auto_image_policy(cfg)
    for name, state in normalize_policy_overrides(overrides).items():
        result[name] = state == "on"
    return result


def policy_payload(cfg=None, overrides: object = None) -> dict[str, object]:
    normalized = normalize_policy_overrides(overrides)
    effective = effective_auto_image_policy(cfg, normalized)
    return {
        "version": AUTO_HSP_POLICY_VERSION,
        "controls": [
            {
                "name": name,
                "label": str(spec["label"]),
                "global": bool(_cfg_enabled(cfg, name)),
                "override": normalized.get(name, "inherit"),
                "effective": bool(effective[name]),
                "warning": str(spec.get("warning") or ""),
            }
            for name, spec in AUTO_IMAGE_CONTROLS.items()
        ],
    }


def policy_signature(cfg=None) -> str:
    payload = {
        "version": AUTO_HSP_POLICY_VERSION,
        "image_controls": global_auto_image_policy(cfg),
        "native_controls": global_auto_native_policy(cfg),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]


def _vec4(raw: object, neutral: float) -> list[float]:
    try:
        values = [float(value) for value in raw]  # type: ignore[arg-type]
    except (TypeError, ValueError):
        values = [neutral, neutral, neutral, neutral]
    if len(values) != 4:
        values = [neutral, neutral, neutral, neutral]
    return values


def suggested_global_offset(correction) -> float:
    """Derive a bounded output-only Offset suggestion from saved evidence.

    Global Offset is not an analyzer degree of freedom and never influences
    Gamma, Midtones, Contrast, scene segmentation, or any other tone decision.
    This deliberately separate estimate exists only so an explicit global or
    per-video Force On can be tested without re-decoding the movie. The shipped
    default remains OFF.
    """
    try:
        p05 = float(getattr(correction, "luma_p05", 0.0) or 0.0)
        p95 = float(getattr(correction, "luma_p95", 0.0) or 0.0)
        exposure = float(getattr(correction, "exposure_bias", 0.0) or 0.0)
        contrast = float(getattr(correction, "contrast", 1.0) or 1.0)
        confidence = float(getattr(correction, "tone_confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if p95 <= p05 or confidence <= 0.0:
        return 0.0
    scale = 2.0 ** exposure
    ep05 = max(0.0, min(1.0, p05 * scale))
    ep95 = max(0.0, min(1.0, p95 * scale))
    tonal_mid = (ep05 + ep95) * 0.5
    raw = max(-0.027, min(0.027, -contrast * (tonal_mid - 0.50)))
    return max(-0.027, min(0.027, raw * 0.48 * max(0.0, min(1.0, confidence))))


def _apply_native_policy(
    native: dict[str, object],
    enabled: dict[str, bool],
    *,
    global_offset: float = 0.0,
) -> dict[str, object]:
    value = copy.deepcopy(native)
    if not enabled["sharpen"]:
        value["sharpen"] = 2.0
    if not enabled["exposure"]:
        value["exposure_bias"] = 0.0
    if not enabled["temperature"]:
        value["white_temp"] = 6500.0
    if not enabled["tint"]:
        value["white_tint"] = 0.0

    gain = _vec4(value.get("gain", (1.0, 1.0, 1.0, 1.0)), 1.0)
    if not enabled["rgb_gain"]:
        gain[:3] = [1.0, 1.0, 1.0]
    gain[3] = 1.0
    value["gain"] = tuple(gain)

    offset = _vec4(value.get("offset", (0.0, 0.0, 0.0, 0.0)), 0.0)
    if not enabled["rgb_offset"]:
        offset[:3] = [0.0, 0.0, 0.0]
    # Output-only: this does not and cannot alter the analyzer's tone solve.
    offset[3] = (
        max(-0.027, min(0.027, float(global_offset)))
        if enabled["global_offset"]
        else 0.0
    )
    value["offset"] = tuple(offset)

    gamma = _vec4(value.get("gamma", (1.0, 1.0, 1.0, 1.0)), 1.0)
    if not enabled["rgb_gamma"]:
        gamma[:3] = [1.0, 1.0, 1.0]
    if not enabled["global_gamma"]:
        gamma[3] = 1.0
    value["gamma"] = tuple(gamma)

    contrast = _vec4(value.get("contrast", (1.0, 1.0, 1.0, 1.0)), 1.0)
    if not enabled["rgb_contrast"]:
        contrast[:3] = [1.0, 1.0, 1.0]
    if not enabled["global_contrast"]:
        contrast[3] = 1.0
    value["contrast"] = tuple(contrast)

    saturation = _vec4(value.get("saturation", (1.0, 1.0, 1.0, 1.0)), 1.0)
    if not enabled["rgb_saturation"]:
        saturation[:3] = [1.0, 1.0, 1.0]
    if not enabled["global_saturation"]:
        saturation[3] = 1.0
    value["saturation"] = tuple(saturation)

    if not enabled["shadows"]:
        value["shadows"] = 0.0
    if not enabled["midtones"]:
        value["midtones"] = 1.0
    if not enabled["highlights"]:
        value["highlights"] = 1.0
    return value


def apply_auto_image_policy(correction, cfg=None, overrides: object = None):
    """Apply global + per-video Auto-HSP serialization policy.

    The analyzer remains offset-free and untouched. This function only decides
    which saved measurements reach the native HSP, making policy changes cheap,
    reversible, and safe to apply without another source decode.
    """
    result = copy.deepcopy(correction)
    enabled = effective_auto_image_policy(cfg, overrides)

    if not enabled["sharpen"]:
        result.sharpen = 2.0
    if not enabled["exposure"]:
        result.exposure_bias = 0.0
    if not enabled["temperature"]:
        result.white_temp = 6500.0
    if not enabled["tint"]:
        result.white_tint = 0.0
    if not enabled["rgb_gain"]:
        result.r_gain = result.g_gain = result.b_gain = 1.0

    offset_suggestion = suggested_global_offset(result)
    result.brightness = offset_suggestion if enabled["global_offset"] else 0.0
    if not enabled["rgb_offset"]:
        result.offset_r = result.offset_g = result.offset_b = 0.0

    if not enabled["global_gamma"]:
        result.gamma = 1.0
    if not enabled["rgb_gamma"]:
        result.gamma_r = result.gamma_g = result.gamma_b = 1.0
    if not enabled["global_contrast"]:
        result.contrast = 1.0
    if not enabled["rgb_contrast"]:
        result.contrast_r = result.contrast_g = result.contrast_b = 1.0
    if not enabled["global_saturation"]:
        result.saturation = 1.0
    if not enabled["rgb_saturation"]:
        result.saturation_r = result.saturation_g = result.saturation_b = 1.0
    if not enabled["shadows"]:
        result.shadows = 0.0
    if not enabled["midtones"]:
        result.midtones = 1.0
    if not enabled["highlights"]:
        result.highlights = 1.0

    if not enabled["scene_keyframes"]:
        result.scene_keyframes = []
        return result

    scenes: list[dict[str, object]] = []
    for item in list(getattr(result, "scene_keyframes", None) or []):
        if not isinstance(item, dict):
            continue
        cloned = copy.deepcopy(item)
        native = cloned.get("native")
        if isinstance(native, dict):
            scene_offset = cloned.get("suggested_global_offset", offset_suggestion)
            try:
                scene_offset = float(scene_offset)
            except (TypeError, ValueError):
                scene_offset = offset_suggestion
            cloned["native"] = _apply_native_policy(
                native,
                enabled,
                global_offset=scene_offset,
            )
        scenes.append(cloned)
    result.scene_keyframes = scenes
    return result
