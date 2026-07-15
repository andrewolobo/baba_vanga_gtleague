"""Settlement: grade served predictions against scraped results.

  python -m settlement.settle run          # settle finished, unsettled events
  python -m settlement.settle scorecard    # rolling hit rates / Brier
  python -m settlement.settle vs-book      # served probs vs the closing price
  python -m settlement.settle x12-vs-book  # served 1x2 triple vs the closing price
  python -m settlement.settle tiers        # propose tier bands from served data

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
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from core.config import OFFSET_TOL_MIN, settings
from core.markets import x12_probs
from model import data, h2h, recal, registry
from model.blend import PMF_MAX_GOALS, blend_sides, totals_probs
from model.heuristic import FormIndex
from store import aliases
from store.db import connect

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

    def __init__(self, conn, db_path, now: datetime | None = None):
        self.conn, self.db_path = conn, db_path
        self.s = settings()
        self._pms: dict[tuple[str, bool], object] = {}
        self._day: str | None = None
        self._form: FormIndex | None = None
        self._fallback = 2.0
        self._clubs = aliases.club_alias_map(conn)
        # same recal maps as serving so regen_agrees stays a λ-honesty
        # canary; the maps drift slightly between predict and settle time
        # (hours of extra settlements) — a known, documented tolerance.
        # Per population, mirroring the cycle (docs/POPULATION_SPLIT.md).
        self._maps = (recal.fit_line_maps(conn, self.s.recal_days,
                                          self.s.recal_min_n,
                                          self.s.recal_min_n_line)
                      if self.s.recal_enabled else {})
        self._maps_sched = (recal.fit_line_maps(conn, self.s.recal_days,
                                                self.s.recal_min_n,
                                                self.s.recal_min_n_line,
                                                population="schedule")
                            if self.s.recal_enabled else {})
        # x12 H2H stackers, same contract as the recal maps: refit at settle
        # time per population, applied only to '-h2h'-tagged rows; the hours
        # of drift between predict and settle are the same documented
        # tolerance. Flag off -> None -> tagged rows regen through identity
        # (exactly recal's rollback behavior on '-recal' rows).
        self._h2h_stacks: dict[str, object] = {"priced": None, "schedule": None}
        if self.s.x12_h2h_enabled:
            idx = h2h.H2HIndex(data.load_matches(conn))
            self._h2h_stacks = {
                pop: h2h.fit_stacker(conn, self.s.x12_h2h_days,
                                     self.s.x12_h2h_min_n, idx,
                                     population=pop, now=now)
                for pop in ("priced", "schedule")}
            self._h2h_idx = idx

    def _poisson(self, day: str, with_club: bool, with_tod: bool):
        key = (day, with_club, with_tod)
        if key not in self._pms:
            self._pms[key] = registry.get_poisson(
                self.conn, self.db_path, day=day,
                half_life=self.s.half_life_days, alpha=self.s.poisson_alpha,
                with_club=with_club, with_tod=with_tod)
        return self._pms[key]

    def _lambdas(self, fixture, model_version: str) -> tuple[float, float] | None:
        """The honest served-λ pair for a fixture, or None if uncovered.

        Clubs are resolved through the same `store.aliases` path the cycle
        uses — a divergence there would show up as a regen_agrees collapse and
        read exactly like a leak. with_club and with_tod are read off the
        *served row's* model_version, not from current settings: rows priced
        before a feature shipped must be regenerated without it, or every one
        of them disagrees during the transition.
        """
        day = fixture["start_time_utc"][:10]
        pm = self._poisson(day, "-club" in model_version,
                           "-tod" in model_version)
        if day != self._day:
            df = data.load_matches(self.conn)
            self._form = FormIndex(data.long_format(df), span=self.s.form_span)
            self._fallback = float(df["home_ft"].add(df["away_ft"]).mean() / 2)
            self._day = day
        hp, ap = fixture["home_player"], fixture["away_player"]
        if hp not in pm.players or ap not in pm.players:
            return None
        kickoff = datetime.fromisoformat(fixture["start_time_utc"])
        cut = pd.Timestamp(kickoff - timedelta(minutes=OFFSET_TOL_MIN)) \
            .tz_convert("UTC").tz_localize(None).to_datetime64()
        lam_p = pm.predict_sides(
            hp, ap,
            aliases.resolve_club(fixture["home_raw"], self._clubs),
            aliases.resolve_club(fixture["away_raw"], self._clubs),
            hour=kickoff.hour + kickoff.minute / 60.0)
        lam_f = (self._form.side_lambda(hp, ap, cut, self._fallback),
                 self._form.side_lambda(ap, hp, cut, self._fallback))
        if self.s.totals_source == "blend":
            return blend_sides(lam_p, lam_f, self.s.totals_blend_weight)
        return lam_p

    def pick(self, fixture, line: float, model_version: str = "",
             maps: dict | None = None) -> str | None:
        """maps selects the population map set (None -> priced, mirroring
        _line_row; _run_schedule passes the schedule maps). Recal is
        version-aware like club/tod: '-recal' has tagged exactly the rows a
        map touched since the population split Phase 0, so an untagged row
        was served through identity and must regen through identity — a map
        intercept can flip the side on a near-coin-flip row, which would
        read as regen disagreement instead of λ dishonesty."""
        lam = self._lambdas(fixture, model_version)
        if lam is None:
            return None
        if "-recal" not in model_version:
            maps = {}
        elif maps is None:
            maps = self._maps
        p_over, p_push, _ = totals_probs(lam[0], lam[1], line)
        p_over, _, p_under = recal.apply_to_line(maps, line, p_over, p_push)
        return "over" if p_over > p_under else "under"

    def x12_pick(self, fixture, model_version: str = "",
                 population: str = "priced") -> str | None:
        """Mirror of cycle._x12_row's pick logic on the honest λs. Known
        tolerance (same as the recal maps): the gate is read from current
        settings, so a threshold change mid-flight can flip agreement on
        near-gate picks — that is config drift, not leakage.

        H2H is version-aware exactly like '-recal' on the totals path: only
        '-h2h'-tagged rows regen through a stacker (this population's own,
        refit at settle time — same drift tolerance), and an untagged row
        must regen unstacked or every pre-h2h row disagrees during the
        transition. Features are re-derived at kickoff − OFFSET_TOL_MIN, the
        cutoff the graded (last pre-kickoff) batch priced with."""
        lam = self._lambdas(fixture, model_version)
        if lam is None:
            return None
        p_home, p_draw, p_away = x12_probs(lam[0], lam[1], PMF_MAX_GOALS)
        stack = (self._h2h_stacks.get(population)
                 if "-h2h" in model_version else None)
        if stack is not None:
            kickoff = datetime.fromisoformat(fixture["start_time_utc"])
            cut = pd.Timestamp(kickoff - timedelta(minutes=OFFSET_TOL_MIN)) \
                .tz_convert("UTC").tz_localize(None).to_datetime64()
            feats = self._h2h_idx.features(fixture["home_player"],
                                           fixture["away_player"], cut)
            s_new = stack.apply_one(
                p_home / max(p_home + p_away, 1e-12), feats)
            p_home, p_draw, p_away = h2h.restack_x12(s_new, p_draw)
        probs = {"home": p_home, "draw": p_draw, "away": p_away}
        sel = max(probs, key=probs.get)
        return sel if probs[sel] >= self.s.x12_pick_prob_threshold else None


def run(conn, db_path, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    ready = (now - timedelta(minutes=settings().settle_delay_min)).isoformat()
    fixtures = conn.execute(
        "SELECT f.* FROM fixtures f"
        " WHERE f.start_time_utc <= ?"
        " AND EXISTS (SELECT 1 FROM predictions p WHERE p.event_id = f.event_id)"
        " AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.event_id = f.event_id)"
        " ORDER BY f.start_time_utc",
        (ready,),
    ).fetchall()

    regen = _Regen(conn, db_path, now=now)
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
        r_pick = regen.pick(f, served["line"], served["model_version"])
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

    x12 = _run_x12(conn, regen, now, ready)
    x12_sched = _run_x12_schedule(conn, regen, now, ready)
    sched = _run_schedule(conn, regen, now, ready)
    return {"settled": settled, "pending": pending, "candidates": len(fixtures),
            **x12, **x12_sched, **sched}


def _served_x12(conn, event_id: str, kickoff_iso: str):
    return conn.execute(
        "SELECT * FROM predictions_x12 WHERE event_id = ? AND predicted_at ="
        " (SELECT MAX(predicted_at) FROM predictions_x12"
        "  WHERE event_id = ? AND predicted_at < ?)",
        (event_id, event_id, kickoff_iso),
    ).fetchone()


def _run_x12(conn, regen: _Regen, now: datetime, ready: str) -> dict:
    """Grade 1x2 predictions. Its own loop with its own NOT EXISTS: events
    settled for totals before this head shipped still get x12 grading the
    first time an x12 row exists for them."""
    fixtures = conn.execute(
        "SELECT f.* FROM fixtures f"
        " WHERE f.start_time_utc <= ?"
        " AND EXISTS (SELECT 1 FROM predictions_x12 p WHERE p.event_id = f.event_id)"
        " AND NOT EXISTS (SELECT 1 FROM settlements_x12 s WHERE s.event_id = f.event_id)"
        " ORDER BY f.start_time_utc",
        (ready,),
    ).fetchall()
    settled = pending = 0
    for f in fixtures:
        m = _match_for(conn, f)
        served = _served_x12(conn, f["event_id"], f["start_time_utc"])
        if m is None or served is None:
            pending += 1
            continue
        gd = m["home_ft"] - m["away_ft"]
        if gd > 0:
            result = "home"
        else:
            result = "draw" if gd == 0 else "away"
        r_pick = regen.x12_pick(f, served["model_version"])
        with conn:
            conn.execute(
                "INSERT INTO settlements_x12 (event_id, matched_match_id,"
                " result, pick_correct, regen_pick, regen_agrees, settled_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (f["event_id"], m["id"], result,
                 None if served["pick"] is None
                 else int(served["pick"] == result),
                 r_pick,
                 None if served["pick"] is None
                 else int(r_pick == served["pick"]),
                 now.isoformat()),
            )
        settled += 1
    return {"x12_settled": settled, "x12_pending": pending}


def _run_x12_schedule(conn, regen: _Regen, now: datetime, ready: str) -> dict:
    """Grade schedule-only (gtl:) 1x2 predictions. Same exact-id join as
    _run_schedule, same NOT EXISTS transition safety as _run_x12. Found live
    2026-07-12: _run_x12 walks fixtures, so without this loop the schedule
    population's x12 rows never settled — the totals population-split bug
    (docs/POPULATION_SPLIT.md Phase 1) recurring in the new head."""
    if not regen.s.schedule_settle_enabled:
        return {"x12_sched_settled": 0, "x12_sched_pending": 0}
    matches = conn.execute(
        "SELECT m.* FROM matches m"
        " WHERE m.status = 3 AND m.home_ft IS NOT NULL AND m.kickoff_ts <= ?"
        " AND EXISTS (SELECT 1 FROM predictions_x12 p"
        "             WHERE p.event_id = 'gtl:' || m.source_match_id)"
        " AND NOT EXISTS (SELECT 1 FROM settlements_x12 s"
        "                 WHERE s.event_id = 'gtl:' || m.source_match_id)"
        " ORDER BY m.kickoff_ts",
        (ready,),
    ).fetchall()
    settled = pending = 0
    for m in matches:
        event_id = f"gtl:{m['source_match_id']}"
        served = _served_x12(conn, event_id, m["kickoff_ts"])
        if served is None:  # no pre-kickoff batch -> nothing gradable
            pending += 1
            continue
        gd = m["home_ft"] - m["away_ft"]
        if gd > 0:
            result = "home"
        else:
            result = "draw" if gd == 0 else "away"
        # fixture-shaped dict, same construction as _run_schedule, so regen
        # resolves clubs through the identical alias path
        fixture = {
            "start_time_utc": m["kickoff_ts"],
            "home_raw": f"{m['home_club']} ({m['home_player']})",
            "away_raw": f"{m['away_club']} ({m['away_player']})",
            "home_player": m["home_player"], "away_player": m["away_player"],
        }
        r_pick = regen.x12_pick(fixture, served["model_version"],
                                population="schedule")
        with conn:
            conn.execute(
                "INSERT INTO settlements_x12 (event_id, matched_match_id,"
                " result, pick_correct, regen_pick, regen_agrees, settled_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (event_id, m["id"], result,
                 None if served["pick"] is None
                 else int(served["pick"] == result),
                 r_pick,
                 None if served["pick"] is None
                 else int(r_pick == served["pick"]),
                 now.isoformat()),
            )
        settled += 1
    return {"x12_sched_settled": settled, "x12_sched_pending": pending}


def _run_schedule(conn, regen: _Regen, now: datetime, ready: str) -> dict:
    """Grade schedule-only (gtl:) predictions — docs/POPULATION_SPLIT.md
    Phase 1. Exact join: the event id IS the match id (the cycle builds it
    as f"gtl:{source_match_id}"), so there is no player+kickoff-window fuzzy
    join and no replay-decoy hazard. Rows land in the existing settlements
    table; every pre-existing consumer (scorecard, vs_book, tier_bands, the
    recal fit) joins through fixtures, so schedule settlements stay
    invisible until a query opts in. Population is derivable as
    event_id LIKE 'gtl:%'. The NOT EXISTS back-settles the full gtl history
    on the first enabled run."""
    if not regen.s.schedule_settle_enabled:
        return {"schedule_settled": 0, "schedule_pending": 0}
    matches = conn.execute(
        # string concat, not CAST: source_match_id is TEXT and the cycle
        # builds the event id from it verbatim
        "SELECT m.* FROM matches m"
        " WHERE m.status = 3 AND m.home_ft IS NOT NULL AND m.kickoff_ts <= ?"
        " AND EXISTS (SELECT 1 FROM predictions p"
        "             WHERE p.event_id = 'gtl:' || m.source_match_id)"
        " AND NOT EXISTS (SELECT 1 FROM settlements s"
        "                 WHERE s.event_id = 'gtl:' || m.source_match_id)"
        " ORDER BY m.kickoff_ts",
        (ready,),
    ).fetchall()
    settled = pending = 0
    for m in matches:
        event_id = f"gtl:{m['source_match_id']}"
        served = _served_prediction(conn, event_id, m["kickoff_ts"])
        if served is None:  # no pre-kickoff batch -> nothing gradable
            pending += 1
            continue
        total = m["home_ft"] + m["away_ft"]
        # Weak guard by construction: scraped_at is overwritten on re-scrape
        # and a finished game has necessarily been scraped after kickoff, so
        # this almost never fires even when it should. The real protections
        # are the cycle's as-of cutoff and the regen check, both
        # population-blind (docs/POPULATION_SPLIT.md Phase 1).
        leak = m["scraped_at"] <= served["predicted_at"]
        # fixture-shaped dict, same construction as cycle._scheduled_games,
        # so regen resolves clubs through the identical alias path
        fixture = {
            "start_time_utc": m["kickoff_ts"],
            "home_raw": f"{m['home_club']} ({m['home_player']})",
            "away_raw": f"{m['away_club']} ({m['away_player']})",
            "home_player": m["home_player"], "away_player": m["away_player"],
        }
        # schedule-population maps, like serving since Phase 2; pick() drops
        # to identity for rows whose served version carries no '-recal' tag
        r_pick = regen.pick(fixture, served["line"], served["model_version"],
                            maps=regen._maps_sched)
        with conn:
            conn.execute(
                "INSERT INTO settlements (event_id, matched_match_id,"
                " offset_min_used, result_total, pick_correct, leak_risk,"
                " regen_pick, regen_agrees, settled_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (event_id, m["id"], 0.0, total,
                 _grade(served["pick"], served["line"], total), int(leak),
                 r_pick,
                 None if served["pick"] is None else int(r_pick == served["pick"]),
                 now.isoformat()),
            )
        settled += 1
    return {"schedule_settled": settled, "schedule_pending": pending}


# Population split (docs/POPULATION_SPLIT.md Phase 3): the scorecard reports
# each prediction population in its own section, never pooled — the pooled
# view systematically reported the model's worst subpopulation and is what
# made the model look broken. The priced query's fixtures join IS its
# population filter; the schedule query goes through matched_match_id.
_SCORECARD_QUERIES = (
    ("book-priced", """
        SELECT s.*, p.pick, p.tier, p.confidence, p.p_over, p.line, p.value_flag
        FROM settlements s
        JOIN fixtures f ON f.event_id = s.event_id
        JOIN predictions p ON p.event_id = s.event_id
            AND p.predicted_at = (SELECT MAX(predicted_at) FROM predictions
                                  WHERE event_id = s.event_id
                                  AND predicted_at < f.start_time_utc)
        WHERE s.settled_at >= ?
        ORDER BY (p.pick IS NULL), p.confidence DESC
    """),
    ("schedule (model-only)", """
        SELECT s.*, p.pick, p.tier, p.confidence, p.p_over, p.line, p.value_flag
        FROM settlements s
        JOIN matches m ON m.id = s.matched_match_id
        JOIN predictions p ON p.event_id = s.event_id
            AND p.predicted_at = (SELECT MAX(predicted_at) FROM predictions
                                  WHERE event_id = s.event_id
                                  AND predicted_at < m.kickoff_ts)
        WHERE s.settled_at >= ? AND s.event_id LIKE 'gtl:%'
        ORDER BY (p.pick IS NULL), p.confidence DESC
    """),
)


def _scorecard_section(df: pd.DataFrame) -> list[str]:
    # one headline row per event (highest-confidence picked line)
    df = df.drop_duplicates(subset="event_id", keep="first")
    graded = df[df["pick_correct"].notna()]
    regen = (f"{df['regen_agrees'].mean():.1%}"
             if df["regen_agrees"].notna().any() else "n/a (no picks)")
    lines = [f"settled events: {len(df)} | graded picks: {len(graded)} "
             f"| leak_risk: {int(df['leak_risk'].sum())} "
             f"| regen agreement: {regen}"]
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
    return lines


def scorecard(conn, days: int = 7) -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out: list[str] = []
    for label, q in _SCORECARD_QUERIES:
        df = pd.read_sql_query(q, conn, params=(since,))
        # ASCII header: the Windows console (cp1252) chokes on box-drawing
        out.append(f"== {label} ==")
        out += _scorecard_section(df) if not df.empty \
            else ["no settlements in window"]
        out.append("")
    return "\n".join(out).rstrip()


# ── model vs book ────────────────────────────────────────────────────────────
#
# The benchmark is the last de-vigged Over price before kickoff. betPawa ships
# margin-free probs (PRICE.10.1), so this is the hard version of the test.
#
# Rows compared are model-covered (lambda_home NOT NULL — book-fallback rows
# would be scored against themselves), leak-clean, and non-push. The served
# batch is the same one scorecard grades: the last one before kickoff.

_VS_BOOK_QUERY = """
    SELECT p.line, p.p_over, p.tier, p.value_flag, s.result_total, s.settled_at,
           p.lambda_home + p.lambda_away AS lam_total,
           (SELECT o.implied_prob FROM odds_snapshots o
             WHERE o.event_id = s.event_id AND o.market = 'ou'
               AND o.line = p.line AND o.selection = 'over'
               AND o.fetched_at < f.start_time_utc
             ORDER BY o.fetched_at DESC LIMIT 1) AS book_over,
           (SELECT o.odds FROM odds_snapshots o
             WHERE o.event_id = s.event_id AND o.market = 'ou'
               AND o.line = p.line AND o.selection = 'over'
               AND o.fetched_at < f.start_time_utc
             ORDER BY o.fetched_at DESC LIMIT 1) AS odds_over,
           (SELECT o.odds FROM odds_snapshots o
             WHERE o.event_id = s.event_id AND o.market = 'ou'
               AND o.line = p.line AND o.selection = 'under'
               AND o.fetched_at < f.start_time_utc
             ORDER BY o.fetched_at DESC LIMIT 1) AS odds_under
    FROM settlements s
    JOIN fixtures f ON f.event_id = s.event_id
    JOIN predictions p ON p.event_id = s.event_id
     AND p.predicted_at = (SELECT MAX(predicted_at) FROM predictions
                           WHERE event_id = s.event_id
                           AND predicted_at < f.start_time_utc)
    WHERE s.settled_at >= ? AND s.leak_risk = 0
      AND s.result_total IS NOT NULL AND p.lambda_home IS NOT NULL
      AND s.result_total != p.line
