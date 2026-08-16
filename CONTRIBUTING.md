# Contributing to HereTune

Thanks for helping the tiny unpaid colorist become a slightly more competent tiny unpaid colorist.

## Before opening a pull request

1. Read `AGENTS.md` for project invariants and the "please do not invent an HSP field because the bytes looked lonely" rules.
2. Run the test suite:

   ```powershell
   python -m pytest -q
   ```

3. Run Python compilation and JavaScript syntax checks:

   ```powershell
   python -m compileall heretune
   node --check heretune/static/app.js
   node --check heretune/static/player.js
   ```

4. Do not commit `config.toml`, `data/`, personal NAS paths, logs, thumbnails, databases, HSP captures from private media, HereSphere binaries/PAKs, or saved HereSphere user data.

## Contribution license grant

HereTune's public license intentionally prohibits third-party commercial use. To keep the project owner able to use, license, and commercialize the complete project (including community contributions), contributions use the additional grant described in `CONTRIBUTOR_LICENSE_AGREEMENT.md`.

By submitting a pull request, you confirm that you have read and agree to that contribution grant. The PR template includes an explicit checkbox so this is not hidden in the carpet fibers.

## Pull-request style

- Keep changes focused.
- Add regression tests for bug fixes.
- Preserve compatibility for previously published `config.toml` settings when practical.
- Do not silently change correction semantics or analysis revisioning.
- UI work should be checked at desktop and narrow widths.
- If you change HSP serialization, include a native/export-derived reason and tests.

## Bug reports

Use the bug-report issue form. It asks for the information that saves everyone three rounds of "which Python? which FFmpeg? which HereTune?".
