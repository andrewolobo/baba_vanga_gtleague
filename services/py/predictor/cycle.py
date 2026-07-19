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
from pathlib import Path

import pandas as pd

from core.config import OFFSET_TOL_MIN, settings
from core.markets import x12_probs
from core.schema import PredictionRow, X12PredictionRow
from model import data, h2h, recal, registry, screen
from model.blend import PMF_MAX_GOALS, blend_sides, totals_probs
from model.heuristic import FormIndex
from store import aliases
from store.db import connect
from store.repo import PredictionRepo, X12PredictionRepo


def _alias(conn, name: str | None) -> str | None:
    if name is None:
        return None
    row = conn.execute("SELECT player FROM player_aliases WHERE book_name = ?",
                       (name,)).fetchone()
    return row["player"] if row else name


def _latest_odds(conn, event_ids: list[str]) -> dict[str, dict]:
    """Latest snapshot batch per event ->
    {'ou': {line: {sel: (odds, implied)}}, 'x12': {sel: (odds, implied)}}"""
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
        ev = out.setdefault(r["event_id"], {"ou": {}, "x12": {}})
        if r["market"] == "ou" and r["line"] is not None:
            ev["ou"].setdefault(float(r["line"]), {})[r["selection"]] = (
                r["odds"], r["implied_prob"])
        elif r["market"] == "1x2":
            ev["x12"][r["selection"]] = (r["odds"], r["implied_prob"])
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
    """(lam_h, lam_a) per the served totals source, or (None, None) uncovered.

    Coverage is a statement about *players* only. An unknown club has catt =
    cdfn = 0 and silently yields the player-only λ, so passing clubs is safe
    whether or not pm was fitted with them — and it never suppresses a pick.
    The kickoff hour is passed the same way: against a model fitted without
    the tod block it is a strict no-op (docs/TOD_FEATURE.md), so neither
    feature flag changes this call site.
    """
    hp, ap = f["home_player"], f["away_player"]
    if hp not in pm.players or ap not in pm.players:
        return None, None
    kick = datetime.fromisoformat(f["start_time_utc"])
    lam_p = pm.predict_sides(hp, ap, f["home_club"], f["away_club"],
                             hour=kick.hour + kick.minute / 60.0)
    cut64 = pd.Timestamp(cutoff).tz_convert("UTC").tz_localize(None).to_datetime64()
    lam_f = (form_idx.side_lambda(hp, ap, cut64, fallback),
             form_idx.side_lambda(ap, hp, cut64, fallback))
    if s.totals_source == "blend":
        return blend_sides(lam_p, lam_f, s.totals_blend_weight)
    return lam_p


def _row_version(version: str, maps, line) -> str:
    """'-recal2' marks rows a map actually touched — never the whole batch.
    Populations diverge here: schedule rows pass identity maps and stay
    untagged (docs/POPULATION_SPLIT.md Phase 0).

    The 2 is the fit generation: plain '-recal' rows (2026-07-11..07-13)
    were priced through maps fit on served p_over — the closed loop
    docs/RECAL_SERVING.md 2026-07-13 documents. Settlement regen matches
    on the '-recal' substring, so both generations regen through the
    current maps; analytics can split generations on the exact tag.

    A pace-extended (a, b, c) map tags '-recal2-h2h' (docs/TOTALS_H2H.md):
    the meaning of the map changed, so the tag changed — and regen routes
    the extended shape on the '-h2h' substring one level deeper."""
    ab = maps.get(float(line))
    if ab is None:
        return version
    return version + ("-recal2-h2h" if len(ab) == 3 else "-recal2")


