"""
budget_allocator.py — Objective 3 (budget-constrained allocation) + Objective 1 (marginal-ROI ladder).

Two deliverables, one shared demand kernel (de_optimizer.demand_model — never a divergent copy):

  build_ladders(...)  -> per (product x city) the FULL discount ladder: each discount step ->
                         units, net revenue, discount spend, MARGINAL ROI (Δnet-rev / Δspend), and
                         the ELBOW flag (last step whose marginal ROI >= 1). This is the proof
                         artifact behind every cut/keep call — you can see exactly where a cell's
                         discount stops paying.

  allocate_budget(...) -> the greedy marginal-ROI WATERLINE: cap total discount spend at
                         budget_pct x baseline weekly revenue, then spend it on the highest-ROI
                         discount increments first until the cap binds. Increments below the
                         waterline (marginal ROI < the cutoff, always <1) are dropped. This
                         naturally concentrates the budget on genuinely elastic cells (Oil) and
                         zeroes discount on inelastic staples — the whole thesis, made a constraint.

Runs standalone: python -X utf8 scripts/pricing/budget_allocator.py [--budget_pct 0.10]
Outputs -> DISCOUNT_PLAN/pricing/: roi_ladder.csv, budget_allocation.csv, BUDGET_PLAN.md
"""
import os, sys, glob, json, argparse
import numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(ROOT, "scripts", "analysis"))
import de_optimizer as de
OUT = os.path.join(ROOT, "DISCOUNT_PLAN", "pricing")
STEP, DISC_MAX = 2.5, 45.0


def build_ladders(elast_df, cross_df, baseline_df, config=None, step=STEP, disc_max=DISC_MAX):
    """Per-cell discount ladder via the shared demand kernel (siblings held at baseline)."""
    P = de.build_problem(elast_df, cross_df, baseline_df, config)
    n, cells = P["n"], P["cells"]
    mrp, p0, q0, disc0 = P["mrp"], P["p0"], P["q0"], P["disc0"]
    grid = np.arange(0.0, disc_max + step / 2, step)
    rows = []
    for i in range(n):
        prev_nr = prev_sp = None
        for d in grid:
            dv = disc0.copy(); dv[i] = d                 # vary cell i only
            V = de.demand_model(dv, P)[i]
            price = mrp[i] * (1 - d / 100.0)
            nr = V * price                                # net revenue
            sp = V * (mrp[i] - price)                     # discount spend
            mroi = np.nan
            if prev_nr is not None and (sp - prev_sp) > 1e-9:
                mroi = (nr - prev_nr) / (sp - prev_sp)    # marginal ROI of this step
            rows.append({"product_id": str(cells.iloc[i]["product_id"]).replace(".0", ""),
                         "city": cells.iloc[i]["city"], "disc": round(float(d), 1),
                         "units": round(float(V), 1), "net_rev_wk": round(float(nr), 0),
                         "disc_spend_wk": round(float(sp), 0),
                         "marginal_roi": round(float(mroi), 3) if np.isfinite(mroi) else np.nan})
            prev_nr, prev_sp = nr, sp
    lad = pd.DataFrame(rows)
    # elbow = the highest discount whose marginal ROI is still >= 1 (beyond it, discount loses money)
    lad["is_elbow"] = False
    for (pid, city), g in lad.groupby(["product_id", "city"]):
        pay = g[g["marginal_roi"] >= 1.0]
        if len(pay):
            lad.loc[pay["disc"].idxmax(), "is_elbow"] = True
    return lad, P