"""

# a Brier gap we would act on; sets the sample size the command asks for
_BRIER_RESOLUTION = 0.002
_PROB_EPS = 1e-6


def _logit(p):
    p = np.clip(np.asarray(p, dtype=float), _PROB_EPS, 1 - _PROB_EPS)
    return np.log(p) - np.log1p(-p)


def _brier(p, y) -> float:
    return float(np.mean((p - y) ** 2))


def _logloss(p, y) -> float:
    p = np.clip(p, _PROB_EPS, 1 - _PROB_EPS)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log1p(-p)))


def _ci(samples) -> tuple[float, float]:
    lo, hi = np.percentile(samples, [2.5, 97.5])
    return float(lo), float(hi)


def _roi(p_over, y, odds_over, odds_under) -> float:
    """Flat 1u on whichever side the model leans, priced at the close.
    Directional on every row, not just the ones that cleared the pick gate."""
    side = (p_over > 0.5).astype(int)
    odds = np.where(side == 1, odds_over, odds_under)
    return float(np.mean(np.where(side == y, odds - 1.0, -1.0)))


def _edge_coefs(model, book, y, boot: int, rng) -> dict:
    """logit(y) ~ logit(book) + [logit(model) - logit(book)].

    The edge coefficient is the whole question: does the model's disagreement
    with the price carry information the price does not already have? Brier
    barely can answer it — the book sets the line so that p ~= 0.5, which
    pins both series near the same score whatever either one knows.
    """
    lb = _logit(book)
    x = np.column_stack([lb, _logit(model) - lb])

    def fit(x_, y_):
        lr = LogisticRegression(C=1e6, max_iter=1000).fit(x_, y_)
        return lr.coef_[0]

    point = fit(x, y)
    draws = []
    for _ in range(boot):
        i = rng.integers(0, len(y), len(y))
        if len(set(y[i])) == 2:
            draws.append(fit(x[i], y[i]))
    d = np.array(draws)
    return {"book": point[0], "edge": point[1],
            "book_ci": _ci(d[:, 0]), "edge_ci": _ci(d[:, 1]),
            "p_edge_positive": float(np.mean(d[:, 1] > 0))}


def _lam_slope(lam, total, boot: int, rng) -> dict:
    """OLS realized_total ~ a + b*lam_total. A calibrated mean gives b = 1;
    b > 1 means the model shrinks lambda toward the league average and the
    market moves further than it does."""
    point = float(np.polyfit(lam, total, 1)[0])
    draws = []
    for _ in range(boot):
        i = rng.integers(0, len(lam), len(lam))
        draws.append(np.polyfit(lam[i], total[i], 1)[0])
    return {"slope": point, "ci": _ci(draws)}


def _breakdown(df: pd.DataFrame, key: str, fmt=str) -> list[str]:
    lines = [f"  {key:>10} {'n':>5} {'model':>8} {'book':>8} {'skill':>8}"
             f" {'hit':>7} {'roi':>8}"]
    for val, g in df.groupby(key, dropna=False, observed=True):
        y = g["y"].to_numpy()
        bm, bb = _brier(g["p_over"].to_numpy(), y), _brier(g["book_over"].to_numpy(), y)
        skill = 1 - bm / bb if bb > 0 else float("nan")
        hit = float(np.mean((g["p_over"].to_numpy() > 0.5).astype(int) == y))
        roi = _roi(g["p_over"].to_numpy(), y, g["odds_over"].to_numpy(),
                   g["odds_under"].to_numpy())
        label = "(no pick)" if pd.isna(val) else fmt(val)
        lines.append(f"  {label:>10} {len(g):>5} {bm:>8.4f} {bb:>8.4f}"
                     f" {skill:>+7.2%} {hit:>7.1%} {roi:>+7.2%}")
    return lines


def vs_book(conn, days: int = 7, boot: int = 2000, seed: int = 0) -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    df = pd.read_sql_query(_VS_BOOK_QUERY, conn, params=(since,))
    df = df.dropna(subset=["book_over", "odds_over", "odds_under"])
    if df.empty:
        return "no settled, model-covered, book-priced predictions in window"
    df["y"] = (df["result_total"] > df["line"]).astype(int)

    y = df["y"].to_numpy()
    model, book = df["p_over"].to_numpy(), df["book_over"].to_numpy()
    n = len(df)
    rng = np.random.default_rng(seed)
    # settlement throughput comes from the span the rows actually cover, not
    # from --days: early on the window is mostly empty and would flatter it
    ts = pd.to_datetime(df["settled_at"], format="ISO8601")
    span_days = max((ts.max() - ts.min()).total_seconds() / 86400.0, 1e-3)

    out = [f"model vs book: {n} settled predictions in a {days}d window"
           f" | spanning {span_days:.1f}d"
           f" | lines {df['line'].min()}..{df['line'].max()}"
           f" | base rate over {y.mean():.1%}",
           "",
           f"  {'':<22}{'Brier':>8} {'LogLoss':>9} {'side-hit':>9}",
           f"  {'model (as served)':<22}{_brier(model, y):>8.4f}"
           f" {_logloss(model, y):>9.4f}"
           f" {np.mean((model > 0.5).astype(int) == y):>8.1%}",
           f"  {'book (de-vig close)':<22}{_brier(book, y):>8.4f}"
           f" {_logloss(book, y):>9.4f}"
           f" {np.mean((book > 0.5).astype(int) == y):>8.1%}",
           f"  {'constant 0.500':<22}{_brier(np.full(n, 0.5), y):>8.4f}"
           f" {_logloss(np.full(n, 0.5), y):>9.4f} {'—':>9}"]

    d = (model - y) ** 2 - (book - y) ** 2
    draws = [d[rng.integers(0, n, n)].mean() for _ in range(boot)]
    lo, hi = _ci(draws)
    verdict = ("model better" if hi < 0 else "book better" if lo > 0
               else "NOT SIGNIFICANT")
    need = int(np.ceil((1.96 * d.std(ddof=1) / _BRIER_RESOLUTION) ** 2))
    eta = max(0.0, (need - n) / (n / span_days))
    out += ["",
            f"paired Brier (model - book): {d.mean():+.5f}"
            f"  95% CI [{lo:+.5f}, {hi:+.5f}]  {verdict}",
            f"  the book parks each line near a coin flip, so Brier is a weak"
            f" lens here; resolving",
            f"  a +/-{_BRIER_RESOLUTION} gap needs ~{need} settled predictions"
            f" (~{eta:.0f} more days at {n / span_days:.0f}/day)."]

    if 0 < y.sum() < n:
        e = _edge_coefs(model, book, y, boot, rng)
        out += ["",
                "does the model add anything to the price?"
                "  y ~ logit(book) + [logit(model) - logit(book)]",
                f"  edge coef {e['edge']:+.3f}  95% CI"
                f" [{e['edge_ci'][0]:+.3f}, {e['edge_ci'][1]:+.3f}]"
                f"  P(>0) = {e['p_edge_positive']:.3f}",
                f"  book coef {e['book']:+.3f}  95% CI"
                f" [{e['book_ci'][0]:+.3f}, {e['book_ci'][1]:+.3f}]",
                "  (a book coef whose CI spans 0 means the sample is too small"
                " to score anything)"]

    out += ["", "lambda dynamic range: realized_total ~ a + b*lam_total"]
    if df["lam_total"].nunique() >= 3:  # a resampled fit needs distinct x's
        s = _lam_slope(df["lam_total"].to_numpy(), df["result_total"].to_numpy(),
                       boot, rng)
        out += [f"  slope {s['slope']:.3f}"
                f"  95% CI [{s['ci'][0]:.3f}, {s['ci'][1]:.3f}]"
                "   (1.0 = calibrated; >1 = model shrinks toward the league mean)"]
    else:
        out += ["  too few distinct lambdas to fit"]

    out += ["", "by line"] + _breakdown(df, "line", fmt=lambda v: f"{v:g}")
    out += ["", "by tier"] + _breakdown(df, "tier")
    out += ["", "by value flag"] + _breakdown(
        df, "value_flag", fmt=lambda v: "value" if v else "no value")
    out += ["", "roi = flat 1u on the model's side at the closing price,"
            " every row (not just picks)"]
    return "\n".join(out)


# ── 1x2 vs book ──────────────────────────────────────────────────────────────
#
# vs_book's question asked of the 1x2 head (the X12_SERVING.md "not done"
# item): does the served home/draw/away triple beat the last de-vigged 1x2
# price before kickoff? Priced population only by construction — schedule
# (gtl:) rows have no book side. The served batch is the one settlement
# grades (_served_x12); x12 rows exist only for model-covered fixtures, so
# there is no book-fallback exclusion to make. Scores are summed over the
# three outcomes; the edge regression runs on the DECISIVE SHARE
# s = p_home/(p_home+p_away), because that is the axis serving can move —
# the H2H stacker reshapes s and p_draw is served raw (docs/H2H_FEATURE.md).

_X12_VS_BOOK_QUERY = """
    SELECT p.p_home, p.p_draw, p.p_away, p.pick, p.confidence, p.value_flag,
           p.model_version, s.result, s.settled_at,
           (SELECT o.implied_prob FROM odds_snapshots o
             WHERE o.event_id = s.event_id AND o.market = '1x2'
               AND o.selection = 'home' AND o.fetched_at < f.start_time_utc
             ORDER BY o.fetched_at DESC LIMIT 1) AS b_home,
           (SELECT o.implied_prob FROM odds_snapshots o
             WHERE o.event_id = s.event_id AND o.market = '1x2'
               AND o.selection = 'draw' AND o.fetched_at < f.start_time_utc
             ORDER BY o.fetched_at DESC LIMIT 1) AS b_draw,
           (SELECT o.implied_prob FROM odds_snapshots o
             WHERE o.event_id = s.event_id AND o.market = '1x2'
               AND o.selection = 'away' AND o.fetched_at < f.start_time_utc
             ORDER BY o.fetched_at DESC LIMIT 1) AS b_away,
           (SELECT o.odds FROM odds_snapshots o
             WHERE o.event_id = s.event_id AND o.market = '1x2'
               AND o.selection = 'home' AND o.fetched_at < f.start_time_utc
             ORDER BY o.fetched_at DESC LIMIT 1) AS odds_home,
           (SELECT o.odds FROM odds_snapshots o
             WHERE o.event_id = s.event_id AND o.market = '1x2'
               AND o.selection = 'draw' AND o.fetched_at < f.start_time_utc
             ORDER BY o.fetched_at DESC LIMIT 1) AS odds_draw,
           (SELECT o.odds FROM odds_snapshots o
             WHERE o.event_id = s.event_id AND o.market = '1x2'
               AND o.selection = 'away' AND o.fetched_at < f.start_time_utc
             ORDER BY o.fetched_at DESC LIMIT 1) AS odds_away
    FROM settlements_x12 s
    JOIN fixtures f ON f.event_id = s.event_id
    JOIN predictions_x12 p ON p.event_id = s.event_id
     AND p.predicted_at = (SELECT MAX(predicted_at) FROM predictions_x12
                           WHERE event_id = s.event_id
                           AND predicted_at < f.start_time_utc)
    WHERE s.settled_at >= ?