def _line_row(f, line, sels, lam_h, lam_a, now, cutoff, version, s,
              maps, pace: float = 0.0, scr=None) -> PredictionRow:
    """`pace` is the pair's pace_decay at the ROW'S OWN cutoff (not kickoff —
    pairs rematch between an early batch and kickoff; the x12 rule). Consumed
    only by pace-extended maps (docs/TOTALS_H2H.md); a plain map ignores it.
    `scr` is this population's ScreenStats or None (screen off) — the screen
    annotates picked rows and never alters the pick (docs/PICK_SCREEN.md)."""
    book_over, book_under = sels["over"][1], sels["under"][1]
    covered = lam_h is not None

    if covered:
        p_over, p_push, p_under = totals_probs(lam_h, lam_a, line)
        p_over, p_push, p_under = recal.apply_to_line(maps, line, p_over,
                                                      p_push, pace)
        source = s.totals_source
        version = _row_version(version, maps, line)
    else:  # book fallback: no pick without model coverage
        p_over, p_push, p_under = book_over or 0.5, 0.0, book_under or 0.5
        source = "book"

    pick = confidence = tier = None
    value = False
    if covered:
        sel = "over" if p_over > p_under else "under"
        model_p = p_over if sel == "over" else p_under
        # Confidence is the model's own probability, never blended with the
        # book's. The book prices the main line at ~0.5 by construction, so
        # averaging it in only shrinks every book-priced row halfway to a
        # coin flip and demotes its tier relative to an identical
        # schedule-only row. The book's information enters through value_flag,
        # which is where a price belongs.
        confidence = round(model_p, 6)
        # EV-gated when engaged (docs/VALUE_FLAG.md), legacy edge rule
        # otherwise; the '-ev' tag marks which semantics this row carries.
        # book basis is the leaned side's UN-defaulted implied prob — the
        # gate must see "no price" as no price, not as prob 0.
        value, ev_engaged = screen.value_ev(
            model_p, book_over if sel == "over" else book_under,
            (sels["over"] if sel == "over" else sels["under"])[0],
            scr, s)
        if ev_engaged:
            version = version + "-ev"
        # a pick is only surfaced above the parent-validated probability gate
        # (0.60) — near-coin-flip calls stay pick-less.
        # The maps clause is priced-population doctrine (docs/POPULATION_SPLIT.md):
        # once this population's maps are engaged, a line WITHOUT a map has too
        # few graded samples to know its correction, and raw confidence there
        # is exactly the adverse-selection surface — measured 2026-07-13, the
        # residual priced picks leaking through unmapped 6.5 hit 47.6%. With
        # maps empty (cold start / RECAL_ENABLED=false) every line picks as
        # before: the clause never changes rollback behavior.
        if confidence >= s.pick_prob_threshold and p_push <= s.max_push_prob \
                and (not maps or float(line) in maps):
            pick, tier = sel, _tier(confidence, s)

    s_pass = s_reason = None
    if pick is not None and scr is not None:
        # the screen's book basis is the picked side's implied prob, None
        # when the book never priced it (unlike value_ev's legacy-path
        # `or 0.0`: a missing price must skip the book rules, not read as
        # opposition)
        s_pass, s_reason = screen.apply_ou(
            line, confidence, tier,
            book_over if pick == "over" else book_under, scr, s)

    return PredictionRow(
        event_id=f["event_id"], predicted_at=now, totals_source=source,
        line=line, p_over=round(p_over, 6), p_push=round(p_push, 6),
        p_under=round(p_under, 6),
        lambda_home=round(lam_h, 4) if covered else None,
        lambda_away=round(lam_a, 4) if covered else None,
        pick=pick, confidence=confidence, tier=tier, value_flag=value,
        screen_pass=s_pass, screen_reason=s_reason,
        model_version=version, as_of_cutoff_ts=cutoff,
    )


