from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dist-release"

EXCLUDED_PARTS = {
    ".git", ".github" + "-cache", ".venv", "venv", "data", "build", "dist", "dist-release",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode",
}
EXCLUDED_NAMES = {"config.toml"}


def include(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in EXCLUDED_PARTS for part in rel.parts):
        return False
    if path.name in EXCLUDED_NAMES or path.suffix in {".pyc", ".pyo"}:
        return False
    if any(part.endswith(".egg-info") for part in rel.parts):
        return False
    return True


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    sys.path.insert(0, str(ROOT))
    from heretune import __version__

    subprocess.run([sys.executable, str(ROOT / "scripts/check_public_release.py")], check=True)
    OUT.mkdir(exist_ok=True)
    for old in OUT.iterdir():
        if old.is_file(): old.unlink()
        elif old.is_dir(): shutil.rmtree(old)

    # setuptools may reuse build/lib when source files are edited multiple times
    # inside the same filesystem timestamp granularity. A stale wheel is worse
    # than no wheel, so release packaging always starts from a clean build tree.
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    shutil.rmtree(ROOT / "dist", ignore_errors=True)
    for egg_info in ROOT.glob("*.egg-info"):
        shutil.rmtree(egg_info, ignore_errors=True)

    subprocess.run([
        sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "-w", str(OUT)
    ], cwd=ROOT, check=True)

    zip_path = OUT / f"HereTune-v{__version__}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(ROOT.rglob("*")):
            if path.is_file() and include(path):
                archive.write(path, path.relative_to(ROOT).as_posix())

    artifacts = [zip_path, *OUT.glob(f"heretune-{__version__}-*.whl")]
    checksum = OUT / f"HereTune-v{__version__}-SHA256.txt"
    checksum.write_text("".join(f"{sha256(path)}  {path.name}\n" for path in artifacts), encoding="utf-8")
    print(f"Built HereTune v{__version__}: {zip_path}")


if __name__ == "__main__":
    main()
