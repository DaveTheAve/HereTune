from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class LibraryCfg:
    roots: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=lambda: [
        ".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi", ".ts", ".m2ts",
        ".mts", ".mpg", ".mpeg", ".wmv",
    ])
    scan_interval_seconds: int = 60


@dataclass(slots=True)
class ServerCfg:
    host: str = "0.0.0.0"
    port: int = 8777
    public_base_url: str = ""
    username: str = ""
    password: str = ""
    auth_token: str = ""
    # Optional HTTP Basic authentication for the HereTune dashboard/admin API.
    # These credentials do NOT change the separate HereSphere Web API login.
    dashboard_username: str = ""
    dashboard_password: str = ""
    allow_file_delete: bool = False
    media_chunk_bytes: int = 1024 * 1024
    max_hsp_writeback_bytes: int = 16 * 1024 * 1024
    # Default camera-drag direction for the browser VR-to-2D viewer. Users can
    # override it per-browser from the player without changing this file.
    web_player_invert_drag: bool = False
    # Start the dashboard in a paused state so scanning/analysis waits until
    # the user explicitly presses Resume work.
    start_paused: bool = True
    # Keep the VR HSP isolation lab code available without forcing it to be
    # visible in every future build.
    show_hsp_diagnostic_tools: bool = False
    # Presentation-only workaround for HereSphere library UIs that cannot scroll long
    # group labels. When enabled, HereTune emits A, B, C... labels dynamically;
    # nothing is stored in the DB or a mapping file.
    compact_heresphere_library_labels: bool = False
    # Advertise only videos that have finished analysis and have a generated
    # or accepted HSP available. The HereTune dashboard still shows the full
    # queue so pending/error work remains manageable.
    heresphere_ready_only: bool = True


