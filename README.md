# Discount Optimizer — Price-Led Pricing Pipeline

End-to-end pricing system for **24 Mantra Organic on Blinkit**. Takes raw weekly
sales exports, learns per-cell price elasticity, and produces a weekly
**price-action plan** the brand team can execute on Monday morning:

- which SKU × city is over-discounted (raise price → save margin)
- which SKU × city is under-priced (drop price → grow volume)
- a portfolio target so the two sides of the flywheel stay in budget

Recommendations are framed in **selling price (₹)** because that's what the
customer sees on Blinkit. The equivalent discount % is shown alongside for
platform entry.

---

## Quick start

```bash
# 1. Drop fresh Excel exports into input_data/
# 2. Run the pipeline
python -X utf8 pipeline.py

# Outputs land in v4_outputs/<timestamp>/
#   ├─ outliers_removed.csv         ← audit of dropped statistical outliers
#   ├─ fact_table.csv               ← cleaned, flagged daily data
#   ├─ features.csv                 ← model inputs (20 engineered features)
#   ├─ elasticity_estimates.csv     ← per-cell price elasticity + uncertainty
#   ├─ recommendations.csv          ← THIS WEEK's price actions, price-led
#   ├─ waste.csv                    ← cells where you're over-discounting
#   ├─ reinvest.csv                 ← cells worth dropping price to grow
#   ├─ WASTE_REINVEST_REPORT.md     ← flywheel summary (open this first)
#   ├─ BRAND_DASHBOARD.html         ← interactive 4-view dashboard
#   └─ per_cell_detail.json         ← full per-cell payload for the dashboard
```

To run only some stages (re-use existing artifacts):

```bash
python -X utf8 pipeline.py --stages 4 5 6 7 8   # re-train model + downstream
python -X utf8 pipeline.py --stages 6 7 8       # re-tier with new costs
python -X utf8 pipeline.py --stages 8           # regenerate report only
```

---

## The 8 stages at a glance

```
┌──────────────────────────────────────────────────────────────────────┐
│                     INPUT: input_data/*.xlsx                         │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ 1. INGEST  — read Excel, dedupe, filter own brand            │
   └──────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ 2. PREPARE — flag OOS / event days,                          │
   │              detect per-cell outliers (audit trail),         │
   │              compute stable MRP + selling_price              │
   └──────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ 3. FEATURES — log(price), discount_pct, rolling features,    │
   │               month / weekend dummies                        │
   └──────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ 4. ELASTICITY — fit ONE Huber-robust model per category      │
   │                 (Jaggery / Moong Dal / Sunflower Oil)        │
   │                 with cell fixed effects + decorrelated badge │
   │   Per-cell elasticity = within-cell raw slope, shrunk toward │
   │   the category MEDIAN of raw slopes (robust prior)           │
   └──────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ 5. CURVES — for each cell, sweep selling price floor→MRP     │
   │             and predict units at each step. Assign           │
   │             confidence: High / Medium / Low / Needs Expt.    │
   └──────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ 6. ECONOMICS — variable cost ladder, contribution margin,    │
   │                marginal ROI → "elbow" (margin-optimal price) │
   └──────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ 7. GUARDRAILS + TIERING — floor price, 3-ppt-per-week cap,   │
   │                            assign tier (Strong Cut /         │
   │                            Trade-off / Hold / Do Not Act)    │
   │                            on THIS-WEEK metrics              │
   └──────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ 8. FLYWHEEL REPORT — cuts ↔ strategic reinvestments,         │
   │                       portfolio weighted-discount tracking   │
   │                       toward TARGET_WEIGHTED_DISCOUNT_PCT    │
   └──────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
                        BRAND_DASHBOARD.html
                        WASTE_REINVEST_REPORT.md
                        recommendations.csv
```

---

## The flywheel (what Stage 8 produces)

