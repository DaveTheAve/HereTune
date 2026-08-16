from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import hashlib
import json
import math
import mimetypes
import re
import secrets
import shutil
import statistics
import subprocess
import sys
import time
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles

from . import __version__
from .accel import gpu_telemetry, resolve_execution_mode, resource_worker_plan
from .analyzer import ANALYSIS_VERSION, CorrectionProfile, OPENCV_NOTE, blend_log, cv2, effective_sample_count
from .db import DB
from .config import load_config, public_config, save_config_patch, analysis_presets, library_roots, primary_library_root, scheduler_library_root
from .hsp import (
    HSPError,
    SUPPORTED_HSP_VERSION,
    alignment_from_metadata,
    audio_from_metadata,
    autofocus_from_metadata,
    build as build_hsp,
    format_from_metadata,
    lens_from_metadata,
    orientation_from_metadata,
    stitch_from_metadata,
    sync_from_metadata,
    inspect as inspect_hsp,
    validate as validate_hsp,
    validate_playback_safe,
    version as hsp_version,
)
from .library import Library
from .hsp_policy import (
    AUTO_IMAGE_CONTROLS,
    apply_auto_image_policy,
    normalize_policy_overrides,
    policy_payload,
    policy_signature,
)
from .worker import THUMBNAIL_LAYOUT_VERSION, effective_metadata, ensure_thumbnail, native_calibration_metadata, probe_metadata, process, thumbnail_browse_metadata, thumbnail_is_current
from .isolation import run_isolated_process
from .analysis_cache import AnalysisCache
from .processes import active_process_count, release_process, run_capture, start_process, terminate_all_processes, terminate_process
from .live import LiveHub
from .media import PREVIEW_RENDER_VERSION, before_after_jpeg, preview_jpeg, preview_frame
from .sidecar import (
    SidecarConflict,
    SidecarError,
    inspect_status as sidecar_status,
    remove_managed_sidecar,
    write_sidecar,
)
from .vrformat import detect as detect_vrformat

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
_PROFILE_MODES = {"original", "mild", "auto", "strong", "manual"}
_PROFILE_STRENGTH = {"original": 0.0, "mild": 0.70, "auto": 1.0, "strong": 1.30}
RatingFilter = Literal[
    "all", "5", "4.5plus", "4plus", "3.5plus", "3plus", "2.5plus",
    "2plus", "1.5plus", "1plus", "0.5plus", "rated", "unrated",
]
# Isolation-lab combinations receive their own tokenized scene ID so HereSphere
# cannot accidentally reuse player-local settings from another test.
_HSP_DIAGNOSTIC_KEY = "__hsp_image_diagnostic"
_HSP_DIAGNOSTIC_CONTROLS = {
    "sharpen",
    "exposure",
    "white_temp",
    "white_tint",
    "saturation_rgb",
    "saturation_global",
    "contrast_rgb",
    "contrast_global",
    "gamma_rgb",
    "gamma_global",
    "gain_rgb",
    "offset_rgb",
    "offset_global",
    "shadows",
    "midtones",
    "highlights",
    "scene_keyframes",
}

# HereSphere eventServer close notifications are the strongest signal that it is
# safe to replace a live HSP/sidecar. A stale open record is eventually expired so
# one missing close event cannot freeze a profile forever after a headset reboot.
_ACTIVE_PLAYBACK_TTL_SECONDS = 12 * 60 * 60


def _active_playback_ids(app) -> set[str]:
    active = getattr(app.state, "active_playback", {})
    now_mono = time.monotonic()
    result: set[str] = set()
    for raw_id, raw_stamp in list(active.items()):
        video_id = str(raw_id)
        try:
            stamp = float(raw_stamp)
        except (TypeError, ValueError):
            active.pop(raw_id, None)
            continue
        if now_mono - stamp > _ACTIVE_PLAYBACK_TTL_SECONDS:
            active.pop(raw_id, None)
            continue
        result.add(video_id)
    return result


def _video_is_active(app, video_id: object) -> bool:
    return str(video_id) in _active_playback_ids(app)


_HSP_WRITEBACK_IDLE_SUPPRESS_SECONDS = 30.0

def _suppress_hsp_reload_after_writeback(app, video_id: object) -> None:
    """Prevent an imported HSP from being replaced again in the same live mutation."""
    active = _video_is_active(app, video_id)
    ttl = _ACTIVE_PLAYBACK_TTL_SECONDS if active else _HSP_WRITEBACK_IDLE_SUPPRESS_SECONDS
    app.state.hsp_reload_suppressed_until[str(video_id)] = time.monotonic() + ttl


def _hsp_reload_suppressed(app, video_id: object) -> bool:
    mapping = getattr(app.state, "hsp_reload_suppressed_until", {})
    key = str(video_id)
    try:
        deadline = float(mapping.get(key) or 0.0)
    except (TypeError, ValueError):
        mapping.pop(key, None)
        return False
    if deadline <= time.monotonic():
        mapping.pop(key, None)
        return False
    return True


def _allow_player_hsp_advertisement(
    app,
    video_id: object,
    revision: str | None,
    *,
    needs_media: bool,
) -> bool:
    """Advertise one player-facing HSP per active playback session.

    HereSphere may revisit the video-data endpoint while a movie is already
    open, including around a local HSP import. Re-advertising the server Auto HSP
    during that live mutation can provoke a second native profile replacement.
    Metadata/library reads never receive HSP information. HereSphere can walk
    hundreds of video-data URLs while merely browsing the Web API library;
    advertising HSPs there makes the player parse profiles before playback and
    gives a bad/stale profile a chance to crash the browsing UI. Once a playback
    response has advertised a revision, further active-session requests omit it
    until the eventServer close notification.
    """
    if not needs_media:
        return False
    if _hsp_reload_suppressed(app, video_id):
        return False
    key = str(video_id)
    if not _video_is_active(app, key):
        return True
    advertised = getattr(app.state, "playback_hsp_advertised", {})
    if key in advertised:
        return False
    # If an eventServer open arrived before video-data, allow the one actual
    # playback request to carry the profile. A metadata-only poll must not install
    # a profile into an already active player.
    return bool(needs_media)


def _record_player_hsp_advertisement(
    app,
    video_id: object,
    revision: str | None,
) -> None:
    if revision:
        app.state.playback_hsp_advertised[str(video_id)] = {
            "revision": str(revision),
            "time": time.monotonic(),
        }


def _normalize_half_star_rating(value: object, *, allow_none: bool = True) -> float | None:
    """Validate HereSphere's 0.5-star rating scale without silent rounding."""
    if value is None and allow_none:
        return None
    try:
        rating = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("rating must be null or a number from 0 to 5") from exc
    if not math.isfinite(rating) or not 0.0 <= rating <= 5.0:
        raise ValueError("rating must be between 0 and 5")
    half_steps = round(rating * 2.0)
    normalized = half_steps / 2.0
    # JSON/float round-trips can produce a tiny amount of binary noise around a
    # genuine half-star. Accept that noise, but reject values such as 4.3 rather
    # than silently changing the user's rating.
    if abs(rating - normalized) > 1e-4:
        raise ValueError("rating must use 0.5-star increments")
    return 0.0 if normalized == 0.0 else normalized


