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
        if isinstance(obj, (np.floating,)):
            f = float(obj)
            return f if np.isfinite(f) else None   # never emit NaN/Inf into JSON
        if isinstance(obj, np.ndarray): return obj.tolist()
        if isinstance(obj, (pd.Timestamp,)): return str(obj)
        if isinstance(obj, pd.DataFrame): return obj.to_dict(orient="records")
        if isinstance(obj, pd.Series): return obj.to_dict()
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

    # ── Leakage decomposition (real vs borrowed vs stolen units) ──────
    # Volume-based; nets out pull-forward (φ) + cannibalization (κ). Used to
    # haircut the reinvest volume lift so "growth" that's really borrowed/
    # stolen doesn't qualify a cell for deeper discount.
    try:
        from stage8_output.leakage import decompose_leakage
        lk = decompose_leakage(feat_df)
        df = df.merge(
            lk[["cell_id", "pull_forward", "cannibalization",
                "true_incremental_frac", "leakage_confidence"]],
            on="cell_id", how="left")
        df["pull_forward"] = df["pull_forward"].fillna(0.0)
        df["cannibalization"] = df["cannibalization"].fillna(0.0)
        df["true_incremental_frac"] = df["true_incremental_frac"].fillna(1.0)
        df["leakage_confidence"] = df["leakage_confidence"].fillna("no_promo")
    except Exception as e:
        print(f"  [Stage 8] Leakage decomposition skipped: {type(e).__name__}: {e}")
        df["pull_forward"] = 0.0; df["cannibalization"] = 0.0
        df["true_incremental_frac"] = 1.0; df["leakage_confidence"] = "n/a"

    # ── "Is it even worth discounting?" gate ──────────────────────────
    # |elasticity| ≤ 1 ⇒ inelastic ⇒ a price cut is very unlikely to pay
    # (revenue falls or barely moves while costs rise); these are prime
    # price-HOLD/RAISE cells, not discount-deeper cells.
    inelastic_thr = float(getattr(cfg, "INELASTIC_ELASTICITY_THRESHOLD", 1.0))
    _elast = df.get("price_elasticity", df.get("elasticity", pd.Series(-1.5, index=df.index)))
    df["abs_elasticity"] = _elast.astype(float).abs()
    df["is_inelastic"] = df["abs_elasticity"] <= inelastic_thr

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
    # Multi-cycle roadmap: week-by-week projection over the target duration
    summary["glide_path"] = _compute_glide_path(df, waste_main, reinvest_main)
    # Track Record (the receipts): out-of-time backtest + live realised-vs-predicted.
    # This is what turns "trust the model" into proven outcomes for a buyer.
    try:
        from stage8_output.track_record import build_track_record
        summary["track_record"] = build_track_record(feat_df, run_dir)
    except Exception as e:
        print(f"  [Stage 8] Track Record skipped: {type(e).__name__}: {e}")
        summary["track_record"] = {"backtest": {"available": False},
                                   "live": {"available": False}}
    # Leakage & discount-worthiness view (real vs borrowed vs stolen + ε gate)
    summary["leakage"] = _build_leakage_summary(df)

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
    md_path    = _write_markdown(summary, waste_main, reinvest_main, needs_test, run_dir)
    excel_path = _write_excel(summary, waste_main, reinvest_main, needs_test, df, run_dir)
    _write_csvs(waste_all, reinvest_all, run_dir)
    _write_json(df, model_output, summary, run_dir)

    print(f"  [Stage 8] Markdown report: {md_path}")
    if excel_path:
        print(f"  [Stage 8] Excel report:    {excel_path}")
    return {"markdown": md_path, "excel": excel_path,
            "waste_csv": os.path.join(run_dir, "waste.csv"),
            "reinvest_csv": os.path.join(run_dir, "reinvest.csv"),
            "json": os.path.join(run_dir, "per_cell_detail.json")}


