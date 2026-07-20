from datetime import datetime, timedelta, timezone

import pytest

from apps.bot import alerts
from apps.bot.format import render_alert
from core.config import settings
from core.schema import FixtureRow, OddsPrice
from store.repo import FixtureRepo, OddsRepo

NOW = datetime(2026, 1, 26, 12, 0, tzinfo=timezone.utc)

_PRED_COLS = ("event_id", "predicted_at", "totals_source", "line", "p_over",
              "p_push", "p_under", "lambda_home", "lambda_away", "pick",
              "confidence", "tier", "value_flag", "model_version",
              "as_of_cutoff_ts")


def _pred(conn, event_id, line, pick, tier, conf, *, value=0, predicted_at=NOW):
    """Insert one served prediction row (the columns the alert query reads)."""
    p_over = conf if pick == "over" else round(1 - conf, 6)
    conn.execute(
        f"INSERT INTO predictions ({','.join(_PRED_COLS)})"
        f" VALUES ({','.join('?' * len(_PRED_COLS))})",
        (event_id, predicted_at.isoformat(), "blend", line, p_over, 0.0,
         round(1 - p_over, 6), 1.9, 2.1, pick, conf, tier, value, "test",
         predicted_at.isoformat()),
    )


def _fixture(conn, event_id, kickoff, home_raw="Barca (Ace)",
             away_raw="Madrid (Viper)", home_player="Ace", away_player="Viper"):
    FixtureRepo(conn).upsert_many([FixtureRow(
        event_id=event_id, start_time_utc=kickoff, competition="GT League 12",
        home_raw=home_raw, away_raw=away_raw, home_player=home_player,
        away_player=away_player, coverage="full")], seen_at=NOW)


@pytest.fixture
def seeded(db):
    # E1: priced, upcoming, two STRONG lines -> headline is the higher-confidence one
    _fixture(db, "E1", NOW + timedelta(minutes=45))
    _pred(db, "E1", 3.5, "over", "strong", 0.70)
    _pred(db, "E1", 2.5, "over", "strong", 0.75, value=1)  # headline
    OddsRepo(db).append_many([
        OddsPrice(event_id="E1", market="ou", line=2.5, selection="over",
                  odds=1.85, implied_prob=0.54),
    ], fetched_at=NOW)

    # E2: priced, upcoming, but only SOLID -> not a candidate
    _fixture(db, "E2", NOW + timedelta(minutes=30), home_raw="A (P0)",
             away_raw="B (P1)", home_player="P0", away_player="P1")
    _pred(db, "E2", 3.5, "under", "solid", 0.66)

    # E3: STRONG but already kicked off -> excluded
    _fixture(db, "E3", NOW - timedelta(minutes=30), home_raw="C (P2)",
             away_raw="D (P3)", home_player="P2", away_player="P3")
    _pred(db, "E3", 3.5, "over", "strong", 0.80)

    # schedule-only strong pick (no fixtures row) -> out of scope
    _pred(db, "gtl:S1", 4.5, "over", "strong", 0.85)
    db.commit()
    return db


def test_candidates_headline_strong_priced_upcoming(seeded):
    s = settings()
    cands = alerts.candidates(seeded, s, now=NOW)
    assert len(cands) == 1
    c = cands[0]
    assert c["event_id"] == "E1"
    assert (c["line"], c["selection"], c["tier"]) == (2.5, "over", "strong")
    assert c["confidence"] == 0.75           # the higher-confidence strong line
    assert c["home_club"] == "Barca" and c["home_player"] == "Ace"
    assert c["away_club"] == "Madrid"
    assert c["book_odds"] == 1.85            # latest snapshot for the picked side
    assert c["value_flag"] is True


def test_candidates_exclusions(seeded):
    events = {c["event_id"] for c in alerts.candidates(seeded, settings(), now=NOW)}
    assert "E2" not in events          # solid, not strong
    assert "E3" not in events          # already kicked off
    assert "gtl:S1" not in events      # schedule-only / no fixture


def test_dedup_only_refires_when_pick_changes(seeded):
    s = settings()
    cands = alerts.candidates(seeded, s, now=NOW)
    assert alerts.filter_unsent(seeded, cands) == cands   # nothing sent yet

    alerts.record_sent(seeded, cands[0], message_id=123, now=NOW)
    assert alerts.filter_unsent(seeded, cands) == []      # same key -> suppressed
    assert alerts.already_sent(seeded, "E1", 2.5, "over")

    # a flipped side on the same line is a fresh key -> not suppressed
    flipped = dict(cands[0], selection="under")
    assert alerts.filter_unsent(seeded, [flipped]) == [flipped]


def test_candidates_uses_only_the_latest_batch(seeded):
    # a newer batch demotes E1 to solid -> no strong candidate remains
    later = NOW + timedelta(minutes=5)
    _pred(seeded, "E1", 2.5, "over", "solid", 0.66, predicted_at=later)
    _pred(seeded, "E1", 3.5, "over", "solid", 0.63, predicted_at=later)
    seeded.commit()
    assert alerts.candidates(seeded, settings(), now=NOW) == []


def test_alerts_tier_setting_is_respected(seeded, monkeypatch):
    s = settings()
    monkeypatch.setattr(s, "alerts_tier", "solid")
    events = {c["event_id"] for c in alerts.candidates(seeded, s, now=NOW)}
    assert events == {"E2"}            # now only the solid pick qualifies


def test_render_alert_contains_the_essentials():
    c = {
        "event_id": "E1", "line": 3.5, "selection": "over", "confidence": 0.71,
        "tier": "strong", "value_flag": True, "kickoff": (NOW).isoformat(),
        "competition": "GT League 12", "home_club": "Barca", "home_player": "Ace",
        "away_club": "Madrid", "away_player": "Viper", "book_odds": 1.85,
    }
    msg = render_alert(c, now=NOW - timedelta(minutes=42))
    assert "STRONG" in msg
    assert "Barca (Ace)" in msg and "Madrid (Viper)" in msg
    assert "OVER 3.5" in msg
    assert "71%" in msg
    assert "1.85" in msg and "value" in msg
    assert "in 42m" in msg
    assert msg.count("\n") >= 4          # multi-line, single message


def test_render_alert_escapes_html():
    c = {"event_id": "E9", "line": 2.5, "selection": "under", "confidence": 0.7,
         "tier": "strong", "value_flag": False, "kickoff": NOW.isoformat(),
         "competition": "A & B <cup>", "home_club": "R&D", "home_player": None,
         "away_club": "X<Y", "away_player": None, "book_odds": None}
    msg = render_alert(c, now=NOW)
    assert "&amp;" in msg and "&lt;" in msg
    assert "<cup>" not in msg            # raw angle brackets never leak through
