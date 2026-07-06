"""
pricing_panel.py — Module 1 of the Blinkit PricingAI engine.

Adapts PepsiCo's "PricingAI" data-prep stage to a SINGLE brand (24 Mantra
Organic) on Blinkit. Turns the daily fact_table into the weekly SKU x city
panel that every downstream module (elasticity, optimizer) consumes.

PUBLIC API
----------
build_pricing_panel(fact_table_path) -> panel_df
    Daily fact_table CSV  ->  one row per (product_id, city, week).
freeze_baselines(panel_df) -> baseline_df
    Weekly panel  ->  one row per (product_id, city) with the "current state"
    (recent volume, recent price, mrp, current discount) the optimizer starts
    from.

Feature engineering is faithful to the PricingAI paper:
  - regular_price = max weekly selling price in a +/-8-week window per cell (DOC p.11).
  - is_promo      = selling_price < 0.95 * regular_price   (>5% below)      (DOC p.11).
  - pack_grams    = robust parse of GRAMMAGE ('500g'->500, '1kg'->1000,
                    '2x500g'->1000, '1ltr'->1000, '100g'->100).
  - base_product  = TITLE with the size/pack token stripped, so different pack
                    sizes of the same product share one base_product.
  - weekly grain  = ISO-week Monday; units summed, price/disc volume-weighted,
                    mrp median, osa mean.
  - recency_w     = exponential decay, half-life ~8 weeks (recent weeks weigh more).
  - volume_w      = proportional to the cell's total units (bigger SKUs weigh more).

Allowed libs only: numpy, pandas, scipy, scikit-learn, statsmodels.
(This module needs only numpy + pandas.)

Money is INR throughout.
"""

from __future__ import annotations

import re
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Source fact_table column names (regular-day rows only; is_regular_day == 1).
# These are the EXACT columns produced by the v4 pipeline's fact_table.csv.
# ---------------------------------------------------------------------------
FT = {
    "product_id": "PRODUCT_ID",
    "city": "GC_CITY",
    "category": "category",
    "title": "TITLE",
    "grammage": "GRAMMAGE",
    "units": "OFFTAKE_QTY",
    "price": "selling_price",
    "mrp": "stable_mrp",
    "disc": "discount_pct_actual",
    "osa": "WT_AVAILABILITY_PCT",
    "date": "DATE",
    "regular_flag": "is_regular_day",
}

RECENCY_HALFLIFE_WEEKS = 8.0      # recency_w half-life (DOC: recent weeks weigh more)
REGULAR_PRICE_WINDOW_WEEKS = 8    # +/- window for regular_price (DOC p.11)
PROMO_THRESHOLD = 0.95            # is_promo if price < 0.95 * regular_price (DOC p.11)


# ===========================================================================
# GRAMMAGE / pack-size parsing
# ===========================================================================
# Multiplier for unit tokens -> grams. Volume units (ml/l/ltr) are treated as
# grams 1:1 (density ~1 for oils/liquids at this granularity) so a single
# pack_grams axis compares all SKUs.
_UNIT_TO_GRAMS = {
    "kg": 1000.0, "kgs": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
    "g": 1.0, "gm": 1.0, "gms": 1.0, "gram": 1.0, "grams": 1.0,
    "l": 1000.0, "ltr": 1000.0, "ltrs": 1000.0, "litre": 1000.0,
    "litres": 1000.0, "liter": 1000.0, "liters": 1000.0,
    "ml": 1.0, "mls": 1.0,
}

# One "N unit" chunk, optionally prefixed by "M x" (e.g. "2 x 500 g").
# Groups: (count multiplier)? (number) (unit)
_PACK_RE = re.compile(
    r"(?:(\d+(?:\.\d+)?)\s*[x*]\s*)?"        # optional "2x" / "2 * "
    r"(\d+(?:\.\d+)?)\s*"                      # the number, e.g. 500 or 1.5
    r"(kgs?|kilograms?|gms?|grams?|g|ltrs?|litres?|liters?|l|mls?|ml)\b",
    re.IGNORECASE,
)


