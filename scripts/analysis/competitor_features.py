"""
competitor_features.py — GAP 10: mine COMPETITOR signal from the raw RCA files.

Business purpose
----------------
The weekly RCA export is an ALL-BRAND market scan: every brand's price, discount,
availability and share in each (Category, City). Today ingestion keeps only the
24 Mantra Organic rows and throws every competitor row away — so the tracker is
blind to what rivals are doing. Yet "did we cut for no reason while competitors
sat still?" or "is a cell losing share because a rival went 40% off?" are exactly
the questions the feedback loop needs to answer honestly.

This module reads those same raw files, KEEPS the competitor rows (everything that
is NOT own-brand), and rolls them up per (Category, City, ISO-week) into a compact
competitor-pressure table the discount tracker can join on:

    comp_median_price  — typical rival shelf price (robust to one outlier SKU)
    comp_avg_disc      — mean rival discount %
    comp_max_disc      — the DEEPEST rival discount that week (the scary one)
    comp_p75_disc      — 75th-pct rival discount (upper-band pressure, less noisy than max)
    comp_avg_osa       — mean rival availability % (are rivals in stock and taking the demand?)
    n_comp_skus        — how many rival SKUs backed the numbers (thin weeks = low trust)

Design choices that matter to the integrator
---------------------------------------------
* Column matching is TOLERANT: names are matched case-insensitively and by
  "contains", so 'Wt. Discount %', 'wt discount %', 'Discount' all resolve. See
  _CANON below for the substring probes used per canonical field.
* Files are read in CHUNKS (dtype=str, chunksize) because the real RCA CSVs are
  ~200MB. Only the needed columns are kept from each chunk; competitor rows are
  filtered inside the chunk so memory stays flat regardless of file size.
* Own vs competitor is decided by `own_brand_patterns` — a list of LOWERCASE
  substrings (e.g. ['24 mantra','24mantra']). A row is OWN if its lower-cased,
  stripped Brand CONTAINS any pattern; otherwise it is COMPETITOR. This mirrors
  the substring convention already used in v4_config.OWN_BRAND_PATTERNS / stage1.
* ISO-week: the aggregation key is the calendar's ISO year+week rendered as
  'YYYY-Www' (e.g. '2025-W37'), so it lines up with any ISO-week keying elsewhere
  and never splits a week across a year boundary.

Dependencies: pandas / numpy / python stdlib only.
"""

from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd


# ── canonical field  ->  ordered list of case-insensitive substring probes ──
# First probe that a real column CONTAINS wins. Order matters: more specific
# probes come first so 'Selling Price' beats a bare 'Price', and 'Wt. OSA %'
# beats a generic 'OSA'.
_CANON = {
    "brand":    ["brand"],
    "category": ["category"],
    "city":     ["city"],
    "price":    ["selling price", "sell price", "price"],
    "disc":     ["wt. discount", "wt discount", "discount %", "discount"],
    "mrp":      ["mrp"],
    "osa":      ["wt. osa", "wt osa", "osa %", "osa", "availability"],
    "date":     ["date"],
}

# Only these canonical fields are pulled from each chunk (keeps memory light on
# the 200MB files). brand/category/city/date are keys; price/disc/osa are values.
_NEEDED = ["brand", "category", "city", "price", "disc", "osa", "date"]


def _resolve_columns(columns) -> dict:
    """Map each canonical field to a REAL column name via case-insensitive
    'contains' probing. Returns {canonical: actual_col} for every field found.
    A field with no match is simply absent from the returned dict (callers must
    tolerate a missing 'osa'/'mrp'; brand/category/city/date/price are required).
    """
    lowered = {str(c).strip().lower(): c for c in columns}
    resolved: dict[str, str] = {}
    for canon, probes in _CANON.items():
        for probe in probes:
            hit = next((orig for low, orig in lowered.items() if probe in low), None)
            if hit is not None:
                resolved[canon] = hit
                break
    return resolved


