"""value_flag EV gate (docs/VALUE_FLAG.md): the helper's engagement contract,
the '-ev' tagging in both row builders, and flag-off legacy equivalence."""

from datetime import datetime, timezone

import pytest

from core.config import settings
from model import screen
from model.screen import ScreenStats
from predictor.cycle import _line_row, _x12_row

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)

# 60% band hit over n=50: clears break-even at odds 1.9 (52.6%), not at 1.6
WARM = ScreenStats(band_hit={b: (30, 50) for b, *_ in screen.EDGE_BANDS})


@pytest.fixture
def ev_on(monkeypatch):
    monkeypatch.setattr(settings(), "value_ev_enabled", True)
    return settings()


# ── helper: engagement contract ──────────────────────────────────────────────

def test_flag_off_is_legacy_untagged():
    s = settings()
    assert screen.value_ev(0.60, 0.52, 1.9, WARM, s) == (True, False)
    assert screen.value_ev(0.55, 0.52, 1.9, WARM, s) == (False, False)
    # the historical `or 0.0` quirk: no book prob -> edge vs 0.0
    assert screen.value_ev(0.60, None, 1.9, WARM, s) == (True, False)


def test_cold_paths_fall_back_to_legacy(ev_on):
    # no stats (screen off), empty band stats, no book, junk odds — all
    # legacy, all untagged
    for stats, book_p, odds in ((None, 0.52, 1.9),
                                (ScreenStats(), 0.52, 1.9),
                                (WARM, None, 1.9),
                                (WARM, 0.52, None),
                                (WARM, 0.52, 1.0)):
        assert screen.value_ev(0.60, book_p, odds, stats, ev_on) \
            == (True, False)


def test_engaged_requires_edge_band_evidence_and_breakeven(ev_on):
    # edge below min_edge: engaged, no value
    assert screen.value_ev(0.55, 0.52, 1.9, WARM, ev_on) == (False, True)
    # edge 8pts (value band), band 60% vs break-even 52.6% -> value
    assert screen.value_ev(0.60, 0.52, 1.9, WARM, ev_on) == (True, True)
    # same band, shorter price: break-even 62.5% > 60% -> no value
    assert screen.value_ev(0.60, 0.52, 1.6, WARM, ev_on) == (False, True)
    # thin band: engaged, no value
    thin = ScreenStats(band_hit={"value": (17, 29)})
    assert screen.value_ev(0.60, 0.52, 1.9, thin, ev_on) == (False, True)
    # band missing from stats entirely: engaged, no value
    other = ScreenStats(band_hit={"agree": (30, 50)})
    assert screen.value_ev(0.60, 0.52, 1.9, other, ev_on) == (False, True)


def test_margin_raises_the_bar(ev_on, monkeypatch):
    monkeypatch.setattr(settings(), "value_ev_margin", 0.10)
    # 60% band clears 52.6% but not 62.6%
    assert screen.value_ev(0.60, 0.52, 1.9, WARM, ev_on) == (False, True)


# ── cycle wiring ─────────────────────────────────────────────────────────────

F = {"event_id": "E1"}
SELS = {"over": (1.9, 0.52), "under": (1.9, 0.48)}


def _mk_line_row(s, scr):
    # lam 3+3 at line 2.5 -> p_over ~0.9: leaned side over, edge ~0.38
    # (absurd band; WARM stats carry every band at 60%)
    return _line_row(F, 2.5, SELS, 3.0, 3.0, NOW, NOW, "v", s, {}, 0.0,
                     scr=scr)


def test_line_row_tags_ev_when_engaged(ev_on):
    row = _mk_line_row(ev_on, WARM)
    assert row.value_flag is True  # 60% band >= 52.6% break-even
    assert row.model_version.endswith("-ev")


def test_line_row_flag_off_keeps_legacy_and_no_tag():
    row = _mk_line_row(settings(), WARM)
    assert row.value_flag is True  # legacy: edge >= min_edge
    assert "-ev" not in row.model_version


def test_line_row_cold_stats_stay_legacy_untagged(ev_on):
    row = _mk_line_row(ev_on, ScreenStats())
    assert row.value_flag is True
    assert "-ev" not in row.model_version


def test_x12_row_tags_ev_when_engaged(ev_on, monkeypatch):
    # lopsided λs -> home prob clears the 0.50 x12 gate
    odds = {"home": (1.9, 0.52), "draw": (5.0, 0.18), "away": (3.2, 0.30)}
    warm = ScreenStats(market="x12",
                       band_hit={b: (30, 50) for b, *_ in screen.EDGE_BANDS})
    row = _x12_row(F, 3.5, 0.8, odds, NOW, NOW, "v", ev_on, scr=warm)
    assert row.pick == "home"
    assert row.model_version.endswith("-ev")
    assert row.value_flag == (row.p_home - 0.52 >= ev_on.min_edge)


def test_x12_unpriced_row_stays_false_untagged(ev_on):
    row = _x12_row(F, 3.5, 0.8, {}, NOW, NOW, "v", ev_on,
                   scr=ScreenStats(market="x12"))
    assert row.value_flag is False
    assert "-ev" not in row.model_version
