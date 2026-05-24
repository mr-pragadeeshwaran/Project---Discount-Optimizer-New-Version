"""
Stage 1 — Data Ingestion.

Reads all Excel files from the sales data directory, combines them,
and produces raw DataFrames ready for Stage 2.

IMPORTANT: Each (PRODUCT_ID, GRAMMAGE, City, Date) is treated as a unique
cell. Grammage is normalised into a canonical string ('500g', '1kg', etc.)
so that mixed raw values (500, '500 g', '500g') are unified before any
grouping or deduplication.
"""
import os
import re
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

    # ── Normalise GRAMMAGE into a canonical string ─────────────────
    # Raw data has mixed types: 500 (int), '500 g' (str), '500g' (str),
    # '1 kg' (str), etc. Normalise all to clean strings like '500g', '1kg'.
    if C["grammage"] in combined.columns:
        combined[C["grammage"]] = combined[C["grammage"]].apply(_normalise_grammage)
        unique_gram = combined[C["grammage"]].unique().tolist()
        print(f"  [Stage 1] Grammages found (normalised): {unique_gram}")
    else:
        print(f"  [Stage 1] ⚠ No GRAMMAGE column — grammage not used in cell identity")

    # Detect category from title
    combined["category"] = combined[C["title"]].apply(_detect_category)

    # Deduplicate: keep last row per (SKU, Grammage, City, Date)
    # Grammage is included so 500g and 1kg variants are NEVER merged.
    dedup_keys = [C["product_id"], C["city"], C["date"]]
    if C["grammage"] in combined.columns:
        dedup_keys = [C["product_id"], C["grammage"], C["city"], C["date"]]
    before = len(combined)
    combined = combined.drop_duplicates(subset=dedup_keys, keep="last")
    if len(combined) < before:
        print(f"    Deduped: {before:,} → {len(combined):,} rows")

    # ── Filter to own-brand SKUs only ──────────────────────────────
    if C["brand"] in combined.columns and hasattr(cfg, "OWN_BRAND_PATTERNS"):
        brand_col = combined[C["brand"]].astype(str).str.strip().str.lower()
        own_patterns = [p.strip().lower() for p in cfg.OWN_BRAND_PATTERNS]
        mask = brand_col.isin(own_patterns)
        n_before = len(combined)
        n_own = mask.sum()
        n_comp = n_before - n_own
        combined = combined[mask].copy()
        brands_removed = combined[C["brand"]].nunique() if n_comp == 0 else n_comp
        print(f"  [Stage 1] Brand filter: keeping own brand ({cfg.BRAND_NAME})")
        print(f"    Own brand rows: {n_own:,} | Competitor rows removed: {n_comp:,}")
    else:
        print(f"  [Stage 1] ⚠ No BRAND column or OWN_BRAND_PATTERNS — processing all SKUs")

    sort_keys = [C["product_id"], C["city"], C["date"]]
    if C["grammage"] in combined.columns:
        sort_keys = [C["product_id"], C["grammage"], C["city"], C["date"]]
    combined = combined.sort_values(sort_keys).reset_index(drop=True)

    n_skus = combined[C["product_id"]].nunique()
    n_cities = combined[C["city"]].nunique()
    grp_keys = [C["product_id"], C["city"]]
    if C["grammage"] in combined.columns:
        grp_keys = [C["product_id"], C["grammage"], C["city"]]
    n_cells = combined.groupby(grp_keys).ngroups
    n_cats = combined["category"].nunique()
    print(f"  [Stage 1] Combined: {len(combined):,} rows | "
          f"{n_skus} PRODUCT_IDs × {n_cities} cities = {n_cells} cells | "
          f"{n_cats} categories")
    return combined


def _normalise_grammage(raw) -> str:
    """
    Normalise raw grammage values into a clean canonical string.

    Examples:
        500        → '500g'
        '500 g'    → '500g'
        '500g'     → '500g'
        '1 kg'     → '1kg'
        '1kg'      → '1kg'
        1000       → '1000g'
        None / NaN → 'unknown'
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return "unknown"
    s = str(raw).strip().lower()
    # Remove spaces between number and unit
    s = re.sub(r'\s+', '', s)
    # '1kg' or '1.5kg' → keep as-is
    if re.match(r'^[\d.]+kg$', s):
        return s
    # '500g' → keep as-is
    if re.match(r'^[\d.]+g$', s):
        return s
    # Bare number like 500 or 1000 → treat as grams
    if re.match(r'^[\d.]+$', s):
        num = float(s)
        if num >= 1000:
            # e.g. 1000 → 1kg
            kg = num / 1000
            return f"{int(kg) if kg == int(kg) else kg}kg"
        return f"{int(num) if num == int(num) else num}g"
    # Fallback: return cleaned string
    return s


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
