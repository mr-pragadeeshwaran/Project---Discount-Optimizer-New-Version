"""
Stage 5 — Saturation Curve Generation (Dual-Signal Log-Log Model).

For each cell, sweeps the SELLING PRICE from floor to MRP and predicts
expected units at each price point using the log-log model:

    units(p) = base_units × (p / base_price) ^ price_elasticity
                          × exp(badge_sensitivity × (d(p) - base_discount))

Where:
  price_elasticity: cell-specific log-log coefficient (negative — higher price = fewer units)
  badge_sensitivity: residual badge/deal effect on units holding price constant
  d(p) = (MRP - p) / MRP × 100  = implied discount at price p
  base_price = avg_selling_price (current state)

The X-axis is SELLING PRICE (₹), not discount %. The discount is derived for
each price point to compute economics and for guardrail display.

Then fits a smooth 4PL curve for interpolation and assigns confidence.
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
    Generate price-axis saturation curves for all cells.

    Sweeps selling_price from floor (30% below MRP) to MRP,
    predicts units using the dual-signal log-log model,
    fits a 4PL curve, and assigns confidence.
    """
    C = cfg.COL
    print(f"  [Stage 5] Generating saturation curves for {len(elasticities_df)} cells...")

    curves = []
    for _, cell in elasticities_df.iterrows():
        pid            = cell["product_id"]
        city           = cell["city"]
        grammage       = cell.get("grammage", None)
        stable_mrp     = float(cell["stable_mrp"])
        price_elast    = float(cell["price_elasticity"])   # log-log: negative
        badge_sens     = float(cell["badge_sensitivity"])  # badge lift per 1ppt
        avg_price      = float(cell.get("avg_selling_price", cell.get("avg_price", stable_mrp * 0.85)))
        avg_discount   = float(cell["avg_discount_pct"])
        base_units     = float(cell["avg_units"])

        # Get cell's feature data
        cell_mask = (
            (feat_df[C["product_id"]] == pid) &
            (feat_df[C["city"]]       == city)
        )
        if grammage and C["grammage"] in feat_df.columns:
            cell_mask = cell_mask & (feat_df[C["grammage"]] == grammage)
        cell_data = feat_df[cell_mask & (feat_df["is_regular_day"] == 1)]

        if cell_data.empty:
            continue

        # ── Price sweep range ───────────────────────────────────────
        # Observed selling price range from data
        if "selling_price" in cell_data.columns:
            obs_price_min = cell_data["selling_price"].min()
            obs_price_max = cell_data["selling_price"].max()
        else:
            obs_price_min = stable_mrp * 0.50
            obs_price_max = stable_mrp

        # Sweep from observed min (or 30% off MRP floor) to MRP
        sweep_floor = max(obs_price_min * 0.95, stable_mrp * 0.30)
        sweep_ceil  = stable_mrp  # MRP = no discount

        # Step: ₹1 for cheapish SKUs, ₹5 for premium
        step = 1.0 if stable_mrp <= 200 else 5.0
        price_range = np.arange(sweep_floor, sweep_ceil + step, step)
        if len(price_range) < 5:
            price_range = np.linspace(sweep_floor, sweep_ceil, 20)

        # Clip avg_price to range (base point)
        base_price = np.clip(avg_price, sweep_floor, sweep_ceil)
        if base_price <= 0:
            base_price = stable_mrp * 0.85

        # ── Predict units at each price point ───────────────────────
        # log(units(p)) = log(base_units) + price_elast × log(p/base_price)
        #                + badge_sens × (d(p) - avg_discount)
        # → units(p) = base_units × (p/base_price)^price_elast
        #                          × exp(badge_sens × (d(p) - avg_discount))
        points = []
        for price in price_range:
            disc_pct = max(0, (stable_mrp - price) / stable_mrp * 100)

            price_ratio  = price / base_price if base_price > 0 else 1.0
            badge_delta  = disc_pct - avg_discount

            # Dual-signal prediction
            pred_units = (
                base_units
                * (price_ratio ** price_elast)
                * np.exp(badge_sens * badge_delta)
            )
            pred_units = max(float(pred_units), 0.01)

            points.append({
                "selling_price": round(float(price), 2),
                "discount_pct":  round(float(disc_pct), 1),
                "price":         round(float(price), 2),       # alias for Stages 6-8
                "predicted_units": round(pred_units, 2),
                "daily_revenue": round(pred_units * price, 2),
            })

        points_df = pd.DataFrame(points)

        # ── Fit 4PL on price axis (inverted: lower price → more units) ──
        # 4PL on discount_pct axis for compatibility with Stage 6
        curve_params = _fit_4pl(
            points_df["discount_pct"].values,
            points_df["predicted_units"].values,
        )

        # ── Confidence assessment + reason ───────────────────────────
        confidence, quality_note = _assess_confidence_with_reason(cell, cell_data)

        # Observed price range info
        obs_disc_min = round(float(max(0, (stable_mrp - obs_price_max) / stable_mrp * 100)), 1)
        obs_disc_max = round(float(max(0, (stable_mrp - obs_price_min) / stable_mrp * 100)), 1)
        extrap_pct   = max(0, (obs_price_min - sweep_floor) / max(obs_price_min, 1) * 100)

        curves.append({
            "product_id":            pid,
            "grammage":              grammage,
            "city":                  city,
            "category":              cell["category"],
            "title":                 cell["title"],
            "stable_mrp":            stable_mrp,
            "price_elasticity":      price_elast,
            "badge_sensitivity":     badge_sens,
            "elasticity":            price_elast,          # backwards-compat
            "discount_sensitivity":  badge_sens,           # backwards-compat
            "avg_units_baseline":    base_units,
            "avg_selling_price":     round(avg_price, 2),
            "avg_discount_pct":      round(avg_discount, 1),
            "historical_floor_disc": round(float(cell.get("historical_floor_disc", avg_discount)), 1),
            "cell_train_r2":         round(float(cell.get("cell_train_r2", 0)), 3),
            "cell_test_r2":          round(float(cell.get("cell_test_r2", 0) or 0), 3),
            "curve_points":          points,
            "curve_params":          curve_params,
            "confidence":            confidence,
            "quality_note":          quality_note,
            # Model-based per-cell confidence (added May 2026) — flows
            # through to Stage 7's tiering as a hard gate against
            # acting on data-thin or high-uncertainty cells.
            "confidence_score":      float(cell.get("confidence_score", 0)),
            "confidence_tier":       str(cell.get("confidence_tier", "")),
            "conf_density":          float(cell.get("conf_density", 0)),
            "conf_variation":        float(cell.get("conf_variation", 0)),
            "conf_fit":              float(cell.get("conf_fit", 0)),
            "conf_plausibility":     int(cell.get("conf_plausibility", 0)),
            "conf_tightness":        float(cell.get("conf_tightness", 0)),
            "n_observations":        cell["n_observations"],
            "n_discount_levels":     cell["n_discount_levels"],
            "observed_discount_min": obs_disc_min,
            "observed_discount_max": obs_disc_max,
            "extrapolation_pct":     round(extrap_pct, 1),
            "cell_id":               cell["cell_id"],
        })

    curves_df = pd.DataFrame(curves)

    if not curves_df.empty:
        conf_counts = curves_df["confidence"].value_counts()
        print(f"  [Stage 5] Generated {len(curves_df)} curves:")
        for conf, count in conf_counts.items():
            avg_pe = curves_df[curves_df["confidence"] == conf]["price_elasticity"].mean()
            avg_bs = curves_df[curves_df["confidence"] == conf]["badge_sensitivity"].mean()
            print(f"    {conf}: {count} cells  "
                  f"(avg price_elast={avg_pe:.3f}, avg badge_sens={avg_bs:.4f})")

    return curves_df


