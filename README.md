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

The headline is the **price-engine decision model** — the price/badge curve that actually sets recommendations — measured held-out:

| Metric | Value | Notes |
|---|---|---|
| **Price-engine held-out R² (3ppt discount-bin grain)** | **~0.87** | The number that governs recommendations |
| **Price-engine held-out MAPE (3ppt bin)** | **~25.8 %** | Average error on the curve that sets prices |
| Full lag-laden regression R² | ~0.88 | **Context only — does not set prices** (uses momentum/lag) |
| Cells with HIGH/MEDIUM confidence (actionable) | 88 % (29 / 33) | Drives the hard gate |
| Overall accuracy tier | **Moderate** | Honestly computed, not rounded up |
| Data Readiness verdict | GREEN | Per-brand data sufficiency |

Stage-4 diagnostics expose these as `decision_test_r2` / `decision_test_mape` (and `_bin` variants at the discount-bin grain). The full-regression R² is reported for context, not for pricing.

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
| `v4_config.py` | **Single source of all knobs** — paths, thresholds, target weighted discount, confidence thresholds, `INELASTIC_ELASTICITY_THRESHOLD = 1.0` |
| `run.bat` | **One-click runner** — runs the pipeline and opens the report |
| `pipeline.py` | Master orchestrator; `--stages N M ...` to run a subset |
| `stage1_ingestion/` | Excel reader + own-brand filter + event calendar |
| `stage2_preparation/` | Cleaning, OOS/event flagging, per-cell outlier detection |
| `stage3_features/` | 33 engineered features (incl. May 2026 lag/DOW/momentum) |
| `stage4_model/` | Per-category Huber model + cell FE + **per-cell confidence score** |
| `stage5_curves/` | Saturation curves; carries confidence forward |
| `stage6_economics/` | Variable-cost ladder + elbow detection |
| `stage7_guardrails/` | Floor price, throttle, **confidence-gated tier assignment** |
| `stage8_output/` | 9-sheet Excel workbook (cuts ↔ reinvestments), plus `track_record.py` (out-of-time backtest + live scorecard) and `leakage.py` (real / borrowed / stolen uplift) |
| `dashboard/` | 4-view interactive HTML output |
| `scripts/diagnostics/` | `data_readiness_report.py`, `baseline_breakdown.py`, plus the validation tools below (`model_credibility_report.py`, `proof_loop.py`, `recovery_test.py`) |
| `scripts/experiments/` | `experiments_robustness*.py` — the 7-experiment harness |
| `scripts/scrape_cost_calculator.py` | Separate cross-platform scrape-cost calculator → `SCRAPE_COST_CALCULATOR.xlsx` (standalone, not part of the pricing pipeline) |
| `doc/` | All documentation |

The previous `archive/` folder has been removed.

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
| `WASTE_REINVEST_REPORT.xlsx` | Formula-driven Excel workbook for the brand team — **9 sheets**: 1 Summary, 2 Glide Path, 3 Track Record, 4 Leakage, 5 By Product, 6 Price Lifts, 7 Price Drops, 8 Needs Test, 9 Data (hidden) |
| `WASTE_REINVEST_REPORT.md` | Same content as plain Markdown |
| `recommendations.csv` | Per-cell action this week (price-led) |
| `elasticity_estimates.csv` | Per-cell elasticity + **confidence_score + confidence_tier** + all sub-scores |
| `waste.csv` / `reinvest.csv` | Stage 8 cuts and reinvestments lists |
| `fact_table.csv` / `features.csv` | Intermediate data for audit |
| `outliers_removed.csv` | Audit trail of statistically dropped days |
| `per_cell_detail.json` | Full per-cell payload for the dashboard |

Every column explained in [doc/OUTPUTS.md](doc/OUTPUTS.md).

### Two sheets worth calling out

- **Track Record** — the receipts. Section A is an out-of-time backtest (train as-of N weeks ago, grade the forecasts on weeks the model never saw) plus a discount-move validation (when price actually moved, did volume respond as predicted?), ending in a plain verdict (e.g. "directional / conservative"). Honest caveat: forward *absolute-volume* R² is ~0 — this is a price-**response** model, not a demand forecaster. Section B is an *illustrative* per-city live scorecard, clearly labelled as not a real acted cycle until the brand acts on fresh data (`score_live()` is built and ready).
- **Leakage** — decomposes promo uplift into **real** vs **borrowed** (pull-forward φ, a dip below baseline *after* a promo) vs **stolen** (cannibalization κ, a dip in sibling packs *during* the promo); `true_incremental_frac = clip(1−φ−κ, 0, 1)`. Pure unit-based, no COGS/margins, and these are **observational proxies, not proven causation** (read "≈" / "consistent with" / "directional signal"). On 24 Mantra most staples look low-leakage; Sunflower Oil 1L shows ~13–18 % pull-forward (stockpiling). The same sheet surfaces the **inelastic gate**: cells with |elasticity| ≤ `INELASTIC_ELASTICITY_THRESHOLD` (1.0) are flagged *"unlikely to pay — hold/raise"*. The Price Drops (reinvest) list now qualifies and headlines on the **net-of-leakage** volume lift (gross × `true_incremental_frac`) and screens out inelastic cells.

---

## Proving the model is trustworthy

Three CLI tools build the receipts a sceptical brand asks for. None set prices; they validate the engine that does.

```bash
python -X utf8 scripts/diagnostics/model_credibility_report.py   # → v4_outputs/_credibility/
python -X utf8 scripts/diagnostics/proof_loop.py                 # → v4_outputs/_proof_loop/
python -X utf8 scripts/diagnostics/recovery_test.py              # → v4_outputs/_recovery/
```

| Tool | What it proves | Outputs |
|---|---|---|
| `model_credibility_report.py` | Decision-model vs full-model accuracy, per-cell confidence calibration, and an omitted-variable (elasticity) bias probe | `CREDIBILITY_REPORT.md`, `decision_vs_full_by_cell.csv`, `confidence_calibration.csv`, `elasticity_bias_probe.csv` |
| `proof_loop.py` | Out-of-time backtest + discount-move validation (the engine behind the Track Record sheet) | `PROOF_LOOP_REPORT.md`, `discount_move_validation.csv` |
| `recovery_test.py` | Plants a *known* elasticity with an endogeneity trap and shows the model recovers it ~3.7× closer to truth than naive; the honest ~0.2 residual over-estimate is reported, not hidden | `RECOVERY_REPORT.md` |

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
