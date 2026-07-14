"""Name resolution between the betPawa feed and the GT Leagues results feed.

Fixture names are `"Club (Player)"`. The player inside the parentheses is the
primary model entity; the club outside it is the second one
(docs/CLUB_FEATURE.md). Both need mapping onto the results feed's spellings.

The asymmetry that motivates this module: a failed *player* join is loud — the
fixture drops to `coverage='none'` and the cycle prices it off the book. A
failed *club* join is silent — the club is simply unknown to the GLM, gets
catt = cdfn = 0, and the prediction degrades to the player-only λ with no
error anywhere. That is the correct behavior at a regime switch (every club
is cold for ~a week) and a silent bug the rest of the time, so the only
defense is counting unresolved names. Use `unresolved_clubs` in the cycle.

The regex mirrors the Node API's `CLUB()` in services/api/src/routes.ts —
keep them in step.
"""

import re
import sqlite3

_TRAILING_PARENS = re.compile(r"\s*\([^)]*\)\s*$")


def club_from_raw(raw: str) -> str:
    """'Germany (Tifosi)' -> 'Germany'. Idempotent on already-bare names."""
    return _TRAILING_PARENS.sub("", raw).strip()


def club_alias_map(conn: sqlite3.Connection) -> dict[str, str]:
    """book_name -> canonical club. Read once per cycle, not once per fixture."""
    return {r["book_name"]: r["club"]
            for r in conn.execute("SELECT book_name, club FROM club_aliases")}


def resolve_club(raw: str, aliases: dict[str, str]) -> str:
    """Fixture-side raw name -> canonical club. Unmapped names pass through:
    most already match, and the rest must reach the GLM as-is so they land in
    `unresolved_clubs` rather than being silently rewritten."""
    return aliases.get(club_from_raw(raw), club_from_raw(raw))


def known_clubs(conn: sqlite3.Connection) -> set[str]:
    """Canonical clubs the model could have learned — those with a finished
    match. A club outside this set contributes nothing to any λ."""
    rows = conn.execute(
        "SELECT DISTINCT home_club c FROM matches WHERE status = 3"
        " UNION SELECT DISTINCT away_club FROM matches WHERE status = 3")
    return {r["c"] for r in rows}


def unresolved_clubs(conn: sqlite3.Connection, raws: list[str]) -> set[str]:
    """Fixture club names that resolve to nothing the model has ever seen.

    Expected non-empty right after a regime switch (World Cup countries are
    all cold) and after betPawa adds a team. Persistently non-empty for a club
    that *is* playing means a missing `club_aliases` row and a quietly
    degraded model — alert on that, not on the transient case.
    """
    aliases, known = club_alias_map(conn), known_clubs(conn)
    return {c for c in (resolve_club(r, aliases) for r in raws) if c not in known}
