import math
from pathlib import Path

from odds_ingest.normalize import decode_events

FIXTURES = Path(__file__).resolve().parent.parent / "scraper" / "fixtures"


def test_decodes_all_events(betpawa_raw):
    events = decode_events(betpawa_raw)
    assert len(events) == 6
    assert all(ev.fixture.event_id for ev in events)
    assert all(ev.fixture.competition == "GT Leagues" for ev in events)


def test_known_event_fields(betpawa_raw):
    ev = {e.fixture.event_id: e for e in decode_events(betpawa_raw)}["36476611"]
    f = ev.fixture
    assert (f.home_raw, f.away_raw) == ("Germany (Tifosi)", "Ivory Coast (Fred)")
    assert (f.home_player, f.away_player) == ("Tifosi", "Fred")
    assert f.start_time_utc.isoformat() == "2026-07-08T18:30:00+00:00"

    by_sel = {(p.market, p.selection): p for p in ev.prices}
    assert by_sel[("1x2", "home")].odds == 1.96
    assert math.isclose(by_sel[("1x2", "home")].implied_prob, 0.4609, abs_tol=1e-3)
    assert by_sel[("ou", "over")].line == 3.5
    assert by_sel[("ou", "over")].odds == 1.75
    assert by_sel[("ou", "under")].odds == 1.89


def test_implied_1x2_sums_to_one(betpawa_raw):
    for ev in decode_events(betpawa_raw):
        probs = [p.implied_prob for p in ev.prices if p.market == "1x2"]
        assert len(probs) == 3
        assert math.isclose(sum(probs), 1.0, abs_tol=2e-3)


def test_ou_lines_are_per_match(betpawa_raw):
    lines = {p.line for ev in decode_events(betpawa_raw)
             for p in ev.prices if p.market == "ou"}
    assert len(lines) >= 3  # 2.5, 3.5, 5.5, 6.5 in the saved slate


def test_offline_cli_ingest(betpawa_raw, tmp_path, capsys):
    from odds_ingest.cli import main

    db_path = tmp_path / "odds.db"
    rc = main(["--db", str(db_path), "fetch",
               "--from-file", str(FIXTURES / "betpawa_gtleagues_2026-07-08.bin")])
    assert rc == 0

    import sqlite3
    conn = sqlite3.connect(db_path)
    n_fix = conn.execute("SELECT COUNT(*) FROM fixtures").fetchone()[0]
    n_odds = conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
    assert n_fix == 6
    assert n_odds == 6 * 5  # 3 x 1X2 + 2 x O/U per event

    # re-ingest: fixtures stay unique, snapshots append (odds history)
    assert main(["--db", str(db_path), "fetch",
                 "--from-file", str(FIXTURES / "betpawa_gtleagues_2026-07-08.bin")]) == 0
    assert conn.execute("SELECT COUNT(*) FROM fixtures").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0] == n_odds * 2