def _is_own(brand_series: pd.Series, own_brand_patterns) -> pd.Series:
    """Boolean mask: True where the brand matches ANY own-brand substring.

    Match is case-insensitive, whitespace-stripped, substring ('contains').
    own_brand_patterns is a list of already-lowercase substrings. An empty /
    None pattern list means "nothing is own" (every row is treated competitor),
    which is the safe default for this module — it never silently drops rivals.
    """
    b = brand_series.astype(str).str.strip().str.lower()
    if not own_brand_patterns:
        return pd.Series(False, index=brand_series.index)
    mask = pd.Series(False, index=brand_series.index)
    for pat in own_brand_patterns:
        p = str(pat).strip().lower()
        if p:
            mask = mask | b.str.contains(p, regex=False, na=False)
    return mask


def _iso_week(date_series: pd.Series) -> pd.Series:
    """Render a datetime series as ISO-week label 'YYYY-Www' (e.g. '2025-W37').

    Uses isocalendar() so the week belongs to its ISO year (a late-December date
    can land in W01 of the next year, and vice versa) — no year-boundary splits.
    Unparseable dates become <NA> and are dropped by the caller.
    """
    dt = pd.to_datetime(date_series, errors="coerce")
    iso = dt.dt.isocalendar()  # columns: year, week, day (nullable Int)
    year = iso["year"].astype("string")
    week = iso["week"].astype("Int64").astype("string").str.zfill(2)
    label = year.str.cat(week, sep="-W")
    # rows where the date failed to parse -> propagate NA so caller can drop them
    label = label.where(dt.notna())
    return label


def _prep_competitor_chunk(chunk: pd.DataFrame, own_brand_patterns) -> pd.DataFrame:
    """From one raw chunk: resolve columns, keep COMPETITOR rows only, coerce the
    numeric value columns, and return a tidy frame with canonical column names
    [category, city, iso_week, price, disc, osa]. Returns an empty frame (with the
    canonical columns) when the chunk has no usable competitor rows.
    """
    canon_out = ["category", "city", "iso_week", "price", "disc", "osa"]
    cols = _resolve_columns(chunk.columns)

    # brand/category/city/date are structurally required to aggregate a row.
    for req in ("brand", "category", "city", "date"):
        if req not in cols:
            return pd.DataFrame(columns=canon_out)

    keep = {canon: cols[canon] for canon in _NEEDED if canon in cols}
    sub = chunk[list(keep.values())].copy()
    sub.columns = list(keep.keys())  # rename to canonical

    # keep competitor rows (NOT own) — this is the whole point of the module
    sub = sub[~_is_own(sub["brand"], own_brand_patterns)]
    if sub.empty:
        return pd.DataFrame(columns=canon_out)

    sub["iso_week"] = _iso_week(sub["date"])
    sub["category"] = sub["category"].astype(str).str.strip()
    sub["city"] = sub["city"].astype(str).str.strip()

    # numeric value columns — coerce; missing 'osa'/'price'/'disc' become all-NaN
    for vcol in ("price", "disc", "osa"):
        sub[vcol] = pd.to_numeric(sub.get(vcol), errors="coerce")

    # a row is only usable if it has a week + category + city
    sub = sub.dropna(subset=["iso_week"])
    sub = sub[(sub["category"] != "") & (sub["city"] != "")
              & (sub["category"].str.lower() != "nan")
              & (sub["city"].str.lower() != "nan")]
    return sub[canon_out]


def _aggregate(rows: pd.DataFrame) -> pd.DataFrame:
    """Roll tidy competitor rows up to one row per (category, city, iso_week)."""
    out_cols = ["Category", "City", "iso_week", "comp_median_price", "comp_avg_disc",
                "comp_max_disc", "comp_p75_disc", "comp_avg_osa", "n_comp_skus"]
    if rows.empty:
        return pd.DataFrame(columns=out_cols)

    g = rows.groupby(["category", "city", "iso_week"], sort=True)
    agg = g.agg(
        comp_median_price=("price", "median"),
        comp_avg_disc=("disc", "mean"),
        comp_max_disc=("disc", "max"),
        comp_p75_disc=("disc", lambda s: s.quantile(0.75)),
        comp_avg_osa=("osa", "mean"),
        n_comp_skus=("price", "size"),  # count of competitor rows backing the cell
    ).reset_index()

    agg = agg.rename(columns={"category": "Category", "city": "City"})
    return agg[out_cols]


