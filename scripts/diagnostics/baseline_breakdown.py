"""
baseline_breakdown.py — Where is the current model weak?

Loads the data through Stages 1-3, runs the CURRENT Stage-4 model, and breaks
test log-R² down THREE ways:
  - per cell (sku x grammage x city)
  - per product (avg across cities)
  - per city (avg across products)

Also reports data density per cell (n train rows, n distinct prices, price std)
so we can see if low R² correlates with thin data.

Outputs:
  output/runs/_diagnostics/baseline_per_cell.csv
  output/runs/_diagnostics/baseline_per_product.csv
  output/runs/_diagnostics/baseline_per_city.csv
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


def _r2(y, p):
    y = np.asarray(y, dtype=float); p = np.asarray(p, dtype=float)
    m = np.isfinite(y) & np.isfinite(p)
    if m.sum() < 2: return np.nan
    ss_res = ((y[m] - p[m]) ** 2).sum()
    ss_tot = ((y[m] - y[m].mean()) ** 2).sum()
    if ss_tot <= 0: return np.nan
    return 1 - ss_res / ss_tot


def main():
    print("=" * 70)
    print("  BASELINE BREAKDOWN — where does the current model fail?")
    print("=" * 70)

    raw = ingest_all_sales()
    cal = load_event_calendar()
    _   = load_master_costs()
    fact = prepare_fact_table(raw, cal)
    feat = engineer_features(fact)
    result = train_hierarchical_model(feat)

    models = result["model"]
    train  = result["train_data"]
    test   = result["test_data"]
    COL = cfg.COL

    # Predict test rows per category
    test = test.copy()
    test["pred_log_units"] = np.nan
    for cat, m in models.items():
        sub = test[test["category"] == cat]
        if sub.empty: continue
        try:
            test.loc[sub.index, "pred_log_units"] = np.asarray(m.predict(sub))
        except Exception as e:
            print(f"  predict failed for {cat}: {e}")

    # Same for train (for the per-cell train R²)
    train = train.copy()
    train["pred_log_units"] = np.nan
    for cat, m in models.items():
        sub = train[train["category"] == cat]
        if sub.empty: continue
        train.loc[sub.index, "pred_log_units"] = np.asarray(m.predict(sub))

    # ── Per-cell breakdown ──
    has_g = COL["grammage"] in test.columns
    keys = [COL["product_id"], COL["grammage"], COL["city"]] if has_g \
           else [COL["product_id"], COL["city"]]

    # train density per cell
    train_density = train.groupby(keys).agg(
        n_train=("log_units", "size"),
        n_price_levels=("log_price", lambda s: s.round(2).nunique()),
        price_std=("log_price", "std"),
        disc_std=("discount_pct", "std"),
        n_disc_levels=("discount_pct", lambda s: s.round(0).nunique()),
        category=("category", "first"),
    ).reset_index()

    # train R² per cell
    rows = []
    for key, gdf in train.groupby(keys):
        if isinstance(key, tuple):
            row = dict(zip(keys, key))
        else:
            row = {keys[0]: key}
        row["train_r2"] = _r2(gdf["log_units"].values, gdf["pred_log_units"].values)
        row["n_train_actual"] = len(gdf)
        rows.append(row)
    train_r2_df = pd.DataFrame(rows)

    # test R² per cell
    rows = []
    for key, gdf in test.groupby(keys):
        if isinstance(key, tuple):
            row = dict(zip(keys, key))
        else:
            row = {keys[0]: key}
        row["test_r2"] = _r2(gdf["log_units"].values, gdf["pred_log_units"].values)
        row["n_test"]  = len(gdf)
        rows.append(row)
    test_r2_df = pd.DataFrame(rows)

    cell_df = train_density.merge(train_r2_df, on=keys, how="left") \
                           .merge(test_r2_df, on=keys, how="left")
    cell_df["meets_07_train"] = (cell_df["train_r2"] >= 0.7).astype(int)
    cell_df["meets_07_test"]  = (cell_df["test_r2"]  >= 0.7).astype(int)
    cell_df = cell_df.sort_values("test_r2", ascending=True, na_position="last")

    # ── Per-product roll-up ──
    # Stack train+test predictions and compute pooled R² across all that
    # product's cells.
    combined = pd.concat([train, test], ignore_index=True)
    prod_keys = [COL["product_id"], COL["grammage"]] if has_g else [COL["product_id"]]
    prod_rows = []
    for key, gdf in combined.groupby(prod_keys):
        sub_te = test[test[COL["product_id"]] == (key[0] if isinstance(key, tuple) else key)]
        if has_g and isinstance(key, tuple):
            sub_te = sub_te[sub_te[COL["grammage"]] == key[1]]
        if isinstance(key, tuple):
            row = dict(zip(prod_keys, key))
        else:
            row = {prod_keys[0]: key}
        row["n_cells"]      = gdf.groupby(keys).ngroups
        row["test_r2_pool"] = _r2(sub_te["log_units"].values, sub_te["pred_log_units"].values) \
                              if len(sub_te) else np.nan
        cell_r2s = cell_df[(cell_df[prod_keys[0]] == row[prod_keys[0]]) &
                           ((cell_df[prod_keys[1]] == row[prod_keys[1]]) if has_g else True)]
        row["test_r2_mean_of_cells"]   = cell_r2s["test_r2"].mean()
        row["test_r2_median_of_cells"] = cell_r2s["test_r2"].median()
        row["cells_meeting_07"]        = int((cell_r2s["test_r2"] >= 0.7).sum())
        prod_rows.append(row)
    prod_df = pd.DataFrame(prod_rows).sort_values("test_r2_pool", ascending=True, na_position="last")

    # ── Per-city roll-up ──
    city_rows = []
    for city, gdf in combined.groupby(COL["city"]):
        sub_te = test[test[COL["city"]] == city]
        row = {"city": city,
               "n_cells": gdf.groupby(keys).ngroups,
               "test_r2_pool": _r2(sub_te["log_units"].values, sub_te["pred_log_units"].values)
                                if len(sub_te) else np.nan}
        cell_r2s = cell_df[cell_df[COL["city"]] == city]
        row["test_r2_mean_of_cells"]   = cell_r2s["test_r2"].mean()
        row["test_r2_median_of_cells"] = cell_r2s["test_r2"].median()
        row["cells_meeting_07"]        = int((cell_r2s["test_r2"] >= 0.7).sum())
        city_rows.append(row)
    city_df = pd.DataFrame(city_rows).sort_values("test_r2_pool", ascending=True, na_position="last")

    # ── Save & print summary ──
    out_dir = os.path.join(cfg.OUTPUT_DIR, "_diagnostics")
    os.makedirs(out_dir, exist_ok=True)
    cell_df.to_csv(os.path.join(out_dir, "baseline_per_cell.csv"), index=False)
    prod_df.to_csv(os.path.join(out_dir, "baseline_per_product.csv"), index=False)
    city_df.to_csv(os.path.join(out_dir, "baseline_per_city.csv"), index=False)

    print("\n" + "=" * 70)
    print("  PER-CELL TEST log-R² DISTRIBUTION (n=%d cells with test data)" % cell_df["test_r2"].notna().sum())
    print("=" * 70)
    r2 = cell_df["test_r2"].dropna()
    if len(r2):
        print(f"  min={r2.min():.3f}  p25={r2.quantile(.25):.3f}  median={r2.median():.3f}  "
              f"p75={r2.quantile(.75):.3f}  max={r2.max():.3f}")
        print(f"  cells with test R² >= 0.70: {(r2 >= 0.7).sum()}/{len(r2)} ({(r2 >= 0.7).mean()*100:.1f}%)")
        print(f"  cells with test R² >= 0.50: {(r2 >= 0.5).sum()}/{len(r2)} ({(r2 >= 0.5).mean()*100:.1f}%)")
        print(f"  cells with test R² <  0.00: {(r2 <  0  ).sum()}/{len(r2)} ({(r2 <  0  ).mean()*100:.1f}%)")

    print("\n" + "=" * 70)
    print("  PER-PRODUCT (rolled-up test log-R² pooled across cities)")
    print("=" * 70)
    for _, r in prod_df.iterrows():
        label = " | ".join(str(r[k]) for k in prod_keys)
        flag  = "OK " if (r["test_r2_pool"] or 0) >= 0.7 else "LOW"
        print(f"  [{flag}] {label:30s}  pooled R²={r['test_r2_pool']:+.3f}  "
              f"median-cell R²={r['test_r2_median_of_cells']:+.3f}  "
              f"cells>=0.70: {r['cells_meeting_07']}/{r['n_cells']}")

    print("\n" + "=" * 70)
    print("  PER-CITY (rolled-up test log-R² pooled across products)")
    print("=" * 70)
    for _, r in city_df.iterrows():
        flag = "OK " if (r["test_r2_pool"] or 0) >= 0.7 else "LOW"
        print(f"  [{flag}] {r['city']:20s}  pooled R²={r['test_r2_pool']:+.3f}  "
              f"median-cell R²={r['test_r2_median_of_cells']:+.3f}  "
              f"cells>=0.70: {r['cells_meeting_07']}/{r['n_cells']}")

    print("\n" + "=" * 70)
    print("  WORST 10 CELLS")
    print("=" * 70)
    cols_show = keys + ["category", "n_train", "n_price_levels", "price_std",
                        "n_disc_levels", "train_r2", "test_r2", "n_test"]
    print(cell_df[cols_show].head(10).to_string(index=False))

    print("\nFiles written to:", out_dir)


if __name__ == "__main__":
    main()
