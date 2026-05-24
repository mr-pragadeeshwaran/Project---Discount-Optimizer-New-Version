"""
Stage 6 — Economics: Contribution Margin + Elbow Detection.

For each cell, calculates variable costs, contribution margin at every
discount level on the saturation curve, and finds the elbow where
marginal ROI crosses the threshold.
"""
import numpy as np
import pandas as pd
import v4_config as cfg


def compute_economics(curves_df: pd.DataFrame,
                       master_costs: pd.DataFrame = None) -> pd.DataFrame:
    """
    For each cell, compute economics at each discount level and find the elbow.

    Returns enriched DataFrame with per-cell economics including:
    - current_discount_pct, elbow_discount_pct
    - expected volume/revenue/margin changes
    - monthly savings at elbow
    """
    print(f"  [Stage 6] Computing economics for {len(curves_df)} cells...")
    results = []

    for _, cell in curves_df.iterrows():
        mrp = cell.get("stable_mrp", cell.get("mrp", 100))
        if pd.isna(mrp) or mrp <= 0:
            mrp = 100
        points       = cell["curve_points"]
        current_disc = cell.get("avg_discount_pct", 0)
        if current_disc is None or pd.isna(current_disc):
            current_disc = 0

        # Current selling price from Stage 5
        current_price = cell.get("avg_selling_price", mrp * (1 - current_disc / 100))
        if current_price is None or pd.isna(current_price):
            current_price = mrp * (1 - current_disc / 100)

        costs = _get_costs(cell["product_id"], mrp, master_costs)

        ladder = []
        for pt in points:
            d     = pt["discount_pct"]
            price = pt.get("selling_price", pt.get("price", mrp * (1 - d / 100)))
            units = pt["predicted_units"]

            variable_cost = costs["cogs"] + costs["commission_pct"] * price + costs["fulfillment"]
            contribution  = (price - variable_cost) * units
            discount_cost = (mrp - price) * units
            daily_revenue = price * units

            ladder.append({
                "discount_pct":          d,
                "selling_price":         price,
                "price":                 price,          # alias
                "units":                 units,
                "daily_revenue":         round(daily_revenue, 2),
                "variable_cost_per_unit": round(variable_cost, 2),
                "contribution_margin":   round(contribution, 2),
                "discount_cost":         round(discount_cost, 2),
            })

        ladder_df = pd.DataFrame(ladder)

        ladder_df["marginal_contribution"]  = ladder_df["contribution_margin"].diff()
        ladder_df["marginal_discount_cost"] = ladder_df["discount_cost"].diff()
        ladder_df["marginal_roi"] = np.where(
            ladder_df["marginal_discount_cost"] > 0,
            ladder_df["marginal_contribution"] / ladder_df["marginal_discount_cost"],
            np.inf
        )

        elbow        = _find_elbow(ladder_df, cfg.MARGINAL_ROI_THRESHOLD)
        current_row  = _interpolate_at_discount(ladder_df, current_disc)
        elbow_row    = _interpolate_at_discount(ladder_df, elbow["discount_pct"])

        vol_change_pct = 0;  rev_change_pct = 0
        margin_change  = 0;  monthly_savings = 0

        if current_row and elbow_row:
            if current_row["units"] > 0:
                vol_change_pct = ((elbow_row["units"] - current_row["units"]) / current_row["units"]) * 100
            if current_row["daily_revenue"] > 0:
                rev_change_pct = ((elbow_row["daily_revenue"] - current_row["daily_revenue"]) / current_row["daily_revenue"]) * 100
            margin_change   = (elbow_row["contribution_margin"] - current_row["contribution_margin"]) * 30
            monthly_savings = (current_row["discount_cost"] - elbow_row["discount_cost"]) * 30

        elbow_price = elbow_row["selling_price"] if elbow_row else mrp * (1 - elbow["discount_pct"] / 100)

        results.append({
            "product_id":         cell["product_id"],
            "grammage":           cell.get("grammage"),
            "city":               cell["city"],
            "category":           cell["category"],
            "title":              cell["title"],
            "mrp":                mrp,
            "cell_id":            cell["cell_id"],
            "confidence":         cell["confidence"],
            "quality_note":       cell.get("quality_note", ""),
            # Model outputs
            "price_elasticity":   cell.get("price_elasticity", cell.get("elasticity", -1.5)),
            "badge_sensitivity":  cell.get("badge_sensitivity", cell.get("discount_sensitivity", 0)),
            "elasticity":         cell.get("price_elasticity", cell.get("elasticity", -1.5)),
            "discount_sensitivity": cell.get("badge_sensitivity", cell.get("discount_sensitivity", 0)),
            "n_observations":     cell["n_observations"],
            # Current state
            "current_discount_pct":  round(current_disc, 1),
            "current_selling_price": round(current_price, 2),
            "current_price":         round(current_price, 2),    # alias
            "current_units_day":     round(current_row["units"], 1)         if current_row else 0,
            "current_revenue_day":   round(current_row["daily_revenue"], 0)  if current_row else 0,
            "current_margin_day":    round(current_row["contribution_margin"], 0) if current_row else 0,
            # Recommended state (at elbow)
            "elbow_discount_pct":    elbow["discount_pct"],
            "elbow_selling_price":   round(float(elbow_price), 2),
            "elbow_price":           round(float(elbow_price), 2),  # alias
            "elbow_units_day":       round(elbow_row["units"], 1)         if elbow_row else 0,
            "elbow_revenue_day":     round(elbow_row["daily_revenue"], 0)  if elbow_row else 0,
            "elbow_margin_day":      round(elbow_row["contribution_margin"], 0) if elbow_row else 0,
            "elbow_marginal_roi":    round(elbow["marginal_roi"], 2),
            # Changes
            "vol_change_pct":        round(vol_change_pct, 1),
            "rev_change_pct":        round(rev_change_pct, 1),
            "margin_change_monthly": round(margin_change, 0),
            "monthly_savings":       round(monthly_savings, 0),
            # Full ladder
            "ladder":       ladder,
            "curve_points": cell["curve_points"],
            "curve_params": cell["curve_params"],
        })

    results_df = pd.DataFrame(results)

    total_savings = results_df["monthly_savings"].sum()
    print(f"  [Stage 6] Total monthly savings potential: ₹{total_savings:,.0f}")
    print(f"    Cells with savings >₹10K/mo: {(results_df['monthly_savings'] > 10000).sum()}")

    return results_df


