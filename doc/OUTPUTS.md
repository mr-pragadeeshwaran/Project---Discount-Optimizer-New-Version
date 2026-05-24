# Output Files Reference

Every pipeline run creates a timestamped folder under `v4_outputs/`:

```
v4_outputs/20260524_165218/
├── outliers_removed.csv         ← audit of statistical outliers
├── fact_table.csv               ← cleaned, flagged daily data
├── features.csv                 ← model-ready features
├── elasticity_estimates.csv     ← per-cell elasticity output of Stage 4
├── recommendations.csv          ← per-cell THIS WEEK action (price-led)
├── waste.csv                    ← Stage 8 cuts list
├── reinvest.csv                 ← Stage 8 strategic reinvest list
├── per_cell_detail.json         ← full per-cell payload for dashboard
├── WASTE_REINVEST_REPORT.md     ← human-readable flywheel report (open first)
└── BRAND_DASHBOARD.html         ← interactive 4-view HTML
```

This page is the column-by-column reference for everything above.

---

## 1. `outliers_removed.csv`

Produced by `stage2_preparation/prepare.py`. Every row is a daily observation that was removed from training because its `log(units)` was more than `OUTLIER_Z_THRESHOLD` (default 3.0) standard deviations from the cell's own mean.

| Column | Description |
|---|---|
| `cell_id` | `{product_id}_{grammage}_{city}` |
| `product_id` | Blinkit SKU |
| `grammage` | `500g`, `1kg`, etc. |
| `city` | City name |
| `date` | Date of the outlier |
| `offtake_qty` | Actual units sold that day |
| `cell_mean_units` | Geometric-mean baseline for the cell |
| `z_score` | `(log_units − cell_mean_log) / cell_std_log` |
| `direction` | `HIGH` (spike) or `LOW` (dip) |
| `discount_pct` | Discount that day |
| `availability_pct` | Availability that day (helps spot stockouts that escaped the 50% OSA filter) |
| `reason` | Human-readable explanation |

**How to use it.** Review monthly. If you spot a pattern — e.g. a cluster of HIGH spikes in a particular week — that week was probably an undeclared promo. Add it to `PLATFORM_EVENT_WINDOWS` in `v4_config.py` so future runs treat those days as events (excluded from training) instead of outliers.

---

## 2. `fact_table.csv`

Produced by `stage2_preparation/prepare.py`. One row per `(product_id × grammage × city × date)`. The cleaned, flagged, audit-trail-ready version of the raw data.

Key columns beyond the raw input:

| Column | Description |
|---|---|
| `stable_mrp` | 90th-percentile MRP per (product, grammage). Used as the reference "label price" — the raw daily MRP wobbles, this doesn't. |
| `discount_pct_actual` | `WT_DISCOUNT_PCT`, clipped to `[0, 80]` |
| `selling_price` | `stable_mrp × (1 − discount/100)` — the consumer-facing price |
| `is_oos_day` | 1 if `WT_AVAILABILITY_PCT < OSA_OOS_THRESHOLD` (default 50) |
| `is_event_day` | 1 if date is in `PLATFORM_EVENT_WINDOWS` or festival calendar |
| `is_festival` | 1 if national festival (Diwali, Holi, etc.) |
| `is_outlier` | 1 if flagged by per-cell z-score detection (see `outliers_removed.csv`) |
| `is_regular_day` | 1 if `not event AND not OOS AND not outlier`. **Used for training.** |
| `outlier_reason` | "Statistical outlier (\|z\|>threshold)" if applicable |
| `cell_id` | Unique cell identifier |

---

## 3. `features.csv`

Produced by `stage3_features/features.py`. Same grain as `fact_table.csv`, plus 20 engineered columns.

| Feature | What it is | Why we need it |
|---|---|---|
| `log_price` | `ln(selling_price)` | Primary signal — coefficient is the price elasticity |
| `log_units` | `ln(offtake_qty)` (floored at 0.1) | Target |
| `discount_pct` | Same as `discount_pct_actual` | Used to compute `badge_resid` in Stage 4 |
| `osa_rolling_7d` | 7-day rolling availability ÷ 100 | Smooths supply noise |
| `log_ad_sov` | `ln(1 + 7-day rolling ad SoV)` | Ad intensity control |
| `rpi` | `selling_price / competitor_price` | Competitive position |
| `is_weekend` | 1 for Sat/Sun | Weekend demand lift |
| `month_2`…`month_12` | Monthly dummies (Jan = baseline) | Seasonality |
| `price_surprise`, `discount_surprise`, `log1p_discount`, `is_deep_promo` | Earlier-design features | **Computed but not used by Stage 4** — kept for backward compatibility / inspection |