def parse_pack_grams(grammage, title=None) -> float:
    """Robustly parse a pack size to grams.

    Handles '500g', '1kg', '2x500g', '1ltr', '100g', '500 g', '1 kg', '1.5 L'.
    Falls back to the TITLE if GRAMMAGE is missing/unparseable. Returns np.nan
    if nothing parseable is found.
    """
    for source in (grammage, title):
        if source is None:
            continue
        s = str(source).strip().lower()
        if not s or s in ("nan", "none"):
            continue
        m = _PACK_RE.search(s)
        if m:
            mult = float(m.group(1)) if m.group(1) else 1.0
            qty = float(m.group(2))
            unit = m.group(3).lower()
            grams = mult * qty * _UNIT_TO_GRAMS.get(unit, np.nan)
            if np.isfinite(grams) and grams > 0:
                return float(grams)
    return np.nan


def strip_size_token(title) -> str:
    """Return the TITLE with any trailing/inline size token removed.

    'Tur/Arhar Dal 500g'  -> 'Tur/Arhar Dal'
    '24 Mantra ... 500 g'  -> '24 Mantra ...'
    '24 Mantra Moong Dal (Dhuli)' -> unchanged (no size token present)
    So different pack sizes of the same product collapse to one base_product.
    """
    if title is None:
        return ""
    s = str(title).strip()
    if not s or s.lower() in ("nan", "none"):
        return ""
    # Remove every size chunk anywhere in the string.
    cleaned = _PACK_RE.sub(" ", s)
    # Tidy leftover punctuation/whitespace created by the removal
    # (e.g. "Oil,  , Cold" -> "Oil, Cold"; trailing commas/dashes dropped).
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s*,\s*,\s*", ", ", cleaned)
    cleaned = cleaned.strip(" ,-–—")
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned if cleaned else s


# ===========================================================================
# Weekly aggregation helpers
# ===========================================================================
def _iso_week_monday(dates: pd.Series) -> pd.Series:
    """Map each date to the Monday of its ISO week (as a normalized Timestamp)."""
    d = pd.to_datetime(dates)
    # dayofweek: Monday==0 ... Sunday==6. Subtract to land on Monday.
    return (d - pd.to_timedelta(d.dt.dayofweek, unit="D")).dt.normalize()


def _vwmean(values: pd.Series, weights: pd.Series) -> float:
    """Volume-weighted mean; falls back to simple mean if all weights <= 0."""
    v = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    w = pd.to_numeric(weights, errors="coerce").to_numpy(dtype=float)
    mask = np.isfinite(v) & np.isfinite(w)
    if not mask.any():
        return np.nan
    v, w = v[mask], w[mask]
    wsum = w.sum()
    if wsum <= 0:
        return float(np.nanmean(v))
    return float(np.dot(v, w) / wsum)


def _rolling_window_max(series: pd.Series, half_window: int) -> pd.Series:
    """Centered rolling max over +/- half_window rows (position-based).

    Weeks are ordered; a centered window of (2*half_window + 1) rows implements
    the "+/-8-week window" for regular_price. Position-based (not calendar) so
    gaps in the weekly series don't silently widen the window.
    """
    n = len(series)
    arr = series.to_numpy(dtype=float)
    out = np.empty(n, dtype=float)
    for i in range(n):
        lo = max(0, i - half_window)
        hi = min(n, i + half_window + 1)
        out[i] = np.nanmax(arr[lo:hi])
    return pd.Series(out, index=series.index)


