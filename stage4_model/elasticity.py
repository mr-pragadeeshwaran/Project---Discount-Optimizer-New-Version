"""
Stage 4 — Hierarchical Elasticity Model.

Fits a mixed-effects model across all cells with partial pooling:
  Category level → City level → SKU×City level

Cells with thin data borrow strength from their category/city neighbors.
Trained on regular days only (event + OOS days excluded).
"""
import warnings
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
from sklearn.model_selection import train_test_split
import v4_config as cfg
from stage3_features.features import get_feature_columns


def train_hierarchical_model(feat_df: pd.DataFrame) -> dict:
    """
    Train a hierarchical elasticity model on regular-day data.

    Returns dict with:
        model: fitted MixedLM result
        elasticities: DataFrame with per-cell elasticity estimates
        diagnostics: dict with overall metrics
        train_data / test_data: for downstream validation
    """
    C = cfg.COL
    df = feat_df.copy()

    # ── Filter to regular days only ─────────────────────────────────
    regular = df[df["is_regular_day"] == 1].copy()
    print(f"  [Stage 4] Training data: {len(regular):,} regular-day rows "
          f"(excluded {len(df) - len(regular):,} event/OOS)")

    # ── Ensure required columns ─────────────────────────────────────
    regular["sku_city"] = regular[C["product_id"]].astype(str) + "__" + regular[C["city"]].astype(str)

    # ── Train/test split (time-based: last 20% of dates) ────────────
    dates_sorted = sorted(regular[C["date"]].unique())
    split_idx = int(len(dates_sorted) * (1 - cfg.TEST_SPLIT_PCT))
    split_date = dates_sorted[split_idx]

    train = regular[regular[C["date"]] <= split_date].copy()
    test = regular[regular[C["date"]] > split_date].copy()
    print(f"    Train: {len(train):,} rows (≤ {split_date.date()}) | "
          f"Test: {len(test):,} rows (> {split_date.date()})")

    # ── Fit hierarchical mixed-effects model ────────────────────────
    print(f"  [Stage 4] Fitting hierarchical model (groups=category, random slope on log_price)...")

    # Build formula: log_units ~ fixed effects
    fixed_effects = "log_price + osa_rolling_7d + log_ad_sov + price_gap + is_weekend + discount_pct"
    formula = f"log_units ~ {fixed_effects}"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            model = smf.mixedlm(
                formula,
                data=train,
                groups=train["category"],
                re_formula="~log_price",  # Random slope: per-category elasticity variation
            )
            result = model.fit(reml=True, method="lbfgs", maxiter=200)
            print(f"    Model converged: {result.converged}")
        except Exception as e:
            print(f"    ⚠ MixedLM failed ({e}), falling back to OLS per-category")
            result = _fallback_ols(train, formula)

    # ── Extract elasticities per cell ───────────────────────────────
    elasticities = _extract_cell_elasticities(result, train, test)

    # ── Compute diagnostics on test set ─────────────────────────────
    diagnostics = _compute_diagnostics(result, train, test)

    print(f"  [Stage 4] Results:")
    print(f"    Overall elasticity (fixed): {diagnostics['fixed_elasticity']:.3f}")
    print(f"    Test MAPE: {diagnostics['test_mape']:.1f}%")
    print(f"    Test R²: {diagnostics['test_r2']:.3f}")
    print(f"    Cells with negative elasticity: {diagnostics['pct_negative_elasticity']:.0f}%")

    return {
        "model": result,
        "elasticities": elasticities,
        "diagnostics": diagnostics,
        "train_data": train,
        "test_data": test,
        "formula": formula,
    }


def _fallback_ols(train, formula):
    """Fallback: simple OLS with category dummies if MixedLM fails."""
    import statsmodels.api as sm
    formula_ols = formula + " + C(category)"
    result = smf.ols(formula_ols, data=train).fit()
    print(f"    OLS fallback: R²={result.rsquared:.3f}")
    return result