---

## 4. `elasticity_estimates.csv`

Produced by `stage4_model/elasticity.py`. One row per cell.

| Column | Meaning |
|---|---|
| `product_id`, `grammage`, `city`, `category`, `title`, `cell_id`, `stable_mrp` | Identification |
| `avg_selling_price`, `avg_units`, `avg_discount_pct` | Cell-history averages |
| `disc_pct_std`, `n_discount_levels`, `n_observations`, `n_train` | Data-quality stats |
| **`price_elasticity`** | Final per-cell elasticity (negative; clipped to `[-4, -0.3]`) |
| `price_elasticity_global` | Category median used as shrinkage prior |
| `price_elasticity_se`, `_lower`, `_upper` | Standard error + 95% CI |
| **`badge_sensitivity`** | Per-cell shrunk slope on `badge_resid` |
| `badge_sensitivity_global`, `_se` | Category prior + SE |
| `elasticity`, `discount_sensitivity`, `avg_price` | Backwards-compat aliases used by Stages 5–8 |

See [doc/MODEL.md](MODEL.md) for the full design rationale.

---

## 5. `recommendations.csv` — **the per-cell weekly action**

Produced by `stage7_guardrails/guardrails.py`. Sorted by tier priority, then by savings.

### Columns (price-led order)

| Section | Column | Meaning |
|---|---|---|
| **Identity** | `product_id`, `city`, `category`, `title`, `mrp`, `cell_id` | — |
| **Decision** | `tier` | `Strong Cut` / `Trade-off` / `Hold` / `Increase` / `Do Not Act` |
| | `confidence` | `High` / `Medium` / `Low` / `Needs Experiment` |
| | `quality_note` | "OK" or e.g. "demand grew 24.7x over period (launch/ramp)" |
| **Price (primary)** | `current_price` | What customer pays today |
| | `rec_price` | What you should set this week |
| | `price_change_inr` | `rec_price − current_price` (positive = price up) |
| | `price_change_pct` | Same in % terms |
| **Discount (derived)** | `current_discount_pct`, `rec_discount_pct` | For Blinkit platform entry |
| **Volume & Revenue** | `current_units_day`, `rec_units_day`, `rec_vol_change_pct` | |
| | `current_revenue_day`, `rec_revenue_day`, `rec_rev_change_pct` | |
| | `rec_monthly_savings` | Discount ₹ saved per month at the recommended price |
| **Model inputs** | `elasticity`, `badge_sensitivity` | For audit / understanding |
| **Guardrails** | `guardrail_floor_ok`, `guardrail_competitor_ok`, `guardrail_change_ok`, `is_throttled`, `phasing_plan` | Were rules triggered? |
| **Reference** | `elbow_discount_pct`, `elbow_price`, `monthly_savings`, `elbow_marginal_roi` | Full-elbow (multi-cycle) target |

### Tier definitions (in `stage7_guardrails/guardrails.py`)

| Tier | Criteria (THIS-CYCLE metrics) |
|---|---|
| **Strong Cut** | `rec_savings ≥ ₹5K/mo` AND `\|rec_vol_drop\| ≤ 8%` AND confidence ∈ {High, Medium}. **Fast-track approve.** |
| **Trade-off** | `rec_savings > 0` AND `\|rec_vol_drop\| ≤ 20%`. **Review individually.** |
| **Hold** | `\|gap_to_elbow\| ≤ 2 ppt`. Already near optimal. |
| **Increase** | `gap_to_elbow < −2 ppt` (cell wants more discount). Rare under current cost structure. |
| **Do Not Act** | Confidence = "Needs Experiment". Run a price test before any change. |

---

## 6. `waste.csv` — Stage 8 cuts list

Produced by `stage8_output/waste_reinvest.py` → `_build_waste_table`. Same cells as the Stage 7 "Strong Cut" + "Trade-off" tiers, but with **three** price views (now / this week / eventual) and only the columns the brand team needs:

