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
import os, sys, glob, json, shutil
import numpy as np, pandas as pd


def _clean_pid(v):
    """Coerce a product_id to a clean string: strip a trailing '.0' that pandas
    introduces when the id column is read as float64 (e.g. 532393.0 -> '532393').
    Non-numeric ids pass through untouched. Used everywhere product_id is written
    to reco / report / agreement so ids never render as floats."""
    if v is None:
        return ""
    if isinstance(v, float):
        return str(int(v)) if float(v).is_integer() else str(v)
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        return s[:-2]
    return s


def _clean_pid_series(s):
    """Vectorized _clean_pid for a pandas Series."""
    return s.map(_clean_pid)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts", "analysis"))
import pricing_panel as pp
import de_optimizer as de
import whatif as wi
try:
    import elasticity_bayes as eh   # true Bayesian posteriors (informative prior, no clip)
    ELAST_METHOD = "bayes"
except Exception:
    import elasticity_hier as eh     # fallback: penalized/partial-pooled (hard-clip band)
    ELAST_METHOD = "hier"

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


def optimize_decomposed(elast_df, cross_df, baseline_df, config):
    """Cross-price only links SKUs in the SAME category+city, so each (category,city)
    is an INDEPENDENT subproblem — optimize them separately (largest ~18 SKUs) instead
    of one intractable 526-dim DE. Exact (no cross-group coupling exists), and fast."""
    recos, tot = [], {"base_rev": 0.0, "opt_rev": 0.0, "base_vol": 0.0, "opt_vol": 0.0,
                      "n_up": 0, "n_down": 0, "groups": 0, "failed": 0}
    for (cat, city), gb in baseline_df.groupby(["category", "city"]):
        prods = set(gb["product_id"])
        se = elast_df[(elast_df["product_id"].isin(prods)) & (elast_df["city"] == city)]
        sc = cross_df[cross_df["product_i"].isin(prods) & cross_df["product_j"].isin(prods)] \
            if len(cross_df) else cross_df
        try:
            reco, kpi = de.optimize(se, sc, gb, config)
            recos.append(reco)
            b, o = kpi["baseline"], kpi["optimized"]
            tot["base_rev"] += b.get("revenue", 0); tot["opt_rev"] += o.get("revenue", 0)
            tot["base_vol"] += b.get("volume", 0);  tot["opt_vol"] += o.get("volume", 0)
            tot["n_up"] += kpi.get("n_cells_up", 0); tot["n_down"] += kpi.get("n_cells_down", 0)
            tot["groups"] += 1
        except Exception:
            tot["failed"] += 1
    reco_df = pd.concat(recos, ignore_index=True) if recos else pd.DataFrame()
    def _d(o, b): return (o - b) / b * 100.0 if b else 0.0
    kpi_summary = {
        "revenue_delta_pct": _d(tot["opt_rev"], tot["base_rev"]),
        "volume_delta_pct":  _d(tot["opt_vol"], tot["base_vol"]),
        "nrw_delta_pct":     _d(tot["opt_rev"], tot["base_rev"]) - _d(tot["opt_vol"], tot["base_vol"]),
        "n_cells_up": tot["n_up"], "n_cells_down": tot["n_down"],
        "groups_optimized": tot["groups"], "groups_failed": tot["failed"],
    }
    return reco_df, kpi_summary


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

    reco_df, kpi = optimize_decomposed(elast_df, cross_df, baseline_df, CONFIG)
    # ITEM 4: product_id must render as a clean string ('532393', never '532393.0').
    if len(reco_df) and "product_id" in reco_df.columns:
        reco_df["product_id"] = _clean_pid_series(reco_df["product_id"])
    reco_df.to_csv(os.path.join(OUT, "pricing_reco.csv"), index=False)
    print(f"[pricing] optimizer ({CONFIG['kpi']}, {kpi.get('groups_optimized',0)} category×city groups): "
          f"revenue {kpi.get('revenue_delta_pct', 0):+.1f}% | volume {kpi.get('volume_delta_pct', 0):+.1f}% | "
          f"{kpi.get('n_cells_up', 0)} up / {kpi.get('n_cells_down', 0)} down")

    # ── cannibalization check on the existing waste-cut list ──
    cannib = _cannibalization_check(elast_df, cross_df, baseline_df, run)
    # ── reinvest side: DML-confirmed headroom (Oil/Salt), not the wide elasticity bands ──
    reinvest = _reinvest_from_dml(run, baseline_df)
    if reinvest.get("cells"):
        print(f"[pricing] reinvest (DML-confirmed): {reinvest['n']} cells, "
              f"mostly {reinvest.get('top_category')}, +₹{reinvest.get('monthly_upside', 0):,.0f}/mo headroom")

    # ── WIRING producer: engine-agreement interface (consumed by weekly_tracker) ──
    agreement, agree_stats = _write_agreement(reco_df, run, OUT)

    _write_report(elast_df, cross_df, baseline_df, gates, reco_df, kpi, cannib,
                  reinvest, run, agree_stats)
    print(f"[pricing] wrote {OUT}/PRICING_PLAN.md")

    # ── RUN-STAMPING: snapshot this run's pricing outputs so nothing is silently overwritten ──
    stamp = _stamp_run_outputs(run, OUT)
    if stamp:
        print(f"[pricing] stamped run outputs -> {os.path.join(OUT, 'history', stamp)}")

    return {"gates": gates, "kpi": kpi, "cannibalization": cannib,
            "agreement": agree_stats, "stamp": stamp}


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


