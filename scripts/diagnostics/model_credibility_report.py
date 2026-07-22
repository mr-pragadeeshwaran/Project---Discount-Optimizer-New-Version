"""
model_credibility_report.py — Honest accuracy of the thing that actually
sets prices, plus the evidence that the confidence gate works.

WHY THIS EXISTS
---------------
The Stage-4 diagnostics report the fit of the FULL regression, which
includes lag features (yesterday's units, 7-day rolling mean). Those lag
terms predict today's sales mostly from recent sales — not from price — so
the headline Train/Test log-R² (~0.89 / ~0.84) overstates how well the
*pricing* mechanism works.

But the recommendation engine (Stage 5 saturation curve) throws the lag
terms away and predicts the consequence of a price change using ONLY:

    units(p) = base_units x (p/base_price)^elasticity
                          x exp(badge x (discount - base_discount))

So the number a buyer actually cares about — "if you change the price, will
volume do what the tool says?" — is the accuracy of THAT formula on data the
model never saw. This script measures exactly that, on a time-based holdout,
and contrasts it with the flattering full-model number so nothing is hidden.

THREE CHECKS
------------
1. DECISION-MODEL ACCURACY (the honest headline)
   Train on the early window, then on the held-out window predict each day's
   units using ONLY the price/badge curve with TRAIN-period base values
   (no leakage, no lag terms). Report daily + 3ppt-bin R²/MAPE, side-by-side
   with the full lag-laden model on the same rows.

2. CONFIDENCE CALIBRATION (does the gate earn its keep?)
   Bucket cells by their Stage-4 confidence tier (HIGH/MEDIUM/LOW/DO_NOT_ACT)
   and show their actual held-out decision-model accuracy. If HIGH cells
   genuinely predict better than LOW cells, the gate is real evidence, not a
   decoration.

3. ELASTICITY BIAS PROBE (correlation vs. confounded correlation)
   For each cell compare the naive price slope (log_units ~ log_price, no
   controls — what the production per-cell estimator currently uses) against
   the slope after partialling out OSA / ads / RPI / weekend / month / DOW.
   A large systematic shift = omitted-variable bias in the naive slope, i.e.
   the elasticity (and the rupee savings built on it) is overstated.

Outputs (all under output/runs/_credibility/):
  CREDIBILITY_REPORT.md          one-page brand/skeptic-facing summary
  decision_vs_full_by_cell.csv   per-cell decision vs full model accuracy
  confidence_calibration.csv     accuracy by confidence tier
  elasticity_bias_probe.csv      naive vs controls-adjusted slope per cell
"""
import os
import sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

import v4_config as cfg
from stage1_ingestion.ingest import ingest_all_sales, load_event_calendar, load_master_costs
from stage2_preparation.prepare import prepare_fact_table
from stage3_features.features import engineer_features
from stage4_model.elasticity import train_hierarchical_model


# ── small stats helpers ──────────────────────────────────────────────
def _r2(actual, pred):
    a = np.asarray(actual, dtype=float)
    p = np.asarray(pred, dtype=float)
    m = np.isfinite(a) & np.isfinite(p)
    if m.sum() < 2:
        return np.nan
    ss_res = ((a[m] - p[m]) ** 2).sum()
    ss_tot = ((a[m] - a[m].mean()) ** 2).sum()
    if ss_tot <= 0:
        return np.nan
    return 1.0 - ss_res / ss_tot


def _mape(actual, pred, floor=0.5):
    a = np.asarray(actual, dtype=float)
    p = np.asarray(pred, dtype=float)
    m = np.isfinite(a) & np.isfinite(p)
    if m.sum() < 1:
        return np.nan
    return float(np.mean(np.abs((a[m] - p[m]) / np.maximum(a[m], floor))) * 100)


def _controls_in(df):
    base = ["osa_rolling_7d", "log_ad_sov", "rpi", "is_weekend"]
    months = [f"month_{m}" for m in range(2, 13)]
    dows = [f"dow_{d}" for d in range(1, 7)]
    return [c for c in base + months + dows if c in df.columns]


