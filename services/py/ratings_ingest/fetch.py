"""Network layer for sofifa.com/teams. Network only — no parsing.

sofifa sits behind Cloudflare, and the block is at the TLS layer: measured
2026-07-22, a valid cf_clearance cookie that works in Chrome returns 403 from
both httpx AND the system curl.exe, but 200 from curl_cffi impersonating
Chrome's TLS/JA3 fingerprint. So this layer uses curl_cffi — the cookie alone
is not enough, the handshake has to look like Chrome too. The cookie itself is
still pasted from a browser (sofifa_cookie_file / SOFIFA_COOKIE), IP+UA-bound
and hours-valid.

On a 403/503 this raises SourceError — re-paste the cookie — and NEVER retries:
a Cloudflare challenge does not clear on a repeat request. This is reference
data (cli refresh), not a live feed; nothing here runs on the predictor timer.

The default /teams order is roughly team-id, which scatters strong teams
through sofifa's ~15k-team catalog. We request col=oa&sort=desc so the crawl
walks Overall descending and can stop at a floor (settings.sofifa_min_overall)
instead of paging the whole site. The paging loop lives in the cli, because the
stop condition needs the parsed Overall — fetch stays network-only.
"""

import time

from curl_cffi import requests

from core.config import USER_AGENT, settings


class SourceError(RuntimeError):
    """Terminal fetch failure (Cloudflare challenge, schema change, persistent
    4xx/5xx). Re-capture data/auto-bet/cookie.txt from devtools."""


PAGE_SIZE = 60  # sofifa's fixed /teams page size == the offset step
_BLOCKED = (401, 403, 429, 503)  # Cloudflare / rate-limit; never retried


def load_cookie() -> str:
    """The cookie block, env first then the pasted file. A leading 'cookie:'
    and surrounding whitespace/newlines are tolerated so a raw devtools copy
    works as-is."""
    s = settings()
    raw = s.sofifa_cookie
    if not raw and s.sofifa_cookie_file and s.sofifa_cookie_file.exists():
        raw = s.sofifa_cookie_file.read_text(encoding="utf-8")
    raw = raw.strip()
    if raw[:7].lower() == "cookie:":
        raw = raw[7:].strip()
    if not raw:
        raise SourceError(
            f"no sofifa cookie — paste the cf_clearance block from devtools into "
            f"{s.sofifa_cookie_file} (or set SOFIFA_COOKIE)"
        )
    return raw


def client() -> requests.Session:
    """A curl_cffi session pinned to a Chrome TLS fingerprint. `impersonate`
    must stay a real Chrome build or Cloudflare re-challenges (see module doc)."""
    s = settings()
    return requests.Session(
        headers={
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "referer": f"{s.sofifa_base}/teams",
            "user-agent": USER_AGENT,  # must match the UA the cf_clearance was issued under
            "cookie": load_cookie(),
        },
        impersonate=s.sofifa_impersonate,
        timeout=30,
    )


def fetch_page(c: requests.Session, offset: int = 0,
               col: str = "oa", sort: str = "desc",
               edition: str | None = None) -> str:
    """One /teams page as raw HTML. col/sort default to Overall-descending so
    the crawl is bounded by a floor; pass col='' for sofifa's default order.
    edition pins the FC title as r=<edition> (settings.sofifa_edition by
    default) so the crawl stays on one roster vintage."""
    s = settings()
    params: dict[str, object] = {"offset": offset}
    if col:
        params["col"] = col
        params["sort"] = sort
    ed = s.sofifa_edition if edition is None else edition
    if ed:
        params["r"] = ed
    r = c.get(f"{s.sofifa_base}/teams", params=params, allow_redirects=False)
    if r.status_code in _BLOCKED:
        raise SourceError(
            f"blocked (HTTP {r.status_code}) at offset {offset} — cf_clearance "
            f"stale/invalid or TLS fingerprint rejected; re-paste "
            f"{s.sofifa_cookie_file} from devtools"
        )
    if r.status_code != 200:
        raise SourceError(f"HTTP {r.status_code} at offset {offset}")
    ctype = r.headers.get("content-type", "")
    if "text/html" not in ctype:
        raise SourceError(f"non-HTML response ({ctype!r}) at offset {offset} "
                          "— likely a Cloudflare challenge page")
    return r.text


def polite_sleep() -> None:
    time.sleep(settings().sofifa_request_delay_s)