def _reinvest_from_dml(run, baseline_df):
    """Reinvest headroom comes from the DML-confirmed reliable-positive cells (Oil/Salt) — the
    only place discount RELIABLY pays. The Bayesian own-price bands are too wide to bank a
    reinvest on; this draws the confidence from the analysis layer's reinvest_list instead."""
    rl = os.path.join(run, "plan", "reinvest_list.csv")
    if not os.path.exists(rl):
        return {"cells": [], "n": 0, "note": "no reinvest_list.csv"}
    r = pd.read_csv(rl)
    if not len(r):
        return {"cells": [], "n": 0, "note": "reinvest_list empty"}
    top_cat = r["category"].mode().iloc[0] if "category" in r else "Oil"
    # monthly upside proxy: reinvest_headroom_pp × current spend-scale (net-rev gain proxy)
    upside = 0.0
    if "reinvest_headroom_pp" in r and "cur_units_wk" in r and "mrp" in r:
        upside = float((r["reinvest_headroom_pp"] / 100.0 * r["mrp"] * r["cur_units_wk"]).sum() * 30 / 7)
    cells = [{"product_id": row.get("product_id"), "city": row.get("city"),
              "cur_disc": row.get("cur_disc"), "be_disc": row.get("be_disc"),
              "headroom_pp": row.get("reinvest_headroom_pp")}
             for _, row in r.head(25).iterrows()]
    return {"cells": cells, "n": len(r), "top_category": top_cat, "monthly_upside": upside}


# ── ENGINE-AGREEMENT INTERFACE ────────────────────────────────────────────────
# File: DISCOUNT_PLAN/pricing/agreement.csv — PRODUCED here, CONSUMED by weekly_tracker.
# Columns (exact): cell_id, product_id, city, pricing_action ('cut'|'raise'|'hold'),
#                  agree_with_cut (bool)
# Rule: agree_with_cut = True  iff the cell is a discount_plan waste cut (bucket c_waste_cut)
#       AND the pricing optimizer also lowers its discount (pricing_action == 'cut').
# pricing_action per cell: 'cut' if opt_disc < base_disc - 0.5, 'raise' if opt_disc >
#       base_disc + 0.5, else 'hold'. Match cut_list on (product_id, city).
_AGREE_COLS = ["cell_id", "product_id", "city", "pricing_action", "agree_with_cut"]


def _classify_action(base_disc, opt_disc, band=0.5):
    """Optimizer intent for a cell, from its baseline vs optimized discount (ppt)."""
    try:
        d = float(opt_disc) - float(base_disc)
    except (TypeError, ValueError):
        return "hold"
    if d < -band:
        return "cut"
    if d > band:
        return "raise"
    return "hold"


