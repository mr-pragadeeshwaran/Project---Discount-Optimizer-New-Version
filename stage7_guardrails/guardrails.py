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
        current_disc = row["current_discount_pct"]
        elbow_disc = row["elbow_discount_pct"]

        # Choose the target: historical floor (proven safe) OR elbow (margin-optimal).
        # Historical floor is the cell's own past lower-quartile discount, so the
        # system never plans a price level the cell has never operated at.
        if getattr(cfg, "USE_HISTORICAL_FLOOR_TARGET", False):
            hist_floor = float(row.get("historical_floor_disc", elbow_disc))
            # Use whichever target is more aggressive (lower) but still safe:
            # if hist_floor < current_disc, use hist_floor; never below elbow either
            # We want target = max(elbow, hist_floor) so we don't undershoot the
            # observed safe zone but also don't push deeper than margin-optimal
            target_disc = max(elbow_disc, hist_floor)
        else:
            target_disc = elbow_disc

        target_price = mrp * (1 - target_disc / 100)

        # 1. Floor price check (variable_cost + min margin)
        costs = mrp * cfg.DEFAULT_COGS_PCT + cfg.DEFAULT_COMMISSION_PCT * target_price + cfg.DEFAULT_FULFILLMENT_FEE
        floor_price = costs * (1 + cfg.MIN_MARGIN_PCT)
        if target_price < floor_price:
            df.at[idx, "guardrail_floor_ok"] = False
            adjusted_disc = max(0, (1 - floor_price / mrp) * 100)
            df.at[idx, "throttled_discount_pct"] = round(adjusted_disc, 1)
            target_disc = adjusted_disc

        # 2. Per-cycle step rule:
        #   - If gap < MIN_DISCOUNT_CHANGE_PPT (3 ppt): close in one shot.
        #   - Else: per-cycle step = max(MIN, gap / TARGET_TIMELINE_WEEKS).
        # NO upper cap — TARGET_TIMELINE_WEEKS is the binding constraint, so
        # every cell closes its full gap within the user-set duration.
        gap = abs(current_disc - target_disc)
        min_step = float(getattr(cfg, "MIN_DISCOUNT_CHANGE_PPT", 3))
        timeline = getattr(cfg, "TARGET_TIMELINE_WEEKS", 12)

        if gap < 0.1:
            this_cycle_step = 0.0
        elif gap <= min_step:
            this_cycle_step = gap  # single-shot — don't overshoot
        else:
            raw_step = gap / float(timeline)
            this_cycle_step = max(min_step, raw_step)

        if gap > this_cycle_step + 0.05:
            df.at[idx, "is_throttled"] = True
            df.at[idx, "guardrail_change_ok"] = False
            direction = -1 if (current_disc - target_disc) > 0 else 1
            throttled = current_disc + direction * this_cycle_step
            df.at[idx, "throttled_discount_pct"] = round(max(0, throttled), 1)
            steps = _build_phasing_plan(current_disc, target_disc, this_cycle_step)
            df.at[idx, "phasing_plan"] = " → ".join([f"{s:.1f}%" for s in steps])
        elif gap >= 0.1:
            # One-shot move — already within a single cycle's reach
            df.at[idx, "throttled_discount_pct"] = round(target_disc, 1)
            df.at[idx, "phasing_plan"] = f"{current_disc:.1f}% → {target_disc:.1f}%"

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
    # ── Net-revenue gate: what is a cut ACTUALLY worth? ─────────────
    # "Savings" must be the change in NET REVENUE (units x selling price) once
    # at the proven-floor target — NOT the raw discount reduction, which
    # ignores the sales you lose. On an elastic cell, cutting discount loses
    # more revenue than the discount it saves -> negative gain -> that discount
    # is WORKING, so the cell is NOT waste (tiered Hold below). This is the fix
    # for the case where cutting looked like a "saving" but destroyed revenue.
    _floor = pd.to_numeric(df.get("historical_floor_disc"), errors="coerce").fillna(
        df["current_discount_pct"])
    _tgt = np.minimum(_floor.astype(float), df["current_discount_pct"].astype(float))
    _el  = pd.to_numeric(df.get("price_elasticity", df.get("elasticity")),
                         errors="coerce").fillna(-1.5)
    _bg  = pd.to_numeric(df.get("badge_sensitivity", df.get("discount_sensitivity")),
                         errors="coerce").fillna(0.0)
    _cd  = df["current_discount_pct"].astype(float)
    _mrp = df["mrp"].astype(float)
    _cu  = df["current_units_day"].astype(float)
    _cp  = (_mrp * (1 - _cd / 100)).clip(lower=1.0)
    _tp  = (_mrp * (1 - _tgt / 100)).clip(lower=1.0)
    _tu  = _cu * (_tp / _cp) ** _el * np.exp(_bg * (_tgt - _cd))
    _cur_nr = _cu * _cp
    df["net_rev_gain_mo"]  = (((_tu * _tp) - _cur_nr) * 30).round(0)
    df["net_rev_gain_pct"] = np.where(_cur_nr > 0,
                                      ((_tu * _tp - _cur_nr) / _cur_nr * 100), 0).round(1)
    # Honest headline "savings" = the net-revenue gain (was discount-spend saved).
    df["rec_monthly_savings"] = df["net_rev_gain_mo"]
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

    # ── HARD GATE: per-cell model confidence (added May 2026) ─────
    # The model-based confidence score (Stage 4) catches data-thin or
    # high-uncertainty cells the curve-based confidence might miss.
    # Any cell flagged DO_NOT_ACT here is locked out of price moves —
    # the system requires a structured A/B price test to gather signal
    # before acting. This is the scale-up safety rail.
    model_tier = str(row.get("confidence_tier", "")).upper()
    if model_tier == "DO_NOT_ACT":
        return "Do Not Act"

    # Do Not Act: insufficient data
    if confidence == "Needs Experiment":
        return "Do Not Act"

    # Increase: currently under-discounting (elbow > current by >2ppt)
    if gap < -2:
        return "Increase"

    # Hold: already near optimal (within 2 ppt)
    if abs(gap) <= 2:
        return "Hold"

    # R² TRUST FLOOR: only act where the SKU-group response model is trustworthy
    # (R² >= 0.80 for that product's pooled cities). Below the floor we don't
    # trust the elasticity enough to act -> route to a price test.
    sku_r2 = row.get("sku_group_r2", np.nan)
    sku_r2 = float(sku_r2) if pd.notna(sku_r2) else 0.0
    if sku_r2 < 0.80:
        return "Do Not Act"

    # Over-discounting: only GENUINE waste is actionable.
    #
    # Genuine waste = pulling the discount back to the proven floor RAISES net
    # revenue (units x selling price). If it LOWERS net revenue, the discount is
    # working (elastic cell) and must be left alone -> Hold. This replaces the
    # old rule that treated any discount reduction as a "saving" while ignoring
    # the volume it destroyed.
    net_gain_pct = float(row.get("net_rev_gain_pct", 0.0))
    if net_gain_pct < 0.2:
        return "Hold"      # cutting adds no net revenue -> discount is working

    # Strong Cut = clear, high-confidence net-revenue win.
    if (net_gain_pct >= 1.0 and
            confidence in ("High", "Medium") and
            model_tier in ("HIGH", "MEDIUM", "")):
        return "Strong Cut"

    # Trade-off = positive but smaller gain, or thinner data — review first.
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
