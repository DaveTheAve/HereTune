from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np

from .color import apply_white_balance_linear, linear_to_srgb, srgb_to_linear
from .vrformat import VISUAL_CALIBRATION_VERSION, detect, detect_visual_layout_sequence
from .processes import run_capture

try:  # preview encoding/dewarp helpers; core probing still works without cv2.
    import cv2  # type: ignore
except Exception:  # pragma: no cover - defensive on broken Windows cv2 installs
    cv2 = None


class MediaError(RuntimeError):
    pass


HDR_TRANSFERS = {"smpte2084", "arib-std-b67", "pq", "hlg"}

# Static drawer comparison renderer. Bump this whenever rendering semantics or
# sample selection changes; the API exposes it so every existing Ready video
# receives the new screenshots without re-analysis.
PREVIEW_RENDER_VERSION = 8
PREVIEW_CARD_WIDTH = 960
PREVIEW_CARD_HEIGHT = 540
PREVIEW_JPEG_QUALITY = 93
# Shadows/Midtones/Highlights are serialized player-owned controls whose exact
# transfer functions are not documented in the v7 layout. Keep their *direction*
# visible in the dashboard while deliberately damping the approximation so the
# static screenshot cannot overstate a grade HereSphere will render differently.
PREVIEW_REGIONAL_TONE_STRENGTH = 1.00
# Native scalar/vector storage is verified, but HereSphere's exact post-process
# transfer functions are player-owned. Dampen only those nonlinear operations in
# the static screenshot rather than pretending an inferred curve is exact.
PREVIEW_NONLINEAR_STRENGTH = 1.00
PREVIEW_WHITEPOINT_STRENGTH = 1.00
PREVIEW_SHARPEN_STRENGTH = 1.00


def _decode_output(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def _bit_depth(pixel_format: str, raw_bits) -> int:
    try:
        value = int(raw_bits or 0)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    text = (pixel_format or "").lower()
    match = re.search(r"(?:p|yuv\d*p?)(9|10|12|14|16)(?:le|be)?", text)
    if match:
        return int(match.group(1))
    for token, value in (("p010", 10), ("p012", 12), ("p016", 16)):
        if token in text:
            return value
    return 8


def _rate(value: str) -> float:
    try:
        numerator, denominator = str(value or "0/1").split("/", 1)
        return float(numerator) / max(float(denominator), 1e-12)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def probe(
    path: Path, ffprobe: str = "ffprobe", timeout_seconds: float = 30.0
) -> dict[str, object]:
    try:
        process = run_capture([
        ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json",
        str(path),
        ], timeout=max(5.0, float(timeout_seconds)))
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"ffprobe timed out after {float(timeout_seconds):.0f}s") from exc
    stdout = _decode_output(process.stdout)
    stderr = _decode_output(process.stderr)
    if process.returncode:
        raise MediaError(stderr.strip() or "ffprobe failed")
    try:
        document = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe returned invalid JSON: {exc}") from exc

    streams = document.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        raise MediaError("No video stream found")
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    container = document.get("format", {})

    try:
        duration = float(container.get("duration") or video.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise MediaError("Could not determine video duration")

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise MediaError("Invalid video dimensions")

    pixel_format = str(video.get("pix_fmt") or "")
    transfer = str(video.get("color_transfer") or "unknown")
    side_data = [
        str(item.get("side_data_type") or "")
        for item in (video.get("side_data_list") or [])
        if item.get("side_data_type")
    ]
    tags = {str(key): str(value) for key, value in (video.get("tags") or {}).items()}

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": _rate(video.get("avg_frame_rate")),
        "video_codec": str(video.get("codec_name") or "unknown"),
        "audio_codecs": [str(stream.get("codec_name") or "") for stream in audio],
        "pix_fmt": pixel_format,
        "bit_depth": _bit_depth(pixel_format, video.get("bits_per_raw_sample")),
        "color_transfer": transfer,
        "color_space": str(video.get("color_space") or "unknown"),
        "color_primaries": str(video.get("color_primaries") or "unknown"),
        "color_range": str(video.get("color_range") or "unknown"),
        "is_hdr": transfer.lower() in HDR_TRANSFERS,
        "vr_side_data": side_data,
        "video_tags": tags,
        **detect(path.name, width, height),
    }