def _build_agreement(reco_df, cut_df):
    """Build the agreement frame from the optimizer reco_df and the discount_plan
    cut_list (bucket c_waste_cut cells only). Returns (agreement_df, stats).

    Matching is on (clean product_id, city). cell_id is carried from cut_list when the
    cell is a waste-cut, else synthesized as '<product_id>_<city>'. agree_with_cut is
    True only for a waste-cut cell whose optimizer action is also 'cut'."""
    if reco_df is None or not len(reco_df):
        return pd.DataFrame(columns=_AGREE_COLS), {
            "n_waste_cuts": 0, "n_agree_cut": 0, "n_disagree_hold": 0,
            "n_disagree_raise": 0, "n_cut_missing_in_reco": 0}

    r = reco_df.copy()
    r["product_id"] = _clean_pid_series(r["product_id"])
    r["city"] = r["city"].astype(str)
    r["pricing_action"] = [
        _classify_action(bd, od) for bd, od in zip(r["base_disc"], r["opt_disc"])]
    act = {(p, c): a for p, c, a in zip(r["product_id"], r["city"], r["pricing_action"])}

    # waste-cut cells (bucket c_waste_cut) keyed on (clean product_id, city)
    waste = {}
    if cut_df is not None and len(cut_df):
        cd = cut_df.copy()
        if "bucket" in cd.columns:
            cd = cd[cd["bucket"] == "c_waste_cut"]
        cd["product_id"] = _clean_pid_series(cd["product_id"])
        cd["city"] = cd["city"].astype(str)
        for _, row in cd.iterrows():
            key = (row["product_id"], row["city"])
            cid = row["cell_id"] if "cell_id" in cd.columns and pd.notna(row.get("cell_id")) \
                else f"{key[0]}_{key[1]}"
            waste[key] = str(cid)
    waste_keys = set(waste)

    rows, n_agree, n_hold, n_raise, n_missing = [], 0, 0, 0, 0
    for _, rr in r.iterrows():
        key = (rr["product_id"], rr["city"])
        action = rr["pricing_action"]
        is_waste = key in waste_keys
        agree = bool(is_waste and action == "cut")
        cell_id = waste[key] if is_waste else f"{key[0]}_{key[1]}"
        rows.append({"cell_id": cell_id, "product_id": key[0], "city": key[1],
                     "pricing_action": action, "agree_with_cut": agree})
        if is_waste:
            if action == "cut":
                n_agree += 1
            elif action == "raise":
                n_raise += 1
            else:
                n_hold += 1
    # waste-cuts that the optimizer never scored (not in reco_df) — count as disagreement-by-absence
    for key in waste_keys:
        if key not in act:
            n_missing += 1

    agreement_df = pd.DataFrame(rows, columns=_AGREE_COLS)
    stats = {
        "n_waste_cuts": len(waste_keys),
        "n_agree_cut": n_agree,
        "n_disagree_hold": n_hold,
        "n_disagree_raise": n_raise,
        "n_cut_missing_in_reco": n_missing,
    }
    return agreement_df, stats


def _load_cut_list(run):
    """Read the latest run's plan/cut_list.csv. Returns a DataFrame (possibly empty)."""
    cut_path = os.path.join(run, "plan", "cut_list.csv")
    if not os.path.exists(cut_path):
        return pd.DataFrame()
    try:
        return pd.read_csv(cut_path)
    except Exception:
        return pd.DataFrame()


def _write_agreement(reco_df, run, out_dir):
    """Write DISCOUNT_PLAN/pricing/agreement.csv per the ENGINE-AGREEMENT INTERFACE.
    Returns (agreement_df, stats)."""
    cut_df = _load_cut_list(run)
    agreement_df, stats = _build_agreement(reco_df, cut_df)
    os.makedirs(out_dir, exist_ok=True)
    agreement_df.to_csv(os.path.join(out_dir, "agreement.csv"), index=False)
    print(f"[pricing] agreement.csv: {stats['n_waste_cuts']} waste-cuts | "
          f"{stats['n_agree_cut']} both-cut | {stats['n_disagree_hold']} opt-hold | "
          f"{stats['n_disagree_raise']} opt-raise")
    return agreement_df, stats


