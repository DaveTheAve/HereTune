# Changelog

## v0.13.1

- Added a global **native calibration HSP output policy** for stereo disparity, stereo roll, eye scale/shear, stitch vectors, encoded-border crop, horizon leveling, 180° orientation correction, eye swap, lens/FOV suggestions, autofocus range, and audio timing/drift. Public v0.13.0 behavior is preserved: confidence-gated stereo disparity/alignment and safe encoded-border crop remain **on by default**, while every newly exposed automatic native-calibration output remains **off by default**. Analysis can still measure and retain the evidence. The video drawer now exposes **Global / On / Off** per-video controls for every native-calibration suggestion so one title can audition or suppress a correction without changing the rest of the library; global confidence gates remain active for inherited automatic behavior, while an explicit per-video **On** bypasses the confidence threshold for that title; hard validity/range/projection checks and explicit numeric layout overrides remain authoritative.
- Retired the older Analysis-side `*_auto_apply` / `auto_crop` application settings so measurement and HSP serialization no longer have overlapping control paths. Saving Settings rewrites the current config shape without those retired keys.
- Changed table bulk selection to a Gmail-style two-step flow: **Select page** first, then offer **Select all N matching videos** when more filtered pages exist.
- Added a numeric **Go to page** control to the full bottom paginator while keeping the compact top paginator unchanged.
- Added the public GitHub repository throughout the relevant help/support surfaces and to the application footer.
- Refreshed Settings and modal presentation for the larger configuration surface: sticky section navigation, settings search, stronger grouping, modern switch-style toggles, clearer enabled states, and consistent dialog chrome/close interactions.

## v0.13.0 — First public release

- Added adaptive native **HSP v7** image calibration without re-encoding or duplicating source media.
- Added multi-root local/drive/UNC/NAS libraries with relocation reconciliation, duplicate hints, favorites, half-star ratings, and watch state.
- Added the `offset-free-tone-1` analyzer: Auto Global Offset is excluded from tone solving and disabled by default; Shadows is measured but disabled by default through the reversible Auto-HSP output policy.
- Added global Auto-HSP control policy plus per-video **Inherit / Force On / Force Off** overrides that rebuild profiles from saved evidence without re-analysis.
- Added sparse scene-by-scene grades, per-channel nonlinear color trims, VR verification/native calibration evidence, audio loudness/timing evidence, and analysis history.
- Added HereSphere browse-safety behavior: Ready-only library mode by default, HSP delivery only on actual playback/media-source requests, HSP write-back disabled by default, immutable revisioned HSP URLs, and strict player-facing HSP validation.
- Added optional managed `.hsp` sidecars beside source media, including retroactive background synchronization with live progress/ETA, conflict protection, atomic writes, and active-playback deferral.
- Added compact HereSphere group labels: `♥`, `All`, then `A`, `B`, `C`… for real folder groups, generated without persistent mappings.
- Added browser playback, VR-to-2D monitoring, full-strength best-effort HSP comparisons, Analysis Inspector, checkpoint statistics cache, Priority Queue, Doctor, and Library Health.
- Added responsive dashboard pagination, half-star interaction, polished hover/focus states, favicon, README screenshots, Windows CI, CodeQL, release attestations, checksums, and public-release hygiene checks.