# ===========================================================================
# PUBLIC: build_pricing_panel
# ===========================================================================
def build_pricing_panel(fact_table_path) -> pd.DataFrame:
    """Build the weekly SKU x city panel from a daily fact_table CSV (or DataFrame).

    Returns panel_df with exactly these columns:
      product_id, city, category, base_product, pack_grams, title, week (str),
      month (int), units, price, mrp, disc, regular_price, is_promo, osa,
      recency_w, volume_w
    """
    if isinstance(fact_table_path, pd.DataFrame):
        raw = fact_table_path.copy()
    else:
        raw = pd.read_csv(fact_table_path)

    # --- Keep regular days only (guard: column may be absent in tiny tests) ---
    if FT["regular_flag"] in raw.columns:
        raw = raw[raw[FT["regular_flag"]] == 1].copy()

    # --- Required columns present? ---
    needed = [FT[k] for k in
              ("product_id", "city", "title", "grammage", "units",
               "price", "mrp", "disc", "osa", "date")]
    missing = [c for c in needed if c not in raw.columns]
    if missing:
        raise KeyError(f"fact_table missing required columns: {missing}")

    # --- Types ---
    raw[FT["date"]] = pd.to_datetime(raw[FT["date"]], errors="coerce")
    raw = raw.dropna(subset=[FT["date"]])
    for c in ("units", "price", "mrp", "disc", "osa"):
        raw[FT[c]] = pd.to_numeric(raw[FT[c]], errors="coerce")
    # Units must be non-negative; drop rows with no usable price/units.
    raw[FT["units"]] = raw[FT["units"]].clip(lower=0)
    raw = raw.dropna(subset=[FT["price"], FT["units"]])

    # category is optional in the source; default gracefully.
    if FT["category"] not in raw.columns:
        raw[FT["category"]] = "unknown"

    # --- Row-level derived attributes (constant within a cell, but cheap here) ---
    raw["_week"] = _iso_week_monday(raw[FT["date"]])
    raw["_pack_grams"] = [
        parse_pack_grams(g, t)
        for g, t in zip(raw[FT["grammage"]], raw[FT["title"]])
    ]
    raw["_base_product"] = raw[FT["title"]].map(strip_size_token)

    # --- Weekly aggregation per (product_id, city, week) ---
    grp_keys = [FT["product_id"], FT["city"], "_week"]
    rows = []
    for (pid, city, week), g in raw.groupby(grp_keys, sort=True):
        w = g[FT["units"]]  # volume weights for VW means
        rows.append({
            "product_id": pid,
            "city": city,
            "category": g[FT["category"]].iloc[0],
            "base_product": g["_base_product"].iloc[0],
            "pack_grams": g["_pack_grams"].iloc[0],
            "title": g[FT["title"]].iloc[0],
            "week": week,
            "units": float(g[FT["units"]].sum()),
            "price": _vwmean(g[FT["price"]], w),
            "mrp": float(np.nanmedian(pd.to_numeric(g[FT["mrp"]], errors="coerce"))),
            "disc": _vwmean(g[FT["disc"]], w),
            "osa": float(np.nanmean(pd.to_numeric(g[FT["osa"]], errors="coerce"))),
        })
    panel = pd.DataFrame(rows)
    if panel.empty:
        # Return an empty frame with the right schema rather than crashing.
        cols = ["product_id", "city", "category", "base_product", "pack_grams",
                "title", "week", "month", "units", "price", "mrp", "disc",
                "regular_price", "is_promo", "osa", "recency_w", "volume_w"]
        return pd.DataFrame(columns=cols)

    # --- month (int) from the week's Monday ---
    panel["month"] = panel["week"].dt.month.astype(int)

    # --- regular_price + is_promo, per cell, on the WEEKLY series (DOC p.11) ---
    panel = panel.sort_values(["product_id", "city", "week"]).reset_index(drop=True)
    reg_parts = []
    for _, cell in panel.groupby(["product_id", "city"], sort=False):
        rp = _rolling_window_max(cell["price"], REGULAR_PRICE_WINDOW_WEEKS)
        reg_parts.append(rp)
    panel["regular_price"] = pd.concat(reg_parts).sort_index()
    panel["is_promo"] = panel["price"] < (PROMO_THRESHOLD * panel["regular_price"])

    # --- volume_w: proportional to the cell's total units (bigger SKUs weigh more) ---
    cell_units = panel.groupby(["product_id", "city"])["units"].transform("sum")
    max_cell = cell_units.max()
    panel["volume_w"] = (cell_units / max_cell) if max_cell > 0 else 1.0

    # --- recency_w: exp decay, half-life ~8 weeks, keyed off most-recent week ---
    latest_week = panel["week"].max()
    weeks_ago = (latest_week - panel["week"]).dt.days / 7.0
    panel["recency_w"] = np.power(0.5, weeks_ago / RECENCY_HALFLIFE_WEEKS)

    # --- week as ISO date string (shared-schema contract: week is a date str) ---
    panel["week"] = panel["week"].dt.strftime("%Y-%m-%d")

    # --- Final column order (exact shared schema) ---
    cols = ["product_id", "city", "category", "base_product", "pack_grams",
            "title", "week", "month", "units", "price", "mrp", "disc",
            "regular_price", "is_promo", "osa", "recency_w", "volume_w"]
    return panel[cols].reset_index(drop=True)