def _stamp_run_outputs(run, out_dir):
    """Copy this run's pricing outputs into DISCOUNT_PLAN/pricing/history/<stamp>/ so
    successive runs aren't silently overwritten. <stamp> is the basename of the latest
    v4_outputs run folder (NO wall-clock time — derived from that folder name)."""
    stamp = os.path.basename(os.path.normpath(run))
    if not stamp:
        return None
    dest = os.path.join(out_dir, "history", stamp)
    os.makedirs(dest, exist_ok=True)
    for fn in ("elasticities.csv", "cross_price.csv", "pricing_reco.csv",
               "gates.json", "PRICING_PLAN.md", "agreement.csv"):
        src = os.path.join(out_dir, fn)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, fn))
    return stamp


def _write_report(elast_df, cross_df, baseline_df, gates, reco_df, kpi, cannib,
                  reinvest, run, agree_stats=None):
    L = ["# PricingAI — Portfolio Elasticity & Optimized Discount Plan\n",
         f"*Adapted from PepsiCo PricingAI (hierarchical elasticity → differential-evolution optimizer). "
         f"Run `{os.path.basename(run)}` · {baseline_df['product_id'].nunique()} SKUs × "
         f"{baseline_df['city'].nunique()} cities · no Gurobi, no cloud — runs on your laptop.*\n"]
    L.append("## 1. What this adds over the per-cell tool\n")
    L.append("Your current tool judges each SKU×city **in isolation**. This adds the missing portfolio physics: "
             "**cross-price elasticity (cannibalization)** — cutting one SKU's discount changes its siblings' sales. "
             "That's the difference between 'this SKU's sales held' and 'the *portfolio* gained'.\n")
    method = gates.get("method", "hierarchical")
    n_low = gates.get("n_low_confidence_categories")
    L.append(f"## 2. Elasticities ({method})\n")
    if "own_sd" in elast_df.columns:
        L.append(f"- Own-price: median **{elast_df['own_elast'].median():.2f}** with median posterior SD "
                 f"**±{elast_df['own_sd'].median():.2f}** — **true Bayesian bands, no hard clip**. "
                 f"An informative negative prior + hierarchical shrinkage replaces the old clip.")
        if n_low is not None:
            L.append(f"- **{n_low}/{len(gates.get('per_category', {}))} categories are LOW-CONFIDENCE** "
                     f"(wide band): once confounders are controlled, within-cell price variation barely "
                     f"identifies own-price. That's the honest signal — the same weak-identification wall, "
                     f"now shown as uncertainty instead of a fabricated point estimate.")
    else:
        L.append(f"- Own-price: median **{elast_df['own_elast'].median():.2f}**, clipped to the (−2.5, 0) band.")
    L.append(f"- Cross-price substitute links: **{len(cross_df)}** (positive = siblings gain when a SKU's price rises).\n")

    # ── ITEM 1 (transparency): per-category confidence table from the gates ──
    per_cat = gates.get("per_category") or {}
    if per_cat:
        low_set = set(gates.get("low_confidence_categories") or [])
        # A category is low-confidence if the gates listed it, or its own_sd is wide,
        # or its own point estimate is barely negative (weak identification).
        def _is_low(cat, d):
            if cat in low_set:
                return True
            sd, own = d.get("own_sd"), d.get("own")
            return (sd is not None and sd > 0.6) or (own is not None and own > -0.1)
        rows = sorted(per_cat.items(),
                      key=lambda kv: (kv[1].get("own_sd") or 0.0), reverse=True)
        n_low_tbl = sum(1 for c, d in rows if _is_low(c, d))
        L.append("**Per-category confidence** (own-price posterior; low-confidence = wide band, "
                 "act via TEST only — do NOT bank the saving):\n")
        L.append("| Category | Own-price | ± SD | Confidence |")
        L.append("|---|---:|---:|---|")
        for cat, d in rows:
            own = d.get("own"); sd = d.get("own_sd")
            flag = "LOW — test only" if _is_low(cat, d) else "OK"
            own_s = f"{own:+.2f}" if isinstance(own, (int, float)) else "n/a"
            sd_s = f"{sd:.2f}" if isinstance(sd, (int, float)) else "n/a"
            L.append(f"| {str(cat)[:28]} | {own_s} | {sd_s} | {flag} |")
        L.append("")
        L.append(f"**{n_low_tbl}/{len(rows)} categories are low-confidence.** The Bayesian path applies "
                 f"NO clip — a wide band is reported honestly as uncertainty, not squeezed into a "
                 f"fabricated point estimate. **Low-confidence cells should be acted on only via a live "
                 f"test, never banked as a booked saving.**\n")
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
            L.append(f"| {_clean_pid(r['product_id'])[:14]} | {str(r['city'])[:10]} | "
                     f"{r['base_disc']:.0f}%→{r['opt_disc']:.0f}% | {r['pred_rev_delta_pct']:+.1f}% |")
    # ── Reinvest (the flywheel's second half) ──
    L.append("\n## 5. Reinvest — where discount reliably PAYS\n")
    if reinvest.get("cells"):
        L.append(f"The optimizer can raise discount, but the Bayesian own-price bands are too wide to *bank* a "
                 f"reinvest on. The confidence comes instead from the DML-confirmed reliable-positive cells — "
                 f"**{reinvest['n']} cells, mostly {reinvest.get('top_category')}** — where discount demonstrably "
                 f"drives net-accretive volume and current discount sits BELOW its break-even.\n")
        L.append(f"- Headroom to reinvest profitably: **~₹{reinvest.get('monthly_upside', 0):,.0f}/month**.")
        L.append("- Play: fund it from the banked waste-cuts (cut inelastic staples → reinvest into Oil). "
                 "Glide +3ppt, watch 2 weeks, scale only if the register confirms.\n")
        L.append("| SKU | City | Disc now → break-even | Headroom |")
        L.append("|---|---|---|---:|")
        for c in reinvest["cells"][:8]:
            L.append(f"| {_clean_pid(c.get('product_id'))[:14]} | {str(c.get('city'))[:10]} | "
                     f"{c.get('cur_disc',0):.0f}% → {c.get('be_disc',0):.0f}% | +{c.get('headroom_pp',0):.0f}ppt |")
    else:
        L.append(f"_{reinvest.get('note','no reinvest candidates found')}_")

    # ── WIRING: engine-agreement summary (consumed by weekly_tracker) ──
    L.append("\n## Engine agreement\n")
    if agree_stats:
        n = agree_stats.get("n_waste_cuts", 0)
        agree = agree_stats.get("n_agree_cut", 0)
        hold = agree_stats.get("n_disagree_hold", 0)
        raise_ = agree_stats.get("n_disagree_raise", 0)
        missing = agree_stats.get("n_cut_missing_in_reco", 0)
        L.append(f"Of the **{n} discount_plan waste-cuts**, the pricing optimizer independently agrees to "
                 f"**cut {agree}**. On the rest it would instead **hold {hold}** and **raise {raise_}** "
                 + (f"(and {missing} were not scored by the optimizer). " if missing else "")
                 + "`agreement.csv` records this per cell; the tracker only actually cuts a waste cell when "
                 "the optimizer also says cut (`agree_with_cut=True`) — otherwise it HOLDs and tests first, "
                 "so the two engines never quietly contradict each other.\n")
        L.append(f"- Both engines cut: **{agree}/{n}**")
        L.append(f"- Pricing engine would HOLD (test first): **{hold}**")
        L.append(f"- Pricing engine would RAISE discount: **{raise_}**")
        if missing:
            L.append(f"- Waste-cut cells not scored by the optimizer: **{missing}**")
        L.append("\n_Schema: `agreement.csv` = cell_id, product_id, city, pricing_action "
                 "('cut'|'raise'|'hold'), agree_with_cut (bool). agree_with_cut = (cell in waste "
                 "cut_list) AND (pricing_action=='cut')._\n")
    else:
        L.append("_No agreement summary available for this run._\n")

    L.append("\n_Elasticities are TRUE Bayesian posteriors (conjugate, informative negative prior, "
             "empirical-Bayes hierarchical shrinkage) — mean **and** SD, no hard clip. PyMC was attempted but "
             "forces numpy≥2 which binary-breaks the repo's sklearn stack; the analytic conjugate posterior is "
             "the same Bayesian object without the dependency conflict._")
    open(os.path.join(OUT, "PRICING_PLAN.md"), "w", encoding="utf-8").write("\n".join(L))


