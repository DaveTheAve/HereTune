# HereTune Troubleshooting

Start with **Run doctor** in the dashboard. It checks Python, FFmpeg/FFprobe, OpenCV, CUDA/NVDEC, HSP serialization, library reachability, database integrity, cache/storage, and other runtime prerequisites.

## Unsupported database schema

If HereTune reports that the configured database schema is unsupported, stop HereTune, move the configured `data/` directory out of the way, and start again with a fresh data directory. Source videos are not stored inside `data/` and are not modified by this reset.

## Library path is not reachable

Open the exact path in Windows Explorer first. For UNC/NAS shares, authenticate in Windows before expecting Python to negotiate SMB through sheer force of personality.

A temporarily unavailable configured root does not cause HereTune to mark its entire catalog missing.

## FFmpeg or FFprobe not found

From PowerShell:

```powershell
ffmpeg -version
ffprobe -version
```

Fix `PATH`, or set full executable paths under **Settings → Runtime/tools**.

## CUDA/NVDEC is unavailable for one file

HereTune can fall back to CPU on a source-specific decoder failure. Codec/profile/bit-depth/GOP characteristics may make one high-resolution file much slower than another even when the final analysis width is identical; source frames must still be decoded and sought before downscaling.

Run Doctor to see the resolved execution mode and CUDA capability checks.

## Analysis seems stuck

Check the work strip first. Each source seek has a watchdog; a genuinely failed video should error and allow the queue to continue.

Failure details are written under the configured `data_dir`, including analysis failure logs. One broken movie is allowed to have a bad day. The rest of the queue is not required to attend.

## Analysis restarted after an interruption

The analysis cache stores compact statistics/checkpoints so work can resume. It does not store per-sample evidence JPEGs. Inspector images are decoded live from the source.

## HereSphere cannot connect to HereTune

- PC and headset must be able to reach each other.
- Allow HereTune/Python through Windows Firewall on the **Private** network profile.
- From the headset, use the PC's LAN IP—not `localhost` or `127.0.0.1`.
- Default Web API endpoint: `http://PC-LAN-IP:8777/heresphere`.
- Keep the service on a trusted LAN; do not expose it directly to the Internet.

## The Copy button cannot access the clipboard

Browsers differ in clipboard permissions, especially on plain HTTP LAN pages. HereTune first uses the synchronous copy path, then the Clipboard API, and finally selects the URL so a manual `Ctrl+C` is immediately available.

## HereSphere's folder names run off the screen

Enable **Compact library folder labels** under HereSphere settings. The library is presented as:

```text
♥
All
A
B
C
...
```

The aliases are generated dynamically and are not stored. Adding/removing folder groups can therefore change which real folder receives a letter.

## Videos still being analyzed appear in HereSphere

Enable **Show only correction-ready videos in HereSphere** (the default). Pending, changed, analyzing, skipped, errored, HSP-less, and missing entries stay visible in HereTune but are withheld from the HereSphere Web API library.

Refresh the Web API library in HereSphere after changing this setting.

## HereSphere crashes while browsing the Web API library

During development, native HereSphere/UE4 failures were observed while browsing a Web API library even with no video playing, including messages such as:

```text
FMemoryWriter does not support data larger than 2GB
BulkData compressed header read error
```

These are native player errors; the exact internal cause is not established. HereTune therefore follows two conservative rules:

1. metadata-only library/video-detail responses do **not** advertise `hspArray`;
2. the selected HSP is advertised only when HereSphere makes an actual playback/media-source request.

Keep **Ready-only HereSphere library** enabled while diagnosing this class of failure. If a crash still occurs, capture the final HereTune terminal lines containing `LIBRARY`, `SCAN`, `VIDEO`, `PLAY`, or `HSP GET`, plus the exact HereSphere crash dialog.

## HereSphere crashes when importing/replacing an HSP

Avoid importing/replacing a target HSP while its video is actively playing. Close the video, import or replace the HSP, then reopen playback.

HereTune keeps Web API HSP write-back disabled by default and avoids echoing another media/HSP URL in mutation acknowledgements. Player-facing generated HSPs are validated before delivery.

A crash entirely inside HereSphere's local import path cannot be intercepted or guaranteed away by HereTune; preserve the exact file and crash dialog for comparison.

## A generated HSP does not appear to load

Check:

- HereSphere **Always Load HSP** is enabled for Web API media;
- the video is Ready in HereTune;
- the requested HSP endpoint returns successfully;
- the HereTune terminal does not report `HSP WITHHELD`;
- you are not testing against a stale cached library entry.

HereTune uses revisioned HSP URLs so recalculated profiles have distinct immutable identities.

## HSP sidecar synchronization reports conflicts

When **Keep Auto HSP files beside source videos** is enabled, HereTune refuses to silently replace an unknown or externally changed `.hsp` file.

The background work strip reports created/current/conflict/deferred/error counts. A video open in HereSphere is deferred until safe. Unknown files require explicit per-video handling.

Turning global sidecar synchronization off stops future writes but does not delete sidecars already created.

## The Before/After preview differs from HereSphere

Expected. HereTune uses the exact selected HSP values at full best-effort approximation strength, but not every internal HereSphere transfer function is public. The preview is diagnostic; **HereSphere remains authoritative for final playback**.

## A video looks hazy or washed out

Auto Global Offset and Shadows are disabled by default because headset A/B testing found that lower-tone lifting could create a gray veil on some sources.

Use the per-video **Inherit / Force On / Force Off** policy or the optional HSP isolation tools to identify a problematic native control without broadly weakening the rest of the correction stack.

## HDR correction was skipped

HereTune may deliberately avoid automatic correction where transfer/bit-depth handling makes the current SDR-oriented analysis unsafe.

Sometimes the most advanced algorithm is “do not ruin it.”

## Browser playback falls back or transcodes temporarily

HereSphere always receives the untouched original-media path. The desktop browser player may need a temporary compatibility stream for codecs Chrome cannot decode directly. This does not create a persistent corrected duplicate in your library.

## Need a useful bug report?

Include:

- HereTune version;
- Python version;
- relevant Doctor results;
- source codec/resolution/bit depth/projection/stereo details;
- the smallest relevant terminal/log excerpt;
- exact steps to reproduce;
- exact HereSphere crash/error text if applicable.

Sanitize media titles, paths, usernames, hostnames, and credentials before posting publicly.
