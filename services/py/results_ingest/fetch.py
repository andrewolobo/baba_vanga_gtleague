"""Network layer for the GT Leagues fixtures API. Network only — no parsing.

The API answers plain HTTP but 451s anonymous clients; origin/referer/UA
headers are required (measured, PHASE0_PROBES.md §0.1). Polite by design:
single-threaded, fixed delay between page requests.
"""

import time

import httpx

from core.config import USER_AGENT, settings


class SourceError(RuntimeError):
    """Terminal fetch failure (blocked, schema change, persistent 4xx/5xx)."""


PAGE_SIZE = 50
MAX_PAGES = 40  # hard stop: > 2000 fixtures/day means the source changed shape


def client() -> httpx.Client:
    s = settings()
    return httpx.Client(
        base_url=s.gtl_api_base,
        headers={
            "accept": "application/json",
            "origin": s.gtl_origin,
            "referer": s.gtl_origin + "/",
            "user-agent": USER_AGENT,
        },
        timeout=30,
    )


def fetch_window(
    c: httpx.Client,
    start_iso: str,
    end_iso: str,
    statuses: str = "3,4",
) -> list[dict]:
    """All fixtures with kickoff in [start, end] (UTC ISO), paged; raw dicts."""
    s = settings()
    out: list[dict] = []
    for page in range(MAX_PAGES):
        params = {
            "kickoff": f"between:{start_iso},{end_iso}",
            "limit": PAGE_SIZE,
            "offset": page * PAGE_SIZE,
            "sort": "kickoff,matchNr",
            "status": f"in:{statuses}",
            "xtc": "true",
        }
        r = c.get("/fixtures", params=params)
        if r.status_code in (401, 403, 451):
            raise SourceError(
                f"blocked (HTTP {r.status_code}) — identity headers rejected; "
                "re-capture scraper/scrape.curl from devtools"
            )
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list):
            raise SourceError(f"unexpected response shape: {type(batch).__name__}")
        out.extend(batch)
        if len(batch) < PAGE_SIZE:
            return out
        time.sleep(s.gtl_request_delay_s)
    raise SourceError(f"window {start_iso}..{end_iso} exceeded {MAX_PAGES} pages")


def day_window(date: str) -> tuple[str, str]:
    """Full UTC-day kickoff window for a YYYY-MM-DD date."""
    return f"{date}T00:00:00.000Z", f"{date}T23:59:59.999Z"
