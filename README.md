# Optimal Price Finder — Pricing Optimisation as a Product

End-to-end pricing system for CPG brands selling on quick-commerce platforms. Currently configured for **24 Mantra Organic on Blinkit**; designed to generalise to any brand × platform with similar daily sales data.

The system turns a brand's raw sales export into two things:

1. A **Data Readiness Report** — a one-page, brand-specific verdict on how much accuracy that brand's own data actually supports, with cell-level "act vs price-test" gating. *This is the first thing you give a new client.*
2. A weekly **pricing action plan** — per SKU × city, gated by confidence: Strong Cut, Trade-off, Hold, Increase, or Do Not Act, balanced into a portfolio target weighted discount.

---

## What changed in the May 2026 redesign

This version is the output of a deep robustness study (see [doc/MODEL_EXPERIMENTS.md](doc/MODEL_EXPERIMENTS.md)) that moved the system from "looks good in aggregate" to "trustworthy per product, per city, ready to scale to other brands":

- **Lag, momentum, and day-of-week features** — single biggest accuracy lever; cut within-cell error roughly in half. Tried five richer model classes (LightGBM, hybrid OLS+GBM, per-cell GBM, ridge, hierarchical) — all were worse with this data depth. The simple OLS with the right features wins.
- **A multi-factor per-cell confidence score (0–100)** combining data density, price variation, in-sample fit, elasticity plausibility, and CI tightness. Drives a hard gate that locks out auto price moves on data-thin cells.
- **A Data Readiness Report** — runs upfront on any brand's data, produces a GREEN / YELLOW / RED verdict with per-product, per-city actionable-% breakdown. The "sellable" discovery deliverable.

### Current model performance

| Metric | Before | **After (May 2026)** |
|---|---|---|
| Aggregated R²(units) at 3pp discount bin | 0.928 | **0.970** |
| MAPE at 3pp discount bin (pricing-decision metric) | 24.0 % | **17.4 %** |
| Pooled test log-R² | 0.844 | **0.875** |
| Raw-unit MAPE | 40.1 % | **35.6 %** |
| Cells with HIGH/MEDIUM confidence (actionable) | n/a | **88 % (29 / 33)** |
| **Data Readiness verdict** | n/a | **GREEN** |

---

## Quick start

```bash
# 1. Drop fresh Excel exports into input_data/
# 2. Assess what the data can deliver (do this first for any new brand)
python -X utf8 scripts/diagnostics/data_readiness_report.py

# 3. If verdict is GREEN or YELLOW, run the production pipeline
python -X utf8 pipeline.py

# All outputs land in v4_outputs/<timestamp>/ plus v4_outputs/_readiness/
```

Run a subset of stages:

```bash
python -X utf8 pipeline.py --stages 4 5 6 7 8   # re-train model + downstream
python -X utf8 pipeline.py --stages 8           # regenerate report only
```

---

## The architecture in two layers

```
                       ┌─────────────────────────────┐
                       │   input_data/  (raw Excel)  │
                       └──────────────┬──────────────┘
                                      │
                       ┌──────────────▼─────────────────────┐
                       │   LAYER 1 — DATA READINESS         │
                       │  (run once per brand or monthly)   │
                       │                                    │
                       │   Stages 1-4 → confidence score    │
                       │             → GREEN/YELLOW/RED     │
                       │                                    │
                       │   Output: DATA_READINESS_REPORT.md │
                       └──────────────┬─────────────────────┘
                                      │
                            verdict = GREEN or YELLOW?
                                      │ yes
                                      ▼
                       ┌────────────────────────────────────┐
                       │   LAYER 2 — WEEKLY PRODUCTION      │
                       │                                    │
                       │   Stage 1: INGEST                  │
                       │   Stage 2: PREPARE (outliers, OOS) │
                       │   Stage 3: FEATURE ENGINEERING     │
                       │           (incl. lag / DOW / mom.) │
                       │   Stage 4: ELASTICITY MODEL        │
                       │           (+ confidence score)     │
                       │   Stage 5: SATURATION CURVES       │
                       │   Stage 6: ECONOMICS + ELBOW       │
                       │   Stage 7: GUARDRAILS + TIERING    │
                       │           (confidence hard-gate)   │
                       │   Stage 8: WASTE + REINVEST        │
                       │                                    │
                       │   Output: BRAND_DASHBOARD.html     │
                       │           WASTE_REINVEST_REPORT    │
                       │           recommendations.csv      │
                       └────────────────────────────────────┘
```

**Layer 1** is the new product layer. It tells the brand team what their data can deliver before any action is taken.
**Layer 2** is the pricing engine. It runs only on cells that Layer 1 cleared.

Detailed flow with every formula and every config knob: [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md).

---

## Documentation map

