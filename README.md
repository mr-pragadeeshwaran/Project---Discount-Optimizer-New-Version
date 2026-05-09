# Discount Optimizer — V4 Pricing Pipeline

An end-to-end pricing optimization system that ingests raw Blinkit sales data, trains a hierarchical elasticity model across product categories and cities, generates saturation curves, and outputs actionable discount recommendations through an interactive HTML dashboard.

Built for the **Brand Team's weekly workflow**: open dashboard Monday morning → review → approve → export to Blinkit by Tuesday.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Pipeline Stages](#pipeline-stages)
- [Repository Structure](#repository-structure)
- [Setup & Installation](#setup--installation)
- [How to Run](#how-to-run)
- [Understanding the Outputs](#understanding-the-outputs)
- [Dashboard Views](#dashboard-views)
- [Configuration Reference](#configuration-reference)

---

## Architecture Overview

The system processes data through 8 sequential stages. Each stage reads from the previous stage's output, ensuring modularity and reproducibility.

```
┌──────────────────────────────────────────────────────────────────────┐
│                        DATA LAYER                                    │
│                                                                      │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐           │
│  │  Sales .xlsx  │    │  Competitor  │    │ Master Data  │           │
│  │  (Blinkit)   │    │   Prices     │    │ (Costs/Cal)  │           │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘           │
│         │                   │                   │                    │
│         └───────────┬───────┘───────────────────┘                    │
│                     ▼                                                │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  STAGE 1: INGESTION — Load, combine, detect categories      │    │
│  └─────────────────────────┬───────────────────────────────────┘    │
│                             ▼                                        │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  STAGE 2: PREPARATION — Clean, validate, flag OOS/Events   │    │
│  │  Output: fact_table.csv (68K+ rows, SKU × City × Day)      │    │
│  └─────────────────────────┬───────────────────────────────────┘    │
│                             ▼                                        │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  STAGE 3: FEATURES — Log transforms, rolling avgs, gaps     │    │
│  │  Output: features.csv (20 engineered features per row)      │    │
│  └─────────────────────────┬───────────────────────────────────┘    │
└─────────────────────────────┼────────────────────────────────────────┘
                              │
┌─────────────────────────────┼────────────────────────────────────────┐
│                        MODEL LAYER                                   │
│                             ▼                                        │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  STAGE 4: HIERARCHICAL MODEL                                │    │
│  │  statsmodels MixedLM with partial pooling:                  │    │
│  │    Category (Jaggery/Dal/Oil)                               │    │
│  │      └─ City (11 cities)                                    │    │
│  │          └─ SKU × City (individual cell)                    │    │
│  │  Output: elasticity_estimates.csv (per-cell elasticity+SE)  │    │
│  └─────────────────────────┬───────────────────────────────────┘    │
│                             ▼                                        │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  STAGE 5: SATURATION CURVES                                 │    │
│  │  Sweep discount 0%→30%, predict units at each level         │    │
│  │  Fit 4-Parameter Logistic (4PL) curve per cell              │    │
│  │  Assign confidence: High / Medium / Low / Needs Experiment  │    │
│  └─────────────────────────┬───────────────────────────────────┘    │
└─────────────────────────────┼────────────────────────────────────────┘
                              │
┌─────────────────────────────┼────────────────────────────────────────┐
│                     OPTIMIZATION LAYER                               │
│                             ▼                                        │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  STAGE 6: ECONOMICS + ELBOW DETECTION                       │    │
│  │  Variable cost = COGS + Commission + Fulfillment            │    │
│  │  Contribution margin at each discount level                 │    │
│  │  Marginal ROI → find "elbow" where ROI crosses 1.0          │    │
│  └─────────────────────────┬───────────────────────────────────┘    │
│                             ▼                                        │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  STAGE 7: GUARDRAILS + TIERING                              │    │
│  │  Floor price check, competitor ceiling, max change rate      │    │
│  │  Tier: Strong Cut / Trade-off / Hold / Increase / Do Not Act│    │
│  │  Output: recommendations.csv (final actionable list)        │    │
│  └─────────────────────────┬───────────────────────────────────┘    │
│                             ▼                                        │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  DASHBOARD: 4-View interactive HTML for Brand Team          │    │
│  │  Output: BRAND_DASHBOARD.html                               │    │
│  └─────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Pipeline Stages

### Stage 1 — Data Ingestion (`stage1_ingestion/ingest.py`)
- Reads all `.xlsx` files from the configured sales data directory
- Combines multiple product files (Jaggery, Moong Dal, Sunflower Oil) into one DataFrame
- Auto-detects product category from title keywords
- Deduplicates rows by (Product ID, City, Date)
- Loads event/festival calendar and master cost data

### Stage 2 — Data Preparation (`stage2_preparation/prepare.py`)
- Joins raw sales + competitor + master data into one fact table
- Forward-fills missing prices and availability within each cell
- Validates ranges (price within 30-110% of MRP, availability 0-100%)
- Flags special days:
  - `is_oos_day` — availability below 50%
  - `is_event_day` — BBD, platform sales, etc.
  - `is_festival` — Diwali, Holi, Eid, etc.
  - `is_regular_day` — clean day usable for model training

### Stage 3 — Feature Engineering (`stage3_features/features.py`)
- Computes log transforms (`log_price`, `log_units`) for log-log elasticity
- Calculates competitive position (`price_gap`, `rpi`)
- Builds customer reference price (30-day rolling average)
- Smooths noisy signals (7-day rolling OSA, ad spend)
- Adds calendar features (day of week, month dummies, weekend flag)

### Stage 4 — Hierarchical Elasticity Model (`stage4_model/elasticity.py`)
- Trains a `statsmodels.MixedLM` (mixed-effects model) on regular days only
- Partial pooling: cells with thin data borrow strength from category/city peers
- Random slope on `log_price` allows per-category elasticity variation
- Time-based 80/20 train/test split
- Outputs per-cell elasticity with standard error and 95% confidence interval

### Stage 5 — Saturation Curves (`stage5_curves/curves.py`)
- For each cell, sweeps discount from 0% to 30% in 1% steps
- Predicts expected units at each discount level using the elasticity model
- Fits a 4-Parameter Logistic (4PL) curve for smooth interpolation
- Assigns confidence flags based on observation count, SE, and observed discount range

### Stage 6 — Economics + Elbow Detection (`stage6_economics/economics.py`)
- Calculates variable cost per unit: `COGS + (commission% × price) + fulfillment fee`
- Computes contribution margin at every discount level
- Calculates marginal ROI between adjacent levels
- Finds the "elbow": where marginal ROI drops below 1.0 (each additional rupee of discount returns less than ₹1 of margin)

### Stage 7 — Guardrails + Tiering (`stage7_guardrails/guardrails.py`)
- **Floor price**: ensures recommended price covers variable cost + 5% margin
- **Max change rate**: limits discount change to 3 percentage points per cycle
- **Throttling**: if elbow is far from current, builds a multi-cycle phasing plan
- **Tier assignment**:
  - **Strong Cut** — high savings, low risk, high confidence → fast-track approval
  - **Trade-off** — meaningful savings but moderate volume risk → needs review
  - **Hold** — already at or near elbow → no action
  - **Increase** — under-discounted, lifting discount adds margin
  - **Do Not Act** — low confidence, needs A/B testing first

---

## Repository Structure

```
Discount-Optimizer-New-Version/
│
├── README.md                      # This documentation
├── OUTPUT_GUIDE.md                # Detailed guide to reading output files
├── v4_config.py                   # Central configuration (all thresholds)
├── pipeline.py                    # Master orchestrator (runs Stages 1-7)
├── .gitignore                     # Excludes outputs, data, cache from repo
│
├── stage1_ingestion/
│   ├── __init__.py
│   └── ingest.py                  # Load Excel files, detect categories
│
├── stage2_preparation/
│   ├── __init__.py
│   └── prepare.py                 # Clean, validate, flag fact table
│
├── stage3_features/
│   ├── __init__.py
│   └── features.py                # Engineer 20 modeling features
│
├── stage4_model/
│   ├── __init__.py
│   └── elasticity.py              # Hierarchical MixedLM model
│
├── stage5_curves/
│   ├── __init__.py
│   └── curves.py                  # 4PL saturation curve fitting
│
├── stage6_economics/
│   ├── __init__.py
│   └── economics.py               # Contribution margin + elbow finder
│
├── stage7_guardrails/
│   ├── __init__.py
│   └── guardrails.py              # Business rules + tier assignment
│
├── stage8_monitoring/
│   └── __init__.py                # [Phase 2] Drift detection placeholder
│
├── dashboard/
│   ├── __init__.py
│   └── dashboard_generator.py     # 4-view HTML dashboard builder
│
├── data/                          # [gitignored] Input data directory
│   └── master/                    # Optional master cost/calendar files
│
└── v4_outputs/                    # [gitignored] Pipeline run outputs
    └── {YYYYMMDD_HHMMSS}/        # Timestamped run folder
        ├── fact_table.csv
        ├── features.csv
        ├── elasticity_estimates.csv
        ├── recommendations.csv
        └── BRAND_DASHBOARD.html
```

---

## Setup & Installation

### Prerequisites
- Python 3.10+
- Required packages: `pandas`, `numpy`, `scipy`, `statsmodels`, `openpyxl`

### Install dependencies
```bash
pip install pandas numpy scipy statsmodels openpyxl
```

### Configure data path
Edit `v4_config.py` and set `SALES_DATA_DIR` to your directory containing `.xlsx` files:
```python
SALES_DATA_DIR = r"D:\path\to\your\data"
```

---

## How to Run

### Full pipeline (all 7 stages)
```bash
python -X utf8 pipeline.py
```

### Run specific stages only
```bash
python -X utf8 pipeline.py --stages 4 5 6 7    # Re-run model + downstream only
python -X utf8 pipeline.py --stages 6 7         # Re-evaluate economics + guardrails
```

### Operating cadence
| Cadence | Stages | What it does |
|---------|--------|-------------|
| **Daily** | 1, 2, 3 | Refresh data, rebuild fact table and features |
| **Weekly** | 6, 7 | Re-evaluate economics with latest data, generate new recommendations |
| **Monthly** | 4, 5 | Retrain elasticity model, regenerate saturation curves |
| **Full** | 1-7 | Complete end-to-end pipeline |

> **Note:** On Windows, use `python -X utf8` to avoid encoding issues with Unicode characters in console output.

---

## Understanding the Outputs

Each pipeline run creates a timestamped folder in `v4_outputs/` containing 5 files. Here is how to read each one.

### 1. `fact_table.csv` — The raw unified dataset

This is the cleaned, validated, and flagged version of your raw data. One row per SKU × City × Day.

| Column | Description | Example |
|--------|-------------|---------|
| `PRODUCT_ID` | Blinkit SKU identifier | `126995` |
| `GC_CITY` | City name | `Bangalore` |
| `DATE` | Calendar date | `2025-06-15` |
| `TITLE` | Product display name | `24 Mantra Organic Moong Dal` |
| `PRICE` | Actual selling price (₹) | `85` |
| `MRP` | Maximum retail price (₹) | `110` |
| `OFFTAKE_QTY` | Units sold that day | `42` |
| `OFFTAKE_MRP` | Revenue at MRP value (₹) | `4,620` |
| `WT_AVAILABILITY_PCT` | Weighted availability (0-100%) | `94.5` |
| `WT_DISCOUNT_PCT` | Platform-reported discount % | `22.7` |
| `Competitor Price` | Weighted avg competitor price (₹) | `88` |
| `category` | Auto-detected category | `Moong Dal` |
| `discount_pct_actual` | Calculated: `(MRP - Price) / MRP × 100` | `22.7` |
| `is_oos_day` | 1 if availability < 50% (excluded from training) | `0` |
| `is_event_day` | 1 if BBD, platform sale, or festival | `0` |
| `is_festival` | 1 if national festival (Diwali, Holi, etc.) | `0` |
| `is_regular_day` | 1 if not OOS and not event (used for training) | `1` |
| `cell_id` | Unique identifier: `{product_id}_{city}` | `126995_Bangalore` |

**How to use it:** This is your audit trail. If anyone questions a recommendation, trace back to the raw daily data here. Filter by `cell_id` to see the full history of any SKU×City combination.

---

### 2. `features.csv` — Model-ready features

Same grain as `fact_table.csv` but with 20 engineered features added. Used as input to Stage 4 (model training).

| Feature | Formula / Source | Why it matters |
|---------|-----------------|----------------|
| `log_price` | `ln(selling_price)` | Log-log elasticity: a 1% price change → X% unit change |
| `log_units` | `ln(units_sold)` | Target variable for the elasticity model |
| `discount_pct` | `(MRP - price) / MRP × 100` | Discount depth as percentage |
| `price_gap` | `own_price - competitor_price` | How far you are from competition (₹) |
| `rpi` | `own_price / competitor_price` | Relative price index (1.0 = parity) |
| `reference_price` | 30-day rolling avg of own price | Customer's mental anchor price |
| `price_vs_reference` | `(price / reference_price) - 1` | Deviation from what customers expect |
| `osa_rolling_7d` | 7-day rolling mean of availability | Smoothed availability (0 to 1) |
| `log_ad_sov` | `ln(7-day rolling ad share of voice)` | Advertising intensity |
| `is_weekend` | 1 for Saturday/Sunday | Weekend shopping patterns |
| `is_promotional` | 1 if price < 85% of competitor | Deep promotional flag |
| `month_2` to `month_12` | Monthly dummy variables (Jan = baseline) | Seasonality effects |

**How to use it:** Inspect feature distributions to understand data quality. A feature with zero variance in a cell means that lever didn't move during the observation period (the model can't learn from it).

---

### 3. `elasticity_estimates.csv` — Per-cell price elasticity

One row per cell (SKU × City). This is the core model output.

| Column | Description | How to read it |
|--------|-------------|----------------|
| `product_id` | SKU identifier | — |
| `city` | City name | — |
| `category` | Jaggery / Moong Dal / Sunflower Oil | Used as grouping variable in hierarchical model |
| `title` | Product name (truncated to 60 chars) | — |
| `mrp` | MRP (₹) — mode from training data | — |
| `avg_price` | Average observed selling price (₹) | — |
| `avg_units` | Average daily units sold | Baseline volume for curve generation |
| `avg_discount_pct` | Average observed discount % | Where this cell typically operates |
| `n_observations` | Total data points for this cell | More data → more reliable elasticity |
| `n_train` | Data points used for training | After filtering out event/OOS days |
| `elasticity` | **Price elasticity of demand** | **Key metric.** Should be negative. E.g., `-2.5` means a 1% price increase → 2.5% volume drop |
| `elasticity_se` | Standard error of elasticity | Lower = more precise. High SE → less confidence |
| `elasticity_lower` | 95% CI lower bound | — |
| `elasticity_upper` | 95% CI upper bound | If this crosses zero, elasticity is not statistically significant |
| `cell_id` | `{product_id}_{city}` | Join key across all output files |

**How to read elasticity values:**
- `-1.0` = Unit elastic. A 10% price increase loses exactly 10% volume.
- `-0.5` = Inelastic. Price increases lose relatively little volume. Good candidates for discount cuts.
- `-3.0` = Highly elastic. Very price-sensitive. Cutting discounts here is risky.
- `> 0` = Should not happen (model issue). Flagged automatically.

---

### 4. `recommendations.csv` — Final actionable recommendations

One row per cell. This is the **primary output** used by the Brand Team.

#### Identification columns
| Column | Description |
|--------|-------------|
| `product_id`, `city`, `category`, `title`, `mrp`, `cell_id` | Cell identification |
| `confidence` | Model confidence: `High`, `Medium`, `Low`, or `Needs Experiment` |
| `elasticity` | Price elasticity (from Stage 4) |
| `n_observations` | Data points available |

#### Current state (what's happening today)
| Column | Description | Example |
|--------|-------------|---------|
| `current_discount_pct` | Average discount currently applied | `22.0` |
| `current_price` | Current selling price (₹) | `85.8` |
| `current_units_day` | Average daily units at current discount | `175` |
| `current_revenue_day` | Average daily revenue (₹) | `15,015` |
| `current_margin_day` | Average daily contribution margin (₹) | `3,500` |

#### Model recommendation (what the elbow suggests)
| Column | Description | Example |
|--------|-------------|---------|
| `elbow_discount_pct` | Optimal discount at the ROI elbow | `13.0` |
| `elbow_price` | Price at the elbow (₹) | `95.7` |
| `elbow_units_day` | Predicted daily units at elbow | `170` |
| `elbow_revenue_day` | Predicted daily revenue at elbow (₹) | `16,269` |
| `elbow_margin_day` | Predicted daily margin at elbow (₹) | `5,100` |
| `elbow_marginal_roi` | Marginal ROI at elbow point | `1.1` |

#### Impact assessment
| Column | Description | How to read it |
|--------|-------------|----------------|
| `vol_change_pct` | Expected volume change (%) | Negative = you lose some units. `-2.8%` is mild. |
| `rev_change_pct` | Expected revenue change (%) | Often positive even when volume drops (higher price). |
| `margin_change_monthly` | Monthly margin improvement (₹) | Positive = you make more profit. |
| `monthly_savings` | Monthly discount spend reduction (₹) | Positive = you spend less on discounts. |

#### Guardrail checks
| Column | Description | Values |
|--------|-------------|--------|
| `guardrail_floor_ok` | Price above minimum viable floor? | `True` / `False` |
| `guardrail_competitor_ok` | Price within competitor ceiling? | `True` / `False` |
| `guardrail_change_ok` | Change within max per-cycle limit? | `True` / `False` |
| `is_throttled` | Was the recommendation throttled? | `True` = elbow too far, phasing needed |
| `phasing_plan` | Multi-cycle path to reach elbow | `22% → 19% → 16% → 13%` |

#### Final recommendation (after guardrails)
| Column | Description |
|--------|-------------|
| `rec_discount_pct` | **Recommended discount % to set this week** |
| `rec_price` | **Recommended selling price (₹)** |
| `rec_units_day` | Expected units at recommended price |
| `rec_revenue_day` | Expected revenue at recommended price |
| `rec_vol_change_pct` | Volume change from current to recommended |
| `rec_rev_change_pct` | Revenue change from current to recommended |
| `rec_monthly_savings` | Monthly discount savings at recommended level |

#### Tier assignment
| Column | Values | What it means |
|--------|--------|---------------|
| `tier` | `Strong Cut` | Safe to approve — high savings, low risk, confident model |
| | `Trade-off` | Needs judgment — meaningful savings but notable volume risk |
| | `Hold` | Already at or near optimal — no change needed |
| | `Increase` | Under-discounted — increasing discount would add margin |
| | `Do Not Act` | Insufficient data — run an A/B test before changing |

---

### 5. `BRAND_DASHBOARD.html` — Interactive dashboard

Open in any web browser. Contains 4 views (see Dashboard section below).

---

## Dashboard Views

### View 1 — Portfolio Summary
The landing page. Answers three questions in 5 seconds:
1. **Are we on track?** — Current discount vs target, glide-path status
2. **What needs attention?** — Cell counts per tier with savings potential
3. **Is the model trustworthy?** — Last week's predicted vs actual performance

### View 2 — Action Queue
Where the Brand Team works. A sortable table grouped by tier:
- **Tier 1 (Green)**: Bulk-approve with one click
- **Tier 2 (Amber)**: Review individually — inline warnings for volume drops > 5%
- **Tier 3 (Blue)**: Increase recommendations — approve or defer

Click any row to drill into View 3.

### View 3 — Cell Detail
Full picture of one SKU × City:
- Current vs Recommended side-by-side comparison
- "Why this recommendation" in plain language (marginal ROI explanation)
- Guardrails applied and any throttling/phasing
- Override options (accept, modify, defer, or flag as strategic)

### View 4 — Export
Convert approved decisions to uploadable CSVs:
- Blinkit-format CSV for direct upload
- Internal review CSV with full model context
- Audit log for compliance

---

## Configuration Reference

All thresholds and business rules are centralized in `v4_config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `SALES_DATA_DIR` | `D:\...\Top 3 Products Data` | Path to Excel files |
| `OSA_OOS_THRESHOLD` | `50` | Below this availability % = OOS day |
| `MODEL_TYPE` | `mixed_lm` | `mixed_lm` or `bayesian` |
| `DISCOUNT_MAX_PCT` | `30` | Maximum discount in curve sweep |
| `DEFAULT_COGS_PCT` | `0.50` | COGS as fraction of MRP (if no master file) |
| `DEFAULT_COMMISSION_PCT` | `0.15` | Blinkit commission rate |
| `DEFAULT_FULFILLMENT_FEE` | `10` | ₹ per unit fulfillment cost |
| `MARGINAL_ROI_THRESHOLD` | `1.0` | Elbow: where ROI drops below this |
| `MIN_MARGIN_PCT` | `0.05` | Minimum margin above variable cost |
| `MAX_DISCOUNT_CHANGE_PPT` | `3` | Max percentage-point change per cycle |
| `TARGET_DISCOUNT_PCT` | `10.0` | Glide-path target |
| `TARGET_QUARTER` | `Q4 2026` | When to reach target |

---

## License

Internal use only. Not for redistribution.