"""

_X12_BOOK_COLS = ["b_home", "b_draw", "b_away",
                  "odds_home", "odds_draw", "odds_away"]


def _x12_row_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Per-row scores shared by the headline and every breakdown. Multiclass
    Brier = summed squared error over the three outcomes; hit/roi are on the
    model's argmax side (in practice never the draw at this league's λs)."""
    y = df["result"].map({"home": 0, "draw": 1, "away": 2}).to_numpy()
    m = df[["p_home", "p_draw", "p_away"]].to_numpy(dtype=float)
    b = df[["b_home", "b_draw", "b_away"]].to_numpy(dtype=float)
    o = df[["odds_home", "odds_draw", "odds_away"]].to_numpy(dtype=float)
    onehot = np.eye(3)[y]
    idx = np.arange(len(y))
    side = m.argmax(axis=1)
    df = df.copy()
    df["y_idx"] = y
    df["mb"] = ((m - onehot) ** 2).sum(axis=1)
    df["bb"] = ((b - onehot) ** 2).sum(axis=1)
    df["m_ll"] = -np.log(np.clip(m[idx, y], _PROB_EPS, None))
    df["b_ll"] = -np.log(np.clip(b[idx, y], _PROB_EPS, None))
    df["m_hit"] = (side == y).astype(float)
    df["b_hit"] = (b.argmax(axis=1) == y).astype(float)
    df["roi"] = np.where(side == y, o[idx, side] - 1.0, -1.0)
    return df


