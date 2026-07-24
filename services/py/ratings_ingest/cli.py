"""sofifa team-ratings ingester CLI (docs/TEAM_RATINGS.md).

  python -m ratings_ingest.cli refresh
  python -m ratings_ingest.cli refresh --min-overall 72 --max-pages 20 --dry-run

Reference data, run BY HAND (never the predictor timer). Crawls /teams in
Overall-descending order — the default order is ~team-id and useless for a
bounded crawl — pinning FC25 (r=250044), until the Overall floor is crossed.
Idempotent upsert; raw HTML archived per page for offline re-parse.

Two reports print every run:
  * coverage_report — ingest data-quality (rows, editions, missing fields);
  * a known-club audit — which canonical matches.*_club names the scrape did
    NOT match by loose name. That unmatched list is the raw material for the
    team_rating_aliases seed (step 4): every entry is either a spelling that
    needs an alias, or a club sofifa ranks below the floor.
"""

import argparse
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

from store.aliases import known_clubs
from store.db import connect
from store.repo import TeamRatingRepo

from core.config import settings

from . import fetch, parse


def _norm(s: str) -> str:
    """Loose comparison key for the audit ONLY (accents/case/punctuation
    folded). Deliberately lossy — step 4's resolver is the real join; here we
    just need to see which known clubs clearly did not land."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _partition(rows: list, min_overall: int) -> tuple[list, bool]:
    """Split a page: rows at/above the floor are kept; `crossed` is True once a
    row falls below it. Because the crawl is Overall-descending, the first
    sub-floor row means every later team is sub-floor too — the stop signal."""
    kept, crossed = [], False
    for r in rows:
        if r.overall is not None and r.overall < min_overall:
            crossed = True
        else:
            kept.append(r)
    return kept, crossed


def crawl(session, min_overall: int, max_pages: int,
          archive_dir: Path | None) -> list:
    kept: list = []
    offset = 0
    for _ in range(max_pages):
        html = fetch.fetch_page(session, offset=offset)
        if archive_dir:
            archive_dir.mkdir(parents=True, exist_ok=True)
            (archive_dir / f"offset_{offset:04d}.html").write_text(
                html, encoding="utf-8")
        rows = parse.parse_teams(html)
        page_kept, crossed = _partition(rows, min_overall)
        kept.extend(page_kept)
        if crossed or len(rows) < fetch.PAGE_SIZE:
            break
        offset += fetch.PAGE_SIZE
        fetch.polite_sleep()
    else:
        print(f"WARNING: hit max_pages={max_pages} before the Overall floor "
              f"({min_overall}) — raise --max-pages or the scrape is truncated",
              file=sys.stderr)
    return kept


def audit_known(conn, rows: list) -> tuple[set, list]:
    """(matched, unmatched) canonical clubs vs the scraped names, loose-normed.
    Unmatched = the step-4 alias worklist."""
    known = known_clubs(conn)
    scraped = {_norm(r.sofifa_name) for r in rows}
    matched = {c for c in known if _norm(c) in scraped}
    return matched, sorted(known - matched)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ratings_ingest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rf = sub.add_parser("refresh", help="crawl sofifa /teams and upsert ratings")
    rf.add_argument("--db", default=None, help="override GTL_DB_PATH")
    rf.add_argument("--min-overall", type=int, default=None,
                    help="Overall floor (default settings.sofifa_min_overall)")
    rf.add_argument("--max-pages", type=int, default=None)
    rf.add_argument("--no-archive", action="store_true", help="skip raw HTML archive")
    rf.add_argument("--dry-run", action="store_true",
                    help="crawl + report, no DB write")
    args = ap.parse_args(argv)

    s = settings()
    min_overall = s.sofifa_min_overall if args.min_overall is None else args.min_overall
    max_pages = s.sofifa_max_pages if args.max_pages is None else args.max_pages
    archive = (None if args.no_archive
               else Path("data/raw/team_ratings") / date.today().isoformat())

    with fetch.client() as session:
        rows = crawl(session, min_overall, max_pages, archive)

    print(f"crawled {len(rows)} teams (overall >= {min_overall}, r={s.sofifa_edition})")
    print(f"coverage: {parse.coverage_report(rows)}")

    conn = connect(args.db)
    matched, unmatched = audit_known(conn, rows)
    total = len(matched) + len(unmatched)
    print(f"known-club audit: {len(matched)}/{total} canonical clubs matched by loose name")
    if unmatched:
        print(f"  unmatched ({len(unmatched)}) -> team_rating_aliases worklist (step 4):")
        for c in unmatched:
            print(f"    - {c!r}")

    if args.dry_run:
        print("dry-run: no DB write")
        return 0
    rep = TeamRatingRepo(conn).upsert_many(rows)
    print(f"upsert: {rep}")
    print(f"team_ratings rows now: {TeamRatingRepo(conn).count()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