def _write_excel(summary, waste_main, reinvest_main, needs_test, df, run_dir):
    """Delegate to the excel_report module — keeps this file focused."""
    try:
        from stage8_output.excel_report import write_excel
        return write_excel(summary, waste_main, reinvest_main, needs_test, df, run_dir)
    except ImportError as e:
        print(f"  [Stage 8] openpyxl not installed — skipping Excel ({e})")
        return None
    except Exception as e:
        print(f"  [Stage 8] Excel generation failed: {type(e).__name__}: {e}")
        import traceback; traceback.print_exc()
        return None


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

        # Keep the volume-weighted full-window current state from Stage 4 (do NOT
        # overwrite current_discount_pct with a last-30 simple mean — that mixed
        # windows and broke reconciliation with source). monthly_units is just
        # the daily current units annualised, so the totals sum from source.
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
    # TRUE WASTE = pulling the discount back RAISES net revenue (units x selling
    # price). Cells where cutting would lower net revenue are elastic — the
    # discount is working there — so they are excluded, not counted as savings.
    ngain = df.get("net_rev_gain_mo")
    waste_mask = (
        ((ngain > 0) if ngain is not None
         else (df["current_discount_pct"] > df["elbow_discount_pct"])) &
        (df["confidence"] != "Needs Experiment")
    )
    waste = df[waste_mask].copy()

    if waste.empty:
        return _empty_waste_df()

    waste["wasted_discount_pct"] = waste["current_discount_pct"] - waste["elbow_discount_pct"]
    # Honest headline: the recoverable amount is the NET-REVENUE gain, not the
    # raw discount reduction (which ignored the sales lost).
    if "net_rev_gain_mo" in waste.columns:
        waste["wasted_inr_per_month"] = waste["net_rev_gain_mo"].round(0)
    else:
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

        # Independent inelastic floor: |ε| ≤ 1 cells are very unlikely to profit
        # from a deeper cut. Subsumed by the |ε| ≥ 2 filter below at default
        # config, but kept so the gate still holds if REINVEST_MIN_ELASTICITY
        # is lowered.
        if bool(row.get("is_inelastic", False)):
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

        # Leakage haircut: only REAL new demand counts toward the lift gate.
        # A cell whose "growth" is mostly borrowed (pull-forward) or stolen from
        # sibling packs (cannibalization) should not qualify for deeper discount.
        real_frac = float(row.get("true_incremental_frac", 1.0))
        gross_lift = float(sim["vol_lift_pct"])
        net_lift = gross_lift * real_frac
        if net_lift < cfg.REINVEST_MIN_VOL_LIFT_PCT:
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
            # Headline lift/units are NET of leakage (the value the gate enforced) —
            # so the brand isn't shown a "growth" number that's mostly borrowed/
            # stolen. Gross is kept alongside for transparency.
            "volume_lift_pct":                   round(net_lift, 1),
            "gross_volume_lift_pct":             round(gross_lift, 1),
            "net_volume_lift_pct":               round(net_lift, 1),
            "margin_sacrifice_pct":              round(sim["margin_sacrifice_pct"], 1),
            "extra_volume_units_per_month":      round(sim["extra_units_monthly"] * real_frac, 0),
            "reinvestment_efficiency":           round(
                sim["extra_units_monthly"] * real_frac / max(sim["extra_disc_cost_monthly"], 1.0) * 100, 2
            ),  # NET units gained per ₹100 of budget
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

        # Per-cycle step: max(MIN_PPT, gap/TIMELINE), no upper cap.
        # Closes the gap within TARGET_TIMELINE_WEEKS regardless of size.
        gap = abs(cur_d - rec_d)
        min_step = float(getattr(cfg, "MIN_DISCOUNT_CHANGE_PPT", 3))
        timeline = int(getattr(cfg, "TARGET_TIMELINE_WEEKS", 12))
        if gap <= min_step:
            cycle_step = gap  # one-shot
        else:
            cycle_step = max(min_step, gap / float(timeline))
        phasing = ""
        if gap > cycle_step + 0.05:
            direction = 1 if rec_d > cur_d else -1
            throttled = cur_d + direction * cycle_step
            steps = [f"{cur_d:.1f}%"]
            c = cur_d
            while abs(c - rec_d) > 0.5:
                c += direction * cycle_step
                c = min(c, rec_d) if direction > 0 else max(c, rec_d)
                steps.append(f"{c:.1f}%")
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
    # Headline = the DECISION model (the price/badge curve that actually sets
    # recommendations, held out, at the 3-ppt bin grain) — the same numbers
    # the tier is computed from and the Excel Summary shows. The full
    # statistical model (incl. momentum/lag features) is context only.
    # Degenerate values (a diverged fit) are labelled as failed, never
    # printed as if they were measurements.
    def _fmt_r2(v):
        v = float(v)
        if not np.isfinite(v) or v <= -9.98:
            return None
        return f"{v:.2f}"

    def _fmt_mape(v):
        v = float(v)
        if not np.isfinite(v) or v > 500:
            return None
        return f"{v:.1f}%"

    acc = summary.get("model_accuracy", {})
    if acc.get("available"):
        dec_r2   = _fmt_r2(acc.get("decision_r2_bin", 0))
        dec_mape = _fmt_mape(acc.get("decision_mape_bin", 99.9))
        full_r2  = _fmt_r2(acc.get("test_r2_log", 0))
        train_r2 = _fmt_r2(acc.get("train_r2_log", 0))
        lines.append("## Model Accuracy")
        lines.append("")
        lines.append(f"**Overall: {acc['tier']}** — trained on {acc['n_train']:,} regular-day rows, "
                     f"validated on {acc['n_test']:,} held-out future rows.")
        lines.append("")
        lines.append("| Metric | Value | What it means |")
        lines.append("|---|---:|---|")
        lines.append(f"| Price-engine accuracy — held-out R² (3-ppt bin grain) | {dec_r2 or 'FAILED — fit unstable this run'} | "
                     f"How well the price/badge curve that actually sets recommendations predicts "
                     f"held-out demand at the grain decisions are made. This is the number the "
                     f"Overall tier is based on. |")
        lines.append(f"| Price-engine error at the same grain | {dec_mape or 'FAILED — fit unstable this run'} | "
                     f"Average % error comparing predicted vs actual mean units in each 3-ppt "
                     f"discount band, held out. |")
        lines.append(f"| Full statistical model — held-out log R² (context only) | {full_r2 or 'FAILED — fit unstable this run'} | "
                     f"The full regression including momentum/lag features. Quoted for context; "
                     f"its fit is flattered by autocorrelation, so the price-engine rows above "
                     f"are the honest headline. |")
        lines.append(f"| Training-data fit (in-distribution R²) | {train_r2 or 'FAILED — fit unstable this run'} | "
                     f"How well the model fits the data it was trained on. High value = price/quantity "
                     f"relationship is well captured. |")
        lines.append("")
        if dec_r2 is None or dec_mape is None or full_r2 is None:
            lines.append("*⚠ One or more accuracy checks came back degenerate this run — the model "
                         "fit diverged. Do not quote this run's accuracy; re-run the pipeline and "
                         "investigate before acting on new recommendations.*")
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