def _get_costs(product_id, mrp, master_costs=None):
    """Get cost structure for a SKU. Uses master data if available, else defaults."""
    if master_costs is not None and not master_costs.empty:
        row = master_costs[master_costs["product_id"] == product_id]
        if not row.empty:
            r = row.iloc[0]
            return {
                "cogs": r.get("cogs", mrp * cfg.DEFAULT_COGS_PCT),
                "commission_pct": r.get("commission_pct", cfg.DEFAULT_COMMISSION_PCT),
                "fulfillment": r.get("fulfillment_fee", cfg.DEFAULT_FULFILLMENT_FEE),
            }
    return {
        "cogs": mrp * cfg.DEFAULT_COGS_PCT,
        "commission_pct": cfg.DEFAULT_COMMISSION_PCT,
        "fulfillment": cfg.DEFAULT_FULFILLMENT_FEE,
    }


def _find_elbow(ladder_df, threshold):
    """Find the discount level where marginal ROI crosses the threshold."""
    valid = ladder_df[
        (ladder_df["marginal_roi"] != np.inf) &
        (~ladder_df["marginal_roi"].isna()) &
        (ladder_df["discount_pct"] > 0)
    ]

    if valid.empty:
        return {"discount_pct": 0, "marginal_roi": 0}

    # Find last point where marginal_roi >= threshold
    above = valid[valid["marginal_roi"] >= threshold]
    if above.empty:
        # All below threshold — recommend 0% discount
        return {"discount_pct": 0, "marginal_roi": 0}

    elbow_row = above.iloc[-1]  # Last point still above threshold
    return {
        "discount_pct": round(float(elbow_row["discount_pct"]), 1),
        "marginal_roi": round(float(elbow_row["marginal_roi"]), 2),
    }


def _interpolate_at_discount(ladder_df, target_disc):
    """Get economics at a specific discount level (nearest point)."""
    if ladder_df.empty:
        return None
    idx = (ladder_df["discount_pct"] - target_disc).abs().idxmin()
    row = ladder_df.iloc[idx]
    return {
        "units":               float(row["units"]),
        "daily_revenue":       float(row["daily_revenue"]),
        "contribution_margin": float(row["contribution_margin"]),
        "discount_cost":       float(row["discount_cost"]),
        "selling_price":       float(row.get("selling_price", row.get("price", 0))),
    }