# ===========================================================================
# PUBLIC: freeze_baselines
# ===========================================================================
def freeze_baselines(panel_df: pd.DataFrame, last_n_weeks: int = 4) -> pd.DataFrame:
    """Freeze the current state per (product_id, city) from the weekly panel.

    Uses the last `last_n_weeks` weeks of each cell:
      q0_units_wk = mean weekly units
      p0_price    = volume-weighted mean selling price
      disc0       = volume-weighted mean discount
      mrp         = median mrp
    Returns baseline_df:
      product_id, city, category, base_product, pack_grams,
      q0_units_wk, p0_price, mrp, disc0
    """
    cols = ["product_id", "city", "category", "base_product", "pack_grams",
            "q0_units_wk", "p0_price", "mrp", "disc0"]
    if panel_df is None or panel_df.empty:
        return pd.DataFrame(columns=cols)

    df = panel_df.copy()
    # Sort by real week date even though panel stores it as a string.
    df["_week_dt"] = pd.to_datetime(df["week"])
    df = df.sort_values(["product_id", "city", "_week_dt"])

    rows = []
    for (pid, city), g in df.groupby(["product_id", "city"], sort=True):
        recent = g.tail(last_n_weeks)
        w = recent["units"]
        rows.append({
            "product_id": pid,
            "city": city,
            "category": recent["category"].iloc[-1],
            "base_product": recent["base_product"].iloc[-1],
            "pack_grams": recent["pack_grams"].iloc[-1],
            "q0_units_wk": float(recent["units"].mean()),
            "p0_price": _vwmean(recent["price"], w),
            "mrp": float(np.nanmedian(pd.to_numeric(recent["mrp"], errors="coerce"))),
            "disc0": _vwmean(recent["disc"], w),
        })
    return pd.DataFrame(rows, columns=cols).reset_index(drop=True)


