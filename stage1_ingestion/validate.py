"""
validate.py — fail-loud input checks (the done-for-you operator's safety net).

When you load a NEW client's aligned data, this catches the common ways it can
be wrong BEFORE the pipeline runs on it — so you get a clear, actionable error
instead of a cryptic crash deep in Stage 4 or (worse) silently wrong numbers.

Two gates:
  validate_columns(df)  — HARD fail if a required column is missing.
  validate_quality(df)  — HARD fail on fatal data problems (no rows, no dates,
                          <2 cells); WARN on soft issues (negatives, out-of-range
                          discounts, mostly-missing fields) so you can decide.
"""
import pandas as pd
import v4_config as cfg

# Columns the pipeline cannot run without (mapped via cfg.COL)
HARD_REQUIRED = ["product_id", "city", "date", "offtake_qty", "mrp",
                 "discount_pct", "title"]
# Used when present, defaulted/optional when absent
SOFT_OPTIONAL = ["grammage", "availability", "ad_sov", "competitor_price",
                 "price", "offtake_mrp", "brand"]


def validate_columns(df: pd.DataFrame) -> None:
    """Raise with an actionable message if any required column is absent."""
    C = cfg.COL
    missing = [C[k] for k in HARD_REQUIRED if C[k] not in df.columns]
    if missing:
        present = list(map(str, df.columns))
        raise ValueError(
            "Input data is missing required column(s): " + ", ".join(missing) +
            ".\n  Align the client's export to the expected names (see v4_config.COL). "
            "Columns found in the file: " + ", ".join(present[:40]) +
            (" …" if len(present) > 40 else ""))
    soft_missing = [C[k] for k in SOFT_OPTIONAL if C[k] not in df.columns]
    if soft_missing:
        print(f"  [validate] note: optional columns absent (defaults will be used): {soft_missing}")


def validate_quality(df: pd.DataFrame) -> list:
    """
    Check basic data quality. HARD-fails on fatal problems; returns a list of
    soft warnings (also printed). Run AFTER the own-brand filter.
    """
    C = cfg.COL
    n = len(df)
    if n == 0:
        raise ValueError("No rows to model after ingestion/brand filter — "
                         "check BRAND_NAME and that the file isn't empty.")
    if pd.to_datetime(df[C["date"]], errors="coerce").isna().all():
        raise ValueError(f"Column '{C['date']}' has no parseable dates.")

    grp = [C["product_id"], C["city"]]
    if C["grammage"] in df.columns:
        grp = [C["product_id"], C["grammage"], C["city"]]
    n_cells = df.groupby(grp).ngroups
    if n_cells < 2:
        raise ValueError(f"Only {n_cells} cell (SKU×city) after filtering — need ≥2 to model. "
                         "Check the brand filter and that the data spans multiple cities/SKUs.")

    issues = []
    q = pd.to_numeric(df[C["offtake_qty"]], errors="coerce")
    if int((q < 0).sum()):
        issues.append(f"{int((q < 0).sum())} rows have negative units")
    if q.isna().mean() > 0.5:
        issues.append("over half of OFFTAKE_QTY is non-numeric/NaN")
    mrp = pd.to_numeric(df[C["mrp"]], errors="coerce")
    if (mrp.isna() | (mrp <= 0)).mean() > 0.5:
        issues.append("MRP is mostly missing or ≤ 0")
    d = pd.to_numeric(df[C["discount_pct"]], errors="coerce")
    oor = int(((d < 0) | (d > 100)).sum())
    if oor:
        issues.append(f"{oor} rows have discount outside 0–100%")
    if C["availability"] in df.columns:
        a = pd.to_numeric(df[C["availability"]], errors="coerce")
        if int(((a < 0) | (a > 100)).sum()):
            issues.append("some availability values are outside 0–100")

    if issues:
        print("  [validate] ⚠ data-quality notes (pipeline will still run):")
        for it in issues:
            print(f"     - {it}")
    else:
        print(f"  [validate] ✓ {n:,} rows · {n_cells} cells · schema & basic quality OK")
    return issues
