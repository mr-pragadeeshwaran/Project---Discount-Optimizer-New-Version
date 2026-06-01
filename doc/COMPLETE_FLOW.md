# Discount Optimiser — Complete Flow

> The whole system in one document. What it does, every stage's logic,
> the math, a worked example for one cell from raw data to final
> recommendation, and the per-city accuracy story.
>
> Pair this with [MODEL.md](MODEL.md) (Stage 4 deep dive),
> [FLYWHEEL.md](FLYWHEEL.md) (Stage 8 deep dive), and
> [OUTPUTS.md](OUTPUTS.md) (file reference) for technical detail.

---

## 1. The business question

For 4 SKUs of 24 Mantra Organic on Blinkit, across 11 cities (33 active
SKU × city "cells"):

> **"For each cell, what selling price should I put on Blinkit each week
> for the next 3 months — to reduce wasted discount where customers won't
> notice and reinvest in cities where deeper discount actually drives
> volume?"**

The system delivers a weekly Excel report the brand team reads Monday
morning and acts on.

---

## 2. System at a glance

```
                              INPUT
              ┌────────────────────────────────┐
              │  input_data/*.xlsx             │
              │  (1 year of daily Blinkit      │
              │   sales per SKU × city)        │
              └───────────────┬────────────────┘
                              │
   ┌──────────────────────────▼──────────────────────────────────┐
   │  STAGE 1  Ingest .xlsx, filter own brand, normalise         │
   │  STAGE 2  Flag OOS / event days, detect outliers (z>2)      │
   │  STAGE 3  Engineer features (log_price, badge_resid, ...)   │
   │                                                              │
   │  STAGE 4  PER-CATEGORY HUBER MODEL (the brain)              │
   │           - Train on last 180 days only                     │
   │           - Cell fixed effects (each city × SKU has its own │
   │             intercept)                                       │
   │           - Output: per-cell price elasticity + Cell R²     │
   │             + historical floor + confidence tier            │
   │                                                              │
   │  STAGE 5  Per-cell saturation curves                        │
   │  STAGE 6  Variable cost ladder + margin-optimal elbow       │
   │                                                              │
   │  STAGE 7  GUARDRAILS + THIS-WEEK ACTION                     │
   │           - Target = historical floor (proven safe)         │
   │           - Per-cycle step = max(3 ppt, gap/12)             │
   │           - Tier each cell: Strong Cut / Trade-off / Hold   │
   │                                                              │
   │  STAGE 8  WEEKLY EXCEL REPORT                               │
   │           - Portfolio summary + 12-week glide path          │
   │           - Per-product city × week matrix                  │
   │           - All in selling-price (Rs.), formula-driven      │
   └──────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
                    OUTPUT  v4_outputs/<run>/
                    WASTE_REINVEST_REPORT.xlsx
                    (the brand-team Monday read)
```

---

## 3. The journey of ONE cell

To make this concrete, follow Jaggery Powder 500g × Delhi-NCR through
the entire system using real numbers from the current run.

### Starting state — what the data file contained
- 350+ daily rows of sales over the past year
- Average selling price recently: ₹70.6 (current discount 21.6%)
- Average daily units: ~30
- MRP on Blinkit: ₹90
- Discount has varied from 6% to 35% over the year

### Where it ends up — what the report says

