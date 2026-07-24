"""Storage layer for external sofifa team ratings (migration 007). Parsing and
network land in later steps; this only proves the table + repo round-trip and
the idempotent (sofifa_id, edition) merge."""

from datetime import datetime, timezone

from core.schema import TeamRatingRow
from store.db import migrate
from store.repo import TeamRatingRepo

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def row(sofifa_id=23, edition="260045", hash_="h1", **kw) -> TeamRatingRow:
    base = dict(
        sofifa_id=sofifa_id, edition=edition,
        sofifa_name="Borussia Mönchengladbach", nationality="Germany",
        league="Bundesliga", league_id=19, is_national=False,
        overall=75, attack=77, midfield=75, defence=75,
        domestic_prestige=6, international_prestige=5,
        num_players=30, starting_age=26.36,
        transfer_budget=19_900_000.0, club_worth=308_700_000.0,
        raw_hash=hash_, scraped_at=NOW,
    )
    base.update(kw)
    return TeamRatingRow(**base)


def test_migration_007_applied(db):
    assert migrate(db) == []  # connect() already applied it
    tables = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"team_ratings", "team_rating_aliases"} <= tables


def test_upsert_is_idempotent(db):
    repo = TeamRatingRepo(db)
    r1 = repo.upsert_many([row()])
    r2 = repo.upsert_many([row()])
    assert (r1.inserted, r2.unchanged) == (1, 1)
    assert repo.count() == 1


def test_utf8_name_round_trips(db):
    TeamRatingRepo(db).upsert_many([row()])
    got = db.execute("SELECT sofifa_name FROM team_ratings").fetchone()
    assert got["sofifa_name"] == "Borussia Mönchengladbach"


def test_hash_change_refreshes_ratings_in_place(db):
    repo = TeamRatingRepo(db)
    repo.upsert_many([row(overall=75, hash_="pre")])
    rep = repo.upsert_many([row(overall=76, hash_="post")])
    assert rep.updated == 1
    got = db.execute("SELECT overall FROM team_ratings").fetchone()
    assert got["overall"] == 76
    assert repo.count() == 1  # updated in place, not duplicated


def test_same_team_distinct_editions_coexist(db):
    repo = TeamRatingRepo(db)
    rep = repo.upsert_many([
        row(edition="260045", hash_="a"),
        row(edition="260050", hash_="b", overall=76),
    ])
    assert rep.inserted == 2
    assert repo.count() == 2
    assert repo.count(edition="260050") == 1
