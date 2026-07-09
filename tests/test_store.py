from datetime import datetime, timezone

from core.schema import MatchRow
from store.db import migrate
from store.repo import MatchRepo

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def row(source_id="1", hash_="h1", **kw) -> MatchRow:
    base = dict(
        source_match_id=source_id, date="2026-07-07",
        kickoff_ts=datetime(2026, 7, 7, 18, 0, tzinfo=timezone.utc),
        competition="World Cup I / Group D", home_player="Hulk",
        away_player="Professor", home_club="France", away_club="DR Congo",
        status=3, home_ft=2, away_ft=1, raw_hash=hash_, scraped_at=NOW,
    )
    base.update(kw)
    return MatchRow(**base)


def test_migrations_are_idempotent(db):
    assert migrate(db) == []  # connect() already applied them


def test_upsert_is_idempotent(db):
    repo = MatchRepo(db)
    r1 = repo.upsert_many([row()])
    r2 = repo.upsert_many([row()])
    assert (r1.inserted, r2.unchanged) == (1, 1)
    assert repo.count() == 1


def test_dedup_constraint_absorbs_double_listing(db):
    # same match relisted under a different source id (parent §2.2.9 pathology)
    repo = MatchRepo(db)
    rep = repo.upsert_many([row(source_id="1"), row(source_id="2", hash_="h2")])
    assert (rep.inserted, rep.dup_ignored) == (1, 1)
    assert repo.count() == 1


def test_score_update_on_hash_change(db):
    repo = MatchRepo(db)
    repo.upsert_many([row(status=0, home_ft=None, away_ft=None, hash_="pre")])
    rep = repo.upsert_many([row(status=3, home_ft=2, away_ft=1, hash_="post")])
    assert rep.updated == 1
    got = db.execute("SELECT status, home_ft FROM matches").fetchone()
    assert (got["status"], got["home_ft"]) == (3, 2)


def test_distinct_matches_both_kept(db):
    repo = MatchRepo(db)
    rep = repo.upsert_many([
        row(source_id="1"),
        row(source_id="2", hash_="h2", home_ft=0, away_ft=0),  # different score
    ])
    assert rep.inserted == 2
