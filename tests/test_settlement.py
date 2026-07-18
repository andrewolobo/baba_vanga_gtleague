from datetime import datetime, timedelta, timezone

from core.config import settings
from core.schema import MatchRow, OddsPrice
from model import recal
from predictor.cycle import run_cycle
from settlement.settle import run as settle_run, scorecard, vs_book, x12_vs_book
from store.repo import MatchRepo, OddsRepo
from tests.test_predictor import (  # noqa: F401  (fixture reuse)
    NOW, _schedule_game, _seed_settled_schedule, seeded)

LATER = NOW + timedelta(hours=2)


def _play_result(db, kickoff, home="P0", away="P1", hf=3, af=2,
                 scraped_at=None, source_id="RES1"):
    MatchRepo(db).upsert_many([MatchRow(
        source_match_id=source_id, date=kickoff.date().isoformat(),
        kickoff_ts=kickoff, competition="GT Leagues",
        home_player=home, away_player=away, home_club="A", away_club="B",
        status=3, home_ft=hf, away_ft=af, raw_hash=f"rh-{source_id}",
        scraped_at=scraped_at or kickoff + timedelta(minutes=14),
    )])


def test_settles_and_grades(seeded):  # noqa: F811
    conn, db_path = seeded
    run_cycle(conn, db_path, now=NOW)
    kickoff = NOW + timedelta(minutes=30)  # E1
    _play_result(conn, kickoff, hf=3, af=2)  # total 5 -> over 3.5 and 4.5 hit

    rep = settle_run(conn, db_path, now=LATER)
    assert rep["settled"] == 1  # E1; E2 has no matching result -> pending
    assert rep["pending"] == 1

    s = conn.execute("SELECT * FROM settlements").fetchone()
    assert s["event_id"] == "E1"
    assert s["result_total"] == 5
    assert s["offset_min_used"] == 0.0
    assert s["leak_risk"] == 0  # result scraped after prediction
    assert s["regen_pick"] in ("over", "under")
    served = conn.execute(
        "SELECT pick FROM predictions WHERE event_id='E1'"
        " ORDER BY (pick IS NULL), confidence DESC LIMIT 1").fetchone()
    if served["pick"] is not None:
        assert s["pick_correct"] == (1 if served["pick"] == "over" else 0)

    # idempotent: second run settles nothing new
    rep2 = settle_run(conn, db_path, now=LATER)
    assert rep2["settled"] == 0


def test_leak_risk_flagged_when_result_precedes_prediction(seeded):  # noqa: F811
    conn, db_path = seeded
    kickoff = NOW + timedelta(minutes=30)
    # result lands in the DB BEFORE the prediction cycle runs (the hazard)
    _play_result(conn, kickoff, scraped_at=NOW - timedelta(minutes=5))
    run_cycle(conn, db_path, now=NOW)

    settle_run(conn, db_path, now=LATER)
    s = conn.execute("SELECT leak_risk FROM settlements WHERE event_id='E1'").fetchone()
    assert s["leak_risk"] == 1


