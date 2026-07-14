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
                alpha: float = poisson.ALPHA,
                with_club: bool = False,
                with_tod: bool = False) -> poisson.PoissonModel:
    """Load the day-frozen Poisson for a UTC day; (re)fit if missing or if
    the training slice changed since it was fitted.

    with_club and with_tod are part of the artifact TAG, not just the fit:
    variants for the same day differ in their coefficients, and a cached
    player-only pickle loading into a feature-enabled cycle would serve
    feature-blind λs while every metric looked healthy. Silent no-ops are
    the failure mode here.
    """
    day = day or datetime.now(timezone.utc).date().isoformat()
    tag = (f"poisson_{day}_hl{half_life:g}_a{alpha:g}"
           f"{'_club' if with_club else ''}"
           f"{'_tod' if with_tod else ''}.pkl")
    path = artifact_dir(db_path) / tag
    fingerprint = _train_fingerprint(conn, day)

    if path.exists():
        with path.open("rb") as f:
            payload = pickle.load(f)
        if (isinstance(payload, dict)
                and payload.get("fingerprint") == fingerprint
                and all(hasattr(payload.get("model"), f)
                        for f in ("catt", "tod"))):  # every post-init field
            return payload["model"]
        # Stale in one of three ways -> refit: pre-fingerprint artifact, data
        # changed underneath it, or the pickle predates a PoissonModel field
        # (unpickle skips __init__, so dataclass defaults do NOT backfill new
        # fields — an old artifact crashes on first attribute access instead
        # of failing here unless we check).

    df = data.load_matches(conn)
    long_df = data.long_format(data.day_frozen(df, day))
    model = poisson.fit(long_df, ref_time=pd.Timestamp(day, tz="UTC"),
                        half_life_days=half_life, alpha=alpha,
                        with_club=with_club, with_tod=with_tod)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as f:
        pickle.dump({"model": model, "fingerprint": fingerprint}, f)
    tmp.replace(path)  # atomic; concurrent cycles race benignly
    return model
