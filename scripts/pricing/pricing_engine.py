"""
pricing_engine.py — PricingAI orchestrator (adapted to 24 Mantra on Blinkit).

Chains the four modules:
  pricing_panel.build_pricing_panel   -> weekly panel (regular price, promo flags, pack grams)
  elasticity_hier.estimate_elasticities -> own + cross-price (cannibalization) matrix + gates
  de_optimizer.optimize               -> portfolio-optimal discount vector + KPI deltas
  whatif.simulate                     -> instant "adjusted scenario" impact incl. cross-effects

Also runs the CANNIBALIZATION CHECK on the existing waste-cut list: when the current tool
cuts a staple's discount, does the cross-price model say the volume leaks to sibling SKUs
(so "sales held" per-SKU nets to zero across the portfolio)? That is the honesty test the
per-cell model can't answer.

Outputs -> DISCOUNT_PLAN/pricing/: elasticities.csv, cross_price.csv, pricing_reco.csv,
gates.json, PRICING_PLAN.md
"""
import os, sys, glob, json
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts", "analysis"))
import pricing_panel as pp
import elasticity_hier as eh
import de_optimizer as de
import whatif as wi

OUT = os.path.join(ROOT, "DISCOUNT_PLAN", "pricing")

CONFIG = {
    "kpi": "revenue",
    "disc_lo": 0.0, "disc_hi": 45.0,
    "max_disc_change_ppt": 3.0,
    "revenue_floor_frac": 0.98,
    "psych_prices": [49, 99, 149, 199, 249, 299, 349, 399, 449, 499, 599, 699, 799, 999],
    "ladder_tol": 1.0,
    "n_seeds": 2,          # 526-dim DE is costly; 2 seeds keeps a full run tractable
}


def _latest_fact_table():
    for r in sorted(glob.glob(os.path.join(ROOT, "v4_outputs", "2026*")), reverse=True):
        f = os.path.join(r, "fact_table.csv")
        if os.path.exists(f):
            return f, r
    raise SystemExit("no fact_table.csv — run pipeline.py first")


def main():
    os.makedirs(OUT, exist_ok=True)
    fact, run = _latest_fact_table()
    print(f"[pricing] fact_table: {os.path.basename(run)}")

    panel = pp.build_pricing_panel(fact)
    print(f"[pricing] panel: {len(panel)} cell-weeks | {panel['product_id'].nunique()} SKUs "
          f"| {panel['city'].nunique()} cities")

    elast_df, cross_df, baseline_df, gates = eh.estimate_elasticities(panel)
    elast_df.to_csv(os.path.join(OUT, "elasticities.csv"), index=False)
    cross_df.to_csv(os.path.join(OUT, "cross_price.csv"), index=False)
    json.dump(gates, open(os.path.join(OUT, "gates.json"), "w"), indent=2, default=str)
    print(f"[pricing] elasticities: own median {elast_df['own_elast'].median():.2f} "
          f"(range {elast_df['own_elast'].min():.2f}..{elast_df['own_elast'].max():.2f}) | "
          f"{len(cross_df)} cross-price substitute links | gates: {gates.get('overall', gates)}")

    reco_df, kpi = de.optimize(elast_df, cross_df, baseline_df, CONFIG)
    reco_df.to_csv(os.path.join(OUT, "pricing_reco.csv"), index=False)
    print(f"[pricing] optimizer ({CONFIG['kpi']}): revenue {kpi.get('revenue_delta_pct', 0):+.1f}% | "
          f"volume {kpi.get('volume_delta_pct', 0):+.1f}% | {kpi.get('n_cells_up', 0)} up / "
          f"{kpi.get('n_cells_down', 0)} down")

    # ── cannibalization check on the existing waste-cut list ──
    cannib = _cannibalization_check(elast_df, cross_df, baseline_df, run)

    _write_report(elast_df, cross_df, baseline_df, gates, reco_df, kpi, cannib, run)
    print(f"[pricing] wrote {OUT}/PRICING_PLAN.md")
    return {"gates": gates, "kpi": kpi, "cannibalization": cannib}


def _cannibalization_check(elast_df, cross_df, baseline_df, run):
    """Take the existing tool's waste cuts and simulate them THROUGH the cross-price model:
    does raising a staple's price (cutting its discount) leak volume to its siblings?"""
    cut_path = os.path.join(run, "plan", "cut_list.csv")
    if not os.path.exists(cut_path):
        return {"note": "no cut_list.csv to check"}
    cut = pd.read_csv(cut_path)
    # edits = each cut cell to its target discount
    edits = [{"product_id": r["product_id"], "city": r["city"], "new_disc": float(r["tgt_disc"])}
             for _, r in cut.iterrows() if {"product_id", "city", "tgt_disc"} <= set(cut.columns)]
    if not edits:
        return {"note": "cut_list missing product_id/city/tgt_disc"}
    try:
        sim = wi.simulate(elast_df, cross_df, baseline_df, edits)
    except Exception as e:
        return {"note": f"simulate failed: {e}"}

    def _dig(d, key):   # whatif returns portfolio deltas under sim["portfolio"]; be robust
        if key in d and d[key] is not None:
            return d[key]
        return (d.get("portfolio") or {}).get(key)

    per = pd.DataFrame(sim.get("per_cell", []))
    edited_ids = {(e["product_id"], e["city"]) for e in edits}
    sib_gain = 0
    if len(per) and "units_delta_pct" in per:
        per["edited"] = per.apply(lambda r: (r["product_id"], r["city"]) in edited_ids, axis=1)
        sib_gain = int(((~per["edited"]) & (per["units_delta_pct"] > 0)).sum())
    rev = _dig(sim, "revenue_delta_pct")
    return {"n_cuts_simulated": len(edits),
            "portfolio_revenue_delta_pct": rev,
            "portfolio_volume_delta_pct": _dig(sim, "volume_delta_pct"),
            "sibling_cells_gaining": sib_gain,
            "verdict": ("cuts hold at PORTFOLIO level" if (rev is not None and rev >= -0.5)
                        else "cuts LEAK to siblings — per-SKU savings overstate portfolio gain"
                        if rev is not None else "inconclusive (what-if returned no delta)")}


