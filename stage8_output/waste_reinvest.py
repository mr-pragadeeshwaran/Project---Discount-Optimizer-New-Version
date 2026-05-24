"""
Stage 8 — Waste & Reinvestment Output Layer.

Reads Stage 7 recommendations + Stage 3 features to produce:
  1. Markdown report (WASTE_REINVEST_REPORT.md)
  2. Two CSVs (waste.csv, reinvest.csv)
  3. JSON (per_cell_detail.json)
"""
import os, json, warnings
import numpy as np
import pandas as pd
import v4_config as cfg


class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer,)): return int(obj)
        if isinstance(obj, (np.floating,)): return float(obj)
        if isinstance(obj, np.ndarray): return obj.tolist()
        if isinstance(obj, (pd.Timestamp,)): return str(obj)
        return super().default(obj)


def generate_waste_reinvest_report(rec_df, feat_df, model_output, run_dir):
    """Main entry point for Stage 8."""
    C = cfg.COL
    print("  [Stage 8] Building Waste & Reinvestment report...")

    df = rec_df.copy()

    # Recompute 30-day regular-day-only averages from feat_df
    df = _enrich_with_recent_data(df, feat_df)

    # Trust Stage 5's confidence assessment (which already applies the right
    # checks: boundary-hit elasticities, launch-ramp growth confounding,
    # min observations). Earlier this stage rebuilt confidence with looser
    # rules and silently upgraded launch-ramp cells back to 'High' — which
    # then put them at the top of the waste table. Don't overwrite anymore.
    if "confidence" not in df.columns:
        df["confidence"] = "Low"
    df["confidence"] = df["confidence"].fillna("Low")

    # Split into waste (Q1) and reinvest (Q2)
    waste_all = _build_waste_table(df)
    reinvest_all = _build_reinvest_table(df, waste_all)

    # Separate Low confidence into "Needs Price Test"
    waste_main = waste_all[waste_all["confidence"] != "Low"].copy()
    reinvest_main = reinvest_all[reinvest_all["confidence"] != "Low"].copy()
    needs_test = pd.concat([
        waste_all[waste_all["confidence"] == "Low"],
        reinvest_all[reinvest_all["confidence"] == "Low"],
    ]).drop_duplicates(subset=["cell_id"])

    summary = _build_summary(waste_all, reinvest_all, waste_main, df_all=df)

    # Print summary
    print(f"    Waste cells (High/Med): {len(waste_main)} | Reinvest cells (High/Med): {len(reinvest_main)}")
    print(f"    Needs Price Test: {len(needs_test)} cells")
    print(f"    Total wasted discount: Rs.{summary['total_wasted']:,.0f}/month")
    print(f"    Total reinvestment opportunity: Rs.{summary['total_reinvest']:,.0f}/month")
    fw = summary.get("flywheel", {})
    if fw.get("current_weighted_discount_pct") is not None:
        print(f"    Weighted discount: current={fw['current_weighted_discount_pct']:.2f}% "
              f"→ after_cuts={fw['after_cuts_weighted_discount_pct']:.2f}% "
              f"→ after_cuts_and_reinvest={fw['after_cuts_and_reinvest_weighted_discount_pct']:.2f}% "
              f"(target={fw['target_weighted_discount_pct']:.1f}%)")

    # Write outputs
    md_path  = _write_markdown(summary, waste_main, reinvest_main, needs_test, run_dir)
    pdf_path = _write_pdf(summary, waste_main, reinvest_main, needs_test, run_dir)
    _write_csvs(waste_all, reinvest_all, run_dir)
    _write_json(df, model_output, summary, run_dir)

    print(f"  [Stage 8] Markdown report: {md_path}")
    if pdf_path:
        print(f"  [Stage 8] PDF report:      {pdf_path}")
    return {"markdown": md_path, "pdf": pdf_path,
            "waste_csv": os.path.join(run_dir, "waste.csv"),
            "reinvest_csv": os.path.join(run_dir, "reinvest.csv"),
            "json": os.path.join(run_dir, "per_cell_detail.json")}


def _enrich_with_recent_data(df, feat_df):
    """Recompute last-30-day regular-day-only discount averages."""
    C = cfg.COL

    # Pre-cast to float to avoid pandas dtype errors when assigning float to int columns
    for col in ["current_discount_pct", "elbow_discount_pct", "current_units_day",
                 "monthly_savings", "margin_change_monthly", "vol_change_pct"]:
        if col in df.columns:
            df[col] = df[col].astype(float)
    df["monthly_units"] = 0.0

    feat = feat_df.copy()
    feat[C["date"]] = pd.to_datetime(feat[C["date"]])
    max_date = feat[C["date"]].max()
    cutoff = max_date - pd.Timedelta(days=30)

    recent = feat[(feat[C["date"]] >= cutoff) & (feat["is_regular_day"] == 1)]

    for idx, row in df.iterrows():
        pid, city = row["product_id"], row["city"]
        grammage = row.get("grammage", None)

        mask = (
            (recent[C["product_id"]] == pid) &
            (recent[C["city"]] == city)
        )
        if grammage and C["grammage"] in recent.columns:
            mask = mask & (recent[C["grammage"]] == grammage)

        cell_recent = recent[mask]
        if not cell_recent.empty:
            df.at[idx, "current_discount_pct"] = round(float(cell_recent["discount_pct"].mean()), 1)
            df.at[idx, "monthly_units"] = round(float(cell_recent[C["offtake_qty"]].mean() * 30), 0)
        else:
            df.at[idx, "monthly_units"] = round(float(row.get("current_units_day", 0)) * 30, 0)

    return df


def _compute_cell_confidence(row, feat_df, model_output):
    """
    Stage 8 confidence scoring — aligned with Stage 5's criteria.

    Thresholds (relaxed from original spec to reflect semi-log model reality):
      High:   n_valid >= 60  AND  n_disc_levels >= 5   AND  disc_std >= 2.0  AND  elbow_stable
      Medium: n_valid >= 30  AND  n_disc_levels >= 3   AND  disc_std >= 1.0
      Low:    anything else with positive discount sensitivity

    Key change: removed per-cell R2 gate (too noisy at the individual cell level;
    global model R2 is the right quality check). Replaced n_prices (PRICE nunique)
    with n_disc_levels (distinct discount % levels) which is the real signal.
    """
    C = cfg.COL
    pid, city = row["product_id"], row["city"]
    grammage = row.get("grammage", None)

    mask = (
        (feat_df[C["product_id"]] == pid) &
        (feat_df[C["city"]] == city) &
        (feat_df["is_regular_day"] == 1)
    )
    if grammage and C["grammage"] in feat_df.columns:
        mask = mask & (feat_df[C["grammage"]] == grammage)

    cell_data = feat_df[mask]
    if cell_data.empty:
        return "Low"

    n_valid = len(cell_data)
    n_disc_levels = int(cell_data["discount_pct"].round(0).nunique()) \
        if "discount_pct" in cell_data.columns else 0
    disc_std = float(cell_data["discount_pct"].std()) \
        if "discount_pct" in cell_data.columns else 0

    # Discount sensitivity must be positive (more discount -> more units)
    disc_sensitivity = float(row.get("discount_sensitivity", 0))
    if disc_sensitivity <= 0:
        return "Low"

    # Elbow stability check (bootstrap 10x)
    elbow_stable = _check_elbow_stability(row, cell_data)

    if n_valid >= 60 and n_disc_levels >= 5 and disc_std >= 2.0 and elbow_stable:
        return "High"
    elif n_valid >= 30 and n_disc_levels >= 3 and disc_std >= 1.0:
        return "Medium"
    return "Low"


