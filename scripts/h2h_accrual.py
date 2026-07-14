"""H2H stacker engagement tracker: rows accrued vs the X12_H2H_MIN_N bar.

  .venv/Scripts/python scripts/h2h_accrual.py

Counts exactly what `h2h.fit_stacker` counts — decisive settled 1x2 rows in
the last X12_H2H_DAYS, via h2h's own fit queries — so the numbers here can
never drift from the engagement logic. Read-only; safe while the app runs.

Exit code 0 when every population has cleared the bar (flip
X12_H2H_ENABLED=true in .env; live switch, next timer cycle), 1 while any
population is still accruing.
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from core.config import settings
from model import h2h

# accrual rate from a fixed recent span, not calendar days — immune to
# partial-day undercounting right after midnight UTC
RATE_WINDOW_H = 48


def main() -> int:
    s = settings()
    conn = sqlite3.connect(f"file:{s.gtl_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=s.x12_h2h_days)).isoformat()
    rate_since = (now - timedelta(hours=RATE_WINDOW_H)).isoformat()

    print(f"h2h engagement: min_n={s.x12_h2h_min_n} per population, "
          f"window {s.x12_h2h_days}d | flags: x12_enabled={s.x12_enabled} "
          f"x12_h2h_enabled={s.x12_h2h_enabled}")

    all_ready = True
    for pop in ("priced", "schedule"):
        rows = conn.execute(h2h._FIT_QUERIES[pop], (since,)).fetchall()
        n = len(rows)
        homes = sum(1 for r in rows if r["result"] == "home")
        both = 0 < homes < n  # degenerate window cannot fit
        ready = n >= s.x12_h2h_min_n and both

        pop_filter = ("s.event_id LIKE 'gtl:%'" if pop == "schedule"
                      else "NOT s.event_id LIKE 'gtl:%'")
        recent = conn.execute(
            f"SELECT COUNT(*) c FROM settlements_x12 s"
            f" WHERE {pop_filter} AND s.result != 'draw'"
            f" AND s.settled_at >= ?", (rate_since,)).fetchone()["c"]
        rate = recent / (RATE_WINDOW_H / 24.0)

        line = (f"  {pop:<9} {n:>4}/{s.x12_h2h_min_n}"
                f"  ({homes} home / {n - homes} away)"
                f"  accruing {rate:.0f}/day")
        if ready:
            line += "  READY"
        elif rate > 0:
            eta = now + timedelta(days=(s.x12_h2h_min_n - n) / rate)
            line += f"  ETA ~{eta.strftime('%Y-%m-%d %H:%M')}Z"
        else:
            line += "  no recent accrual -- is settlement running?"
        if n >= s.x12_h2h_min_n and not both:
            line += "  (n cleared but only one outcome seen: cannot fit)"
        print(line)
        all_ready = all_ready and ready

    print("all populations READY -- flip X12_H2H_ENABLED=true when you"
          " choose (docs/H2H_FEATURE.md #Enabling)" if all_ready else
          "below the bar -- flipping early is safe: identity, untagged,"
          " engages automatically per population")
    return 0 if all_ready else 1


if __name__ == "__main__":
    sys.exit(main())
