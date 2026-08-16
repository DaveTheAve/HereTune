from __future__ import annotations

import argparse
import json
import sys
import traceback

from .accel import resource_worker_plan
from .config import load_config, scheduler_library_root
from .db import DB
from .worker import process

PREFIX = "HERETUNE_PROGRESS\t"


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", required=True)
    parser.add_argument("--video-id", required=True)
    args = parser.parse_args()
    cfg = load_config(args.config)
    db = DB(cfg.db_path)
    row = db.get(args.video_id)
    if not row:
        print(f"[HereTune Worker] Video not found: {args.video_id}", file=sys.stderr, flush=True)
        return 3

    def progress(payload):
        print(PREFIX + json.dumps(payload, separators=(",", ":"), ensure_ascii=False), flush=True)

    def plan_provider():
        return resource_worker_plan(cfg.analysis, scheduler_library_root(cfg), max_age=1.0)

    try:
        process(db, cfg, row, progress, plan_provider)
        final = db.one("SELECT status FROM videos WHERE id=?", (args.video_id,)) or {}
        return 0 if str(final.get("status") or "") in {"ready", "skipped", "error"} else 4
    except BaseException:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
