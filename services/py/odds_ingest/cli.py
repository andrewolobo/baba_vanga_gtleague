"""Odds ingester CLI.

  python -m odds_ingest.cli fetch                      # live slate -> DB
  python -m odds_ingest.cli fetch --from-file X.bin    # offline decode -> DB
  python -m odds_ingest.cli unmatched                  # alias-table worklist

Every fetch upserts `fixtures` and APPENDS to `odds_snapshots` (full odds
history). Raw pages are archived under data/raw/odds/. Exit code 3 = FeedError.
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from store.db import connect
from store.repo import FixtureRepo, OddsRepo

from . import fetch, normalize

PAGE_TAKE = 100
MAX_PAGES = 20


def _known_players(conn) -> set[str]:
    known = {r["home_player"] for r in conn.execute("SELECT DISTINCT home_player FROM matches")}
    known |= {r["away_player"] for r in conn.execute("SELECT DISTINCT away_player FROM matches")}
    known |= {r["player"] for r in conn.execute("SELECT player FROM player_aliases")}
    return known


def _aliased(conn, name: str | None) -> str | None:
    if name is None:
        return None
    row = conn.execute(
        "SELECT player FROM player_aliases WHERE book_name = ?", (name,)
    ).fetchone()
    return row["player"] if row else name


def cmd_fetch(args) -> int:
    conn = connect(args.db)
    now = datetime.now(timezone.utc)

    pages: list[bytes] = []
    if args.from_file:
        pages.append(Path(args.from_file).read_bytes())
    else:
        with fetch.client() as c:
            for page in range(MAX_PAGES):
                raw = fetch.fetch_page(c, skip=page * PAGE_TAKE, take=PAGE_TAKE)
                if len(raw) < 8:
                    break
                pages.append(raw)
                if len(normalize.decode_events(raw)) < PAGE_TAKE:
                    break
        if not args.no_archive:
            arch = Path("data/raw/odds")
            arch.mkdir(parents=True, exist_ok=True)
            for i, raw in enumerate(pages):
                (arch / f"{now:%Y%m%dT%H%M%S}_p{i}.bin").write_bytes(raw)

    events: dict[str, normalize.NormalizedEvent] = {}
    for raw in pages:
        for ev in normalize.decode_events(raw):  # pages can overlap; last wins
            events[ev.fixture.event_id] = ev

    known = _known_players(conn)
    unmatched: set[str] = set()
    fixtures, prices = [], []
    for ev in events.values():
        f = ev.fixture
        hp, ap = _aliased(conn, f.home_player), _aliased(conn, f.away_player)
        hit = sum(1 for p in (hp, ap) if p in known)
        f = f.model_copy(update={
            "home_player": hp, "away_player": ap,
            "coverage": {2: "full", 1: "partial", 0: "none"}[hit],
        })
        unmatched |= {p for p in (hp, ap) if p and p not in known}
        fixtures.append(f)
        prices.extend(ev.prices)

    FixtureRepo(conn).upsert_many(fixtures, seen_at=now)
    n = OddsRepo(conn).append_many(prices, fetched_at=now)

    print(f"events={len(fixtures)} odds_rows={n} "
          f"coverage_full={sum(1 for f in fixtures if f.coverage == 'full')}"
          f"/{len(fixtures)}")
    for f in sorted(fixtures, key=lambda x: x.start_time_utc):
        ou = sorted({p.line for p in events[f.event_id].prices
                     if p.market == "ou" and p.line is not None})
        print(f"  {f.start_time_utc:%m-%d %H:%M}Z {f.home_raw} v {f.away_raw} "
              f"[{f.coverage}] ou_lines={ou}")
    if unmatched:
        print(f"unmatched book names (alias worklist): {sorted(unmatched)}",
              file=sys.stderr)
    return 0


def cmd_unmatched(args) -> int:
    conn = connect(args.db)
    known = _known_players(conn)
    rows = conn.execute(
        "SELECT DISTINCT home_player p FROM fixtures"
        " UNION SELECT DISTINCT away_player FROM fixtures"
    ).fetchall()
    missing = sorted({r["p"] for r in rows if r["p"] and r["p"] not in known})
    print("\n".join(missing) if missing else "all fixture players matched")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="odds_ingest")
    ap.add_argument("--db", default=None, help="override GTL_DB_PATH")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="fetch (or decode) a slate into the DB")
    f.add_argument("--from-file", default=None, metavar="X.bin",
                   help="decode a saved raw page instead of the network")
    f.add_argument("--no-archive", action="store_true")
    f.set_defaults(fn=cmd_fetch)

    u = sub.add_parser("unmatched", help="fixture players missing from matches/aliases")
    u.set_defaults(fn=cmd_unmatched)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except fetch.FeedError as e:
        print(f"FEED ERROR: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
