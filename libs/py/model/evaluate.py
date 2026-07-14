"""Walk-forward evaluation harnesses. The batch-mode harness is canonical:
the only backtest that counts is the one that mirrors serving (parent §2.1.3,
§4.5). Day-frozen Poisson + form leg looked up at each fixture's prediction
cutoff, honoring the measured results-publish lag.

  python -m model.evaluate gate            # headline metrics vs gates
  python -m model.evaluate dispersion      # distribution-family diagnostic
  python -m model.evaluate sweep-poisson   # half-life x alpha
  python -m model.evaluate sweep-blend     # blend weight x lag
  python -m model.evaluate sweep-multiform # poisson x multi-span form weights
  python -m model.evaluate club            # club as a second GLM entity
  python -m model.evaluate tod             # cyclic time-of-day term (--tod-k)
  python -m model.evaluate tod-club        # tod's marginal worth over club
  python -m model.evaluate x12             # 1x2 gate off the same lambdas
  python -m model.evaluate sweep-club      # club-block ridge scale sweep
  python -m model.evaluate conditional     # book-population (lines near E[total])
  python -m model.evaluate h2h             # pairwise H2H gate, 1x2 + totals
"""

import argparse
import json
import sys
from collections import deque
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from model import data, h2h, heuristic, poisson, sharpen
from model.blend import PMF_MAX_GOALS, TOTALS_BLEND_WEIGHT
from model.recal import apply_platt, fit_platt, fit_platt_shared
from store.db import connect

# results visible when published (~14 min after kickoff) + refresh margin
DEFAULT_LAG_MIN = 20.0
# 5.5 added 2026-07-11: the book quotes it on high-total fixtures and serving
# was measurably miscalibrated there while this harness couldn't see it.
EVAL_LINES = (2.5, 3.5, 4.5, 5.5)
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
    extra_spans: tuple[int, ...] = (),
    with_var: bool = False,
    with_club: bool = False,
    with_tod: bool = False,
    tod_k: int = poisson.TOD_FOURIER_K,
) -> pd.DataFrame:
    """One row per out-of-sample match with day-frozen Poisson λ's and
    cutoff-aware form λ's. All metric/blend computation happens after.

    extra_spans adds one form leg per span as lam_f{span}_h/_a columns
    (FEATURE_IDEAS option 1); the default-span leg keeps its lam_f_h/_a
    names so existing modes and tests read the same frame. with_var adds
    each player's variance-profile ratio as var_h/var_a (option 3; NaN
    below the profile's min-periods).

    with_club fits a *second* day-frozen Poisson with club factors and adds
    its λ's as lam_pc_h/_a, so club and no-club are scored on identical rows
    (CLUB_FEATURE.md). club_cov flags whether both clubs were in the day's
    training slice — unseen clubs fall back to the player-only λ, they are
    never dropped.

    with_tod does the same with a cyclic hour-of-day term (TOD_FEATURE.md):
    a second day-frozen Poisson fitted with_tod=True, λ's as lam_pt_h/_a.
    Hour needs no coverage flag — every match has a kickoff.

    with_club AND with_tod together additionally fit the combined GLM
    (both blocks in one design matrix), λ's as lam_pct_h/_a — the arm that
    answers whether tod's worth survives club being present.
    """
    long_df = data.long_format(df)
    days = sorted(df["date"].unique())
    if len(days) <= WARMUP_DAYS + 1:
        raise ValueError(f"need > {WARMUP_DAYS + 1} days of data")
    eval_set = days[max(WARMUP_DAYS, len(days) - eval_days):]

    form_idx = FormIndex(long_df, span=span, lag_min=lag_min)
    extra_idx = {s: FormIndex(long_df, span=s, lag_min=lag_min)
                 for s in extra_spans}
    var_idx = sharpen.VarIndex(long_df, lag_min=lag_min) if with_var else None
    rows = []
    for day in eval_set:
        day00 = pd.Timestamp(day, tz="UTC")
        train_long = long_df[long_df["kickoff_ts"] < day00]
        pm = poisson.fit(train_long, ref_time=day00,
                         half_life_days=half_life, alpha=alpha)
        cm = (poisson.fit(train_long, ref_time=day00, half_life_days=half_life,
                          alpha=alpha, with_club=True) if with_club else None)
        tm = (poisson.fit(train_long, ref_time=day00, half_life_days=half_life,
                          alpha=alpha, with_tod=True, tod_k=tod_k)
              if with_tod else None)
        ctm = (poisson.fit(train_long, ref_time=day00, half_life_days=half_life,
                           alpha=alpha, with_club=True, with_tod=True,
                           tod_k=tod_k)
               if (with_club and with_tod) else None)
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
            if cm is not None:
                lam_c = cm.predict_sides(m.home_player, m.away_player,
                                         m.home_club, m.away_club)
                row["lam_pc_h"], row["lam_pc_a"] = lam_c
                row["club_cov"] = (m.home_club in cm.clubs
                                   and m.away_club in cm.clubs)
            if tm is not None:
                hr = m.kickoff_ts.hour + m.kickoff_ts.minute / 60.0
                row["lam_pt_h"], row["lam_pt_a"] = tm.predict_sides(
                    m.home_player, m.away_player, hour=hr)
                if ctm is not None:
                    row["lam_pct_h"], row["lam_pct_a"] = ctm.predict_sides(
                        m.home_player, m.away_player,
                        m.home_club, m.away_club, hour=hr)
            for s, idx in extra_idx.items():
                row[f"lam_f{s}_h"] = idx.side_lambda(
                    m.home_player, m.away_player, cut, fallback)
                row[f"lam_f{s}_a"] = idx.side_lambda(
                    m.away_player, m.home_player, cut, fallback)
            if var_idx is not None:
                rh = var_idx.ratio(m.home_player, cut)
                ra = var_idx.ratio(m.away_player, cut)
                row["var_h"] = np.nan if rh is None else rh
                row["var_a"] = np.nan if ra is None else ra
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


def _leg_prefix(name: str) -> str:
    """'poisson' -> lam_p, 'form{span}' -> lam_f{span} (build_predictions naming)."""
    return "lam_p" if name == "poisson" else f"lam_f{name.removeprefix('form')}"


def metrics_weighted(preds: pd.DataFrame, weights: dict[str, float],
                     lines=EVAL_LINES) -> dict:
    """Metrics for an arbitrary λ-combo, weights keyed 'poisson' / 'form{span}'."""
    p = preds[preds["covered"]]
    lam = np.zeros(len(p))
    for name, w in weights.items():
        pre = _leg_prefix(name)
        lam = lam + w * (p[f"{pre}_h"] + p[f"{pre}_a"]).to_numpy()
    out: dict = {"weights": weights, "n": len(p)}
    for line in lines:
        y = (p["total"] > line).to_numpy().astype(int)
        prob = _p_over_poisson_total(lam, line)
        out[f"auc_{line}"] = round(float(roc_auc_score(y, prob)), 4)
        out[f"brier_{line}"] = round(float(np.mean((prob - y) ** 2)), 4)
        out[f"acc_{line}"] = round(float(np.mean((prob > 0.5) == y)), 4)
    return out


def _simplex_weights(k: int, step: float):
    """Every non-negative length-k weight vector summing to 1 on a `step` grid."""
    n = round(1 / step)

    def parts(k_left: int, n_left: int):
        if k_left == 1:
            yield (n_left,)
            return
        for i in range(n_left + 1):
            for rest in parts(k_left - 1, n_left - i):
                yield (i, *rest)

    for c in parts(k, n):
        yield tuple(v / n for v in c)


