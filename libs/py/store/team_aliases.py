"""sofifa team-ratings -> canonical club resolution (docs/TEAM_RATINGS.md).

The third name-resolution layer. store.aliases bridges the betPawa fixture feed
to the results feed; this bridges sofifa /teams ratings onto the same canonical
matches.*_club spelling. Same silent-failure shape as the club join: a sofifa
name that resolves to nothing the model knows contributes no external rating,
and the club quietly keeps its GLM-only strength. Count the gaps
(clubs_without_rating); never guess a mapping.

Resolution is by EXACT string, not fuzzy. Most sofifa names already equal the
canonical club ('Manchester City'); the ~38 that do not each have a REVIEWED
row in team_rating_aliases (migration 008). Exact matching keeps every
non-identity mapping auditable -- fuzzy matching silently mislabels (sofifa
'Inter Miami' would otherwise grab canonical 'Inter Milan', 'Wigan Athletic'
would grab 'Athletic Bilbao'). Both sides are clean accented UTF-8; there is no
mojibake to normalize around.
"""

import sqlite3

from store.aliases import known_clubs


def team_alias_map(conn: sqlite3.Connection) -> dict[str, str]:
    """sofifa_name -> canonical club, the reviewed exceptions. Read once, not
    once per team."""
    return {r["sofifa_name"]: r["club"]
            for r in conn.execute("SELECT sofifa_name, club FROM team_rating_aliases")}


def resolve_club(sofifa_name: str, aliases: dict[str, str]) -> str:
    """A sofifa /teams name -> canonical club. Unmapped names pass through: most
    already equal the canonical spelling, and the rest must stay as-is so they
    surface in clubs_without_rating instead of being silently rewritten."""
    return aliases.get(sofifa_name, sofifa_name)


def club_ratings(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """canonical club -> its team_ratings row, limited to clubs the model has
    actually played (known_clubs). A club carrying several editions keeps the
    newest, since a defunct team can pin to an older one."""
    aliases = team_alias_map(conn)
    known = known_clubs(conn)
    out: dict[str, sqlite3.Row] = {}
    for r in conn.execute("SELECT * FROM team_ratings ORDER BY edition"):
        club = resolve_club(r["sofifa_name"], aliases)
        if club in known:
            out[club] = r  # ascending edition => the last write is the newest
    return out


def clubs_without_rating(conn: sqlite3.Connection) -> list[str]:
    """Canonical clubs (with a finished match) that have NO external rating: the
    absent-from-sofifa set (EA licensing -- Brazil, Belgium...) plus any missing
    alias. Watch it like store.aliases.unresolved_clubs -- a club that IS on
    sofifa yet lands here means a missing team_rating_aliases row, not a genuine
    source gap."""
    return sorted(known_clubs(conn) - set(club_ratings(conn)))