def _apply_rating_filter(items: list[dict[str, object]], rating: str) -> list[dict[str, object]]:
    if rating == "unrated":
        return [item for item in items if item.get("rating") is None]
    if rating == "rated":
        return [item for item in items if item.get("rating") is not None]
    thresholds = {
        "5": 5.0,
        "4.5plus": 4.5,
        "4plus": 4.0,
        "3.5plus": 3.5,
        "3plus": 3.0,
        "2.5plus": 2.5,
        "2plus": 2.0,
        "1.5plus": 1.5,
        "1plus": 1.0,
        "0.5plus": 0.5,
    }
    threshold = thresholds.get(rating)
    if threshold is None:
        return items
    return [
        item for item in items
        if item.get("rating") is not None and float(item["rating"]) >= threshold
    ]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _daemon_to_thread(func, /, *args, **kwargs):
    """Run blocking work in a daemon thread so shutdown never waits on it forever."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def resolve(value=None, error=None):
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(value)

    def target():
        try:
            value = func(*args, **kwargs)
        except BaseException as exc:  # propagate ordinary worker failures
            try:
                loop.call_soon_threadsafe(resolve, None, exc)
            except RuntimeError:
                pass
        else:
            try:
                loop.call_soon_threadsafe(resolve, value, None)
            except RuntimeError:
                pass

    threading.Thread(target=target, daemon=True, name=f"heretune-{getattr(func, '__name__', 'worker')}").start()
    return await future


def hs_json(data, status: int = 200) -> JSONResponse:
    return JSONResponse(
        data,
        status_code=status,
        headers={"HereSphere-JSON-Version": "1", "Cache-Control": "no-store"},
    )


def public_base(request: Request, cfg) -> str:
    if cfg.server.public_base_url:
        return cfg.server.public_base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def _hsp_diagnostic_state(video: dict[str, object]) -> dict[str, object]:
    """Return the non-destructive per-video Image isolation state.

    Diagnostic selections are kept inside the existing per-video override JSON.
    Native calibration code ignores this diagnostic-only key; only the HSP-serving
    path consumes it.
    """
    override = video.get("layout_override") or {}
    raw = override.get(_HSP_DIAGNOSTIC_KEY) if isinstance(override, dict) else None
    disabled: list[str] = []
    if isinstance(raw, dict):
        values = raw.get("disabled")
        if isinstance(values, (list, tuple)):
            disabled = sorted({
                str(value) for value in values
                if str(value) in _HSP_DIAGNOSTIC_CONTROLS
            })
    payload = ",".join(disabled)
    token = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10] if payload else "baseline"
    return {
        "enabled": bool(disabled),
        "disabled": disabled,
        "token": token,
        "available": sorted(_HSP_DIAGNOSTIC_CONTROLS),
    }


def _heresphere_video_path(video: dict[str, object]) -> str:
    """Return the stable public Web API scene identity.

    HereSphere persists video settings against the scene/identifier URL. Keeping
    the real basename in that identity preserves filename suffix recognition
    (_LR, _180, _MKX200, etc.) as an independent player hint. Diagnostic HSP
    combinations receive their own tokenized identity to avoid cache crossover.
    """
    source_name = Path(str(video.get("path") or video.get("rel_path") or video.get("title") or "video.mp4")).name
    diagnostic = _hsp_diagnostic_state(video)
    if diagnostic["enabled"]:
        return (
            f"/heresphere/diag/{diagnostic['token']}/video/"
            f"{video['id']}/{quote(source_name, safe='')}"
        )
    return (
        f"/heresphere/video/"
        f"{video['id']}/{quote(source_name, safe='')}"
    )


def _source_filename(video: dict[str, object]) -> str:
    return Path(str(video.get("path") or video.get("rel_path") or video.get("title") or "video.mp4")).name


def _override_value(override: dict[str, object], key: str) -> bool:
    value = override.get(key)
    return value is not None and str(value).lower() != "auto"


def _heresphere_metadata(video: dict[str, object]) -> dict[str, object]:
    """Resolve HereSphere geometry from current basename + explicit overrides.

    Strong filename conventions are re-evaluated from the current basename every
    time HereSphere asks for metadata, so renames are reflected immediately. User
    overrides stay above filename evidence; analyzed visual metadata is the fallback.
    """
    stored = dict(video.get("metadata") or {})
    resolved = effective_metadata(video, stored)
    override = dict(video.get("layout_override") or {})
    detected = detect_vrformat(
        _source_filename(video),
        int(stored.get("width") or 0),
        int(stored.get("height") or 0),
    )
    sources: list[str] = []

    if bool(detected.get("layout_filename_explicit_stereo")):
        for key in ("stereo", "isEyeSwapped", "full_side_by_side"):
            if not _override_value(override, key) and key in detected:
                resolved[key] = detected[key]
        sources.append("filename-stereo")

    if bool(detected.get("layout_filename_explicit_projection")):
        for key in ("projection", "fov", "lens"):
            if not _override_value(override, key) and key in detected:
                resolved[key] = detected[key]
        sources.append("filename-projection")

    active_override = [
        key for key in ("stereo", "projection", "lens", "fov", "isEyeSwapped", "full_side_by_side")
        if _override_value(override, key)
    ]
    if active_override:
        sources.insert(0, "override:" + ",".join(active_override))

    if not sources:
        detection = stored.get("layout_detection") or {}
        try:
            confidence = float(detection.get("confidence") or 0.0) if isinstance(detection, dict) else 0.0
        except (TypeError, ValueError):
            confidence = 0.0
        sources.append("visual/cached" if confidence >= 0.50 else "cached/native-auto")

    resolved["heresphere_geometry_source"] = "+".join(sources)
    resolved["heresphere_filename_detection"] = detected
    return resolved


async def request_json(request: Request) -> dict[str, object]:
    """Parse HereSphere/API mutation bodies without silently losing diagnostics.

    HereSphere's documented Web API uses JSON. URL-encoded form bodies are also
    accepted because native write-back traffic has been observed in that form.
    Starlette caches request.body(), so callers may safely inspect the raw body
    again for wire-level diagnostics without consuming the stream twice.
    """
    if request.method not in {"POST", "PUT", "PATCH"}:
        return {}
    body = await request.body()
    if not body:
        return {}
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type == "application/x-www-form-urlencoded":
            try:
                parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=False)
            except UnicodeDecodeError:
                return {}
            return {key: values[-1] if values else "" for key, values in parsed.items()}
        return {}
    return value if isinstance(value, dict) else {}


def _heresphere_post_diagnostic(request: Request, body: dict[str, object], raw_body: bytes) -> str:
    """Return a safe one-line summary; never dump HSP Base64 or credentials."""
    content_type = request.headers.get("content-type", "-").split(";", 1)[0] or "-"
    keys = ",".join(sorted(str(key) for key in body.keys())) if body else "-"
    if raw_body and not body:
        parse_state = "UNPARSED"
    elif raw_body:
        parse_state = "parsed"
    else:
        parse_state = "empty"
    return (
        f"method={request.method} path={request.url.path} type={content_type} "
        f"bytes={len(raw_body)} {parse_state} keys={keys} "
        f"hsp={'yes' if 'hsp' in body else 'no'}"
    )


def auth_token(cfg) -> str:
    return cfg.server.auth_token or "heretune-local"


def authorized(request: Request, body: dict[str, object], cfg) -> bool:
    if not cfg.server.username and not cfg.server.password:
        return True
    supplied_token = request.headers.get("auth-token", "")
    if supplied_token and supplied_token == auth_token(cfg):
        return True
    return (
        str(body.get("username") or "") == cfg.server.username
        and str(body.get("password") or "") == cfg.server.password
    )


def stream_file(path: Path, start: int, end: int, chunk_size: int):
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def parse_byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    if not header:
        return None
    match = _RANGE.fullmatch(header.strip())
    if not match:
        raise ValueError("Malformed Range header")
    first, last = match.groups()
    if not first and not last:
        raise ValueError("Empty Range header")
    if not first:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("Invalid suffix range")
        return max(0, size - suffix), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start < 0 or start >= size or end < start:
        raise ValueError("Range is outside the file")
    return start, min(end, size - 1)



def _media_response(video: dict[str, object], request: Request, cfg) -> Response:
    path = Path(str(video["path"]))
    try:
        stat = path.stat()
        size = stat.st_size
    except OSError as exc:
        raise HTTPException(404, "Source file is unavailable") from exc
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    etag = f'"{size:x}-{stat.st_mtime_ns:x}"'
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(size),
        "ETag": etag,
        "Last-Modified": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        "Content-Disposition": f'inline; filename="{path.name.replace(chr(34), "_")}"',
        "X-HereTune-Original": "1",
        "X-HereTune-Transcoded": "0",
    }
    if request.method == "HEAD":
        return Response(headers=headers, media_type=media_type)
    try:
        byte_range = parse_byte_range(request.headers.get("range"), size)
    except (ValueError, OverflowError):
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )
    if byte_range is None:
        return StreamingResponse(
            stream_file(path, 0, size - 1, cfg.server.media_chunk_bytes),
            media_type=media_type,
            headers=headers,
        )
    start, end = byte_range
    headers.update({
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(end - start + 1),
    })
    return StreamingResponse(
        stream_file(path, start, end, cfg.server.media_chunk_bytes),
        status_code=206,
        media_type=media_type,
        headers=headers,
    )


_WEB_NVENC_CACHE: dict[str, bool] = {}
_WEB_SOURCE_NVENC_CACHE: dict[str, bool] = {}

def _web_log(video_id: str, message: str) -> None:
    print(f"[HereTune Web] {video_id}: {message}", flush=True)



def _web_nvenc_works(ffmpeg: str) -> bool:
    """Return whether h264_nvenc can actually open, not merely whether it is listed."""
    cached = _WEB_NVENC_CACHE.get(ffmpeg)
    if cached is not None:
        return cached
    try:
        proc = run_capture([
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=black:size=64x64:rate=1:duration=0.05",
            "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-",
        ], timeout=12.0)
        ok = proc.returncode == 0
    except Exception:
        ok = False
    _WEB_NVENC_CACHE[ffmpeg] = ok
    return ok


def _web_source_nvenc_works(video: dict[str, object], cfg) -> bool:
    """Preflight NVENC against the actual source, not only a synthetic 64x64 frame."""
    path = Path(str(video["path"]))
    metadata = video.get("metadata") or {}
    try:
        stat = path.stat()
        key = f"{path}|{stat.st_size}|{stat.st_mtime_ns}|{int(metadata.get('width') or 0)}"
    except OSError:
        key = str(path)
    cached = _WEB_SOURCE_NVENC_CACHE.get(key)
    if cached is not None:
        return cached
    if not _web_nvenc_works(cfg.paths.ffmpeg):
        _WEB_SOURCE_NVENC_CACHE[key] = False
        return False
    width = int(metadata.get("width") or 0)
    command = [cfg.paths.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", "0.5", "-i", str(path), "-map", "0:v:0", "-frames:v", "1"]
    if width > 3840:
        command += ["-vf", "scale=3840:-2:flags=lanczos"]
    command += ["-pix_fmt", "yuv420p", "-c:v", "h264_nvenc", "-preset", "p5", "-f", "null", "-"]
    try:
        proc = run_capture(command, timeout=25.0)
        ok = proc.returncode == 0
        if not ok:
            detail = (proc.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()[-1:]
            _web_log(str(video.get("id") or path.name), "NVENC source preflight failed" + (f": {detail[0]}" if detail else ""))
    except Exception as exc:
        ok = False
        _web_log(str(video.get("id") or path.name), f"NVENC source preflight exception: {exc}")
    _WEB_SOURCE_NVENC_CACHE[key] = ok
    return ok


def _web_compat_command(video: dict[str, object], cfg, *, mode: str, start: float) -> tuple[list[str], str]:
    """Build an FFmpeg command for browser compatibility streaming.

    mode=remux keeps video bytes untouched when practical and repackages them as
    fragmented MP4. mode=transcode is the robust fallback: high-quality H.264/AAC,
    capped at 3840px wide so extremely wide 5.7K+ VR sources remain browser-friendly
    while still retaining enough pixels for the one-eye/WebGL 2D view.
    """
    path = Path(str(video["path"]))
    metadata = video.get("metadata") or {}
    codec = str(metadata.get("video_codec") or "").casefold()
    width = int(metadata.get("width") or 0)
    command = [cfg.paths.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin"]
    if start > 0:
        command += ["-ss", f"{start:.3f}"]
    command += ["-i", str(path), "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]

    if mode == "remux" and codec in {"h264", "hevc", "h265"}:
        command += ["-c:v", "copy"]
        if codec in {"hevc", "h265"}:
            command += ["-tag:v", "hvc1"]
        # Audio is normalized to AAC because otherwise an otherwise-playable video
        # can still fail in Chrome due to an unsupported MKV/AC3/DTS audio track.
        command += ["-c:a", "aac", "-b:a", "320k"]
        label = "lossless-video remux"
    else:
        if width > 3840:
            command += ["-vf", "scale=3840:-2:flags=lanczos"]
        command += ["-pix_fmt", "yuv420p"]
        if _web_source_nvenc_works(video, cfg):
            command += [
                "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", "18", "-b:v", "0",
                "-maxrate", "80M", "-bufsize", "160M", "-spatial-aq", "1",
            ]
            label = "NVENC H.264 compatibility stream"
        else:
            command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
            label = "CPU H.264 compatibility stream"
        command += ["-c:a", "aac", "-b:a", "320k"]

    command += [
        "-avoid_negative_ts", "make_zero", "-reset_timestamps", "1",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    return command, label


def _stream_ffmpeg(command: list[str], *, video_id: str = "web", label: str = "compatibility stream"):
    stderr_tail: list[str] = []
    bytes_sent = 0
    proc = start_process(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        bufsize=0,
    )
    _web_log(video_id, f"FFmpeg started pid={proc.pid} · {label}")

    def drain_stderr() -> None:
        if proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                stderr_tail.append(line)
                del stderr_tail[:-12]
                lowered = line.casefold()
                if any(word in lowered for word in ("error", "failed", "invalid", "unsupported", "cannot", "could not")):
                    _web_log(video_id, f"FFmpeg: {line}")
        except Exception as exc:
            _web_log(video_id, f"stderr reader stopped: {exc}")

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True, name=f"heretune-web-ffmpeg-{video_id}")
    stderr_thread.start()
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(1024 * 1024)
            if not chunk:
                break
            bytes_sent += len(chunk)
            yield chunk
    finally:
        try:
            return_code = proc.poll()
            if return_code is None:
                try:
                    return_code = proc.wait(timeout=0.75)
                except subprocess.TimeoutExpired:
                    terminate_process(proc, grace=0.3)
                    return_code = proc.poll()
            stderr_thread.join(timeout=0.25)
            if bytes_sent == 0 or (return_code not in (0, None)):
                tail = " | ".join(stderr_tail[-4:]) or "no FFmpeg stderr captured"
                _web_log(video_id, f"FFmpeg ended rc={return_code} · bytes={bytes_sent} · {tail}")
            else:
                _web_log(video_id, f"FFmpeg stream ended rc={return_code} · bytes={bytes_sent}")
        finally:
            release_process(proc)

def _resolution(metadata: dict[str, object]) -> int:
    height = int(metadata.get("height") or 0)
    return height // 2 if metadata.get("stereo") == "tb" else height


def _safe_delete_source(video: dict[str, object], cfg) -> None:
    path = Path(str(video["path"]))
    try:
        resolved_path = path.resolve(strict=True)
    except OSError as exc:
        raise HTTPException(404, "Source file is unavailable") from exc
    inside_library = False
    for root_text in library_roots(cfg):
        try:
            resolved_path.relative_to(Path(root_text).resolve(strict=True))
            inside_library = True
            break
        except (OSError, ValueError):
            continue
    if not inside_library:
        raise HTTPException(400, "Refusing to delete a source outside the configured library folders")
    if not resolved_path.is_file():
        raise HTTPException(404, "Source file is unavailable")
    try:
        resolved_path.unlink()
    except OSError as exc:
        raise HTTPException(500, f"Could not delete source file: {exc}") from exc


def _parse_timestamp(value: object) -> datetime:
    text = str(value or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    # A fixed fallback keeps generated variant bytes stable instead of producing
    # a new URL revision every time HereSphere asks for metadata.
    return datetime(2025, 1, 1, tzinfo=timezone.utc)


def _correction_from_analysis(analysis: dict[str, object]) -> CorrectionProfile:
    correction = CorrectionProfile()
    for item in fields(CorrectionProfile):
        if item.name not in analysis:
            continue
        try:
            setattr(correction, item.name, copy.deepcopy(analysis[item.name]))
        except (TypeError, ValueError):
            continue
    return correction


def _neutralize_native_image_controls(
    native: dict[str, object], disabled: set[str]
) -> dict[str, object]:
    """Neutralize only explicitly disabled native Image controls.

    The isolation lab is intentionally surgical: controls not named in
    ``disabled`` retain the values produced by the current Auto policy.
    """
    result = copy.deepcopy(native)

    if "sharpen" in disabled:
        result["sharpen"] = 2.0
    if "exposure" in disabled:
        result["exposure_bias"] = 0.0
    if "white_temp" in disabled:
        result["white_temp"] = 6500.0
    if "white_tint" in disabled:
        result["white_tint"] = 0.0
    if "shadows" in disabled:
        result["shadows"] = 0.0
    if "midtones" in disabled:
        result["midtones"] = 1.0
    if "highlights" in disabled:
        result["highlights"] = 1.0

    vector_rules = (
        ("saturation", "saturation_rgb", "saturation_global", 1.0),
        ("contrast", "contrast_rgb", "contrast_global", 1.0),
        ("gamma", "gamma_rgb", "gamma_global", 1.0),
        ("offset", "offset_rgb", "offset_global", 0.0),
    )
    for name, rgb_key, global_key, neutral in vector_rules:
        raw = result.get(name)
        try:
            values = [float(value) for value in raw]  # type: ignore[arg-type]
        except (TypeError, ValueError):
            values = [neutral, neutral, neutral, neutral]
        if len(values) != 4:
            values = [neutral, neutral, neutral, neutral]
        if rgb_key in disabled:
            values[0:3] = [neutral, neutral, neutral]
        if global_key in disabled:
            values[3] = neutral
        result[name] = tuple(values)

    if "gain_rgb" in disabled:
        raw = result.get("gain")
        try:
            values = [float(value) for value in raw]  # type: ignore[arg-type]
        except (TypeError, ValueError):
            values = [1.0, 1.0, 1.0, 1.0]
        if len(values) != 4:
            values = [1.0, 1.0, 1.0, 1.0]
        values[0:3] = [1.0, 1.0, 1.0]
        result["gain"] = tuple(values)

    return result


def _diagnostic_correction(
    analysis: dict[str, object], disabled_values: list[str] | tuple[str, ...] | set[str]
) -> CorrectionProfile:
    """Create a correction with only selected controls forced to native neutral."""
    disabled = {str(value) for value in disabled_values if str(value) in _HSP_DIAGNOSTIC_CONTROLS}
    correction = copy.deepcopy(_correction_from_analysis(analysis))

    if "sharpen" in disabled:
        correction.sharpen = 2.0
    if "exposure" in disabled:
        correction.exposure_bias = 0.0
    if "white_temp" in disabled:
        correction.white_temp = 6500.0
    if "white_tint" in disabled:
        correction.white_tint = 0.0
    if "saturation_global" in disabled:
        correction.saturation = 1.0
    if "saturation_rgb" in disabled:
        correction.saturation_r = correction.saturation_g = correction.saturation_b = 1.0
    if "contrast_global" in disabled:
        correction.contrast = 1.0
    if "contrast_rgb" in disabled:
        correction.contrast_r = correction.contrast_g = correction.contrast_b = 1.0
    if "gamma_global" in disabled:
        correction.gamma = 1.0
    if "gamma_rgb" in disabled:
        correction.gamma_r = correction.gamma_g = correction.gamma_b = 1.0
    if "gain_rgb" in disabled:
        correction.r_gain = correction.g_gain = correction.b_gain = 1.0
    if "offset_rgb" in disabled:
        correction.offset_r = correction.offset_g = correction.offset_b = 0.0
    if "offset_global" in disabled:
        correction.brightness = 0.0
    if "shadows" in disabled:
        correction.shadows = 0.0
    if "midtones" in disabled:
        correction.midtones = 1.0
    if "highlights" in disabled:
        correction.highlights = 1.0

    if "scene_keyframes" in disabled:
        correction.scene_keyframes = []
    else:
        scenes: list[dict[str, object]] = []
        for item in list(correction.scene_keyframes or []):
            if not isinstance(item, dict):
                continue
            cloned = copy.deepcopy(item)
            native = cloned.get("native")
            if isinstance(native, dict):
                cloned["native"] = _neutralize_native_image_controls(native, disabled)
            scenes.append(cloned)
        correction.scene_keyframes = scenes
    return correction


def _scale_native_image(native: dict[str, object], strength: float) -> dict[str, object]:
    """Blend one complete native Image mapping toward HereSphere neutral."""
    strength = max(0.0, min(1.5, float(strength)))

    def scalar(name: str, neutral: float, logarithmic: bool = False) -> float:
        try:
            value = float(native.get(name, neutral))
        except (TypeError, ValueError):
            value = neutral
        return blend_log(value, strength) if logarithmic else neutral + (value - neutral) * strength

    def vector(name: str, neutral: tuple[float, float, float, float], logarithmic: bool = False):
        raw = native.get(name, neutral)
        try:
            values = tuple(float(item) for item in raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            values = neutral
        if len(values) != 4:
            values = neutral
        return tuple(
            blend_log(value, strength) if logarithmic else base + (value - base) * strength
            for value, base in zip(values, neutral)
        )

    return {
        "sharpen": scalar("sharpen", 2.0),
        "exposure_bias": scalar("exposure_bias", 0.0),
        "white_temp": scalar("white_temp", 6500.0),
        "white_tint": scalar("white_tint", 0.0),
        "saturation": vector("saturation", (1.0, 1.0, 1.0, 1.0), True),
        "contrast": vector("contrast", (1.0, 1.0, 1.0, 1.0), True),
        "gamma": vector("gamma", (1.0, 1.0, 1.0, 1.0), True),
        "gain": vector("gain", (1.0, 1.0, 1.0, 1.0), True),
        "offset": vector("offset", (0.0, 0.0, 0.0, 0.0), False),
        "shadows": scalar("shadows", 0.0),
        "midtones": scalar("midtones", 1.0),
        "highlights": scalar("highlights", 1.0),
    }


def _scaled_correction(analysis: dict[str, object], strength: float) -> CorrectionProfile:
    correction = _correction_from_analysis(analysis)
    strength = max(0.0, min(1.5, float(strength)))
    for name in (
        "r_gain", "g_gain", "b_gain", "contrast", "gamma", "saturation",
        "saturation_r", "saturation_g", "saturation_b",
        "contrast_r", "contrast_g", "contrast_b",
        "gamma_r", "gamma_g", "gamma_b",
    ):
        setattr(correction, name, blend_log(float(getattr(correction, name)), strength))
    for name in ("brightness", "offset_r", "offset_g", "offset_b"):
        setattr(correction, name, float(getattr(correction, name)) * strength)
    correction.sharpen = 2.0 + (float(correction.sharpen) - 2.0) * strength
    correction.exposure_bias = float(correction.exposure_bias) * strength
    correction.white_temp = 6500.0 + (float(correction.white_temp) - 6500.0) * strength
    correction.white_tint = float(correction.white_tint) * strength
    correction.shadows = float(correction.shadows) * strength
    correction.midtones = 1.0 + (float(correction.midtones) - 1.0) * strength
    correction.highlights = 1.0 + (float(correction.highlights) - 1.0) * strength
    correction.audio_volume_multiplier = blend_log(
        float(correction.audio_volume_multiplier or 1.0), strength
    )
    correction.audio_time_offset = float(correction.audio_time_offset or 0.0) * strength
    correction.sync_drift_seconds_per_hour = float(
        correction.sync_drift_seconds_per_hour or 0.0
    ) * strength

    scenes: list[dict[str, object]] = []
    for item in list(correction.scene_keyframes or []):
        if not isinstance(item, dict) or not isinstance(item.get("native"), dict):
            continue
        scaled = dict(item)
        scaled["native"] = _scale_native_image(dict(item["native"]), strength)
        scenes.append(scaled)
    correction.scene_keyframes = scenes
    return correction


def _profile_mode(video: dict[str, object]) -> str:
    value = str(video.get("profile_mode") or "auto").lower()
    return value if value in _PROFILE_MODES else "auto"


def _profile_blob(
    video: dict[str, object],
    cfg,
    requested_mode: str | None = None,
) -> tuple[str | None, bytes | None, int | None]:
    mode = (requested_mode or _profile_mode(video)).lower()
    if mode not in _PROFILE_MODES:
        mode = "auto"

    if mode == "manual":
        manual = video.get("manual_hsp")
        if manual is not None:
            detected = hsp_version(manual)
            return "manual", manual, int(detected if detected is not None else (video.get("manual_hsp_version") or 0))
        mode = "auto"

    if mode == "auto":
        diagnostic = _hsp_diagnostic_state(video)
        if diagnostic["enabled"]:
            diagnostic_blob = _rebuild_auto_hsp(
                video,
                cfg,
                disabled_controls=list(diagnostic["disabled"]),
                profile_label="Auto isolation test",
            )
            if diagnostic_blob is not None:
                return "auto", diagnostic_blob, int(cfg.hsp.generated_version)
        automatic = video.get("auto_hsp")
        if automatic is not None:
            return "auto", automatic, int(video.get("auto_hsp_version") or 0)
        return None, None, None

    metadata = video.get("metadata") or {}
    analysis = video.get("analysis") or {}
    if mode in {"mild", "strong"} and (not analysis or "reason" in analysis):
        return None, None, None

    strength = _PROFILE_STRENGTH[mode]
    correction = _scaled_correction(analysis, strength)
    correction = apply_auto_image_policy(correction, cfg, video.get("hsp_policy_override"))
    timestamp = _parse_timestamp(video.get("last_analyzed") or video.get("date_added"))
    blob = build_hsp(
        str(video["id"]),
        str(video["title"]),
        float(metadata.get("duration") or 0.0),
        correction,
        hsp_version=int(cfg.hsp.generated_version),
        profile_label=mode.title(),
        timestamp=timestamp,
        metadata=native_calibration_metadata(video, _heresphere_metadata(video), cfg, correction),
        source_filename=_source_filename(video),
    )
    return mode, blob, int(cfg.hsp.generated_version)


def _hsp_revision(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()[:16]


def _video_policy_overrides(video: dict[str, object]) -> dict[str, str]:
    return normalize_policy_overrides(video.get("hsp_policy_override") or {})


def _store_sidecar_result(db: DB, video_id: str, result) -> None:
    db.exec(
        "UPDATE videos SET sidecar_hsp_path=?,sidecar_hsp_revision=?,sidecar_hsp_managed=1 WHERE id=?",
        (str(result.path), str(result.revision), video_id),
    )


def _write_auto_sidecar_for_video(
    db: DB,
    cfg,
    video: dict[str, object],
    *,
    replace: bool = False,
) -> dict[str, object]:
    blob = video.get("auto_hsp")
    if not blob:
        raise SidecarError("This video does not have a completed Auto HSP yet")
    result = write_sidecar(video, bytes(blob), replace=replace)
    _store_sidecar_result(db, str(video["id"]), result)
    return {
        "path": str(result.path),
        "revision": result.revision,
        "status": result.status,
        "backup": str(result.backup) if result.backup else None,
    }


def _rebuild_auto_hsp(
    video: dict[str, object],
    cfg,
    *,
    disabled_controls: list[str] | tuple[str, ...] | set[str] | None = None,
    profile_label: str = "Auto",
) -> bytes | None:
    analysis = video.get("analysis") or {}
    metadata = video.get("metadata") or {}
    if not analysis or "reason" in analysis or not metadata:
        return None
    disabled_set = {str(value) for value in (disabled_controls or set())}
    correction = (
        _diagnostic_correction(analysis, disabled_set)
        if disabled_set
        else _correction_from_analysis(analysis)
    )
    policy_overrides = dict(video.get("hsp_policy_override") or {})
    # The isolation lab is temporary and must be able to neutralize a Global
    # Offset that was explicitly enabled by global/per-video policy.
    if "offset_global" in disabled_set:
        policy_overrides["global_offset"] = "off"
    correction = apply_auto_image_policy(correction, cfg, policy_overrides)
    timestamp = _parse_timestamp(video.get("last_analyzed") or video.get("date_added"))
    return build_hsp(
        str(video["id"]),
        str(video["title"]),
        float(metadata.get("duration") or 0.0),
        correction,
        metadata=native_calibration_metadata(video, _heresphere_metadata(video), cfg, correction),
        source_filename=_source_filename(video),
        hsp_version=int(cfg.hsp.generated_version),
        profile_label=profile_label,
        timestamp=timestamp,
    )


def _rebuild_auto_hsps_for_policy(
    db: DB,
    cfg,
    *,
    skip_ids: set[str] | None = None,
    write_sidecars: bool = True,
) -> dict[str, object]:
    """Reserialize ready Auto HSPs from saved evidence; never decode media."""
    skip = {str(value) for value in (skip_ids or set())}
    rebuilt = 0
    skipped: list[str] = []
    sidecars_written = 0
    sidecar_errors = 0
    rows = db.lightweight_rows(
        "id",
        "status!='missing' AND analysis_json IS NOT NULL AND auto_hsp IS NOT NULL",
    )
    for item in rows:
        video_id = str(item["id"])
        if video_id in skip:
            skipped.append(video_id)
            continue
        video = db.get(video_id)
        if not video:
            continue
        blob = _rebuild_auto_hsp(video, cfg)
        if blob is None:
            continue
        db.exec(
            "UPDATE videos SET auto_hsp=?,auto_hsp_version=? WHERE id=?",
            (blob, int(cfg.hsp.generated_version), video_id),
        )
        rebuilt += 1
        if write_sidecars and bool(getattr(cfg.hsp, "write_auto_sidecars", False)):
            updated = db.get(video_id) or video
            try:
                _write_auto_sidecar_for_video(db, cfg, updated, replace=False)
                sidecars_written += 1
            except (SidecarConflict, SidecarError):
                sidecar_errors += 1
    return {
        "rebuilt": rebuilt,
        "skipped": skipped,
        "sidecars_written": sidecars_written,
        "sidecar_errors": sidecar_errors,
    }


def _sync_auto_sidecars(
    db: DB,
    cfg,
    *,
    skip_ids: set[str] | None = None,
    progress: Callable[[dict[str, object]], None] | None = None,
    is_active: Callable[[str], bool] | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """Synchronize existing Ready Auto HSPs to source-adjacent sidecars.

    The worker is deliberately independent from video analysis. ``progress`` may
    be called from a background thread and therefore must remain thread-safe.
    Unknown/user-owned sidecars are counted as conflicts and never overwritten.
    """
    skip = {str(value) for value in (skip_ids or set())}
    rows = db.lightweight_rows("id", "status='ready' AND auto_hsp IS NOT NULL")
    total = len(rows)
    written = 0
    unchanged = 0
    conflicts = 0
    errors = 0
    processed = 0
    skipped: list[str] = []
    details: list[dict[str, object]] = []
    started = time.monotonic()

    def emit(*, stage: str = "write", current: dict[str, object] | None = None, cancelled: bool = False) -> None:
        if progress is None:
            return
        elapsed = max(0.0, time.monotonic() - started)
        eta = None
        if processed > 0 and total > processed and elapsed > 0:
            eta = (elapsed / processed) * (total - processed)
        progress({
            "kind": "sidecar-sync",
            "stage": stage,
            "total": total,
            "processed": processed,
            "written": written,
            "unchanged": unchanged,
            "conflicts": conflicts,
            "errors": errors,
            "deferred": len(skipped),
            "elapsed_seconds": elapsed,
            "eta_seconds": eta,
            "current_id": str((current or {}).get("id") or "") or None,
            "current_title": str((current or {}).get("title") or "") or None,
            "cancelled": bool(cancelled),
        })

    emit(stage="discover")
    for item in rows:
        if should_continue is not None and not should_continue():
            emit(stage="done", cancelled=True)
            break
        video_id = str(item["id"])
        if video_id in skip or (is_active is not None and is_active(video_id)):
            skipped.append(video_id)
            processed += 1
            emit(current={"id": video_id}, stage="write")
            continue
        video = db.get(video_id)
        if not video:
            processed += 1
            errors += 1
            details.append({"video_id": video_id, "title": video_id, "status": "error", "detail": "Video row disappeared during sidecar synchronization."})
            emit(current={"id": video_id}, stage="write")
            continue
        try:
            result = _write_auto_sidecar_for_video(db, cfg, video, replace=False)
        except SidecarConflict as exc:
            conflicts += 1
            details.append({"video_id": video_id, "title": str(video.get("title") or video_id), "status": "conflict", "detail": str(exc)})
        except SidecarError as exc:
            errors += 1
            details.append({"video_id": video_id, "title": str(video.get("title") or video_id), "status": "error", "detail": str(exc)})
        else:
            if result["status"] == "unchanged":
                unchanged += 1
            else:
                written += 1
        processed += 1
        emit(current=video, stage="write")

    elapsed = max(0.0, time.monotonic() - started)
    cancelled = bool(should_continue is not None and not should_continue())
    result = {
        "total": total,
        "processed": processed,
        "written": written,
        "unchanged": unchanged,
        "conflicts": conflicts,
        "errors": errors,
        "skipped": skipped,
        "deferred": len(skipped),
        "elapsed_seconds": elapsed,
        "cancelled": cancelled,
        "details": details[:200],
        "details_truncated": max(0, len(details) - 200),
    }
    if progress is not None:
        progress({**result, "kind": "sidecar-sync", "stage": "done", "eta_seconds": 0.0})
    return result


def _hsp_geometry_matches(blob: bytes, metadata: dict[str, object]) -> bool:
    """Compare every HereTune-owned non-Image HSP group with desired metadata."""
    try:
        parsed = inspect_hsp(blob)
        required = (
            parsed.format_keyframes, parsed.lens_keyframes, parsed.stitch_keyframes,
            parsed.alignment_keyframes, parsed.orientation_keyframes,
            parsed.autofocus_keyframes, parsed.sync_keyframes, parsed.audio_keyframes,
        )
        if any(not group for group in required):
            return False
        actual_fmt = parsed.format_keyframes[0]
        actual_lens = parsed.lens_keyframes[0]
        actual_stitch = parsed.stitch_keyframes[0]
        actual_alignment = parsed.alignment_keyframes[0]
        actual_orientation = parsed.orientation_keyframes[0]
        actual_autofocus = parsed.autofocus_keyframes[0]
        actual_sync = parsed.sync_keyframes[0]
        actual_audio = parsed.audio_keyframes[0]

        expected_fmt = format_from_metadata(metadata)
        expected_lens = lens_from_metadata(metadata)
        expected_stitch = stitch_from_metadata(metadata)
        expected_alignment = alignment_from_metadata(metadata)
        expected_orientation = orientation_from_metadata(metadata)
        expected_autofocus = autofocus_from_metadata(metadata)
        expected_sync = sync_from_metadata(metadata)
        expected_audio = audio_from_metadata(metadata)

        def close_values(actual, expected, tolerance: float = 1e-4) -> bool:
            return all(abs(float(a) - float(b)) < tolerance for a, b in zip(actual, expected))

        return (
            actual_fmt.projection_mode == expected_fmt.projection_mode
            and actual_fmt.stereo_mode == expected_fmt.stereo_mode
            and actual_fmt.eye_swap_enabled == expected_fmt.eye_swap_enabled
            and actual_fmt.force_mono_enabled == expected_fmt.force_mono_enabled
            and actual_fmt.flip_front_back == expected_fmt.flip_front_back
            and actual_fmt.flip_left_right == expected_fmt.flip_left_right
            and actual_fmt.flip_up_down == expected_fmt.flip_up_down
            and actual_fmt.aspect_ratio_type == expected_fmt.aspect_ratio_type
            and abs(float(actual_fmt.aspect_ratio) - float(expected_fmt.aspect_ratio)) < 1e-5
            and close_values(actual_fmt.zoom, expected_fmt.zoom)
            and close_values(actual_fmt.pan, expected_fmt.pan)
            and actual_fmt.orientation_type == expected_fmt.orientation_type
            and actual_lens.true_lens_name == expected_lens.true_lens_name
            and abs(float(actual_lens.true_fov) - float(expected_lens.true_fov)) < 1e-4
            and close_values(actual_stitch.shift, expected_stitch.shift)
            and close_values(actual_stitch.scale, expected_stitch.scale)
            and close_values(actual_stitch.shear, expected_stitch.shear)
            and close_values(actual_stitch.flare, expected_stitch.flare)
            and close_values(actual_stitch.slant, expected_stitch.slant)
            and close_values(actual_alignment.position, expected_alignment.position)
            and close_values(actual_alignment.rotation, expected_alignment.rotation)
            and close_values(actual_orientation.orientation, expected_orientation.orientation)
            and close_values(actual_autofocus.rotation, expected_autofocus.rotation)
            and abs(float(actual_autofocus.focal_distance_override) - float(expected_autofocus.focal_distance_override)) < 1e-4
            and abs(float(actual_autofocus.min_focus_distance) - float(expected_autofocus.min_focus_distance)) < 1e-4
            and abs(float(actual_autofocus.max_focus_distance) - float(expected_autofocus.max_focus_distance)) < 1e-4
            and abs(float(actual_autofocus.focus_transition_speed) - float(expected_autofocus.focus_transition_speed)) < 1e-4
            and actual_sync.starting_ticks_offset == expected_sync.starting_ticks_offset
            and actual_sync.ticks_per_hour_offset == expected_sync.ticks_per_hour_offset
            and abs(float(actual_audio.time_offset) - float(expected_audio.time_offset)) < 1e-5
            and abs(float(actual_audio.volume_multiplier) - float(expected_audio.volume_multiplier)) < 1e-5
        )
    except Exception:
        return False


def _reconcile_auto_hsps_for_heresphere(db: DB, cfg) -> int:
    """Refresh stale Auto HSP geometry without touching analysis evidence."""
    changed = 0
    for video in db.iter_video_rows("id,path,rel_path,title,size,mtime_ns,status,error,metadata_json,analysis_json,auto_hsp,auto_hsp_version,profile_mode,is_favorite,rating,layout_override_json,hsp_policy_override_json,date_added,last_analyzed"):
        blob = video.get("auto_hsp")
        if not blob or int(video.get("auto_hsp_version") or 0) != int(cfg.hsp.generated_version):
            continue
        desired = native_calibration_metadata(
            video, _heresphere_metadata(video), cfg,
            _correction_from_analysis(video.get("analysis") or {}),
        )
        if _hsp_geometry_matches(bytes(blob), desired):
            continue
        rebuilt = _rebuild_auto_hsp(video, cfg)
        if rebuilt is None:
            continue
        db.exec(
            "UPDATE videos SET auto_hsp=?, auto_hsp_version=? WHERE id=?",
            (rebuilt, int(cfg.hsp.generated_version), video["id"]),
        )
        changed += 1
    return changed


def _confidence(analysis: dict[str, object]) -> float:
    if not analysis or "reason" in analysis:
        return 0.0
    try:
        wb = float(analysis.get("wb_confidence") or 0.0)
        skin = float(analysis.get("skin_confidence") or 0.0)
        sampled = float(analysis.get("sampled_frames") or 0.0)
        requested = float(analysis.get("requested_samples") or sampled or 1.0)
    except (TypeError, ValueError):
        return 0.0
    evidence = max(wb, skin * 0.90)
    coverage = max(0.55, min(1.0, sampled / max(requested, 1.0)))
    return max(0.0, min(1.0, evidence * coverage))


def _wb_method_label(value: object) -> str:
    labels = {
        "face-skin+neutral": "Face-guided + neutral reference",
        "skin-fallback+neutral": "Skin-guided + neutral reference",
        "neutral": "Neutral reference",
    }
    raw = str(value or "neutral").strip()
    return labels.get(raw, raw.replace("-", " ").replace("+", " + ").title())


def _warnings(video: dict[str, object]) -> list[str]:
    analysis = video.get("analysis") or {}
    metadata = video.get("metadata") or {}
    warnings: list[str] = []
    if analysis and "reason" not in analysis:
        if _confidence(analysis) < 0.35:
            warnings.append("Low correction confidence; review this video before using Strong mode.")
        if float(analysis.get("neutral_fraction") or 0.0) < 0.01:
            warnings.append("Very little neutral-color evidence was found; skin and gray-world guardrails carried more weight.")
        if float(analysis.get("clipped_highlight_fraction") or 0.0) >= 0.035:
            warnings.append("A meaningful part of the sampled image is already clipped white; native highlight recovery can soften it but cannot reconstruct missing detail.")
        if float(analysis.get("clipped_black_fraction") or 0.0) >= 0.055:
            warnings.append("The samples contain substantial crushed black; shadow lifting is intentionally restrained to preserve scene intent and noise.")
        if float(analysis.get("noise_score") or 0.0) >= 0.050:
            warnings.append("Visible high-frequency noise was detected, so automatic sharpening was reduced or disabled.")

    detection = metadata.get("layout_detection") or {}
    if isinstance(detection, dict):
        for conflict in list(detection.get("conflicts") or [])[:3]:
            warnings.append(f"VR verification conflict: {conflict}.")
        if detection.get("verification") == "ambiguous" and int(detection.get("frames_used") or 0) > 0:
            warnings.append("Pixel-based VR layout verification was ambiguous; the filename/cached layout remains in control and is reviewable below.")

    alignment = metadata.get("stereo_alignment") or {}
    if isinstance(alignment, dict):
        confidence = float(alignment.get("confidence") or 0.0)
        dy = abs(float(alignment.get("median_vertical_disparity_px") or 0.0))
        roll = abs(float(alignment.get("median_roll_deg") or 0.0))
        if confidence >= 0.50 and (dy >= 2.0 or roll >= 0.20):
            warnings.append("Stereo eye mismatch was measured. Auto can correct only a stable vertical mismatch; horizontal disparity and roll remain diagnostic to preserve real depth.")
        if bool(alignment.get("eye_swap_suspected")) and float(alignment.get("eye_swap_confidence") or 0.0) >= 0.60:
            warnings.append("Eye order looks suspicious from multi-frame disparity evidence. It is not changed automatically unless the explicit eye-swap option is enabled.")

    orientation = metadata.get("orientation_verification") or {}
    if isinstance(orientation, dict) and bool(orientation.get("rotate_180_suspected")):
        warnings.append("Multiple face detections suggest the encoded image may be upside down. Automatic rotation is opt-in and can be reviewed in VR calibration.")

    lens = metadata.get("lens_verification") or {}
    if isinstance(lens, dict):
        for issue in list(lens.get("issues") or [])[:2]:
            warnings.append(f"Lens verification: {issue}.")

    horizon = metadata.get("horizon_verification") or {}
    if isinstance(horizon, dict) and float(horizon.get("confidence") or 0.0) >= 0.60:
        roll = abs(float(horizon.get("median_roll_deg") or 0.0))
        if roll >= 0.35:
            warnings.append(f"A stable camera-roll/horizon tilt of about {roll:.2f}° was measured. Automatic leveling remains confidence-gated and opt-in by default.")

    active_bounds = metadata.get("active_image_bounds") or {}
    if isinstance(active_bounds, dict) and bool(active_bounds.get("significant_borders")):
        border = float(active_bounds.get("largest_border_fraction") or 0.0) * 100.0
        warnings.append(
            f"Persistent encoded black borders were detected (largest side about {border:.1f}%). "
            "HereTune can express a bounded crop through native Zoom/Pan only when the projection and confidence are safe."
        )

    if analysis and "reason" not in analysis:
        scene_count = len(analysis.get("scene_keyframes") or [])
        if scene_count > 1:
            warnings.append(
                f"Sparse scene grading created {scene_count} stable Image keyframes. "
                "These are sustained lighting sections, not frame-by-frame auto white balance."
            )
        if float(analysis.get("per_channel_confidence") or 0.0) >= 0.45:
            warnings.append(
                "Brightness-dependent color drift was measured; small RGB Contrast/Gamma/Offset/Saturation trims are included in the native grade."
            )
        if float(analysis.get("av_sync_confidence") or 0.0) >= 0.55 and abs(float(analysis.get("suggested_audio_time_offset") or 0.0)) >= 0.04:
            warnings.append(
                "A possible A/V timing offset was measured. It remains diagnostic unless Audio Sync auto-apply is explicitly enabled."
            )
    return warnings


def _thumbnail_revision(video: dict[str, object]) -> str:
    value = str(video.get("thumb_path") or "")
    if not value:
        return str(THUMBNAIL_LAYOUT_VERSION)
    path = Path(value)
    try:
        stat = path.stat()
        return f"{THUMBNAIL_LAYOUT_VERSION}-{stat.st_size:x}-{stat.st_mtime_ns:x}"
    except OSError:
        return str(THUMBNAIL_LAYOUT_VERSION)


def _global_scalar_from_vec4(value) -> float:
    """Return HereSphere's native W/A global component from an FVector4."""
    values = tuple(float(part) for part in value)
    return values[3]


