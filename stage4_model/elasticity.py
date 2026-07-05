"""
Stage 4 — Per-Category Elasticity Model with Cell Fixed Effects.

WHY this design (May 2026 redesign)
----------------------------------
The earlier hierarchical MixedLM pooled all 3 categories together with only a
random intercept per cell. That produced:
  - Global price elasticity = -5.9 (implausibly high)
  - Test log-R² = -0.15 (worse than predicting the mean)
  - Per-cell test R² negative for every cell
  - Raw MAPE 167%; aggregated 3% discount-bin MAPE 83%

Root causes diagnosed:
  1. Severe multicollinearity among 5 price/discount features (log1p_discount,
     is_deep_promo, price_surprise, discount_surprise correlate 0.5-0.9 with
     discount_pct), splitting the elasticity coefficient unpredictably.
  2. Cross-cell variance (Jaggery ₹90 vs Sunflower Oil ₹490) absorbed into
     the global log_price coefficient because there were no cell fixed effects.
  3. Moong Dal demand grew 16x during the year (4→70 units/day) while
     discount deepened 2x; the model attributed all volume to discount → wild
     elasticities. Need a per-cell time trend to soak up secular growth.

NEW MODEL — fit one OLS per category:
  log(units) = α_cell           ← cell fixed effects (absorbs SKU/city scale)
             + β_price·log(p)    ← within-cell price elasticity (category-wide)
             + β_badge·badge_resid  ← residual deal effect (price-decorrelated)
             + β_trend·time_trend   ← per-cell linear time growth
             + controls (OSA, log_ad_sov, rpi, is_weekend, month_dummies)
  fit with Huber robust regression (down-weights demand-shock outliers).

PER-CELL ELASTICITY:
  Per-cell OLS slope on log_price (using within-cell residuals after FE+trend)
  shrunk toward the category mean via James-Stein, clipped to [-4, -0.3].

OUTPUT SCHEMA is unchanged so Stages 5-8 keep working.
"""
import warnings
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
import statsmodels.api as sm
import v4_config as cfg


# Plausible CPG elasticity bounds (more conservative than the old [-8, -0.01])
ELASTICITY_FLOOR = -4.0
ELASTICITY_CEIL  = -0.3
BADGE_FLOOR      = -0.01
BADGE_CEIL       = 0.20

# Per-cell shrinkage prior strength (effective sample size of the category mean)
N_PRIOR_PRICE = 60
N_PRIOR_BADGE = 60