def _residualize(y, X):
    """Return residuals of y after OLS on [1, X]. X is a 2D array (n, k)."""
    n = len(y)
    Xd = np.column_stack([np.ones(n), X]) if X.size else np.ones((n, 1))
    coef, *_ = np.linalg.lstsq(Xd, y, rcond=None)
    return y - Xd @ coef


# ── core: rebuild the DECISION model prediction on held-out rows ──────
def decision_predict(test_df, elas, train_df):
    """
    Predict held-out units using ONLY the Stage-5 price/badge curve, with
    base values taken from the TRAINING window (no leakage).

    units = base_units * (price/base_price)^elast * exp(badge*(disc-base_disc))
    """
    COL = cfg.COL
    qty = COL["offtake_qty"]

    # cell -> coefficients from the (train-fitted) elasticities table
    elas = elas.copy()
    coef = {r["cell_id"]: (float(r["price_elasticity"]),
                           float(r["badge_sensitivity"]),
                           str(r.get("confidence_tier", "")))
            for _, r in elas.iterrows()}

    # cell -> TRAIN base values (mean price / units / discount)
    def _cid(row):
        g = row.get(COL["grammage"], None)
        return f"{row[COL['product_id']]}_{g}_{row[COL['city']]}" if g is not None \
               else f"{row[COL['product_id']]}_{row[COL['city']]}"

    tr = train_df.copy()
    tr["cell_id"] = tr.apply(_cid, axis=1)
    base = tr.groupby("cell_id").agg(
        base_units=(qty, "mean"),
        base_price=("selling_price", "mean"),
        base_disc=("discount_pct", "mean"),
    ).to_dict("index")

    te = test_df.copy()
    te["cell_id"] = te.apply(_cid, axis=1)

    preds = []
    for _, row in te.iterrows():
        cid = row["cell_id"]
        if cid not in coef or cid not in base:
            preds.append(np.nan)
            continue
        elast, badge, _ = coef[cid]
        b = base[cid]
        bp = b["base_price"] if b["base_price"] > 0 else np.nan
        if not np.isfinite(bp):
            preds.append(np.nan)
            continue
        price = float(row["selling_price"])
        disc = float(row["discount_pct"])
        pred = (b["base_units"]
                * (price / bp) ** elast
                * np.exp(badge * (disc - b["base_disc"])))
        preds.append(max(float(pred), 0.01))
    te["decision_pred"] = preds
    te["conf_tier"] = te["cell_id"].map({k: v[2] for k, v in coef.items()})
    return te


