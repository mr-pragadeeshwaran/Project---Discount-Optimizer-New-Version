"""
scorecard.py — HONEST running accuracy scorecard for the Weekly Discount Tracker.

Business purpose
----------------
Every week the tracker predicts a net-revenue delta (and units) for each SKU x city
cell. This module grades those PAST predictions against ACTUALS once they land, so the
brand owner can see — with receipts, not reassurance — whether the tool is actually
right, week over week. It answers: "When we said a move would help, did it? By how much?
And is the money we claim we saved real?"

Honesty rules baked in
----------------------
* NO look-ahead: we only ever score weeks that already have actuals. The caller passes
  a history_df that, by contract, contains only realized weeks — we never peek forward.
* We report a hit_rate (did we get the direction right?), an R^2 (do predicted deltas
  track actual deltas?), a units MAPE (how far off on volume?), a revenue bias (are we
  systematically optimistic or pessimistic in rupees?), and the cumulative REALIZED
  saving (sum of ACTUAL deltas, not predicted — the number that actually hit the P&L).
* First-run / empty history is handled gracefully: zeros / None and a clear note.

Public function
---------------
    score_history(history_df) -> dict

Contract for history_df (one row per (cell_id, week) for weeks WITH actuals):
    cell_id, week(str), confidence,
    pred_net_rev_delta(float), actual_net_rev_delta(float),
    pred_units(float), actual_units(float)

Dependencies: pandas / numpy / python stdlib only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# Columns the module relies on. Kept explicit so an integrator sees exactly what's read.
_REQUIRED_COLS = [
    "cell_id",
    "week",
    "confidence",
    "pred_net_rev_delta",
    "actual_net_rev_delta",
    "pred_units",
    "actual_units",
]


def _empty_result(note: str) -> dict:
    """Return the canonical 'nothing to score yet' scorecard.

    Used for the first run (no actuals) and for the degenerate case where rows exist
    but none are usable. Every numeric field is a hard zero except the accuracy stats,
    which are None (undefined, not 'zero accuracy' — an important honesty distinction).
    """
    return {
        "n_weeks_scored": 0,
        "n_obs": 0,
        "hit_rate": None,
        "pred_vs_actual_r2": None,
        "units_mape": None,
        "revenue_bias_inr": 0.0,
        "cumulative_realized_saving_inr": 0.0,
        "by_confidence": {},
        "weekly": [],
        "note": note,
    }


def _sign(series: pd.Series) -> pd.Series:
    """np.sign but as a plain int Series (-1, 0, +1). Zero stays zero (no move called)."""
    return np.sign(series).astype(int)


def _hit_rate(pred: pd.Series, actual: pd.Series) -> float | None:
    """Share of obs where predicted direction == actual direction.

    Honesty choice: a prediction of exactly 0 (we called 'no meaningful move') is only a
    'hit' when the actual is also exactly 0. Any real move against a 0 call is a miss.
    Returns None when there is nothing to score.
    """
    if len(pred) == 0:
        return None
    hits = (_sign(pred) == _sign(actual)).sum()
    return float(hits) / float(len(pred))


def _r2(pred: pd.Series, actual: pd.Series) -> float | None:
    """R^2 of actual on predicted: 1 - SS_res / SS_tot.

    Measures how much of the variation in ACTUAL net-rev delta is explained by our
    PREDICTED net-rev delta. Can go negative if predictions are worse than just guessing
    the mean actual — we DO NOT clamp that; a negative R^2 is a real, honest signal that
    the model is not tracking reality, and hiding it would be dishonest.

    Returns None when it is mathematically undefined:
      * fewer than 2 observations, or
      * zero variance in the actuals (SS_tot == 0) — nothing to explain.
    """
    n = len(pred)
    if n < 2:
        return None
    actual = actual.astype(float)
    pred = pred.astype(float)
    ss_tot = float(((actual - actual.mean()) ** 2).sum())
    if ss_tot == 0.0:
        # All actuals identical: R^2 is undefined (0/0). Report None, not a fake 1.0.
        return None
    ss_res = float(((actual - pred) ** 2).sum())
    return 1.0 - ss_res / ss_tot


def _units_mape(pred_units: pd.Series, actual_units: pd.Series) -> float | None:
    """Mean absolute percentage error of predicted vs actual units, in PERCENT.

    MAPE = mean( |actual - pred| / |actual| ) * 100, over rows where actual != 0.
    Rows with actual_units == 0 are dropped (division blows up and % error is undefined
    against a zero base). Returns None if no row has a non-zero actual.
    """
    actual_units = actual_units.astype(float)
    pred_units = pred_units.astype(float)
    mask = actual_units != 0
    if not mask.any():
        return None
    ape = (pred_units[mask] - actual_units[mask]).abs() / actual_units[mask].abs()
    return float(ape.mean() * 100.0)


def score_history(history_df) -> dict:
    """Compute a running, honest accuracy scorecard from realized (past) weeks.

    Parameters
    ----------
    history_df : pandas.DataFrame
        One row per (cell_id, week) for PAST weeks that already have actuals. Required
        columns are listed in _REQUIRED_COLS. Extra columns are ignored. May be empty
        (first run) — handled gracefully.

    Returns
    -------
    dict with keys:
        n_weeks_scored : int   distinct weeks that had at least one usable obs
        n_obs          : int   total scored (cell x week) observations
        hit_rate       : float|None   share where sign(pred delta) == sign(actual delta)
        pred_vs_actual_r2 : float|None  R^2 of actual net-rev delta on predicted
        units_mape     : float|None   mean abs % error of pred_units vs actual_units
        revenue_bias_inr : float      mean(actual - pred) net-rev delta; +ve = we under-
                                      promised (actual beat forecast), -ve = we over-promised
        cumulative_realized_saving_inr : float  sum of ACTUAL net-rev deltas (real P&L)
        by_confidence  : dict  conf -> {"hit_rate": float|None, "n": int}
        weekly         : list  per-week dicts, sorted by week ascending:
                         {week, hit_rate, realized_saving_inr, pred_vs_actual_r2}
        note           : str   present only on the empty / degenerate path
    """
    # --- First-run guard: no DataFrame, or an empty one -> nothing to score. -----------
    if history_df is None or len(history_df) == 0:
        return _empty_result("no actuals yet")

    df = history_df.copy()

    # --- Validate contract: fail loud on a missing column rather than silently wrong. ---
    missing = [c for c in _REQUIRED_COLS if c not in df.columns]
    if missing:
        raise KeyError(
            f"history_df is missing required column(s): {missing}. "
            f"Expected columns: {_REQUIRED_COLS}"
        )

    # --- Coerce the numeric columns; drop rows we cannot score. ------------------------
    # A row is unusable if the two net-rev-delta fields (needed for hit_rate, R^2, bias,
    # realized saving) are not both present. Units can be independently missing — MAPE
    # simply skips those rows — so we do NOT drop a row just for missing units.
    for col in ("pred_net_rev_delta", "actual_net_rev_delta", "pred_units", "actual_units"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    scored = df.dropna(subset=["pred_net_rev_delta", "actual_net_rev_delta"]).copy()
    if len(scored) == 0:
        return _empty_result("no scorable rows (net-rev deltas all missing)")

    # confidence: normalize to a clean string bucket so grouping is stable.
    scored["confidence"] = scored["confidence"].fillna("Unknown").astype(str)

    # week: treat as an opaque sortable string label (contract says week is a str).
    scored["week"] = scored["week"].astype(str)

    pred = scored["pred_net_rev_delta"]
    actual = scored["actual_net_rev_delta"]

    # --- Headline stats ----------------------------------------------------------------
    n_obs = int(len(scored))
    n_weeks_scored = int(scored["week"].nunique())
    hit_rate = _hit_rate(pred, actual)
    r2 = _r2(pred, actual)
    units_mape = _units_mape(scored["pred_units"], scored["actual_units"])

    # revenue_bias: mean(actual - pred). Positive => actuals beat forecast (we were
    # conservative); negative => we over-promised in rupees. This is the honest
    # 'are our promises inflated?' gauge.
    revenue_bias_inr = float((actual - pred).mean())

    # cumulative realized saving: sum of ACTUAL deltas — the money that truly moved,
    # NOT the money we predicted. This is the number the owner can defend to the P&L.
    cumulative_realized_saving_inr = float(actual.sum())

    # --- By-confidence breakdown -------------------------------------------------------
    by_confidence: dict[str, dict] = {}
    for conf, grp in scored.groupby("confidence", sort=True):
        by_confidence[str(conf)] = {
            "hit_rate": _hit_rate(grp["pred_net_rev_delta"], grp["actual_net_rev_delta"]),
            "n": int(len(grp)),
        }

    # --- Per-week timeline -------------------------------------------------------------
    weekly: list[dict] = []
    for wk, grp in scored.groupby("week", sort=True):
        weekly.append(
            {
                "week": str(wk),
                "hit_rate": _hit_rate(grp["pred_net_rev_delta"], grp["actual_net_rev_delta"]),
                "realized_saving_inr": float(grp["actual_net_rev_delta"].sum()),
                "pred_vs_actual_r2": _r2(grp["pred_net_rev_delta"], grp["actual_net_rev_delta"]),
            }
        )
    weekly.sort(key=lambda d: d["week"])

    return {
        "n_weeks_scored": n_weeks_scored,
        "n_obs": n_obs,
        "hit_rate": hit_rate,
        "pred_vs_actual_r2": r2,
        "units_mape": units_mape,
        "revenue_bias_inr": revenue_bias_inr,
        "cumulative_realized_saving_inr": cumulative_realized_saving_inr,
        "by_confidence": by_confidence,
        "weekly": weekly,
    }


if __name__ == "__main__":
    # ------------------------------------------------------------------------------
    # Smoke test: build a tiny synthetic history_df, score it, and also exercise the
    # first-run (empty) path. Prints results and exits 0.
    # ------------------------------------------------------------------------------
    import json
    import sys

    # --- Case 1: first run, empty history -> graceful zeros/None + note. ---------------
    empty = pd.DataFrame(
        columns=[
            "cell_id",
            "week",
            "confidence",
            "pred_net_rev_delta",
            "actual_net_rev_delta",
            "pred_units",
            "actual_units",
        ]
    )
    print("=== Case 1: empty history (first run) ===")
    print(json.dumps(score_history(empty), indent=2, default=str))
    print()

    # --- Case 2: two weeks of realized data across confidence tiers. -------------------
    # Hand-picked so the answers are checkable:
    #   W1: 3 cells, all directions correct (3/3 hit); actual deltas sum = 900
    #   W2: 3 cells, one direction wrong (2/3 hit);     actual deltas sum = 250
    history = pd.DataFrame(
        [
            # week 1
            {"cell_id": "A", "week": "W1", "confidence": "High",
             "pred_net_rev_delta": 500.0, "actual_net_rev_delta": 600.0,
             "pred_units": 100.0, "actual_units": 110.0},
            {"cell_id": "B", "week": "W1", "confidence": "High",
             "pred_net_rev_delta": -200.0, "actual_net_rev_delta": -150.0,
             "pred_units": 40.0, "actual_units": 38.0},
            {"cell_id": "C", "week": "W1", "confidence": "Experimental",
             "pred_net_rev_delta": 300.0, "actual_net_rev_delta": 450.0,
             "pred_units": 60.0, "actual_units": 66.0},
            # week 2
            {"cell_id": "A", "week": "W2", "confidence": "High",
             "pred_net_rev_delta": 400.0, "actual_net_rev_delta": 500.0,
             "pred_units": 90.0, "actual_units": 95.0},
            {"cell_id": "B", "week": "W2", "confidence": "Low",
             "pred_net_rev_delta": 250.0, "actual_net_rev_delta": -100.0,  # wrong direction
             "pred_units": 50.0, "actual_units": 20.0},
            {"cell_id": "C", "week": "W2", "confidence": "Experimental",
             "pred_net_rev_delta": -150.0, "actual_net_rev_delta": -150.0,
             "pred_units": 30.0, "actual_units": 30.0},
        ]
    )
    result = score_history(history)
    print("=== Case 2: two realized weeks ===")
    print(json.dumps(result, indent=2, default=str))

    # --- Lightweight self-checks (assertions) so a regression fails the smoke test. ----
    assert result["n_weeks_scored"] == 2, result["n_weeks_scored"]
    assert result["n_obs"] == 6, result["n_obs"]
    # 5 of 6 directions correct -> hit_rate 5/6
    assert abs(result["hit_rate"] - 5.0 / 6.0) < 1e-9, result["hit_rate"]
    # cumulative realized saving = sum of all actual deltas
    assert abs(result["cumulative_realized_saving_inr"] - (600 - 150 + 450 + 500 - 100 - 150)) < 1e-9
    # W1 hit rate is a perfect 3/3
    w1 = next(w for w in result["weekly"] if w["week"] == "W1")
    assert abs(w1["hit_rate"] - 1.0) < 1e-9, w1
    # W2 hit rate is 2/3
    w2 = next(w for w in result["weekly"] if w["week"] == "W2")
    assert abs(w2["hit_rate"] - 2.0 / 3.0) < 1e-9, w2
    # by_confidence has the three tiers we used
    assert set(result["by_confidence"].keys()) == {"High", "Experimental", "Low"}, result["by_confidence"]

    print("\nAll smoke-test assertions passed.")
    sys.exit(0)
