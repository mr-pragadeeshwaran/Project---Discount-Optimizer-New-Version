"""
actuals.py — Feedback-loop GAP 1: freeze pre-action baselines + backfill actuals.

Business purpose
----------------
The tracker logs a PREDICTION for every SKU x city cell each week (a net-revenue
delta and a unit count) versus a *baseline* — where the cell sat BEFORE we touched
its discount. For the scorecard to grade those predictions honestly, two things
must be nailed down:

  1. The baseline must be FROZEN at the moment of action and never move. If the
     baseline drifted with every fresh export, a cell that simply mean-reverted
     would look like a "win" (or a "loss") that our discount move never caused.
     freeze_baselines() captures the pre-action reference (mean of the last 4
     clean weeks) once, per cell.

  2. When next week's real numbers land, we must fill the ACTUAL delta =
     (what actually happened) - (the frozen baseline) — and never overwrite an
     actual we already recorded. backfill_actuals() does exactly this.

"actual net revenue" for a cell-week = units * price (per the panel).

This module only reads the WEEKLY PANEL (from discount_plan.build_panel) and the
shared tracker_history.csv. It uses pandas / numpy / stdlib only.

Public functions
----------------
    freeze_baselines(history_df, panel) -> dict
    backfill_actuals(history_df, fresh_panel, baselines) -> DataFrame
    panel_from_fact_table(fact_path) -> DataFrame        (optional, import-guarded)

Columns this module ADDS to tracker_history (never renames existing ones):
    applied(bool), baseline_net_rev_wk, baseline_units_wk, baseline_osa,
    baseline_sov, actual_osa, actual_sov, strikes(int),
    cell_status(str: active|confounded|reverted|frozen)
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

# How many pre-action weeks define the baseline reference.
BASELINE_WEEKS = 4

# The NEW columns this module introduces onto tracker_history. Existing columns
# (week, week_date, cell_id, confidence, scored, pred_net_rev_delta,
#  actual_net_rev_delta, pred_units, actual_units) are NEVER renamed.
_BASELINE_COLS = [
    "baseline_net_rev_wk",
    "baseline_units_wk",
    "baseline_osa",
    "baseline_sov",
]
_NEW_COLS = _BASELINE_COLS + [
    "applied",
    "actual_osa",
    "actual_sov",
    "strikes",
    "cell_status",
]

# Default value per new column, used when we first materialise it on the frame.
_NEW_COL_DEFAULTS = {
    "baseline_net_rev_wk": np.nan,
    "baseline_units_wk": np.nan,
    "baseline_osa": np.nan,
    "baseline_sov": np.nan,
    "applied": False,
    "actual_osa": np.nan,
    "actual_sov": np.nan,
    # Match killswitch's "not judged" convention: a freshly seeded, unscored row must
    # NOT read as a healthy "active" cell. strikes=NaN / cell_status="" mean "not yet
    # judged"; killswitch stamps strikes=0 / cell_status="active" only after scoring.
    "strikes": np.nan,
    "cell_status": "",
}


# ────────────────────────────────────────────────────────────────────────────
#  helpers
# ────────────────────────────────────────────────────────────────────────────
def _ensure_new_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Add any missing NEW columns with their defaults, leaving existing data
    untouched. Idempotent — safe to call on a frame that already has them."""
    df = df.copy()
    for col in _NEW_COLS:
        if col not in df.columns:
            df[col] = _NEW_COL_DEFAULTS[col]
    return df


def _week_key(series: pd.Series) -> pd.Series:
    """Normalise a week column to a comparable string key.

    The weekly panel's `week` is a Timestamp (period-start). The tracker's
    history `week` is a human label ("W1") while `week_date` holds the real
    calendar date. We normalise both sides to 'YYYY-MM-DD' when the value parses
    as a date, otherwise keep the raw string. This lets a Timestamp panel week
    match a history row keyed on either its week label OR its week_date.
    """
    # We deliberately pass mixed values here — real dates AND bare labels like
    # "W1". Non-dates are meant to fall back to their string; pandas' "could not
    # infer format" warning on those is expected noise, so we silence just it.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        dt = pd.to_datetime(series, errors="coerce")
    key = series.astype(str).str.strip()
    parsed = dt.notna()
    key = key.where(~parsed, dt.dt.strftime("%Y-%m-%d"))
    return key


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """Return df[name] if present, else an all-NaN column of matching length/index.

    Mirrors the tolerant .get(...) fallback in _panel_lookup so a malformed panel
    missing an expected column (units/price/osa/ad_sov/week/cell_id) yields NaN rather
    than a KeyError.
    """
    if name in df.columns:
        return df[name]
    return pd.Series(np.nan, index=df.index)


