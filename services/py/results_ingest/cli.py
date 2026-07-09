"""Results ingester CLI.

  python -m results_ingest.cli backfill --from 2026-06-08 --to 2026-07-07
  python -m results_ingest.cli today

Both are idempotent merges: re-runs update changed rows and insert new ones;
the DB UNIQUE constraints make repeats safe. Raw responses are archived per
day under data/raw/results/ for reprocessing.
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from store.db import connect
from store.repo import MatchRepo

from . import fetch, parse


def _ingest_day(c, repo: MatchRepo, day: str, archive_dir: Path | None,
                statuses: str = "3,4") -> dict:
    start, end = fetch.day_window(day)
    raw = fetch.fetch_window(c, start, end, statuses=statuses)
    if archive_dir:
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / f"{day}.json").write_text(
            json.dumps(raw, separators=(",", ":")), encoding="utf-8"
        )
    rows = parse.parse_fixtures(raw)
    rep = repo.upsert_many(rows)
    q = parse.quality_report(rows)
    print(f"{day}: {rep} | {q}")
    return q


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, help="override GTL_DB_PATH")
    common.add_argument("--no-archive", action="store_true",
                        help="skip raw JSON archive")

    ap = argparse.ArgumentParser(prog="results_ingest", parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)

    bf = sub.add_parser("backfill", parents=[common],
                        help="ingest a date range (inclusive)")
    bf.add_argument("--from", dest="date_from", required=True, metavar="YYYY-MM-DD")
    bf.add_argument("--to", dest="date_to", required=True, metavar="YYYY-MM-DD")

    sub.add_parser("today", parents=[common],
                   help="incremental merge of today's UTC fixtures")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    repo = MatchRepo(conn)
    archive = None if args.no_archive else Path("data/raw/results")

    if args.cmd == "today":
        # today + tomorrow, including scheduled (status 0) fixtures: the UI
        # predicts the published schedule before betPawa prices it
        today = datetime.now(timezone.utc).date()
        days = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
    else:
        d0, d1 = date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
        if d0 > d1:
            ap.error("--from is after --to")
        days = [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]

    statuses = "0,3,4" if args.cmd == "today" else "3,4"
    anomalies = 0
    with fetch.client() as c:
        for day in days:
            q = _ingest_day(c, repo, day, archive, statuses=statuses)
            anomalies += q["finished_unscored"] + q["in_batch_dupes"]

    n = repo.count()
    print(f"db total matches: {n} (finished: {repo.count(status=3)})")
    if anomalies:
        print(f"WARNING: {anomalies} quality anomalies (see per-day lines)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