def sweep_multiform(preds: pd.DataFrame, spans: tuple[int, ...],
                    step: float = 0.1, line: float = HEADLINE_LINE) -> list[dict]:
    """Grid-search simplex weights over (poisson, form_s1, form_s2, ...) legs
    (FEATURE_IDEAS option 1). Returns every combo, best headline-AUC first.

    Read the plateau, not the peak: a few hundred combos on one eval window
    will always have a lucky argmax; conclusions come from the shape.
    """
    p = preds[preds["covered"]]
    legs = [("poisson", (p["lam_p_h"] + p["lam_p_a"]).to_numpy())]
    legs += [(f"form{s}", (p[f"lam_f{s}_h"] + p[f"lam_f{s}_a"]).to_numpy())
             for s in spans]
    y = (p["total"] > line).to_numpy().astype(int)
    out = []
    for w in _simplex_weights(len(legs), step):
        lam = sum(wi * leg for wi, (_, leg) in zip(w, legs))
        prob = _p_over_poisson_total(lam, line)
        out.append({**{f"w_{name}": wi for wi, (name, _) in zip(w, legs)},
                    f"auc_{line}": round(float(roc_auc_score(y, prob)), 4),
                    f"brier_{line}": round(float(np.mean((prob - y) ** 2)), 4)})
    out.sort(key=lambda r: (-r[f"auc_{line}"], r[f"brier_{line}"]))
    return out


PHI_WARMUP_DAYS = 7  # eval days that only seed the φ fits, excluded from metrics


def _p_over_from_pmf(pmf: np.ndarray, line: float) -> np.ndarray:
    """P(total > line) per row of a pmf matrix, half lines."""
    return 1.0 - pmf[:, :int(np.floor(line)) + 1].sum(axis=1)


def _calib_table(prob: np.ndarray, y: np.ndarray, bins: int = 10) -> pd.DataFrame:
    df = pd.DataFrame({"prob": prob, "y": y})
    df["bin"] = pd.qcut(df["prob"], bins, duplicates="drop")
    g = df.groupby("bin", observed=True).agg(n=("y", "size"), pred=("prob", "mean"),
                                             real=("y", "mean"))
    g["gap_pts"] = ((g["real"] - g["pred"]) * 100).round(1)
    return g.round(3)


def sharpen_report(preds: pd.DataFrame, w: float = TOTALS_BLEND_WEIGHT,
                   warmup_days: int = PHI_WARMUP_DAYS, lines=EVAL_LINES) -> dict:
    """FEATURE_IDEAS option 3, walk-forward. Three variants priced off the
    same blend μ on the identical post-warmup set:

      base    Poisson(μ)                      (served today)
      global  DP(μ, φ) — one φ, expanding-window MLE on prior eval days
      player  DP(μ, φ_i), log φ_i = b0 + b1·(x_i − 1), x the matchup
              variance-profile ratio (missing profiles fall back to x = 1,
              i.e. the global dispersion level)

    φ must be fit on *out-of-sample* residuals, hence on prior eval-day
    predictions (mirrors serving: fit on settled predictions), never on
    training-set fit residuals.
    """
    p = preds[preds["covered"]].copy()
    p["mu"] = (w * (p["lam_p_h"] + p["lam_p_a"])
               + (1 - w) * (p["lam_f_h"] + p["lam_f_a"]))
    p["x"] = p[["var_h", "var_a"]].mean(axis=1).fillna(1.0)  # skipna mean
    days = sorted(p["date"].unique())
    if len(days) <= warmup_days + 1:
        raise ValueError(f"need > {warmup_days + 1} eval days for φ warmup")

    fit_log = []
    for day in days[warmup_days:]:
        hist = p[p["date"] < day]
        mu_h, tot_h = hist["mu"].to_numpy(), hist["total"].to_numpy()
        phi_g = sharpen.fit_phi(mu_h, tot_h)
        b = sharpen.fit_phi_model(mu_h, tot_h, hist["x"].to_numpy())
        cur = p["date"] == day
        mu_c = p.loc[cur, "mu"].to_numpy()
        pmf_g = sharpen.dp_pmf(mu_c, phi_g, PMF_MAX_GOALS)
        pmf_p = sharpen.dp_pmf(mu_c, sharpen.phi_from(b, p.loc[cur, "x"]),
                               PMF_MAX_GOALS)
        for line in lines:
            p.loc[cur, f"dpg_{line}"] = _p_over_from_pmf(pmf_g, line)
            p.loc[cur, f"dpp_{line}"] = _p_over_from_pmf(pmf_p, line)
        fit_log.append({"day": day, "phi_global": round(phi_g, 4),
                        "b0": round(b[0], 4), "b1": round(b[1], 4),
                        "n_fit": len(hist)})

    q = p[p["date"] >= days[warmup_days]]
    out: dict = {"n": len(q), "w": w, "fit_first": fit_log[0],
                 "fit_last": fit_log[-1]}
    for line in lines:
        y = (q["total"] > line).to_numpy().astype(int)
        variants = {"base": _p_over_poisson_total(q["mu"].to_numpy(), line),
                    "global": q[f"dpg_{line}"].to_numpy(),
                    "player": q[f"dpp_{line}"].to_numpy()}
        for name, prob in variants.items():
            out[f"{name}_auc_{line}"] = round(float(roc_auc_score(y, prob)), 4)
            out[f"{name}_brier_{line}"] = round(float(np.mean((prob - y) ** 2)), 4)
    line = HEADLINE_LINE
    y = (q["total"] > line).to_numpy().astype(int)
    out["calib"] = {
        "base": _calib_table(_p_over_poisson_total(q["mu"].to_numpy(), line), y),
        "global": _calib_table(q[f"dpg_{line}"].to_numpy(), y),
        "player": _calib_table(q[f"dpp_{line}"].to_numpy(), y),
    }
    out["var_profile_coverage"] = round(
        float(preds[preds["covered"]][["var_h", "var_a"]].notna().all(axis=1).mean()), 4)
    return out


def recal_report(preds: pd.DataFrame, w: float = TOTALS_BLEND_WEIGHT,
                 warmup_days: int = PHI_WARMUP_DAYS, lines=EVAL_LINES) -> dict:
    """Per-line monotone recalibration of the blend's Poisson P(over)
    (FEATURE_IDEAS follow-up to option 3: the tail miss is a map problem).

      base  Poisson(μ_blend) P(over)              (served today)
      platt sigmoid(a·logit(p) + b), 2-param MLE
      iso   isotonic regression, nonparametric
      hier  shared-slope Platt: one a pooled across lines, per-line b
            (the serving tier that lets thin lines engage — recal.py)

    All calibrators are refit each eval day on prior eval days only
    (mirrors serving: fit on settled predictions). Monotone maps cannot
    change per-line ranking, so AUC is structurally ~unchanged; the
    experiment is Brier and the calibration deciles. hier's job is to match
    platt at full n — its small-n advantage (1 free parameter per line
    instead of 2) is structural, not something this asymptotic run measures.
    """
    p = preds[preds["covered"]].copy()
    p["mu"] = (w * (p["lam_p_h"] + p["lam_p_a"])
               + (1 - w) * (p["lam_f_h"] + p["lam_f_a"]))
    for line in lines:
        p[f"p_{line}"] = _p_over_poisson_total(p["mu"].to_numpy(), line)
        for arm in ("plt", "iso", "hier"):
            p[f"{arm}_{line}"] = np.nan
    days = sorted(p["date"].unique())
    if len(days) <= warmup_days + 1:
        raise ValueError(f"need > {warmup_days + 1} eval days for warmup")

    platt_last, hier_last = {}, {}
    for day in days[warmup_days:]:
        hist, cur = p[p["date"] < day], p["date"] == day
        pool: dict[float, tuple[np.ndarray, np.ndarray]] = {}
        for line in lines:
            prob_h = hist[f"p_{line}"].to_numpy()
            y_h = (hist["total"] > line).to_numpy().astype(int)
            if not 0 < y_h.sum() < len(y_h):  # degenerate day: identity
                for arm in ("plt", "iso", "hier"):
                    p.loc[cur, f"{arm}_{line}"] = p.loc[cur, f"p_{line}"]
                continue
            pool[line] = (prob_h, y_h)
            ab = fit_platt(prob_h, y_h)
            iso = IsotonicRegression(out_of_bounds="clip").fit(prob_h, y_h)
            p.loc[cur, f"plt_{line}"] = apply_platt(ab, p.loc[cur, f"p_{line}"])
            p.loc[cur, f"iso_{line}"] = iso.predict(p.loc[cur, f"p_{line}"])
            platt_last[line] = {"a": round(ab[0], 4), "b": round(ab[1], 4),
                                "n_fit": len(hist)}
        if pool:
            a, bs = fit_platt_shared(pool)
            for line in pool:
                p.loc[cur, f"hier_{line}"] = apply_platt(
                    (a, bs[line]), p.loc[cur, f"p_{line}"])
            hier_last = {"a": round(a, 4),
                         "b": {li: round(bi, 4) for li, bi in bs.items()}}

    q = p[p["date"] >= days[warmup_days]]
    out: dict = {"n": len(q), "w": w, "platt_last": platt_last,
                 "hier_last": hier_last}
    for line in lines:
        y = (q["total"] > line).to_numpy().astype(int)
        for name, col in (("base", f"p_{line}"), ("platt", f"plt_{line}"),
                          ("iso", f"iso_{line}"), ("hier", f"hier_{line}")):
            prob = q[col].to_numpy()
            out[f"{name}_auc_{line}"] = round(float(roc_auc_score(y, prob)), 4)
            out[f"{name}_brier_{line}"] = round(float(np.mean((prob - y) ** 2)), 4)
    line = HEADLINE_LINE
    y = (q["total"] > line).to_numpy().astype(int)
    out["calib"] = {name: _calib_table(q[col].to_numpy(), y)
                    for name, col in (("base", f"p_{line}"),
                                      ("platt", f"plt_{line}"),
                                      ("iso", f"iso_{line}"),
                                      ("hier", f"hier_{line}"))}
    return out