def _panel_lookup(fresh_panel: pd.DataFrame) -> dict:
    """Build {(cell_id, week_key) -> {units, price, osa, ad_sov}} from a panel.

    A cell-week appears once in a well-formed panel; if duplicates exist we keep
    the last (most recently exported) row. price/units come straight from the
    panel; net revenue is units*price computed at read time.
    """
    p = fresh_panel.copy()
    p["_wk"] = _week_key(p["week"])
    lut: dict = {}
    for _, r in p.iterrows():
        key = (r["cell_id"], r["_wk"])
        lut[key] = {
            "units": float(r["units"]) if pd.notna(r.get("units")) else np.nan,
            "price": float(r["price"]) if pd.notna(r.get("price")) else np.nan,
            "osa": float(r["osa"]) if pd.notna(r.get("osa")) else np.nan,
            "ad_sov": float(r["ad_sov"]) if pd.notna(r.get("ad_sov")) else np.nan,
        }
    return lut


# ────────────────────────────────────────────────────────────────────────────
#  GAP 1a — freeze pre-action baselines
# ────────────────────────────────────────────────────────────────────────────
def freeze_baselines(history_df, panel) -> dict:
    """Freeze the PRE-ACTION baseline for every cell in history_df.

    For each cell_id present in history_df, the baseline is the mean over the
    LAST `BASELINE_WEEKS` (=4) weeks of `panel` for that cell — i.e. the clean
    reference from before any discount action. Robust to fewer than 4 weeks: it
    averages over whatever weeks exist.

    Returns
    -------
    dict: {cell_id: {
        "baseline_net_rev_wk": mean(units*price),
        "baseline_units_wk":   mean(units),
        "baseline_osa":        mean(osa),
        "baseline_sov":        mean(ad_sov),
    }}
    Cells that have no rows in the panel get NaN baselines (so a caller can still
    see the cell was requested but had no history to freeze against).
    """
    if history_df is None or len(history_df) == 0:
        return {}

    cells = pd.Index(history_df["cell_id"].dropna().unique())

    # Guard: an empty / column-only panel -> every cell gets NaN baselines.
    if panel is None or len(panel) == 0:
        return {
            c: {
                "baseline_net_rev_wk": np.nan,
                "baseline_units_wk": np.nan,
                "baseline_osa": np.nan,
                "baseline_sov": np.nan,
            }
            for c in cells
        }

    p = panel.copy()
    # Tolerate a malformed panel missing expected columns: pull each via _col so a
    # missing column becomes all-NaN instead of a KeyError. Normalise the key columns
    # we depend on onto stable private names.
    p["_cell_id"] = _col(p, "cell_id")
    p["_wk_raw"] = _col(p, "week")
    # Only cells we actually track; sort so "last N weeks" is well-defined.
    p = p[p["_cell_id"].isin(cells)].copy()
    # Timestamp weeks sort correctly; string weeks sort lexically. Use a stable
    # sortable key so tail(N) really is the most recent N weeks.
    p["_wk_sort"] = pd.to_datetime(p["_wk_raw"], errors="coerce")
    # Rows whose week doesn't parse fall back to their raw value's rank so they
    # still order deterministically after the parseable ones.
    p = p.sort_values(["_cell_id", "_wk_sort", "_wk_raw"], kind="stable")

    p["_net_rev"] = pd.to_numeric(_col(p, "units"), errors="coerce") * pd.to_numeric(
        _col(p, "price"), errors="coerce"
    )

    out: dict = {}
    for cell_id, g in p.groupby("_cell_id", sort=False):
        last = g.tail(BASELINE_WEEKS)
        units = pd.to_numeric(_col(last, "units"), errors="coerce")
        osa = pd.to_numeric(_col(last, "osa"), errors="coerce")
        sov = pd.to_numeric(_col(last, "ad_sov"), errors="coerce")
        out[cell_id] = {
            "baseline_net_rev_wk": float(np.nanmean(last["_net_rev"]))
            if last["_net_rev"].notna().any()
            else np.nan,
            "baseline_units_wk": float(np.nanmean(units))
            if units.notna().any()
            else np.nan,
            "baseline_osa": float(np.nanmean(osa)) if osa.notna().any() else np.nan,
            "baseline_sov": float(np.nanmean(sov)) if sov.notna().any() else np.nan,
        }

    # Cells requested but absent from the panel -> explicit NaN baselines.
    for c in cells:
        if c not in out:
            out[c] = {
                "baseline_net_rev_wk": np.nan,
                "baseline_units_wk": np.nan,
                "baseline_osa": np.nan,
                "baseline_sov": np.nan,
            }
    return out


