from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .hsp import HSPError, validate_playback_safe


class SidecarError(RuntimeError):
    pass


class SidecarConflict(SidecarError):
    pass


@dataclass(slots=True)
class SidecarResult:
    path: Path
    revision: str
    status: str
    backup: Path | None = None


def revision(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()[:16]


def sidecar_path(source: str | Path) -> Path:
    return Path(source).with_suffix(".hsp")


def _same_path(a: object, b: Path) -> bool:
    if not a:
        return False
    try:
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))
    except OSError:
        return str(a) == str(b)


def inspect_status(video: dict[str, object], desired_blob: bytes | None = None) -> dict[str, object]:
    target = sidecar_path(str(video.get("path") or ""))
    exists = target.is_file()
    current_revision = None
    error = None
    if exists:
        try:
            current_revision = revision(target.read_bytes())
        except OSError as exc:
            error = str(exc)
    tracked_revision = str(video.get("sidecar_hsp_revision") or "") or None
    # A video + sidecar may be moved together. Preserve HereTune ownership when
    # the file at the new location still has the exact revision we wrote, even
    # though the stored absolute path necessarily changed during relocation.
    managed_flag = bool(video.get("sidecar_hsp_managed"))
    managed = bool(
        managed_flag
        and (
            _same_path(video.get("sidecar_hsp_path"), target)
            or (tracked_revision and current_revision == tracked_revision)
        )
    )
    desired_revision = revision(desired_blob) if desired_blob else None
    current = bool(exists and desired_revision and current_revision == desired_revision)
    conflict = bool(exists and not current and (not managed or tracked_revision != current_revision))
    return {
        "path": str(target),
        "exists": exists,
        "managed": managed,
        "tracked_revision": tracked_revision,
        "file_revision": current_revision,
        "desired_revision": desired_revision,
        "current": current,
        "conflict": conflict,
        "error": error,
    }


def write_sidecar(
    video: dict[str, object],
    blob: bytes,
    *,
    replace: bool = False,
) -> SidecarResult:
    source = Path(str(video.get("path") or ""))
    if not source.name:
        raise SidecarError("Source video path is unavailable")
    target = sidecar_path(source)
    try:
        validate_playback_safe(
            blob,
            expected_filename=source.name,
            duration_seconds=float((video.get("metadata") or {}).get("duration") or 0.0),
        )
    except (HSPError, TypeError, ValueError) as exc:
        raise SidecarError(f"Refusing to write an unsafe HSP: {exc}") from exc

    desired_revision = revision(blob)
    status = inspect_status(video, blob)
    if status["current"]:
        return SidecarResult(target, desired_revision, "unchanged")

    backup = None
    if status["exists"] and status["conflict"]:
        if not replace:
            raise SidecarConflict(
                "A different, non-HereTune sidecar already exists beside this video. "
                "Use the explicit replace action if you intend to back it up and replace it."
            )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(f"{target.name}.bak.{stamp}")
        try:
            shutil.copy2(target, backup)
        except OSError as exc:
            raise SidecarError(f"Could not back up the existing sidecar: {exc}") from exc

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.heretune-", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            try:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            except OSError:
                pass
    except OSError as exc:
        raise SidecarError(f"Could not write sidecar beside the source video: {exc}") from exc

    return SidecarResult(target, desired_revision, "replaced" if status["exists"] else "written", backup)


def remove_managed_sidecar(video: dict[str, object]) -> Path:
    target = sidecar_path(str(video.get("path") or ""))
    status = inspect_status(video)
    if not status["exists"]:
        return target
    if not status["managed"]:
        raise SidecarConflict("This sidecar is not marked as HereTune-managed and will not be deleted.")
    tracked = str(video.get("sidecar_hsp_revision") or "")
    current = str(status.get("file_revision") or "")
    if tracked and current and tracked != current:
        raise SidecarConflict(
            "The sidecar changed outside HereTune after it was written. It will not be deleted automatically."
        )
    try:
        target.unlink()
    except OSError as exc:
        raise SidecarError(f"Could not remove sidecar: {exc}") from exc
    return target