CONDITIONAL_MIN_FIT = 50  # per-line daily-refit floor inside conditional_report


def conditional_report(preds: pd.DataFrame, w: float = TOTALS_BLEND_WEIGHT,
                       warmup_days: int = PHI_WARMUP_DAYS) -> dict:
    """Book-population evaluation: each match is scored ONLY at the two
    half-lines straddling its own expected total, base = max(1, round(μ)) —
    serving's _schedule_row rule and a proxy for the book's line menu.

    The fixed-line tables answer "can the model rank ALL matches at line L?";
    most of that AUC at tail lines comes from separating low-μ from high-μ
    matches, which the book's line choice removes. This answers the serving
    question: within the sub-population that actually gets line L quoted, is
    there discrimination and calibration left? (Measured 2026-07-11 before
    this mode existed: unconditional AUC 0.74 at 5.5 collapsed to 0.61 on
    the μ ≥ 4.75 sub-population.)

    Arms: base (raw Poisson map) and recal (per-line Platt refit daily on
    prior eval days, fit only on rows whose own line menu included L — the
    same selection serving's maps are fit on).

    Read per-line/per-segment blocks for discrimination: the top-level pooled
    AUC also counts cross-line composition (a 3.5-line row SHOULD carry a
    higher P(over) than a 5.5-line row), so only the within-line AUCs answer
    "is there skill left once the book has chosen the line?".
    """
    from scipy.stats import poisson as sp_poisson

    p = preds[preds["covered"]]
    mu = (w * (p["lam_p_h"] + p["lam_p_a"])
          + (1 - w) * (p["lam_f_h"] + p["lam_f_a"])).to_numpy()
    base_line = np.maximum(1.0, np.round(mu))
    parts = []
    for off in (-0.5, +0.5):
        line = base_line + off
        parts.append(pd.DataFrame({
            "date": p["date"].to_numpy(), "total": p["total"].to_numpy(),
            "base": base_line, "line": line,
            "prob": 1.0 - sp_poisson.cdf(np.floor(line), mu),
        }))
    d = pd.concat(parts, ignore_index=True)
    d["y"] = (d["total"] > d["line"]).astype(int)
    d["recal"] = np.nan
    days = sorted(d["date"].unique())
    if len(days) <= warmup_days + 1:
        raise ValueError(f"need > {warmup_days + 1} eval days for warmup")

    for day in days[warmup_days:]:
        hist, cur = d[d["date"] < day], d["date"] == day
        for line in sorted(d.loc[cur, "line"].unique()):
            h = hist[hist["line"] == line]
            c = cur & (d["line"] == line)
            y_h = h["y"].to_numpy()
            if len(h) >= CONDITIONAL_MIN_FIT and 0 < y_h.sum() < len(y_h):
                ab = fit_platt(h["prob"].to_numpy(), y_h)
                d.loc[c, "recal"] = apply_platt(ab, d.loc[c, "prob"])
            else:  # thin line: identity, exactly like an unmapped served line
                d.loc[c, "recal"] = d.loc[c, "prob"]

    q = d[d["date"] >= days[warmup_days]]
    arms = ("base", "recal")

    def _block(g: pd.DataFrame) -> dict:
        rec: dict = {"n": len(g), "realized_over": round(float(g["y"].mean()), 4)}
        for arm in arms:
            col = g["prob"] if arm == "base" else g["recal"]
            rec[f"{arm}_p_mean"] = round(float(col.mean()), 4)
            rec[f"{arm}_brier"] = round(float(np.mean((col - g["y"]) ** 2)), 4)
            if g["y"].nunique() == 2:
                rec[f"{arm}_auc"] = round(float(roc_auc_score(g["y"], col)), 4)
        return rec

    out: dict = {"w": w, **{f"{k}": v for k, v in _block(q).items()}}
    out["by_base"] = {seg: _block(q[m]) for seg, m in
                      (("low(<=3)", q["base"] <= 3), ("mid(4)", q["base"] == 4),
                       ("high(>=5)", q["base"] >= 5)) if m.any()}
    out["by_line"] = {f"{line:g}": _block(q[q["line"] == line])
                      for line in (3.5, 4.5, 5.5, 6.5)
                      if (q["line"] == line).any()}
    return out


BOOTSTRAP_N = 400


def _paired_auc_ci(y: np.ndarray, prob_a: np.ndarray, prob_b: np.ndarray,
                   n_boot: int = BOOTSTRAP_N, seed: int = 0) -> tuple[float, float]:
    """95% CI on AUC(b) − AUC(a), resampling matches (not sides) in pairs.

    Paired because both models score the identical rows: the shared match
    noise cancels, which is the whole reason a +0.5-pt effect is resolvable
    on ~19k matches at all.
    """
    rng = np.random.default_rng(seed)
    d = []
    for _ in range(n_boot):
        i = rng.integers(0, len(y), len(y))
        if y[i].min() == y[i].max():
            continue
        d.append(roc_auc_score(y[i], prob_b[i]) - roc_auc_score(y[i], prob_a[i]))
    lo, hi = np.percentile(d, [2.5, 97.5])
    return float(lo), float(hi)


def club_report(preds: pd.DataFrame, w: float = TOTALS_BLEND_WEIGHT,
                lines=EVAL_LINES) -> dict:
    """Club as a second GLM entity (CLUB_FEATURE.md), walk-forward.

      blend       w·Poisson(player)      + (1−w)·form   (served today)
      blend+club  w·Poisson(player+club) + (1−w)·form

    The form leg is byte-identical between the two arms, so the difference is
    club's *marginal* worth over the served model — not over pure Poisson.
    That distinction is the experiment: club could have been a slow proxy for
    recent form, in which case the blend arm would show no gain.
    """
    p = preds[preds["covered"]]
    mu = {
        "blend": w * (p["lam_p_h"] + p["lam_p_a"]),
        "blend+club": w * (p["lam_pc_h"] + p["lam_pc_a"]),
    }
    form = (1 - w) * (p["lam_f_h"] + p["lam_f_a"])
    mu = {k: (v + form).to_numpy() for k, v in mu.items()}

    out: dict = {"n": len(p), "w": w,
                 "club_coverage": round(float(p["club_cov"].mean()), 4),
                 "sd_lam_blend": round(float(np.std(mu["blend"])), 4),
                 "sd_lam_club": round(float(np.std(mu["blend+club"])), 4)}
    for line in lines:
        y = (p["total"] > line).to_numpy().astype(int)
        prob = {k: _p_over_poisson_total(v, line) for k, v in mu.items()}
        for name, pr in prob.items():
            out[f"{name}_auc_{line}"] = round(float(roc_auc_score(y, pr)), 4)
            out[f"{name}_brier_{line}"] = round(float(np.mean((pr - y) ** 2)), 4)
        lo, hi = _paired_auc_ci(y, prob["blend"], prob["blend+club"])
        out[f"dauc_{line}"] = round(out[f"blend+club_auc_{line}"]
                                    - out[f"blend_auc_{line}"], 4)
        out[f"dauc_ci_{line}"] = [round(lo, 4), round(hi, 4)]
        out[f"dbrier_{line}"] = round(out[f"blend+club_brier_{line}"]
                                      - out[f"blend_brier_{line}"], 4)
        out[f"significant_{line}"] = bool(lo > 0)
    return out


