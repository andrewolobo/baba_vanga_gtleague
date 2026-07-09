"""Reverse-engineered betPawa field map → clean fixtures + odds rows.

Field numbers documented in docs/BETPAWA_FEED.md §5.2; verified unchanged for
GT Leagues in the 2026-07-08 probe. Pure bytes→objects — offline-testable
against scraper/fixtures/betpawa_gtleagues_*.bin.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from core.schema import FixtureRow, OddsPrice

from .protobuf_decode import as_f64, as_msg, as_str, field, fields, walk

_PLAYER = re.compile(r"\(([^)]*)\)")

MARKET_1X2 = "3743"
MARKET_OU = "5000"
_1X2_OUTCOME = {"1": "home", "X": "draw", "2": "away"}


@dataclass
class NormalizedEvent:
    fixture: FixtureRow
    prices: list[OddsPrice]


def decode_events(raw: bytes) -> list[NormalizedEvent]:
    out: list[NormalizedEvent] = []
    for f, w, v in walk(raw):
        if f == 1 and w == 2:
            for f2, w2, v2 in walk(v):  # wrapper
                if f2 == 2 and w2 == 2:  # EVENT
                    out.append(_event(walk(v2)))
    return out


def _event(ev) -> NormalizedEvent:
    event_id = as_str(field(ev, 1)) or ""
    name = as_str(field(ev, 2)) or ""
    start_unix = field(as_msg(field(ev, 6)), 1)
    competition = as_str(field(as_msg(field(ev, 12)), 2)) or ""

    # participant side flag (.5.3, 1=home 2=away) is the authority — never
    # infer home/away from name order (parent §5.2 note)
    sides: dict[int, str] = {}
    for pm in fields(ev, 5):
        p = walk(pm)  # type: ignore[arg-type]
        side = field(p, 3)
        pname = as_str(field(p, 2)) or ""
        if isinstance(side, int):
            sides[side] = pname
    home_raw, away_raw = sides.get(1, ""), sides.get(2, "")

    fixture = FixtureRow(
        event_id=event_id,
        start_time_utc=datetime.fromtimestamp(int(start_unix), timezone.utc),  # type: ignore[arg-type]
        competition=competition,
        home_raw=home_raw,
        away_raw=away_raw,
        home_player=_player(home_raw),
        away_player=_player(away_raw),
    )

    prices: list[OddsPrice] = []
    for mm in fields(ev, 7):  # MARKET
        m = walk(mm)  # type: ignore[arg-type]
        mtype = as_str(field(as_msg(field(m, 1)), 1))
        market = as_msg(field(m, 2))
        for pr in fields(market, 4):  # PRICE
            p = walk(pr)  # type: ignore[arg-type]
            odds = as_f64(field(p, 4))
            label = as_str(field(p, 8)) or ""
            line = as_str(field(p, 6))
            implied = as_f64(field(as_msg(field(p, 10)), 1))
            if odds is None:
                continue
            sel = _selection(mtype, label)
            if sel is None:
                continue
            prices.append(OddsPrice(
                event_id=event_id,
                market="1x2" if mtype == MARKET_1X2 else "ou",
                line=float(line) if line else None,
                selection=sel,
                odds=round(odds, 3),
                implied_prob=round(implied, 6) if implied is not None else None,
            ))
    return NormalizedEvent(fixture=fixture, prices=prices)


def _player(raw_name: str) -> str | None:
    m = _PLAYER.search(raw_name)
    return m.group(1).strip() or None if m else None


def _selection(market_type: str | None, label: str) -> str | None:
    if market_type == MARKET_1X2:
        return _1X2_OUTCOME.get(label)
    if market_type == MARKET_OU:
        low = label.lower()
        if low.startswith("over"):
            return "over"
        if low.startswith("under"):
            return "under"
    return None
