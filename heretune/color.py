from __future__ import annotations

"""Small color-science helpers shared by analysis and browser previews.

The temperature/tint functions mirror the chromaticity model used by Unreal's
post-process white-balance stage: Planckian below 4000 K, daylight above it,
with tint moving along an isothermal line.  HereTune still treats measurements
from ordinary encoded video as evidence—not laboratory colorimetry—and keeps
all automatic corrections conservative.
"""

import math
from functools import lru_cache

import numpy as np

SRGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float64,
)
XYZ_TO_SRGB = np.linalg.inv(SRGB_TO_XYZ)
BRADFORD = np.array(
    [
        [0.8951, 0.2664, -0.1614],
        [-0.7502, 1.7135, 0.0367],
        [0.0389, -0.0685, 1.0296],
    ],
    dtype=np.float64,
)
BRADFORD_INV = np.linalg.inv(BRADFORD)
D65_XY = (0.31270, 0.32900)


def srgb_to_linear(values: np.ndarray) -> np.ndarray:
    rgb = np.asarray(values, dtype=np.float64)
    return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(values: np.ndarray) -> np.ndarray:
    rgb = np.asarray(values, dtype=np.float64)
    return np.where(
        rgb <= 0.0031308,
        12.92 * rgb,
        1.055 * np.power(np.clip(rgb, 0.0, None), 1.0 / 2.4) - 0.055,
    )


def xy_to_uv(x: float, y: float) -> tuple[float, float]:
    denominator = -2.0 * x + 12.0 * y + 3.0
    if abs(denominator) < 1e-12:
        return 0.0, 0.0
    return 4.0 * x / denominator, 6.0 * y / denominator


def uv_to_xy(u: float, v: float) -> tuple[float, float]:
    denominator = 2.0 * u - 8.0 * v + 4.0
    if abs(denominator) < 1e-12:
        return D65_XY
    return 3.0 * u / denominator, 2.0 * v / denominator


def xy_to_xyz(x: float, y: float) -> np.ndarray:
    y = max(float(y), 1e-8)
    return np.array([float(x) / y, 1.0, (1.0 - float(x) - y) / y], dtype=np.float64)


def linear_rgb_to_xy(rgb: np.ndarray) -> tuple[float, float] | None:
    values = np.asarray(rgb, dtype=np.float64).reshape(3)
    xyz = SRGB_TO_XYZ @ values
    total = float(np.sum(xyz))
    if total <= 1e-12 or not np.all(np.isfinite(xyz)):
        return None
    return float(xyz[0] / total), float(xyz[1] / total)


def planckian_locus_xy(temp: float) -> tuple[float, float]:
    """Krystek 1985 Planckian locus approximation used by Unreal-era shaders."""
    t = max(1000.0, min(25000.0, float(temp)))
    u = (
        0.860117757 + 1.54118254e-4 * t + 1.28641212e-7 * t * t
    ) / (
        1.0 + 8.42420235e-4 * t + 7.08145163e-7 * t * t
    )
    v = (
        0.317398726 + 4.22806245e-5 * t + 4.20481691e-8 * t * t
    ) / (
        1.0 - 2.89741816e-5 * t + 1.61456053e-7 * t * t
    )
    return uv_to_xy(u, v)


def daylight_locus_xy(temp: float) -> tuple[float, float]:
    """CIE D-series daylight chromaticity approximation used by Unreal."""
    t = max(4000.0, min(25000.0, float(temp))) * (1.4388 / 1.438)
    if t <= 7000.0:
        x = 0.244063 + (0.09911e3 + (2.9678e6 - 4.6070e9 / t) / t) / t
    else:
        x = 0.237040 + (0.24748e3 + (1.9018e6 - 2.0064e9 / t) / t) / t
    y = -3.0 * x * x + 2.87 * x - 0.275
    return float(x), float(y)


def planckian_isothermal_xy(temp: float, tint: float) -> tuple[float, float]:
    """Move along Unreal's temperature-orthogonal magenta/green tint axis."""
    t = max(1000.0, min(25000.0, float(temp)))
    tint = max(-1.0, min(1.0, float(tint)))
    u = (
        0.860117757 + 1.54118254e-4 * t + 1.28641212e-7 * t * t
    ) / (
        1.0 + 8.42420235e-4 * t + 7.08145163e-7 * t * t
    )
    v = (
        0.317398726 + 4.22806245e-5 * t + 4.20481691e-8 * t * t
    ) / (
        1.0 - 2.89741816e-5 * t + 1.61456053e-7 * t * t
    )
    # Derivatives of the Krystek rational approximation.  Unreal moves along
    # the normalized isothermal direction by tint * 0.05 in 1960 UCS.
    ud = (
        -1.13758118e9 - 1.91615621e6 * t - 1.53177 * t * t
    ) / ((1.41213984e6 + 1189.62 * t + t * t) ** 2)
    vd = (
        1.97471536e9 - 705674.0 * t - 308.607 * t * t
    ) / ((6.19363586e6 - 179.456 * t + t * t) ** 2)
    norm = math.hypot(ud, vd)
    if norm > 1e-20:
        ud /= norm
        vd /= norm
        u += -vd * tint * 0.05
        v += ud * tint * 0.05
    return uv_to_xy(u, v)


