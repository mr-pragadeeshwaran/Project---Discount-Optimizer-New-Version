"""
optimize_plan.py — FULL discount optimization (reinvest + cut) for the ₹5L goal.

For every product×city cell, move discount to its net-revenue-maximizing level
*within the observed discount range* (no extrapolation), using the validated
confounder-controlled quadratic response. Split the total net-revenue gain into:
  - REINVEST  : cells where the optimum is ABOVE current (discount pays → scale up)
  - CUT       : cells where the optimum is BELOW current (waste → trim, keep sales)
Reports the total achievable net-revenue improvement vs the ₹5,00,000/mo target.

Honesty rails:
  * reinvest only where the discount effect is reliably positive (sig_pos) and the
    cell is NOT a stock/competitive problem (never scale discount on a broken cell);
  * optimum capped to the category's observed p95 discount — no betting beyond data;
  * cuts use the anti-phantom clamp (cutting never modeled to raise units).
"""
import os, sys, glob, json
import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
import importlib.util
spec = importlib.util.spec_from_file_location("dp", os.path.join(ROOT, "scripts/analysis/discount_plan.py"))
dp = importlib.util.module_from_spec(spec); spec.loader.exec_module(dp)

TARGET = 500_000
MONTH = 30.0 / 7.0


def main():
    run, fact = dp._latest_facttable()
    panel = dp.build_panel(fact)
    models, _ = dp.fit_models(panel)
    df = pd.read_csv(os.path.join(run, "plan", "all_cells.csv"))

    # observed discount cap per category (p95) — reinvest never extrapolates past this
    cap = panel.groupby("category")["disc"].quantile(0.95).to_dict()
    b1 = {c: m.get("beta_disc", np.nan) for c, m in models.items()}
    b2 = {c: (m.get("beta_disc2", 0.0) if m.get("ok") else 0.0) for c, m in models.items()}

    def optimal(cat, cur):
        m = models.get(cat, {})
        if not m.get("ok"):
            return cur, 0.0
        lo, hi = 0.0, float(min(cap.get(cat, cur), 60.0))
        hi = max(hi, cur)                      # allow holding current at least
        grid = np.arange(lo, hi + 0.25, 0.25)
        rr = (1 - grid/100.0) * dp._units_factor(grid, b1[cat], b2[cat])
        d = float(grid[int(np.argmax(rr))])
        return d, hi

    rows = []
    for _, r in df.iterrows():
        cat, cur, u, mrp = r["category"], r["cur_disc"], r["cur_units_wk"], r["mrp"]
        d_star, hicap = optimal(cat, cur)
        uf_cur = dp._units_factor(cur, b1.get(cat, 0), b2.get(cat, 0.0))
        uf_new = dp._units_factor(d_star, b1.get(cat, 0), b2.get(cat, 0.0))
        ratio  = uf_new / max(uf_cur, 1e-9)
        is_up = d_star > cur + 0.5
        is_dn = d_star < cur - 0.5
        # ANTI-PHANTOM CLAMP: cutting discount can never be modeled to RAISE units.
        # (kills the reverse-causality free lunch on negative-beta cells)
        if is_dn:
            ratio = min(ratio, 1.0)
        nr_cur = mrp * (1 - cur/100.0) * u
        nr_new = mrp * (1 - d_star/100.0) * u * ratio
        spend_cur = mrp * (cur/100.0) * u
        spend_new = mrp * (d_star/100.0) * u * ratio
        delta_nr_mo = (nr_new - nr_cur) * MONTH
        delta_spend_mo = (spend_new - spend_cur) * MONTH

        stock_or_comp = r["bucket"] in ("a_stock", "b_competitive")
        # Confident actions reuse the VALIDATED bucketing:
        #   reinvest = reliable positive response, optimum above current, not broken
        #   cut      = the validated waste bucket (clamped, gated, no confounder drag)
        if is_up and r["sig_pos"] and not stock_or_comp:
            action = "reinvest"
        elif r["bucket"] == "c_waste_cut":
            action = "cut"; delta_nr_mo = r["net_gain_mo"]  # validated clamped value
        elif is_dn and not stock_or_comp and delta_nr_mo > 0:
            # would-be cut on a weak/unreliable-beta cell: NOT bankable, test-to-unlock
            action = "test_cut"
        else:
            action = "hold"; delta_nr_mo = 0.0; delta_spend_mo = 0.0; d_star = cur
        rows.append({**r.to_dict(), "opt_disc": round(d_star, 1), "cap_disc": round(hicap, 1),
                     "action2": action, "delta_nr_mo": round(delta_nr_mo, 0),
                     "delta_spend_mo": round(delta_spend_mo, 0)})

    o = pd.DataFrame(rows)
    rein = o[o["action2"] == "reinvest"]; cutd = o[o["action2"] == "cut"]; test = o[o["action2"] == "test_cut"]
    def tot(x): return float(x["delta_nr_mo"].clip(lower=0).sum())
    rein_hi = rein[rein["confidence"] == "High"]; cut_hi = cutd[cutd["confidence"] == "High"]
    confident_hi  = tot(rein_hi) + tot(cut_hi)
    confident_all = tot(rein) + tot(cutd)
    unlock = tot(test)                                   # requires validation, NOT banked
    cur_spend = float((df["mrp"] * df["cur_disc"]/100.0 * df["cur_units_wk"]).sum() * MONTH)

    o.to_csv(os.path.join(run, "plan", "optimization.csv"), index=False)
    print(f"[opt] run {os.path.basename(run)} | current discount spend ≈ ₹{cur_spend:,.0f}/mo\n")
    print(f"  CONFIDENT (bankable) net-revenue improvement:")
    print(f"    REINVEST  {len(rein):3d} cells → +₹{tot(rein):>9,.0f}/mo  (Oil/Salt/Daliya scale-up, within observed range)")
    print(f"    CUT       {len(cutd):3d} cells → +₹{tot(cutd):>9,.0f}/mo  (validated waste, sales kept)")
    print(f"    ── confident total: High ₹{confident_hi:,.0f}/mo | all ₹{confident_all:,.0f}/mo")
    print(f"\n  TEST-TO-UNLOCK (NOT banked — coefficients unstable, must validate by experiment):")
    print(f"    {len(test)} cells where the model can't detect discount driving sales.")
    print(f"    IF volume truly holds when trimmed: up to +₹{unlock:,.0f}/mo — but bootstrap stability is 23–47%.")
    print(f"    This is a test-and-learn pipeline, not a saving you can bank today.\n")
    print(f"  vs ₹5,00,000/mo target:")
    print(f"    confident (all-conf): ₹{confident_all:,.0f}/mo = {confident_all/TARGET*100:.0f}% of target → "
          f"{'MET' if confident_all>=TARGET else 'BELOW'}")
    print(f"    confident + full unlock ceiling: ₹{confident_all+unlock:,.0f}/mo "
          f"({'≥' if confident_all+unlock>=TARGET else '<'} target, but unlock is unconfirmed)")
    print("\n  top REINVEST (scale up where discount reliably pays):")
    for _, x in rein.nlargest(6, "delta_nr_mo").iterrows():
        print(f"    {x['title'].replace('24 Mantra Organic ','')[:26]:26s} {x['city'][:10]:10s} "
              f"{x['cur_disc']:.0f}%→{x['opt_disc']:.0f}% (cap {x['cap_disc']:.0f}%)  +₹{x['delta_nr_mo']:,.0f}/mo")
    return dict(confident_all=confident_all, confident_hi=confident_hi, unlock=unlock, target=TARGET)


if __name__ == "__main__":
    main()
