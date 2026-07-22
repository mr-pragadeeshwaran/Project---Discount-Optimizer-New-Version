"""
data_readiness_report.py — The FIRST script to run on any new brand's data.

WHY THIS EXISTS
---------------
When we onboard a new brand (or a new set of SKUs from an existing brand),
the most important question is NOT "what discount should we set?" — it is
"based on THIS specific data, how much can we actually trust the answers?".

This script runs an automated, self-contained assessment on the input data
and produces a one-page Markdown report answering, in business language:

  1. DATA DEPTH       — how much data per cell? per product? per city?
  2. PRICE VARIATION  — is there enough price movement to learn elasticity?
  3. MODEL FIT        — what accuracy does this data actually support?
  4. ACTIONABLE %     — for how many cells can we make a strong call now?
  5. WHAT'S MISSING   — which cells need a price test before we act?
  6. SCALE-UP VERDICT — green / yellow / red gate for the engagement

The output is the sellable "discovery deliverable" — given to the client
BEFORE the actual price recommendations, so they know upfront what to expect.

Usage
-----
    python scripts/diagnostics/data_readiness_report.py

Inputs are read from the same paths the production pipeline uses
(v4_config.SALES_DATA_DIR, MASTER_DATA_DIR), so swap the input_data folder
to assess a different brand and re-run.

Output
------
    v4_outputs/_readiness/DATA_READINESS_REPORT.md     (the one-page sellable artifact)
    v4_outputs/_readiness/per_cell_assessment.csv      (audit trail)
    v4_outputs/_readiness/per_product_assessment.csv
    v4_outputs/_readiness/per_city_assessment.csv
"""
import os
import sys
import warnings
import datetime as dt
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

import v4_config as cfg
from stage1_ingestion.ingest import ingest_all_sales, load_event_calendar
from stage2_preparation.prepare import prepare_fact_table
from stage3_features.features import engineer_features
from stage4_model.elasticity import train_hierarchical_model

COL = cfg.COL


# ─────────────────────────────────────────────────────────────────
# Thresholds — these define what "ready" means
# ─────────────────────────────────────────────────────────────────

DEPTH_GOOD          = 90        # n_train rows per cell for full credit
DEPTH_OK            = 45
VARIATION_GOOD      = 15        # distinct discount levels for full credit
VARIATION_OK        = 7
AGGREGATED_R2_TARGET = 0.70     # the metric that gates actionability

# Verdict thresholds (share of cells flagged HIGH/MEDIUM)
VERDICT_GREEN_PCT   = 70        # ≥70% HIGH-or-MEDIUM = green-light engagement
VERDICT_YELLOW_PCT  = 40        # 40-70% = yellow, expect partial coverage


def _r2(y, p):
    y = np.asarray(y, dtype=float); p = np.asarray(p, dtype=float)
    m = np.isfinite(y) & np.isfinite(p)
    if m.sum() < 2: return np.nan
    ss_res = ((y[m] - p[m]) ** 2).sum()
    ss_tot = ((y[m] - y[m].mean()) ** 2).sum()
    if ss_tot <= 0: return np.nan
    return 1 - ss_res / ss_tot


def _safe_pct(x, n):
    return 0.0 if not n else 100.0 * x / n


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────