def tod_report(preds: pd.DataFrame, w: float = TOTALS_BLEND_WEIGHT,
               lines=EVAL_LINES) -> dict:
    """Cyclic time-of-day term in the GLM (TOD_FEATURE.md), walk-forward.

      blend      w·Poisson(player)     + (1−w)·form   (served today)
      blend+tod  w·Poisson(player+tod) + (1−w)·form

    Same experiment shape as club_report: the form leg is byte-identical
    between arms, so the difference is tod's *marginal* worth over the served
    model. The threat is subtler than club's, though — the raw hourly effect
    could be a player-shift artifact (the GLM's joint fit controls that) or
    could already be absorbed by the 7-day-half-life recency weights tracking
    slate composition. The paired bootstrap answers both at once.

    Splits the gate by UTC-hour tercile of the measured profile (trough
    03–05 vs elsewhere) so a pass driven entirely by five overnight hours is
    visible as such.
    """
    p = preds[preds["covered"]]
    mu = {
        "blend": w * (p["lam_p_h"] + p["lam_p_a"]),
        "blend+tod": w * (p["lam_pt_h"] + p["lam_pt_a"]),
    }
    form = (1 - w) * (p["lam_f_h"] + p["lam_f_a"])
    mu = {k: (v + form).to_numpy() for k, v in mu.items()}

    out: dict = {"n": len(p), "w": w,
                 "sd_lam_blend": round(float(np.std(mu["blend"])), 4),
                 "sd_lam_tod": round(float(np.std(mu["blend+tod"])), 4)}
    for line in lines:
        y = (p["total"] > line).to_numpy().astype(int)
        prob = {k: _p_over_poisson_total(v, line) for k, v in mu.items()}
        for name, pr in prob.items():
            out[f"{name}_auc_{line}"] = round(float(roc_auc_score(y, pr)), 4)
            out[f"{name}_brier_{line}"] = round(float(np.mean((pr - y) ** 2)), 4)
        lo, hi = _paired_auc_ci(y, prob["blend"], prob["blend+tod"])
        out[f"dauc_{line}"] = round(out[f"blend+tod_auc_{line}"]
                                    - out[f"blend_auc_{line}"], 4)
        out[f"dauc_ci_{line}"] = [round(lo, 4), round(hi, 4)]
        out[f"dbrier_{line}"] = round(out[f"blend+tod_brier_{line}"]
                                      - out[f"blend_brier_{line}"], 4)
        out[f"significant_{line}"] = bool(lo > 0)

    hours = pd.to_datetime(p["kickoff_ts"]).dt.hour
    for seg, mask in (("trough", hours.between(2, 5)),
                      ("rest", ~hours.between(2, 5))):
        m = mask.to_numpy()
        y = (p["total"] > HEADLINE_LINE).to_numpy().astype(int)[m]
        if len(np.unique(y)) < 2:
            continue
        for name, v in mu.items():
            pr = _p_over_poisson_total(v[m], HEADLINE_LINE)
            out[f"{seg}_{name}_auc_{HEADLINE_LINE}"] = round(
                float(roc_auc_score(y, pr)), 4)
        out[f"{seg}_n"] = int(m.sum())
    return out


def tod_club_report(preds: pd.DataFrame, w: float = TOTALS_BLEND_WEIGHT,
                    lines=EVAL_LINES) -> dict:
    """Does tod's worth survive club being present? (TOD_FEATURE.md caveat 3)

      blend+club      w·Poisson(player+club)     + (1−w)·form  (served today)
      blend+club+tod  w·Poisson(player+club+tod) + (1−w)·form

    The paired bootstrap is on THIS pair — tod's marginal worth over actual
    production, not over the player-only blend. blend and blend+tod AUCs are
    reported for context (club mix varies by hour, so club could have been
    absorbing part of the hour effect, or vice versa).
    """
    p = preds[preds["covered"]]
    mu = {
        "blend": w * (p["lam_p_h"] + p["lam_p_a"]),
        "blend+tod": w * (p["lam_pt_h"] + p["lam_pt_a"]),
        "blend+club": w * (p["lam_pc_h"] + p["lam_pc_a"]),
        "blend+club+tod": w * (p["lam_pct_h"] + p["lam_pct_a"]),
    }
    form = (1 - w) * (p["lam_f_h"] + p["lam_f_a"])
    mu = {k: (v + form).to_numpy() for k, v in mu.items()}

    out: dict = {"n": len(p), "w": w,
                 "club_coverage": round(float(p["club_cov"].mean()), 4),
                 "sd_lam_club": round(float(np.std(mu["blend+club"])), 4),
                 "sd_lam_club_tod": round(float(np.std(mu["blend+club+tod"])), 4)}
    for line in lines:
        y = (p["total"] > line).to_numpy().astype(int)
        prob = {k: _p_over_poisson_total(v, line) for k, v in mu.items()}
        for name, pr in prob.items():
            out[f"{name}_auc_{line}"] = round(float(roc_auc_score(y, pr)), 4)
            out[f"{name}_brier_{line}"] = round(float(np.mean((pr - y) ** 2)), 4)
        lo, hi = _paired_auc_ci(y, prob["blend+club"], prob["blend+club+tod"])
        out[f"dauc_{line}"] = round(out[f"blend+club+tod_auc_{line}"]
                                    - out[f"blend+club_auc_{line}"], 4)
        out[f"dauc_ci_{line}"] = [round(lo, 4), round(hi, 4)]
        out[f"dbrier_{line}"] = round(out[f"blend+club+tod_brier_{line}"]
                                      - out[f"blend+club_brier_{line}"], 4)
        out[f"significant_{line}"] = bool(lo > 0)
    return out


CLUB_SCALES = (0.25, 0.5, 1.0, 2.0, 4.0)  # log-spaced; 1.0 = served today


def sweep_club_predictions(
    df: pd.DataFrame,
    scales: tuple[float, ...] = CLUB_SCALES,
    eval_days: int = 45,
    lag_min: float = DEFAULT_LAG_MIN,
    half_life: float = poisson.HALF_LIFE_DAYS,
    alpha: float = poisson.ALPHA,
    span: int = heuristic.FORM_SPAN,
    verbose: bool = False,
) -> pd.DataFrame:
    """One walk-forward pass, one club fit per scale per day, so every scale
    is scored on identical rows. Columns lam_c{scale}_h/_a plus the shared
    form leg. Deliberately leaner than build_predictions (no extra spans/var);
    the only question here is the club block's shrinkage."""
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
        models = {c: poisson.fit(train_long, ref_time=day00,
                                 half_life_days=half_life, alpha=alpha,
                                 with_club=True, club_scale=c)
                  for c in scales}
        any_pm = next(iter(models.values()))
        fallback = float(train_long["goals_for"].mean())
        for m in df[df["date"] == day].itertuples():
            if (m.home_player not in any_pm.players
                    or m.away_player not in any_pm.players):
                continue
            cut = m.kickoff_ts.to_datetime64()
            row = {
                "date": day, "total": m.total,
                "lam_f_h": form_idx.side_lambda(m.home_player, m.away_player,
                                                cut, fallback),
                "lam_f_a": form_idx.side_lambda(m.away_player, m.home_player,
                                                cut, fallback),
            }
            for c, pm in models.items():
                lam = pm.predict_sides(m.home_player, m.away_player,
                                       m.home_club, m.away_club)
                row[f"lam_c{c:g}_h"], row[f"lam_c{c:g}_a"] = lam
            rows.append(row)
        if verbose:
            print(f"  {day}: {len(scales)} fits", file=sys.stderr, flush=True)
    return pd.DataFrame(rows)