def _x12_breakdown(df: pd.DataFrame, key: str, fmt=str) -> list[str]:
    lines = [f"  {key:>10} {'n':>5} {'model':>8} {'book':>8} {'skill':>8}"
             f" {'hit':>7} {'roi':>8}"]
    for val, g in df.groupby(key, dropna=False, observed=True):
        bm, bb = g["mb"].mean(), g["bb"].mean()
        skill = 1 - bm / bb if bb > 0 else float("nan")
        label = "(no pick)" if pd.isna(val) else fmt(val)
        lines.append(f"  {label:>10} {len(g):>5} {bm:>8.4f} {bb:>8.4f}"
                     f" {skill:>+7.2%} {g['m_hit'].mean():>7.1%}"
                     f" {g['roi'].mean():>+7.2%}")
    return lines


def x12_vs_book(conn, days: int = 7, boot: int = 2000, seed: int = 0) -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    df = pd.read_sql_query(_X12_VS_BOOK_QUERY, conn, params=(since,))
    n_settled = len(df)
    df = df.dropna(subset=_X12_BOOK_COLS)
    if df.empty:
        return "no settled 1x2 predictions with a book close in window"
    df = _x12_row_scores(df)

    n = len(df)
    rng = np.random.default_rng(seed)
    ts = pd.to_datetime(df["settled_at"], format="ISO8601")
    span_days = max((ts.max() - ts.min()).total_seconds() / 86400.0, 1e-3)
    shares = np.array([(df["y_idx"] == k).mean() for k in range(3)])
    onehot = np.eye(3)[df["y_idx"].to_numpy()]
    base_brier = float(np.mean(((shares - onehot) ** 2).sum(axis=1)))
    base_ll = float(-np.mean(np.log(np.clip(shares[df["y_idx"]], _PROB_EPS,
                                            None))))

    out = [f"1x2 model vs book: {n} settled predictions in a {days}d window"
           f" | spanning {span_days:.1f}d"
           f" | {n_settled - n} settled without a 1x2 close (excluded)"
           f" | outcomes h/d/a {shares[0]:.1%}/{shares[1]:.1%}/{shares[2]:.1%}",
           "",
           f"  {'':<22}{'Brier(3)':>9} {'LogLoss':>9} {'argmax-hit':>11}",
           f"  {'model (as served)':<22}{df['mb'].mean():>9.4f}"
           f" {df['m_ll'].mean():>9.4f} {df['m_hit'].mean():>10.1%}",
           f"  {'book (de-vig close)':<22}{df['bb'].mean():>9.4f}"
           f" {df['b_ll'].mean():>9.4f} {df['b_hit'].mean():>10.1%}",
           f"  {'window base rates':<22}{base_brier:>9.4f}"
           f" {base_ll:>9.4f} {'—':>11}"]

    d = (df["mb"] - df["bb"]).to_numpy()
    draws = [d[rng.integers(0, n, n)].mean() for _ in range(boot)]
    lo, hi = _ci(draws)
    verdict = ("model better" if hi < 0 else "book better" if lo > 0
               else "NOT SIGNIFICANT")
    out += ["",
            f"paired Brier (model - book): {d.mean():+.5f}"
            f"  95% CI [{lo:+.5f}, {hi:+.5f}]  {verdict}"]
    if n >= 2:  # a 1-row window has no spread to size the sample from
        need = int(np.ceil((1.96 * d.std(ddof=1) / _BRIER_RESOLUTION) ** 2))
        eta = max(0.0, (need - n) / (n / span_days))
        out += [f"  resolving a +/-{_BRIER_RESOLUTION} gap needs ~{need}"
                f" settled predictions"
                f" (~{eta:.0f} more days at {n / span_days:.0f}/day)."]

    # Decisive share: the axis the head can actually move (the H2H stacker
    # reshapes s, p_draw is served raw), so the edge question is asked there.
    dec = df[df["result"] != "draw"]
    y_dec = (dec["result"] == "home").to_numpy().astype(int)
    if 0 < y_dec.sum() < len(dec):
        s_m = (dec["p_home"] / (dec["p_home"] + dec["p_away"])).to_numpy()
        s_b = (dec["b_home"] / (dec["b_home"] + dec["b_away"])).to_numpy()
        e = _edge_coefs(s_m, s_b, y_dec, boot, rng)
        out += ["",
                f"does the model add anything to the price? (decisive share,"
                f" {len(dec)} decisive rows)",
                "  y ~ logit(s_book) + [logit(s_model) - logit(s_book)]",
                f"  edge coef {e['edge']:+.3f}  95% CI"
                f" [{e['edge_ci'][0]:+.3f}, {e['edge_ci'][1]:+.3f}]"
                f"  P(>0) = {e['p_edge_positive']:.3f}",
                f"  book coef {e['book']:+.3f}  95% CI"
                f" [{e['book_ci'][0]:+.3f}, {e['book_ci'][1]:+.3f}]",
                "  (a book coef whose CI spans 0 means the sample is too small"
                " to score anything)"]

    y_draw = (df["result"] == "draw").to_numpy().astype(int)
    out += ["",
            "draw head (served raw — no stacker, no recal):",
            f"  Brier model p_draw {_brier(df['p_draw'].to_numpy(), y_draw):.4f}"
            f"  vs book p_draw {_brier(df['b_draw'].to_numpy(), y_draw):.4f}"
            f"  | realized draw rate {y_draw.mean():.1%}"
            f" | mean served {df['p_draw'].mean():.1%}"
            f" / book {df['b_draw'].mean():.1%}"]

    df["h2h"] = np.where(df["model_version"].str.contains("-h2h"),
                         "h2h", "pre-h2h")
    out += ["", "by pick"] + _x12_breakdown(df, "pick")
    out += ["", "by value flag"] + _x12_breakdown(
        df, "value_flag", fmt=lambda v: "value" if v else "no value")
    out += ["", "by h2h regime (rows the stacker touched carry -h2h)"] \
        + _x12_breakdown(df, "h2h")
    out += ["", "roi = flat 1u on the model's argmax at the closing price,"
            " every row (not just picks)"]
    return "\n".join(out)