| Column | Meaning |
|---|---|
| `product_id`, `title`, `city`, `mrp` | Identity |
| `current_price` | Last 30-day average regular-day price |
| `new_price` | Margin-optimal target (the multi-cycle endpoint) |
| `price_increase_inr` | How much to eventually raise the price |
| `current_discount_pct`, `elbow_discount_pct`, `wasted_discount_pct` | Same view in % terms |
| `wasted_inr_per_month` | Monthly discount waste at the current price |
| `vol_change_pct` | Predicted volume change if going all the way to elbow |
| `confidence`, `quality_note` | Inherited from Stage 5 |
| `logic_explanation` | One-sentence summary leading with selling price |
| `this_week_price` | Throttled action this cycle (set by `_apply_guardrails`) |
| `rec_discount_final` | Same in % terms (Blinkit entry) |

---

## 7. `reinvest.csv` — Stage 8 strategic reinvestment list

| Column | Meaning |
|---|---|
| `product_id`, `title`, `city`, `mrp` | Identity |
| `current_price` | Last 30-day average regular-day price |
| `new_price` | Proposed price after +3 ppt deeper discount |
| `price_drop_inr` | How much cheaper |
| `current_discount_pct`, `recommended_discount_pct` | Discount % view |
| `volume_lift_pct` | Projected +volume from the deeper discount |
| `extra_volume_units_per_month` | Same in absolute units |
| `budget_needed_inr_per_month` | Additional discount spend needed |
| `expected_margin_lift_inr_per_month` | Contribution margin change (may be positive = pure win) |
| `margin_sacrifice_pct` | `+` = losing margin, `−` = volume gain outweighs price drop |
| `reinvestment_efficiency` | Extra units per ₹100 of budget |
| `confidence`, `quality_note` | Inherited from Stage 5 |
| `funded_by` | Top-3 waste cells whose cuts could pay for this reinvestment |
| `logic_explanation` | One-sentence summary leading with selling price |

---

## 8. `WASTE_REINVEST_REPORT.md` — **the Monday-morning read**

Generated markdown. Top-down structure:

```
## Flywheel: Portfolio Rebalancing (selling-price view)
  Target / Current / After cuts / After cuts + reinvest
  Per-category current discount
  Monthly savings, budget redirected, extra units, net margin

  (multi-cycle journey note)

### Confidence breakdown of waste pool

## Q1: Where Am I Overspending on Discount?
  (waste table, sorted by ₹ wasted/month)

## Q2: Where Can I Reinvest the Saved Money?
  (reinvest table, sorted by extra units/month)

## Needs Price Test (Low Confidence)
  (cells where the model can't act — need pilot)
```

The "Why" / `logic_explanation` columns are dropped from the markdown tables for readability but are present in the CSVs.

---

## 9. `per_cell_detail.json`

Used by the dashboard. Schema:

```json
{
  "model_diagnostics": {
    "overall_holdout_mape": 52.5,
    "overall_holdout_r2":   0.40,
    "n_train": 6387,
    "n_test":  2148
  },
  "summary": {
    "total_wasted":    2028905,
    "total_reinvest":  103129,
    "flywheel": { ... }
  },
  "cells": [
    {
      "product_id": 3583, "city": "Bangalore", ...,
      "current_discount_pct": 21.3,
      "elbow_discount_pct":   0,
      "curve_points": [ {"discount_pct": 0, "predicted_units": ...}, ... ],
      "curve_params": {"A": ..., "B": ..., "C": ..., "D": ...}
    }
  ]
}
```

---

## 10. `BRAND_DASHBOARD.html`

Standalone HTML, no server needed. Four views:

1. **Portfolio Summary** — flywheel headline, glide-path to target
2. **Action Queue** — sortable table grouped by tier, "approve" button per row
3. **Cell Detail** — click any row → side-by-side current vs recommended with curves
4. **Export** — generates a Blinkit-format CSV of approved decisions

---

## Reading order for a fresh run

1. **`WASTE_REINVEST_REPORT.md`** — top of the funnel; understand the portfolio move
2. **Strong Cut rows in `recommendations.csv`** — this week's fast-track actions
3. **Reinvest cells in `reinvest.csv`** — strategic growth bets
4. **`outliers_removed.csv`** — sanity check; investigate clusters
5. **`elasticity_estimates.csv`** — only if a recommendation looks wrong; trace back

Skip `fact_table.csv` and `features.csv` unless something downstream looks off — those are audit/replay artifacts.