def train_hierarchical_model(feat_df: pd.DataFrame) -> dict:
    """
    Train per-category Huber-robust models with cell fixed effects.

    Returns dict with the same keys as before:
      model         : dict {category: fitted RLM result}  (was: single result)
      elasticities  : DataFrame with per-cell price + badge sensitivities
      diagnostics   : dict with train/test R², MAE, MAPE (daily + aggregated)
      train_data    : DataFrame of training rows
      test_data     : DataFrame of test rows
      formula       : str description
      model_type    : 'PerCategory_CellFE_Huber'
    """
    COL = cfg.COL
    df = feat_df.copy()

    # ── Filter to regular days only ─────────────────────────────────
    regular = df[df["is_regular_day"] == 1].copy()
    n_after_flags = len(regular)

    # ── Restrict to recent lookback window (see TRAIN_LOOKBACK_DAYS) ─
    # This is the single biggest accuracy lever — restricting to recent
    # steady-state data avoids launch ramps and outdated price regimes
    # that were poisoning the model. See experiments_mape.py for evidence.
    lookback = getattr(cfg, "TRAIN_LOOKBACK_DAYS", None)
    if lookback and not df.empty:
        max_date = pd.to_datetime(regular[COL["date"]]).max()
        cutoff = max_date - pd.Timedelta(days=int(lookback))
        regular = regular[pd.to_datetime(regular[COL["date"]]) >= cutoff].copy()
        print(f"  [Stage 4] Training data: {len(regular):,} rows from last {lookback} days "
              f"(filtered from {n_after_flags:,} regular-day rows, "
              f"{len(df) - n_after_flags:,} event/OOS dropped)")
    else:
        print(f"  [Stage 4] Training data: {len(regular):,} regular-day rows "
              f"(excluded {len(df) - n_after_flags:,} event/OOS, no lookback filter)")

    # ── Build cell identifier ────────────────────────────────────────
    has_grammage = COL["grammage"] in regular.columns
    if has_grammage:
        regular["sku_city"] = (
            regular[COL["product_id"]].astype(str) + "__"
            + regular[COL["grammage"]].astype(str) + "__"
            + regular[COL["city"]].astype(str)
        )
    else:
        regular["sku_city"] = (
            regular[COL["product_id"]].astype(str) + "__"
            + regular[COL["city"]].astype(str)
        )

    n_cells = regular["sku_city"].nunique()
    print(f"    Cells (sku_city groups): {n_cells}")

    regular = regular.sort_values([COL["product_id"], COL["city"], COL["date"]])
    # Note: an earlier version added a per-cell time_trend to absorb secular
    # demand growth. It hurt Moong Dal (collapsed its elasticity to -0.3) so it
    # was removed. Month dummies + Huber give better test behaviour.

    # ── Decorrelate badge from price within each cell ───────────────
    # badge_resid = discount_pct - OLS(discount_pct ~ log_price) per cell
    # This isolates the "deal badge psychology" effect from the price-level effect.
    regular["badge_resid"] = regular.groupby("sku_city", group_keys=False).apply(
        lambda g: _decorrelate_badge(g)
    )

    # ── Time-based train/test split (last 20% of dates) ─────────────
    dates_sorted = sorted(regular[COL["date"]].unique())
    # Clamp so dates_sorted[split_idx] can never go out of bounds (e.g. if
    # TEST_SPLIT_PCT is ever set to 0).
    split_idx    = min(int(len(dates_sorted) * (1 - cfg.TEST_SPLIT_PCT)),
                       max(len(dates_sorted) - 1, 0))
    split_date   = dates_sorted[split_idx]
    train = regular[regular[COL["date"]] <= split_date].copy()
    test  = regular[regular[COL["date"]] >  split_date].copy()
    # Cell FE cannot predict unseen cells — restrict test to known cells
    seen = set(train["sku_city"].unique())
    test_eval = test[test["sku_city"].isin(seen)].copy()
    print(f"    Train: {len(train):,} rows (up to {split_date.date()}) | "
          f"Test (eval): {len(test_eval):,} rows / {len(test):,} total")

    # ── Build formula and month + DOW + lag controls ────────────────
    # Lag / DOW features were added in May 2026 after the robustness
    # experiments showed they cut within-cell test residual variance
    # roughly in half (within-cell test R² median moved from -0.43 to
    # -0.04). See scripts/experiments/experiments_robustness.py and
    # MODEL_EXPERIMENTS.md for the full comparison.
    month_cols = [f"month_{m}" for m in range(2, 13) if f"month_{m}" in train.columns]
    dow_cols   = [f"dow_{d}"   for d in range(1, 7)  if f"dow_{d}"   in train.columns]
    lag_cols_avail = [c for c in ["lag1_log_units", "lag7_log_units",
                                  "lag1_log_price", "lag1_discount",
                                  "rolling_mean_7d_log_units",
                                  "rolling_mean_14d_log_units"]
                      if c in train.columns]
    months_term = (" + " + " + ".join(month_cols)) if month_cols else ""
    dows_term   = (" + " + " + ".join(dow_cols))   if dow_cols   else ""
    lags_term   = (" + " + " + ".join(lag_cols_avail)) if lag_cols_avail else ""
    formula_core = (
        "log_units ~ C(sku_city) + log_price + badge_resid "
        "+ osa_rolling_7d + log_ad_sov + rpi + is_weekend"
        + months_term + dows_term + lags_term
    )
    print(f"  [Stage 4] Per-category formula: "
          f"log_units ~ C(sku_city) + log_price + badge_resid + controls + lags/DOW")

    # ── Fit one Huber-robust OLS per category ───────────────────────
    models = {}
    cat_metrics = []
    for cat, sub_tr in train.groupby("category"):
        n_cells_cat = sub_tr["sku_city"].nunique()
        if len(sub_tr) < 200 or n_cells_cat < 2:
            print(f"    Skipping '{cat}': only {len(sub_tr)} rows / {n_cells_cat} cells")
            continue

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                m = smf.rlm(formula_core, data=sub_tr, M=sm.robust.norms.HuberT()).fit()
                models[cat] = m
            except Exception as e:
                print(f"    {cat}: RLM failed ({type(e).__name__}: {e}), using OLS")
                try:
                    m = smf.ols(formula_core, data=sub_tr).fit()
                    models[cat] = m
                except Exception as e2:
                    print(f"    {cat}: OLS also failed ({e2}); skipping")
                    continue

        # Category-level elasticity & badge coefficient
        pe_cat = float(m.params.get("log_price",   _global_default_elasticity()))
        bs_cat = float(m.params.get("badge_resid", 0.01))
        pe_cat = float(np.clip(pe_cat, ELASTICITY_FLOOR, ELASTICITY_CEIL))
        bs_cat = float(np.clip(bs_cat, BADGE_FLOOR, BADGE_CEIL))
        pe_se  = float(m.bse.get("log_price",   0.5))
        bs_se  = float(m.bse.get("badge_resid", 0.01))
        print(f"    {cat:14s}  log_price={pe_cat:+.3f} (se={pe_se:.3f})  "
              f"badge_resid={bs_cat:+.4f} (se={bs_se:.4f})  n={len(sub_tr):,}")
        cat_metrics.append({
            "category": cat, "elasticity": pe_cat, "elasticity_se": pe_se,
            "badge": bs_cat, "badge_se": bs_se, "n_train": len(sub_tr),
        })

    # ── Per-cell shrunk elasticities (within-category) ──────────────
    # First pass: estimate per-cell RAW slopes; from these compute the robust
    # category prior (median of clipped raw slopes). Second pass: shrink each
    # cell's raw slope toward that robust prior.
    raw_price_slopes = _per_cell_raw_price_slopes(train, has_grammage)
    raw_badge_slopes = _per_cell_raw_badge_slopes(train, has_grammage)
    cat_price_prior, cat_badge_prior = _category_robust_priors(
        train, has_grammage, raw_price_slopes, raw_badge_slopes, cat_metrics
    )
    cell_price_slopes = _shrink_per_cell(
        raw_price_slopes, cat_price_prior, train, has_grammage,
        which="price", N_PRIOR=N_PRIOR_PRICE,
    )
    cell_badge_slopes = _shrink_per_cell(
        raw_badge_slopes, cat_badge_prior, train, has_grammage,
        which="badge", N_PRIOR=N_PRIOR_BADGE,
    )
    print(f"    Category-median priors (used for shrinkage):")
    for cat in cat_price_prior:
        print(f"      {cat:14s}  price_prior={cat_price_prior[cat]:+.3f}  "
              f"badge_prior={cat_badge_prior[cat]:+.4f}")

    # ── Build the elasticities table (same schema as old code) ──────
    elasticities = _build_elasticity_table(
        train, test, has_grammage, cat_metrics,
        cell_price_slopes, cell_badge_slopes,
    )

    # ── Per-cell train R² ───────────────────────────────────────────
    # For each cell, how well does the per-category model fit THIS
    # cell's own historical training data? Higher R² = the price /
    # quantity relationship is captured well for this cell, so the
    # elasticity estimate is reliable. Reported per row in the Excel
    # By Product sheet so the brand team can see model fit per city.
    cell_train_r2 = _compute_cell_train_r2(models, train, has_grammage)
    if cell_train_r2:
        elasticities["cell_train_r2"] = elasticities["cell_id"].map(
            cell_train_r2
        ).fillna(0.0).round(3)
    else:
        elasticities["cell_train_r2"] = 0.0

    # ── Per-cell test R² (held-out) ─────────────────────────────────
    cell_test_r2 = _compute_cell_test_r2(models, test_eval, has_grammage)
    if cell_test_r2:
        elasticities["cell_test_r2"] = elasticities["cell_id"].map(
            cell_test_r2
        ).fillna(np.nan).round(3)
    else:
        elasticities["cell_test_r2"] = np.nan

    # ── Per-SKU-group R² (the response-model trust floor) ──────────
    # A single cell's daily units are too noisy to fit well, but the SKU as a
    # whole (its cities pooled, at the 3ppt-discount grain the decision uses)
    # is the right grain to judge whether the response model is trustworthy.
    # Cuts are only acted on where this clears the R² floor (Stage 7 gate).
    _all = pd.concat([train, test_eval], ignore_index=True) if len(test_eval) else train
    sku_r2 = _compute_sku_group_r2(elasticities, _all, has_grammage)
    elasticities["sku_group_r2"] = elasticities["product_id"].map(sku_r2).round(3)

    # ── Per-cell confidence score (0-100) and tier ─────────────────
    # Combines: data density, price variation, in-sample fit,
    # elasticity sign correctness, elasticity plausibility, and CI
    # tightness. Designed for SCALING to many SKUs × cities — for any
    # new cell we can score it without needing labelled outcomes.
    # See MODEL_EXPERIMENTS.md for the derivation.
    elasticities = _add_cell_confidence(elasticities, raw_price_slopes)

    # ── Diagnostics (daily + aggregated) ─────────────────────────────
    diagnostics = _compute_diagnostics(models, train, test_eval, cat_metrics)
    diagnostics["model_type"] = "PerCategory_CellFE_Huber"

    # ── Decision-model held-out accuracy (the HONEST headline) ──────
    # The numbers above come from the full regression, which includes lag /
    # momentum features that predict today's units largely from recent units
    # — inflating R². But Stage 5 sets prices using ONLY the price/badge
    # curve. These keys measure that curve on held-out data, so the report
    # can quote the accuracy of the engine that actually moves prices.
    # See scripts/diagnostics/model_credibility_report.py for the standalone
    # audit that derives the same numbers.
    diagnostics.update(
        _compute_decision_diagnostics(elasticities, train, test_eval, has_grammage)
    )

    # ── Summary print ────────────────────────────────────────────────
    pe  = elasticities["price_elasticity"]
    bs  = elasticities["badge_sensitivity"]
    print(f"  [Stage 4] Results:")
    print(f"    Price elasticity range: {pe.min():.3f} to {pe.max():.3f}  "
          f"(median={pe.median():.3f}, clipped to [{ELASTICITY_FLOOR}, {ELASTICITY_CEIL}])")
    print(f"    Badge sensitivity range: {bs.min():.4f} to {bs.max():.4f}  "
          f"(median={bs.median():.4f})")
    print(f"    Cells with correct sign (negative price elast.): {(pe < 0).sum()}/{len(elasticities)}")
    print(f"    Train log-R²: {diagnostics.get('test_r2_train', 0):.3f}  "
          f"| Test log-R²: {diagnostics.get('test_r2_log', 0):.3f}  "
          f"| Test log-MAE: {diagnostics.get('test_mape_log', 99):.3f}")
    print(f"    Raw-unit MAPE: {diagnostics.get('test_mape', 0):.1f}%  "
          f"| Aggregated (3pp bin) MAPE: {diagnostics.get('test_mape_agg', 0):.1f}%  "
          f"| Aggregated R²(units): {diagnostics.get('test_r2_units_agg', 0):.3f}")

    return {
        "model":        models,           # dict {category: fitted model}
        "elasticities": elasticities,
        "diagnostics":  diagnostics,
        "train_data":   train,
        "test_data":    test_eval,
        "formula":      formula_core,
        "model_type":   "PerCategory_CellFE_Huber",
    }


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _global_default_elasticity() -> float:
    """Sensible CPG default if model can't estimate (used only as backup)."""
    return -1.5