def _compute_per_cell_r2(cell_data, model_output):
    """Compute approximate per-cell R2 using model predictions vs actuals."""
    if model_output is None:
        return 0.0
    try:
        model = model_output["model"]
        y_actual = cell_data["log_units"].values
        y_pred = model.predict(cell_data).values
        ss_res = np.sum((y_actual - y_pred) ** 2)
        ss_tot = np.sum((y_actual - y_actual.mean()) ** 2)
        if ss_tot == 0:
            return 0.0
        return max(0, 1 - ss_res / ss_tot)
    except Exception:
        return 0.0


def _check_elbow_stability(row, cell_data, n_bootstrap=10):
    """
    Bootstrap elbow stability using semi-log model.
    Resamples cell data 10x and checks if the elbow discount level
    is stable (std < 2.5 ppt across bootstraps).
    """
    disc_sensitivity = float(row.get("discount_sensitivity", 0))
    stable_mrp = float(row.get("mrp", row.get("stable_mrp", 100)))
    base_units = float(row.get("current_units_day", row.get("avg_units_baseline", 1)))
    avg_disc = float(row.get("current_discount_pct", row.get("avg_discount_pct", 10)))

    if disc_sensitivity <= 0 or base_units <= 0:
        return False

    elbows = []
    rng = np.random.RandomState(42)
    for _ in range(n_bootstrap):
        sample = cell_data.sample(frac=1.0, replace=True, random_state=rng)
        sample_base_units = float(sample[cfg.COL["offtake_qty"]].mean())
        sample_avg_disc   = float(sample["discount_pct"].mean()) \
            if "discount_pct" in sample.columns else avg_disc

        if sample_base_units <= 0:
            continue

        elbow_d = _find_elbow_for_bootstrap_semilog(
            stable_mrp, disc_sensitivity, sample_avg_disc, sample_base_units
        )
        elbows.append(elbow_d)

    if len(elbows) < 5:
        return False
    return np.std(elbows) < 2.5


def _find_elbow_for_bootstrap(mrp, elasticity, base_price, base_units):
    """Find elbow discount for a bootstrap sample."""
    disc_range = np.arange(0, 31, 1)
    prev_margin = None
    prev_dcost = None
    elbow = 0

    for d in disc_range:
        price = mrp * (1 - d / 100)
        ratio = price / base_price if base_price > 0 else 1
        units = base_units * (max(ratio, 0.01) ** elasticity) if ratio > 0 else base_units
        units = max(units, 0)

        vc = mrp * cfg.DEFAULT_COGS_PCT + cfg.DEFAULT_COMMISSION_PCT * price + cfg.DEFAULT_FULFILLMENT_FEE
        margin = (price - vc) * units
        dcost = (mrp - price) * units

        if prev_margin is not None:
            delta_m = margin - prev_margin
            delta_d = dcost - prev_dcost
            if delta_d > 0:
                mroi = delta_m / delta_d
                if mroi >= cfg.MARGINAL_ROI_THRESHOLD:
                    elbow = d
        prev_margin, prev_dcost = margin, dcost

    return elbow


def _find_elbow_for_bootstrap_semilog(stable_mrp, disc_sensitivity, avg_disc, base_units):
    """
    Find elbow discount using the semi-log model for a bootstrap sample.
    units(d) = base_units * exp(disc_sensitivity * (d - avg_disc))
    """
    disc_range = np.arange(0, 51, 1)
    prev_margin = None
    prev_dcost = None
    elbow = 0

    for d in disc_range:
        price = stable_mrp * (1 - d / 100)
        units = base_units * np.exp(disc_sensitivity * (d - avg_disc))
        units = max(units, 0.01)

        vc = stable_mrp * cfg.DEFAULT_COGS_PCT + cfg.DEFAULT_COMMISSION_PCT * price + cfg.DEFAULT_FULFILLMENT_FEE
        margin = (price - vc) * units
        dcost = (stable_mrp - price) * units

        if prev_margin is not None:
            delta_m = margin - prev_margin
            delta_d = dcost - prev_dcost
            if delta_d > 0:
                mroi = delta_m / delta_d
                if mroi >= cfg.MARGINAL_ROI_THRESHOLD:
                    elbow = d
        prev_margin, prev_dcost = margin, dcost

    return elbow



def _build_waste_table(df):
    """Q1: Where am I overspending on discount?

    Excludes 'Needs Experiment' cells from the actionable table — those
    need a price test before any cut, so they belong in 'Needs Price Test'
    rather than as a savings line item.
    """
    waste_mask = (
        (df["current_discount_pct"] > df["elbow_discount_pct"]) &
        (df["confidence"] != "Needs Experiment")
    )
    waste = df[waste_mask].copy()

    if waste.empty:
        return _empty_waste_df()

    waste["wasted_discount_pct"] = waste["current_discount_pct"] - waste["elbow_discount_pct"]
    waste["wasted_inr_per_month"] = (
        waste["wasted_discount_pct"] / 100 * waste["mrp"] * waste["monthly_units"]
    ).round(0)
    # Price-led view (what customers actually see on Blinkit)
    waste["current_price"]      = (waste["mrp"] * (1 - waste["current_discount_pct"] / 100)).round(1)
    waste["eventual_price"]     = (waste["mrp"] * (1 - waste["elbow_discount_pct"]   / 100)).round(1)
    waste["price_increase_inr"] = (waste["eventual_price"] - waste["current_price"]).round(1)
    # this_week_price is filled later by _apply_guardrails (throttled to 3 ppt/cycle)

    # Marginal ROI at current discount
    waste["marginal_roi_at_current"] = waste.apply(_marginal_roi_at_discount, args=("current",), axis=1)

    # Volume change if cutting to elbow
    waste["volume_change_pct"] = waste["vol_change_pct"]

    # Logic explanation
    waste["logic_explanation"] = waste.apply(
        lambda r: _generate_logic_explanation(r, "waste"), axis=1
    )

    # Apply guardrails
    waste = _apply_guardrails(waste)

    return waste.sort_values("wasted_inr_per_month", ascending=False).reset_index(drop=True)