@dataclass(slots=True)
class AnalysisCfg:
    # Auto deliberately resolves to Hybrid when a working CUDA path is available.
    execution_mode: str = "auto"
    auto_preference: str = "hybrid"
    gpu_device: int = 0
    gpu_decode_workers: int = 4
    cpu_analysis_workers: int = 8
    probe_workers: int = 8
    gpu_fallback_to_cpu: bool = True
    # Treat worker counts above as ceilings and automatically back off when CPU,
    # NVDEC, VRAM pressure, or NAS latency indicate the machine is already busy.
    adaptive_workers: bool = True
    # Run each heavy per-video analysis in a child Python process. The FastAPI
    # dashboard stays alive even if OpenCV/FFmpeg/native code terminates a worker.
    isolate_analysis_worker: bool = True

    # Persistent analysis checkpoint/cache. Only compact probe/full statistics
    # are stored. Inspector imagery is decoded from the source video on demand.
    analysis_cache_enabled: bool = True
    analysis_cache_max_gb: float = 20.0

    # Tuned for long 5.7K+ VR material: 960px keeps each SBS eye useful for
    # face/skin detection without hauling full-resolution frames through Python.
    samples: int = 48
    analysis_width: int = 960
    adaptive_sampling: bool = True
    sample_interval_seconds: float = 30.0
    max_samples: int = 360
    smart_sampling: bool = True
    candidate_multiplier: float = 1.35
    strength: float = 1.0
    skin_priority: float = 1.25
    white_balance: bool = True
    tone: bool = True
    saturation: bool = True
    # Use HereSphere's full native image stack instead of forcing every tonal
    # problem through contrast/gamma/offset alone.
    advanced_tone: bool = True
    sharpening: bool = True
    temperature_tint: bool = True
    # Fraction of a trustworthy neutral correction expressed through native
    # white-temperature/tint; the remaining correction stays in RGB gain. This
    # keeps the proven gain path as a residual/safety net.
    temperature_tint_strength: float = 0.45
    max_sharpen: float = 3.0

    # Use the RGB components of HereSphere's native Saturation/Contrast/Gamma/
    # Offset vectors to correct brightness-dependent channel casts that one
    # white-balance gain cannot remove.  These controls stay deliberately tiny:
    # they are finishing trims, not a second psychedelic color grade.
    per_channel_color: bool = True
    per_channel_strength: float = 0.55
    max_channel_gamma_delta: float = 0.045
    max_channel_offset: float = 0.016
    max_channel_contrast_delta: float = 0.035
    max_channel_saturation_delta: float = 0.030

    # Sparse, stable image keyframes for genuinely different lighting sections.
    # This is not frame-by-frame auto white balance; long-lived segments only.
    scene_grading: bool = True
    scene_max_keyframes: int = 0
    scene_min_seconds: float = 30.0
    # Normalized sustained-change score. Values near 1 mean the before/after
    # windows differ by roughly one expected natural-variation unit.
    scene_change_threshold: float = 1.15
    scene_min_confidence: float = 0.62
    # Hierarchical sparse-scene sampling.  A cheap low-resolution pass watches
    # the entire timeline densely, then suspicious boundaries are refined and
    # only stable grading segments receive the normal expensive analysis.
    # ``scene_probe_interval_seconds`` is the 20-60 minute baseline; shorter and
    # longer videos adapt around it (roughly 8/10/15/20 seconds).
    scene_probe_interval_seconds: float = 10.0
    scene_probe_width: int = 480
    scene_boundary_refine_seconds: float = 2.0
    scene_probe_persistence: int = 2
    scene_full_samples_per_segment: int = 9
    scene_probe_max_samples: int = 900

    # External decoder watchdogs. A remote/NAS seek can occasionally leave
    # FFmpeg alive but idle forever on Windows; no single sample is allowed to
    # hold the entire analysis worker indefinitely.
    ffmpeg_sample_timeout_seconds: float = 20.0
    ffmpeg_aux_timeout_seconds: float = 45.0
    ffprobe_timeout_seconds: float = 30.0

    # Multi-frame VR verification and conservative native geometry correction.
    vr_verification: bool = True
    vr_verification_frames: int = 6
    orientation_verification: bool = True
    # Orientation is reported automatically, but the 180-degree correction is
    # opt-in because face detection alone should not overrule artistic footage.
    orientation_auto_apply: bool = False
    # Eye order is measurable only indirectly from binocular disparity and can
    # be intentionally unusual. Report suspicions by default; opt in to automatic
    # swapping only after reviewing the detector on the local library.
    eye_swap_auto_apply: bool = False
    stereo_alignment: bool = True
    stereo_alignment_auto_apply: bool = True
    stereo_alignment_min_confidence: float = 0.72
    stereo_alignment_max_degrees: float = 0.75
    stereo_roll_correction: bool = True
    stereo_roll_auto_apply: bool = False
    stereo_scale_correction: bool = True
    stereo_scale_auto_apply: bool = False
    stitch_correction: bool = True
    stitch_auto_apply: bool = False
    max_stitch_shift: float = 0.025
    max_stitch_scale_delta: float = 0.030

    auto_crop: bool = True
    auto_crop_min_confidence: float = 0.82
    auto_crop_max_zoom: float = 1.32
    horizon_leveling: bool = True
    horizon_auto_apply: bool = False
    horizon_min_confidence: float = 0.78
    horizon_max_degrees: float = 3.0

    autofocus_tuning: bool = True
    autofocus_auto_apply: bool = False
    lens_verification: bool = True
    # Pixel boundaries can verify centering/crop and suggest a FOV family, but
    # they cannot identify an unknown proprietary lens with certainty.
    lens_fov_auto_apply: bool = False
    lens_fov_min_confidence: float = 0.80

    # HSP Audio and Sync keyframes.  Loudness normalization is bounded and may
    # apply automatically; timing/drift remains diagnostic by default because
    # player sign conventions deserve the same native-export calibration care
    # that the standalone HSP wrapper received.
    audio_correction: bool = True
    loudness_normalization: bool = True
    audio_sync_analysis: bool = True
    audio_sync_auto_apply: bool = False
    target_audio_mean_db: float = -20.0
    max_audio_gain: float = 1.35
    min_rgb_gain: float = 0.80
    max_rgb_gain: float = 1.24


