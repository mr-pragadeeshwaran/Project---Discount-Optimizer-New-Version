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
    # Remove reinvest cells from the waste view — a cell flagged for strategic
    # reinvestment should appear ONLY in Q2 (we're investing, not cutting it).
    if not reinvest_main.empty and "cell_id" in reinvest_main.columns:
        waste_main = waste_main[~waste_main["cell_id"].isin(reinvest_main["cell_id"])].copy()
    needs_test = pd.concat([
        waste_all[waste_all["confidence"] == "Low"],
        reinvest_all[reinvest_all["confidence"] == "Low"],
    ]).drop_duplicates(subset=["cell_id"])

    summary = _build_summary(waste_all, reinvest_all, waste_main, df_all=df)
    # Canonical business metrics: total_spend / total_sales_at_MRP and per-product breakdown
    summary["business"] = _compute_business_metrics(df, waste_main, reinvest_main)
    # Model accuracy from Stage 4 diagnostics — pulled into the brand-team report
    summary["model_accuracy"] = _compute_model_accuracy(model_output)

    # Print summary — business-style
    biz = summary["business"]
    t, c, b = biz.get("today", {}), biz.get("after_cuts", {}), biz.get("after_cuts_and_reinvest", {})
    print(f"    Waste cells (High/Med): {len(waste_main)} | Reinvest cells (High/Med): {len(reinvest_main)}")
    print(f"    Needs Price Test: {len(needs_test)} cells")
    if t:
        print(f"    Today:           gross=Rs.{t['gross_sales_inr']:>12,.0f}  "
              f"discount=Rs.{t['discount_spend_inr']:>11,.0f}  ({t['weighted_discount_pct']:.2f}%)")
        print(f"    After cuts:      gross=Rs.{c['gross_sales_inr']:>12,.0f}  "
              f"discount=Rs.{c['discount_spend_inr']:>11,.0f}  ({c['weighted_discount_pct']:.2f}%)")
        print(f"    After cuts+inv:  gross=Rs.{b['gross_sales_inr']:>12,.0f}  "
              f"discount=Rs.{b['discount_spend_inr']:>11,.0f}  ({b['weighted_discount_pct']:.2f}%)")
        print(f"    Target weighted discount: {cfg.TARGET_WEIGHTED_DISCOUNT_PCT:.2f}%")

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