def sweep_club_report(preds: pd.DataFrame,
                      scales: tuple[float, ...] = CLUB_SCALES,
                      w: float = TOTALS_BLEND_WEIGHT,
                      baseline: float = 1.0) -> list[dict]:
    """Per-scale blend metrics + paired-bootstrap dAUC vs the served scale.

    Read the plateau, not the peak (sweep-multiform's rule): with ~15–20k
    matches, scales inside each other's CIs are the same scale — prefer 1.0
    unless a NEIGHBORING pair of scales both clear it, which is a slope, not
    a lucky argmax.
    """
    out = []
    prob_base = {}
    for line in EVAL_LINES:
        lam = (w * (preds[f"lam_c{baseline:g}_h"] + preds[f"lam_c{baseline:g}_a"])
               + (1 - w) * (preds["lam_f_h"] + preds["lam_f_a"])).to_numpy()
        prob_base[line] = _p_over_poisson_total(lam, line)
    for c in scales:
        lam = (w * (preds[f"lam_c{c:g}_h"] + preds[f"lam_c{c:g}_a"])
               + (1 - w) * (preds["lam_f_h"] + preds["lam_f_a"])).to_numpy()
        rec: dict = {"club_scale": c, "n": len(preds)}
        for line in EVAL_LINES:
            y = (preds["total"] > line).to_numpy().astype(int)
            prob = _p_over_poisson_total(lam, line)
            rec[f"auc_{line}"] = round(float(roc_auc_score(y, prob)), 4)
            rec[f"brier_{line}"] = round(float(np.mean((prob - y) ** 2)), 4)
            if c != baseline:
                lo, hi = _paired_auc_ci(y, prob_base[line], prob)
                rec[f"dauc_ci_{line}"] = [round(lo, 4), round(hi, 4)]
        out.append(rec)
    return out


def _x12_matrix(lam_h: np.ndarray, lam_a: np.ndarray,
                max_goals: int = PMF_MAX_GOALS) -> np.ndarray:
    """(n, 3) [p_home, p_draw, p_away] under independent Poisson sides.
    Vectorized mirror of core.markets.x12_probs (test-pinned to match)."""
    from scipy.stats import poisson as sp_poisson
    k = np.arange(max_goals + 1)
    ph = sp_poisson.pmf(k[None, :], lam_h[:, None])  # (n, K+1)
    pa = sp_poisson.pmf(k[None, :], lam_a[:, None])
    cdf_a = np.cumsum(pa, axis=1)
    p_draw = (ph * pa).sum(axis=1)
    p_home = (ph[:, 1:] * cdf_a[:, :-1]).sum(axis=1)  # P(A < k), k >= 1
    p_away = np.clip(1.0 - p_home - p_draw, 0.0, 1.0)
    return np.column_stack([p_home, p_draw, p_away])


def x12_report(preds: pd.DataFrame, w: float = TOTALS_BLEND_WEIGHT,
               n_boot: int = BOOTSTRAP_N, seed: int = 0) -> dict:
    """1x2 walk-forward gate (CLUB_FEATURE.md): can the λs price the match
    winner, and does club add signal there? Two arms on identical rows:

      blend       side λs from Poisson(player) blended with form  (served)
      blend+club  side λs from Poisson(player+club) + same form leg

    Headline is the decisive AUC — home-vs-away among non-draw matches on
    p_home/(p_home+p_away) — because the draw class is thin (~20%) and a
    3-class score would drown the ranking question in draw-calibration noise.
    Draw calibration is reported separately: predicted vs realized draw rate,
    which is the measurement that decides whether a Dixon-Coles-style
    diagonal is needed at all.
    """
    p = preds[preds["covered"]]
    arms = {}
    for name, pre in (("blend", "lam_p"), ("blend+club", "lam_pc")):
        lam_h = (w * p[f"{pre}_h"] + (1 - w) * p["lam_f_h"]).to_numpy()
        lam_a = (w * p[f"{pre}_a"] + (1 - w) * p["lam_f_a"]).to_numpy()
        arms[name] = _x12_matrix(lam_h, lam_a)

    gd = (p["home_ft"] - p["away_ft"]).to_numpy()
    outcome = np.select([gd > 0, gd == 0], [0, 1], default=2)  # H/D/A
    onehot = np.eye(3)[outcome]
    decisive = outcome != 1
    y_home = (outcome[decisive] == 0).astype(int)

    out: dict = {"n": len(p), "n_decisive": int(decisive.sum()),
                 "w": w, "realized": {"home": round(float((outcome == 0).mean()), 4),
                                      "draw": round(float((outcome == 1).mean()), 4),
                                      "away": round(float((outcome == 2).mean()), 4)}}
    base = onehot.mean(axis=0)  # league base rates as the constant predictor
    out["brier3_base"] = round(float(((base - onehot) ** 2).sum(axis=1).mean()), 4)

    score = {}
    for name, m in arms.items():
        score[name] = (m[decisive, 0] / (m[decisive, 0] + m[decisive, 2]))
        out[f"{name}_auc_decisive"] = round(
            float(roc_auc_score(y_home, score[name])), 4)
        out[f"{name}_brier3"] = round(
            float(((m - onehot) ** 2).sum(axis=1).mean()), 4)
        out[f"{name}_p_draw_mean"] = round(float(m[:, 1].mean()), 4)
        out[f"{name}_p_home_mean"] = round(float(m[:, 0].mean()), 4)

    lo, hi = _paired_auc_ci(y_home, score["blend"], score["blend+club"],
                            n_boot=n_boot, seed=seed)
    out["dauc_decisive"] = round(out["blend+club_auc_decisive"]
                                 - out["blend_auc_decisive"], 4)
    out["dauc_ci"] = [round(lo, 4), round(hi, 4)]
    out["significant"] = bool(lo > 0)

    # Draw calibration by predicted-draw decile: is the Poisson diagonal
    # short (real-football folklore) or fine (this league's empirics)?
    m = arms["blend+club"]
    df = pd.DataFrame({"p_draw": m[:, 1], "is_draw": (outcome == 1).astype(int)})
    df["bin"] = pd.qcut(df["p_draw"], 5, duplicates="drop")
    out["draw_calib"] = df.groupby("bin", observed=True).agg(
        n=("is_draw", "size"), pred=("p_draw", "mean"),
        real=("is_draw", "mean")).round(4)
    return out


SKILL_HALF_LIFE_DAYS = 60.0  # the "slow skill" scale the 7d GLM forgets