def _compute_glide_path(df, waste_main, reinvest_main):
    """
    Project the portfolio week-by-week from today to the target duration.

    For each cell, the per-cycle discount move is:
       step = (current_disc − target_disc) / TARGET_TIMELINE_WEEKS
       bounded by [0.25, MAX_DISCOUNT_CHANGE_PPT] for safety
    where target_disc is:
       - cut cells (in waste_main):    elbow_discount (often 0% per Stage 6)
       - reinvest cells (in reinvest_main): current + ~3 ppt (one-time strategic move)
       - others:                       current (no change)

    Each cycle's predicted units come from the dual-signal log-log model
    (same math the rest of the system uses).

    Returns a DataFrame with one row per week:
       cycle | weighted_disc% | gross_sales | discount_spend | net_revenue |
       units | cumulative_savings | gap_to_target
    """
    timeline = getattr(cfg, "TARGET_TIMELINE_WEEKS", 12)
    target_wd = getattr(cfg, "TARGET_WEIGHTED_DISCOUNT_PCT", 9.0)

    cut_set = set()
    if not waste_main.empty:
        cut_set = set(waste_main["cell_id"].dropna())
    rinv_disc = {}
    if not reinvest_main.empty and "rec_discount_final" in reinvest_main.columns:
        rinv_disc = (reinvest_main.dropna(subset=["rec_discount_final"])
                                  .set_index("cell_id")["rec_discount_final"]
                                  .to_dict())

    cells = []
    for _, r in df.iterrows():
        cid = r.get("cell_id")
        mrp = float(r.get("mrp", 0))
        cur_d = float(r.get("current_discount_pct", 0))
        cur_u = float(r.get("current_units_day", 0))
        if mrp <= 0 or cur_u <= 0:
            continue
        elast = float(r.get("price_elasticity", r.get("elasticity", -1.5)))
        badge = float(r.get("badge_sensitivity", r.get("discount_sensitivity", 0.0)))

        # Target & per-cycle step
        if cid in rinv_disc:
            target = float(rinv_disc[cid])
        elif cid in cut_set:
            # Target = historical floor (proven safe) if config flag is on,
            # else the margin-optimal elbow (often 0%).
            elbow = float(r.get("elbow_discount_pct", 0))
            if getattr(cfg, "USE_HISTORICAL_FLOOR_TARGET", False):
                hist_floor = float(r.get("historical_floor_disc", elbow))
                # Use max(elbow, floor) — don't go below the proven-safe level
                target = max(0.0, max(elbow, hist_floor))
            else:
                target = max(0.0, elbow)
        else:
            target = cur_d

        # Same step rule as Stage 7:
        #   - gap < MIN_DISCOUNT_CHANGE_PPT: one-shot cut/raise
        #   - else: step = max(MIN, gap/timeline)
        # NO upper cap — TARGET_TIMELINE_WEEKS is the binding constraint,
        # so every cell closes its gap within the user-set duration.
        min_step = float(getattr(cfg, "MIN_DISCOUNT_CHANGE_PPT", 3))
        gap = cur_d - target  # positive = cut, negative = raise
        abs_gap = abs(gap)
        if abs_gap < 0.1:
            step = 0.0
        elif abs_gap <= min_step:
            step = abs_gap  # one-shot
        else:
            raw_step = abs_gap / float(timeline)
            step = max(min_step, raw_step)
        # apply direction
        if gap < 0:
            step = -step
        cells.append({
            "mrp": mrp, "cur_d": cur_d, "cur_u": cur_u,
            "elast": elast, "badge": badge,
            "target": target, "step": step,
        })

    # Simulate cycles 0..N inclusive
    def _predict_units(c, d):
        cur_p = c["mrp"] * (1 - c["cur_d"] / 100)
        new_p = c["mrp"] * (1 - d / 100)
        if cur_p <= 0 or new_p <= 0:
            return c["cur_u"]
        try:
            mult = (new_p / cur_p) ** c["elast"] * np.exp(c["badge"] * (d - c["cur_d"]))
            return max(c["cur_u"] * mult, 0.01)
        except Exception:
            return c["cur_u"]

    rows = []
    today_spend = None
    for cycle in range(int(timeline) + 1):
        gross = 0.0; spend = 0.0; units = 0.0
        for c in cells:
            # Discount at this cycle = cur - cycle * step, clamped to target
            d = c["cur_d"] - cycle * c["step"]  # step is positive for cutting
            if c["step"] > 0:   # cutting → d going down → clamp to target from below
                d = max(d, c["target"])
            elif c["step"] < 0: # raising disc → d going up → clamp to target from above
                d = min(d, c["target"])
            u = _predict_units(c, d)
            gross += c["mrp"] * u * 30
            spend += c["mrp"] * d / 100 * u * 30
            units += u * 30
        wdisc = spend / gross * 100 if gross > 0 else 0
        if cycle == 0:
            today_spend = spend
        rows.append({
            "cycle":                cycle,
            "label":                "Today" if cycle == 0 else f"Week {cycle}",
            "weighted_discount_pct":round(wdisc, 2),
            "gross_sales_inr":      round(gross, 0),
            "discount_spend_inr":   round(spend, 0),
            "net_revenue_inr":      round(gross - spend, 0),
            "total_units":          round(units, 0),
            "cumulative_savings":   round(today_spend - spend, 0),
            "gap_to_target_ppt":    round(wdisc - target_wd, 2),
            "reached_target":       bool(wdisc <= target_wd + 0.05),
        })
    out = pd.DataFrame(rows)
    # Trim trailing rows that are identical to the prior row — once all
    # cells have reached their floor, the table flatlines and adding more
    # rows just clutters the view. Keep one "settled" row to make it
    # explicit that the plan completes there.
    if len(out) > 1:
        last_wdisc = float(out.iloc[-1]["weighted_discount_pct"])
        keep_to = len(out)
        for i in range(len(out) - 1, 0, -1):
            if abs(float(out.iloc[i]["weighted_discount_pct"]) - last_wdisc) < 0.01:
                keep_to = i + 1
            else:
                break
        # Keep one extra "plan complete" row beyond the last change
        keep_to = min(keep_to + 0, len(out))
        out = out.iloc[:keep_to].reset_index(drop=True)
    return out