> **Delhi-NCR — Jaggery 500g**
> - Confidence: **Medium**, Cell R²: **0.87**, 108 days of data
> - Current price: **₹70.6** (21.6% off)
> - Target price: **₹79.0** (12.2% off — the cell's historical floor)
> - Action: **CUT** discount over 4 weeks
> - Save: **₹41,801/month** once at target
> - Plan: Week 1 → ₹73.3 → Week 2 → ₹76.0 → Week 3 → ₹78.7 → Week 4+ → **₹79.0** (stable)

This whole journey is what the next sections explain.

---

## 4. Stage-by-stage with worked examples

### Stage 1 — Ingest

**File:** `stage1_ingestion/ingest.py`

Reads every `.xlsx` in `input_data/`, combines them, deduplicates by
(product, city, date), filters to own-brand rows (24 Mantra Organic
patterns), and loads the event calendar.

```
Inputs:  3 Excel files (73,416 raw rows)
Output:  13,646 own-brand daily rows × 37 cells
```

For our worked-example cell (Jaggery 500g × Delhi-NCR), Stage 1 picked
out ~350 rows from the full file.

### Stage 2 — Prepare

**File:** `stage2_preparation/prepare.py`

Three jobs:

1. **Compute `stable_mrp`** (the 90th percentile of MRP per SKU — the
   "label price" that doesn't wobble daily). For Jaggery 500g: **₹90**.
2. **Compute `selling_price`** = `stable_mrp × (1 − discount/100)`.
3. **Flag day types**:
   - `is_oos_day` — availability < 50%
   - `is_event_day` — within ±2 days of a festival or platform sale
   - **`is_outlier`** — per-cell `|z-score| > 2` on log(units). Audit-trailed
     to `outliers_removed.csv`. Removes 280 of 13,646 rows this run.
   - `is_regular_day` = NOT event AND NOT OOS AND NOT outlier — only
     these go to model training.

**Worked example — outlier detection for Delhi-NCR Jaggery:**

```
Cell mean log(units) = 3.40,  std = 0.55
  Day with units=120 → log = 4.79  → z = +2.53  ❌ flagged (HIGH spike)
  Day with units=2   → log = 0.69  → z = −4.93  ❌ flagged (LOW dip)
  Day with units=28  → log = 3.33  → z = −0.13  ✓ kept
```

The flagged days might be: an undeclared promo we missed (HIGH), a
half-day stockout the 50% OSA filter didn't catch (LOW). Both would
distort the elasticity if left in.

### Stage 3 — Features

**File:** `stage3_features/features.py`

Engineers ~20 columns used downstream:

| Feature | Formula | Why |
|---|---|---|
| `log_price` | `ln(selling_price)` | Coefficient = price elasticity |
| `discount_pct` | as given | Used to compute `badge_resid` in Stage 4 |
| `osa_rolling_7d` | 7-day avg of availability | Supply control |
| `log_ad_sov` | `ln(1 + 7-day rolling ad SoV)` | Ad intensity control |
| `rpi` | `selling_price / competitor_price` | Competitive position |
| `is_weekend`, `month_2..12` | dummies | Seasonality |

### Stage 4 — The model (the brain)

**File:** `stage4_model/elasticity.py`

This is where the elasticity per cell is learned. Three big design choices:

```
For each category (Jaggery, Moong Dal, Sunflower Oil):

   Fit one Huber-robust regression on the last 180 days of regular days:

      log(units) = α_cell                              ← cell intercepts
                 + β_price · log(selling_price)        ← price elasticity
                 + β_badge · badge_resid               ← deal-badge effect
                 + controls (osa, ad, rpi, weekend, months)
```

Three things make this work:

1. **Per-category models.** Pooling Jaggery + Dal + Oil into one model
   inflated elasticity to −5.9. Separate models give plausible values
   (−0.5 to −3.6).
2. **Cell fixed effects** (`α_cell`). Identifies the slope from
   within-cell price moves only, not from cross-cell differences in
   absolute price.
3. **Decorrelated badge.** `badge_resid = discount_pct − OLS(discount ~
   log_price)` per cell. Removes the multicollinearity that was
   scrambling the elasticity coefficient.

Plus the **180-day lookback** (set via `TRAIN_LOOKBACK_DAYS = 180`):
training only on recent steady-state data avoids the Moong Dal launch
ramp and old price regimes that were poisoning the fit. This is the
single biggest accuracy lever — moved the model from "Weak" to "Strong".

#### Per-cell elasticity output

After fitting the per-category coefficient, each cell gets its own
elasticity via per-cell OLS shrunk toward the category median:

```
final_elasticity_cell = w · per_cell_OLS + (1 − w) · category_median_OLS
                        where w = n_train / (n_train + 60)
```

Clipped to `[−4, −0.3]` (plausible CPG range).

#### Stage 4 also computes for each cell:

- **`price_elasticity`** — the per-cell slope (e.g. −0.73 for Jaggery)
- **`cell_train_r2`** — the model's fit on this cell's training data,
  measured at the 3-ppt discount-bin grain (see §6)
- **`historical_floor_disc`** — the cell's lower-quartile discount in
  the last 90 days. That's the **proven-safe target** for Stage 7.
- **`n_observations`** — sample size

#### Diagnostics (current run)

```
Train log-R²: 0.887  (in-distribution fit)
Test  log-R²: 0.844  (out-of-distribution, last 20% of dates)
Raw daily MAPE: 40.1%
Aggregated (3-ppt bin) MAPE: 24.0%
Aggregated R²(units):       0.928
Accuracy tier:              STRONG  (R² ≥ 0.70 AND MAPE ≤ 25%)
```

### Stage 5 — Saturation curves

For each cell, sweep discount from 0% to 30% in 1% steps and predict
units at each level using the dual-signal model:

```
units(d) = current_units × (price(d) / current_price) ^ elasticity
                         × exp(badge_sensitivity × (d − current_discount))
```

Each cell also gets a **confidence tier** assigned at this stage:

- **High** — ≥200 obs, ≥10 distinct discount levels, ≥3 ppt std, elasticity not at boundary
- **Medium** — ≥100 obs, ≥5 levels, ≥2 ppt std
- **Low** — anything else with enough data
- **Needs Experiment** — too thin, run a price test before acting

Downgrades:
- Cell elasticity hits the −4 floor or −0.3 ceiling → demote one tier
- Cell demand grew ≥ 2× over the period (launch ramp) → demote one tier

### Stage 6 — Economics

**File:** `stage6_economics/economics.py`

For each cell, build a margin ladder:

```
For each discount level 0..30%:
  selling_price   = MRP × (1 − d/100)
  variable_cost   = 50%·MRP + 15%·selling_price + Rs.10   (COGS + commission + fulfil)
  contribution    = (selling_price − variable_cost) × predicted_units
  marginal_ROI    = Δcontribution / Δdiscount_cost (vs previous discount)
```

The **elbow** is the first discount level where marginal ROI drops
below 1.0 — adding more discount stops paying its way. With current
cost structure the elbow lands at 0% discount (pure margin says "no
discount") for most cells.

### Stage 7 — Guardrails + this-week action

**File:** `stage7_guardrails/guardrails.py`

For each cell, decide TWO things: the target and the per-cycle step.

#### Target

```
if cell qualifies for strategic reinvest:
    target = current_disc + 3 ppt          (drop price to grow volume)
else:
    target = max(elbow_disc, historical_floor_disc)
    # Default behaviour with USE_HISTORICAL_FLOOR_TARGET=True
    # → never plan a price the cell hasn't operated at recently
```

For our Delhi-NCR example: `target = max(0%, 12.2%) = 12.2%` (= ₹79.0 price).

#### Per-cycle step (the glide rule)

```
gap = |current_disc − target_disc|

if gap < 0.1:                  step = 0     (already done)
elif gap ≤ MIN (3 ppt):        step = gap   (one-shot — don't overshoot)
else:                           step = max(MIN, gap / TARGET_TIMELINE_WEEKS)
                                # No upper cap. TIMELINE is the deadline.
```

For Delhi-NCR: gap = 9.4 ppt > 3, so `step = max(3, 9.4/12) = 3 ppt/week`.
Cycles to close: `ceil(9.4/3) = 4`.

Phasing plan: `21.6% → 18.6% → 15.6% → 12.6% → 12.2%` (4 weekly steps).

#### Tier assignment (this-cycle action gate)

```
if confidence == "Needs Experiment":             → Do Not Act
elif |gap| ≤ 2 ppt:                              → Hold
elif gap < −2 ppt (elbow above current):         → Increase (rare)
elif this_cycle_savings ≥ Rs.5K
     AND |this_cycle_vol_drop| ≤ 8%
     AND confidence ∈ {High, Medium}:            → Strong Cut
elif this_cycle_savings > 0
     AND |this_cycle_vol_drop| ≤ 20%:            → Trade-off
else:                                             → Hold
```

For Delhi-NCR: Cut, Medium confidence → **Trade-off** tier (Strong Cut
needs savings ≥ ₹5K AND vol-drop ≤ 8% — this cell hits ≥ ₹5K but the
3-ppt move predicts a slightly larger vol-drop).

### Stage 8 — The weekly Excel report

**File:** `stage8_output/waste_reinvest.py` + `stage8_output/excel_report.py`

Generates the McKinsey-style 7-sheet workbook that the brand team opens
Monday morning. See §7 for the sheet-by-sheet view.

---

## 5. The flywheel (the business framing)

```
                       PORTFOLIO TARGET
              (TARGET_WEIGHTED_DISCOUNT_PCT = 9%)
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
    ┌─────────────────────┐         ┌─────────────────────┐
    │     CUT side        │         │   REINVEST side     │
    │  (raise prices)     │         │  (drop prices)      │
    │                     │         │                     │
    │ Cells where current │  funds  │ Cells where +3 ppt  │
    │ discount > target,  │ ──────► │ projected to add    │
    │ walk to floor over  │         │ ≥5% vol with ≤10%   │
    │ 12 weeks @ ≥3 ppt   │         │ margin sacrifice    │
    │ per week.           │         │                     │
    └─────────────────────┘         └─────────────────────┘
              │                               │
              └───────────────┬───────────────┘
                              ▼
                  PORTFOLIO WEIGHTED DISCOUNT
                  glides from today's 23.31%
                  toward the per-cell floors
                  over the user-set duration
```

### Why historical floor as target (and not 0%)

The pure margin-optimal elbow lives at 0% discount for almost every
cell. But pushing every cell to 0% would:

- Send some prices to MRP — a level customers haven't seen
- Risk huge volume loss in elastic markets
- Be impossible to defend ("0% off? customers will leave")

The historical floor — the cell's lower quartile of past discounts in
the last 90 days — is by construction **proven safe**: the cell has
operated at or below it on ~25% of days with healthy sales.

For Delhi-NCR Jaggery: historical floor = 12.2% off (₹79.0). The cell
has been at-or-below that on 25%+ of recent days, so returning to it
is a safe move.

---

## 6. The accuracy story

Two layers of "is this trustworthy?":

### Portfolio level (Summary sheet)

```
Out-of-sample R²:               0.84   ← model explains 84% of test variance
Aggregated (3-ppt-bin) MAPE:    24%
Aggregated R²(units):           0.93   ← model explains 93% of unit variance at the curve grain
Train R² (in-distribution):     0.89
Accuracy tier:                  STRONG (R² ≥ 0.70 AND MAPE ≤ 25%)
```

The tier formula is editable in the Excel cell — change `0.70` to `0.60`
and it recomputes live.

### Per-city level (By Product sheet)

Each row in the city table carries 3 confidence signals:

| Signal | What it is | Read it as |
|---|---|---|
| **Conf** | Stage 5 verdict (High / Medium / Low / Needs Test) | Categorical — easy to scan |
| **Cell R²** | Model's fit on this cell's 3-ppt-bin grain | Numerical — proof of accuracy |
| **Obs** | Number of training days for this cell | Sample size |

### Why Cell R² is measured at the 3-ppt-bin grain (not daily)

CPG day-to-day data has irreducible noise (weather, weekday, neighbour
effects). Daily R² for a small cell can be 0.05-0.30 even with a
perfect price-effect model — because most daily variance isn't from
price.

The recommendation logic doesn't use daily predictions. It uses **mean
units per 3-ppt discount bin** (the saturation curve). So Cell R² is
computed at exactly that grain. Switching from daily to bin-grain took
city R² values from 0.08-0.65 to 0.30-0.91 — same model, same
predictions, more honest metric.

| City (Jaggery 500g) | Daily R² (old) | Aggregated R² (current) |
|---|---:|---:|
| Kolkata | 0.23 | **0.41** |
| Bangalore | 0.27 | **0.75** |
| Hyderabad | 0.35 | **0.81** |
| Mumbai | 0.28 | **0.88** |
| Others | 0.65 | **0.91** |

A few cells still show low R² (e.g. Lucknow Sunflower Oil = −0.5).
Those are now **real** confidence flags — the model genuinely can't
separate price effect from other factors there. Action: run a price
test in that city before acting.

---

## 7. How to read the Excel report

7 sheets, in reading order:

### Sheet 1 — Summary

```
Portfolio Summary
                    Today         After cuts     After cuts + invest
Gross sales / mo    Rs.78.87 L    Rs.78.65 L     Rs.79.71 L
Discount spend / mo Rs.18.38 L    Rs.17.93 L     Rs.18.33 L
Net revenue / mo    Rs.60.49 L    Rs.60.72 L     Rs.61.38 L
Units / mo          53,283        53,292         53,509
Weighted discount % 23.31%        22.80%         23.00%

Target weighted discount: 9.00%
Gap today:                +14.31 ppt
Gap after this-week plan: +14.00 ppt

This week's plan
                       Cells   Discount spend Δ   Units Δ
Cut (raise price)        8     −Rs. 44,881        −10
Reinvest (drop price)    1     +Rs. 40,005        +217

Model accuracy
Out-of-sample R²    0.84
Avg error (MAPE)    24.0%
Training fit R²     0.89
Overall tier        STRONG
```

Every number on this sheet is a **live formula** referencing the
hidden `Data` sheet. Edit `TARGET_TIMELINE_WEEKS` in config and re-run
the pipeline — the weighted-discount projection moves with it.

### Sheet 2 — Glide Path

Week-by-week projection at portfolio level:

```
Cycle  Label   Wt Disc%   Spend/mo      Cum Savings   Gap
0      Today   23.31      Rs.18.38 L    —             14.31
1      Week 1  23.22      Rs.18.33 L    Rs.5,288      14.22
2      Week 2  23.13      Rs.18.28 L    Rs.10,492     14.13
3      Week 3  23.05      Rs.18.22 L    Rs.15,611     14.05
4      Week 4  22.96      Rs.18.17 L    Rs.20,646     13.96
...
12     Week 12 22.40      Rs.17.91 L    Rs.46,911     13.40  ← plan complete
```

### Sheet 3 — By Product (the workhorse)

For each of the 4 SKUs:

1. **Product title** (bold)
2. **Mini-summary in cells**: MRP / today gross / today discount / disc% / cities (cut/invest/hold)
3. **Model accuracy bar in cells**: Strong / Test R² 0.84 / MAPE 24%
4. **City × week table** — the operational heart:

```
City        Conf    Cell R²  Obs  Cur Rs  Tgt Rs  Action   Save Rs/mo  W1     W2     ...  W12
Delhi-NCR   Medium  0.87     108  70.6    79.0    CUT      41,801      73.3   76.0   ...  79.0
Kolkata     Medium  0.41     110  71.0    79.0    CUT      24,198      73.7   76.4   ...  79.0
Pune        Medium  0.60     112  70.8    79.0    CUT       8,894      73.5   76.2   ...  79.0
Ahmedabad   Medium  0.30     108  70.6    79.0    CUT       5,431      73.3   76.0   ...  79.0
Others      Low     0.91     115  70.7    70.7    HOLD          —      70.7   70.7   ...  70.7
Bangalore   Low     0.75     109  71.8    71.8    HOLD          —      71.8   71.8   ...  71.8
...
```

Frozen panes keep City + Conf + Cell R² + Obs + Cur + Tgt + Action +
Save visible when scrolling right through W1..W12.

### Sheets 4, 5, 6 — Price Lifts / Price Drops / Needs Test

Standalone lists for bulk-loading into Blinkit:
- **Price Lifts** — all cuts, sorted by ₹ wasted/mo
- **Price Drops** — strategic reinvestments
- **Needs Test** — Low-confidence cells that should get a pilot first

### Sheet 7 — Data (hidden)

The single source of truth. 16 columns × 33 cells. Format ▸ Sheet ▸
Unhide to peek. All other sheets reference this via formulas.

---

## 8. Configuration cheat sheet (v4_config.py)

```python
# How the model trains
TRAIN_LOOKBACK_DAYS          = 180   # train on last N days only — single biggest
                                      # accuracy lever
OUTLIER_Z_THRESHOLD          = 2.0   # per-cell |z| > this gets dropped
HISTORICAL_FLOOR_PERCENTILE  = 25    # lower-quartile of past discounts = floor
HISTORICAL_FLOOR_LOOKBACK_DAYS = 90  # window for the floor

# How aggressively the system moves
USE_HISTORICAL_FLOOR_TARGET  = True  # target = floor (not elbow at 0%)
TARGET_TIMELINE_WEEKS        = 12    # 3 months — HARD deadline
MIN_DISCOUNT_CHANGE_PPT      = 3     # smallest weekly move
TARGET_WEIGHTED_DISCOUNT_PCT = 9.0   # portfolio target shown in Glide Path

# Strategic reinvest filters
REINVEST_MIN_ELASTICITY      = 2.0
REINVEST_MIN_VOL_LIFT_PCT    = 5.0
REINVEST_MAX_MARGIN_SAC_PCT  = 10.0
```

| You want | Change |
|---|---|
| Faster glide (bigger weekly cuts) | Lower `TARGET_TIMELINE_WEEKS` to 8 |
| Smaller weekly moves | Raise `MIN_DISCOUNT_CHANGE_PPT` to 5 |
| Deeper end-of-glide target | Lower `HISTORICAL_FLOOR_PERCENTILE` to 10 |
| Pure margin chase (ignore floor) | Set `USE_HISTORICAL_FLOOR_TARGET = False` |
| Different accuracy bar | Edit the IF formula in Summary!B34 |

---

## 9. Worked example — Delhi-NCR Jaggery end-to-end

```
RAW DATA (Stage 1)
   350+ daily rows for Jaggery 500g × Delhi-NCR over the past year

       │
       ▼  Stage 2: drop OOS / event / outlier days
       │  Result: 108 regular days kept

       ▼  Stage 3: engineer log_price, badge_resid, controls
       │
       ▼  Stage 4: train per-category Huber model on last 180 days
       │  - Jaggery category coefficient on log_price = −1.19
       │  - Per-cell shrunk elasticity = −0.81
       │  - Cell R² (3-ppt-bin grain) = 0.87
       │  - Historical floor (P25 of last 90 days) = 12.2%
       │  - Confidence tier = Medium (n=108, several discount levels tested)
       │
       ▼  Stage 5: build saturation curve 0%..30%
       │  Predicted daily units at each discount level
       │
       ▼  Stage 6: variable cost ladder
       │  - VC at price 70.6 = 50%·90 + 15%·70.6 + 10 = Rs.65.6
       │  - Margin/unit at 21.6% off = Rs.5.0
       │  - Margin/unit at 12.2% off = Rs.13.4   ← much higher
       │  - Elbow lands at 0% (margin-optimal)
       │
       ▼  Stage 7: targets and step
       │  - Target = max(elbow=0%, floor=12.2%) = 12.2%   ← FLOOR wins
       │  - Gap = 21.6 − 12.2 = 9.4 ppt
       │  - Step = max(3, 9.4/12) = 3 ppt/week
       │  - Cycles = ceil(9.4 / 3) = 4 weeks
       │  - Phasing: 21.6% → 18.6% → 15.6% → 12.6% → 12.2%
       │  - This-week savings: 3% · 90 · 30 · 30 = Rs.2,430 (one cell, one cycle)
       │  - Cumulative savings at target: Rs.41,801/mo
       │  - Tier: TRADE-OFF (Cell R² medium, gap moderate)
       │
       ▼  Stage 8: write to Excel report
          - Summary sheet shows portfolio impact
          - By Product sheet shows Delhi-NCR row:
              City: Delhi-NCR | Conf: Medium | Cell R²: 0.87 | Obs: 108
              Cur Rs: 70.6 | Tgt Rs: 79.0 | Action: CUT | Save Rs/mo: 41,801
              W1: 73.3 | W2: 76.0 | W3: 78.7 | W4..W12: 79.0
          - Glide Path sheet shows the cell's contribution to portfolio glide

ACTION (Monday morning)
   Brand team opens Excel → By Product → Jaggery 500g
   → Delhi-NCR row → set Blinkit price to Rs.73.3 this week
```

---

## 10. FAQ

**Q: Why does the Glide Path stop at Week 4 even though the budget is 12 weeks?**
Because most cell gaps are small (2-5 ppt). At a 3-ppt minimum step, those gaps close in 1-2 cycles. The 12-week budget is a ceiling, not a target.

**Q: Why isn't the portfolio reaching the 9% target?**
Per-cell floors are 12-22% (the historical safe levels). The weighted-average minimum the portfolio can hit without violating "never go below proven safe" is ~22%. To go further, either lower `HISTORICAL_FLOOR_PERCENTILE`, or run price tests on the Needs-Test cells to discover their true floor.

**Q: What if a cell's Cell R² is negative?**
The model is fitting that cell worse than just predicting its mean. It might be in a launch ramp, have data quality issues, or be subject to factors the model doesn't see. Action: don't auto-act, run a small price test in that city.

**Q: How does the system handle "Increase" cells (where deeper discount would help)?**
Two paths: (1) Stage 7 detects cells where elbow > current and tiers them as `Increase`. Rare under current cost structure. (2) Stage 8 separately identifies **strategic reinvest** candidates — high-elasticity cells with positive net economic impact at +3 ppt. These are the Q2 "Price Drops" list.

**Q: What if the brand team disagrees with the model?**
Open the Data sheet (hidden), edit any cell's discount manually, watch the Summary and By Product sheets recompute via formulas. Or change `TARGET_TIMELINE_WEEKS` / `MIN_DISCOUNT_CHANGE_PPT` and re-run the pipeline to get a different plan.

**Q: How do I add a new SKU or city?**
Just drop a new .xlsx into `input_data/` with the same column structure. Stage 1 picks it up automatically. The model retrains and the report includes it next run.

**Q: How often should the pipeline run?**
Weekly. Each run re-reads fresh data, re-fits the model on the last 180 days, and re-plans. The plan is **self-correcting** — if last week's recommendation produced an unexpected response, this week's elasticities adjust accordingly.

---

## Related docs

- [README.md](../README.md) — quick start, file structure
- [doc/README.md](README.md) — full technical reference, every stage with formulas
- [doc/MODEL.md](MODEL.md) — Stage 4 deep dive, all the design choices with experimental evidence
- [doc/FLYWHEEL.md](FLYWHEEL.md) — Stage 8 deep dive, glide rule and reinvest filters
- [doc/OUTPUTS.md](OUTPUTS.md) — column-by-column output file reference
