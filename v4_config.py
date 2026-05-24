"""
v4_config.py — Central configuration for the 8-Stage Pricing Optimization System.

Designed for 24 Mantra Organic SKUs across 11 cities on Blinkit.
Competitor brands are excluded from modeling; their pricing is captured
via the Competitor Price column for competitive positioning features.
"""
import os

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  STAGE 1: DATA INGESTION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SALES_DATA_DIR = os.path.join(os.path.dirname(__file__), "input_data")
MASTER_DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "master")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "v4_outputs")

# Column name mapping (raw Excel → internal)
COL = {
    "product_id":   "PRODUCT_ID",
    "platform":     "GC_PLATFORM",
    "date":         "DATE",
    "title":        "TITLE",
    "grammage":     "GRAMMAGE",
    "city":         "GC_CITY",
    "brand":        "BRAND",
    "offtake_mrp":  "OFFTAKE_MRP",
    "offtake_qty":  "OFFTAKE_QTY",
    "price":        "PRICE",
    "mrp":          "MRP",
    "availability": "WT_AVAILABILITY_PCT",
    "discount_pct": "WT_DISCOUNT_PCT",
    "cat_share":    "MONTHLY_CAT_SHARE_MRP",
    "overall_sov":  "MONTHLY_OVERALL_SOV",
    "organic_sov":  "MONTHLY_ORGANIC_SOV",
    "ad_sov":       "MONTHLY_AD_SOV",
    "wt_avg_ppu":   "WT_AVG_PPU_X100",
    "competitor_price": "Competitor Price",
    "rpi":          "Relative Price Index",
}

