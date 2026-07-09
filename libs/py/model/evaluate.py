"""Walk-forward evaluation harnesses. The batch-mode harness is canonical:
the only backtest that counts is the one that mirrors serving (parent §2.1.3,
§4.5). Day-frozen Poisson + form leg looked up at each fixture's prediction
cutoff, honoring the measured results-publish lag.

  python -m model.evaluate gate            # headline metrics vs gates
  python -m model.evaluate dispersion      # distribution-family diagnostic
  python -m model.evaluate sweep-poisson   # half-life x alpha
  python -m model.evaluate sweep-blend     # blend weight x lag
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from model import data, heuristic, poisson
from store.db import connect

# results visible when published (~14 min after kickoff) + refresh margin
DEFAULT_LAG_MIN = 20.0
EVAL_LINES = (2.5, 3.5, 4.5)
HEADLINE_LINE = 3.5  # closest to the league mean total (~3.9)
WARMUP_DAYS = 7


FormIndex = heuristic.FormIndex  # shared with the predictor service


def build_predictions(
    df: pd.DataFrame,
    eval_days: int = 45,
    lag_min: float = DEFAULT_LAG_MIN,
    half_life: float = poisson.HALF_LIFE_DAYS,
    alpha: float = poisson.ALPHA,
    span: int = heuristic.FORM_SPAN,
    verbose: bool = False,
) -> pd.DataFrame:
    """One row per out-of-sample match with day-frozen Poisson λ's and
    cutoff-aware form λ's. All metric/blend computation happens after."""
    long_df = data.long_format(df)
    days = sorted(df["date"].unique())
    if len(days) <= WARMUP_DAYS + 1:
        raise ValueError(f"need > {WARMUP_DAYS + 1} days of data")
    eval_set = days[max(WARMUP_DAYS, len(days) - eval_days):]

    form_idx = FormIndex(long_df, span=span, lag_min=lag_min)
    rows = []
    for day in eval_set:
        day00 = pd.Timestamp(day, tz="UTC")
        train_long = long_df[long_df["kickoff_ts"] < day00]
        pm = poisson.fit(train_long, ref_time=day00,
                         half_life_days=half_life, alpha=alpha)
        fallback = float(train_long["goals_for"].mean())
        base_totals = df[df["date"] < day]["total"]

        for m in df[df["date"] == day].itertuples():
            lam_p = pm.predict_sides(m.home_player, m.away_player)
            cut = m.kickoff_ts.to_datetime64()
            lam_f = (form_idx.side_lambda(m.home_player, m.away_player, cut, fallback),
                     form_idx.side_lambda(m.away_player, m.home_player, cut, fallback))
            covered = m.home_player in pm.players and m.away_player in pm.players
            row = {
                "date": day, "match_id": m.match_id, "kickoff_ts": m.kickoff_ts,
                "home_ft": m.home_ft, "away_ft": m.away_ft, "total": m.total,
                "lam_p_h": lam_p[0], "lam_p_a": lam_p[1],
                "lam_f_h": lam_f[0], "lam_f_a": lam_f[1],
                "covered": covered,
            }
            for line in EVAL_LINES:
                row[f"base_over_{line}"] = float((base_totals > line).mean())
            rows.append(row)
        if verbose:
            print(f"  {day}: fitted on {pm.train_rows} side-rows", file=sys.stderr)
    return pd.DataFrame(rows)


def _p_over_poisson_total(lam_total: np.ndarray, line: float) -> np.ndarray:
    """Vectorized P(total > line) under Poisson(lam_total), half lines."""
    from scipy.stats import poisson as sp_poisson
    return 1.0 - sp_poisson.cdf(np.floor(line), lam_total)


def metrics(preds: pd.DataFrame, source: str = "poisson", w: float = 0.5,
            lines=EVAL_LINES) -> dict:
    """source: 'poisson' | 'form' | 'blend' (w = poisson weight in blend)."""
    p = preds[preds["covered"]]
    lam = {
        "poisson": p["lam_p_h"] + p["lam_p_a"],
        "form": p["lam_f_h"] + p["lam_f_a"],
        "blend": w * (p["lam_p_h"] + p["lam_p_a"]) + (1 - w) * (p["lam_f_h"] + p["lam_f_a"]),
    }[source].to_numpy()
    out: dict = {"source": source, "w": w if source == "blend" else None,
                 "n": len(p), "n_skipped_uncovered": len(preds) - len(p)}
    for line in lines:
        y = (p["total"] > line).to_numpy().astype(int)
        prob = _p_over_poisson_total(lam, line)
        base = p[f"base_over_{line}"].to_numpy()
        out[f"auc_{line}"] = round(float(roc_auc_score(y, prob)), 4)
        out[f"brier_{line}"] = round(float(np.mean((prob - y) ** 2)), 4)
        out[f"brier_base_{line}"] = round(float(np.mean((base - y) ** 2)), 4)
        out[f"acc_{line}"] = round(float(np.mean((prob > 0.5) == y)), 4)
    return out