# ── tier re-quantile ─────────────────────────────────────────────────────────
#
# Tier bands must be quantiled against the confidence distribution the CURRENT
# serving config actually produces. Two things shift it: Platt maps engaging
# (sharpens, pushes confidence right) and club (widens λ). Quantiles taken from
# the walk-forward eval frame are NOT transferable — that frame prices fixed
# lines 2.5/3.5/4.5, while the book lists lines near each match's expected
# total, which is a far harder and much less confident problem. Eval-derived
# bands put `strong` at 0.87 when served confidence never exceeds ~0.67.

TIER_TARGET_SHARES = (0.47, 0.37, 0.17)  # lean / solid / strong, as designed
TIER_MIN_PICKS = 500  # below this the tail quantiles are noise

# The two trailing predicates exclude pre-2026-07-10 rows whose `confidence`
# does not mean what it means today: early batches blended the book's prob into
# it, and older ones assigned a tier below the pick gate. Neither bumped
# model_version, so semantics — not the tag — is the only way to tell them
# apart. Quantiling over them would drag every band down.
_TIER_QUERY = """
    SELECT p.line, p.pick, p.confidence, p.tier, p.model_version, s.pick_correct
    FROM predictions p
    JOIN fixtures f ON f.event_id = p.event_id
    LEFT JOIN settlements s ON s.event_id = p.event_id
    WHERE p.predicted_at = (SELECT MAX(predicted_at) FROM predictions
                            WHERE event_id = p.event_id
                            AND predicted_at < f.start_time_utc)
      AND p.lambda_home IS NOT NULL AND p.pick IS NOT NULL
      AND f.start_time_utc >= ?
      AND p.confidence >= ?
      AND ABS(p.confidence - MAX(p.p_over, p.p_under)) < 1e-6
"""

