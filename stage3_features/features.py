"""
Stage 3 — Feature Engineering.

Transforms the fact table into modeling-ready features at SKU × Grammage × City × Day grain.

MODEL DESIGN: Dual-signal log-log model
  log(units) ~ log(selling_price)   ← consumer price level (PRIMARY)
             + discount_pct         ← badge/deal signal (SECONDARY)
             + controls

  Coefficient on log(selling_price) = PRICE ELASTICITY
    "A 1% increase in selling price → elasticity% change in units"
    Typically negative: higher price → fewer units (e.g., -2.0 means 1% price rise → 2% drop)

  Coefficient on discount_pct = BADGE SENSITIVITY
    "An extra 1ppt of discount badge → X% more units (holding price constant)"
    Typically small and positive — captures the psychological "deal" effect

  This dual-signal design answers TWO business questions:
    1. How sensitive are consumers to absolute price? (price elasticity)
    2. Does showing a bigger discount badge lift volume beyond the price effect? (badge)

All features computed per-cell (grouped by product_id + grammage + city).
"""
import pandas as pd
import numpy as np
import v4_config as cfg


def engineer_features(fact_df: pd.DataFrame) -> pd.DataFrame:
    """
    Build all modeling features from the clean fact table.
    Operates per-cell (SKU × Grammage × City) to respect group boundaries.
    """
    C = cfg.COL
    df = fact_df.copy()
    print(f"  [Stage 3] Input: {len(df):,} rows")

    # Build cell grouping keys — include grammage if present
    has_grammage = C["grammage"] in df.columns
    grp = [C["product_id"], C["city"]]
    if has_grammage:
        grp = [C["product_id"], C["grammage"], C["city"]]

    sort_keys = grp + [C["date"]]
    df = df.sort_values(sort_keys)

    # ── Target variable ──────────────────────────────────────────────
    df["log_units"]   = np.log(df[C["offtake_qty"]].clip(lower=0.1))
    df["log_revenue"] = np.log(df[C["offtake_mrp"]].clip(lower=0.1))

    # ── Primary price signal: log(selling_price) ─────────────────────
    # selling_price = stable_mrp × (1 - discount/100) — computed in Stage 2
    # This is the actual rupee price the consumer sees on Blinkit.
    # log transform makes the coefficient a standard price elasticity.
    if "selling_price" not in df.columns:
        # Fallback: reconstruct from discount_pct_actual
        disc = df.get("discount_pct_actual", df[C["discount_pct"]]).clip(0, 80)
        stable_mrp = df.get("stable_mrp", df[C["mrp"]])
        df["selling_price"] = (stable_mrp * (1 - disc / 100)).clip(lower=1.0)

    df["log_price"] = np.log(df["selling_price"].clip(lower=1.0))

    # ── Badge signal: discount_pct ────────────────────────────────────
    # Blinkit shows a red badge "X% OFF" — this is a separate psychological
    # trigger from the price level itself. Holding price constant, a bigger
    # badge number may lift conversions (deal-seeker behaviour).
    df["discount_pct"] = df["discount_pct_actual"].clip(lower=0, upper=70)

    # ── Price surprise (vs 30-day moving average selling price) ──────
    # How much cheaper today vs the recent "expected price"?
    # Positive = cheaper than usual → positive demand shock
    df["avg_selling_price_30d"] = df.groupby(grp)["selling_price"].transform(
        lambda s: s.rolling(cfg.REFERENCE_PRICE_WINDOW, min_periods=1).mean()
    )
    df["price_surprise"] = df["avg_selling_price_30d"] - df["selling_price"]  # positive = cheaper

    # ── Discount surprise (badge vs expectation) ──────────────────────
    df["reference_discount"] = df.groupby(grp)["discount_pct"].transform(
        lambda s: s.rolling(cfg.REFERENCE_PRICE_WINDOW, min_periods=1).mean()
    )
    df["discount_surprise"] = df["discount_pct"] - df["reference_discount"]

    # ── Competitive position ─────────────────────────────────────────
    comp = df[C["competitor_price"]].replace(0, np.nan)
    # Relative price index vs competitor (>1 = more expensive than competitor)
    df["rpi"] = (df["selling_price"] / comp.fillna(df["selling_price"])).fillna(1.0).clip(0.5, 2.0)

    # ── Rolling smoothed features ─────────────────────────────────────
    df["osa_rolling_7d"] = df.groupby(grp)[C["availability"]].transform(
        lambda s: s.rolling(cfg.OSA_ROLLING_WINDOW, min_periods=1).mean()
    ) / 100.0

    df["ad_rolling_7d"] = df.groupby(grp)[C["ad_sov"]].transform(
        lambda s: s.rolling(cfg.AD_ROLLING_WINDOW, min_periods=1).mean()
    )
    df["log_ad_sov"] = np.log1p(df["ad_rolling_7d"])

    # ── Non-linear price transform ────────────────────────────────────
    # log1p_discount: smooth non-linear badge effect
    df["log1p_discount"] = np.log1p(df["discount_pct"])

    # ── Time features ─────────────────────────────────────────────────
    df["day_of_week"] = df[C["date"]].dt.dayofweek
    df["is_weekend"]  = (df["day_of_week"] >= 5).astype(int)
    df["month"]       = df[C["date"]].dt.month

    # Month dummies (drop month 1 as baseline)
    for m in range(2, 13):
        df[f"month_{m}"] = (df["month"] == m).astype(int)

    # Day-of-week dummies (drop dow=0 / Monday as baseline) — captures
    # the systematic weekday-vs-weekend grocery shopping rhythm that
    # E1 experiments showed is a major component of within-cell variance.
    for d in range(1, 7):
        df[f"dow_{d}"] = (df["day_of_week"] == d).astype(int)

    # ── Deep promo flag ───────────────────────────────────────────────
    df["is_deep_promo"] = (df["discount_pct"] > 20).astype(int)

    # ── Lag / momentum features (computed PER CELL, sorted by date) ──
    # These were the single biggest accuracy lever in the robustness
    # experiments (scripts/experiments/experiments_robustness.py):
    # within-cell test R² median moved from -0.43 → -0.04 when added.
    # Each lag is computed BEFORE filtering — they use the cell's own
    # earlier rows regardless of regular/event/OOS status, so the model
    # always has a fresh signal for "how did this cell perform recently".
    df["lag1_log_units"] = df.groupby(grp)["log_units"].shift(1)
    df["lag7_log_units"] = df.groupby(grp)["log_units"].shift(7)
    df["lag1_log_price"] = df.groupby(grp)["log_price"].shift(1)
    df["lag1_discount"]  = df.groupby(grp)["discount_pct"].shift(1)
    df["rolling_mean_7d_log_units"] = (
        df.groupby(grp)["log_units"]
          .transform(lambda s: s.shift(1).rolling(7, min_periods=2).mean())
    )
    df["rolling_mean_14d_log_units"] = (
        df.groupby(grp)["log_units"]
          .transform(lambda s: s.shift(1).rolling(14, min_periods=3).mean())
    )
    # Sensible fill for the first 7-14 days of each cell — use cell mean
    # so we don't drop those rows entirely. The model can still distinguish
    # a "warm-up" row via the cell intercept.
    lag_cols = ["lag1_log_units", "lag7_log_units", "lag1_log_price", "lag1_discount",
                "rolling_mean_7d_log_units", "rolling_mean_14d_log_units"]
    for col in lag_cols:
        df[col] = df.groupby(grp)[col].transform(lambda s: s.fillna(s.mean()))
        df[col] = df[col].fillna(df[col].mean())  # global fallback for entirely-NaN cells

    # ── Drop rows with NaN in critical features ──────────────────────
    feature_cols = get_feature_columns()
    target_col   = "log_units"
    before = len(df)
    df = df.dropna(subset=feature_cols + [target_col])
    dropped = before - len(df)
    if dropped:
        print(f"    Dropped {dropped} rows with NaN in features")

    print(f"  [Stage 3] Output: {len(df):,} rows x {len(feature_cols)} features")
    print(f"    selling_price range: ₹{df['selling_price'].min():.1f} – ₹{df['selling_price'].max():.1f}  "
          f"(mean=₹{df['selling_price'].mean():.1f})")
    print(f"    log_price range: {df['log_price'].min():.3f} to {df['log_price'].max():.3f}")
    print(f"    discount_pct range: {df['discount_pct'].min():.1f}% to {df['discount_pct'].max():.1f}%  "
          f"(mean={df['discount_pct'].mean():.1f}%, std={df['discount_pct'].std():.1f}%)")
    print(f"    log_units range: {df['log_units'].min():.2f} to {df['log_units'].max():.2f}")
    return df.reset_index(drop=True)


