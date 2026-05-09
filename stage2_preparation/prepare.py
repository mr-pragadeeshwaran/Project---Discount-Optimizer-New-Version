"""
Stage 2 — Data Preparation.

Cleans and unifies raw data into a single fact table at SKU × City × Day grain.
Flags event days, festivals, OOS days, and marks regular training days.
"""
import pandas as pd
import numpy as np
import v4_config as cfg


def prepare_fact_table(raw_df: pd.DataFrame, calendar_df: pd.DataFrame) -> pd.DataFrame:
    """
    Clean, validate, and flag the raw combined data.

    Returns a fact table at SKU × City × Day grain with flags for
    downstream filtering (event/OOS/regular days).
    """
    C = cfg.COL
    df = raw_df.copy()
    print(f"  [Stage 2] Input: {len(df):,} rows")

    # ── 1. Fill missing values ──────────────────────────────────────
    # Forward-fill availability and price within each cell
    df = df.sort_values([C["product_id"], C["city"], C["date"]])

    for col in [C["availability"], C["price"], C["competitor_price"]]:
        if col in df.columns:
            df[col] = df.groupby([C["product_id"], C["city"]])[col].transform(
                lambda s: s.ffill().bfill()
            )

    # Fill remaining NaN in numeric columns with 0
    fill_zero = [C["offtake_mrp"], C["offtake_qty"], C["ad_sov"], C["discount_pct"]]
    for col in fill_zero:
        if col in df.columns:
            df[col] = df[col].fillna(0)

    # ── 2. Validate ranges ──────────────────────────────────────────
    # Price sanity: should be between 30% and 110% of MRP
    if C["mrp"] in df.columns and C["price"] in df.columns:
        mrp = df[C["mrp"]]
        price = df[C["price"]]
        invalid_price = (price < mrp * 0.3) | (price > mrp * 1.1)
        n_invalid = invalid_price.sum()
        if n_invalid > 0:
            print(f"    ⚠ {n_invalid} rows with price outside [30%-110%] of MRP — clamped")
            df.loc[invalid_price & (price < mrp * 0.3), C["price"]] = mrp * 0.3
            df.loc[invalid_price & (price > mrp * 1.1), C["price"]] = mrp * 1.1

    # Availability: clamp to [0, 100]
    if C["availability"] in df.columns:
        df[C["availability"]] = df[C["availability"]].clip(0, 100)

    # Units: floor at 0
    if C["offtake_qty"] in df.columns:
        df[C["offtake_qty"]] = df[C["offtake_qty"]].clip(lower=0)

    # ── 3. Compute discount_pct from price/MRP if not present ──────
    if C["mrp"] in df.columns and C["price"] in df.columns:
        df["discount_pct_actual"] = ((df[C["mrp"]] - df[C["price"]]) / df[C["mrp"]] * 100).clip(0, 100)
    else:
        df["discount_pct_actual"] = df.get(C["discount_pct"], 0)

    # ── 4. Flag days ────────────────────────────────────────────────
    # OOS flag
    df["is_oos_day"] = (df[C["availability"]] < cfg.OSA_OOS_THRESHOLD).astype(int)

    # Event/festival flags from calendar
    df["is_event_day"] = 0
    df["is_festival"] = 0
    df["event_name"] = ""

    if not calendar_df.empty:
        cal = calendar_df.copy()
        cal["date"] = pd.to_datetime(cal["date"])

        # Festival days
        festival_dates = set(cal[cal["event_type"] == "festival"]["date"].dt.date)
        platform_dates = set(cal[cal["event_type"] == "platform_sale"]["date"].dt.date)
        all_event_dates = festival_dates | platform_dates

        df_dates = df[C["date"]].dt.date
        df["is_festival"] = df_dates.isin(festival_dates).astype(int)
        df["is_event_day"] = df_dates.isin(all_event_dates).astype(int)

        # Event name lookup
        event_lookup = cal.drop_duplicates(subset=["date"]).set_index("date")["event_name"]
        df["event_name"] = df[C["date"]].map(event_lookup).fillna("")

    # Regular day: NOT event AND NOT OOS (used for model training)
    df["is_regular_day"] = ((df["is_event_day"] == 0) & (df["is_oos_day"] == 0)).astype(int)

    # ── 5. Create cell identifier ───────────────────────────────────
    df["cell_id"] = df[C["product_id"]].astype(str) + "_" + df[C["city"]].astype(str)

    # ── 6. Summary stats ────────────────────────────────────────────
    n_cells = df["cell_id"].nunique()
    n_regular = df["is_regular_day"].sum()
    n_event = df["is_event_day"].sum()
    n_oos = df["is_oos_day"].sum()
    print(f"  [Stage 2] Fact table: {len(df):,} rows | {n_cells} cells")
    print(f"    Regular days: {n_regular:,} | Event days: {n_event:,} | OOS days: {n_oos:,}")
    print(f"    Training-eligible (regular): {n_regular/len(df)*100:.1f}%")

    return df.reset_index(drop=True)


if __name__ == "__main__":
    from stage1_ingestion.ingest import ingest_all_sales, load_event_calendar
    raw = ingest_all_sales()
    cal = load_event_calendar()
    fact = prepare_fact_table(raw, cal)
    print(f"\nFact table shape: {fact.shape}")
    print(f"Columns: {list(fact.columns)}")
