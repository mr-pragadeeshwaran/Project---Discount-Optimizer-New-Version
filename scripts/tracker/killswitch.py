"""
killswitch.py — the SAFETY MECHANISM for the Weekly Discount Tracker (GAP 2).

Business purpose
----------------
The tracker recommends discount cuts. This module is the automatic brake: it watches
what ACTUALLY happened after each cut and pulls the plug on a cell when the cut is
clearly hurting — BEFORE it bleeds another week of lost sales. It is the code that
finally *reads* the two safety tolerances the config has always carried but nothing
consumed (VOLUME_DROP_TOLERANCE_PCT and the portfolio drift threshold).

The golden rule from the weekly readout ("if a cut loses sales for 2 straight weeks,
revert it — the model was wrong on that cell") is enforced here, mechanically, with
one crucial piece of honesty layered on top:

    A cell only earns a "strike" if the sales miss is the CUT'S fault. If the shelf
    went out of stock (OSA collapsed) or we lost share of voice (SOV collapsed) that
    week, the flat/soft sales are a CONFOUNDER, not proof the cut failed — so we do
    NOT punish the cell for it. Blaming a cut for an OOS week would revert good cells
    and destroy trust. Confounder is checked FIRST, every week.

Two-strikes-and-revert, with confounders excused:
  * Week is CONFOUNDED  -> flagged, running strike count is left untouched (not reset,
    not incremented): the week is simply uninformative about the cut.
  * Week is a STRIKE    -> units missed the promise by more than tolerance AND the cell
    actually lost net revenue. Running strikes += 1.
  * Week is CLEAN       -> running strikes reset to 0 (one good week clears the slate).
  * strikes >= threshold -> REVERT the cell (put the discount back), then FREEZE it for
    a cooling-off window so we stop poking it every week.

Only ACTED cells are judged:
  The kill-switch only ever judges cells we actually MOVED this week — rows where
  applied==True AND week_action in {'cut','reinvest'}. Unacted holds (we recommended
  'hold', or the cut was never confirmed on the portal) carry no prediction we made a
  bet on — their pred_net_rev_delta is 0 — so judging them would manufacture phantom
  strikes/reverts and poison the portfolio-drift denominator with hundreds of no-op
  holds. Unacted rows are left untouched (cell_status='', strikes=NaN, no strike) and
  are EXCLUDED from the drift hit_rate / n_scored. If the applied/week_action columns
  are absent (older history), we fall back to "acted = pred_net_rev_delta != 0" so a
  zero-pred hold still can't be judged.

Portfolio drift brake:
  If enough ACTED cells have been scored and the overall latest-week hit-rate falls below
  the floor, the whole engine is drifting — so we raise block_new_cuts to stop rolling out
  NEW cuts until accuracy recovers (existing reverts still fire).

Public function
---------------
    evaluate(history_df, config) -> (history_df_out, alerts: dict)

Contract for history_df (one row per (cell_id, week); the SHARED tracker_history schema):
    Existing: week(str), week_date(str), cell_id, confidence, scored(bool),
              pred_net_rev_delta(float), actual_net_rev_delta(float, NaN until filled),
              pred_units(float), actual_units(float, NaN until filled)
    New columns this module reads if present / writes:
              applied(bool), baseline_net_rev_wk, baseline_units_wk,
              baseline_osa, baseline_sov, actual_osa, actual_sov,
              strikes(int), cell_status(str: active|confounded|reverted|frozen)

Dependencies: pandas / numpy / python stdlib only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# Config keys this module honours, with their defaults. An integrator can pass a plain
# dict; anything omitted falls back to these. Defaults mirror v4_config.py:
#   vol_tol_pct    = VOLUME_DROP_TOLERANCE_PCT / 100  = 0.05
#   confounder_pct = 0.10  (a 10% OSA/SOV drop vs baseline flips the week to 'confounded')
#   strikes_to_revert = 2, freeze_weeks = 4, drift_min_cells = 30, hit_rate_floor = 0.60
_CONFIG_DEFAULTS = {
    "vol_tol_pct": 0.05,
    "confounder_pct": 0.10,
    "strikes_to_revert": 2,
    "freeze_weeks": 4,
    "drift_min_cells": 30,
    "hit_rate_floor": 0.60,
}

# Columns we WRITE. Existing columns are never renamed (contract), only these are added
# / overwritten with this module's verdict.
_OUT_STRIKES = "strikes"
_OUT_STATUS = "cell_status"

# Cell-status vocabulary (kept as constants so callers can compare without typos).
STATUS_ACTIVE = "active"
STATUS_CONFOUNDED = "confounded"
STATUS_REVERTED = "reverted"
STATUS_FROZEN = "frozen"


def _cfg(config, key):
    """Read a config key with a fallback to the module default.

    Accepts a dict, or anything with .get, or None. Missing / None -> default. This is
    deliberately forgiving so the integrator can pass a partial config (e.g. only
    override hit_rate_floor) without exploding.
    """
    if config is None:
        return _CONFIG_DEFAULTS[key]
    val = config.get(key) if hasattr(config, "get") else None
    return _CONFIG_DEFAULTS[key] if val is None else val


def _natural_week_key(week_series):
    """Numeric-aware ordering key for week LABELS like 'W1','W2',...,'W9','W10'.

    Plain string comparison sorts 'W10' before 'W9' (lexical), which would pick the
    wrong "latest" week. We extract the leading run of digits from each label and sort
    on that integer; labels with no digits fall to the end (NaN) but still order
    deterministically via the caller's stable sort on original row position.
    """
    s = week_series.astype(str)
    num = s.str.extract(r"(\d+)", expand=False)
    return pd.to_numeric(num, errors="coerce")


def _order_key(df):
    """Return a Series to sort a cell's weeks chronologically.

    Prefer week_date (a real date string) because the week LABEL ('W1','W2',... or even
    'W10') does not sort correctly as a plain string once you pass W9 -> W10. Fall back
    to a NUMERIC-aware key on the week label only when week_date is absent/unparseable —
    never raw str max, which would rank 'W9' above 'W10'. We keep the ORIGINAL row order
    as the final tie-breaker via a stable sort, so equal keys stay put.
    """
    if "week_date" in df.columns:
        dt = pd.to_datetime(df["week_date"], errors="coerce")
        if dt.notna().any():
            return dt
    return _natural_week_key(df["week"])


def _is_confounded(row, confounder_pct):
    """Was this week's soft result explained away by an OSA or SOV collapse?

    True if actual OSA fell below baseline_osa*(1-confounder_pct), OR actual SOV fell
    below baseline_sov*(1-confounder_pct). Missing baselines or actuals -> we CANNOT
    prove a confounder, so we return False (the week is treated on its merits). We only
    excuse a week when we have positive evidence the shelf/visibility broke.
    """
    for base_col, act_col in (("baseline_osa", "actual_osa"), ("baseline_sov", "actual_sov")):
        base = row.get(base_col, np.nan)
        act = row.get(act_col, np.nan)
        if pd.notna(base) and pd.notna(act) and float(base) > 0:
            if float(act) < float(base) * (1.0 - confounder_pct):
                return True
    return False


def _is_strike(row, vol_tol_pct):
    """Did the CUT itself fail this week?

    A strike requires BOTH:
      * units missed the promise by more than tolerance:
            actual_units < pred_units * (1 - vol_tol_pct)
      * the cell actually lost net revenue this week:
            actual_net_rev_delta < 0
    Requiring the revenue leg too means a cut that shifted a few units but still made
    money is NOT a strike — we only strike moves that are genuinely underwater. Missing
    pred/actual units -> cannot judge the volume leg -> not a strike.
    """
    pred_u = row.get("pred_units", np.nan)
    act_u = row.get("actual_units", np.nan)
    rev_delta = row.get("actual_net_rev_delta", np.nan)
    if pd.isna(pred_u) or pd.isna(act_u) or pd.isna(rev_delta):
        return False
    volume_missed = float(act_u) < float(pred_u) * (1.0 - vol_tol_pct)
    lost_revenue = float(rev_delta) < 0.0
    return bool(volume_missed and lost_revenue)


def _sign(x):
    """Plain int sign (-1/0/+1); NaN -> np.nan so it can be compared safely."""
    if pd.isna(x):
        return np.nan
    return int(np.sign(float(x)))


_ACTED_ACTIONS = frozenset({"cut", "reinvest"})


def _acted_mask(df):
    """Which rows did we ACTUALLY move this week — the only rows the kill-switch judges.

    A row is 'acted' when we placed a real bet on it:
        applied == True  AND  week_action in {'cut','reinvest'}.
    Unacted rows are our own 'hold' recommendations, or cuts never confirmed on the
    portal (applied False). They carry pred_net_rev_delta == 0 — no promise to keep —
    so judging them would fabricate phantom strikes/reverts and flood the drift
    denominator with no-op holds.

    Backward-compat: if the applied / week_action columns are BOTH absent (older
    history that predates execution logging), we cannot read intent, so we fall back to
    'acted = pred_net_rev_delta != 0'. That still refuses to judge a zero-pred hold
    (the exact phantom-revert case) while judging every real move as before.
    """
    has_applied = "applied" in df.columns
    has_action = "week_action" in df.columns
    if has_applied or has_action:
        mask = pd.Series(True, index=df.index)
        if has_applied:
            applied = df["applied"].map(
                lambda v: str(v).strip().lower() in ("true", "1", "yes", "y", "t")
                if not isinstance(v, bool) else v
            )
            # NaN/blank applied -> not confirmed -> not acted.
            mask &= applied.fillna(False).astype(bool)
        if has_action:
            mask &= df["week_action"].astype(str).str.strip().str.lower().isin(_ACTED_ACTIONS)
        return mask
    # Fallback for pre-execution-log history: a non-zero prediction means we moved it.
    pred = pd.to_numeric(df.get("pred_net_rev_delta", np.nan), errors="coerce")
    return pred.fillna(0.0) != 0.0


def evaluate(history_df, config):
    """Run the safety mechanism over the tracker history.

    Parameters
    ----------
    history_df : pandas.DataFrame
        The shared tracker history (one row per cell per week). Only rows whose
        actual_net_rev_delta is filled (not NaN) are evaluated — a week without actuals
        yet is invisible to the kill-switch. May be empty / None (first run).
    config : dict | None
        Overrides for the keys in _CONFIG_DEFAULTS. Partial dicts are fine.

    Returns
    -------
    (history_df_out, alerts)
        history_df_out : a COPY of history_df with two columns written for every
            evaluated row: 'strikes' (running count at that week) and 'cell_status'
            (active | confounded | reverted | frozen). Rows without actuals are left
            with strikes=<NaN kept as existing or 0> and cell_status='' so the caller
            can tell "not yet judged" from "judged clean".
        alerts : dict with keys
            reverts        : list[cell_id]  cells that hit the strike threshold this pass
            frozen         : list[cell_id]  cells now in the post-revert cooling window
            confounded     : list[cell_id]  cells whose LATEST scored week was confounded
            block_new_cuts : bool           stop rolling out NEW cuts (portfolio drift)
            note           : str            plain-English summary for the readout
            n_scored       : int            #cells with >=1 scored week (drift denominator)
            hit_rate       : float | None   latest-week direction hit-rate (drift numerator)
    """
    vol_tol_pct = float(_cfg(config, "vol_tol_pct"))
    confounder_pct = float(_cfg(config, "confounder_pct"))
    strikes_to_revert = int(_cfg(config, "strikes_to_revert"))
    freeze_weeks = int(_cfg(config, "freeze_weeks"))
    drift_min_cells = int(_cfg(config, "drift_min_cells"))
    hit_rate_floor = float(_cfg(config, "hit_rate_floor"))

    alerts = {
        "reverts": [],
        "frozen": [],
        "confounded": [],
        "block_new_cuts": False,
        "note": "",
        "n_scored": 0,
        "hit_rate": None,
    }

    # --- First-run guard: nothing to evaluate. ----------------------------------------
    if history_df is None or len(history_df) == 0:
        out = history_df.copy() if history_df is not None else pd.DataFrame()
        if _OUT_STRIKES not in out.columns:
            out[_OUT_STRIKES] = pd.Series(dtype="float")
        if _OUT_STATUS not in out.columns:
            out[_OUT_STATUS] = pd.Series(dtype="object")
        alerts["note"] = "no scored weeks yet — kill-switch idle"
        return out, alerts

    out = history_df.copy()
    # Ensure the output columns exist. Preserve any prior values; default to blank so an
    # un-evaluated row reads as "not judged", distinct from strikes=0 "judged clean".
    if _OUT_STRIKES not in out.columns:
        out[_OUT_STRIKES] = np.nan
    if _OUT_STATUS not in out.columns:
        out[_OUT_STATUS] = ""
    out[_OUT_STATUS] = out[_OUT_STATUS].astype(object)

    # Coerce the numeric columns we depend on (leave missing ones as all-NaN).
    for col in ("pred_units", "actual_units", "actual_net_rev_delta", "pred_net_rev_delta",
                "baseline_osa", "actual_osa", "baseline_sov", "actual_sov"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
        else:
            out[col] = np.nan

    # A row is "scored" once it has an actual net-rev delta. But we only JUDGE rows we
    # actually ACTED on (applied cut/reinvest) — an unacted hold has a zero prediction we
    # never bet on, so judging it would fabricate phantom strikes/reverts and pollute the
    # drift denominator. is_judged = has actuals AND was acted. Unacted rows fall through
    # every branch below: status stays '', strikes stay NaN, and they are dropped from the
    # drift hit_rate / n_scored.
    has_actual = out["actual_net_rev_delta"].notna()
    is_acted = _acted_mask(out)
    is_scored = has_actual & is_acted

    # ------------------------------------------------------------------------------
    # PER-CELL walk, in chronological (week) order.
    # ------------------------------------------------------------------------------
    reverts, frozen, confounded_latest = [], [], []

    for cell_id, cell_rows in out.groupby("cell_id", sort=False):
        # Sort this cell's rows chronologically; keep index so we can write back.
        order = _order_key(cell_rows)
        cell_sorted = cell_rows.assign(_ord=order.values).sort_values(
            "_ord", kind="stable"
        )

        running = 0            # running strike count (survives across weeks)
        reverted_here = False  # once reverted, remaining weeks are 'frozen'
        freeze_left = 0        # cooling-off weeks remaining after a revert
        last_scored_confounded = False

        # The cell's LATEST scored week: a revert only ALERTS (re-fires) when it lands
        # on this week, i.e. it's newly reverted this pass. Reverts buried in history
        # (still visible in raw actuals every run) must not re-alert every time.
        scored_idxs = [i for i in cell_sorted.index if is_scored.loc[i]]
        last_scored_idx = scored_idxs[-1] if scored_idxs else None
        reverted_on_latest = False

        for idx in cell_sorted.index:
            if not is_scored.loc[idx]:
                # No actuals yet -> not judged. Leave status blank, strikes untouched.
                continue

            row = out.loc[idx]

            # If the freeze window has fully elapsed, clear the frozen state BEFORE
            # scoring this week so the cell re-enters normal 'active' evaluation. This
            # is what makes freeze_weeks a real, expiring window rather than a permanent
            # latch: after `freeze_weeks` scored weeks the cell is judged again.
            if reverted_here and freeze_left == 0:
                reverted_here = False
                running = 0

            # If we're still inside the cooling-off window, subsequent scored weeks are
            # flagged frozen: don't strike, don't revert again; just tick the window down.
            if reverted_here:
                out.at[idx, _OUT_STATUS] = STATUS_FROZEN
                out.at[idx, _OUT_STRIKES] = running
                if freeze_left > 0:
                    freeze_left -= 1
                last_scored_confounded = False
                continue

            # 1. CONFOUNDER FIRST — OSA/SOV collapse excuses the week entirely.
            if _is_confounded(row, confounder_pct):
                out.at[idx, _OUT_STATUS] = STATUS_CONFOUNDED
                out.at[idx, _OUT_STRIKES] = running  # strikes untouched
                last_scored_confounded = True
                continue

            last_scored_confounded = False

            # 2. STRIKE vs CLEAN.
            if _is_strike(row, vol_tol_pct):
                running += 1
            else:
                running = 0  # a good week clears the count

            out.at[idx, _OUT_STRIKES] = running

            # 3. REVERT when the running count hits the threshold.
            if running >= strikes_to_revert:
                out.at[idx, _OUT_STATUS] = STATUS_REVERTED
                reverted_here = True
                freeze_left = freeze_weeks
                # Only remember this for the alert if it's the cell's latest scored
                # week — otherwise it's a historical revert that must not re-fire.
                reverted_on_latest = (idx == last_scored_idx)
            else:
                out.at[idx, _OUT_STATUS] = STATUS_ACTIVE

        if reverted_on_latest:
            reverts.append(cell_id)
        if reverted_here:
            frozen.append(cell_id)
        if last_scored_confounded:
            confounded_latest.append(cell_id)

    alerts["reverts"] = reverts
    alerts["frozen"] = frozen
    alerts["confounded"] = confounded_latest

    # ------------------------------------------------------------------------------
    # PORTFOLIO DRIFT — is the whole engine losing its aim?
    # Denominator: distinct cells with at least one scored week.
    # Numerator:   direction hit-rate on the LATEST scored week only.
    # ------------------------------------------------------------------------------
    scored_rows = out[is_scored]
    n_scored = int(scored_rows["cell_id"].nunique()) if len(scored_rows) else 0
    alerts["n_scored"] = n_scored

    hit_rate = None
    if len(scored_rows):
        latest_ord = _order_key(scored_rows)
        # Max on a date/numeric key picks the true latest week (never lexical str max,
        # which ranks 'W9' above 'W10'). If every key is unparseable (all-NaN), fall
        # back to the last rows in original order so we still have SOMETHING to score.
        latest_val = latest_ord.max()
        if pd.isna(latest_val):
            latest_mask = np.zeros(len(scored_rows), dtype=bool)
            latest_mask[-1] = True
        else:
            latest_mask = latest_ord.values == latest_val
        latest = scored_rows[latest_mask]
        if len(latest):
            pred_sign = latest["pred_net_rev_delta"].map(_sign)
            act_sign = latest["actual_net_rev_delta"].map(_sign)
            comparable = pred_sign.notna() & act_sign.notna()
            if comparable.any():
                hits = (pred_sign[comparable] == act_sign[comparable]).sum()
                hit_rate = float(hits) / float(int(comparable.sum()))
    alerts["hit_rate"] = hit_rate

    if n_scored >= drift_min_cells and hit_rate is not None and hit_rate < hit_rate_floor:
        alerts["block_new_cuts"] = True

    # ------------------------------------------------------------------------------
    # Plain-English note for the weekly readout.
    # ------------------------------------------------------------------------------
    bits = []
    if reverts:
        bits.append(f"{len(reverts)} cell(s) auto-reverted after {strikes_to_revert} "
                    f"strikes; frozen {freeze_weeks} wk")
    if confounded_latest:
        bits.append(f"{len(confounded_latest)} cell(s) confounded (OSA/SOV drop) — "
                    f"not penalised")
    if alerts["block_new_cuts"]:
        hr = f"{hit_rate*100:.0f}%" if hit_rate is not None else "n/a"
        bits.append(f"PORTFOLIO DRIFT: latest hit-rate {hr} < "
                    f"{hit_rate_floor*100:.0f}% floor over {n_scored} cells — "
                    f"blocking NEW cuts")
    if not bits:
        bits.append("all cuts within tolerance — no reverts")
    alerts["note"] = "; ".join(bits)

    return out, alerts


if __name__ == "__main__":
    # ------------------------------------------------------------------------------
    # Smoke test: build tiny synthetic histories that exercise every branch, run
    # evaluate(), print results, assert the verdicts, exit 0.
    # ------------------------------------------------------------------------------
    import json
    import sys

    def _mk(cell_id, week, week_date, pred_u, act_u, rev_delta, pred_rev=None,
            b_osa=95.0, a_osa=95.0, b_sov=20.0, a_sov=20.0):
        """One history row. pred_rev defaults to same sign as rev_delta for hit-rate."""
        if pred_rev is None:
            pred_rev = rev_delta  # perfect direction by default
        return {
            "week": week, "week_date": week_date, "cell_id": cell_id,
            "confidence": "High", "scored": True,
            "pred_net_rev_delta": pred_rev, "actual_net_rev_delta": rev_delta,
            "pred_units": pred_u, "actual_units": act_u,
            "baseline_osa": b_osa, "actual_osa": a_osa,
            "baseline_sov": b_sov, "actual_sov": a_sov,
        }

    cfg = {  # explicit config = the documented defaults
        "vol_tol_pct": 0.05, "confounder_pct": 0.10,
        "strikes_to_revert": 2, "freeze_weeks": 4,
        "drift_min_cells": 30, "hit_rate_floor": 0.60,
    }

    # --- Case 0: empty / first run -> idle, no crash. ---------------------------------
    out0, al0 = evaluate(pd.DataFrame(), cfg)
    print("=== Case 0: empty history ===")
    print(json.dumps(al0, indent=2, default=str))
    assert al0["reverts"] == [] and al0["block_new_cuts"] is False
    assert al0["n_scored"] == 0 and al0["hit_rate"] is None

    # --- Case 1: the four cell archetypes. --------------------------------------------
    rows = []
    # REVERT: two consecutive underwater weeks (units miss > 5% AND rev_delta < 0).
    #   pred_units 100, actual 80 (20% miss), rev_delta -500  -> strike x2 -> revert.
    #   NOTE: the revert lands on W2, but W3 (a later scored frozen week) exists, so on
    #   THIS pass the revert is HISTORICAL, not new -> it must NOT re-fire the 'reverts'
    #   alert (fix #3), only the persistent 'frozen' state. REVERT_NOW below covers the
    #   newly-reverted-this-pass case that DOES fire.
    rows += [_mk("REVERT", "W1", "2026-07-06", 100, 80, -500),
             _mk("REVERT", "W2", "2026-07-13", 100, 80, -500),
             _mk("REVERT", "W3", "2026-07-20", 100, 80, -500)]  # frozen after revert
    # REVERT_NOW: strikes to the threshold exactly on its LATEST scored week -> the
    # revert IS new this pass -> fires the 'reverts' alert.
    rows += [_mk("REVERT_NOW", "W1", "2026-07-06", 100, 80, -500),
             _mk("REVERT_NOW", "W2", "2026-07-13", 100, 80, -500)]  # reverts on latest
    # CONFOUNDED: same bad numbers, but OSA collapsed each week -> excused, no strike.
    rows += [_mk("CONF", "W1", "2026-07-06", 100, 80, -500, a_osa=50.0),
             _mk("CONF", "W2", "2026-07-13", 100, 80, -500, a_osa=50.0)]
    # RECOVER: one strike, then a clean week resets -> never reverts, ends 'active'.
    rows += [_mk("RECOVER", "W1", "2026-07-06", 100, 80, -500),   # strike (running=1)
             _mk("RECOVER", "W2", "2026-07-13", 100, 100, 300)]   # clean -> running=0
    # GOOD: units hold, revenue up -> clean every week -> 'active', 0 strikes.
    rows += [_mk("GOOD", "W1", "2026-07-06", 100, 102, 400),
             _mk("GOOD", "W2", "2026-07-13", 100, 101, 350)]
    # PENDING: latest week has no actuals -> not judged (stays blank).
    pend = _mk("PENDING", "W1", "2026-07-06", 100, np.nan, np.nan)
    rows.append(pend)

    hist = pd.DataFrame(rows)
    out1, al1 = evaluate(hist, cfg)
    print("\n=== Case 1: four archetypes ===")
    show = out1[["cell_id", "week", "strikes", "cell_status"]]
    print(show.to_string(index=False))
    print("\nalerts:")
    print(json.dumps(al1, indent=2, default=str))

    def _status(cell, week):
        m = (out1["cell_id"] == cell) & (out1["week"] == week)
        return out1.loc[m, "cell_status"].iloc[0]

    def _strikes(cell, week):
        m = (out1["cell_id"] == cell) & (out1["week"] == week)
        return out1.loc[m, "strikes"].iloc[0]

    # REVERT: W1 strike(1)/active, W2 strike(2)/reverted, W3 frozen.
    assert _status("REVERT", "W1") == STATUS_ACTIVE, _status("REVERT", "W1")
    assert _strikes("REVERT", "W1") == 1
    assert _status("REVERT", "W2") == STATUS_REVERTED, _status("REVERT", "W2")
    assert _strikes("REVERT", "W2") == 2
    assert _status("REVERT", "W3") == STATUS_FROZEN, _status("REVERT", "W3")
    # fix #3: revert is HISTORICAL (W3 is the latest scored week, not W2) -> the cell
    # stays 'frozen' but does NOT re-fire the 'reverts' alert this pass.
    assert "REVERT" in al1["frozen"], al1["frozen"]
    assert "REVERT" not in al1["reverts"], al1["reverts"]

    # REVERT_NOW: reverts exactly on its latest scored week -> fires 'reverts' AND is
    # frozen going forward.
    assert _status("REVERT_NOW", "W2") == STATUS_REVERTED, _status("REVERT_NOW", "W2")
    assert "REVERT_NOW" in al1["reverts"], al1["reverts"]
    assert "REVERT_NOW" in al1["frozen"], al1["frozen"]

    # CONFOUNDED: both weeks excused, strikes stay 0, never reverts.
    assert _status("CONF", "W1") == STATUS_CONFOUNDED
    assert _status("CONF", "W2") == STATUS_CONFOUNDED
    assert _strikes("CONF", "W2") == 0
    assert "CONF" not in al1["reverts"]
    assert "CONF" in al1["confounded"]  # latest scored week was confounded

    # RECOVER: strike then clean -> ends active, 0 strikes, no revert.
    assert _strikes("RECOVER", "W1") == 1
    assert _status("RECOVER", "W2") == STATUS_ACTIVE
    assert _strikes("RECOVER", "W2") == 0
    assert "RECOVER" not in al1["reverts"]

    # GOOD: clean throughout.
    assert _status("GOOD", "W2") == STATUS_ACTIVE
    assert _strikes("GOOD", "W2") == 0

    # PENDING: no actuals -> not judged (blank status).
    assert _status("PENDING", "W1") == "", repr(_status("PENDING", "W1"))

    # Drift NOT triggered: only 5 scored cells << drift_min_cells (30).
    assert al1["block_new_cuts"] is False
    # REVERT, REVERT_NOW, CONF, RECOVER, GOOD (not PENDING).
    assert al1["n_scored"] == 5, al1["n_scored"]

    # --- Case 2: portfolio drift block. -----------------------------------------------
    # 40 cells scored in one week; 30 of them predicted the WRONG direction
    # (pred +, actual -), so latest hit-rate = 10/40 = 0.25 < 0.60 floor.
    drift_rows = []
    for i in range(40):
        wrong = i < 30
        # keep units clean so we isolate the drift path (no strikes muddying it)
        drift_rows.append(_mk(f"D{i}", "W1", "2026-07-06", 100, 100,
                              rev_delta=(-200 if wrong else 200),
                              pred_rev=200))  # always predicted positive
    out2, al2 = evaluate(pd.DataFrame(drift_rows), cfg)
    print("\n=== Case 2: portfolio drift ===")
    print(json.dumps({k: al2[k] for k in
                      ("n_scored", "hit_rate", "block_new_cuts", "note")},
                     indent=2, default=str))
    assert al2["n_scored"] == 40
    assert abs(al2["hit_rate"] - 10.0 / 40.0) < 1e-9, al2["hit_rate"]
    assert al2["block_new_cuts"] is True

    # --- Case 3: config forgiving-ness — None config uses defaults, no crash. ---------
    out3, al3 = evaluate(hist, None)
    assert al3["reverts"] == al1["reverts"]  # same verdict via defaults

    # --- Case 4: fix #1 — freeze_weeks is a REAL expiring window, not a permanent latch.
    # One cell, 5 underwater weeks. It reverts at W2 (strike x2), then the freeze runs.
    # With a SHORT freeze it thaws and re-strikes/re-reverts on its latest week; with a
    # LONG freeze it stays frozen for the rest of history. freeze_weeks=1 and =4 MUST
    # produce different per-week verdicts.
    fz_rows = [_mk("FZ", f"W{w}", f"2026-07-{6 + 7*(w-1):02d}", 100, 80, -500)
               for w in range(1, 6)]  # W1..W5, all underwater
    fz_hist = pd.DataFrame(fz_rows)

    cfg_short = dict(cfg, freeze_weeks=1)
    cfg_long = dict(cfg, freeze_weeks=4)
    out_s, al_s = evaluate(fz_hist, cfg_short)
    out_l, al_l = evaluate(fz_hist, cfg_long)

    def _statuses(out):
        m = out["cell_id"] == "FZ"
        return list(out.loc[m].sort_values("week_date")["cell_status"])

    st_short = _statuses(out_s)
    st_long = _statuses(out_l)
    print("\n=== Case 4: freeze expiry (fix #1) ===")
    print(f"  freeze_weeks=1 statuses: {st_short}")
    print(f"  freeze_weeks=4 statuses: {st_long}")
    print(f"  freeze_weeks=1 reverts:  {al_s['reverts']}")
    print(f"  freeze_weeks=4 reverts:  {al_l['reverts']}")

    # The two windows MUST produce different per-week verdicts (freeze actually expires).
    assert st_short != st_long, (st_short, st_long)
    # freeze=1: [active, reverted, frozen, active, reverted] — W3 is the single cooling
    # week, W4 thaws, W5 re-reverts ON the latest week -> 'reverts' fires.
    assert st_short == [STATUS_ACTIVE, STATUS_REVERTED, STATUS_FROZEN,
                        STATUS_ACTIVE, STATUS_REVERTED], st_short
    assert "FZ" in al_s["reverts"], al_s["reverts"]
    # freeze=4: [active, reverted, frozen, frozen, frozen] — window never elapses in 5
    # wks, so the only revert is historical -> 'reverts' does NOT fire.
    assert st_long == [STATUS_ACTIVE, STATUS_REVERTED, STATUS_FROZEN,
                       STATUS_FROZEN, STATUS_FROZEN], st_long
    assert st_short.count(STATUS_FROZEN) < st_long.count(STATUS_FROZEN), \
        (st_short, st_long)
    assert "FZ" not in al_l["reverts"], al_l["reverts"]

    # --- Case 5: fix #2 — drift "latest week" uses numeric/date order, not str max. ----
    # Ten weekly rows W1..W10 for one cell, ALL clean units (no strikes). Predictions
    # are correct on every week EXCEPT the true latest (W10), where pred +, actual -.
    # Lexical str max would pick "W9" (wrong sign only on W10 would be missed) and
    # report a perfect 100% hit-rate; correct ordering picks W10 -> hit-rate 0%.
    dr_rows = []
    for w in range(1, 11):
        latest = (w == 10)
        dr_rows.append(_mk("ORD", f"W{w}", f"2026-{'07' if w<5 else '08'}-{((w-1)%4)*7+1:02d}",
                           100, 100,
                           rev_delta=(-200 if latest else 200),
                           pred_rev=200))  # always predicted positive
    # Blank out week_date so the fallback (numeric week-label key) is what's exercised.
    ord_hist = pd.DataFrame(dr_rows)
    ord_hist["week_date"] = ""  # force _order_key onto the week-label fallback
    # 40 clean-direction extra cells so drift denominator passes and block can trigger.
    filler = [_mk(f"F{i}", "W10", "", 100, 100, rev_delta=200, pred_rev=200)
              for i in range(40)]
    fill_hist = pd.DataFrame(filler)
    fill_hist["week_date"] = ""
    ord_all = pd.concat([ord_hist, fill_hist], ignore_index=True)

    out5, al5 = evaluate(ord_all, cfg)
    print("\n=== Case 5: drift latest-week ordering (fix #2) ===")
    print(f"  latest week picked correctly? hit_rate={al5['hit_rate']}")
    # W10 is the true latest for ORD. Its wrong-direction row must be included in the
    # latest-week set. With str max, 'W9' would win and ORD's W10 miss would be ignored,
    # inflating hit_rate to 1.0. Correct numeric order includes W10's miss.
    assert al5["hit_rate"] is not None
    assert al5["hit_rate"] < 1.0, al5["hit_rate"]  # the W10 miss is counted

    # --- Case 6: ISSUE A — evaluate() judges ONLY acted cells, never unacted holds. -----
    # This reproduces the real bug: the first week actuals arrive, hundreds of 'hold'
    # cells (pred_net_rev_delta == 0, week_action='hold', applied=False) get an actual
    # net-rev delta too. The OLD code judged them, tripping phantom strikes/reverts and
    # flooding the drift denominator. The FIX: holds are invisible to the kill-switch.
    def _mk_full(cell_id, week, week_date, pred_u, act_u, rev_delta, pred_rev,
                 applied, week_action, b_osa=95.0, a_osa=95.0, b_sov=20.0, a_sov=20.0):
        r = _mk(cell_id, week, week_date, pred_u, act_u, rev_delta, pred_rev,
                b_osa, a_osa, b_sov, a_sov)
        r["applied"] = applied
        r["week_action"] = week_action
        return r

    issue_a = []
    # One ACTED cut that is genuinely underwater two weeks -> SHOULD revert & be scored.
    issue_a += [_mk_full("ACT_CUT", "W1", "2026-07-06", 100, 80, -500, -500, True, "cut"),
                _mk_full("ACT_CUT", "W2", "2026-07-13", 100, 80, -500, -500, True, "cut")]
    # 50 UNACTED holds: pred 0, applied False, week_action 'hold', but actuals ARE filled
    # (a real negative delta on some, to prove even "bad-looking" holds never strike).
    for i in range(50):
        issue_a.append(_mk_full(f"HOLD{i}", "W1", "2026-07-06", 0, 0,
                                rev_delta=(-999 if i % 2 else 999), pred_rev=0,
                                applied=False, week_action="hold"))
    # One CONFIRMED cut (applied True) that never fired a strike -> acted & clean.
    issue_a.append(_mk_full("ACT_OK", "W1", "2026-07-06", 100, 101, 300, 300, True, "cut"))
    ia_hist = pd.DataFrame(issue_a)
    out6, al6 = evaluate(ia_hist, cfg)
    print("\n=== Case 6: ISSUE A — unacted holds are never judged ===")
    print(f"  n_scored (acted only)={al6['n_scored']}  reverts={al6['reverts']}  "
          f"block_new_cuts={al6['block_new_cuts']}")

    def _row6(cell):
        return out6.loc[out6["cell_id"] == cell].iloc[0]

    # Every unacted hold: blank status, NaN strikes, NO strike, NOT reverted.
    for i in range(50):
        r = _row6(f"HOLD{i}")
        assert r["cell_status"] == "", (i, repr(r["cell_status"]))
        assert pd.isna(r["strikes"]), (i, r["strikes"])
        assert f"HOLD{i}" not in al6["reverts"], i
    # The acted underwater cut IS judged and reverts as before.
    assert "ACT_CUT" in al6["reverts"], al6["reverts"]
    # Drift denominator counts ONLY the 2 acted cells (ACT_CUT, ACT_OK), NOT the 50 holds.
    assert al6["n_scored"] == 2, al6["n_scored"]
    # 50 unacted holds present but the brake ignores them -> no phantom drift block.
    assert al6["block_new_cuts"] is False, al6["block_new_cuts"]

    # --- Case 6b: backward-compat fallback — no applied/week_action cols, zero-pred holds
    # still can't be judged (acted = pred_net_rev_delta != 0). Older history path. --------
    compat = [_mk("BET", "W1", "2026-07-06", 100, 80, -500),        # non-zero pred -> acted
              _mk("BET", "W2", "2026-07-13", 100, 80, -500)]
    compat += [_mk(f"Z{i}", "W1", "2026-07-06", 0, 0, rev_delta=-999, pred_rev=0)
               for i in range(50)]  # zero-pred holds -> NOT acted even without the columns
    compat_hist = pd.DataFrame(compat)
    out6b, al6b = evaluate(compat_hist, cfg)
    for i in range(50):
        rz = out6b.loc[out6b["cell_id"] == f"Z{i}"].iloc[0]
        assert rz["cell_status"] == "", repr(rz["cell_status"])
        assert pd.isna(rz["strikes"]), rz["strikes"]
    assert "BET" in al6b["reverts"], al6b["reverts"]
    assert al6b["n_scored"] == 1, al6b["n_scored"]  # only the non-zero-pred cell
    print(f"  6b fallback n_scored={al6b['n_scored']} (zero-pred holds excluded)")

    print("\nAll smoke-test assertions passed.")
    sys.exit(0)