def _build_leakage_summary(df: pd.DataFrame) -> dict:
    """
    Per-cell 'real vs borrowed vs stolen' + inelastic flag, for the Leakage
    sheet. Surfaces the cells where promo uplift is least real (highest leakage)
    and the cells where discounting can't pay (inelastic).
    """
    if df is None or df.empty or "true_incremental_frac" not in df.columns:
        return {"available": False}

    rows = []
    for _, r in df.iterrows():
        title = str(r.get("title", r.get("cell_id", "")))[:26]
        city = r.get("city", "")
        rows.append({
            "label": f"{title} · {city}" if city else title,
            "pull_forward": float(r.get("pull_forward", 0.0)),
            "cannibalization": float(r.get("cannibalization", 0.0)),
            "true_incremental_frac": float(r.get("true_incremental_frac", 1.0)),
            "is_inelastic": bool(r.get("is_inelastic", False)),
            "abs_elasticity": float(r.get("abs_elasticity", 0.0)),
            "leakage_confidence": str(r.get("leakage_confidence", "n/a")),
        })
    rows.sort(key=lambda x: x["pull_forward"] + x["cannibalization"], reverse=True)

    # Only cells whose leakage was ACTUALLY measured count toward the headline —
    # 'no_promo', 'always_promo', 'no_variation', failed ('n/a') etc. were not.
    not_measured = {"no_promo", "always_promo", "no_variation", "n/a", "unavailable"}
    promo = [x for x in rows if x["leakage_confidence"] not in not_measured]
    n_inelastic = sum(1 for x in rows if x["is_inelastic"])
    n_high_leak = sum(1 for x in promo if (x["pull_forward"] + x["cannibalization"]) >= 0.20)
    med_true = (float(np.median([x["true_incremental_frac"] for x in promo]))
                if promo else 1.0)
    return {
        "available": True,
        "cells": rows,
        "n_cells": len(rows),
        "n_with_promo": len(promo),
        "n_inelastic": n_inelastic,
        "n_high_leakage": n_high_leak,
        "median_true_incremental": round(med_true, 3),
    }