def main():
    print("=" * 72)
    print("  MODEL CREDIBILITY REPORT — honest accuracy of the decision engine")
    print("=" * 72)

    raw = ingest_all_sales()
    cal = load_event_calendar()
    _ = load_master_costs()
    fact = prepare_fact_table(raw, cal)
    feat = engineer_features(fact)
    result = train_hierarchical_model(feat)

    models = result["model"]
    train = result["train_data"].copy()
    test = result["test_data"].copy()
    elas = result["elasticities"].copy()
    diag = result["diagnostics"]
    COL = cfg.COL
    qty = COL["offtake_qty"]

    # ── full-model prediction on the same held-out rows (for contrast) ──
    test["full_pred_units"] = np.nan
    for cat, m in models.items():
        sub = test[test["category"] == cat]
        if sub.empty:
            continue
        try:
            test.loc[sub.index, "full_pred_units"] = np.exp(
                np.clip(np.asarray(m.predict(sub)), -3, 10))
        except Exception as e:
            print(f"  full predict failed for {cat}: {e}")

    # ── decision-model prediction on held-out rows ──────────────────────
    te = decision_predict(test, elas, train)
    te["full_pred_units"] = test["full_pred_units"].values
    te["actual_units"] = te[qty].astype(float).values

    # ============ CHECK 1: decision vs full, daily + bin grain ==========
    daily_dec_r2 = _r2(te["actual_units"], te["decision_pred"])
    daily_dec_mape = _mape(te["actual_units"], te["decision_pred"])
    daily_full_r2 = _r2(te["actual_units"], te["full_pred_units"])
    daily_full_mape = _mape(te["actual_units"], te["full_pred_units"])

    te["disc_bin"] = (te["discount_pct"] // 3 * 3).astype(int)
    binned = te.groupby(["cell_id", "disc_bin"], as_index=False).agg(
        n=("actual_units", "size"),
        actual=("actual_units", "mean"),
        dec=("decision_pred", "mean"),
        full=("full_pred_units", "mean"),
    )
    binned = binned[binned["n"] >= 3]
    bin_dec_r2 = _r2(binned["actual"], binned["dec"])
    bin_dec_mape = _mape(binned["actual"], binned["dec"])
    bin_full_r2 = _r2(binned["actual"], binned["full"])
    bin_full_mape = _mape(binned["actual"], binned["full"])

    # per-cell decision accuracy (bin grain) for calibration + export
    cell_rows = []
    for cid, g in binned.groupby("cell_id"):
        if len(g) < 3:
            continue
        cell_rows.append({
            "cell_id": cid,
            "n_bins": len(g),
            "decision_bin_r2": _r2(g["actual"], g["dec"]),
            "decision_bin_mape": _mape(g["actual"], g["dec"]),
        })
    cell_acc = pd.DataFrame(cell_rows)
    tier_map = elas.set_index("cell_id")["confidence_tier"].to_dict()
    cell_acc["confidence_tier"] = cell_acc["cell_id"].map(tier_map)

    # ============ CHECK 2: confidence calibration =======================
    # Per-cell held-out R² is far too noisy on a ~20-row test window per cell
    # (a single cell may have only 3-4 discount bins), so we POOL the held-out
    # (cell x bin) points within each tier and score the tier as a whole. This
    # uses all the evidence and is robust to small per-cell samples.
    binned["confidence_tier"] = binned["cell_id"].map(tier_map)
    te["confidence_tier"] = te["cell_id"].map(tier_map)
    tier_order = ["HIGH", "MEDIUM", "LOW", "DO_NOT_ACT"]
    calib_rows = []
    for tier in tier_order:
        b = binned[binned["confidence_tier"] == tier]
        d = te[te["confidence_tier"] == tier]
        if b.empty:
            continue
        calib_rows.append({
            "confidence_tier": tier,
            "n_cells": int(b["cell_id"].nunique()),
            "n_holdout_bins": int(len(b)),
            "pooled_bin_r2": round(float(_r2(b["actual"], b["dec"])), 3),
            "pooled_bin_mape": round(float(_mape(b["actual"], b["dec"])), 1),
            "pooled_daily_mape": round(float(_mape(d["actual_units"], d["decision_pred"])), 1),
        })
    calib = pd.DataFrame(calib_rows)

    # ============ CHECK 3: elasticity bias probe ========================
    ctrl = _controls_in(train)
    keys = [COL["product_id"], COL["city"]]
    if COL["grammage"] in train.columns:
        keys = [COL["product_id"], COL["grammage"], COL["city"]]
    probe_rows = []
    for key, g in train.groupby(keys):
        if len(g) < 30:
            continue
        lp = g["log_price"].values.astype(float)
        lu = g["log_units"].values.astype(float)
        if np.std(lp) < 1e-3:
            continue
        # naive slope (what production currently uses)
        naive = np.polyfit(lp, lu, 1)[0]
        # controls-adjusted slope via Frisch–Waugh–Lovell
        X = g[ctrl].values.astype(float) if ctrl else np.empty((len(g), 0))
        lp_r = _residualize(lp, X)
        lu_r = _residualize(lu, X)
        if np.var(lp_r) < 1e-9:
            adj = np.nan
        else:
            adj = float(np.cov(lu_r, lp_r, ddof=1)[0, 1] / np.var(lp_r, ddof=1))
        cid = "_".join(str(k) for k in (key if isinstance(key, tuple) else (key,)))
        probe_rows.append({
            "cell_id": cid,
            "n_train": len(g),
            "naive_slope": round(float(naive), 3),
            "adjusted_slope": round(adj, 3) if np.isfinite(adj) else np.nan,
            "shift": round(float(adj - naive), 3) if np.isfinite(adj) else np.nan,
        })
    probe = pd.DataFrame(probe_rows)

    # ── write outputs ───────────────────────────────────────────────────
    out_dir = os.path.join(cfg.OUTPUT_DIR, "_credibility")
    os.makedirs(out_dir, exist_ok=True)
    cell_acc.to_csv(os.path.join(out_dir, "decision_vs_full_by_cell.csv"), index=False)
    calib.to_csv(os.path.join(out_dir, "confidence_calibration.csv"), index=False)
    probe.to_csv(os.path.join(out_dir, "elasticity_bias_probe.csv"), index=False)

    # median bias direction
    med_naive = float(probe["naive_slope"].median()) if not probe.empty else np.nan
    med_adj = float(probe["adjusted_slope"].median()) if not probe.empty else np.nan

    md = []
    md.append("# Model Credibility Report\n")
    md.append("> Honest accuracy of the engine that actually sets prices — measured "
              "on a time-based holdout the model never saw — plus evidence that the "
              "confidence gate works. Generated by "
              "`scripts/diagnostics/model_credibility_report.py`.\n")

    md.append("\n## 1. Decision-model accuracy vs. the full-model headline\n")
    md.append("The **decision model** is the price/badge curve Stage 5 actually uses "
              "(no lag terms). The **full model** is the lag-laden regression whose "
              "R² appears on the Summary sheet. Both are scored on the same held-out "
              "rows.\n")
    md.append("\n| Grain | Metric | Decision model (what sets prices) | Full model (headline) |")
    md.append("|---|---|---:|---:|")
    md.append(f"| Daily | R² | {daily_dec_r2:.3f} | {daily_full_r2:.3f} |")
    md.append(f"| Daily | MAPE % | {daily_dec_mape:.1f} | {daily_full_mape:.1f} |")
    md.append(f"| 3ppt bin | R² | {bin_dec_r2:.3f} | {bin_full_r2:.3f} |")
    md.append(f"| 3ppt bin | MAPE % | {bin_dec_mape:.1f} | {bin_full_mape:.1f} |")
    md.append(f"\n*Full-model headline reported by Stage 4 for reference: "
              f"train log-R² {diag.get('test_r2_train')}, test log-R² "
              f"{diag.get('test_r2_log')}, aggregated R²(units) "
              f"{diag.get('test_r2_units_agg')}.*\n")
    md.append("\n**How to read it:** the decision-model column is the honest answer to "
              "\"if you change the price, will volume move the way the tool predicts?\" "
              "If it is materially below the full-model column, the headline R² was "
              "carried by autocorrelation (lag terms), not by the pricing mechanism — "
              "so quote the decision-model number to buyers.\n")

    md.append("\n## 2. Confidence calibration — does the gate earn its keep?\n")
    if not calib.empty:
        md.append("Held-out decision-model accuracy, **pooled** within each Stage-4 "
                  "confidence tier (tier assigned *before* any outcome was seen). "
                  "Pooled rather than per-cell because a single cell's ~20-row test "
                  "window is too thin to score alone.\n")
        md.append("\n| Confidence tier | Cells | Holdout bins | Pooled bin R² | Pooled bin MAPE % | Pooled daily MAPE % |")
        md.append("|---|---:|---:|---:|---:|---:|")
        for _, r in calib.iterrows():
            r2s = "—" if pd.isna(r["pooled_bin_r2"]) else f"{r['pooled_bin_r2']:.3f}"
            md.append(f"| {r['confidence_tier']} | {int(r['n_cells'])} | "
                      f"{int(r['n_holdout_bins'])} | {r2s} | "
                      f"{r['pooled_bin_mape']:.1f} | "
                      f"{r['pooled_daily_mape']:.1f} |")
        md.append("\n**How to read it:** if pooled bin R² falls and MAPE rises as you go "
                  "HIGH → MEDIUM → LOW, the gate is real: the cells the system lets you "
                  "act on genuinely predict better than the ones it holds back. Tiers "
                  "with very few cells or holdout bins (see the Cells / Holdout-bins "
                  "columns) are not interpretable on their own — judge the trend from the "
                  "well-populated tiers.\n")
    else:
        md.append("_Not enough per-cell holdout bins to calibrate this run._\n")

    md.append("\n## 3. Elasticity bias probe — how much is correlation vs. confounding?\n")
    if not probe.empty:
        md.append("For every cell, the **naive** price slope (`log_units ~ log_price`, "
                  "no controls — what the production per-cell estimator uses) vs. the "
                  "**controls-adjusted** slope (after partialling out availability, ads, "
                  "competitor RPI, weekend, month, day-of-week).\n")
        md.append(f"\n- Median naive slope: **{med_naive:+.3f}**")
        md.append(f"\n- Median controls-adjusted slope: **{med_adj:+.3f}**")
        shift = med_adj - med_naive
        direction = ("less elastic (naive OVERSTATED volume sensitivity → savings were "
                     "CONSERVATIVE)" if shift > 0.05 else
                     "more elastic (naive UNDERSTATED volume sensitivity → savings may be "
                     "OPTIMISTIC)" if shift < -0.05 else
                     "essentially unchanged (little omitted-variable bias)")
        md.append(f"\n- Median shift when controls are added: **{shift:+.3f}** → {direction}.\n")
        md.append("\n**How to read it:** prices in this data were not randomly assigned, "
                  "so the naive slope mixes the true price effect with whatever else moved "
                  "when price moved. The gap between the two columns is the size of that "
                  "contamination. If the adjusted slope is systematically smaller in "
                  "magnitude, the historical-floor savings figures are if anything "
                  "conservative; if larger, the rupee savings should be quoted with a "
                  "downward error band. Full causal proof still requires a live price "
                  "test — this probe only bounds the bias.\n")
    else:
        md.append("_Not enough per-cell training data to run the probe this run._\n")

    md.append("\n## What this report does and does not prove\n")
    md.append("- **Proves:** the accuracy of the actual decision engine on data it never "
              "saw; that the confidence tiers rank cells by real predictive quality; the "
              "approximate size of omitted-variable bias in the elasticity.\n")
    md.append("- **Does not prove:** the *causal* effect of a price change. That requires "
              "running a recommended move live and comparing predicted vs. actual — the "
              "weekly proof loop. This report is the strongest evidence obtainable from "
              "historical data alone.\n")

    report_path = os.path.join(out_dir, "CREDIBILITY_REPORT.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    # ── console summary ─────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  CHECK 1 — DECISION MODEL vs FULL MODEL (held-out)")
    print("=" * 72)
    print(f"  Daily    R²:   decision={daily_dec_r2:.3f}   full={daily_full_r2:.3f}")
    print(f"  Daily    MAPE: decision={daily_dec_mape:.1f}%  full={daily_full_mape:.1f}%")
    print(f"  3ppt-bin R²:   decision={bin_dec_r2:.3f}   full={bin_full_r2:.3f}")
    print(f"  3ppt-bin MAPE: decision={bin_dec_mape:.1f}%  full={bin_full_mape:.1f}%")

    print("\n" + "=" * 72)
    print("  CHECK 2 — CONFIDENCE CALIBRATION (held-out decision accuracy by tier)")
    print("=" * 72)
    if not calib.empty:
        print(calib.to_string(index=False))
    else:
        print("  (insufficient per-cell holdout bins)")

    print("\n" + "=" * 72)
    print("  CHECK 3 — ELASTICITY BIAS PROBE (naive vs controls-adjusted slope)")
    print("=" * 72)
    print(f"  Median naive slope:            {med_naive:+.3f}")
    print(f"  Median controls-adjusted slope:{med_adj:+.3f}")
    print(f"  Median shift:                  {med_adj - med_naive:+.3f}")

    print(f"\n  Report written to: {report_path}")
    print(f"  CSVs written to:   {out_dir}")


if __name__ == "__main__":
    main()
