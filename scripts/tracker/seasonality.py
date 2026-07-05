"""
seasonality.py — Festival / seasonality module for the Weekly Discount Tracker.

Purpose
-------
Indian CPG demand (24 Mantra Organic on Blinkit) spikes hard around festivals.
During those weeks a brand *intentionally* runs deeper discounts. If the waste-
scoring engine sees a deep festival discount and does not know it is a planned
festival play, it will happily flag it as "reliably wasteful" and tell the owner
to cut it — exactly the wrong call, and exactly when volume is highest.

This module does two jobs:
  1. Ships a DEFAULT_FESTIVAL_CALENDAR for H2 2026 (editable, approximate windows).
  2. apply_seasonality() tags each cell for the current tracker week and, when the
     week lands inside a festival window, EXCLUDES those cells from waste scoring
     (scored=False) and relaxes the discount budget cap.

Contract
--------
Follows the shared tracker contract. This module only ADDS columns; it never
renames or drops existing ones. Columns added to plan_df:
    is_festival_week (bool), festival_name (str), scored (bool)

Dependencies: pandas, numpy, python stdlib only. No network.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

import numpy as np  # noqa: F401  (kept for contract consistency; used defensively below)
import pandas as pd


# ---------------------------------------------------------------------------
# DEFAULT FESTIVAL CALENDAR — H2 2026 (India)
# ---------------------------------------------------------------------------
# NOTE FOR THE INTEGRATOR / OWNER:
#   * These are APPROXIMATE, EDITABLE week windows, not exact tithi dates.
#   * Windows are widened to roughly the shopping run-up + festival day, because
#     that is when Blinkit demand and planned discounts actually happen — not just
#     the single calendar day.
#   * 'categories' is either the string 'all' (applies to every cell) or a list of
#     category strings that must match plan_df['category'] (case-insensitive).
#   * All dates are ISO 'YYYY-MM-DD' strings (inclusive start, inclusive end).
#   * Edit freely each year; nothing else in this module hard-codes these dates.
DEFAULT_FESTIVAL_CALENDAR = [
    {"name": "Raksha Bandhan", "start": "2026-08-05", "end": "2026-08-09", "categories": "all"},
    {"name": "Independence Day", "start": "2026-08-13", "end": "2026-08-16", "categories": "all"},
    {"name": "Onam",            "start": "2026-08-22", "end": "2026-08-28", "categories": "all"},
    {"name": "Janmashtami",     "start": "2026-09-02", "end": "2026-09-05", "categories": "all"},
    {"name": "Ganesh Chaturthi","start": "2026-09-11", "end": "2026-09-17", "categories": "all"},
    {"name": "Navratri",        "start": "2026-10-11", "end": "2026-10-19", "categories": "all"},
    {"name": "Dussehra",        "start": "2026-10-19", "end": "2026-10-22", "categories": "all"},
    {"name": "Karva Chauth",    "start": "2026-10-28", "end": "2026-10-30", "categories": "all"},
    {"name": "Dhanteras",       "start": "2026-11-05", "end": "2026-11-07", "categories": "all"},
    {"name": "Diwali",          "start": "2026-11-06", "end": "2026-11-10", "categories": "all"},
    {"name": "Bhai Dooj",       "start": "2026-11-10", "end": "2026-11-12", "categories": "all"},
    # Year-end grocery-stocking spike (New Year run-up) — not a religious festival
    # but a real Blinkit demand window worth protecting from waste flags.
    {"name": "Year-End / New Year", "start": "2026-12-28", "end": "2026-12-31", "categories": "all"},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_iso(date_str: str) -> _dt.date:
    """Parse an ISO 'YYYY-MM-DD' string into a datetime.date.

    Accepts full ISO datetimes too (e.g. '2026-11-08T00:00:00') by taking the
    date part. Raises ValueError with a clear message on bad input.
    """
    if isinstance(date_str, _dt.date) and not isinstance(date_str, _dt.datetime):
        return date_str
    if isinstance(date_str, _dt.datetime):
        return date_str.date()
    s = str(date_str).strip()
    # Tolerate a trailing time component.
    s = s.split("T")[0].split(" ")[0]
    return _dt.date.fromisoformat(s)


def _category_matches(row_category: object, fest_categories: object) -> bool:
    """Return True if a festival's category scope covers this row's category.

    'all' (any case) matches everything. Otherwise fest_categories is a list of
    category names; match is case-insensitive and whitespace-trimmed.
    """
    if isinstance(fest_categories, str) and fest_categories.strip().lower() == "all":
        return True
    if fest_categories is None:
        return True
    # Normalise to a set of lowercase strings.
    if isinstance(fest_categories, str):
        allowed = {fest_categories.strip().lower()}
    else:
        allowed = {str(c).strip().lower() for c in fest_categories}
    if "all" in allowed:
        return True
    return str(row_category).strip().lower() in allowed


def _find_active_festival(week_date: _dt.date, calendar: list) -> Optional[dict]:
    """Return the first calendar entry whose [start, end] window contains week_date.

    Windows are inclusive on both ends. If multiple overlap, the earliest-listed
    entry wins (calendar order is treated as priority).
    """
    for fest in calendar:
        try:
            start = _parse_iso(fest["start"])
            end = _parse_iso(fest["end"])
        except (KeyError, ValueError):
            # Skip malformed entries rather than crashing the weekly run.
            continue
        if start <= week_date <= end:
            return fest
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def apply_seasonality(plan_df: pd.DataFrame,
                      config: dict,
                      calendar: Optional[list] = None):
    """Tag cells for festival weeks and relax budget when a festival is active.

    Parameters
    ----------
    plan_df : pd.DataFrame
        One row per SKU x city cell (shared tracker contract). Must contain a
        'category' column if any calendar entry scopes to specific categories;
        with the default 'all' calendar, 'category' is optional.
    config : dict
        Must contain 'week_date' (ISO 'YYYY-MM-DD'). Uses 'festival_uplift_pct'
        (default 0.0 if absent) as the budget relaxation amount and 'week_label'
        for the human note (optional).
    calendar : list of dict, optional
        Festival windows. Defaults to DEFAULT_FESTIVAL_CALENDAR. Each dict:
        {name, start(ISO), end(ISO), categories('all' or list)}.

    Returns
    -------
    (plan_df_out, season_info) : (pd.DataFrame, dict)
        plan_df_out is a COPY with added columns:
            is_festival_week (bool), festival_name (str), scored (bool)
        season_info keys:
            active (bool), festival_name (str), budget_uplift_pct (float),
            window (str), note (str)

    Behaviour
    ---------
    * Non-festival week: every row gets is_festival_week=False, festival_name='',
      scored=True; budget_uplift_pct=0.0.
    * Festival week: rows whose category is covered by the festival get
      is_festival_week=True, festival_name=<name>, scored=False (EXCLUDED from
      waste scoring so a planned festival discount is never mislabeled as waste).
      Rows NOT covered by the festival's category scope stay scored=True.
      The budget cap is relaxed by returning budget_uplift_pct = festival_uplift_pct.
    """
    if calendar is None:
        calendar = DEFAULT_FESTIVAL_CALENDAR

    # Work on a copy so we never mutate the caller's DataFrame.
    out = plan_df.copy()
    n = len(out)

    # --- Resolve the current tracker week date -----------------------------
    if "week_date" not in config or config["week_date"] in (None, ""):
        raise KeyError(
            "config['week_date'] is required (ISO 'YYYY-MM-DD') to apply seasonality."
        )
    week_date = _parse_iso(config["week_date"])
    uplift = float(config.get("festival_uplift_pct", 0.0) or 0.0)
    week_label = str(config.get("week_label", "") or "")

    # --- Default (non-festival) tagging ------------------------------------
    # Start everyone as a normal, scored, non-festival cell.
    out["is_festival_week"] = False
    out["festival_name"] = ""
    out["scored"] = True

    active_fest = _find_active_festival(week_date, calendar)

    if active_fest is None:
        # Ordinary week: nothing to relax, everything is scored.
        note = (
            f"No festival window contains {week_date.isoformat()}"
            f"{' (' + week_label + ')' if week_label else ''}; "
            f"all {n} cells scored normally, budget uplift 0.0."
        )
        season_info = {
            "active": False,
            "festival_name": "",
            "budget_uplift_pct": 0.0,
            "window": "",
            "note": note,
        }
        return out, season_info

    # --- Festival week: tag covered cells and exclude them from scoring -----
    fest_name = str(active_fest.get("name", "Festival"))
    fest_start = _parse_iso(active_fest["start"]).isoformat()
    fest_end = _parse_iso(active_fest["end"]).isoformat()
    window = f"{fest_start} to {fest_end}"
    fest_categories = active_fest.get("categories", "all")

    # Determine per-row category coverage. If there's no 'category' column, a
    # non-'all' scope cannot be evaluated safely, so we treat scope as 'all'.
    has_category_col = "category" in out.columns
    scope_is_all = (
        isinstance(fest_categories, str) and fest_categories.strip().lower() == "all"
    )

    if scope_is_all or not has_category_col:
        covered_mask = pd.Series(True, index=out.index)
        if not has_category_col and not scope_is_all:
            # Defensive note surfaced to integrator via season_info below.
            scope_note = (
                " (plan_df has no 'category' column; category-scoped festival "
                "treated as 'all')"
            )
        else:
            scope_note = ""
    else:
        covered_mask = out["category"].apply(
            lambda c: _category_matches(c, fest_categories)
        )
        scope_note = ""

    # Apply tags only to covered rows.
    out.loc[covered_mask, "is_festival_week"] = True
    out.loc[covered_mask, "festival_name"] = fest_name
    out.loc[covered_mask, "scored"] = False  # EXCLUDE from waste scoring

    n_covered = int(covered_mask.sum())
    n_scored = int((out["scored"]).sum())

    note = (
        f"Festival '{fest_name}' active for week {week_date.isoformat()}"
        f"{' (' + week_label + ')' if week_label else ''} "
        f"[window {window}]. {n_covered}/{n} cells tagged festival and EXCLUDED "
        f"from waste scoring; {n_scored} cells still scored. "
        f"Budget cap relaxed by uplift {uplift}.{scope_note} "
        f"Dates are approximate/editable."
    )

    season_info = {
        "active": True,
        "festival_name": fest_name,
        "budget_uplift_pct": uplift,
        "window": window,
        "note": note,
    }
    return out, season_info


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    def _make_plan_df():
        """Tiny synthetic plan_df matching the shared contract (subset of cols)."""
        rows = [
            # cell_id, product_id, city, category, title, mrp, cur_price, cur_disc, cur_units_wk
            ("c1", "p1", "Mumbai",   "Oil",   "Cold Pressed Oil 1L", 500.0, 425.0, 15.0, 40),
            ("c2", "p2", "Delhi",    "Salt",  "Rock Salt 1kg",       120.0, 108.0, 10.0, 90),
            ("c3", "p3", "Bengaluru","Dal",   "Toor Dal 1kg",        220.0, 176.0, 20.0, 60),
        ]
        df = pd.DataFrame(
            rows,
            columns=[
                "cell_id", "product_id", "city", "category", "title",
                "mrp", "cur_price", "cur_disc", "cur_units_wk",
            ],
        )
        df["cur_net_rev_wk"] = df["cur_units_wk"] * df["cur_price"]
        df["cur_disc_spend_wk"] = df["cur_units_wk"] * df["mrp"] * df["cur_disc"] / 100.0
        return df

    failures = 0

    # --- Case 1: festival week (Diwali) -----------------------------------
    df = _make_plan_df()
    cfg_fest = {
        "budget_pct_cap": 0.11,
        "max_step_ppt": 3.0,
        "festival_uplift_pct": 0.5,
        "week_date": "2026-11-08",  # inside Diwali window
        "week_label": "W-Diwali",
    }
    out_f, info_f = apply_seasonality(df, cfg_fest)
    print("=== Case 1: Diwali week (2026-11-08) ===")
    print(out_f[["cell_id", "category", "is_festival_week", "festival_name", "scored"]].to_string(index=False))
    print("season_info:", info_f)
    assert info_f["active"] is True, "expected active festival"
    assert info_f["festival_name"] == "Diwali", info_f["festival_name"]
    assert info_f["budget_uplift_pct"] == 0.5, info_f["budget_uplift_pct"]
    assert bool(out_f["is_festival_week"].all()), "all rows should be festival (all-category scope)"
    assert not bool(out_f["scored"].any()), "no rows should be scored during festival"
    print("Case 1 PASS\n")

    # --- Case 2: ordinary week (no festival) ------------------------------
    df2 = _make_plan_df()
    cfg_norm = {
        "budget_pct_cap": 0.11,
        "max_step_ppt": 3.0,
        "festival_uplift_pct": 0.5,
        "week_date": "2026-07-05",  # today's date in memory context; no festival
        "week_label": "W1",
    }
    out_n, info_n = apply_seasonality(df2, cfg_norm)
    print("=== Case 2: Ordinary week (2026-07-05) ===")
    print(out_n[["cell_id", "is_festival_week", "festival_name", "scored"]].to_string(index=False))
    print("season_info:", info_n)
    assert info_n["active"] is False, "expected no active festival"
    assert info_n["festival_name"] == "", info_n["festival_name"]
    assert info_n["budget_uplift_pct"] == 0.0, info_n["budget_uplift_pct"]
    assert bool(out_n["scored"].all()), "all rows should be scored in ordinary week"
    assert not bool(out_n["is_festival_week"].any()), "no festival rows expected"
    print("Case 2 PASS\n")

    # --- Case 3: category-scoped festival + no mutation of input ----------
    df3 = _make_plan_df()
    custom_cal = [
        {"name": "Oil Fest", "start": "2026-07-01", "end": "2026-07-31",
         "categories": ["Oil"]},
    ]
    cfg_cat = {
        "festival_uplift_pct": 0.3,
        "week_date": "2026-07-05",
        "week_label": "W1",
    }
    original_cols = list(df3.columns)
    out_c, info_c = apply_seasonality(df3, cfg_cat, calendar=custom_cal)
    print("=== Case 3: Category-scoped 'Oil Fest' ===")
    print(out_c[["cell_id", "category", "is_festival_week", "scored"]].to_string(index=False))
    print("season_info:", info_c)
    # Only the Oil cell (c1) should be tagged / excluded.
    oil_row = out_c.loc[out_c["cell_id"] == "c1"].iloc[0]
    salt_row = out_c.loc[out_c["cell_id"] == "c2"].iloc[0]
    assert bool(oil_row["is_festival_week"]) is True, "Oil cell should be festival"
    assert bool(oil_row["scored"]) is False, "Oil cell should be excluded from scoring"
    assert bool(salt_row["is_festival_week"]) is False, "Salt cell should NOT be festival"
    assert bool(salt_row["scored"]) is True, "Salt cell should still be scored"
    # Input DataFrame must be untouched.
    assert list(df3.columns) == original_cols, "input plan_df columns were mutated!"
    assert "is_festival_week" not in df3.columns, "input plan_df was mutated!"
    print("Case 3 PASS\n")

    print("All seasonality smoke tests passed.")
    sys.exit(0)
