# Changelog

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