| Doc | What it covers | Read when |
|---|---|---|
| [README.md](README.md) (this) | What the system is, top-level architecture, quick start | Always start here |
| **[doc/COMPLETE_FLOW.md](doc/COMPLETE_FLOW.md)** | **The whole system in one story: every stage with logic, math, diagrams, worked example (Delhi-NCR Jaggery end-to-end), FAQ** | **You want one document that explains everything** |
| [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) | Complete data flow with both layers, every stage, every gate | You want to understand the system end-to-end |
| [doc/MODEL_LOGIC.md](doc/MODEL_LOGIC.md) | The model in business language: features → elasticity → confidence → action | You're explaining the system to a brand stakeholder |
| [doc/MODEL_EXPERIMENTS.md](doc/MODEL_EXPERIMENTS.md) | The May 2026 deep dive: 7 experiments, what won, why complexity didn't help | You want to defend the model design or extend it |
| [doc/MODEL.md](doc/MODEL.md) | The Stage 4 model design: why per-category, why cell FE, what each term does | You're modifying `stage4_model/elasticity.py` |
| [doc/OUTPUTS.md](doc/OUTPUTS.md) | Every output file column-by-column, including the new confidence columns | You're reading a CSV or report and want to know what a field means |
| [doc/SCALING_PLAYBOOK.md](doc/SCALING_PLAYBOOK.md) | How to onboard a new brand from week 0 to ongoing | You're starting a new engagement |
| [doc/FLYWHEEL.md](doc/FLYWHEEL.md) | Stage 8 deep dive: cuts ↔ reinvestments, weighted-discount target math | You're tuning the portfolio target |
| [doc/README.md](doc/README.md) | The pre-2026 long-form technical reference | You need every detail of a single stage |

---

## What's in the box

| Folder / File | Purpose |
|---|---|
| `v4_config.py` | **Single source of all knobs** — paths, thresholds, target weighted discount, confidence thresholds |
| `pipeline.py` | Master orchestrator; `--stages N M ...` to run a subset |
| `stage1_ingestion/` | Excel reader + own-brand filter + event calendar |
| `stage2_preparation/` | Cleaning, OOS/event flagging, per-cell outlier detection |
| `stage3_features/` | 33 engineered features (incl. May 2026 lag/DOW/momentum) |
| `stage4_model/` | Per-category Huber model + cell FE + **per-cell confidence score** |
| `stage5_curves/` | Saturation curves; carries confidence forward |
| `stage6_economics/` | Variable-cost ladder + elbow detection |
| `stage7_guardrails/` | Floor price, throttle, **confidence-gated tier assignment** |
| `stage8_output/` | Flywheel report (cuts ↔ strategic reinvestments) |
| `dashboard/` | 4-view interactive HTML output |
| `scripts/diagnostics/` | `data_readiness_report.py`, `baseline_breakdown.py` |
| `scripts/experiments/` | `experiments_robustness*.py` — the 7-experiment harness |
| `doc/` | All documentation |

---

## Output files at a glance

After a full run you get **two** sets of outputs:

**`v4_outputs/_readiness/`** (from `data_readiness_report.py`)

| File | Purpose |
|---|---|
| `DATA_READINESS_REPORT.md` | The one-page brand-team-facing verdict |
| `per_cell_assessment.csv` | Audit row per cell with all confidence sub-scores |
| `per_product_assessment.csv` | Per-product roll-up (actionable %, median R², etc.) |
| `per_city_assessment.csv` | Per-city roll-up (actionable %, median R², etc.) |

**`v4_outputs/<timestamp>/`** (from `pipeline.py`)

| File | Purpose |
|---|---|
| `BRAND_DASHBOARD.html` | Interactive 4-view dashboard — open first |
| `WASTE_REINVEST_REPORT.xlsx` | Formula-driven Excel workbook for the brand team |
| `WASTE_REINVEST_REPORT.md` | Same content as plain Markdown |
| `recommendations.csv` | Per-cell action this week (price-led) |
| `elasticity_estimates.csv` | Per-cell elasticity + **confidence_score + confidence_tier** + all sub-scores |
| `waste.csv` / `reinvest.csv` | Stage 8 cuts and reinvestments lists |
| `fact_table.csv` / `features.csv` | Intermediate data for audit |
| `outliers_removed.csv` | Audit trail of statistically dropped days |
| `per_cell_detail.json` | Full per-cell payload for the dashboard |

Every column explained in [doc/OUTPUTS.md](doc/OUTPUTS.md).

---

## Operating cadence

| Frequency | What you run | Why |
|---|---|---|
| **Once at onboarding** | `data_readiness_report.py` | Establish what the data can deliver; produce the GREEN/YELLOW/RED verdict and per-segment actionable % |
| **Weekly** | `pipeline.py` (full) | Refresh recommendations on this week's data; brand team approves Strong Cut tier |
| **Monthly** | `data_readiness_report.py` + `pipeline.py` | Track how actionable % grows as more data arrives; re-train elasticities |
| **Quarterly** | Manual review of `v4_config.py` | Re-tune `TARGET_WEIGHTED_DISCOUNT_PCT`, tier thresholds, confidence weights with the brand team |

---

## Setup

```bash
pip install pandas numpy scipy statsmodels openpyxl lightgbm scikit-learn
```

Edit `v4_config.py`:

```python
SALES_DATA_DIR = r"/path/to/your/input_data"      # where the .xlsx files live
TARGET_WEIGHTED_DISCOUNT_PCT = 9.0                # portfolio target
BRAND_NAME = "Your Brand Name"                    # for the readiness report
OWN_BRAND_PATTERNS = ["Your Brand Name"]          # case-insensitive match
```

Drop Excel files into `input_data/`, then run:

```bash
python -X utf8 scripts/diagnostics/data_readiness_report.py
python -X utf8 pipeline.py
```

> On Windows, `python -X utf8` avoids encoding issues with ₹ and other Unicode characters in console output. Alternatively set `PYTHONUTF8=1` in your shell.

---

## License

Internal use only.