def _compute_model_accuracy(model_output: dict) -> dict:
    """
    Extract Stage 4 model diagnostics into a brand-team-friendly accuracy
    summary. Returns a dict with the raw metrics + a plain-English tier
    (Strong / Moderate / Weak / Unreliable).

    Tier thresholds (calibrated so 'Strong' requires genuine production
    quality for CPG SKU x city x day data):
      Strong:     test R^2 >= 0.70 AND aggregated MAPE <= 25%
      Moderate:   test R^2 >= 0.40 AND aggregated MAPE <= 50%
      Weak:       test R^2 >= 0.10 AND aggregated MAPE <= 80%
      Unreliable: anything below

    These are also written as live IF() formulas in the Excel report so the
    user can see the math and tune the bar without touching code.
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

    # ── Decision-model held-out accuracy (the HONEST headline) ──────
    # This is the price/badge curve that actually sets recommendations,
    # scored on held-out data WITHOUT the lag/momentum features that inflate
    # the full-model R² above. Quote THIS to a buyer. Falls back to the
    # full-model number if Stage 4 didn't emit it (older runs).
    dec_r2_bin   = float(d.get("decision_test_r2_bin",  agg_r2))
    dec_mape_bin = float(d.get("decision_test_mape_bin", agg_mape))
    dec_r2_daily = float(d.get("decision_test_r2",      test_r2))
    dec_mape_day = float(d.get("decision_test_mape",    daily_mape))

    # Tier is now based on the DECISION model — what governs prices.
    if dec_r2_bin >= 0.70 and dec_mape_bin <= 25:
        tier = "Strong"
    elif dec_r2_bin >= 0.40 and dec_mape_bin <= 50:
        tier = "Moderate"
    elif dec_r2_bin >= 0.10 and dec_mape_bin <= 80:
        tier = "Weak"
    else:
        tier = "Unreliable"

    return {
        "available": True,
        "tier":             tier,
        # decision model (the engine that sets prices) — honest headline
        "decision_r2_bin":   round(dec_r2_bin, 3),
        "decision_mape_bin": round(dec_mape_bin, 1),
        "decision_r2_daily": round(dec_r2_daily, 3),
        "decision_mape_daily": round(dec_mape_day, 1),
        # full statistical model (incl. momentum) — context only
        "train_r2_log":     round(train_r2, 3),
        "test_r2_log":      round(test_r2, 3),
        "test_mape_daily":  round(daily_mape, 1),
        "test_mape_agg":    round(agg_mape, 1),
        "test_r2_agg":      round(agg_r2, 3),
        "n_train":          n_train,
        "n_test":           n_test,
    }


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

    def _finite_or_none(v):
        """Non-finite metrics (a diverged fit) become null — never emit
        Infinity/NaN, which is invalid strict JSON and reads as a real value."""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return round(f, 3) if np.isfinite(f) else None

    output = {
        "model_diagnostics": {
            "overall_holdout_mape": _finite_or_none(diagnostics.get("test_mape")),
            "overall_holdout_r2": _finite_or_none(diagnostics.get("test_r2")),
            "decision_holdout_r2_bin": _finite_or_none(diagnostics.get("decision_test_r2_bin")),
            "decision_holdout_mape_bin": _finite_or_none(diagnostics.get("decision_test_mape_bin")),
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