def build_competitor_features(input_dir, own_brand_patterns, out_csv) -> pd.DataFrame:
    """Mine competitor pricing/discount/availability signal from raw RCA CSVs.

    Parameters
    ----------
    input_dir : str
        Directory of raw RCA CSV exports. Every '*.csv' is read EXCEPT files whose
        name contains 'my sku' / 'sku list' (own-SKU master metadata, not market
        data) or Excel lock files (leading '~').
    own_brand_patterns : list[str]
        Lowercase substrings identifying OWN brand rows (e.g. ['24 mantra','24mantra']).
        A row is COMPETITOR when its Brand contains none of these. Empty list => every
        row treated as competitor (module never silently drops rivals).
    out_csv : str
        Path to write the aggregated competitor-features CSV. Parent dir is created.

    Returns
    -------
    pandas.DataFrame
        One row per (Category, City, iso_week) with columns:
        Category, City, iso_week, comp_median_price, comp_avg_disc, comp_max_disc,
        comp_p75_disc, comp_avg_osa, n_comp_skus.
        Empty (headers only) when input_dir has no usable competitor rows.

    Notes
    -----
    * Reads each file in chunks (dtype=str, chunksize) and keeps only needed
      columns, so memory stays flat on ~200MB files.
    * ISO-week key ('YYYY-Www') lines up cleanly across a year boundary.
    """
    files = []
    for f in sorted(glob.glob(os.path.join(input_dir, "*.csv"))):
        base = os.path.basename(f).lower()
        if base.startswith("~") or "my sku" in base or "sku list" in base:
            continue  # skip own-SKU master metadata + lock files
        files.append(f)

    parts: list[pd.DataFrame] = []
    for fpath in files:
        try:
            reader = pd.read_csv(fpath, dtype=str, chunksize=200000, low_memory=False)
        except Exception:
            # unreadable / empty file — skip it rather than aborting the whole run
            continue
        for chunk in reader:
            prepped = _prep_competitor_chunk(chunk, own_brand_patterns)
            if not prepped.empty:
                parts.append(prepped)

    rows = (pd.concat(parts, ignore_index=True) if parts
            else pd.DataFrame(columns=["category", "city", "iso_week",
                                       "price", "disc", "osa"]))
    result = _aggregate(rows)

    out_dir = os.path.dirname(os.path.abspath(out_csv))
    os.makedirs(out_dir, exist_ok=True)
    result.to_csv(out_csv, index=False)
    return result


