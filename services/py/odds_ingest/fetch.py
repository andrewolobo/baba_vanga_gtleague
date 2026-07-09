"""Network layer for the betPawa sportsbook feed. Network only.

The whole query rides URL-encoded in the `q` parameter; only the response is
protobuf. As of the 2026-07-08 probe the feed answers cookie-less with just
identity headers; cookie support is kept because the parent needed it.
On 401/403 this raises FeedError — an operational alert, never silent.
"""

import json

import httpx

from core.config import USER_AGENT, settings


class FeedError(RuntimeError):
    """Auth/blocking failure — re-capture scraper/betpawa.curl and update .env."""


LIST_PATH = "/api/sportsbook/v4/events/lists/by-queries"


def client() -> httpx.Client:
    s = settings()
    headers = {
        "Accept": "application/x-protobuf",
        "Content-Type": "application/x-protobuf",
        "User-Agent": USER_AGENT,
        "Referer": f"{s.betpawa_base}/events?categoryId={s.betpawa_category}"
                   f"&marketId=1X2&competitions={s.betpawa_competition}",
        "deviceType": "web",
        "X-Pawa-Brand": s.betpawa_brand,
        "X-Pawa-Language": "en",
    }
    if s.betpawa_fingerprint:
        headers["X-Device-Fingerprint"] = s.betpawa_fingerprint
    cookies = {}
    if s.betpawa_token:
        cookies["x-pawa-token"] = s.betpawa_token
    if s.betpawa_cf_bm:
        cookies["__cf_bm"] = s.betpawa_cf_bm
    return httpx.Client(base_url=s.betpawa_base, headers=headers,
                        cookies=cookies, timeout=30)


def build_query(skip: int = 0, take: int = 100,
                event_type: str = "UPCOMING") -> dict:
    s = settings()
    return {"queries": [{
        "query": {
            "eventType": event_type,
            "categories": [s.betpawa_category],
            "zones": {"competitions": [s.betpawa_competition]},
            "hasOdds": True,
        },
        "view": {"marketTypes": s.market_type_list},
        "skip": skip,
        "take": take,
    }]}


def fetch_page(c: httpx.Client, skip: int = 0, take: int = 100,
               event_type: str = "UPCOMING") -> bytes:
    q = json.dumps(build_query(skip, take, event_type), separators=(",", ":"))
    r = c.get(LIST_PATH, params={"q": q})
    if r.status_code in (401, 403):
        raise FeedError(
            f"token_expired: HTTP {r.status_code} — re-capture scraper/betpawa.curl "
            "and refresh BETPAWA_* env vars"
        )
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if "protobuf" not in ctype:
        raise FeedError(f"non-protobuf response ({ctype!r}) — likely a challenge page")
    return r.content