def _vec4_text(value) -> str:
    return "/".join(f"{float(part):.3f}" for part in value)


def _vec3_text(value) -> str:
    return "/".join(f"{float(part):.3f}" for part in value)


def _hsp_inspection_payload(parsed) -> dict[str, object]:
    images = []
    for image in parsed.image_keyframes:
        images.append({
            "time_ticks": int(image.time_ticks),
            "time_seconds": float(image.time_ticks) / 10_000_000.0,
            "sharpen": float(image.sharpen),
            "exposure_bias": float(image.exposure_bias),
            "white_temp": float(image.white_temp),
            "white_tint": float(image.white_tint),
            "saturation": list(image.saturation),
            "contrast": list(image.contrast),
            "gamma": list(image.gamma),
            "gain": list(image.gain),
            "offset": list(image.offset),
            "shadows": float(image.shadows),
            "midtones": float(image.midtones),
            "highlights": float(image.highlights),
        })
    first = images[0] if images else None
    return {
        "version": parsed.version,
        "container_prefix": parsed.container_prefix,
        "unique_id": parsed.unique_id,
        "title": parsed.title,
        "array_counts": parsed.array_counts,
        "format": [asdict(item) for item in parsed.format_keyframes],
        "lens": [asdict(item) for item in parsed.lens_keyframes],
        "stitch": [asdict(item) for item in parsed.stitch_keyframes],
        "alignment": [asdict(item) for item in parsed.alignment_keyframes],
        "orientation": [asdict(item) for item in parsed.orientation_keyframes],
        "autofocus": [asdict(item) for item in parsed.autofocus_keyframes],
        "sync": [asdict(item) for item in parsed.sync_keyframes],
        "audio": [asdict(item) for item in parsed.audio_keyframes],
        "image_keyframes": images,
        "image": first,
        "image_time_ticks": int(first["time_ticks"]) if first else None,
        "sharpen": first["sharpen"] if first else None,
        "exposure_bias": first["exposure_bias"] if first else None,
        "white_temp": first["white_temp"] if first else None,
        "white_tint": first["white_tint"] if first else None,
        "shadows": first["shadows"] if first else None,
        "midtones": first["midtones"] if first else None,
        "highlights": first["highlights"] if first else None,
        "gain": first["gain"] if first else None,
        "saturation": first["saturation"] if first else None,
        "contrast": first["contrast"] if first else None,
        "gamma": first["gamma"] if first else None,
        "offset": first["offset"] if first else None,
        "color_vector_mapping": "global-w-with-per-channel-rgb-gain",
        "color_vector_mapping_detail": "global W/A scalar plus independent RGB saturation/contrast/gamma/offset and RGB gain",
        "scalar_summary": ({
            "saturation": _global_scalar_from_vec4(first["saturation"]),
            "contrast": _global_scalar_from_vec4(first["contrast"]),
            "gamma": _global_scalar_from_vec4(first["gamma"]),
            "offset": _global_scalar_from_vec4(first["offset"]),
        } if first else None),
        "raw_size": parsed.raw_size,
        "compressed": parsed.compressed,
        "trailing_bytes": parsed.trailing_bytes,
    }


def _image_payload(image, *, time_seconds: float | None = None) -> dict[str, object]:
    payload = {
        "gain_r": float(image.gain[0]),
        "gain_g": float(image.gain[1]),
        "gain_b": float(image.gain[2]),
        "offset_r": float(image.offset[0]),
        "offset_g": float(image.offset[1]),
        "offset_b": float(image.offset[2]),
        "offset": float(image.offset[3]),
        "contrast_r": float(image.contrast[0]),
        "contrast_g": float(image.contrast[1]),
        "contrast_b": float(image.contrast[2]),
        "contrast": float(image.contrast[3]),
        "saturation_r": float(image.saturation[0]),
        "saturation_g": float(image.saturation[1]),
        "saturation_b": float(image.saturation[2]),
        "saturation": float(image.saturation[3]),
        "gamma_r": float(image.gamma[0]),
        "gamma_g": float(image.gamma[1]),
        "gamma_b": float(image.gamma[2]),
        "gamma": float(image.gamma[3]),
        "sharpen": float(image.sharpen),
        "exposure_bias": float(image.exposure_bias),
        "white_temp": float(image.white_temp),
        "white_tint": float(image.white_tint),
        "shadows": float(image.shadows),
        "midtones": float(image.midtones),
        "highlights": float(image.highlights),
        "native_vectors": {
            "gain": list(image.gain),
            "offset": list(image.offset),
            "contrast": list(image.contrast),
            "saturation": list(image.saturation),
            "gamma": list(image.gamma),
        },
    }
    if time_seconds is not None:
        payload["time_seconds"] = max(0.0, float(time_seconds))
    return payload


def _profile_payload(
    profile: CorrectionProfile,
    *,
    confidence_label: str,
    warnings: list[str],
) -> dict[str, object]:
    class _NativeImage:
        pass

    native = profile.native_image_settings()
    image = _NativeImage()
    image.gain = tuple(native["gain"])
    image.offset = tuple(native["offset"])
    image.contrast = tuple(native["contrast"])
    image.saturation = tuple(native["saturation"])
    image.gamma = tuple(native["gamma"])
    image.sharpen = native["sharpen"]
    image.exposure_bias = native["exposure_bias"]
    image.white_temp = native["white_temp"]
    image.white_tint = native["white_tint"]
    image.shadows = native["shadows"]
    image.midtones = native["midtones"]
    image.highlights = native["highlights"]
    payload = _image_payload(image)
    scenes: list[dict[str, object]] = []
    for item in list(profile.scene_keyframes or []):
        if not isinstance(item, dict) or not isinstance(item.get("native"), dict):
            continue
        scene_native = dict(item["native"])
        scene = _NativeImage()
        scene.gain = tuple(scene_native.get("gain", (1.0, 1.0, 1.0, 1.0)))
        scene.offset = tuple(scene_native.get("offset", (0.0, 0.0, 0.0, 0.0)))
        scene.contrast = tuple(scene_native.get("contrast", (1.0, 1.0, 1.0, 1.0)))
        scene.saturation = tuple(scene_native.get("saturation", (1.0, 1.0, 1.0, 1.0)))
        scene.gamma = tuple(scene_native.get("gamma", (1.0, 1.0, 1.0, 1.0)))
        scene.sharpen = scene_native.get("sharpen", 2.0)
        scene.exposure_bias = scene_native.get("exposure_bias", 0.0)
        scene.white_temp = scene_native.get("white_temp", 6500.0)
        scene.white_tint = scene_native.get("white_tint", 0.0)
        scene.shadows = scene_native.get("shadows", 0.0)
        scene.midtones = scene_native.get("midtones", 1.0)
        scene.highlights = scene_native.get("highlights", 1.0)
        scene_payload = _image_payload(scene, time_seconds=float(item.get("time_seconds") or 0.0))
        scene_payload.update({
            "confidence": float(item.get("confidence") or 0.0),
            "change_score": float(item.get("change_score") or 0.0),
            "sample_count": int(item.get("sample_count") or 0),
        })
        scenes.append(scene_payload)
    payload.update({
        "scene_keyframes": scenes,
        "scene_count": len(scenes) or 1,
        "scene_grade_confidence": float(profile.scene_grade_confidence or 0.0),
        "per_channel_confidence": float(profile.per_channel_confidence or 0.0),
        "per_channel_samples": int(profile.per_channel_samples or 0),
        "audio_mean_db": float(profile.audio_mean_db or 0.0),
        "audio_peak_db": float(profile.audio_peak_db or 0.0),
        "audio_loudness_confidence": float(profile.audio_loudness_confidence or 0.0),
        "audio_loudness_windows": int(profile.audio_loudness_windows or 0),
        "audio_volume_multiplier": float(profile.audio_volume_multiplier or 1.0),
        "audio_volume_auto_applied": bool(profile.audio_volume_auto_applied),
        "suggested_audio_time_offset": float(profile.suggested_audio_time_offset or 0.0),
        "audio_time_offset": float(profile.audio_time_offset or 0.0),
        "av_sync_confidence": float(profile.av_sync_confidence or 0.0),
        "av_sync_windows": int(profile.av_sync_windows or 0),
        "suggested_sync_drift_seconds_per_hour": float(profile.suggested_sync_drift_seconds_per_hour or 0.0),
        "sync_drift_seconds_per_hour": float(profile.sync_drift_seconds_per_hour or 0.0),
        "sync_drift_confidence": float(profile.sync_drift_confidence or 0.0),
        "audio_sync_auto_applied": bool(profile.audio_sync_auto_applied),
        "confidence_label": confidence_label,
        "warnings": warnings,
    })
    return payload


def _video_view(video: dict[str, object], cfg) -> dict[str, object]:
    metadata = effective_metadata(video, video.get("metadata") or {})
    raw_analysis = video.get("analysis") or {}
    native_metadata: dict[str, object] = dict(metadata)
    diagnostic = _hsp_diagnostic_state(video)
    mode = _profile_mode(video)
    kind, blob, version_value = _profile_blob(video, cfg)
    revision = _hsp_revision(blob) if blob is not None else None
    confidence = _confidence(raw_analysis)
    duration_ms = float(metadata.get("duration") or 0.0) * 1000.0
    library_root = str(video.get("library_root") or primary_library_root(cfg) or "").strip()
    root_name = Path(library_root.rstrip("\\/" )).name if library_root else "Library"
    if not root_name and library_root:
        root_name = library_root.rstrip("\\/")
    relative_parent = str(Path(str(video.get("rel_path") or "")).parent).replace("\\", "/")
    parent = root_name or "Library"
    if relative_parent not in {"", "."}:
        parent = f"{parent} / {relative_parent}"

    correction: dict[str, object] = {}
    suggested_correction: dict[str, object] = {}
    analysis = dict(raw_analysis)
    display_correction: CorrectionProfile | None = None

    if mode in _PROFILE_STRENGTH and (
        mode == "original" or (raw_analysis and "reason" not in raw_analysis)
    ):
        display_correction = _scaled_correction(
            raw_analysis, _PROFILE_STRENGTH[mode]
        )
    elif mode == "manual" and video.get("manual_hsp") is not None:
        try:
            manual = inspect_hsp(bytes(video["manual_hsp"]), require_complete=False)
            image = manual.image_keyframes[0]
            correction = _image_payload(image)
            correction["scene_keyframes"] = [
                _image_payload(key, time_seconds=float(key.time_ticks) / 10_000_000.0)
                for key in manual.image_keyframes
            ]
            correction["scene_count"] = len(manual.image_keyframes)
            correction["confidence_label"] = "manual HereSphere write-back"
            correction["warnings"] = _warnings(video)
            if manual.audio_keyframes:
                correction["audio_time_offset"] = float(manual.audio_keyframes[0].time_offset)
                correction["audio_volume_multiplier"] = float(manual.audio_keyframes[0].volume_multiplier)
            if manual.sync_keyframes:
                correction["sync_drift_seconds_per_hour"] = (
                    float(manual.sync_keyframes[0].ticks_per_hour_offset) / 10_000_000.0
                )
        except (HSPError, IndexError, TypeError, ValueError):
            correction = {}

    if display_correction is not None:
        correction = _profile_payload(
            display_correction,
            confidence_label=(
                "high" if confidence >= 0.70 else "medium" if confidence >= 0.40 else "low"
            ),
            warnings=_warnings(video),
        )
        suggested_correction = copy.deepcopy(correction)

    # Show exact selected-HSP values for Auto/Mild/Strong. This makes global and
    # per-video output policy visible in the drawer instead of displaying the raw
    # analyzer suggestion after a control has been neutralized for playback.
    # The drawer, browser preview, HSP GET log and headset-served bytes therefore
    # all describe the same selected profile.
    if mode in {"auto", "mild", "strong"} and blob is not None:
        try:
            parsed = inspect_hsp(blob)
            if parsed.image_keyframes:
                inherited = {
                    key: correction.get(key)
                    for key in (
                        "confidence_label", "warnings", "audio_mean_db", "audio_peak_db",
                        "audio_loudness_confidence", "audio_loudness_windows",
                        "audio_volume_multiplier", "audio_volume_auto_applied",
                        "suggested_audio_time_offset", "audio_time_offset", "av_sync_confidence",
                        "av_sync_windows", "suggested_sync_drift_seconds_per_hour",
                        "sync_drift_seconds_per_hour", "sync_drift_confidence",
                        "audio_sync_auto_applied",
                    ) if key in correction
                }
                correction = _image_payload(parsed.image_keyframes[0])
                correction["scene_keyframes"] = [
                    _image_payload(key, time_seconds=float(key.time_ticks) / 10_000_000.0)
                    for key in parsed.image_keyframes
                ]
                correction["scene_count"] = len(parsed.image_keyframes)
                correction.update(inherited)
                correction["preview_source"] = (
                    "diagnostic-selected-hsp" if diagnostic["enabled"] and mode == "auto"
                    else "selected-hsp"
                )
        except Exception:
            pass

    if video.get("auto_hsp") is not None:
        try:
            baseline = inspect_hsp(bytes(video["auto_hsp"]))
            if baseline.image_keyframes:
                diagnostic["baseline_image"] = _image_payload(baseline.image_keyframes[0])
                diagnostic["baseline_scene_count"] = len(baseline.image_keyframes)
        except Exception:
            pass

    # Global Offset is an output-only policy control: OFF by default and never
    # part of the analyzer's Gamma/Midtones/Contrast solve. When explicitly
    # enabled, the parsed selected HSP already exposes its actual bounded value.
    diagnostic["global_offset_policy"] = "off by default; output-only suggestion"
    if correction:
        correction["global_offset_policy"] = diagnostic["global_offset_policy"]

    calibration_profile = display_correction
    if calibration_profile is None and raw_analysis and "reason" not in raw_analysis:
        calibration_profile = _correction_from_analysis(raw_analysis)
    native_metadata = native_calibration_metadata(
        video, metadata, cfg, calibration_profile
    )

    if raw_analysis and "reason" not in raw_analysis:
        sampled_frames = int(raw_analysis.get("sampled_frames") or 0)
        attempted_frames = int(raw_analysis.get("target_samples") or 0)
        if attempted_frames <= 0:
            attempted_frames = int(raw_analysis.get("gpu_samples") or 0) + int(raw_analysis.get("cpu_samples") or 0)
        if attempted_frames <= 0:
            attempted_frames = sampled_frames
        analysis.update({
            "frames_used": sampled_frames,
            "frames_received": attempted_frames,
            "base_samples": int(raw_analysis.get("requested_samples") or attempted_frames),
            "candidate_samples": int(raw_analysis.get("candidate_samples") or attempted_frames),
            "rejected_black_fade": int(raw_analysis.get("rejected_black_fade") or 0),
            "rejected_title_like": int(raw_analysis.get("rejected_title_like") or 0),
            "rejected_duplicate": int(raw_analysis.get("rejected_duplicate") or 0),
            "rejected_total": int(raw_analysis.get("rejected_total") or 0),
            "average_sample_ms": float(raw_analysis.get("average_sample_ms") or 0.0),
            "neutral_fraction": float(raw_analysis.get("neutral_fraction") or 0.0),
            "skin_samples": int(raw_analysis.get("skin_sample_count") or 0),
            "face_samples": int(raw_analysis.get("face_sample_count") or 0),
            "median_luma": float(raw_analysis.get("luma_p50") or 0.0),
            "luma_spread": float(raw_analysis.get("luma_p95") or 0.0) - float(raw_analysis.get("luma_p05") or 0.0),
            "white_balance_method": _wb_method_label(raw_analysis.get("wb_method")),
            "white_balance_native_mode": str(raw_analysis.get("wb_native_mode") or "rgb-gain"),
            "source_white_temp": float(raw_analysis.get("source_white_temp") or 6500.0),
            "source_white_tint": float(raw_analysis.get("source_white_tint") or 0.0),
            "temperature_tint_confidence": float(raw_analysis.get("temperature_tint_confidence") or 0.0),
            "temperature_tint_samples": int(raw_analysis.get("temperature_tint_samples") or 0),
            "tone_confidence": float(raw_analysis.get("tone_confidence") or 0.0),
            "clipped_black_fraction": float(raw_analysis.get("clipped_black_fraction") or 0.0),
            "clipped_highlight_fraction": float(raw_analysis.get("clipped_highlight_fraction") or 0.0),
            "shadow_fraction": float(raw_analysis.get("shadow_fraction") or 0.0),
            "highlight_fraction": float(raw_analysis.get("highlight_fraction") or 0.0),
            "sharpness_score": float(raw_analysis.get("sharpness_score") or 0.0),
            "noise_score": float(raw_analysis.get("noise_score") or 0.0),
            "sharpen_confidence": float(raw_analysis.get("sharpen_confidence") or 0.0),
            "analysis_seconds": float(raw_analysis.get("analysis_seconds") or 0.0),
            "adaptive_workers": bool(raw_analysis.get("adaptive_workers", False)),
            "gpu_worker_peak": int(raw_analysis.get("gpu_worker_peak") or 0),
            "cpu_worker_peak": int(raw_analysis.get("cpu_worker_peak") or 0),
        })

    auto_blob = bytes(video["auto_hsp"]) if video.get("auto_hsp") is not None else None
    sidecar = sidecar_status(video, auto_blob)
    sidecar["global_enabled"] = bool(getattr(cfg.hsp, "write_auto_sidecars", False))
    output_policy = policy_payload(cfg, _video_policy_overrides(video))
    output_policy["suggested"] = suggested_correction

    return {
        "id": video["id"],
        "title": video["title"],
        "rel_path": video["rel_path"],
        "library_root": library_root,
        "library_name": root_name or "Library",
        "media_filename": Path(str(video.get("path") or video["rel_path"])).name,
        "folder": parent,
        "status": "pending" if video["status"] in {"queued", "changed"} else video["status"],
        "raw_status": video["status"],
        "error": video.get("error"),
        "size": int(video.get("size") or 0),
        "date_added": video.get("date_added"),
        "last_analyzed": video.get("last_analyzed"),
        "width": int(metadata.get("width") or 0),
        "height": int(metadata.get("height") or 0),
        "duration_ms": duration_ms,
        "projection": metadata.get("projection", "perspective"),
        "stereo": metadata.get("stereo", "mono"),
        "lens": metadata.get("lens", "Linear"),
        "fov": float(metadata.get("fov") or 0.0),
        "video_codec": metadata.get("video_codec", "unknown"),
        "bit_depth": int(metadata.get("bit_depth") or 8),
        "is_hdr": bool(metadata.get("is_hdr", False)),
        "isEyeSwapped": bool(metadata.get("isEyeSwapped", False)),
        "profile_mode": mode,
        "web_player_invert_drag": bool(getattr(cfg.server, "web_player_invert_drag", False)),
        "show_hsp_diagnostic_tools": bool(getattr(cfg.server, "show_hsp_diagnostic_tools", True)),
        "selected_hsp_kind": kind,
        "hsp_status": f"{kind} v{version_value}" if kind and version_value else "not generated",
        "hsp_version": version_value,
        "hsp_revision": revision,
        "manual_hsp": bool(video.get("manual_hsp")),
        "manual_hsp_path": bool(video.get("manual_hsp")),
        "auto_hsp_path": bool(video.get("auto_hsp")),
        "manual_hsp_version": video.get("manual_hsp_version"),
        "thumbnail_path": bool(video.get("thumb_path")),
        "thumbnail_revision": _thumbnail_revision(video),
        "preview_revision": PREVIEW_RENDER_VERSION,
        "hsp_diagnostic": diagnostic,
        "auto_hsp_policy": output_policy,
        "hsp_sidecar": sidecar,
        "manual_priority": bool(video.get("manual_priority")),
        "priority_requested_at": video.get("priority_requested_at"),
        "priority_rank": (int(video["priority_rank"]) if video.get("priority_rank") is not None else None),
        "is_favorite": bool(video.get("is_favorite")),
        "rating": (float(video["rating"]) if video.get("rating") is not None else None),
        "layout_detection": (video.get("metadata") or {}).get("layout_detection") or {},
        "stereo_alignment": (video.get("metadata") or {}).get("stereo_alignment") or {},
        "lens_verification": (video.get("metadata") or {}).get("lens_verification") or {},
        "orientation_verification": (video.get("metadata") or {}).get("orientation_verification") or {},
        "horizon_verification": (video.get("metadata") or {}).get("horizon_verification") or {},
        "active_image_bounds": (video.get("metadata") or {}).get("active_image_bounds") or {},
        "stereo_alignment_applied": bool(native_metadata.get("stereo_alignment_applied")),
        "native_calibration": {
            "alignment_position": list(native_metadata.get("alignment_position") or (0.0, 6.5, 0.0)),
            "alignment_rotation": list(native_metadata.get("alignment_rotation") or (0.0, 0.0, 0.0)),
            "alignment_applied": bool(native_metadata.get("stereo_alignment_applied")),
            "alignment_confidence": float(native_metadata.get("stereo_alignment_confidence") or 0.0),
            "alignment_policy": str(native_metadata.get("stereo_alignment_policy") or "diagnostic only"),
            "stitch_shift": list(native_metadata.get("stitch_shift") or (0.0, 0.0, 0.0, 0.0)),
            "stitch_scale": list(native_metadata.get("stitch_scale") or (1.0, 1.0, 1.0, 1.0)),
            "stitch_shear": list(native_metadata.get("stitch_shear") or (0.0, 0.0, 0.0, 0.0)),
            "stitch_flare": list(native_metadata.get("stitch_flare") or (0.0, 0.0, 0.0, 0.0)),
            "stitch_slant": list(native_metadata.get("stitch_slant") or (0.0, 0.0, 0.0, 0.0)),
            "stitch_confidence": float(native_metadata.get("stitch_confidence") or 0.0),
            "stitch_applied": bool(native_metadata.get("stitch_applied")),
            "stereo_scale_applied": bool(native_metadata.get("stereo_scale_applied")),
            "zoom": list(native_metadata.get("zoom") or (1.0, 1.0)),
            "pan": list(native_metadata.get("pan") or (0.0, 0.0)),
            "auto_crop_applied": bool(native_metadata.get("auto_crop_applied")),
            "auto_crop_confidence": float(native_metadata.get("auto_crop_confidence") or 0.0),
            "orientation_rotation": list(native_metadata.get("orientation_rotation") or (0.0, 0.0, 0.0)),
            "horizon_leveling_applied": bool(native_metadata.get("horizon_leveling_applied")),
            "horizon_confidence": float(native_metadata.get("horizon_confidence") or 0.0),
            "lens_fov_applied": bool(native_metadata.get("lens_fov_applied")),
            "lens_fov_confidence": float(native_metadata.get("lens_fov_confidence") or 0.0),
            "autofocus_applied": bool(native_metadata.get("autofocus_applied")),
            "autofocus_rotation": list(native_metadata.get("autofocus_rotation") or (0.0, 0.0, 0.0)),
            "autofocus_focal_distance": float(native_metadata.get("autofocus_focal_distance") or native_metadata.get("autofocus_focal_override") or 0.0),
            "autofocus_min_distance": float(native_metadata.get("autofocus_min_distance") or 10.0),
            "autofocus_max_distance": float(native_metadata.get("autofocus_max_distance") or 1000.0),
            "autofocus_transition_speed": float(native_metadata.get("autofocus_transition_speed") or 1.0),
            "autofocus_confidence": float(native_metadata.get("autofocus_confidence") or 0.0),
            "audio_volume_multiplier": float(native_metadata.get("audio_volume_multiplier") or 1.0),
            "audio_volume_applied": bool(native_metadata.get("audio_volume_applied")),
            "audio_time_offset": float(native_metadata.get("audio_time_offset") or 0.0),
            "sync_start_offset_seconds": float(native_metadata.get("sync_start_offset_seconds") or 0.0),
            "sync_drift_seconds_per_hour": float(native_metadata.get("sync_drift_seconds_per_hour") or 0.0),
            "audio_sync_applied": bool(native_metadata.get("audio_sync_applied")),
            "flip_front_back": bool(native_metadata.get("flip_front_back", False)),
            "flip_left_right": bool(native_metadata.get("flip_left_right", False)),
            "flip_up_down": bool(native_metadata.get("flip_up_down", False)),
        },
        "layout_override": video.get("layout_override") or {},
        "content_fingerprint": video.get("content_fingerprint"),
        "duplicate_group": video.get("duplicate_group"),
        "watch": {
            "position_ms": float(video.get("watch_position_ms") or 0.0),
            "progress": max(0.0, min(1.0, float(video.get("watch_position_ms") or 0.0) / max(duration_ms, 1.0))) if duration_ms else 0.0,
            "last_watched": video.get("last_watched"),
            "play_count": int(video.get("play_count") or 0),
            "completed": bool(video.get("completed")),
        },
        "confidence": confidence,
        "correction": correction,
        "analysis": analysis,
        "player_settings": {
            "invert_drag": bool(getattr(cfg.server, "web_player_invert_drag", False)),
        },
    }