def _control_frame(df: pd.DataFrame, lag_min: float = DEFAULT_LAG_MIN) -> pd.DataFrame:
    """NON-pairwise control features for the h2h gate (docs/H2H_FEATURE.md),
    one visibility-honest pass over the match frame:

      skill_life / skill_60d  home−away overall decisive win-rate diff
                              (lifetime shrunk / 60d-decayed) — absorbs
                              "H2H is just slow skill the half-life forgot"
      ppace_h / ppace_a       each player's 7d-decayed mean match total vs
                              the league running mean — absorbs "pair pace
                              is just player pace the blend under-weights"

    The controls are part of the gate, not scaffolding: the pairwise verdict
    is the increment OVER them.
    """
    lag = pd.Timedelta(minutes=lag_min)
    skill: dict[str, dict] = {}
    pace: dict[str, dict] = {}
    glob = {"n": 0, "sum": 0.0}
    pending: deque = deque()  # (publish, kickoff, player, sign, total)
    out = []

    def _decayed(st: dict, at: pd.Timestamp, hl: float) -> tuple[float, float]:
        if st["ts"] is None:
            return 0.0, 0.0
        f = 0.5 ** ((at - st["ts"]).total_seconds() / 86400.0 / hl)
        return st["n_d"] * f, st["sum_d"] * f

    def _fold(st: dict, ts: pd.Timestamp, val: float, hl: float) -> None:
        if st["ts"] is not None:
            f = 0.5 ** ((ts - st["ts"]).total_seconds() / 86400.0 / hl)
            st["n_d"] *= f
            st["sum_d"] *= f
        st["ts"] = ts
        st["n_d"] += 1.0
        st["sum_d"] += val

    for m in df.itertuples():
        while pending and pending[0][0] <= m.kickoff_ts:
            _, ts, player, sign, total = pending.popleft()
            pc = pace.setdefault(player, {"ts": None, "n_d": 0.0, "sum_d": 0.0})
            _fold(pc, ts, total, h2h.H2H_HALF_LIFE_DAYS)
            glob["n"] += 1
            glob["sum"] += total
            if sign != 0.0:
                sk = skill.setdefault(player, {"ts": None, "n_d": 0.0,
                                               "sum_d": 0.0, "n": 0, "diff": 0.0})
                _fold(sk, ts, sign, SKILL_HALF_LIFE_DAYS)
                sk["n"] += 1
                sk["diff"] += sign
        league_mean = glob["sum"] / glob["n"] if glob["n"] else 0.0

        def _skill(player: str) -> tuple[float, float]:
            sk = skill.get(player)
            if sk is None:
                return 0.0, 0.0
            n_d, diff_d = _decayed(sk, m.kickoff_ts, SKILL_HALF_LIFE_DAYS)
            return (sk["diff"] / (sk["n"] + h2h.SHRINK_LIFE),
                    diff_d / (n_d + h2h.SHRINK_DECAY))

        def _ppace(player: str) -> float:
            pc = pace.get(player)
            if pc is None:
                return 0.0
            n_d, sum_d = _decayed(pc, m.kickoff_ts, h2h.H2H_HALF_LIFE_DAYS)
            return (sum_d - n_d * league_mean) / (n_d + h2h.SHRINK_DECAY)

        h_life, h_60 = _skill(m.home_player)
        a_life, a_60 = _skill(m.away_player)
        out.append({"match_id": m.match_id,
                    "skill_life": h_life - a_life, "skill_60d": h_60 - a_60,
                    "ppace_h": _ppace(m.home_player),
                    "ppace_a": _ppace(m.away_player)})

        gd = m.home_ft - m.away_ft
        total = float(m.home_ft + m.away_ft)
        # glob counts one total per PLAYER event (two per match): the mean is
        # unbiased, matches the per-player pace weighting, and stays one queue
        pending.append((m.kickoff_ts + lag, m.kickoff_ts, m.home_player,
                        float(np.sign(gd)), total))
        pending.append((m.kickoff_ts + lag, m.kickoff_ts, m.away_player,
                        float(-np.sign(gd)), total))
    return pd.DataFrame(out)


def _h2h_eval_frame(preds: pd.DataFrame, df: pd.DataFrame,
                    w: float = TOTALS_BLEND_WEIGHT,
                    lag_min: float = DEFAULT_LAG_MIN,
                    half_life: float = h2h.H2H_HALF_LIFE_DAYS,
                    shrink_life: float = h2h.SHRINK_LIFE,
                    shrink_decay: float = h2h.SHRINK_DECAY) -> pd.DataFrame:
    """Covered eval rows + H2H features (each at its own kickoff) + the
    served-arm scores: x12_s (decisive share) and mu (blend λ total).

    Uses the club λs when preds carries them (the served 1x2 arm) and falls
    back to the player-only λs on club-less frames (synthetic test leagues).
    """
    idx = h2h.H2HIndex(df, lag_min=lag_min, half_life=half_life,
                       shrink_life=shrink_life, shrink_decay=shrink_decay)
    feats = idx.frame(df[df["match_id"].isin(preds["match_id"])])
    p = preds[preds["covered"]].merge(feats, on="match_id", how="left")
    pre = "lam_pc" if "lam_pc_h" in p.columns else "lam_p"
    lam_h = (w * p[f"{pre}_h"] + (1 - w) * p["lam_f_h"]).to_numpy()
    lam_a = (w * p[f"{pre}_a"] + (1 - w) * p["lam_f_a"]).to_numpy()
    m3 = _x12_matrix(lam_h, lam_a)
    p["x12_s"] = m3[:, 0] / np.maximum(m3[:, 0] + m3[:, 2], 1e-12)
    p["mu"] = lam_h + lam_a
    return p.reset_index(drop=True)


def _stack_walk(dates: np.ndarray, arms: dict[str, np.ndarray], y: np.ndarray,
                warmup_days: int) -> tuple[dict[str, np.ndarray], np.ndarray, dict]:
    """Daily-refit logistic stacking, the recal fitting rule: each eval day
    is predicted by a model fit on prior eval days only. All arms share the
    refit cadence, so extra columns are the only difference between them.
    Returns (per-arm prob arrays, scored mask, last-day coefficients)."""
    days = sorted(set(dates))
    if len(days) <= warmup_days + 1:
        raise ValueError(f"need > {warmup_days + 1} eval days for warmup")
    prob = {a: np.full(len(y), np.nan) for a in arms}
    coefs: dict[str, list] = {}
    for day in days[warmup_days:]:
        hist, cur = dates < day, dates == day
        yh = y[hist]
        if yh.min() == yh.max():  # degenerate day: constant predictor
            for a in arms:
                prob[a][cur] = float(yh.mean())
            continue
        for a, x in arms.items():
            lr = LogisticRegression(C=1e6, max_iter=1000).fit(x[hist], yh)
            prob[a][cur] = lr.predict_proba(x[cur])[:, 1]
            coefs[a] = [round(float(v), 4)
                        for v in np.r_[lr.intercept_, lr.coef_[0]]]
    return prob, dates >= days[warmup_days], coefs


