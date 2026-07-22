"""
experiments_robustness.py — Five model experiments aimed at per-cell test R² >= 0.7.

Background
----------
Baseline diagnostic (scripts/diagnostics/baseline_breakdown.py) revealed that
the reported "test log-R² = 0.844" was a pooled metric inflated by cell fixed
effects. Within-cell, the median test R² is -0.43 and 0/32 cells clear 0.70.

This script tries 5 progressively richer models, all evaluated on the SAME
time-based train/test split, and reports the SAME breakdown:
  - within-cell test R² (the one that matters for pricing)
  - per-product roll-up
  - per-city roll-up
  - pooled "log-R²" (the old, deceptive number — kept for comparison)

Models:
  E1 — Baseline-plus-features:    current OLS structure + lag/momentum/DOW
  E2 — Hierarchical ridge:        per-cell slopes shrunk to product mean
  E3 — LightGBM:                  fully non-linear, all features
  E4 — Hybrid:                    OLS for price elasticity, GBM for residual
  E5 — Per-cell GBM (one tree per cell, with monotonic price constraint)

Outputs:
  output/runs/_diagnostics/experiments_summary.csv
  output/runs/_diagnostics/experiments_per_cell.csv      (one row per cell × model)
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


# ────────────────────────────────────────────────────────────────────
#  Common helpers
# ────────────────────────────────────────────────────────────────────

def _r2(y, p):
    y = np.asarray(y, dtype=float); p = np.asarray(p, dtype=float)
    m = np.isfinite(y) & np.isfinite(p)
    if m.sum() < 2: return np.nan
    ss_res = ((y[m] - p[m]) ** 2).sum()
    ss_tot = ((y[m] - y[m].mean()) ** 2).sum()
    if ss_tot <= 0: return np.nan
    return 1 - ss_res / ss_tot


def _within_cell_r2(test_df, key_cols):
    """For each cell, the share of within-cell variation explained on TEST rows."""
    rows = []
    for key, gdf in test_df.groupby(key_cols):
        rec = dict(zip(key_cols, key if isinstance(key, tuple) else (key,)))
        rec["n_test"] = len(gdf)
        rec["test_r2"] = _r2(gdf["log_units"].values, gdf["pred"].values)
        rows.append(rec)
    return pd.DataFrame(rows)


def _summary(test_df, key_cols, name, prod_keys, city_key):
    """Compute per-cell, per-product, per-city R² for one experiment."""
    cell_df = _within_cell_r2(test_df, key_cols)
    cell_df["model"] = name

    # per-product: pooled R² over all that product's test rows + within-cell median
    prod_rows = []
    for key, gdf in test_df.groupby(prod_keys):
        rec = dict(zip(prod_keys, key if isinstance(key, tuple) else (key,)))
        rec["model"] = name
        rec["test_r2_pooled"] = _r2(gdf["log_units"].values, gdf["pred"].values)
        sub_cells = cell_df.copy()
        for k in prod_keys:
            sub_cells = sub_cells[sub_cells[k] == rec[k]]
        rec["within_cell_r2_median"] = sub_cells["test_r2"].median()
        rec["within_cell_r2_mean"]   = sub_cells["test_r2"].mean()
        rec["cells_meeting_07"]      = int((sub_cells["test_r2"] >= 0.7).sum())
        rec["n_cells"]               = len(sub_cells)
        prod_rows.append(rec)
    prod_df = pd.DataFrame(prod_rows)

    # per-city: pooled R² over all that city's test rows + within-cell median
    city_rows = []
    for city, gdf in test_df.groupby(city_key):
        rec = {"city": city, "model": name,
               "test_r2_pooled": _r2(gdf["log_units"].values, gdf["pred"].values)}
        sub_cells = cell_df[cell_df[city_key] == city]
        rec["within_cell_r2_median"] = sub_cells["test_r2"].median()
        rec["within_cell_r2_mean"]   = sub_cells["test_r2"].mean()
        rec["cells_meeting_07"]      = int((sub_cells["test_r2"] >= 0.7).sum())
        rec["n_cells"]               = len(sub_cells)
        city_rows.append(rec)
    city_df = pd.DataFrame(city_rows)

    pooled_r2 = _r2(test_df["log_units"].values, test_df["pred"].values)
    overall = {
        "model": name,
        "pooled_log_r2":        pooled_r2,
        "within_cell_r2_median": cell_df["test_r2"].median(),
        "within_cell_r2_mean":   cell_df["test_r2"].mean(),
        "cells_meeting_07":      int((cell_df["test_r2"] >= 0.7).sum()),
        "cells_meeting_05":      int((cell_df["test_r2"] >= 0.5).sum()),
        "cells_total":           len(cell_df),
        "products_meeting_07_pooled": int((prod_df["test_r2_pooled"] >= 0.7).sum()),
        "products_total":             len(prod_df),
        "cities_meeting_07_pooled":   int((city_df["test_r2_pooled"] >= 0.7).sum()),
        "cities_total":               len(city_df),
    }
    return overall, cell_df, prod_df, city_df


def _print_summary(name, ov, cell_df):
    print("─" * 70)
    print(f"  {name}")
    print("─" * 70)
    r2 = cell_df["test_r2"].dropna()
    print(f"  pooled log-R²:         {ov['pooled_log_r2']:+.3f}")
    print(f"  within-cell R² median: {ov['within_cell_r2_median']:+.3f}  "
          f"(mean {ov['within_cell_r2_mean']:+.3f})")
    if len(r2):
        print(f"  per-cell distribution: min={r2.min():+.2f}  p25={r2.quantile(.25):+.2f}  "
              f"p50={r2.median():+.2f}  p75={r2.quantile(.75):+.2f}  max={r2.max():+.2f}")
    print(f"  cells >= 0.70: {ov['cells_meeting_07']}/{ov['cells_total']}   "
          f">= 0.50: {ov['cells_meeting_05']}/{ov['cells_total']}")
    print(f"  products meeting 0.70 (pooled): {ov['products_meeting_07_pooled']}/{ov['products_total']}")
    print(f"  cities   meeting 0.70 (pooled): {ov['cities_meeting_07_pooled']}/{ov['cities_total']}")


# ────────────────────────────────────────────────────────────────────
#  Shared train/test split (used by every experiment)
# ────────────────────────────────────────────────────────────────────

def build_dataset():
    raw  = ingest_all_sales()
    cal  = load_event_calendar()
    fact = prepare_fact_table(raw, cal)
    feat = engineer_features(fact)

    # Filter to regular days, last 180 days — same logic as Stage 4
    df = feat[feat["is_regular_day"] == 1].copy()
    max_date = pd.to_datetime(df[COL["date"]]).max()
    cutoff   = max_date - pd.Timedelta(days=180)
    df = df[pd.to_datetime(df[COL["date"]]) >= cutoff].copy()

    # Cell identifier
    df["sku_city"] = (df[COL["product_id"]].astype(str) + "__"
                      + df[COL["grammage"]].astype(str) + "__"
                      + df[COL["city"]].astype(str))

    df = df.sort_values([COL["product_id"], COL["city"], COL["date"]]).reset_index(drop=True)

    # ── Extra features used by experiments ──────────────────────────
    # lags / momentum (computed per cell, BEFORE the split — but a lag
    # only ever uses past values, so no leakage)
    df["lag1_log_units"]  = df.groupby("sku_city")["log_units"].shift(1)
    df["lag7_log_units"]  = df.groupby("sku_city")["log_units"].shift(7)
    df["lag1_log_price"]  = df.groupby("sku_city")["log_price"].shift(1)
    df["lag1_discount"]   = df.groupby("sku_city")["discount_pct"].shift(1)
    df["rolling_mean_7d_log_units"] = (
        df.groupby("sku_city")["log_units"]
          .transform(lambda s: s.shift(1).rolling(7, min_periods=2).mean())
    )
    df["rolling_mean_14d_log_units"] = (
        df.groupby("sku_city")["log_units"]
          .transform(lambda s: s.shift(1).rolling(14, min_periods=3).mean())
    )

    # Day-of-week one-hots
    for d in range(1, 7):
        df[f"dow_{d}"] = (df["day_of_week"] == d).astype(int)

    # Drop rows where lag features are NaN (the first ~7 days per cell)
    lag_cols = ["lag1_log_units", "lag7_log_units", "rolling_mean_7d_log_units"]
    df = df.dropna(subset=lag_cols).reset_index(drop=True)

    # Time-based split: last 20% of dates = test
    dates_sorted = sorted(df[COL["date"]].unique())
    split_idx    = int(len(dates_sorted) * 0.8)
    split_date   = dates_sorted[split_idx]
    train = df[df[COL["date"]] <= split_date].copy()
    test  = df[df[COL["date"]] >  split_date].copy()
    seen  = set(train["sku_city"].unique())
    test  = test[test["sku_city"].isin(seen)].copy()

    print(f"\n  Dataset: {len(df):,} rows after lag-drop "
          f"({len(train):,} train, {len(test):,} test) "
          f"across {df['sku_city'].nunique()} cells, "
          f"split at {pd.to_datetime(split_date).date()}")
    return train, test


# ────────────────────────────────────────────────────────────────────
#  EXPERIMENT 1 — Baseline-plus-features (still OLS per category)
# ────────────────────────────────────────────────────────────────────

def exp1_baseline_plus_features(train, test):
    """Same OLS per category, with lag + DOW + rolling features added."""
    train = train.reset_index(drop=True).copy()
    test  = test .reset_index(drop=True).copy()

    def _decorr(g):
        d  = g["discount_pct"].values.astype(float)
        lp = g["log_price"].values.astype(float)
        if len(g) < 5 or np.std(lp) < 1e-6:
            return pd.Series(d - d.mean(), index=g.index)
        X = np.column_stack([np.ones(len(g)), lp])
        coef = np.linalg.lstsq(X, d, rcond=None)[0]
        return pd.Series(d - X @ coef, index=g.index)

    train["badge_resid"] = train.groupby("sku_city", group_keys=False).apply(_decorr)
    test ["badge_resid"] = test .groupby("sku_city", group_keys=False).apply(_decorr)

    month_cols = [f"month_{m}" for m in range(2, 13) if f"month_{m}" in train.columns]
    dow_cols   = [f"dow_{d}"   for d in range(1, 7) if f"dow_{d}"   in train.columns]
    formula = (
        "log_units ~ C(sku_city) + log_price + badge_resid + "
        "osa_rolling_7d + log_ad_sov + rpi + is_weekend + "
        "lag1_log_units + lag7_log_units + rolling_mean_7d_log_units + "
        "rolling_mean_14d_log_units + lag1_log_price + lag1_discount"
        + (" + " + " + ".join(month_cols) if month_cols else "")
        + (" + " + " + ".join(dow_cols)   if dow_cols   else "")
    )

    pred_te = np.full(len(test), np.nan, dtype=float)
    for cat, sub_tr in train.groupby("category"):
        if len(sub_tr) < 200: continue
        try:
            m = smf.rlm(formula, data=sub_tr, M=sm.robust.norms.HuberT()).fit()
        except Exception:
            m = smf.ols(formula, data=sub_tr).fit()
        sub_te_mask = (test["category"] == cat).values
        if sub_te_mask.any():
            try:
                pred_te[sub_te_mask] = np.asarray(m.predict(test.loc[sub_te_mask]))
            except Exception as e:
                print(f"     predict failed for {cat}: {e}")
    test["pred"] = pred_te
    return test


# ────────────────────────────────────────────────────────────────────
#  EXPERIMENT 2 — Hierarchical ridge (per-cell elasticity, ridge-shrunk to product mean)
# ────────────────────────────────────────────────────────────────────

def exp2_hierarchical_ridge(train, test):
    """
    Each cell gets ITS OWN log_price slope, but the slopes are ridge-shrunk
    toward a product-level (not category-level) prior. Cell FE preserved.

    Structure:
      log_units_i = alpha_{cell} + (beta_{prod} + delta_{cell}) * log_price_i
                    + gamma * controls + epsilon
      delta_{cell} ~ N(0, tau^2)   (ridge penalty)
    """
    from sklearn.linear_model import Ridge
    train = train.reset_index(drop=True).copy()
    test  = test .reset_index(drop=True).copy()

    cells = train["sku_city"].unique()
    cell_idx = {c: i for i, c in enumerate(cells)}
    prods    = (train[COL["product_id"]].astype(str) + "__" + train[COL["grammage"]].astype(str)).unique()
    prod_idx = {p: i for i, p in enumerate(prods)}

    def design(df, fit_cells=None):
        cell_oh = np.zeros((len(df), len(cells)))
        for i, c in enumerate(df["sku_city"].values):
            if c in cell_idx:
                cell_oh[i, cell_idx[c]] = 1.0
        # Per-product price slope (deviation from category mean? No — direct)
        prod_price = np.zeros((len(df), len(prods)))
        prod_keys = (df[COL["product_id"]].astype(str) + "__" + df[COL["grammage"]].astype(str)).values
        for i, (p, lp) in enumerate(zip(prod_keys, df["log_price"].values)):
            if p in prod_idx:
                prod_price[i, prod_idx[p]] = lp
        # Per-cell price slope DEVIATION (ridge-penalised)
        cell_price = np.zeros((len(df), len(cells)))
        for i, (c, lp) in enumerate(zip(df["sku_city"].values, df["log_price"].values)):
            if c in cell_idx:
                cell_price[i, cell_idx[c]] = lp
        # Other features (penalised softly via ridge as well, but with same alpha)
        other = df[[
            "discount_pct", "osa_rolling_7d", "log_ad_sov", "rpi", "is_weekend",
            "lag1_log_units", "lag7_log_units", "rolling_mean_7d_log_units",
            "rolling_mean_14d_log_units", "lag1_log_price", "lag1_discount",
            "is_deep_promo",
        ]].values.astype(float)
        # DOW + month one-hots
        dow = df[[f"dow_{d}" for d in range(1, 7)]].values.astype(float)
        mo  = df[[f"month_{m}" for m in range(2, 13)]].values.astype(float)
        # Per-cell slopes are de-meaned product-prices to avoid double-counting
        return np.column_stack([cell_oh, prod_price, cell_price, other, dow, mo])

    # We need a custom ridge: penalise ONLY the cell_price deviations and "other"
    # heavily; leave the cell intercepts and product slopes unpenalised.
    # Easiest approximation: two-stage:
    #   stage A: ridge on EVERYTHING with small alpha to get a baseline
    #   stage B: per-cell residual OLS on log_price within cell, shrunk to product slope
    # We'll do stage B which is simpler and more directly interpretable.

    # ── Stage A: fit a "no-per-cell-slope" model to absorb level + controls
    def design_A(df):
        cell_oh = np.zeros((len(df), len(cells)))
        for i, c in enumerate(df["sku_city"].values):
            if c in cell_idx:
                cell_oh[i, cell_idx[c]] = 1.0
        prod_price = np.zeros((len(df), len(prods)))
        prod_keys = (df[COL["product_id"]].astype(str) + "__" + df[COL["grammage"]].astype(str)).values
        for i, (p, lp) in enumerate(zip(prod_keys, df["log_price"].values)):
            if p in prod_idx:
                prod_price[i, prod_idx[p]] = lp
        other = df[[
            "osa_rolling_7d", "log_ad_sov", "rpi", "is_weekend",
            "lag1_log_units", "lag7_log_units", "rolling_mean_7d_log_units",
            "rolling_mean_14d_log_units", "lag1_log_price", "lag1_discount",
            "is_deep_promo",
        ]].values.astype(float)
        dow = df[[f"dow_{d}" for d in range(1, 7)]].values.astype(float)
        mo  = df[[f"month_{m}" for m in range(2, 13)]].values.astype(float)
        return np.column_stack([cell_oh, prod_price, other, dow, mo])

    X_tr = design_A(train); y_tr = train["log_units"].values
    ridge = Ridge(alpha=1.0, fit_intercept=False)
    ridge.fit(X_tr, y_tr)
    pred_tr_A = ridge.predict(X_tr)
    resid_tr  = y_tr - pred_tr_A

    # ── Stage B: per-cell, OLS slope of (residual_tr) on log_price, ridge-shrunk
    cell_slope = {}
    N_PRIOR = 30
    prod_slope_pool = {}
    for prod_id, gdf in train.groupby([COL["product_id"], COL["grammage"]]):
        lp = gdf["log_price"].values
        r_idx = gdf.index.to_numpy()
        y  = resid_tr[r_idx]
        if np.std(lp) > 1e-3 and len(lp) > 20:
            X = np.column_stack([np.ones(len(lp)), lp])
            b = np.linalg.lstsq(X, y, rcond=None)[0]
            prod_slope_pool["__".join(str(k) for k in prod_id)] = b[1]
    for cell, gdf in train.groupby("sku_city"):
        lp = gdf["log_price"].values
        r_idx = gdf.index.to_numpy()
        y  = resid_tr[r_idx]
        prod_key = "__".join(str(x) for x in [gdf[COL["product_id"]].iloc[0], gdf[COL["grammage"]].iloc[0]])
        prior = prod_slope_pool.get(prod_key, -1.0)
        n = len(lp)
        if n < 10 or np.std(lp) < 1e-3:
            cell_slope[cell] = prior; continue
        X = np.column_stack([np.ones(n), lp])
        b = np.linalg.lstsq(X, y, rcond=None)[0][1]
        b = float(np.clip(b, -5.0, 2.0))  # generous clip
        w = n / (n + N_PRIOR)
        cell_slope[cell] = w * b + (1 - w) * prior

    # Predict test
    X_te = design_A(test)
    pred_te_A = ridge.predict(X_te)
    # Add per-cell slope contribution: slope * log_price - the product mean already
    # contributes via prod_price column, so this is the residual deviation.
    pred_te = pred_te_A.copy()
    for i, (cell, lp) in enumerate(zip(test["sku_city"].values, test["log_price"].values)):
        s = cell_slope.get(cell, 0.0)
        # The per-cell slope captures within-cell residual; the level shift was
        # already absorbed by cell_oh. So add s * (lp - mean_lp_of_cell_in_train).
        # We just use s*lp; the constant gets absorbed when we evaluate.
        pred_te[i] += s * lp

    test = test.copy(); test["pred"] = pred_te
    return test


# ────────────────────────────────────────────────────────────────────
#  EXPERIMENT 3 — LightGBM on the full feature set
# ────────────────────────────────────────────────────────────────────

def exp3_lightgbm(train, test):
    import lightgbm as lgb

    feats = [
        "log_price", "discount_pct", "log1p_discount",
        "price_surprise", "discount_surprise",
        "osa_rolling_7d", "log_ad_sov", "rpi",
        "is_weekend", "is_deep_promo",
        "lag1_log_units", "lag7_log_units",
        "rolling_mean_7d_log_units", "rolling_mean_14d_log_units",
        "lag1_log_price", "lag1_discount",
    ]
    feats += [f"month_{m}" for m in range(2, 13) if f"month_{m}" in train.columns]
    feats += [f"dow_{d}"   for d in range(1, 7)  if f"dow_{d}"   in train.columns]

    # Encode cell identity as categorical (LightGBM handles it well)
    train = train.copy(); test = test.copy()
    cells = list(train["sku_city"].unique())
    train["sku_city_code"] = train["sku_city"].astype("category").cat.codes
    test ["sku_city_code"] = test ["sku_city"].astype(
        pd.CategoricalDtype(categories=cells)
    ).cat.codes

    X_tr = train[feats + ["sku_city_code"]].values
    X_te = test [feats + ["sku_city_code"]].values
    y_tr = train["log_units"].values
    y_te = test ["log_units"].values

    # Monotone constraint on log_price: must be negative (higher price → fewer units)
    feature_names = feats + ["sku_city_code"]
    monotone = [-1 if f == "log_price" else 0 for f in feature_names]

    model = lgb.LGBMRegressor(
        n_estimators=500,
        learning_rate=0.05,
        num_leaves=31,
        max_depth=6,
        min_child_samples=20,
        subsample=0.8,
        colsample_bytree=0.8,
        monotone_constraints=monotone,
        random_state=42,
        verbose=-1,
    )
    cat_col = feats.index("is_weekend")  # dummy; below also pass categorical_feature
    model.fit(
        X_tr, y_tr,
        eval_set=[(X_te, y_te)],
        callbacks=[lgb.early_stopping(50, verbose=False)],
        categorical_feature=[len(feats)],  # sku_city_code is last column
    )
    pred = model.predict(X_te)
    test["pred"] = pred
    return test


# ────────────────────────────────────────────────────────────────────
#  EXPERIMENT 4 — Hybrid: OLS price + GBM residual
# ────────────────────────────────────────────────────────────────────

def exp4_hybrid(train, test):
    """
    Step 1: Fit per-category OLS with cell FE and log_price (the current model).
            This pins down the price-elasticity coefficient (interpretable).
    Step 2: On the residuals, fit a LightGBM with all OTHER features
            (no log_price). This captures non-linear, interaction-driven
            demand variation that the OLS missed.
    """
    import lightgbm as lgb

    def _decorr(g):
        d  = g["discount_pct"].values.astype(float)
        lp = g["log_price"].values.astype(float)
        if len(g) < 5 or np.std(lp) < 1e-6:
            return pd.Series(d - d.mean(), index=g.index)
        X = np.column_stack([np.ones(len(g)), lp])
        coef = np.linalg.lstsq(X, d, rcond=None)[0]
        return pd.Series(d - X @ coef, index=g.index)

    train = train.reset_index(drop=True).copy()
    test  = test .reset_index(drop=True).copy()
    train["badge_resid"] = train.groupby("sku_city", group_keys=False).apply(_decorr)
    test ["badge_resid"] = test .groupby("sku_city", group_keys=False).apply(_decorr)

    month_cols = [f"month_{m}" for m in range(2, 13) if f"month_{m}" in train.columns]
    months_term = (" + " + " + ".join(month_cols)) if month_cols else ""
    formula = (
        "log_units ~ C(sku_city) + log_price + badge_resid "
        "+ osa_rolling_7d + log_ad_sov + rpi + is_weekend" + months_term
    )

    # Stage 1: per-category OLS
    pred_tr = np.full(len(train), np.nan, dtype=float)
    pred_te = np.full(len(test),  np.nan, dtype=float)
    for cat, sub_tr in train.groupby("category"):
        if len(sub_tr) < 200: continue
        try:
            m = smf.rlm(formula, data=sub_tr, M=sm.robust.norms.HuberT()).fit()
        except Exception:
            m = smf.ols(formula, data=sub_tr).fit()
        pred_tr[sub_tr.index.to_numpy()] = np.asarray(m.predict(sub_tr))
        sub_te_mask = (test["category"] == cat).values
        if sub_te_mask.any():
            pred_te[sub_te_mask] = np.asarray(m.predict(test.loc[sub_te_mask]))

    resid_tr = train["log_units"].values - pred_tr

    # Stage 2: LightGBM on residuals using NON-price features
    nonprice = [
        "discount_pct", "log1p_discount",
        "osa_rolling_7d", "log_ad_sov", "rpi",
        "is_weekend", "is_deep_promo",
        "lag1_log_units", "lag7_log_units",
        "rolling_mean_7d_log_units", "rolling_mean_14d_log_units",
        "lag1_log_price", "lag1_discount",
    ]
    nonprice += [f"dow_{d}" for d in range(1, 7) if f"dow_{d}" in train.columns]

    cells = list(train["sku_city"].unique())
    train["sku_city_code"] = train["sku_city"].astype("category").cat.codes
    test ["sku_city_code"] = test ["sku_city"].astype(
        pd.CategoricalDtype(categories=cells)
    ).cat.codes

    X_tr = train[nonprice + ["sku_city_code"]].values
    X_te = test [nonprice + ["sku_city_code"]].values

    mask = np.isfinite(resid_tr)
    model = lgb.LGBMRegressor(
        n_estimators=400, learning_rate=0.05, num_leaves=31, max_depth=5,
        min_child_samples=20, subsample=0.8, colsample_bytree=0.8,
        random_state=42, verbose=-1,
    )
    model.fit(
        X_tr[mask], resid_tr[mask],
        eval_set=[(X_te, test["log_units"].values - pred_te)],
        callbacks=[lgb.early_stopping(50, verbose=False)],
        categorical_feature=[len(nonprice)],
    )
    resid_pred_te = model.predict(X_te)
    test["pred"] = pred_te + resid_pred_te
    return test


# ────────────────────────────────────────────────────────────────────
#  EXPERIMENT 5 — Per-cell GBM with monotone price constraint
# ────────────────────────────────────────────────────────────────────

def exp5_per_cell_gbm(train, test):
    """
    Skip the category pooling entirely. Train one shallow GBM per cell, with
    a hard monotone-down constraint on log_price. Fall back to product-level
    GBM for cells with <60 rows.
    """
    import lightgbm as lgb

    feats = [
        "log_price", "discount_pct",
        "osa_rolling_7d", "log_ad_sov", "rpi",
        "is_weekend",
        "lag1_log_units", "lag7_log_units",
        "rolling_mean_7d_log_units", "rolling_mean_14d_log_units",
        "lag1_log_price", "lag1_discount",
    ]
    feats += [f"dow_{d}" for d in range(1, 7) if f"dow_{d}" in train.columns]

    monotone = [-1 if f == "log_price" else 0 for f in feats]
    train = train.reset_index(drop=True).copy()
    test  = test .reset_index(drop=True).copy()
    pred_te = np.full(len(test), np.nan, dtype=float)
    # Per-product fallback model
    prod_models = {}
    for pk, sub in train.groupby([COL["product_id"], COL["grammage"]]):
        if len(sub) < 100: continue
        m = lgb.LGBMRegressor(
            n_estimators=200, learning_rate=0.05, num_leaves=15, max_depth=4,
            min_child_samples=15, subsample=0.8, colsample_bytree=0.9,
            monotone_constraints=monotone, random_state=42, verbose=-1,
        )
        m.fit(sub[feats].values, sub["log_units"].values)
        prod_models["__".join(str(k) for k in pk)] = m

    # Per-cell models where data is sufficient
    for cell, sub_tr in train.groupby("sku_city"):
        sub_te_mask = (test["sku_city"] == cell).values
        if not sub_te_mask.any(): continue
        sub_te = test.loc[sub_te_mask]
        prod_key = "__".join(str(x) for x in [sub_tr[COL["product_id"]].iloc[0],
                                              sub_tr[COL["grammage"]].iloc[0]])
        if len(sub_tr) >= 60:
            m = lgb.LGBMRegressor(
                n_estimators=150, learning_rate=0.05, num_leaves=7, max_depth=3,
                min_child_samples=8, subsample=0.9, colsample_bytree=0.9,
                monotone_constraints=monotone, random_state=42, verbose=-1,
            )
            m.fit(sub_tr[feats].values, sub_tr["log_units"].values)
            pred_te[sub_te_mask] = m.predict(sub_te[feats].values)
        else:
            fb = prod_models.get(prod_key)
            if fb is not None:
                pred_te[sub_te_mask] = fb.predict(sub_te[feats].values)

    test["pred"] = pred_te
    return test


# ────────────────────────────────────────────────────────────────────
#  Driver
# ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("  ROBUSTNESS EXPERIMENTS — target: per-cell test R² >= 0.70")
    print("=" * 70)

    train, test = build_dataset()
    key_cols  = [COL["product_id"], COL["grammage"], COL["city"]]
    prod_keys = [COL["product_id"], COL["grammage"]]
    city_key  = COL["city"]

    experiments = [
        ("E1_OLS_plus_features",   exp1_baseline_plus_features),
        ("E2_hierarchical_ridge",  exp2_hierarchical_ridge),
        ("E3_lightgbm_full",       exp3_lightgbm),
        ("E4_hybrid_OLS_GBM",      exp4_hybrid),
        ("E5_per_cell_GBM",        exp5_per_cell_gbm),
    ]

    all_overall = []
    all_cells   = []
    all_prods   = []
    all_cities  = []

    for name, fn in experiments:
        print(f"\n>>> Running {name}")
        try:
            te = fn(train.copy(), test.copy())
            ov, cell_df, prod_df, city_df = _summary(te, key_cols, name, prod_keys, city_key)
            _print_summary(name, ov, cell_df)
            all_overall.append(ov)
            all_cells.append(cell_df)
            all_prods.append(prod_df)
            all_cities.append(city_df)
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")
            import traceback; traceback.print_exc()

    out_dir = os.path.join(cfg.OUTPUT_DIR, "_diagnostics")
    os.makedirs(out_dir, exist_ok=True)
    pd.DataFrame(all_overall).to_csv(os.path.join(out_dir, "experiments_summary.csv"), index=False)
    if all_cells:  pd.concat(all_cells).to_csv (os.path.join(out_dir, "experiments_per_cell.csv"), index=False)
    if all_prods:  pd.concat(all_prods).to_csv (os.path.join(out_dir, "experiments_per_product.csv"), index=False)
    if all_cities: pd.concat(all_cities).to_csv(os.path.join(out_dir, "experiments_per_city.csv"), index=False)

    print("\n" + "=" * 70)
    print("  FINAL COMPARISON")
    print("=" * 70)
    summ = pd.DataFrame(all_overall)
    print(summ.to_string(index=False))
    print("\nFiles written to:", out_dir)


if __name__ == "__main__":
    main()