def _x12_row(f, lam_h, lam_a, x12_odds, now, cutoff, version, s,
             stack=None, h2h_idx=None, scr=None) -> X12PredictionRow:
    """One 1x2 row per covered fixture, priced off the same served λs as the
    totals rows (docs/X12_SERVING.md). The gate is x12_pick_prob_threshold
    (0.50, measured), NOT the totals 0.60 — max-of-three lives on a different
    scale and the totals gate would surface ~1% of matches. In practice the
    argmax is never the draw at these λs, so picks are home/away.

    `stack` must be this population's own H2H stacker or None
    (docs/H2H_FEATURE.md; the recal population rule). It re-splits only the
    decisive mass at the row's own cutoff — features at `cutoff`, not
    kickoff: a pair can rematch between an early batch and kickoff, and an
    early row must not see that meeting. p_draw is untouched. Rows a stacker
    touched carry '-h2h', never the whole batch (the '-recal' convention);
    the pick gate reads the restacked probs — that is the point."""
    p_home, p_draw, p_away = x12_probs(lam_h, lam_a, PMF_MAX_GOALS)
    if stack is not None:
        cut64 = pd.Timestamp(cutoff).tz_convert("UTC").tz_localize(None) \
            .to_datetime64()
        feats = h2h_idx.features(f["home_player"], f["away_player"], cut64)
        s_new = stack.apply_one(p_home / max(p_home + p_away, 1e-12), feats)
        p_home, p_draw, p_away = h2h.restack_x12(s_new, p_draw)
        version = version + "-h2h"
    probs = {"home": p_home, "draw": p_draw, "away": p_away}
    sel = max(probs, key=probs.get)
    pick = confidence = None
    value = False
    if probs[sel] >= s.x12_pick_prob_threshold:
        pick, confidence = sel, round(probs[sel], 6)
        book_odds, book_p = x12_odds.get(sel) or (None, None)
        if book_p is not None:
            # EV-gated when engaged (docs/VALUE_FLAG.md) — same helper and
            # '-ev' contract as the totals rows; unpriced (schedule) rows
            # keep value False without consulting the gate, as before
            value, ev_engaged = screen.value_ev(probs[sel], book_p,
                                                book_odds, scr, s)
            if ev_engaged:
                version = version + "-ev"
    s_pass = s_reason = None
    if pick is not None and scr is not None:
        book = {k: (x12_odds.get(k) or (None, None))[1]
                for k in ("home", "draw", "away")} if x12_odds else None
        s_pass, s_reason = screen.apply_x12(confidence, book, pick, scr, s)
    return X12PredictionRow(
        event_id=f["event_id"], predicted_at=now, p_home=round(p_home, 6),
        p_draw=round(p_draw, 6), p_away=round(p_away, 6),
        lambda_home=round(lam_h, 4), lambda_away=round(lam_a, 4),
        pick=pick, confidence=confidence, value_flag=value,
        screen_pass=s_pass, screen_reason=s_reason,
        model_version=version, as_of_cutoff_ts=cutoff,
    )


SCHEDULE_PREFIX = "gtl:"  # predictions for league-scheduled games without odds
SCHEDULE_LINES_AROUND = (-0.5, +0.5)  # canonical half-lines straddling E[total]


def _pace_at(h2h_idx, f, cutoff) -> float:
    """The pair's pace_decay at a fixture's own cutoff, 0.0 without an index
    (flag off) — the value _line_row/_schedule_row hand to apply_to_line."""
    if h2h_idx is None:
        return 0.0
    cut64 = pd.Timestamp(cutoff).tz_convert("UTC").tz_localize(None) \
        .to_datetime64()
    return h2h_idx.features(f["home_player"], f["away_player"],
                            cut64)["pace_decay"]


