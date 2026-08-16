from __future__ import annotations

import functools
import shutil
import subprocess
from dataclasses import asdict, dataclass

from .processes import run_capture


@dataclass(frozen=True, slots=True)
class CudaCapabilities:
    ffmpeg_found: bool
    cuda_hwaccel: bool
    scale_cuda: bool
    hwupload_cuda: bool
    runtime_ok: bool
    detail: str = ""

    def dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def usable(self) -> bool:
        return bool(self.ffmpeg_found and self.cuda_hwaccel and self.runtime_ok)


def _run_text(command: list[str], timeout: float = 12.0) -> tuple[int, str]:
    try:
        proc = run_capture(command, timeout=timeout, stderr_to_stdout=True)
    except FileNotFoundError:
        return 127, "FFmpeg executable was not found."
    except subprocess.TimeoutExpired:
        return 124, "FFmpeg CUDA capability check timed out."
    except OSError as exc:
        return 126, str(exc)
    output = (proc.stdout or b"").decode("utf-8", errors="replace")
    return proc.returncode, output


def _contains_line(text: str, token: str) -> bool:
    token = token.casefold()
    return any(line.strip().casefold() == token for line in text.splitlines())


def _contains_filter(text: str, token: str) -> bool:
    token = token.casefold()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1].casefold() == token:
            return True
    return False


@functools.lru_cache(maxsize=16)
def detect_cuda(ffmpeg: str = "ffmpeg", device: int = 0) -> CudaCapabilities:
    """Detect the CUDA path HereTune actually needs, including a tiny runtime test.

    This deliberately checks filters as well as ``-hwaccels``. Some FFmpeg builds
    can decode with CUDA but do not ship ``scale_cuda``/``hwupload_cuda``. Hybrid
    mode only advertises itself when the complete sampling path can initialize.
    """
    executable = shutil.which(ffmpeg) if not any(c in ffmpeg for c in ("/", "\\")) else ffmpeg
    if not executable:
        return CudaCapabilities(False, False, False, False, False, "FFmpeg not found")

    code, hwaccels = _run_text([executable, "-hide_banner", "-hwaccels"])
    cuda_hwaccel = code == 0 and _contains_line(hwaccels, "cuda")

    code, filters = _run_text([executable, "-hide_banner", "-filters"])
    scale_cuda = code == 0 and _contains_filter(filters, "scale_cuda")
    hwupload_cuda = code == 0 and _contains_filter(filters, "hwupload_cuda")

    if not cuda_hwaccel:
        return CudaCapabilities(True, False, scale_cuda, hwupload_cuda, False, "FFmpeg is missing CUDA hwaccel")

    if not (scale_cuda and hwupload_cuda):
        # NVDEC-only builds can still accelerate decoding. Per-file preflight
        # verifies the decoder before a video is advertised as GPU-sampled.
        missing = []
        if not scale_cuda:
            missing.append("scale_cuda")
        if not hwupload_cuda:
            missing.append("hwupload_cuda")
        return CudaCapabilities(
            True, True, scale_cuda, hwupload_cuda, True,
            "CUDA decode is available; using GPU-decode/CPU-resize compatibility path (missing " + ", ".join(missing) + ").",
        )

    # Test CUDA device/filter initialization without requiring a real media file.
    # The actual video sampler still falls back per-file if a codec cannot use NVDEC.
    test_filter = (
        f"format=nv12,hwupload_cuda=device={int(device)},"
        "scale_cuda=32:32:format=nv12:interp_algo=bilinear,"
        "hwdownload,format=nv12"
    )
    code, output = _run_text(
        [
            executable,
            "-hide_banner",
            "-loglevel", "error",
            "-f", "lavfi",
            "-i", "color=black:size=64x64:rate=1:duration=0.05",
            "-vf", test_filter,
            "-frames:v", "1",
            "-f", "null",
            "-",
        ],
        timeout=15.0,
    )
    runtime_ok = code == 0
    detail = "CUDA sampling path initialized successfully." if runtime_ok else (
        (output or "CUDA runtime initialization failed.").strip().replace("\n", " | ")[-1200:]
    )
    return CudaCapabilities(
        True,
        cuda_hwaccel,
        scale_cuda,
        hwupload_cuda,
        runtime_ok,
        detail,
    )


