from __future__ import annotations

import math
import re
from typing import Any

import numpy as np

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover - defensive for broken Windows installs
    cv2 = None

VRFORMAT_VERSION = 5
VISUAL_CALIBRATION_VERSION = 5

_FACE_CASCADE = None
_FACE_CASCADE_ATTEMPTED = False


def clamp(value: float, minimum: float, maximum: float) -> float:
    """Return ``value`` bounded to a finite numeric interval."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = minimum
    if not math.isfinite(numeric):
        numeric = minimum
    return max(float(minimum), min(float(maximum), numeric))


def _has(text: str, *patterns: str) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _explicit_tokens(text: str) -> dict[str, bool]:
    return {
        "stereo": _has(
            text,
            r"(?:^|[_\-.])(?:tb|topbottom|top-bottom|ou|overunder|bt)(?:[_\-.]|$)",
            r"(?:fullsbs|full-sbs|fsbs|sbsf|lrf|sbs|hsbs|side-by-side|sidebyside)",
            r"(?:^|[_\-.])(?:lr|rl|3d)(?:[_\-.]|$)",
            r"(?:^|[_\-.])(?:mono|2d|flat)(?:[_\-.]|$)",
        ),
        "projection": _has(
            text,
            r"mkx22(?:0)?|mkx200|vrca220|rf52|fisheye|f135|f180|180f|vr180|180vr",
            r"360x180|(?:^|[_\-.])360(?:[_\-.]|$)",
            r"180x180|(?:^|[_\-.])180(?:[_\-.]|$)",
            r"(?:^|[_\-.])(?:flat|2d|perspective)(?:[_\-.]|$)",
            r"equiangularcubemap|(?:^|[_\-.])eac(?:[_\-.]|$)|cubemap",
        ),
    }


def detect(name: str, width: int = 0, height: int = 0) -> dict[str, object]:
    """Infer HereSphere projection metadata from stable filename conventions.

    Filename recognition remains one evidence source, but it no longer has
    to carry the entire burden: ``detect_visual_layout`` can add independent
    frame-based evidence and the dashboard can store explicit per-video
    overrides. Stereo packing and projection remain separate concepts.
    """
    text = name.casefold().replace(" ", "_")
    explicit = _explicit_tokens(text)
    stereo = "mono"
    projection = "perspective"
    fov = 0.0
    lens = "Linear"
    eye_swapped = False
    full_side_by_side = False

    if _has(text, r"(?:^|[_\-.])(?:tb|topbottom|top-bottom|ou|overunder)(?:[_\-.]|$)"):
        stereo = "tb"
    elif _has(text, r"(?:^|[_\-.])bt(?:[_\-.]|$)"):
        stereo = "tb"
        eye_swapped = True
    elif _has(text, r"(?:fullsbs|full-sbs|fsbs|sbsf|lrf)"):
        stereo = "sbs"
        full_side_by_side = True
    elif _has(text, r"(?:sbs|hsbs|side-by-side|sidebyside|(?:^|[_\-.])lr(?:[_\-.]|$)|(?:^|[_\-.])3d(?:[_\-.]|$))"):
        stereo = "sbs"
    elif _has(text, r"(?:^|[_\-.])rl(?:[_\-.]|$)"):
        stereo = "sbs"
        eye_swapped = True

    lens_map = (
        ("mkx220", "MKX220", 220.0),
        ("mkx22", "MKX220", 220.0),
        ("mkx200", "MKX200", 200.0),
        ("vrca220", "VRCA220", 220.0),
        ("rf52", "Linear", 190.0),
        ("fisheye190", "Linear", 190.0),
        ("f135", "Linear", 135.0),
        ("f180", "Linear", 180.0),
        ("180f", "Linear", 180.0),
    )
    matched_lens = False
    for token, lens_name, lens_fov in lens_map:
        if token in text:
            projection, fov, lens = "fisheye", lens_fov, lens_name
            stereo = "sbs" if stereo == "mono" else stereo
            matched_lens = True
            break

    if not matched_lens:
        if "fisheye" in text:
            projection = "fisheye"
            fov = 180.0
        elif _has(text, r"360x180", r"(?:^|[_\-.])360(?:[_\-.]|$)"):
            projection, fov = "equirectangular360", 360.0
        elif _has(text, r"180x180", r"vr180", r"180vr", r"(?:^|[_\-.])180(?:[_\-.]|$)"):
            projection, fov = "equirectangular", 180.0
        elif _has(text, r"(?:^|[_\-.])(?:flat|2d|perspective|mono)(?:[_\-.]|$)"):
            projection, stereo, fov, lens = "perspective", "mono", 90.0, "Linear"

    if "equiangularcubemap" in text or _has(text, r"(?:^|[_\-.])eac(?:[_\-.]|$)"):
        projection = "equiangularCubemap"
    elif "cubemap" in text:
        projection = "cubemap"

    return {
        "projection": projection,
        "stereo": stereo,
        "fov": fov,
        "lens": lens,
        "isEyeSwapped": eye_swapped,
        "full_side_by_side": full_side_by_side,
        "source_aspect_ratio": (width / height) if width > 0 and height > 0 else 0.0,
        "vrformat_version": VRFORMAT_VERSION,
        "layout_filename_explicit_stereo": explicit["stereo"],
        "layout_filename_explicit_projection": explicit["projection"],
    }


def _resize_gray(gray: np.ndarray, out_h: int = 72, out_w: int = 72) -> np.ndarray:
    h, w = gray.shape[:2]
    ys = np.linspace(0, max(0, h - 1), out_h).astype(np.int32)
    xs = np.linspace(0, max(0, w - 1), out_w).astype(np.int32)
    return gray[np.ix_(ys, xs)].astype(np.float32)


def _similarity(a: np.ndarray, b: np.ndarray) -> float:
    a = _resize_gray(a)
    b = _resize_gray(b)
    # Gradient structure is more robust to exposure differences between eyes.
    ay, ax = np.gradient(a)
    by, bx = np.gradient(b)
    a = np.hypot(ax, ay).ravel()
    b = np.hypot(bx, by).ravel()
    a -= float(a.mean())
    b -= float(b.mean())
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom < 1e-6:
        return 0.0
    return max(-1.0, min(1.0, float(np.dot(a, b) / denom)))


def _fisheye_score(eye: np.ndarray) -> float:
    if eye.size == 0:
        return 0.0
    rgb = eye.astype(np.float32) / 255.0
    gray = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
    h, w = gray.shape
    if h < 16 or w < 16:
        return 0.0
    bh = max(2, int(h * 0.16))
    bw = max(2, int(w * 0.16))
    corners = np.concatenate([
        gray[:bh, :bw].ravel(), gray[:bh, -bw:].ravel(),
        gray[-bh:, :bw].ravel(), gray[-bh:, -bw:].ravel(),
    ])
    center = gray[int(h * .30):int(h * .70), int(w * .30):int(w * .70)]
    corner_dark = float(np.mean(corners < 0.055))
    center_lit = float(np.mean(center > 0.12)) if center.size else 0.0

    yy, xx = np.ogrid[:h, :w]
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    rx, ry = max(w * .48, 1.0), max(h * .48, 1.0)
    radius = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2
    outside = gray[radius > 1.0]
    ring_dark = float(np.mean(outside < 0.06)) if outside.size else 0.0
    return max(0.0, min(1.0, 0.45 * corner_dark + 0.35 * ring_dark + 0.20 * center_lit))


def _split_eyes(
    frame: np.ndarray,
    stereo: str,
    swapped: bool = False,
) -> tuple[np.ndarray, np.ndarray] | None:
    rgb = np.asarray(frame, dtype=np.uint8)
    if rgb.ndim != 3 or min(rgb.shape[:2]) < 16:
        return None
    h, w = rgb.shape[:2]
    stereo = str(stereo or "mono").lower()
    if stereo == "sbs":
        first, second = rgb[:, :w // 2], rgb[:, w - w // 2:]
    elif stereo == "tb":
        first, second = rgb[:h // 2, :], rgb[h - h // 2:, :]
    else:
        return None
    return (second, first) if swapped else (first, second)


def _gray_u8(rgb: np.ndarray) -> np.ndarray:
    values = np.asarray(rgb, dtype=np.uint8)
    if cv2 is not None:
        return cv2.cvtColor(values, cv2.COLOR_RGB2GRAY)
    gray = 0.2126 * values[..., 0] + 0.7152 * values[..., 1] + 0.0722 * values[..., 2]
    return np.clip(gray, 0, 255).astype(np.uint8)


def _resize_for_features(rgb: np.ndarray, max_width: int = 720) -> tuple[np.ndarray, float]:
    h, w = rgb.shape[:2]
    if w <= max_width:
        return rgb, 1.0
    scale = max_width / float(w)
    nh = max(16, int(round(h * scale)))
    if cv2 is not None:
        return cv2.resize(rgb, (max_width, nh), interpolation=cv2.INTER_AREA), scale
    ys = np.linspace(0, h - 1, nh).astype(np.int32)
    xs = np.linspace(0, w - 1, max_width).astype(np.int32)
    return rgb[ys[:, None], xs[None, :], :], scale


def _stereo_alignment_one(left: np.ndarray, right: np.ndarray) -> dict[str, object]:
    """Estimate relative eye rotation/vertical disparity using robust features.

    Horizontal disparity is retained as evidence because it is real depth. Only
    vertical disparity and roll become conservative alignment suggestions.
    """
    if cv2 is None:
        return {"confidence": 0.0, "reason": "OpenCV feature matching unavailable"}
    left_small, scale = _resize_for_features(left)
    right_small, _ = _resize_for_features(right)
    if left_small.shape[:2] != right_small.shape[:2]:
        right_small = cv2.resize(
            right_small,
            (left_small.shape[1], left_small.shape[0]),
            interpolation=cv2.INTER_AREA,
        )
    lg = cv2.equalizeHist(_gray_u8(left_small))
    rg = cv2.equalizeHist(_gray_u8(right_small))
    orb = cv2.ORB_create(nfeatures=1600, scaleFactor=1.2, nlevels=8, fastThreshold=10)
    kp_l, des_l = orb.detectAndCompute(lg, None)
    kp_r, des_r = orb.detectAndCompute(rg, None)
    if des_l is None or des_r is None or len(kp_l) < 20 or len(kp_r) < 20:
        return {"confidence": 0.0, "reason": "not enough stable stereo features"}

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    pairs = matcher.knnMatch(des_r, des_l, k=2)
    good = [first for first, second in pairs if first.distance < 0.76 * second.distance]
    if len(good) < 16:
        return {"confidence": 0.0, "reason": "not enough unambiguous eye matches", "matches": len(good)}

    pts_r = np.float32([kp_r[m.queryIdx].pt for m in good])
    pts_l = np.float32([kp_l[m.trainIdx].pt for m in good])
    matrix, inlier_mask = cv2.estimateAffinePartial2D(
        pts_r,
        pts_l,
        method=cv2.RANSAC,
        ransacReprojThreshold=2.75,
        maxIters=3000,
        confidence=0.995,
        refineIters=20,
    )
    if matrix is None or inlier_mask is None:
        return {"confidence": 0.0, "reason": "stereo affine fit failed", "matches": len(good)}
    inliers = inlier_mask.ravel().astype(bool)
    count = int(inliers.sum())
    if count < 12:
        return {"confidence": 0.0, "reason": "too few stereo inliers", "matches": len(good), "inliers": count}

    a, b = float(matrix[0, 0]), float(matrix[1, 0])
    rotation = math.degrees(math.atan2(b, a))
    scale_value = math.sqrt(a * a + b * b)
    disparities = pts_l[inliers] - pts_r[inliers]
    dx = float(np.median(disparities[:, 0])) / max(scale, 1e-6)
    dy = float(np.median(disparities[:, 1])) / max(scale, 1e-6)
    dx_sign = float(max(np.mean(disparities[:, 0] > 0.5), np.mean(disparities[:, 0] < -0.5)))
    dy_mad = float(np.median(np.abs(disparities[:, 1] - np.median(disparities[:, 1])))) / max(scale, 1e-6)

    predicted = cv2.transform(pts_r[inliers][None, :, :], matrix)[0]
    residual = float(np.median(np.linalg.norm(predicted - pts_l[inliers], axis=1))) / max(scale, 1e-6)

    # A second, full affine fit supplies non-uniform scale and shear evidence.
    # It is diagnostic unless explicitly enabled because HereSphere's Stitch
    # axes are native calibration values, not generic CSS transforms.
    full_matrix, full_mask = cv2.estimateAffine2D(
        pts_r,
        pts_l,
        method=cv2.RANSAC,
        ransacReprojThreshold=2.75,
        maxIters=3000,
        confidence=0.995,
        refineIters=20,
    )
    scale_x = scale_y = scale_value
    shear = 0.0
    full_residual = residual
    if full_matrix is not None and full_mask is not None:
        full_inliers = full_mask.ravel().astype(bool)
        if int(full_inliers.sum()) >= 12:
            linear = np.asarray(full_matrix[:, :2], dtype=np.float64)
            col_x = linear[:, 0]
            col_y = linear[:, 1]
            scale_x = float(np.linalg.norm(col_x))
            scale_y = float(np.linalg.norm(col_y))
            if scale_x > 1e-6 and scale_y > 1e-6:
                shear = float(np.dot(col_x / scale_x, col_y / scale_y))
            full_predicted = cv2.transform(pts_r[full_inliers][None, :, :], full_matrix)[0]
            full_residual = float(np.median(np.linalg.norm(
                full_predicted - pts_l[full_inliers], axis=1
            ))) / max(scale, 1e-6)

    inlier_ratio = count / max(len(good), 1)
    geometry_quality = (
        math.exp(-abs(scale_value - 1.0) / 0.018)
        * math.exp(-max(0.0, abs(rotation) - 2.5) / 1.5)
    )
    confidence = max(0.0, min(1.0,
        min(1.0, count / 80.0)
        * (0.35 + 0.65 * inlier_ratio)
        * math.exp(-residual / 3.5)
        * math.exp(-dy_mad / 6.0)
        * geometry_quality
    ))
    return {
        "confidence": confidence,
        "matches": len(good),
        "inliers": count,
        "inlier_ratio": inlier_ratio,
        "median_dx_px": dx,
        "median_dy_px": dy,
        "dy_mad_px": dy_mad,
        "rotation_deg": rotation,
        "scale": scale_value,
        "scale_x": scale_x,
        "scale_y": scale_y,
        "relative_scale_x": scale_x - 1.0,
        "relative_scale_y": scale_y - 1.0,
        "shear": shear,
        "full_affine_residual_px": full_residual,
        "horizontal_disparity_p10_px": float(np.quantile(disparities[:, 0], 0.10)) / max(scale, 1e-6),
        "horizontal_disparity_p90_px": float(np.quantile(disparities[:, 0], 0.90)) / max(scale, 1e-6),
        "absolute_disparity_p90_px": float(np.quantile(np.abs(disparities[:, 0]), 0.90)) / max(scale, 1e-6),
        "residual_px": residual,
        "geometry_quality": float(geometry_quality),
        "horizontal_sign_consistency": dx_sign,
        "eye_width": int(left.shape[1]),
        "eye_height": int(left.shape[0]),
    }


def _aggregate_stereo_alignment(
    frames: list[np.ndarray],
    stereo: str,
    swapped: bool,
    source_fov: float,
    max_degrees: float = 0.75,
) -> dict[str, object]:
    measurements: list[dict[str, object]] = []
    for frame in frames:
        pair = _split_eyes(frame, stereo, swapped)
        if pair is None:
            continue
        result = _stereo_alignment_one(*pair)
        if (
            float(result.get("confidence") or 0.0) > 0.05
            and 0.965 <= float(result.get("scale") or 1.0) <= 1.035
            and abs(float(result.get("rotation_deg") or 0.0)) <= 5.0
        ):
            measurements.append(result)
    if not measurements:
        return {"confidence": 0.0, "reason": "no trustworthy stereo feature matches", "frames": 0}

    weights = np.asarray([max(0.05, float(item["confidence"])) for item in measurements])
    dy_values = np.asarray([float(item["median_dy_px"]) for item in measurements])
    roll_values = np.asarray([float(item["rotation_deg"]) for item in measurements])
    dx_values = np.asarray([float(item["median_dx_px"]) for item in measurements])
    scale_x_values = np.asarray([float(item.get("scale_x") or 1.0) for item in measurements])
    scale_y_values = np.asarray([float(item.get("scale_y") or 1.0) for item in measurements])
    shear_values = np.asarray([float(item.get("shear") or 0.0) for item in measurements])
    disparity_p90_values = np.asarray([
        float(item.get("absolute_disparity_p90_px") or abs(float(item.get("median_dx_px") or 0.0)))
        for item in measurements
    ])
    eye_heights = np.asarray([max(1.0, float(item["eye_height"])) for item in measurements])
    dy = float(np.median(dy_values))
    roll = float(np.median(roll_values))
    dx = float(np.median(dx_values))
    dy_std = float(np.std(dy_values))
    roll_std = float(np.std(roll_values))
    scale_x = float(np.median(scale_x_values))
    scale_y = float(np.median(scale_y_values))
    shear = float(np.median(shear_values))
    scale_stability = float(max(np.std(scale_x_values), np.std(scale_y_values)))
    shear_stability = float(np.std(shear_values))
    stability = math.exp(-dy_std / 5.0) * math.exp(-roll_std / 0.8)
    support = min(1.0, len(measurements) / 3.0)
    confidence = max(0.0, min(1.0,
        float(np.average(weights)) * (0.42 + 0.58 * stability) * (0.60 + 0.40 * support)
    ))

    # Map vertical pixel disparity into a tiny angular pitch adjustment. We
    # split the measured correction because Alignment is relative stereo
    # calibration, not a request to rotate the entire world by the full error.
    vertical_fov = min(max(float(source_fov or 180.0), 60.0), 220.0)
    pitch = -dy / max(float(np.median(eye_heights)), 1.0) * vertical_fov * 0.5
    cap = max(0.0, float(max_degrees))
    pitch = max(-cap, min(cap, pitch))
    roll_diagnostic = max(-cap, min(cap, -roll * 0.5))

    roll_auto_applicable = bool(
        len(measurements) >= 2
        and 0.03 <= abs(roll) <= max(0.10, cap * 2.0)
        and roll_std <= max(0.18, abs(roll) * 0.45)
        and confidence >= 0.68
    )
    scale_auto_applicable = bool(
        len(measurements) >= 2
        and max(abs(scale_x - 1.0), abs(scale_y - 1.0)) >= 0.0025
        and max(abs(scale_x - 1.0), abs(scale_y - 1.0)) <= 0.030
        and scale_stability <= 0.008
        and abs(shear) <= 0.030
        and confidence >= 0.72
    )

    eye_width = max(1.0, float(np.median([
        float(item.get("eye_width") or 1.0) for item in measurements
    ])))
    eye_height = max(1.0, float(np.median(eye_heights)))
    # Experimental Stitch suggestions use normalized per-eye geometry.  They
    # are never auto-applied unless the dedicated opt-in setting is enabled.
    shift_x = max(-0.04, min(0.04, -dx / eye_width * 0.25))
    shift_y = max(-0.04, min(0.04, -dy / eye_height * 0.25))
    scale_trim_x = max(-0.03, min(0.03, 1.0 - scale_x))
    scale_trim_y = max(-0.03, min(0.03, 1.0 - scale_y))
    shear_trim = max(-0.025, min(0.025, -shear * 0.35))
    stitch_confidence = max(0.0, min(1.0,
        confidence
        * math.exp(-scale_stability / 0.012)
        * math.exp(-shear_stability / 0.018)
    ))

    meaningful_dy = np.abs(dy_values) >= 0.50
    if bool(np.any(meaningful_dy)):
        signs_y = np.sign(dy_values[meaningful_dy])
        vertical_sign_consistency = float(max(
            np.mean(signs_y > 0),
            np.mean(signs_y < 0),
        ))
    else:
        vertical_sign_consistency = 0.0

    # Applying horizontal disparity would flatten real stereo depth. Relative
    # roll can likewise be introduced by camera geometry or imperfect feature
    # matches, so it remains diagnostic until we have a native calibration set.
    # Only a stable vertical mismatch is eligible for automatic correction.
    auto_applicable = bool(
        len(measurements) >= 2
        and abs(dy) >= 0.75
        and vertical_sign_consistency >= 0.75
        and dy_std <= max(2.5, abs(dy) * 0.65)
        and confidence >= 0.50
        and abs(pitch) > 0.005
    )

    signs = [float(item.get("horizontal_sign_consistency") or 0.0) for item in measurements]
    eye_swap_suspected = dx < -1.25 and float(np.median(signs)) >= 0.72
    eye_swap_confidence = confidence * min(1.0, abs(dx) / 8.0) * float(np.median(signs))
    return {
        "confidence": confidence,
        "frames": len(measurements),
        "median_horizontal_disparity_px": dx,
        "median_vertical_disparity_px": dy,
        "median_roll_deg": roll,
        "median_scale_x": scale_x,
        "median_scale_y": scale_y,
        "median_shear": shear,
        "scale_stability": scale_stability,
        "shear_stability": shear_stability,
        "absolute_disparity_p90_px": float(np.median(disparity_p90_values)),
        "vertical_stability_px": dy_std,
        "vertical_sign_consistency": vertical_sign_consistency,
        "roll_stability_deg": roll_std,
        "suggested_rotation": (pitch, 0.0, 0.0),
        "diagnostic_roll_correction": roll_diagnostic,
        "suggested_roll_rotation": (0.0, 0.0, roll_diagnostic),
        "roll_auto_applicable": roll_auto_applicable,
        "scale_auto_applicable": scale_auto_applicable,
        "stitch_confidence": stitch_confidence,
        "suggested_stitch": {
            "shift": (shift_x, shift_y, -shift_x, -shift_y),
            "scale": (1.0, 1.0, 1.0 + scale_trim_x, 1.0 + scale_trim_y),
            "shear": (0.0, 0.0, shear_trim, -shear_trim),
            "flare": (0.0, 0.0, 0.0, 0.0),
            "slant": (0.0, 0.0, 0.0, 0.0),
        },
        "stitch_auto_applicable": bool(
            stitch_confidence >= 0.76
            and (scale_auto_applicable or abs(shift_y) >= 0.0015 or abs(shear_trim) >= 0.0015)
        ),
        "autofocus_recommendation": {
            "min_distance": float(max(2.0, min(10.0, 10.0 / (1.0 + float(np.median(disparity_p90_values)) / 18.0)))),
            "max_distance": float(max(300.0, min(1000.0, 1000.0 / (1.0 + max(0.0, abs(dx) - 1.0) / 35.0)))),
            "transition_speed": float(max(0.45, min(1.8, 1.0 + abs(dy) / 30.0))),
            "confidence": float(confidence * min(1.0, len(measurements) / 3.0)),
        },
        "suggested_position": (0.0, 6.5, 0.0),
        "auto_applicable": auto_applicable,
        "auto_policy": "stable vertical disparity only",
        "eye_swap_suspected": bool(eye_swap_suspected),
        "eye_swap_confidence": float(eye_swap_confidence),
        "measurements": measurements,
    }



def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    if values.size == 0:
        return 0.0
    order = np.argsort(values)
    ordered_values = values[order]
    ordered_weights = np.maximum(weights[order], 0.0)
    total = float(np.sum(ordered_weights))
    if total <= 1e-9:
        return float(np.median(ordered_values))
    threshold = total * 0.5
    index = int(np.searchsorted(np.cumsum(ordered_weights), threshold, side="left"))
    return float(ordered_values[min(index, len(ordered_values) - 1)])


def _horizon_evidence(
    frames: list[np.ndarray],
    stereo: str,
    swapped: bool,
    projection: str,
) -> dict[str, object]:
    """Estimate a small persistent camera-roll error from long horizontal lines.

    This is intentionally conservative. It ignores short edges, weights lines near
    the image center, requires agreement across frames, and reports a suggestion
    rather than silently rotating the world. Fisheye evidence is down-weighted
    because a curved lens projection can make straight scene structure misleading.
    """
    if cv2 is None:
        return {"confidence": 0.0, "reason": "OpenCV line analysis unavailable"}
    per_frame: list[tuple[float, float, int]] = []
    for frame in frames:
        pair = _split_eyes(frame, stereo, swapped)
        view = pair[0] if pair is not None else np.asarray(frame, dtype=np.uint8)
        if view.ndim != 3 or min(view.shape[:2]) < 64:
            continue
        view, _ = _resize_for_features(view, max_width=720)
        gray = _gray_u8(view)
        h, w = gray.shape[:2]
        # Avoid the most distorted top/bottom edges and keep central scene lines.
        y0, y1 = int(h * 0.12), max(int(h * 0.88), int(h * 0.12) + 1)
        roi = gray[y0:y1]
        roi = cv2.GaussianBlur(roi, (5, 5), 0.9)
        edges = cv2.Canny(roi, 45, 135, L2gradient=True)
        lines = cv2.HoughLinesP(
            edges,
            1,
            np.pi / 360.0,
            threshold=max(30, int(w * 0.065)),
            minLineLength=max(38, int(w * 0.12)),
            maxLineGap=max(8, int(w * 0.025)),
        )
        if lines is None:
            continue
        angles: list[float] = []
        weights: list[float] = []
        for raw in lines[:, 0, :]:
            x1, yy1, x2, yy2 = map(float, raw)
            dx, dy = x2 - x1, yy2 - yy1
            length = math.hypot(dx, dy)
            if length < w * 0.10:
                continue
            angle = math.degrees(math.atan2(dy, dx))
            # Normalize to the nearest horizontal orientation.
            while angle > 90.0:
                angle -= 180.0
            while angle < -90.0:
                angle += 180.0
            if abs(angle) > 18.0:
                continue
            midpoint_y = ((yy1 + yy2) * 0.5 + y0) / max(h, 1)
            centrality = math.exp(-abs(midpoint_y - 0.5) / 0.34)
            angles.append(angle)
            weights.append(length * centrality)
        if len(angles) < 3:
            continue
        values = np.asarray(angles, dtype=np.float64)
        line_weights = np.asarray(weights, dtype=np.float64)
        angle = _weighted_median(values, line_weights)
        mad = float(np.median(np.abs(values - angle)))
        support = min(1.0, float(np.sum(line_weights)) / max(w * 2.4, 1.0))
        frame_conf = clamp(support * math.exp(-mad / 2.2), 0.0, 1.0)
        per_frame.append((angle, frame_conf, len(angles)))

    if not per_frame:
        return {"confidence": 0.0, "reason": "no stable horizontal scene lines", "frames": 0}
    angles = np.asarray([item[0] for item in per_frame], dtype=np.float64)
    confidences = np.asarray([max(0.02, item[1]) for item in per_frame], dtype=np.float64)
    roll = _weighted_median(angles, confidences)
    spread = float(np.median(np.abs(angles - roll)))
    sign_support = 1.0
    meaningful = np.abs(angles) >= 0.18
    if bool(np.any(meaningful)):
        signs = np.sign(angles[meaningful])
        sign_support = float(max(np.mean(signs > 0), np.mean(signs < 0)))
    projection_factor = 0.62 if str(projection).lower() == "fisheye" else 0.82 if "equirect" in str(projection).lower() else 1.0
    confidence = clamp(
        float(np.average(confidences))
        * min(1.0, len(per_frame) / 3.0)
        * math.exp(-spread / 1.1)
        * (0.55 + 0.45 * sign_support)
        * projection_factor,
        0.0,
        1.0,
    )
    return {
        "confidence": confidence,
        "frames": len(per_frame),
        "roll_deg": roll,
        "median_roll_deg": roll,
        "stability_deg": spread,
        "suggested_orientation_rotation": (0.0, 0.0, -roll),
        "auto_applicable": bool(
            len(per_frame) >= 2
            and 0.20 <= abs(roll) <= 5.0
            and spread <= max(0.35, abs(roll) * 0.45)
            and sign_support >= 0.72
            and confidence >= 0.62
        ),
        "reason": "stable horizontal-line camera roll" if confidence >= 0.45 else "weak/inconsistent horizon evidence",
        "measurements": [
            {"roll_deg": float(angle), "confidence": float(conf), "lines": int(count)}
            for angle, conf, count in per_frame
        ],
    }

def _active_image_bounds(
    frames: list[np.ndarray],
    stereo: str,
    swapped: bool,
) -> dict[str, object]:
    """Detect persistent encoded black bars and derive bounded Zoom/Pan evidence.

    The detector itself remains projection-agnostic; the worker refuses automatic
    application for fisheye/cubemap material and requires strong symmetry and
    confidence before writing native Format Zoom/Pan values.
    """
    views: list[np.ndarray] = []
    for frame in frames:
        pair = _split_eyes(frame, stereo, swapped)
        view = pair[0] if pair is not None else np.asarray(frame, dtype=np.uint8)
        if view.ndim == 3 and min(view.shape[:2]) >= 24:
            views.append(view)
    if not views:
        return {"confidence": 0.0, "reason": "no usable frames"}

    target_h = min(view.shape[0] for view in views)
    target_w = min(view.shape[1] for view in views)
    if target_h < 24 or target_w < 24:
        return {"confidence": 0.0, "reason": "frames too small"}

    lumas: list[np.ndarray] = []
    for view in views:
        if view.shape[:2] != (target_h, target_w):
            if cv2 is not None:
                view = cv2.resize(view, (target_w, target_h), interpolation=cv2.INTER_AREA)
            else:
                ys = np.linspace(0, view.shape[0] - 1, target_h).astype(np.int32)
                xs = np.linspace(0, view.shape[1] - 1, target_w).astype(np.int32)
                view = view[ys[:, None], xs[None, :], :]
        values = view.astype(np.float32) / 255.0
        lumas.append(0.2126 * values[..., 0] + 0.7152 * values[..., 1] + 0.0722 * values[..., 2])

    # A high temporal quantile keeps a single very dark scene from masquerading
    # as permanent bars, while still requiring the border itself to stay dark.
    activity = np.quantile(np.stack(lumas), 0.85, axis=0)
    center = activity[
        int(target_h * 0.22):max(int(target_h * 0.78), int(target_h * 0.22) + 1),
        int(target_w * 0.22):max(int(target_w * 0.78), int(target_w * 0.22) + 1),
    ]
    center_level = float(np.quantile(center, 0.70)) if center.size else 0.0
    if center_level < 0.025:
        return {
            "confidence": 0.0,
            "reason": "sampled frames are too dark for border verification",
            "frames": len(views),
        }
    threshold = max(0.012, min(0.075, center_level * 0.16))
    row_score = np.quantile(activity, 0.82, axis=1)
    col_score = np.quantile(activity, 0.82, axis=0)
    row_active = row_score > threshold
    col_active = col_score > threshold

    def bounds(mask: np.ndarray, length: int) -> tuple[int, int]:
        indexes = np.flatnonzero(mask)
        if not len(indexes):
            return 0, length - 1
        return int(indexes[0]), int(indexes[-1])

    top, bottom = bounds(row_active, target_h)
    left, right = bounds(col_active, target_w)
    border = {
        "left": left / max(target_w, 1),
        "right": (target_w - 1 - right) / max(target_w, 1),
        "top": top / max(target_h, 1),
        "bottom": (target_h - 1 - bottom) / max(target_h, 1),
    }
    max_border = max(border.values())
    inner = activity[top:bottom + 1, left:right + 1]
    inner_level = float(np.quantile(inner, 0.60)) if inner.size else center_level

    border_samples: list[np.ndarray] = []
    if left:
        border_samples.append(activity[:, :left])
    if right < target_w - 1:
        border_samples.append(activity[:, right + 1:])
    if top:
        border_samples.append(activity[:top, :])
    if bottom < target_h - 1:
        border_samples.append(activity[bottom + 1:, :])
    border_level = (
        float(np.median(np.concatenate([sample.ravel() for sample in border_samples if sample.size])))
        if border_samples and any(sample.size for sample in border_samples)
        else inner_level
    )
    contrast = max(0.0, (inner_level - border_level) / max(inner_level, 1e-6))
    confidence = max(0.0, min(1.0,
        contrast * min(1.0, max_border / 0.035) * min(1.0, len(views) / 3.0)
    ))
    significant = bool(max_border >= 0.025 and confidence >= 0.55)
    x1, y1, x2, y2 = (
        left / max(target_w, 1),
        top / max(target_h, 1),
        (right + 1) / max(target_w, 1),
        (bottom + 1) / max(target_h, 1),
    )
    active_w = max(0.01, x2 - x1)
    active_h = max(0.01, y2 - y1)
    center_x = (x1 + x2) * 0.5
    center_y = (y1 + y2) * 0.5
    zoom_x = 1.0 / active_w
    zoom_y = 1.0 / active_h
    # Format Pan is kept in normalized image coordinates. Symmetric letterbox
    # bars therefore produce zero pan, while an off-center encoded window gets a
    # small correction. Exact application is still confidence-gated downstream.
    pan_x = (0.5 - center_x) * zoom_x
    pan_y = (0.5 - center_y) * zoom_y
    horizontal_symmetry = 1.0 - min(1.0, abs(border["left"] - border["right"]) / max(max(border["left"], border["right"]), 0.015))
    vertical_symmetry = 1.0 - min(1.0, abs(border["top"] - border["bottom"]) / max(max(border["top"], border["bottom"]), 0.015))
    symmetry = min(horizontal_symmetry, vertical_symmetry)
    auto_applicable = bool(
        significant
        and confidence >= 0.72
        and symmetry >= 0.72
        and active_w >= 0.58
        and active_h >= 0.58
        and max(zoom_x, zoom_y) <= 1.55
    )
    return {
        "confidence": confidence,
        "frames": len(views),
        "active_box_normalized": (x1, y1, x2, y2),
        "suggested_zoom": (zoom_x, zoom_y),
        "suggested_pan": (pan_x, pan_y),
        "symmetry": symmetry,
        "auto_applicable": auto_applicable,
        "border_fractions": border,
        "largest_border_fraction": max_border,
        "inner_activity": inner_level,
        "border_activity": border_level,
        "significant_borders": significant,
        "auto_applied": False,
        "reason": "persistent dark encoded border" if significant else "no confident persistent border",
    }


def _fisheye_geometry(eyes: list[np.ndarray]) -> dict[str, object]:
    """Estimate a stable active lens circle from several eye images."""
    if not eyes or cv2 is None:
        return {"confidence": 0.0, "reason": "fisheye contour analysis unavailable"}
    normalized: list[np.ndarray] = []
    target_h = min(eye.shape[0] for eye in eyes)
    target_w = min(eye.shape[1] for eye in eyes)
    if target_h < 32 or target_w < 32:
        return {"confidence": 0.0, "reason": "fisheye eye image too small"}
    for eye in eyes:
        if eye.shape[:2] != (target_h, target_w):
            eye = cv2.resize(eye, (target_w, target_h), interpolation=cv2.INTER_AREA)
        gray = _gray_u8(eye).astype(np.float32) / 255.0
        normalized.append(gray)
    # Max over time keeps a dark scene from pretending the whole lens is black.
    activity = np.max(np.stack(normalized), axis=0)
    threshold = max(0.025, min(0.09, float(np.quantile(activity, 0.65)) * 0.24))
    mask = (activity > threshold).astype(np.uint8) * 255
    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return {"confidence": 0.0, "reason": "no active fisheye contour"}
    contour = max(contours, key=cv2.contourArea)
    area = float(cv2.contourArea(contour))
    if area < target_h * target_w * 0.18:
        return {"confidence": 0.0, "reason": "active contour is too small"}
    perimeter = max(float(cv2.arcLength(contour, True)), 1e-6)
    circularity = max(0.0, min(1.0, 4.0 * math.pi * area / (perimeter * perimeter)))
    (cx, cy), radius = cv2.minEnclosingCircle(contour)
    circle_area = math.pi * max(radius, 1e-6) ** 2
    fill = max(0.0, min(1.0, area / circle_area))
    radius_norm = radius / max(min(target_w, target_h) * 0.5, 1.0)
    center_x = (cx - (target_w - 1) * 0.5) / max(target_w * 0.5, 1.0)
    center_y = (cy - (target_h - 1) * 0.5) / max(target_h * 0.5, 1.0)
    edge_clip = max(
        0.0,
        (radius - cx) / max(radius, 1.0),
        (radius - cy) / max(radius, 1.0),
        (cx + radius - target_w) / max(radius, 1.0),
        (cy + radius - target_h) / max(radius, 1.0),
    )
    confidence = max(0.0, min(1.0,
        (0.30 + 0.40 * circularity + 0.30 * fill)
        * min(1.0, area / (target_h * target_w * 0.50))
    ))

    # This is deliberately a coarse family/FOV-band estimate. Exact proprietary
    # calibration still comes from known lens metadata; a black circle alone is
    # not enough to reverse-engineer a camera model.
    if radius_norm < 0.82:
        fov_hint = 180.0
    elif radius_norm < 0.94:
        fov_hint = 190.0
    elif radius_norm < 1.06:
        fov_hint = 200.0
    else:
        fov_hint = 220.0
    fov_confidence = confidence * (0.55 if edge_clip < 0.08 else 0.42)
    center_offset = math.hypot(center_x, center_y)
    issues: list[str] = []
    if center_offset >= 0.10:
        issues.append("lens circle is noticeably off-center")
    if edge_clip >= 0.08:
        issues.append("lens circle appears clipped by the encoded eye frame")
    if circularity < 0.72:
        issues.append("active boundary is not strongly circular")
    return {
        "confidence": confidence,
        "fov_confidence": fov_confidence,
        "suggested_fov": fov_hint,
        "suggested_fov_family": f"approximately {int(fov_hint)}°",
        "fov_is_heuristic": True,
        "auto_apply_recommended": False,
        "circle_center_normalized": (float(center_x), float(center_y)),
        "circle_radius_normalized": float(radius_norm),
        "circle_center_offset": float(center_offset),
        "circularity": circularity,
        "contour_fill": fill,
        "edge_clip_fraction": float(edge_clip),
        "issues": issues,
        "frames": len(eyes),
    }


def _face_cascade():
    global _FACE_CASCADE, _FACE_CASCADE_ATTEMPTED
    if _FACE_CASCADE_ATTEMPTED:
        return _FACE_CASCADE
    _FACE_CASCADE_ATTEMPTED = True
    if cv2 is None or not hasattr(cv2, "data"):
        return None
    try:
        path = cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml"
        detector = cv2.CascadeClassifier(path)
        if detector.empty():
            return None
        _FACE_CASCADE = detector
    except Exception:
        _FACE_CASCADE = None
    return _FACE_CASCADE


def _orientation_evidence(frames: list[np.ndarray], stereo: str, swapped: bool) -> dict[str, object]:
    detector = _face_cascade()
    if detector is None:
        return {"confidence": 0.0, "reason": "face orientation detector unavailable"}
    upright = 0
    rotated = 0
    upright_frames = 0
    rotated_frames = 0
    for frame in frames:
        frame_upright = 0
        frame_rotated = 0
        pair = _split_eyes(frame, stereo, swapped)
        # One eye is enough and avoids counting the same face twice in stereo.
        views = (pair[0],) if pair is not None else (frame,)
        for view in views:
            small, _ = _resize_for_features(view, 520)
            gray = cv2.equalizeHist(_gray_u8(small))
            min_side = max(24, int(min(gray.shape) * 0.09))
            frame_upright += len(detector.detectMultiScale(gray, 1.1, 4, minSize=(min_side, min_side)))
            upside = cv2.rotate(gray, cv2.ROTATE_180)
            frame_rotated += len(detector.detectMultiScale(upside, 1.1, 4, minSize=(min_side, min_side)))
        upright += frame_upright
        rotated += frame_rotated
        upright_frames += int(frame_upright > frame_rotated)
        rotated_frames += int(frame_rotated > frame_upright)
    total = upright + rotated
    if total == 0:
        return {"confidence": 0.0, "upright_faces": 0, "rotated_faces": 0, "reason": "no orientation-capable faces"}
    margin = abs(rotated - upright) / total
    frame_support = max(upright_frames, rotated_frames) / max(len(frames), 1)
    confidence = min(1.0, total / 5.0) * margin * (0.45 + 0.55 * frame_support)
    suspected = bool(
        rotated >= upright + 3
        and rotated >= 3
        and rotated_frames >= min(2, len(frames))
        and confidence >= 0.72
    )
    return {
        "confidence": float(confidence),
        "upright_faces": int(upright),
        "rotated_faces": int(rotated),
        "upright_frames": int(upright_frames),
        "rotated_frames": int(rotated_frames),
        "rotate_180_suspected": suspected,
    }


def _detect_visual_layout_single(
    frame_u8: np.ndarray,
    name: str,
    width: int,
    height: int,
    filename_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    """Estimate stereo packing/projection from one representative frame.

    This is deliberately heuristic and reports confidence/evidence. It is not a
    secret camera-model oracle. The important improvement is that layout no
    longer depends solely on filenames, and every result can be overridden in
    the dashboard.
    """
    base = dict(filename_metadata or detect(name, width, height))
    rgb = np.asarray(frame_u8, dtype=np.uint8)
    if rgb.ndim != 3 or rgb.shape[0] < 16 or rgb.shape[1] < 16:
        return {"resolved": base, "confidence": 0.0, "reason": "frame unavailable"}
    gray = (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]).astype(np.float32)
    h, w = gray.shape

    left, right = gray[:, :w // 2], gray[:, w - w // 2:]
    top, bottom = gray[:h // 2, :], gray[h - h // 2:, :]
    sbs_similarity = _similarity(left, right) if min(left.shape) >= 8 else 0.0
    tb_similarity = _similarity(top, bottom) if min(top.shape) >= 8 else 0.0

    aspect = width / max(height, 1)
    sbs_shape_prior = max(0.0, min(1.0, (aspect - 1.35) / 0.85))
    tb_shape_prior = max(0.0, min(1.0, ((1.0 / max(aspect, .01)) - .55) / .65))
    sbs_score = max(0.0, min(1.0, 0.78 * max(sbs_similarity, 0.0) + 0.22 * sbs_shape_prior))
    tb_score = max(0.0, min(1.0, 0.78 * max(tb_similarity, 0.0) + 0.22 * tb_shape_prior))

    resolved = dict(base)
    stereo_conf = 0.92 if bool(base.get("layout_filename_explicit_stereo")) else max(sbs_score, tb_score)
    stereo_reason = "filename"
    if not bool(base.get("layout_filename_explicit_stereo")):
        if sbs_score >= 0.50 and sbs_score >= tb_score + 0.08:
            resolved["stereo"] = "sbs"
            stereo_reason = "visual left/right similarity"
        elif tb_score >= 0.50 and tb_score >= sbs_score + 0.08:
            resolved["stereo"] = "tb"
            stereo_reason = "visual top/bottom similarity"
        else:
            resolved["stereo"] = "mono"
            stereo_reason = "no convincing stereo pair"

    if resolved.get("stereo") == "sbs":
        eye_rgb = rgb[:, :w // 2] if not bool(resolved.get("isEyeSwapped")) else rgb[:, w - w // 2:]
        eye_aspect = (width / 2.0) / max(height, 1)
    elif resolved.get("stereo") == "tb":
        eye_rgb = rgb[:h // 2, :] if not bool(resolved.get("isEyeSwapped")) else rgb[h - h // 2:, :]
        eye_aspect = width / max(height / 2.0, 1)
    else:
        eye_rgb = rgb
        eye_aspect = aspect

    fisheye = _fisheye_score(eye_rgb)
    projection_reason = "filename"
    projection_conf = 0.92 if bool(base.get("layout_filename_explicit_projection")) else 0.0
    if not bool(base.get("layout_filename_explicit_projection")):
        if fisheye >= 0.62:
            resolved["projection"] = "fisheye"
            resolved["fov"] = 180.0 if not float(resolved.get("fov") or 0) else resolved["fov"]
            projection_conf = fisheye
            projection_reason = "dark circular lens boundary"
        elif resolved.get("stereo") in {"sbs", "tb"} and 0.78 <= eye_aspect <= 1.22:
            resolved["projection"] = "equirectangular"
            resolved["fov"] = 180.0
            projection_conf = 0.62
            projection_reason = "stereo square-eye VR180 geometry"
        elif resolved.get("stereo") == "mono" and 1.75 <= eye_aspect <= 2.25:
            resolved["projection"] = "equirectangular360"
            resolved["fov"] = 360.0
            projection_conf = 0.55
            projection_reason = "2:1 mono panorama geometry"
        else:
            resolved["projection"] = str(resolved.get("projection") or "perspective")
            projection_conf = 0.35
            projection_reason = "no strong projection signature"

    confidence = max(0.0, min(1.0, (stereo_conf * 0.55 + projection_conf * 0.45)))
    return {
        "resolved": resolved,
        "confidence": confidence,
        "stereo_confidence": float(stereo_conf),
        "projection_confidence": float(projection_conf),
        "stereo_reason": stereo_reason,
        "projection_reason": projection_reason,
        "sbs_score": float(sbs_score),
        "tb_score": float(tb_score),
        "fisheye_score": float(fisheye),
        "eye_aspect_ratio": float(eye_aspect),
        "visual_version": VISUAL_CALIBRATION_VERSION,
    }


def detect_visual_layout_sequence(
    frames: list[np.ndarray],
    name: str,
    width: int,
    height: int,
    filename_metadata: dict[str, object] | None = None,
    *,
    orientation_verification: bool = True,
    orientation_auto_apply: bool = False,
    stereo_alignment: bool = True,
    stereo_alignment_max_degrees: float = 0.75,
    lens_verification: bool = True,
    auto_apply_eye_swap: bool = False,
    lens_fov_auto_apply: bool = False,
    lens_fov_min_confidence: float = 0.80,
    horizon_leveling: bool = True,
) -> dict[str, object]:
    """Aggregate several frames into one conservative VR calibration result."""
    usable = [np.asarray(frame, dtype=np.uint8) for frame in frames if np.asarray(frame).ndim == 3]
    base = dict(filename_metadata or detect(name, width, height))
    if not usable:
        return {"resolved": base, "confidence": 0.0, "reason": "frames unavailable"}

    singles = [
        _detect_visual_layout_single(frame, name, width, height, base)
        for frame in usable
    ]
    sbs_score = float(np.median([item.get("sbs_score", 0.0) for item in singles]))
    tb_score = float(np.median([item.get("tb_score", 0.0) for item in singles]))
    fisheye_score = float(np.median([item.get("fisheye_score", 0.0) for item in singles]))
    eye_aspect = float(np.median([item.get("eye_aspect_ratio", 0.0) for item in singles]))

    resolved = dict(base)
    conflicts: list[str] = []
    explicit_stereo = bool(base.get("layout_filename_explicit_stereo"))
    explicit_projection = bool(base.get("layout_filename_explicit_projection"))

    visual_stereo = "mono"
    stereo_confidence = max(sbs_score, tb_score)
    stereo_reason = "no convincing stereo pair"
    if sbs_score >= 0.50 and sbs_score >= tb_score + 0.07:
        visual_stereo = "sbs"
        stereo_reason = "multi-frame left/right similarity"
    elif tb_score >= 0.50 and tb_score >= sbs_score + 0.07:
        visual_stereo = "tb"
        stereo_reason = "multi-frame top/bottom similarity"
    if explicit_stereo:
        if visual_stereo != "mono" and visual_stereo != str(base.get("stereo")) and stereo_confidence >= 0.62:
            conflicts.append(
                f"filename says {base.get('stereo')}, pixels resemble {visual_stereo}"
            )
        stereo_confidence = max(stereo_confidence, 0.92)
        stereo_reason = "filename (pixel-verified)" if not conflicts else "filename (pixel conflict)"
    elif visual_stereo != "mono":
        resolved["stereo"] = visual_stereo
    else:
        resolved["stereo"] = "mono"

    if resolved.get("stereo") == "sbs":
        per_eye_aspect = (width / 2.0) / max(height, 1)
        # Two full 16:9 frames packed side-by-side are much wider than ordinary
        # half-SBS. Keep filename authority, but infer the distinct HSP mode when
        # the geometry is unmistakable.
        if not explicit_stereo and per_eye_aspect >= 1.55:
            resolved["full_side_by_side"] = True
    elif resolved.get("stereo") != "sbs":
        resolved["full_side_by_side"] = False

    visual_projection = str(base.get("projection") or "perspective")
    projection_confidence = 0.35
    projection_reason = "no strong projection signature"
    if fisheye_score >= 0.60:
        visual_projection = "fisheye"
        projection_confidence = fisheye_score
        projection_reason = "multi-frame circular lens boundary"
    elif resolved.get("stereo") in {"sbs", "tb"} and 0.78 <= eye_aspect <= 1.22:
        visual_projection = "equirectangular"
        projection_confidence = 0.64
        projection_reason = "stereo square-eye VR180 geometry"
    elif resolved.get("stereo") == "mono" and 1.75 <= width / max(height, 1) <= 2.25:
        visual_projection = "equirectangular360"
        projection_confidence = 0.57
        projection_reason = "2:1 mono panorama geometry"

    if explicit_projection:
        if visual_projection != str(base.get("projection")) and projection_confidence >= 0.66:
            conflicts.append(
                f"filename says {base.get('projection')}, pixels resemble {visual_projection}"
            )
        projection_confidence = max(projection_confidence, 0.92)
        projection_reason = "filename (pixel-verified)" if not conflicts else "filename (pixel conflict)"
    elif projection_confidence >= 0.55:
        resolved["projection"] = visual_projection
        if visual_projection == "equirectangular":
            resolved["fov"] = 180.0
        elif visual_projection == "equirectangular360":
            resolved["fov"] = 360.0
        elif visual_projection == "fisheye" and not float(resolved.get("fov") or 0.0):
            resolved["fov"] = 180.0

    alignment: dict[str, object] = {"confidence": 0.0, "reason": "not stereoscopic"}
    if stereo_alignment and resolved.get("stereo") in {"sbs", "tb"}:
        alignment = _aggregate_stereo_alignment(
            usable,
            str(resolved.get("stereo")),
            bool(resolved.get("isEyeSwapped")),
            float(resolved.get("fov") or 180.0),
            stereo_alignment_max_degrees,
        )
        if auto_apply_eye_swap and not explicit_stereo and float(alignment.get("eye_swap_confidence") or 0.0) >= 0.86:
            resolved["isEyeSwapped"] = not bool(resolved.get("isEyeSwapped"))
            alignment["eye_swap_auto_applied"] = True

    orientation: dict[str, object] = {"confidence": 0.0, "reason": "disabled"}
    if orientation_verification:
        orientation = _orientation_evidence(
            usable,
            str(resolved.get("stereo") or "mono"),
            bool(resolved.get("isEyeSwapped")),
        )
        if (
            orientation_auto_apply
            and bool(orientation.get("rotate_180_suspected"))
            and float(orientation.get("confidence") or 0.0) >= 0.82
        ):
            resolved["flip_left_right"] = True
            resolved["flip_up_down"] = True
            orientation["auto_applied"] = True
        else:
            orientation["auto_applied"] = False

    horizon: dict[str, object] = {"confidence": 0.0, "reason": "disabled"}
    if horizon_leveling:
        horizon = _horizon_evidence(
            usable,
            str(resolved.get("stereo") or "mono"),
            bool(resolved.get("isEyeSwapped")),
            str(resolved.get("projection") or "perspective"),
        )

    active_bounds = _active_image_bounds(
        usable,
        str(resolved.get("stereo") or "mono"),
        bool(resolved.get("isEyeSwapped")),
    )

    lens_detail: dict[str, object] = {"confidence": 0.0, "reason": "not fisheye"}
    if lens_verification and resolved.get("projection") == "fisheye":
        eyes: list[np.ndarray] = []
        for frame in usable:
            pair = _split_eyes(frame, str(resolved.get("stereo") or "mono"), bool(resolved.get("isEyeSwapped")))
            eyes.append(pair[0] if pair is not None else frame)
        lens_detail = _fisheye_geometry(eyes)
        existing_fov = float(resolved.get("fov") or 0.0)
        suggested_fov = float(lens_detail.get("suggested_fov") or 0.0)
        fov_conf = float(lens_detail.get("fov_confidence") or 0.0)
        lens_detail["filename_lens"] = str(base.get("lens") or "Linear")
        lens_detail["filename_fov"] = existing_fov
        lens_detail["known_profile"] = bool(
            "mkx200" in str(base.get("lens") or "").casefold()
            or (str(base.get("lens") or "").casefold() == "linear" and existing_fov in {135.0, 180.0, 190.0})
        )
        if (
            lens_fov_auto_apply
            and not explicit_projection
            and suggested_fov > 0
            and fov_conf >= float(lens_fov_min_confidence)
        ):
            resolved["fov"] = suggested_fov
            lens_detail["fov_auto_applied"] = True
        elif existing_fov > 0 and suggested_fov > 0 and abs(existing_fov - suggested_fov) >= 25 and fov_conf >= 0.62:
            conflicts.append(
                f"advertised FOV {existing_fov:g}° differs from the coarse lens-boundary family hint {suggested_fov:g}°"
            )

    alignment_conf = float(alignment.get("confidence") or 0.0)
    confidence = max(0.0, min(1.0,
        stereo_confidence * 0.42
        + projection_confidence * 0.38
        + max(alignment_conf, float(lens_detail.get("confidence") or 0.0)) * 0.20
    ))
    verification = "conflict" if conflicts else "verified" if confidence >= 0.70 else "ambiguous"
    return {
        "resolved": resolved,
        "confidence": confidence,
        "verification": verification,
        "conflicts": conflicts,
        "frames_used": len(usable),
        "stereo_confidence": float(stereo_confidence),
        "projection_confidence": float(projection_confidence),
        "stereo_reason": stereo_reason,
        "projection_reason": projection_reason,
        "sbs_score": sbs_score,
        "tb_score": tb_score,
        "fisheye_score": fisheye_score,
        "eye_aspect_ratio": eye_aspect,
        "alignment": alignment,
        "orientation": orientation,
        "horizon": horizon,
        "active_bounds": active_bounds,
        "lens_verification": lens_detail,
        "visual_version": VISUAL_CALIBRATION_VERSION,
    }


def detect_visual_layout(
    frame_u8: np.ndarray,
    name: str,
    width: int,
    height: int,
    filename_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    """One-frame convenience wrapper around the sequence verifier."""
    return detect_visual_layout_sequence(
        [frame_u8], name, width, height, filename_metadata,
        orientation_verification=False,
        orientation_auto_apply=False,
        stereo_alignment=False,
        lens_verification=True,
        horizon_leveling=False,
    )


def apply_layout_override(metadata: dict[str, object], override: dict[str, Any] | None) -> dict[str, object]:
    result = dict(metadata or {})
    if not override:
        return result
    allowed = {
        "stereo", "projection", "lens", "fov", "isEyeSwapped", "full_side_by_side",
        "flip_front_back", "flip_left_right", "flip_up_down", "aspect_ratio_type",
        "aspect_ratio", "zoom", "pan", "orientation_type",
        "alignment_position", "alignment_rotation",
        "stitch_shift", "stitch_scale", "stitch_shear", "stitch_flare", "stitch_slant",
        "orientation_rotation",
        "autofocus_rotation", "autofocus_focal_distance", "autofocus_min_distance",
        "autofocus_max_distance", "autofocus_transition_speed",
        "audio_time_offset", "audio_volume_multiplier",
        "sync_start_offset_seconds", "sync_drift_seconds_per_hour",
    }
    for key in allowed:
        if key in override and override[key] is not None and override[key] != "auto":
            result[key] = override[key]
    result["layout_override_active"] = True
    return result