# Category detection from product title keywords
CATEGORY_KEYWORDS = {
    "Jaggery":       ["jaggery"],
    "Moong Dal":     ["moong", "dal"],
    "Sunflower Oil": ["sunflower", "oil"],
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  STAGE 2: DATA PREPARATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OSA_OOS_THRESHOLD = 50  # Below this % availability = out-of-stock day

# ── Per-cell outlier detection (within product × grammage × city) ──
# Rows with |log_units z-score| > this threshold (computed per cell on
# regular days) are flagged and excluded from training. Saved to
# outliers_removed.csv in the run directory for audit.
#
# Empirically tuned (see scripts/experiments/experiments_mape.py):
# z=2.0 with TRAIN_LOOKBACK_DAYS=180 produced the strong-tier model.
# Loosen to 3.0 if you want to keep more days (will reduce R² ~3 ppt).
OUTLIER_Z_THRESHOLD       = 2.0
OUTLIER_MIN_OBS_PER_CELL  = 30   # Need at least this many obs to compute z

# ── Training lookback window ──
# Stage 4 trains only on regular days within the last N days. Restricting
# to recent steady-state data avoids contamination from:
#  - launch-ramp periods (e.g. Moong Dal grew 16× in the first 9 months)
#  - older price regimes that don't match the current market
# This was the single biggest accuracy lever — increased aggregated R²
# from 0.40 → 0.94 and dropped MAPE from 53% → 23%.
# Set to None to train on full history (the old behavior).
TRAIN_LOOKBACK_DAYS = 180

# Event / festival calendar (Indian market)
FESTIVAL_DATES = {
    # Format: "YYYY-MM-DD": "event_name"
    "2025-01-14": "Makar Sankranti",
    "2025-03-14": "Holi",
    "2025-03-31": "Eid ul-Fitr",
    "2025-04-14": "Baisakhi",
    "2025-08-15": "Independence Day",
    "2025-08-27": "Janmashtami",
    "2025-10-02": "Gandhi Jayanti",
    "2025-10-12": "Dussehra",
    "2025-10-20": "Diwali",
    "2025-11-01": "Diwali (extended)",
    "2025-11-15": "Guru Nanak Jayanti",
    "2025-12-25": "Christmas",
    "2026-01-14": "Makar Sankranti",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
}

# Blinkit platform event days (BBD = Big Billion Days etc.)
PLATFORM_EVENT_WINDOWS = {
    # (start, end): event_name  — approximate windows
    ("2025-09-27", "2025-10-06"): "BBD",
    ("2025-11-20", "2025-11-30"): "Black Friday Sale",
    ("2026-01-15", "2026-01-20"): "Republic Day Sale",
}

FESTIVAL_WINDOW_DAYS = 2  # days before/after a festival to flag

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  STAGE 3: FEATURE ENGINEERING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REFERENCE_PRICE_WINDOW = 30   # Rolling window for customer reference price
OSA_ROLLING_WINDOW = 7
AD_ROLLING_WINDOW = 7

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  STAGE 4: HIERARCHICAL ELASTICITY MODEL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODEL_TYPE = "mixed_lm"  # "mixed_lm" (statsmodels) or "bayesian" (PyMC)
TEST_SPLIT_PCT = 0.20
RANDOM_STATE = 42

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  STAGE 5: SATURATION CURVES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DISCOUNT_MIN_PCT = 0
DISCOUNT_MAX_PCT = 30
DISCOUNT_STEP_PCT = 1
EXTRAPOLATION_FLAG_PCT = 50   # Flag if >50% of curve is outside training range
STABILITY_VARIATION_THRESHOLD = 0.20  # 20% param variation = unstable

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  STAGE 6: ECONOMICS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFAULT_COGS_PCT = 0.50       # 50% of MRP
DEFAULT_COMMISSION_PCT = 0.15  # 15% Blinkit commission
DEFAULT_FULFILLMENT_FEE = 10   # ₹10/unit
MARGINAL_ROI_THRESHOLD = 1.0   # Elbow: where marginal ROI crosses this

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  STAGE 7: GUARDRAILS + TIERING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MIN_MARGIN_PCT = 0.05          # 5% min margin above variable cost
MAX_COMPETITOR_PREMIUM_PCT = 0.10  # Max 10% above competitor

# ── Per-cycle discount change cap ──
# Two modes:
#   1. DYNAMIC (default): system derives the per-cell step from the user-
#      set duration in TARGET_TIMELINE_WEEKS. Each cell walks from its
#      current discount to its target over that many weeks.
#         per_cell_step = (current_disc − target_disc) / TARGET_TIMELINE_WEEKS
#      Still bounded by MAX_DISCOUNT_CHANGE_PPT as an absolute safety rail.
#   2. STATIC: every cell capped at MAX_DISCOUNT_CHANGE_PPT/cycle.
USE_DYNAMIC_GLIDE      = True
TARGET_TIMELINE_WEEKS  = 12    # User-editable: ~3 months. HARD upper
                                # bound — every cell's full gap must
                                # close within this many cycles, no
                                # exceptions. This is the ONLY upper
                                # constraint on the per-cycle step.
MIN_DISCOUNT_CHANGE_PPT = 3    # Each weekly cut is at least this big —
                                # no tiny moves that don't meaningfully
                                # shift the customer price. If a cell's
                                # gap is smaller than this, the cell
                                # closes the whole gap in ONE cycle.

# ── What's the END point of the glide? ──
# True (default):   target = historical_floor_discount per cell.
#                   The proven-safe minimum discount based on the cell's
#                   own past behaviour. The system never pushes a cell
#                   to a discount level it has never operated at.
# False:            target = elbow_discount per cell (margin-optimal,
#                   often 0% with the current cost structure).
USE_HISTORICAL_FLOOR_TARGET  = True

# What percentile of the cell's recent observed discounts counts as the
# "floor we know it can survive". 25 = lower quartile = "we've been here
# on ~25% of days, units held up, so we can return to this safely".
HISTORICAL_FLOOR_PERCENTILE  = 25
HISTORICAL_FLOOR_LOOKBACK_DAYS = 90   # Look back 90 days for the floor
STRATEGIC_SKUS = []             # SKU IDs with override rules

# Tiering thresholds
TIER_STRONG_CUT_MIN_SAVINGS = 10000    # ₹10K/month minimum for Strong Cut
TIER_STRONG_CUT_MAX_VOL_DROP = 0.05    # Max 5% volume drop
TIER_TRADEOFF_MAX_VOL_DROP = 0.10      # Max 10% volume drop for Trade-off
TIER_INCREASE_MIN_MARGINAL_ROI = 2.0   # If marginal ROI > 2, under-discounted

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  STAGE 8: MONITORING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VOLUME_DROP_TOLERANCE_PCT = 5.0   # Alert if actual > predicted + this
DRIFT_ALERT_THRESHOLD = 0.15      # 15% prediction error = drift

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DASHBOARD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TARGET_DISCOUNT_PCT = 10.0
TARGET_QUARTER = "Q4 2026"

# ── Portfolio flywheel: target weighted discount across all cells ──
# Stage 8 rebalances cuts ↔ reinvestments to move the revenue-weighted
# portfolio discount toward this target. Change to retune the flywheel.
TARGET_WEIGHTED_DISCOUNT_PCT = 9.0

# Strategic reinvestment criteria (Stage 8):
# A cell qualifies as a growth-reinvest candidate if a +3 ppt discount move
# would lift volume by ≥ MIN_VOL_LIFT_PCT and sacrifice margin by no more
# than MAX_MARGIN_SAC_PCT of current contribution.
REINVEST_MIN_VOL_LIFT_PCT  = 5.0
REINVEST_MAX_MARGIN_SAC_PCT = 10.0
REINVEST_MIN_ELASTICITY    = 2.0  # |elast| must be at least this
BRAND_NAME = "24 Mantra Organic"
# Brand name patterns to filter own-brand SKUs (case-insensitive match)
# All other brands are treated as competitors
OWN_BRAND_PATTERNS = ["24 Mantra Organic", "24 Mantra"]
PLATFORM_NAME = "Blinkit"