def test_integer_line_push_is_ungraded(seeded):  # noqa: F811
    conn, db_path = seeded
    kickoff = NOW + timedelta(minutes=30)
    conn.execute(
        "INSERT INTO predictions (event_id, predicted_at, totals_source, line,"
        " p_over, p_push, p_under, pick, confidence, tier, value_flag,"
        " model_version, as_of_cutoff_ts)"
        " VALUES ('E1', ?, 'blend', 4.0, 0.5, 0.1, 0.4, 'over', 0.6, 'solid',"
        " 0, 'test', ?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    _play_result(conn, kickoff, hf=2, af=2)  # total 4 == line -> push
    settle_run(conn, db_path, now=LATER)
    s = conn.execute("SELECT pick_correct FROM settlements WHERE event_id='E1'").fetchone()
    assert s["pick_correct"] is None


def test_no_ambiguous_join(seeded):  # noqa: F811
    conn, db_path = seeded
    run_cycle(conn, db_path, now=NOW)
    kickoff = NOW + timedelta(minutes=30)
    # two candidate results for the same pair inside the window -> pending
    _play_result(conn, kickoff, source_id="RES1")
    _play_result(conn, kickoff + timedelta(minutes=4), source_id="RES2")
    rep = settle_run(conn, db_path, now=LATER)
    assert rep["settled"] == 0
    assert rep["pending"] == 2


def test_scorecard_renders(seeded):  # noqa: F811
    conn, db_path = seeded
    run_cycle(conn, db_path, now=NOW)
    _play_result(conn, NOW + timedelta(minutes=30))
    settle_run(conn, db_path, now=LATER)
    out = scorecard(conn, days=365)  # synthetic clock is months behind wall time
    assert "settled events: 1" in out


def test_vs_book_renders(seeded):  # noqa: F811
    conn, db_path = seeded
    run_cycle(conn, db_path, now=NOW)
    _play_result(conn, NOW + timedelta(minutes=30))  # E1, total 5 -> over both lines
    settle_run(conn, db_path, now=LATER)

    out = vs_book(conn, days=365, boot=50)  # synthetic clock is months behind
    assert "model vs book: 2 settled predictions" in out  # E1 x lines 3.5, 4.5
    assert "paired Brier (model - book)" in out
    assert "by line" in out
    # single-class outcome: the edge regression must be skipped, not crash
    assert "edge coef" not in out


def test_book_agreement_classification():
    """with/against = pick side vs close side; a close inside the coin margin
    beats both (the book claims no side there)."""
    import numpy as np
    from settlement.settle import _book_agreement
    side = np.array([1, 1, -1, -1, 1, -1])
    close = np.array([0.60, 0.40, 0.40, 0.60, 0.51, 0.49])
    assert list(_book_agreement(side, close)) == [
        "with-book", "against-book", "with-book", "against-book",
        "book~coin", "book~coin"]


def test_vs_book_agreement_section(seeded):  # noqa: F811
    """The section appears iff the window has picked rows."""
    conn, db_path = seeded
    run_cycle(conn, db_path, now=NOW)
    _play_result(conn, NOW + timedelta(minutes=30))
    settle_run(conn, db_path, now=LATER)

    conn.execute("UPDATE predictions SET pick = NULL, tier = NULL")
    assert "picks by book agreement" not in vs_book(conn, days=365, boot=50)

    conn.execute("UPDATE predictions SET pick = 'over', tier = 'lean'")
    out = vs_book(conn, days=365, boot=50)
    assert "picks by book agreement" in out
    assert "book~coin" in out or "with-book" in out or "against-book" in out


def test_vs_book_excludes_book_fallback_rows(seeded):  # noqa: F811
    """E2's player is unknown to the model, so its served probs ARE the book's.
    Scoring those against the book would be scoring the book against itself."""
    conn, db_path = seeded
    run_cycle(conn, db_path, now=NOW)
    _play_result(conn, NOW + timedelta(minutes=30))
    _play_result(conn, NOW + timedelta(minutes=45), home="Ghost", away="P2",
                 source_id="RES2")
    rep = settle_run(conn, db_path, now=LATER)
    assert rep["settled"] == 2

    covered = conn.execute(
        "SELECT COUNT(*) c FROM predictions WHERE lambda_home IS NULL").fetchone()["c"]
    assert covered == 2  # E2's two lines took the book fallback
    assert "model vs book: 2 settled predictions" in vs_book(conn, days=365, boot=50)


def test_vs_book_empty_window(seeded):  # noqa: F811
    conn, db_path = seeded
    run_cycle(conn, db_path, now=NOW)
    assert "no settled" in vs_book(conn, days=365, boot=50)


def test_vs_book_tag_filter(seeded):  # noqa: F811
    """--tag keeps only rows whose model_version contains the substring, so a
    mixed-generation window can be judged on one serving generation alone."""
    conn, db_path = seeded
    run_cycle(conn, db_path, now=NOW)
    _play_result(conn, NOW + timedelta(minutes=30))
    settle_run(conn, db_path, now=LATER)
    conn.execute("UPDATE predictions SET model_version ="
                 " model_version || '-recal2' WHERE line = 3.5")

    out = vs_book(conn, days=365, boot=50, tag="recal2")
    assert "model vs book: 1 settled predictions" in out
    assert "tag 'recal2' (1 other-version rows excluded)" in out
    assert "no settled" in vs_book(conn, days=365, boot=50, tag="no-such-tag")


# ── 1x2 vs book ──────────────────────────────────────────────────────────────

def _seed_x12_odds(conn, event_id="E1"):
    OddsRepo(conn).append_many([
        OddsPrice(event_id=event_id, market="1x2", line=None, selection="home",
                  odds=2.0, implied_prob=0.50),
        OddsPrice(event_id=event_id, market="1x2", line=None, selection="draw",
                  odds=4.0, implied_prob=0.25),
        OddsPrice(event_id=event_id, market="1x2", line=None, selection="away",
                  odds=4.0, implied_prob=0.25)], fetched_at=NOW)


def test_x12_vs_book_renders(seeded, monkeypatch):  # noqa: F811
    conn, db_path = seeded
    monkeypatch.setattr(settings(), "x12_enabled", True)
    _seed_x12_odds(conn, "E1")
    run_cycle(conn, db_path, now=NOW)
    _play_result(conn, NOW + timedelta(minutes=30), hf=3, af=2)  # home win
    settle_run(conn, db_path, now=LATER)

    out = x12_vs_book(conn, days=365, boot=50)  # synthetic clock lags wall time
    assert "1x2 model vs book: 1 settled predictions" in out
    assert "paired Brier (model - book)" in out
    assert "draw head" in out
    assert "by h2h regime" in out
    # single-class decisive outcome: edge regression skipped, not crashed
    assert "edge coef" not in out


def test_x12_vs_book_needs_a_book_close(seeded, monkeypatch):  # noqa: F811
    """E1 settles for 1x2 but has no stored 1x2 odds — the row must be
    excluded (there is no price to compare against), leaving nothing."""
    conn, db_path = seeded
    monkeypatch.setattr(settings(), "x12_enabled", True)
    run_cycle(conn, db_path, now=NOW)
    _play_result(conn, NOW + timedelta(minutes=30))
    settle_run(conn, db_path, now=LATER)
    assert conn.execute(
        "SELECT COUNT(*) c FROM settlements_x12").fetchone()["c"] >= 1
    assert "no settled" in x12_vs_book(conn, days=365, boot=50)


def test_x12_vs_book_tag_filter(seeded, monkeypatch):  # noqa: F811
    conn, db_path = seeded
    monkeypatch.setattr(settings(), "x12_enabled", True)
    _seed_x12_odds(conn, "E1")
    run_cycle(conn, db_path, now=NOW)
    _play_result(conn, NOW + timedelta(minutes=30), hf=3, af=2)
    settle_run(conn, db_path, now=LATER)
    conn.execute("UPDATE predictions_x12 SET model_version ="
                 " model_version || '-h2h'")

    out = x12_vs_book(conn, days=365, boot=50, tag="-h2h")
    assert "1x2 model vs book: 1 settled predictions" in out
    assert "tag '-h2h' (0 other-version rows excluded)" in out
    assert "no settled" in x12_vs_book(conn, days=365, boot=50, tag="no-such")


# ── schedule-only (gtl:) settlement (docs/POPULATION_SPLIT.md Phase 1) ──────

def _finish_schedule_game(db, kickoff, home="P3", away="P4", hf=4, af=2,
                          scraped_at=None, source_id="SCHED1"):
    """Re-scrape the scheduled game as finished: same source_match_id, new
    raw_hash -> MatchRepo takes the UPDATE path, exactly like production."""
    MatchRepo(db).upsert_many([MatchRow(
        source_match_id=source_id, date=kickoff.date().isoformat(),
        kickoff_ts=kickoff, competition="GT Leagues",
        home_player=home, away_player=away, home_club="H", away_club="A",
        status=3, home_ft=hf, away_ft=af, raw_hash=f"fin-{source_id}",
        scraped_at=scraped_at or kickoff + timedelta(minutes=14),
    )])


def test_schedule_settles_and_grades(seeded):  # noqa: F811
    conn, db_path = seeded
    kickoff = NOW + timedelta(hours=1)
    _schedule_game(conn, kickoff)
    run_cycle(conn, db_path, now=NOW)
    _finish_schedule_game(conn, kickoff, hf=4, af=2)  # total 6: over both lines

    rep = settle_run(conn, db_path, now=LATER)
    assert rep["schedule_settled"] == 1

    s = conn.execute(
        "SELECT * FROM settlements WHERE event_id = 'gtl:SCHED1'").fetchone()
    assert s["result_total"] == 6
    assert s["offset_min_used"] == 0.0  # exact-id join, by construction
    assert s["matched_match_id"] is not None
    assert s["leak_risk"] == 0
    assert s["regen_pick"] in ("over", "under")
    served = conn.execute(
        "SELECT pick FROM predictions WHERE event_id = 'gtl:SCHED1'"
        " ORDER BY (pick IS NULL), confidence DESC, line LIMIT 1").fetchone()
    if served["pick"] is not None:  # total 6 clears both schedule lines
        assert s["pick_correct"] == (1 if served["pick"] == "over" else 0)
        assert s["regen_agrees"] is not None
    else:
        assert s["pick_correct"] is None

    # NOT EXISTS idempotency: second run settles nothing new
    rep2 = settle_run(conn, db_path, now=LATER)
    assert rep2["schedule_settled"] == 0


def test_schedule_join_is_by_id_never_a_window_decoy(seeded):  # noqa: F811
    """The ±2.5 h replay hazard does not exist here: the join is by exact
    source_match_id. A finished replay with the same players inside the
    kickoff window must not settle the predicted game."""
    conn, db_path = seeded
    kickoff = NOW + timedelta(hours=1)
    _schedule_game(conn, kickoff)  # SCHED1, stays unfinished
    run_cycle(conn, db_path, now=NOW)
    _finish_schedule_game(conn, kickoff + timedelta(minutes=4),
                          source_id="DECOY1", hf=9, af=0)

    rep = settle_run(conn, db_path, now=LATER)
    assert rep["schedule_settled"] == 0
    n = conn.execute("SELECT COUNT(*) c FROM settlements"
                     " WHERE event_id LIKE 'gtl:%'").fetchone()["c"]
    assert n == 0


def test_schedule_no_pick_row_grades_null(seeded):  # noqa: F811
    conn, db_path = seeded
    kickoff = NOW + timedelta(hours=1)
    _schedule_game(conn, kickoff, source_id="SCHED3")
    conn.execute(
        "INSERT INTO predictions (event_id, predicted_at, totals_source, line,"
        " p_over, p_push, p_under, lambda_home, lambda_away, pick, confidence,"
        " tier, value_flag, model_version, as_of_cutoff_ts)"
        " VALUES ('gtl:SCHED3', ?, 'blend', 3.5, 0.55, 0.0, 0.45, 2.0, 2.0,"
        " NULL, 0.55, NULL, 0, 'test', ?)",
        (NOW.isoformat(), NOW.isoformat()))
    _finish_schedule_game(conn, kickoff, source_id="SCHED3", hf=3, af=2)

    settle_run(conn, db_path, now=LATER)
    s = conn.execute(
        "SELECT * FROM settlements WHERE event_id = 'gtl:SCHED3'").fetchone()
    assert s is not None and s["result_total"] == 5
    assert s["pick_correct"] is None  # no pick -> no grade
    assert s["regen_agrees"] is None  # agreement needs a served pick


def test_schedule_leak_flag_when_result_precedes_prediction(seeded):  # noqa: F811
    conn, db_path = seeded
    kickoff = NOW + timedelta(hours=1)
    _schedule_game(conn, kickoff)
    run_cycle(conn, db_path, now=NOW)
    # result scraped BEFORE the prediction was made (the hazard shape)
    _finish_schedule_game(conn, kickoff, scraped_at=NOW - timedelta(minutes=5))

    settle_run(conn, db_path, now=LATER)
    s = conn.execute("SELECT leak_risk FROM settlements"
                     " WHERE event_id = 'gtl:SCHED1'").fetchone()
    assert s["leak_risk"] == 1


def test_scorecard_reports_populations_separately(seeded):  # noqa: F811
    """Phase 3 (docs/POPULATION_SPLIT.md): the scorecard shows each
    population in its own section, never pooled — the pooled view
    systematically reported the model's worst subpopulation. vs-book stays
    priced-only by definition (it needs a price)."""
    conn, db_path = seeded
    kickoff = NOW + timedelta(hours=1)
    _schedule_game(conn, kickoff)
    run_cycle(conn, db_path, now=NOW)
    _play_result(conn, NOW + timedelta(minutes=30))  # E1, the priced result
    _finish_schedule_game(conn, kickoff)

    rep = settle_run(conn, db_path, now=LATER)
    assert rep["settled"] == 1 and rep["schedule_settled"] == 1
    out = scorecard(conn, days=365)
    priced_sec, sched_sec = out.split("== schedule (model-only) ==")
    assert "== book-priced ==" in priced_sec
    assert "settled events: 1" in priced_sec  # exactly the priced settlement
    assert "settled events: 1" in sched_sec   # exactly the schedule one
    # vs-book compares against a price, so schedule settlements stay out
    assert "model vs book: 2 settled predictions" in vs_book(conn, days=365,
                                                             boot=50)


def test_schedule_regen_keeps_identity_for_untagged_rows(seeded, monkeypatch):  # noqa: F811
    """A row served through identity (no per-row '-recal' tag) must regen
    through identity even when schedule maps have engaged by settle time.
    Otherwise an engaged map's intercept flips near-coin-flip sides and
    fakes regen disagreement — the exact artifact the Phase 1 backfill
    measured on rows served in the pre-Phase-0 priced-map window."""
    conn, db_path = seeded
    kickoff = NOW + timedelta(hours=1)
    _schedule_game(conn, kickoff)
    s = settings()
    monkeypatch.setattr(s, "recal_enabled", False)
    run_cycle(conn, db_path, now=NOW)  # identity serving, rows untagged

    served = conn.execute(
        "SELECT * FROM predictions WHERE event_id = 'gtl:SCHED1'"
        " ORDER BY (pick IS NULL), confidence DESC, line LIMIT 1").fetchone()
    raw_side = "over" if served["p_over"] > served["p_under"] else "under"

    # maps engage between predict and settle, fit on a pool whose realized
    # totals ran 1.5 goals off the served λs — in whichever direction makes
    # the intercept flip THIS row's side (the hazard being tested).
    # settled_at must sit inside regen's wall-clock fit window, and the
    # seeded kickoffs on a LATER day than the fixture so they stay out of
    # regen's day-frozen retraining (λs must match serving exactly).
    monkeypatch.setattr(s, "recal_enabled", True)
    _seed_settled_schedule(
        conn, under_bias=1.5 if raw_side == "over" else -1.5,
        settled_at=datetime.now(timezone.utc).isoformat(),
        kickoff_base=NOW + timedelta(days=1))
    maps = recal.fit_line_maps(conn, days=14, min_n=300, min_n_line=75,
                               population="schedule")
    mapped_over = recal.apply_to_line(maps, served["line"], served["p_over"],
                                      served["p_push"])[0]
    assert ("over" if mapped_over > 0.5 else "under") != raw_side  # would flip

    _finish_schedule_game(conn, kickoff, hf=4, af=2)
    settle_run(conn, db_path, now=LATER)
    row = conn.execute("SELECT * FROM settlements"
                       " WHERE event_id = 'gtl:SCHED1'").fetchone()
    assert row["regen_pick"] == raw_side  # identity, not the engaged map
    if row["pick_correct"] is not None:
        assert row["regen_agrees"] == 1


def test_schedule_settle_flag_is_the_rollback(seeded, monkeypatch):  # noqa: F811
    conn, db_path = seeded
    kickoff = NOW + timedelta(hours=1)
    _schedule_game(conn, kickoff)
    run_cycle(conn, db_path, now=NOW)
    _finish_schedule_game(conn, kickoff)

    monkeypatch.setattr(settings(), "schedule_settle_enabled", False)
    rep = settle_run(conn, db_path, now=LATER)
    assert rep["schedule_settled"] == 0
    n = conn.execute("SELECT COUNT(*) c FROM settlements"
                     " WHERE event_id LIKE 'gtl:%'").fetchone()["c"]
    assert n == 0


# ── tier re-quantile (docs/CLUB_FEATURE.md step 4) ───────────────────────────

def _served_pick(conn, i, conf, kickoff, graded=None, version="blend-w0.7"):
    ev = f"T{i}"
    conn.execute("INSERT INTO fixtures VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (ev, kickoff, "GT", "a", "b", "PX", "PY", "full",
                  kickoff, kickoff))
    conn.execute(
        "INSERT INTO predictions (event_id, predicted_at, totals_source, line,"
        " p_over, p_push, p_under, lambda_home, lambda_away, pick, confidence,"
        " tier, value_flag, model_version, as_of_cutoff_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ev, "2026-01-25T00:00:00+00:00", "blend", 3.5, conf, 0.0, 1 - conf,
         2.0, 2.0, "over", conf,
         "strong" if conf >= settings().tier_strong
         else "solid" if conf >= settings().tier_solid else "lean",
         0, version, "2026-01-25T00:00:00+00:00"))
    if graded is not None:
        conn.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?,?)",
                     (ev, None, 0.0, 5, int(graded), 0, "over", 1,
                      "2026-01-26T00:00:00+00:00"))