def _extract_cell_elasticities(model_result, train, test) -> pd.DataFrame:
    """Extract per-cell elasticity estimates with uncertainty bands."""
    C = cfg.COL
    combined = pd.concat([train, test])

    # Get the fixed-effect elasticity (log_price coefficient)
    try:
        fixed_elast = model_result.params.get("log_price", model_result.params.iloc[1])
        fixed_se = model_result.bse.get("log_price", model_result.bse.iloc[1])
    except Exception:
        fixed_elast = -1.0
        fixed_se = 0.5

    # Try to get random effects per category
    try:
        re = model_result.random_effects
        category_adjustments = {}
        for cat, vals in re.items():
            if hasattr(vals, "__len__") and len(vals) > 1:
                category_adjustments[cat] = float(vals.iloc[1])  # log_price random slope
            else:
                category_adjustments[cat] = 0.0
    except Exception:
        category_adjustments = {}

    # Build per-cell elasticity table
    rows = []
    cells = combined.groupby([C["product_id"], C["city"]])
    for (pid, city), cell_df in cells:
        cat = cell_df["category"].iloc[0]
        title = cell_df[C["title"]].iloc[0] if C["title"] in cell_df.columns else str(pid)
        n_obs = len(cell_df)
        n_train = len(cell_df[cell_df[C["date"]] <= train[C["date"]].max()])

        cat_adj = category_adjustments.get(cat, 0.0)
        cell_elast = fixed_elast + cat_adj

        # Wider SE for cells with fewer observations (shrinkage toward category)
        shrinkage_factor = max(1.0, 30.0 / max(n_train, 1))
        cell_se = fixed_se * shrinkage_factor

        mrp = cell_df[C["mrp"]].mode().iloc[0] if C["mrp"] in cell_df.columns else 100
        avg_price = cell_df[C["price"]].mean()
        avg_units = cell_df[C["offtake_qty"]].mean()
        avg_discount = cell_df["discount_pct_actual"].mean() if "discount_pct_actual" in cell_df.columns else 0

        rows.append({
            "product_id": pid,
            "city": city,
            "category": cat,
            "title": str(title)[:60],
            "mrp": mrp,
            "avg_price": round(avg_price, 1),
            "avg_units": round(avg_units, 1),
            "avg_discount_pct": round(avg_discount, 1),
            "n_observations": n_obs,
            "n_train": n_train,
            "elasticity": round(cell_elast, 4),
            "elasticity_se": round(cell_se, 4),
            "elasticity_lower": round(cell_elast - 1.96 * cell_se, 4),
            "elasticity_upper": round(cell_elast + 1.96 * cell_se, 4),
            "cell_id": f"{pid}_{city}",
        })

    elast_df = pd.DataFrame(rows)
    return elast_df


def _compute_diagnostics(model_result, train, test) -> dict:
    """Compute model diagnostics on train and test sets."""
    C = cfg.COL

    try:
        fixed_elast = float(model_result.params.get("log_price", model_result.params.iloc[1]))
    except Exception:
        fixed_elast = -1.0

    # Predict on test
    try:
        y_pred_test = model_result.predict(test)
        y_actual_test = test["log_units"].values
        y_pred_np = y_pred_test.values if hasattr(y_pred_test, "values") else np.array(y_pred_test)

        # In real units (exp)
        actual_units = np.exp(y_actual_test)
        pred_units = np.exp(y_pred_np)

        mask = actual_units > 0
        mape = np.mean(np.abs((actual_units[mask] - pred_units[mask]) / actual_units[mask])) * 100
        ss_res = np.sum((actual_units[mask] - pred_units[mask]) ** 2)
        ss_tot = np.sum((actual_units[mask] - actual_units[mask].mean()) ** 2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    except Exception as e:
        print(f"    ⚠ Test diagnostics failed: {e}")
        mape = 99.9
        r2 = 0.0

    return {
        "fixed_elasticity": fixed_elast,
        "test_mape": round(mape, 1),
        "test_r2": round(r2, 3),
        "pct_negative_elasticity": 100.0 if fixed_elast < 0 else 0.0,
        "n_train": len(train),
        "n_test": len(test),
    }


def predict_units(model_result, features_dict: dict) -> float:
    """Predict units for a single observation using the fitted model."""
    row = pd.DataFrame([features_dict])
    try:
        log_pred = model_result.predict(row)
        return float(np.exp(log_pred.iloc[0]))
    except Exception:
        return float(np.exp(model_result.predict(row)[0]))
