"""
Stage 3 — Feature Engineering.

Transforms the fact table into modeling-ready features at SKU × City × Day grain.
All features computed per-cell (grouped by product_id + city).
"""
import pandas as pd
import numpy as np
import v4_config as cfg


def engineer_features(fact_df: pd.DataFrame) -> pd.DataFrame:
    """
    Build all modeling features from the clean fact table.
    Operates per-cell (SKU × City) to respect group boundaries.
    """
    C = cfg.COL
    df = fact_df.copy()
    print(f"  [Stage 3] Input: {len(df):,} rows")

    df = df.sort_values([C["product_id"], C["city"], C["date"]])
    grp = [C["product_id"], C["city"]]

    # ── Core log transforms (elasticity is log-log) ─────────────────
    df["log_price"] = np.log(df[C["price"]].clip(lower=1))
    df["log_units"] = np.log(df[C["offtake_qty"]].clip(lower=0.1))
    df["log_revenue"] = np.log(df[C["offtake_mrp"]].clip(lower=0.1))

    # ── Discount depth ──────────────────────────────────────────────
    df["discount_pct"] = df["discount_pct_actual"]

    # ── Competitive position ────────────────────────────────────────
    comp = df[C["competitor_price"]].replace(0, np.nan)
    df["price_gap"] = df[C["price"]] - comp.fillna(df[C["price"]])
    df["rpi"] = df[C["price"]] / comp.fillna(df[C["price"]])
    df["rpi"] = df["rpi"].fillna(1.0)

    # ── Reference price (customer perception anchor) ────────────────
    df["reference_price"] = df.groupby(grp)[C["price"]].transform(
        lambda s: s.rolling(cfg.REFERENCE_PRICE_WINDOW, min_periods=1).mean()
    )
    df["price_vs_reference"] = (df[C["price"]] / df["reference_price"]) - 1

    # ── Rolling smoothed features ───────────────────────────────────
    df["osa_rolling_7d"] = df.groupby(grp)[C["availability"]].transform(
        lambda s: s.rolling(cfg.OSA_ROLLING_WINDOW, min_periods=1).mean()
    ) / 100.0  # Normalize to 0-1

    df["ad_rolling_7d"] = df.groupby(grp)[C["ad_sov"]].transform(
        lambda s: s.rolling(cfg.AD_ROLLING_WINDOW, min_periods=1).mean()
    )
    df["log_ad_sov"] = np.log(df["ad_rolling_7d"].clip(lower=0.1))

    # ── Time features ───────────────────────────────────────────────
    df["day_of_week"] = df[C["date"]].dt.dayofweek
    df["is_weekend"] = (df["day_of_week"] >= 5).astype(int)
    df["month"] = df[C["date"]].dt.month

    # Month dummies (drop month 1 as baseline)
    for m in range(2, 13):
        df[f"month_{m}"] = (df["month"] == m).astype(int)

    # ── Promotional flag ────────────────────────────────────────────
    df["is_promotional"] = (df[C["price"]] < 0.85 * comp.fillna(df[C["price"]])).astype(int)

    # ── Drop rows with NaN in critical features ─────────────────────
    feature_cols = get_feature_columns()
    target_col = "log_units"
    before = len(df)
    df = df.dropna(subset=feature_cols + [target_col])
    dropped = before - len(df)
    if dropped:
        print(f"    Dropped {dropped} rows with NaN in features")

    print(f"  [Stage 3] Output: {len(df):,} rows × {len(feature_cols)} features")
    return df.reset_index(drop=True)


def get_feature_columns() -> list:
    """Return the ordered list of feature columns for the elasticity model."""
    base = [
        "log_price",
        "osa_rolling_7d",
        "log_ad_sov",
        "price_gap",
        "rpi",
        "price_vs_reference",
        "is_weekend",
        "is_promotional",
        "discount_pct",
    ]
    month_cols = [f"month_{m}" for m in range(2, 13)]
    return base + month_cols


if __name__ == "__main__":
    from stage1_ingestion.ingest import ingest_all_sales, load_event_calendar
    from stage2_preparation.prepare import prepare_fact_table
    raw = ingest_all_sales()
    cal = load_event_calendar()
    fact = prepare_fact_table(raw, cal)
    feat = engineer_features(fact)
    print(f"\nFeature table: {feat.shape}")
    print(feat[get_feature_columns()].describe().round(3))
