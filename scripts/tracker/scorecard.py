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

Public functions
----------------
    score_history(history_df) -> dict
    acceptance_history(history_df, exec_weeks=None) -> dict

Contract for history_df (one row per (cell_id, week) for weeks WITH actuals):
    cell_id, week(str), confidence,
    pred_net_rev_delta(float), actual_net_rev_delta(float),
    pred_units(float), actual_units(float)

acceptance_history (paper §4.3 — the operational trust metric) additionally
needs week_action + applied (both written by weekly_tracker.append_history /
apply_execution_log) and, crucially, `exec_weeks`: the set of week labels the
KAM's execution_log.csv actually covers. A week with recommendations but no
returned log is reported as rate=None ("log not returned") — NEVER 0%, because
"not confirmed" is not "rejected". Benchmark: the PepsiCo paper's deployed
system ran at ~85% acceptance (ADOPTION_FLOOR below).

Dependencies: pandas / numpy / python stdlib only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ── Acceptance-rate benchmarks (paper §4.3) ─────────────────────────────────
# The PepsiCo paper reports its deployed system running at ~85% acceptance of
# recommendations. OK >= FLOOR, WATCH in [WATCH, FLOOR), LOW below WATCH.
# LOW is an ops/trust alarm (the KAM isn't executing), NOT a model alarm.
ADOPTION_FLOOR = 0.85
ADOPTION_WATCH = 0.60

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


def _rate_status(rate: float) -> str:
    """Bucket an acceptance rate against the paper benchmark (~85% deployed)."""
    if rate >= ADOPTION_FLOOR:
        return "OK"
    if rate >= ADOPTION_WATCH:
        return "WATCH"
    return "LOW"


