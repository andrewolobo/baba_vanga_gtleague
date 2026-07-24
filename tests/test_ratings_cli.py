"""Crawl orchestration logic: the Overall-floor stop and the known-club audit.
The live crawl is exercised by hand (`refresh`); these pin the pure pieces so
the floor can't silently over- or under-read and the audit can't miss a club."""

from datetime import datetime, timezone

from core.schema import TeamRatingRow
from ratings_ingest.cli import _norm, _partition, audit_known
from store.repo import MatchRepo
from tests.test_store import row as match_row  # reuse the MatchRow builder

NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)


def team(name, overall) -> TeamRatingRow:
    return TeamRatingRow(sofifa_id=abs(hash(name)) % 10**6, edition="250044",
                         sofifa_name=name, overall=overall,
                         raw_hash=name, scraped_at=NOW)


def test_partition_keeps_at_or_above_floor_and_flags_cross():
    rows = [team("A", 80), team("B", 68), team("C", 67)]
    kept, crossed = _partition(rows, min_overall=68)
    assert [r.sofifa_name for r in kept] == ["A", "B"]  # 68 kept (>=), 67 dropped
    assert crossed is True


def test_partition_no_cross_when_whole_page_above_floor():
    kept, crossed = _partition([team("A", 90), team("B", 85)], min_overall=68)
    assert len(kept) == 2 and crossed is False


def test_partition_keeps_none_overall_defensively():
    kept, crossed = _partition([team("Blank", None)], min_overall=68)
    assert len(kept) == 1 and crossed is False


def test_norm_folds_accents_case_punctuation():
    assert _norm("Manchester City") == _norm("manchester  city!")
    assert _norm("Atlético") == "atletico"


def test_audit_known_matches_and_lists_gaps(db):
    # two canonical clubs in the DB: one sofifa spells the same, one differently
    MatchRepo(db).upsert_many([
        match_row(source_id="1", home_club="Manchester City", away_club="PSG"),
    ])
    scraped = [team("Manchester City", 85), team("Paris Saint-Germain", 84)]
    matched, unmatched = audit_known(db, scraped)
    assert "Manchester City" in matched
    assert unmatched == ["PSG"]  # different spelling -> step-4 alias worklist