def _selected_hsp_preview_correction(video: dict[str, object], cfg) -> dict[str, object]:
    """Return the exact serialized Image values currently served to HereSphere.

    The drawer preview used to rebuild its display payload from analysis JSON.
    That should normally be equivalent, but it leaves room for profile scaling,
    serializer clamping, manual HSPs, or future serializer changes to diverge. For the
    comparison tool, parse the *selected HSP blob itself* and feed those exact
    Image vectors into the monitor approximation. This cannot reproduce
    HereSphere's private transfer functions, but it guarantees the values being
    approximated are byte-for-byte the values HereSphere receives.
    """
    view_correction = dict((_video_view(video, cfg).get("correction") or {}))
    _kind, blob, _version = _profile_blob(video, cfg)
    if not blob:
        return view_correction
    try:
        parsed = inspect_hsp(bytes(blob), require_complete=False)
        if not parsed.image_keyframes:
            return view_correction
        first = parsed.image_keyframes[0]
        payload = _image_payload(first)
        view_scenes = view_correction.get("scene_keyframes")
        confidence_by_time: dict[int, dict[str, object]] = {}
        if isinstance(view_scenes, list):
            for item in view_scenes:
                if not isinstance(item, dict):
                    continue
                try:
                    key = int(round(float(item.get("time_seconds") or 0.0) * 1000.0))
                except (TypeError, ValueError):
                    continue
                confidence_by_time[key] = item
        scenes: list[dict[str, object]] = []
        for image in parsed.image_keyframes:
            time_seconds = float(image.time_ticks) / 10_000_000.0
            scene = _image_payload(image, time_seconds=time_seconds)
            metadata = confidence_by_time.get(int(round(time_seconds * 1000.0)), {})
            for key in ("confidence", "change_score", "sample_count"):
                if key in metadata:
                    scene[key] = metadata[key]
            scenes.append(scene)
        payload["scene_keyframes"] = scenes
        payload["scene_count"] = len(scenes)
        payload["preview_source"] = "selected-hsp"
        return payload
    except Exception:
        # Preview accuracy improvements must never make the drawer unavailable.
        return view_correction


def _queue_eta_model(db: DB, cfg) -> dict[str, object]:
    """Learn a lightweight ETA model without loading HSP BLOBs/full rows."""
    history = []
    history_sql = """SELECT metadata_json, analysis_json
                     FROM videos
                     WHERE status='ready' AND analysis_json IS NOT NULL
                     ORDER BY COALESCE(last_analyzed, date_added) DESC
                     LIMIT 500"""
    for row in db.iter_all(history_sql):
        try:
            analysis = json.loads(row.get("analysis_json") or "{}")
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        try:
            seconds = float(analysis.get("analysis_seconds") or 0.0)
            samples = int(analysis.get("candidate_samples") or analysis.get("target_samples") or analysis.get("sampled_frames") or 0)
        except (TypeError, ValueError):
            continue
        if seconds <= 0 or samples <= 0:
            continue
        history.append({
            "seconds_per_sample": seconds / samples,
            "codec": str(metadata.get("video_codec") or "unknown"),
            "pixels": max(1, int(metadata.get("width") or 1) * int(metadata.get("height") or 1)),
        })
    global_rate = statistics.median([item["seconds_per_sample"] for item in history]) if history else 1.8
    global_pixels = statistics.median([item["pixels"] for item in history]) if history else 5760 * 2880

    def estimate_metadata(metadata: dict[str, object]) -> float:
        duration = float(metadata.get("duration") or 0.0)
        target = effective_sample_count(
            duration, int(cfg.analysis.samples), bool(cfg.analysis.adaptive_sampling),
            float(cfg.analysis.sample_interval_seconds), int(cfg.analysis.max_samples),
        )
        multiplier = float(getattr(cfg.analysis, "candidate_multiplier", 1.35)) if bool(getattr(cfg.analysis, "smart_sampling", True)) else 1.0
        candidates = min(max(target, int(round(target * multiplier))), target + 24)
        max_full = int(getattr(cfg.analysis, "max_samples", 360))
        if max_full > 0:
            candidates = min(candidates, max_full)
        codec = str(metadata.get("video_codec") or "unknown")
        peers = [item["seconds_per_sample"] for item in history if item["codec"] == codec]
        rate = statistics.median(peers) if len(peers) >= 2 else global_rate
        pixels = max(1, int(metadata.get("width") or 1) * int(metadata.get("height") or 1))
        pixel_factor = max(0.65, min(1.65, (pixels / max(global_pixels, 1)) ** 0.28))
        return max(5.0, float(candidates) * float(rate) * pixel_factor)

    estimates: list[float] = []
    unknown = 0
    pending_count = 0
    for row in db.iter_all("SELECT metadata_json FROM videos WHERE status IN ('queued','changed')"):
        pending_count += 1
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if metadata:
            estimates.append(estimate_metadata(metadata))
        else:
            unknown += 1
    total_seconds = sum(estimates)
    if unknown:
        typical = statistics.median(estimates) if estimates else max(30.0, global_rate * int(cfg.analysis.samples))
        total_seconds += typical * unknown
    return {
        "history_count": len(history),
        "seconds_per_sample": float(global_rate),
        "queue_seconds": float(total_seconds),
        "pending_count": int(pending_count),
        "estimate_source": "recent-analysis-history" if history else "baseline",
    }


