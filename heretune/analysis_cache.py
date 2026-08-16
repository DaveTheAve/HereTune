from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

CACHE_VERSION = 2


def _json_safe(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _restore_stat(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    for key in ("channel_means", "shadow_channel_means", "midtone_channel_means", "highlight_channel_means"):
        if key in result and isinstance(result[key], list):
            result[key] = np.asarray(result[key], dtype=np.float64)
    if "signature" in result and isinstance(result["signature"], list):
        result["signature"] = np.asarray(result["signature"], dtype=np.float32)
    elif "signature" not in result:
        result["signature"] = np.asarray([], dtype=np.float32)
    return result


class AnalysisCache:
    """Persistent per-video analysis-statistics cache with bounded LRU storage.

    Only compact analysis statistics are persisted. Inspector/rejected-sample
    imagery is decoded from the source video on demand and is never stored in
    the analysis cache.
    """

    def __init__(
        self,
        root: str | Path,
        video_key: str,
        *,
        enabled: bool = True,
        max_gb: float = 20.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.root = Path(root)
        self.max_bytes = max(256 * 1024 * 1024, int(float(max_gb) * 1024**3))
        digest = hashlib.sha1(video_key.encode("utf-8", errors="replace")).hexdigest()
        self.bucket = self.root / digest[:2] / digest
        self.manifest_path = self.bucket / "manifest.json"
        self._lock = threading.RLock()
        self._dirty = 0
        self._manifest: dict[str, Any] = {
            "version": CACHE_VERSION,
            "video_key": video_key,
            "updated_at": time.time(),
            "stats": {"probe": {}, "refine": {}, "full": {}},
        }
        if not self.enabled:
            return
        self.bucket.mkdir(parents=True, exist_ok=True)
        try:
            existing = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if existing.get("version") == CACHE_VERSION and existing.get("video_key") == video_key:
                self._manifest = existing
        except Exception:
            pass
        try:
            os.utime(self.bucket, None)
        except OSError:
            pass

    @staticmethod
    def key(timestamp: float, width: int) -> str:
        return f"{int(width)}:{float(timestamp):.3f}"

    def get_stat(self, kind: str, timestamp: float, width: int) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        with self._lock:
            item = (((self._manifest.get("stats") or {}).get(kind) or {}).get(self.key(timestamp, width)))
            if not isinstance(item, dict):
                return None
            try:
                os.utime(self.bucket, None)
            except OSError:
                pass
            return _restore_stat(item)

    def put_stat(self, kind: str, timestamp: float, width: int, stat: dict[str, Any]) -> None:
        if not self.enabled:
            return
        with self._lock:
            stats = self._manifest.setdefault("stats", {}).setdefault(kind, {})
            stats[self.key(timestamp, width)] = _json_safe(stat)
            self._manifest["updated_at"] = time.time()
            self._dirty += 1
            if self._dirty >= 5:
                self.flush()

    def flush(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if not self._dirty and self.manifest_path.exists():
                return
            self.bucket.mkdir(parents=True, exist_ok=True)
            temp = self.manifest_path.with_suffix(".tmp")
            temp.write_text(json.dumps(self._manifest, separators=(",", ":")), encoding="utf-8")
            temp.replace(self.manifest_path)
            self._dirty = 0

    def close(self) -> None:
        self.flush()

    @classmethod
    def prune(cls, root: str | Path, max_gb: float) -> dict[str, int]:
        root = Path(root)
        limit = max(256 * 1024 * 1024, int(float(max_gb) * 1024**3))
        if not root.exists():
            return {"bytes_before": 0, "bytes_after": 0, "removed_buckets": 0}
        buckets: list[tuple[float, Path, int]] = []
        total = 0
        for manifest in root.glob("*/*/manifest.json"):
            bucket = manifest.parent
            try:
                size = sum(p.stat().st_size for p in bucket.rglob("*") if p.is_file())
                mtime = bucket.stat().st_mtime
            except OSError:
                continue
            total += size
            buckets.append((mtime, bucket, size))
        before = total
        removed = 0
        for _mtime, bucket, size in sorted(buckets):
            if total <= limit:
                break
            try:
                import shutil
                shutil.rmtree(bucket, ignore_errors=True)
                total -= size
                removed += 1
            except OSError:
                pass
        return {"bytes_before": before, "bytes_after": max(0, total), "removed_buckets": removed}

    @classmethod
    def size_bytes(cls, root: str | Path) -> int:
        root = Path(root)
        if not root.exists():
            return 0
        total = 0
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                pass
        return total