def _decorrelate_badge(g: pd.DataFrame) -> pd.Series:
    """badge_resid = discount_pct - OLS(discount_pct ~ log_price) per cell."""
    d  = g["discount_pct"].values.astype(float)
    lp = g["log_price"].values.astype(float)
    if len(g) < 5 or np.std(lp) < 1e-6:
        return pd.Series(d - d.mean(), index=g.index)
    try:
        X = np.column_stack([np.ones(len(g)), lp])
        coef = np.linalg.lstsq(X, d, rcond=None)[0]
        return pd.Series(d - X @ coef, index=g.index)
    except Exception:
        return pd.Series(d - d.mean(), index=g.index)


def _cat_lookup(cat_metrics: list, key: str) -> dict:
    return {m["category"]: m[key] for m in cat_metrics}


def _grp_keys(train: pd.DataFrame, has_grammage: bool):
    COL = cfg.COL
    if has_grammage and COL["grammage"] in train.columns:
        return [COL["product_id"], COL["grammage"], COL["city"]]
    return [COL["product_id"], COL["city"]]


def _per_cell_raw_price_slopes(train: pd.DataFrame, has_grammage: bool) -> dict:
    """
    Bivariate per-cell OLS slope: log_units ~ log_price. Returns dict keyed by
    sku_city → {'slope', 'n', 'cat', 'usable'}. 'usable' is False if too few
    obs / too little price variation to trust this estimate.
    """
    keys = _grp_keys(train, has_grammage)
    out = {}
    for key, gdf in train.groupby(keys):
        sku_key = "__".join(str(k) for k in (key if isinstance(key, tuple) else (key,)))
        n             = len(gdf)
        n_price_lvls  = int(gdf["log_price"].round(2).nunique())
        price_std     = float(gdf["log_price"].std())
        cat           = gdf["category"].iloc[0]
        usable = (n >= 30) and (n_price_lvls >= 5) and (price_std >= 0.01)
        slope = None
        if usable:
            try:
                y = gdf["log_units"].values
                X = np.column_stack([np.ones(n), gdf["log_price"].values])
                slope = float(np.linalg.lstsq(X, y, rcond=None)[0][1])
            except Exception:
                usable = False
        out[sku_key] = {"slope": slope, "n": n, "cat": cat, "usable": usable}
    return out