@dataclass(slots=True)
class HspCfg:
    generated_version: int = 7
    # Auto is authoritative by default. A headset write-back is preserved, but it
    # is only served after the user deliberately selects Manual in the dashboard.
    prefer_manual_writeback: bool = False
    # HereSphere may POST an imported/edited HSP back to the Web API. Some player
    # builds appear unstable when that write-back and an active live preset reload
    # overlap, so accepting headset HSP bytes is opt-in. Favorites/ratings are
    # independent and continue to write back normally.
    accept_hsp_writebacks: bool = False
    # Add a content digest to every HSP URL so HereSphere and HTTP caches cannot
    # accidentally reuse an older correction after reanalysis.
    revisioned_urls: bool = True

    # Global Auto-HSP output policy. Analysis evidence remains stored even when a
    # control is disabled, allowing immediate policy changes and per-video
    # Inherit / Force On / Force Off overrides without re-decoding the movie.
    auto_sharpen: bool = True
    auto_exposure: bool = True
    auto_temperature: bool = True
    auto_tint: bool = True
    auto_rgb_gain: bool = True
    # Global Offset is never part of the analyzer's tone solve. This optional
    # output policy only serializes a separate, tightly bounded suggestion.
    auto_global_offset: bool = False
    auto_rgb_offset: bool = True
    auto_global_gamma: bool = True
    auto_rgb_gamma: bool = True
    auto_global_contrast: bool = True
    auto_rgb_contrast: bool = True
    auto_global_saturation: bool = True
    auto_rgb_saturation: bool = True
    auto_shadows: bool = False
    auto_midtones: bool = True
    auto_highlights: bool = True
    auto_scene_keyframes: bool = True

    # Optional HereTune-managed sidecar export. This writes <video-stem>.hsp
    # atomically beside the source, but never overwrites an unknown/user-owned
    # sidecar without an explicit per-video replace action.
    write_auto_sidecars: bool = False


@dataclass(slots=True)
class PathsCfg:
    data_dir: str = "./data"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"


# User-facing analysis presets. They intentionally touch only performance /
# sampling knobs; correction safety bounds remain explicit and individually
# editable. Applying a preset in Settings fills these values, then the user can
# tweak anything before saving.
ANALYSIS_PRESETS: dict[str, dict[str, object]] = {
    "balanced": {
        "label": "Balanced",
        "description": "Recommended starting point for modern desktop PCs and long 5.7K+ VR media.",
        "values": {
            "gpu_decode_workers": 4, "cpu_analysis_workers": 8, "probe_workers": 8,
            "samples": 48, "analysis_width": 960, "sample_interval_seconds": 30.0,
            "max_samples": 360, "scene_probe_interval_seconds": 10.0,
            "scene_probe_width": 480, "scene_full_samples_per_segment": 9,
            "scene_probe_max_samples": 900,
        },
    },
    "high_coverage": {
        "label": "High Coverage",
        "description": "More timeline coverage and no full-sample ceiling. Slower, nosier fans, happier evidence goblins.",
        "values": {
            "gpu_decode_workers": 4, "cpu_analysis_workers": 8, "probe_workers": 8,
            "samples": 48, "analysis_width": 960, "sample_interval_seconds": 20.0,
            "max_samples": 0, "scene_probe_interval_seconds": 8.0,
            "scene_probe_width": 480, "scene_full_samples_per_segment": 10,
            "scene_probe_max_samples": 1200,
        },
    },
    "fast": {
        "label": "Fast",
        "description": "Quicker first pass for large libraries. Still scene-aware, just less interested in interviewing every photon.",
        "values": {
            "gpu_decode_workers": 3, "cpu_analysis_workers": 6, "probe_workers": 6,
            "samples": 28, "analysis_width": 720, "sample_interval_seconds": 45.0,
            "max_samples": 180, "scene_probe_interval_seconds": 15.0,
            "scene_probe_width": 360, "scene_full_samples_per_segment": 6,
            "scene_probe_max_samples": 600,
        },
    },
    "low_resource": {
        "label": "Low Resource",
        "description": "For laptops, older CPUs, slower NAS links, or anyone whose cooling system has filed a grievance.",
        "values": {
            "gpu_decode_workers": 2, "cpu_analysis_workers": 4, "probe_workers": 4,
            "samples": 24, "analysis_width": 640, "sample_interval_seconds": 60.0,
            "max_samples": 120, "scene_probe_interval_seconds": 20.0,
            "scene_probe_width": 320, "scene_full_samples_per_segment": 5,
            "scene_probe_max_samples": 420,
        },
    },
}

def analysis_presets() -> dict[str, dict[str, object]]:
    return {name: {"label": preset["label"], "description": preset["description"], "values": dict(preset["values"])} for name, preset in ANALYSIS_PRESETS.items()}

def apply_analysis_preset(cfg: "Config", preset_name: str) -> "Config":
    preset = ANALYSIS_PRESETS.get(str(preset_name or "").strip().lower())
    if not preset:
        raise ValueError(f"Unknown analysis preset: {preset_name}")
    for name, value in dict(preset["values"]).items():
        setattr(cfg.analysis, name, value)
    return _normalize(cfg)