def calibration(preds: pd.DataFrame, source: str = "poisson", w: float = 0.5,
                line: float = HEADLINE_LINE, bins: int = 10) -> pd.DataFrame:
    p = preds[preds["covered"]]
    lam = (w * (p["lam_p_h"] + p["lam_p_a"]) + (1 - w) * (p["lam_f_h"] + p["lam_f_a"])
           if source == "blend" else
           (p["lam_p_h"] + p["lam_p_a"] if source == "poisson"
            else p["lam_f_h"] + p["lam_f_a"])).to_numpy()
    prob = _p_over_poisson_total(lam, line)
    y = (p["total"] > line).to_numpy().astype(int)
    df = pd.DataFrame({"prob": prob, "y": y})
    df["bin"] = pd.qcut(df["prob"], bins, duplicates="drop")
    g = df.groupby("bin", observed=True).agg(n=("y", "size"), pred=("prob", "mean"),
                                             real=("y", "mean"))
    g["gap_pts"] = ((g["real"] - g["pred"]) * 100).round(1)
    return g.round(3)


def dispersion(preds: pd.DataFrame) -> dict:
    """Family diagnostic on honest out-of-sample side predictions.

    Cameron–Trivedi NB2 auxiliary regression: ((y−μ)² − y) = a·μ², t-stat on a.
    a > 0 (t > ~2)  -> over-dispersed  -> NB/double-Poisson candidates
    a < 0 (t < ~-2) -> under-dispersed -> Poisson tails fine (parent's case)
    """
    p = preds[preds["covered"]]
    y = np.concatenate([p["home_ft"].to_numpy(), p["away_ft"].to_numpy()]).astype(float)
    mu = np.concatenate([p["lam_p_h"].to_numpy(), p["lam_p_a"].to_numpy()])
    z = (y - mu) ** 2 - y
    x = mu**2
    a = float((x * z).sum() / (x * x).sum())
    resid = z - a * x
    se = float(np.sqrt((resid**2 * x**2).sum()) / (x * x).sum())  # HC0
    marginal = {
        "side_var_mean_ratio": round(float(np.var(y) / np.mean(y)), 3),
        "total_var_mean_ratio": round(float(np.var(p["total"]) / np.mean(p["total"])), 3),
    }
    return {**marginal, "ct_alpha": round(a, 4), "ct_t": round(a / se, 2),
            "n_sides": len(y),
            "verdict": ("over-dispersed" if a / se > 2 else
                        "under-dispersed" if a / se < -2 else "poisson-consistent")}


def _record_run(conn, kind: str, params: dict, metrics_obj) -> None:
    conn.execute(
        "INSERT INTO model_runs (kind, fitted_at, train_rows, params_json, metrics_json)"
        " VALUES (?,?,?,?,?)",
        (kind, datetime.now(timezone.utc).isoformat(), None,
         json.dumps(params), json.dumps(metrics_obj, default=str)),
    )
    conn.commit()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="model.evaluate")
    ap.add_argument("mode", choices=["gate", "dispersion", "sweep-poisson", "sweep-blend"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--eval-days", type=int, default=45)
    ap.add_argument("--lag", type=float, default=DEFAULT_LAG_MIN)
    ap.add_argument("--half-life", type=float, default=poisson.HALF_LIFE_DAYS)
    ap.add_argument("--alpha", type=float, default=poisson.ALPHA)
    ap.add_argument("--span", type=int, default=heuristic.FORM_SPAN)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    df = data.load_matches(conn)
    print(f"data: {len(df)} matches, {df['date'].min()}..{df['date'].max()}")

    if args.mode == "sweep-poisson":
        results = []
        for hl in (7.0, 14.0, 28.0):
            for al in (0.01, 0.02, 0.05):
                preds = build_predictions(df, args.eval_days, args.lag, hl, al, args.span)
                m = metrics(preds, "poisson")
                results.append({"half_life": hl, "alpha": al,
                                f"auc_{HEADLINE_LINE}": m[f"auc_{HEADLINE_LINE}"],
                                f"brier_{HEADLINE_LINE}": m[f"brier_{HEADLINE_LINE}"]})
                print(results[-1])
        _record_run(conn, "sweep-poisson", vars(args), results)
        return 0

    preds = build_predictions(df, args.eval_days, args.lag, args.half_life,
                              args.alpha, args.span, verbose=False)

    if args.mode == "dispersion":
        rep = dispersion(preds)
        print(json.dumps(rep, indent=2))
        _record_run(conn, "dispersion", vars(args), rep)
        return 0

    if args.mode == "sweep-blend":
        results = []
        for w in np.arange(0.0, 1.01, 0.1):
            m = metrics(preds, "blend", w=round(float(w), 1))
            results.append({"w_poisson": round(float(w), 1),
                            f"auc_{HEADLINE_LINE}": m[f"auc_{HEADLINE_LINE}"],
                            f"acc_{HEADLINE_LINE}": m[f"acc_{HEADLINE_LINE}"],
                            f"brier_{HEADLINE_LINE}": m[f"brier_{HEADLINE_LINE}"]})
            print(results[-1])
        _record_run(conn, "sweep-blend", vars(args), results)
        return 0

    # gate
    out = {}
    for src, w in (("poisson", None), ("form", None), ("blend", 0.5)):
        m = metrics(preds, src, w=w or 0.5)
        out[src] = m
        print(json.dumps(m))
    print("\ncalibration (blend, line 3.5):")
    print(calibration(preds, "blend").to_string())
    print("\ndispersion:", json.dumps(dispersion(preds)))
    _record_run(conn, "gate", vars(args), out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