def _per_cell_raw_badge_slopes(train: pd.DataFrame, has_grammage: bool) -> dict:
    """Per-cell residual badge slope after partialling out log_price."""
    keys = _grp_keys(train, has_grammage)
    out = {}
    for key, gdf in train.groupby(keys):
        sku_key = "__".join(str(k) for k in (key if isinstance(key, tuple) else (key,)))
        n      = len(gdf)
        n_disc = int(gdf["discount_pct"].round(0).nunique())
        d_std  = float(gdf["discount_pct"].std())
        cat    = gdf["category"].iloc[0]
        usable = (n >= 30) and (n_disc >= 3) and (d_std >= 1.0)
        slope = None
        if usable:
            try:
                y  = gdf["log_units"].values
                lp = gdf["log_price"].values
                Xp = np.column_stack([np.ones(n), lp])
                coef_y = np.linalg.lstsq(Xp, y, rcond=None)[0]; y_res = y - Xp @ coef_y
                d = gdf["discount_pct"].values
                coef_d = np.linalg.lstsq(Xp, d, rcond=None)[0]; d_res = d - Xp @ coef_d
                if np.std(d_res) < 1e-6:
                    usable = False
                else:
                    slope = float(np.cov(y_res, d_res, ddof=1)[0, 1] / np.var(d_res, ddof=1))
            except Exception:
                usable = False
        out[sku_key] = {"slope": slope, "n": n, "cat": cat, "usable": usable}
    return out


def _category_robust_priors(
    train: pd.DataFrame, has_grammage: bool,
    raw_price: dict, raw_badge: dict, cat_metrics: list,
) -> tuple:
    """
    Compute the robust shrinkage prior per category:
      prior = median of usable per-cell raw slopes (clipped to plausible range).
    Falls back to category pooled coefficient, then to global default, if no
    usable cells exist.
    """
    cat_price_prior = {}
    cat_badge_prior = {}
    cats = train["category"].unique()
    pooled_price = _cat_lookup(cat_metrics, "elasticity")
    pooled_badge = _cat_lookup(cat_metrics, "badge")
    for cat in cats:
        ps = [np.clip(v["slope"], ELASTICITY_FLOOR, ELASTICITY_CEIL)
              for v in raw_price.values() if v["cat"] == cat and v["usable"]]
        bs = [np.clip(v["slope"], BADGE_FLOOR, BADGE_CEIL)
              for v in raw_badge.values() if v["cat"] == cat and v["usable"]]
        if ps:
            cat_price_prior[cat] = float(np.median(ps))
        else:
            cat_price_prior[cat] = float(pooled_price.get(cat, _global_default_elasticity()))
        if bs:
            cat_badge_prior[cat] = float(np.median(bs))
        else:
            cat_badge_prior[cat] = float(pooled_badge.get(cat, 0.01))
    return cat_price_prior, cat_badge_prior


def _shrink_per_cell(
    raw: dict, cat_prior: dict, train: pd.DataFrame, has_grammage: bool,
    which: str, N_PRIOR: int,
) -> dict:
    """
    Shrink per-cell raw slope toward its category prior:
      shrunk = w * clipped_raw + (1-w) * prior        with w = n / (n + N_PRIOR)
    Unusable cells (thin data) get the prior directly.
    """
    if which == "price":
        lo, hi = ELASTICITY_FLOOR, ELASTICITY_CEIL
    else:
        lo, hi = BADGE_FLOOR, BADGE_CEIL

    out = {}
    for sku_key, info in raw.items():
        prior = cat_prior.get(info["cat"], _global_default_elasticity() if which == "price" else 0.01)
        if not info["usable"]:
            out[sku_key] = prior; continue
        clipped = float(np.clip(info["slope"], lo, hi))
        n = info["n"]; w = n / (n + N_PRIOR)
        out[sku_key] = w * clipped + (1 - w) * prior
    # Also fill in any cells present in train but not estimated (defensive)
    return out