@dataclass(slots=True)
class Config:
    library: LibraryCfg = field(default_factory=LibraryCfg)
    server: ServerCfg = field(default_factory=ServerCfg)
    analysis: AnalysisCfg = field(default_factory=AnalysisCfg)
    hsp: HspCfg = field(default_factory=HspCfg)
    paths: PathsCfg = field(default_factory=PathsCfg)
    config_path: Path | None = None

    @property
    def data_dir(self) -> Path:
        path = Path(self.paths.data_dir)
        if not path.is_absolute() and self.config_path:
            path = self.config_path.parent / path
        return path.resolve()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "heretune.sqlite3"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def analysis_cache_dir(self) -> Path:
        return self.data_dir / "analysis-cache"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"


def library_roots(cfg: Config) -> list[str]:
    """Return normalized configured media roots in user-defined order."""
    values = list(cfg.library.roots or [])
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        key = value.rstrip("\\/").casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def primary_library_root(cfg: Config) -> str:
    roots = library_roots(cfg)
    return roots[0] if roots else ""


def scheduler_library_root(cfg: Config) -> str:
    """Pick a conservative representative root for adaptive NAS-latency checks."""
    roots = library_roots(cfg)
    for root in roots:
        if root.startswith("\\") or root.startswith("//"):
            return root
    return roots[0] if roots else ""


def _apply(obj, values: dict[str, object]) -> None:
    for key, value in values.items():
        if hasattr(obj, key):
            setattr(obj, key, value)