# ────────────────────────────────────────────────────────────────────────────
#  GAP 1b — backfill actuals from a fresh export
# ────────────────────────────────────────────────────────────────────────────
def backfill_actuals(history_df, fresh_panel, baselines) -> pd.DataFrame:
    """Fill ACTUALS for any history row that is still open and now measurable.

    For every history row where actual_net_rev_delta is NaN AND the row's
    (cell_id, week) exists in `fresh_panel`:
        actual_units          = fresh units
        actual_net_rev        = fresh units * fresh price
        actual_net_rev_delta  = actual_net_rev - baseline_net_rev_wk[cell]
        actual_osa, actual_sov = fresh osa, ad_sov
    and the frozen baseline_* columns are written onto that row.

    Rules
    -----
    * Never overwrite an already-filled actual (actual_net_rev_delta not NaN).
    * A row with no matching fresh-panel cell-week is left open (unchanged).
    * If a cell has no frozen baseline_net_rev_wk, the delta can't be formed;
      we still record actual_units / actual_osa / actual_sov, leave
      actual_net_rev_delta NaN, and the row stays open for a later fill.
    * Returns a frame with the same (or additional) columns — existing columns
      are never renamed.
    """
    if history_df is None or len(history_df) == 0:
        # Still return a well-formed empty frame carrying the new columns.
        base = history_df.copy() if history_df is not None else pd.DataFrame()
        return _ensure_new_cols(base)

    hist = _ensure_new_cols(history_df)
    hist["_wk"] = _week_key(hist["week"])
    # week_date is the real calendar key when week is a bare label like "W1".
    if "week_date" in hist.columns:
        hist["_wk_date"] = _week_key(hist["week_date"])
    else:
        hist["_wk_date"] = hist["_wk"]

    baselines = baselines or {}
    lut = _panel_lookup(fresh_panel) if fresh_panel is not None and len(fresh_panel) else {}

    # Ensure the actual columns are float so we can assign NaN/values cleanly.
    for col in ("actual_net_rev_delta", "actual_units", "actual_osa", "actual_sov"):
        hist[col] = pd.to_numeric(hist[col], errors="coerce")

    for i in hist.index:
        cell = hist.at[i, "cell_id"]
        b = baselines.get(cell)

        # Skip rows already scored — never clobber a recorded actual, and never
        # re-stamp its baseline. A locked baseline and its actual delta were computed
        # together; overwriting the baseline from a later, different `baselines` dict
        # would make the two disagree. Only OPEN rows get (re-)stamped, below.
        if pd.notna(hist.at[i, "actual_net_rev_delta"]):
            continue

        # Row is still OPEN: stamp the frozen baseline so the register carries the
        # reference even before actuals arrive. Safe because no actual depends on it yet.
        if b is not None:
            for col in _BASELINE_COLS:
                val = b.get(col, np.nan)
                if pd.notna(val):
                    hist.at[i, col] = val

        # Find this row's fresh cell-week. Try the week label key, then the
        # calendar week_date key (a Timestamp panel week matches the latter).
        rec = lut.get((cell, hist.at[i, "_wk"]))
        if rec is None:
            rec = lut.get((cell, hist.at[i, "_wk_date"]))
        if rec is None:
            continue  # not yet measurable — leave the row open

        units = rec["units"]
        price = rec["price"]

        # Record availability/SOV actuals regardless (informative even without a delta).
        if pd.notna(rec["osa"]):
            hist.at[i, "actual_osa"] = rec["osa"]
        if pd.notna(rec["ad_sov"]):
            hist.at[i, "actual_sov"] = rec["ad_sov"]

        if pd.notna(units):
            hist.at[i, "actual_units"] = units

        # Net-rev delta needs both a fresh net-rev and a frozen baseline.
        base_nr = (b or {}).get("baseline_net_rev_wk", np.nan) if b else np.nan
        if pd.notna(units) and pd.notna(price) and pd.notna(base_nr):
            actual_net_rev = units * price
            hist.at[i, "actual_net_rev_delta"] = actual_net_rev - base_nr

    hist = hist.drop(columns=["_wk", "_wk_date"], errors="ignore")
    return hist


