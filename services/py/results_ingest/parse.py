"""Pure JSON→MatchRow normalization. No I/O — offline-testable against
scraper/fixtures/gtleagues_results_*.json.
"""

import hashlib
import json
import re
from datetime import datetime, timezone

from core.config import STATUS_FINISHED
from core.schema import MatchRow

_SEASON_DATE = re.compile(r"\s*\(\d{2}-\d{2}-\d{4}\)\s*$")


class ParseError(ValueError):
    """A fixture row missing fields the pipeline cannot proceed without."""


def parse_fixtures(raw: list[dict], scraped_at: datetime | None = None) -> list[MatchRow]:
    ts = scraped_at or datetime.now(timezone.utc)
    return [_row(r, ts) for r in raw]


def _row(r: dict, scraped_at: datetime) -> MatchRow:
    try:
        kickoff = datetime.fromisoformat(r["kickoff"].replace("Z", "+00:00"))
        sides = {}
        for p in r["participants"]:
            part = p["participant"]
            sides[p["side"]] = (
                part["player"]["nickname"].strip(),
                (part.get("team") or {}).get("name", "").strip(),
            )
        home, away = sides["home"], sides["away"]
    except (KeyError, TypeError, ValueError) as e:
        raise ParseError(f"fixture {r.get('id')!r}: {e!r}") from e
    if not home[0] or not away[0]:
        raise ParseError(f"fixture {r.get('id')!r}: blank player nickname")

    status = int(r["status"])
    stats = (r.get("result") or {}).get("stats") or {}
    home_ft, away_ft = stats.get("home_score"), stats.get("away_score")
    # A result exists only when finished AND scored — result rows are
    # pre-created with null scores on scheduled fixtures (PHASE0_PROBES §0.4).
    if status != STATUS_FINISHED:
        home_ft = away_ft = None

    season = _SEASON_DATE.sub("", r["season"]["name"])
    competition = f"{season} / {r['season']['tournament']['name']}"

    digest_src = json.dumps(
        [r["id"], r["kickoff"], status, home_ft, away_ft, home, away],
        separators=(",", ":"),
    )
    return MatchRow(
        source_match_id=str(r["id"]),
        date=kickoff.date().isoformat(),
        kickoff_ts=kickoff,
        competition=competition,
        home_player=home[0],
        away_player=away[0],
        home_club=home[1],
        away_club=away[1],
        status=status,
        home_ft=home_ft,
        away_ft=away_ft,
        raw_hash=hashlib.sha1(digest_src.encode()).hexdigest(),
        scraped_at=scraped_at,
    )


def quality_report(rows: list[MatchRow]) -> dict:
    """Ingest-time data-quality numbers (plan Phase 2): run on every ingest."""
    finished = [r for r in rows if r.status == STATUS_FINISHED]
    scored = [r for r in finished if r.home_ft is not None and r.away_ft is not None]
    totals = [r.home_ft + r.away_ft for r in scored]  # type: ignore[operator]
    keys = [(r.date, r.kickoff_ts, r.home_player, r.away_player, r.home_ft, r.away_ft)
            for r in rows]
    return {
        "rows": len(rows),
        "finished": len(finished),
        "finished_unscored": len(finished) - len(scored),  # anomaly if > 0
        "in_batch_dupes": len(keys) - len(set(keys)),
        "mean_total": round(sum(totals) / len(totals), 2) if totals else None,
        "players": len({r.home_player for r in rows} | {r.away_player for r in rows}),
    }
