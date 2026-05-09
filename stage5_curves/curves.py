"""
Stage 5 — Saturation Curve Generation.

For each cell (SKU × City), sweeps discount from 0% to 30% and predicts
expected units at each level. Fits a smooth 4-parameter logistic (4PL)
curve. Runs extrapolation and stability checks, assigns confidence flags.
"""
import numpy as np
import pandas as pd
from scipy.optimize import curve_fit
import warnings
import v4_config as cfg


def generate_saturation_curves(elasticities_df: pd.DataFrame,
                                model_result,
                                feat_df: pd.DataFrame) -> pd.DataFrame:
    """
    Generate saturation curves for all cells.

    For each cell, sweeps discount 0-30%, predicts units using the elasticity
    model, and fits a 4PL curve for smooth interpolation.

    Returns DataFrame with one row per cell containing curve parameters,
    raw points, and confidence flags.
    """
    C = cfg.COL
    print(f"  [Stage 5] Generating saturation curves for {len(elasticities_df)} cells...")

    discount_range = np.arange(
        cfg.DISCOUNT_MIN_PCT, cfg.DISCOUNT_MAX_PCT + cfg.DISCOUNT_STEP_PCT, cfg.DISCOUNT_STEP_PCT
    )

    curves = []
    for _, cell in elasticities_df.iterrows():
        pid = cell["product_id"]
        city = cell["city"]
        mrp = cell["mrp"]
        elasticity = cell["elasticity"]

        # Get typical context for this cell from training data
        cell_data = feat_df[
            (feat_df[C["product_id"]] == pid) & (feat_df[C["city"]] == city)
        ]
        if cell_data.empty:
            continue

        typical = _get_typical_context(cell_data)
        base_units = cell["avg_units"]

        # Sweep discount levels
        points = []
        for disc_pct in discount_range:
            price = mrp * (1 - disc_pct / 100)
            # Log-log elasticity: %Δunits / %Δprice = elasticity
            # units(p) = base_units * (p / base_price) ^ elasticity
            price_ratio = price / typical["base_price"] if typical["base_price"] > 0 else 1
            if price_ratio > 0:
                predicted_units = base_units * (price_ratio ** elasticity)
            else:
                predicted_units = base_units
            predicted_units = max(predicted_units, 0)
            points.append({
                "discount_pct": disc_pct,
                "price": round(price, 1),
                "predicted_units": round(predicted_units, 2),
                "daily_revenue": round(predicted_units * price, 2),
            })

        points_df = pd.DataFrame(points)

        # Fit 4PL curve
        curve_params = _fit_4pl(points_df["discount_pct"].values,
                                points_df["predicted_units"].values)

        # Confidence assessment
        confidence = _assess_confidence(cell, cell_data, typical)

        curves.append({
            "product_id": pid,
            "city": city,
            "category": cell["category"],
            "title": cell["title"],
            "mrp": mrp,
            "elasticity": elasticity,
            "avg_units_baseline": base_units,
            "curve_points": points,
            "curve_params": curve_params,
            "confidence": confidence,
            "n_observations": cell["n_observations"],
            "observed_discount_min": round(cell_data["discount_pct"].min(), 1) if "discount_pct" in cell_data.columns else 0,
            "observed_discount_max": round(cell_data["discount_pct"].max(), 1) if "discount_pct" in cell_data.columns else 0,
            "cell_id": cell["cell_id"],
        })

    curves_df = pd.DataFrame(curves)

    # Summary
    conf_counts = curves_df["confidence"].value_counts()
    print(f"  [Stage 5] Generated {len(curves_df)} curves:")
    for conf, count in conf_counts.items():
        print(f"    {conf}: {count} cells")

    return curves_df


def _get_typical_context(cell_data: pd.DataFrame) -> dict:
    """Get median values of control variables for counterfactual predictions."""
    C = cfg.COL
    return {
        "base_price": cell_data[C["price"]].median(),
        "base_units": cell_data[C["offtake_qty"]].median(),
        "osa": cell_data[C["availability"]].median(),
        "ad_sov": cell_data[C["ad_sov"]].median() if C["ad_sov"] in cell_data.columns else 0,
        "competitor_price": cell_data[C["competitor_price"]].median() if C["competitor_price"] in cell_data.columns else 0,
    }


def _fit_4pl(x, y):
    """
    Fit a 4-parameter logistic curve: y = D + (A - D) / (1 + (x/C)^B)
    Returns dict with parameters or None if fitting fails.
    """
    try:
        def logistic_4pl(x, A, B, C, D):
            return D + (A - D) / (1 + (np.maximum(x, 0.01) / max(C, 0.01)) ** B)

        # Initial guesses
        p0 = [y.min(), 2.0, 15.0, y.max()]
        bounds = ([0, 0.1, 1, 0], [y.max() * 2, 10, 30, y.max() * 3])

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            popt, _ = curve_fit(logistic_4pl, x, y, p0=p0, bounds=bounds, maxfev=5000)

        return {"A": round(popt[0], 3), "B": round(popt[1], 3),
                "C": round(popt[2], 3), "D": round(popt[3], 3)}
    except Exception:
        return None


def _assess_confidence(cell_row, cell_data, typical) -> str:
    """
    Assign confidence flag: High / Medium / Low / Needs Experiment.

    Based on:
    - Number of observations
    - Variance in observed discount range
    - Elasticity uncertainty (SE)
    """
    n = cell_row["n_observations"]
    se = abs(cell_row["elasticity_se"])
    elast = cell_row["elasticity"]

    # Check if observed discount range covers recommended range
    if "discount_pct" in cell_data.columns:
        obs_range = cell_data["discount_pct"].max() - cell_data["discount_pct"].min()
    else:
        obs_range = 0

    # Scoring
    if n >= 200 and se < 0.5 and obs_range >= 10 and elast < 0:
        return "High"
    elif n >= 100 and se < 1.0 and obs_range >= 5 and elast < 0:
        return "Medium"
    elif n >= 50 and elast < 0:
        return "Low"
    else:
        return "Needs Experiment"


def evaluate_curve_at_discount(curve_params: dict, discount_pct: float) -> float:
    """Evaluate the fitted 4PL curve at a specific discount level."""
    if curve_params is None:
        return 0
    A, B, C, D = curve_params["A"], curve_params["B"], curve_params["C"], curve_params["D"]
    x = max(discount_pct, 0.01)
    return D + (A - D) / (1 + (x / max(C, 0.01)) ** B)
