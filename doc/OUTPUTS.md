# Output Files Reference

This system produces **two sets of outputs**:

```
v4_outputs/
├── _readiness/                        ← LAYER 1: Data Readiness
│   ├── DATA_READINESS_REPORT.md       ← one-page brand-team verdict
│   ├── per_cell_assessment.csv        ← audit row per cell
│   ├── per_product_assessment.csv     ← per-product roll-up
│   └── per_city_assessment.csv        ← per-city roll-up
│
└── 20260524_165218/                   ← LAYER 2: weekly pipeline run (timestamped)
    ├── outliers_removed.csv           ← audit of statistical outliers
    ├── fact_table.csv                 ← cleaned, flagged daily data
    ├── features.csv                   ← model-ready features (33 columns)
    ├── elasticity_estimates.csv       ← per-cell elasticity + confidence (Stage 4)
    ├── recommendations.csv            ← per-cell THIS WEEK action (price-led)
    ├── waste.csv                      ← Stage 8 cuts list
    ├── reinvest.csv                   ← Stage 8 strategic reinvest list
    ├── per_cell_detail.json           ← full per-cell payload for dashboard
    ├── WASTE_REINVEST_REPORT.xlsx     ← McKinsey-style formula-driven workbook (open first)
    ├── WASTE_REINVEST_REPORT.md       ← same content as plain Markdown
    └── BRAND_DASHBOARD.html           ← interactive 4-view HTML
```

This page is the column-by-column reference for everything above.

---

## 0. Layer 1 outputs — Data Readiness

Produced by `scripts/diagnostics/data_readiness_report.py`. Run **first** for any brand engagement.

### `DATA_READINESS_REPORT.md` — the one-page brand-facing verdict

Markdown document with these sections (in order):

1. **Verdict** — `GREEN` / `YELLOW` / `RED` with a single-paragraph explanation
2. **Numbers at a glance** — SKUs, cities, cells, categories, days of history, median train rows per cell, median discount levels per cell, aggregated R²(units), MAPE, pooled test log-R²
3. **Per-cell confidence breakdown** — `HIGH` / `MEDIUM` / `LOW` / `DO_NOT_ACT` counts with the action implied for each tier
4. **By product** — table with `n_cells`, median train rows, median discount levels, median aggregated R², tier counts, % actionable
5. **By city** — same metrics rolled by city
6. **Gap analysis** — three numbered lists:
   - Thin-data cells (< 45 train rows)
   - Low-variation cells (< 7 distinct discount levels) — *prime price-test candidates*
   - Cells with poor model fit (train R² < 0.30) — *investigate upstream data*
7. **How to read this report** — built-in glossary
8. **Next steps** — verdict-specific action plan

This file is designed to be handed to a brand client as-is. It is the first invoice-able artifact of an engagement.

### `per_cell_assessment.csv` — audit trail

One row per (product × grammage × city) cell. Columns:

| Column | Meaning |
|---|---|
| `product_id`, `grammage`, `city`, `category` | Identification |
| `n_train`, `n_discount_levels`, `n_observations` | Data depth |
| `price_elasticity`, `price_elasticity_se` | Final shrunk elasticity + standard error |
| `cell_train_r2`, `cell_test_r2` | In-sample and held-out R² for this cell |
| `aggregated_3pp_r2` | Test R² at the 3pp discount-bin grain (pricing-decision metric) |
| `data_depth_score`, `variation_score` | Sub-scores 0–100 used for the verdict |
| `meets_3pp_target` | 1 if `aggregated_3pp_r2 ≥ 0.70` |
| `confidence_score` | Final 0–100 model-confidence score |
| `confidence_tier` | `HIGH` / `MEDIUM` / `LOW` / `DO_NOT_ACT` |
| `conf_density`, `conf_variation`, `conf_fit`, `conf_plausibility`, `conf_tightness` | Sub-scores feeding the composite |

### `per_product_assessment.csv` and `per_city_assessment.csv`

Per-product / per-city roll-ups of the per-cell file. Key columns:

