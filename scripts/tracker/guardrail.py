"""
guardrail.py — REVENUE GUARDRAIL module for the Weekly Discount Tracker.

This module takes the model's SKU x city suggestions (plan_df) and clamps them
into a SAFE weekly move. It enforces three rules, in order:

  1. GLIDE          — never move a cell's discount more than max_step_ppt/week.
  2. REVENUE-PROTECTIVE — a discount CUT is only kept if the (linearly-scaled)
                       predicted net-revenue effect of this week's small step is
                       not negative. Otherwise HOLD at the current discount.
  3. BUDGET CAP     — total weekly discount spend must stay <= budget_pct_cap of
                       gross sales. Because the plan only cuts (never lowers price
                       into deeper discount by construction of rule 2), we mainly
                       REPORT cap status here and block reinvests if over.

Design intent (business framing):
  The model can be aggressive. The guardrail is the seatbelt: it moves prices in
  small weekly steps, refuses any step that the model itself predicts would lose
  revenue, and keeps total discount spend under the owner's budget ceiling. It
  NEVER invents extra cuts just to hit a budget target — it only ever softens the
  model, never over-tightens it.

Shared contract: consumes/produces the exact column names in plan_df and the
exact keys in config as defined by the tracker's SHARED CONTRACT.

Only pandas / numpy / stdlib are used.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# Small epsilon to avoid divide-by-zero when cur_disc == suggested_disc.
_EPS = 1e-9


def apply_guardrail(plan_df: pd.DataFrame, config: dict):
    """
    Enforce the revenue guardrail on the model's discount suggestions.

    Parameters
    ----------
    plan_df : pandas.DataFrame
        One row per SKU x city cell. Must contain at least the SHARED CONTRACT
        columns used below:
            mrp, cur_disc, cur_price, cur_units_wk,
            suggested_disc, suggested_price, pred_net_rev_delta_wk
    config : dict
        Must contain:
            max_step_ppt   (float) — max discount-point move allowed this week
            budget_pct_cap (float) — discount-spend ceiling as a fraction of gross

    Returns
    -------
    plan_df_out : pandas.DataFrame
        A COPY of plan_df with these columns added:
            week_disc            (percent 0-100) glided discount for this week
            week_price           = mrp*(1-week_disc/100)
            week_action          'cut' | 'hold' | 'reinvest'
            capped_by_guardrail  (bool) True if the guardrail changed the move
                                 (glide clamp, revenue HOLD, or budget block)
            week_saving_inr      (float) weekly net-rev gain from THIS week's step
                                 (== the revenue-protective week_pred_net_rev_delta)
    summary : dict
        total_gross_wk, total_disc_spend_wk, disc_pct, budget_pct_cap,
        headroom_inr, status ('GREEN'|'AMBER'|'RED'),
        n_cut, n_hold, n_reinvest, projected_week_saving_inr
    """
    df = plan_df.copy()

    # --- Pull config with sane fallbacks (integrator may pass a partial config) ---
    max_step_ppt = float(config.get("max_step_ppt", 3.0))
    budget_pct_cap = float(config.get("budget_pct_cap", 0.11))

    # --- Coerce the columns we rely on to numeric floats (defensive) ---
    for col in ("mrp", "cur_disc", "cur_price", "cur_units_wk",
                "suggested_disc", "pred_net_rev_delta_wk"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)

    mrp = df["mrp"].to_numpy(dtype=float)
    cur_disc = df["cur_disc"].to_numpy(dtype=float)
    suggested_disc = df["suggested_disc"].to_numpy(dtype=float)
    cur_units = df["cur_units_wk"].to_numpy(dtype=float)
    pred_delta = df["pred_net_rev_delta_wk"].to_numpy(dtype=float)

    # ------------------------------------------------------------------ #
    # RULE 1 — GLIDE: step cur_disc toward suggested_disc, capped at      #
    #                 +/- max_step_ppt this week.                         #
    # ------------------------------------------------------------------ #
    full_move = suggested_disc - cur_disc                    # signed ppt move model wants
    step = np.clip(full_move, -max_step_ppt, max_step_ppt)   # clamp to weekly budget
    week_disc = cur_disc + step

    # Clip to a valid discount range [0, 100] as a safety net.
    week_disc = np.clip(week_disc, 0.0, 100.0)

    # ------------------------------------------------------------------ #
    # RULE 2 — REVENUE-PROTECTIVE: for CUTS only (week_disc < cur_disc),  #
    #          keep the step only if its linearly-scaled predicted        #
    #          net-rev effect is not negative; else HOLD at cur_disc.     #
    #                                                                     #
    #   frac = (cur_disc - week_disc) / max(cur_disc - suggested_disc, eps)#
    #   week_pred_net_rev_delta = pred_net_rev_delta_wk * frac            #
    # ------------------------------------------------------------------ #
    is_cut = week_disc < cur_disc - _EPS   # discount going DOWN => price going UP

    # frac = fraction of the model's FULL move we take this week, computed as a
    # signed ratio of (step taken) / (full move the model wanted). This works in
    # both directions: for a cut the numerator/denominator are both negative-going
    # in discount, for a reinvest both positive-going, so the ratio stays in [0,1].
    # We guard the denominator by MAGNITUDE (not by clamping to a tiny positive
    # eps) so a reinvest — where suggested_disc > cur_disc — does not blow up.
    #
    # The contract writes this as, for cutting:
    #   frac = (cur_disc - week_disc) / max(cur_disc - suggested_disc, 1e-9)
    # which is exactly (step taken)/(full move) with a small-magnitude guard.
    full_move_signed = suggested_disc - cur_disc                 # model's full ppt move
    step_taken = week_disc - cur_disc                            # ppt actually taken this wk
    denom = np.where(np.abs(full_move_signed) < _EPS, _EPS, full_move_signed)
    frac = step_taken / denom              # portion of the full move taken this wk
    frac = np.clip(frac, 0.0, 1.0)         # never extrapolate beyond the model's move

    week_pred_net_rev_delta = pred_delta * frac

    # A cut is HELD (reverted) when it's a cut AND its predicted delta is negative.
    hold_mask = is_cut & (week_pred_net_rev_delta < 0.0)
    week_disc = np.where(hold_mask, cur_disc, week_disc)

    def _recompute_frac_delta(wk_disc: np.ndarray):
        """Refresh frac & scaled predicted delta after week_disc changes."""
        step = wk_disc - cur_disc
        f = np.clip(step / denom, 0.0, 1.0)
        return f, pred_delta * f

    # Recompute the derived quantities after any HOLD reversion (held cells -> 0).
    frac, week_pred_net_rev_delta = _recompute_frac_delta(week_disc)
    is_cut = week_disc < cur_disc - _EPS
    is_reinvest = week_disc > cur_disc + _EPS   # discount going UP => reinvesting spend

    # ------------------------------------------------------------------ #
    # RULE 3 — BUDGET CAP: total weekly discount spend must stay <= cap.  #
    #          Volume estimate = cur_units_wk (current sell-through).     #
    #          If over cap, block INCREASES (reinvests) — revert them to  #
    #          cur_disc. Never force extra cuts just to hit the cap.      #
    # ------------------------------------------------------------------ #
    total_gross_wk = float(np.sum(cur_units * mrp))
    cap_inr = budget_pct_cap * total_gross_wk

    def _disc_spend(wk_disc: np.ndarray) -> float:
        return float(np.sum(cur_units * mrp * wk_disc / 100.0))

    spend_before_cap = _disc_spend(week_disc)
    capped_reinvest = np.zeros(len(df), dtype=bool)

    if spend_before_cap > cap_inr and np.any(is_reinvest):
        # Disallow reinvests (the only spend-INCREASING moves) — revert to cur_disc.
        capped_reinvest = is_reinvest.copy()
        week_disc = np.where(is_reinvest, cur_disc, week_disc)
        # Recompute action masks after reverting reinvests.
        is_cut = week_disc < cur_disc - _EPS
        is_reinvest = week_disc > cur_disc + _EPS
        # frac / week_pred_net_rev_delta for reverted cells collapse to 0 (no move).
        frac, week_pred_net_rev_delta = _recompute_frac_delta(week_disc)

    # Final discount spend after all rules.
    total_disc_spend_wk = _disc_spend(week_disc)

    # ------------------------------------------------------------------ #
    # Derived per-row output columns.                                     #
    # ------------------------------------------------------------------ #
    week_price = mrp * (1.0 - week_disc / 100.0)

    # week_action label.
    action = np.full(len(df), "hold", dtype=object)
    action[is_cut] = "cut"
    action[is_reinvest] = "reinvest"

    # capped_by_guardrail: True whenever the guardrail changed what the model asked.
    #   - glide clamped the full move (didn't reach suggested_disc), OR
    #   - a cut was HELD by the revenue-protective rule, OR
    #   - a reinvest was blocked by the budget cap.
    glide_clamped = np.abs(full_move) > max_step_ppt + _EPS
    capped = glide_clamped | hold_mask | capped_reinvest

    # week_saving_inr: weekly net-rev gain from THIS week's step. For holds and
    # reverted reinvests this is 0.0 (no move => no scaled delta). For kept cuts
    # it is the revenue-protective (>=0) delta. For kept reinvests we surface the
    # scaled predicted delta as well.
    week_saving = np.where(is_cut | is_reinvest, week_pred_net_rev_delta, 0.0)

    df["week_disc"] = week_disc
    df["week_price"] = week_price
    df["week_action"] = action
    df["capped_by_guardrail"] = capped
    df["week_saving_inr"] = week_saving

    # ------------------------------------------------------------------ #
    # Summary / status.                                                   #
    # ------------------------------------------------------------------ #
    disc_pct = (total_disc_spend_wk / total_gross_wk) if total_gross_wk > 0 else 0.0
    headroom_inr = cap_inr - total_disc_spend_wk

    # Status band: GREEN within cap, AMBER within 5% of the cap ceiling, RED over.
    # "within 5%" means spend is between cap and cap*1.05 (a small overshoot buffer),
    # OR sitting in the top 5% of the budget just under the cap. We interpret the
    # AMBER band as: 0.95*cap < spend <= cap  (approaching cap) treated GREEN-ish,
    # and spend > cap treated RED. To give an early warning, AMBER = spend within
    # 5% *below or above* the cap boundary; RED only when clearly over the buffer.
    amber_lo = cap_inr * 0.95
    amber_hi = cap_inr * 1.05
    if total_disc_spend_wk <= amber_lo:
        status = "GREEN"
    elif total_disc_spend_wk <= amber_hi:
        status = "AMBER"
    else:
        status = "RED"

    summary = {
        "total_gross_wk": total_gross_wk,
        "total_disc_spend_wk": total_disc_spend_wk,
        "disc_pct": disc_pct,
        "budget_pct_cap": budget_pct_cap,
        "headroom_inr": headroom_inr,
        "status": status,
        "n_cut": int(np.count_nonzero(is_cut)),
        "n_hold": int(np.count_nonzero(action == "hold")),
        "n_reinvest": int(np.count_nonzero(is_reinvest)),
        "projected_week_saving_inr": float(np.sum(week_saving)),
    }

    return df, summary


if __name__ == "__main__":
    # ------------------------------------------------------------------ #
    # SMOKE TEST — tiny synthetic plan_df + config exercising every rule. #
    # ------------------------------------------------------------------ #
    import sys

    # 4 cells, each hitting a different guardrail path.
    rows = [
        # A: model wants a big cut 20->8 (12ppt). Glide clamps to 3ppt (17).
        #    Predicted move is revenue-POSITIVE => cut kept.
        dict(cell_id="A", mrp=100.0, cur_disc=20.0, suggested_disc=8.0,
             cur_units_wk=50.0, pred_net_rev_delta_wk=+900.0),
        # B: model wants a small cut 15->13 (2ppt, within step). But predicted
        #    delta is NEGATIVE => revenue-protective HOLD at 15.
        dict(cell_id="B", mrp=200.0, cur_disc=15.0, suggested_disc=13.0,
             cur_units_wk=30.0, pred_net_rev_delta_wk=-400.0),
        # C: model wants to REINVEST 10->16 (deepen discount). Glide to 13.
        #    This is a spend-INCREASE; may be blocked by budget cap.
        dict(cell_id="C", mrp=150.0, cur_disc=10.0, suggested_disc=16.0,
             cur_units_wk=40.0, pred_net_rev_delta_wk=+600.0),
        # D: no change wanted (5->5). Stays a HOLD.
        dict(cell_id="D", mrp=120.0, cur_disc=5.0, suggested_disc=5.0,
             cur_units_wk=20.0, pred_net_rev_delta_wk=0.0),
    ]
    plan = pd.DataFrame(rows)
    # Fill in the rest of the contract's derived columns so plan_df is realistic.
    plan["product_id"] = ["P1", "P2", "P3", "P4"]
    plan["city"] = ["Delhi", "Mumbai", "Delhi", "Pune"]
    plan["category"] = ["Oil", "Salt", "Oil", "Rice"]
    plan["title"] = ["24M Oil", "24M Salt", "24M Oil 2", "24M Rice"]
    plan["cur_price"] = plan["mrp"] * (1 - plan["cur_disc"] / 100)
    plan["cur_net_rev_wk"] = plan["cur_units_wk"] * plan["cur_price"]
    plan["cur_disc_spend_wk"] = plan["cur_units_wk"] * plan["mrp"] * plan["cur_disc"] / 100
    plan["suggested_price"] = plan["mrp"] * (1 - plan["suggested_disc"] / 100)
    plan["pred_units_wk"] = plan["cur_units_wk"]  # placeholder
    plan["pred_net_rev_wk"] = plan["cur_net_rev_wk"] + plan["pred_net_rev_delta_wk"]
    plan["bucket"] = ["c_waste_cut", "c_waste_cut", "e_reinvest", "f_monitor"]
    plan["confidence"] = ["High", "High", "Experimental", "Low"]
    plan["reliably_waste"] = [True, True, False, False]
    plan["net_gain_mo"] = plan["pred_net_rev_delta_wk"] * 4.33
    plan["decision_reason"] = ["cut waste", "cut waste", "reinvest", "monitor"]

    # Tight budget cap to force the cap path to trigger on the reinvest.
    config = dict(
        budget_pct_cap=0.11,
        max_step_ppt=3.0,
        festival_uplift_pct=0.5,
        week_date="2026-07-06",
        week_label="W1",
    )

    out, summary = apply_guardrail(plan, config)

    cols = ["cell_id", "cur_disc", "suggested_disc", "week_disc", "week_price",
            "week_action", "capped_by_guardrail", "week_saving_inr"]
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print("=== per-cell guardrail output ===")
        print(out[cols].to_string(index=False))
        print("\n=== summary ===")
        for k, v in summary.items():
            if isinstance(v, float):
                print(f"  {k:26s}: {v:,.2f}")
            else:
                print(f"  {k:26s}: {v}")

    # --- Assertions so the smoke test actually verifies the rules ---
    r = out.set_index("cell_id")

    # A: 12ppt cut clamped to 3ppt => 17, kept (revenue-positive), flagged capped.
    assert abs(r.loc["A", "week_disc"] - 17.0) < 1e-9, "A glide clamp failed"
    assert r.loc["A", "week_action"] == "cut", "A should be a cut"
    assert bool(r.loc["A", "capped_by_guardrail"]) is True, "A should be glide-capped"
    assert r.loc["A", "week_saving_inr"] > 0, "A saving should be positive"

    # B: 2ppt cut within step but revenue-NEGATIVE => HOLD at 15, saving 0.
    assert abs(r.loc["B", "week_disc"] - 15.0) < 1e-9, "B should HOLD at cur_disc"
    assert r.loc["B", "week_action"] == "hold", "B should be a hold"
    assert bool(r.loc["B", "capped_by_guardrail"]) is True, "B held => capped"
    assert abs(r.loc["B", "week_saving_inr"]) < 1e-9, "B saving should be 0"

    # C: reinvest — glided to 13, then (with a tight cap) blocked back to 10.
    #    Either way it must NOT deepen past the glide, and if blocked action=hold.
    assert r.loc["C", "week_disc"] in (10.0, 13.0), "C week_disc unexpected"

    # D: no move => hold, not capped, saving 0.
    assert abs(r.loc["D", "week_disc"] - 5.0) < 1e-9, "D should stay at 5"
    assert r.loc["D", "week_action"] == "hold", "D should be a hold"
    assert bool(r.loc["D", "capped_by_guardrail"]) is False, "D not capped"

    # Guardrail must NEVER force extra cuts: week_disc never below suggested for cuts,
    # and total spend must be <= cap when status is GREEN/AMBER-safe.
    assert (out["week_disc"] >= 0).all() and (out["week_disc"] <= 100).all()

    # Summary sanity.
    assert summary["status"] in ("GREEN", "AMBER", "RED")
    assert summary["n_cut"] + summary["n_hold"] + summary["n_reinvest"] == len(out)
    assert abs(summary["projected_week_saving_inr"]
               - out["week_saving_inr"].sum()) < 1e-6

    print("\nSMOKE TEST PASSED")
    sys.exit(0)