def _decode_rgb_frame(
    path: Path,
    ffmpeg: str,
    duration: float,
    source_width: int,
    source_height: int,
    max_width: int = 640,
    timestamp_ratio: float = 0.35,
    timeout_seconds: float = 20.0,
) -> np.ndarray:
    width = min(max(64, int(max_width)), max(64, int(source_width)))
    height = max(2, int(round(source_height * (width / max(source_width, 1)))))
    if width % 2:
        width -= 1
    if height % 2:
        height -= 1
    timestamp = max(0.0, min(float(duration) * timestamp_ratio, max(0.0, float(duration) - 0.05)))
    try:
        process = run_capture([
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{timestamp:.3f}",
        "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn",
        "-frames:v", "1", "-vf", f"scale={width}:{height}:flags=area,format=rgb24",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ], timeout=max(5.0, float(timeout_seconds)))
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"FFmpeg preview seek timed out after {float(timeout_seconds):.0f}s") from exc
    expected = width * height * 3
    raw = process.stdout or b""
    if process.returncode or len(raw) < expected:
        raise MediaError(_decode_output(process.stderr).strip() or "Could not decode preview frame")
    return np.frombuffer(raw[:expected], dtype=np.uint8).reshape(height, width, 3).copy()


def detect_layout_from_video(
    path: Path,
    ffmpeg: str,
    metadata: dict[str, object],
    analysis_cfg=None,
) -> dict[str, object]:
    """Add frame-based layout evidence to ffprobe/filename metadata.

    Failure is deliberately non-fatal; filename metadata remains usable and the
    dashboard exposes an explicit override for ambiguous sources.
    """
    if analysis_cfg is not None and not bool(getattr(analysis_cfg, "vr_verification", True)):
        return {"confidence": 0.0, "reason": "VR verification disabled"}
    try:
        frame_count = max(1, min(7, int(getattr(analysis_cfg, "vr_verification_frames", 6))))
        ratios = np.linspace(0.16, 0.76, frame_count).tolist()
        frames = [
            _decode_rgb_frame(
                path,
                ffmpeg,
                float(metadata.get("duration") or 0.0),
                int(metadata.get("width") or 0),
                int(metadata.get("height") or 0),
                max_width=max(640, min(960, int(getattr(analysis_cfg, "analysis_width", 960)))),
                timestamp_ratio=float(ratio),
                timeout_seconds=float(getattr(analysis_cfg, "ffmpeg_sample_timeout_seconds", 20.0)) if analysis_cfg is not None else 20.0,
            )
            for ratio in ratios
        ]
        visual = detect_visual_layout_sequence(
            frames,
            path.name,
            int(metadata.get("width") or 0),
            int(metadata.get("height") or 0),
            metadata,
            orientation_verification=bool(getattr(analysis_cfg, "orientation_verification", True)),
            stereo_alignment=bool(getattr(analysis_cfg, "stereo_alignment", True)),
            stereo_alignment_max_degrees=float(getattr(analysis_cfg, "stereo_alignment_max_degrees", 0.75)),
            lens_verification=bool(getattr(analysis_cfg, "lens_verification", True)),
            horizon_leveling=bool(getattr(analysis_cfg, "horizon_leveling", True)),
        )
    except Exception as exc:
        return {"confidence": 0.0, "reason": f"visual detection unavailable: {exc}"}

    resolved = visual.get("resolved") if isinstance(visual, dict) else None
    if isinstance(resolved, dict):
        # Strong filename signals stay authoritative. Otherwise visual evidence
        # can replace the default mono/perspective placeholders.
        if not bool(metadata.get("layout_filename_explicit_stereo")) and float(visual.get("stereo_confidence") or 0.0) >= 0.50:
            for key in ("stereo", "isEyeSwapped", "full_side_by_side"):
                if key in resolved:
                    metadata[key] = resolved[key]
        if not bool(metadata.get("layout_filename_explicit_projection")) and float(visual.get("projection_confidence") or 0.0) >= 0.55:
            for key in ("projection", "fov", "lens"):
                if key in resolved:
                    metadata[key] = resolved[key]
        for key in ("flip_front_back", "flip_left_right", "flip_up_down"):
            if key in resolved:
                metadata[key] = bool(resolved[key])
        alignment = visual.get("alignment") if isinstance(visual, dict) else None
        if isinstance(alignment, dict):
            metadata["stereo_alignment"] = alignment
        lens_verification = visual.get("lens_verification") if isinstance(visual, dict) else None
        if isinstance(lens_verification, dict):
            metadata["lens_verification"] = lens_verification
        orientation = visual.get("orientation") if isinstance(visual, dict) else None
        if isinstance(orientation, dict):
            metadata["orientation_verification"] = orientation
        horizon = visual.get("horizon") if isinstance(visual, dict) else None
        if isinstance(horizon, dict):
            metadata["horizon_verification"] = horizon
        active_bounds = visual.get("active_bounds") if isinstance(visual, dict) else None
        if isinstance(active_bounds, dict):
            metadata["active_image_bounds"] = active_bounds
    metadata["layout_detection"] = visual
    metadata["visual_calibration_version"] = VISUAL_CALIBRATION_VERSION
    return visual