def resolve_execution_mode(cfg, ffmpeg: str = "ffmpeg") -> tuple[str, CudaCapabilities | None]:
    """Resolve cpu/gpu/hybrid/auto. Auto intentionally prefers Hybrid."""
    requested = str(getattr(cfg, "execution_mode", "auto") or "auto").strip().casefold()
    if requested not in {"auto", "cpu", "gpu", "hybrid"}:
        requested = "auto"
    if requested == "cpu":
        return "cpu", None

    device = int(getattr(cfg, "gpu_device", 0) or 0)
    caps = detect_cuda(ffmpeg, device)
    fallback = bool(getattr(cfg, "gpu_fallback_to_cpu", True))

    if requested == "auto":
        preferred = str(getattr(cfg, "auto_preference", "hybrid") or "hybrid").casefold()
        if preferred not in {"gpu", "hybrid"}:
            preferred = "hybrid"
        return (preferred if caps.usable else "cpu"), caps

    if caps.usable:
        return requested, caps
    if fallback:
        return "cpu", caps
    raise RuntimeError(f"{requested.upper()} requested, but CUDA sampling is unavailable: {caps.detail}")

# Lightweight nvidia-smi telemetry cache. The dashboard polls every few seconds;
# there is no value in spawning several nvidia-smi processes per second.
_TELEMETRY_CACHE: dict[int, tuple[float, dict[str, object] | None]] = {}


def gpu_telemetry(device: int = 0, max_age: float = 1.5) -> dict[str, object] | None:
    import time

    device = max(0, int(device))
    cached = _TELEMETRY_CACHE.get(device)
    now = time.monotonic()
    if cached and now - cached[0] <= max_age:
        return cached[1]
    executable = shutil.which("nvidia-smi")
    if not executable:
        _TELEMETRY_CACHE[device] = (now, None)
        return None

    queries = [
        "name,utilization.gpu,utilization.decoder,memory.used,memory.total,temperature.gpu",
        "name,utilization.gpu,memory.used,memory.total,temperature.gpu",
    ]
    result: dict[str, object] | None = None
    for query in queries:
        code, output = _run_text([
            executable,
            f"--id={device}",
            f"--query-gpu={query}",
            "--format=csv,noheader,nounits",
        ], timeout=5.0)
        if code != 0 or not output.strip():
            continue
        fields = [part.strip() for part in output.strip().splitlines()[0].split(",")]
        try:
            if len(fields) == 6:
                name, gpu, decoder, used, total, temp = fields
                decoder_value: float | None = float(decoder)
            elif len(fields) == 5:
                name, gpu, used, total, temp = fields
                decoder_value = None
            else:
                continue
            result = {
                "device": device,
                "name": name,
                "gpu_utilization": float(gpu),
                "decoder_utilization": decoder_value,
                "memory_used_mb": float(used),
                "memory_total_mb": float(total),
                "temperature_c": float(temp),
            }
            break
        except (TypeError, ValueError):
            continue
    _TELEMETRY_CACHE[device] = (now, result)
    return result

# ---------------------------------------------------------------------------
# Adaptive resource planner
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WorkerPlan:
    gpu_workers: int
    cpu_workers: int
    probe_workers: int
    cpu_percent: float | None
    memory_percent: float | None
    nas_latency_ms: float | None
    gpu_percent: float | None
    decoder_percent: float | None
    adaptive: bool
    reasons: tuple[str, ...] = ()

    def dict(self) -> dict[str, object]:
        return asdict(self)


_RESOURCE_PLAN_CACHE: dict[tuple, tuple[float, WorkerPlan]] = {}


def _host_telemetry() -> tuple[float | None, float | None]:
    try:
        import psutil  # optional at import-time, required by the distribution
        # A short non-blocking sample is enough for back-pressure decisions. The
        # first call after process start may be 0; subsequent calls are meaningful.
        cpu = float(psutil.cpu_percent(interval=None))
        memory = float(psutil.virtual_memory().percent)
        return cpu, memory
    except Exception:
        return None, None


def _nas_latency_ms(root: str) -> float | None:
    """Measure one inexpensive metadata touch.

    This is deliberately a single stat-like operation, not a bandwidth test. It
    answers the scheduler's real question: "is the share feeling slow right now?"
    The scan/probe paths already need this share, so no extra large reads occur.
    """
    import os
    import time

    started = time.perf_counter()
    try:
        os.stat(root)
    except OSError:
        return None
    return (time.perf_counter() - started) * 1000.0