# ===========================================================================
# Smoke test
# ===========================================================================
if __name__ == "__main__":
    import sys

    # --- Assertions on the parsers (fast, deterministic) ---
    assert parse_pack_grams("500g") == 500.0
    assert parse_pack_grams("1kg") == 1000.0
    assert parse_pack_grams("2x500g") == 1000.0
    assert parse_pack_grams("1ltr") == 1000.0
    assert parse_pack_grams("100g") == 100.0
    assert parse_pack_grams("500 g") == 500.0
    assert parse_pack_grams("1 kg") == 1000.0
    assert parse_pack_grams("nan", "Tur Dal 250 g") == 250.0   # TITLE fallback
    assert np.isnan(parse_pack_grams("", None))
    assert strip_size_token("Tur/Arhar Dal 500g") == "Tur/Arhar Dal"
    assert strip_size_token("24 Mantra Organic Jaggery Powder 500 g") \
        == "24 Mantra Organic Jaggery Powder"
    # Title without a size token must survive unchanged.
    assert strip_size_token("24 Mantra Organic Moong Dal (Dhuli)") \
        == "24 Mantra Organic Moong Dal (Dhuli)"
    print("[OK] parser assertions passed")

    # --- Tiny synthetic fact_table: 2 SKUs (500g/1kg of same base) x 2 cities,
    #     ~14 weeks of daily rows, with a promo dip in the middle. ---
    rng = np.random.default_rng(7)
    start = pd.Timestamp("2025-01-06")  # a Monday
    recs = []
    skus = [
        # product_id, title, grammage, mrp, base_price
        (3583, "24 Mantra Organic Jaggery Powder 500 g", "500g", 90.0, 86.0),
        (3584, "24 Mantra Organic Jaggery Powder 1 kg",  "1kg", 170.0, 158.0),
    ]
    cities = ["Ahmedabad", "Mumbai"]
    for pid, title, gram, mrp, base_price in skus:
        for city in cities:
            for day in range(98):  # 14 weeks x 7 days
                date = start + pd.Timedelta(days=day)
                wk = day // 7
                promo = 6 <= wk <= 8          # a 3-week promo window
                price = round(base_price * (0.80 if promo else 1.0), 2)
                disc = round(100 * (1 - price / mrp), 2)
                base_units = 10 if gram == "500g" else 5
                city_mult = 1.0 if city == "Ahmedabad" else 0.6
                units = max(0, base_units * city_mult *
                            (1.6 if promo else 1.0) + rng.normal(0, 1))
                recs.append({
                    "PRODUCT_ID": pid, "GC_CITY": city, "category": "Jaggery",
                    "TITLE": title, "GRAMMAGE": gram,
                    "OFFTAKE_QTY": round(units, 1),
                    "selling_price": price, "stable_mrp": mrp,
                    "discount_pct_actual": disc,
                    "WT_AVAILABILITY_PCT": round(90 + rng.normal(0, 3), 1),
                    "DATE": date.strftime("%Y-%m-%d"),
                    "is_regular_day": 1,
                })
    fact = pd.DataFrame(recs)
    # Add a couple of is_regular_day==0 rows that MUST be filtered out.
    fact = pd.concat([fact, fact.head(3).assign(is_regular_day=0,
                                                OFFTAKE_QTY=999)],
                     ignore_index=True)

    panel = build_pricing_panel(fact)
    base = freeze_baselines(panel)

    print("\n=== panel_df ===")
    print("shape:", panel.shape)
    print("columns:", list(panel.columns))
    with pd.option_context("display.width", 200, "display.max_columns", 30):
        print(panel.head(6).to_string(index=False))
    print("\npromo weeks flagged:", int(panel["is_promo"].sum()),
          "of", len(panel), "cell-weeks")
    print("pack_grams unique:", sorted(panel["pack_grams"].unique()))
    print("base_product unique:", sorted(panel["base_product"].unique()))

    print("\n=== baseline_df ===")
    print("shape:", base.shape)
    print("columns:", list(base.columns))
    with pd.option_context("display.width", 200, "display.max_columns", 30):
        print(base.to_string(index=False))

    # --- Structural checks ---
    exp_panel = ["product_id", "city", "category", "base_product", "pack_grams",
                 "title", "week", "month", "units", "price", "mrp", "disc",
                 "regular_price", "is_promo", "osa", "recency_w", "volume_w"]
    exp_base = ["product_id", "city", "category", "base_product", "pack_grams",
                "q0_units_wk", "p0_price", "mrp", "disc0"]
    assert list(panel.columns) == exp_panel, "panel schema mismatch"
    assert list(base.columns) == exp_base, "baseline schema mismatch"
    assert panel["is_promo"].sum() > 0, "expected some promo weeks"
    assert (panel["regular_price"] >= panel["price"] - 1e-6).all(), \
        "regular_price must be >= price"
    # Two pack sizes of the same base_product must collapse to one base_product.
    assert panel["base_product"].nunique() == 1, "base_product should collapse packs"
    assert set(panel["pack_grams"].unique()) == {500.0, 1000.0}
    # recency_w: most recent week weight == 1.0
    assert abs(panel["recency_w"].max() - 1.0) < 1e-9
    # baseline: one row per cell (2 skus x 2 cities = 4)
    assert len(base) == 4, f"expected 4 baseline rows, got {len(base)}"
    assert base["q0_units_wk"].notna().all() and base["p0_price"].notna().all()

    print("\n[OK] all smoke-test assertions passed — exit 0")
    sys.exit(0)