def _build_reinvest_table(df, waste_df):
    """
    Q2: STRATEGIC reinvestment for the flywheel.

    The pure margin-optimal elbow puts ~every cell at 0% discount because any
    discount technically reduces margin per unit. That ignores the business
    reality: in growth-responsive cells, deeper discount drives meaningful
    volume that's worth the margin sacrifice for market share & brand presence.

    A cell qualifies as a growth-reinvest candidate when a +3 ppt discount move:
      - lifts volume by ≥ REINVEST_MIN_VOL_LIFT_PCT (default 5%)
      - sacrifices contribution margin by ≤ REINVEST_MAX_MARGIN_SAC_PCT (default 10%)
      - AND the cell has |elasticity| ≥ REINVEST_MIN_ELASTICITY (default 2.0)
      - AND confidence is High/Medium (data trusted)
      - AND current discount < category-mean (room to grow)

    Cells are ranked by VOLUME_LIFT_PER_RUPEE_BUDGET — best bang per ₹ invested.
    """
    if df.empty:
        return _empty_reinvest_df()

    # Per-category mean discount (used to identify "room to grow" cells)
    cat_mean_disc = df.groupby("category")["current_discount_pct"].mean().to_dict()

    candidates = []
    for _, row in df.iterrows():
        confidence  = row.get("confidence", "Low")
        if confidence not in ("High", "Medium"):
            continue

        elast = abs(float(row.get("price_elasticity", row.get("elasticity", 0))))
        if elast < cfg.REINVEST_MIN_ELASTICITY:
            continue

        cat = row.get("category", "")
        cur_d = float(row["current_discount_pct"])
        # Skip cells already above category mean discount — no headroom to grow
        if cur_d >= cat_mean_disc.get(cat, cur_d) + 1.0:
            continue

        sim = _simulate_discount_move(row, delta_ppt=+3.0)
        if sim is None:
            continue

        if sim["vol_lift_pct"] < cfg.REINVEST_MIN_VOL_LIFT_PCT:
            continue
        if sim["margin_sacrifice_pct"] > cfg.REINVEST_MAX_MARGIN_SAC_PCT:
            continue
        # Quality gate: don't reinvest into cells the model is unsure about
        qn = str(row.get("quality_note", "OK"))
        if "elasticity at floor" in qn:
            continue

        cand = row.to_dict()
        mrp_val = float(row.get("mrp", row.get("stable_mrp", 0)))
        cur_price_val = mrp_val * (1 - cur_d / 100)
        new_price_val = mrp_val * (1 - (cur_d + 3.0) / 100)
        cand.update({
            "recommended_discount_pct":          round(cur_d + 3.0, 1),
            "current_price":                     round(cur_price_val, 1),
            "new_price":                         round(new_price_val, 1),
            "price_drop_inr":                    round(cur_price_val - new_price_val, 1),
            "budget_needed_inr_per_month":       round(sim["extra_disc_cost_monthly"], 0),
            "expected_margin_lift_inr_per_month":round(sim["net_contribution_change_monthly"], 0),
            "volume_lift_pct":                   round(sim["vol_lift_pct"], 1),
            "margin_sacrifice_pct":              round(sim["margin_sacrifice_pct"], 1),
            "extra_volume_units_per_month":      round(sim["extra_units_monthly"], 0),
            "reinvestment_efficiency":           round(
                sim["extra_units_monthly"] / max(sim["extra_disc_cost_monthly"], 1.0) * 100, 2
            ),  # units gained per ₹100 of budget
            "marginal_roi_at_current":           round(sim["marginal_roi_now"], 2),
        })
        candidates.append(cand)

    if not candidates:
        return _empty_reinvest_df()

    reinvest = pd.DataFrame(candidates)
    # funded_by linkage: same SKU first, then any waste cell
    reinvest["funded_by"] = reinvest.apply(
        lambda r: _find_funding_sources(r, waste_df), axis=1
    )
    reinvest["logic_explanation"] = reinvest.apply(
        lambda r: _generate_reinvest_explanation(r), axis=1
    )
    reinvest = _apply_guardrails(reinvest)

    return reinvest.sort_values(
        "extra_volume_units_per_month", ascending=False
    ).reset_index(drop=True)


def _simulate_discount_move(row, delta_ppt: float) -> dict:
    """
    Simulate the economic outcome of moving this cell's discount by delta_ppt.
    Uses the dual-signal model (price level effect + badge effect) and the
    same cost structure as Stage 6.
    Returns dict of {vol_lift_pct, margin_sacrifice_pct,
                     net_contribution_change_monthly, extra_disc_cost_monthly,
                     extra_units_monthly, marginal_roi_now}, or None if
    something is missing.
    """
    try:
        mrp        = float(row.get("mrp", row.get("stable_mrp", 0)))
        if mrp <= 0: return None
        cur_d      = float(row["current_discount_pct"])
        new_d      = cur_d + delta_ppt
        cur_units  = float(row["current_units_day"])
        if cur_units <= 0: return None
        elast      = float(row.get("price_elasticity", row.get("elasticity", -1.5)))
        badge      = float(row.get("badge_sensitivity", row.get("discount_sensitivity", 0)))
        cur_price  = mrp * (1 - cur_d / 100)
        new_price  = mrp * (1 - new_d / 100)
        if cur_price <= 0 or new_price <= 0: return None

        # Dual-signal volume change
        price_ratio = new_price / cur_price
        new_units   = cur_units * (price_ratio ** elast) * np.exp(badge * delta_ppt)
        if new_units <= 0: return None

        # Costs
        vc_cur = mrp * cfg.DEFAULT_COGS_PCT + cfg.DEFAULT_COMMISSION_PCT * cur_price + cfg.DEFAULT_FULFILLMENT_FEE
        vc_new = mrp * cfg.DEFAULT_COGS_PCT + cfg.DEFAULT_COMMISSION_PCT * new_price + cfg.DEFAULT_FULFILLMENT_FEE
        cm_cur = (cur_price - vc_cur) * cur_units * 30  # monthly
        cm_new = (new_price - vc_new) * new_units * 30
        disc_cost_cur = (mrp - cur_price) * cur_units * 30
        disc_cost_new = (mrp - new_price) * new_units * 30

        vol_lift_pct      = (new_units - cur_units) / cur_units * 100
        contribution_change = cm_new - cm_cur
        margin_sacrifice_pct = -contribution_change / max(cm_cur, 1.0) * 100  # +ve if losing
        extra_disc_cost   = disc_cost_new - disc_cost_cur
        extra_units       = (new_units - cur_units) * 30

        # Marginal ROI at current: δcontribution / δdiscount_cost
        marginal_roi_now = contribution_change / max(extra_disc_cost, 1e-6) if extra_disc_cost > 0 else 0.0

        return {
            "vol_lift_pct":                    vol_lift_pct,
            "margin_sacrifice_pct":            margin_sacrifice_pct,
            "net_contribution_change_monthly": contribution_change,
            "extra_disc_cost_monthly":         extra_disc_cost,
            "extra_units_monthly":             extra_units,
            "marginal_roi_now":                marginal_roi_now,
        }
    except Exception:
        return None


def _generate_reinvest_explanation(row) -> str:
    cur_p   = float(row.get("current_price", 0))
    new_p   = float(row.get("new_price",     0))
    drop    = float(row.get("price_drop_inr", cur_p - new_p))
    cur_d   = row["current_discount_pct"]
    new_d   = row["recommended_discount_pct"]
    vol     = row.get("volume_lift_pct", 0)
    units   = row.get("extra_volume_units_per_month", 0)
    budget  = row.get("budget_needed_inr_per_month", 0)
    sac     = row.get("margin_sacrifice_pct", 0)
    elast   = float(row.get("price_elasticity", row.get("elasticity", 0)))
    if sac > 0:
        margin_text = f"margin cost: {sac:.1f}% of current contribution"
    else:
        margin_text = f"margin actually GAINS {-sac:.1f}% — pure win"
    return (
        f"High-elasticity cell (|e|={abs(elast):.2f}) with room to grow. "
        f"Drop price Rs.{cur_p:.0f} -> Rs.{new_p:.0f} (Rs.{drop:.0f} cheaper, "
        f"= {cur_d:.0f}% -> {new_d:.0f}% off) projected to add {vol:.1f}% volume "
        f"({units:,.0f} extra units/month) for Rs.{budget:,.0f}/month extra discount "
        f"spend ({margin_text}). Strategic investment to grow share."
    )