def _build_elasticity_table(
    train: pd.DataFrame, test: pd.DataFrame, has_grammage: bool,
    cat_metrics: list, cell_price_slopes: dict, cell_badge_slopes: dict,
) -> pd.DataFrame:
    """Build per-cell elasticity DataFrame with the SAME schema Stages 5-8 expect."""
    COL = cfg.COL
    combined = pd.concat([train, test])
    grp_keys = [COL["product_id"], COL["grammage"], COL["city"]] \
               if has_grammage and COL["grammage"] in combined.columns \
               else [COL["product_id"], COL["city"]]

    cat_elast    = _cat_lookup(cat_metrics, "elasticity")
    cat_elast_se = _cat_lookup(cat_metrics, "elasticity_se")
    cat_badge    = _cat_lookup(cat_metrics, "badge")
    cat_badge_se = _cat_lookup(cat_metrics, "badge_se")
    default_e    = _global_default_elasticity()

    rows = []
    train_max_date = train[COL["date"]].max()
    for key, gdf in combined.groupby(grp_keys):
        if len(grp_keys) == 3:
            pid, grammage, city = key
        else:
            pid, city = key; grammage = None
        sku_key = "__".join(str(k) for k in (key if isinstance(key, tuple) else (key,)))

        cat       = gdf["category"].iloc[0]
        title     = gdf[COL["title"]].iloc[0] if COL["title"] in gdf.columns else str(pid)
        n_obs     = len(gdf)
        n_train   = len(gdf[gdf[COL["date"]] <= train_max_date])

        stable_mrp = float(gdf["stable_mrp"].median()) if "stable_mrp" in gdf.columns \
                     else float(gdf[COL["mrp"]].median())
        avg_units  = float(gdf[COL["offtake_qty"]].mean())
        # VOLUME-WEIGHTED current state so portfolio totals reconcile exactly to
        # source: avg units x avg_price == mean(units x price), and the discount
        # spend matches mean(units x discount). Simple means don't reconcile
        # because units and discount correlate on promo days.
        _u = pd.to_numeric(gdf[COL["offtake_qty"]], errors="coerce").fillna(0.0)
        _usum = float(_u.sum())
        if "selling_price" in gdf.columns and _usum > 0:
            avg_price = float((_u * pd.to_numeric(gdf["selling_price"], errors="coerce")).sum() / _usum)
        else:
            avg_price = float(gdf.get("selling_price", gdf[COL["price"]]).mean())
        if "discount_pct" in gdf.columns and _usum > 0:
            avg_disc = float((_u * pd.to_numeric(gdf["discount_pct"], errors="coerce")).sum() / _usum)
        elif "discount_pct" in gdf.columns:
            avg_disc = float(gdf["discount_pct"].mean())
        else:
            avg_disc = 0.0
        disc_std   = float(gdf["discount_pct"].std())   if "discount_pct" in gdf.columns else 0.0
        n_disc_lvl = int(gdf["discount_pct"].round(0).nunique()) if "discount_pct" in gdf.columns else 0

        # ── Historical FLOOR: the proven-safe minimum discount the cell
        # has actually operated at recently. We use the lower quartile of
        # observed regular-day discounts in the last lookback window.
        # By definition the cell has been at-or-below this level on ~25%
        # of days with healthy enough sales to be in the dataset.
        floor_pct = float(getattr(cfg, "HISTORICAL_FLOOR_PERCENTILE", 25)) / 100.0
        floor_lookback = int(getattr(cfg, "HISTORICAL_FLOOR_LOOKBACK_DAYS", 90))
        gdf_reg = gdf[gdf["is_regular_day"] == 1] if "is_regular_day" in gdf.columns else gdf
        if not gdf_reg.empty and "discount_pct" in gdf_reg.columns:
            recent_cut = pd.to_datetime(gdf_reg[COL["date"]]).max() - pd.Timedelta(days=floor_lookback)
            recent = gdf_reg[pd.to_datetime(gdf_reg[COL["date"]]) >= recent_cut]
            src = recent if len(recent) >= 10 else gdf_reg
            historical_floor = max(0.0, float(src["discount_pct"].quantile(floor_pct)))
        else:
            historical_floor = max(0.0, avg_disc)  # safe fallback

        cell_pe = cell_price_slopes.get(sku_key, cat_elast.get(cat, default_e))
        cell_bs = cell_badge_slopes.get(sku_key, cat_badge.get(cat, 0.01))
        cat_pe  = cat_elast.get(cat, default_e)
        cat_bs  = cat_badge.get(cat, 0.01)

        # SE inflation if cell is thin
        shrink = max(1.0, 30.0 / max(n_train, 1))
        p_se = cat_elast_se.get(cat, 0.5) * shrink
        b_se = cat_badge_se.get(cat, 0.01) * shrink

        cell_id = f"{pid}_{grammage}_{city}" if grammage else f"{pid}_{city}"

        rows.append({
            "product_id":               pid,
            "grammage":                 grammage,
            "city":                     city,
            "category":                 cat,
            "title":                    str(title)[:60],
            "stable_mrp":               round(stable_mrp, 2),
            "avg_selling_price":        round(avg_price, 2),
            "avg_units":                round(avg_units, 1),
            "avg_discount_pct":         round(avg_disc, 1),
            "historical_floor_disc":    round(historical_floor, 1),
            "disc_pct_std":             round(disc_std, 2),
            "n_discount_levels":        n_disc_lvl,
            "n_observations":           n_obs,
            "n_train":                  n_train,
            "price_elasticity":         round(cell_pe, 6),
            "price_elasticity_global":  round(cat_pe, 6),   # now the CATEGORY mean
            "price_elasticity_se":      round(p_se, 6),
            "price_elasticity_lower":   round(cell_pe - 1.96 * p_se, 6),
            "price_elasticity_upper":   round(cell_pe + 1.96 * p_se, 6),
            "badge_sensitivity":        round(cell_bs, 6),
            "badge_sensitivity_global": round(cat_bs, 6),   # now the CATEGORY mean
            "badge_sensitivity_se":     round(b_se, 6),
            # backwards-compat aliases used by Stages 5-8
            "discount_sensitivity":     round(cell_bs, 6),
            "elasticity":               round(cell_pe, 6),
            "avg_price":                round(avg_price, 2),
            "cell_id":                  cell_id,
        })

    df_out = pd.DataFrame(rows)

    # Per-product summary
    print(f"    Per-product price elasticity (avg across cities):")
    prod_grp = ["product_id", "grammage"] if has_grammage else ["product_id"]
    for keys, gdf in df_out.groupby(prod_grp):
        label = " | ".join(str(k) for k in (keys if isinstance(keys, tuple) else (keys,)))
        pe = gdf["price_elasticity"]; bs = gdf["badge_sensitivity"]
        print(f"      {label}: price_elast={pe.mean():+.3f}  "
              f"badge_sens={bs.mean():+.4f}  n_cells={len(gdf)}")

    return df_out