| Column | Meaning |
|---|---|
| `n_cells`, `n_train_total` | Volume |
| `median_n_train_per_cell`, `median_n_discount_levels` | Median data depth |
| `median_aggregated_R2` | Median across cells of `aggregated_3pp_r2` |
| `cells_HIGH`, `cells_MEDIUM`, `cells_LOW`, `cells_DONOTACT` | Tier counts |
| `pct_actionable` | `(HIGH + MEDIUM) / n_cells × 100` |

---

## Layer 2 outputs (weekly pipeline run)

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

Produced by `stage3_features/features.py`. Same grain as `fact_table.csv`, plus 33 engineered columns. **The lag / momentum / DOW block (the bottom rows of the table) was added in May 2026** — see [MODEL_EXPERIMENTS.md](MODEL_EXPERIMENTS.md) for the experimental evidence.

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
| `dow_1`…`dow_6` | Day-of-week dummies (Mon = baseline) | Within-week grocery shopping rhythm |
| `lag1_log_units` | Yesterday's `log_units` | Demand momentum / autocorrelation |
| `lag7_log_units` | 7-day-ago `log_units` | Weekly seasonality residual |
| `rolling_mean_7d_log_units` | Mean of `log_units` over the last 7 days (shifted) | Smoothed baseline |
| `rolling_mean_14d_log_units` | Same, 14 days | Slower-moving baseline |
| `lag1_log_price`, `lag1_discount` | Yesterday's price / discount | Captures lagged response |
| `price_surprise`, `discount_surprise`, `log1p_discount`, `is_deep_promo` | Earlier-design features | Computed but not in the Stage 4 formula — kept for inspection |

**Why these specific features:** the lag / momentum block was the single biggest accuracy lever in the May 2026 experiments. Adding them cut within-cell test residual variance roughly in half (median within-cell R² moved from −0.43 to −0.04) and improved the aggregated 3pp-bin R² from 0.928 to 0.970.

---

## 4. `elasticity_estimates.csv`

Produced by `stage4_model/elasticity.py`. One row per cell.

| Column | Meaning |
|---|---|
| `product_id`, `grammage`, `city`, `category`, `title`, `cell_id`, `stable_mrp` | Identification |
| `avg_selling_price`, `avg_units`, `avg_discount_pct` | Cell-history averages |
| `disc_pct_std`, `n_discount_levels`, `n_observations`, `n_train` | Data-quality stats |
| `historical_floor_disc` | The cell's lower-quartile discount in the last 90 days. Stage 7/8 use this as the *target* of the multi-week glide path when `USE_HISTORICAL_FLOOR_TARGET=True` (default). |
| **`price_elasticity`** | Final per-cell elasticity (negative; clipped to `[-4, -0.3]`) |
| `price_elasticity_global` | Category median used as shrinkage prior |
| `price_elasticity_se`, `_lower`, `_upper` | Standard error + 95% CI |
| **`badge_sensitivity`** | Per-cell shrunk slope on `badge_resid` |
| `badge_sensitivity_global`, `_se` | Category prior + SE |
| `elasticity`, `discount_sensitivity`, `avg_price` | Backwards-compat aliases used by Stages 5–8 |
| `cell_train_r2` | In-sample R² when the category model is scored on this cell's own training rows |
| `cell_test_r2` | Held-out R² for this cell — noise-limited; see MODEL_EXPERIMENTS.md for why this is not the action gate |
| **`confidence_score`** | 0–100 composite (May 2026). Combines 5 sub-signals (below). |
| **`confidence_tier`** | `HIGH` (≥70) / `MEDIUM` (50–70) / `LOW` (30–50) / `DO_NOT_ACT` (<30) |
| `conf_density` | Sub-score from `n_train / 120` clipped to 1 (weight 0.25) |
| `conf_variation` | Sub-score from `n_discount_levels / 15` clipped to 1 (weight 0.20) |
| `conf_fit` | Sub-score from `cell_train_r2 / 0.50` clipped to 1 (weight 0.20) |
| `conf_plausibility` | 1 if elasticity in [−4, −0.3], else 0 (weight 0.15) |
| `conf_tightness` | Sub-score from `|elast| / SE / 4` clipped to 1 (weight 0.20) |