# ─────────────────────────────────────────────────────────────────────────────
# Business metrics — canonical numbers used in the summary report
#
# Discount % is computed the standard brand-finance way:
#     weighted_discount_pct = total_discount_spend / total_gross_sales_at_MRP × 100
#
# That is: of every Rs.100 of full-price potential revenue, how many rupees
# went out as discount? This is the metric a brand manager actually controls
# as a budget — not the (mathematically valid but unintuitive) weighted
# average of percentages by net revenue.
# ─────────────────────────────────────────────────────────────────────────────
def _compute_business_metrics(df, waste_main, reinvest_main):
    """
    Returns a nested dict with portfolio + per-product metrics under
    three scenarios:
      - today                    : current state (last 30-day reality)
      - after_cuts               : after applying THIS-WEEK waste cuts
      - after_cuts_and_reinvest  : after both cuts AND strategic reinvestments

    For each scenario we compute, in monthly terms:
      - gross_sales_inr     = Σ MRP × units × 30
      - discount_spend_inr  = Σ (MRP − selling_price) × units × 30
      - net_revenue_inr     = gross_sales − discount_spend
      - total_units         = Σ units × 30
      - weighted_discount_pct = discount_spend / gross_sales × 100
    """
    if df.empty:
        return {"today": {}, "after_cuts": {}, "after_cuts_and_reinvest": {},
                "per_product": {}}

    # Map cell_id → recommended_discount_final for cuts and reinvest
    cut_disc_map = {}
    if not waste_main.empty and "rec_discount_final" in waste_main.columns:
        cut_disc_map = waste_main.dropna(subset=["rec_discount_final"]).set_index(
            "cell_id")["rec_discount_final"].to_dict()
    rinv_disc_map = {}
    if not reinvest_main.empty and "rec_discount_final" in reinvest_main.columns:
        rinv_disc_map = reinvest_main.dropna(subset=["rec_discount_final"]).set_index(
            "cell_id")["rec_discount_final"].to_dict()

    def _predict_units(row, new_disc):
        """Predict daily units at a new discount level using dual-signal model."""
        cur_units = float(row.get("current_units_day", 0))
        cur_disc  = float(row["current_discount_pct"])
        mrp       = float(row["mrp"])
        if cur_units <= 0 or mrp <= 0:
            return cur_units
        cur_price = mrp * (1 - cur_disc / 100)
        new_price = mrp * (1 - new_disc / 100)
        if cur_price <= 0 or new_price <= 0:
            return cur_units
        elast = float(row.get("price_elasticity", row.get("elasticity", -1.5)))
        badge = float(row.get("badge_sensitivity", row.get("discount_sensitivity", 0.0)))
        delta_disc = new_disc - cur_disc
        try:
            mult = (new_price / cur_price) ** elast * np.exp(badge * delta_disc)
            return max(cur_units * mult, 0.01)
        except Exception:
            return cur_units

    def _aggregate(rows):
        """Sum to portfolio + per-product totals for a given list of (mrp, disc, units) tuples."""
        out = {
            "gross_sales_inr":      0.0,
            "discount_spend_inr":   0.0,
            "net_revenue_inr":      0.0,
            "total_units":          0.0,
        }
        for mrp, disc, units in rows:
            monthly_units = units * 30
            gross         = mrp * monthly_units
            net           = mrp * (1 - disc / 100) * monthly_units
            spend         = gross - net
            out["gross_sales_inr"]    += gross
            out["discount_spend_inr"] += spend
            out["net_revenue_inr"]    += net
            out["total_units"]        += monthly_units
        out["weighted_discount_pct"] = (
            out["discount_spend_inr"] / out["gross_sales_inr"] * 100
            if out["gross_sales_inr"] > 0 else 0.0
        )
        return out

    # Build the (mrp, discount, units) tuples for the 3 scenarios
    def _scenario(scenario):
        tuples = []
        per_product_tuples = {}
        for _, row in df.iterrows():
            mrp   = float(row["mrp"])
            cur_d = float(row["current_discount_pct"])
            cell_id = row.get("cell_id", "")

            # A cell appears in BOTH cut and reinvest maps because the
            # waste table is permissive (anything with current > elbow).
            # The brand-intent is: cells flagged for strategic reinvest
            # should be invested in, not cut. So reinvest takes precedence
            # in 'after_cuts_and_reinvest', and is EXCLUDED from 'after_cuts'
            # so the headline cuts number reflects only true price lifts.
            in_reinvest = cell_id in rinv_disc_map
            in_cut      = cell_id in cut_disc_map

            if scenario == "today":
                new_d, new_units = cur_d, float(row.get("current_units_day", 0))
            elif scenario == "after_cuts":
                if in_cut and not in_reinvest:
                    new_d = float(cut_disc_map[cell_id])
                    new_units = _predict_units(row, new_d)
                else:
                    new_d, new_units = cur_d, float(row.get("current_units_day", 0))
            else:  # after_cuts_and_reinvest
                if in_reinvest:
                    new_d = float(rinv_disc_map[cell_id])
                elif in_cut:
                    new_d = float(cut_disc_map[cell_id])
                else:
                    new_d = cur_d
                if new_d != cur_d:
                    new_units = _predict_units(row, new_d)
                else:
                    new_units = float(row.get("current_units_day", 0))

            tuples.append((mrp, new_d, new_units))
            # Per-product key: prefer "{pid} | {grammage}", fallback to pid
            grm = row.get("grammage", "")
            pkey = f"{row['product_id']} | {grm}" if grm and pd.notna(grm) else str(row["product_id"])
            per_product_tuples.setdefault(pkey, []).append((mrp, new_d, new_units))

        portfolio = _aggregate(tuples)
        per_product = {k: _aggregate(v) for k, v in per_product_tuples.items()}
        return portfolio, per_product

    today_portfolio,    today_per_prod    = _scenario("today")
    cuts_portfolio,     cuts_per_prod     = _scenario("after_cuts")
    both_portfolio,     both_per_prod     = _scenario("after_cuts_and_reinvest")

    # Reshape per-product as {product_key: {today, after_cuts, after_both}}
    all_products = set(today_per_prod) | set(cuts_per_prod) | set(both_per_prod)
    per_product = {}
    for pkey in sorted(all_products):
        # Pull title from df for nicer display
        sample = df[df["product_id"].astype(str) + (
            (" | " + df.get("grammage", pd.Series([""] * len(df))).fillna("").astype(str)) if "grammage" in df.columns else ""
        ) == pkey]
        raw_title = sample["title"].iloc[0] if not sample.empty else pkey
        # Disambiguate by grammage — two products can share a title (e.g.
        # both 500g and 1kg Moong Dal show as "Moong Dal (Dhuli)"). Append
        # the pack size so brand team can tell them apart.
        grm = ""
        if not sample.empty and "grammage" in sample.columns:
            g = sample["grammage"].iloc[0]
            if pd.notna(g) and str(g).strip():
                grm = str(g).strip()
        title = f"{str(raw_title)[:50]}  ({grm})" if grm else str(raw_title)[:50]
        per_product[pkey] = {
            "title":     title,
            "today":     today_per_prod.get(pkey, {}),
            "after_cuts": cuts_per_prod.get(pkey, {}),
            "after_cuts_and_reinvest": both_per_prod.get(pkey, {}),
        }

    return {
        "today":                    today_portfolio,
        "after_cuts":               cuts_portfolio,
        "after_cuts_and_reinvest":  both_portfolio,
        "per_product":              per_product,
        "n_waste_cells":            int(len(waste_main)) if not waste_main.empty else 0,
        "n_reinvest_cells":         int(len(reinvest_main)) if not reinvest_main.empty else 0,
    }


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
    """
    Write WASTE_REINVEST_REPORT.md — mirrors the PDF structure:
      1. Portfolio summary (sales / spend / discount % under 3 scenarios)
      2. This week's plan (cells × spend Δ × units Δ)
      3. Per-product breakdown
      4. Q1 detail (price lifts), Q2 detail (price drops), Needs Price Test
    """
    biz   = summary.get("business", {})
    today = biz.get("today", {})
    cuts  = biz.get("after_cuts", {})
    both  = biz.get("after_cuts_and_reinvest", {})
    target = cfg.TARGET_WEIGHTED_DISCOUNT_PCT

    def fmt_money(v):
        if v is None or pd.isna(v): return "—"
        v = float(v)
        if abs(v) >= 1e7: return f"Rs. {v/1e7:,.2f} Cr"
        if abs(v) >= 1e5: return f"Rs. {v/1e5:,.2f} L"
        return f"Rs. {v:,.0f}"

    def fmt_pct(v):
        if v is None or pd.isna(v): return "—"
        return f"{float(v):.2f}%"

    def fmt_units(v):
        if v is None or pd.isna(v): return "—"
        return f"{float(v):,.0f}"

    lines = []
    lines.append("# Discount Optimisation — Weekly Report")
    lines.append(f"\n*Generated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}  |  24 Mantra Organic × Blinkit*\n")

    # ── Portfolio summary ──────────────────────────────────────────────
    lines.append("## Portfolio Summary")
    lines.append("")
    if today:
        lines.append("| Metric | Today | After cuts | After cuts + invest |")
        lines.append("|---|---:|---:|---:|")
        lines.append(f"| Gross sales / month (at MRP) | {fmt_money(today.get('gross_sales_inr'))} | {fmt_money(cuts.get('gross_sales_inr'))} | {fmt_money(both.get('gross_sales_inr'))} |")
        lines.append(f"| Discount spend / month | {fmt_money(today.get('discount_spend_inr'))} | {fmt_money(cuts.get('discount_spend_inr'))} | {fmt_money(both.get('discount_spend_inr'))} |")
        lines.append(f"| Net revenue / month | {fmt_money(today.get('net_revenue_inr'))} | {fmt_money(cuts.get('net_revenue_inr'))} | {fmt_money(both.get('net_revenue_inr'))} |")
        lines.append(f"| Units sold / month | {fmt_units(today.get('total_units'))} | {fmt_units(cuts.get('total_units'))} | {fmt_units(both.get('total_units'))} |")
        lines.append(f"| **Weighted discount %** | **{fmt_pct(today.get('weighted_discount_pct'))}** | **{fmt_pct(cuts.get('weighted_discount_pct'))}** | **{fmt_pct(both.get('weighted_discount_pct'))}** |")
        lines.append("")
        gap = today.get("weighted_discount_pct", 0) - target
        aft_gap = both.get("weighted_discount_pct", 0) - target
        lines.append(f"*Target weighted discount: **{target:.2f}%**. Gap today: **+{gap:.2f} ppt**. Gap after this-week plan: **+{aft_gap:.2f} ppt**.*")
        lines.append("")

    # ── This week's plan ───────────────────────────────────────────────
    n_waste = biz.get("n_waste_cells", 0)
    n_reinv = biz.get("n_reinvest_cells", 0)
    spend_change_cut = today.get("discount_spend_inr", 0) - cuts.get("discount_spend_inr", 0)
    spend_change_inv = both.get("discount_spend_inr", 0)  - cuts.get("discount_spend_inr", 0)
    units_change_cut = cuts.get("total_units", 0) - today.get("total_units", 0)
    units_change_inv = both.get("total_units", 0) - cuts.get("total_units", 0)
    net_spend_change = today.get("discount_spend_inr", 0) - both.get("discount_spend_inr", 0)
    net_units_change = both.get("total_units", 0) - today.get("total_units", 0)

    lines.append("## This Week's Plan")
    lines.append("")
    lines.append("| Action | Cells | Discount spend Δ | Units Δ / month |")
    lines.append("|---|---:|---:|---:|")
    lines.append(f"| Cut (raise price) | {n_waste} | −{fmt_money(spend_change_cut)} | {int(units_change_cut):+,} |")
    lines.append(f"| Reinvest (drop price) | {n_reinv} | +{fmt_money(spend_change_inv)} | {int(units_change_inv):+,} |")
    lines.append(f"| **Net change** | **{n_waste + n_reinv}** | **−{fmt_money(net_spend_change)}** | **{int(net_units_change):+,}** |")
    lines.append("")

    if today.get("weighted_discount_pct", 0) > target:
        cur_wd = today["weighted_discount_pct"]
        aft_wd = both.get("weighted_discount_pct", cur_wd)
        ppt = max(0.01, cur_wd - aft_wd)
        cycles = int(((cur_wd - target) / ppt) + 0.999)
        lines.append(f"*This plan moves the weighted discount by **{ppt:.2f} ppt** in one cycle. "
                     f"At this pace, reaching the {target:.1f}% target takes ~**{cycles} weekly cycles**. "
                     f"The plan re-optimises each week against fresh data.*")
        lines.append("")

    # ── Model accuracy ──────────────────────────────────────────────
    acc = summary.get("model_accuracy", {})
    if acc.get("available"):
        lines.append("## Model Accuracy")
        lines.append("")
        lines.append(f"**Overall: {acc['tier']}** — trained on {acc['n_train']:,} regular-day rows, "
                     f"validated on {acc['n_test']:,} held-out future rows.")
        lines.append("")
        lines.append("| Metric | Value | What it means |")
        lines.append("|---|---:|---|")
        lines.append(f"| Out-of-sample R² (test set) | {acc['test_r2_log']:.2f} | "
                     f"Fraction of week-to-week variation in unit sales the model explains on data it "
                     f"hasn't seen during training. 1.0 = perfect, 0 = useless. |")
        lines.append(f"| Average error at discount-bin grain | {acc['test_mape_agg']:.1f}% | "
                     f"Average % error when comparing predicted vs actual mean units in each 3-ppt "
                     f"discount band. This is the metric the saturation curve uses. |")
        lines.append(f"| Training-data fit (in-distribution R²) | {acc['train_r2_log']:.2f} | "
                     f"How well the model fits the data it was trained on. High value = price/quantity "
                     f"relationship is well captured. |")
        lines.append("")
        lines.append("*Daily-level predictions are inherently noisy for CPG SKU × city data; "
                     "recommendations are most reliable on the High-confidence cells.*")
        lines.append("")

    # ── Per-product breakdown ──────────────────────────────────────────
    per_prod = biz.get("per_product", {})
    if per_prod:
        lines.append("## By Product")
        lines.append("")
        for pkey, pdata in per_prod.items():
            t = pdata.get("today", {})
            c = pdata.get("after_cuts", {})
            b = pdata.get("after_cuts_and_reinvest", {})
            if not t: continue
            lines.append(f"### {pdata.get('title', pkey)}")
            lines.append("")
            lines.append("| Metric | Today | After cuts | After cuts + invest |")
            lines.append("|---|---:|---:|---:|")
            lines.append(f"| Gross sales / mo | {fmt_money(t.get('gross_sales_inr'))} | {fmt_money(c.get('gross_sales_inr'))} | {fmt_money(b.get('gross_sales_inr'))} |")
            lines.append(f"| Discount spend / mo | {fmt_money(t.get('discount_spend_inr'))} | {fmt_money(c.get('discount_spend_inr'))} | {fmt_money(b.get('discount_spend_inr'))} |")
            lines.append(f"| Units / mo | {fmt_units(t.get('total_units'))} | {fmt_units(c.get('total_units'))} | {fmt_units(b.get('total_units'))} |")
            lines.append(f"| **Weighted discount %** | **{fmt_pct(t.get('weighted_discount_pct'))}** | **{fmt_pct(c.get('weighted_discount_pct'))}** | **{fmt_pct(b.get('weighted_discount_pct'))}** |")
            lines.append("")

    # Q1 table
    lines.append("## Where to Raise Prices This Week")
    lines.append("")
    lines.append(_confidence_legend_md())
    lines.append("")
    if waste_main.empty:
        lines.append("\n*No High/Medium confidence cells to cut this week.*\n")
    else:
        lines.append(_df_to_md_table(waste_main, _waste_cols()))

    # Q2 table
    lines.append("\n## Where to Drop Prices to Grow Volume")
    lines.append("")
    lines.append(_confidence_legend_md())
    lines.append("")
    if reinvest_main.empty:
        lines.append("\n*No High/Medium confidence reinvestment candidates this cycle.*\n")
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
# PDF report — minimalist McKinsey-style business handout.
#
# Design principles:
#   - One subtle accent color (slate navy), used sparingly
#   - No background colors on table headers — bold text with horizontal rules
#   - Generous whitespace; hierarchy via typography not chrome
#   - Numbers right-aligned, text left-aligned, with consistent decimals
#   - Single font family (Helvetica), 3-4 sizes used consistently
# ─────────────────────────────────────────────────────────────────────────────
def _write_pdf(summary, waste_main, reinvest_main, needs_test, run_dir):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
            PageBreak, KeepTogether
        )
        from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
    except ImportError:
        print("  [Stage 8] reportlab not installed — skipping PDF (pip install reportlab)")
        return None

    path = os.path.join(run_dir, "WASTE_REINVEST_REPORT.pdf")
    # Portrait A4 — feels more like a clean business memo than a wide spreadsheet
    doc = SimpleDocTemplate(
        path, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=18*mm,  bottomMargin=18*mm,
        title="Discount Optimisation — Weekly Report",
        author="24 Mantra Organic × Blinkit",
    )

    # ── Design tokens ──────────────────────────────────────────────────
    INK      = colors.HexColor("#0F172A")   # near-black, primary text
    BODY     = colors.HexColor("#1F2937")   # body text
    MUTED    = colors.HexColor("#6B7280")   # secondary text
    HAIRLINE = colors.HexColor("#E5E7EB")   # very light rule
    RULE     = colors.HexColor("#9CA3AF")   # medium rule
    ACCENT   = colors.HexColor("#1E3A5F")   # slate navy — used VERY sparingly
    POS      = colors.HexColor("#15803D")   # positive (savings)
    NEG      = colors.HexColor("#B91C1C")   # negative (over-target)

    title_style = ParagraphStyle("t", fontName="Helvetica-Bold",
                                  fontSize=18, leading=22, textColor=INK,
                                  spaceAfter=2, alignment=TA_LEFT)
    eyebrow     = ParagraphStyle("e", fontName="Helvetica",
                                  fontSize=8, leading=11, textColor=MUTED,
                                  spaceAfter=14, alignment=TA_LEFT,
                                  letterSpace=0.5)
    h2_style    = ParagraphStyle("h2", fontName="Helvetica-Bold",
                                  fontSize=11, leading=15, textColor=INK,
                                  spaceBefore=16, spaceAfter=6, alignment=TA_LEFT)
    h3_style    = ParagraphStyle("h3", fontName="Helvetica-Bold",
                                  fontSize=9.5, leading=13, textColor=BODY,
                                  spaceBefore=10, spaceAfter=4, alignment=TA_LEFT)
    body_style  = ParagraphStyle("b", fontName="Helvetica",
                                  fontSize=9.5, leading=14, textColor=BODY,
                                  spaceAfter=6)
    note_style  = ParagraphStyle("n", fontName="Helvetica-Oblique",
                                  fontSize=8.5, leading=12, textColor=MUTED,
                                  spaceAfter=6)
    label_style = ParagraphStyle("l", fontName="Helvetica",
                                  fontSize=8, leading=10, textColor=MUTED)
    cell_style  = ParagraphStyle("c", fontName="Helvetica",
                                  fontSize=8.5, leading=11, textColor=BODY)

    biz = summary.get("business", {})
    today = biz.get("today", {})
    cuts  = biz.get("after_cuts", {})
    both  = biz.get("after_cuts_and_reinvest", {})
    target_disc = cfg.TARGET_WEIGHTED_DISCOUNT_PCT

    def _money(v):
        if v is None or pd.isna(v): return "—"
        v = float(v)
        if abs(v) >= 1e7: return f"Rs. {v/1e7:,.2f} Cr"
        if abs(v) >= 1e5: return f"Rs. {v/1e5:,.2f} L"
        return f"Rs. {v:,.0f}"

    def _pct(v):
        if v is None or pd.isna(v): return "—"
        return f"{float(v):.2f}%"

    def _units(v):
        if v is None or pd.isna(v): return "—"
        return f"{float(v):,.0f}"

    story = []

    # ╔══════════════════════════════════════════════════════════════════╗
    # PAGE 1 — EXECUTIVE SUMMARY
    # ╚══════════════════════════════════════════════════════════════════╝
    story.append(Paragraph(
        f"DISCOUNT OPTIMISATION  ·  WEEKLY REPORT  ·  {pd.Timestamp.now().strftime('%d %B %Y')}",
        eyebrow))
    story.append(Paragraph("Portfolio Summary", title_style))
    story.append(Paragraph("24 Mantra Organic on Blinkit", body_style))
    story.append(Spacer(1, 4*mm))

    # ── Headline 3-scenario table ──────────────────────────────────────
    if today:
        headline = [
            ["", "Today",        "After cuts",       "After cuts + invest"],
            ["Gross sales / month (at MRP)",
                _money(today.get("gross_sales_inr")),
                _money(cuts.get("gross_sales_inr")),
                _money(both.get("gross_sales_inr"))],
            ["Discount spend / month",
                _money(today.get("discount_spend_inr")),
                _money(cuts.get("discount_spend_inr")),
                _money(both.get("discount_spend_inr"))],
            ["Net revenue / month",
                _money(today.get("net_revenue_inr")),
                _money(cuts.get("net_revenue_inr")),
                _money(both.get("net_revenue_inr"))],
            ["Units sold / month",
                _units(today.get("total_units")),
                _units(cuts.get("total_units")),
                _units(both.get("total_units"))],
            ["Weighted discount %",
                _pct(today.get("weighted_discount_pct")),
                _pct(cuts.get("weighted_discount_pct")),
                _pct(both.get("weighted_discount_pct"))],
        ]
        head_tbl = Table(headline, colWidths=[68*mm, 33*mm, 33*mm, 36*mm])
        head_tbl.setStyle(_minimal_table_style(INK, HAIRLINE, RULE))
        # Highlight the discount % row (it's the headline metric)
        head_tbl.setStyle(TableStyle([
            ("FONTNAME",     (0,5), (-1,5), "Helvetica-Bold"),
            ("TEXTCOLOR",    (0,5), (-1,5), INK),
            ("LINEABOVE",    (0,5), (-1,5), 0.5, RULE),
            ("TOPPADDING",   (0,5), (-1,5), 8),
            ("BOTTOMPADDING",(0,5), (-1,5), 8),
        ]))
        story.append(head_tbl)
        story.append(Spacer(1, 3*mm))

        # Target reminder line
        if today.get("weighted_discount_pct") is not None:
            gap = today["weighted_discount_pct"] - target_disc
            after_gap = both.get("weighted_discount_pct", 0) - target_disc
            story.append(Paragraph(
                f"<font color='{MUTED.hexval()}'>Target weighted discount: "
                f"<b>{target_disc:.2f}%</b>. "
                f"Gap today: <font color='{NEG.hexval()}'><b>+{gap:.2f} ppt</b></font>. "
                f"Gap after this-week plan: <b>+{after_gap:.2f} ppt</b>.</font>",
                body_style))

    # ── This Week's Plan section ───────────────────────────────────────
    story.append(Paragraph("This week's plan", h2_style))

    n_waste = biz.get("n_waste_cells", 0)
    n_reinv = biz.get("n_reinvest_cells", 0)
    spend_change_cut  = today.get("discount_spend_inr", 0) - cuts.get("discount_spend_inr", 0)
    spend_change_inv  = both.get("discount_spend_inr", 0)  - cuts.get("discount_spend_inr", 0)
    units_change_cut  = cuts.get("total_units", 0) - today.get("total_units", 0)
    units_change_inv  = both.get("total_units", 0) - cuts.get("total_units", 0)
    net_spend_change  = today.get("discount_spend_inr", 0) - both.get("discount_spend_inr", 0)
    net_units_change  = both.get("total_units", 0) - today.get("total_units", 0)

    # Wrap colored cells in Paragraph so the <font> tags render
    num_style = ParagraphStyle("num", fontName="Helvetica", fontSize=9.5,
                                leading=11, textColor=BODY, alignment=TA_RIGHT)
    plan_data = [
        ["", "Cells", "Discount spend Δ", "Units Δ / month"],
        ["Cut (raise price)",     str(n_waste),
            Paragraph(_money_signed(-spend_change_cut, POS, NEG), num_style),
            Paragraph(_units_signed(units_change_cut, POS, NEG), num_style)],
        ["Reinvest (drop price)", str(n_reinv),
            Paragraph(_money_signed(spend_change_inv, POS, NEG), num_style),
            Paragraph(_units_signed(units_change_inv, POS, NEG), num_style)],
        ["Net change",            f"{n_waste + n_reinv}",
            Paragraph(_money_signed(-net_spend_change, POS, NEG), num_style),
            Paragraph(_units_signed(net_units_change, POS, NEG), num_style)],
    ]
    plan_tbl = Table(plan_data, colWidths=[68*mm, 22*mm, 40*mm, 40*mm])
    plan_tbl.setStyle(_minimal_table_style(INK, HAIRLINE, RULE))
    plan_tbl.setStyle(TableStyle([
        ("FONTNAME",     (0,3), (-1,3), "Helvetica-Bold"),
        ("LINEABOVE",    (0,3), (-1,3), 0.5, RULE),
        ("TOPPADDING",   (0,3), (-1,3), 8),
        ("BOTTOMPADDING",(0,3), (-1,3), 8),
    ]))
    story.append(plan_tbl)
    story.append(Spacer(1, 3*mm))

    if today.get("weighted_discount_pct", 0) > target_disc:
        cur_wd = today["weighted_discount_pct"]
        aft_wd = both.get("weighted_discount_pct", cur_wd)
        ppt    = max(0.01, cur_wd - aft_wd)
        cycles = int(((cur_wd - target_disc) / ppt) + 0.999)
        story.append(Paragraph(
            f"This plan moves the weighted discount by <b>{ppt:.2f} ppt</b> in one cycle. "
            f"At this pace, reaching the {target_disc:.1f}% target takes "
            f"~<b>{cycles} weekly cycles</b>. The plan re-optimises each week against fresh data.",
            note_style))

    # ── Model accuracy block ───────────────────────────────────────────
    acc = summary.get("model_accuracy", {})
    if acc.get("available"):
        story.append(Paragraph("Model accuracy", h2_style))
        tier = acc["tier"]
        tier_color = {"Strong": POS, "Moderate": ACCENT, "Weak": NEG, "Unreliable": NEG}.get(tier, MUTED)
        # Long explanation text needs Paragraph wrapping so it wraps properly
        exp_style = ParagraphStyle("exp", fontName="Helvetica", fontSize=8,
                                    leading=10, textColor=MUTED, alignment=TA_LEFT)
        acc_data = [
            ["", "Value", "What it means"],
            ["Out-of-sample R² (test set)",
                f"{acc['test_r2_log']:.2f}",
                Paragraph("Fraction of week-to-week variation in unit sales the model explains "
                          "on data it has not seen during training. 1.0 = perfect, 0 = useless.",
                          exp_style)],
            ["Avg error at discount-bin grain",
                f"{acc['test_mape_agg']:.1f}%",
                Paragraph("Average % error when comparing predicted vs actual mean units in each "
                          "3-ppt discount band. This is the grain the saturation curve uses.",
                          exp_style)],
            ["Training-data fit (in-distribution R²)",
                f"{acc['train_r2_log']:.2f}",
                Paragraph("How well the model fits the data it was trained on. High value means "
                          "the price/quantity relationship is well captured.",
                          exp_style)],
        ]
        acc_tbl = Table(acc_data, colWidths=[58*mm, 22*mm, 90*mm])
        acc_tbl.setStyle(_minimal_table_style(INK, HAIRLINE, RULE))
        acc_tbl.setStyle(TableStyle([
            ("ALIGN", (2, 0), (2, -1), "LEFT"),  # explanation col left-aligned
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 1), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 1), (-1, -1), 6),
        ]))
        story.append(acc_tbl)
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph(
            f"<b>Overall: <font color='{tier_color.hexval()}'>{tier}</font></b> — "
            f"trained on {acc['n_train']:,} regular-day rows, validated on {acc['n_test']:,} "
            f"held-out future rows. Daily-level predictions are inherently noisy for CPG SKU × "
            f"city data; recommendations are most reliable on the High-confidence cells.",
            note_style))

    # ╔══════════════════════════════════════════════════════════════════╗
    # PAGE 2 — PER-PRODUCT BREAKDOWN
    # ╚══════════════════════════════════════════════════════════════════╝
    per_prod = biz.get("per_product", {})
    if per_prod:
        story.append(PageBreak())
        story.append(Paragraph(
            f"DISCOUNT OPTIMISATION  ·  PER-PRODUCT VIEW",
            eyebrow))
        story.append(Paragraph("By product", title_style))
        story.append(Paragraph(
            "Same metrics as the portfolio summary, broken out by SKU. "
            "The discount % row tells you which products are furthest from the "
            f"{target_disc:.0f}% target and how this week's plan affects each.",
            body_style))
        story.append(Spacer(1, 4*mm))

        for pkey, pdata in per_prod.items():
            t = pdata.get("today", {})
            c = pdata.get("after_cuts", {})
            b = pdata.get("after_cuts_and_reinvest", {})
            if not t: continue

            story.append(Paragraph(pdata.get("title", pkey), h3_style))
            prod_tbl = Table([
                ["", "Today", "After cuts", "After cuts + invest"],
                ["Gross sales (MRP) / mo",
                    _money(t.get("gross_sales_inr")),
                    _money(c.get("gross_sales_inr")),
                    _money(b.get("gross_sales_inr"))],
                ["Discount spend / mo",
                    _money(t.get("discount_spend_inr")),
                    _money(c.get("discount_spend_inr")),
                    _money(b.get("discount_spend_inr"))],
                ["Units / mo",
                    _units(t.get("total_units")),
                    _units(c.get("total_units")),
                    _units(b.get("total_units"))],
                ["Weighted discount %",
                    _pct(t.get("weighted_discount_pct")),
                    _pct(c.get("weighted_discount_pct")),
                    _pct(b.get("weighted_discount_pct"))],
            ], colWidths=[60*mm, 35*mm, 35*mm, 40*mm])
            prod_tbl.setStyle(_minimal_table_style(INK, HAIRLINE, RULE))
            prod_tbl.setStyle(TableStyle([
                ("FONTNAME",     (0,4), (-1,4), "Helvetica-Bold"),
                ("LINEABOVE",    (0,4), (-1,4), 0.4, RULE),
            ]))
            story.append(prod_tbl)
            story.append(Spacer(1, 4*mm))

    # ╔══════════════════════════════════════════════════════════════════╗
    # PAGE 3+ — DETAIL: WHERE TO CUT
    # ╚══════════════════════════════════════════════════════════════════╝
    story.append(PageBreak())
    story.append(Paragraph(
        f"DISCOUNT OPTIMISATION  ·  PRICE LIFTS",
        eyebrow))
    story.append(Paragraph("Where to raise prices this week", title_style))
    story.append(Paragraph(
        "Cells sorted by Rs. wasted per month. <b>Now</b> is the current selling price. "
        "<b>This Week</b> is what to set on Blinkit this Monday — capped at a 3 ppt move "
        "per cycle so customers aren't shocked. <b>Wasted/mo</b> is the full multi-cycle "
        "savings opportunity if you walked the price all the way back to MRP.",
        body_style))
    story.append(_confidence_legend(body_style, note_style))
    story.append(Spacer(1, 3*mm))

    if waste_main.empty:
        story.append(Paragraph("No High/Medium confidence cells to cut this week.", note_style))
    else:
        story.append(_minimal_data_table(
            waste_main,
            cols=[
                ("title",            "Product",         60*mm, "left"),
                ("city",             "City",            30*mm, "left"),
                ("mrp",              "MRP",             14*mm, "right"),
                ("current_price",    "Now",             16*mm, "right"),
                ("this_week_price",  "This Week",       20*mm, "right"),
                ("wasted_inr_per_month", "Wasted/mo",   25*mm, "right"),
                ("confidence",       "Conf",            18*mm, "center"),
            ],
            money_cols={"mrp", "current_price", "this_week_price",
                        "wasted_inr_per_month"},
            cell_style=cell_style,
            ink=INK, hairline=HAIRLINE, rule=RULE, muted=MUTED,
            # Plain integer with thousand separators — consistent across all
            # rows of the detail table (avoids mixing "2.67 L" and "97,230").
            money_fn=lambda v: f"{float(v):,.0f}",
        ))

    # ╔══════════════════════════════════════════════════════════════════╗
    # NEXT PAGE — DETAIL: WHERE TO INVEST
    # ╚══════════════════════════════════════════════════════════════════╝
    story.append(PageBreak())
    story.append(Paragraph(
        f"DISCOUNT OPTIMISATION  ·  STRATEGIC INVESTMENTS",
        eyebrow))
    story.append(Paragraph("Where to drop prices to grow volume", title_style))
    story.append(Paragraph(
        "Cells where dropping the price by 3 ppt is projected to add enough volume "
        "to be worth the extra discount spend. These are funded by the savings from "
        "the price lifts above.",
        body_style))
    story.append(_confidence_legend(body_style, note_style))
    story.append(Spacer(1, 3*mm))

    if reinvest_main.empty:
        story.append(Paragraph("No High/Medium confidence reinvestment candidates this cycle.", note_style))
    else:
        story.append(_minimal_data_table(
            reinvest_main,
            cols=[
                ("title",                       "Product",       53*mm, "left"),
                ("city",                        "City",          22*mm, "left"),
                ("mrp",                         "MRP",           12*mm, "right"),
                ("current_price",               "Now",           14*mm, "right"),
                ("new_price",                   "New",           14*mm, "right"),
                ("volume_lift_pct",             "Vol Δ",         15*mm, "right"),
                ("extra_volume_units_per_month","+Units/mo",     19*mm, "right"),
                ("budget_needed_inr_per_month", "Budget/mo",     22*mm, "right"),
                ("confidence",                  "Conf",          18*mm, "center"),
            ],
            money_cols={"mrp", "current_price", "new_price",
                        "budget_needed_inr_per_month", "extra_volume_units_per_month"},
            pct_cols={"volume_lift_pct"},
            cell_style=cell_style,
            ink=INK, hairline=HAIRLINE, rule=RULE, muted=MUTED,
            money_fn=lambda v: f"{float(v):,.0f}",
        ))

    # ╔══════════════════════════════════════════════════════════════════╗
    # FINAL PAGE — NEEDS PRICE TEST
    # ╚══════════════════════════════════════════════════════════════════╝
    if not needs_test.empty:
        story.append(PageBreak())
        story.append(Paragraph(
            f"DISCOUNT OPTIMISATION  ·  PILOT REQUIRED",
            eyebrow))
        story.append(Paragraph("Cells needing a price test", title_style))
        story.append(Paragraph(
            "These cells don't have enough clean data to act on. The model isn't "
            "confident enough — usually because of too few observations, too little "
            "price variation, or a launch ramp confounding the elasticity signal. "
            "Run a small A/B test in one city before changing anything.",
            body_style))
        story.append(Spacer(1, 3*mm))
        story.append(_minimal_data_table(
            needs_test,
            cols=[
                ("title",                "Product",     85*mm, "left"),
                ("city",                 "City",        35*mm, "left"),
                ("current_discount_pct", "Now %",       20*mm, "right"),
                ("elbow_discount_pct",   "Model %",     20*mm, "right"),
                ("confidence",           "Status",      28*mm, "center"),
            ],
            money_cols=set(),
            cell_style=cell_style,
            ink=INK, hairline=HAIRLINE, rule=RULE, muted=MUTED,
        ))

    # ── Footer ─────────────────────────────────────────────────────────
    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(20*mm, 10*mm,
            "Discount % is computed as total monthly discount spend ÷ total monthly gross sales at MRP.")
        canvas.drawRightString(doc_.pagesize[0] - 20*mm, 10*mm,
            f"{doc_.page}")
        # subtle top rule on every page after page 1
        if doc_.page > 1:
            canvas.setStrokeColor(HAIRLINE)
            canvas.setLineWidth(0.5)
            canvas.line(20*mm, doc_.pagesize[1] - 12*mm,
                        doc_.pagesize[0] - 20*mm, doc_.pagesize[1] - 12*mm)
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return path