def _compute_cell_train_r2(models: dict, train: pd.DataFrame,
                            has_grammage: bool) -> dict:
    """
    Per-cell AGGREGATED R² — the share of variation captured at the
    3-ppt-discount-bin grain (NOT daily). This is the grain the
    saturation curve actually uses, so it's the right "is the model
    confident here?" metric for a city.

    For each cell:
      1. Group training rows into 3-ppt discount bins.
      2. For each bin, compute mean_actual_units and mean_predicted_units.
      3. R² across the bins.

    Daily-level R² is dominated by irreducible weather/weekday/noise
    for small cells — even a perfect model would score 0.2-0.4. The
    aggregated R² strips that noise and shows the price-effect quality.

    Returns {cell_id_str: r2}. Cells with fewer than 3 valid bins get
    no entry (caller will show as N/A or 0).
    """
    COL = cfg.COL
    if not models:
        return {}

    out = {}
    grp_keys = [COL["product_id"], COL["grammage"], COL["city"]] \
               if has_grammage and COL["grammage"] in train.columns \
               else [COL["product_id"], COL["city"]]

    for key, gdf in train.groupby(grp_keys):
        if isinstance(key, tuple):
            if len(key) == 3:
                pid, grm, city = key
            else:
                pid, city = key; grm = None
        else:
            pid = key; grm = None; city = ""

        cell_id = f"{pid}_{grm}_{city}" if grm else f"{pid}_{city}"
        cat = gdf["category"].iloc[0]
        m = models.get(cat)
        if m is None or len(gdf) < 10:
            continue
        try:
            yhat_log = np.asarray(m.predict(gdf))
            y_units  = gdf[COL["offtake_qty"]].values.astype(float)
            yhat_units = np.exp(np.clip(yhat_log, -3, 10))
            disc     = gdf["discount_pct"].values

            mask = np.isfinite(y_units) & np.isfinite(yhat_units) & np.isfinite(disc)
            if mask.sum() < 10:
                continue

            sub = pd.DataFrame({
                "bin":    (disc[mask] // 3 * 3).astype(int),
                "actual": y_units[mask],
                "pred":   yhat_units[mask],
            })
            # Need at least 3 obs per bin for the mean to be reliable
            grp = sub.groupby("bin").agg(
                n=("actual", "size"),
                act=("actual", "mean"),
                prd=("pred",   "mean"),
            ).reset_index()
            grp = grp[grp["n"] >= 3]
            if len(grp) < 3:
                continue   # not enough discount variation in this cell

            ss_res = float(((grp["act"] - grp["prd"]) ** 2).sum())
            ss_tot = float(((grp["act"] - grp["act"].mean()) ** 2).sum())
            if ss_tot <= 0:
                continue
            r2 = 1.0 - ss_res / ss_tot
            out[cell_id] = max(min(r2, 1.0), -0.5)
        except Exception:
            continue
    return out


def _compute_cell_test_r2(models: dict, test: pd.DataFrame,
                           has_grammage: bool) -> dict:
    """Per-cell held-out R²: how well does the per-category model predict
    this cell's TEST log_units? Same formulation as train R² but on the
    held-out window. NaN if too few test rows."""
    COL = cfg.COL
    if not models or test is None or test.empty:
        return {}
    grp_keys = [COL["product_id"], COL["grammage"], COL["city"]] \
               if has_grammage and COL["grammage"] in test.columns \
               else [COL["product_id"], COL["city"]]
    out = {}
    for key, gdf in test.groupby(grp_keys):
        if isinstance(key, tuple):
            if len(key) == 3:
                pid, grm, city = key
            else:
                pid, city = key; grm = None
        else:
            pid = key; grm = None; city = ""
        cell_id = f"{pid}_{grm}_{city}" if grm else f"{pid}_{city}"
        cat = gdf["category"].iloc[0]
        m = models.get(cat)
        if m is None or len(gdf) < 5:
            continue
        try:
            yhat = np.asarray(m.predict(gdf))
            y    = gdf["log_units"].values
            mask = np.isfinite(y) & np.isfinite(yhat)
            if mask.sum() < 3: continue
            ss_res = float(((y[mask] - yhat[mask]) ** 2).sum())
            ss_tot = float(((y[mask] - y[mask].mean()) ** 2).sum())
            if ss_tot <= 0: continue
            r2 = 1.0 - ss_res / ss_tot
            out[cell_id] = max(min(r2, 1.0), -2.0)  # clamp very-bad R² for display
        except Exception:
            continue
    return out


def _add_cell_confidence(elasticities: pd.DataFrame, raw_price_slopes: dict) -> pd.DataFrame:
    """
    Per-cell confidence score (0-100). Designed so a brand team can scale this
    to thousands of SKU × city cells and know which ones to ACT on vs which
    need a price test before any move.

    Score combines five sub-scores (0-1 each, weighted):
      - density (n_train)              w = 0.25  full credit at >=120 train rows
      - variation (n_price_levels)     w = 0.20  full credit at >=15 distinct prices
      - in-sample fit (cell_train_r2)  w = 0.20  full credit at >=0.50
      - plausibility (elast in band)   w = 0.15  binary: elasticity in [-4, -0.3]?
      - CI tightness (1 - se/|elast|)  w = 0.20  full credit at |elast|/se >= 4

    Tier mapping:
      score >= 70  → HIGH       (act on price moves)
      score >= 50  → MEDIUM     (act but smaller step / closer monitoring)
      score >= 30  → LOW        (hold; consider a structured A/B price test)
      score <  30  → DO_NOT_ACT (run a price test first; flag thin/unstable cell)
    """
    e = elasticities.copy()

    # ── sub-score components ──────────────────────────────────────
    density  = np.clip(e["n_train"]            / 120.0, 0, 1)
    variation= np.clip(e["n_discount_levels"]  / 15.0,  0, 1)
    # Series default so .astype works even if the column is ever absent
    _ctr2    = e["cell_train_r2"] if "cell_train_r2" in e.columns else pd.Series(0.0, index=e.index)
    fit      = np.clip(_ctr2.astype(float) / 0.50, 0, 1)

    pe = e["price_elasticity"].astype(float)
    plausibility = ((pe >= -4.0) & (pe <= -0.3)).astype(float)

    se = e["price_elasticity_se"].astype(float).replace(0, 1e-6)
    tightness = np.clip(np.abs(pe) / (se * 4.0), 0, 1)   # |elast|/se >= 4 → full credit

    # ── composite ─────────────────────────────────────────────────
    score = (
        0.25 * density +
        0.20 * variation +
        0.20 * fit +
        0.15 * plausibility +
        0.20 * tightness
    ) * 100.0
    e["confidence_score"] = score.round(1)
    # tier mapping
    def _tier(s):
        if s >= 70: return "HIGH"
        if s >= 50: return "MEDIUM"
        if s >= 30: return "LOW"
        return "DO_NOT_ACT"
    e["confidence_tier"] = e["confidence_score"].apply(_tier)

    # ── exposed sub-scores for audit / Excel ──────────────────────
    e["conf_density"]      = density.round(3)
    e["conf_variation"]    = variation.round(3)
    e["conf_fit"]          = fit.round(3)
    e["conf_plausibility"] = plausibility.astype(int)
    e["conf_tightness"]    = tightness.round(3)

    return e


def _compute_diagnostics(models: dict, train: pd.DataFrame,
                          test: pd.DataFrame, cat_metrics: list) -> dict:
    """
    Compute diagnostics across all per-category models.
    Reports BOTH daily-grain (log) and aggregated (3% discount-bin) metrics.
    The aggregated metric is what the saturation curve actually consumes.
    """
    COL = cfg.COL
    y_tr_all, p_tr_all, y_te_all, p_te_all = [], [], [], []
    test_pred = []

    for cat, m in models.items():
        sub_tr = train[train["category"] == cat]
        sub_te = test [test["category"]  == cat]
        if not sub_tr.empty:
            try:
                yp = np.asarray(m.predict(sub_tr))
                y_tr_all.append(sub_tr["log_units"].values); p_tr_all.append(yp)
            except Exception:
                pass
        if not sub_te.empty:
            try:
                yp = np.asarray(m.predict(sub_te))
                y_te_all.append(sub_te["log_units"].values); p_te_all.append(yp)
                tmp = sub_te.copy(); tmp["pred_log_units"] = yp
                test_pred.append(tmp)
            except Exception:
                pass

    def _r2(y, p):
        m = np.isfinite(y) & np.isfinite(p)
        if m.sum() < 2: return 0.0
        ss_res = ((y[m] - p[m]) ** 2).sum()
        ss_tot = ((y[m] - y[m].mean()) ** 2).sum()
        return max(1 - ss_res / ss_tot if ss_tot > 0 else 0.0, -9.99)

    r2_tr = r2_te = 0.0; log_mae = 99.9; raw_mape = 99.9
    if y_tr_all:
        y = np.concatenate(y_tr_all); p = np.concatenate(p_tr_all)
        r2_tr = _r2(y, p)
    if y_te_all:
        y = np.concatenate(y_te_all); p = np.concatenate(p_te_all)
        r2_te   = _r2(y, p)
        log_mae = float(np.mean(np.abs(y - p)))
        au = np.exp(y); pu = np.exp(p)
        raw_mape = float(np.mean(np.abs((au - pu) / np.maximum(au, 0.5))) * 100)

    # Aggregated (cell × 3% discount-bin) — what Stage 5 consumes
    mape_agg = 99.9; r2_units_agg = 0.0
    if test_pred:
        t = pd.concat(test_pred)
        t["pred_units"] = np.exp(np.clip(t["pred_log_units"], -3, 10))
        t["disc_bin"]   = (t["discount_pct"] // 3 * 3).astype(int)
        grp = t.groupby(["sku_city", "disc_bin"], as_index=False).agg(
            n=(COL["offtake_qty"], "size"),
            actual=(COL["offtake_qty"], "mean"),
            pred=("pred_units", "mean"),
        )
        grp = grp[grp["n"] >= 3]
        if not grp.empty:
            ae = (grp["actual"] - grp["pred"]).abs()
            mape_agg = float((ae / grp["actual"].clip(lower=0.5)).mean() * 100)
            ss_res = ((grp["actual"] - grp["pred"]) ** 2).sum()
            ss_tot = ((grp["actual"] - grp["actual"].mean()) ** 2).sum()
            r2_units_agg = float(1 - ss_res / ss_tot) if ss_tot > 0 else 0.0

    # Per-category elasticities for the diagnostics dict
    cat_elast_map = {m["category"]: m["elasticity"] for m in cat_metrics}
    cat_badge_map = {m["category"]: m["badge"]      for m in cat_metrics}

    return {
        "model_type":             "PerCategory_CellFE_Huber",
        "n_train":                len(train),
        "n_test":                 len(test),
        "n_categories_fit":       len(models),
        "test_r2_train":          round(r2_tr, 3),
        "test_r2_log":            round(r2_te, 3),
        "test_mape_log":          round(log_mae, 3),
        "test_mape":              round(raw_mape, 1),
        "test_r2":                round(r2_units_agg, 3),   # legacy key
        "test_mape_agg":          round(mape_agg, 1),
        "test_r2_units_agg":      round(r2_units_agg, 3),
        "category_elasticities":  {k: round(v, 3) for k, v in cat_elast_map.items()},
        "category_badge":         {k: round(v, 4) for k, v in cat_badge_map.items()},
        # Old-style scalar globals (use first category as a proxy for back-compat)
        "price_coef_global":      round(next(iter(cat_elast_map.values()), -1.5), 3) if cat_elast_map else -1.5,
        "discount_coefficient":   round(next(iter(cat_badge_map.values()), 0.01), 4) if cat_badge_map else 0.01,
    }


def _simple_r2(a, p) -> float:
    a = np.asarray(a, dtype=float); p = np.asarray(p, dtype=float)
    m = np.isfinite(a) & np.isfinite(p)
    if m.sum() < 2:
        return 0.0
    ss_res = ((a[m] - p[m]) ** 2).sum()
    ss_tot = ((a[m] - a[m].mean()) ** 2).sum()
    return max(1.0 - ss_res / ss_tot, -9.99) if ss_tot > 0 else 0.0


def _compute_sku_group_r2(elasticities: pd.DataFrame, data: pd.DataFrame,
                          has_grammage: bool) -> dict:
    """
    R² of the DECISION response model per SKU/platform group (product_id), pooling
    that SKU's cities at the 3ppt-discount-bin grain — the trust floor for acting.
    A single cell is too noisy to fit; the SKU pooled is the right grain.
    Returns {product_id: r2}.
    """
    COL = cfg.COL
    qty = COL["offtake_qty"]
    if elasticities is None or elasticities.empty or data is None or data.empty:
        return {}
    if "selling_price" not in data.columns or "discount_pct" not in data.columns:
        return {}

    def _cid(df):
        if has_grammage and COL["grammage"] in df.columns:
            return (df[COL["product_id"]].astype(str) + "_" + df[COL["grammage"]].astype(str)
                    + "_" + df[COL["city"]].astype(str))
        return df[COL["product_id"]].astype(str) + "_" + df[COL["city"]].astype(str)

    base = elasticities.set_index("cell_id")[
        ["price_elasticity", "avg_units", "avg_selling_price", "avg_discount_pct",
         "badge_sensitivity", "product_id"]].to_dict("index")
    d = data.copy()
    d["cell_id"] = _cid(d)
    rows = []
    for _, r in d.iterrows():
        b = base.get(r["cell_id"])
        if not b or b["avg_selling_price"] <= 0 or b["avg_units"] <= 0:
            continue
        price = float(r["selling_price"]); disc = float(r["discount_pct"])
        if price <= 0:
            continue
        lu = (np.log(b["avg_units"]) + float(b["price_elasticity"]) * np.log(price / b["avg_selling_price"])
              + float(b["badge_sensitivity"]) * (disc - b["avg_discount_pct"]))
        rows.append({"pid": b["product_id"], "cell_id": r["cell_id"],
                     "bin": int(disc // 3 * 3), "actual": float(r[qty]),
                     "pred": float(np.exp(np.clip(lu, -3, 10)))})
    if not rows:
        return {}
    rdf = pd.DataFrame(rows)
    out = {}
    for pid, g in rdf.groupby("pid"):
        gg = g.groupby(["cell_id", "bin"]).agg(n=("actual", "size"),
                                               a=("actual", "mean"), p=("pred", "mean")).reset_index()
        gg = gg[gg["n"] >= 3]
        if len(gg) >= 3:
            out[pid] = _simple_r2(gg["a"].values, gg["p"].values)
    return out


def _compute_decision_diagnostics(elasticities: pd.DataFrame, train: pd.DataFrame,
                                  test: pd.DataFrame, has_grammage: bool) -> dict:
    """
    Held-out accuracy of the DECISION model — the Stage-5 price/badge curve
    that actually sets recommendations (elasticity + badge only, NO lag /
    momentum terms), using TRAIN-window base values (no leakage):

        units = base_units · (price/base_price)^elasticity
                           · exp(badge · (discount − base_discount))

    This is the honest "if you change the price, will volume move as
    predicted?" number — distinct from the full lag-laden regression R²,
    which is inflated by autocorrelation. Returns daily + 3ppt-bin R²/MAPE.
    """
    COL = cfg.COL
    qty = COL["offtake_qty"]
    out = {
        "decision_test_r2":      0.0,  "decision_test_mape":      99.9,
        "decision_test_r2_bin":  0.0,  "decision_test_mape_bin":  99.9,
    }
    if (test is None or test.empty or elasticities is None or elasticities.empty
            or "selling_price" not in test.columns or "discount_pct" not in test.columns):
        return out

    def _cid(df):
        if has_grammage and COL["grammage"] in df.columns:
            return (df[COL["product_id"]].astype(str) + "_"
                    + df[COL["grammage"]].astype(str) + "_"
                    + df[COL["city"]].astype(str))
        return df[COL["product_id"]].astype(str) + "_" + df[COL["city"]].astype(str)

    coef = elasticities.set_index("cell_id")[
        ["price_elasticity", "badge_sensitivity"]].to_dict("index")
    tr = train.copy(); tr["cell_id"] = _cid(tr)
    base = tr.groupby("cell_id").agg(
        base_units=(qty, "mean"),
        base_price=("selling_price", "mean"),
        base_disc=("discount_pct", "mean"),
    ).to_dict("index")
    te = test.copy(); te["cell_id"] = _cid(te)

    rows = []
    for _, row in te.iterrows():
        cid = row["cell_id"]
        if cid not in coef or cid not in base:
            continue
        b = base[cid]; bp = b["base_price"]
        if not bp or bp <= 0:
            continue
        elast = float(coef[cid]["price_elasticity"])
        badge = float(coef[cid]["badge_sensitivity"])
        price = float(row["selling_price"]); disc = float(row["discount_pct"])
        if price <= 0 or b["base_units"] <= 0:
            continue
        # Clip predicted log-units to [-3, 10] — same band the full-model
        # diagnostics use — so a deep-promo row (price << base) can't explode
        # the metric. (Was: (price/bp)**elast unclipped → up to ~39x on elast=-4.)
        log_units = (np.log(b["base_units"]) + elast * np.log(price / bp)
                     + badge * (disc - b["base_disc"]))
        pred = float(np.exp(np.clip(log_units, -3, 10)))
        rows.append({"cid": cid, "bin": int(disc // 3 * 3),
                     "actual": float(row[qty]), "pred": pred})

    if len(rows) < 5:
        return out
    rdf = pd.DataFrame(rows)
    a = rdf["actual"].values; p = rdf["pred"].values
    out["decision_test_r2"]   = round(_simple_r2(a, p), 3)
    out["decision_test_mape"] = round(float(np.mean(np.abs((a - p) / np.maximum(a, 0.5))) * 100), 1)

    g = rdf.groupby(["cid", "bin"]).agg(
        n=("actual", "size"), av=("actual", "mean"), pv=("pred", "mean")).reset_index()
    g = g[g["n"] >= 3]
    if len(g) >= 3:
        out["decision_test_r2_bin"]   = round(_simple_r2(g["av"].values, g["pv"].values), 3)
        out["decision_test_mape_bin"] = round(
            float((np.abs(g["av"] - g["pv"]) / np.maximum(g["av"], 0.5)).mean() * 100), 1)
    return out


def predict_units(model, features_dict: dict) -> float:
    """
    Predict units for a single observation.
    `model` is now a dict {category: fitted_model}. Looks up the right one.
    """
    row = pd.DataFrame([features_dict])
    cat = features_dict.get("category")
    if isinstance(model, dict):
        m = model.get(cat) if cat else next(iter(model.values()))
    else:
        m = model
    try:
        log_pred = m.predict(row)
        return float(np.exp(log_pred.iloc[0]))
    except Exception:
        return float(np.exp(m.predict(row)[0]))
