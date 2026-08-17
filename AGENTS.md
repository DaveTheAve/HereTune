# AGENTS.md — guidance for coding agents and automated contributors

This file is intentionally direct. HereTune manipulates media metadata, native HereSphere HSP profiles, and large NAS libraries; confident guessing is expensive.

## Project invariants

1. **Never re-encode source media for normal HereTune correction.** Corrections are delivered through native HereSphere HSP settings; the original media stream remains untouched.
2. **HSP standalone files use the validated outer `int32 1024` marker followed by the inner HSP/UMediaPreset version (`7` for the current writer).** Embedded `Library.hsl` records start at the inner version and do not use the standalone wrapper.
3. **Do not invent HSP enum values, field meanings, transfer functions, or newer-version serialization.** Unknown is an acceptable answer.
4. **Image-vector fourth components are global controls.** RGB components are per-channel trims. Preserve this in parsers, previews, and UI copy.
5. **`analysis_width = 960` is the current quality default.** Do not casually lower it in the Balanced/High Coverage profiles.
6. **`max_samples = 0` and `scene_max_keyframes = 0` mean unlimited.** Never normalize them into hidden caps.
7. **One failed video must log loudly and allow the queue to continue.** Do not add a queue-wide circuit breaker unless explicitly requested.
8. **Analysis-cache data is statistics/checkpoints only.** Do not reintroduce persistent per-sample JPEG caches.
9. **Priority insertion is FIFO.** New priority requests append behind existing priority items unless the user explicitly reorders them.
10. **Preserve native-calibration defaults deliberately.** Analysis may measure stereo disparity, roll, eye scale/shear, stitch, crop, horizon, orientation, eye-swap, generic FOV, autofocus, and sync evidence. Stereo disparity/alignment and safe encoded-border crop preserve the public v0.13.0 confidence-gated ON defaults; newly exposed automatic native-calibration HSP outputs default OFF. Every native output supports a persistent per-video Global / On / Off policy. Global automation is confidence-gated; explicit per-video On bypasses only the confidence threshold while retaining hard validity/range/projection checks. Explicit numeric layout overrides remain authoritative.
11. **Auto never generates Global Offset.** Headset A/B testing identified additive whole-image Offset as the main haze/washed-out culprit. Gamma/Midtones must be solved without an Offset term; RGB Offset remains a separate bounded per-channel correction. Do not reintroduce Global Offset as a hidden analyzer variable or "temporary" brightness helper.
12. **Multi-root identity is root + relative path.** Identical relative paths on different roots must not collide. A temporarily unreachable root must not make its catalog missing; a deliberately removed root may.
13. **Preview approximations are diagnostic, not cosmetic.** Do not silently damp HSP controls just to make Before/After look nicer. Use the selected serialized HSP at full best-effort strength and clearly leave HereSphere authoritative.
14. **Auto-HSP output policy is separate from analysis evidence.** Global Offset and Shadows are OFF by default, but measurements remain available for reversible global/per-video policy. Global Offset may be serialized only as a separately bounded suggestion; it must never feed the tone solver.
15. **Never provoke a live HSP replacement during an import/write-back.** HSP write-back is opt-in, acknowledgements must omit media/HSP URLs, stale immutable revisions must not return different bytes, and sidecar/policy/layout mutations must wait until HereSphere closes the active video.
16. **Do not advertise HSPs during metadata-only HereSphere browsing.** Supply `hspArray` only on the actual playback/media-source request. Ready-only library mode is the safe default.

## Public-repository hygiene

- Never commit `config.toml`, `data/`, databases, logs, private paths, credentials, thumbnails, captured private frames, proprietary HereSphere executables/PAKs, or saved HereSphere user data.
- Keep exactly one tracked config template: `config.example.toml`.
- Documentation may describe observed behavior and narrow reverse-engineering results, but may not redistribute proprietary HereSphere assets.
- HereTune is independent and not affiliated with HereSphere.

## Validation expectations

For Python/UI changes, run:

```text
python -m compileall heretune
python -m pytest -q
node --check heretune/static/app.js
node --check heretune/static/player.js
```

For packaging/release work also run:

```text
python scripts/check_public_release.py
python scripts/build_release.py
```

## Change discipline

- Bug fix -> regression test.
- HSP change -> native/export-derived evidence + parser/writer test.
- Config default change -> update `config.example.toml`, Settings copy, preset logic, and config consistency tests.
- UI change -> check both wide desktop and narrow layouts; do not solve every responsive problem by making the entire site wider.
- Keep humor in user-facing docs/UI where it helps; do not let jokes obscure safety, errors, or configuration semantics.
