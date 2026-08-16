from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS videos(
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    rel_path TEXT NOT NULL,
    library_root TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    metadata_json TEXT,
    analysis_json TEXT,
    auto_hsp BLOB,
    auto_hsp_version INTEGER,
    manual_hsp BLOB,
    manual_hsp_version INTEGER,
    profile_mode TEXT NOT NULL DEFAULT 'auto',
    is_favorite INTEGER NOT NULL DEFAULT 0,
    rating REAL,
    manual_priority INTEGER NOT NULL DEFAULT 0,
    priority_requested_at TEXT,
    priority_rank INTEGER,
    thumb_path TEXT,
    layout_override_json TEXT,
    hsp_policy_override_json TEXT,
    sidecar_hsp_path TEXT,
    sidecar_hsp_revision TEXT,
    sidecar_hsp_managed INTEGER NOT NULL DEFAULT 0,
    content_fingerprint TEXT,
    fingerprint_version INTEGER NOT NULL DEFAULT 0,
    duplicate_group TEXT,
    watch_position_ms REAL NOT NULL DEFAULT 0,
    last_watched TEXT,
    play_count INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    date_added TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    last_analyzed TEXT
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_rel_path ON videos(rel_path);
CREATE INDEX IF NOT EXISTS idx_videos_fingerprint ON videos(content_fingerprint);
CREATE INDEX IF NOT EXISTS idx_videos_priority ON videos(manual_priority, priority_rank);
CREATE INDEX IF NOT EXISTS idx_videos_last_watched ON videos(last_watched);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT,
    event INTEGER,
    time_ms REAL,
    speed REAL,
    utc_ms REAL,
    username TEXT,
    connection_key TEXT,
    received_at TEXT,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_video_id ON events(video_id);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS analysis_runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    analysis_version TEXT,
    stage TEXT,
    planned_samples INTEGER NOT NULL DEFAULT 0,
    used_samples INTEGER NOT NULL DEFAULT 0,
    rejected_samples INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 0,
    error TEXT,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_video ON analysis_runs(video_id,id DESC);
CREATE TABLE IF NOT EXISTS maintenance_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    details_json TEXT
);
"""


class DB:
    """Small thread-safe SQLite wrapper used by scanner, worker and API."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _existing_schema_version(self) -> int | None:
        """Return the public database schema version, or None for a fresh database."""
        if not self.path.exists() or self.path.stat().st_size <= 0:
            return None
        try:
            conn = sqlite3.connect(self.path, timeout=5)
            try:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not tables:
                    return None
                if "settings" not in tables:
                    return 0
                row = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
                if row is None:
                    return 0
                return int(row[0])
            finally:
                conn.close()
        except (sqlite3.Error, TypeError, ValueError):
            return 0

    def _initialize(self) -> None:
        existing_version = self._existing_schema_version()
        if existing_version is not None and existing_version != SCHEMA_VERSION:
            raise RuntimeError(
                "This HereTune build found an unsupported database schema. "
                "Stop HereTune, move or delete the configured data directory, and start again."
            )
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT INTO settings(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            # A process interruption can leave the active row marked analyzing.
            # Requeue it on startup so one interrupted job never strands the queue.
            connection.execute("UPDATE videos SET status='queued' WHERE status='analyzing'")
            connection.commit()

    def exec(self, sql: str, parameters: Iterable[Any] = ()) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute(sql, tuple(parameters))
            connection.commit()

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._write_lock, self.connect() as connection:
            connection.executemany(sql, [tuple(row) for row in rows])
            connection.commit()

    def one(self, sql: str, parameters: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(sql, tuple(parameters)).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, parameters: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(sql, tuple(parameters)).fetchall()
        return [dict(row) for row in rows]


    def iter_all(self, sql: str, parameters: Iterable[Any] = ()):
        """Stream result rows instead of fetchall() for memory-sensitive scans.

        Analysis JSON and HSP BLOBs make a whole-table SELECT *
        surprisingly expensive. Callers that only need a few columns should
        project them explicitly and iterate one row at a time.
        """
        with self.connect() as connection:
            cursor = connection.execute(sql, tuple(parameters))
            for row in cursor:
                yield dict(row)

    @staticmethod
    def inflate_selected(row: dict[str, Any] | None) -> dict[str, Any] | None:
        """Inflate JSON columns that are actually present in a projected row."""
        if not row:
            return None
        value = dict(row)
        for source, destination in (
            ("metadata_json", "metadata"),
            ("analysis_json", "analysis"),
            ("layout_override_json", "layout_override"),
            ("hsp_policy_override_json", "hsp_policy_override"),
        ):
            if source not in value:
                continue
            try:
                value[destination] = json.loads(value.get(source) or "{}")
            except (TypeError, json.JSONDecodeError):
                value[destination] = {}
        return value

    def scan_rows(self) -> list[dict[str, Any]]:
        """Rows needed by the library scanner, excluding HSP BLOBs."""
        rows = self.all(
            """SELECT id,path,rel_path,library_root,title,size,mtime_ns,status,analysis_json,
                      content_fingerprint,fingerprint_version,date_added,last_seen
               FROM videos ORDER BY rel_path COLLATE NOCASE"""
        )
        return [self.inflate_selected(row) for row in rows if row]

    def reconcile_relocation(self, canonical_id: str, transient_id: str) -> bool:
        """Collapse a transient duplicate row back into the historical identity.

        This is used when a large cross-volume move is observed mid-copy: one scan
        can briefly see both paths and create a duplicate row, then the next scan
        sees the original disappear. The historical row remains canonical so its
        analysis/HSP/library state survives; any events or run history attached to
        the short-lived row follow it.
        """
        if not canonical_id or not transient_id or canonical_id == transient_id:
            return False
        with self._write_lock, self.connect() as connection:
            source = connection.execute("SELECT * FROM videos WHERE id=?", (canonical_id,)).fetchone()
            target = connection.execute("SELECT * FROM videos WHERE id=?", (transient_id,)).fetchone()
            if source is None or target is None:
                return False
            source = dict(source)
            target = dict(target)
            if not source.get("content_fingerprint") or source.get("content_fingerprint") != target.get("content_fingerprint"):
                return False

            # Prefer the historical record's expensive/user-owned state. If the
            # transient row managed to accumulate useful state before reconciliation,
            # keep it only where the historical value is absent or clearly older.
            source_last_watched = str(source.get("last_watched") or "")
            target_last_watched = str(target.get("last_watched") or "")
            target_watch_is_newer = bool(target_last_watched and target_last_watched > source_last_watched)
            watch_position = target.get("watch_position_ms") if target_watch_is_newer else source.get("watch_position_ms")
            last_watched = target.get("last_watched") if target_watch_is_newer else source.get("last_watched")
            completed = target.get("completed") if target_watch_is_newer else source.get("completed")

            status = str(source.get("status") or "queued")
            if status == "missing":
                status = "ready" if source.get("analysis_json") and source.get("auto_hsp") else str(target.get("status") or "queued")
                if status == "missing":
                    status = "queued"

            merged = {
                "path": target.get("path"),
                "rel_path": target.get("rel_path"),
                "library_root": target.get("library_root"),
                "title": target.get("title"),
                "size": target.get("size"),
                "mtime_ns": target.get("mtime_ns"),
                "status": status,
                "error": source.get("error") if status == "error" else None,
                "metadata_json": source.get("metadata_json") or target.get("metadata_json"),
                "analysis_json": source.get("analysis_json") or target.get("analysis_json"),
                "auto_hsp": source.get("auto_hsp") or target.get("auto_hsp"),
                "auto_hsp_version": source.get("auto_hsp_version") or target.get("auto_hsp_version"),
                "manual_hsp": source.get("manual_hsp") or target.get("manual_hsp"),
                "manual_hsp_version": source.get("manual_hsp_version") or target.get("manual_hsp_version"),
                "profile_mode": source.get("profile_mode") or target.get("profile_mode") or "auto",
                "is_favorite": 1 if source.get("is_favorite") or target.get("is_favorite") else 0,
                "rating": source.get("rating") if source.get("rating") is not None else target.get("rating"),
                "manual_priority": 1 if source.get("manual_priority") or target.get("manual_priority") else 0,
                "priority_requested_at": source.get("priority_requested_at") or target.get("priority_requested_at"),
                "priority_rank": source.get("priority_rank") if source.get("priority_rank") is not None else target.get("priority_rank"),
                "thumb_path": source.get("thumb_path") or target.get("thumb_path"),
                "layout_override_json": source.get("layout_override_json") or target.get("layout_override_json"),
                "hsp_policy_override_json": source.get("hsp_policy_override_json") or target.get("hsp_policy_override_json"),
                # A move changes the sidecar path. Keep ownership only if the transient
                # destination already carries a managed sidecar record.
                "sidecar_hsp_path": target.get("sidecar_hsp_path"),
                "sidecar_hsp_revision": target.get("sidecar_hsp_revision"),
                "sidecar_hsp_managed": int(target.get("sidecar_hsp_managed") or 0),
                "content_fingerprint": target.get("content_fingerprint"),
                "fingerprint_version": max(int(source.get("fingerprint_version") or 0), int(target.get("fingerprint_version") or 0)),
                "watch_position_ms": watch_position or 0.0,
                "last_watched": last_watched,
                "play_count": int(source.get("play_count") or 0) + int(target.get("play_count") or 0),
                "completed": int(completed or 0),
                "date_added": min(str(source.get("date_added") or target.get("date_added") or ""), str(target.get("date_added") or source.get("date_added") or "")),
                "last_seen": target.get("last_seen") or source.get("last_seen"),
                "last_analyzed": source.get("last_analyzed") or target.get("last_analyzed"),
            }

            connection.execute("UPDATE events SET video_id=? WHERE video_id=?", (canonical_id, transient_id))
            connection.execute("UPDATE analysis_runs SET video_id=? WHERE video_id=?", (canonical_id, transient_id))
            # Delete the transient row first because videos.path is UNIQUE.
            connection.execute("DELETE FROM videos WHERE id=?", (transient_id,))
            connection.execute(
                """UPDATE videos SET path=:path,rel_path=:rel_path,library_root=:library_root,title=:title,
                   size=:size,mtime_ns=:mtime_ns,status=:status,error=:error,metadata_json=:metadata_json,
                   analysis_json=:analysis_json,auto_hsp=:auto_hsp,auto_hsp_version=:auto_hsp_version,
                   manual_hsp=:manual_hsp,manual_hsp_version=:manual_hsp_version,profile_mode=:profile_mode,
                   is_favorite=:is_favorite,rating=:rating,manual_priority=:manual_priority,
                   priority_requested_at=:priority_requested_at,priority_rank=:priority_rank,thumb_path=:thumb_path,
                   layout_override_json=:layout_override_json,hsp_policy_override_json=:hsp_policy_override_json,
                   sidecar_hsp_path=:sidecar_hsp_path,sidecar_hsp_revision=:sidecar_hsp_revision,
                   sidecar_hsp_managed=:sidecar_hsp_managed,content_fingerprint=:content_fingerprint,
                   fingerprint_version=:fingerprint_version,duplicate_group=NULL,watch_position_ms=:watch_position_ms,
                   last_watched=:last_watched,play_count=:play_count,completed=:completed,date_added=:date_added,
                   last_seen=:last_seen,last_analyzed=:last_analyzed WHERE id=:canonical_id""",
                {**merged, "canonical_id": canonical_id},
            )
            connection.commit()
        return True

    def lightweight_rows(self, columns: str, where: str = "", parameters: Iterable[Any] = ()) -> list[dict[str, Any]]:
        """Projected video rows for endpoints that do not need HSP BLOB bytes."""
        sql = f"SELECT {columns} FROM videos" + (f" WHERE {where}" if where else "")
        rows = self.all(sql, parameters)
        return [self.inflate_selected(row) for row in rows if row]

    def iter_video_rows(self, columns: str, where: str = "", parameters: Iterable[Any] = ()):
        """Stream projected video rows and inflate only selected JSON columns."""
        sql = f"SELECT {columns} FROM videos" + (f" WHERE {where}" if where else "")
        for row in self.iter_all(sql, parameters):
            value = self.inflate_selected(row)
            if value is not None:
                yield value
    @staticmethod
    def inflate(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        value = dict(row)
        for source, destination in (
            ("metadata_json", "metadata"),
            ("analysis_json", "analysis"),
            ("layout_override_json", "layout_override"),
            ("hsp_policy_override_json", "hsp_policy_override"),
        ):
            try:
                value[destination] = json.loads(value.get(source) or "{}")
            except (TypeError, json.JSONDecodeError):
                value[destination] = {}
        return value

    def get(self, video_id: str) -> dict[str, Any] | None:
        return self.inflate(self.one("SELECT * FROM videos WHERE id=?", (video_id,)))

    def videos(self, include_missing: bool = False) -> list[dict[str, Any]]:
        where = "" if include_missing else " WHERE status!='missing'"
        rows = self.all("SELECT * FROM videos" + where + " ORDER BY rel_path COLLATE NOCASE")
        return [self.inflate(row) for row in rows if row]

    def queued(self) -> dict[str, Any] | None:
        return self.inflate(self.one(
            """SELECT * FROM videos
               WHERE status IN ('queued','changed')
               ORDER BY manual_priority DESC,
                        CASE WHEN manual_priority=1 THEN COALESCE(priority_rank, 2147483647) END ASC,
                        CASE WHEN manual_priority=1 THEN priority_requested_at END ASC,
                        CASE status WHEN 'changed' THEN 0 ELSE 1 END,
                        rel_path COLLATE NOCASE
               LIMIT 1"""
        ))

    def needs_metadata(self, limit: int = 4) -> list[dict[str, Any]]:
        rows = self.all(
            """SELECT * FROM videos
               WHERE status IN ('queued','changed') AND metadata_json IS NULL
               ORDER BY manual_priority DESC,
                        CASE WHEN manual_priority=1 THEN COALESCE(priority_rank, 2147483647) END ASC,
                        CASE WHEN manual_priority=1 THEN priority_requested_at END ASC,
                        CASE status WHEN 'changed' THEN 0 ELSE 1 END,
                        rel_path COLLATE NOCASE
               LIMIT ?""",
            (max(1, int(limit)),),
        )
        return [self.inflate(row) for row in rows if row]

    def next_priority_rank(self) -> int:
        row = self.one("SELECT COALESCE(MAX(priority_rank),0)+1 AS n FROM videos WHERE manual_priority=1")
        return int((row or {}).get("n") or 1)

    def priority_queue(self) -> list[dict[str, Any]]:
        rows = self.all(
            """SELECT * FROM videos WHERE manual_priority=1 AND status IN ('queued','changed','error')
               ORDER BY COALESCE(priority_rank,2147483647), priority_requested_at, rel_path COLLATE NOCASE"""
        )
        return [self.inflate(row) for row in rows if row]

    def set_setting(self, key: str, value: str) -> None:
        self.exec(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_setting(self, key: str) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return str(row["value"]) if row and row.get("value") is not None else None

    def start_analysis_run(self, video_id: str, analysis_version: str = "") -> int:
        with self._write_lock, self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO analysis_runs(video_id,started_at,status,analysis_version,stage) VALUES(?,?,?,?,?)",
                (video_id, datetime.now(timezone.utc).isoformat(), "running", analysis_version, "starting"),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def update_analysis_run(self, run_id: int, *, stage: str | None = None, planned: int | None = None, details: dict[str, Any] | None = None) -> None:
        updates: list[str] = []
        params: list[Any] = []
        if stage is not None:
            updates.append("stage=?"); params.append(stage)
        if planned is not None:
            updates.append("planned_samples=?"); params.append(int(planned))
        if details is not None:
            updates.append("details_json=?"); params.append(json.dumps(details))
        if not updates:
            return
        params.append(int(run_id))
        self.exec(f"UPDATE analysis_runs SET {', '.join(updates)} WHERE id=?", params)

    def finish_analysis_run(self, run_id: int, *, status: str, stage: str, used: int = 0, rejected: int = 0, duration: float = 0.0, error: str | None = None, details: dict[str, Any] | None = None) -> None:
        self.exec(
            """UPDATE analysis_runs SET finished_at=?,status=?,stage=?,used_samples=?,rejected_samples=?,duration_seconds=?,error=?,details_json=? WHERE id=?""",
            (datetime.now(timezone.utc).isoformat(), status, stage, int(used), int(rejected), float(duration), error, json.dumps(details or {}), int(run_id)),
        )

    def analysis_history(self, video_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.all(
            "SELECT id,started_at,finished_at,status,analysis_version,stage,planned_samples,used_samples,rejected_samples,duration_seconds,error,details_json FROM analysis_runs WHERE video_id=? ORDER BY id DESC LIMIT ?",
            (video_id, max(1, min(100, int(limit)))),
        )
        for row in rows:
            try:
                row["details"] = json.loads(row.get("details_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                row["details"] = {}
            row.pop("details_json", None)
        return rows

    def integrity_check(self) -> list[str]:
        with self.connect() as connection:
            return [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]

    def vacuum(self) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute("VACUUM")

    def backup_to(self, destination: str | Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            source = sqlite3.connect(self.path, timeout=30)
            dest = sqlite3.connect(destination)
            try:
                source.backup(dest)
            finally:
                dest.close(); source.close()
        return destination

    def database_sizes(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for suffix, name in (("", "database"), ("-wal", "wal"), ("-shm", "shm")):
            path = Path(str(self.path) + suffix)
            try:
                result[name] = path.stat().st_size
            except OSError:
                result[name] = 0
        return result

    def stats(self) -> dict[str, Any]:
        status_counts = {
            row["status"]: row["n"]
            for row in self.all(
                "SELECT status, COUNT(*) AS n FROM videos WHERE status!='missing' GROUP BY status"
            )
        }
        missing = self.one("SELECT COUNT(*) AS n FROM videos WHERE status='missing'")
        manual = self.one("SELECT COUNT(*) AS n FROM videos WHERE status!='missing' AND manual_hsp IS NOT NULL")
        favorite = self.one("SELECT COUNT(*) AS n FROM videos WHERE status!='missing' AND is_favorite=1")
        rated = self.one(
            "SELECT COUNT(*) AS n, AVG(rating) AS avg_rating FROM videos "
            "WHERE status!='missing' AND rating IS NOT NULL"
        )
        duplicates = self.one(
            "SELECT COUNT(*) AS n FROM videos WHERE status!='missing' AND duplicate_group IS NOT NULL"
        )
        return {
            "total": sum(status_counts.values()),
            "by_status": status_counts,
            "missing": int((missing or {}).get("n") or 0),
            "manual_hsp": int((manual or {}).get("n") or 0),
            "favorites": int((favorite or {}).get("n") or 0),
            "rated": int((rated or {}).get("n") or 0),
            "average_rating": float((rated or {}).get("avg_rating") or 0.0),
            "duplicates": int((duplicates or {}).get("n") or 0),
        }
