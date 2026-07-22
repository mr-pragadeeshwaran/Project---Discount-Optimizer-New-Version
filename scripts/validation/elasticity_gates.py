"""
elasticity_gates.py — PricingAI three-stage elasticity validation protocol (val_02).

Applies the paper's acceptance protocol to the PRODUCTION elasticity estimates —
DISCOUNT_PLAN/pricing/elasticities.csv + cross_price.csv + gates.json — scored
against the weekly pricing panel built from the latest output/runs/2026*/fact_table.csv.
This is a downstream GATE, champion/challenger style: it edits nothing, it only
measures the matrix the optimizer is about to consume and returns a hard verdict.

THE THREE STAGES (each -> PASS/FAIL with the measured number vs its threshold):
  Stage 1 — statistical fit on a HOLDOUT (last N weeks, default 4):
      predict each holdout cell-week from the production matrix
      (per-cell log anchor + own_elast * dln price + cross_elast * sibling dln price),
      then require  holdout weighted R2 (log space) >= 0.50   (codebase R2_FLOOR),
                    wMAPE (units space)            <= 0.40    (codebase WMAPE_CEIL),
                    |bias| (units space)           <= 0.05    (paper-strict 5%).
  Stage 2 — sign / magnitude sanity on the matrix itself:
      share of cells with own_elast < 0                >= 0.95,
      share of cells with |own_elast| in [0.05, 5.0]   >= 0.95,
      share of cross pairs inside [-1.0, +1.0]         >= 0.99,
      share of cross pairs >= 0 (substitutes)          >= 0.50 (codebase gate),
      plus a REPORTED (non-gating) flag: share of cells pinned at the prior
      (own_sd > 0.6 = posterior ~ prior; the honest weak-identification signal).
  Stage 3 — stability: refit the estimator (same bayes->hier fallback chain the
      production engine uses) on two half-window splits of the panel and require
      |median own(half A) - median own(half B)| <= 0.30. Falls back to a jackknife
      (full vs drop-latest-4-weeks) when the panel is too short to halve.

Overall verdict = PASS only if ALL three stages pass. Exit code 0 on PASS,
1 on any hard failure — so a cron retrain can gate on it:
    python -X utf8 scripts/validation/elasticity_gates.py   && <promote matrix>
(--report-only writes the same reports but always exits 0, for human report runs.)

OUTPUTS
  DISCOUNT_PLAN/validation/elasticity_validation.json  — full machine-readable gates
  DISCOUNT_PLAN/validation/ELASTICITY_GATES.md         — business-readable scorecard

HOW TO READ IT
  A stage-1 FAIL means the matrix does not predict held-out weeks well enough to
  trust its point estimates for banked savings — act on it via live TESTS only.
  A high "pinned at prior" share in stage 2 means the posteriors are mostly the
  prior (weak identification); stage-3 stability is then partly the PRIOR being
  stable, not the data — the report says so explicitly. No number is softened.

Self-test:  python -X utf8 scripts/validation/elasticity_gates.py --selfcheck
  (planted synthetic matrix must PASS stage 2; a sign-flipped corruption must FAIL).
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts", "pricing"))
import pricing_panel as pp                     # noqa: E402
try:
    import elasticity_bayes as est_mod         # same fallback chain as pricing_engine
    EST_METHOD = "bayes"
except Exception:                              # pragma: no cover
    import elasticity_hier as est_mod
    EST_METHOD = "hier"

PRICING_DIR = os.path.join(ROOT, "output", "DISCOUNT_PLAN", "pricing")
OUT_DIR = os.path.join(ROOT, "output", "DISCOUNT_PLAN", "validation")

# ── HARD thresholds (documented; sources cited) ──────────────────────────────
WMAPE_CEIL = 0.40            # reuse elasticity_hier.WMAPE_CEIL (the codebase's bar)
R2_FLOOR_HOLDOUT = 0.50      # reuse elasticity_hier.R2_FLOOR, applied to the holdout
BIAS_CEIL = 0.05             # paper-strict |bias| <= 5% (codebase's own bar is 10%)
OWN_NEG_SHARE_MIN = 0.95     # >= 95% of cells must have own_elast < 0
OWN_ABS_LO, OWN_ABS_HI = 0.05, 5.0   # |own| plausibility band
OWN_MAG_SHARE_MIN = 0.95
CROSS_LO, CROSS_HI = -1.0, 1.0       # per-pair cross clip band (pairs are cat-share/n)
CROSS_BAND_SHARE_MIN = 0.99
CROSS_NONNEG_SHARE_MIN = 0.50        # reuse gates.json 'cross_nonneg_subs' convention
PRIOR_PIN_SD = 0.6           # own_sd above this = posterior ~ prior (bayes WIDE_SD)
DRIFT_MAX = 0.30             # stage 3: |median own drift| between refits
HOLDOUT_WEEKS = 4
MIN_WEEKS_HALF_SPLIT = 12    # need >= this many distinct weeks to half-split
MIN_WEEKS_JACKKNIFE = 8      # else jackknife (drop latest 4 wk); below this: FAIL
EPS = 1e-9


def _clean_pid(v):
    """'532393.0' -> '532393' (same convention as pricing_engine._clean_pid)."""
    if v is None:
        return ""
    if isinstance(v, float):
        return str(int(v)) if float(v).is_integer() else str(v)
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        return s[:-2]
    return s


def _latest_fact_table():
    for r in sorted(glob.glob(os.path.join(ROOT, "output", "runs", "2026*")), reverse=True):
        f = os.path.join(r, "fact_table.csv")
        if os.path.exists(f):
            return f, r
    raise SystemExit("no fact_table.csv under output/runs/2026* — run pipeline.py first")


def _wavg(x, w):
    w = np.clip(np.asarray(w, float), EPS, None)
    return float(np.average(np.asarray(x, float), weights=w))


def _prep_panel(panel_df):
    """Filter to loggable rows, add weights/logs/clean ids/week dates."""
    p = panel_df.copy()
    p = p[(pd.to_numeric(p["units"], errors="coerce") > 0)
          & (pd.to_numeric(p["price"], errors="coerce") > 0)].copy()
    p["pid"] = p["product_id"].map(_clean_pid)
    rw = p["recency_w"].to_numpy(float) if "recency_w" in p else np.ones(len(p))
    vw = p["volume_w"].to_numpy(float) if "volume_w" in p else np.ones(len(p))
    p["w"] = np.clip(np.nan_to_num(rw * vw, nan=EPS), EPS, None)
    p["ln_u"] = np.log(p["units"].to_numpy(float))
    p["ln_p"] = np.log(p["price"].to_numpy(float))
    p["week_dt"] = pd.to_datetime(p["week"])
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — statistical fit on a holdout
# ─────────────────────────────────────────────────────────────────────────────
def stage1_fit(panel_df, elast_df, cross_df, holdout_weeks=HOLDOUT_WEEKS):
    """Score the PRODUCTION matrix against held-out weeks of the panel.

    Prediction per holdout row of cell i (log-log, matching the estimators):
        ln_u_hat = anchor_ln_u_i + own_i*(ln_p_it - anchor_ln_p_i)
                   + sum_j cross_ij*(ln_p_jt - anchor_ln_p_j)
    where anchors are the recency*volume-weighted means over TRAIN weeks.
    Units-space wMAPE/bias follow the elasticity_hier._gates convention
    (exp of the log fit) — any Jensen-driven under-prediction is reported, not hidden.
    """
    p = _prep_panel(panel_df)
    weeks = sorted(p["week_dt"].unique())
    if len(weeks) < 3:
        return {"pass": False, "reason": f"only {len(weeks)} distinct weeks — cannot hold out"}
    n_hold = holdout_weeks if len(weeks) > holdout_weeks + 4 else max(1, len(weeks) // 4)
    hold_wk = set(weeks[-n_hold:])
    train = p[~p["week_dt"].isin(hold_wk)]
    test = p[p["week_dt"].isin(hold_wk)]

    # anchors per cell from train rows (need >= 2 rows)
    anchors = {}
    for (pid, city), g in train.groupby(["pid", "city"]):
        if len(g) >= 2:
            anchors[(pid, city)] = (_wavg(g["ln_u"], g["w"]), _wavg(g["ln_p"], g["w"]))

    e = elast_df.copy()
    e["pid"] = e["product_id"].map(_clean_pid)
    own = {(r["pid"], str(r["city"])): float(r["own_elast"]) for _, r in e.iterrows()}

    # cross lookup: (pid_i, city) -> [(pid_j, cross_elast)]; hier version has no city col
    cross = {}
    if cross_df is not None and len(cross_df):
        c = cross_df.copy()
        c["pi"] = c["product_i"].map(_clean_pid)
        c["pj"] = c["product_j"].map(_clean_pid)
        has_city = "city" in c.columns
        for _, r in c.iterrows():
            key = (r["pi"], str(r["city"])) if has_city else (r["pi"], None)
            cross.setdefault(key, []).append((r["pj"], float(r["cross_elast"])))

    # holdout price lookup: (pid, city, week) -> ln_p
    lnp_wk = {(r.pid, str(r.city), r.week_dt): r.ln_p for r in test.itertuples()}

    y, yhat, w = [], [], []
    n_scored = n_no_anchor = n_no_elast = 0
    for r in test.itertuples():
        key = (r.pid, str(r.city))
        if key not in own:
            n_no_elast += 1
            continue
        if key not in anchors:
            n_no_anchor += 1
            continue
        a_lnu, a_lnp = anchors[key]
        pred = a_lnu + own[key] * (r.ln_p - a_lnp)
        sibs = cross.get(key) or cross.get((r.pid, None)) or []
        for pj, ce in sibs:
            kj = (pj, str(r.city))
            lpj = lnp_wk.get((pj, str(r.city), r.week_dt))
            if lpj is not None and kj in anchors:
                pred += ce * (lpj - anchors[kj][1])
        y.append(r.ln_u); yhat.append(pred); w.append(r.w)
        n_scored += 1

    if n_scored < 10:
        return {"pass": False, "reason": f"only {n_scored} scorable holdout rows",
                "n_scored": n_scored, "n_no_anchor": n_no_anchor, "n_no_elast": n_no_elast}

    y = np.array(y); yhat = np.array(yhat); w = np.array(w)
    ybar = np.average(y, weights=w)
    ss_res = float(np.sum(w * (y - yhat) ** 2))
    ss_tot = float(np.sum(w * (y - ybar) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > EPS else np.nan
    u_true, u_pred = np.exp(y), np.exp(yhat)
    denom = float(np.sum(w * u_true)) + EPS
    wmape = float(np.sum(w * np.abs(u_pred - u_true)) / denom)
    bias = float(np.sum(w * (u_pred - u_true)) / denom)

    out = {
        "holdout_weeks": int(n_hold),
        "holdout_range": f"{min(hold_wk).date()}..{max(hold_wk).date()}",
        "n_holdout_rows_scored": int(n_scored),
        "n_rows_skipped_no_anchor": int(n_no_anchor),
        "n_rows_skipped_no_elasticity": int(n_no_elast),
        "r2_log_holdout": round(float(r2), 3),
        "r2_floor": R2_FLOOR_HOLDOUT,
        "r2_pass": bool(np.isfinite(r2) and r2 >= R2_FLOOR_HOLDOUT),
        "wmape_units": round(wmape, 3),
        "wmape_ceil": WMAPE_CEIL,
        "wmape_pass": bool(wmape <= WMAPE_CEIL),
        "abs_bias_units": round(abs(bias), 3),
        "bias_signed": round(bias, 3),
        "bias_ceil": BIAS_CEIL,
        "bias_pass": bool(abs(bias) <= BIAS_CEIL),
    }
    out["pass"] = bool(out["r2_pass"] and out["wmape_pass"] and out["bias_pass"])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — sign / magnitude sanity on the matrix itself
# ─────────────────────────────────────────────────────────────────────────────
def stage2_sanity(elast_df, cross_df, gates_prod=None):
    e = elast_df.drop_duplicates(subset=["product_id", "city"]).copy()
    own = pd.to_numeric(e["own_elast"], errors="coerce").dropna().to_numpy(float)
    if own.size == 0:
        return {"pass": False, "reason": "elasticities.csv has no usable own_elast"}
    share_neg = float((own < 0).mean())
    share_mag = float(((np.abs(own) >= OWN_ABS_LO) & (np.abs(own) <= OWN_ABS_HI)).mean())

    if cross_df is not None and len(cross_df):
        ce = pd.to_numeric(cross_df["cross_elast"], errors="coerce").dropna().to_numpy(float)
        share_band = float(((ce >= CROSS_LO) & (ce <= CROSS_HI)).mean())
        share_nonneg = float((ce >= 0).mean())
        n_cross = int(ce.size)
    else:
        share_band, share_nonneg, n_cross = 1.0, 1.0, 0   # no cross pairs = nothing wrong

    # pinned-at-prior FLAG (reported, not gated): posterior SD so wide the point
    # estimate is essentially the prior. Uses low_confidence when present, else own_sd.
    if "low_confidence" in e.columns:
        pinned_share = float(e["low_confidence"].astype(bool).mean())
    elif "own_sd" in e.columns:
        sd = pd.to_numeric(e["own_sd"], errors="coerce")
        pinned_share = float(((sd > PRIOR_PIN_SD) | sd.isna()).mean())  # NaN sd = unknown = pinned
    else:
        pinned_share = float("nan")
    mu_g = None
    if isinstance(gates_prod, dict):
        mu_g = gates_prod.get("global_own (mu_g)")

    out = {
        "n_cells": int(own.size),
        "own_neg_share": round(share_neg, 3), "own_neg_min": OWN_NEG_SHARE_MIN,
        "own_neg_pass": bool(share_neg >= OWN_NEG_SHARE_MIN),
        "own_mag_share_in_band": round(share_mag, 3),
        "own_mag_band": [OWN_ABS_LO, OWN_ABS_HI], "own_mag_min": OWN_MAG_SHARE_MIN,
        "own_mag_pass": bool(share_mag >= OWN_MAG_SHARE_MIN),
        "n_cross_pairs": n_cross,
        "cross_in_band_share": round(share_band, 3),
        "cross_band": [CROSS_LO, CROSS_HI], "cross_band_min": CROSS_BAND_SHARE_MIN,
        "cross_band_pass": bool(share_band >= CROSS_BAND_SHARE_MIN),
        "cross_nonneg_share": round(share_nonneg, 3),
        "cross_nonneg_min": CROSS_NONNEG_SHARE_MIN,
        "cross_nonneg_pass": bool(share_nonneg >= CROSS_NONNEG_SHARE_MIN),
        "pinned_at_prior_share": round(pinned_share, 3) if np.isfinite(pinned_share) else None,
        "pinned_at_prior_flag": bool(np.isfinite(pinned_share) and pinned_share > 0.5),
        "global_prior_mu_g": mu_g,
    }
    out["pass"] = bool(out["own_neg_pass"] and out["own_mag_pass"]
                       and out["cross_band_pass"] and out["cross_nonneg_pass"])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3 — stability: half-window refits (or jackknife when the panel is short)
# ─────────────────────────────────────────────────────────────────────────────
def _refit_median_own(panel_sub):
    """Refit with the production estimator chain; return (median own, n_cells)."""
    e, _, _, _ = est_mod.estimate_elasticities(panel_sub)
    v = pd.to_numeric(e["own_elast"], errors="coerce").dropna()
    return float(v.median()), int(len(v))


def stage3_stability(panel_df, elast_df, drift_max=DRIFT_MAX):
    p = panel_df.copy()
    p["_wk"] = pd.to_datetime(p["week"])
    weeks = sorted(p["_wk"].unique())
    prod_median = float(pd.to_numeric(elast_df["own_elast"], errors="coerce").median())
    out = {"drift_max": drift_max, "production_median_own": round(prod_median, 3),
           "refit_method": EST_METHOD, "n_weeks": len(weeks)}
    try:
        if len(weeks) >= MIN_WEEKS_HALF_SPLIT:
            half = len(weeks) // 2
            a = p[p["_wk"].isin(weeks[:half])].drop(columns="_wk")
            b = p[p["_wk"].isin(weeks[half:])].drop(columns="_wk")
            med_a, n_a = _refit_median_own(a)
            med_b, n_b = _refit_median_own(b)
            out.update({"mode": "half_split",
                        "window_a": f"{pd.Timestamp(weeks[0]).date()}..{pd.Timestamp(weeks[half-1]).date()} ({n_a} cells)",
                        "window_b": f"{pd.Timestamp(weeks[half]).date()}..{pd.Timestamp(weeks[-1]).date()} ({n_b} cells)",
                        "median_own_a": round(med_a, 3), "median_own_b": round(med_b, 3)})
            drift = abs(med_a - med_b)
        elif len(weeks) >= MIN_WEEKS_JACKKNIFE:
            full_med, n_f = _refit_median_own(p.drop(columns="_wk"))
            jk = p[p["_wk"].isin(weeks[:-4])].drop(columns="_wk")
            jk_med, n_j = _refit_median_own(jk)
            out.update({"mode": "jackknife_drop_latest_4wk",
                        "median_own_full": round(full_med, 3),
                        "median_own_jackknife": round(jk_med, 3),
                        "n_cells": [n_f, n_j]})
            drift = abs(full_med - jk_med)
        else:
            out.update({"mode": "insufficient_data", "pass": False,
                        "reason": f"only {len(weeks)} weeks (< {MIN_WEEKS_JACKKNIFE}) — stability untestable"})
            return out
    except Exception as exc:   # a refit crash is a hard failure, not a silent pass
        out.update({"pass": False, "reason": f"refit failed: {exc}"})
        return out
    out["median_own_drift"] = round(float(drift), 3)
    out["pass"] = bool(drift <= drift_max)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Report writers
# ─────────────────────────────────────────────────────────────────────────────
def _fmt_pass(b):
    return "PASS" if b else "FAIL"


def _write_markdown(res, path):
    s1, s2, s3 = res["stage1"], res["stage2"], res["stage3"]
    L = ["# Elasticity Validation Gates — 3-Stage Protocol\n",
         f"*Run `{res['run']}` · matrix `{res['inputs']['elasticities']}` · "
         f"{s2.get('n_cells', '?')} SKU×city cells · generated {res['generated']}*\n",
         f"## Verdict: **{res['verdict']}**"
         + ("" if res["all_pass"] else " — do NOT bank savings from this matrix; act via live tests only")
         + "\n"]

    L.append("## Stage 1 — statistical fit (holdout "
             f"{s1.get('holdout_weeks', '?')} weeks, {s1.get('holdout_range', 'n/a')}) — **{_fmt_pass(s1.get('pass'))}**\n")
    if "reason" in s1:
        L.append(f"- Could not evaluate: {s1['reason']}\n")
    else:
        L.append("| Metric | Measured | Threshold | Verdict |")
        L.append("|---|---:|---:|---|")
        L.append(f"| Holdout R² (log space, weighted) | {s1['r2_log_holdout']:.3f} | ≥ {s1['r2_floor']:.2f} | {_fmt_pass(s1['r2_pass'])} |")
        L.append(f"| wMAPE (units) | {s1['wmape_units']:.3f} | ≤ {s1['wmape_ceil']:.2f} | {_fmt_pass(s1['wmape_pass'])} |")
        L.append(f"| \\|bias\\| (units) | {s1['abs_bias_units']:.3f} (signed {s1['bias_signed']:+.3f}) | ≤ {s1['bias_ceil']:.2f} | {_fmt_pass(s1['bias_pass'])} |")
        L.append(f"\n- Scored {s1['n_holdout_rows_scored']} holdout cell-weeks "
                 f"(skipped: {s1['n_rows_skipped_no_anchor']} thin-history, "
                 f"{s1['n_rows_skipped_no_elasticity']} no elasticity row).")
        L.append("- Prediction = per-cell weighted log anchor + own/cross elasticity terms; "
                 "wMAPE/bias in units space follow the estimator's own gate convention "
                 "(exp of the log fit; the log-anchor Jensen effect pulls bias slightly "
                 "negative, so it is not the excuse here) — reported, not corrected away.")
        if s1.get("bias_signed", 0) > s1.get("bias_ceil", BIAS_CEIL):
            L.append(f"- Signed bias is **+{s1['bias_signed']*100:.1f}%** = systematic "
                     "OVER-prediction: the holdout weeks sold below what the trailing "
                     "anchor + elasticity model expects. The matrix is not tracking the "
                     "recent demand level, let alone the price response.")
        L.append("")

    L.append(f"## Stage 2 — sign & magnitude sanity — **{_fmt_pass(s2.get('pass'))}**\n")
    if "reason" in s2:
        L.append(f"- Could not evaluate: {s2['reason']}\n")
    else:
        L.append("| Check | Measured | Threshold | Verdict |")
        L.append("|---|---:|---:|---|")
        L.append(f"| Own elasticity negative (share of {s2['n_cells']} cells) | {s2['own_neg_share']:.3f} | ≥ {s2['own_neg_min']:.2f} | {_fmt_pass(s2['own_neg_pass'])} |")
        L.append(f"| \\|own\\| in [{OWN_ABS_LO}, {OWN_ABS_HI}] | {s2['own_mag_share_in_band']:.3f} | ≥ {s2['own_mag_min']:.2f} | {_fmt_pass(s2['own_mag_pass'])} |")
        L.append(f"| Cross pairs in [{CROSS_LO}, {CROSS_HI}] (n={s2['n_cross_pairs']}) | {s2['cross_in_band_share']:.3f} | ≥ {s2['cross_band_min']:.2f} | {_fmt_pass(s2['cross_band_pass'])} |")
        L.append(f"| Cross pairs ≥ 0 (substitutes) | {s2['cross_nonneg_share']:.3f} | ≥ {s2['cross_nonneg_min']:.2f} | {_fmt_pass(s2['cross_nonneg_pass'])} |")
        pin = s2.get("pinned_at_prior_share")
        if pin is not None:
            L.append(f"| Cells pinned at the prior (flag only, not gated) | {pin:.3f} | reported | "
                     f"{'FLAGGED' if s2['pinned_at_prior_flag'] else 'ok'} |")
            if s2["pinned_at_prior_flag"]:
                L.append(f"\n**{pin*100:.0f}% of cells are pinned at the prior** (posterior SD > "
                         f"{PRIOR_PIN_SD}): the data barely moves the estimate off the prior mean "
                         f"({s2.get('global_prior_mu_g')}). Signs and magnitudes above pass largely "
                         "BECAUSE of the prior, not because the data identified them. That is the "
                         "honest weak-identification signal — treat point estimates as test "
                         "hypotheses, not bankable numbers.")
        L.append("")

    L.append(f"## Stage 3 — stability ({s3.get('mode', 'n/a')}, refit = {s3.get('refit_method')}) — **{_fmt_pass(s3.get('pass'))}**\n")
    if "reason" in s3:
        L.append(f"- {s3['reason']}\n")
    else:
        if s3.get("mode") == "half_split":
            L.append(f"- Window A: {s3['window_a']} → median own **{s3['median_own_a']:+.3f}**")
            L.append(f"- Window B: {s3['window_b']} → median own **{s3['median_own_b']:+.3f}**")
        else:
            L.append(f"- Full panel median own **{s3.get('median_own_full', float('nan')):+.3f}** vs "
                     f"drop-latest-4wk **{s3.get('median_own_jackknife', float('nan')):+.3f}**")
        L.append(f"- Drift **{s3['median_own_drift']:.3f}** vs threshold ≤ {s3['drift_max']:.2f} → "
                 f"{_fmt_pass(s3['pass'])} (production matrix median: {s3['production_median_own']:+.3f})")
        if res["stage2"].get("pinned_at_prior_flag"):
            L.append("- Caveat: with most cells pinned at the prior, low drift is partly the PRIOR "
                     "being stable across windows, not evidence the data pins the elasticity.")
        L.append("")

    L.append("## What to do with this\n")
    if res["all_pass"]:
        L.append("All three stages pass — the matrix may feed the optimizer. Keep the pinned-at-prior "
                 "share in view: wide-band cells should still be moved via glide + live test, "
                 "never banked as a saving.")
    else:
        failed = [n for n, s in (("Stage 1 fit", s1), ("Stage 2 sanity", s2), ("Stage 3 stability", s3))
                  if not s.get("pass")]
        L.append(f"**{' and '.join(failed)} failed.** Do not promote this matrix as a demand "
                 "forecaster. The validated use remains directional + conservative: glide moves, "
                 "2-week watch, scale only on register receipts. Exit code 1 lets a cron retrain "
                 "refuse to auto-promote.")
    L.append("\n_Thresholds: wMAPE/R² reuse the codebase's own gates (elasticity_hier), "
             "|bias| ≤ 5% is the paper-strict bar (codebase's is 10%). Stage 3 refits with the "
             "production estimator chain (bayes → hier fallback), identical to pricing_engine._")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────
def run_validation(holdout_weeks=HOLDOUT_WEEKS, drift_max=DRIFT_MAX, out_dir=OUT_DIR):
    fact, run = _latest_fact_table()
    e_path = os.path.join(PRICING_DIR, "elasticities.csv")
    c_path = os.path.join(PRICING_DIR, "cross_price.csv")
    g_path = os.path.join(PRICING_DIR, "gates.json")
    if not os.path.exists(e_path):
        raise SystemExit(f"missing {e_path} — run pricing_engine.py first")
    elast_df = pd.read_csv(e_path)
    cross_df = pd.read_csv(c_path) if os.path.exists(c_path) else pd.DataFrame()
    gates_prod = json.load(open(g_path)) if os.path.exists(g_path) else {}

    print(f"[gates] fact_table: {os.path.basename(run)} | matrix: {len(elast_df)} cells, "
          f"{len(cross_df)} cross pairs")
    panel = pp.build_pricing_panel(fact)
    print(f"[gates] panel: {len(panel)} cell-weeks | {panel['product_id'].nunique()} SKUs | "
          f"{panel['city'].nunique()} cities | {panel['week'].nunique()} weeks")

    s1 = stage1_fit(panel, elast_df, cross_df, holdout_weeks)
    print(f"[gates] Stage 1 fit: R2 {s1.get('r2_log_holdout', 'n/a')} (need >= {R2_FLOOR_HOLDOUT}) "
          f"{_fmt_pass(s1.get('r2_pass', False))} | wMAPE {s1.get('wmape_units', 'n/a')} "
          f"(need <= {WMAPE_CEIL}) {_fmt_pass(s1.get('wmape_pass', False))} | "
          f"|bias| {s1.get('abs_bias_units', 'n/a')} (need <= {BIAS_CEIL}) "
          f"{_fmt_pass(s1.get('bias_pass', False))} -> {_fmt_pass(s1.get('pass'))}")

    s2 = stage2_sanity(elast_df, cross_df, gates_prod)
    print(f"[gates] Stage 2 sanity: own<0 share {s2.get('own_neg_share', 'n/a')} | "
          f"|own| in-band share {s2.get('own_mag_share_in_band', 'n/a')} | "
          f"cross in-band {s2.get('cross_in_band_share', 'n/a')} | "
          f"pinned-at-prior {s2.get('pinned_at_prior_share', 'n/a')} "
          f"({'FLAGGED' if s2.get('pinned_at_prior_flag') else 'ok'}) -> {_fmt_pass(s2.get('pass'))}")

    s3 = stage3_stability(panel, elast_df, drift_max)
    print(f"[gates] Stage 3 stability ({s3.get('mode', 'n/a')}): drift "
          f"{s3.get('median_own_drift', 'n/a')} (need <= {drift_max}) -> {_fmt_pass(s3.get('pass'))}"
          + (f" [{s3.get('reason')}]" if "reason" in s3 else ""))

    all_pass = bool(s1.get("pass") and s2.get("pass") and s3.get("pass"))
    res = {
        "protocol": "pricingai_3stage",
        "run": os.path.basename(run),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "inputs": {"elasticities": os.path.relpath(e_path, ROOT),
                   "cross_price": os.path.relpath(c_path, ROOT),
                   "gates_prod": os.path.relpath(g_path, ROOT),
                   "fact_table_run": os.path.basename(run)},
        "stage1": s1, "stage2": s2, "stage3": s3,
        "all_pass": all_pass,
        "verdict": "PASS" if all_pass else "FAIL",
    }
    os.makedirs(out_dir, exist_ok=True)
    j_path = os.path.join(out_dir, "elasticity_validation.json")
    m_path = os.path.join(out_dir, "ELASTICITY_GATES.md")
    json.dump(res, open(j_path, "w"), indent=2, default=str)
    _write_markdown(res, m_path)
    print(f"[gates] wrote {j_path}")
    print(f"[gates] wrote {m_path}")
    print(f"[gates] OVERALL: {res['verdict']}"
          + ("" if all_pass else " — matrix NOT cleared to bank savings"))
    return res


# ─────────────────────────────────────────────────────────────────────────────
# Self-check on a planted synthetic panel (elasticity_hier._synth_panel)
# ─────────────────────────────────────────────────────────────────────────────
def _selfcheck():
    # elasticity_hier._synth_panel uses hash(str) internally, which Python randomizes
    # per process — re-exec with PYTHONHASHSEED=0 so the planted panel (and therefore
    # every selfcheck number) is bit-reproducible run to run.
    if os.environ.get("PYTHONHASHSEED") != "0":
        import subprocess
        rc = subprocess.call([sys.executable, "-X", "utf8", os.path.abspath(__file__),
                              "--selfcheck"], env=dict(os.environ, PYTHONHASHSEED="0"))
        sys.exit(rc)
    import elasticity_hier as ehier
    panel = ehier._synth_panel(seed=0)
    elast_df, cross_df, _, gates = est_mod.estimate_elasticities(panel)
    s1 = stage1_fit(panel, elast_df, cross_df, holdout_weeks=4)
    s2 = stage2_sanity(elast_df, cross_df, gates)
    s3 = stage3_stability(panel, elast_df)
    print(f"[selfcheck] planted matrix ({EST_METHOD}): "
          f"stage1 {_fmt_pass(s1.get('pass'))} (R2 {s1.get('r2_log_holdout')}, "
          f"wMAPE {s1.get('wmape_units')}, |bias| {s1.get('abs_bias_units')}) | "
          f"stage2 {_fmt_pass(s2.get('pass'))} | "
          f"stage3 {_fmt_pass(s3.get('pass'))} (drift {s3.get('median_own_drift')})")
    assert s2["pass"], "planted matrix must PASS stage 2 sanity"
    assert s3.get("pass"), "planted matrix must PASS stage 3 stability"
    # corrupted matrix: flip own signs -> stage 2 must reject
    bad = elast_df.copy()
    bad["own_elast"] = -bad["own_elast"]
    s2_bad = stage2_sanity(bad, cross_df, gates)
    print(f"[selfcheck] corrupted matrix (own*=-1): stage2 {_fmt_pass(s2_bad['pass'])} "
          f"(own<0 share {s2_bad['own_neg_share']})")
    assert not s2_bad["pass"], "sign-flipped matrix must FAIL stage 2 — gate is live"
    print("[selfcheck] OK — gate accepts a sane planted matrix and rejects a corrupted one")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="3-stage elasticity validation gates (val_02)")
    ap.add_argument("--holdout-weeks", type=int, default=HOLDOUT_WEEKS,
                    help="holdout window for stage 1 (default 4)")
    ap.add_argument("--drift-max", type=float, default=DRIFT_MAX,
                    help="stage 3 median-own drift threshold (default 0.30)")
    ap.add_argument("--selfcheck", action="store_true",
                    help="run the planted-synthetic self-test and exit")
    ap.add_argument("--report-only", action="store_true",
                    help="write reports but always exit 0 (human report mode; "
                         "default is the hard cron gate: exit 1 on FAIL)")
    args = ap.parse_args()
    if args.selfcheck:
        _selfcheck()
        sys.exit(0)
    result = run_validation(args.holdout_weeks, args.drift_max)
    code = 0 if (result["all_pass"] or args.report_only) else 1
    if not result["all_pass"]:
        print(f"[gates] exit {code}" + (" (report-only mode)" if args.report_only
                                        else " — cron retrain must NOT auto-promote"))
    sys.exit(code)
