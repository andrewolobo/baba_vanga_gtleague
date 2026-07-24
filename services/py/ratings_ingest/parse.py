"""Pure sofifa /teams HTML -> TeamRatingRow. No I/O — offline-testable against
data/raw/team_ratings/*.html.

Parsing keys on the STABLE semantic markup sofifa emits — the `data-col`
attribute on every rating cell, and the `s20`/`flag`/`sub` classes in the name
cell — not on column position. That means a reordered `teamCol` cookie can add,
drop or shuffle columns without landing a value in the wrong field: an absent
column simply yields None. `edition` is read from each row's own team link
(`/team/<id>/<slug>/<edition>/`), so it can never drift from the data it labels.

Technique note: targeted `re` over a machine-generated, dead-regular table
rather than a stateful HTMLParser — same stdlib, far less code, and the offline
fixture test is the guard. Raw HTML is archived per page (cli), so any structure
change is caught here and re-parseable without a re-fetch. Reads UTF-8: both
sofifa ('Mönchengladbach') and the matches club names are clean accented UTF-8
(no mojibake — a console mis-render made it look otherwise); the sofifa->club
join is by exact spelling through store.team_aliases, never here.
"""

import hashlib
import html as _html
import re
from datetime import datetime, timezone

from core.schema import TeamRatingRow


class ParseError(ValueError):
    """A page whose shape the parser no longer recognizes (structure change or
    a Cloudflare challenge that slipped past the fetch layer)."""


_TBODY = re.compile(r"<tbody[^>]*>(?P<body>.*?)</tbody>", re.S)
_ROW = re.compile(r"<tr>(?P<body>.*?)</tr>", re.S)
# name cell: team id + edition from the link, optional "<em>rank</em>&nbsp;"
# prefix stripped, name captured up to the closing anchor.
_NAME = re.compile(
    r'<td class="s20">\s*<a href="/team/(?P<id>\d+)/[^"/]*/(?P<edition>\d+)/?">'
    r'(?:<em>\d+</em>&nbsp;)?(?P<name>.*?)</a>',
    re.S,
)
_FLAG = re.compile(r'<img title="(?P<nat>[^"]*)"[^>]*class="flag"')
_LEAGUE = re.compile(r'href="/league/(?P<lid>\d+)"[^>]*class="sub">(?P<lname>[^<]*)</a>')
# one shape for every value cell: data-col, then an optional <em ...> wrapper
# (ratings/prestige/age) or bare text (money), captured up to the next tag.
_COLS = ("oa", "at", "md", "df", "dp", "ip", "ps", "sa", "tb", "cw")
_COL_RE = {
    c: re.compile(rf'data-col="{c}"[^>]*>(?:<em[^>]*>)?(?P<v>[^<]*)')
    for c in _COLS
}
_MONEY = re.compile(r"([\d.]+)\s*([KMBkmb]?)")
_SUFFIX = {"": 1, "k": 1e3, "m": 1e6, "b": 1e9}


def _int(s: str | None) -> int | None:
    if not s or not s.strip():
        return None
    try:
        return int(s.strip())
    except ValueError:
        return None


def _float(s: str | None) -> float | None:
    if not s or not s.strip():
        return None
    try:
        return float(s.strip())
    except ValueError:
        return None


def _money(s: str | None) -> float | None:
    """'€19.9M' -> 19_900_000.0, '€308.7M' -> 3.087e8, '€0' -> 0.0, blank ->
    None. The euro glyph and any stray markup are ignored; K/M/B scale."""
    if not s:
        return None
    m = _MONEY.search(s)
    if not m:
        return None
    return float(m.group(1)) * _SUFFIX[m.group(2).lower()]


def _cell(body: str, col: str) -> str | None:
    m = _COL_RE[col].search(body)
    return m.group("v") if m else None


def parse_teams(
    html_text: str,
    scraped_at: datetime | None = None,
) -> list[TeamRatingRow]:
    """Every team row on one /teams page — clubs and national teams both, since
    sofifa interleaves them in one listing. Raises ParseError if the page has no
    teams table (challenge / structure change) or a row lacks the id/name the
    pipeline cannot proceed without."""
    ts = scraped_at or datetime.now(timezone.utc)
    tb = _TBODY.search(html_text)
    if not tb:
        raise ParseError("no <tbody> — Cloudflare challenge or structure change")
    rows = [_row(r.group("body"), ts) for r in _ROW.finditer(tb.group("body"))]
    if not rows:
        raise ParseError("teams table present but zero rows parsed")
    return rows


def _row(body: str, scraped_at: datetime) -> TeamRatingRow:
    nm = _NAME.search(body)
    if not nm:
        raise ParseError(f"row missing team link/name: {body[:120]!r}")
    flag = _FLAG.search(body)
    lg = _LEAGUE.search(body)
    # National teams are interleaved with clubs and carry no club league link
    # (their flag shows the confederation, e.g. UEFA/CAF, and budget/worth are
    # €0). Absence of the /league/ sub-link IS the signal — coverage_report's
    # `national` count is the guard: a national count that swamps a clubs page
    # means the league regex broke, not that every team went international.
    is_national = lg is None
    # raw_hash over the source row body: the true "did sofifa change this team"
    # signal, mirroring MatchRow's sha1-of-source-row approach.
    raw_hash = hashlib.sha1(body.encode("utf-8")).hexdigest()
    return TeamRatingRow(
        sofifa_id=int(nm.group("id")),
        edition=nm.group("edition"),
        sofifa_name=_html.unescape(nm.group("name")).strip(),
        nationality=_html.unescape(flag.group("nat")).strip() if flag else None,
        league=_html.unescape(lg.group("lname")).strip() if lg else None,
        league_id=int(lg.group("lid")) if lg else None,
        is_national=is_national,
        overall=_int(_cell(body, "oa")),
        attack=_int(_cell(body, "at")),
        midfield=_int(_cell(body, "md")),
        defence=_int(_cell(body, "df")),
        domestic_prestige=_int(_cell(body, "dp")),
        international_prestige=_int(_cell(body, "ip")),
        num_players=_int(_cell(body, "ps")),
        starting_age=_float(_cell(body, "sa")),
        transfer_budget=_money(_cell(body, "tb")),
        club_worth=_money(_cell(body, "cw")),
        raw_hash=raw_hash,
        scraped_at=scraped_at,
    )


def coverage_report(rows: list[TeamRatingRow]) -> dict:
    """Ingest-time data-quality numbers, run on every scrape (mirrors the
    results ingester's quality_report). A field that is None across many rows
    means the `teamCol` cookie dropped that column — a silent scrape defect."""
    n = len(rows)
    editions = {r.edition for r in rows}
    return {
        "rows": n,
        "editions": sorted(editions),
        "national": sum(r.is_national for r in rows),
        "missing_overall": sum(r.overall is None for r in rows),
        "missing_worth": sum(r.club_worth is None for r in rows),
        "distinct_leagues": len({r.league for r in rows if r.league}),
        "distinct_ids": len({r.sofifa_id for r in rows}),
    }
