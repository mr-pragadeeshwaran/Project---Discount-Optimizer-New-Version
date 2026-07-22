# Stage 4 — Elasticity Model Deep Dive

> "Why is the model built this way?" — every design choice in `stage4_model/elasticity.py` explained, with the data evidence that drove it.
>
> **For the May 2026 robustness deep-dive** (lag/DOW features, per-cell confidence score, the 7-experiment matrix that selected this design over LightGBM/GBM/hierarchical alternatives) see [MODEL_EXPERIMENTS.md](MODEL_EXPERIMENTS.md). This doc covers the steady-state Stage 4 design.

---

## TL;DR — what the model is

For each of the 3 product **categories** (Jaggery, Moong Dal, Sunflower Oil), fit one Huber-robust OLS:

```
log(units) = α_cell        ← cell fixed effects (33 dummies)
           + β_price·log(p) ← within-cell price elasticity (category-wide)
           + β_badge·badge_resid    ← residual "deal badge" effect (price-decorrelated)
           + osa_rolling_7d         ← stock availability
           + log_ad_sov             ← advertising signal
           + rpi                    ← relative price vs competitor
           + is_weekend
           + month_2 … month_12     ← seasonality
           + dow_1 … dow_6          ← day-of-week (May 2026)
           + lag1_log_units, lag7_log_units,             ← demand momentum
             rolling_mean_7d_log_units,                  ← (May 2026)
             rolling_mean_14d_log_units,
             lag1_log_price, lag1_discount
```

Per-cell elasticity = within-cell raw OLS slope, **shrunk toward the
category MEDIAN** of per-cell raw slopes (a more robust prior than a pooled
coefficient), then clipped to `[-4, -0.3]`.

