"""
Stage 1 — Data Ingestion.

Reads all Excel files from the sales data directory, combines them,
and produces raw DataFrames ready for Stage 2.
"""
import os
import glob
import pandas as pd
import numpy as np
import v4_config as cfg


def ingest_all_sales() -> pd.DataFrame:
    """Load all Excel files from SALES_DATA_DIR into one combined DataFrame."""
    pattern = os.path.join(cfg.SALES_DATA_DIR, "*.xlsx")
    files = [f for f in glob.glob(pattern) if not os.path.basename(f).startswith("~")]

    if not files:
        raise FileNotFoundError(f"No .xlsx files found in {cfg.SALES_DATA_DIR}")

    print(f"  [Stage 1] Found {len(files)} data files")
    frames = []
    for fpath in sorted(files):
        fname = os.path.basename(fpath)
        df = pd.read_excel(fpath)
        print(f"    {fname}: {len(df):,} rows, {df[cfg.COL['product_id']].nunique()} SKUs")
        frames.append(df)

    combined = pd.concat(frames, ignore_index=True)

    # Basic type coercion
    C = cfg.COL
    combined[C["date"]] = pd.to_datetime(combined[C["date"]])

    numeric_cols = [C["offtake_mrp"], C["offtake_qty"], C["price"], C["mrp"],
                    C["availability"], C["discount_pct"], C["ad_sov"],
                    C["competitor_price"]]
    for col in numeric_cols:
        if col in combined.columns:
            combined[col] = pd.to_numeric(combined[col], errors="coerce")

    # Detect category from title
    combined["category"] = combined[C["title"]].apply(_detect_category)

    # Deduplicate: keep last row per (SKU, City, Date)
    before = len(combined)
    combined = combined.drop_duplicates(
        subset=[C["product_id"], C["city"], C["date"]], keep="last"
    )
    if len(combined) < before:
        print(f"    Deduped: {before:,} → {len(combined):,} rows")

    combined = combined.sort_values([C["product_id"], C["city"], C["date"]]).reset_index(drop=True)

    n_skus = combined[C["product_id"]].nunique()
    n_cities = combined[C["city"]].nunique()
    n_cells = combined.groupby([C["product_id"], C["city"]]).ngroups
    n_cats = combined["category"].nunique()
    print(f"  [Stage 1] Combined: {len(combined):,} rows | "
          f"{n_skus} SKUs × {n_cities} cities = {n_cells} cells | "
          f"{n_cats} categories")
    return combined


def _detect_category(title: str) -> str:
    """Detect product category from title using keyword matching."""
    if not isinstance(title, str):
        return "Unknown"
    title_lower = title.lower()
    for cat, keywords in cfg.CATEGORY_KEYWORDS.items():
        if all(kw in title_lower for kw in keywords):
            return cat
    return "Other"


def load_master_costs() -> pd.DataFrame:
    """Load or generate default cost data per SKU."""
    master_path = os.path.join(cfg.MASTER_DATA_DIR, "sku_costs.csv")
    if os.path.exists(master_path):
        print(f"  [Stage 1] Loading master costs from {master_path}")
        return pd.read_csv(master_path)

    # Generate defaults from data
    print(f"  [Stage 1] No master cost file found — using configurable defaults")
    print(f"    COGS: {cfg.DEFAULT_COGS_PCT*100:.0f}% of MRP | "
          f"Commission: {cfg.DEFAULT_COMMISSION_PCT*100:.0f}% | "
          f"Fulfillment: ₹{cfg.DEFAULT_FULFILLMENT_FEE}")
    return pd.DataFrame()  # Empty = use defaults in Stage 6


def load_event_calendar() -> pd.DataFrame:
    """Build event/festival calendar from config."""
    rows = []
    # Festival dates
    for date_str, name in cfg.FESTIVAL_DATES.items():
        dt = pd.Timestamp(date_str)
        for offset in range(-cfg.FESTIVAL_WINDOW_DAYS, cfg.FESTIVAL_WINDOW_DAYS + 1):
            rows.append({
                "date": dt + pd.Timedelta(days=offset),
                "event_name": name,
                "event_type": "festival",
            })

    # Platform events (date ranges)
    for (start, end), name in cfg.PLATFORM_EVENT_WINDOWS.items():
        for dt in pd.date_range(start, end):
            rows.append({
                "date": dt,
                "event_name": name,
                "event_type": "platform_sale",
            })

    if rows:
        cal = pd.DataFrame(rows).drop_duplicates(subset=["date", "event_name"])
        print(f"  [Stage 1] Event calendar: {len(cal)} event-days loaded")
        return cal
    return pd.DataFrame(columns=["date", "event_name", "event_type"])


if __name__ == "__main__":
    df = ingest_all_sales()
    print(df.head())
    cal = load_event_calendar()
    print(f"\nCalendar: {len(cal)} rows")
