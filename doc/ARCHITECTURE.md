# Architecture — the system end to end

> The complete data flow, every gate, what each stage produces, and how the May 2026 confidence layer wraps around the original 8-stage pipeline. Read this once, then use the deep-dive docs for any single piece.

---

## The two-layer mental model

```
                         RAW DATA (Excel exports per category)
                                       │
                                       ▼
   ┌───────────────────────────────────────────────────────────────────┐
   │                  LAYER 1 — DATA READINESS                         │
   │   "What can THIS brand's data actually deliver?"                  │
   │                                                                   │
   │   Run once at onboarding, then monthly.                           │
   │   Output: DATA_READINESS_REPORT.md with GREEN/YELLOW/RED verdict, │
   │   per-product and per-city actionable %, gap analysis.            │
   └─────────────────────────────────┬─────────────────────────────────┘
                                     │
                              verdict ≠ RED?
                                     │ yes
                                     ▼
   ┌───────────────────────────────────────────────────────────────────┐
   │                  LAYER 2 — WEEKLY PRODUCTION PIPELINE             │
   │   "Given what we know, what price moves this week?"               │
   │                                                                   │
   │   8 stages. Confidence score (from Stage 4) flows through every   │
   │   downstream stage and HARD-GATES Stage 7's tier assignment.      │
   │                                                                   │
   │   Output: BRAND_DASHBOARD, WASTE_REINVEST_REPORT, recommendations │
   └───────────────────────────────────────────────────────────────────┘
```

Both layers share Stages 1–4 (they use the same model). The split is operational:

- **Layer 1** asks *"can we act?"* — and answers it before any action is taken.
- **Layer 2** asks *"what should we act on?"* — and only acts where Layer 1 said yes.

---

## Layer 1 — Data Readiness (the sellable discovery deliverable)

### Why it exists

When you sell pricing tooling, the worst possible outcome is recommending a price move on data that can't support it. Every prior incident in this codebase (a Moong Dal SKU with 16 × volume growth, a Sunflower Oil cell with only 3 train rows) was a *data confidence* problem, not a model problem. Layer 1 catches all of them in a single pass before they ever reach a recommendation.

The Readiness Report is also the **first invoice-able artifact** in a brand engagement. You hand the client a one-page verdict in week 1 that:

1. Says GREEN / YELLOW / RED for the engagement as a whole.
2. Lists per-product and per-city % actionable now.
3. Identifies the specific cells that need a price-test programme before they can be acted on.
4. Includes a verdict-specific next-steps plan.

### What it runs

```
scripts/diagnostics/data_readiness_report.py
   │
   ├── Stage 1: ingest the brand's raw data
   ├── Stage 2: clean + flag (same as production)
   ├── Stage 3: feature engineering (same as production)
   ├── Stage 4: per-category elasticity model + confidence score
   │   ─ NO stage 5/6/7/8 ─
   │
   └── Then on top of Stage 4:
       ─ compute per-cell aggregated R² at 3pp discount bin
       ─ roll up per product and per city
       ─ apply verdict logic (GREEN ≥ 70% actionable, YELLOW ≥ 40%, else RED)
       ─ produce the Markdown report + audit CSVs
```

### What "actionable" means in Layer 1

A cell is **actionable** if and only if its `confidence_tier` ∈ `{HIGH, MEDIUM}`. The tier is computed from a 5-factor score (see Layer 2 → Stage 4 below).

**Verdict bands:**

| Verdict | % cells actionable | What you do |
|---|---|---|
| GREEN | ≥ 70 % | Run the production pipeline. Act on actionable cells, structured price-test on the rest. |
| YELLOW | 40 % – 70 % | Run production *only on actionable cells*. Run a 6–8 week price-test programme on the rest. Expect transition to GREEN in 2–3 months. |
| RED | < 40 % | Do not run production yet. Run an 8–12 week structured price test across all cells first. |

### Outputs from Layer 1

```
v4_outputs/_readiness/
├── DATA_READINESS_REPORT.md      ← the one-page brand-facing deliverable
├── per_cell_assessment.csv       ← audit trail (every cell, every sub-score)
├── per_product_assessment.csv    ← per-product roll-up
└── per_city_assessment.csv       ← per-city roll-up
```