def _fit_4pl(x, y):
    """Fit a 4-parameter logistic curve: y = D + (A-D)/(1+(x/C)^B)."""
    try:
        def logistic_4pl(x, A, B, C, D):
            return D + (A - D) / (1 + (np.maximum(x, 0.01) / max(C, 0.01)) ** B)

        p0     = [y.min(), 2.0, max(x.mean(), 5.0), y.max()]
        bounds = ([0, 0.1, 0.1, 0], [y.max() * 3, 10, max(x) + 10, y.max() * 3])

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            popt, _ = curve_fit(logistic_4pl, x, y, p0=p0, bounds=bounds, maxfev=5000)

        return {"A": round(float(popt[0]), 3), "B": round(float(popt[1]), 3),
                "C": round(float(popt[2]), 3), "D": round(float(popt[3]), 3)}
    except Exception:
        return None


def _assess_confidence_with_reason(cell_row, cell_data):
    """
    Returns (confidence, quality_note).

    quality_note is a short human-readable explanation for the brand team
    of any data-quality issue that downgraded the confidence.

    Downgrade rules:
      - elasticity pinned at the [-4, -0.3] safety bound → model is at its rail
      - cell-level demand grew >2x over the observation window → price/qty
        relationship is confounded by secular growth (e.g. launch ramp)
      - low n_train → elasticity is mostly the category prior, not cell-specific
    """
    n            = cell_row["n_observations"]
    n_train      = cell_row.get("n_train", n)
    n_disc       = int(cell_row.get("n_discount_levels", 0))
    disc_std     = float(cell_row.get("disc_pct_std", 0))
    price_elast  = float(cell_row.get("price_elasticity", 0))
    n_obs_data   = len(cell_data) if cell_data is not None else 0

    # Correct sign check: price elasticity should be negative
    if price_elast >= 0:
        return "Needs Experiment", "Elasticity has wrong sign — model unreliable"

    # No training data → elasticity is category default
    if n_train < 30:
        return "Needs Experiment", f"Only {n_train} training rows — needs price test"

    # Detect elasticity boundary-hit
    BOUNDARY_TOL = 0.05
    elast_at_floor = price_elast <= -4.0 + BOUNDARY_TOL
    elast_at_ceil  = price_elast >= -0.3 - BOUNDARY_TOL

    # Detect rapid demand growth (launch ramp etc.)
    growth_ratio = 1.0
    growth_confounded = False
    if n_obs_data >= 60 and cell_data is not None:
        try:
            sorted_data = cell_data.sort_values(cfg.COL["date"])
            q = max(15, int(len(sorted_data) * 0.25))
            early = float(sorted_data[cfg.COL["offtake_qty"]].head(q).mean())
            late  = float(sorted_data[cfg.COL["offtake_qty"]].tail(q).mean())
            if early >= 1.0:
                growth_ratio = late / early
                if growth_ratio >= 2.0:
                    growth_confounded = True
        except Exception:
            pass

    # Base tier from sample-size + price variation
    if n >= 200 and n_disc >= 10 and disc_std >= 3.0:
        base = "High"
    elif n >= 100 and n_disc >= 5 and disc_std >= 2.0:
        base = "Medium"
    elif n >= 50 and disc_std >= 1.0:
        base = "Low"
    else:
        return "Needs Experiment", "Insufficient discount variation in history"

    notes = []
    if elast_at_floor:
        notes.append("elasticity at floor (-4)")
        if base == "High": base = "Medium"
        elif base == "Medium": base = "Low"
        else: base = "Needs Experiment"
    elif elast_at_ceil:
        notes.append("elasticity at ceiling (-0.3)")
        if base == "High": base = "Medium"
        elif base == "Medium": base = "Low"
        else: base = "Needs Experiment"

    if growth_confounded:
        notes.append(f"demand grew {growth_ratio:.1f}x over period (launch/ramp)")
        if base == "High": base = "Medium"
        elif base == "Medium": base = "Low"

    note = "; ".join(notes) if notes else "OK"
    return base, note


# Back-compat shim (some code may still import the old name)
def _assess_confidence(cell_row, cell_data):
    return _assess_confidence_with_reason(cell_row, cell_data)[0]


def evaluate_curve_at_discount(curve_params: dict, discount_pct: float) -> float:
    """Evaluate the fitted 4PL curve at a specific discount level."""
    if curve_params is None:
        return 0
    A, B, C, D = curve_params["A"], curve_params["B"], curve_params["C"], curve_params["D"]
    x = max(discount_pct, 0.01)
    return D + (A - D) / (1 + (x / max(C, 0.01)) ** B)