def acceptance_history(history_df, exec_weeks=None) -> dict:
    """Weekly + cumulative ACCEPTANCE rate: recommendations actually executed.

    Acceptance = applied==True / recommended, where "recommended" means rows whose
    week_action is 'cut' or 'reinvest' (holds are not recommendations to execute —
    same acted-cell definition as killswitch.py). This is the paper's §4.3
    operational trust metric: value is only realized when a rec is implemented.

    Parameters
    ----------
    history_df : pandas.DataFrame | None
        FULL tracker history (DISCOUNT_PLAN/tracker_history.csv) — needs columns
        week, week_action, applied; pred_net_rev_delta used if present for the
        value-weighted rate. May be None/empty (first run).
    exec_weeks : set[str] | None
        Week labels covered by the KAM's returned execution_log.csv. A week with
        recommendations but NOT in this set gets acceptance_rate=None with status
        'log not returned' — the honest n/a, never a fake 0%. None/empty set =
        no log returned yet at all.

    Returns
    -------
    dict:
        weekly : list of {week, n_recommended, n_applied, acceptance_rate(float|None),
                          value_weighted_rate(float|None), status}
        cum_acceptance_rate, cum_value_weighted_rate : float|None
            over CONFIRMED weeks only (weeks the log covered)
        n_recommended_total, n_confirmed_weeks : int
        benchmark : float  (ADOPTION_FLOOR, the paper's ~85%)
        note : str  present when there is nothing to report / no log yet
    """
    empty = {"weekly": [], "cum_acceptance_rate": None, "cum_value_weighted_rate": None,
             "n_recommended_total": 0, "n_confirmed_weeks": 0, "benchmark": ADOPTION_FLOOR}
    if history_df is None or len(history_df) == 0:
        return {**empty, "note": "no tracker history yet"}
    if "week_action" not in history_df.columns:
        return {**empty, "note": "history has no week_action column (pre-upgrade format)"}

    df = history_df.copy()
    df["week"] = df["week"].astype(str)
    rec = df[df["week_action"].astype(str).isin(["cut", "reinvest"])].copy()
    if len(rec) == 0:
        return {**empty, "note": "no cut/reinvest recommendations logged yet"}

    # applied: normalize to bool exactly like weekly_tracker.apply_execution_log's flag.
    applied = rec["applied"] if "applied" in rec.columns else pd.Series(False, index=rec.index)
    rec["applied_b"] = applied.astype(str).str.strip().str.upper().isin(["TRUE", "Y", "YES", "1"])
    val_src = rec["pred_net_rev_delta"] if "pred_net_rev_delta" in rec.columns \
        else pd.Series(0.0, index=rec.index)
    val = pd.to_numeric(val_src, errors="coerce").abs().fillna(0.0)
    rec["abs_val"] = val
    exec_weeks = {str(w) for w in (exec_weeks or set())}

    weekly, cum_rec, cum_app, cum_val, cum_val_app = [], 0, 0, 0.0, 0.0
    for wk, grp in rec.groupby("week", sort=True):
        n_rec = int(len(grp))
        if wk not in exec_weeks:
            # Recommendations exist but the KAM never returned the log for this week:
            # rate is UNKNOWN (n/a), not zero. Conflating the two would smear an ops
            # gap (no log) into a fake adoption collapse.
            weekly.append({"week": wk, "n_recommended": n_rec, "n_applied": None,
                           "acceptance_rate": None, "value_weighted_rate": None,
                           "status": "log not returned"})
            continue
        n_app = int(grp["applied_b"].sum())
        v_all = float(grp["abs_val"].sum())
        v_app = float(grp.loc[grp["applied_b"], "abs_val"].sum())
        rate = n_app / n_rec
        vrate = (v_app / v_all) if v_all > 0 else None
        weekly.append({"week": wk, "n_recommended": n_rec, "n_applied": n_app,
                       "acceptance_rate": rate, "value_weighted_rate": vrate,
                       "status": _rate_status(rate)})
        cum_rec += n_rec; cum_app += n_app; cum_val += v_all; cum_val_app += v_app

    out = {
        "weekly": weekly,
        "cum_acceptance_rate": (cum_app / cum_rec) if cum_rec else None,
        "cum_value_weighted_rate": (cum_val_app / cum_val) if cum_val > 0 else None,
        "n_recommended_total": int(len(rec)),
        # DENOMINATOR HONESTY: cum_acceptance_rate is computed over CONFIRMED weeks
        # only, so any "X% of N recs executed" readout must use THIS N — quoting the
        # all-weeks total next to a confirmed-weeks rate would overstate execution.
        "n_recommended_confirmed": int(cum_rec),
        "n_confirmed_weeks": int(sum(1 for w in weekly if w["acceptance_rate"] is not None)),
        "benchmark": ADOPTION_FLOOR,
    }
    if out["n_confirmed_weeks"] == 0:
        out["note"] = "no execution log returned yet — acceptance is n/a, not 0"
    return out


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

    # --- Case 3: acceptance rate (val_17) — 10 acted cells in W1 (8 applied), 5 in W2
    # with NO execution log returned for W2, plus hold rows that must be ignored. -----
    acc_hist = pd.DataFrame(
        [{"week": "W1", "cell_id": f"c{i}", "week_action": "cut",
          "applied": (i < 8), "pred_net_rev_delta": 100.0} for i in range(10)]
        + [{"week": "W1", "cell_id": "h1", "week_action": "hold",
            "applied": False, "pred_net_rev_delta": 0.0}]
        + [{"week": "W2", "cell_id": f"d{i}", "week_action": "reinvest",
            "applied": False, "pred_net_rev_delta": 50.0} for i in range(5)]
    )
    acc = acceptance_history(acc_hist, exec_weeks={"W1"})
    print("\n=== Case 3: acceptance rate ===")
    print(json.dumps(acc, indent=2, default=str))
    w1a = next(w for w in acc["weekly"] if w["week"] == "W1")
    w2a = next(w for w in acc["weekly"] if w["week"] == "W2")
    assert abs(w1a["acceptance_rate"] - 0.80) < 1e-9, w1a       # 8 of 10 applied
    assert w1a["status"] == "WATCH", w1a                         # 0.80 < 0.85 floor
    assert w2a["acceptance_rate"] is None, w2a                   # log not returned != 0%
    assert w2a["status"] == "log not returned", w2a
    assert abs(acc["cum_acceptance_rate"] - 0.80) < 1e-9         # cumulative over W1 only
    # denominator honesty: the confirmed denominator (10, W1) is what the 80% is over —
    # NOT the all-weeks total (15) that includes W2's unreturned log.
    assert acc["n_recommended_confirmed"] == 10, acc
    assert acc["n_recommended_total"] == 15, acc
    # no execution log at all -> every week n/a, cumulative None (never 0)
    acc_none = acceptance_history(acc_hist, exec_weeks=None)
    assert all(w["acceptance_rate"] is None for w in acc_none["weekly"])
    assert acc_none["cum_acceptance_rate"] is None
    assert "n/a" in acc_none.get("note", "")
    # empty / pre-upgrade history -> graceful note
    assert "note" in acceptance_history(None)
    assert "note" in acceptance_history(pd.DataFrame({"week": ["W1"]}))

    print("\nAll smoke-test assertions passed.")
    sys.exit(0)