def _normalize(cfg: Config) -> Config:
    cfg.library.extensions = [
        extension.lower() if extension.startswith(".") else "." + extension.lower()
        for extension in cfg.library.extensions
    ]
    cfg.library.roots = library_roots(cfg)
    cfg.library.scan_interval_seconds = max(10, int(cfg.library.scan_interval_seconds))
    cfg.server.port = max(1, min(65535, int(cfg.server.port)))
    cfg.server.media_chunk_bytes = max(64 * 1024, int(cfg.server.media_chunk_bytes))
    cfg.server.max_hsp_writeback_bytes = max(
        1024, int(cfg.server.max_hsp_writeback_bytes)
    )
    cfg.analysis.execution_mode = str(cfg.analysis.execution_mode or "auto").strip().lower()
    if cfg.analysis.execution_mode not in {"auto", "cpu", "gpu", "hybrid"}:
        cfg.analysis.execution_mode = "auto"
    cfg.analysis.auto_preference = str(cfg.analysis.auto_preference or "hybrid").strip().lower()
    if cfg.analysis.auto_preference not in {"gpu", "hybrid"}:
        cfg.analysis.auto_preference = "hybrid"
    cfg.analysis.gpu_device = max(0, int(cfg.analysis.gpu_device))
    cfg.analysis.gpu_decode_workers = max(1, min(8, int(cfg.analysis.gpu_decode_workers)))
    cfg.analysis.cpu_analysis_workers = max(1, min(16, int(cfg.analysis.cpu_analysis_workers)))
    cfg.analysis.probe_workers = max(1, min(16, int(cfg.analysis.probe_workers)))
    cfg.analysis.analysis_cache_max_gb = max(0.25, min(500.0, float(cfg.analysis.analysis_cache_max_gb)))
    cfg.analysis.samples = max(4, min(128, int(cfg.analysis.samples)))
    cfg.analysis.analysis_width = max(160, min(1920, int(cfg.analysis.analysis_width)))
    cfg.analysis.sample_interval_seconds = max(
        10.0, min(600.0, float(cfg.analysis.sample_interval_seconds))
    )
    # max_samples is a user-owned ceiling. 0 explicitly means unlimited. Do not
    # silently replace 0 with the base sample count or clamp large positive values.
    cfg.analysis.max_samples = max(0, int(cfg.analysis.max_samples))
    cfg.analysis.candidate_multiplier = max(1.0, min(1.75, float(cfg.analysis.candidate_multiplier)))
    cfg.analysis.strength = max(0.0, min(2.0, float(cfg.analysis.strength)))
    cfg.analysis.skin_priority = max(0.0, min(2.0, float(cfg.analysis.skin_priority)))
    cfg.analysis.temperature_tint_strength = max(
        0.0, min(0.75, float(cfg.analysis.temperature_tint_strength))
    )
    cfg.analysis.max_sharpen = max(2.0, min(5.0, float(cfg.analysis.max_sharpen)))
    cfg.analysis.per_channel_strength = max(
        0.0, min(1.0, float(cfg.analysis.per_channel_strength))
    )
    cfg.analysis.max_channel_gamma_delta = max(
        0.0, min(0.15, float(cfg.analysis.max_channel_gamma_delta))
    )
    cfg.analysis.max_channel_offset = max(
        0.0, min(0.05, float(cfg.analysis.max_channel_offset))
    )
    cfg.analysis.max_channel_contrast_delta = max(
        0.0, min(0.12, float(cfg.analysis.max_channel_contrast_delta))
    )
    cfg.analysis.max_channel_saturation_delta = max(
        0.0, min(0.12, float(cfg.analysis.max_channel_saturation_delta))
    )
    # 0 means unlimited. Any positive value is honored exactly; scene stability,
    # hysteresis and minimum duration are the anti-pumping controls, not a hidden cap.
    cfg.analysis.scene_max_keyframes = max(0, int(cfg.analysis.scene_max_keyframes))
    cfg.analysis.scene_min_seconds = max(
        20.0, min(900.0, float(cfg.analysis.scene_min_seconds))
    )
    cfg.analysis.scene_change_threshold = max(
        0.45, min(3.0, float(cfg.analysis.scene_change_threshold))
    )
    cfg.analysis.scene_min_confidence = max(
        0.40, min(0.95, float(cfg.analysis.scene_min_confidence))
    )
    cfg.analysis.scene_probe_interval_seconds = max(
        4.0, min(60.0, float(cfg.analysis.scene_probe_interval_seconds))
    )
    cfg.analysis.scene_probe_width = max(
        192, min(768, int(cfg.analysis.scene_probe_width))
    )
    cfg.analysis.scene_boundary_refine_seconds = max(
        0.5, min(10.0, float(cfg.analysis.scene_boundary_refine_seconds))
    )
    cfg.analysis.scene_probe_persistence = max(
        2, min(5, int(cfg.analysis.scene_probe_persistence))
    )
    cfg.analysis.scene_full_samples_per_segment = max(
        4, min(12, int(cfg.analysis.scene_full_samples_per_segment))
    )
    cfg.analysis.scene_probe_max_samples = max(
        60, min(2000, int(cfg.analysis.scene_probe_max_samples))
    )
    cfg.analysis.ffmpeg_sample_timeout_seconds = max(
        5.0, min(120.0, float(cfg.analysis.ffmpeg_sample_timeout_seconds))
    )
    cfg.analysis.ffmpeg_aux_timeout_seconds = max(
        10.0, min(300.0, float(cfg.analysis.ffmpeg_aux_timeout_seconds))
    )
    cfg.analysis.ffprobe_timeout_seconds = max(
        5.0, min(180.0, float(cfg.analysis.ffprobe_timeout_seconds))
    )
    cfg.analysis.vr_verification_frames = max(
        1, min(7, int(cfg.analysis.vr_verification_frames))
    )
    cfg.analysis.stereo_alignment_min_confidence = max(
        0.50, min(0.98, float(cfg.analysis.stereo_alignment_min_confidence))
    )
    cfg.analysis.stereo_alignment_max_degrees = max(
        0.0, min(2.0, float(cfg.analysis.stereo_alignment_max_degrees))
    )
    cfg.analysis.max_stitch_shift = max(
        0.0, min(0.10, float(cfg.analysis.max_stitch_shift))
    )
    cfg.analysis.max_stitch_scale_delta = max(
        0.0, min(0.10, float(cfg.analysis.max_stitch_scale_delta))
    )
    cfg.analysis.auto_crop_min_confidence = max(
        0.55, min(0.98, float(cfg.analysis.auto_crop_min_confidence))
    )
    cfg.analysis.auto_crop_max_zoom = max(
        1.0, min(2.0, float(cfg.analysis.auto_crop_max_zoom))
    )
    cfg.analysis.horizon_min_confidence = max(
        0.50, min(0.98, float(cfg.analysis.horizon_min_confidence))
    )
    cfg.analysis.horizon_max_degrees = max(
        0.0, min(10.0, float(cfg.analysis.horizon_max_degrees))
    )
    cfg.analysis.lens_fov_min_confidence = max(
        0.45, min(0.98, float(cfg.analysis.lens_fov_min_confidence))
    )
    cfg.analysis.target_audio_mean_db = max(
        -32.0, min(-10.0, float(cfg.analysis.target_audio_mean_db))
    )
    cfg.analysis.max_audio_gain = max(
        1.0, min(3.0, float(cfg.analysis.max_audio_gain))
    )
    cfg.analysis.min_rgb_gain = max(0.5, min(1.0, float(cfg.analysis.min_rgb_gain)))
    cfg.analysis.max_rgb_gain = max(1.0, min(1.5, float(cfg.analysis.max_rgb_gain)))
    cfg.hsp.generated_version = max(1, min(7, int(cfg.hsp.generated_version)))
    return cfg