def _eye_crop(metadata: dict[str, object], eye: str = "auto") -> str | None:
    stereo = str(metadata.get("stereo") or "mono").lower()
    swapped = bool(metadata.get("isEyeSwapped", False))
    requested = str(eye or "auto").lower()
    if requested in {"left", "right"}:
        choose_second = requested == "right"
    else:
        choose_second = swapped
    if stereo == "sbs":
        x = "iw/2" if choose_second else "0"
        return f"crop=trunc(iw/2/2)*2:trunc(ih/2)*2:{x}:0"
    if stereo == "tb":
        y = "ih/2" if choose_second else "0"
        return f"crop=trunc(iw/2)*2:trunc(ih/2/2)*2:0:{y}"
    return None


def thumbnail_filters(metadata: dict[str, object], *, dewarp: bool = True, eye: str = "auto") -> list[str]:
    # Deliberately use the proven, predictable browse strategy: one eye first,
    # then preserve aspect ratio and center-crop. Player projection calibration is
    # not approximated in the library browser.
    filters: list[str] = []
    crop = _eye_crop(metadata, eye)
    if crop:
        filters.append(crop)
    filters.extend([
        "scale=640:360:force_original_aspect_ratio=increase:flags=lanczos",
        "crop=640:360",
        "setsar=1",
    ])
    return filters


def _run_thumbnail_command(
    path: Path,
    destination: Path,
    ffmpeg: str,
    timestamp: float,
    filters: list[str],
    timeout_seconds: float = 30.0,
) -> tuple[bool, str]:
    try:
        process = run_capture([
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{timestamp:.3f}",
        "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn",
        "-frames:v", "1", "-vf", ",".join(filters), "-q:v", "3",
        "-pix_fmt", "yuvj420p", "-y", str(destination),
        ], timeout=max(5.0, float(timeout_seconds)))
    except subprocess.TimeoutExpired:
        return False, f"FFmpeg thumbnail timed out after {float(timeout_seconds):.0f}s"
    ok = process.returncode == 0 and destination.exists() and destination.stat().st_size > 0
    return ok, _decode_output(process.stderr).strip()


def thumbnail(
    path: Path,
    destination: Path,
    ffmpeg: str,
    duration: float,
    metadata: dict[str, object] | None = None,
) -> None:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    timestamp = max(0.0, float(duration) * 0.35)
    metadata = metadata or {}
    filters = thumbnail_filters(metadata)
    ok, error = _run_thumbnail_command(path, destination, ffmpeg, timestamp, filters)
    if ok:
        return
    raise MediaError(error or "thumbnail generation failed")


def _jpeg_bytes(rgb: np.ndarray, quality: int = 88) -> bytes:
    if cv2 is None:
        raise MediaError("OpenCV is required for preview JPEG generation")
    bgr = cv2.cvtColor(np.asarray(rgb, dtype=np.uint8), cv2.COLOR_RGB2BGR)
    ok, encoded = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise MediaError("Could not encode preview JPEG")
    return encoded.tobytes()


