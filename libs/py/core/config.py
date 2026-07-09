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
    pick_prob_threshold: float = 0.60  # blended confidence needed to flag a pick
    max_push_prob: float = 0.20
    min_edge: float = 0.05  # model−book edge for value_flag
    tier_lean: float = 0.50  # re-quantile after first settled week
    tier_solid: float = 0.58
    tier_strong: float = 0.66

    @property
    def market_type_list(self) -> list[str]:
        return [m.strip() for m in self.betpawa_market_types.split(",") if m.strip()]


@lru_cache
def settings() -> Settings:
    return Settings()