def main():
    print("=" * 72)
    print("  DATA READINESS REPORT — assess what THIS data can deliver")
    print("=" * 72)

    # ── 1. Ingest + feature-engineer
    raw   = ingest_all_sales()
    cal   = load_event_calendar()
    fact  = prepare_fact_table(raw, cal)
    feat  = engineer_features(fact)
    result = train_hierarchical_model(feat)

    elast  = result["elasticities"]
    diag   = result["diagnostics"]
    train  = result["train_data"]
    test   = result["test_data"]
    models = result["model"]

    # ── 2. Predict test rows for per-cell aggregated metrics
    test = test.copy()
    test["pred_log_units"] = np.nan
    for cat, m in models.items():
        sub = test[test["category"] == cat]
        if sub.empty: continue
        test.loc[sub.index, "pred_log_units"] = np.asarray(m.predict(sub))

    # ── 3. Per-cell aggregated (3pp discount-bin) R² — the actionable metric
    def _bin_r2(gdf):
        g = gdf.dropna(subset=["pred_log_units"]).copy()
        if g.empty: return np.nan
        g["disc_bin"] = (g["discount_pct"] // 3 * 3).round().astype(int)
        g["pred_units"]   = np.exp(np.clip(g["pred_log_units"], -3, 10))
        g["actual_units"] = np.exp(g["log_units"])
        b = g.groupby("disc_bin", as_index=False).agg(
            n=("actual_units", "size"),
            actual=("actual_units", "mean"),
            pred=("pred_units",     "mean"))
        b = b[b["n"] >= 2]
        if len(b) < 2: return np.nan
        return _r2(b["actual"].values, b["pred"].values)

    has_g = COL["grammage"] in test.columns
    cell_keys = [COL["product_id"], COL["grammage"], COL["city"]] if has_g \
                else [COL["product_id"], COL["city"]]

    cell_bin_r2 = {}
    for key, gdf in test.groupby(cell_keys):
        cid = f"{key[0]}_{key[1]}_{key[2]}" if has_g else f"{key[0]}_{key[1]}"
        cell_bin_r2[cid] = _bin_r2(gdf)
    elast["aggregated_3pp_r2"] = elast["cell_id"].map(cell_bin_r2)

    # ── 4. Per-cell assessment table (single source of truth for the report)
    cells = elast.copy()
    cells["data_depth_score"]   = (cells["n_train"] / DEPTH_GOOD).clip(0, 1) * 100
    cells["variation_score"]    = (cells["n_discount_levels"] / VARIATION_GOOD).clip(0, 1) * 100
    cells["meets_3pp_target"]   = (cells["aggregated_3pp_r2"] >= AGGREGATED_R2_TARGET).astype(int)

    # ── 5. Per-product roll-up (rows × cells × pooled R² on test)
    # The elasticities table uses lowercase column names (its own schema),
    # NOT the COL[] raw-data names. Map accordingly.
    pid_col = "product_id" if "product_id" in cells.columns else COL["product_id"]
    grm_col = "grammage"   if "grammage"   in cells.columns else COL["grammage"]
    city_col_e = "city"    if "city"       in cells.columns else COL["city"]
    has_grm_e  = grm_col in cells.columns
    prod_keys = [pid_col, grm_col] if has_grm_e else [pid_col]
    prod_rows = []
    for key, gdf in cells.groupby(prod_keys):
        rec = dict(zip(prod_keys, key if isinstance(key, tuple) else (key,)))
        rec["category"] = gdf["category"].iloc[0]
        rec["n_cells"] = len(gdf)
        rec["n_train_total"] = int(gdf["n_train"].sum())
        rec["median_n_train_per_cell"] = float(gdf["n_train"].median())
        rec["median_n_discount_levels"] = float(gdf["n_discount_levels"].median())
        rec["median_aggregated_R2"] = float(gdf["aggregated_3pp_r2"].median(skipna=True))
        rec["cells_HIGH"]    = int((gdf["confidence_tier"] == "HIGH").sum())
        rec["cells_MEDIUM"]  = int((gdf["confidence_tier"] == "MEDIUM").sum())
        rec["cells_LOW"]     = int((gdf["confidence_tier"] == "LOW").sum())
        rec["cells_DONOTACT"] = int((gdf["confidence_tier"] == "DO_NOT_ACT").sum())
        rec["pct_actionable"] = round(_safe_pct(rec["cells_HIGH"] + rec["cells_MEDIUM"], rec["n_cells"]), 1)
        prod_rows.append(rec)
    prod_df = pd.DataFrame(prod_rows).sort_values("pct_actionable", ascending=False)

    # ── 6. Per-city roll-up
    city_rows = []
    for city, gdf in cells.groupby(city_col_e):
        rec = {"city": city, "n_cells": len(gdf)}
        rec["median_n_train_per_cell"] = float(gdf["n_train"].median())
        rec["median_n_discount_levels"] = float(gdf["n_discount_levels"].median())
        rec["median_aggregated_R2"] = float(gdf["aggregated_3pp_r2"].median(skipna=True))
        rec["cells_HIGH"]    = int((gdf["confidence_tier"] == "HIGH").sum())
        rec["cells_MEDIUM"]  = int((gdf["confidence_tier"] == "MEDIUM").sum())
        rec["cells_LOW"]     = int((gdf["confidence_tier"] == "LOW").sum())
        rec["cells_DONOTACT"] = int((gdf["confidence_tier"] == "DO_NOT_ACT").sum())
        rec["pct_actionable"] = round(_safe_pct(rec["cells_HIGH"] + rec["cells_MEDIUM"], rec["n_cells"]), 1)
        city_rows.append(rec)
    city_df = pd.DataFrame(city_rows).sort_values("pct_actionable", ascending=False)

    # ── 7. Overall verdict
    n_cells = len(cells)
    n_high  = int((cells["confidence_tier"] == "HIGH").sum())
    n_med   = int((cells["confidence_tier"] == "MEDIUM").sum())
    n_low   = int((cells["confidence_tier"] == "LOW").sum())
    n_dna   = int((cells["confidence_tier"] == "DO_NOT_ACT").sum())
    pct_actionable = _safe_pct(n_high + n_med, n_cells)

    if pct_actionable >= VERDICT_GREEN_PCT:
        verdict = "GREEN"
        verdict_text = (f"This data is ready for production pricing decisions. "
                        f"{n_high+n_med} of {n_cells} cells ({pct_actionable:.0f}%) "
                        f"can be acted on with high or medium confidence right now. "
                        f"The remaining {n_low+n_dna} cells should be parked behind "
                        f"a structured A/B price test before any change is made.")
    elif pct_actionable >= VERDICT_YELLOW_PCT:
        verdict = "YELLOW"
        verdict_text = (f"This data supports partial production use. "
                        f"{n_high+n_med} of {n_cells} cells ({pct_actionable:.0f}%) "
                        f"are actionable, but {n_low+n_dna} need a price-test phase first. "
                        f"Recommend running the system on the HIGH/MEDIUM cells for the first "
                        f"6-8 weeks while collecting price-test data on the LOW/DO_NOT_ACT cells.")
    else:
        verdict = "RED"
        verdict_text = (f"This data does NOT support production pricing decisions yet. "
                        f"Only {n_high+n_med} of {n_cells} cells ({pct_actionable:.0f}%) "
                        f"clear the confidence bar. The right next step is a structured "
                        f"price-test programme to gather more price variation per cell "
                        f"before activating the pricing engine.")

    # ── 8. Identify gaps (what to fix)
    thin_cells = cells[cells["n_train"] < DEPTH_OK].copy()
    low_variation = cells[cells["n_discount_levels"] < VARIATION_OK].copy()
    bad_fit = cells[cells["cell_train_r2"] < 0.3].copy()

    # ── 9. Compose the Markdown report
    today = dt.date.today().isoformat()
    md = []
    md.append(f"# Data Readiness Report")
    md.append(f"")
    md.append(f"_Generated: {today}_  _Brand: {cfg.BRAND_NAME}_  _Platform: {cfg.PLATFORM_NAME}_")
    md.append(f"")
    md.append(f"## Verdict: **{verdict}**")
    md.append(f"")
    md.append(f"{verdict_text}")
    md.append(f"")
    md.append(f"## Numbers at a glance")
    md.append(f"")
    md.append(f"| Metric | Value |")
    md.append(f"|---|---|")
    md.append(f"| SKUs in data | {cells[pid_col].nunique()} |")
    md.append(f"| Cities in data | {cells[city_col_e].nunique()} |")
    md.append(f"| Cells (SKU × city) | {n_cells} |")
    md.append(f"| Categories | {cells['category'].nunique()} ({', '.join(sorted(cells['category'].unique()))}) |")
    md.append(f"| Days of history used | {cfg.TRAIN_LOOKBACK_DAYS} (production lookback) |")
    md.append(f"| Train rows / cell (median) | {int(cells['n_train'].median())} |")
    md.append(f"| Distinct discount levels / cell (median) | {int(cells['n_discount_levels'].median())} |")
    md.append(f"| **Aggregated test R²(units) at 3pp discount bin** | **{diag.get('test_r2_units_agg', 0):.3f}** |")
    md.append(f"| **MAPE at 3pp discount bin** | **{diag.get('test_mape_agg', 0):.1f}%** |")
    md.append(f"| Pooled test log-R² | {diag.get('test_r2_log', 0):.3f} |")
    md.append(f"")
    md.append(f"## Per-cell confidence breakdown")
    md.append(f"")
    md.append(f"| Tier | Cells | % | What this means for the brand team |")
    md.append(f"|---|---|---|---|")
    md.append(f"| HIGH | {n_high} | {_safe_pct(n_high, n_cells):.0f}% | Act on these cells with the recommended price moves. |")
    md.append(f"| MEDIUM | {n_med} | {_safe_pct(n_med, n_cells):.0f}% | Act, but use smaller throttled steps and review weekly. |")
    md.append(f"| LOW | {n_low} | {_safe_pct(n_low, n_cells):.0f}% | No Strong Cut. Trade-off recommendations only; manager review before move. |")
    md.append(f"| DO_NOT_ACT | {n_dna} | {_safe_pct(n_dna, n_cells):.0f}% | Locked out of automatic moves. Run a 4-week structured price test first. |")
    md.append(f"")

    md.append(f"## By product")
    md.append(f"")
    md.append(f"| Product | Cells | Median train rows | Median discount levels | Median agg R² | HIGH | MED | LOW | DNA | % Actionable |")
    md.append(f"|---|---|---|---|---|---|---|---|---|---|")
    for _, r in prod_df.iterrows():
        label = " | ".join(str(r[k]) for k in prod_keys)
        agg_r2 = f"{r['median_aggregated_R2']:+.2f}" if pd.notna(r['median_aggregated_R2']) else "n/a"
        md.append(f"| {label} | {r['n_cells']} | {r['median_n_train_per_cell']:.0f} | "
                  f"{r['median_n_discount_levels']:.0f} | {agg_r2} | {r['cells_HIGH']} | "
                  f"{r['cells_MEDIUM']} | {r['cells_LOW']} | {r['cells_DONOTACT']} | "
                  f"**{r['pct_actionable']:.0f}%** |")
    md.append(f"")

    md.append(f"## By city")
    md.append(f"")
    md.append(f"| City | Cells | Median train rows | Median discount levels | Median agg R² | HIGH | MED | LOW | DNA | % Actionable |")
    md.append(f"|---|---|---|---|---|---|---|---|---|---|")
    for _, r in city_df.iterrows():
        agg_r2 = f"{r['median_aggregated_R2']:+.2f}" if pd.notna(r['median_aggregated_R2']) else "n/a"
        md.append(f"| {r['city']} | {r['n_cells']} | {r['median_n_train_per_cell']:.0f} | "
                  f"{r['median_n_discount_levels']:.0f} | {agg_r2} | {r['cells_HIGH']} | "
                  f"{r['cells_MEDIUM']} | {r['cells_LOW']} | {r['cells_DONOTACT']} | "
                  f"**{r['pct_actionable']:.0f}%** |")
    md.append(f"")

    md.append(f"## Gap analysis — what's missing in this data")
    md.append(f"")
    md.append(f"### Thin-data cells (<{DEPTH_OK} train rows) — {len(thin_cells)} cells")
    if len(thin_cells):
        md.append(f"These cells have insufficient observations. Fix: collect more days of data before scaling, or run a 4-week structured price test.")
        md.append(f"")
        md.append(f"| Cell | Train rows | Discount levels |")
        md.append(f"|---|---|---|")
        for _, r in thin_cells.head(15).iterrows():
            label = f"{r[pid_col]} | {r.get(grm_col, '')} | {r[city_col_e]}"
            md.append(f"| {label} | {int(r['n_train'])} | {int(r['n_discount_levels'])} |")
    else:
        md.append(f"None. All cells have sufficient depth.")
    md.append(f"")

    md.append(f"### Low-variation cells (<{VARIATION_OK} distinct discount levels) — {len(low_variation)} cells")
    if len(low_variation):
        md.append(f"These cells have enough data points but the discount has not moved much. The model cannot learn elasticity without variation. Fix: vary the discount in a structured 4-week test.")
        md.append(f"")
        md.append(f"| Cell | Train rows | Discount levels |")
        md.append(f"|---|---|---|")
        for _, r in low_variation.head(15).iterrows():
            label = f"{r[pid_col]} | {r.get(grm_col, '')} | {r[city_col_e]}"
            md.append(f"| {label} | {int(r['n_train'])} | {int(r['n_discount_levels'])} |")
    else:
        md.append(f"None. All cells have adequate price variation.")
    md.append(f"")

    md.append(f"### Cells with poor model fit (train R² < 0.30) — {len(bad_fit)} cells")
    if len(bad_fit):
        md.append(f"These cells have data but the model can't fit them well. Common causes: irregular launch period, demand shocks, or shifting product positioning. Fix: clean upstream data quality issues or treat as price-test candidates.")
        md.append(f"")
        md.append(f"| Cell | Train rows | Train R² |")
        md.append(f"|---|---|---|")
        for _, r in bad_fit.head(15).iterrows():
            label = f"{r[pid_col]} | {r.get(grm_col, '')} | {r[city_col_e]}"
            md.append(f"| {label} | {int(r['n_train'])} | {float(r['cell_train_r2']):+.2f} |")
    else:
        md.append(f"None. The model fits every cell's training data acceptably.")
    md.append(f"")

    md.append(f"## How to read this report")
    md.append(f"")
    md.append(f"- **Verdict** is the top-level go/no-go for this engagement. GREEN = ship. YELLOW = ship-with-caveats. RED = stop and gather more data first.")
    md.append(f"- **% Actionable** per product / city tells you where you can move fast vs where you need to test first. Aim for ≥70% before broad rollout in that segment.")
    md.append(f"- **Aggregated 3pp-bin R²** is the metric that matches a pricing decision (averaging over within-cell daily noise). When this is ≥0.70, the model's discount→units curve can be trusted.")
    md.append(f"- **Daily within-cell R²** is intentionally NOT in this report. It is noise-limited and not a useful action gate — see `doc/legacy/MODEL_EXPERIMENTS.md` for the reasoning.")
    md.append(f"")
    md.append(f"## Next steps")
    md.append(f"")
    if verdict == "GREEN":
        md.append(f"1. Run the production pipeline (`python pipeline.py`).")
        md.append(f"2. Brand team approves the recommendations for HIGH and MEDIUM cells.")
        md.append(f"3. Spin up a 4-week price-test programme for the {n_low+n_dna} LOW/DO_NOT_ACT cells.")
        md.append(f"4. Re-run this readiness report monthly to track how the actionable share grows.")
    elif verdict == "YELLOW":
        md.append(f"1. Run the production pipeline for the {n_high+n_med} HIGH/MEDIUM cells only.")
        md.append(f"2. Start a structured 6-8 week price-test programme on the LOW/DO_NOT_ACT cells, with explicit randomised discount levels.")
        md.append(f"3. Re-run this readiness report monthly. Expect the YELLOW → GREEN transition within 2-3 months once the price-test data lands.")
    else:
        md.append(f"1. **Do NOT activate the pricing engine yet.**")
        md.append(f"2. Run a 8-12 week structured price-test programme across all cells, with the experimental design produced in step 3.")
        md.append(f"3. Use this script to design the test: cells flagged DO_NOT_ACT and LOW need price variation; the test should cycle each cell through 3-4 distinct discount levels for ≥2 weeks each.")
        md.append(f"4. Re-run this readiness report after the test concludes.")

    # ── 10. Write outputs
    out_dir = os.path.join(cfg.OUTPUT_DIR, "_readiness")
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, "DATA_READINESS_REPORT.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    cells.to_csv(os.path.join(out_dir, "per_cell_assessment.csv"), index=False)
    prod_df.to_csv(os.path.join(out_dir, "per_product_assessment.csv"), index=False)
    city_df.to_csv(os.path.join(out_dir, "per_city_assessment.csv"), index=False)

    # ── 11. Console summary
    print()
    print("=" * 72)
    print(f"  VERDICT: {verdict}")
    print("=" * 72)
    print(f"  {verdict_text}")
    print()
    print(f"  Cells:    {n_cells} total | {n_high} HIGH | {n_med} MED | {n_low} LOW | {n_dna} DNA")
    print(f"  Actionable: {pct_actionable:.0f}% (HIGH + MEDIUM)")
    print(f"  Aggregated test R²(units) at 3pp bin: {diag.get('test_r2_units_agg', 0):.3f}")
    print(f"  MAPE at 3pp bin: {diag.get('test_mape_agg', 0):.1f}%")
    print()
    print(f"  Report: {md_path}")
    print(f"  Audit CSVs in: {out_dir}")


if __name__ == "__main__":
    main()