if __name__ == "__main__":
    # ------------------------------------------------------------------------------
    # Smoke test: build a tiny synthetic 2-file RCA input in the OS temp dir, run
    # build_competitor_features, and verify the aggregates (a) EXCLUDE own-brand rows
    # and (b) compute correctly. Also exercises the 'my sku' skip and a mixed
    # column-name-casing file. Prints results and exits 0.
    # ------------------------------------------------------------------------------
    import json
    import sys
    import tempfile

    tmp = tempfile.mkdtemp(prefix="compfeat_smoke_")
    own_patterns = ["24 mantra", "24mantra"]

    # ── File 1: canonical RCA column names. One (Category, City) group across two
    #    weeks. Own-brand rows are present and MUST be excluded from the aggregates.
    #    Competitor discounts in Jaggery/Mumbai W37: [10, 20, 30, 40] across 4 rivals.
    #      median price of those 4 = median(100,110,90,120)=105
    #      avg disc = 25 ; max = 40 ; p75 = 32.5 ; avg osa = mean(80,90,70,60)=75
    #    The two own-brand rows (huge disc 99, price 1) would wreck every stat if leaked.
    f1 = pd.DataFrame([
        # --- competitor rows, Jaggery / Mumbai / 2025-09-08 (ISO W37) ---
        {"Brand": "Rival A",  "Category": "Jaggery", "City": "Mumbai",
         "Selling Price": "100", "Wt. Discount %": "10", "MRP": "120", "Wt. OSA %": "80", "DATE": "2025-09-08"},
        {"Brand": "Rival B",  "Category": "Jaggery", "City": "Mumbai",
         "Selling Price": "110", "Wt. Discount %": "20", "MRP": "130", "Wt. OSA %": "90", "DATE": "2025-09-09"},
        {"Brand": "Rival C",  "Category": "Jaggery", "City": "Mumbai",
         "Selling Price": "90",  "Wt. Discount %": "30", "MRP": "130", "Wt. OSA %": "70", "DATE": "2025-09-10"},
        {"Brand": "Rival D",  "Category": "Jaggery", "City": "Mumbai",
         "Selling Price": "120", "Wt. Discount %": "40", "MRP": "150", "Wt. OSA %": "60", "DATE": "2025-09-11"},
        # --- OWN-brand rows in the SAME cell — MUST be excluded (note different casings) ---
        {"Brand": "24 Mantra Organic", "Category": "Jaggery", "City": "Mumbai",
         "Selling Price": "1", "Wt. Discount %": "99", "MRP": "120", "Wt. OSA %": "100", "DATE": "2025-09-08"},
        {"Brand": "24MANTRA",          "Category": "Jaggery", "City": "Mumbai",
         "Selling Price": "1", "Wt. Discount %": "99", "MRP": "120", "Wt. OSA %": "100", "DATE": "2025-09-09"},
        # --- competitor rows, Jaggery / Mumbai / next ISO week (2025-09-15 = W38) ---
        {"Brand": "Rival A",  "Category": "Jaggery", "City": "Mumbai",
         "Selling Price": "105", "Wt. Discount %": "15", "MRP": "120", "Wt. OSA %": "85", "DATE": "2025-09-15"},
        {"Brand": "Rival B",  "Category": "Jaggery", "City": "Mumbai",
         "Selling Price": "95",  "Wt. Discount %": "25", "MRP": "120", "Wt. OSA %": "95", "DATE": "2025-09-16"},
    ])
    f1.to_csv(os.path.join(tmp, "rca_week37_38.csv"), index=False)

    # ── File 2: LOWERCASE / spacing-variant column names + a different city. Proves
    #    tolerant column matching. Competitor Oil/Delhi W37 disc [5, 35] -> avg 20,
    #    max 35, p75 27.5, median price median(200,180)=190, avg osa mean(88,92)=90.
    f2 = pd.DataFrame([
        {"brand": "OilCo",  "category": "Oil", "city": "Delhi",
         "price": "200", "wt discount %": "5",  "mrp": "250", "osa %": "88", "date": "2025-09-08"},
        {"brand": "OilBro", "category": "Oil", "city": "Delhi",
         "price": "180", "wt discount %": "35", "mrp": "250", "osa %": "92", "date": "2025-09-10"},
        # own-brand row here too — excluded
        {"brand": "24 Mantra", "category": "Oil", "city": "Delhi",
         "price": "5", "wt discount %": "80", "mrp": "250", "osa %": "100", "date": "2025-09-08"},
    ])
    f2.to_csv(os.path.join(tmp, "rca_oil.csv"), index=False)

    # ── Decoy file that MUST be skipped by name ('my sku'): if leaked, an own SKU
    #    with an absurd competitor-looking brand would poison the aggregates.
    decoy = pd.DataFrame([
        {"Brand": "SneakyRival", "Category": "Jaggery", "City": "Mumbai",
         "Selling Price": "999", "Wt. Discount %": "0", "MRP": "999", "Wt. OSA %": "1", "DATE": "2025-09-08"},
    ])
    decoy.to_csv(os.path.join(tmp, "MY SKU list.csv"), index=False)

    out_csv = os.path.join(tmp, "competitor_features.csv")
    result = build_competitor_features(tmp, own_patterns, out_csv)

    print("=== competitor_features (aggregated) ===")
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(result.to_string(index=False))
    print()

    # --- Locate the three expected groups ------------------------------------------
    def row(cat, city, wk):
        m = result[(result["Category"] == cat) & (result["City"] == city)
                   & (result["iso_week"] == wk)]
        assert len(m) == 1, f"expected exactly 1 row for {cat}/{city}/{wk}, got {len(m)}"
        return m.iloc[0]

    jag_w37 = row("Jaggery", "Mumbai", "2025-W37")
    jag_w38 = row("Jaggery", "Mumbai", "2025-W38")
    oil_w37 = row("Oil", "Delhi", "2025-W37")

    # --- Own-brand exclusion: 4 rival SKUs in Jaggery/Mumbai/W37, NOT 6 -------------
    assert int(jag_w37["n_comp_skus"]) == 4, jag_w37["n_comp_skus"]
    assert int(jag_w38["n_comp_skus"]) == 2, jag_w38["n_comp_skus"]
    assert int(oil_w37["n_comp_skus"]) == 2, oil_w37["n_comp_skus"]

    # --- Jaggery/Mumbai/W37 numeric checks (own 99% disc / ₹1 price NOT leaked) -----
    assert abs(jag_w37["comp_median_price"] - 105.0) < 1e-9, jag_w37["comp_median_price"]
    assert abs(jag_w37["comp_avg_disc"] - 25.0) < 1e-9, jag_w37["comp_avg_disc"]
    assert abs(jag_w37["comp_max_disc"] - 40.0) < 1e-9, jag_w37["comp_max_disc"]
    assert abs(jag_w37["comp_p75_disc"] - 32.5) < 1e-9, jag_w37["comp_p75_disc"]
    assert abs(jag_w37["comp_avg_osa"] - 75.0) < 1e-9, jag_w37["comp_avg_osa"]

    # --- Oil/Delhi/W37 checks (tolerant lowercase columns resolved correctly) -------
    assert abs(oil_w37["comp_median_price"] - 190.0) < 1e-9, oil_w37["comp_median_price"]
    assert abs(oil_w37["comp_avg_disc"] - 20.0) < 1e-9, oil_w37["comp_avg_disc"]
    assert abs(oil_w37["comp_max_disc"] - 35.0) < 1e-9, oil_w37["comp_max_disc"]
    assert abs(oil_w37["comp_p75_disc"] - 27.5) < 1e-9, oil_w37["comp_p75_disc"]
    assert abs(oil_w37["comp_avg_osa"] - 90.0) < 1e-9, oil_w37["comp_avg_osa"]

    # --- Decoy 'my sku' file was skipped: no ₹999 poison in the median --------------
    assert jag_w37["comp_median_price"] < 200.0, "decoy MY SKU file leaked into aggregates"

    # --- CSV round-trip: written file matches the returned frame --------------------
    reread = pd.read_csv(out_csv)
    assert list(reread.columns) == list(result.columns), (list(reread.columns), list(result.columns))
    assert len(reread) == len(result) == 3, (len(reread), len(result))

    # --- Empty-input path: a dir with no competitor rows -> headers-only frame ------
    empty_dir = tempfile.mkdtemp(prefix="compfeat_empty_")
    pd.DataFrame([
        {"Brand": "24 Mantra Organic", "Category": "Jaggery", "City": "Mumbai",
         "Selling Price": "1", "Wt. Discount %": "99", "MRP": "120", "Wt. OSA %": "100", "DATE": "2025-09-08"},
    ]).to_csv(os.path.join(empty_dir, "own_only.csv"), index=False)
    empty_res = build_competitor_features(empty_dir, own_patterns,
                                          os.path.join(empty_dir, "out.csv"))
    assert len(empty_res) == 0, "own-only input should yield zero competitor rows"
    assert list(empty_res.columns)[:3] == ["Category", "City", "iso_week"], empty_res.columns

    print("Summary:")
    print(json.dumps({
        "input_dir": tmp,
        "out_csv": out_csv,
        "n_groups": int(len(result)),
        "jaggery_mumbai_W37_n_comp_skus": int(jag_w37["n_comp_skus"]),
        "own_brand_rows_excluded": True,
        "my_sku_file_skipped": True,
        "empty_input_handled": True,
    }, indent=2))

    print("\nAll smoke-test assertions passed.")
    sys.exit(0)
