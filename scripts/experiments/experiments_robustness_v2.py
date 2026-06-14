"""
experiments_robustness_v2.py — Two more experiments after E1 emerged as winner.

Findings from v1:
  - E1 (OLS + lag/DOW/momentum features) beats every richer model on within-cell
    test R² (median -0.04 vs -0.31 for LightGBM).
  - But NO model hits per-cell R² >= 0.70 because the test window is only ~22
    days per cell — daily within-cell variance is mostly noise.

This script tests two reframes:

  E6 — WEEKLY AGGREGATION
      Aggregate cell-day to cell-week (mean discount, mean log_price, mean log_units).
      Weekly is the cadence the dashboard / pricing decisions actually use.
      Daily noise averages out; per-cell R² should jump.

  E7 — WALK-FORWARD 4-FOLD CV
      Replace single 80/20 holdout with 4 sequential folds. For each cell,
      report the median R² across folds + the stability (std of fold R²s).
      A cell is "actionable" if median fold-R² >= 0.7 AND stability is good.
"""
import os
import sys
import warnings
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
import statsmodels.api as sm

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

import v4_config as cfg
from stage1_ingestion.ingest import ingest_all_sales, load_event_calendar
from stage2_preparation.prepare import prepare_fact_table
from stage3_features.features import engineer_features

COL = cfg.COL


def _r2(y, p):
    y = np.asarray(y, dtype=float); p = np.asarray(p, dtype=float)
    m = np.isfinite(y) & np.isfinite(p)
    if m.sum() < 2: return np.nan
    ss_res = ((y[m] - p[m]) ** 2).sum()
    ss_tot = ((y[m] - y[m].mean()) ** 2).sum()
    if ss_tot <= 0: return np.nan
    return 1 - ss_res / ss_tot


def build_daily_dataset():
    raw  = ingest_all_sales()
    cal  = load_event_calendar()
    fact = prepare_fact_table(raw, cal)
    feat = engineer_features(fact)
    df = feat[feat["is_regular_day"] == 1].copy()
    max_date = pd.to_datetime(df[COL["date"]]).max()
    df = df[pd.to_datetime(df[COL["date"]]) >= max_date - pd.Timedelta(days=180)].copy()
    df["sku_city"] = (df[COL["product_id"]].astype(str) + "__"
                      + df[COL["grammage"]].astype(str) + "__"
                      + df[COL["city"]].astype(str))
    df = df.sort_values(["sku_city", COL["date"]]).reset_index(drop=True)
    # lag features
    df["lag1_log_units"]  = df.groupby("sku_city")["log_units"].shift(1)
    df["lag7_log_units"]  = df.groupby("sku_city")["log_units"].shift(7)
    df["lag1_log_price"]  = df.groupby("sku_city")["log_price"].shift(1)
    df["lag1_discount"]   = df.groupby("sku_city")["discount_pct"].shift(1)
    df["rolling_mean_7d_log_units"]  = (df.groupby("sku_city")["log_units"]
                                          .transform(lambda s: s.shift(1).rolling(7,  min_periods=2).mean()))
    df["rolling_mean_14d_log_units"] = (df.groupby("sku_city")["log_units"]
                                          .transform(lambda s: s.shift(1).rolling(14, min_periods=3).mean()))
    for d in range(1, 7):
        df[f"dow_{d}"] = (df["day_of_week"] == d).astype(int)
    df = df.dropna(subset=["lag1_log_units", "rolling_mean_7d_log_units"]).reset_index(drop=True)
    return df


# ─────────────────────────────────────────────────────────────────────
#  Winning model (E1) packaged as a reusable predictor
# ─────────────────────────────────────────────────────────────────────

def _decorr_badge(g):
    d  = g["discount_pct"].values.astype(float)
    lp = g["log_price"].values.astype(float)
    if len(g) < 5 or np.std(lp) < 1e-6:
        return pd.Series(d - d.mean(), index=g.index)
    X = np.column_stack([np.ones(len(g)), lp])
    coef = np.linalg.lstsq(X, d, rcond=None)[0]
    return pd.Series(d - X @ coef, index=g.index)


