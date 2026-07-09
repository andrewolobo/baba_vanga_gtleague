"""The prediction cycle: one idempotent pass, run as a short-lived process.

  python -m predictor.cycle           # predict the upcoming slate, append rows

Steps (plan §Phase 5): latest fixtures/odds → alias join → registry models
(day-frozen Poisson artifact; form leg rebuilt fresh) → as-of cutoff →
pick/confidence/tier/value per listed O/U line → APPEND to predictions.

No fitting in any request path: the Node API (Phase 7) spawns this on a timer
and on ingest completion; it reads only the predictions table itself.
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from core.config import OFFSET_TOL_MIN, settings
from core.schema import PredictionRow
from model import data, registry
from model.blend import blend_sides, totals_probs
from model.heuristic import FormIndex
from store.db import connect
from store.repo import PredictionRepo


def _alias(conn, name: str | None) -> str | None:
    if name is None:
        return None
    row = conn.execute("SELECT player FROM player_aliases WHERE book_name = ?",
                       (name,)).fetchone()
    return row["player"] if row else name


def _latest_odds(conn, event_ids: list[str]) -> dict[str, list]:
    """Latest snapshot batch per event -> {'ou': {line: {sel: (odds, implied)}}}"""
    if not event_ids:
        return {}
    ph = ",".join("?" * len(event_ids))
    rows = conn.execute(
        f"SELECT o.* FROM odds_snapshots o"
        f" JOIN (SELECT event_id, MAX(fetched_at) mf FROM odds_snapshots"
        f"       WHERE event_id IN ({ph}) GROUP BY event_id) m"
        f" ON o.event_id = m.event_id AND o.fetched_at = m.mf",
        event_ids,
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        if r["market"] != "ou" or r["line"] is None:
            continue
        ev = out.setdefault(r["event_id"], {})
        ev.setdefault(float(r["line"]), {})[r["selection"]] = (
            r["odds"], r["implied_prob"])
    return out


def _tier(confidence: float, s) -> str | None:
    if confidence >= s.tier_strong:
        return "strong"
    if confidence >= s.tier_solid:
        return "solid"
    if confidence >= s.tier_lean:
        return "lean"
    return None


def _fixture_lambdas(f, pm, form_idx, fallback, cutoff, s):
    """(lam_h, lam_a) per the served totals source, or (None, None) uncovered."""
    hp, ap = f["home_player"], f["away_player"]
    if hp not in pm.players or ap not in pm.players:
        return None, None
    lam_p = pm.predict_sides(hp, ap)
    cut64 = pd.Timestamp(cutoff).tz_convert("UTC").tz_localize(None).to_datetime64()
    lam_f = (form_idx.side_lambda(hp, ap, cut64, fallback),
             form_idx.side_lambda(ap, hp, cut64, fallback))
    if s.totals_source == "blend":
        return blend_sides(lam_p, lam_f, s.totals_blend_weight)
    return lam_p


def _line_row(f, line, sels, lam_h, lam_a, now, cutoff, version, s) -> PredictionRow:
    book_over, book_under = sels["over"][1], sels["under"][1]
    covered = lam_h is not None

    if covered:
        p_over, p_push, p_under = totals_probs(lam_h, lam_a, line)
        source = s.totals_source
    else:  # book fallback: no pick without model coverage
        p_over, p_push, p_under = book_over or 0.5, 0.0, book_under or 0.5
        source = "book"

    pick = confidence = tier = None
    value = False
    if covered:
        sel = "over" if p_over > p_under else "under"
        model_p = p_over if sel == "over" else p_under
        book_p = (book_over if sel == "over" else book_under) or 0.0
        confidence = round(0.5 * model_p + 0.5 * book_p, 6)
        value = (model_p - book_p) >= s.min_edge
        if confidence >= s.tier_lean and p_push <= s.max_push_prob:
            pick, tier = sel, _tier(confidence, s)

    return PredictionRow(
        event_id=f["event_id"], predicted_at=now, totals_source=source,
        line=line, p_over=round(p_over, 6), p_push=round(p_push, 6),
        p_under=round(p_under, 6),
        lambda_home=round(lam_h, 4) if covered else None,
        lambda_away=round(lam_a, 4) if covered else None,
        pick=pick, confidence=confidence, tier=tier, value_flag=value,
        model_version=version, as_of_cutoff_ts=cutoff,
    )


SCHEDULE_PREFIX = "gtl:"  # predictions for league-scheduled games without odds
SCHEDULE_LINES_AROUND = (-0.5, +0.5)  # canonical half-lines straddling E[total]


def _schedule_row(f, line, lam_h, lam_a, now, cutoff, version, s) -> PredictionRow:
    """Model-only prediction: no book yet, so confidence is the model prob
    alone and value_flag stays false (value needs a price to beat)."""
    p_over, p_push, p_under = totals_probs(lam_h, lam_a, line)
    sel = "over" if p_over > p_under else "under"
    confidence = round(p_over if sel == "over" else p_under, 6)
    pick = tier = None
    if confidence >= s.tier_lean and p_push <= s.max_push_prob:
        pick, tier = sel, _tier(confidence, s)
    return PredictionRow(
        event_id=f["event_id"], predicted_at=now, totals_source=s.totals_source,
        line=line, p_over=round(p_over, 6), p_push=round(p_push, 6),
        p_under=round(p_under, 6), lambda_home=round(lam_h, 4),
        lambda_away=round(lam_a, 4), pick=pick, confidence=confidence,
        tier=tier, value_flag=False, model_version=version,
        as_of_cutoff_ts=cutoff,
    )


def _scheduled_games(conn, now: datetime, horizon: datetime,
                     taken: set[tuple]) -> list[dict]:
    """Upcoming league-scheduled games not already covered by a book fixture."""
    rows = conn.execute(
        "SELECT * FROM matches WHERE status = 0 AND kickoff_ts > ?"
        " AND kickoff_ts <= ? ORDER BY kickoff_ts",
        (now.isoformat(), horizon.isoformat()),
    ).fetchall()
    out = []
    for r in rows:
        if (r["kickoff_ts"], r["home_player"], r["away_player"]) in taken:
            continue
        out.append({
            "event_id": f"{SCHEDULE_PREFIX}{r['source_match_id']}",
            "start_time_utc": r["kickoff_ts"],
            "home_raw": f"{r['home_club']} ({r['home_player']})",
            "away_raw": f"{r['away_club']} ({r['away_player']})",
            "home_player": r["home_player"], "away_player": r["away_player"],
        })
    return out


def run_cycle(conn, db_path, now: datetime | None = None) -> dict:
    t0 = time.perf_counter()
    s = settings()
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(hours=s.slate_horizon_hours)

    fixtures = conn.execute(
        "SELECT * FROM fixtures WHERE start_time_utc > ? AND start_time_utc <= ?"
        " ORDER BY start_time_utc",
        (now.isoformat(), horizon.isoformat()),
    ).fetchall()
    taken = {(f["start_time_utc"], f["home_player"], f["away_player"])
             for f in fixtures}
    scheduled = _scheduled_games(conn, now, horizon, taken)
    if not fixtures and not scheduled:
        return {"fixtures": 0, "rows": 0, "elapsed_s": 0.0}

    odds = _latest_odds(conn, [f["event_id"] for f in fixtures])

    pm = registry.get_poisson(conn, db_path,
                              half_life=s.half_life_days, alpha=s.poisson_alpha)
    df = data.load_matches(conn)
    recent = df[df["kickoff_ts"] >= pd.Timestamp(now)
                - pd.Timedelta(days=s.form_window_days)]
    form_idx = FormIndex(data.long_format(recent), span=s.form_span)
    fallback = float(recent["home_ft"].add(recent["away_ft"]).mean() / 2) \
        if len(recent) else float("nan")

    version = (f"{s.totals_source}-w{s.totals_blend_weight:g}"
               f"-hl{s.half_life_days:g}-a{s.poisson_alpha:g}")
    out: list[PredictionRow] = []
    slate = []
    for f in fixtures:  # event_id is the table PK — rows are unique
        ev_odds = odds.get(f["event_id"], {})
        if not ev_odds:
            continue  # nothing priced -> nothing to predict against
        f = dict(f, home_player=_alias(conn, f["home_player"]),
                 away_player=_alias(conn, f["away_player"]))
        kickoff = datetime.fromisoformat(f["start_time_utc"])
        # as-of guard: form may only see results published before the cutoff
        cutoff = min(now, kickoff - timedelta(minutes=OFFSET_TOL_MIN))
        lam_h, lam_a = _fixture_lambdas(f, pm, form_idx, fallback, cutoff, s)

        for line, sels in sorted(ev_odds.items()):
            if "over" not in sels or "under" not in sels:
                continue
            row = _line_row(f, line, sels, lam_h, lam_a, now, cutoff, version, s)
            out.append(row)
            slate.append((f, line, row.pick, row.tier, row.p_over))

    for f in scheduled:
        f = dict(f, home_player=_alias(conn, f["home_player"]),
                 away_player=_alias(conn, f["away_player"]))
        kickoff = datetime.fromisoformat(f["start_time_utc"])
        cutoff = min(now, kickoff - timedelta(minutes=OFFSET_TOL_MIN))
        lam_h, lam_a = _fixture_lambdas(f, pm, form_idx, fallback, cutoff, s)
        if lam_h is None:
            continue  # no model coverage and no odds -> nothing to say
        base = max(1.0, round(lam_h + lam_a))
        for dl in SCHEDULE_LINES_AROUND:
            row = _schedule_row(f, base + dl, lam_h, lam_a, now, cutoff,
                                version, s)
            out.append(row)
            slate.append((f, row.line, row.pick, row.tier, row.p_over))

    n = PredictionRepo(conn).append_many(out)
    return {"fixtures": len(fixtures), "scheduled": len(scheduled), "rows": n,
            "elapsed_s": round(time.perf_counter() - t0, 2), "slate": slate}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="predictor.cycle")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    s = settings()
    db_path = args.db or s.gtl_db_path
    conn = connect(db_path)

    rep = run_cycle(conn, db_path)
    print(f"fixtures={rep['fixtures']} scheduled={rep.get('scheduled', 0)} "
          f"prediction_rows={rep['rows']} elapsed={rep['elapsed_s']}s")
    for f, line, pick, tier, p_over in rep.get("slate", []):
        label = f"{pick} ({tier})" if pick else "no pick"
        print(f"  {f['start_time_utc'][11:16]}Z {f['home_raw']} v {f['away_raw']}"
              f" | O/U {line}: p_over={p_over:.3f} -> {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
