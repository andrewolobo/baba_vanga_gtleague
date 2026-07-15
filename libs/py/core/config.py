"""Typed settings for every Python service. Single source: environment / .env.

Values marked "measured" come from the Phase 0 probes (docs/PHASE0_PROBES.md);
they are constants of the source's behavior, not tunables.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Browser identity shared by both ingesters; sites 451/403 anonymous clients.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

# GT Leagues fixture status codes (observed; see PHASE0_PROBES.md).
STATUS_SCHEDULED = 0
STATUS_FINISHED = 3
STATUS_CANCELLED = 4

# Measured join geometry: feed start_time == results kickoff exactly.
FEED_OFFSET_MIN = 0
OFFSET_TOL_MIN = 5


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # storage
    gtl_db_path: Path = Path("data/gtleague.db")

    # GT Leagues results API
    gtl_api_base: str = "https://api.gtleagues.com/api"
    gtl_origin: str = "https://www.gtleagues.com"
    gtl_request_delay_s: float = 0.5

    # betPawa odds feed
    betpawa_base: str = "https://www.betpawa.ug"
    betpawa_brand: str = "betpawa-uganda"
    betpawa_category: str = "101"
    betpawa_competition: str = "17491"
    betpawa_market_types: str = "3743,5000"
    betpawa_fingerprint: str = ""
    betpawa_token: str = ""
    betpawa_cf_bm: str = ""

    # predictor serving knobs (Phase-4-validated defaults; see PHASE4_RESULTS.md)
    totals_source: str = "blend"  # blend | poisson (safe fallback, −0.4 AUC pts)
    totals_blend_weight: float = 0.7  # poisson share of the λ-blend
    half_life_days: float = 7.0
    poisson_alpha: float = 0.01
    form_span: int = 8
    form_window_days: int = 14  # slice fed to the form leg each cycle
    slate_horizon_hours: float = 6.0  # how far ahead to predict fixtures
    pick_prob_threshold: float = 0.60  # model prob of a side needed to pick it
    max_push_prob: float = 0.20
    min_edge: float = 0.05  # model−book edge for value_flag
    # per-line Platt recalibration of served probs (docs/RECAL_SERVING.md;
    # RECAL_ENABLED=false is the one-flag rollback to pre-recal behavior)
    recal_enabled: bool = True
    recal_days: int = 14     # settled-prediction window the maps are fit on
    recal_min_n: int = 300   # graded samples a line needs for its OWN 2-param map
    # hierarchical tier: a thinner line (>= this many samples) borrows the
    # slope pooled across all lines and fits only its own intercept — tail
    # lines (4.5/5.5) engage days earlier. 0 disables the tier (pre-2026-07-11
    # per-line-only engagement); RECAL_ENABLED=false still disables everything.
    recal_min_n_line: int = 75
    # Club as a second GLM entity (docs/CLUB_FEATURE.md). ENABLED in prod
    # 2026-07-10. CLUB_ENABLED=false is the one-flag rollback. Note the API
    # spawns predictor cycles on a timer, so changing this default takes effect
    # on the next cycle with no deploy — treat it as a live switch.
    club_enabled: bool = True
    # 1x2 serving head (docs/X12_SERVING.md; gate PASSED 2026-07-11). Defaults
    # OFF — the timer makes any True default an instant ship. The gate is NOT
    # the totals 0.60: max-of-three medians 0.434 on this league, and 0.50 was
    # measured at 59% hit on 11% of matches; 0.60 would surface ~1%.
    x12_enabled: bool = False
    x12_pick_prob_threshold: float = 0.50
    # H2H stacker on the 1x2 head (docs/H2H_FEATURE.md; recorded 90-day gate
    # PASSED 2026-07-13: +1.84 AUC pts, pairwise +1.48 over the skill
    # control). Default OFF — same timer live-switch caveat as every flag.
    # Only meaningful with x12_enabled: the stacker reshapes the decisive
    # split of x12 rows and touches nothing else. Fit on settled
    # predictions_x12 per population (never pooled); a population below
    # x12_h2h_min_n decisive graded rows serves identity, untagged.
    x12_h2h_enabled: bool = False
    x12_h2h_days: int = 14
    x12_h2h_min_n: int = 500
    # Minutes after scheduled kickoff before a fixture is ELIGIBLE for
    # settlement — a first-touch churn guard, not a correctness guard (a
    # missing result leaves the fixture pending; the NOT EXISTS loop
    # retries). Measured 2026-07-15 on 14d of live-captured results:
    # arrival at kickoff +21 min p50 / +24 p75, physical floor ~25 (13-min
    # match + feed publish lag + 10-min results merge cadence). 30 clears
    # the floor with drift margin; the arrival tail (p90 ~58) rides the
    # pending retry either way. Do not lower further without re-measuring
    # the arrival lag. Was 45 (component-sum guess) until 2026-07-15.
    settle_delay_min: int = 30
    # Settle schedule-only (gtl:) predictions against their own match rows
    # (docs/POPULATION_SPLIT.md Phase 1). Additive and default-on: rows land
    # in the settlements table but every pre-existing consumer joins through
    # fixtures, so they are invisible until a query opts in. The first
    # enabled run back-settles the full gtl history (NOT EXISTS). One-flag
    # rollback; same live-switch caveat as club_enabled.
    schedule_settle_enabled: bool = True
    # Cyclic time-of-day term in the GLM (docs/TOD_FEATURE.md). Gate passed
    # 2026-07-10 both standalone and on top of club (+0.11..0.18 AUC pts over
    # the served blend+club). Default OFF: enabling is a deploy decision.
    # Same live-switch caveat as club_enabled.
    tod_enabled: bool = False
    # Tier bands partition the picked region [pick_prob_threshold, 1]. Keep
    # tier_lean == pick_prob_threshold: a band below the gate can never be
    # assigned, because _tier only runs once the gate has passed.
    # Re-quantiled 2026-07-12 from the schedule (model-only) population —
    # the volume population post-split (docs/POPULATION_SPLIT.md Phase 3);
    # proposals were 0.6404/0.689–0.692, graded 59.5/64.6/77.8 under them.
    # Bands apply to BOTH populations; rows keep the tier they were served
    # with, so tier analytics spanning 2026-07-12 mix band regimes.
    tier_lean: float = 0.60  # 47% of picks
    tier_solid: float = 0.64  # 37%
    tier_strong: float = 0.69  # top 17% — 'strong' has to stay selective

    @property
    def market_type_list(self) -> list[str]:
        return [m.strip() for m in self.betpawa_market_types.split(",") if m.strip()]


@lru_cache
def settings() -> Settings:
    return Settings()