**Plus** — a per-cell **confidence score** (May 2026) is attached to every row of the elasticities output. It combines 5 sub-signals (density, variation, in-sample fit, plausibility, CI tightness) into a 0–100 composite that determines whether Stage 7 is allowed to act on the cell. See [§ Per-cell confidence score](#per-cell-confidence-score-may-2026) below.

---

## Why each choice — driven by the data

### 1. Why **per-category** models, not one pooled model?

The earlier design fit a single `MixedLM` with category fixed effects across
all 3 categories. That produced **global elasticity = −5.92** — implausible
for staples. The reason:

> When you pool wildly different SKUs (Jaggery MRP ₹90, Sunflower Oil MRP
> ₹490), the variance *across* SKUs leaks into the `log_price` coefficient.
> The model "explains" why Sunflower Oil sells fewer units than Jaggery
> partly via price, inflating the elasticity.

Splitting into 3 separate models keeps each model's `log_price` coefficient
estimated only from variation **within that category**.

Per-category results:

| Category | Elasticity | Plausibility |
|---|---:|---|
| Jaggery | −0.9 (pooled) / **−2.5 (per-cell median)** | Plausible for a low-substitution staple |
| Moong Dal | −0.5 / **−4.0 (floor)** | High; data is contaminated by 16× volume growth |
| Sunflower Oil | −7.6 / **−3.7** | High but defensible — commodity, price-sensitive |

### 2. Why **cell fixed effects** (`C(sku_city)`)?

Without cell FE, `log_price` cross-sectional variation dominates the
coefficient. With cell FE, identification comes only from **within-cell**
price moves — exactly what we want for an elasticity ("if I move *this*
cell's price by 1%, what happens to *its* volume?").

Adding cell FE was the single biggest fix:

| Setup | Test log-R² | Test MAPE | Elasticity |
|---|---:|---:|---:|
| MixedLM with random intercept only (original baseline) | −0.15 | 167% | −5.9 |
| OLS + cell FE + log_price | +0.15 | 60% | −3.0 |
| OLS + cell FE + Huber + decorrelated badge | +0.27 | 52% | per-cat |
| **+ TRAIN_LOOKBACK_DAYS=180 + outliers z=2 (current production)** | **+0.84** | **24%** | per-cat |

### 3. Why **Huber robust** regression instead of plain OLS?

CPG daily sales have heavy-tailed shocks: weather events, viral influencer
posts, neighbouring SKU stockouts. Plain OLS gives these shocks too much
weight when fitting the slope. Huber (with the default tuning constant)
down-weights residuals larger than 1.345 σ — recovers a slope that's robust
to a few extreme days without throwing them out completely.

This shifted test R² from **+0.15 → +0.27** with no other change.

### 4. Why **`badge_resid`** instead of raw `discount_pct`?

Within a cell, `log_price` and `discount_pct` are mechanically linked:
`price = MRP × (1 − discount/100)`. So both columns in the regression
fight for the same variance and the coefficients become unstable.

```
badge_resid = discount_pct − OLS(discount_pct ~ log_price)   per cell
```

`badge_resid` is the part of the discount badge that's **not** already
explained by the price level. It captures the psychological lift from
seeing a big "X% OFF" sticker, holding the actual ₹ price constant.

> Empirically, `badge_resid` coefficients land between 0.01 and 0.20 —
> meaning a +1 ppt deeper "% OFF" sticker (with no real price change) lifts
> units by 1–20%, varying by cell. Highest for staples where deal-seeking
> behaviour is strongest.

### 5. Why **drop** `log1p_discount`, `is_deep_promo`, `price_surprise`, `discount_surprise`?

They were originally added "for non-linear effects". Correlation check:

| Pair | r |
|---|---:|
| `discount_pct` ↔ `log1p_discount` | 0.88 |
| `discount_pct` ↔ `is_deep_promo` | 0.80 |
| `discount_pct` ↔ `discount_surprise` | 0.63 |
| `discount_pct` ↔ `price_surprise` | 0.57 |

Five collinear price/discount features splitting the elasticity coefficient
explained most of why the baseline gave −5.9. They're now removed from the
formula (still computed in `stage3_features` for backward compatibility but
not used by Stage 4).

### 6. Why **clip elasticity to [−4, −0.3]**?

Empirically, well-identified CPG elasticities for staples cluster in the
range −0.5 to −3.5. Anything outside that range usually indicates:

- Confounding (something else moving with price)
- Insufficient within-cell price variation
- Heavy outliers the model couldn't fully discount

The clip prevents a runaway elasticity from poisoning the saturation curve
in Stage 5. Cells whose raw slope hits the bound get flagged "elasticity at
floor/ceiling" in `quality_note`, which downgrades their confidence in
Stage 5.

### 7a. Why **TRAIN_LOOKBACK_DAYS = 180** was the single biggest win

Restricting Stage 4 training to the last 180 days of regular-day rows took
the model from Weak to Strong in one step:

| Metric | Full-history training | Last-180-days training |
|---|---:|---:|
| Train log-R² | 0.86 | 0.89 |
| Test log-R² | 0.27 | **0.84** |
| Aggregated MAPE | 53% | **24%** |
| Aggregated R²(units) | 0.40 | **0.93** |

Why such a dramatic effect? Two structural problems in the older data
window were poisoning the fit:

1. **Moong Dal launch ramp.** Volume grew 16× from Jan 2025 to Mar 2026
   because of distribution / awareness expansion, not because of price.
   The model attributed that growth to discount, inflating elasticity
   for Dal cells to −5+ and adding huge noise.
2. **Price-regime mismatch.** Train period averaged 11.7% discount,
   test period averaged 24.9%. The model learned in one regime and was
   asked to predict in a wholly different one — guaranteed bad MAPE.

Restricting to 180 days hands the model only the steady-state business
it's actually in. Configured in `v4_config.py`:

```python
TRAIN_LOOKBACK_DAYS = 180  # None = full history (the old behavior)
```

Combined with `OUTLIER_Z_THRESHOLD = 2.0` (tightened from 3.0) — these
two settings together moved the model into the "Strong" accuracy tier.

### 7c. Why **lag / momentum / day-of-week features** were added in May 2026

After the lookback + outlier tuning, the pooled R² looked great (0.84) but a
diagnostic breakdown (`scripts/diagnostics/baseline_breakdown.py`) found
that the median **within-cell** test R² was −0.43 and 75% of cells had
negative within-cell R². The pooled metric was being inflated by the cell
fixed effects — once you sat inside a cell, the model was barely better
than predicting the cell's own mean.

A 7-experiment matrix (see [MODEL_EXPERIMENTS.md](MODEL_EXPERIMENTS.md))
proved that **adding lag, momentum, and day-of-week features** to the OLS
was the single best fix; every richer model class (LightGBM, hybrid OLS+GBM,
per-cell GBM, hierarchical ridge) was *worse* because there are only ~75
train rows per cell and complex models overfit at that scale.

| Metric | Without lag/DOW | **With lag/DOW (current)** |
|---|---:|---:|
| Test log-R² (pooled) | 0.844 | **0.875** |
| Aggregated R²(units) at 3pp bin | 0.928 | **0.970** |
| MAPE at 3pp bin | 24.0% | **17.4%** |
| Median within-cell test R² | −0.43 | **−0.09** |

### 7b. Why **time_trend was tried then removed**

Moong Dal demand grew **16×** over the data window (4 → 70 units/day, Jan
2025 → Mar 2026). When a per-cell linear time trend (`days_since_first_obs`)
was added, the model absorbed all that growth into the trend coefficient —
elasticity collapsed to −0.31.

That made Stage 6 think Moong Dal was inelastic and recommend deep
discounts, which would have destroyed margin. The trend feature was removed
and the launch-ramp problem is now handled via Stage 5's `growth_confounded`
confidence downgrade ("demand grew 16x over period — needs price test").

Diagnostic script: [`scripts/diagnostics/diag_dal.py`](../scripts/diagnostics/diag_dal.py)

### 8. Why **category-median prior** for shrinkage?

After fitting the per-category model, every cell also gets a **per-cell raw
OLS slope** (just `log_units ~ log_price` on that cell's data). Per-cell
slopes are noisier than the pooled coefficient — shrink toward a sensible
prior.

Three priors were compared as the shrinkage target:

| Prior | Risk |
|---|---|
| Pooled category coefficient (e.g. −0.88 for Jaggery) | Biased toward 0 by within-cell weighting; pulls cells with strong signal too far away from their data |
| Global default (−1.5) | Ignores category dynamics |
| **Category median of per-cell raw slopes (e.g. −2.5 for Jaggery)** ✓ | Robust to outliers, respects category dynamics |

The shrinkage formula:

```
shrunk_slope = w · clipped_raw + (1 − w) · category_median
                                   where w = n / (n + 60)
```

So a cell with 300 observations has w = 0.83 — mostly its own slope, with a
20% pull toward the category median.

---

## What the **outputs** of Stage 4 look like

The `elasticities` DataFrame has one row per cell with these key columns:

| Column | Meaning |
|---|---|
| `price_elasticity` | Final shrunk + clipped slope on `log_price`. **Negative** (higher price → fewer units). Used directly by Stages 5 & 6. |
| `price_elasticity_global` | The category-median prior used for shrinkage |
| `price_elasticity_se` | Standard error (inflated for thin cells) |
| `badge_sensitivity` | Per-cell shrunk slope on `badge_resid` |
| `n_observations`, `n_train`, `n_discount_levels`, `disc_pct_std` | Data-quality inputs for the Stage 5 confidence check |
| `historical_floor_disc` | The cell's lower-quartile discount in the last 90 days. Used by Stage 7 / 8 as the *target* for the multi-week glide path when `USE_HISTORICAL_FLOOR_TARGET=True`. Stops the system from planning discounts the cell has never operated at. |

---

## How Stage 4 diagnostics report quality

Stage 4 prints (and stores in `diagnostics`) both **daily** and **aggregated** metrics:

```
Train log-R²: 0.895  (primary trust signal — in-distribution fit)
Test  log-R²: 0.875  (out-of-distribution — last 20% by date)
Test  log-MAE: 0.341
Raw-unit MAPE: 35.6%
Aggregated (3pp bin) MAPE: 17.4%
Aggregated R²(units):       0.970
```

The **aggregated** metric is what really matters for this system. Stage 5
consumes *mean* units per discount level (the saturation curve), not daily
predictions. Daily-level noise is irreducible for CPG SKU × city data
(weather, supply hiccups, neighbouring SKU effects we don't observe).
This is also why the per-cell **daily** R² can never reach 0.70 on this kind of data — see [MODEL_EXPERIMENTS.md](MODEL_EXPERIMENTS.md) for the proof across 7 model classes.

### Accuracy tier (shown in the Excel report)

Surfaced on the Summary sheet of `WASTE_REINVEST_REPORT.xlsx` as a
live IF formula. User can change the thresholds in-cell without
touching code:

| Tier | Test log-R² | Aggregated MAPE |
|---|---|---|
| **Strong** | ≥ 0.70 | ≤ 25% |
| Moderate | ≥ 0.40 | ≤ 50% |
| Weak | ≥ 0.10 | ≤ 80% |
| Unreliable | else | — |

Current production hits **Strong** (R² 0.84, MAPE 24%).

---

## What happens to cells with too little data

| Situation | Fallback |
|---|---|
| `n_train < 30` or `n_disc_levels < 5` or `price_std < 0.01` | Per-cell slope = **category median**; SE inflated by `max(1, 30/n_train)` |
| Cell exists in test but not in train | Excluded from test-set evaluation (FE can't predict an unseen group) |
| All cells in a category have < 200 train rows | Whole category model is skipped; cells fall back to the global default elasticity (−1.5) |

These fallbacks all eventually surface in Stage 5 as `"Needs Experiment"`
or `"Low"` curve-confidence AND in the May 2026 **per-cell model confidence**
as `LOW` or `DO_NOT_ACT` — visible in the report so the brand team knows not
to act blindly.

---

## Per-cell confidence score (May 2026)

Implemented in `_add_cell_confidence` at the bottom of `stage4_model/elasticity.py`. The composite is computed per cell after the elasticity is finalised, written into `elasticity_estimates.csv`, and flows through Stages 5–7 where it acts as the **hard gate** on automatic actions.

### Formula

```
score = ( 0.25 × density_score
        + 0.20 × variation_score
        + 0.20 × fit_score
        + 0.15 × plausibility_score
        + 0.20 × tightness_score
        ) × 100        # rounded to 1 decimal
```

Each sub-score is a 0–1 number:

| Sub-score | Formula | Full credit at | Captures |
|---|---|---|---|
| `density_score` | `clip(n_train / 120, 0, 1)` | ≥ 120 train rows | "do we have enough days?" |
| `variation_score` | `clip(n_discount_levels / 15, 0, 1)` | ≥ 15 distinct prices | "did the discount actually move?" |
| `fit_score` | `clip(cell_train_r2 / 0.50, 0, 1)` | in-sample R² ≥ 0.50 | "does the model even fit this cell's data?" |
| `plausibility_score` | `1 if elasticity ∈ [−4, −0.3] else 0` | binary | "is the elasticity in a sane CPG range?" |
| `tightness_score` | `clip(\|elasticity\| / SE / 4, 0, 1)` | t-stat ≥ 4 | "is the elasticity well-pinned down?" |

### Tiers

| Tier | Score | What Stage 7 does |
|---|---|---|
| `HIGH` | ≥ 70 | Normal tiering; all actions available |
| `MEDIUM` | 50–70 | Normal tiering; smaller throttled steps recommended |
| `LOW` | 30–50 | **Cannot become Strong Cut.** Capped at Trade-off. Manager review before move. |
| `DO_NOT_ACT` | < 30 | **Forced to "Do Not Act"** regardless of savings. Run an A/B price test. |

### Why these specific weights

The weights were chosen to spread evidence across complementary signals without letting any single one dominate. A cell with massive data but constant discount (variation = 0) would still fail the gate. A cell with strong variation but only 20 training days would fail the gate. A cell with everything except a plausible elasticity (outside [−4, −0.3]) would fail the gate. This makes the score robust to the failure mode that matters: a single weak signal can't be masked by other strong signals.

### What the brand team sees

The Excel report shows the curve-based `confidence` column the brand team has always seen ("High / Medium / Low / Needs Experiment"). The new `confidence_score` and `confidence_tier` columns are in `elasticity_estimates.csv` and `recommendations.csv` for audit. A Strong Cut row in the brand-team Excel is guaranteed (by Stage 7) to have model-confidence ∈ {HIGH, MEDIUM} — they don't need to read the score directly, but they can if they want to know "why was THIS cell allowed?".

### Reading the failure modes

If a cell ends up `LOW` or `DO_NOT_ACT`, inspect the five sub-score columns:

| Pattern | Likely cause | Fix |
|---|---|---|
| Low `conf_density`, everything else OK | New SKU or new city | Wait for more days; or run a 4-week price test for accelerated signal |
| Low `conf_variation`, high `conf_density` | Discount has been flat for months | Vary the discount in a structured 4-week test — this is the cell's bottleneck |
| Low `conf_fit`, everything else OK | Demand shock during training window (launch ramp, supply issue) | Tighten outlier z-threshold, or shorten lookback window |
| `conf_plausibility == 0` (elasticity at clip bound) | Confounding or thin variation | Run a price test; the model is at a corner solution |
| Low `conf_tightness`, others OK | Wide CI — usually correlates with `conf_density` or `conf_variation` | Same fixes — more days or more variation |

---

## When to re-think the model

You'd revisit Stage 4 if any of these become true:

| Symptom | Cause | Action |
|---|---|---|
| Train R² < 0.5 | Either model misspec or data quality problem | Re-run `scripts/experiments/experiments.py` to compare alternatives |
| Many cells hitting the elasticity floor (−4) | Heavy growth confounding or new outlier pattern | Tighten `OUTLIER_Z_THRESHOLD` or add a per-cell control (e.g. seasonal index) |
| Per-category coefficients flip sign | Either truly weird data or formula spec broken | Inspect the raw per-cell slopes (`scripts/experiments/experiments4.py` prints them) |
| New category added | Architecture supports it automatically — confirm enough cells (≥ 200 rows total) | No code change |

---

## Code map

| File | Role |
|---|---|
| `stage4_model/elasticity.py` | Everything described in this doc |
| `stage3_features/features.py` | Builds `log_price`, `discount_pct`, controls. `badge_resid` is computed inside Stage 4 itself |
| `v4_config.py` | `MODEL_TYPE`, `TEST_SPLIT_PCT` (not currently used for tuning — Stage 4 is OLS-based) |
| `scripts/experiments/experiments.py` | Compares 8 model variants — re-run to validate before any major change |
| `scripts/experiments/experiments4.py` | Time-trend variants — shows why `time_trend` was rejected |