def _marginal_roi_at_discount(row, which="current"):
    """Approximate marginal ROI at a given discount level using the ladder."""
    ladder = row.get("ladder", [])
    if not ladder:
        return 0.0
    target = row["current_discount_pct"] if which == "current" else row["elbow_discount_pct"]
    ldf = pd.DataFrame(ladder)
    idx = (ldf["discount_pct"] - target).abs().idxmin()
    if idx > 0:
        dm = ldf.loc[idx, "contribution_margin"] - ldf.loc[idx-1, "contribution_margin"]
        dd = ldf.loc[idx, "discount_cost"] - ldf.loc[idx-1, "discount_cost"]
        return round(dm / dd, 2) if dd > 0 else 0.0
    return 0.0


def _find_funding_sources(reinvest_row, waste_df):
    """Find waste cells that could fund this reinvestment. Same SKU first."""
    if waste_df.empty:
        return ""
    same_sku = waste_df[
        (waste_df["product_id"] == reinvest_row["product_id"]) &
        (waste_df["city"] != reinvest_row["city"])
    ]
    diff_sku = waste_df[waste_df["product_id"] != reinvest_row["product_id"]]
    sources = []
    for _, w in pd.concat([same_sku, diff_sku]).iterrows():
        sources.append(f"{w['title'][:25]} / {w['city']}")
        if len(sources) >= 3:
            break
    return ", ".join(sources)


def _generate_logic_explanation(row, direction):
    """Templated logic explanation — leads with selling price (what customer sees)."""
    mrp = float(row.get("mrp", row.get("stable_mrp", 0)))
    cur_d = float(row["current_discount_pct"])
    new_d = float(row.get("elbow_discount_pct", 0))
    cur_p = mrp * (1 - cur_d / 100)
    new_p = mrp * (1 - new_d / 100)
    mroi  = row.get("marginal_roi_at_current", 0)

    if direction == "waste":
        vol_chg = row.get("volume_change_pct", row.get("vol_change_pct", 0))
        savings = row.get("wasted_inr_per_month", 0)
        return (
            f"Currently selling at Rs.{cur_p:.0f} (MRP Rs.{mrp:.0f}, {cur_d:.0f}% off). "
            f"Each extra Rs.1 of discount returns only Rs.{mroi:.2f} of incremental margin. "
            f"Lifting price to Rs.{new_p:.0f} (= {new_d:.0f}% off) is projected to change "
            f"volume by {vol_chg:.1f}% and save Rs.{savings:,.0f}/month."
        )
    else:
        vol_lift = row.get("volume_lift_pct", row.get("vol_change_pct", 0))
        margin_lift = row.get("expected_margin_lift_inr_per_month", 0)
        return (
            f"Currently selling at Rs.{cur_p:.0f}. Dropping price to Rs.{new_p:.0f} "
            f"(= {new_d:.0f}% off) is projected to add {vol_lift:.1f}% volume and "
            f"Rs.{margin_lift:,.0f}/month in contribution margin."
        )


def _apply_guardrails(df):
    """
    Ensure all recommendations respect guardrails and add multi-cycle phasing
    if the requested move exceeds MAX_DISCOUNT_CHANGE_PPT.

    Reads target discount from `recommended_discount_pct` if present (reinvest
    table), otherwise from `elbow_discount_pct` (waste table). This is what
    makes reinvest phasing go UP and waste phasing go DOWN correctly.
    """
    for idx, row in df.iterrows():
        mrp   = row["mrp"]
        cur_d = float(row["current_discount_pct"])

        # Target: prefer recommended_discount_pct (reinvest), else elbow (waste)
        if "recommended_discount_pct" in row.index and pd.notna(row.get("recommended_discount_pct")):
            target_d = float(row["recommended_discount_pct"])
        else:
            target_d = float(row["elbow_discount_pct"])
        rec_d = target_d

        # Floor price check (only meaningful when cutting)
        target_price = mrp * (1 - target_d / 100)
        vc    = mrp * cfg.DEFAULT_COGS_PCT + cfg.DEFAULT_COMMISSION_PCT * target_price + cfg.DEFAULT_FULFILLMENT_FEE
        floor = vc * (1 + cfg.MIN_MARGIN_PCT)
        if target_price < floor:
            rec_d = max(0, (1 - floor / mrp) * 100)

        # Max change rate per cycle
        change = abs(cur_d - rec_d)
        phasing = ""
        if change > cfg.MAX_DISCOUNT_CHANGE_PPT:
            direction = 1 if rec_d > cur_d else -1
            throttled = cur_d + direction * cfg.MAX_DISCOUNT_CHANGE_PPT
            steps = [f"{cur_d:.0f}%"]
            c = cur_d
            while abs(c - rec_d) > 0.5:
                c += direction * cfg.MAX_DISCOUNT_CHANGE_PPT
                c = min(c, rec_d) if direction > 0 else max(c, rec_d)
                steps.append(f"{c:.0f}%")
            phasing = f" Multi-cycle phasing: {' -> '.join(steps)}."
            rec_d = max(0, throttled)

        df.at[idx, "rec_discount_final"] = round(rec_d, 1)
        # Add this_week_price — what brand team should set on Blinkit this cycle
        df.at[idx, "this_week_price"] = round(mrp * (1 - rec_d / 100), 1)
        if phasing:
            df.at[idx, "logic_explanation"] = row["logic_explanation"] + phasing

    return df