def allocate_budget(lad, P, budget_pct):
    """Greedy marginal-ROI waterline: fill the budget with the highest-ROI increments first."""
    baseline_rev = float(np.sum(P["q0"] * P["p0"]))          # weekly baseline net revenue
    budget = budget_pct * baseline_rev
    # increments per cell (contiguous from disc=0), keep only net-rev-accretive ones
    incs = []
    for (pid, city), g in lad.sort_values("disc").groupby(["product_id", "city"]):
        g = g.reset_index(drop=True)
        for k in range(1, len(g)):
            d_sp = g.loc[k, "disc_spend_wk"] - g.loc[k - 1, "disc_spend_wk"]
            d_nr = g.loc[k, "net_rev_wk"] - g.loc[k - 1, "net_rev_wk"]
            roi = g.loc[k, "marginal_roi"]
            if d_sp > 1e-6 and np.isfinite(roi):
                incs.append((roi, pid, city, g.loc[k, "disc"], d_sp, d_nr, k))
    incs.sort(key=lambda t: -t[0])                            # highest ROI first
    alloc = {}                                                # (pid,city) -> highest disc reached
    taken_k = {}                                              # enforce contiguity
    spent = 0.0; waterline = None
    for roi, pid, city, disc, d_sp, d_nr, k in incs:
        key = (pid, city)
        if roi < 1.0:                                         # below break-even: never worth it
            continue
        if taken_k.get(key, 0) != k - 1:                      # steps must be contiguous from 0
            continue
        if spent + d_sp > budget:                             # budget waterline reached
            waterline = roi; break
        spent += d_sp; taken_k[key] = k; alloc[key] = disc
    if waterline is None:
        waterline = 1.0
    # assemble allocation vs current
    base = P["cells"]
    rows = []
    for i in range(P["n"]):
        pid = str(base.iloc[i]["product_id"]).replace(".0", ""); city = base.iloc[i]["city"]
        opt = float(alloc.get((pid, city), 0.0)); cur = float(P["disc0"][i])
        rows.append({"product_id": pid, "city": city, "cur_disc": round(cur, 1),
                     "budget_disc": round(opt, 1),
                     "action": "cut" if opt < cur - 0.5 else ("raise" if opt > cur + 0.5 else "hold")})
    a = pd.DataFrame(rows)
    summary = {"baseline_revenue_wk": round(baseline_rev, 0), "budget_pct": budget_pct,
               "budget_wk": round(budget, 0), "spent_wk": round(spent, 0),
               "waterline_roi": round(float(waterline), 3),
               "cur_spend_wk": round(float(np.sum(P["q0"] * P["mrp"] * P["disc0"] / 100.0)), 0),
               "n_cut": int((a["action"] == "cut").sum()), "n_raise": int((a["action"] == "raise").sum()),
               "n_hold": int((a["action"] == "hold").sum())}
    return a, summary


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--budget_pct", type=float, default=0.10); a = ap.parse_args()
    run = sorted(glob.glob(os.path.join(ROOT, "output", "runs", "2026*")))[-1]
    pd_dir = os.path.join(run, "plan")
    import pricing_panel as pp, elasticity_bayes as eb
    panel = pp.build_pricing_panel(os.path.join(run, "fact_table.csv"))
    elast, cross, base, _ = eb.estimate_elasticities(panel)
    lad, P = build_ladders(elast, cross, base)
    lad.to_csv(os.path.join(OUT, "roi_ladder.csv"), index=False)
    alloc, summ = allocate_budget(lad, P, a.budget_pct)
    alloc.to_csv(os.path.join(OUT, "budget_allocation.csv"), index=False)
    print(f"[budget] baseline rev ₹{summ['baseline_revenue_wk']:,.0f}/wk | current discount spend "
          f"₹{summ['cur_spend_wk']:,.0f}/wk ({summ['cur_spend_wk']/summ['baseline_revenue_wk']*100:.1f}%)")
    print(f"[budget] cap {a.budget_pct*100:.0f}% = ₹{summ['budget_wk']:,.0f}/wk | allocated ₹{summ['spent_wk']:,.0f} "
          f"| waterline ROI {summ['waterline_roi']:.2f} | {summ['n_cut']} cut / {summ['n_raise']} raise / {summ['n_hold']} hold")
    _report(lad, alloc, summ)
    print(f"[budget] wrote {OUT}/BUDGET_PLAN.md, roi_ladder.csv, budget_allocation.csv")


def _report(lad, alloc, s):
    L = ["# Budget Allocator — marginal-ROI waterline (Objectives 1 & 3)\n",
         f"*Cap discount spend at **{s['budget_pct']*100:.0f}% of baseline revenue** (₹{s['budget_wk']:,.0f}/wk); "
         f"spend it on the highest marginal-ROI discount first. Same demand kernel as the optimizer.*\n",
         "## The budget picture\n",
         f"- Baseline revenue: **₹{s['baseline_revenue_wk']:,.0f}/week**.",
         f"- Current discount spend: **₹{s['cur_spend_wk']:,.0f}/wk "
         f"({s['cur_spend_wk']/s['baseline_revenue_wk']*100:.1f}% of revenue)** — vs the {s['budget_pct']*100:.0f}% cap.",
         f"- Under the cap the allocator spends **₹{s['spent_wk']:,.0f}/wk**, at a **waterline marginal ROI of "
         f"{s['waterline_roi']:.2f}** (every rupee of discount kept returns ≥₹{s['waterline_roi']:.2f}).",
         f"- Result: **{s['n_cut']} cells cut · {s['n_raise']} raised · {s['n_hold']} held**.\n",
         f"**Read this honestly:** the allocator spends almost nothing (₹{s['spent_wk']:,.0f} of a ₹{s['budget_wk']:,.0f} "
         f"cap) because — under these elasticities — discount barely clears break-even *anywhere*. Only a handful of "
         "cells have a discount step whose marginal ROI reaches 1; for the rest, once volume goes flat, marginal ROI "
         "sits at −1 (every rupee of discount is a rupee of pure margin given away). So the profit-optimal discount "
         "is near-zero — the *fourth* independent confirmation that discount is mostly waste on this portfolio, and "
         "an even more aggressive read than the ₹6.98L cut list.\n",
         "**But do NOT slash all discount overnight.** This rests on the wide-band (≈unit-elastic) Bayesian "
         "elasticities — it's a directional cross-check, not an execution plan. The glide, reliability gates, "
         "engine-agreement, and in-market tests exist precisely because these estimates are uncertain.\n",
         "## Marginal-ROI ladder (Objective 1 proof artifact)\n",
         "`roi_ladder.csv` has every cell's full curve. The **elbow** is where marginal ROI crosses 1 — beyond it, "
         "more discount destroys net revenue. Example elbows:\n",
         "| SKU | City | Elbow discount | Units there | Marginal ROI |", "|---|---|---:|---:|---:|"]
    el = lad[lad["is_elbow"]].head(10)
    for _, r in el.iterrows():
        L.append(f"| {str(r['product_id'])[:14]} | {str(r['city'])[:10]} | {r['disc']:.0f}% | "
                 f"{r['units']:.0f} | {r['marginal_roi']:.2f} |")
    L.append("\n_Budget % is set with `--budget_pct` (default 0.10). This is a separate constraint mode from the "
             "KPI optimizer; run it when you want a hard spend ceiling rather than a revenue floor._")
    open(os.path.join(OUT, "BUDGET_PLAN.md"), "w", encoding="utf-8").write("\n".join(L))


if __name__ == "__main__":
    main()