def test_tiers_refuses_thin_data(db):
    from settlement.settle import tier_bands
    kick = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    for i in range(20):
        _served_pick(db, i, 0.60 + i * 0.002, kick)
    db.commit()
    out = tier_bands(db, days=14)
    assert "REFUSING" in out and "20 picks" in out


def test_tiers_reports_populations_separately(db):
    """Each population proposes (or refuses) bands from its own served
    confidence distribution — the config bands apply to both, so the
    proposal must be readable per population."""
    from settlement.settle import tier_bands
    kick_dt = datetime.now(timezone.utc) + timedelta(hours=1)
    for i in range(20):
        _served_pick(db, i, 0.60 + i * 0.002, kick_dt.isoformat())
    now_iso = datetime.now(timezone.utc).isoformat()
    for i in range(10):
        sid = f"TS{i}"
        db.execute(
            "INSERT INTO matches (source_match_id, date, kickoff_ts,"
            " competition, home_player, away_player, home_club, away_club,"
            " status, home_ft, away_ft, raw_hash, scraped_at)"
            " VALUES (?,?,?,?,?,?,?,?,0,NULL,NULL,?,?)",
            (sid, now_iso[:10], (kick_dt + timedelta(minutes=i)).isoformat(),
             "GT", "PA", "PB", "H", "A", f"h{sid}", now_iso))
        db.execute(
            "INSERT INTO predictions (event_id, predicted_at, totals_source,"
            " line, p_over, p_push, p_under, lambda_home, lambda_away, pick,"
            " confidence, tier, value_flag, model_version, as_of_cutoff_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"gtl:{sid}", now_iso, "blend", 3.5, 0.62, 0.0, 0.38, 2.0, 2.0,
             "over", 0.62, "lean", 0, "test", now_iso))
    db.commit()
    out = tier_bands(db, days=14)
    priced_sec, sched_sec = out.split("== schedule (model-only) ==")
    assert "served picks: 20 in 14d" in priced_sec
    assert "served picks: 10 in 14d" in sched_sec


def test_tiers_proposes_reachable_bands(db):
    """Bands must sit inside the observed confidence range: an unreachable
    `strong` retires the tier silently instead of erroring."""
    from settlement.settle import tier_bands
    import numpy as np
    rng = np.random.default_rng(5)
    kick = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    for i in range(600):
        _served_pick(db, i, float(np.clip(rng.normal(0.64, 0.03), 0.601, 0.95)),
                     kick, graded=rng.random() < 0.7)
    db.commit()
    out = tier_bands(db, days=14)
    assert "REFUSING" not in out and "WARNING" not in out
    solid = float(out.split("TIER_SOLID=")[1].split()[0])
    strong = float(out.split("TIER_STRONG=")[1].split()[0])
    assert 0.60 < solid < strong < 0.95, out