def _build_summary(waste_all, reinvest_all, waste_main, df_all=None):
    """
    Build portfolio summary dict including the flywheel weighted-discount math:
      - current weighted discount %
      - after cuts only
      - after cuts + reinvestments
      - vs TARGET_WEIGHTED_DISCOUNT_PCT
    """
    total_wasted = waste_all["wasted_inr_per_month"].sum() if not waste_all.empty else 0
    total_reinvest = reinvest_all["budget_needed_inr_per_month"].sum() if not reinvest_all.empty else 0
    total_margin_lift = reinvest_all["expected_margin_lift_inr_per_month"].sum() if not reinvest_all.empty else 0
    extra_units = reinvest_all["extra_volume_units_per_month"].sum() if (
        not reinvest_all.empty and "extra_volume_units_per_month" in reinvest_all.columns
    ) else 0

    conf_breakdown = {}
    if not waste_all.empty:
        for c in ["High", "Medium", "Low"]:
            subset = waste_all[waste_all["confidence"] == c]
            conf_breakdown[c] = {
                "pct": round(len(subset) / max(len(waste_all), 1) * 100, 0),
                "inr": subset["wasted_inr_per_month"].sum()
            }

    # ── Flywheel: portfolio weighted discount math ──
    flywheel = {
        "target_weighted_discount_pct": cfg.TARGET_WEIGHTED_DISCOUNT_PCT,
        "current_weighted_discount_pct": None,
        "after_cuts_weighted_discount_pct": None,
        "after_cuts_and_reinvest_weighted_discount_pct": None,
        "monthly_revenue_base": 0.0,
        "per_category_current": {},
    }
    if df_all is not None and not df_all.empty:
        d = df_all.copy()
        # Monthly revenue ≈ current_units_day × current_selling_price × 30
        if "current_revenue_day" in d.columns:
            d["monthly_rev"] = d["current_revenue_day"].astype(float) * 30
        else:
            d["monthly_rev"] = d["monthly_units"].astype(float) * (
                d["mrp"].astype(float) * (1 - d["current_discount_pct"].astype(float) / 100)
            )
        total_rev = float(d["monthly_rev"].sum())
        if total_rev > 0:
            cur_wd = float((d["current_discount_pct"] * d["monthly_rev"]).sum() / total_rev)
            flywheel["current_weighted_discount_pct"] = round(cur_wd, 2)
            flywheel["monthly_revenue_base"] = round(total_rev, 0)

            # After cuts: replace current_disc with rec_discount_final for waste cells
            d2 = d.copy()
            if not waste_all.empty and "rec_discount_final" in waste_all.columns:
                cuts = waste_all.set_index("cell_id")["rec_discount_final"]
                for cid, rd in cuts.items():
                    m = d2["cell_id"] == cid
                    if m.any() and pd.notna(rd):
                        d2.loc[m, "current_discount_pct"] = float(rd)
            wd_after_cuts = float((d2["current_discount_pct"] * d2["monthly_rev"]).sum() / total_rev)
            flywheel["after_cuts_weighted_discount_pct"] = round(wd_after_cuts, 2)

            # After cuts + reinvest
            d3 = d2.copy()
            if not reinvest_all.empty and "rec_discount_final" in reinvest_all.columns:
                rinv = reinvest_all.set_index("cell_id")["rec_discount_final"]
                for cid, rd in rinv.items():
                    m = d3["cell_id"] == cid
                    if m.any() and pd.notna(rd):
                        d3.loc[m, "current_discount_pct"] = float(rd)
            wd_after_both = float((d3["current_discount_pct"] * d3["monthly_rev"]).sum() / total_rev)
            flywheel["after_cuts_and_reinvest_weighted_discount_pct"] = round(wd_after_both, 2)

            # Per-category current weighted disc
            for cat, gg in d.groupby("category"):
                cw = float((gg["current_discount_pct"] * gg["monthly_rev"]).sum() / max(gg["monthly_rev"].sum(), 1))
                flywheel["per_category_current"][cat] = round(cw, 2)

    return {
        "total_wasted": total_wasted,
        "total_reinvest": total_reinvest,
        "total_extra_units_monthly": float(extra_units),
        "total_margin_lift": float(total_margin_lift),
        "net_margin_cut_only": total_wasted,
        "net_margin_cut_reinvest": total_wasted - total_reinvest + total_margin_lift,
        "conf_breakdown": conf_breakdown,
        "flywheel": flywheel,
    }