def fit_predict_e1(train, test, use_weekly=False):
    train = train.reset_index(drop=True).copy()
    test  = test .reset_index(drop=True).copy()
    train["badge_resid"] = train.groupby("sku_city", group_keys=False).apply(_decorr_badge)
    test ["badge_resid"] = test .groupby("sku_city", group_keys=False).apply(_decorr_badge)

    month_cols = [f"month_{m}" for m in range(2, 13) if f"month_{m}" in train.columns]
    dow_cols   = [f"dow_{d}"   for d in range(1, 7)  if f"dow_{d}"   in train.columns]
    formula = (
        "log_units ~ C(sku_city) + log_price + badge_resid + "
        "osa_rolling_7d + log_ad_sov + rpi + is_weekend + "
        "lag1_log_units + lag7_log_units + rolling_mean_7d_log_units + "
        "rolling_mean_14d_log_units + lag1_log_price + lag1_discount"
        + (" + " + " + ".join(month_cols) if month_cols else "")
        + (" + " + " + ".join(dow_cols)   if dow_cols   else "")
    )
    # In weekly mode, DOW dummies & is_weekend lose meaning
    if use_weekly:
        formula = (
            "log_units ~ C(sku_city) + log_price + badge_resid + "
            "osa_rolling_7d + log_ad_sov + rpi + "
            "lag1_log_units + rolling_mean_7d_log_units + lag1_log_price + lag1_discount"
            + (" + " + " + ".join(month_cols) if month_cols else "")
        )

    pred_te = np.full(len(test), np.nan, dtype=float)
    for cat, sub_tr in train.groupby("category"):
        if len(sub_tr) < (40 if use_weekly else 200): continue
        try:
            m = smf.rlm(formula, data=sub_tr, M=sm.robust.norms.HuberT()).fit()
        except Exception:
            try:
                m = smf.ols(formula, data=sub_tr).fit()
            except Exception:
                continue
        mask = (test["category"] == cat).values
        if mask.any():
            try:
                pred_te[mask] = np.asarray(m.predict(test.loc[mask]))
            except Exception:
                pass
    test["pred"] = pred_te
    return test


# ─────────────────────────────────────────────────────────────────────
#  E6 — Weekly aggregation
# ─────────────────────────────────────────────────────────────────────

def aggregate_to_weekly(daily):
    """Roll daily to ISO-week × cell."""
    d = daily.copy()
    d["week_start"] = pd.to_datetime(d[COL["date"]]) - pd.to_timedelta(
        pd.to_datetime(d[COL["date"]]).dt.weekday, unit="D"
    )
    agg = (
        d.groupby([COL["product_id"], COL["grammage"], COL["city"], "sku_city",
                   "category", "week_start"])
         .agg(
            log_units              =("log_units",                  "mean"),
            log_price              =("log_price",                  "mean"),
            discount_pct           =("discount_pct",               "mean"),
            osa_rolling_7d         =("osa_rolling_7d",             "mean"),
            log_ad_sov             =("log_ad_sov",                 "mean"),
            rpi                    =("rpi",                        "mean"),
            lag1_log_units         =("lag1_log_units",             "mean"),
            rolling_mean_7d_log_units=("rolling_mean_7d_log_units","mean"),
            lag1_log_price         =("lag1_log_price",             "mean"),
            lag1_discount          =("lag1_discount",              "mean"),
            month                  =("month",                      "first"),
            n_days                 =("log_units",                  "size"),
         ).reset_index()
    )
    # Re-add month dummies
    for m in range(2, 13):
        agg[f"month_{m}"] = (agg["month"] == m).astype(int)
    # Require at least 4 days in the week to keep it (avoid partial-week noise)
    agg = agg[agg["n_days"] >= 4].reset_index(drop=True)
    # Re-create weekly lags (shifted by 1 week) instead of daily lags
    agg = agg.sort_values(["sku_city", "week_start"])
    agg["lag1_log_units"] = agg.groupby("sku_city")["log_units"].shift(1)
    agg["rolling_mean_7d_log_units"] = (
        agg.groupby("sku_city")["log_units"]
           .transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean())
    )
    agg["lag1_log_price"] = agg.groupby("sku_city")["log_price"].shift(1)
    agg["lag1_discount"]  = agg.groupby("sku_city")["discount_pct"].shift(1)
    agg = agg.dropna(subset=["lag1_log_units"]).reset_index(drop=True)
    return agg