def _status_payload(db: DB, cfg, app: FastAPI, resolved_mode: str, accel) -> dict[str, object]:
    """Build the frequently-polled status payload with bounded memory use.

    Analysis JSON and HSP BLOBs are intentionally rich, so status queries use
    projected aggregates rather than materializing full video rows.
    """
    summary = db.one(
        """SELECT
             COUNT(*) AS total,
             SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END) AS ready,
             SUM(CASE WHEN status IN ('queued','changed') THEN 1 ELSE 0 END) AS pending,
             SUM(CASE WHEN status='analyzing' THEN 1 ELSE 0 END) AS analyzing,
             SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped,
             SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error_count,
             SUM(CASE WHEN profile_mode='manual' AND manual_hsp IS NOT NULL THEN 1 ELSE 0 END) AS manual_active,
             SUM(CASE WHEN manual_hsp IS NOT NULL THEN 1 ELSE 0 END) AS manual_stored,
             SUM(CASE WHEN is_favorite!=0 THEN 1 ELSE 0 END) AS favorites,
             SUM(CASE WHEN rating IS NOT NULL THEN 1 ELSE 0 END) AS rated,
             AVG(rating) AS average_rating,
             SUM(CASE WHEN duplicate_group IS NOT NULL THEN 1 ELSE 0 END) AS duplicates,
             SUM(CASE WHEN last_watched IS NOT NULL AND completed=0 AND watch_position_ms>0 THEN 1 ELSE 0 END) AS continue_watching
           FROM videos WHERE status!='missing'"""
    ) or {}
    by_status = {
        str(row.get("status")): int(row.get("n") or 0)
        for row in db.all(
            "SELECT status, COUNT(*) AS n FROM videos WHERE status!='missing' GROUP BY status"
        )
    }

    confidence_sum = 0.0
    confidence_count = 0
    for row in db.iter_all("SELECT analysis_json FROM videos WHERE status='ready' AND analysis_json IS NOT NULL"):
        try:
            analysis = json.loads(row.get("analysis_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        confidence_sum += _confidence(analysis)
        confidence_count += 1

    eta_model = _queue_eta_model(db, cfg)
    telemetry = gpu_telemetry(int(cfg.analysis.gpu_device)) if resolved_mode in {"gpu", "hybrid"} else None
    runtime_progress = dict(getattr(app.state, "analysis_progress", None) or {})
    if runtime_progress:
        updated_at = getattr(app.state, "analysis_progress_updated_at", None)
        runtime_progress["stalled_seconds"] = max(0.0, time.monotonic() - float(updated_at)) if updated_at else 0.0
        active = [runtime_progress]
    else:
        active_row = db.one("SELECT id,title FROM videos WHERE status='analyzing' ORDER BY rel_path COLLATE NOCASE LIMIT 1")
        active = [{
            "id": active_row["id"],
            "title": active_row["title"],
            "stage": "analyzing",
            "completed": 0,
            "total": 0,
            "elapsed_seconds": 0.0,
            "eta_seconds": None,
        }] if active_row else []

    error = db.one("SELECT value FROM settings WHERE key='last_scan_error'")
    sidecar_sync = dict(getattr(app.state, "sidecar_sync_progress", None) or {})
    last_sidecar_sync = dict(getattr(app.state, "last_sidecar_sync", None) or {})
    active_eta = float(runtime_progress.get("eta_seconds") or 0.0) if runtime_progress else 0.0
    queue_seconds = float(eta_model.get("queue_seconds") or 0.0) + active_eta
    eta_finish = (datetime.now(timezone.utc) + timedelta(seconds=queue_seconds)).isoformat() if queue_seconds > 0 else None
    return {
        "version": __version__,
        "health": "ok",
        "library": primary_library_root(cfg),
        "library_roots": library_roots(cfg),
        "stats": {
            "total": int(summary.get("total") or 0),
            "ready": int(summary.get("ready") or 0),
            "pending": int(summary.get("pending") or 0),
            "analyzing": int(summary.get("analyzing") or 0),
            "skipped": int(summary.get("skipped") or 0),
            "error": int(summary.get("error_count") or 0),
            "manual": int(summary.get("manual_active") or 0),
            "manual_stored": int(summary.get("manual_stored") or 0),
            "favorites": int(summary.get("favorites") or 0),
            "rated": int(summary.get("rated") or 0),
            "average_rating": float(summary.get("average_rating") or 0.0),
            "average_confidence": (confidence_sum / confidence_count) if confidence_count else 0.0,
            "duplicates": int(summary.get("duplicates") or 0),
            "continue_watching": int(summary.get("continue_watching") or 0),
            "by_status": by_status,
        },
        "active": active,
        "scan": {
            "running": bool(getattr(app.state, "scan_running", False)),
            "paused": bool(getattr(app.state, "work_paused", False)),
        },
        "work": {
            "paused": bool(getattr(app.state, "work_paused", False)),
            "pause_requested": bool(getattr(app.state, "work_paused", False) and active),
        },
        "queue_eta": {
            **eta_model,
            "remaining_seconds": queue_seconds,
            "finish_at": eta_finish,
        },
        "analysis": {
            "requested_mode": cfg.analysis.execution_mode,
            "auto_preference": cfg.analysis.auto_preference,
            "resolved_mode": resolved_mode,
            "samples": cfg.analysis.samples,
            "analysis_width": cfg.analysis.analysis_width,
            "adaptive_sampling": cfg.analysis.adaptive_sampling,
            "max_samples": cfg.analysis.max_samples,
            "sample_timeout_seconds": float(getattr(cfg.analysis, "ffmpeg_sample_timeout_seconds", 20.0)),
            "active_child_processes": active_process_count(),
            "cuda": accel,
            "gpu_telemetry": telemetry,
            "worker_plan": dict(getattr(app.state, "worker_plan", None) or {}),
        },
        "sidecar_sync": sidecar_sync,
        "last_sidecar_sync": last_sidecar_sync,
        "hsp": {
            "generated_version": cfg.hsp.generated_version,
            "prefer_manual_writeback": cfg.hsp.prefer_manual_writeback,
            "auto_authoritative": not cfg.hsp.prefer_manual_writeback,
            "revisioned_urls": bool(cfg.hsp.revisioned_urls),
            "write_auto_sidecars": bool(getattr(cfg.hsp, "write_auto_sidecars", False)),
        },
        "last_analysis_failure": dict(getattr(app.state, "last_analysis_failure", None) or {}),
        "last_scan_error": error["value"] if error else None,
    }


def _filtered_video_ids(
    db: DB,
    *,
    status: str | None = None,
    q: str | None = None,
    favorite: str = "all",
    rating: str = "all",
    watch: str = "all",
    duplicates: str = "all",
    order: str = "title",
) -> tuple[list[str], int]:
    """Filter/sort the library without materializing HSP BLOBs.

    The UI previously expanded every complete video row (including automatic and
    manual HSP bytes) before applying a 250-row page limit. Rich analyses
    made that an avoidable memory spike. This lightweight first pass selects IDs;
    only the requested page is inflated afterward.
    """
    columns = (
        "id,title,rel_path,library_root,status,date_added,is_favorite,rating,manual_priority,"
        "priority_requested_at,priority_rank,last_watched,watch_position_ms,play_count,"
        "completed,duplicate_group"
    )
    if order == "confidence":
        columns += ",analysis_json"
    rows = db.lightweight_rows(columns, "status!='missing'")
    items: list[dict[str, object]] = []
    for row in rows:
        public_status = "pending" if row.get("status") in {"queued", "changed"} else str(row.get("status") or "")
        item = {
            **row,
            "status": public_status,
            "confidence": _confidence(row.get("analysis") or {}) if order == "confidence" else 0.0,
            "watch": {
                "position_ms": float(row.get("watch_position_ms") or 0.0),
                "last_watched": row.get("last_watched"),
                "play_count": int(row.get("play_count") or 0),
                "completed": bool(row.get("completed")),
            },
        }
        items.append(item)

    query = (q or "").strip().casefold()
    if query:
        items = [item for item in items if query in f"{item['title']} {item['rel_path']} {item.get('library_root') or ''}".casefold()]
    if status:
        wanted = status.lower()
        items = [item for item in items if item["status"] == wanted]
    if favorite == "favorite":
        items = [item for item in items if bool(item.get("is_favorite"))]
    elif favorite == "not_favorite":
        items = [item for item in items if not bool(item.get("is_favorite"))]
    items = _apply_rating_filter(items, rating)
    if watch == "continue":
        items = [item for item in items if item.get("watch", {}).get("last_watched") and not item.get("watch", {}).get("completed") and float(item.get("watch", {}).get("position_ms") or 0) > 0]
    elif watch == "completed":
        items = [item for item in items if bool(item.get("watch", {}).get("completed"))]
    elif watch == "never":
        items = [item for item in items if not item.get("watch", {}).get("last_watched")]
    if duplicates == "duplicates":
        items = [item for item in items if item.get("duplicate_group")]

    if order == "recent":
        items.sort(key=lambda item: str(item.get("date_added") or ""), reverse=True)
    elif order == "status":
        items.sort(key=lambda item: (str(item["status"]), str(item["title"]).casefold()))
    elif order == "confidence":
        items.sort(key=lambda item: (float(item["confidence"]), str(item["title"]).casefold()), reverse=True)
    elif order == "rating":
        items.sort(key=lambda item: ((item.get("rating") is not None), float(item.get("rating") or -1), str(item["title"]).casefold()), reverse=True)
    elif order == "favorite":
        items.sort(key=lambda item: (bool(item.get("is_favorite")), float(item.get("rating") or -1), str(item["title"]).casefold()), reverse=True)
    elif order == "priority":
        items.sort(key=lambda item: (0 if item.get("manual_priority") else 1, int(item.get("priority_rank") or 2147483647), str(item.get("priority_requested_at") or "9999"), str(item["title"]).casefold()))
    elif order == "watched":
        items.sort(key=lambda item: (str(item.get("watch", {}).get("last_watched") or ""), str(item["title"]).casefold()), reverse=True)
    elif order == "plays":
        items.sort(key=lambda item: (int(item.get("watch", {}).get("play_count") or 0), str(item["title"]).casefold()), reverse=True)
    else:
        items.sort(key=lambda item: str(item["title"]).casefold())
    ids = [str(item["id"]) for item in items]
    return ids, len(ids)


def _doctor_payload(cfg) -> dict[str, object]:
    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, detail: str, version_value: str | None = None) -> None:
        item: dict[str, object] = {"name": name, "ok": bool(ok), "detail": detail}
        if version_value:
            item["version"] = version_value
        checks.append(item)

    add("Python", sys.version_info >= (3, 11), sys.executable, sys.version.split()[0])
    roots = library_roots(cfg)
    root_states = []
    for root_text in roots:
        try:
            ok = Path(root_text).is_dir()
        except OSError:
            ok = False
        root_states.append((root_text, ok))
    reachable = sum(1 for _, ok in root_states if ok)
    detail = (
        f"{reachable}/{len(root_states)} media folders reachable"
        if root_states else
        "Not configured yet — finish first-run setup in the dashboard."
    )
    if root_states:
        detail += "; " + "; ".join(f"{'OK' if ok else 'OFFLINE'} {root}" for root, ok in root_states)
    add("Library folders", bool(root_states and reachable == len(root_states)), detail)
    ffmpeg_path = shutil.which(cfg.paths.ffmpeg)
    ffprobe_path = shutil.which(cfg.paths.ffprobe)
    add("FFmpeg", bool(ffmpeg_path), ffmpeg_path or "Not found")
    add("FFprobe", bool(ffprobe_path), ffprobe_path or "Not found")
    try:
        resolved_mode, cuda = resolve_execution_mode(cfg.analysis, cfg.paths.ffmpeg)
        detail = f"{cfg.analysis.execution_mode} → {resolved_mode}; Auto prefers {cfg.analysis.auto_preference}"
        if cuda is not None:
            detail += f"; {cuda.detail}"
        add("Acceleration", True, detail)
    except Exception as exc:
        add("Acceleration", False, str(exc))
    add(
        "OpenCV",
        cv2 is not None,
        OPENCV_NOTE or "Face-localized skin sampling available.",
        str(getattr(cv2, "__version__", "unavailable")),
    )
    try:
        blob = build_hsp("doctor", "HereTune Doctor", 60.0, CorrectionProfile())
        parsed = inspect_hsp(blob)
        image = parsed.image_keyframes[0]
        complete_groups = (
            "format", "lens", "stitch", "alignment", "orientation", "origin",
            "motion", "autofocus", "sync", "transition", "image", "audio", "environment",
        )
        ok = (
            parsed.version == 7
            and all(parsed.array_counts.get(name) == 1 for name in complete_groups)
            and image.sharpen == 2.0
            and image.white_temp == 6500.0
            and image.shadows == 0.0
            and image.midtones == 1.0
            and image.highlights == 1.0
        )
        add("HSP v7", ok, "Complete current-style preset structure and image round-trip validated.")
    except Exception as exc:
        add("HSP v7", False, str(exc))
    add(
        "Profile policy",
        True,
        "Auto is authoritative; headset write-backs are stored but require explicit Manual selection."
        if not cfg.hsp.prefer_manual_writeback
        else "Headset write-backs automatically become Manual.",
    )
    dashboard_auth = bool(str(getattr(cfg.server, "dashboard_username", "") or "") or str(getattr(cfg.server, "dashboard_password", "") or ""))
    add(
        "Dashboard access",
        True,
        "HTTP Basic authentication is enabled for the dashboard/admin API."
        if dashboard_auth else
        "No dashboard password is configured. This is acceptable on a trusted private LAN; do not expose HereTune directly to the Internet.",
    )
    add(
        "Source deletion",
        not bool(cfg.server.allow_file_delete),
        "Disabled (recommended)." if not cfg.server.allow_file_delete else "ENABLED — HereSphere can request permanent source-file deletion.",
    )
    return {"checks": checks}



def _rotate_named_files(directory: Path, pattern: str, keep: int) -> None:
    """Keep only the newest matching maintenance artifacts."""
    try:
        files = sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return
    for path in files[max(0, int(keep)):]:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _library_health_payload(db: DB, cfg) -> dict[str, object]:
    """Return an on-demand library/storage health snapshot.

    This deliberately runs only from the Health dialog or maintenance response,
    not the fast status poll.  It may touch filesystem metadata for every media
    row so the user gets an honest orphan/missing/stale report without bloating
    normal dashboard requests.
    """
    status_rows = db.all("SELECT status, COUNT(*) AS n FROM videos GROUP BY status")
    statuses = {str(row.get("status") or "unknown"): int(row.get("n") or 0) for row in status_rows}
    duplicate = db.one(
        "SELECT COUNT(DISTINCT duplicate_group) AS groups, COUNT(*) AS members "
        "FROM videos WHERE status!='missing' AND duplicate_group IS NOT NULL"
    ) or {}
    profiles = db.one(
        """SELECT
             SUM(CASE WHEN status='ready' AND auto_hsp IS NULL THEN 1 ELSE 0 END) AS missing_auto_hsp,
             SUM(CASE WHEN manual_hsp IS NOT NULL THEN 1 ELSE 0 END) AS manual_hsp,
             SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors
           FROM videos"""
    ) or {}

    stale_analysis = 0
    unversioned = 0
    for row in db.iter_all("SELECT analysis_json FROM videos WHERE status='ready' AND analysis_json IS NOT NULL"):
        try:
            analysis = json.loads(row.get("analysis_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            unversioned += 1
            continue
        revision = str(analysis.get("analysis_version") or analysis.get("version") or "")
        if not revision:
            unversioned += 1
        elif revision != ANALYSIS_VERSION:
            stale_analysis += 1

    inaccessible = 0
    missing_on_disk = 0
    for row in db.iter_video_rows("path,status", "status!='missing'"):
        try:
            if not Path(str(row.get("path") or "")).is_file():
                missing_on_disk += 1
        except OSError:
            inaccessible += 1

    known_thumbs = {
        str(row.get("thumb_path"))
        for row in db.iter_video_rows("thumb_path", "thumb_path IS NOT NULL")
        if row.get("thumb_path")
    }
    orphan_thumbs = 0
    thumbnail_bytes = 0
    try:
        for thumb in cfg.thumbs_dir.glob("*.jpg"):
            try:
                thumbnail_bytes += thumb.stat().st_size
            except OSError:
                pass
            if str(thumb) not in known_thumbs:
                orphan_thumbs += 1
    except OSError:
        pass

    backup_items: list[dict[str, object]] = []
    try:
        for path in sorted(cfg.backups_dir.glob("*.sqlite3"), key=lambda item: item.stat().st_mtime, reverse=True)[:10]:
            stat = path.stat()
            backup_items.append({"name": path.name, "bytes": stat.st_size, "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()})
    except OSError:
        pass

    try:
        usage = shutil.disk_usage(cfg.data_dir)
        disk = {"total": int(usage.total), "used": int(usage.used), "free": int(usage.free)}
    except OSError:
        disk = {"total": 0, "used": 0, "free": 0}

    root_statuses: list[dict[str, object]] = []
    for root_text in library_roots(cfg):
        try:
            reachable = bool(Path(root_text).is_dir())
        except OSError:
            reachable = False
        root_statuses.append({"root": root_text, "reachable": reachable})
    roots_reachable = sum(1 for item in root_statuses if item["reachable"])

    return {
        "library_root": primary_library_root(cfg),
        "library_roots": root_statuses,
        "root_reachable": bool(root_statuses and roots_reachable == len(root_statuses)),
        "roots_reachable": roots_reachable,
        "roots_configured": len(root_statuses),
        "statuses": statuses,
        "missing_on_disk": int(missing_on_disk),
        "inaccessible": int(inaccessible),
        "duplicates": {"groups": int(duplicate.get("groups") or 0), "members": int(duplicate.get("members") or 0)},
        "analysis": {
            "current_revision": ANALYSIS_VERSION,
            "stale_ready": int(stale_analysis),
            "unversioned_ready": int(unversioned),
        },
        "profiles": {
            "missing_auto_hsp": int(profiles.get("missing_auto_hsp") or 0),
            "manual_stored": int(profiles.get("manual_hsp") or 0),
            "errors": int(profiles.get("errors") or 0),
        },
        "storage": {
            "database": db.database_sizes(),
            "analysis_cache_bytes": int(AnalysisCache.size_bytes(cfg.analysis_cache_dir)),
            "analysis_cache_limit_bytes": int(float(getattr(cfg.analysis, "analysis_cache_max_gb", 20.0)) * 1024**3),
            "thumbnail_bytes": int(thumbnail_bytes),
            "orphan_thumbnails": int(orphan_thumbs),
            "disk": disk,
        },
        "backups": backup_items,
    }


def _video_analysis_cache(cfg, video: dict[str, object]) -> AnalysisCache:
    """Open the same per-video cache bucket used by the analysis worker."""
    path_text = str(video.get("path") or "")
    cache_key = f"{video.get('content_fingerprint') or path_text}|{video.get('size') or 0}|{video.get('mtime_ns') or 0}"
    return AnalysisCache(
        cfg.analysis_cache_dir,
        cache_key,
        enabled=bool(getattr(cfg.analysis, "analysis_cache_enabled", True)),
        max_gb=float(getattr(cfg.analysis, "analysis_cache_max_gb", 20.0)),
    )


def _dashboard_basic_credentials(request: Request) -> tuple[str, str] | None:
    header = str(request.headers.get("authorization") or "")
    if not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
        return username, password
    except Exception:
        return None


def _dashboard_authorized(request: Request, cfg) -> bool:
    expected_user = str(getattr(cfg.server, "dashboard_username", "") or "")
    expected_password = str(getattr(cfg.server, "dashboard_password", "") or "")
    if not expected_user and not expected_password:
        return True
    supplied = _dashboard_basic_credentials(request)
    if supplied is None:
        return False
    user, password = supplied
    return secrets.compare_digest(user, expected_user) and secrets.compare_digest(password, expected_password)


def _dashboard_path(path: str) -> bool:
    return (
        path in {"/", "/ui"}
        or path.startswith("/api/")
        or path.startswith("/static/")
        or path.startswith("/watch/")
    )


_RESTART_ONLY_CONFIG_FIELDS = {"server.host", "server.port", "paths.data_dir"}


def _runtime_apply_config(cfg, saved) -> tuple[list[str], list[str]]:
    """Hot-apply every saved setting except resources bound at process startup.

    Host/port own the listening socket and data_dir owns the open SQLite/cache
    tree, so those remain restart-only. Everything else updates the shared Config
    object immediately after the atomic config.toml write succeeds.
    """
    applied: list[str] = []
    restart_required: list[str] = []
    for section_name in ("library", "server", "analysis", "hsp", "paths"):
        target = getattr(cfg, section_name)
        source = getattr(saved, section_name)
        for item in fields(source):
            key = f"{section_name}.{item.name}"
            old_value = getattr(target, item.name)
            new_value = getattr(source, item.name)
            if old_value == new_value:
                continue
            if key in _RESTART_ONLY_CONFIG_FIELDS:
                restart_required.append(key)
                continue
            setattr(target, item.name, copy.deepcopy(new_value))
            applied.append(key)
    return applied, restart_required


def _runtime_apply_setup(cfg, saved) -> None:
    # First-run setup is just a restricted form of the normal hot-apply path.
    # The setup form does not expose restart-only fields.
    _runtime_apply_config(cfg, saved)


def _generate_unique_operation_id(route: APIRoute) -> str:
    methods = "_".join(sorted(method.lower() for method in (route.methods or set()) if method != "HEAD")) or "route"
    path = (route.path_format or route.path or "root").strip("/") or "root"
    path = re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_") or "root"
    return f"{route.name}_{path}_{methods}"


def _alpha_library_label(index: int) -> str:
    """Return spreadsheet-style alphabetic labels: A..Z, AA..AZ, BA..."""
    value = max(0, int(index)) + 1
    chars: list[str] = []
    while value:
        value, remainder = divmod(value - 1, 26)
        chars.append(chr(ord("A") + remainder))
    return "".join(reversed(chars))


def _compact_library_groups(groups: list[dict[str, object]]) -> list[dict[str, object]]:
    """Relabel one response only; no persistent name mapping is created.

    HereSphere places these groups in one horizontal strip that may not scroll.
    Keep its two useful synthetic groups recognizable, then spend the compact
    alphabet only on real media-folder groups:

        Favorites -> ♥
        All Movies -> All
        first real folder -> A, then B, C ...

    The aliases are regenerated for every response and never stored.
    """
    compact: list[dict[str, object]] = []
    folder_index = 0
    for group in groups:
        original_name = str(group.get("name") or "")
        if original_name == "Favorites":
            display_name = "♥"
        elif original_name in {"All Movies", "All Videos", "All"}:
            display_name = "All"
        else:
            display_name = _alpha_library_label(folder_index)
            folder_index += 1
        compact.append({**group, "name": display_name})
    return compact


def _heresphere_library_where(cfg) -> str:
    """Return the SQL visibility rule for the HereSphere-facing library."""
    if bool(getattr(cfg.server, "heresphere_ready_only", True)):
        return "status='ready' AND (auto_hsp IS NOT NULL OR manual_hsp IS NOT NULL)"
    return "status!='missing'"


def _heresphere_library_diagnostic(payload: dict[str, object], *, ready_only: bool, compact: bool) -> str:
    groups = payload.get("library") or []
    if not isinstance(groups, list):
        groups = []
    total_links = 0
    unique_links: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        links = group.get("list") or []
        if not isinstance(links, list):
            continue
        total_links += len(links)
        unique_links.update(str(item) for item in links)
    encoded_bytes = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    return (
        f"groups={len(groups)} links={total_links} unique={len(unique_links)} "
        f"bytes={encoded_bytes} ready_only={str(bool(ready_only)).lower()} "
        f"compact={str(bool(compact)).lower()}"
    )


def create_app(cfg) -> FastAPI:
    db = DB(cfg.db_path)
    library = Library(db, cfg)
    live = LiveHub()

    def request_sidecar_sync(reason: str) -> None:
        """Coalesce sidecar synchronization requests into one background lane."""
        app.state.sidecar_sync_requested = True
        app.state.sidecar_sync_reason = str(reason or "requested")
        wakeup = getattr(app.state, "sidecar_sync_wakeup", None)
        if wakeup is not None:
            wakeup.set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cfg.thumbs_dir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        previous_exception_handler = loop.get_exception_handler()

        def quiet_expected_disconnects(event_loop, context):
            exc = context.get("exception")
            if isinstance(exc, ConnectionResetError) and getattr(exc, "winerror", None) == 10054:
                return
            if previous_exception_handler is not None:
                previous_exception_handler(event_loop, context)
            else:
                event_loop.default_exception_handler(context)

        loop.set_exception_handler(quiet_expected_disconnects)
        live.bind(loop)
        app.state.sidecar_sync_wakeup = asyncio.Event()
        print("[HereTune HereSphere] Filename-bound Web API scene identities active.", flush=True)
        reconciled_hsps = await _daemon_to_thread(_reconcile_auto_hsps_for_heresphere, db, cfg)
        if reconciled_hsps:
            print(
                f"[HereTune HereSphere] Reconciled {reconciled_hsps} Auto HSP "
                f"profile{'s' if reconciled_hsps != 1 else ''} with current filename/layout geometry; "
                "saved color analysis was reused.",
                flush=True,
            )
        current_policy_signature = policy_signature(cfg)
        stored_policy_signature = db.get_setting("auto_hsp_policy_signature")
        if stored_policy_signature != current_policy_signature:
            policy_result = await _daemon_to_thread(_rebuild_auto_hsps_for_policy, db, cfg, write_sidecars=False)
            db.set_setting("auto_hsp_policy_signature", current_policy_signature)
            if policy_result["rebuilt"]:
                print(
                    f"[HereTune HSP Policy] Rebuilt {policy_result['rebuilt']} Auto HSP "
                    f"profile{'s' if policy_result['rebuilt'] != 1 else ''} from saved evidence; "
                    "no media re-analysis.",
                    flush=True,
                )
            if bool(getattr(cfg.hsp, "write_auto_sidecars", False)):
                request_sidecar_sync("startup-policy-refresh")
        elif bool(getattr(cfg.hsp, "write_auto_sidecars", False)):
            request_sidecar_sync("startup")
        stop = asyncio.Event()
        app.state.scan_wakeup = asyncio.Event()

        async def scan_loop() -> None:
            while not stop.is_set():
                if app.state.work_paused:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=0.5)
                    except asyncio.TimeoutError:
                        pass
                    continue
                if not library_roots(cfg):
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                    continue
                app.state.scan_wakeup.clear()
                app.state.scan_running = True
                live.publish("scan", state="started")
                try:
                    result = await _daemon_to_thread(library.scan)
                    db.exec("DELETE FROM settings WHERE key='last_scan_error'")
                    live.publish("library", reason="scan", **{k: v for k, v in result.items() if k != "errors"})
                except Exception as exc:
                    db.exec(
                        """INSERT INTO settings(key,value) VALUES('last_scan_error',?)
                           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                        (str(exc),),
                    )
                finally:
                    app.state.scan_running = False
                    live.publish("scan", state="finished")
                try:
                    await asyncio.wait_for(app.state.scan_wakeup.wait(), timeout=cfg.library.scan_interval_seconds)
                    app.state.scan_wakeup.clear()
                except asyncio.TimeoutError:
                    pass

        async def metadata_loop() -> None:
            while not stop.is_set():
                if app.state.work_paused:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=0.5)
                    except asyncio.TimeoutError:
                        pass
                    continue
                metadata_cfg = copy.deepcopy(cfg)
                plan = await _daemon_to_thread(resource_worker_plan, metadata_cfg.analysis, scheduler_library_root(metadata_cfg))
                app.state.worker_plan = plan.dict()
                workers = max(1, int(plan.probe_workers))
                rows = db.needs_metadata(workers)
                if rows:
                    await asyncio.gather(*(
                        _daemon_to_thread(probe_metadata, db, metadata_cfg, row)
                        for row in rows
                    ), return_exceptions=True)
                    live.publish("library", reason="metadata")
                else:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass

        async def preview_loop() -> None:
            """Fill browse-preview cache only when readiness work is idle.

            Preview generation is cosmetic. It must never steal the front of the
            analysis queue or make the queue counter fall while videos are merely
            getting thumbnails. Explicit Regenerate Thumbnail requests still run
            immediately; automatic cache filling waits for queued analysis to drain.
            """
            while not stop.is_set():
                if app.state.work_paused or app.state.analysis_progress is not None or db.queued() is not None:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                    continue
                candidate = next((
                    row for row in db.iter_video_rows(
                        "id,path,rel_path,title,status,metadata_json,layout_override_json,thumb_path",
                        "status='ready'"
                    )
                    if not thumbnail_is_current(row)
                ), None)
                if candidate is None:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                    continue
                app.state.preview_running = str(candidate.get("id") or "")
                try:
                    await _daemon_to_thread(ensure_thumbnail, db, cfg, candidate, force=True)
                    live.publish("library", reason="thumbnail-ready", video_id=str(candidate.get("id") or ""))
                finally:
                    app.state.preview_running = None

        async def sidecar_sync_loop() -> None:
            """Low-cost, non-blocking lane for retroactive Auto-HSP sidecars.

            It is intentionally separate from the video-analysis queue. Multiple
            requests coalesce, and disabling the global option cancels the current
            pass at the next file boundary without deleting existing sidecars.
            """
            while not stop.is_set():
                if not bool(getattr(app.state, "sidecar_sync_requested", False)):
                    app.state.sidecar_sync_wakeup.clear()
                    try:
                        await asyncio.wait_for(app.state.sidecar_sync_wakeup.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if stop.is_set():
                        break
                if not bool(getattr(cfg.hsp, "write_auto_sidecars", False)):
                    app.state.sidecar_sync_requested = False
                    app.state.sidecar_sync_progress = None
                    continue

                reason = str(getattr(app.state, "sidecar_sync_reason", "requested") or "requested")
                app.state.sidecar_sync_requested = False
                app.state.sidecar_sync_wakeup.clear()

                def progress(payload: dict[str, object]) -> None:
                    state = {**dict(payload), "running": payload.get("stage") != "done", "reason": reason}
                    # Keep the high-frequency status/SSE payload compact. Detailed
                    # conflict text stays in the worker result rather than being
                    # broadcast on every dashboard status update.
                    state.pop("details", None)
                    if state.get("stage") == "done":
                        app.state.last_sidecar_sync = state
                    else:
                        app.state.sidecar_sync_progress = state
                    live.publish("sidecar-sync", **state)

                try:
                    result = await _daemon_to_thread(
                        _sync_auto_sidecars,
                        db,
                        cfg,
                        progress=progress,
                        is_active=lambda video_id: _video_is_active(app, video_id),
                        should_continue=lambda: bool(getattr(cfg.hsp, "write_auto_sidecars", False)),
                    )
                    app.state.pending_sidecar_sync.update(set(result.get("skipped") or []))
                    final = {**result, "running": False, "reason": reason, "completed_at": datetime.now(timezone.utc).isoformat()}
                    final.pop("details", None)
                    app.state.last_sidecar_sync = final
                    print(
                        f"[HereTune HSP Sidecars] background sync · written {result['written']} · "
                        f"current {result['unchanged']} · conflicts {result['conflicts']} · "
                        f"deferred {result['deferred']} · errors {result['errors']}",
                        flush=True,
                    )
                    live.publish("sidecar-sync", state="complete", **final)
                    live.publish("library", reason="hsp-sidecars-synced")
                except Exception as exc:
                    failure = {
                        "running": False,
                        "reason": reason,
                        "error": f"{type(exc).__name__}: {exc}",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }
                    app.state.last_sidecar_sync = failure
                    live.publish("sidecar-sync", state="error", **failure)
                    print(f"[HereTune HSP Sidecars] background sync failed · {failure['error']}", flush=True)
                finally:
                    app.state.sidecar_sync_progress = None

        async def maintenance_loop() -> None:
            """Low-priority housekeeping lane that always yields to analysis work."""
            while not stop.is_set():
                queue_busy = app.state.work_paused or app.state.analysis_progress is not None or db.queued() is not None or app.state.scan_running
                if queue_busy:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                    continue
                app.state.maintenance_running = "cache"
                try:
                    await _daemon_to_thread(AnalysisCache.prune, cfg.analysis_cache_dir, float(getattr(cfg.analysis, "analysis_cache_max_gb", 20.0)))
                    last_backup = db.get_setting("last_auto_backup")
                    due = True
                    if last_backup:
                        try:
                            due = datetime.now(timezone.utc) - datetime.fromisoformat(last_backup) >= timedelta(hours=24)
                        except ValueError:
                            due = True
                    if due:
                        app.state.maintenance_running = "backup"
                        cfg.backups_dir.mkdir(parents=True, exist_ok=True)
                        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                        await _daemon_to_thread(db.backup_to, cfg.backups_dir / f"auto-{stamp}.sqlite3")
                        _rotate_named_files(cfg.backups_dir, "auto-*.sqlite3", 7)
                        db.set_setting("last_auto_backup", datetime.now(timezone.utc).isoformat())
                except Exception as exc:
                    db.set_setting("last_maintenance_error", f"{type(exc).__name__}: {exc}"[:2000])
                finally:
                    app.state.maintenance_running = None
                try:
                    await asyncio.wait_for(stop.wait(), timeout=300.0)
                except asyncio.TimeoutError:
                    pass

        async def work_loop() -> None:
            while not stop.is_set():
                if app.state.work_paused:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=0.5)
                    except asyncio.TimeoutError:
                        pass
                    continue
                row = db.queued()
                if row:
                    # Freeze configuration for the lifetime of one video. Saving
                    # Settings while analysis is running affects the next job, not
                    # a half-processed movie.
                    job_cfg = copy.deepcopy(cfg)

                    def worker_plan_provider():
                        plan = resource_worker_plan(job_cfg.analysis, scheduler_library_root(job_cfg), max_age=1.0)
                        app.state.worker_plan = plan.dict()
                        live.publish("resources", plan=plan.dict())
                        return plan

                    def progress(payload):
                        app.state.analysis_progress = dict(payload)
                        app.state.analysis_progress_updated_at = time.monotonic()
                        live.publish("analysis", **dict(payload))
                    try:
                        if bool(getattr(job_cfg.analysis, "isolate_analysis_worker", True)):
                            await _daemon_to_thread(run_isolated_process, db, job_cfg, row, progress)
                        else:
                            await _daemon_to_thread(process, db, job_cfg, row, progress, worker_plan_provider)
                        finished = db.one("SELECT status,error FROM videos WHERE id=?", (str(row["id"]),)) or {}
                        if str(finished.get("status") or "") == "error":
                            failure = {
                                "video_id": str(row["id"]),
                                "title": str(row.get("title") or row["id"]),
                                "error": str(finished.get("error") or "Unknown analysis failure"),
                                "occurred_at": datetime.now(timezone.utc).isoformat(),
                                "queue_continues": True,
                            }
                            app.state.last_analysis_failure = failure
                            live.publish("library", reason="analysis-failed", **failure)
                        else:
                            completed_id = str(row["id"])
                            if bool(getattr(job_cfg.hsp, "write_auto_sidecars", False)):
                                if _video_is_active(app, completed_id):
                                    app.state.pending_sidecar_sync.add(completed_id)
                                else:
                                    completed_video = db.get(completed_id)
                                    if completed_video and completed_video.get("auto_hsp"):
                                        try:
                                            await _daemon_to_thread(
                                                _write_auto_sidecar_for_video,
                                                db,
                                                job_cfg,
                                                completed_video,
                                                replace=False,
                                            )
                                        except (SidecarConflict, SidecarError) as exc:
                                            print(
                                                f"[HereTune HSP Sidecars] analysis sync skipped "
                                                f"{_source_filename(completed_video)} · {exc}",
                                                flush=True,
                                            )
                            live.publish("library", reason="analysis-complete", video_id=completed_id)
                    finally:
                        app.state.analysis_progress = None
                        app.state.analysis_progress_updated_at = None
                        live.publish("status", reason="worker-idle")
                else:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass

        tasks = [
            asyncio.create_task(scan_loop()),
            asyncio.create_task(metadata_loop()),
            asyncio.create_task(work_loop()),
            asyncio.create_task(preview_loop()),
            asyncio.create_task(sidecar_sync_loop()),
            asyncio.create_task(maintenance_loop()),
        ]
        try:
            yield
        finally:
            stop.set()
            if getattr(app.state, "sidecar_sync_wakeup", None) is not None:
                app.state.sidecar_sync_wakeup.set()
            # Cancel the orchestration tasks first, then terminate any FFmpeg/
            # FFprobe children they own. Blocking work runs in daemon threads, so
            # a dead NAS or interrupted decoder cannot hold the Python process open.
            for task in tasks:
                task.cancel()
            terminate_all_processes()
            await asyncio.gather(*tasks, return_exceptions=True)
            # A row interrupted between status='analyzing' and completion is safe
            # to retry next launch.
            db.exec("UPDATE videos SET status='queued' WHERE status='analyzing'")
            loop.set_exception_handler(previous_exception_handler)

    app = FastAPI(title="HereTune", version=__version__, lifespan=lifespan, generate_unique_id_function=_generate_unique_operation_id)
    app.state.db = db
    app.state.library = library
    app.state.config = cfg
    app.state.scan_running = False
    app.state.work_paused = bool(getattr(cfg.server, "start_paused", False))
    app.state.active_playback = {}
    app.state.pending_hsp_rebuild = set()
    app.state.pending_sidecar_sync = set()
    app.state.sidecar_sync_requested = False
    app.state.sidecar_sync_reason = None
    app.state.sidecar_sync_wakeup = None
    app.state.sidecar_sync_progress = None
    app.state.last_sidecar_sync = None
    app.state.hsp_reload_suppressed_until = {}
    app.state.playback_hsp_advertised = {}
    app.state.hsp_revision_cache = {}
    app.state.analysis_progress = None
    app.state.maintenance_running = None
    app.state.last_analysis_failure = None
    app.state.analysis_progress_updated_at = None
    app.state.preview_running = None
    app.state.worker_plan = {}
    app.state.live = live
    app.state.thumbnail_semaphore = asyncio.Semaphore(2)

    @app.middleware("http")
    async def dashboard_security(request: Request, call_next):
        if _dashboard_path(request.url.path) and not _dashboard_authorized(request, cfg):
            return Response(
                "HereTune dashboard authentication required.",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="HereTune"'},
            )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return response

    static_dir = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    @app.get("/ui", include_in_schema=False)
    async def dashboard():
        return FileResponse(
            static_dir / "index.html",
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/live")
    async def live_events():
        return StreamingResponse(
            live.stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/watch/{video_id}", include_in_schema=False)
    async def web_player(video_id: str):
        if not db.get(video_id):
            raise HTTPException(404, "Video not found")
        return FileResponse(
            static_dir / "player.html",
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/status")
    async def status():
        try:
            resolved_mode, caps = await _daemon_to_thread(
                resolve_execution_mode, cfg.analysis, cfg.paths.ffmpeg
            )
            accel = ({**caps.dict(), "usable": caps.usable} if caps else None)
        except Exception as exc:
            resolved_mode, accel = "cpu", {"usable": False, "detail": str(exc)}
        # Populate scheduler telemetry immediately for a freshly opened dashboard
        # instead of waiting for the background metadata loop to run first.
        try:
            plan = await _daemon_to_thread(
                resource_worker_plan, cfg.analysis, scheduler_library_root(cfg), max_age=1.5
            )
            app.state.worker_plan = plan.dict()
        except Exception:
            pass
        return _status_payload(db, cfg, app, resolved_mode, accel)

    @app.get("/api/doctor")
    async def doctor():
        return await _daemon_to_thread(_doctor_payload, cfg)

    @app.get("/api/setup/status")
    async def setup_status():
        roots = library_roots(cfg)
        root_states = []
        for root_text in roots:
            try:
                reachable = bool(Path(root_text).is_dir())
            except OSError:
                reachable = False
            root_states.append({"root": root_text, "reachable": reachable})
        ffmpeg_ok = bool(shutil.which(str(cfg.paths.ffmpeg)) or Path(str(cfg.paths.ffmpeg)).is_file())
        ffprobe_ok = bool(shutil.which(str(cfg.paths.ffprobe)) or Path(str(cfg.paths.ffprobe)).is_file())
        try:
            resolved_mode, caps = await _daemon_to_thread(
                resolve_execution_mode, cfg.analysis, cfg.paths.ffmpeg
            )
            acceleration = {
                "resolved_mode": resolved_mode,
                "cuda_usable": bool(caps and caps.usable),
                "detail": str(getattr(caps, "detail", "") or "") if caps else "",
            }
        except Exception as exc:
            acceleration = {"resolved_mode": "cpu", "cuda_usable": False, "detail": str(exc)}
        return {
            "required": not bool(roots),
            "library_roots": roots,
            "library_root_status": root_states,
            "library_reachable": bool(root_states and all(item["reachable"] for item in root_states)),
            "ffmpeg_ok": ffmpeg_ok,
            "ffprobe_ok": ffprobe_ok,
            "acceleration": acceleration,
            "presets": analysis_presets(),
            "version": __version__,
        }

    @app.post("/api/setup/test-library")
    async def setup_test_library(request: Request):
        body = await request_json(request)
        root_text = str(body.get("root") or "").strip()
        if not root_text:
            return {"ok": False, "detail": "Choose a local or UNC media-library folder."}
        root = Path(root_text)
        return {
            "ok": root.is_dir(),
            "detail": "Library folder is reachable." if root.is_dir() else "That folder is not reachable from the HereTune computer.",
            "root": root_text,
        }

    @app.post("/api/setup/complete")
    async def setup_complete(request: Request):
        body = await request_json(request)
        raw_roots = body.get("library_roots")
        if not isinstance(raw_roots, list):
            raise HTTPException(422, "library_roots must be an array")
        roots = [str(value or "").strip() for value in raw_roots if str(value or "").strip()]
        preset_name = str(body.get("preset") or "balanced").strip().lower()
        if not roots:
            raise HTTPException(422, "Add at least one media folder")
        unreachable = []
        for root_text in roots:
            try:
                ok = Path(root_text).is_dir()
            except OSError:
                ok = False
            if not ok:
                unreachable.append(root_text)
        if unreachable:
            raise HTTPException(422, f"Media folder not reachable: {unreachable[0]}")
        presets = analysis_presets()
        if preset_name not in presets:
            raise HTTPException(422, "Unknown analysis preset")
        patch = {
            "library": {"roots": roots},
            "analysis": dict(presets[preset_name]["values"]),
        }
        saved = await _daemon_to_thread(save_config_patch, cfg.config_path or "config.toml", patch)
        _runtime_apply_setup(cfg, saved)
        live.publish("library", reason="setup-complete")
        return {
            "ok": True,
            "message": "First-run setup saved. HereTune can start scanning immediately.",
            "config": public_config(saved),
        }

    @app.get("/api/config")
    async def get_config():
        # Read the file on every request so the Settings dialog reflects the
        # last saved values even before the running process is restarted.
        disk_cfg = await _daemon_to_thread(load_config, cfg.config_path or "config.toml")
        return public_config(disk_cfg)

    @app.put("/api/config")
    async def put_config(request: Request):
        body = await request_json(request)
        if not isinstance(body, dict):
            raise HTTPException(400, "Settings payload must be an object")
        old_policy_signature = policy_signature(cfg)
        old_sidecar_enabled = bool(getattr(cfg.hsp, "write_auto_sidecars", False))
        try:
            saved = await _daemon_to_thread(
                save_config_patch, cfg.config_path or "config.toml", body
            )
        except (TypeError, ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

        applied, restart_required_for = _runtime_apply_config(cfg, saved)
        library_changed = any(key.startswith("library.") for key in applied)
        analysis_changed = any(key.startswith("analysis.") for key in applied)
        startup_preference_changed = "server.start_paused" in applied
        if library_changed and getattr(app.state, "scan_wakeup", None) is not None:
            app.state.scan_wakeup.set()

        active_ids = _active_playback_ids(app)
        hsp_policy_result: dict[str, object] | None = None
        sidecar_sync_scheduled = False
        new_policy_signature = policy_signature(cfg)
        if new_policy_signature != old_policy_signature:
            hsp_policy_result = await _daemon_to_thread(
                _rebuild_auto_hsps_for_policy, db, cfg, skip_ids=active_ids, write_sidecars=False
            )
            deferred = set(hsp_policy_result.get("skipped") or [])
            app.state.pending_hsp_rebuild.update(deferred)
            # A live video is intentionally skipped to avoid replacing an HSP
            # while HereSphere may still be rendering it. Keep the persisted
            # signature dirty until every deferred rebuild completes, so an
            # unexpected server/headset exit cannot strand that video on the
            # previous global policy forever.
            db.set_setting(
                "auto_hsp_policy_signature",
                "" if deferred else new_policy_signature,
            )
            live.publish(
                "library", reason="hsp-policy-rebuilt",
                rebuilt=int(hsp_policy_result.get("rebuilt") or 0),
                deferred=len(deferred),
            )
        if bool(getattr(cfg.hsp, "write_auto_sidecars", False)) and (
            not old_sidecar_enabled or hsp_policy_result is not None
        ):
            request_sidecar_sync("enabled" if not old_sidecar_enabled else "policy-refresh")
            sidecar_sync_scheduled = True
        elif old_sidecar_enabled and not bool(getattr(cfg.hsp, "write_auto_sidecars", False)):
            # The background worker notices this flag at the next file boundary.
            # Existing sidecars are intentionally left untouched.
            app.state.sidecar_sync_requested = False
            if getattr(app.state, "sidecar_sync_wakeup", None) is not None:
                app.state.sidecar_sync_wakeup.set()

        live.publish("status", reason="config-saved", applied=applied)
        if library_changed:
            live.publish("library", reason="config-roots-changed")

        if restart_required_for:
            message = "Settings saved and applied where possible. Restart required for: " + ", ".join(restart_required_for) + "."
        else:
            message = "Settings saved and applied."
        if hsp_policy_result:
            message += f" Rebuilt {int(hsp_policy_result.get('rebuilt') or 0)} Auto HSPs from saved analysis."
            if hsp_policy_result.get("skipped"):
                message += " Videos currently open in HereSphere will update after they close."
        if sidecar_sync_scheduled:
            message += " Existing Ready videos will synchronize sidecar HSPs in the background."
        return {
            "ok": True,
            "message": message,
            "restart_required": bool(restart_required_for),
            "restart_required_for": restart_required_for,
            "applied": applied,
            "analysis_changes_apply_next_job": analysis_changed,
            "startup_preference_applies_next_launch": startup_preference_changed,
            "hsp_policy_result": hsp_policy_result,
            "sidecar_sync_scheduled": sidecar_sync_scheduled,
            "config": public_config(saved),
        }

    @app.get("/api/videos")
    async def videos(
        status: str | None = None,
        q: str | None = None,
        favorite: Literal["all", "favorite", "not_favorite"] = "all",
        rating: RatingFilter = "all",
        watch: Literal["all", "continue", "completed", "never"] = "all",
        duplicates: Literal["all", "duplicates"] = "all",
        order: Literal["title", "recent", "status", "confidence", "rating", "favorite", "priority", "watched", "plays"] = "title",
        limit: int = Query(default=250, ge=1, le=5000),
        offset: int = Query(default=0, ge=0),
    ):
        ids, total = _filtered_video_ids(
            db, status=status, q=q, favorite=favorite, rating=rating, watch=watch,
            duplicates=duplicates, order=order,
        )
        page_ids = ids[offset:offset + limit]
        page: list[dict[str, object]] = []
        for video_id in page_ids:
            video = db.get(video_id)
            if video is not None:
                page.append(_video_view(video, cfg))
        return {"items": page, "total": total, "limit": limit, "offset": offset}

    @app.get("/api/video-ids")
    async def video_ids(
        status: str | None = None,
        q: str | None = None,
        favorite: Literal["all", "favorite", "not_favorite"] = "all",
        rating: RatingFilter = "all",
        watch: Literal["all", "continue", "completed", "never"] = "all",
        duplicates: Literal["all", "duplicates"] = "all",
        order: Literal["title", "recent", "status", "confidence", "rating", "favorite", "priority", "watched", "plays"] = "title",
    ):
        ids, total = _filtered_video_ids(
            db, status=status, q=q, favorite=favorite, rating=rating, watch=watch,
            duplicates=duplicates, order=order,
        )
        return {"ids": ids, "total": total}

    @app.post("/api/videos/bulk-priority")
    async def bulk_priority(request: Request):
        body = await request_json(request)
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
            raise HTTPException(422, "ids must be a non-empty array of video IDs")
        if len(ids) > 10000:
            raise HTTPException(422, "Bulk priority is limited to 10,000 videos per request")
        priority = bool(body.get("priority", True))
        # Priority insertion is always FIFO: new requests append to the tail.
        # Users can still explicitly reorder the existing priority queue later.
        unique_ids = list(dict.fromkeys(ids))
        changed = 0
        skipped = 0
        if not priority:
            marks = ",".join("?" for _ in unique_ids)
            db.exec(f"UPDATE videos SET manual_priority=0, priority_requested_at=NULL, priority_rank=NULL WHERE id IN ({marks})", tuple(unique_ids))
            changed = len(unique_ids)
        else:
            eligible: list[str] = []
            for video_id in unique_ids:
                row = db.get(video_id)
                if not row or str(row.get("status") or "") not in {"queued", "changed", "error"}:
                    skipped += 1
                    continue
                if str(row.get("status") or "") == "error":
                    db.exec("UPDATE videos SET status='queued', error=NULL WHERE id=?", (video_id,))
                eligible.append(video_id)
            start_rank = db.next_priority_rank()
            stamp = now()
            for offset, video_id in enumerate(eligible):
                db.exec(
                    "UPDATE videos SET manual_priority=1, priority_requested_at=?, priority_rank=? WHERE id=?",
                    (stamp, start_rank + offset, video_id),
                )
            changed = len(eligible)
        live.publish("queue", reason="bulk-priority", changed=changed, skipped=skipped, priority=priority)
        return {"ok": True, "changed": changed, "skipped": skipped, "priority": priority}

    @app.get("/api/videos/{video_id}")
    async def video_detail(video_id: str):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        view = _video_view(video, cfg)
        view["heresphere_playback_active"] = _video_is_active(app, video_id)
        if video.get("duplicate_group"):
            peers = db.all(
                "SELECT id,title,rel_path,size FROM videos WHERE duplicate_group=? AND status!='missing' AND id!=? ORDER BY rel_path COLLATE NOCASE",
                (video["duplicate_group"], video_id),
            )
            view["duplicates"] = peers
        else:
            view["duplicates"] = []
        return view

    @app.patch("/api/videos/{video_id}/hsp-diagnostic")
    async def set_hsp_diagnostic(video_id: str, request: Request):
        """Select Image controls to neutralize only in the served Auto HSP."""
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        if _video_is_active(app, video_id):
            raise HTTPException(409, "Close this video in HereSphere before changing its diagnostic HSP.")
        if not video.get("analysis") or "reason" in (video.get("analysis") or {}):
            raise HTTPException(409, "A completed analysis is required")
        body = await request_json(request)
        current = dict(video.get("layout_override") or {})

        if bool(body.get("reset")):
            current.pop(_HSP_DIAGNOSTIC_KEY, None)
            disabled: list[str] = []
        else:
            raw_disabled = body.get("disabled", [])
            if not isinstance(raw_disabled, list):
                raise HTTPException(422, "disabled must be a JSON array")
            unknown = sorted({str(value) for value in raw_disabled} - _HSP_DIAGNOSTIC_CONTROLS)
            if unknown:
                raise HTTPException(422, f"Unknown HSP diagnostic control(s): {', '.join(unknown)}")
            disabled = sorted({str(value) for value in raw_disabled})
            if disabled:
                current[_HSP_DIAGNOSTIC_KEY] = {"disabled": disabled}
            else:
                current.pop(_HSP_DIAGNOSTIC_KEY, None)

        db.exec(
            "UPDATE videos SET layout_override_json=?, profile_mode='auto' WHERE id=?",
            (json.dumps(current), video_id),
        )
        updated = db.get(video_id)
        state = _hsp_diagnostic_state(updated or video)
        print(
            f"[HereTune HSP Lab] {_source_filename(updated or video)} · "
            f"token={state['token']} · disabled="
            f"{','.join(state['disabled']) if state['disabled'] else 'none'}",
            flush=True,
        )
        live.publish("library", reason="hsp-diagnostic", video_id=video_id)
        return _video_view(updated or video, cfg)

    @app.patch("/api/videos/{video_id}/hsp-policy")
    async def set_video_hsp_policy(video_id: str, request: Request):
        """Persist Inherit / Force On / Force Off Auto-HSP controls."""
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        body = await request_json(request)
        if bool(body.get("reset")):
            overrides: dict[str, str] = {}
        else:
            raw = body.get("overrides", {})
            if not isinstance(raw, dict):
                raise HTTPException(422, "overrides must be an object")
            unknown = sorted(set(str(key) for key in raw) - set(AUTO_IMAGE_CONTROLS))
            if unknown:
                raise HTTPException(422, f"Unknown Auto-HSP control(s): {', '.join(unknown)}")
            invalid = [
                str(key) for key, value in raw.items()
                if str(value or "inherit").lower() not in {"inherit", "on", "off"}
            ]
            if invalid:
                raise HTTPException(422, "Policy values must be inherit, on, or off")
            overrides = normalize_policy_overrides(raw)

        db.exec(
            "UPDATE videos SET hsp_policy_override_json=? WHERE id=?",
            (json.dumps(overrides) if overrides else None, video_id),
        )
        active = _video_is_active(app, video_id)
        rebuilt = False
        sidecar_written = False
        if active:
            app.state.pending_hsp_rebuild.add(video_id)
        else:
            updated = db.get(video_id)
            blob = _rebuild_auto_hsp(updated or video, cfg)
            if blob is not None:
                db.exec(
                    "UPDATE videos SET auto_hsp=?,auto_hsp_version=? WHERE id=?",
                    (blob, int(cfg.hsp.generated_version), video_id),
                )
                rebuilt = True
                updated = db.get(video_id) or updated or video
                if bool(getattr(cfg.hsp, "write_auto_sidecars", False)):
                    try:
                        _write_auto_sidecar_for_video(db, cfg, updated, replace=False)
                        sidecar_written = True
                    except (SidecarConflict, SidecarError):
                        pass
        live.publish(
            "library", reason="video-hsp-policy", video_id=video_id,
            rebuilt=rebuilt, deferred=active,
        )
        payload = await video_detail(video_id)
        payload["policy_update"] = {
            "rebuilt": rebuilt,
            "deferred_until_close": active,
            "sidecar_written": sidecar_written,
        }
        return payload

    @app.post("/api/videos/{video_id}/hsp-sidecar")
    async def write_video_hsp_sidecar(video_id: str, request: Request):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        if _video_is_active(app, video_id):
            raise HTTPException(
                409,
                "Close this video in HereSphere before replacing its sidecar HSP. "
                "This avoids a live player-side HSP reload.",
            )
        body = await request_json(request)
        replace = bool(body.get("replace", False))
        try:
            result = await _daemon_to_thread(
                _write_auto_sidecar_for_video, db, cfg, video, replace=replace
            )
        except SidecarConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except SidecarError as exc:
            raise HTTPException(422, str(exc)) from exc
        live.publish("library", reason="hsp-sidecar", video_id=video_id)
        return {"ok": True, "result": result, "video": await video_detail(video_id)}

    @app.delete("/api/videos/{video_id}/hsp-sidecar")
    async def delete_video_hsp_sidecar(video_id: str):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        if _video_is_active(app, video_id):
            raise HTTPException(409, "Close this video in HereSphere before removing its sidecar HSP.")
        try:
            removed = await _daemon_to_thread(remove_managed_sidecar, video)
        except SidecarConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except SidecarError as exc:
            raise HTTPException(422, str(exc)) from exc
        db.exec(
            "UPDATE videos SET sidecar_hsp_path=NULL,sidecar_hsp_revision=NULL,sidecar_hsp_managed=0 WHERE id=?",
            (video_id,),
        )
        live.publish("library", reason="hsp-sidecar-removed", video_id=video_id)
        return {"ok": True, "removed": str(removed), "video": await video_detail(video_id)}

    @app.patch("/api/videos/{video_id}/layout")
    async def set_layout_override(video_id: str, request: Request):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        body = await request_json(request)
        current = dict(video.get("layout_override") or {})
        if body.get("reset"):
            diagnostic_state = current.get(_HSP_DIAGNOSTIC_KEY)
            current = {}
            if diagnostic_state:
                current[_HSP_DIAGNOSTIC_KEY] = diagnostic_state
        stereo_allowed = {"auto", "mono", "sbs", "tb"}
        projection_allowed = {"auto", "perspective", "equirectangular", "equirectangular360", "fisheye", "cubemap", "equiangularCubemap"}
        if "stereo" in body:
            value = str(body.get("stereo") or "auto")
            if value not in stereo_allowed:
                raise HTTPException(422, "Unknown stereo layout")
            if value == "auto": current.pop("stereo", None)
            else: current["stereo"] = value
        if "projection" in body:
            value = str(body.get("projection") or "auto")
            if value not in projection_allowed:
                raise HTTPException(422, "Unknown projection")
            if value == "auto": current.pop("projection", None)
            else: current["projection"] = value
        if "eye" in body:
            value = str(body.get("eye") or "auto").lower()
            if value not in {"auto", "left", "right"}:
                raise HTTPException(422, "eye must be auto, left or right")
            if value == "auto": current.pop("isEyeSwapped", None)
            else: current["isEyeSwapped"] = value == "right"
        if "fov" in body:
            value = body.get("fov")
            if value in {None, "", "auto"}:
                current.pop("fov", None)
            else:
                try: fov = float(value)
                except (TypeError, ValueError) as exc: raise HTTPException(422, "fov must be a number") from exc
                if not 30 <= fov <= 360: raise HTTPException(422, "fov must be between 30 and 360")
                current["fov"] = fov
        if "lens" in body:
            value = str(body.get("lens") or "").strip()
            if not value or value.casefold() == "auto": current.pop("lens", None)
            else: current["lens"] = value[:100]
        if "full_side_by_side" in body:
            value = body.get("full_side_by_side")
            if value in {None, "", "auto"}:
                current.pop("full_side_by_side", None)
            else:
                current["full_side_by_side"] = bool(value)
        if "aspect_ratio_type" in body:
            value = str(body.get("aspect_ratio_type") or "auto").strip().lower()
            allowed = {"auto", "autodetect", "standard", "highdefinition", "widescreen", "custom"}
            if value not in allowed:
                raise HTTPException(422, "aspect_ratio_type must be auto, autodetect, standard, highdefinition, widescreen or custom")
            if value == "auto":
                current.pop("aspect_ratio_type", None)
            else:
                current["aspect_ratio_type"] = value
        if "aspect_ratio" in body:
            value = body.get("aspect_ratio")
            if value in {None, "", "auto"}:
                current.pop("aspect_ratio", None)
            else:
                try:
                    ratio = float(value)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(422, "aspect_ratio must be a number") from exc
                if not 0.25 <= ratio <= 8.0:
                    raise HTTPException(422, "aspect_ratio must be between 0.25 and 8")
                current["aspect_ratio"] = ratio
        for key in ("flip_front_back", "flip_left_right", "flip_up_down"):
            if key not in body:
                continue
            value = body.get(key)
            if value in {None, "", "auto"}:
                current.pop(key, None)
            else:
                current[key] = bool(value)
        if "orientation_type" in body:
            value = str(body.get("orientation_type") or "auto").strip().lower()
            if value not in {"auto", "up", "left", "right", "down"}:
                raise HTTPException(422, "orientation_type must be auto, up, left, right or down")
            if value == "auto": current.pop("orientation_type", None)
            else: current["orientation_type"] = value

        def optional_number(key: str, low: float, high: float) -> None:
            if key not in body:
                return
            value = body.get(key)
            if value is None or value == "" or str(value).strip().casefold() == "auto":
                current.pop(key, None)
                return
            try:
                parsed = float(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(422, f"{key} must be a number or auto") from exc
            if not math.isfinite(parsed) or not low <= parsed <= high:
                raise HTTPException(422, f"{key} must be between {low:g} and {high:g}")
            current[key] = parsed

        def optional_vector(key: str, length: int, low: float, high: float) -> None:
            if key not in body:
                return
            value = body.get(key)
            if value is None or value == "" or (isinstance(value, str) and value.strip().casefold() == "auto"):
                current.pop(key, None)
                return
            if isinstance(value, str):
                raw = [part.strip() for part in value.replace("/", ",").split(",") if part.strip()]
            elif isinstance(value, (list, tuple)):
                raw = list(value)
            else:
                raise HTTPException(422, f"{key} must contain {length} comma-separated numbers")
            if len(raw) != length:
                raise HTTPException(422, f"{key} must contain exactly {length} numbers")
            try:
                parsed = [float(part) for part in raw]
            except (TypeError, ValueError) as exc:
                raise HTTPException(422, f"{key} contains a non-numeric value") from exc
            if not all(math.isfinite(part) and low <= part <= high for part in parsed):
                raise HTTPException(422, f"every {key} component must be between {low:g} and {high:g}")
            current[key] = parsed

        optional_vector("zoom", 2, 0.25, 4.0)
        optional_vector("pan", 2, -1.0, 1.0)
        optional_vector("alignment_position", 3, -1000.0, 1000.0)
        optional_vector("alignment_rotation", 3, -45.0, 45.0)
        optional_vector("stitch_shift", 4, -0.5, 0.5)
        optional_vector("stitch_scale", 4, 0.25, 4.0)
        optional_vector("stitch_shear", 4, -0.5, 0.5)
        optional_vector("stitch_flare", 4, -1.0, 1.0)
        optional_vector("stitch_slant", 4, -1.0, 1.0)
        optional_vector("orientation_rotation", 3, -180.0, 180.0)
        optional_vector("autofocus_rotation", 3, -180.0, 180.0)
        optional_number("autofocus_focal_distance", 0.0, 5000.0)
        optional_number("autofocus_min_distance", 0.0, 5000.0)
        optional_number("autofocus_max_distance", 0.0, 5000.0)
        optional_number("autofocus_transition_speed", 0.0, 20.0)
        optional_number("audio_time_offset", -10.0, 10.0)
        optional_number("audio_volume_multiplier", 0.0, 8.0)
        optional_number("sync_start_offset_seconds", -10.0, 10.0)
        optional_number("sync_drift_seconds_per_hour", -10.0, 10.0)

        db.exec("UPDATE videos SET layout_override_json=?, thumb_path=NULL WHERE id=?", (json.dumps(current), video_id))
        updated = db.get(video_id)
        # Layout/projection is part of a complete v7 HSP. Never replace the
        # player-facing bytes while this media is open in HereSphere; defer that
        # rebuild until the close event to avoid a live preset mutation.
        active = _video_is_active(app, video_id)
        hsp_deferred = False
        if updated and updated.get("auto_hsp") and updated.get("analysis"):
            if active:
                app.state.pending_hsp_rebuild.add(video_id)
                hsp_deferred = True
            else:
                rebuilt = await _daemon_to_thread(_rebuild_auto_hsp, updated, cfg)
                if rebuilt is not None:
                    db.exec(
                        "UPDATE videos SET auto_hsp=?, auto_hsp_version=? WHERE id=?",
                        (rebuilt, int(cfg.hsp.generated_version), video_id),
                    )
                    updated = db.get(video_id)
                    if bool(getattr(cfg.hsp, "write_auto_sidecars", False)):
                        try:
                            await _daemon_to_thread(
                                _write_auto_sidecar_for_video, db, cfg, updated, replace=False
                            )
                        except (SidecarConflict, SidecarError):
                            pass
        await _daemon_to_thread(ensure_thumbnail, db, cfg, updated, force=True)
        live.publish("library", reason="layout", video_id=video_id, deferred=hsp_deferred)
        payload = await video_detail(video_id)
        payload["hsp_update_deferred_until_close"] = hsp_deferred
        return payload

    @app.post("/api/videos/{video_id}/regenerate-thumbnail")
    async def regenerate_thumbnail(video_id: str):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        db.exec("UPDATE videos SET thumb_path=NULL WHERE id=?", (video_id,))
        await _daemon_to_thread(ensure_thumbnail, db, cfg, db.get(video_id), force=True)
        live.publish("library", reason="thumbnail", video_id=video_id)
        return {"ok": True, "video": await video_detail(video_id)}

    @app.get("/api/videos/{video_id}/analysis-inspector")
    async def analysis_inspector(video_id: str):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        analysis = video.get("analysis") or {}
        # Reuse the public video-view serializer so inspector scene keyframes
        # have the exact same flattened HSP-vector shape as the drawer/player.
        correction = (_video_view(video, cfg).get("correction") or {})
        return {
            "video_id": video_id,
            "duration_ms": float((video.get("metadata") or {}).get("duration") or 0.0) * 1000.0,
            "samples": analysis.get("sample_inspector") or [],
            "probes": analysis.get("scene_probe_timeline") or [],
            "scene_keyframes": correction.get("scene_keyframes") or [],
            "confidence": analysis.get("confidence_breakdown") or {},
            "rejections": {
                "total": int(analysis.get("rejected_total") or 0),
                "black_fade": int(analysis.get("rejected_black_fade") or 0),
                "title_like": int(analysis.get("rejected_title_like") or 0),
                "duplicate": int(analysis.get("rejected_duplicate") or 0),
                "coverage_trim": int(analysis.get("rejected_coverage_trim") or 0),
            },
            "cache": {
                "resume_samples": int(analysis.get("cache_resume_samples") or 0),
                "saved_samples": int(analysis.get("cache_saved_samples") or 0),
            },
            "runs": db.analysis_history(video_id, 20),
        }

    @app.get("/api/videos/{video_id}/analysis-sample.jpg")
    async def analysis_sample_image(
        video_id: str,
        kind: Literal["full", "probe", "refine"] = "full",
        time_seconds: float = Query(ge=0),
        width: int = Query(default=960, ge=160, le=1920),
    ):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        # Evidence imagery is decoded directly from the original source video
        # on demand. No analysis JPEGs are read or written.
        metadata = effective_metadata(video, video.get("metadata") or {})
        try:
            frame = await _daemon_to_thread(
                preview_frame, Path(str(video["path"])), cfg.paths.ffmpeg, metadata, "final",
                timestamp_seconds=float(time_seconds), card_width=min(960, int(width)), card_height=540,
            )
            bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            ok, payload = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            if not ok:
                raise RuntimeError("JPEG encoding failed")
            return Response(
                payload.tobytes(), media_type="image/jpeg",
                headers={"Cache-Control": "no-store, max-age=0", "X-HereTune-Frame-Source": "live-source-seek"},
            )
        except Exception:
            # One standard library thumbnail may be used as a graceful fallback;
            # it is not part of the analysis/checkpoint cache.
            thumb_path = str(video.get("thumb_path") or "")
            if thumb_path and Path(thumb_path).is_file():
                return FileResponse(
                    thumb_path, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store, max-age=0", "X-HereTune-Frame-Source": "library-thumbnail-fallback"},
                )
            raise HTTPException(500, "Could not render sample")


    @app.get("/api/library/health")
    async def library_health():
        payload = await _daemon_to_thread(_library_health_payload, db, cfg)
        payload["integrity"] = await _daemon_to_thread(db.integrity_check)
        payload["maintenance_running"] = app.state.maintenance_running
        return payload

    @app.post("/api/library/maintenance")
    async def library_maintenance(request: Request):
        body = await request_json(request)
        action = str(body.get("action") or "").strip().lower()
        if app.state.analysis_progress is not None or db.queued() is not None:
            raise HTTPException(409, "Maintenance waits for the analysis queue to become idle")
        app.state.maintenance_running = action
        try:
            if action == "backup":
                cfg.backups_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                target = await _daemon_to_thread(db.backup_to, cfg.backups_dir / f"manual-{stamp}.sqlite3")
                _rotate_named_files(cfg.backups_dir, "manual-*.sqlite3", 7)
                result = {"backup": str(target)}
            elif action == "integrity":
                result = {"integrity": await _daemon_to_thread(db.integrity_check)}
            elif action == "vacuum":
                await _daemon_to_thread(db.vacuum)
                result = {"vacuum": "complete"}
            elif action == "prune-cache":
                result = await _daemon_to_thread(AnalysisCache.prune, cfg.analysis_cache_dir, float(getattr(cfg.analysis, "analysis_cache_max_gb", 20.0)))
            elif action == "clean-thumbnails":
                known = {str(row.get("thumb_path")) for row in db.iter_video_rows("thumb_path", "thumb_path IS NOT NULL") if row.get("thumb_path")}
                removed = 0
                for path in cfg.thumbs_dir.glob("*.jpg"):
                    if str(path) not in known:
                        path.unlink(missing_ok=True); removed += 1
                result = {"removed": removed}
            else:
                raise HTTPException(422, "Unknown maintenance action")
            return {"ok": True, "action": action, "result": result, "health": _library_health_payload(db, cfg)}
        finally:
            app.state.maintenance_running = None

    @app.get("/api/videos/{video_id}/thumbnail-debug")
    async def thumbnail_debug(video_id: str, request: Request):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        metadata = video.get("metadata") or {}
        effective = thumbnail_browse_metadata(video, effective_metadata(video, metadata))
        base = public_base(request, cfg)
        rev = _thumbnail_revision(video)
        return {
            "detected": metadata.get("layout_detection") or {},
            "override": video.get("layout_override") or {},
            "effective": {key: effective.get(key) for key in ("stereo","projection","lens","fov","isEyeSwapped","thumbnail_stereo_reason")},
            "images": {
                name: f"{base}/preview/{video_id}/{name}.jpg?v={rev}"
                for name in ("source", "left", "right", "final", "before", "after")
            },
        }

    @app.get("/api/videos/{video_id}/events")
    async def video_events(video_id: str, limit: int = Query(default=25, ge=1, le=250)):
        if not db.get(video_id):
            raise HTTPException(404, "Video not found")
        return {"items": db.all("SELECT event,time_ms,speed,received_at FROM events WHERE video_id=? ORDER BY id DESC LIMIT ?", (video_id, limit))}

    @app.patch("/api/videos/{video_id}/library-state")
    async def set_library_state(video_id: str, request: Request):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        body = await request_json(request)
        updates: list[str] = []
        params: list[object] = []
        if "is_favorite" in body:
            value = body.get("is_favorite")
            if not isinstance(value, bool):
                raise HTTPException(422, "is_favorite must be true or false")
            updates.append("is_favorite=?")
            params.append(1 if value else 0)
        if "rating" in body:
            try:
                rating_value = _normalize_half_star_rating(body.get("rating"), allow_none=True)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            updates.append("rating=?")
            params.append(rating_value)
        if not updates:
            raise HTTPException(422, "No favorite or rating field was supplied")
        params.append(video_id)
        db.exec(f"UPDATE videos SET {', '.join(updates)} WHERE id=?", params)
        live.publish("library", reason="library-state", video_id=video_id)
        return _video_view(db.get(video_id), cfg)

    @app.post("/api/work/pause")
    async def set_work_pause(request: Request):
        body = await request_json(request)
        paused = bool(body.get("paused", True))
        app.state.work_paused = paused
        live.publish("status", reason="pause", paused=paused)
        return {
            "ok": True,
            "paused": paused,
            "message": (
                "Queue paused. A job already in progress will finish before the pause becomes idle."
                if paused and app.state.analysis_progress else
                "Indexing and analysis paused." if paused else
                "Indexing and analysis resumed."
            ),
        }

    @app.post("/api/scan")
    async def scan_now():
        if app.state.work_paused:
            raise HTTPException(409, "Indexing/analysis is paused. Resume work before scanning.")
        app.state.scan_running = True
        try:
            result = await _daemon_to_thread(library.scan)
            live.publish("library", reason="manual-scan")
            return result
        finally:
            app.state.scan_running = False
            live.publish("scan", state="finished")

    @app.post("/api/analyze/all")
    async def analyze_all(request: Request):
        body = await request_json(request)
        include_ready = bool(body.get("include_ready", False))
        if include_ready:
            rows = db.all("SELECT id FROM videos WHERE status!='missing'")
        else:
            rows = db.all("SELECT id FROM videos WHERE status='error'")
        for row in rows:
            db.exec(
                """UPDATE videos SET status='queued', error=NULL, analysis_json=NULL,
                   auto_hsp=NULL, auto_hsp_version=NULL, profile_mode='auto',
                   manual_priority=0, priority_requested_at=NULL, priority_rank=NULL WHERE id=?""",
                (row["id"],),
            )
        live.publish("queue", reason="bulk-requeue", queued=len(rows))
        return {"queued": len(rows), "include_ready": include_ready}

    @app.get("/api/queue")
    async def priority_queue():
        return {"items": [_video_view(video, cfg) for video in db.priority_queue()]}

    @app.put("/api/queue")
    async def reorder_priority_queue(request: Request):
        body = await request_json(request)
        ids = body.get("ids")
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise HTTPException(422, "ids must be an ordered array of video IDs")
        current = {str(video["id"]): video for video in db.priority_queue()}
        if set(ids) != set(current):
            raise HTTPException(409, "Queue changed while it was being reordered; refresh and try again")
        db.executemany(
            "UPDATE videos SET priority_rank=? WHERE id=? AND manual_priority=1",
            ((rank, video_id) for rank, video_id in enumerate(ids, 1)),
        )
        live.publish("queue", reason="reordered")
        return {"ok": True, "items": [_video_view(video, cfg) for video in db.priority_queue()]}

    @app.post("/api/videos/{video_id}/priority")
    async def set_priority(video_id: str, request: Request):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        body = await request_json(request)
        priority = bool(body.get("priority", True))
        status = str(video.get("status") or "")
        if priority and status == "error":
            rank = db.next_priority_rank()
            db.exec(
                "UPDATE videos SET status='queued', error=NULL, manual_priority=1, priority_requested_at=?, priority_rank=? WHERE id=?",
                (now(), rank, video_id),
            )
        elif priority and status in {"queued", "changed"}:
            rank = int(video.get("priority_rank") or db.next_priority_rank())
            db.exec(
                "UPDATE videos SET manual_priority=1, priority_requested_at=COALESCE(priority_requested_at, ?), priority_rank=COALESCE(priority_rank, ?) WHERE id=?",
                (now(), rank, video_id),
            )
        elif not priority:
            db.exec("UPDATE videos SET manual_priority=0, priority_requested_at=NULL, priority_rank=NULL WHERE id=?", (video_id,))
        elif status == "analyzing":
            raise HTTPException(409, "This video is already being analyzed")
        else:
            raise HTTPException(409, "Only pending/error videos can be promoted. Use Reanalyze for a ready video.")
        live.publish("queue", reason="priority", video_id=video_id, priority=priority)
        return _video_view(db.get(video_id), cfg)

    @app.post("/api/videos/{video_id}/reanalyze")
    @app.post("/api/videos/{video_id}/analyze")
    async def reanalyze(video_id: str):
        if not db.get(video_id):
            raise HTTPException(404, "Video not found")
        if _video_is_active(app, video_id):
            raise HTTPException(409, "Close this video in HereSphere before reanalyzing it.")
        db.exec(
            """UPDATE videos SET status='queued', error=NULL, analysis_json=NULL,
               auto_hsp=NULL, auto_hsp_version=NULL, profile_mode='auto',
               manual_priority=0, priority_requested_at=NULL, priority_rank=NULL WHERE id=?""",
            (video_id,),
        )
        live.publish("queue", reason="reanalyze", video_id=video_id)
        return {"ok": True, "queued": True}

    @app.post("/api/videos/{video_id}/profile")
    async def set_profile(video_id: str, request: Request):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        if _video_is_active(app, video_id):
            raise HTTPException(409, "Close this video in HereSphere before changing its active HSP profile.")
        body = await request_json(request)
        mode = str(body.get("mode") or "").lower()
        if mode not in _PROFILE_MODES:
            raise HTTPException(422, "Unknown profile mode")
        if mode == "manual" and video.get("manual_hsp") is None:
            raise HTTPException(409, "No manual HSP has been stored for this video")
        if mode in {"auto", "mild", "strong"}:
            kind, blob, _ = _profile_blob(video, cfg, mode)
            if blob is None:
                raise HTTPException(409, "This profile requires a completed automatic analysis")
            validate_hsp(blob)
        elif mode == "original":
            _, blob, _ = _profile_blob(video, cfg, mode)
            if blob is None:
                raise HTTPException(409, "Could not build a neutral profile")
            validate_hsp(blob)
        db.exec("UPDATE videos SET profile_mode=? WHERE id=?", (mode, video_id))
        app.state.hsp_reload_suppressed_until.pop(str(video_id), None)
        app.state.playback_hsp_advertised.pop(str(video_id), None)
        updated = db.get(video_id)
        live.publish("library", reason="profile", video_id=video_id)
        return _video_view(updated, cfg)

    @app.delete("/api/videos/{video_id}/manual-hsp")
    async def clear_manual_hsp(video_id: str):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        if _video_is_active(app, video_id):
            raise HTTPException(409, "Close this video in HereSphere before discarding its stored manual HSP.")
        mode = "auto" if _profile_mode(video) == "manual" else _profile_mode(video)
        db.exec(
            """UPDATE videos SET manual_hsp=NULL, manual_hsp_version=NULL,
               profile_mode=? WHERE id=?""",
            (mode, video_id),
        )
        live.publish("library", reason="manual-hsp-cleared", video_id=video_id)
        return {"ok": True, "profile_mode": mode}

    @app.post("/heresphere/auth")
    async def heresphere_auth(request: Request):
        body = await request_json(request)
        ok = authorized(request, body, cfg)
        return hs_json({
            "auth-token": auth_token(cfg) if ok else "",
            "access": 1 if ok else -1,
        })

    @app.api_route("/heresphere", methods=["GET", "POST", "HEAD"], include_in_schema=False)
    async def heresphere_index(request: Request):
        if request.method == "HEAD":
            return Response(headers={"HereSphere-JSON-Version": "1"})
        body = await request_json(request)
        if not authorized(request, body, cfg):
            return hs_json({"access": -1, "library": [{"name": "Login Failed", "list": []}]})
        base = public_base(request, cfg)
        groups: dict[str, list[str]] = {"All Movies": []}
        favorites: list[str] = []
        visibility_where = _heresphere_library_where(cfg)
        for video in db.iter_video_rows("id,path,rel_path,library_root,title,is_favorite,layout_override_json", visibility_where):
            url = f"{base}{_heresphere_video_path(video)}"
            groups["All Movies"].append(url)
            if bool(video.get("is_favorite")):
                favorites.append(url)
            root_text = str(video.get("library_root") or "").strip()
            root_name = Path(root_text.rstrip("\\/" )).name if root_text else "Library"
            if not root_name and root_text:
                root_name = root_text.rstrip("\\/")
            parent = str(Path(video["rel_path"]).parent).replace("\\", "/")
            group_name = root_name or "Library"
            if parent not in {"", "."}:
                group_name = f"{group_name} / {parent}"
            groups.setdefault(group_name, []).append(url)
        library_groups = [{"name": "Favorites", "list": favorites}] if favorites else []
        library_groups += [{"name": name, "list": urls} for name, urls in groups.items()]
        if bool(getattr(cfg.server, "compact_heresphere_library_labels", False)):
            # Presentation-only compatibility mode for HereSphere's non-scrolling
            # library-group UI. The ordering comes from this response only; there
            # is deliberately no DB/config mapping to maintain.
            library_groups = _compact_library_groups(library_groups)
        payload = {
            "access": 1,
            "library": library_groups,
        }
        print(
            "[HereTune HereSphere] LIBRARY "
            + _heresphere_library_diagnostic(
                payload,
                ready_only=bool(getattr(cfg.server, "heresphere_ready_only", True)),
                compact=bool(getattr(cfg.server, "compact_heresphere_library_labels", False)),
            ),
            flush=True,
        )
        return hs_json(payload)

    @app.api_route("/heresphere/scan", methods=["GET", "POST"], include_in_schema=False)
    async def heresphere_scan(request: Request):
        body = await request_json(request)
        if not authorized(request, body, cfg):
            return hs_json({"access": -1, "scanData": []})
        base = public_base(request, cfg)
        scan_data = []
        visibility_where = _heresphere_library_where(cfg)
        for video in db.iter_video_rows("id,path,rel_path,library_root,title,status,date_added,is_favorite,rating,metadata_json,layout_override_json", visibility_where):
            metadata = _heresphere_metadata(video)
            item = {
                "link": f"{base}{_heresphere_video_path(video)}",
                "title": video["title"],
                "dateAdded": video["date_added"][:10],
                "duration": float(metadata.get("duration") or 0.0) * 1000.0,
                "isFavorite": bool(video.get("is_favorite")),
                "tags": [{"name": f"HereTune:{video['status']}"}],
            }
            if video.get("rating") is not None:
                item["rating"] = float(video["rating"])
            scan_data.append(item)
        print(
            f"[HereTune HereSphere] SCAN videos={len(scan_data)} "
            f"ready_only={str(bool(getattr(cfg.server, 'heresphere_ready_only', True))).lower()}",
            flush=True,
        )
        return hs_json({"scanData": scan_data})

    @app.api_route(
        "/heresphere/video/{video_id}/{source_filename}",
        methods=["GET", "POST"],
        include_in_schema=False,
    )
    @app.api_route(
        "/heresphere/diag/{diag_token}/video/{video_id}/{source_filename}",
        methods=["GET", "POST"],
        include_in_schema=False,
    )
    async def heresphere_video(
        video_id: str,
        request: Request,
        source_filename: str | None = None,
        diag_token: str | None = None,
    ):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        if source_filename is not None and source_filename != Path(str(video["path"])).name:
            raise HTTPException(404, "Video identity filename does not match")
        if diag_token is not None:
            diagnostic = _hsp_diagnostic_state(video)
            if not diagnostic["enabled"] or str(diagnostic["token"]) != str(diag_token):
                raise HTTPException(404, "Diagnostic HSP identity is stale; refresh the HereSphere library")
        body = await request_json(request)
        if request.method == "POST":
            raw_body = await request.body()
            print(
                f"[HereTune HereSphere] VIDEO POST {_source_filename(video)} · "
                f"{_heresphere_post_diagnostic(request, body, raw_body)}",
                flush=True,
            )
        if not authorized(request, body, cfg):
            return hs_json({"access": -1, "title": "Login Failed"})

        metadata_changed = False
        if "isFavorite" in body:
            favorite_value = body.get("isFavorite")
            if not isinstance(favorite_value, bool):
                raise HTTPException(400, "isFavorite write-back must be a boolean")
            db.exec("UPDATE videos SET is_favorite=? WHERE id=?", (1 if favorite_value else 0, video_id))
            metadata_changed = True
        if "rating" in body:
            try:
                rating_value = _normalize_half_star_rating(body.get("rating"), allow_none=False)
            except ValueError as exc:
                raise HTTPException(400, f"rating write-back {exc}") from exc
            db.exec("UPDATE videos SET rating=? WHERE id=?", (rating_value, video_id))
            metadata_changed = True
        if metadata_changed:
            video = db.get(video_id) or video
            live.publish("library", reason="heresphere-writeback", video_id=video_id)

        if body.get("deleteFile") is True:
            if not cfg.server.allow_file_delete:
                raise HTTPException(403, "HereTune file deletion is disabled")
            _safe_delete_source(video, cfg)
            db.exec("UPDATE videos SET status='missing' WHERE id=?", (video_id,))
            return hs_json({
                "access": 1,
                "title": video["title"],
                "description": "Source file deleted at the user's explicit request.",
            })

        incoming_hsp = body.get("hsp")
        if incoming_hsp is not None:
            if not isinstance(incoming_hsp, str):
                raise HTTPException(400, "HSP write-back must be a Base64 string")
            if len(incoming_hsp) > cfg.server.max_hsp_writeback_bytes * 2:
                raise HTTPException(413, "HSP write-back is too large")

            accept_writeback = bool(getattr(cfg.hsp, "accept_hsp_writebacks", False))
            if not accept_writeback:
                # writeHSP is advertised false by default. If a player still POSTs
                # imported bytes because it cached an earlier scene response, do not
                # parse, store, or reject the live import with an HTTP error. A
                # minimal successful acknowledgement is the least re-entrant path.
                _suppress_hsp_reload_after_writeback(app, video_id)
                print(
                    f"[HereTune HereSphere] HSP WRITEBACK ACK-ONLY {_source_filename(video)} · "
                    "persistence disabled; payload ignored; live reload suppressed",
                    flush=True,
                )
                return hs_json({
                    "access": 1,
                    "title": video["title"],
                    "description": (
                        "HSP import acknowledged, but HereTune HSP persistence is disabled. "
                        "No media or server HSP reload was returned."
                    ),
                    "writeHSP": False,
                })

            try:
                incoming_blob = base64.b64decode(incoming_hsp, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(400, "HSP write-back is not valid Base64") from exc
            if len(incoming_blob) > cfg.server.max_hsp_writeback_bytes:
                raise HTTPException(413, "HSP write-back is too large")
            found_version = hsp_version(incoming_blob)
            if found_version is None:
                raise HTTPException(400, "Could not determine HSP write-back version")
            try:
                if int(found_version) == int(cfg.hsp.generated_version):
                    parsed_writeback = validate_playback_safe(
                        incoming_blob,
                        # Imported/manual HSPs may retain a title/UniqueID chosen
                        # inside HereSphere. Validate structure, timeline, and
                        # numeric safety without requiring media-name identity.
                        duration_seconds=float((video.get("metadata") or {}).get("duration") or 0.0),
                    )
                else:
                    parsed_writeback = validate_hsp(incoming_blob)
            except HSPError as exc:
                raise HTTPException(400, f"Invalid HSP write-back: {exc}") from exc

            if parsed_writeback is not None:
                fmt = parsed_writeback.format_keyframes[0] if parsed_writeback.format_keyframes else None
                lens = parsed_writeback.lens_keyframes[0] if parsed_writeback.lens_keyframes else None
                image = parsed_writeback.image_keyframes[0] if parsed_writeback.image_keyframes else None
                fmt_text = (
                    f"format={fmt.projection_mode}/{fmt.stereo_mode} aspect={fmt.aspect_ratio:g}"
                    if fmt is not None else "format=none"
                )
                lens_text = (
                    f"lens={lens.true_lens_name!r} fov={lens.true_fov:g}"
                    if lens is not None else "lens=none"
                )
                image_text = (
                    f"sharp={image.sharpen:.3f} exp={image.exposure_bias:+.3f} "
                    f"temp={image.white_temp:.1f} tint={image.white_tint:+.4f} "
                    f"gain={_vec4_text(image.gain)} "
                    f"contrast={_vec4_text(image.contrast)} "
                    f"sat={_vec4_text(image.saturation)} "
                    f"gamma={_vec4_text(image.gamma)} "
                    f"offset={_vec4_text(image.offset)} "
                    f"smh={image.shadows:+.3f}/{image.midtones:.3f}/{image.highlights:.3f}"
                    if image is not None else "image=none"
                )
                alignment = parsed_writeback.alignment_keyframes[0] if parsed_writeback.alignment_keyframes else None
                alignment_text = (
                    f"align_pos={_vec3_text(alignment.position)} align_rot={_vec3_text(alignment.rotation)}"
                    if alignment is not None else "alignment=none"
                )
                print(
                    f"[HereTune HereSphere] HSP WRITEBACK {_source_filename(video)} · "
                    f"v{found_version} · uid={parsed_writeback.unique_id!r} · "
                    f"{fmt_text} · {lens_text} · {alignment_text} · {image_text}",
                    flush=True,
                )

            stored = False
            next_mode = "manual" if cfg.hsp.prefer_manual_writeback else _profile_mode(video)
            db.exec(
                """UPDATE videos SET manual_hsp=?, manual_hsp_version=?,
                   profile_mode=? WHERE id=?""",
                (incoming_blob, found_version, next_mode, video_id),
            )
            video = db.get(video_id) or video
            live.publish("library", reason="hsp-writeback", video_id=video_id)
            stored = True

            # The mutation response is deliberately minimal. HereSphere is
            # already applying the imported HSP; echoing media/hspArray here (or
            # on its immediate follow-up request) can trigger a second live
            # profile replacement while the first is still being installed.
            _suppress_hsp_reload_after_writeback(app, video_id)
            print(
                f"[HereTune HereSphere] HSP WRITEBACK {'STORED' if stored else 'ACK-ONLY'} "
                f"{_source_filename(video)} · media and hspArray omitted; live reload suppressed",
                flush=True,
            )
            return hs_json({
                "access": 1,
                "title": video["title"],
                "description": (
                    "HSP write-back stored. Live media/profile reload was intentionally suppressed."
                    if stored else
                    "HSP import acknowledged, but HereTune persistence is disabled. Live media/profile reload was intentionally suppressed."
                ),
                # Advertise false in this mutation acknowledgement even if future
                # normal reads allow write-back. This prevents an import loop.
                "writeHSP": False,
            })

        base = public_base(request, cfg)
        metadata = _heresphere_metadata(video)
        analysis = video["analysis"]
        kind, selected_blob, found_version = _profile_blob(video, cfg)
        hsp_url = None
        revision = None
        if selected_blob is not None and found_version is not None and kind is not None:
            revision = _hsp_revision(selected_blob)
            hsp_url = f"{base}/hsp/{video_id}/{kind}/{int(found_version)}.hsp"
            if cfg.hsp.revisioned_urls:
                hsp_url += f"?rev={revision}"

        if analysis and "reason" not in analysis:
            description = (
                f"HereTune {_profile_mode(video)} profile · RGB "
                f"{analysis.get('r_gain', 1):.3f}/"
                f"{analysis.get('g_gain', 1):.3f}/"
                f"{analysis.get('b_gain', 1):.3f} · "
                f"Contrast {analysis.get('contrast', 1):.3f} · "
                f"Gamma {analysis.get('gamma', 1):.3f} · "
                f"Sat {analysis.get('saturation', 1):.3f} · "
                f"Exp {analysis.get('exposure_bias', 0):+.3f} · "
                f"Temp {analysis.get('white_temp', 6500):.0f}K · "
                f"Tint {analysis.get('white_tint', 0):+.3f} · "
                f"S/M/H {analysis.get('shadows', 0):+.3f}/"
                f"{analysis.get('midtones', 1):.3f}/"
                f"{analysis.get('highlights', 1):.3f} · "
                f"Sharp {analysis.get('sharpen', 2):.2f}"
            )
        else:
            description = video["error"] or "HereTune analysis pending"

        needs_media = request.method == "POST" and bool(body.get("needsMediaSource", True))
        response = {
            "access": 1,
            "title": video["title"],
            "description": description,
            "thumbnailImage": (
                f"{base}/thumb/{video_id}.jpg?v={THUMBNAIL_LAYOUT_VERSION}" if video["thumb_path"]
                else f"{base}/placeholder.svg"
            ),
            "dateAdded": video["date_added"][:10],
            "duration": float(metadata.get("duration") or 0.0) * 1000.0,
            "projection": metadata.get("projection", "perspective"),
            "stereo": metadata.get("stereo", "mono"),
            "isEyeSwapped": bool(metadata.get("isEyeSwapped", False)),
            "fov": float(metadata.get("fov") or 0.0),
            "lens": metadata.get("lens", "Linear"),
            "cameraIPD": float(metadata.get("cameraIPD") or 6.5),
            "tags": [
                {"name": f"HereTune:{video['status']}"},
                {"name": f"HereTuneProfile:{_profile_mode(video)}"},
                {"name": f"WB:{_wb_method_label(analysis.get('wb_method', 'pending'))}"},
            ],
            "eventServer": f"{base}/events/{video_id}",
            "isFavorite": bool(video.get("is_favorite")),
            "writeHSP": bool(getattr(cfg.hsp, "accept_hsp_writebacks", False)),
            "writeFavorite": True,
            "writeRating": True,
            "writeTags": False,
        }
        if video.get("rating") is not None:
            response["rating"] = float(video["rating"])
        # Playback-safe write-back acknowledgement: when HereSphere POSTs an
        # imported/edited HSP, do not immediately echo another HSP URL in the
        # same response. The player is already applying the incoming preset;
        # asking it to load Auto again during that mutation can create a re-entrant
        # live HSP replacement. The next normal GET/open advertises the selected
        # Auto/Manual profile as usual.
        allow_hsp = bool(
            hsp_url
            and needs_media
            and _allow_player_hsp_advertisement(
                app, video_id, revision, needs_media=needs_media
            )
        )
        if allow_hsp:
            # Use the version-aware hspArray path only. Advertising multiple HSP
            # sources for one profile can provoke duplicate loads in some builds.
            response["hspArray"] = [{"url": hsp_url, "version": int(found_version)}]
            if needs_media:
                _record_player_hsp_advertisement(app, video_id, revision)
        elif hsp_url and needs_media:
            response["description"] = description + " · HSP reload deferred until the active import/playback closes."
        if needs_media:
            # The media source request is the earliest reliable signal that this
            # video is entering playback. Freeze player-facing HSP mutations until
            # the eventServer close notification (or the safety TTL expires).
            app.state.active_playback[video_id] = time.monotonic()
            source_name = Path(str(video["path"])).name
            source_url = f"{base}/media/{video_id}/{quote(source_name, safe='')}"
            response["media"] = [{
                # Keep the real filename visible in both the media label and URL.
                # HereSphere uses filename suffix tokens such as _LR/_TB/_MKX200
                # as an important projection/stereo recognition path.
                "name": str(metadata.get("video_codec") or "Original"),
                "sources": [{
                    "resolution": _resolution(metadata),
                    "height": int(metadata.get("height") or 0),
                    "width": int(metadata.get("width") or 0),
                    "size": int(video["size"]),
                    "url": source_url,
                }],
            }]
            print(
                f"[HereTune HereSphere] PLAY {source_name} · "
                f"{metadata.get('projection','perspective')}/{metadata.get('stereo','mono')} · "
                f"FOV {float(metadata.get('fov') or 0):g} · lens {metadata.get('lens','Linear')} · "
                "filename-bound identity · "
                f"geometry {metadata.get('heresphere_geometry_source','cached')} · "
                f"HSP {kind or 'none'} v{found_version or '-'} {revision or ''}",
                flush=True,
            )
        return hs_json(response)

    @app.get("/api/videos/{video_id}/heresphere-debug")
    async def heresphere_debug(video_id: str, request: Request):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        base = public_base(request, cfg)
        served = _heresphere_metadata(video)
        kind, blob, found_version = _profile_blob(video, cfg)
        hsp_detail = None
        if blob is not None:
            try:
                parsed = inspect_hsp(blob)
                hsp_detail = _hsp_inspection_payload(parsed)
                hsp_detail["revision"] = _hsp_revision(blob)
            except Exception as exc:
                hsp_detail = {"error": str(exc)}
        return {
            "identity_scheme": "filename-bound",
            "hsp_diagnostic": _hsp_diagnostic_state(video),
            "scene_url": f"{base}{_heresphere_video_path(video)}",
            "media_url": f"{base}/media/{video_id}/{quote(_source_filename(video), safe='')}",
            "filename": _source_filename(video),
            "geometry_source": served.get("heresphere_geometry_source"),
            "served_geometry": {key: served.get(key) for key in (
                "projection", "stereo", "isEyeSwapped", "fov", "lens", "full_side_by_side",
                "flip_front_back", "flip_left_right", "flip_up_down",
            )},
            "filename_detection": served.get("heresphere_filename_detection") or {},
            "stored_geometry": {key: (video.get("metadata") or {}).get(key) for key in (
                "projection", "stereo", "isEyeSwapped", "fov", "lens", "full_side_by_side",
                "flip_front_back", "flip_left_right", "flip_up_down",
            )},
            "visual_verification": (video.get("metadata") or {}).get("layout_detection") or {},
            "stereo_alignment": (video.get("metadata") or {}).get("stereo_alignment") or {},
            "lens_verification": (video.get("metadata") or {}).get("lens_verification") or {},
            "orientation_verification": (video.get("metadata") or {}).get("orientation_verification") or {},
            "horizon_verification": (video.get("metadata") or {}).get("horizon_verification") or {},
            "active_image_bounds": (video.get("metadata") or {}).get("active_image_bounds") or {},
            "native_calibration": native_calibration_metadata(
                video, served, cfg, _correction_from_analysis(video.get("analysis") or {}),
            ),
            "layout_override": video.get("layout_override") or {},
            "web_player_invert_drag": bool(getattr(cfg.server, "web_player_invert_drag", False)),
        "show_hsp_diagnostic_tools": bool(getattr(cfg.server, "show_hsp_diagnostic_tools", True)),
            "profile_kind": kind,
            "write_hsp_advertised": bool(getattr(cfg.hsp, "accept_hsp_writebacks", False)),
            "hsp_url": (
                f"{base}/hsp/{video_id}/{kind}/{int(found_version)}.hsp?rev={_hsp_revision(blob)}"
                if blob is not None and kind is not None and found_version is not None
                else None
            ),
            "hsp": hsp_detail,
        }

    @app.get("/hsp/{video_id}/{kind}/{hsp_version_value}.hsp")
    async def hsp_file(
        video_id: str,
        kind: str,
        hsp_version_value: int,
        rev: str | None = None,
    ):
        video = db.get(video_id)
        if not video or kind not in _PROFILE_MODES:
            raise HTTPException(404, "HSP not found")
        selected_kind, blob, found_version = _profile_blob(video, cfg, kind)
        if blob is None or found_version != hsp_version_value or selected_kind != kind:
            raise HTTPException(404, "HSP not found")
        blob = bytes(blob)
        current_revision = _hsp_revision(blob)
        revision = current_revision

        # Keep recently served revisions alive while a video is open. Previously
        # a reanalysis/policy change made the previous immutable URL return 404; a
        # player retry during playback could then lose the active preset. Exact
        # historical bytes are retained in-memory for six hours.
        cache = app.state.hsp_revision_cache
        now_mono = time.monotonic()
        for key, entry in list(cache.items()):
            if now_mono - float(entry[1]) > 21_600:
                cache.pop(key, None)
        current_key = (video_id, kind, int(found_version), current_revision)
        cache[current_key] = (blob, now_mono)
        if len(cache) > 512:
            oldest = sorted(cache.items(), key=lambda item: float(item[1][1]))[: len(cache) - 512]
            for key, _ in oldest:
                cache.pop(key, None)

        if rev is not None and rev != current_revision:
            cached = cache.get((video_id, kind, int(found_version), rev))
            if cached is not None:
                blob = bytes(cached[0])
                revision = rev
            else:
                # Revisioned URLs are immutable identities. Returning current bytes
                # under an unknown prior revision can make a live player believe it
                # received one preset while parsing another. Only exact retained
                # historical bytes are served; otherwise the stale URL is gone.
                raise HTTPException(404, "HSP revision is no longer available")

        cache_control = (
            "public, max-age=31536000, immutable"
            if cfg.hsp.revisioned_urls and rev == revision
            else "no-store, no-cache, must-revalidate, max-age=0"
        )
        parsed_hsp = None
        try:
            if int(found_version) <= int(SUPPORTED_HSP_VERSION):
                parsed_hsp = validate_playback_safe(
                    blob,
                    # HereTune-generated profiles are filename-bound. A safe
                    # manual write-back may retain a user-selected HereSphere
                    # title/UID, so Manual is validated structurally instead.
                    expected_filename=None if kind == "manual" else _source_filename(video),
                    duration_seconds=float((video.get("metadata") or {}).get("duration") or 0.0),
                )
            else:
                # Preserve future HereSphere write-backs byte-for-byte. We can
                # identify their version but cannot claim semantic validation of
                # a layout newer than this build understands.
                validate_hsp(blob, expected_version=int(found_version))
        except HSPError as exc:
            print(
                f"[HereTune HereSphere] HSP WITHHELD {_source_filename(video)} · "
                f"{kind} v{found_version} rev {revision} · {exc}",
                flush=True,
            )
            raise HTTPException(422, f"Unsafe HSP withheld: {exc}") from exc

        if parsed_hsp is None:
            hsp_detail = f"future HSP v{found_version} preserved without semantic inspection"
        else:
            fmt_key = parsed_hsp.format_keyframes[0]
            lens_key = parsed_hsp.lens_keyframes[0]
            image_key = parsed_hsp.image_keyframes[0]
            last_image = parsed_hsp.image_keyframes[-1]
            alignment_key = parsed_hsp.alignment_keyframes[0] if parsed_hsp.alignment_keyframes else None
            stitch_key = parsed_hsp.stitch_keyframes[0] if parsed_hsp.stitch_keyframes else None
            orientation_key = parsed_hsp.orientation_keyframes[0] if parsed_hsp.orientation_keyframes else None
            autofocus_key = parsed_hsp.autofocus_keyframes[0] if parsed_hsp.autofocus_keyframes else None
            sync_key = parsed_hsp.sync_keyframes[0] if parsed_hsp.sync_keyframes else None
            audio_key = parsed_hsp.audio_keyframes[0] if parsed_hsp.audio_keyframes else None
            counts_text = ",".join(f"{k}:{int(v)}" for k, v in parsed_hsp.array_counts.items())
            image_times = "/".join(
                f"{float(item.time_ticks) / 10_000_000.0:.1f}s"
                for item in parsed_hsp.image_keyframes[:8]
            )
            hsp_detail = (
                f"wrapper={parsed_hsp.container_prefix or '-'} · uid={parsed_hsp.unique_id!r} · "
                f"counts={counts_text} · images={image_times or '-'} · "
                f"format {fmt_key.projection_mode}/{fmt_key.stereo_mode} swap={int(fmt_key.eye_swap_enabled)} "
                f"zoom={fmt_key.zoom[0]:.3f}/{fmt_key.zoom[1]:.3f} "
                f"pan={fmt_key.pan[0]:+.3f}/{fmt_key.pan[1]:+.3f} · "
                f"lens {lens_key.true_lens_name or '-'} {lens_key.true_fov:g}° · "
                f"align {(_vec3_text(alignment_key.rotation) if alignment_key else '-')} · "
                f"stitch-scale {(_vec4_text(stitch_key.scale) if stitch_key else '-')} · "
                f"orient {(_vec3_text(orientation_key.orientation) if orientation_key else '-')} · "
                f"AF {(f'{autofocus_key.min_focus_distance:.1f}-{autofocus_key.max_focus_distance:.1f}' if autofocus_key else '-')} · "
                f"audio {(f'{audio_key.time_offset:+.3f}s×{audio_key.volume_multiplier:.3f}' if audio_key else '-')} · "
                f"sync {(f'{sync_key.ticks_per_hour_offset / 10_000_000.0:+.3f}s/h' if sync_key else '-')} · "
                f"first sharp {image_key.sharpen:.3f} exp {image_key.exposure_bias:+.3f} "
                f"temp {image_key.white_temp:.0f}K tint {image_key.white_tint:+.4f} · "
                f"smh {image_key.shadows:+.3f}/{image_key.midtones:.3f}/{image_key.highlights:.3f} · "
                f"gain {_vec4_text(image_key.gain)} · "
                f"contrast {_vec4_text(image_key.contrast)} · "
                f"sat {_vec4_text(image_key.saturation)} · "
                f"gamma {_vec4_text(image_key.gamma)} · "
                f"offset {_vec4_text(image_key.offset)}"
                + (
                    f" · last@{last_image.time_ticks / 10_000_000.0:.1f}s "
                    f"contrast {_vec4_text(last_image.contrast)}"
                    if last_image is not image_key else ""
                )
            )
        print(
            f"[HereTune HereSphere] HSP GET {_source_filename(video)} · "
            f"{kind} v{found_version} · rev {revision} · "
            f"diag={','.join(_hsp_diagnostic_state(video)['disabled']) or 'baseline'} · "
            f"{len(blob)} bytes · {hsp_detail}",
            flush=True,
        )
        hsp_name = Path(str(video["path"])).with_suffix(".hsp").name
        return Response(
            blob,
            media_type="application/octet-stream",
            headers={
                "Cache-Control": cache_control,
                "Pragma": "no-cache" if "no-store" in cache_control else "",
                "ETag": f'"{revision}"',
                "Content-Disposition": f"inline; filename*=UTF-8''{quote(hsp_name, safe='')}",
                "X-HereTune-Profile": kind,
                "X-HereTune-Revision": revision,
                "X-HereTune-Diagnostic": ",".join(_hsp_diagnostic_state(video)["disabled"]) or "baseline",
                "X-HereTune-HSP-Filename": hsp_name,
                "X-HereTune-Revision-Fallback": "exact",
            },
        )

    @app.get("/preview/{video_id}/{variant}.jpg")
    async def preview_image(video_id: str, variant: str):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        if variant not in {"source", "left", "right", "final", "before", "after"}:
            raise HTTPException(404, "Unknown preview variant")
        metadata = thumbnail_browse_metadata(video, effective_metadata(video, video.get("metadata") or {}))
        try:
            if variant in {"before", "after"}:
                correction = _selected_hsp_preview_correction(video, cfg)
                payload = await _daemon_to_thread(
                    before_after_jpeg, Path(video["path"]), cfg.paths.ffmpeg, metadata,
                    correction, variant == "after",
                )
            else:
                payload = await _daemon_to_thread(preview_jpeg, Path(video["path"]), cfg.paths.ffmpeg, metadata, variant)
        except Exception as exc:
            raise HTTPException(500, f"Preview generation failed: {exc}") from exc
        return Response(payload, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/thumb/{video_id}.jpg")
    @app.get("/thumbnail/{video_id}")
    async def thumbnail_file(video_id: str):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        # Raw stereo/fisheye thumbnails are not browse-friendly. Preview
        # paths are regenerated lazily with a two-job semaphore so upgrading a
        # large library does not launch hundreds of FFmpeg processes at once.
        if not thumbnail_is_current(video):
            # Automatic browse-preview work is cosmetic and low priority. During
            # a readiness backlog return the placeholder immediately; the idle
            # preview worker will fill this cache after queued analysis drains.
            queue_busy = db.queued() is not None or app.state.analysis_progress is not None
            if video.get("status") == "ready" and not queue_busy:
                async with app.state.thumbnail_semaphore:
                    video = db.get(video_id) or video
                    if not thumbnail_is_current(video):
                        await _daemon_to_thread(ensure_thumbnail, db, cfg, video, force=True)
                    video = db.get(video_id) or video
            else:
                return FileResponse(
                    static_dir / "placeholder.svg",
                    media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store", "X-HereTune-Preview-Deferred": "1"},
                )
        path = Path(video["thumb_path"]) if video.get("thumb_path") else None
        if path is None or not path.exists():
            return FileResponse(
                static_dir / "placeholder.svg",
                media_type="image/svg+xml",
                headers={"Cache-Control": "no-store", "X-HereTune-Preview-Deferred": "1"},
            )
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    @app.get("/placeholder.svg")
    async def placeholder():
        return FileResponse(static_dir / "placeholder.svg", media_type="image/svg+xml")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon_ico():
        return FileResponse(static_dir / "favicon.ico", media_type="image/x-icon")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon_svg():
        return FileResponse(static_dir / "favicon.svg", media_type="image/svg+xml")

    @app.api_route("/media/{video_id}/{filename:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def media_file_named(video_id: str, filename: str, request: Request):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        expected = Path(str(video["path"])).name
        if Path(filename).name != expected:
            raise HTTPException(404, "Media filename does not match this video")
        return _media_response(video, request, cfg)

    @app.get("/webmedia/{video_id}.mp4")
    async def web_compat_media(
        video_id: str,
        mode: Literal["remux", "transcode"] = Query("transcode"),
        start: float = Query(0.0, ge=0.0),
    ):
        """Browser-compatibility stream for sources Chrome rejects directly.

        Remux mode preserves the original H.264/HEVC video bitstream and only
        repackages it into fragmented MP4 (audio is normalized to AAC). If the
        browser still cannot decode that, the player automatically falls through
        to a high-quality H.264/AAC compatibility stream. This route is intentionally
        separate from /media: HereSphere and compatible browsers always retain the
        untouched original-file path.
        """
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        metadata = video.get("metadata") or {}
        codec = str(metadata.get("video_codec") or "").casefold()
        if mode == "remux" and codec not in {"h264", "hevc", "h265"}:
            raise HTTPException(400, "This codec requires the compatibility transcode path")
        command, label = _web_compat_command(video, cfg, mode=mode, start=float(start))
        _web_log(video_id, f"browser fallback requested · mode={mode} · start={float(start):.3f}s · codec={codec or 'unknown'} · {label}")
        return StreamingResponse(
            _stream_ffmpeg(command, video_id=video_id, label=label),
            media_type="video/mp4",
            headers={
                "Cache-Control": "no-store",
                "X-HereTune-Web-Mode": mode,
                "X-HereTune-Web-Description": label,
                "X-HereTune-Original": "0" if mode == "transcode" else "video",
            },
        )

    @app.post("/api/webplayback/{video_id}/client-event")
    async def web_playback_client_event(video_id: str, request: Request):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        body = await request_json(request)
        event = str(body.get("event") or "client-event")[:60]
        mode = str(body.get("mode") or "unknown")[:30]
        detail = str(body.get("detail") or "")[:800]
        error_code = body.get("error_code")
        ready_state = body.get("ready_state")
        network_state = body.get("network_state")
        current_src = str(body.get("current_src") or "")[:500]
        _web_log(
            video_id,
            f"Chrome {event} · mode={mode} · mediaError={error_code} · readyState={ready_state} · networkState={network_state}"
            + (f" · {detail}" if detail else "")
            + (f" · src={current_src}" if current_src else ""),
        )
        return {"ok": True}

    @app.api_route("/play/{video_id}/{filename:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def native_play_file(video_id: str, filename: str, request: Request):
        """Direct original-media link with a real filename extension.

        HereSphere's built-in browser can intercept direct .mp4/.webm links and
        hand them to the native player. The filename path is cosmetic; the video
        ID remains authoritative and prevents arbitrary filesystem access.
        """
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        expected = Path(str(video["path"])).name
        if Path(filename).suffix.casefold() != Path(expected).suffix.casefold():
            raise HTTPException(404, "Media filename does not match this video")
        return _media_response(video, request, cfg)

    @app.post("/events/{video_id}")
    async def playback_event(video_id: str, request: Request):
        video = db.get(video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        body = await request_json(request)
        received = now()
        db.exec(
            """INSERT INTO events(video_id,event,time_ms,speed,utc_ms,username,
               connection_key,received_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                video_id, body.get("event"), body.get("time"), body.get("speed"),
                body.get("utc"), body.get("username"), body.get("connectionKey"),
                received, json.dumps(body),
            ),
        )
        try:
            event_code = int(body.get("event"))
        except (TypeError, ValueError):
            event_code = -1
        if event_code in {0, 1, 2}:
            app.state.active_playback[video_id] = time.monotonic()
        elif event_code == 3:
            app.state.active_playback.pop(video_id, None)
            app.state.hsp_reload_suppressed_until.pop(video_id, None)
            app.state.playback_hsp_advertised.pop(video_id, None)
            needs_rebuild = video_id in app.state.pending_hsp_rebuild
            needs_sidecar = video_id in app.state.pending_sidecar_sync
            app.state.pending_hsp_rebuild.discard(video_id)
            app.state.pending_sidecar_sync.discard(video_id)
            refreshed = db.get(video_id)
            if refreshed and needs_rebuild:
                rebuilt = await _daemon_to_thread(_rebuild_auto_hsp, refreshed, cfg)
                if rebuilt is not None:
                    db.exec(
                        "UPDATE videos SET auto_hsp=?,auto_hsp_version=? WHERE id=?",
                        (rebuilt, int(cfg.hsp.generated_version), video_id),
                    )
                    refreshed = db.get(video_id) or refreshed
                    needs_sidecar = needs_sidecar or bool(getattr(cfg.hsp, "write_auto_sidecars", False))
                    live.publish("library", reason="deferred-hsp-rebuild", video_id=video_id)
            if refreshed and needs_sidecar and bool(getattr(cfg.hsp, "write_auto_sidecars", False)):
                try:
                    await _daemon_to_thread(
                        _write_auto_sidecar_for_video, db, cfg, refreshed, replace=False
                    )
                except (SidecarConflict, SidecarError) as exc:
                    print(
                        f"[HereTune HSP Sidecars] deferred sync skipped "
                        f"{_source_filename(refreshed)} · {exc}",
                        flush=True,
                    )
                else:
                    live.publish("library", reason="deferred-sidecar-sync", video_id=video_id)
            if not app.state.pending_hsp_rebuild:
                # Complete the two-phase global-policy commit only after every
                # live video deferred by Settings has safely closed and rebuilt.
                db.set_setting("auto_hsp_policy_signature", policy_signature(cfg))
        try:
            position_ms = max(0.0, float(body.get("time") or 0.0))
        except (TypeError, ValueError):
            position_ms = 0.0
        metadata = video.get("metadata") or {}
        duration_ms = max(0.0, float(metadata.get("duration") or 0.0) * 1000.0)
        # Open events count as a viewing session. Position 0 from an open/play event
        # must not erase a useful resume point from a previous pause/close event.
        updates = ["last_watched=?"]
        params: list[object] = [received]
        if position_ms > 0:
            updates.append("watch_position_ms=?")
            params.append(position_ms)
        if event_code == 0:
            updates.append("play_count=play_count+1")
        if duration_ms > 0 and position_ms / duration_ms >= 0.92:
            updates.append("completed=1")
        params.append(video_id)
        db.exec(f"UPDATE videos SET {', '.join(updates)} WHERE id=?", params)
        updated = db.get(video_id)
        live.publish("library", reason="watch", video_id=video_id)
        return {"ok": True, "watch": _video_view(updated, cfg).get("watch", {})}

    return app