def _split_half_dauc(dates: np.ndarray, y: np.ndarray, prob_a: np.ndarray,
                     prob_b: np.ndarray) -> dict[str, float | None]:
    """dAUC(b − a) on the first vs second half of scored days. A real effect
    shows the same sign in both halves (the tod stability precedent)."""
    days = sorted(set(dates))
    out = {}
    for name, half in (("h1", days[:len(days) // 2]),
                       ("h2", days[len(days) // 2:])):
        m = np.isin(dates, half)
        if len(np.unique(y[m])) < 2:
            out[f"{name}_dauc"] = None
            continue
        out[f"{name}_dauc"] = round(
            float(roc_auc_score(y[m], prob_b[m])
                  - roc_auc_score(y[m], prob_a[m])), 4)
    return out


def h2h_report(preds: pd.DataFrame, df: pd.DataFrame,
               w: float = TOTALS_BLEND_WEIGHT,
               lag_min: float = DEFAULT_LAG_MIN,
               warmup_days: int = PHI_WARMUP_DAYS, lines=EVAL_LINES) -> dict:
    """Pairwise H2H gate (docs/H2H_FEATURE.md), walk-forward, both markets.

    1x2 block (decisive rows, stacked on the served x12 score):
      base        Platt of the served decisive share (refit daily — equal
                  capacity, so extra features are the only arm difference)
      +h2h        base + edge_life + edge_decay + gd_decay
      +skill      base + lifetime/60d overall win-rate diff (control)
      +skill+h2h  the pairwise verdict is THIS increment over +skill

    totals block (per line, stacked on the served P(over)):
      +pace       base + pair pace (lifetime + 7d-decayed)
      +ppace      base + player pace (control)
      +ppace+pace the pairwise verdict, as above

    The controls are load-bearing: without them a pass could be slow player
    skill (1x2) or under-weighted player pace (totals), which are different
    features with different fixes.
    """
    p = _h2h_eval_frame(preds, df, w, lag_min).merge(
        _control_frame(df, lag_min), on="match_id", how="left")
    score_logit = np.log(np.clip(p["x12_s"], 1e-6, 1 - 1e-6)
                         / np.clip(1 - p["x12_s"], 1e-6, 1 - 1e-6))
    out: dict = {"n": len(p), "w": w,
                 "census": {
                     "h2h_n_median": float(p["h2h_n"].median()),
                     "h2h_n_ge5_share": round(float((p["h2h_n"] >= 5).mean()), 4),
                     # high corr = the confound is real and the +skill
                     # control arm is doing necessary work
                     "corr_score_edge_life": round(float(
                         np.corrcoef(score_logit, p["edge_life"])[0, 1]), 4),
                 }}

    # ---- 1x2 block, decisive rows only (the x12_report headline rule)
    d = p[p["home_ft"] != p["away_ft"]].reset_index(drop=True)
    x0 = np.log(np.clip(d["x12_s"], 1e-6, 1 - 1e-6)
                / np.clip(1 - d["x12_s"], 1e-6, 1 - 1e-6)).to_numpy()
    h2h_cols = ["edge_life", "edge_decay", "gd_decay"]
    skill_cols = ["skill_life", "skill_60d"]
    arms = {
        "base": np.column_stack([x0]),
        "+h2h": np.column_stack([x0, d[h2h_cols]]),
        "+skill": np.column_stack([x0, d[skill_cols]]),
        "+skill+h2h": np.column_stack([x0, d[skill_cols], d[h2h_cols]]),
    }
    dates = d["date"].to_numpy()
    y = (d["home_ft"] > d["away_ft"]).to_numpy().astype(int)
    prob, scored, coefs = _stack_walk(dates, arms, y, warmup_days)
    ys = y[scored]
    x12: dict = {"n_decisive_scored": int(scored.sum()), "coefs_last": coefs}
    for a in arms:
        x12[f"{a}_auc"] = round(float(roc_auc_score(ys, prob[a][scored])), 4)
        if a != "base":
            lo, hi = _paired_auc_ci(ys, prob["base"][scored], prob[a][scored])
            x12[f"dauc_{a}"] = round(x12[f"{a}_auc"] - x12["base_auc"], 4)
            x12[f"dauc_ci_{a}"] = [round(lo, 4), round(hi, 4)]
            x12[f"significant_{a}"] = bool(lo > 0)
    lo, hi = _paired_auc_ci(ys, prob["+skill"][scored], prob["+skill+h2h"][scored])
    x12["pairwise_dauc"] = round(x12["+skill+h2h_auc"] - x12["+skill_auc"], 4)
    x12["pairwise_ci"] = [round(lo, 4), round(hi, 4)]
    x12["pairwise_significant"] = bool(lo > 0)
    x12["split_half"] = _split_half_dauc(dates[scored], ys,
                                         prob["base"][scored],
                                         prob["+h2h"][scored])
    out["x12"] = x12

    # ---- totals block, per line
    pace_cols = ["pace_life", "pace_decay"]
    ppace_cols = ["ppace_h", "ppace_a"]
    dates_t = p["date"].to_numpy()
    out["totals"] = {}
    for line in lines:
        prob0 = _p_over_poisson_total(p["mu"].to_numpy(), line)
        x0 = np.log(np.clip(prob0, 1e-6, 1 - 1e-6)
                    / np.clip(1 - prob0, 1e-6, 1 - 1e-6))
        arms = {
            "base": np.column_stack([x0]),
            "+pace": np.column_stack([x0, p[pace_cols]]),
            "+ppace": np.column_stack([x0, p[ppace_cols]]),
            "+ppace+pace": np.column_stack([x0, p[ppace_cols], p[pace_cols]]),
        }
        y = (p["total"] > line).to_numpy().astype(int)
        prob, scored, coefs = _stack_walk(dates_t, arms, y, warmup_days)
        ys, rec = y[scored], {"n": int(scored.sum())}
        for a in arms:
            rec[f"{a}_auc"] = round(float(roc_auc_score(ys, prob[a][scored])), 4)
            rec[f"{a}_brier"] = round(float(
                np.mean((prob[a][scored] - ys) ** 2)), 4)
            if a != "base":
                lo, hi = _paired_auc_ci(ys, prob["base"][scored], prob[a][scored])
                rec[f"dauc_{a}"] = round(rec[f"{a}_auc"] - rec["base_auc"], 4)
                rec[f"dauc_ci_{a}"] = [round(lo, 4), round(hi, 4)]
                rec[f"significant_{a}"] = bool(lo > 0)
        lo, hi = _paired_auc_ci(ys, prob["+ppace"][scored],
                                prob["+ppace+pace"][scored])
        rec["pairwise_dauc"] = round(rec["+ppace+pace_auc"] - rec["+ppace_auc"], 4)
        rec["pairwise_ci"] = [round(lo, 4), round(hi, 4)]
        rec["pairwise_significant"] = bool(lo > 0)
        if line == HEADLINE_LINE:
            rec["split_half"] = _split_half_dauc(dates_t[scored], ys,
                                                 prob["base"][scored],
                                                 prob["+pace"][scored])
        out["totals"][f"{line:g}"] = rec
    return out


H2H_SWEEP_HALF_LIVES = (3.5, 7.0, 14.0)
H2H_SWEEP_SHRINKS = (5.0, 10.0, 20.0)


def h2h_sweep(preds: pd.DataFrame, df: pd.DataFrame,
              w: float = TOTALS_BLEND_WEIGHT,
              lag_min: float = DEFAULT_LAG_MIN,
              half_lives: tuple[float, ...] = H2H_SWEEP_HALF_LIVES,
              shrinks: tuple[float, ...] = H2H_SWEEP_SHRINKS,
              warmup_days: int = PHI_WARMUP_DAYS) -> list[dict]:
    """Feature-subset x half-life x shrinkage sweep for the serving sets.
    Read the plateau, not the peak (sweep-multiform's rule). dAUCs are vs
    the same refit base arm; 1x2 on decisive rows, totals at the headline
    line. edge_life ignores the half-life by construction — its row-to-row
    stability across half-lives is a consistency check, not information."""
    x12_sets = (("edge_life",), ("edge_life", "gd_decay"),
                ("edge_life", "edge_decay", "gd_decay"))
    tot_sets = (("pace_decay",), ("pace_life", "pace_decay"))
    out = []
    for hl in half_lives:
        for shrink in shrinks:
            if shrink != h2h.SHRINK_LIFE and hl != h2h.H2H_HALF_LIFE_DAYS:
                continue  # sweep axes independently around the default
            p = _h2h_eval_frame(preds, df, w, lag_min, half_life=hl,
                                shrink_life=shrink)
            rec: dict = {"half_life": hl, "shrink_life": shrink}
            d = p[p["home_ft"] != p["away_ft"]].reset_index(drop=True)
            x0 = np.log(np.clip(d["x12_s"], 1e-6, 1 - 1e-6)
                        / np.clip(1 - d["x12_s"], 1e-6, 1 - 1e-6)).to_numpy()
            arms = {"base": np.column_stack([x0])}
            arms |= {"+".join(fs): np.column_stack([x0, d[list(fs)]])
                     for fs in x12_sets}
            y = (d["home_ft"] > d["away_ft"]).to_numpy().astype(int)
            prob, scored, _ = _stack_walk(d["date"].to_numpy(), arms, y,
                                          warmup_days)
            base = float(roc_auc_score(y[scored], prob["base"][scored]))
            for fs in x12_sets:
                a = "+".join(fs)
                rec[f"x12_dauc_{a}"] = round(float(
                    roc_auc_score(y[scored], prob[a][scored])) - base, 4)

            prob0 = _p_over_poisson_total(p["mu"].to_numpy(), HEADLINE_LINE)
            x0 = np.log(np.clip(prob0, 1e-6, 1 - 1e-6)
                        / np.clip(1 - prob0, 1e-6, 1 - 1e-6))
            arms = {"base": np.column_stack([x0])}
            arms |= {"+".join(fs): np.column_stack([x0, p[list(fs)]])
                     for fs in tot_sets}
            y = (p["total"] > HEADLINE_LINE).to_numpy().astype(int)
            prob, scored, _ = _stack_walk(p["date"].to_numpy(), arms, y,
                                          warmup_days)
            base = float(roc_auc_score(y[scored], prob["base"][scored]))
            for fs in tot_sets:
                a = "+".join(fs)
                rec[f"tot_dauc_{a}"] = round(float(
                    roc_auc_score(y[scored], prob[a][scored])) - base, 4)
            out.append(rec)
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
    ap.add_argument("mode", choices=["gate", "dispersion", "sweep-poisson",
                                     "sweep-blend", "sweep-multiform", "sharpen",
                                     "recal", "club", "tod", "tod-club", "x12",
                                     "sweep-club", "conditional", "h2h"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--eval-days", type=int, default=45)
    ap.add_argument("--lag", type=float, default=DEFAULT_LAG_MIN)
    ap.add_argument("--half-life", type=float, default=poisson.HALF_LIFE_DAYS)
    ap.add_argument("--alpha", type=float, default=poisson.ALPHA)
    ap.add_argument("--span", type=int, default=heuristic.FORM_SPAN)
    ap.add_argument("--club-scales", default="0.25,0.5,1,2,4",
                    help="club-column scales for sweep-club (penalty alpha/c^2)")
    ap.add_argument("--spans", default="3,10,30",
                    help="comma-separated form spans for sweep-multiform")
    ap.add_argument("--step", type=float, default=0.1,
                    help="simplex weight grid step for sweep-multiform")
    ap.add_argument("--tod-k", type=int, default=poisson.TOD_FOURIER_K,
                    help="Fourier harmonics for the tod mode")
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

    if args.mode == "sweep-multiform":
        spans = tuple(int(x) for x in args.spans.split(","))
        preds = build_predictions(df, args.eval_days, args.lag, args.half_life,
                                  args.alpha, args.span, extra_spans=spans)
        base = metrics(preds, "blend", w=0.7)
        print("baseline blend w=0.7 (poisson + form span 8):")
        print(json.dumps(base))
        results = sweep_multiform(preds, spans, step=args.step)
        print(f"\ntop 10 of {len(results)} weight combos (line {HEADLINE_LINE}):")
        for r in results[:10]:
            print(json.dumps(r))
        best_w = {k[2:]: v for k, v in results[0].items() if k.startswith("w_")}
        best_all = metrics_weighted(preds, best_w)
        print("\nbest combo across all lines:")
        print(json.dumps(best_all))
        _record_run(conn, "sweep-multiform", vars(args),
                    {"baseline": base, "top25": results[:25],
                     "best_all_lines": best_all})
        return 0

    if args.mode == "sweep-club":
        scales = tuple(float(v) for v in args.club_scales.split(","))
        preds = sweep_club_predictions(df, scales, args.eval_days, args.lag,
                                       args.half_life, args.alpha, args.span,
                                       verbose=True)
        results = sweep_club_report(preds, scales)
        for r in results:
            print(json.dumps(r))
        _record_run(conn, "sweep-club", vars(args), results)
        return 0

    if args.mode == "x12":
        preds = build_predictions(df, args.eval_days, args.lag, args.half_life,
                                  args.alpha, args.span, with_club=True,
                                  verbose=True)
        rep = x12_report(preds)
        calib = rep.pop("draw_calib")
        print(json.dumps(rep, default=str, indent=2))
        print("\ndraw calibration (blend+club, quintiles of predicted p_draw):")
        print(calib.to_string())
        _record_run(conn, "x12", vars(args),
                    {**rep, "draw_calib": calib.rename(index=str).to_dict()})
        return 0

    if args.mode == "club":
        preds = build_predictions(df, args.eval_days, args.lag, args.half_life,
                                  args.alpha, args.span, with_club=True,
                                  verbose=True)
        rep = club_report(preds)
        print(json.dumps(rep, indent=2))
        for line in EVAL_LINES:
            lo, hi = rep[f"dauc_ci_{line}"]
            verdict = "SIG" if rep[f"significant_{line}"] else "wash"
            print(f"  line {line}: dAUC {rep[f'dauc_{line}']:+.4f} "
                  f"[{lo:+.4f},{hi:+.4f}]  "
                  f"dBrier {rep[f'dbrier_{line}']:+.4f}  {verdict}")
        _record_run(conn, "club", vars(args), rep)
        return 0

    if args.mode == "tod":
        preds = build_predictions(df, args.eval_days, args.lag, args.half_life,
                                  args.alpha, args.span, with_tod=True,
                                  tod_k=args.tod_k, verbose=True)
        rep = tod_report(preds)
        print(json.dumps(rep, indent=2))
        for line in EVAL_LINES:
            lo, hi = rep[f"dauc_ci_{line}"]
            verdict = "SIG" if rep[f"significant_{line}"] else "wash"
            print(f"  line {line}: dAUC {rep[f'dauc_{line}']:+.4f} "
                  f"[{lo:+.4f},{hi:+.4f}]  "
                  f"dBrier {rep[f'dbrier_{line}']:+.4f}  {verdict}")
        _record_run(conn, "tod", vars(args), rep)
        return 0

    if args.mode == "tod-club":
        preds = build_predictions(df, args.eval_days, args.lag, args.half_life,
                                  args.alpha, args.span, with_club=True,
                                  with_tod=True, tod_k=args.tod_k, verbose=True)
        rep = tod_club_report(preds)
        print(json.dumps(rep, indent=2))
        for line in EVAL_LINES:
            lo, hi = rep[f"dauc_ci_{line}"]
            verdict = "SIG" if rep[f"significant_{line}"] else "wash"
            print(f"  line {line}: dAUC(club -> club+tod) "
                  f"{rep[f'dauc_{line}']:+.4f} [{lo:+.4f},{hi:+.4f}]  "
                  f"dBrier {rep[f'dbrier_{line}']:+.4f}  {verdict}")
        _record_run(conn, "tod-club", vars(args), rep)
        return 0

    if args.mode == "h2h":
        preds = build_predictions(df, args.eval_days, args.lag, args.half_life,
                                  args.alpha, args.span, with_club=True,
                                  verbose=True)
        rep = h2h_report(preds, df, lag_min=args.lag)
        print(json.dumps(rep, indent=2, default=str))
        x = rep["x12"]
        for a in ("+h2h", "+skill", "+skill+h2h"):
            lo, hi = x[f"dauc_ci_{a}"]
            verdict = "SIG" if x[f"significant_{a}"] else "wash"
            print(f"  x12 {a}: dAUC {x[f'dauc_{a}']:+.4f} "
                  f"[{lo:+.4f},{hi:+.4f}]  {verdict}")
        lo, hi = x["pairwise_ci"]
        print(f"  x12 pairwise increment (+skill -> +skill+h2h): "
              f"{x['pairwise_dauc']:+.4f} [{lo:+.4f},{hi:+.4f}]  "
              f"{'SIG' if x['pairwise_significant'] else 'wash'}")
        for line, rec in rep["totals"].items():
            lo, hi = rec["pairwise_ci"]
            verdict = "SIG" if rec["pairwise_significant"] else "wash"
            print(f"  totals line {line}: +pace dAUC {rec['dauc_+pace']:+.4f}, "
                  f"pairwise {rec['pairwise_dauc']:+.4f} "
                  f"[{lo:+.4f},{hi:+.4f}]  {verdict}")
        print("\nsweep (read the plateau, not the peak):")
        sweep = h2h_sweep(preds, df, lag_min=args.lag)
        for r in sweep:
            print(json.dumps(r))
        _record_run(conn, "h2h", vars(args), {**rep, "sweep": sweep})
        return 0

    if args.mode == "conditional":
        preds = build_predictions(df, args.eval_days, args.lag, args.half_life,
                                  args.alpha, args.span, verbose=True)
        rep = conditional_report(preds)
        print(json.dumps(rep, indent=2, default=str))
        _record_run(conn, "conditional", vars(args), rep)
        return 0

    if args.mode == "recal":
        preds = build_predictions(df, args.eval_days, args.lag, args.half_life,
                                  args.alpha, args.span)
        rep = recal_report(preds)
        calib = rep.pop("calib")
        print(json.dumps(rep, default=str))
        for name, table in calib.items():
            print(f"\ncalibration ({name}, line {HEADLINE_LINE}):")
            print(table.to_string())
        _record_run(conn, "recal", vars(args),
                    {**rep, "calib": {k: v.rename(index=str).to_dict()
                                      for k, v in calib.items()}})
        return 0

    if args.mode == "sharpen":
        preds = build_predictions(df, args.eval_days, args.lag, args.half_life,
                                  args.alpha, args.span, with_var=True)
        rep = sharpen_report(preds)
        calib = rep.pop("calib")
        print(json.dumps(rep, default=str))
        for name, table in calib.items():
            print(f"\ncalibration ({name}, line {HEADLINE_LINE}):")
            print(table.to_string())
        _record_run(conn, "sharpen", vars(args),
                    {**rep, "calib": {k: v.rename(index=str).to_dict()
                                      for k, v in calib.items()}})
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
