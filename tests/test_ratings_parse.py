"""Offline parse of a real sofifa /teams page (scraper/fixtures/sofifa_teams_
page.html, captured 2026-07-22). Network and storage are tested elsewhere; this
pins the HTML->TeamRatingRow contract against source markup, so a sofifa
structure change fails here rather than silently corrupting ratings."""

import pytest

from ratings_ingest.parse import ParseError, _money, coverage_report, parse_teams


def test_row_count(sofifa_html):
    assert len(parse_teams(sofifa_html)) == 60


def test_club_row_all_fields(sofifa_html):
    rows = parse_teams(sofifa_html)
    r = next(r for r in rows if r.sofifa_id == 23)  # Borussia Mönchengladbach
    assert r.edition == "260045"
    assert r.sofifa_name == "Borussia Mönchengladbach"  # UTF-8 intact, rank stripped
    assert r.nationality == "Germany"
    assert (r.league, r.league_id) == ("Bundesliga", 19)
    assert r.is_national is False
    assert (r.overall, r.attack, r.midfield, r.defence) == (75, 77, 75, 75)
    assert (r.domestic_prestige, r.international_prestige) == (6, 5)
    assert (r.num_players, r.starting_age) == (30, 26.36)
    assert r.transfer_budget == 19_900_000.0   # €19.9M
    assert r.club_worth == 308_700_000.0        # €308.7M


def test_rank_prefix_stripped(sofifa_html):
    # the "<em>26</em>&nbsp;" rank must never leak into the name
    assert not any(r.sofifa_name[0].isdigit() for r in parse_teams(sofifa_html))


def test_national_teams_detected_structurally(sofifa_html):
    nats = {r.sofifa_name for r in parse_teams(sofifa_html) if r.is_national}
    assert nats == {"England", "South Africa", "Cabo Verde"}


def test_national_teams_have_no_league_and_zero_worth(sofifa_html):
    for r in parse_teams(sofifa_html):
        if r.is_national:
            assert r.league is None and r.league_id is None
            assert r.club_worth == 0 and r.transfer_budget == 0
        else:
            assert r.league is not None  # every club carries a league link


def test_edition_is_per_team_not_per_page(sofifa_html):
    # a dissolved club (Bordeaux, id 59) pins to its last edition while the
    # rest of the page is 260045 — the (sofifa_id, edition) PK depends on this
    rows = {r.sofifa_id: r for r in parse_teams(sofifa_html)}
    assert rows[59].edition == "240050"
    assert rows[23].edition == "260045"


def test_coverage_report(sofifa_html):
    rep = coverage_report(parse_teams(sofifa_html))
    assert rep["rows"] == 60
    assert rep["national"] == 3
    assert rep["missing_overall"] == 0
    assert rep["distinct_ids"] == 60
    assert set(rep["editions"]) == {"240050", "260045"}


def test_challenge_page_raises(sofifa_html):
    with pytest.raises(ParseError):
        parse_teams("<html><body>Just a moment... (Cloudflare)</body></html>")


@pytest.mark.parametrize("text,expected", [
    ("€19.9M", 19_900_000.0),
    ("€308.7M", 308_700_000.0),
    ("€1.2B", 1_200_000_000.0),   # elite clubs (not on this page) use billions
    ("€900K", 900_000.0),
    ("€0", 0.0),
    ("", None),
    (None, None),
])
def test_money_parsing(text, expected):
    assert _money(text) == expected