**Reading the confidence:**
- `HIGH`: act on automatic recommendations.
- `MEDIUM`: act but with smaller throttled steps; review weekly.
- `LOW`: no Strong Cut allowed in Stage 7 (capped at Trade-off). Manager review before any move.
- `DO_NOT_ACT`: locked out of automatic price moves (Stage 7 forces tier = "Do Not Act"). Run a structured 4-week A/B price test to gather signal.

See [MODEL.md](MODEL.md) for the model design rationale and [MODEL_EXPERIMENTS.md](MODEL_EXPERIMENTS.md) for the evidence behind the confidence score.

---

## 5. `recommendations.csv` — **the per-cell weekly action**

Produced by `stage7_guardrails/guardrails.py`. Sorted by tier priority, then by savings.

### Columns (price-led order)

| Section | Column | Meaning |
|---|---|---|
| **Identity** | `product_id`, `city`, `category`, `title`, `mrp`, `cell_id` | — |
| **Decision** | `tier` | `Strong Cut` / `Trade-off` / `Hold` / `Increase` / `Do Not Act` |
| | `confidence` | Curve-based: `High` / `Medium` / `Low` / `Needs Experiment` (Stage 5) |
| | `confidence_tier` | Model-based: `HIGH` / `MEDIUM` / `LOW` / `DO_NOT_ACT` (Stage 4, May 2026) |
| | `confidence_score` | 0–100 composite — the audit number behind `confidence_tier` |
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
| **Strong Cut** | `rec_savings ≥ ₹5K/mo` AND `\|rec_vol_drop\| ≤ 8%` AND curve-confidence ∈ {High, Medium} AND **model `confidence_tier` ∈ {HIGH, MEDIUM}**. **Fast-track approve.** |
| **Trade-off** | `rec_savings > 0` AND `\|rec_vol_drop\| ≤ 20%`. **Review individually.** Available also to LOW model-confidence cells. |
| **Hold** | `\|gap_to_elbow\| ≤ 2 ppt`. Already near optimal. |
| **Increase** | `gap_to_elbow < −2 ppt` (cell wants more discount). Rare under current cost structure. |
| **Do Not Act** | EITHER curve-confidence = "Needs Experiment" OR model `confidence_tier == DO_NOT_ACT`. Run a structured price test before any change. |

The model-confidence gate (the **bold** clauses above) was added in May 2026. It is the hard safety rail for scale-up: a cell with insufficient data, low price variation, or a wide elasticity CI cannot become Strong Cut regardless of how attractive its savings number looks.

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

## 8. `WASTE_REINVEST_REPORT.xlsx` — **the Monday-morning read (Excel)**

McKinsey-style 6-sheet workbook built with `openpyxl`. Every number on
the Summary, By Product, and Glide Path sheets is a **live formula**
referencing the hidden Data sheet — you can edit Data cells and watch
everything recompute. Only old-school formulas used (SUM, SUMPRODUCT,
IF, AND, LEFT) so it works in any Excel 2010 or later.

### Sheet 1: Summary

Top of the workbook. Contains in order:

1. **Portfolio table** — Today / After cuts / After cuts + invest, with rows for:
   Gross sales / Discount spend / Net revenue / Units / **Weighted discount %** (live formula `=B_spend / B_gross * 100`).
2. **Target and gap** — `B12 − B14` formula for the live gap to target.
3. **This week's plan** — Cuts / Reinvest / Net change with cells, spend Δ, units Δ.
4. **Model accuracy** — three metrics + nested-IF formula for the **Strong / Moderate / Weak / Unreliable** tier. The tier thresholds are visible in cell A35 so the brand team can rewrite them in-cell.

### Sheet 2: Glide Path  ← *NEW*

The week-by-week roadmap. Two parts:

| Section | What it shows |
|---|---|
| Header card | Today vs end-of-roadmap projection (weighted disc %, monthly spend, net revenue, gap closure) |
| Week-by-week table | One row per cycle (0..N): Cycle / Label / Weighted Disc % / Gross Sales / Discount Spend / Net Revenue / Units / Cumulative Savings / Gap to Target |

Trailing identical rows are trimmed — once every cell reaches its
floor, the table stops to avoid showing flat weeks.

See [FLYWHEEL.md](FLYWHEEL.md) for the math.

### Sheet 3: By Product