# Schedule population: served batch defined against the match's own kickoff.
# Post-Phase-2 the priced bands are near-vacuous (picks rare by design), so
# the schedule section is the one that actually drives TIER_* proposals.
_TIER_QUERY_SCHEDULE = """
    SELECT p.line, p.pick, p.confidence, p.tier, p.model_version, s.pick_correct
    FROM predictions p
    JOIN matches m ON 'gtl:' || m.source_match_id = p.event_id
    LEFT JOIN settlements s ON s.event_id = p.event_id
    WHERE p.predicted_at = (SELECT MAX(predicted_at) FROM predictions
                            WHERE event_id = p.event_id
                            AND predicted_at < m.kickoff_ts)
      AND p.lambda_home IS NOT NULL AND p.pick IS NOT NULL
      AND m.kickoff_ts >= ?
      AND p.confidence >= ?
      AND ABS(p.confidence - MAX(p.p_over, p.p_under)) < 1e-6
"""

_TIER_QUERIES = (("book-priced", _TIER_QUERY),
                 ("schedule (model-only)", _TIER_QUERY_SCHEDULE))


def tier_bands(conn, days: int = 14, min_picks: int = TIER_MIN_PICKS) -> str:
    """Propose TIER_SOLID / TIER_STRONG from the served confidence
    distribution, one section per population (never pooled — the config
    bands apply to both populations, so read the proposal from the
    population you are actually tuning for).

    Refuses to recommend on thin data, and refuses to propose a band that the
    live model cannot reach — an unreachable `strong` silently retires the tier
    rather than failing, which is the exact shape of bug the tier bands already
    hit once (a band below the pick gate).
    """
    s = settings()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out: list[str] = []
    for label, q in _TIER_QUERIES:
        df = pd.read_sql_query(q, conn, params=(since, s.pick_prob_threshold))
        out.append(f"== {label} ==")
        out += _tier_section(df, s, days, min_picks) if not df.empty \
            else ["no served picks in window"]
        out.append("")
    return "\n".join(out).rstrip()


