"""Club identity resolution (docs/CLUB_FEATURE.md step 2).

A failed club join raises nothing and drops nothing — it silently degrades the
prediction to player-only. Every test here exists because the failure mode is
quiet.
"""

import pytest

from store import aliases
from store.db import migrate


def _match(conn, i, home_club, away_club, status=3):
    conn.execute(
        "INSERT INTO matches (source_match_id, date, kickoff_ts, competition,"
        " home_player, away_player, home_club, away_club, status, home_ft,"
        " away_ft, raw_hash, scraped_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(i), "2026-07-01", f"2026-07-01T{i:02d}:00:00+00:00", "WC",
         f"P{i}", f"Q{i}", home_club, away_club, status,
         2 if status == 3 else None, 1 if status == 3 else None, "h", "t"),
    )


def test_migration_creates_and_seeds(db):
    rows = dict(db.execute("SELECT book_name, club FROM club_aliases").fetchall())
    assert rows["Congo Dr"] == "DR Congo"
    assert rows["Czechia"] == "Czech Republic"
    assert rows["Ivory Coast"] == "Cote d' Ivoire"  # the escaped apostrophe
    assert rows["Korea Republic"] == "South Korea"
    assert rows["Turkiye"] == "Turkey"
    assert rows["Bosnia And Herzegovina"] == "Bosnia Herzegovina"


def test_usa_is_deliberately_unseeded(db):
    """betPawa lists USA and United States; neither has a results-side spelling
    yet. Seeding a guess would silently mis-join once they play."""
    names = {r["book_name"] for r in db.execute("SELECT book_name FROM club_aliases")}
    assert "USA" not in names and "United States" not in names


def test_migration_is_idempotent(db):
    before = db.execute("SELECT COUNT(*) FROM club_aliases").fetchone()[0]
    migrate(db)  # re-running must not duplicate or fail on the seeds
    assert db.execute("SELECT COUNT(*) FROM club_aliases").fetchone()[0] == before


@pytest.mark.parametrize("raw,want", [
    ("Germany (Tifosi)", "Germany"),
    ("Bayern München (Snail)", "Bayern München"),
    ("DR Congo", "DR Congo"),               # already bare
    ("Cote d' Ivoire (X)", "Cote d' Ivoire"),  # apostrophe survives
    ("Real Betis  (A B)", "Real Betis"),    # padded
])
def test_club_from_raw(raw, want):
    assert aliases.club_from_raw(raw) == want
    assert aliases.club_from_raw(want) == want  # idempotent


def test_resolve_maps_and_passes_through(db):
    amap = aliases.club_alias_map(db)
    assert aliases.resolve_club("Congo Dr (Kabila)", amap) == "DR Congo"
    assert aliases.resolve_club("Turkiye", amap) == "Turkey"
    # unmapped names must reach the GLM unrewritten so they show up as
    # unresolved rather than being silently coerced onto a wrong club
    assert aliases.resolve_club("Brazil (Zico)", amap) == "Brazil"
    assert aliases.resolve_club("USA (Donovan)", amap) == "USA"


def test_no_alias_target_is_itself_a_book_name(db):
    """Guards against a chained alias (A->B, B->C), which resolve() would only
    follow one hop of. Canonical names must be terminal."""
    rows = db.execute("SELECT book_name, club FROM club_aliases").fetchall()
    book = {r["book_name"] for r in rows}
    assert {r["club"] for r in rows} & book == set()


def test_unresolved_clubs_flags_only_unknown(db):
    _match(db, 1, "DR Congo", "Turkey")
    _match(db, 2, "Brazil", "Germany")
    db.commit()

    # aliased and exact names resolve onto known clubs -> nothing unresolved
    assert aliases.unresolved_clubs(
        db, ["Congo Dr (A)", "Turkiye (B)", "Brazil (C)"]) == set()
    # a genuinely new club surfaces, under its canonical (post-alias) spelling
    assert aliases.unresolved_clubs(db, ["USA (D)", "Germany (E)"]) == {"USA"}


def test_unresolved_ignores_unplayed_clubs(db):
    """known_clubs is finished matches only: a club that appears solely on the
    upcoming schedule has taught the GLM nothing and must count as unresolved."""
    _match(db, 1, "Brazil", "Germany", status=0)  # scheduled, not finished
    db.commit()
    assert aliases.unresolved_clubs(db, ["Brazil (A)"]) == {"Brazil"}