def run_e6_weekly(daily):
    print("\n>>> Running E6_weekly_aggregation")
    wk = aggregate_to_weekly(daily)
    weeks_sorted = sorted(wk["week_start"].unique())
    split_idx = int(len(weeks_sorted) * 0.8)
    split = weeks_sorted[split_idx]
    train = wk[wk["week_start"] <= split].copy()
    test  = wk[wk["week_start"] >  split].copy()
    seen  = set(train["sku_city"].unique())
    test  = test[test["sku_city"].isin(seen)].copy()
    print(f"  Weekly dataset: {len(wk)} rows | train={len(train)} test={len(test)} | "
          f"{wk['sku_city'].nunique()} cells")
    te = fit_predict_e1(train, test, use_weekly=True)
    return te


# ─────────────────────────────────────────────────────────────────────
#  E7 — Walk-forward 4-fold CV
# ─────────────────────────────────────────────────────────────────────

def run_e7_walkforward(daily, n_folds=4):
    print(f"\n>>> Running E7_walkforward_{n_folds}fold")
    df = daily.copy()
    dates_sorted = sorted(df[COL["date"]].unique())
    # Each fold's test = next 20% of dates after the cumulative train portion
    fold_results = []
    for i in range(n_folds):
        # train = first (50 + 10*i)%, test = next 12.5%
        train_end_idx = int(len(dates_sorted) * (0.5 + 0.1 * i))
        test_end_idx  = int(len(dates_sorted) * (0.625 + 0.1 * i))
        train_end = dates_sorted[train_end_idx]
        test_end  = dates_sorted[min(test_end_idx, len(dates_sorted) - 1)]
        train = df[df[COL["date"]] <= train_end].copy()
        test  = df[(df[COL["date"]] >  train_end) & (df[COL["date"]] <= test_end)].copy()
        seen  = set(train["sku_city"].unique())
        test  = test[test["sku_city"].isin(seen)].copy()
        if len(test) == 0: continue
        te = fit_predict_e1(train, test, use_weekly=False)
        te["fold"] = i + 1
        fold_results.append(te)
        print(f"  Fold {i+1}: train ends {pd.to_datetime(train_end).date()} "
              f"({len(train)} rows), test ends {pd.to_datetime(test_end).date()} ({len(test)} rows)")
    if not fold_results: return None
    return pd.concat(fold_results, ignore_index=True)


# ─────────────────────────────────────────────────────────────────────
#  Reporting
# ─────────────────────────────────────────────────────────────────────

def summarize(te, name, key_cols, prod_keys, city_key):
    cell_rows = []
    for key, gdf in te.groupby(key_cols):
        rec = dict(zip(key_cols, key if isinstance(key, tuple) else (key,)))
        rec["n_test"]  = len(gdf)
        rec["test_r2"] = _r2(gdf["log_units"].values, gdf["pred"].values)
        cell_rows.append(rec)
    cell_df = pd.DataFrame(cell_rows); cell_df["model"] = name

    prod_rows = []
    for key, gdf in te.groupby(prod_keys):
        rec = dict(zip(prod_keys, key if isinstance(key, tuple) else (key,)))
        rec["model"] = name
        rec["test_r2_pooled"] = _r2(gdf["log_units"].values, gdf["pred"].values)
        prod_rows.append(rec)
    prod_df = pd.DataFrame(prod_rows)

    city_rows = []
    for city, gdf in te.groupby(city_key):
        rec = {"city": city, "model": name,
               "test_r2_pooled": _r2(gdf["log_units"].values, gdf["pred"].values)}
        city_rows.append(rec)
    city_df = pd.DataFrame(city_rows)

    r2 = cell_df["test_r2"].dropna()
    print("─" * 70)
    print(f"  {name}")
    print("─" * 70)
    print(f"  pooled log-R²:         {_r2(te['log_units'].values, te['pred'].values):+.3f}")
    if len(r2):
        print(f"  within-cell R² median: {r2.median():+.3f}  (mean {r2.mean():+.3f})")
        print(f"  per-cell distribution: min={r2.min():+.2f}  p25={r2.quantile(.25):+.2f}  "
              f"p50={r2.median():+.2f}  p75={r2.quantile(.75):+.2f}  max={r2.max():+.2f}")
        print(f"  cells >= 0.70: {(r2>=0.7).sum()}/{len(r2)}   >= 0.50: {(r2>=0.5).sum()}/{len(r2)}")
    print(f"  products meeting 0.70 (pooled): {(prod_df['test_r2_pooled']>=0.7).sum()}/{len(prod_df)}")
    print(f"  cities   meeting 0.70 (pooled): {(city_df['test_r2_pooled']>=0.7).sum()}/{len(city_df)}")
    return cell_df, prod_df, city_df