def _fit_card(rgb: np.ndarray, width: int = 640, height: int = 360) -> np.ndarray:
    if cv2 is None:
        raise MediaError("OpenCV is required for preview generation")
    h, w = rgb.shape[:2]
    scale = max(width / max(w, 1), height / max(h, 1))
    nw, nh = max(width, int(round(w * scale))), max(height, int(round(h * scale)))
    resized = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LANCZOS4)
    x = max(0, (nw - width) // 2)
    y = max(0, (nh - height) // 2)
    return resized[y:y + height, x:x + width].copy()


def _select_eye_array(rgb: np.ndarray, metadata: dict[str, object], eye: str = "auto") -> np.ndarray:
    stereo = str(metadata.get("stereo") or "mono").lower()
    swapped = bool(metadata.get("isEyeSwapped", False))
    choose_second = (str(eye).lower() == "right") if str(eye).lower() in {"left", "right"} else swapped
    h, w = rgb.shape[:2]
    if stereo == "sbs":
        return rgb[:, w // 2:] if choose_second else rgb[:, :w // 2]
    if stereo == "tb":
        return rgb[h // 2:, :] if choose_second else rgb[:h // 2, :]
    return rgb


def _comparison_preview_timestamp(
    correction: dict[str, object] | None,
    duration: float,
) -> float:
    """Choose a stable, representative timestamp for the drawer comparison.

    A fixed 35% timestamp is less representative than sparse grading, which gives us better
    information: choose the midpoint of the longest/highest-confidence stable
    grading segment, while avoiding intros/outros. The result is deterministic,
    so BEFORE and AFTER always decode the exact same source frame.
    """
    duration = max(0.0, float(duration or 0.0))
    if duration <= 0.0:
        return 0.0
    fallback = duration * 0.35
    scenes = (correction or {}).get("scene_keyframes") if isinstance(correction, dict) else None
    if not isinstance(scenes, list) or not scenes:
        return max(0.0, min(fallback, max(0.0, duration - 0.05)))

    starts: list[tuple[float, float]] = []
    for item in scenes:
        if not isinstance(item, dict):
            continue
        try:
            start = max(0.0, min(duration, float(item.get("time_seconds") or 0.0)))
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            continue
        starts.append((start, confidence))
    if not starts:
        return max(0.0, min(fallback, max(0.0, duration - 0.05)))
    starts.sort(key=lambda pair: pair[0])

    best_timestamp = fallback
    best_score = -1.0
    safe_lo = duration * 0.08
    safe_hi = duration * 0.92
    for index, (start, confidence) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else duration
        span = max(0.0, end - start)
        if span < 1.0:
            continue
        midpoint = start + span * 0.50
        # Long stable segments and high-confidence grades are better screenshots.
        # Mildly prefer the middle of the movie so intros/outros do not dominate.
        centrality = 1.0 - min(1.0, abs(midpoint / duration - 0.5) * 1.45)
        span_score = min(1.0, span / max(90.0, duration * 0.22))
        score = 0.56 * confidence + 0.30 * span_score + 0.14 * centrality
        if score > best_score:
            best_score = score
            best_timestamp = midpoint
    best_timestamp = max(safe_lo, min(safe_hi, best_timestamp)) if duration >= 20.0 else best_timestamp
    return max(0.0, min(best_timestamp, max(0.0, duration - 0.05)))


def preview_frame(
    path: Path,
    ffmpeg: str,
    metadata: dict[str, object],
    variant: str = "final",
    *,
    timestamp_seconds: float | None = None,
    card_width: int = 640,
    card_height: int = 360,
    timeout_seconds: float = 20.0,
) -> np.ndarray:
    duration = float(metadata.get("duration") or 0.0)
    ratio = 0.35
    if timestamp_seconds is not None and duration > 0.0:
        ratio = max(0.0, min(1.0, float(timestamp_seconds) / duration))
    variant = str(variant or "final").lower()
    stereo = str(metadata.get("stereo") or "mono").lower()
    # Preserve actual eye detail. An SBS frame decoded at 960px contains only
    # ~480px per eye; decode twice the requested card width before selecting an
    # eye so a 960px comparison contains ~960px of source detail instead of a
    # 2x upscale. TB keeps full horizontal resolution per eye.
    packed_width = int(card_width) * 2 if stereo == "sbs" and variant != "source" else int(card_width)
    rgb = _decode_rgb_frame(
        path,
        ffmpeg,
        duration,
        int(metadata.get("width") or 0),
        int(metadata.get("height") or 0),
        max_width=max(960, packed_width),
        timestamp_ratio=ratio,
        timeout_seconds=timeout_seconds,
    )
    if variant == "source":
        selected = rgb
    elif variant == "left":
        selected = _select_eye_array(rgb, metadata, "left")
    elif variant == "right":
        selected = _select_eye_array(rgb, metadata, "right")
    else:
        selected = _select_eye_array(rgb, metadata, "auto")
    return _fit_card(selected, width=int(card_width), height=int(card_height))


def preview_jpeg(path: Path, ffmpeg: str, metadata: dict[str, object], variant: str = "final", timeout_seconds: float = 20.0) -> bytes:
    return _jpeg_bytes(preview_frame(path, ffmpeg, metadata, variant, timeout_seconds=timeout_seconds))


def _scene_correction_at(
    correction: dict[str, object], timestamp_seconds: float
) -> dict[str, object]:
    """Select the last sparse Image keyframe active at ``timestamp_seconds``."""
    scenes = correction.get("scene_keyframes")
    if not isinstance(scenes, list) or not scenes:
        return correction
    selected: dict[str, object] | None = None
    for item in scenes:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("time_seconds") or 0.0)
        except (TypeError, ValueError):
            continue
        if start <= float(timestamp_seconds) + 1e-6:
            selected = item
        else:
            break
    if selected is None:
        return correction
    merged = dict(correction)
    # Analysis profiles retain the serializer-ready values beneath ``native``
    # while API/HSP inspection payloads are already flattened. Support both so
    # the comparator always grades with the exact active scene values.
    native = selected.get("native") if isinstance(selected, dict) else None
    if isinstance(native, dict):
        merged.update(native)
    else:
        merged.update(selected)
    # Do not recursively carry the timeline into the selected frame payload.
    merged.pop("scene_keyframes", None)
    merged.pop("native", None)
    return merged


def apply_correction_preview(frame_u8: np.ndarray, correction: dict[str, object]) -> np.ndarray:
    """Render a restrained monitor approximation of the native HereSphere grade.

    The proven v7 vector controls follow Unreal-style ordering: independent RGB
    components combine with the fourth/global component. Temperature/tint uses
    Bradford chromatic adaptation. The exact HereSphere transfer functions are
    private, so the preview favors conservative, reversible approximations.

    Shadows/Midtones/Highlights remain intentionally damped because the supplied
    v7 layout establishes their storage and neutral values, but not their exact
    HereSphere transfer functions. The static image should communicate direction
    without making an unverified approximation look more extreme than native VR.
    """
    encoded = np.asarray(frame_u8, dtype=np.float64) / 255.0

    def number(name: str, default: float) -> float:
        try:
            value = float(correction.get(name, default))
        except (TypeError, ValueError):
            return float(default)
        return value if np.isfinite(value) else float(default)

    def triplet(prefix: str, neutral: float) -> np.ndarray:
        return np.asarray([
            number(f"{prefix}_r", neutral),
            number(f"{prefix}_g", neutral),
            number(f"{prefix}_b", neutral),
        ], dtype=np.float64)

    gains = np.clip(triplet("gain", 1.0), 0.5, 1.5)
    offsets = np.clip(triplet("offset", 0.0), -0.20, 0.20)
    contrasts = np.clip(triplet("contrast", 1.0), 0.50, 1.50)
    gammas = np.clip(triplet("gamma", 1.0), 0.50, 1.50)
    saturations = np.clip(triplet("saturation", 1.0), 0.0, 2.0)
    brightness = np.clip(number("offset", number("brightness", 0.0)), -0.20, 0.20)
    contrast = np.clip(number("contrast", 1.0), 0.50, 1.75)
    gamma = np.clip(number("gamma", 1.0), 0.50, 1.75)
    saturation = np.clip(number("saturation", 1.0), 0.0, 2.0)
    exposure = np.clip(number("exposure_bias", 0.0), -2.0, 2.0)
    white_temp = np.clip(number("white_temp", 6500.0), 1000.0, 15_000.0)
    white_tint = np.clip(number("white_tint", 0.0), -1.0, 1.0)
    shadows = np.clip(number("shadows", 0.0), -1.0, 1.0)
    midtones = np.clip(number("midtones", 1.0), 0.0, 2.0)
    highlights = np.clip(number("highlights", 1.0), 0.0, 2.0)
    sharpen = np.clip(number("sharpen", 2.0), 0.0, 5.0)

    # Apply a restrained detail pass before the nonlinear grade. This is a
    # monitor approximation only; serialization order is not treated as proof
    # of HereSphere's internal render order.
    encoded_work = np.asarray(encoded, dtype=np.float32)
    if cv2 is not None and abs(sharpen - 2.0) > 0.015:
        blur = cv2.GaussianBlur(encoded_work, (0, 0), 0.9)
        amount = max(-0.18, min(0.65, (sharpen - 2.0) * PREVIEW_SHARPEN_STRENGTH))
        encoded_work = np.clip(encoded_work + (encoded_work - blur) * amount, 0.0, 1.0)

    # Keep the preview pipeline honest: apply the full HSP approximation at
    # full strength instead of damping controls merely to make the monitor view
    # look prettier. HereSphere remains the authority, but the preview should
    # expose the same problems rather than hide them.
    work = srgb_to_linear(np.clip(encoded_work, 0.0, 1.0))
    preview_temp = 6500.0 + (white_temp - 6500.0) * PREVIEW_WHITEPOINT_STRENGTH
    preview_tint = white_tint * PREVIEW_WHITEPOINT_STRENGTH
    work = apply_white_balance_linear(work, preview_temp, preview_tint)
    work = np.maximum(work * (2.0 ** exposure), 0.0)

    luma = (0.2126 * work[..., 0] + 0.7152 * work[..., 1] + 0.0722 * work[..., 2])[..., None]
    saturation_vector = saturations * saturation
    saturation_vector = 1.0 + (saturation_vector - 1.0) * PREVIEW_NONLINEAR_STRENGTH
    work = np.maximum(0.0, luma + (work - luma) * saturation_vector.reshape(1, 1, 3))

    contrast_vector = contrasts * contrast
    contrast_vector = 1.0 + (contrast_vector - 1.0) * PREVIEW_NONLINEAR_STRENGTH
    pivot = 0.18
    work = np.maximum(0.0, pivot + (work - pivot) * contrast_vector.reshape(1, 1, 3))

    gamma_vector = gammas * gamma
    gamma_vector = 1.0 + (gamma_vector - 1.0) * PREVIEW_NONLINEAR_STRENGTH
    work = np.power(np.clip(work, 0.0, None), 1.0 / np.maximum(gamma_vector.reshape(1, 1, 3), 0.05))
    work = work * gains.reshape(1, 1, 3) + offsets.reshape(1, 1, 3) + brightness

    luma = (0.2126 * work[..., 0] + 0.7152 * work[..., 1] + 0.0722 * work[..., 2])[..., None]
    shadow_weight = np.power(np.clip(1.0 - luma, 0.0, 1.0), 3.0)
    highlight_weight = np.power(np.clip(luma, 0.0, 1.0), 3.0)
    mid_weight = np.clip(1.0 - np.abs(luma - 0.5) * 2.0, 0.0, 1.0) ** 2
    regional_delta = (
        shadows * shadow_weight
        + (midtones - 1.0) * mid_weight
        + (highlights - 1.0) * highlight_weight
    ) * PREVIEW_REGIONAL_TONE_STRENGTH
    work = np.maximum(work * (1.0 + regional_delta), 0.0)

    return np.round(np.clip(linear_to_srgb(work), 0.0, 1.0) * 255.0).astype(np.uint8)

def before_after_jpeg(
    path: Path,
    ffmpeg: str,
    metadata: dict[str, object],
    correction: dict[str, object] | None,
    after: bool,
    timeout_seconds: float = 20.0,
) -> bytes:
    duration = float(metadata.get("duration") or 0.0)
    timestamp = _comparison_preview_timestamp(correction, duration)
    frame = preview_frame(
        path,
        ffmpeg,
        metadata,
        "final",
        timestamp_seconds=timestamp,
        card_width=PREVIEW_CARD_WIDTH,
        card_height=PREVIEW_CARD_HEIGHT,
        timeout_seconds=timeout_seconds,
    )
    if after and correction:
        frame = apply_correction_preview(frame, _scene_correction_at(correction, timestamp))
    return _jpeg_bytes(frame, PREVIEW_JPEG_QUALITY)