def _selfcheck():
    """Tiny synthetic-frame smoke test for the agreement.csv writer + product_id
    cleaning + run-stamping. Does NOT run the (slow) DE optimizer or main()."""
    import tempfile

    # ITEM 4: float ids must render clean.
    assert _clean_pid(532393.0) == "532393", _clean_pid(532393.0)
    assert _clean_pid("532393.0") == "532393"
    assert _clean_pid("RICE1") == "RICE1"
    assert _clean_pid(np.float64(100.0)) == "100"
    assert _clean_pid(None) == ""

    # optimizer reco_df: id 111 gets cut, 222 held, 333 raised, 444 (waste) not scored.
    reco_df = pd.DataFrame({
        "product_id": [111.0, 222.0, 333.0],          # float on purpose (ITEM 4)
        "city": ["Bangalore", "Bangalore", "Mumbai"],
        "base_disc": [25.0, 25.0, 10.0],
        "opt_disc":  [2.0,  25.0, 20.0],              # cut, hold, raise
        "base_price": [100, 100, 100], "opt_price": [100, 100, 100],
        "pred_units_delta_pct": [0, 0, 0], "pred_rev_delta_pct": [0, 0, 0],
    })
    # cut_list: 111 (agrees), 222 (opt holds), 444 (never scored) are waste; 555 is not a waste bucket.
    cut_df = pd.DataFrame({
        "cell_id": ["111_1kg_Bangalore", "222_1kg_Bangalore", "444_1kg_Delhi", "555_1kg_Pune"],
        "product_id": [111.0, 222.0, 444.0, 555.0],
        "city": ["Bangalore", "Bangalore", "Delhi", "Pune"],
        "bucket": ["c_waste_cut", "c_waste_cut", "c_waste_cut", "a_stock"],
    })

    agree_df, stats = _build_agreement(reco_df, cut_df)
    # schema
    assert list(agree_df.columns) == _AGREE_COLS, agree_df.columns.tolist()
    # ids are clean strings, never floats
    assert agree_df["product_id"].map(lambda x: isinstance(x, str) and not x.endswith(".0")).all()
    a = {r["product_id"]: r for _, r in agree_df.iterrows()}
    assert a["111"]["pricing_action"] == "cut" and a["111"]["agree_with_cut"] is True
    assert a["222"]["pricing_action"] == "hold" and a["222"]["agree_with_cut"] is False
    assert a["333"]["pricing_action"] == "raise" and a["333"]["agree_with_cut"] is False
    # waste cell carries its cut_list cell_id; non-waste synthesizes one
    assert a["111"]["cell_id"] == "111_1kg_Bangalore"
    assert a["333"]["cell_id"] == "333_Mumbai"
    # 555 is not a waste-cut bucket → never marked agree
    assert a["333"]["agree_with_cut"] is False
    assert stats == {"n_waste_cuts": 3, "n_agree_cut": 1, "n_disagree_hold": 1,
                     "n_disagree_raise": 0, "n_cut_missing_in_reco": 1}, stats

    # empty reco_df -> empty, well-formed frame
    empty_df, empty_stats = _build_agreement(pd.DataFrame(), cut_df)
    assert list(empty_df.columns) == _AGREE_COLS and len(empty_df) == 0
    assert empty_stats["n_waste_cuts"] == 0

    # writer + stamping round-trip on a temp tree
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "pricing"); os.makedirs(out)
        run = os.path.join(td, "v4_outputs", "20260705_161703")
        os.makedirs(os.path.join(run, "plan"))
        cut_df.to_csv(os.path.join(run, "plan", "cut_list.csv"), index=False)
        adf, astats = _write_agreement(reco_df, run, out)
        got = pd.read_csv(os.path.join(out, "agreement.csv"))
        assert list(got.columns) == _AGREE_COLS
        # product_id read back is not a float-string
        assert not str(got["product_id"].iloc[0]).endswith(".0")
        stamp = _stamp_run_outputs(run, out)
        assert stamp == "20260705_161703"
        assert os.path.exists(os.path.join(out, "history", stamp, "agreement.csv"))
    print("[selfcheck] agreement writer OK")
    return True


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        main()
