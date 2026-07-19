"""Pick screen: a rule-based veto layer over served picks (docs/PICK_SCREEN.md).

Deterministic cascade, no fitting anywhere — the rules are static and the
*inputs* are rolling stats recomputed from settlements each cycle, which is
the entire sense in which the screen is dynamic. The screen ANNOTATES
(screen_pass/screen_reason); it never touches pick/tier/confidence, so
settlement regen keeps grading the unscreened pick.

Doctrine the stats fit inherits:
- Counterfactual basis: hit rates are computed over ALL settled rows (the
  model's lean graded against result_total), never over surfaced picks —
  a screen fed only its own survivors judges itself through its own veto,
  the recal 2026-07-13 loop in rule form. The one exception is `trailing`,
  which deliberately measures the served product (pick_correct) because the
  drawdown breaker asks "how are the picks we actually surfaced doing".
- Conditioning basis: lean and edge are read from the STORED (served)
  probabilities, because that is exactly the quantity `apply_*` sees at
  serving time. This is safe where the recal fit basis was not: nothing here
  transforms a probability, so there is no output to consume.
- Populations never pooled (docs/POPULATION_SPLIT.md); book-dependent stats
  simply do not exist for the schedule population and the rules that need
  them self-skip.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# Edge bands (probe 2026-07-18, docs/PICK_SCREEN.md — boundaries provisional;
# the cold_band rule re-reads each band's rolling hit every cycle, so a
# mis-drawn boundary costs accuracy of the stats, not correctness).
# edge = model_p(pick) - book_p(pick); "agree" = the book likes our side at
# least as much as we do.
EDGE_BANDS = (
    ("agree", None, 0.0),
    ("noise", 0.0, 0.05),
    ("value", 0.05, 0.12),
    ("stretch", 0.12, 0.20),
    ("absurd", 0.20, None),
)

REASONS_OU = ("book_opposed", "cold_line", "cold_band", "drawdown")
REASONS_X12 = ("book_opposed", "cold_band", "drawdown")


def edge_band(edge: float) -> str:
    for name, lo, hi in EDGE_BANDS:
        if (lo is None or edge > lo) and (hi is None or edge <= hi):
            return name
    return "agree"  # unreachable; bands tile the line


@dataclass
class ScreenStats:
    """Rolling stats for one (population, market). Each entry is (hits, n).

    Empty dicts / (0, 0) are the cold start and mean every rule self-skips —
    the screen passes everything until the data says otherwise.
    """

    population: str = "priced"
    market: str = "ou"  # ou | x12
    line_hit: dict[float, tuple[int, int]] = field(default_factory=dict)
    band_hit: dict[str, tuple[int, int]] = field(default_factory=dict)
    trailing: tuple[int, int] = (0, 0)


def _rate(hn: tuple[int, int]) -> float:
    return hn[0] / hn[1] if hn[1] else float("nan")


# ── the cascade ──────────────────────────────────────────────────────────────
#
# Ordered; the first veto wins and becomes screen_reason (one reason per row
# keeps the report legible). Every rule is a veto — the book never boosts a
# pick, it only stands one down (the value_flag comment in cycle._line_row:
# a price's information enters where a price belongs).


def apply_ou(line: float, confidence: float, tier: str | None,
             book_p: float | None, stats: ScreenStats, s,
             ) -> tuple[int, str | None]:
    """Screen one picked O/U line. `book_p` is the book's implied prob of the
    PICKED side (None for schedule rows / missing price). Returns
    (screen_pass, screen_reason)."""
    if book_p is not None and book_p < 0.50 \
            and confidence < s.screen_override_conf:
        return 0, "book_opposed"
    if _cold_line(stats, s, line):
        return 0, "cold_line"
    if book_p is not None and _cold_band(confidence - book_p, stats, s):
        return 0, "cold_band"
    if tier != "strong" and _drawdown(stats, s):
        return 0, "drawdown"
    return 1, None


def apply_x12(confidence: float, book: dict[str, float] | None, pick: str,
              stats: ScreenStats, s) -> tuple[int, str | None]:
    """Screen one picked 1x2 row. `book` maps outcome -> implied prob (None
    or partial for unpriced rows; a partial book cannot name an argmax and
    skips the opposition rule). No line rule (no lines) and no tier
    exemption on the breaker (x12 has no tiers)."""
    full = book is not None and len(book) == 3 and None not in book.values()
    if full and max(book, key=book.get) != pick \
            and confidence < s.screen_x12_override_conf:
        return 0, "book_opposed"
    if full and _cold_band(confidence - book[pick], stats, s):
        return 0, "cold_band"
    if _drawdown(stats, s):
        return 0, "drawdown"
    return 1, None


def _cold_line(stats: ScreenStats, s, line: float) -> bool:
    hn = stats.line_hit.get(float(line))
    return hn is not None and hn[1] >= s.screen_min_n_line \
        and _rate(hn) < s.screen_line_floor


def _cold_band(edge: float, stats: ScreenStats, s) -> bool:
    hn = stats.band_hit.get(edge_band(edge))
    return hn is not None and hn[1] >= s.screen_min_n_band \
        and _rate(hn) < s.screen_band_floor


def _drawdown(stats: ScreenStats, s) -> bool:
    hits, n = stats.trailing
    return n >= s.screen_trailing_n and hits / n < s.screen_breaker_floor


# ── rolling stats ────────────────────────────────────────────────────────────
#
# Same joins as recal's fit queries / settle's _VS_BOOK_QUERY: the last
# pre-kickoff batch of each settled event, leak-clean, model-covered. The
# totals queries fan out over the batch's lines — every line is its own
# (line, edge, outcome) observation, which is what gives thin lines and thin
# bands enough sample to ever clear their floors.

_OU_QUERY = """
    SELECT p.line, p.p_over, p.p_under, s.result_total,
           (SELECT o.implied_prob FROM odds_snapshots o
             WHERE o.event_id = s.event_id AND o.market = 'ou'
               AND o.line = p.line AND o.selection = 'over'
               AND o.fetched_at < f.start_time_utc
             ORDER BY o.fetched_at DESC LIMIT 1) AS book_over
    FROM settlements s
    JOIN fixtures f ON f.event_id = s.event_id
    JOIN predictions p ON p.event_id = s.event_id
     AND p.predicted_at = (SELECT MAX(predicted_at) FROM predictions
                           WHERE event_id = s.event_id
                           AND predicted_at < f.start_time_utc)
    WHERE s.result_total IS NOT NULL AND s.leak_risk = 0
      AND s.settled_at >= ? AND p.lambda_home IS NOT NULL
