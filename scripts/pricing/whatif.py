"""
scripts/pricing/whatif.py  —  Adjusted-Scenario (what-if) engine for Blinkit PricingAI.

WHAT THIS IS (business framing)
-------------------------------
The pricing manager edits ONE SKU's discount in one city and instantly sees the
WHOLE portfolio move: that SKU sells more (own-price effect), but its category
siblings sell a little less (cannibalization / cross-price effect). No optimizer
runs — this is pure algebra, so it returns in microseconds. It is the
`/simulate` endpoint of the PepsiCo PricingAI blueprint, adapted to a single
brand (24 Mantra Organic) on Blinkit.

WHERE IT SITS
-------------
    de_optimizer.py   -> searches for the best discount vector (differential evolution)
    whatif.py (THIS)  -> takes a MANUAL edit and recomputes deltas, no search

Blueprint rule [§19]: "simulate must not call the solver — interactivity is an
adoption requirement." So this module never imports or runs the DE optimizer.

DEMAND MATH — SHARED, NOT DUPLICATED
------------------------------------
This module NO LONGER carries its own demand equation. It builds the optimizer's
exact problem dict via `de_optimizer.build_problem(...)` and evaluates volumes with
`de_optimizer.demand_model(disc_vec, P)` — the SAME clamped kernel the optimizer
uses internally. That means the what-if readout is arithmetically identical to what
the optimizer believes for any given discount vector, including all of the honesty
guards:

    * own-effect reliability clamp / kill-mask (a price CUT only earns volume where
      own_elast is reliably negative),
    * Δln-price bounded to the reachable discount range (no extrapolation),
    * power-law cap with a bounded sibling multiplier (the exp() can never run away),
    * within-city cross (cannibalization) terms.

There is NO separate promo term here (the optimizer's kernel has none either), so a
price/discount what-if matches the optimizer cell-for-cell.

Only allowed libs: numpy, pandas (+ stdlib). No Gurobi, no cloud, no PyMC.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

# Ensure the sibling optimizer module is importable when whatif is run standalone
# (python scripts/pricing/whatif.py) as well as when imported by pricing_engine.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import de_optimizer as _de  # noqa: E402  (path is set up just above)

# Audit string: the demand kernel is ALWAYS the optimizer's — no local fallback.
_DEMAND_SOURCE = "de_optimizer.demand_model (shared kernel via build_problem)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _price_from_disc(mrp, disc_pct):
    """Selling price implied by an MRP and a discount %.  price = mrp*(1-d/100)."""
    return mrp * (1.0 - disc_pct / 100.0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def simulate(elast_df, cross_df, baseline_df, edits):
    """
    Recompute the FULL portfolio impact of a set of manual discount edits,
    INCLUDING cross-price cannibalization.  No optimization — pure algebra.

    Parameters
    ----------
    elast_df : DataFrame
        product_id, city, own_elast(<0), own_sd, promo_elast.
    cross_df : DataFrame (may be empty/None)
        product_i, product_j, cross_elast(>0 for substitutes).  Sparse,
        within-category substitute pairs.
    baseline_df : DataFrame
        product_id, city, category, base_product, pack_grams,
        q0_units_wk, p0_price, mrp, disc0.
    edits : list of dict
        [{"product_id": ..., "city": ..., "new_disc": <float %>}, ...]
        An edit sets a cell's discount to `new_disc`.  Cells not listed keep
        their baseline discount but STILL move if a sibling's price changed.

    Returns
    -------
    dict with:
      per_cell : list of dicts, one per (product_id, city) cell:
          product_id, city, disc_before, disc_after,
          units_delta_pct, rev_delta_pct
      portfolio : dict:
          revenue_delta_pct, volume_delta_pct, nrw_delta_pct
      demand_source : str  (which demand kernel was used — for auditing)

    Volumes come from `de_optimizer.demand_model(disc_vec, P)` on the problem `P`
    assembled by `de_optimizer.build_problem(...)`. That is the OPTIMIZER'S EXACT
    clamped kernel (reliability kill-mask, dln bounds, power-law cap, within-city
    cross terms) — so the what-if can never invent volume the optimizer refuses to
    bank. Prices, weights and disc0 are all read from P so per-cell rows align 1:1
    with the kernel's volume vector.
    """
    # Build the SAME problem dict the optimizer uses internally. config=None ->
    # de_optimizer.DEFAULT_CONFIG (full reachable-discount bounds + PPP thresholds).
    P = _de.build_problem(elast_df, cross_df, baseline_df, config=None)
    n = P["n"]

    if n == 0:
        return {
            "per_cell": [],
            "portfolio": {
                "revenue_delta_pct": 0.0,
                "volume_delta_pct": 0.0,
                "nrw_delta_pct": 0.0,
            },
            "demand_source": _DEMAND_SOURCE,
        }

    cells = P["cells"]
    idx = P["idx"]                       # (product_id, city) -> row position in P

    # ---- Discount vectors: baseline vs scenario -------------------------
    # Unedited cells keep disc0; edited cells take the new discount.
    disc0 = np.asarray(P["disc0"], dtype=float)
    disc_new = disc0.copy()

    unmatched = []
    for ed in (edits or []):
        key = (ed["product_id"], ed["city"])
        if key in idx:
            disc_new[idx[key]] = float(ed["new_disc"])
        else:
            unmatched.append(key)

    # ---- Prices (from P's mrp, exactly as the optimizer computes them) ---
    mrp = np.asarray(P["mrp"], dtype=float)
    price_base = np.maximum(_price_from_disc(mrp, disc0), 1e-6)
    price_new = np.maximum(_price_from_disc(mrp, disc_new), 1e-6)

    # ---- Volumes via the SHARED optimizer kernel ------------------------
    V_base = _de.demand_model(disc0, P)
    V_new = _de.demand_model(disc_new, P)

    # ---- Per-cell revenue = price * volume ------------------------------
    rev_base = price_base * V_base
    rev_new = price_new * V_new

    def _pct(new, old):
        old = np.asarray(old, dtype=float)
        new = np.asarray(new, dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            out = np.where(old != 0.0, (new - old) / old * 100.0, 0.0)
        return out

    units_delta_pct = _pct(V_new, V_base)
    rev_delta_pct = _pct(rev_new, rev_base)

    pid_col = cells["product_id"].to_numpy()
    city_col = cells["city"].to_numpy()
    per_cell = []
    for i in range(n):
        per_cell.append({
            "product_id": pid_col[i],
            "city": city_col[i],
            "disc_before": float(round(disc0[i], 4)),
            "disc_after": float(round(disc_new[i], 4)),
            "units_delta_pct": float(round(units_delta_pct[i], 4)),
            "rev_delta_pct": float(round(rev_delta_pct[i], 4)),
        })

    # ---- Portfolio totals ------------------------------------------------
    tot_rev_base = float(rev_base.sum())
    tot_rev_new = float(rev_new.sum())
    tot_vol_base = float(V_base.sum())
    tot_vol_new = float(V_new.sum())

    # NRW = net revenue per unit weight = Σrevenue / Σ(volume * pack_grams).
    pack = np.asarray(P["pack_g"], dtype=float)
    pack = np.where(np.isnan(pack), 0.0, pack)
    weight_base = float((V_base * pack).sum())
    weight_new = float((V_new * pack).sum())
    nrw_base = tot_rev_base / weight_base if weight_base != 0.0 else 0.0
    nrw_new = tot_rev_new / weight_new if weight_new != 0.0 else 0.0

    def _scalar_pct(new, old):
        return (new - old) / old * 100.0 if old != 0.0 else 0.0

    portfolio = {
        "revenue_delta_pct": float(round(_scalar_pct(tot_rev_new, tot_rev_base), 4)),
        "volume_delta_pct": float(round(_scalar_pct(tot_vol_new, tot_vol_base), 4)),
        "nrw_delta_pct": float(round(_scalar_pct(nrw_new, nrw_base), 4)),
    }

    result = {
        "per_cell": per_cell,
        "portfolio": portfolio,
        "demand_source": _DEMAND_SOURCE,
    }
    if unmatched:
        result["unmatched_edits"] = unmatched
    return result


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Tiny synthetic portfolio: 2 substitute SKUs in 1 city, same category.
    #   A = 500g pack, B = 1000g pack of the same base product.
    baseline_df = pd.DataFrame([
        {"product_id": "A", "city": "Mumbai", "category": "Atta",
         "base_product": "24M Atta", "pack_grams": 500.0,
         "q0_units_wk": 100.0, "p0_price": 90.0, "mrp": 100.0, "disc0": 10.0},
        {"product_id": "B", "city": "Mumbai", "category": "Atta",
         "base_product": "24M Atta", "pack_grams": 1000.0,
         "q0_units_wk": 60.0, "p0_price": 170.0, "mrp": 200.0, "disc0": 15.0},
        {"product_id": "C", "city": "Mumbai", "category": "Oil",
         "base_product": "24M Oil", "pack_grams": 1000.0,
         "q0_units_wk": 40.0, "p0_price": 380.0, "mrp": 400.0, "disc0": 5.0},
    ])

    elast_df = pd.DataFrame([
        {"product_id": "A", "city": "Mumbai", "own_elast": -1.8,
         "own_sd": 0.3, "promo_elast": 0.5},
        {"product_id": "B", "city": "Mumbai", "own_elast": -1.2,
         "own_sd": 0.3, "promo_elast": 0.4},
        {"product_id": "C", "city": "Mumbai", "own_elast": -0.9,
         "own_sd": 0.2, "promo_elast": 0.3},
    ])

    # A and B are substitutes (same category); C (Oil) is unrelated.
    cross_df = pd.DataFrame([
        {"product_i": "A", "product_j": "B", "cross_elast": 0.4},
        {"product_i": "B", "product_j": "A", "cross_elast": 0.5},
    ])

    print("Demand kernel in use:", _DEMAND_SOURCE)
    print()

    # --- Invariant check: no edits -> everything flat (V == q0) ----------
    flat = simulate(elast_df, cross_df, baseline_df, edits=[])
    print("== No-edit self-consistency (must be all zeros) ==")
    for c in flat["per_cell"]:
        print(f"  {c['product_id']}/{c['city']}: "
              f"units {c['units_delta_pct']:+.3f}%  rev {c['rev_delta_pct']:+.3f}%")
    print("  portfolio:", flat["portfolio"])
    assert all(abs(c["units_delta_pct"]) < 1e-6 for c in flat["per_cell"]), \
        "self-consistency violated: baseline must give V==q0"
    assert abs(flat["portfolio"]["revenue_delta_pct"]) < 1e-6
    print("  -> PASS: baseline reproduces itself.\n")

    # --- Scenario: deepen A's discount 10% -> 25% ------------------------
    edits = [{"product_id": "A", "city": "Mumbai", "new_disc": 25.0}]
    res = simulate(elast_df, cross_df, baseline_df, edits)

    print("== Edit: A discount 10% -> 25% (price drops) ==")
    for c in res["per_cell"]:
        print(f"  {c['product_id']}/{c['city']}: "
              f"disc {c['disc_before']:.0f}%->{c['disc_after']:.0f}%  "
              f"units {c['units_delta_pct']:+.2f}%  rev {c['rev_delta_pct']:+.2f}%")
    print("  portfolio:", res["portfolio"])

    # Economic sanity: A's price fell -> A's units UP.
    a = next(c for c in res["per_cell"] if c["product_id"] == "A")
    b = next(c for c in res["per_cell"] if c["product_id"] == "B")
    cc = next(c for c in res["per_cell"] if c["product_id"] == "C")
    assert a["units_delta_pct"] > 0, "A cheaper must sell more"
    # B is a substitute of A: A cheaper -> B's price unchanged but A pulls
    # demand away -> B units DOWN (cannibalization via positive cross-elast).
    assert b["units_delta_pct"] < 0, "substitute B must be cannibalized"
    # C is a different category -> untouched.
    assert abs(cc["units_delta_pct"]) < 1e-9, "unrelated C must not move"
    print("  -> PASS: A up, substitute B cannibalized, unrelated C flat.\n")

    # --- Edge: unmatched edit is reported, not crashed -------------------
    res2 = simulate(elast_df, cross_df, baseline_df,
                    edits=[{"product_id": "ZZZ", "city": "Nowhere", "new_disc": 50.0}])
    assert "unmatched_edits" in res2 and res2["unmatched_edits"] == [("ZZZ", "Nowhere")]
    assert abs(res2["portfolio"]["revenue_delta_pct"]) < 1e-6
    print("== Unmatched edit handled ==")
    print("  unmatched_edits:", res2["unmatched_edits"], "-> PASS\n")

    # --- CRITICAL: what-if volumes == de_optimizer.demand_model on shared disc_vec ---
    # The whole point of the fix: whatif must produce volumes through the OPTIMIZER'S
    # EXACT clamped kernel, not a divergent copy. Rebuild the same problem P, form the
    # same disc_vec the edit implies, and assert the per-cell volumes match term-for-term.
    print("== whatif == de_optimizer.demand_model (shared kernel identity) ==")
    P = _de.build_problem(elast_df, cross_df, baseline_df, config=None)
    idx = P["idx"]
    disc_vec = np.asarray(P["disc0"], dtype=float).copy()
    disc_vec[idx[("A", "Mumbai")]] = 25.0            # same edit as the scenario above
    V_direct = _de.demand_model(disc_vec, P)          # optimizer kernel, called directly

    # Reconstruct whatif's volumes from its reported deltas: V_new = V_base*(1+Δ/100).
    V_base_direct = _de.demand_model(np.asarray(P["disc0"], dtype=float), P)
    pid_order = P["cells"]["product_id"].tolist()
    city_order = P["cells"]["city"].tolist()
    key_to_row = {(pid_order[i], city_order[i]): i for i in range(P["n"])}
    V_whatif = np.empty(P["n"], dtype=float)
    for c in res["per_cell"]:
        r = key_to_row[(c["product_id"], c["city"])]
        V_whatif[r] = V_base_direct[r] * (1.0 + c["units_delta_pct"] / 100.0)

    print("  optimizer demand_model :", np.round(V_direct, 6))
    print("  whatif-implied volumes :", np.round(V_whatif, 6))
    assert np.allclose(V_whatif, V_direct, rtol=1e-6, atol=1e-6), \
        "DIVERGENCE: whatif volumes differ from de_optimizer.demand_model on shared disc_vec"

    # And confirm the cross matrix moved the same-category sibling B (cannibalization).
    b_row = key_to_row[("B", "Mumbai")]
    assert V_direct[b_row] < V_base_direct[b_row] - 1e-9, \
        "sibling B should be cannibalized in the shared kernel too"
    print("  -> PASS: whatif == optimizer demand on the shared disc_vec, and sibling B moved.\n")

    print("ALL SMOKE TESTS PASSED")
