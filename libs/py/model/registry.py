"""Fitted-artifact cache. The cost asymmetry made explicit (plan §3.1):

- Poisson is day-frozen: fit once per UTC day on all strictly-earlier days,
  pickled to disk so every spawned predictor cycle after the first loads it
  in milliseconds (the API spawns cycles as short-lived processes in dev).
- The form leg is cheap and always fresh: rebuilt every cycle from a recent
  window, never cached.

Staleness guard: the artifact stores a fingerprint of its training slice
(row count + newest scraped_at strictly before the day). If a catch-up
backfill lands past-dated rows after the day's fit, the fingerprint no
longer matches and the next cycle silently refits — no manual invalidation.
"""

import pickle
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from model import data, poisson


def artifact_dir(db_path: Path) -> Path:
    return db_path.parent / "artifacts"


def _train_fingerprint(conn, day: str) -> dict:
    r = conn.execute(
        "SELECT COUNT(*) c, COALESCE(MAX(scraped_at), '') m FROM matches"
        " WHERE status = 3 AND home_ft IS NOT NULL AND date < ?",
        (day,),
    ).fetchone()
    return {"train_rows": r["c"], "max_scraped_at": r["m"]}


def get_poisson(conn, db_path: Path, day: str | None = None,
                half_life: float = poisson.HALF_LIFE_DAYS,
                alpha: float = poisson.ALPHA) -> poisson.PoissonModel:
    """Load the day-frozen Poisson for a UTC day; (re)fit if missing or if
    the training slice changed since it was fitted."""
    day = day or datetime.now(timezone.utc).date().isoformat()
    tag = f"poisson_{day}_hl{half_life:g}_a{alpha:g}.pkl"
    path = artifact_dir(db_path) / tag
    fingerprint = _train_fingerprint(conn, day)

    if path.exists():
        with path.open("rb") as f:
            payload = pickle.load(f)
        if isinstance(payload, dict) and payload.get("fingerprint") == fingerprint:
            return payload["model"]
        # pre-fingerprint artifact or data changed underneath it -> refit

    df = data.load_matches(conn)
    long_df = data.long_format(data.day_frozen(df, day))
    model = poisson.fit(long_df, ref_time=pd.Timestamp(day, tz="UTC"),
                        half_life_days=half_life, alpha=alpha)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as f:
        pickle.dump({"model": model, "fingerprint": fingerprint}, f)
    tmp.replace(path)  # atomic; concurrent cycles race benignly
    return model