def get_feature_columns() -> list:
    """Return the ordered list of feature columns for the elasticity model."""
    base = [
        "log_price",          # PRIMARY: log(selling_price) → price elasticity
        "discount_pct",       # SECONDARY: badge/deal signal
        "log1p_discount",     # Non-linear badge transform
        "price_surprise",     # Cheaper than 30d avg selling price
        "discount_surprise",  # Bigger badge than recent expectation
        "osa_rolling_7d",     # Availability (0-1)
        "log_ad_sov",         # Advertising signal
        "rpi",                # Relative price vs competitor
        "is_weekend",         # Weekend demand lift
        "is_deep_promo",      # Deep promo flag
        # Lag / momentum (added May 2026 — see MODEL_EXPERIMENTS.md):
        "lag1_log_units", "lag7_log_units",
        "lag1_log_price", "lag1_discount",
        "rolling_mean_7d_log_units", "rolling_mean_14d_log_units",
    ]
    month_cols = [f"month_{m}" for m in range(2, 13)]
    dow_cols   = [f"dow_{d}"   for d in range(1, 7)]
    return base + month_cols + dow_cols


if __name__ == "__main__":
    from stage1_ingestion.ingest import ingest_all_sales, load_event_calendar
    from stage2_preparation.prepare import prepare_fact_table
    raw  = ingest_all_sales()
    cal  = load_event_calendar()
    fact = prepare_fact_table(raw, cal)
    feat = engineer_features(fact)
    print(f"\nFeature table: {feat.shape}")
    print(feat[get_feature_columns()].describe().round(3))