def resource_worker_plan(cfg, library_root: str = "", *, max_age: float = 1.5) -> WorkerPlan:
    """Choose worker counts from live host/GPU/NAS pressure.

    Configured worker counts are *ceilings*. When adaptive workers are disabled,
    the returned plan is exactly those configured values. With adaptation enabled,
    the planner backs off under high CPU/memory/NVDEC/GPU load or slow SMB metadata
    access, then automatically returns to the configured ceilings as pressure drops.
    """
    import os
    import time

    gpu_cap = max(1, int(getattr(cfg, "gpu_decode_workers", 4) or 4))
    cpu_cap = max(1, int(getattr(cfg, "cpu_analysis_workers", 8) or 8))
    probe_cap = max(1, int(getattr(cfg, "probe_workers", 8) or 8))
    adaptive = bool(getattr(cfg, "adaptive_workers", True))
    device = max(0, int(getattr(cfg, "gpu_device", 0) or 0))
    cache_key = (id(cfg), library_root, gpu_cap, cpu_cap, probe_cap, adaptive, device)
    now = time.monotonic()
    cached = _RESOURCE_PLAN_CACHE.get(cache_key)
    if cached and now - cached[0] <= max_age:
        return cached[1]

    cpu_pct, memory_pct = _host_telemetry()
    gpu = gpu_telemetry(device, max_age=max_age)
    nas_ms = _nas_latency_ms(library_root) if library_root else None
    gpu_pct = float(gpu.get("gpu_utilization")) if gpu and gpu.get("gpu_utilization") is not None else None
    decoder_pct = float(gpu.get("decoder_utilization")) if gpu and gpu.get("decoder_utilization") is not None else None

    gpu_workers, cpu_workers, probe_workers = gpu_cap, cpu_cap, probe_cap
    reasons: list[str] = []

    if adaptive:
        # CPU analysis is intentionally conservative: avoid making the local UI,
        # Virtual Desktop, HereSphere, or Windows itself fight OpenCV for every core.
        logical = max(1, int(os.cpu_count() or 1))
        friendly_cpu_ceiling = max(1, min(cpu_cap, max(2, logical // 3)))
        cpu_workers = min(cpu_workers, friendly_cpu_ceiling)

        if cpu_pct is not None:
            if cpu_pct >= 90:
                cpu_workers = 1
                probe_workers = min(probe_workers, 1)
                reasons.append(f"CPU {cpu_pct:.0f}%")
            elif cpu_pct >= 78:
                cpu_workers = max(1, min(cpu_workers, max(1, cpu_cap // 2)))
                probe_workers = max(1, min(probe_workers, max(1, probe_cap // 2)))
                reasons.append(f"CPU {cpu_pct:.0f}%")
            elif cpu_pct >= 65:
                cpu_workers = max(1, min(cpu_workers, max(1, cpu_cap - 1)))
                reasons.append(f"CPU {cpu_pct:.0f}%")

        if memory_pct is not None and memory_pct >= 90:
            cpu_workers = max(1, min(cpu_workers, max(1, cpu_cap // 2)))
            gpu_workers = max(1, min(gpu_workers, max(1, gpu_cap // 2)))
            reasons.append(f"RAM {memory_pct:.0f}%")

        if decoder_pct is not None:
            if decoder_pct >= 82:
                gpu_workers = 1
                reasons.append(f"NVDEC {decoder_pct:.0f}%")
            elif decoder_pct >= 58:
                gpu_workers = max(1, min(gpu_workers, max(1, gpu_cap - 1)))
                reasons.append(f"NVDEC {decoder_pct:.0f}%")
        elif gpu_pct is not None and gpu_pct >= 92:
            gpu_workers = 1
            reasons.append(f"GPU {gpu_pct:.0f}%")

        # SMB latency is a useful signal that more simultaneous seeks/probes will
        # only make the queue slower. This is metadata latency, not throughput.
        if nas_ms is not None:
            if nas_ms >= 140:
                gpu_workers = 1
                probe_workers = 1
                reasons.append(f"NAS {nas_ms:.0f}ms")
            elif nas_ms >= 55:
                gpu_workers = max(1, min(gpu_workers, 2))
                probe_workers = max(1, min(probe_workers, max(1, probe_cap // 2)))
                reasons.append(f"NAS {nas_ms:.0f}ms")

    plan = WorkerPlan(
        gpu_workers=max(1, gpu_workers),
        cpu_workers=max(1, cpu_workers),
        probe_workers=max(1, probe_workers),
        cpu_percent=cpu_pct,
        memory_percent=memory_pct,
        nas_latency_ms=nas_ms,
        gpu_percent=gpu_pct,
        decoder_percent=decoder_pct,
        adaptive=adaptive,
        reasons=tuple(reasons),
    )
    _RESOURCE_PLAN_CACHE[cache_key] = (now, plan)
    return plan