def unreal_white_xy(temp: float, tint: float = 0.0) -> tuple[float, float]:
    """White chromaticity selected by Unreal's Temp/Tint controls."""
    t = max(1500.0, min(15000.0, float(temp)))
    base = planckian_locus_xy(t) if t < 4000.0 else daylight_locus_xy(t)
    planck = planckian_locus_xy(t)
    isothermal = planckian_isothermal_xy(t, tint)
    return (
        float(base[0] + isothermal[0] - planck[0]),
        float(base[1] + isothermal[1] - planck[1]),
    )


@lru_cache(maxsize=1)
def _temperature_grid() -> tuple[float, ...]:
    # Dense enough to make the UI value stable while remaining tiny compared
    # with video decoding.  Extra density around household/camera lighting.
    low = np.linspace(1800.0, 4000.0, 70, endpoint=False)
    high = np.linspace(4000.0, 15000.0, 180)
    return tuple(float(value) for value in np.concatenate([low, high]))


def estimate_unreal_temp_tint(
    encoded_rgb: np.ndarray,
) -> tuple[float, float, float] | None:
    """Fit encoded RGB neutral evidence to Unreal Temp/Tint controls.

    Returns ``(temperature_kelvin, tint, fit_error_uv)``.  The fit error is in
    CIE 1960 UCS and lets the caller reject samples that are not actually
    explainable as a simple temperature/tint cast.
    """
    values = np.asarray(encoded_rgb, dtype=np.float64).reshape(3)
    if not np.all(np.isfinite(values)) or float(np.max(values)) <= 1e-7:
        return None
    xy = linear_rgb_to_xy(srgb_to_linear(np.clip(values, 0.0, 1.0)))
    if xy is None:
        return None
    target_uv = np.asarray(xy_to_uv(*xy), dtype=np.float64)

    best: tuple[float, float, float] | None = None
    for temp in _temperature_grid():
        base_xy = unreal_white_xy(temp, 0.0)
        plus_xy = unreal_white_xy(temp, 1.0)
        base_uv = np.asarray(xy_to_uv(*base_xy), dtype=np.float64)
        plus_uv = np.asarray(xy_to_uv(*plus_xy), dtype=np.float64)
        direction = plus_uv - base_uv
        denom = float(np.dot(direction, direction))
        tint = 0.0 if denom <= 1e-14 else float(
            np.dot(target_uv - base_uv, direction) / denom
        )
        tint = max(-1.0, min(1.0, tint))
        predicted_uv = np.asarray(xy_to_uv(*unreal_white_xy(temp, tint)))
        error = float(np.linalg.norm(predicted_uv - target_uv))
        if best is None or error < best[2]:
            best = (float(temp), float(tint), error)

    if best is None:
        return None
    # One local temperature refinement avoids 50–150 K display jitter.
    center, _, _ = best
    for temp in np.linspace(max(1500.0, center - 120.0), min(15000.0, center + 120.0), 31):
        base_uv = np.asarray(xy_to_uv(*unreal_white_xy(float(temp), 0.0)))
        plus_uv = np.asarray(xy_to_uv(*unreal_white_xy(float(temp), 1.0)))
        direction = plus_uv - base_uv
        denom = float(np.dot(direction, direction))
        tint = 0.0 if denom <= 1e-14 else float(
            np.dot(target_uv - base_uv, direction) / denom
        )
        tint = max(-1.0, min(1.0, tint))
        predicted_uv = np.asarray(xy_to_uv(*unreal_white_xy(float(temp), tint)))
        error = float(np.linalg.norm(predicted_uv - target_uv))
        if error < best[2]:
            best = (float(temp), float(tint), error)
    return best


def chromatic_adaptation_rgb_matrix(temp: float, tint: float) -> np.ndarray:
    """Bradford-adapt the selected source white to D65 in linear sRGB."""
    source = xy_to_xyz(*unreal_white_xy(temp, tint))
    target = xy_to_xyz(*D65_XY)
    source_lms = BRADFORD @ source
    target_lms = BRADFORD @ target
    ratio = target_lms / np.maximum(np.abs(source_lms), 1e-9)
    adaptation_xyz = BRADFORD_INV @ np.diag(ratio) @ BRADFORD
    return XYZ_TO_SRGB @ adaptation_xyz @ SRGB_TO_XYZ


def apply_white_balance_linear(
    linear_rgb: np.ndarray,
    temp: float,
    tint: float,
) -> np.ndarray:
    matrix = chromatic_adaptation_rgb_matrix(temp, tint)
    values = np.asarray(linear_rgb, dtype=np.float64)
    return np.einsum("...c,dc->...d", values, matrix)