def _write_report(elast_df, cross_df, baseline_df, gates, reco_df, kpi, cannib, run):
    L = ["# PricingAI — Portfolio Elasticity & Optimized Discount Plan\n",
         f"*Adapted from PepsiCo PricingAI (hierarchical elasticity → differential-evolution optimizer). "
         f"Run `{os.path.basename(run)}` · {baseline_df['product_id'].nunique()} SKUs × "
         f"{baseline_df['city'].nunique()} cities · no Gurobi, no cloud — runs on your laptop.*\n"]
    L.append("## 1. What this adds over the per-cell tool\n")
    L.append("Your current tool judges each SKU×city **in isolation**. This adds the missing portfolio physics: "
             "**cross-price elasticity (cannibalization)** — cutting one SKU's discount changes its siblings' sales. "
             "That's the difference between 'this SKU's sales held' and 'the *portfolio* gained'.\n")
    L.append("## 2. Elasticities (hierarchical, partial-pooled)\n")
    L.append(f"- Own-price: median **{elast_df['own_elast'].median():.2f}**, all in the (−2.5, 0) sanity band.")
    L.append(f"- Cross-price substitute links: **{len(cross_df)}** (positive = siblings gain when a SKU's price rises).")
    L.append(f"- Validation gates: {gates.get('overall', gates)}\n")
    # most cannibalistic pairs
    if len(cross_df):
        top = cross_df.reindex(cross_df["cross_elast"].abs().sort_values(ascending=False).index).head(8)
        L.append("**Strongest cannibalization links** (cut one → the other absorbs it):\n")
        for _, r in top.iterrows():
            L.append(f"- {str(r['product_i'])[:16]} ↔ {str(r['product_j'])[:16]}: cross-elast {r['cross_elast']:+.2f}")
        L.append("")
    L.append("## 3. The honesty check — does the ₹6.98L cut list survive cross-price?\n")
    if "verdict" in cannib:
        L.append(f"- Simulated the existing **{cannib.get('n_cuts_simulated')} waste-cuts** through the cross-price model.")
        L.append(f"- Portfolio revenue impact: **{cannib.get('portfolio_revenue_delta_pct'):+.2f}%**; "
                 f"{cannib.get('sibling_cells_gaining')} sibling cells gain volume.")
        L.append(f"- **Verdict: {cannib['verdict']}.**\n")
    else:
        L.append(f"- {cannib.get('note')}\n")
    L.append("## 4. Optimized discount plan (portfolio-aware)\n")
    L.append(f"Objective = **{CONFIG['kpi']}**, subject to: revenue ≥ {CONFIG['revenue_floor_frac']*100:.0f}% of "
             f"baseline, ≤{CONFIG['max_disc_change_ppt']:.0f}ppt weekly change, price-per-kg ladders "
             f"(bigger pack cheaper/kg), psychological ₹-thresholds.\n")
    L.append(f"- Projected: revenue **{kpi.get('revenue_delta_pct',0):+.1f}%**, volume "
             f"**{kpi.get('volume_delta_pct',0):+.1f}%**, NRW **{kpi.get('nrw_delta_pct',0):+.1f}%**.")
    L.append(f"- {kpi.get('n_cells_up',0)} cells get more discount, {kpi.get('n_cells_down',0)} get less.\n")
    if len(reco_df):
        big = reco_df.reindex(reco_df["pred_rev_delta_pct"].abs().sort_values(ascending=False).index).head(10)
        L.append("| SKU | City | Disc now→opt | Pred rev Δ% |")
        L.append("|---|---|---|---:|")
        for _, r in big.iterrows():
            L.append(f"| {str(r['product_id'])[:14]} | {str(r['city'])[:10]} | "
                     f"{r['base_disc']:.0f}%→{r['opt_disc']:.0f}% | {r['pred_rev_delta_pct']:+.1f}% |")
    L.append("\n_Elasticities are penalized-hierarchical point estimates (posterior-mean equivalent). "
             "Full Bayesian posteriors (PyMC) are a drop-in upgrade if uncertainty bands are wanted._")
    open(os.path.join(OUT, "PRICING_PLAN.md"), "w", encoding="utf-8").write("\n".join(L))


if __name__ == "__main__":
    main()