"""

# Schedule rows have no odds; the book column is omitted rather than a
# guaranteed-NULL subquery so the query cannot silently pool a book in later.
_OU_QUERY_SCHEDULE = """
    SELECT p.line, p.p_over, p.p_under, s.result_total, NULL AS book_over
    FROM settlements s
    JOIN matches m ON m.id = s.matched_match_id
    JOIN predictions p ON p.event_id = s.event_id
     AND p.predicted_at = (SELECT MAX(predicted_at) FROM predictions
                           WHERE event_id = s.event_id
                           AND predicted_at < m.kickoff_ts)
    WHERE s.event_id LIKE 'gtl:%' AND s.result_total IS NOT NULL
      AND s.leak_risk = 0 AND s.settled_at >= ? AND p.lambda_home IS NOT NULL
"""

_X12_QUERY = """
    SELECT p.p_home, p.p_draw, p.p_away, s.result,
           (SELECT o.implied_prob FROM odds_snapshots o
             WHERE o.event_id = s.event_id AND o.market = '1x2'
               AND o.selection = 'home' AND o.fetched_at < f.start_time_utc
             ORDER BY o.fetched_at DESC LIMIT 1) AS b_home,
           (SELECT o.implied_prob FROM odds_snapshots o
             WHERE o.event_id = s.event_id AND o.market = '1x2'
               AND o.selection = 'away' AND o.fetched_at < f.start_time_utc
             ORDER BY o.fetched_at DESC LIMIT 1) AS b_away
    FROM settlements_x12 s
    JOIN fixtures f ON f.event_id = s.event_id
    JOIN predictions_x12 p ON p.event_id = s.event_id
     AND p.predicted_at = (SELECT MAX(predicted_at) FROM predictions_x12
                           WHERE event_id = s.event_id
                           AND predicted_at < f.start_time_utc)
    WHERE s.settled_at >= ?