Per-SKU breakdown of the same 5 metrics as the portfolio table. Uses
`LEFT(cell_id, N) = "{pid}_{grm}_"` prefix-matching against the Data
sheet — robust against title variants.

### Sheet 4: Price Lifts (cuts list)

| Column | What |
|---|---|
| Product, City, MRP | Identification |
| Now | Current selling price (Rs.) |
| This Week | Throttled price for this Monday |
| Wasted/mo | Full multi-cycle savings opportunity in Rs. |
| Conf | Inherited from Stage 5 |

A confidence legend explains the High/Medium/Low rules in-sheet.

### Sheet 5: Price Drops (strategic reinvest list)

| Column | What |
|---|---|
| Product, City, MRP | — |
| Now / New | Current vs proposed selling price |
| Vol Δ | Projected % volume lift |
| +Units/mo | Absolute volume gain |
| Budget/mo | Additional discount spend |

### Sheet 6: Needs Test

Cells the model isn't confident enough to act on. A/B test these.

### Sheet 7: Data (hidden)

Raw per-cell data — single source of truth that all the formula sheets reference:

```
cell_id | product | grammage | city | mrp |
cur_disc_pct | cur_units_day | cur_price |
aftercut_disc_pct | aftercut_units | aftercut_price |
final_disc_pct | final_units | final_price |
confidence | elasticity | category
```

`Format ▸ Sheet ▸ Unhide` if you want to inspect or what-if it.

### `WASTE_REINVEST_REPORT.md` — same content, plain Markdown

Identical structure to the Excel, just as text. Use for git diffs,
email, grep, anything where Excel is awkward.

---

## 9. `per_cell_detail.json`

Used by the dashboard. Schema:

```json
{
  "model_diagnostics": {
    "overall_holdout_mape": 24.0,
    "overall_holdout_r2":   0.93,
    "n_train": 3286,
    "n_test":  654
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

### Onboarding a new brand — read these in order

1. **`v4_outputs/_readiness/DATA_READINESS_REPORT.md`** — the verdict (GREEN/YELLOW/RED) and what to do next. **Read FIRST.**
2. **`per_product_assessment.csv`** and **`per_city_assessment.csv`** — find the actionable-% per segment so you know where to ship and where to test.
3. **Gap analysis section of the readiness report** — feeds the price-test design for LOW / DO_NOT_ACT cells.

### Weekly cadence (production run)

1. **`WASTE_REINVEST_REPORT.xlsx`** — open this first.
   - **Summary sheet**: portfolio numbers + this-week plan + accuracy tier.
   - **Glide Path sheet**: week-by-week projection over the 3-month duration.
   - **Price Lifts / Drops / Needs Test**: detailed action lists.
2. **`BRAND_DASHBOARD.html`** — open in browser, walk through the Action Queue with the brand team.
3. **Strong Cut rows in `recommendations.csv`** — this week's fast-track actions with `phasing_plan` column showing the full multi-week glide.
4. **Reinvest cells in `reinvest.csv`** — strategic growth bets.
5. **`outliers_removed.csv`** — sanity check; investigate clusters monthly.
6. **`elasticity_estimates.csv`** — only if a recommendation looks wrong; trace back. Inspect `confidence_score` and the five sub-scores (`conf_density` etc.) to see WHY a cell is HIGH or LOW model-confidence.

### Auditing a specific tier decision

To answer "why was THIS cell tiered the way it was?":

| Cell tiered as | Check these columns |
|---|---|
| **Strong Cut** | `confidence_tier ∈ {HIGH, MEDIUM}` AND `confidence ∈ {High, Medium}` AND `rec_monthly_savings ≥ 5000` AND `\|rec_vol_change_pct\| ≤ 8` |
| **Trade-off** | Either was Strong Cut blocked by a single criterion, OR `confidence_tier == LOW` (cap), OR `\|rec_vol_change_pct\| > 8` |
| **Hold** | `\|gap_to_elbow\| ≤ 2 ppt` |
| **Do Not Act** | EITHER `confidence_tier == DO_NOT_ACT` OR `confidence == Needs Experiment` |

Skip `fact_table.csv` and `features.csv` unless something downstream looks off — those are audit/replay artifacts.
