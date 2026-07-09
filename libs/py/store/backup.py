"""Nightly ops (plan Phase 8): checkpointed DB backup + retention pruning.

  python -m store.backup

- WAL-checkpoints, then copies the DB to data/backups/<name>_YYYYMMDD.db
  (same-day re-runs overwrite; keeps the last BACKUP_KEEP).
- Prunes model artifacts older than ARTIFACT_KEEP_DAYS (a fresh one is
  refitted on demand) and raw odds pages older than RAW_ODDS_KEEP_DAYS.
  Raw results JSON is kept indefinitely (small, and it is the reprocessing
  source of record).
"""

import argparse
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from core.config import settings
from store.db import connect

BACKUP_KEEP = 7
ARTIFACT_KEEP_DAYS = 7
RAW_ODDS_KEEP_DAYS = 14


def _prune_by_age(dir_: Path, days: float) -> int:
    if not dir_.is_dir():
        return 0
    cutoff = time.time() - days * 86_400
    n = 0
    for f in dir_.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            n += 1
    return n


def run(db_path: Path) -> dict:
    conn = connect(db_path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    backups = db_path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    dest = backups / f"{db_path.stem}_{stamp}.db"
    shutil.copy2(db_path, dest)

    old = sorted(backups.glob(f"{db_path.stem}_*.db"))[:-BACKUP_KEEP]
    for f in old:
        f.unlink()

    return {
        "backup": str(dest),
        "backup_bytes": dest.stat().st_size,
        "backups_pruned": len(old),
        "artifacts_pruned": _prune_by_age(db_path.parent / "artifacts",
                                          ARTIFACT_KEEP_DAYS),
        "raw_odds_pruned": _prune_by_age(db_path.parent / "raw" / "odds",
                                         RAW_ODDS_KEEP_DAYS),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="store.backup")
    ap.add_argument("--db", default=None, help="override GTL_DB_PATH")
    args = ap.parse_args(argv)
    rep = run(Path(args.db) if args.db else settings().gtl_db_path)
    print(" ".join(f"{k}={v}" for k, v in rep.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
