from datetime import timezone

from core.config import STATUS_CANCELLED, STATUS_FINISHED
from results_ingest.parse import parse_fixtures, quality_report


def test_parses_full_page(results_raw):
    rows = parse_fixtures(results_raw)
    assert len(rows) == len(results_raw) == 50
    assert {r.status for r in rows} <= {STATUS_FINISHED, STATUS_CANCELLED}


def test_known_match_values(results_raw):
    rows = {r.source_match_id: r for r in parse_fixtures(results_raw)}
    m = rows["544684"]  # France (Hulk) 2-1 DR Congo (Professor), 23:45Z
    assert (m.home_player, m.away_player) == ("Hulk", "Professor")
    assert (m.home_club, m.away_club) == ("France", "DR Congo")
    assert (m.home_ft, m.away_ft) == (2, 1)
    assert m.date == "2026-07-07"
    assert m.kickoff_ts.tzinfo is not None
    assert m.kickoff_ts.astimezone(timezone.utc).hour == 23
    assert m.competition == "World Cup I / Group D"


def test_finished_rows_have_scores(results_raw):
    for r in parse_fixtures(results_raw):
        if r.status == STATUS_FINISHED:
            assert r.home_ft is not None and r.away_ft is not None
        else:
            assert r.home_ft is None and r.away_ft is None


def test_hash_is_stable_and_content_sensitive(results_raw):
    a = parse_fixtures(results_raw)
    b = parse_fixtures(results_raw)
    assert [r.raw_hash for r in a] == [r.raw_hash for r in b]
    mutated = [dict(results_raw[0], status=4)] + results_raw[1:]
    c = parse_fixtures(mutated)
    assert c[0].raw_hash != a[0].raw_hash


def test_quality_report(results_raw):
    q = quality_report(parse_fixtures(results_raw))
    assert q["rows"] == 50
    assert q["finished_unscored"] == 0
    assert q["in_batch_dupes"] == 0
    assert q["players"] > 5