def _tier_section(df: pd.DataFrame, s, days: int, min_picks: int) -> list[str]:
    out = [f"served picks: {len(df)} in {days}d"
           f" | recal active on {df['model_version'].str.contains('-recal').mean():.0%}"
           f" | club active on {df['model_version'].str.contains('-club').mean():.0%}",
           f"confidence range {df.confidence.min():.4f}..{df.confidence.max():.4f}"]

    graded = df[df["pick_correct"].notna()]
    out += ["", f"current bands {s.tier_lean}/{s.tier_solid}/{s.tier_strong}:"]
    for tier in ("lean", "solid", "strong"):
        t, g = df[df.tier == tier], graded[graded.tier == tier]
        hit = f"{g['pick_correct'].mean():.1%}" if len(g) else "—"
        out.append(f"  {tier:>6}: {len(t):>5} picks ({len(t) / len(df):>5.1%})"
                   f"  graded {len(g):>4}  hit {hit}")

    if len(df) < min_picks:
        out += ["", f"REFUSING to propose bands: {len(df)} picks < {min_picks}."
                    " Tail quantiles are noise below that."]
        return out

    lean, solid, strong = TIER_TARGET_SHARES
    q_solid = float(np.quantile(df.confidence, lean))
    q_strong = float(np.quantile(df.confidence, lean + solid))
    out += ["", f"bands preserving {lean:.0%}/{solid:.0%}/{strong:.0%}:",
            f"  TIER_LEAN={s.pick_prob_threshold:g}"
            f"  TIER_SOLID={q_solid:.4f}  TIER_STRONG={q_strong:.4f}"]

    if not (s.pick_prob_threshold < q_solid < q_strong < df.confidence.max()):
        out += ["  WARNING: proposed bands are degenerate against the observed"
                " confidence range — do not apply."]
        return out

    if len(graded):
        bands = [("lean", s.pick_prob_threshold, q_solid),
                 ("solid", q_solid, q_strong), ("strong", q_strong, 1.01)]
        out += ["", "hit rate under the proposed bands (graded picks only):"]
        for name, lo, hi in bands:
            g = graded[(graded.confidence >= lo) & (graded.confidence < hi)]
            hit = f"{g['pick_correct'].mean():.1%}" if len(g) else "—"
            out.append(f"  {name:>6}: {len(g):>4} graded  hit {hit}")
        out += ["  (tiers must be monotone in hit rate; if they are not, the"
                " confidence scale is broken, not the bands)"]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="settlement.settle")
    ap.add_argument("cmd", choices=["run", "scorecard", "vs-book",
                                    "x12-vs-book", "tiers"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--boot", type=int, default=2000,
                    help="bootstrap resamples for vs-book confidence intervals")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    s = settings()
    db_path = Path(args.db) if args.db else s.gtl_db_path
    conn = connect(db_path)

    if args.cmd == "run":
        rep = run(conn, db_path)
        print(f"settled={rep['settled']} pending={rep['pending']} "
              f"candidates={rep['candidates']} "
              f"x12_settled={rep.get('x12_settled', 0)} "
              f"x12_pending={rep.get('x12_pending', 0)} "
              f"x12_sched_settled={rep.get('x12_sched_settled', 0)} "
              f"x12_sched_pending={rep.get('x12_sched_pending', 0)} "
              f"schedule_settled={rep.get('schedule_settled', 0)} "
              f"schedule_pending={rep.get('schedule_pending', 0)}")
        leaks = conn.execute(
            "SELECT COUNT(*) c FROM settlements WHERE leak_risk = 1").fetchone()["c"]
        if leaks:
            print(f"WARNING: {leaks} settlements flagged leak_risk", file=sys.stderr)
            return 2
        return 0
    if args.cmd == "vs-book":
        print(vs_book(conn, args.days, args.boot, args.seed))
        return 0
    if args.cmd == "x12-vs-book":
        print(x12_vs_book(conn, args.days, args.boot, args.seed))
        return 0
    if args.cmd == "tiers":
        print(tier_bands(conn, args.days))
        return 0
    print(scorecard(conn, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