def _confidence_legend_md() -> str:
    """One-line plain-English explanation of the Conf column for markdown reports."""
    return (
        "*Confidence: **High** = 200+ days of clean history with 10+ distinct discount levels "
        "and ≥3 ppt of price variation; the model trusts this cell. "
        "**Medium** = 100+ days and 5+ discount levels — actionable but worth a review. "
        "**Low** = thin data; shown separately as 'Needs Price Test'. "
        "Cells where the model flagged a data-quality concern (boundary-hit elasticity "
        "or rapid demand growth) are automatically downgraded one tier.*"
    )


def _confidence_legend(body_style, note_style):
    """One-line plain-English explanation of the Conf column for the brand team."""
    from reportlab.platypus import Paragraph
    return Paragraph(
        "<i>Confidence: <b>High</b> = 200+ days of clean history with 10+ distinct discount levels "
        "and ≥3 ppt of price variation; the model trusts this cell.  "
        "<b>Medium</b> = 100+ days and 5+ discount levels — actionable but worth a review.  "
        "<b>Low</b> = thin data; shown separately as 'Needs Price Test'.  "
        "Cells where the model flagged a data-quality concern (boundary-hit elasticity or "
        "rapid demand growth) are automatically downgraded one tier.</i>",
        note_style)


def _compute_model_accuracy(model_output: dict) -> dict:
    """
    Extract Stage 4 model diagnostics into a brand-team-friendly accuracy
    summary. Returns a dict with the raw metrics + a plain-English tier
    (Strong / Moderate / Weak / Unreliable).

    Tier thresholds (gut-calibrated for SKU x city x day CPG data):
      Strong:     test R^2 >= 0.50 AND aggregated MAPE <= 30%
      Moderate:   test R^2 >= 0.20 AND aggregated MAPE <= 60%
      Weak:       test R^2 >= 0.00 AND aggregated MAPE <= 100%
      Unreliable: anything below
    """
    if not model_output or "diagnostics" not in model_output:
        return {"available": False}
    d = model_output["diagnostics"]
    train_r2 = float(d.get("test_r2_train", 0))
    test_r2  = float(d.get("test_r2_log",   0))
    daily_mape = float(d.get("test_mape",   99.9))
    agg_mape   = float(d.get("test_mape_agg", d.get("test_mape", 99.9)))
    agg_r2     = float(d.get("test_r2_units_agg", d.get("test_r2", 0)))
    n_train    = int(d.get("n_train", 0))
    n_test     = int(d.get("n_test",  0))

    if test_r2 >= 0.50 and agg_mape <= 30:
        tier = "Strong"
    elif test_r2 >= 0.20 and agg_mape <= 60:
        tier = "Moderate"
    elif test_r2 >= 0.00 and agg_mape <= 100:
        tier = "Weak"
    else:
        tier = "Unreliable"

    return {
        "available": True,
        "tier":             tier,
        "train_r2_log":     round(train_r2, 3),
        "test_r2_log":      round(test_r2, 3),
        "test_mape_daily":  round(daily_mape, 1),
        "test_mape_agg":    round(agg_mape, 1),
        "test_r2_agg":      round(agg_r2, 3),
        "n_train":          n_train,
        "n_test":           n_test,
    }


