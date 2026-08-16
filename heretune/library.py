from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .analyzer import ANALYSIS_VERSION
from .config import library_roots

FINGERPRINT_VERSION = 1
FINGERPRINT_CHUNK = 256 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sid(relative_path: str) -> str:
    normalized = relative_path.replace("\\", "/").casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def friendly_title(path: Path) -> str:
    title = re.sub(r"[_\s]+", " ", path.stem).strip()
    return title or path.stem


def content_fingerprint(path: Path, size: int | None = None) -> str:
    """Cheap rename/duplicate fingerprint without hashing a 50 GB movie.

    Hash size + first/middle/last 256 KiB. It is not a cryptographic proof of
    identity, but collisions are extraordinarily unlikely for the local-library
    jobs it serves and it keeps SMB traffic bounded.
    """
    stat = path.stat()
    size = int(stat.st_size if size is None else size)
    digest = hashlib.blake2b(digest_size=20, person=b"HereTuneMediaV1")
    digest.update(size.to_bytes(8, "little", signed=False))
    if size <= 0:
        return digest.hexdigest()
    offsets = [0, max(0, size // 2 - FINGERPRINT_CHUNK // 2), max(0, size - FINGERPRINT_CHUNK)]
    with path.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            chunk = handle.read(FINGERPRINT_CHUNK)
            digest.update(offset.to_bytes(8, "little", signed=False))
            digest.update(chunk)
    return digest.hexdigest()


class Library:
    def __init__(self, db, cfg) -> None:
        self.db = db
        self.cfg = cfg

    def _refresh_duplicates(self) -> int:
        self.db.exec("UPDATE videos SET duplicate_group=NULL")
        groups = self.db.all(
            """SELECT content_fingerprint, COUNT(*) AS n
               FROM videos WHERE status!='missing' AND content_fingerprint IS NOT NULL
               GROUP BY content_fingerprint HAVING COUNT(*) > 1"""
        )
        for group in groups:
            self.db.exec(
                "UPDATE videos SET duplicate_group=? WHERE status!='missing' AND content_fingerprint=?",
                (group["content_fingerprint"], group["content_fingerprint"]),
            )
        return len(groups)

    def scan(self) -> dict[str, object]:
        roots = library_roots(self.cfg)
        if not roots:
            raise FileNotFoundError(
                "Library is not configured yet. Open the HereTune dashboard to add at least one media folder."
            )

        def path_key(value: str | Path) -> str:
            return os.path.normcase(os.path.normpath(str(value))).casefold()

        old_rows = self.db.scan_rows()
        old = {str(video["id"]): video for video in old_rows}
        fingerprint_map: dict[str, list[dict[str, object]]] = {}
        for video in old_rows:
            fp = str(video.get("content_fingerprint") or "")
            if fp:
                fingerprint_map.setdefault(fp, []).append(video)

        seen: set[str] = set()
        seen_paths: set[str] = set()
        errors: list[str] = []
        added = changed = unchanged = stale = renamed = fingerprinted = 0
        relocation_reconciled = 0
        retired_ids: set[str] = set()
        timestamp = now()
        root_results: list[dict[str, object]] = []
        root_success: set[str] = set()
        configured_root_keys = {path_key(root): root for root in roots}

        for root_text in roots:
            root = Path(root_text)
            root_key = path_key(root_text)
            root_errors: list[str] = []
            root_seen = 0
            try:
                reachable = root.exists() and root.is_dir()
            except OSError as exc:
                reachable = False
                root_errors.append(str(exc))
            if not reachable:
                detail = f"Library folder not reachable: {root_text}"
                errors.append(detail)
                root_results.append({
                    "root": root_text, "reachable": False, "seen": 0,
                    "partial": True, "errors": [detail],
                })
                continue

            def walk_error(exc: OSError) -> None:
                message = str(exc)
                root_errors.append(message)
                errors.append(f"{root_text}: {message}")

            for directory, _, names in os.walk(root, onerror=walk_error):
                for name in names:
                    path = Path(directory) / name
                    if path.suffix.lower() not in self.cfg.library.extensions:
                        continue
                    absolute_key = path_key(path)
                    # Overlapping configured folders may expose the exact same file.
                    # Index it once under the first matching root rather than creating
                    # two database rows for one source path.
                    if absolute_key in seen_paths:
                        continue
                    seen_paths.add(absolute_key)
                    try:
                        stat = path.stat()
                        relative = path.relative_to(root).as_posix()
                    except (OSError, ValueError) as exc:
                        message = f"{path}: {exc}"
                        root_errors.append(message)
                        errors.append(message)
                        continue

                    root_seen += 1
                    identity = f"{root_key}::{relative}"
                    path_id = sid(identity)
                    previous = old.get(path_id)
                    title = friendly_title(path)
                    fp: str | None = None

                    if (
                        previous is None
                        or not previous.get("content_fingerprint")
                        or previous.get("size") != stat.st_size
                        or previous.get("mtime_ns") != stat.st_mtime_ns
                    ):
                        try:
                            fp = content_fingerprint(path, stat.st_size)
                            fingerprinted += 1
                        except OSError as exc:
                            errors.append(f"Fingerprint {path}: {exc}")
                    else:
                        fp = str(previous.get("content_fingerprint"))

                    # Preserve identity across a rename/move, including moves from
                    # one configured library root to another, when one old missing
                    # path has the same bounded content fingerprint.
                    if previous is None and fp:
                        candidates = [
                            candidate for candidate in fingerprint_map.get(fp, [])
                            if str(candidate.get("id")) not in seen
                        ]
                        gone = []
                        for candidate in candidates:
                            try:
                                old_exists = Path(str(candidate.get("path") or "")).exists()
                            except OSError:
                                old_exists = True
                            if not old_exists:
                                gone.append(candidate)
                        if len(gone) == 1:
                            previous = gone[0]
                            path_id = str(previous["id"])
                            self.db.exec(
                                """UPDATE videos SET path=?, rel_path=?, library_root=?, title=?, size=?, mtime_ns=?,
                                   last_seen=?, status=CASE WHEN status='missing' THEN 'ready' ELSE status END,
                                   content_fingerprint=?, fingerprint_version=? WHERE id=?""",
                                (
                                    str(path), relative, root_text, title, stat.st_size, stat.st_mtime_ns,
                                    timestamp, fp, FINGERPRINT_VERSION, path_id,
                                ),
                            )
                            renamed += 1
                            seen.add(path_id)
                            continue

                    seen.add(path_id)
                    if previous is None:
                        self.db.exec(
                            """INSERT INTO videos(
                                   id,path,rel_path,library_root,title,size,mtime_ns,status,date_added,last_seen,
                                   content_fingerprint,fingerprint_version
                               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                path_id, str(path), relative, root_text, title, stat.st_size,
                                stat.st_mtime_ns, "queued", timestamp, timestamp,
                                fp, FINGERPRINT_VERSION if fp else 0,
                            ),
                        )
                        if fp:
                            fingerprint_map.setdefault(fp, []).append(self.db.get(path_id) or {})
                        added += 1
                        continue

                    if previous["size"] != stat.st_size or previous["mtime_ns"] != stat.st_mtime_ns:
                        self.db.exec(
                            """UPDATE videos SET
                                   path=?, rel_path=?, library_root=?, title=?, size=?, mtime_ns=?, status='changed',
                                   error=NULL, metadata_json=NULL, analysis_json=NULL, auto_hsp=NULL,
                                   auto_hsp_version=NULL, profile_mode='auto', thumb_path=NULL, last_seen=?,
                                   content_fingerprint=?, fingerprint_version=?
                               WHERE id=?""",
                            (
                                str(path), relative, root_text, title, stat.st_size, stat.st_mtime_ns,
                                timestamp, fp, FINGERPRINT_VERSION if fp else 0, path_id,
                            ),
                        )
                        changed += 1
                    else:
                        previous_analysis = previous.get("analysis") or {}
                        old_version = str(previous_analysis.get("analysis_version") or "")
                        old_gpu_probe = int(previous_analysis.get("gpu_probe_version") or 0)
                        needs_gpu_probe_refresh = (
                            previous.get("status") == "ready"
                            and bool(previous_analysis.get("gpu_preflight_fallback"))
                            and old_gpu_probe < 2
                        )
                        needs_new_analysis = (
                            previous.get("status") == "ready"
                            and (old_version != ANALYSIS_VERSION or needs_gpu_probe_refresh)
                        )
                        if needs_new_analysis:
                            self.db.exec(
                                """UPDATE videos SET path=?, rel_path=?, library_root=?, title=?, last_seen=?, status='queued',
                                   error=NULL, analysis_json=NULL, auto_hsp=NULL, auto_hsp_version=NULL,
                                   profile_mode='auto', content_fingerprint=COALESCE(content_fingerprint,?),
                                   fingerprint_version=CASE WHEN content_fingerprint IS NULL THEN ? ELSE fingerprint_version END
                                   WHERE id=?""",
                                (str(path), relative, root_text, title, timestamp, fp, FINGERPRINT_VERSION, path_id),
                            )
                            stale += 1
                        else:
                            self.db.exec(
                                """UPDATE videos SET path=?, rel_path=?, library_root=?, title=?, last_seen=?,
                                   status=CASE WHEN status='missing' THEN 'queued' ELSE status END,
                                   profile_mode=CASE WHEN status='missing' THEN 'auto' ELSE profile_mode END,
                                   content_fingerprint=COALESCE(content_fingerprint,?),
                                   fingerprint_version=CASE WHEN content_fingerprint IS NULL THEN ? ELSE fingerprint_version END
                                   WHERE id=?""",
                                (str(path), relative, root_text, title, timestamp, fp, FINGERPRINT_VERSION, path_id),
                            )
                            unchanged += 1

            if not root_errors:
                root_success.add(root_key)
            root_results.append({
                "root": root_text, "reachable": True, "seen": root_seen,
                "partial": bool(root_errors), "errors": root_errors[:10],
            })

        def row_root_key(video: dict[str, object]) -> str | None:
            stored = str(video.get("library_root") or "").strip()
            if stored:
                return path_key(stored)
            source = Path(str(video.get("path") or ""))
            for root_text in roots:
                try:
                    source.relative_to(Path(root_text))
                    return path_key(root_text)
                except (ValueError, OSError):
                    continue
            return None

        # A cross-volume move can momentarily look like a duplicate: Windows may
        # finish copying the destination before deleting the source. If a previous
        # scan saw both paths, the destination already has a transient row and the
        # normal "new path" rename matcher above no longer runs. Reconcile that
        # short-lived duplicate only when the historical source is now proven gone,
        # the destination appeared after the historical record, and the match is
        # unique. Long-standing duplicate copies therefore remain separate.
        current_rows = {str(row["id"]): row for row in self.db.scan_rows()}

        def parsed_time(value: object) -> datetime | None:
            text = str(value or "").strip()
            if not text:
                return None
            try:
                result = datetime.fromisoformat(text)
                return result if result.tzinfo is not None else result.replace(tzinfo=timezone.utc)
            except ValueError:
                return None

        for source_id, source in old.items():
            if source_id in seen or not source.get("content_fingerprint"):
                continue
            key = row_root_key(source)
            if key in configured_root_keys:
                # Only a complete successful scan proves absence. A sleeping NAS
                # or partial walk is not evidence that a file was moved.
                source_gone = key in root_success
            else:
                try:
                    source_gone = not Path(str(source.get("path") or "")).exists()
                except OSError:
                    source_gone = False
            if not source_gone:
                continue

            source_added = parsed_time(source.get("date_added"))
            source_last_seen = parsed_time(source.get("last_seen"))
            if source_added is None or source_last_seen is None:
                continue

            candidates: list[dict[str, object]] = []
            for target_id in seen:
                if target_id == source_id:
                    continue
                target = current_rows.get(target_id)
                if not target or target.get("content_fingerprint") != source.get("content_fingerprint"):
                    continue
                target_added = parsed_time(target.get("date_added"))
                if target_added is None:
                    continue
                # The destination must be newer than the historical identity and
                # must have appeared no earlier than the source's previous scan.
                # This avoids collapsing ordinary long-standing duplicate copies.
                if target_added <= source_added:
                    continue
                if target_added < source_last_seen - timedelta(seconds=2):
                    continue
                candidates.append(target)

            if len(candidates) != 1:
                continue
            target_id = str(candidates[0]["id"])
            if self.db.reconcile_relocation(source_id, target_id):
                seen.discard(target_id)
                seen.add(source_id)
                retired_ids.add(target_id)
                relocation_reconciled += 1
                renamed += 1

        missing = 0
        for video_id, video in old.items():
            if video_id in seen or video_id in retired_ids:
                continue
            key = row_root_key(video)
            # A deliberately removed root makes its old rows missing. An
            # unreachable/partially scanned root does not, preventing a flaky NAS
            # from making thousands of titles disappear on a transient failure.
            if key is None or key not in configured_root_keys or key in root_success:
                self.db.exec("UPDATE videos SET status='missing' WHERE id=?", (video_id,))
                missing += 1

        duplicate_groups = self._refresh_duplicates()
        return {
            "seen": len(seen),
            "added": added,
            "changed": changed,
            "renamed": renamed,
            "relocation_reconciled": relocation_reconciled,
            "fingerprinted": fingerprinted,
            "duplicate_groups": duplicate_groups,
            "unchanged": unchanged,
            "stale_requeued": stale,
            "missing": missing,
            "partial": bool(errors),
            "errors": errors[:20],
            "roots": root_results,
            "roots_configured": len(roots),
            "roots_reachable": sum(1 for item in root_results if item.get("reachable")),
        }