def load_config(path: str | os.PathLike[str] = "config.toml") -> Config:
    config_path = Path(path).resolve()
    cfg = Config(config_path=config_path)
    if config_path.exists():
        with config_path.open("rb") as handle:
            values = tomllib.load(handle)
        _apply(cfg.library, values.get("library", {}))
        _apply(cfg.server, values.get("server", {}))
        _apply(cfg.analysis, values.get("analysis", {}))
        _apply(cfg.hsp, values.get("hsp", {}))
        _apply(cfg.paths, values.get("paths", {}))

    env_library = os.getenv("HERETUNE_LIBRARY")
    if env_library is not None:
        cfg.library.roots = [env_library] if str(env_library).strip() else []
    cfg.server.public_base_url = os.getenv(
        "HERETUNE_PUBLIC_BASE_URL", cfg.server.public_base_url
    )
    if os.getenv("HERETUNE_PORT"):
        cfg.server.port = int(os.environ["HERETUNE_PORT"])
    return _normalize(cfg)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        # JSON string escaping is a valid TOML basic-string subset and correctly
        # preserves UNC backslashes, quotes, and non-ASCII paths.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"Unsupported TOML value: {type(value).__name__}")


def config_to_toml(cfg: Config) -> str:
    lines = [
        "# HereTune configuration",
        "# This file can also be edited from Dashboard -> Settings.",
        "",
    ]
    section_notes = {
        "library": "# Media library and background discovery.",
        "server": "# Local web/API server. Host/port changes require restart.",
        "analysis": "# Analysis and CPU/GPU execution settings.",
        "hsp": "# Native HereSphere HSP behavior.",
        "paths": "# Tool/data paths. data_dir changes require restart.",
    }
    for section_name in ("library", "server", "analysis", "hsp", "paths"):
        lines.append(section_notes[section_name])
        lines.append(f"[{section_name}]")
        section = getattr(cfg, section_name)
        for item in fields(section):
            lines.append(f"{item.name} = {_toml_value(getattr(section, item.name))}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# UI-editable fields. Secrets are intentionally excluded from the browser API.
_EDITABLE_FIELDS: dict[str, set[str]] = {
    "library": {"roots", "scan_interval_seconds"},
    "server": {"host", "port", "public_base_url", "allow_file_delete", "web_player_invert_drag", "start_paused", "show_hsp_diagnostic_tools", "compact_heresphere_library_labels", "heresphere_ready_only", "dashboard_username"},
    "analysis": {
        "execution_mode", "auto_preference", "gpu_device", "gpu_decode_workers",
        "cpu_analysis_workers", "probe_workers", "gpu_fallback_to_cpu", "adaptive_workers", "isolate_analysis_worker",
        "analysis_cache_enabled", "analysis_cache_max_gb", "samples",
        "analysis_width", "adaptive_sampling", "sample_interval_seconds", "max_samples",
        "smart_sampling", "candidate_multiplier", "strength", "skin_priority", "white_balance", "tone", "saturation",
        "advanced_tone", "sharpening", "temperature_tint", "temperature_tint_strength", "max_sharpen",
        "per_channel_color", "per_channel_strength", "max_channel_gamma_delta", "max_channel_offset",
        "max_channel_contrast_delta", "max_channel_saturation_delta", "scene_grading", "scene_max_keyframes",
        "scene_min_seconds", "scene_change_threshold", "scene_min_confidence", "scene_probe_interval_seconds",
        "scene_probe_width", "scene_boundary_refine_seconds", "scene_probe_persistence",
        "scene_full_samples_per_segment", "scene_probe_max_samples",
        "ffmpeg_sample_timeout_seconds", "ffmpeg_aux_timeout_seconds", "ffprobe_timeout_seconds",
        "vr_verification", "vr_verification_frames", "orientation_verification", "orientation_auto_apply", "eye_swap_auto_apply",
        "stereo_alignment", "stereo_alignment_auto_apply", "stereo_alignment_min_confidence", "stereo_alignment_max_degrees",
        "stereo_roll_correction", "stereo_roll_auto_apply", "stereo_scale_correction", "stereo_scale_auto_apply",
        "stitch_correction", "stitch_auto_apply", "max_stitch_shift", "max_stitch_scale_delta",
        "auto_crop", "auto_crop_min_confidence", "auto_crop_max_zoom", "horizon_leveling", "horizon_auto_apply",
        "horizon_min_confidence", "horizon_max_degrees", "autofocus_tuning", "autofocus_auto_apply",
        "lens_verification", "lens_fov_auto_apply", "lens_fov_min_confidence",
        "audio_correction", "loudness_normalization", "audio_sync_analysis", "audio_sync_auto_apply",
        "target_audio_mean_db", "max_audio_gain",
        "min_rgb_gain", "max_rgb_gain",
    },
    "hsp": {
        "generated_version", "prefer_manual_writeback", "accept_hsp_writebacks", "revisioned_urls",
        "auto_sharpen", "auto_exposure", "auto_temperature", "auto_tint",
        "auto_rgb_gain", "auto_global_offset", "auto_rgb_offset", "auto_global_gamma", "auto_rgb_gamma",
        "auto_global_contrast", "auto_rgb_contrast", "auto_global_saturation",
        "auto_rgb_saturation", "auto_shadows", "auto_midtones", "auto_highlights",
        "auto_scene_keyframes", "write_auto_sidecars",
    },
    "paths": {"data_dir", "ffmpeg", "ffprobe"},
}


def public_config(cfg: Config) -> dict[str, object]:
    result: dict[str, object] = {
        "config_path": str(cfg.config_path or Path("config.toml").resolve()),
        "restart_required_for": ["server.host", "server.port", "paths.data_dir"],
        "environment_overrides": {
            "library.roots": bool(os.getenv("HERETUNE_LIBRARY")),
            "server.port": bool(os.getenv("HERETUNE_PORT")),
            "server.public_base_url": bool(os.getenv("HERETUNE_PUBLIC_BASE_URL")),
        },
        "setup_required": not bool(library_roots(cfg)),
        "analysis_presets": analysis_presets(),
    }
    for section_name, allowed in _EDITABLE_FIELDS.items():
        section = getattr(cfg, section_name)
        result[section_name] = {
            name: getattr(section, name)
            for name in sorted(allowed)
        }
    # The UI should explain these without ever exposing the secret values.
    result["server"]["username"] = cfg.server.username
    result["server"]["password_set"] = bool(cfg.server.password)
    result["server"]["auth_token_set"] = bool(cfg.server.auth_token)
    result["server"]["dashboard_username"] = cfg.server.dashboard_username
    result["server"]["dashboard_password_set"] = bool(cfg.server.dashboard_password)
    return result


def save_config_patch(
    config_path: str | os.PathLike[str], patch: dict[str, object]
) -> Config:
    """Validate a dashboard patch and atomically rewrite config.toml.

    Runtime adoption is handled by the server only after this atomic write
    succeeds. Bind host, port, and data_dir remain restart-only; other settings
    can be hot-applied safely by the running application.
    """
    path = Path(config_path).resolve()
    cfg = load_config(path)
    for section_name, allowed in _EDITABLE_FIELDS.items():
        incoming = patch.get(section_name)
        if incoming is None:
            continue
        if not isinstance(incoming, dict):
            raise ValueError(f"{section_name} must be an object")
        section = getattr(cfg, section_name)
        for name, value in incoming.items():
            if section_name == "server" and name == "dashboard_password":
                text = str(value or "")
                if text:
                    cfg.server.dashboard_password = text
                continue
            if name not in allowed:
                raise ValueError(f"Setting is not editable here: {section_name}.{name}")
            setattr(section, name, value)

    cfg = _normalize(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(config_to_toml(cfg), encoding="utf-8")
    os.replace(temp, path)
    return cfg