def _money_signed(v, pos_color, neg_color):
    """Format a signed currency change with color cue (used in plan table)."""
    if v is None or pd.isna(v) or abs(v) < 1: return "—"
    sign = "−" if v < 0 else "+"
    val = abs(float(v))
    if val >= 1e7: s = f"{sign}Rs. {val/1e7:,.2f} Cr"
    elif val >= 1e5: s = f"{sign}Rs. {val/1e5:,.2f} L"
    else: s = f"{sign}Rs. {val:,.0f}"
    color = pos_color.hexval() if v < 0 else neg_color.hexval()  # spend going DOWN is positive
    return f'<font color="{color}">{s}</font>'


def _units_signed(v, pos_color, neg_color):
    """Format a signed unit change with color cue."""
    if v is None or pd.isna(v) or abs(v) < 1: return "—"
    sign = "+" if v > 0 else "−"
    color = pos_color.hexval() if v > 0 else neg_color.hexval()
    return f'<font color="{color}">{sign}{abs(int(v)):,}</font>'


def _minimal_table_style(ink, hairline, rule):
    """Shared minimalist table style — top + header bottom rules, no grid."""
    from reportlab.platypus import TableStyle
    return TableStyle([
        # Header row
        ("FONTNAME",     (0,0), (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",     (0,0), (-1,0),  8.5),
        ("TEXTCOLOR",    (0,0), (-1,0),  ink),
        ("ALIGN",        (1,0), (-1,0),  "RIGHT"),
        ("LINEABOVE",    (0,0), (-1,0),  0.8, ink),
        ("LINEBELOW",    (0,0), (-1,0),  0.5, ink),
        ("TOPPADDING",   (0,0), (-1,0),  6),
        ("BOTTOMPADDING",(0,0), (-1,0),  6),
        # Body rows
        ("FONTNAME",     (0,1), (-1,-1), "Helvetica"),
        ("FONTSIZE",     (0,1), (-1,-1), 9.5),
        ("ALIGN",        (0,1), (0,-1),  "LEFT"),
        ("ALIGN",        (1,1), (-1,-1), "RIGHT"),
        ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",   (0,1), (-1,-1), 5),
        ("BOTTOMPADDING",(0,1), (-1,-1), 5),
        # Bottom rule
        ("LINEBELOW",    (0,-1), (-1,-1), 0.8, ink),
    ])


def _minimal_data_table(df, cols, money_cols, cell_style, ink, hairline, rule, muted,
                         pct_cols=None, money_fn=None):
    """Minimalist data table for the detail pages."""
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle, Paragraph

    pct_cols = pct_cols or set()
    money_fn = money_fn or (lambda v: f"{float(v):,.0f}")

    def _fmt(col, v):
        if pd.isna(v): return ""
        if col in money_cols:
            return money_fn(v)
        if col in pct_cols:
            return f"{float(v):+.1f}%"
        if isinstance(v, float):
            return f"{v:,.1f}"
        return str(v)

    # Header
    headers = [Paragraph(f"<b><font size='8'>{h}</font></b>", cell_style)
               for (_, h, _, _) in cols]
    rows = [headers]
    for _, r in df.head(60).iterrows():
        row = []
        for col, _, _, align in cols:
            v = r.get(col, "")
            txt = _fmt(col, v)
            if col == "title":
                txt = txt[:55]
            row.append(Paragraph(txt, cell_style))
        rows.append(row)

    widths = [w for (_, _, w, _) in cols]
    tbl = Table(rows, colWidths=widths, repeatRows=1)

    style_cmds = [
        ("FONTSIZE",     (0,0), (-1,0),  8.5),
        ("TEXTCOLOR",    (0,0), (-1,0),  ink),
        ("LINEABOVE",    (0,0), (-1,0),  0.8, ink),
        ("LINEBELOW",    (0,0), (-1,0),  0.5, ink),
        ("TOPPADDING",   (0,0), (-1,0),  6),
        ("BOTTOMPADDING",(0,0), (-1,0),  6),
        ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",   (0,1), (-1,-1), 4),
        ("BOTTOMPADDING",(0,1), (-1,-1), 4),
        ("LINEBELOW",    (0,-1), (-1,-1), 0.8, ink),
    ]
    # very subtle row separators
    for r in range(1, len(rows) - 1):
        style_cmds.append(("LINEBELOW", (0, r), (-1, r), 0.25, hairline))
    # per-col alignment
    for i, (_, _, _, align) in enumerate(cols):
        if align == "right":
            style_cmds.append(("ALIGN", (i, 0), (i, -1), "RIGHT"))
        elif align == "center":
            style_cmds.append(("ALIGN", (i, 0), (i, -1), "CENTER"))
        else:
            style_cmds.append(("ALIGN", (i, 0), (i, -1), "LEFT"))
    tbl.setStyle(TableStyle(style_cmds))
    return tbl


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
    # 2 prices (now / this week), wasted ₹/mo, confidence
    # Eventual = MRP for nearly every cell (margin-optimal elbow = no discount),
    # so it's redundant — dropped.
    return [
        ("title", "SKU"), ("city", "City"),
        ("mrp", "MRP"),
        ("current_price", "Now Rs."),
        ("this_week_price", "This Week Rs."),
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