def _write_markdown(summary, waste_main, reinvest_main, needs_test, run_dir):
    """Write WASTE_REINVEST_REPORT.md."""
    lines = []
    lines.append("# Waste & Reinvestment Report")
    lines.append(f"\n*Generated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}*\n")

    # ── Flywheel summary (top of report) — price-led view ──
    fw = summary.get("flywheel", {})
    lines.append("## Flywheel: Portfolio Rebalancing (selling-price view)")
    lines.append("```")
    cur = fw.get("current_weighted_discount_pct")
    aft_c = fw.get("after_cuts_weighted_discount_pct")
    aft_b = fw.get("after_cuts_and_reinvest_weighted_discount_pct")
    tgt  = fw.get("target_weighted_discount_pct")
    if cur is not None:
        # Selling price as % of MRP is what customers effectively see (Rs.X on Rs.Y MRP).
        cur_sp_ratio  = 100.0 - cur
        aft_c_sp_ratio = 100.0 - aft_c
        aft_b_sp_ratio = 100.0 - aft_b
        tgt_sp_ratio  = 100.0 - tgt
        lines.append("                                Avg sell price as % of MRP   (equivalent discount)")
        lines.append(f"Target:                                  {tgt_sp_ratio:>6.2f}%               ({tgt:>5.2f}%)")
        lines.append(f"Current:                                 {cur_sp_ratio:>6.2f}%               ({cur:>5.2f}%)   gap: {cur - tgt:+.2f} ppt")
        lines.append(f"After this-cycle PRICE LIFTS only:       {aft_c_sp_ratio:>6.2f}%               ({aft_c:>5.2f}%)")
        lines.append(f"After PRICE LIFTS + STRATEGIC DROPS:     {aft_b_sp_ratio:>6.2f}%               ({aft_b:>5.2f}%)   <-- flywheel plan")
        lines.append("")
        if fw.get("per_category_current"):
            lines.append("Per-category current avg sell price (= 100% - discount):")
            for cat, v in fw["per_category_current"].items():
                lines.append(f"  {cat:18s}  {100.0 - v:>6.2f}% of MRP    (= {v:>5.2f}% off)")
        lines.append("")
    lines.append(f"Monthly savings from cuts:             Rs.{summary['total_wasted']:>12,.0f}")
    lines.append(f"Monthly budget redirected to growth:   Rs.{summary['total_reinvest']:>12,.0f}")
    lines.append(f"Extra volume from reinvestments:           {summary.get('total_extra_units_monthly', 0):>12,.0f} units/month")
    lines.append(f"Net monthly margin improvement:        Rs.{summary['net_margin_cut_reinvest']:>12,.0f}")
    lines.append("```\n")

    # Multi-cycle journey note
    if cur is not None and aft_b is not None:
        ppt_per_cycle = max(0.01, cur - aft_b)
        cycles_to_target = max(1, int(np.ceil((cur - tgt) / ppt_per_cycle))) if cur > tgt else 0
        if cycles_to_target > 0:
            lines.append(f"*This plan covers one cycle ({ppt_per_cycle:.2f} ppt move). At this pace it takes "
                         f"~{cycles_to_target} cycles to reach the {tgt:.1f}% target. Re-run weekly; "
                         f"the plan re-optimises against fresh data each time.*\n")

    # Confidence breakdown of waste pool
    lines.append("### Confidence breakdown of waste pool")
    lines.append("```")
    for c in ["High", "Medium", "Low"]:
        info = summary["conf_breakdown"].get(c, {"pct": 0, "inr": 0})
        tag = "  (shown separately as 'Needs Price Test')" if c == "Low" else ""
        lines.append(f"  {c:8s}: {info['pct']:3.0f}%   Rs.{info['inr']:>10,.0f}{tag}")
    lines.append("```\n")

    # Q1 table
    lines.append("## Q1: Where Am I Overspending on Discount?")
    if waste_main.empty:
        lines.append("\n*No High/Medium confidence waste cells found.*\n")
    else:
        lines.append(_df_to_md_table(waste_main, _waste_cols()))

    # Q2 table
    lines.append("\n## Q2: Where Can I Reinvest the Saved Money?")
    if reinvest_main.empty:
        lines.append("\n*No High/Medium confidence reinvestment candidates found.*\n")
    else:
        lines.append(_df_to_md_table(reinvest_main, _reinvest_cols()))

    # Needs Price Test
    lines.append("\n## Needs Price Test (Low Confidence)")
    if needs_test.empty:
        lines.append("\n*No low-confidence cells.*\n")
    else:
        test_cols = [("title", "SKU"), ("city", "City"), ("current_discount_pct", "Current %"),
                     ("elbow_discount_pct", "Elbow %"), ("confidence", "Confidence")]
        lines.append(_df_to_md_table(needs_test, test_cols))

    content = "\n".join(lines)
    path = os.path.join(run_dir, "WASTE_REINVEST_REPORT.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


# ─────────────────────────────────────────────────────────────────────────────
# PDF report — brand-team handout. Cover page with the flywheel headline,
# then per-section pages for waste (Q1), reinvest (Q2), and needs-test cells.
# Built with reportlab so we get proper tables, paging, and styled headers.
# ─────────────────────────────────────────────────────────────────────────────
def _write_pdf(summary, waste_main, reinvest_main, needs_test, run_dir):
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
            PageBreak, KeepTogether
        )
        from reportlab.lib.enums import TA_LEFT, TA_CENTER
    except ImportError:
        print("  [Stage 8] reportlab not installed — skipping PDF (pip install reportlab)")
        return None

    path = os.path.join(run_dir, "WASTE_REINVEST_REPORT.pdf")
    doc = SimpleDocTemplate(
        path, pagesize=landscape(A4),
        leftMargin=12*mm, rightMargin=12*mm,
        topMargin=10*mm,  bottomMargin=12*mm,
        title="Waste & Reinvestment Report",
    )

    # ── Styles ─────────────────────────────────────────────────────────
    styles = getSampleStyleSheet()
    BRAND  = colors.HexColor("#1F3864")     # deep navy
    ACCENT = colors.HexColor("#2E7D32")     # green for savings
    WARN   = colors.HexColor("#C62828")     # red for waste
    GREY   = colors.HexColor("#666666")
    LIGHT  = colors.HexColor("#F4F6FA")     # row stripe
    HEADER = colors.HexColor("#E8EEF7")     # table header background

    title_style = ParagraphStyle("t", parent=styles["Heading1"],
                                  fontSize=22, leading=26, textColor=BRAND,
                                  spaceAfter=4, alignment=TA_LEFT)
    sub_style   = ParagraphStyle("s", parent=styles["Normal"],
                                  fontSize=10, textColor=GREY, spaceAfter=8)
    h2_style    = ParagraphStyle("h2", parent=styles["Heading2"],
                                  fontSize=14, leading=18, textColor=BRAND,
                                  spaceBefore=10, spaceAfter=6)
    body_style  = ParagraphStyle("b", parent=styles["Normal"],
                                  fontSize=9.5, leading=13, spaceAfter=4)
    small_style = ParagraphStyle("sm", parent=styles["Normal"],
                                  fontSize=8, leading=10, textColor=GREY)
    cell_style  = ParagraphStyle("c", parent=styles["Normal"],
                                  fontSize=8.5, leading=11)

    story = []

    # ── COVER ──────────────────────────────────────────────────────────
    story.append(Paragraph("Discount Optimisation Report", title_style))
    story.append(Paragraph(
        f"Weekly flywheel plan &nbsp;|&nbsp; Generated {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}"
        f" &nbsp;|&nbsp; 24 Mantra Organic × Blinkit",
        sub_style
    ))

    fw = summary.get("flywheel", {})
    cur   = fw.get("current_weighted_discount_pct")
    aft_c = fw.get("after_cuts_weighted_discount_pct")
    aft_b = fw.get("after_cuts_and_reinvest_weighted_discount_pct")
    tgt   = fw.get("target_weighted_discount_pct")

    # Headline flywheel table
    if cur is not None:
        flywheel_data = [
            ["", "Avg sell price\n(% of MRP)", "Equivalent\ndiscount", "Gap to target"],
            ["Target",
             f"{100-tgt:.2f}%",
             f"{tgt:.2f}%",
             "—"],
            ["Today",
             f"{100-cur:.2f}%",
             f"{cur:.2f}%",
             f"{cur - tgt:+.2f} ppt"],
            ["After this-cycle PRICE LIFTS only",
             f"{100-aft_c:.2f}%",
             f"{aft_c:.2f}%",
             f"{aft_c - tgt:+.2f} ppt"],
            ["After PRICE LIFTS + STRATEGIC DROPS",
             f"{100-aft_b:.2f}%",
             f"{aft_b:.2f}%",
             f"{aft_b - tgt:+.2f} ppt"],
        ]
        tbl = Table(flywheel_data, colWidths=[95*mm, 35*mm, 30*mm, 35*mm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,0),  BRAND),
            ("TEXTCOLOR",    (0,0), (-1,0),  colors.white),
            ("FONTNAME",     (0,0), (-1,0),  "Helvetica-Bold"),
            ("FONTSIZE",     (0,0), (-1,0),  9),
            ("ALIGN",        (1,0), (-1,-1), "CENTER"),
            ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
            ("FONTSIZE",     (0,1), (-1,-1), 10),
            ("BACKGROUND",   (0,1), (-1,1),  LIGHT),
            ("BACKGROUND",   (0,2), (-1,2),  HEADER),
            ("FONTNAME",     (0,2), (-1,2),  "Helvetica-Bold"),
            ("BACKGROUND",   (0,4), (-1,4),  colors.HexColor("#EDF7EE")),
            ("FONTNAME",     (0,4), (-1,4),  "Helvetica-Bold"),
            ("TEXTCOLOR",    (0,4), (-1,4),  ACCENT),
            ("GRID",         (0,0), (-1,-1), 0.5, colors.lightgrey),
            ("LEFTPADDING",  (0,0), (-1,-1), 6),
            ("RIGHTPADDING", (0,0), (-1,-1), 6),
            ("TOPPADDING",   (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0), (-1,-1), 6),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 8*mm))

    # Money summary cards (3-column)
    cards = [
        [
            Paragraph("<b>Monthly savings from cuts</b>", body_style),
            Paragraph("<b>Budget redirected to growth</b>", body_style),
            Paragraph("<b>Extra volume from reinvestments</b>", body_style),
        ],
        [
            Paragraph(f"<font color='{WARN.hexval()}' size='18'><b>Rs. {summary['total_wasted']:,.0f}</b></font>",
                      ParagraphStyle("amt", parent=body_style, fontSize=14, leading=18)),
            Paragraph(f"<font color='{ACCENT.hexval()}' size='18'><b>Rs. {summary['total_reinvest']:,.0f}</b></font>",
                      ParagraphStyle("amt", parent=body_style, fontSize=14, leading=18)),
            Paragraph(f"<font color='{BRAND.hexval()}' size='18'><b>+{summary.get('total_extra_units_monthly', 0):,.0f}</b></font>"
                      f" units/month",
                      ParagraphStyle("amt", parent=body_style, fontSize=14, leading=18)),
        ],
    ]
    card_tbl = Table(cards, colWidths=[65*mm, 65*mm, 65*mm])
    card_tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0,0), (-1,-1), LIGHT),
        ("BOX",          (0,0), (0,-1),  0.5, colors.lightgrey),
        ("BOX",          (1,0), (1,-1),  0.5, colors.lightgrey),
        ("BOX",          (2,0), (2,-1),  0.5, colors.lightgrey),
        ("LEFTPADDING",  (0,0), (-1,-1), 10),
        ("RIGHTPADDING", (0,0), (-1,-1), 10),
        ("TOPPADDING",   (0,0), (-1,-1), 8),
        ("BOTTOMPADDING",(0,0), (-1,-1), 10),
    ]))
    story.append(card_tbl)

    # Multi-cycle journey + per-category breakdown
    story.append(Spacer(1, 6*mm))
    if cur is not None and aft_b is not None and cur > tgt:
        ppt = max(0.01, cur - aft_b)
        cycles = int(((cur - tgt) / ppt) + 0.999)
        story.append(Paragraph(
            f"<i>This plan covers <b>one cycle</b> (a {ppt:.2f} ppt move). "
            f"At this pace it takes ~<b>{cycles} weekly cycles</b> to reach the "
            f"{tgt:.1f}% target. Re-run weekly; the plan re-optimises against fresh data.</i>",
            body_style
        ))

    per_cat = fw.get("per_category_current", {})
    if per_cat:
        story.append(Spacer(1, 4*mm))
        story.append(Paragraph("Per-category snapshot", h2_style))
        cat_data = [["Category", "Today: sell price (% of MRP)", "Equivalent discount"]]
        for cat, v in per_cat.items():
            cat_data.append([cat, f"{100-v:.2f}%", f"{v:.2f}%"])
        cat_tbl = Table(cat_data, colWidths=[60*mm, 65*mm, 50*mm])
        cat_tbl.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,0),  BRAND),
            ("TEXTCOLOR",    (0,0), (-1,0),  colors.white),
            ("FONTNAME",     (0,0), (-1,0),  "Helvetica-Bold"),
            ("FONTSIZE",     (0,0), (-1,-1), 9.5),
            ("ALIGN",        (1,0), (-1,-1), "CENTER"),
            ("GRID",         (0,0), (-1,-1), 0.4, colors.lightgrey),
            ("TOPPADDING",   (0,0), (-1,-1), 5),
            ("BOTTOMPADDING",(0,0), (-1,-1), 5),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.white, LIGHT]),
        ]))
        story.append(cat_tbl)

    # ── Q1 — WASTE (price lifts) ────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("Q1 — Where am I overspending on discount?", title_style))
    story.append(Paragraph(
        "Cells sorted by Rs. wasted per month. <b>Now Rs.</b> = current selling price. "
        "<b>This Week Rs.</b> = what to set on Blinkit this Monday (throttled to a max Rs.3 / 3 ppt move per cycle). "
        "<b>Eventual Rs.</b> = the model's target price, reached over multiple cycles.",
        sub_style
    ))
    if waste_main.empty:
        story.append(Paragraph("<i>No High/Medium confidence waste cells found.</i>", body_style))
    else:
        story.append(_styled_table(
            waste_main,
            cols=[
                ("title",            "SKU",            55*mm, "left"),
                ("city",             "City",           28*mm, "left"),
                ("mrp",              "MRP",            14*mm, "right"),
                ("current_price",    "Now Rs.",        18*mm, "right"),
                ("this_week_price",  "This Week Rs.",  24*mm, "right"),
                ("eventual_price",   "Eventual Rs.",   22*mm, "right"),
                ("wasted_inr_per_month", "Wasted Rs./mo", 25*mm, "right"),
                ("confidence",       "Conf",           18*mm, "center"),
            ],
            header_color=BRAND, row_color=LIGHT, cell_style=cell_style,
            money_cols={"mrp", "current_price", "this_week_price",
                        "eventual_price", "wasted_inr_per_month"},
        ))

    # ── Q2 — REINVEST (strategic price drops) ──────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("Q2 — Where can I reinvest to grow volume?", title_style))
    story.append(Paragraph(
        "Cells where dropping price by 3 ppt is projected to drive enough extra volume to be worth the investment. "
        "<b>Drop by Rs.</b> = the per-unit price drop. <b>+Units/mo</b> = projected incremental volume. "
        "<b>Budget</b> = additional monthly discount spend needed. These cells are funded by the savings from Q1.",
        sub_style
    ))
    if reinvest_main.empty:
        story.append(Paragraph("<i>No High/Medium confidence reinvestment candidates this cycle.</i>", body_style))
    else:
        story.append(_styled_table(
            reinvest_main,
            cols=[
                ("title",                       "SKU",            55*mm, "left"),
                ("city",                        "City",           24*mm, "left"),
                ("mrp",                         "MRP",            14*mm, "right"),
                ("current_price",               "Now Rs.",        16*mm, "right"),
                ("new_price",                   "New Rs.",        16*mm, "right"),
                ("price_drop_inr",              "Drop Rs.",       16*mm, "right"),
                ("volume_lift_pct",             "Vol +%",         15*mm, "right"),
                ("extra_volume_units_per_month","+Units/mo",      20*mm, "right"),
                ("budget_needed_inr_per_month", "Budget Rs./mo",  25*mm, "right"),
                ("confidence",                  "Conf",           17*mm, "center"),
            ],
            header_color=ACCENT, row_color=colors.HexColor("#EDF7EE"), cell_style=cell_style,
            money_cols={"mrp", "current_price", "new_price", "price_drop_inr",
                        "budget_needed_inr_per_month", "extra_volume_units_per_month"},
            pct_cols={"volume_lift_pct"},
        ))

    # ── Needs Price Test ───────────────────────────────────────────────
    if not needs_test.empty:
        story.append(PageBreak())
        story.append(Paragraph("Needs Price Test (low confidence)", title_style))
        story.append(Paragraph(
            "These cells don't have enough clean data — either too few observations, too little discount variation, "
            "or the demand signal is being confounded by something else (e.g. a launch ramp). "
            "Run a small A/B price test in one city before acting.",
            sub_style
        ))
        story.append(_styled_table(
            needs_test,
            cols=[
                ("title",                "SKU",        80*mm, "left"),
                ("city",                 "City",       35*mm, "left"),
                ("current_discount_pct", "Now %",      18*mm, "right"),
                ("elbow_discount_pct",   "Elbow %",    20*mm, "right"),
                ("confidence",           "Confidence", 30*mm, "center"),
            ],
            header_color=GREY, row_color=LIGHT, cell_style=cell_style,
            money_cols=set(),
        ))

    # ── Footer note on every page ──────────────────────────────────────
    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(GREY)
        canvas.drawString(12*mm, 6*mm,
            "Discount Optimizer — Stage 8 Flywheel Report  •  "
            "Customer-facing price (Rs.) is the primary view; equivalent discount % shown for Blinkit entry.")
        canvas.drawRightString(doc_.pagesize[0] - 12*mm, 6*mm,
            f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return path


def _styled_table(df, cols, header_color, row_color, cell_style,
                   money_cols=None, pct_cols=None):
    """
    Helper: build a reportlab Table from a DataFrame given column specs.
    cols = [(df_column, header_label, width, align), ...]
    money_cols/pct_cols control numeric formatting.
    """
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle, Paragraph

    money_cols = money_cols or set()
    pct_cols   = pct_cols or set()

    def _fmt(col, v):
        if pd.isna(v): return ""
        if col in money_cols:
            return f"{float(v):,.0f}" if abs(float(v)) >= 100 else f"{float(v):,.1f}"
        if col in pct_cols:
            return f"{float(v):+.1f}%"
        if isinstance(v, float):
            return f"{v:,.1f}"
        return str(v)

    # Header
    headers = [Paragraph(f"<b><font color='white'>{h}</font></b>", cell_style)
               for (_, h, _, _) in cols]
    rows = [headers]

    # Data rows (cap at 60 to keep PDF manageable; report says how many)
    df_show = df.head(60)
    for _, r in df_show.iterrows():
        row = []
        for col, _, _, align in cols:
            v = r.get(col, "")
            txt = _fmt(col, v)
            # SKU column: wrap long names
            if col == "title":
                txt = txt[:55]
            row.append(Paragraph(txt, cell_style))
        rows.append(row)

    widths = [w for (_, _, w, _) in cols]
    tbl = Table(rows, colWidths=widths, repeatRows=1)

    # Style
    style_cmds = [
        ("BACKGROUND",   (0,0), (-1,0),  header_color),
        ("FONTNAME",     (0,0), (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",     (0,0), (-1,0),  8.5),
        ("ALIGN",        (0,0), (-1,0),  "CENTER"),
        ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
        ("FONTSIZE",     (0,1), (-1,-1), 8),
        ("GRID",         (0,0), (-1,-1), 0.3, colors.lightgrey),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.white, row_color]),
        ("LEFTPADDING",  (0,0), (-1,-1), 4),
        ("RIGHTPADDING", (0,0), (-1,-1), 4),
        ("TOPPADDING",   (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0), (-1,-1), 4),
    ]
    # Per-column alignment
    for i, (_, _, _, align) in enumerate(cols):
        if align == "right":
            style_cmds.append(("ALIGN", (i,1), (i,-1), "RIGHT"))
        elif align == "center":
            style_cmds.append(("ALIGN", (i,1), (i,-1), "CENTER"))
        else:
            style_cmds.append(("ALIGN", (i,1), (i,-1), "LEFT"))
    tbl.setStyle(TableStyle(style_cmds))
    return tbl


def _write_csvs(waste_all, reinvest_all, run_dir):
    """Write waste.csv and reinvest.csv — price-led columns first."""
    w_cols = ["product_id", "title", "city", "mrp",
              "current_price", "new_price", "price_increase_inr",
              "current_discount_pct", "elbow_discount_pct", "wasted_discount_pct",
              "wasted_inr_per_month", "vol_change_pct", "confidence",
              "quality_note", "logic_explanation"]
    r_cols = ["product_id", "title", "city", "mrp",
              "current_price", "new_price", "price_drop_inr",
              "current_discount_pct", "recommended_discount_pct",
              "volume_lift_pct", "extra_volume_units_per_month",
              "budget_needed_inr_per_month", "expected_margin_lift_inr_per_month",
              "margin_sacrifice_pct", "reinvestment_efficiency",
              "confidence", "quality_note", "logic_explanation", "funded_by"]

    w_out = waste_all[[c for c in w_cols if c in waste_all.columns]] if not waste_all.empty else pd.DataFrame(columns=w_cols)
    r_out = reinvest_all[[c for c in r_cols if c in reinvest_all.columns]] if not reinvest_all.empty else pd.DataFrame(columns=r_cols)

    w_out.to_csv(os.path.join(run_dir, "waste.csv"), index=False, encoding="utf-8-sig")
    r_out.to_csv(os.path.join(run_dir, "reinvest.csv"), index=False, encoding="utf-8-sig")


def _write_json(df, model_output, summary, run_dir):
    """Write per_cell_detail.json with full per-cell payload."""
    diagnostics = model_output.get("diagnostics", {}) if model_output else {}

    output = {
        "model_diagnostics": {
            "overall_holdout_mape": diagnostics.get("test_mape", "N/A"),
            "overall_holdout_r2": diagnostics.get("test_r2", "N/A"),
            "n_train": diagnostics.get("n_train", 0),
            "n_test": diagnostics.get("n_test", 0),
        },
        "summary": summary,
        "cells": [],
    }

    for _, row in df.iterrows():
        cell = {
            "product_id": row["product_id"],
            "title": row.get("title", ""),
            "city": row["city"],
            "cell_id": row["cell_id"],
            "category": row.get("category", ""),
            "mrp": row["mrp"],
            "elasticity": row["elasticity"],
            "confidence": row["confidence"],
            "current_discount_pct": row["current_discount_pct"],
            "elbow_discount_pct": row["elbow_discount_pct"],
            "n_observations": int(row.get("n_observations", 0)),
            "curve_points": row.get("curve_points", []),
            "curve_params": row.get("curve_params", {}),
        }
        output["cells"].append(cell)

    path = os.path.join(run_dir, "per_cell_detail.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, cls=NpEncoder)


def _waste_cols():
    # Lean view: 3 prices (now / this week / eventual), then ₹ wasted + conf
    return [
        ("title", "SKU"), ("city", "City"),
        ("mrp", "MRP"),
        ("current_price", "Now Rs."),
        ("this_week_price", "This Week Rs."),
        ("eventual_price", "Eventual Rs."),
        ("wasted_inr_per_month", "Wasted Rs./mo"),
        ("confidence", "Conf"),
    ]

def _reinvest_cols():
    # Reinvest moves fit within the 3-ppt-per-cycle cap, so "New Rs." IS this-week
    return [
        ("title", "SKU"), ("city", "City"),
        ("mrp", "MRP"),
        ("current_price", "Now Rs."),
        ("new_price", "This Week Rs."),
        ("price_drop_inr", "Drop by Rs."),
        ("volume_lift_pct", "Vol +%"),
        ("extra_volume_units_per_month", "+Units/mo"),
        ("budget_needed_inr_per_month", "Budget Rs./mo"),
        ("confidence", "Conf"),
    ]

def _df_to_md_table(df, col_spec):
    """Convert DataFrame to markdown table."""
    headers = [h for _, h in col_spec]
    cols = [c for c, _ in col_spec]
    available = [c for c in cols if c in df.columns]
    h_avail = [h for (c, h) in col_spec if c in df.columns]

    lines = ["| " + " | ".join(h_avail) + " |"]
    lines.append("| " + " | ".join(["---"] * len(h_avail)) + " |")
    for _, row in df.head(50).iterrows():
        vals = []
        for c in available:
            v = row[c]
            if isinstance(v, float):
                vals.append(f"{v:,.1f}" if abs(v) < 1e6 else f"{v:,.0f}")
            else:
                vals.append(str(v)[:80])
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def _empty_waste_df():
    return pd.DataFrame(columns=[
        "product_id", "city", "title", "mrp", "cell_id", "confidence",
        "current_discount_pct", "elbow_discount_pct", "wasted_discount_pct",
        "wasted_inr_per_month", "vol_change_pct", "logic_explanation",
        "marginal_roi_at_current", "volume_change_pct", "monthly_units",
        "elasticity", "elbow_marginal_roi", "ladder",
    ])

def _empty_reinvest_df():
    return pd.DataFrame(columns=[
        "product_id", "city", "title", "mrp", "cell_id", "confidence",
        "current_discount_pct", "elbow_discount_pct", "recommended_discount_pct",
        "budget_needed_inr_per_month", "expected_margin_lift_inr_per_month",
        "extra_volume_units_per_month", "volume_lift_pct",
        "margin_sacrifice_pct", "reinvestment_efficiency",
        "logic_explanation", "funded_by", "marginal_roi_at_current",
        "monthly_units", "elasticity", "ladder", "rec_discount_final",
    ])
