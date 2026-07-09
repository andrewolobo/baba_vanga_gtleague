"""Settlement: grade served predictions against scraped results.

  python -m settlement.settle run          # settle finished, unsettled events
  python -m settlement.settle scorecard    # rolling hit rates / Brier

Join rule (PHASE0_PROBES §0.4): fixture -> match on BOTH player names and an
exact-kickoff window of ±OFFSET_TOL_MIN. Never nearest-join — the same pair
replays every ~2.5 h and decoy candidates are real.

Two contamination guards (parent §2.2.10):
- leak_risk: the matched result row existed in our DB before the served
  prediction was made (should be impossible — results publish ~14 min after
  kickoff; any hit means the serving path leaked).
- regen: re-predict with training truncated before kickoff (day-frozen
  Poisson + form as-of kickoff−tol) and compare picks; regen_agrees must be
  ~100% or the serving path and the honest path have diverged.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

from core.config import OFFSET_TOL_MIN, settings
from model import data, registry
from model.blend import blend_sides, totals_probs
from model.heuristic import FormIndex
from store.db import connect

SETTLE_DELAY_MIN = 45  # kickoff + match (~13 min) + publish + merge cadence


def _served_prediction(conn, event_id: str, kickoff_iso: str):
    """Headline row of the last prediction batch made before kickoff."""
    batch_ts = conn.execute(
        "SELECT MAX(predicted_at) t FROM predictions"
        " WHERE event_id = ? AND predicted_at < ?",
        (event_id, kickoff_iso),
    ).fetchone()["t"]
    if batch_ts is None:
        return None
    rows = conn.execute(
        "SELECT * FROM predictions WHERE event_id = ? AND predicted_at = ?"
        " ORDER BY (pick IS NULL), confidence DESC, line",
        (event_id, batch_ts),
    ).fetchall()
    return rows[0] if rows else None


def _match_for(conn, fixture, tol_min: float = OFFSET_TOL_MIN):
    k = datetime.fromisoformat(fixture["start_time_utc"])
    lo = (k - timedelta(minutes=tol_min)).isoformat()
    hi = (k + timedelta(minutes=tol_min)).isoformat()
    cands = conn.execute(
        "SELECT * FROM matches WHERE status = 3 AND home_ft IS NOT NULL"
        " AND home_player = ? AND away_player = ?"
        " AND kickoff_ts BETWEEN ? AND ?",
        (fixture["home_player"], fixture["away_player"], lo, hi),
    ).fetchall()
    return cands[0] if len(cands) == 1 else None  # ambiguity -> leave pending


def _grade(pick: str | None, line: float, total: int) -> int | None:
    if pick is None:
        return None
    if total == line:  # integer-line push: no grade
        return None
    won = total > line if pick == "over" else total < line
    return int(won)


class _Regen:
    """Per-day cache of the honest re-prediction machinery."""

    def __init__(self, conn, db_path):
        self.conn, self.db_path = conn, db_path
        self.s = settings()
        self._day: str | None = None
        self._pm = None
        self._form: FormIndex | None = None
        self._fallback = 2.0

    def pick(self, fixture, line: float) -> str | None:
        day = fixture["start_time_utc"][:10]
        if day != self._day:
            self._pm = registry.get_poisson(
                self.conn, self.db_path, day=day,
                half_life=self.s.half_life_days, alpha=self.s.poisson_alpha)
            df = data.load_matches(self.conn)
            self._form = FormIndex(data.long_format(df), span=self.s.form_span)
            self._fallback = float(df["home_ft"].add(df["away_ft"]).mean() / 2)
            self._day = day
        hp, ap = fixture["home_player"], fixture["away_player"]
        if hp not in self._pm.players or ap not in self._pm.players:
            return None
        kickoff = datetime.fromisoformat(fixture["start_time_utc"])
        cut = pd.Timestamp(kickoff - timedelta(minutes=OFFSET_TOL_MIN)) \
            .tz_convert("UTC").tz_localize(None).to_datetime64()
        lam_p = self._pm.predict_sides(hp, ap)
        lam_f = (self._form.side_lambda(hp, ap, cut, self._fallback),
                 self._form.side_lambda(ap, hp, cut, self._fallback))
        if self.s.totals_source == "blend":
            lam = blend_sides(lam_p, lam_f, self.s.totals_blend_weight)
        else:
            lam = lam_p
        p_over, _, p_under = totals_probs(lam[0], lam[1], line)
        return "over" if p_over > p_under else "under"


def run(conn, db_path, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    ready = (now - timedelta(minutes=SETTLE_DELAY_MIN)).isoformat()
    fixtures = conn.execute(
        "SELECT f.* FROM fixtures f"
        " WHERE f.start_time_utc <= ?"
        " AND EXISTS (SELECT 1 FROM predictions p WHERE p.event_id = f.event_id)"
        " AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.event_id = f.event_id)"
        " ORDER BY f.start_time_utc",
        (ready,),
    ).fetchall()

    regen = _Regen(conn, db_path)
    settled = pending = 0
    for f in fixtures:
        m = _match_for(conn, f)
        if m is None:
            pending += 1
            continue
        served = _served_prediction(conn, f["event_id"], f["start_time_utc"])
        if served is None:
            pending += 1
            continue

        total = m["home_ft"] + m["away_ft"]
        leak = m["scraped_at"] <= served["predicted_at"]
        r_pick = regen.pick(f, served["line"])
        offset_min = (
            datetime.fromisoformat(m["kickoff_ts"])
            - datetime.fromisoformat(f["start_time_utc"])
        ).total_seconds() / 60.0

        with conn:
            conn.execute(
                "INSERT INTO settlements (event_id, matched_match_id,"
                " offset_min_used, result_total, pick_correct, leak_risk,"
                " regen_pick, regen_agrees, settled_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f["event_id"], m["id"], offset_min, total,
                 _grade(served["pick"], served["line"], total), int(leak),
                 r_pick,
                 None if served["pick"] is None else int(r_pick == served["pick"]),
                 now.isoformat()),
            )
        settled += 1
    return {"settled": settled, "pending": pending, "candidates": len(fixtures)}


def scorecard(conn, days: int = 7) -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = """
        SELECT s.*, p.pick, p.tier, p.confidence, p.p_over, p.line, p.value_flag
        FROM settlements s
        JOIN fixtures f ON f.event_id = s.event_id
        JOIN predictions p ON p.event_id = s.event_id
            AND p.predicted_at = (SELECT MAX(predicted_at) FROM predictions
                                  WHERE event_id = s.event_id
                                  AND predicted_at < f.start_time_utc)
        WHERE s.settled_at >= ?
        ORDER BY (p.pick IS NULL), p.confidence DESC
    """
    df = pd.read_sql_query(q, conn, params=(since,))
    if df.empty:
        return "no settlements in window"
    # one headline row per event (highest-confidence picked line)
    df = df.drop_duplicates(subset="event_id", keep="first")
    graded = df[df["pick_correct"].notna()]
    lines = [f"settled events: {len(df)} | graded picks: {len(graded)} "
             f"| leak_risk: {int(df['leak_risk'].sum())} "
             f"| regen agreement: {df['regen_agrees'].mean():.1%}"
             if df["regen_agrees"].notna().any() else ""]
    if len(graded):
        lines.append(f"overall hit rate: {graded['pick_correct'].mean():.1%} "
                     f"({int(graded['pick_correct'].sum())}/{len(graded)})")
        for tier in ("strong", "solid", "lean"):
            t = graded[graded["tier"] == tier]
            if len(t):
                lines.append(f"  {tier:>6}: {t['pick_correct'].mean():.1%} ({len(t)})")
        v = graded[graded["value_flag"] == 1]
        if len(v):
            lines.append(f"  value: {v['pick_correct'].mean():.1%} ({len(v)})")
        y = (graded["result_total"] > graded["line"]).astype(float)
        lines.append(f"model Brier (p_over): {((graded['p_over'] - y) ** 2).mean():.4f}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="settlement.settle")
    ap.add_argument("cmd", choices=["run", "scorecard"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    s = settings()
    db_path = args.db or s.gtl_db_path
    conn = connect(db_path)

    if args.cmd == "run":
        rep = run(conn, db_path)
        print(f"settled={rep['settled']} pending={rep['pending']} "
              f"candidates={rep['candidates']}")
        leaks = conn.execute(
            "SELECT COUNT(*) c FROM settlements WHERE leak_risk = 1").fetchone()["c"]
        if leaks:
            print(f"WARNING: {leaks} settlements flagged leak_risk", file=sys.stderr)
            return 2
        return 0
    print(scorecard(conn, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
