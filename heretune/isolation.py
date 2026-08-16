from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .processes import release_process, start_process, terminate_process

PREFIX = "HERETUNE_PROGRESS\t"


def run_isolated_process(db, cfg, row, progress=None) -> None:
    """Run one heavy analysis in a replaceable child Python process.

    The API/server process owns queue orchestration and remains available if a
    native decoder/OpenCV dependency crashes the analysis process.
    """
    if not getattr(cfg, "config_path", None):
        from .worker import process
        return process(db, cfg, row, progress)

    command = [
        sys.executable, "-m", "heretune.worker_subprocess",
        "--config", str(Path(cfg.config_path)),
        "--video-id", str(row["id"]),
    ]
    proc = start_process(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    tail: list[str] = []
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line.startswith(PREFIX):
                try:
                    payload = json.loads(line[len(PREFIX):])
                    if progress:
                        progress(payload)
                except Exception:
                    pass
                continue
            if line:
                print(line, flush=True)
                tail.append(line)
                if len(tail) > 30:
                    tail.pop(0)
        returncode = proc.wait()
    finally:
        if proc.poll() is None:
            terminate_process(proc)
        release_process(proc)

    if returncode != 0:
        current = db.one("SELECT status,error FROM videos WHERE id=?", (str(row["id"]),)) or {}
        if str(current.get("status") or "") == "analyzing":
            reason = f"Isolated analysis worker exited unexpectedly with code {returncode}."
            if tail:
                reason += " Last output: " + " | ".join(tail[-3:])
            db.exec("UPDATE videos SET status='error', error=? WHERE id=?", (reason[:4000], str(row["id"])))
        print(f"[HereTune Analysis] WORKER EXIT {row.get('title') or row['id']} · code {returncode}", file=sys.stderr, flush=True)
