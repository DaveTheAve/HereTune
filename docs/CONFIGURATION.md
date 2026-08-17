# HereTune Configuration

Project repository: [https://github.com/DaveTheAve/HereTune](https://github.com/DaveTheAve/HereTune)

HereTune can be configured through the dashboard **Settings** dialog or by editing `config.toml`. The repository ships only `config.example.toml`; the installer creates your real `config.toml` locally.

Settings saved from the UI are validated and written atomically. Most settings hot-apply immediately. The dashboard tells you when a change requires restart.

> Keep `config.toml` private. It can contain media paths, network shares, usernames, and passwords.

## Library

```toml
[library]
roots = ["D:\\VR", "E:\\Movies", "\\\\NAS\\Media\\VR"]
extensions = [".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi", ".ts", ".m2ts", ".mts", ".mpg", ".mpeg", ".wmv"]
scan_interval_seconds = 60
```

- `roots`: local folders, attached drives, UNC paths, and NAS shares.
- `extensions`: media types indexed by the scanner.
- `scan_interval_seconds`: delay between automatic discovery passes while work is running.

Each source identity includes its configured root plus relative path. Temporarily unreachable roots retain their catalog instead of marking everything missing.

## Server and HereSphere

```toml
[server]
host = "0.0.0.0"
port = 8777
public_base_url = ""
username = ""
password = ""
auth_token = ""
dashboard_username = ""
dashboard_password = ""
allow_file_delete = false
start_paused = true
show_hsp_diagnostic_tools = false
compact_heresphere_library_labels = false
heresphere_ready_only = true
```

### Networking

- `host = "0.0.0.0"` lets a headset on your LAN reach HereTune.
- `port = 8777` is the default dashboard/Web API port.
- `public_base_url` is normally blank; set it only when HereTune must advertise a specific externally visible base URL.
- `HERETUNE_PORT` and `HERETUNE_PUBLIC_BASE_URL` can override these after restart.

Do not expose HereTune directly to the public Internet.

### Authentication

`username`, `password`, and `auth_token` belong to the HereSphere Web API. `dashboard_username` / `dashboard_password` protect the HereTune dashboard with HTTP Basic authentication.

### Startup and diagnostics

- `start_paused = true`: wait for **Resume work** before automatic scanning/analysis.
- `show_hsp_diagnostic_tools = false`: hide temporary per-control headset A/B tools until needed.

### HereSphere library presentation

- `heresphere_ready_only = true`: advertise only videos with completed analysis and an available Auto/accepted Manual HSP. Pending/error media remains visible in HereTune.
- `compact_heresphere_library_labels = true`: present `♥`, `All`, then `A`, `B`, `C`… folder aliases to work around HereSphere library navigation with long group names. Aliases are generated per response and are not persisted.

## Analysis runtime

```toml
[analysis]
execution_mode = "auto"
auto_preference = "hybrid"
gpu_device = 0
gpu_decode_workers = 4
cpu_analysis_workers = 8
probe_workers = 8
gpu_fallback_to_cpu = true
adaptive_workers = true
isolate_analysis_worker = true
analysis_cache_enabled = true
analysis_cache_max_gb = 20.0
```

Modes:

- `cpu`: CPU decode/scale + analysis.
- `gpu`: CUDA/NVDEC decode/resize, then the same NumPy/OpenCV analysis.
- `hybrid`: overlap GPU frame acquisition with CPU analysis.
- `auto`: prefer Hybrid when CUDA is healthy, otherwise CPU.

`adaptive_workers` treats worker counts as ceilings and backs off under CPU/RAM/GPU/NAS pressure. `isolate_analysis_worker` keeps heavy native decoder/OpenCV failures away from the dashboard process.

The checkpoint cache contains **statistics only**, not analysis JPEGs.

## Sampling

```toml
samples = 48
analysis_width = 960
adaptive_sampling = true
sample_interval_seconds = 30.0
max_samples = 360
smart_sampling = true
candidate_multiplier = 1.35
```

- `analysis_width = 960` is the shipped quality default.
- `max_samples = 0` means **unlimited**.
- `sample_interval_seconds` governs expensive full correction sampling, not dense scene-boundary probing.

The Settings presets—Balanced, High Coverage, Fast, and Low Resource—are convenient starting points, not hidden modes. Applying a preset simply fills visible settings before Save.

## Image analysis

```toml
strength = 1.0
skin_priority = 1.25
white_balance = true
tone = true
saturation = true
advanced_tone = true
sharpening = true
temperature_tint = true
temperature_tint_strength = 0.45
max_sharpen = 3.0
min_rgb_gain = 0.80
max_rgb_gain = 1.24
```

Per-channel residual controls:

```toml
per_channel_color = true
per_channel_strength = 0.55
max_channel_gamma_delta = 0.045
max_channel_offset = 0.016
max_channel_contrast_delta = 0.035
max_channel_saturation_delta = 0.030
```

These bounded RGB trims follow the main white-balance/tone correction.

## Scene grading

```toml
scene_grading = true
scene_max_keyframes = 0
scene_min_seconds = 30.0
scene_change_threshold = 1.15
scene_min_confidence = 0.62
scene_probe_interval_seconds = 10.0
scene_probe_width = 480
scene_boundary_refine_seconds = 2.0
scene_probe_persistence = 2
scene_full_samples_per_segment = 9
scene_probe_max_samples = 900
```

`scene_max_keyframes = 0` means **unlimited analysis evidence**. Player-facing HSP Image arrays are separately canonicalized and safety-capped.

## Decoder watchdogs

```toml
ffmpeg_sample_timeout_seconds = 20.0
ffmpeg_aux_timeout_seconds = 45.0
ffprobe_timeout_seconds = 30.0
```

These limits prevent a broken/random NAS seek from leaving an `ffmpeg.exe` stuck indefinitely.

## VR verification and native calibration

Analysis and HSP output are intentionally separate. The Analysis settings below decide what HereTune measures and stores; the HSP switches later in this document decide which automatic native-calibration suggestions may reach HereSphere.

```toml
vr_verification = true
vr_verification_frames = 6
orientation_verification = true
stereo_alignment = true
stereo_roll_correction = true
stereo_scale_correction = true
stitch_correction = true
auto_crop_min_confidence = 0.82
auto_crop_max_zoom = 1.32
horizon_leveling = true
autofocus_tuning = true
lens_verification = true
```

These controls keep measuring stereo disparity, eye scale/shear, stitch suggestions, persistent borders, horizon, orientation, lens/FOV, and autofocus evidence even when automatic HSP application is disabled. The confidence/range settings in `config.example.toml` bound those measurements.

## Audio and sync

```toml
audio_correction = true
loudness_normalization = true
audio_sync_analysis = true
target_audio_mean_db = -20.0
max_audio_gain = 1.35
```

Loudness normalization is bounded. A/V offset/drift remains measured evidence until `hsp.auto_audio_sync` is explicitly enabled.

## HSP output policy

```toml
[hsp]
generated_version = 7
prefer_manual_writeback = false
accept_hsp_writebacks = false
revisioned_urls = true

auto_global_offset = false
auto_shadows = false
auto_midtones = true
auto_highlights = true

# Native calibration output: v0.13.0 defaults preserved
# Stereo disparity and safe encoded-border crop are confidence-gated ON.
auto_stereo_disparity = true
auto_stereo_roll = false
auto_eye_scale_shear = false
auto_stitch_vectors = false
auto_crop = true
auto_horizon = false
auto_orientation_180 = false
auto_eye_swap = false
auto_lens_fov = false
auto_autofocus = false
auto_audio_sync = false
```

The full set of image and native-calibration `auto_*` switches is in `config.example.toml` and Settings.

Important semantics:

- **Analysis evidence and HSP output policy are separate.** Turning a control off does not discard its saved measurement.
- Global Offset is special: it never participates in the analyzer/tone solver. Its optional Force On value is output-only and tightly bounded.
- Shadows is measured but disabled in Auto by default.
- Stereo disparity/alignment and safe encoded-border crop preserve their v0.13.0 confidence-gated defaults. Newly exposed native geometry/player-calibration outputs are OFF by default. Changing one rebuilds existing Auto HSPs from saved evidence; it does not re-analyze the source video.
- Every native-calibration suggestion also has a persistent per-video **Global / On / Off** control in the video drawer. **On** explicitly requests that suggestion for the selected video and bypasses its confidence threshold; **Off** suppresses it only for that video; **Global** follows Settings and keeps the normal confidence gate. These overrides rebuild the Auto HSP from saved evidence without re-analysis.
- Per-video **On** bypasses only the confidence threshold. Hard validity checks still remain: usable measurements must exist, values must be finite/range-safe, projection/layout constraints must be compatible, and explicit/manual metadata overrides remain authoritative.
- Explicit per-video numeric layout/vector overrides remain authoritative over both global and per-video automatic native-calibration policy.
- Older `analysis.*_auto_apply` / `analysis.auto_crop` settings have been retired; automatic application policy now lives under `[hsp]` plus the per-video HSP policy stored by the drawer. Saving Settings rewrites `config.toml` without those retired keys.
- Per-video **Image** policy continues to support **Inherit global / Force On / Force Off** independently from the native-calibration Global / On / Off controls.

### HSP write-back

`accept_hsp_writebacks = false` is the safety default. Enable it only when you specifically need HereSphere-imported/manual HSP persistence.

HereTune supplies HSPs only on actual playback/media-source requests—not metadata-only browsing.

## HSP sidecars

```toml
write_auto_sidecars = false
```

When enabled, HereTune atomically maintains `<video-stem>.hsp` beside Ready source videos. Existing Ready media is synchronized retroactively as a visible background job. Unknown/user-owned HSPs are not silently overwritten, and active playback defers writes.

Turning the option off stops future synchronization; it does not bulk-delete existing sidecars.

## Paths

```toml
[paths]
data_dir = "./data"
ffmpeg = "ffmpeg"
ffprobe = "ffprobe"
```

`data_dir` contains the SQLite database, thumbnails, logs, checkpoint statistics, and backups. Changing `data_dir`, server host, or port requires restart.

## Environment overrides

Supported convenience overrides include:

```text
HERETUNE_LIBRARY
HERETUNE_PORT
HERETUNE_PUBLIC_BASE_URL
```

Environment overrides take precedence after restart and are shown in Settings when active.
