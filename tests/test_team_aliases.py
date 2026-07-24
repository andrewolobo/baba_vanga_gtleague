"""sofifa->canonical club resolution + the migration-008 alias seed. The seed
is applied to every DB (including this test's), so the exact-match bridge and
the silent-gap accounting are pinned here against real seeded rows."""

from datetime import datetime, timezone

from core.schema import TeamRatingRow
from store.repo import MatchRepo, TeamRatingRepo
from store.team_aliases import (club_ratings, clubs_without_rating,
                                resolve_club, team_alias_map)
from tests.test_store import row as match_row

NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)


def rating(name, overall=80, edition="250044", **kw) -> TeamRatingRow:
    return TeamRatingRow(sofifa_id=abs(hash((name, edition))) % 10**7,
                         edition=edition, sofifa_name=name, overall=overall,
                         raw_hash=f"{name}-{edition}", scraped_at=NOW, **kw)


def test_seed_loaded_and_exact(db):
    aliases = team_alias_map(db)
    # a few reviewed pairs incl. the ones a fuzzy matcher gets wrong
    assert aliases["Juventus"] == "Juventus Turin"
    assert aliases["Paris Saint-Germain"] == "PSG"
    assert aliases["Inter"] == "Inter Milan"
    assert aliases["Napoli"] == "SSC Napoli"
    assert aliases["Czechia"] == "Czech Republic"
    # migration 009: the accent/case-only pairs the loose 008 audit missed
    assert aliases["Atlético Madrid"] == "Atletico Madrid"
    assert aliases["Olympique Lyonnais"] == "Olympique lyonnais"


def test_accented_pair_is_byte_exact(db):
    # the seed carries accents verbatim (no mojibake); it must equal what the
    # scraper stores for the same team
    assert team_alias_map(db)["FC Bayern München"] == "Bayern München"


def test_resolve_passes_through_exact_names(db):
    aliases = team_alias_map(db)
    assert resolve_club("Manchester City", aliases) == "Manchester City"
    assert resolve_club("Juventus", aliases) == "Juventus Turin"


def test_club_ratings_joins_known_clubs_only(db):
    MatchRepo(db).upsert_many([
        match_row(source_id="1", home_club="Juventus Turin", away_club="PSG"),
    ])
    TeamRatingRepo(db).upsert_many([
        rating("Juventus", overall=80),                # alias -> Juventus Turin
        rating("Paris Saint-Germain", overall=84),     # alias -> PSG
        rating("Bodrumspor", overall=66),              # not a known club -> ignored
    ])
    cr = club_ratings(db)
    assert set(cr) == {"Juventus Turin", "PSG"}
    assert cr["Juventus Turin"]["overall"] == 80
    assert cr["PSG"]["overall"] == 84


def test_newest_edition_wins(db):
    MatchRepo(db).upsert_many([match_row(source_id="1", home_club="Juventus Turin")])
    TeamRatingRepo(db).upsert_many([
        rating("Juventus", overall=79, edition="240050"),
        rating("Juventus", overall=81, edition="250044"),
    ])
    assert club_ratings(db)["Juventus Turin"]["overall"] == 81


def test_clubs_without_rating_flags_absent(db):
    # Brazil is absent from sofifa (EA licensing); Juventus is present via alias
    MatchRepo(db).upsert_many([
        match_row(source_id="1", home_club="Juventus Turin", away_club="Brazil"),
    ])
    TeamRatingRepo(db).upsert_many([rating("Juventus", overall=80)])
    gaps = clubs_without_rating(db)
    assert "Brazil" in gaps
    assert "Juventus Turin" not in gaps