"""

# trailing served-pick hit: settlements already hold the headline grade
# (pick_correct is the highest-confidence picked line of the graded batch),
# so no predictions join is needed — just the population filter.
_TRAILING = {
    ("priced", "ou"): """
        SELECT pick_correct FROM settlements
        WHERE pick_correct IS NOT NULL AND leak_risk = 0
          AND event_id NOT LIKE 'gtl:%'
        ORDER BY settled_at DESC LIMIT ?""",
    ("schedule", "ou"): """
        SELECT pick_correct FROM settlements
        WHERE pick_correct IS NOT NULL AND leak_risk = 0
          AND event_id LIKE 'gtl:%'
        ORDER BY settled_at DESC LIMIT ?""",
    ("priced", "x12"): """
        SELECT pick_correct FROM settlements_x12
        WHERE pick_correct IS NOT NULL AND event_id NOT LIKE 'gtl:%'
        ORDER BY settled_at DESC LIMIT ?""",
    ("schedule", "x12"): """
        SELECT pick_correct FROM settlements_x12
        WHERE pick_correct IS NOT NULL AND event_id LIKE 'gtl:%'
        ORDER BY settled_at DESC LIMIT ?""",
}


def _tally(acc: dict, key, hit: bool) -> None:
    h, n = acc.get(key, (0, 0))
    acc[key] = (h + int(hit), n + 1)


def _trailing(conn, population: str, market: str, s) -> tuple[int, int]:
    rows = conn.execute(_TRAILING[(population, market)],
                        (s.screen_trailing_n,)).fetchall()
    return sum(r["pick_correct"] for r in rows), len(rows)


def fit_ou(conn: sqlite3.Connection, s, population: str = "priced",
           now: datetime | None = None) -> ScreenStats:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=s.screen_days)).isoformat()
    q = _OU_QUERY if population == "priced" else _OU_QUERY_SCHEDULE
    line_hit: dict[float, tuple[int, int]] = {}
    band_hit: dict[str, tuple[int, int]] = {}
    for r in conn.execute(q, (since,)):
        line = float(r["line"])
        if r["result_total"] == line:
            continue  # integer-line push: no counterfactual grade
        over = r["p_over"] > r["p_under"]
        hit = (r["result_total"] > line) == over
        _tally(line_hit, line, hit)
        if r["book_over"] is not None:
            model_p = r["p_over"] if over else r["p_under"]
            book_p = r["book_over"] if over else 1.0 - r["book_over"]
            _tally(band_hit, edge_band(model_p - book_p), hit)
    return ScreenStats(population, "ou", line_hit, band_hit,
                       _trailing(conn, population, "ou", s))


def fit_x12(conn: sqlite3.Connection, s, now: datetime | None = None,
            ) -> ScreenStats:
    """Priced only: the band rule is the only counterfactual consumer and it
    needs a book, which the schedule population never has — its stats reduce
    to `trailing` (see fit_all)."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=s.screen_days)).isoformat()
    band_hit: dict[str, tuple[int, int]] = {}
    for r in conn.execute(_X12_QUERY, (since,)):
        probs = {"home": r["p_home"], "draw": r["p_draw"], "away": r["p_away"]}
        side = max(probs, key=probs.get)
        # draw close is not selected: the argmax is never the draw at this
        # league's λs (docs/X12_SERVING.md) and two subqueries stay cheaper
        book = {"home": r["b_home"], "away": r["b_away"]}.get(side)
        if book is None:
            continue
        _tally(band_hit, edge_band(probs[side] - book), side == r["result"])
    return ScreenStats("priced", "x12", {}, band_hit,
                       _trailing(conn, "priced", "x12", s))


def fit_all(conn: sqlite3.Connection, s, now: datetime | None = None,
            ) -> dict[tuple[str, str], ScreenStats]:
    """The four stats objects one predictor cycle serves from."""
    return {
        ("priced", "ou"): fit_ou(conn, s, "priced", now),
        ("schedule", "ou"): fit_ou(conn, s, "schedule", now),
        ("priced", "x12"): fit_x12(conn, s, now),
        ("schedule", "x12"): ScreenStats(
            "schedule", "x12", trailing=_trailing(conn, "schedule", "x12", s)),
    }