def _schedule_row(f, line, lam_h, lam_a, now, cutoff, version, s,
                  maps, pace: float = 0.0, scr=None) -> PredictionRow:
    """Model-only prediction: same confidence scale as a priced row, but
    value_flag stays false (value needs a price to beat).

    `maps` must be this population's own maps — never the priced-population
    fit. The two populations do not share a calibration curve (priced
    a ≈ 0.14 vs schedule a ≈ 1.30, docs/POPULATION_SPLIT.md); priced maps
    applied here would suppress the model's best picks. `pace` as in
    _line_row: the pair's pace_decay at the row's own cutoff."""
    p_over, p_push, p_under = totals_probs(lam_h, lam_a, line)
    p_over, p_push, p_under = recal.apply_to_line(maps, line, p_over,
                                                  p_push, pace)
    version = _row_version(version, maps, line)
    sel = "over" if p_over > p_under else "under"
    confidence = round(p_over if sel == "over" else p_under, 6)
    pick = tier = None
    if confidence >= s.pick_prob_threshold and p_push <= s.max_push_prob:
        pick, tier = sel, _tier(confidence, s)
    s_pass = s_reason = None
    if pick is not None and scr is not None:
        # no book on this population: the book rules self-skip on book_p=None
        s_pass, s_reason = screen.apply_ou(line, confidence, tier, None,
                                           scr, s)
    return PredictionRow(
        event_id=f["event_id"], predicted_at=now, totals_source=s.totals_source,
        line=line, p_over=round(p_over, 6), p_push=round(p_push, 6),
        p_under=round(p_under, 6), lambda_home=round(lam_h, 4),
        lambda_away=round(lam_a, 4), pick=pick, confidence=confidence,
        tier=tier, value_flag=False, screen_pass=s_pass,
        screen_reason=s_reason, model_version=version,
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
                              half_life=s.half_life_days, alpha=s.poisson_alpha,
                              with_club=s.club_enabled,
                              with_tod=s.tod_enabled)
    club_map = aliases.club_alias_map(conn) if s.club_enabled else {}
    df = data.load_matches(conn)
    recent = df[df["kickoff_ts"] >= pd.Timestamp(now)
                - pd.Timedelta(days=s.form_window_days)]
    form_idx = FormIndex(data.long_format(recent), span=s.form_span)
    fallback = float(recent["home_ft"].add(recent["away_ft"]).mean() / 2) \
        if len(recent) else float("nan")

    # One H2HIndex serves both heads (features are cutoff-addressed): the
    # x12 stackers (docs/H2H_FEATURE.md) and the totals pace maps
    # (docs/TOTALS_H2H.md). totals_h2h requires recal — the pace term rides
    # the recal map, so RECAL_ENABLED=false overrides the flag.
    x12_h2h_on = s.x12_enabled and s.x12_h2h_enabled
    totals_h2h_on = s.totals_h2h_enabled and s.recal_enabled
    h2h_idx = h2h_stack = h2h_stack_sched = None
    if x12_h2h_on or totals_h2h_on:
        h2h_idx = h2h.H2HIndex(df)

    # per-line Platt maps from settled predictions; {} (identity) until a
    # line accrues recal_min_n graded samples — or recal_min_n_line for the
    # shared-slope hierarchical tier. See docs/RECAL_SERVING.md.
    # Fit per population, never pooled (docs/POPULATION_SPLIT.md: priced
    # a ≈ 0.14 vs schedule a ≈ 1.30 — no shared calibration curve). Same
    # knobs for both; the schedule pool accrues ~3× faster so it engages
    # sooner on its own. With totals_h2h on, every engaged line fits the
    # pace-extended (a, b, c) shape — one map set per population, no
    # plain/extended mixture inside one (docs/TOTALS_H2H.md).
    totals_idx = h2h_idx if totals_h2h_on else None
    maps = (recal.fit_line_maps(conn, s.recal_days, s.recal_min_n,
                                s.recal_min_n_line, now=now,
                                h2h_idx=totals_idx)
            if s.recal_enabled else {})
    maps_sched = (recal.fit_line_maps(conn, s.recal_days, s.recal_min_n,
                                      s.recal_min_n_line, now=now,
                                      population="schedule",
                                      h2h_idx=totals_idx)
                  if s.recal_enabled else {})

    # Pick screen rolling stats (docs/PICK_SCREEN.md): four objects, one per
    # (population, market), refit each cycle from settlements like the recal
    # maps. None (screen off) leaves screen_pass/screen_reason NULL — the
    # one-flag rollback. Cold-start stats pass everything (default-open).
    scr = screen.fit_all(conn, s, now=now) if s.screen_enabled else {}
    scr_get = scr.get  # {} -> every lookup None -> row builders skip

    # H2H stacker on the 1x2 head (docs/H2H_FEATURE.md). Per population,
    # never pooled; None (identity, no '-h2h' tag) until a population has
    # x12_h2h_min_n decisive graded rows.
    if x12_h2h_on:
        h2h_stack = h2h.fit_stacker(conn, s.x12_h2h_days, s.x12_h2h_min_n,
                                    h2h_idx, now=now)
        h2h_stack_sched = h2h.fit_stacker(conn, s.x12_h2h_days,
                                          s.x12_h2h_min_n, h2h_idx,
                                          population="schedule", now=now)

    # '-recal' is NOT appended here: it is a per-row property (_row_version),
    # not a batch property — schedule rows pass through identity while the
    # priced maps engage, and must not carry the tag.
    version = (f"{s.totals_source}-w{s.totals_blend_weight:g}"
               f"-hl{s.half_life_days:g}-a{s.poisson_alpha:g}"
               + ("-club" if pm.with_club else "")
               + ("-tod" if pm.with_tod else ""))
    out: list[PredictionRow] = []
    x12_out: list[X12PredictionRow] = []
    slate = []
    seen_clubs: list[str] = []
    for f in fixtures:  # event_id is the table PK — rows are unique
        ev_odds = odds.get(f["event_id"], {})
        if not ev_odds:
            continue  # nothing priced -> nothing to predict against
        f = dict(f, home_player=_alias(conn, f["home_player"]),
                 away_player=_alias(conn, f["away_player"]),
                 home_club=aliases.resolve_club(f["home_raw"], club_map),
                 away_club=aliases.resolve_club(f["away_raw"], club_map))
        seen_clubs += [f["home_raw"], f["away_raw"]]
        kickoff = datetime.fromisoformat(f["start_time_utc"])
        # as-of guard: form may only see results published before the cutoff
        cutoff = min(now, kickoff - timedelta(minutes=OFFSET_TOL_MIN))
        lam_h, lam_a = _fixture_lambdas(f, pm, form_idx, fallback, cutoff, s)
        pace = _pace_at(totals_idx, f, cutoff)

        for line, sels in sorted(ev_odds["ou"].items()):
            if "over" not in sels or "under" not in sels:
                continue
            row = _line_row(f, line, sels, lam_h, lam_a, now, cutoff, version,
                            s, maps, pace, scr=scr_get(("priced", "ou")))
            out.append(row)
            slate.append((f, line, row.pick, row.tier, row.p_over))

        if s.x12_enabled and lam_h is not None:
            x12_out.append(_x12_row(f, lam_h, lam_a, ev_odds["x12"], now,
                                    cutoff, version, s,
                                    stack=h2h_stack, h2h_idx=h2h_idx,
                                    scr=scr_get(("priced", "x12"))))

    for f in scheduled:
        # home_raw here is built from matches.*_club, already canonical; the
        # resolver is idempotent so both sources go through the same path
        f = dict(f, home_player=_alias(conn, f["home_player"]),
                 away_player=_alias(conn, f["away_player"]),
                 home_club=aliases.resolve_club(f["home_raw"], club_map),
                 away_club=aliases.resolve_club(f["away_raw"], club_map))
        kickoff = datetime.fromisoformat(f["start_time_utc"])
        cutoff = min(now, kickoff - timedelta(minutes=OFFSET_TOL_MIN))
        lam_h, lam_a = _fixture_lambdas(f, pm, form_idx, fallback, cutoff, s)
        if lam_h is None:
            continue  # no model coverage and no odds -> nothing to say
        base = max(1.0, round(lam_h + lam_a))
        pace = _pace_at(totals_idx, f, cutoff)
        for dl in SCHEDULE_LINES_AROUND:
            row = _schedule_row(f, base + dl, lam_h, lam_a, now, cutoff,
                                version, s, maps_sched, pace,
                                scr=scr_get(("schedule", "ou")))
            out.append(row)
            slate.append((f, row.line, row.pick, row.tier, row.p_over))
        if s.x12_enabled:  # no book 1x2 here -> value_flag stays False
            x12_out.append(_x12_row(f, lam_h, lam_a, {}, now, cutoff,
                                    version, s,
                                    stack=h2h_stack_sched, h2h_idx=h2h_idx,
                                    scr=scr_get(("schedule", "x12"))))

    n = PredictionRepo(conn).append_many(out)
    n_x12 = X12PredictionRepo(conn).append_many(x12_out) if x12_out else 0
    # Names the GLM has never seen. Transient after a regime switch (every
    # World Cup country is cold for ~a week); a *playing* club that stays here
    # means a missing club_aliases row and a quietly club-blind λ.
    unresolved = (sorted(aliases.unresolved_clubs(conn, seen_clubs))
                  if s.club_enabled else [])
    return {"fixtures": len(fixtures), "scheduled": len(scheduled), "rows": n,
            "x12_rows": n_x12, "recal_lines": sorted(maps),
            "recal_lines_sched": sorted(maps_sched),
            # lines whose engaged map carries the pace term (3-param fit);
            # [] when totals_h2h is off or a population is below engagement
            "recal_pace_lines": sorted(li for li, ab in maps.items()
                                       if len(ab) == 3),
            "recal_pace_lines_sched": sorted(li for li, ab in maps_sched.items()
                                             if len(ab) == 3),
            # n_fit per engaged population; 0 = identity (below min_n or off)
            "x12_h2h_n": {"priced": h2h_stack.n_fit if h2h_stack else 0,
                          "schedule": (h2h_stack_sched.n_fit
                                       if h2h_stack_sched else 0)},
            "unresolved_clubs": unresolved,
            # advisory only (docs/PICK_SCREEN.md): picks the screen would
            # veto; 0 with the screen off or cold
            "screen_vetoed": sum(r.screen_pass == 0 for r in out)
            + sum(r.screen_pass == 0 for r in x12_out),
            "elapsed_s": round(time.perf_counter() - t0, 2), "slate": slate}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="predictor.cycle")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    s = settings()
    db_path = Path(args.db) if args.db else s.gtl_db_path
    conn = connect(db_path)

    rep = run_cycle(conn, db_path)
    print(f"fixtures={rep['fixtures']} scheduled={rep.get('scheduled', 0)} "
          f"prediction_rows={rep['rows']} x12_rows={rep.get('x12_rows', 0)} "
          f"recal_lines={rep.get('recal_lines', [])} "
          f"recal_lines_sched={rep.get('recal_lines_sched', [])} "
          f"pace_lines={rep.get('recal_pace_lines', [])} "
          f"pace_lines_sched={rep.get('recal_pace_lines_sched', [])} "
          f"x12_h2h_n={rep.get('x12_h2h_n', {})} "
          f"screen_vetoed={rep.get('screen_vetoed', 0)} "
          f"elapsed={rep['elapsed_s']}s")
    if rep.get("unresolved_clubs"):
        print(f"  unresolved clubs (priced club-blind): "
              f"{', '.join(rep['unresolved_clubs'])}", file=sys.stderr)
    for f, line, pick, tier, p_over in rep.get("slate", []):
        label = f"{pick} ({tier})" if pick else "no pick"
        print(f"  {f['start_time_utc'][11:16]}Z {f['home_raw']} v {f['away_raw']}"
              f" | O/U {line}: p_over={p_over:.3f} -> {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
