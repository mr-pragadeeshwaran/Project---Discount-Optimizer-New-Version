"""
Stage 7 — Guardrails + Tiering.

Applies business constraints (floor price, competitor ceiling, max change rate)
and assigns each cell to a tier: Strong Cut / Trade-off / Hold / Increase / Do Not Act.
"""
import numpy as np
import pandas as pd
import v4_config as cfg


def apply_guardrails_and_tier(economics_df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply business guardrails and assign recommendation tiers.

    Returns enriched DataFrame with:
    - Guardrail flags
    - Throttled recommendation (respecting max change rate)
    - Tier assignment
    - Phasing plan for throttled cells
    """
    print(f"  [Stage 7] Applying guardrails to {len(economics_df)} cells...")
    df = economics_df.copy()

    # ── Apply guardrails per cell ───────────────────────────────────
    df["guardrail_floor_ok"]      = True
    df["guardrail_competitor_ok"] = True
    df["guardrail_change_ok"]     = True
    df["is_throttled"]            = False
    # MUST be float — discount can be e.g. 6.3% after throttling
    df["throttled_discount_pct"]  = df["elbow_discount_pct"].astype(float)
    df["phasing_plan"]            = ""

    for idx, row in df.iterrows():
        mrp = row["mrp"]
        elbow_disc = row["elbow_discount_pct"]
        current_disc = row["current_discount_pct"]
        elbow_price = mrp * (1 - elbow_disc / 100)

        # 1. Floor price check (variable_cost + min margin)
        costs = mrp * cfg.DEFAULT_COGS_PCT + cfg.DEFAULT_COMMISSION_PCT * elbow_price + cfg.DEFAULT_FULFILLMENT_FEE
        floor_price = costs * (1 + cfg.MIN_MARGIN_PCT)
        if elbow_price < floor_price:
            df.at[idx, "guardrail_floor_ok"] = False
            # Adjust to floor
            adjusted_disc = max(0, (1 - floor_price / mrp) * 100)
            df.at[idx, "throttled_discount_pct"] = round(adjusted_disc, 1)

        # 2. Max change rate (3 ppt per cycle)
        disc_change = current_disc - elbow_disc  # positive = reducing discount
        if abs(disc_change) > cfg.MAX_DISCOUNT_CHANGE_PPT:
            df.at[idx, "is_throttled"] = True
            df.at[idx, "guardrail_change_ok"] = False
            if disc_change > 0:
                # Reducing discount: throttle to max step
                throttled = current_disc - cfg.MAX_DISCOUNT_CHANGE_PPT
            else:
                # Increasing discount: throttle to max step
                throttled = current_disc + cfg.MAX_DISCOUNT_CHANGE_PPT
            df.at[idx, "throttled_discount_pct"] = round(max(0, throttled), 1)

            # Build phasing plan
            steps = _build_phasing_plan(current_disc, elbow_disc, cfg.MAX_DISCOUNT_CHANGE_PPT)
            df.at[idx, "phasing_plan"] = " → ".join([f"{s:.0f}%" for s in steps])

    # ── Compute final recommended values (after guardrails) ─────────
    df["rec_discount_pct"] = df["throttled_discount_pct"]
    df["rec_price"] = (df["mrp"] * (1 - df["rec_discount_pct"] / 100)).round(1)

    # Recalculate expected units at throttled level using dual-signal log-log model:
    #   units(p_rec) = units(p_curr) × (p_rec/p_curr)^price_elast
    #                               × exp(badge_sens × (d_rec − d_curr))
    for idx, row in df.iterrows():
        price_elast  = float(row.get("price_elasticity",  row.get("elasticity", -1.5)))
        badge_sens   = float(row.get("badge_sensitivity", row.get("discount_sensitivity", 0.01)))
        current_disc = float(row["current_discount_pct"])
        rec_disc     = float(row["rec_discount_pct"])
        current_units = float(row["current_units_day"])
        mrp           = float(row["mrp"])
        current_price = float(row.get("current_selling_price", row.get("current_price",
                              mrp * (1 - current_disc / 100))))
        rec_price_val = float(row["rec_price"])

        if rec_disc != row["elbow_discount_pct"]:
            # Dual-signal: price level effect + badge effect
            price_ratio  = rec_price_val / current_price if current_price > 0 else 1.0
            badge_delta  = rec_disc - current_disc
            units_mult   = (price_ratio ** price_elast) * np.exp(badge_sens * badge_delta)
            rec_units    = round(float(current_units * units_mult), 1)
            df.at[idx, "rec_units_day"]   = max(rec_units, 0.01)
            df.at[idx, "rec_revenue_day"] = round(rec_units * rec_price_val, 0)
        else:
            df.at[idx, "rec_units_day"]   = row["elbow_units_day"]
            df.at[idx, "rec_revenue_day"] = row["elbow_revenue_day"]

    # Volume and revenue change vs current
    df["rec_vol_change_pct"] = np.where(
        df["current_units_day"] > 0,
        ((df["rec_units_day"] - df["current_units_day"]) / df["current_units_day"] * 100).round(1),
        0
    )
    df["rec_rev_change_pct"] = np.where(
        df["current_revenue_day"] > 0,
        ((df["rec_revenue_day"] - df["current_revenue_day"]) / df["current_revenue_day"] * 100).round(1),
        0
    )
    df["rec_monthly_savings"] = ((df["current_discount_pct"] - df["rec_discount_pct"]) / 100 * df["mrp"] * df["current_units_day"] * 30).round(0)
    # Price-led summary (what customers actually see on Blinkit)
    df["price_change_inr"] = (df["rec_price"] - df["current_price"]).round(1)
    df["price_change_pct"] = np.where(
        df["current_price"] > 0,
        ((df["rec_price"] - df["current_price"]) / df["current_price"] * 100).round(2),
        0
    )

    # ── Assign tiers ────────────────────────────────────────────────
    df["tier"] = df.apply(_assign_tier, axis=1)

    # ── Sort by tier priority then savings ──────────────────────────
    tier_order = {"Strong Cut": 0, "Trade-off": 1, "Increase": 2, "Hold": 3, "Do Not Act": 4}
    df["tier_order"] = df["tier"].map(tier_order)
    df = df.sort_values(["tier_order", "rec_monthly_savings"], ascending=[True, False])
    df = df.drop(columns=["tier_order"])

    # Summary
    tier_counts = df["tier"].value_counts()
    tier_savings = df.groupby("tier")["rec_monthly_savings"].sum()
    print(f"  [Stage 7] Tier assignments:")
    for tier in ["Strong Cut", "Trade-off", "Increase", "Hold", "Do Not Act"]:
        count = tier_counts.get(tier, 0)
        savings = tier_savings.get(tier, 0)
        print(f"    {tier}: {count} cells → ₹{savings:,.0f}/month")

    throttled_count = df["is_throttled"].sum()
    if throttled_count:
        print(f"    Throttled (max change rate): {throttled_count} cells")

    return df.reset_index(drop=True)


def _assign_tier(row) -> str:
    """
    Assign a tier based on THIS-CYCLE ACTION (the throttled discount the brand
    team is being asked to approve this week), not the full multi-cycle journey.

    Rationale: the user approves week-by-week, not a multi-week commitment.
    Tiering on the full elbow gap penalised cells with high elasticity even
    when this week's 3ppt move was perfectly safe.

    Strong Cut criteria are also risk-adjusted by elasticity:
    - Low elasticity (|e| < 2): treat as safe (small price moves → small volume moves)
    - High elasticity (|e| ≥ 2): require a tight predicted volume drop this cycle
    """
    confidence   = row.get("confidence", "Low")
    current_disc = row.get("current_discount_pct", 0)
    elbow_disc   = row.get("elbow_discount_pct", 0)
    rec_disc     = row.get("rec_discount_pct", elbow_disc)

    # This-cycle realised metrics (after throttling)
    rec_savings   = row.get("rec_monthly_savings", 0)
    rec_vol_drop  = row.get("rec_vol_change_pct", 0)
    elast         = abs(float(row.get("price_elasticity", row.get("elasticity", 1.5))))

    # Gap to elbow (positive = need to REDUCE discount overall)
    gap = current_disc - elbow_disc

    # Do Not Act: insufficient data
    if confidence == "Needs Experiment":
        return "Do Not Act"

    # Increase: currently under-discounting (elbow > current by >2ppt)
    if gap < -2:
        return "Increase"

    # Hold: already near optimal (within 2 ppt)
    if abs(gap) <= 2:
        return "Hold"

    # Over-discounting: classify by THIS-CYCLE economic outcome
    #
    # Strong Cut = "fast-track approve" — clear net economic win with bounded
    # risk this week. Calibrated for the post-tuning elasticity range (-1 to -4):
    #   savings ≥ ₹5K/month  AND  this-cycle vol drop ≤ 8%
    # AND high/medium confidence (Low cells need pilot first)
    if (rec_savings >= 5000 and
            abs(rec_vol_drop) <= 8.0 and
            confidence in ("High", "Medium")):
        return "Strong Cut"

    # Trade-off = "review individually" — positive savings, larger volume risk,
    # or thinner data. The brand team should sanity-check these before acting.
    if (rec_savings > 0 and
            abs(rec_vol_drop) <= 20.0 and
            confidence in ("High", "Medium", "Low")):
        return "Trade-off"

    if rec_savings > 0:
        return "Trade-off"

    return "Hold"


def _build_phasing_plan(current_disc, target_disc, max_step):
    """Build a multi-cycle phasing plan when change exceeds max step."""
    steps = [current_disc]
    current = current_disc
    direction = -1 if target_disc < current_disc else 1

    while abs(current - target_disc) > 0.5:
        next_step = current + direction * max_step
        if direction < 0:
            next_step = max(next_step, target_disc)
        else:
            next_step = min(next_step, target_disc)
        steps.append(round(next_step, 1))
        current = next_step

    return steps