# ────────────────────────────────────────────────────────────────────────────
#  optional orchestrator helper — weekly-aggregate a fact_table into a panel
# ────────────────────────────────────────────────────────────────────────────
def panel_from_fact_table(fact_path):
    """Build a fresh weekly panel from a raw fact_table.csv via
    discount_plan.build_panel.

    Import-guarded on purpose: the smoke test builds a synthetic panel directly
    and must NOT depend on statsmodels / the real analysis package being present.
    The orchestrator calls this with a real export path; if the import chain is
    unavailable it raises a clear ImportError rather than failing obscurely.
    """
    import os
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", ".."))
    analysis = os.path.join(root, "scripts", "analysis")
    for pth in (root, analysis):
        if pth not in sys.path:
            sys.path.insert(0, pth)
    try:
        from discount_plan import build_panel  # type: ignore
    except Exception as e:  # pragma: no cover - env-dependent
        raise ImportError(
            "panel_from_fact_table needs scripts/analysis/discount_plan.build_panel "
            f"(and its deps) importable: {e}"
        )
    return build_panel(fact_path)


# ────────────────────────────────────────────────────────────────────────────
#  smoke test
# ────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 40)

    # --- tiny synthetic weekly PANEL (pre-action reference) ------------------
    # Two cells, 5 clean weeks each. Baseline = mean of the LAST 4 weeks.
    #   AAA: units 100,110,120,130,140 (last4 mean=125), price flat 50
    #        -> baseline_net_rev_wk = 125*50 = 6250 ; baseline_units_wk = 125
    #   BBB: only 2 weeks -> robust: averages the 2 it has.
    weeks = pd.to_datetime(
        ["2026-06-01", "2026-06-08", "2026-06-15", "2026-06-22", "2026-06-29"]
    )
    panel_rows = []
    for i, w in enumerate(weeks):
        panel_rows.append(
            dict(cell_id="AAA", week=w, product_id="P1", city="Pune",
                 category="Oil", units=100 + 10 * i, price=50.0, disc=10.0,
                 osa=90.0 + i, ad_sov=5.0, cat_share=0.2, mrp=60.0)
        )
    for i, w in enumerate(weeks[:2]):  # BBB: only 2 weeks of history
        panel_rows.append(
            dict(cell_id="BBB", week=w, product_id="P2", city="Delhi",
                 category="Salt", units=40 + 5 * i, price=25.0, disc=8.0,
                 osa=80.0, ad_sov=3.0, cat_share=0.1, mrp=30.0)
        )
    panel = pd.DataFrame(panel_rows)

    # --- tiny synthetic HISTORY (predictions logged; actuals empty) ----------
    # Existing tracker schema exactly. AAA/BBB open for W1; AAA already scored
    # for a prior week W0 (must NOT be overwritten). CCC has no panel history.
    history = pd.DataFrame(
        [
            dict(week="W0", week_date="2026-06-29", cell_id="AAA", confidence="High",
                 scored=True, pred_net_rev_delta=200.0, actual_net_rev_delta=321.0,
                 pred_units=130.0, actual_units=131.0),  # already filled
            dict(week="W1", week_date="2026-07-06", cell_id="AAA", confidence="High",
                 scored=True, pred_net_rev_delta=150.0, actual_net_rev_delta=np.nan,
                 pred_units=120.0, actual_units=np.nan),  # OPEN -> should fill
            dict(week="W1", week_date="2026-07-06", cell_id="BBB", confidence="Experimental",
                 scored=True, pred_net_rev_delta=-50.0, actual_net_rev_delta=np.nan,
                 pred_units=42.0, actual_units=np.nan),   # OPEN -> should fill
            dict(week="W1", week_date="2026-07-06", cell_id="CCC", confidence="Low",
                 scored=True, pred_net_rev_delta=10.0, actual_net_rev_delta=np.nan,
                 pred_units=5.0, actual_units=np.nan),     # no panel -> stays open
        ]
    )

    # --- 1) freeze baselines -------------------------------------------------
    baselines = freeze_baselines(history, panel)
    print("=== freeze_baselines ===")
    for cell, b in baselines.items():
        print(f"  {cell}: " + ", ".join(f"{k}={v:.3f}" if pd.notna(v) else f"{k}=NaN"
                                          for k, v in b.items()))

    # Checkable expectations for AAA (last-4 units mean = 125, price 50):
    assert abs(baselines["AAA"]["baseline_units_wk"] - 125.0) < 1e-9, baselines["AAA"]
    assert abs(baselines["AAA"]["baseline_net_rev_wk"] - 6250.0) < 1e-9, baselines["AAA"]
    # AAA osa last4 = mean(91,92,93,94) = 92.5
    assert abs(baselines["AAA"]["baseline_osa"] - 92.5) < 1e-9, baselines["AAA"]
    # BBB has only 2 weeks -> units mean(40,45)=42.5 ; net_rev mean(40*25,45*25)=1062.5
    assert abs(baselines["BBB"]["baseline_units_wk"] - 42.5) < 1e-9, baselines["BBB"]
    assert abs(baselines["BBB"]["baseline_net_rev_wk"] - 1062.5) < 1e-9, baselines["BBB"]
    # CCC requested but absent from panel -> NaN baseline
    assert np.isnan(baselines["CCC"]["baseline_net_rev_wk"]), baselines["CCC"]

    # --- 2) backfill actuals from a FRESH export -----------------------------
    # The new week's real numbers. Panel week 2026-07-06 == history W1 week_date.
    fresh = pd.DataFrame(
        [
            dict(cell_id="AAA", week=pd.Timestamp("2026-07-06"), product_id="P1",
                 city="Pune", category="Oil", units=118.0, price=51.0, disc=8.0,
                 osa=95.0, ad_sov=6.0, cat_share=0.22, mrp=60.0),
            dict(cell_id="BBB", week=pd.Timestamp("2026-07-06"), product_id="P2",
                 city="Delhi", category="Salt", units=44.0, price=25.0, disc=8.0,
                 osa=82.0, ad_sov=3.5, cat_share=0.11, mrp=30.0),
            # note: no CCC row -> that history line stays OPEN
        ]
    )

    filled = backfill_actuals(history, fresh, baselines)
    print("\n=== backfill_actuals ===")
    show = ["week", "cell_id", "actual_units", "actual_net_rev_delta",
            "baseline_net_rev_wk", "actual_osa", "actual_sov",
            "applied", "strikes", "cell_status"]
    print(filled[show].to_string(index=False))

    # AAA W1: actual_net_rev = 118*51 = 6018 ; baseline 6250 -> delta = -232
    aaa_w1 = filled[(filled.cell_id == "AAA") & (filled.week == "W1")].iloc[0]
    assert abs(aaa_w1["actual_units"] - 118.0) < 1e-9, aaa_w1
    assert abs(aaa_w1["actual_net_rev_delta"] - (6018.0 - 6250.0)) < 1e-9, aaa_w1
    assert abs(aaa_w1["actual_osa"] - 95.0) < 1e-9, aaa_w1
    assert abs(aaa_w1["actual_sov"] - 6.0) < 1e-9, aaa_w1

    # AAA W0 was already scored -> untouched (still 321.0, 131.0)
    aaa_w0 = filled[(filled.cell_id == "AAA") & (filled.week == "W0")].iloc[0]
    assert abs(aaa_w0["actual_net_rev_delta"] - 321.0) < 1e-9, aaa_w0
    assert abs(aaa_w0["actual_units"] - 131.0) < 1e-9, aaa_w0

    # BBB W1: actual_net_rev = 44*25 = 1100 ; baseline 1062.5 -> delta = +37.5
    bbb_w1 = filled[(filled.cell_id == "BBB") & (filled.week == "W1")].iloc[0]
    assert abs(bbb_w1["actual_net_rev_delta"] - (1100.0 - 1062.5)) < 1e-9, bbb_w1

    # CCC W1: no fresh panel row -> stays OPEN (actual still NaN)
    ccc_w1 = filled[(filled.cell_id == "CCC") & (filled.week == "W1")].iloc[0]
    assert pd.isna(ccc_w1["actual_net_rev_delta"]), ccc_w1
    assert pd.isna(ccc_w1["actual_units"]), ccc_w1

    # new columns exist with the "not judged" defaults that match killswitch's
    # convention (fix #5): a seeded, un-scored-by-the-kill-switch row must read as
    # strikes=NaN / cell_status="" (NOT a healthy "active"). applied stays False.
    for col in _NEW_COLS:
        assert col in filled.columns, col
    assert bool(filled["applied"].iloc[0]) is False
    assert pd.isna(filled["strikes"].iloc[0]), filled["strikes"].iloc[0]
    assert filled["cell_status"].iloc[0] == "", repr(filled["cell_status"].iloc[0])

    # --- 3) idempotency: re-running backfill must NOT change filled actuals ---
    again = backfill_actuals(filled, fresh, baselines)
    a2 = again[(again.cell_id == "AAA") & (again.week == "W1")].iloc[0]
    assert abs(a2["actual_net_rev_delta"] - (6018.0 - 6250.0)) < 1e-9, a2

    # --- 3b) fix #4: a later call with a DIFFERENT baselines dict must NOT re-stamp a
    # baseline onto an already-scored row (its actual delta was locked against the old
    # baseline; overwriting the baseline alone would make the two disagree). ----------
    drifted = {c: dict(b) for c, b in baselines.items()}
    drifted["AAA"]["baseline_net_rev_wk"] = 99999.0  # a wildly different reference
    rerun = backfill_actuals(filled, fresh, drifted)
    aaa_w1_2 = rerun[(rerun.cell_id == "AAA") & (rerun.week == "W1")].iloc[0]
    # W1 was filled against baseline 6250 -> its stamped baseline must stay 6250, and
    # its delta unchanged, even though `drifted` carries 99999.
    assert abs(aaa_w1_2["baseline_net_rev_wk"] - 6250.0) < 1e-9, aaa_w1_2
    assert abs(aaa_w1_2["actual_net_rev_delta"] - (6018.0 - 6250.0)) < 1e-9, aaa_w1_2

    # --- 3c) fix #6: a malformed panel missing expected columns must NOT KeyError; the
    # affected baseline components just come back NaN. --------------------------------
    bad_panel = panel.drop(columns=["osa", "ad_sov"])  # drop OSA/SOV columns
    bad_baselines = freeze_baselines(history, bad_panel)
    assert np.isnan(bad_baselines["AAA"]["baseline_osa"]), bad_baselines["AAA"]
    assert np.isnan(bad_baselines["AAA"]["baseline_sov"]), bad_baselines["AAA"]
    # units/price survive -> net-rev + units baselines still computed.
    assert abs(bad_baselines["AAA"]["baseline_units_wk"] - 125.0) < 1e-9, bad_baselines["AAA"]
    # a panel with NO 'week' column at all also must not explode.
    freeze_baselines(history, panel.drop(columns=["week"]))

    # --- 4) guards: empty history / empty panel don't crash ------------------
    assert freeze_baselines(pd.DataFrame(columns=["cell_id"]), panel) == {}
    empty_hist = pd.DataFrame(
        columns=["week", "week_date", "cell_id", "confidence", "scored",
                 "pred_net_rev_delta", "actual_net_rev_delta", "pred_units", "actual_units"]
    )
    eb = backfill_actuals(empty_hist, fresh, baselines)
    assert len(eb) == 0 and "cell_status" in eb.columns

    print("\nAll smoke-test assertions passed.")
    sys.exit(0)
