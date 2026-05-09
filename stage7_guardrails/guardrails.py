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
    df["guardrail_floor_ok"] = True
    df["guardrail_competitor_ok"] = True
    df["guardrail_change_ok"] = True
    df["is_throttled"] = False
    df["throttled_discount_pct"] = df["elbow_discount_pct"]
    df["phasing_plan"] = ""

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

    # Recalculate expected impact at throttled level
    for idx, row in df.iterrows():
        if row["rec_discount_pct"] != row["elbow_discount_pct"]:
            # Simple elasticity-based adjustment
            price_ratio = row["rec_price"] / row["current_price"] if row["current_price"] > 0 else 1
            elast = row["elasticity"]
            units_ratio = price_ratio ** elast if price_ratio > 0 else 1
            rec_units = round(row["current_units_day"] * units_ratio, 1)
            df.at[idx, "rec_units_day"] = rec_units
            df.at[idx, "rec_revenue_day"] = round(rec_units * row["rec_price"], 0)
        else:
            df.at[idx, "rec_units_day"] = row["elbow_units_day"]
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
    """Assign a tier based on economics, confidence, and guardrails."""
    savings = row.get("rec_monthly_savings", 0)
    vol_drop = row.get("rec_vol_change_pct", 0)
    confidence = row.get("confidence", "Low")
    current_disc = row.get("current_discount_pct", 0)
    elbow_disc = row.get("elbow_discount_pct", 0)
    elbow_roi = row.get("elbow_marginal_roi", 0)

    # Do Not Act: low confidence or needs experiment
    if confidence == "Needs Experiment":
        return "Do Not Act"

    # Increase: currently under-discounted (elbow > current discount)
    if elbow_disc > current_disc + 2:
        return "Increase"

    # Hold: already near elbow (within 2 ppt)
    if abs(current_disc - elbow_disc) <= 2:
        return "Hold"

    # Strong Cut: high savings, low volume risk, high confidence
    if (savings >= cfg.TIER_STRONG_CUT_MIN_SAVINGS and
            abs(vol_drop) <= cfg.TIER_STRONG_CUT_MAX_VOL_DROP * 100 and
            confidence in ("High", "Medium")):
        return "Strong Cut"

    # Trade-off: meaningful savings but moderate risk
    if (savings > 0 and
            abs(vol_drop) <= cfg.TIER_TRADEOFF_MAX_VOL_DROP * 100):
        return "Trade-off"

    # Default: Hold if nothing else matches
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