```
                       PORTFOLIO BUDGET
              (TARGET_WEIGHTED_DISCOUNT_PCT = 9%)
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
    ┌─────────────────────┐         ┌─────────────────────┐
    │  Q1: WHERE TO CUT   │         │ Q2: WHERE TO INVEST │
    │   (raise prices)    │         │  (drop prices for   │
    │                     │         │     volume growth)  │
    │ Low-elasticity      │ funds   │ High-elasticity     │
    │ cells where         │ ──────► │ cells with room to  │
    │ small price         │         │ grow & positive     │
    │ rises barely        │         │ net contribution    │
    │ dent volume         │         │ at +3 ppt           │
    └─────────────────────┘         └─────────────────────┘
              │                               │
              └───────────────┬───────────────┘
                              ▼
                    NET WEIGHTED DISCOUNT
                  moves toward 9% target over
                       ~5–8 weekly cycles
```

Customers see **₹ on the label**, not "X% OFF". Every recommendation in the
report is led by selling price; the equivalent discount % is shown for
platform entry.

---

## What's in the box

| Folder / File | Purpose |
|---|---|
| `v4_config.py` | **Single source of all knobs** — paths, thresholds, target weighted discount, etc. |
| `pipeline.py` | Master orchestrator; `--stages N M ...` to run a subset |
| `stage1_ingestion/` | Excel reader + own-brand filter + event calendar loader |
| `stage2_preparation/` | Cleaning, OOS/event flagging, **per-cell outlier detection** |
| `stage3_features/` | 20 engineered features for the elasticity model |
| `stage4_model/` | **Per-category Huber + cell-FE elasticity model** |
| `stage5_curves/` | Saturation curves; **confidence assignment with quality notes** |
| `stage6_economics/` | Variable-cost ladder + elbow detection |
| `stage7_guardrails/` | Floor price, change-rate throttle, **this-cycle tier assignment** |
| `stage8_output/` | **Flywheel report** (cuts ↔ strategic reinvestments) |
| `dashboard/` | 4-view interactive HTML output |
| `scripts/` | Diagnostic & experiment scripts (not part of production) |
| `doc/` | Detailed technical documentation |

---

## Documentation map

For details beyond this README:

| Doc | What it covers |
|---|---|
| [doc/README.md](doc/README.md) | Full technical reference: every stage, every formula, every config knob, worked examples |
| [doc/MODEL.md](doc/MODEL.md) | **Why** the model is built the way it is: multicollinearity diagnosis, per-category design, fixed effects, shrinkage |
| [doc/FLYWHEEL.md](doc/FLYWHEEL.md) | Stage 8 deep dive: cuts ↔ reinvestment math, target weighted discount, multi-cycle journey |
| [doc/OUTPUTS.md](doc/OUTPUTS.md) | Per-file output reference: what every column means, how to read the report |

---

## Model performance (current)

| Metric | What it means | Value |
|---|---|---|
| Train log-R² | Fit on training rows | **0.86** |
| Test log-R² | Out-of-sample fit | **0.27** |
| Aggregated MAPE (3-ppt discount bins) | Error on the grain the curve uses | **52%** |
| Aggregated R² (units, same grain) | Variance explained at the curve grain | **0.40** |
| Per-category elasticities | Jaggery / Dal / Oil | **−2.5 / −3.6 / −3.3** (plausible CPG range) |

These metrics replaced an earlier hierarchical model that produced train R² = 0.41, test R² = −0.15, and elasticities of −5.9 — see [doc/MODEL.md](doc/MODEL.md) for the diagnosis and fix.

---

## Operating cadence

| Frequency | Stages | What you do |
|---|---|---|
| **Weekly** | full pipeline | Run Mon AM, review report, approve Strong Cut tier in dashboard, queue Trade-off for review |
| **Monthly** | 1–8 with fresh data | Re-train elasticity on the latest data |
| **Quarterly** | — | Re-tune `TARGET_WEIGHTED_DISCOUNT_PCT` and tier thresholds in `v4_config.py` with the brand team |

---

## Setup

```bash
pip install pandas numpy scipy statsmodels openpyxl
```

Edit `v4_config.py`:
```python
SALES_DATA_DIR = r"/path/to/your/input_data"      # where the .xlsx files live
TARGET_WEIGHTED_DISCOUNT_PCT = 9.0                 # portfolio target
```

Drop Excel files into `input_data/`, then run `python -X utf8 pipeline.py`.

> On Windows, the `-X utf8` flag avoids encoding issues with ₹ and other
> Unicode characters in console output.

---

## License

Internal use only.