---

## Layer 2 — Weekly Production Pipeline

### Stage-by-stage with the data that flows

```
┌─────────────────────────────────────────────────────────────────────┐
│ STAGE 1 — INGESTION                                                 │
│ Reads input_data/*.xlsx (one file per category in current setup).   │
│ Dedupes on (PRODUCT_ID, DATE, CITY). Filters to OWN_BRAND_PATTERNS  │
│ (everything else is treated as competitor data, used only for RPI). │
│ Loads event calendar (festivals + platform-level event windows).    │
│                                                                     │
│ Out: raw_df, calendar_df                                            │
│ Knobs: SALES_DATA_DIR, OWN_BRAND_PATTERNS, FESTIVAL_DATES,          │
│        PLATFORM_EVENT_WINDOWS                                       │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ STAGE 2 — PREPARATION                                               │
│ Computes stable_mrp (90th percentile per SKU-grammage; not the      │
│ daily MRP which platforms tweak). Reconstructs selling_price as     │
│ stable_mrp × (1 − discount/100).                                    │
│ Flags each day as REGULAR / EVENT (festival or platform window)     │
│ / OOS (availability < OSA_OOS_THRESHOLD).                           │
│ Per-cell outlier detection — drops rows with |z(log_units)| >       │
│ OUTLIER_Z_THRESHOLD computed on REGULAR days only. Audited to       │
│ outliers_removed.csv (one row per dropped observation).             │
│                                                                     │
│ Out: fact_table.csv                                                 │
│ Knobs: OSA_OOS_THRESHOLD, OUTLIER_Z_THRESHOLD,                      │
│        OUTLIER_MIN_OBS_PER_CELL                                     │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ STAGE 3 — FEATURE ENGINEERING                                       │
│ Computes 33 features per (cell × day):                              │
│   PRICE:      log_price, log1p_discount, price_surprise (vs 30d),   │
│               discount_surprise (vs 30d), rpi (vs competitor)       │
│   AVAILABILITY+ADS: osa_rolling_7d, log_ad_sov                      │
│   TIME:       is_weekend, month_2..month_12, dow_1..dow_6           │
│   MOMENTUM:   lag1_log_units, lag7_log_units,                       │
│               rolling_mean_7d_log_units, rolling_mean_14d_log_units │
│   PRICE-LAG:  lag1_log_price, lag1_discount, is_deep_promo          │
│                                                                     │
│ Lag features are the May 2026 addition — they cut within-cell test  │
│ residual variance in half. See doc/MODEL_EXPERIMENTS.md.            │
│                                                                     │
│ Out: features.csv                                                   │
│ Knobs: REFERENCE_PRICE_WINDOW, OSA_ROLLING_WINDOW, AD_ROLLING_WINDOW│
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ STAGE 4 — ELASTICITY MODEL  ★ confidence score lives here ★         │
│                                                                     │
│ For each of N categories (currently 3), fit one Huber-robust OLS:   │
│   log(units) ~ C(sku_city)            ← cell fixed effects          │
│              + log_price              ← within-cell price elasticity│
│              + badge_resid            ← residual badge effect        │
│              + controls (RPI, OSA, ads, weekend, month, DOW, lag)   │
│                                                                     │
│ Trains on REGULAR days in the last TRAIN_LOOKBACK_DAYS days only —  │
│ avoids contamination from launch ramps and old price regimes.       │
│ Time-based 80/20 split (last 20% of dates = test).                  │
│                                                                     │
│ Per-cell elasticity = within-cell raw OLS slope, shrunk toward      │
│ category MEDIAN of raw slopes (a robust prior, not the pooled       │
│ coefficient which leaks cross-SKU variance).                        │
│                                                                     │
│ ★ Confidence score (0-100) per cell, combining:                     │
│     0.25 × density       (n_train / 120 capped at 1)                │
│     0.20 × variation     (n_distinct_prices / 15 capped at 1)       │
│     0.20 × fit           (in-sample R² / 0.50 capped at 1)          │
│     0.15 × plausibility  (1 if elasticity in [-4, -0.3])            │
│     0.20 × tightness     (|elast|/SE / 4 capped at 1)               │
│                                                                     │
│   → tier:  HIGH (≥70) | MEDIUM (50-70) | LOW (30-50) | DO_NOT_ACT   │
│                                                                     │
│ Out: elasticity_estimates.csv                                       │
│ Knobs: TRAIN_LOOKBACK_DAYS, TEST_SPLIT_PCT, ELASTICITY_FLOOR/CEIL,  │
│        N_PRIOR_PRICE, N_PRIOR_BADGE                                 │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ STAGE 5 — SATURATION CURVES                                         │
│ For each cell, sweep selling_price from floor to stable_mrp at      │
│ DISCOUNT_STEP_PCT increments. At each step, predict units using:    │
│   ln(units) = predicted_baseline + elasticity × Δlog_price          │
│                                  + badge_sensitivity × Δdiscount    │
│ Fit a 4PL curve through the swept points (smooth interpolation).    │
│ Assign a curve-based confidence (High / Medium / Low / Needs Expt)  │
│ — orthogonal to the model confidence; both flow forward.            │
│                                                                     │
│ Out: included in per_cell_detail.json                               │
│ Knobs: DISCOUNT_MIN_PCT, DISCOUNT_MAX_PCT, DISCOUNT_STEP_PCT,       │
│        EXTRAPOLATION_FLAG_PCT, STABILITY_VARIATION_THRESHOLD        │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ STAGE 6 — ECONOMICS + ELBOW DETECTION                               │
│ For each price step build the variable-cost ladder:                 │
│   COGS = stable_mrp × DEFAULT_COGS_PCT                              │
│   Commission = selling_price × DEFAULT_COMMISSION_PCT               │
│   Fulfilment = DEFAULT_FULFILLMENT_FEE                              │
│   Contribution margin = revenue − all of the above                  │
│ Marginal ROI = ΔcontributionMargin / ΔdiscountSpend.                │
│ Elbow = the discount level where marginal ROI crosses               │
│ MARGINAL_ROI_THRESHOLD (default 1.0).                               │
│                                                                     │
│ Out: included in recommendations + per_cell_detail.json             │
│ Knobs: DEFAULT_COGS_PCT, DEFAULT_COMMISSION_PCT,                    │
│        DEFAULT_FULFILLMENT_FEE, MARGINAL_ROI_THRESHOLD              │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ STAGE 7 — GUARDRAILS + TIERING  ★ confidence hard-gate ★            │
│                                                                     │
│ 1. Compute target discount per cell:                                │
│      historical_floor if USE_HISTORICAL_FLOOR_TARGET                │
│      else elbow_discount                                            │
│ 2. Throttle the per-cycle move to a step that closes the gap        │
│      within TARGET_TIMELINE_WEEKS, bounded below by                 │
│      MIN_DISCOUNT_CHANGE_PPT.                                       │
│ 3. Compute this-cycle metrics: predicted vol drop, rec savings.     │
│ 4. Assign tier (Strong Cut / Trade-off / Increase / Hold / Do Not   │
│    Act) using THIS-CYCLE metrics, NOT the full multi-cycle gap.     │
│ 5. ★ Hard gate: if confidence_tier == DO_NOT_ACT, force tier =      │
│    "Do Not Act" regardless of savings. If confidence_tier == LOW,   │
│    cap the tier at "Trade-off" (no Strong Cut allowed).             │
│                                                                     │
│ Out: recommendations.csv                                            │
│ Knobs: TARGET_TIMELINE_WEEKS, MIN_DISCOUNT_CHANGE_PPT,              │
│        MIN_MARGIN_PCT, MAX_COMPETITOR_PREMIUM_PCT,                  │
│        TIER_STRONG_CUT_MIN_SAVINGS, TIER_STRONG_CUT_MAX_VOL_DROP    │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ STAGE 8 — WASTE / REINVEST + FLYWHEEL REPORT                        │
│ Partitions cells into waste (cuts that save money) and reinvest     │
│ (cuts that grow volume profitably). Builds a portfolio scenario     │
│ that moves the revenue-weighted discount toward                     │
│ TARGET_WEIGHTED_DISCOUNT_PCT.                                       │
│ Generates the McKinsey-style Excel workbook + Markdown twin.        │
│                                                                     │
│ Out: waste.csv, reinvest.csv,                                       │
│      WASTE_REINVEST_REPORT.xlsx, WASTE_REINVEST_REPORT.md,          │
│      BRAND_DASHBOARD.html                                           │
│ Knobs: TARGET_WEIGHTED_DISCOUNT_PCT,                                │
│        REINVEST_MIN_VOL_LIFT_PCT, REINVEST_MAX_MARGIN_SAC_PCT,      │
│        REINVEST_MIN_ELASTICITY                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## How the confidence score flows from Stage 4 to Stage 7

This is the heart of the May 2026 redesign — the audit trail for "why was THIS cell allowed to be a Strong Cut, but THAT one wasn't?".

```
Stage 4   computes per-cell confidence_score (0-100) + confidence_tier
          and writes them into elasticity_estimates.csv.