def main():
    daily = build_daily_dataset()
    print(f"\nDaily dataset: {len(daily):,} rows across {daily['sku_city'].nunique()} cells")
    key_cols  = [COL["product_id"], COL["grammage"], COL["city"]]
    prod_keys = [COL["product_id"], COL["grammage"]]
    city_key  = COL["city"]

    out_dir = os.path.join(cfg.OUTPUT_DIR, "_diagnostics")
    os.makedirs(out_dir, exist_ok=True)

    # ── E6 weekly
    te6 = run_e6_weekly(daily)
    c6, p6, ci6 = summarize(te6, "E6_weekly", key_cols, prod_keys, city_key)
    c6.to_csv (os.path.join(out_dir, "e6_per_cell.csv"),    index=False)
    p6.to_csv (os.path.join(out_dir, "e6_per_product.csv"), index=False)
    ci6.to_csv(os.path.join(out_dir, "e6_per_city.csv"),    index=False)

    # ── E7 walk-forward CV
    te7 = run_e7_walkforward(daily, n_folds=4)
    if te7 is not None:
        # overall summary (pool across folds)
        c7, p7, ci7 = summarize(te7, "E7_walkforward", key_cols, prod_keys, city_key)
        c7.to_csv (os.path.join(out_dir, "e7_per_cell.csv"),    index=False)
        p7.to_csv (os.path.join(out_dir, "e7_per_product.csv"), index=False)
        ci7.to_csv(os.path.join(out_dir, "e7_per_city.csv"),    index=False)

        # per-cell stability across folds
        rows = []
        for key, gdf in te7.groupby(key_cols + ["fold"]):
            rec = dict(zip(key_cols + ["fold"], key))
            rec["fold_r2"] = _r2(gdf["log_units"].values, gdf["pred"].values)
            rec["n_test"]  = len(gdf)
            rows.append(rec)
        fold_df = pd.DataFrame(rows)
        stability = (fold_df.groupby(key_cols)
                            .agg(median_r2=("fold_r2", "median"),
                                 mean_r2  =("fold_r2", "mean"),
                                 std_r2   =("fold_r2", "std"),
                                 min_r2   =("fold_r2", "min"),
                                 max_r2   =("fold_r2", "max"),
                                 n_folds  =("fold_r2", "count"))
                            .reset_index())
        stability["actionable_strict"]   = (stability["median_r2"] >= 0.70).astype(int)
        stability["actionable_moderate"] = (stability["median_r2"] >= 0.50).astype(int)
        stability.to_csv(os.path.join(out_dir, "e7_stability_per_cell.csv"), index=False)
        print("\n─── E7 stability across folds ───")
        print(f"  cells with median fold-R² >= 0.70: "
              f"{int(stability['actionable_strict'].sum())}/{len(stability)}")
        print(f"  cells with median fold-R² >= 0.50: "
              f"{int(stability['actionable_moderate'].sum())}/{len(stability)}")
        print(f"  median stability (std of fold-R²): {stability['std_r2'].median():.3f}")

    print(f"\nFiles written to: {out_dir}")


if __name__ == "__main__":
    main()