Stage 5   carries them through into the curves DataFrame (alongside the
          curve-based confidence which is computed independently).

Stage 6   carries them through into recommendations.csv.

Stage 7   reads confidence_tier on each row before assigning a tier:
            DO_NOT_ACT → tier forced to "Do Not Act"
            LOW        → cannot reach "Strong Cut"; capped at "Trade-off"
            HIGH/MED   → normal tiering rules apply

Stage 8   inherits the final tier from Stage 7 unchanged. Cells locked
          out by the gate never appear in waste.csv or reinvest.csv —
          they show up only in "Do Not Act".
```

So a Strong Cut recommendation in `WASTE_REINVEST_REPORT.xlsx` carries five-factor evidence behind it: enough rows, enough price variation, in-sample fit, plausible elasticity, tight CI. The brand team can audit it row-by-row.

---

## Two independent confidences — why?

Yes, there are now **two** confidence labels per cell. They measure different things and a cell needs both to be acted on:

| Confidence | Computed in | What it answers |
|---|---|---|
| **`confidence` (curve-based)** | Stage 5 | "Is the saturation curve well-shaped and supported by enough swept points?" |
| **`confidence_tier` (model-based, NEW)** | Stage 4 | "Do we have enough evidence to trust THIS cell's elasticity estimate?" |

They check different failure modes:

- The curve confidence catches: extrapolation, unstable curve shape, narrow observed range.
- The model confidence catches: data thinness, low price variation, wide elasticity CI, implausible coefficient.

In practice they agree most of the time. When they disagree, the **stricter** wins via the Stage 7 gate.

---

## Sources of truth

| Question | File |
|---|---|
| "What discount should this cell be at?" | `recommendations.csv` |
| "How confident are we in this cell's elasticity?" | `elasticity_estimates.csv` columns `confidence_score`, `confidence_tier`, `conf_density`, `conf_variation`, `conf_fit`, `conf_plausibility`, `conf_tightness` |
| "How does the whole brand look right now?" | `v4_outputs/_readiness/DATA_READINESS_REPORT.md` |
| "Why was this row tiered Strong Cut?" | Cross-reference: `confidence_tier == HIGH/MEDIUM` in `elasticity_estimates.csv` AND `confidence == High/Medium` in Stage 5 output AND `tier == Strong Cut` in `recommendations.csv` |
| "Why was this row tiered Do Not Act?" | Either `confidence_tier == DO_NOT_ACT` (model gate), or curve `confidence == Needs Experiment` |

---

## Knob index — what to tune and where

All in `v4_config.py`.

### Brand-specific (must set when onboarding)
- `SALES_DATA_DIR` — path to the Excel exports
- `BRAND_NAME`, `OWN_BRAND_PATTERNS` — case-insensitive matching for "this brand vs competitors"
- `PLATFORM_NAME` — used in report titles

### Data scope
- `TRAIN_LOOKBACK_DAYS` (180) — train window. Critical for accuracy; see doc/MODEL.md.
- `OSA_OOS_THRESHOLD` (50) — daily availability cutoff below which a day is OOS
- `OUTLIER_Z_THRESHOLD` (2.0) — per-cell z-score for outlier exclusion

### Modelling
- `ELASTICITY_FLOOR / CEIL` (−4.0 / −0.3) — plausible elasticity band
- `N_PRIOR_PRICE / BADGE` (60 / 60) — shrinkage strength toward category prior

### Pricing decisions
- `TARGET_TIMELINE_WEEKS` (12) — how many cycles to close any gap
- `MIN_DISCOUNT_CHANGE_PPT` (3) — minimum meaningful weekly move
- `USE_HISTORICAL_FLOOR_TARGET` (True) — target = cell's recent floor, not elbow
- `MIN_MARGIN_PCT` (0.05), `MAX_COMPETITOR_PREMIUM_PCT` (0.10) — guardrails

### Tiering
- `TIER_STRONG_CUT_MIN_SAVINGS` (₹ 10 000/mo)
- `TIER_STRONG_CUT_MAX_VOL_DROP` (0.05 = 5 %)
- `TIER_TRADEOFF_MAX_VOL_DROP` (0.10 = 10 %)

### Confidence thresholds (May 2026)
- *Currently embedded in `_add_cell_confidence` in `stage4_model/elasticity.py`*. Set there: DEPTH full credit at 120 rows, VARIATION full credit at 15 price levels, FIT full credit at 0.50 in-sample R², TIGHTNESS full credit at t-stat 4. Tier boundaries: 70 / 50 / 30.

### Portfolio
- `TARGET_WEIGHTED_DISCOUNT_PCT` (9.0) — flywheel target weighted discount
- `REINVEST_MIN_VOL_LIFT_PCT`, `REINVEST_MAX_MARGIN_SAC_PCT`, `REINVEST_MIN_ELASTICITY` — reinvest qualification

---

## When to re-run what

| Trigger | What to re-run | Why |
|---|---|---|
| New brand onboarding | `data_readiness_report.py` once | Establish the GREEN/YELLOW/RED verdict and per-segment actionable % |
| Weekly Monday morning | `pipeline.py` (full) | Refresh recommendations; brand team approves Strong Cut tier |
| New month — fresh data dropped in | `data_readiness_report.py` + `pipeline.py` | Track whether actionable % grew (it should, as more days / more price variation arrives) |
| Price-test programme concludes | `data_readiness_report.py` | Verify the previously LOW/DO_NOT_ACT cells now have HIGH/MEDIUM confidence |
| You changed costs (COGS / commission / fulfilment) | `pipeline.py --stages 6 7 8` | Re-do economics + tiering + report; model coefficients are unchanged |
| You added a new festival/event | `pipeline.py` (full) | Stage 2 needs to flag the new event days; everything downstream cascades |
| You changed `TARGET_WEIGHTED_DISCOUNT_PCT` | `pipeline.py --stages 8` | Only the flywheel rebalance is affected |

---

## What this architecture is NOT (yet)

Limitations to be transparent about with brands:

- **Single platform per run.** The current ingestion expects all input Excel to be from one platform (Blinkit). For multi-platform you'd run the pipeline once per platform and merge outputs.
- **Brand-internal modelling.** Competitor data is used only for the `rpi` feature, not for cross-brand elasticities. Cross-brand effects are inferred indirectly through that one signal.
- **Daily granularity.** Weekly or fortnightly data won't work without rewriting Stages 3–4 (the lag features assume daily cadence).
- **English/Indian-rupee formatting** in the report templates. Localising for other markets requires changes in `dashboard/` and `stage8_output/`.

---

## What to read next

- For the experimental evidence behind every design choice: [doc/MODEL_EXPERIMENTS.md](MODEL_EXPERIMENTS.md).
- For the Stage 4 model design specifically: [doc/MODEL.md](MODEL.md).
- For column-by-column output reference: [doc/OUTPUTS.md](OUTPUTS.md).
- For the new-brand onboarding playbook: [doc/SCALING_PLAYBOOK.md](SCALING_PLAYBOOK.md).
- For Stage 8 flywheel math: [doc/FLYWHEEL.md](FLYWHEEL.md).
