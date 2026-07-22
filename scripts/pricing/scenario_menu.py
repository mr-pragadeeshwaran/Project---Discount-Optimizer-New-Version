"""
scenario_menu.py — Negotiation scenario MENU (PricingAI paper §4.1/§5.2), challenger-side.

WHAT THIS IS (business framing)
-------------------------------
The paper's negotiation loop is not "here is THE plan" — it is a MENU of optimized
scenarios (different objective KPIs x different constraint tightness) so the person in
the room can trade top-line vs margin vs volume with receipts. This module runs the
EXISTING validated optimizer stack once per scenario and lays the results side by side
with the full financial chain (units, revenue, discount spend, profit, weighted-avg
discount) and the delta vs today.

HOW A NEGOTIATION USES THIS (the closed loop)
---------------------------------------------
1. Pick a scenario off the menu (e.g. "revenue_tight" if finance demands a 99% floor).
2. The KAM executes that scenario's per-cell discounts as a TEST (glide-capped moves).
3. Actuals flow back through the weekly tracker (weekly_tracker.py) -> next round of
   the menu is re-optimized on refreshed data. Optional: counterpart constraints land in
   DISCOUNT_PLAN/pricing/negotiation_feedback.csv (lock / opt_out / max_disc / min_disc
   per cell) and every scenario is re-run honoring them — the paper's "recommendations
   that cannot be executed have no value" rule made explicit.

CHAMPION/CHALLENGER DISCIPLINE
------------------------------
Read-only imports of the validated stack: pricing_panel, elasticity_bayes (fallback
elasticity_hier), de_optimizer (build_problem / demand_model / _kpis /
_penalized_objective), whatif. NOTHING existing is edited or overwritten —
pricing_reco.csv, agreement.csv and everything the tracker consumes stay untouched.
Scenario "revenue_base" reproduces the champion engine's objective+constraints, so the
menu is anchored to the champion rather than replacing it. Adopting another scenario is
an explicit human step.

RUNTIME GUARD
-------------
de_optimizer.build_problem is built ONCE per (category, city) group and shared across
all scenarios (only the config dict is swapped — build_problem output depends on
disc_lo/disc_hi/psych_prices, identical across the menu). DE search effort is trimmed
(maxiter/popsize CLI-tunable) because the menu is a comparison artifact, not the
executed plan; the champion pricing_reco.csv remains the full-effort run.

KPI MENU IS DISCOVERED AT RUNTIME
---------------------------------
The objective list is built from whatever de_optimizer._kpis actually returns on the
real problem (today: revenue, volume, nrw, share). If a concurrent extension adds
profit/margin to _kpis, those scenarios switch on automatically; until then profit and
margin are still REPORTED in the financial chain using v4_config default costs
(COGS 50% of MRP, 15% commission, ₹10/unit fulfillment) — flagged as assumptions.

INPUTS
------
- newest output/runs/2026*/fact_table.csv -> pricing_panel.build_pricing_panel
- elasticity_bayes.estimate_elasticities (same fallback chain as pricing_engine)
- optional DISCOUNT_PLAN/pricing/negotiation_feedback.csv
  (columns: product_id, city, action in {lock, opt_out, max_disc, min_disc}, value, note)

OUTPUTS
-------
- DISCOUNT_PLAN/pricing/scenario_menu.csv     one row per scenario: financial chain + deltas
- DISCOUNT_PLAN/pricing/SCENARIO_MENU.md      comparison table + how-to-negotiate note
- DISCOUNT_PLAN/pricing/scenarios/round_<NN>/reco_<scenario>.csv  per-cell plans
  (same schema as pricing_reco.csv) + menu_kpis.json
- DISCOUNT_PLAN/pricing/negotiation_log.csv   append-only audit trail per run

HOW TO READ scenario_menu.csv
-----------------------------
Row "current" = today's discounts (the delta reference). Every other row is one
optimized scenario: `revenue_wk`/`disc_spend_wk`/`profit_wk` are projected weekly ₹,
`*_delta_*` columns are vs current, `wavg_disc_pct` is the gross-weighted average
discount, `whatif_match` confirms the numbers re-derive exactly through the shared
whatif kernel (arithmetic identity, not a re-estimate), `budget_cap_ok` flags whether
the row respects the 12% weekly discount-spend policy cap (scenarios above it are
reported as infeasible-under-current-rules, never silently clipped — note today's
actual spend can itself be above the cap). Small deltas are the honest result — the
validated elasticities say discount moves demand weakly here.

Run:  python -X utf8 scripts/pricing/scenario_menu.py [--maxiter 40] [--popsize 12]
      [--feedback path.csv] [--scenarios revenue_base,volume_base] [--selfcheck]
"""
import os, sys, glob, json, time, argparse, hashlib
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy.optimize import differential_evolution

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)                                   # v4_config lives at repo root
sys.path.insert(0, os.path.join(ROOT, "scripts", "analysis"))

import de_optimizer as de            # noqa: E402  shared clamped demand kernel (read-only)
import whatif as wi                  # noqa: E402  shared-kernel cross-verifier (read-only)
import pricing_engine as pe          # noqa: E402  CONFIG anchor + _clean_pid (read-only)

try:
    import v4_config as _cfg
    COGS_PCT = float(getattr(_cfg, "DEFAULT_COGS_PCT", 0.50))
    COMM_PCT = float(getattr(_cfg, "DEFAULT_COMMISSION_PCT", 0.15))
    FULFIL_FEE = float(getattr(_cfg, "DEFAULT_FULFILLMENT_FEE", 10.0))
    BUDGET_CAP_FRAC = float(getattr(_cfg, "DEFAULT_BUDGET_PCT_CAP", 0.12))
except Exception:                                          # cost defaults if config moves
    COGS_PCT, COMM_PCT, FULFIL_FEE, BUDGET_CAP_FRAC = 0.50, 0.15, 10.0, 0.12

OUT = os.path.join(ROOT, "DISCOUNT_PLAN", "pricing")
SEED = 101                       # fixed seed -> menu is reproducible run-to-run
MOVE_PPT = 0.25                  # a cell "moved" if |opt-cur| > this (readability threshold)

# Constraint presets (the negotiation levers). "base" mirrors pricing_engine.CONFIG.
PRESETS = {
    "base":  {"revenue_floor_frac": 0.98, "max_disc_change_ppt": 3.0},
    "tight": {"revenue_floor_frac": 0.99, "max_disc_change_ppt": 2.0},
    "loose": {"revenue_floor_frac": 0.96, "max_disc_change_ppt": 4.0},
}

# Decision-useful subset, capped at ~8 DE runs: the champion KPI (revenue) gets all
# three tightness presets (that IS the negotiation lever); every other KPI gets "base".
_MENU_ORDER = [
    ("revenue_base",  "revenue", "base"),
    ("revenue_tight", "revenue", "tight"),
    ("revenue_loose", "revenue", "loose"),
    ("volume_base",   "volume",  "base"),
    ("nrw_base",      "nrw",     "base"),
    ("share_base",    "share",   "base"),
    ("profit_base",   "profit",  "base"),    # active only if _kpis exposes 'profit'
    ("margin_base",   "margin",  "base"),    # active only if _kpis exposes 'margin'
]


def available_kpis(P):
    """KPI names de_optimizer._kpis actually exposes on a REAL problem dict — built at
    runtime so this module works with or without the concurrent profit/margin
    extension landing. Falls back to the classic four if the probe fails."""
    try:
        price0 = np.maximum(P["mrp"] * (1.0 - P["disc0"] / 100.0), 1e-6)
        k = de._kpis(de.demand_model(P["disc0"], P), price0, P)
        return [n for n, v in k.items() if isinstance(v, (int, float, np.floating))]
    except Exception as e:
        print(f"[menu] _kpis probe failed ({e}) — using classic KPI set")
        return ["revenue", "volume", "nrw", "share"]


def build_scenarios(kpis, only=None):
    """Menu = _MENU_ORDER filtered to KPIs that exist (and an optional --scenarios subset)."""
    men = [(n, k, p) for n, k, p in _MENU_ORDER if k in kpis]
    if only:
        men = [(n, k, p) for n, k, p in men if n in only]
    return men[:8]


def load_feedback(path):
    """Optional negotiation constraints -> {(clean_pid, city): (action, value)}.
    Missing file = no-op (defense_hold.csv pattern)."""
    if not path or not os.path.exists(path):
        return {}
    try:
        fb = pd.read_csv(path)
    except Exception as e:
        print(f"[menu] feedback file unreadable ({e}) — ignored"); return {}
    out = {}
    for _, r in fb.iterrows():
        act = str(r.get("action", "")).strip().lower()
        if act not in ("lock", "opt_out", "max_disc", "min_disc"):
            continue
        val = r.get("value")
        val = float(val) if pd.notna(val) else None
        out[(pe._clean_pid(r.get("product_id")), str(r.get("city")))] = (act, val)
    return out


def _cell_bounds(P, config):
    """Per-cell glide-window box bounds — replicated from de_optimizer.optimize()
    (de_optimizer.py:379-405, pure numpy; the original is inline in optimize() and we
    must not edit that file). Returns (lo_cell, hi_cell) arrays."""
    disc_lo = float(config["disc_lo"]); disc_hi = float(config["disc_hi"])
    max_ch = float(config.get("max_disc_change_ppt", 100.0))
    disc0 = P["disc0"]
    glide_lo, glide_hi = disc0 - max_ch, disc0 + max_ch
    lo_cell = np.maximum(glide_lo, disc_lo)
    hi_cell = np.minimum(glide_hi, disc_hi)
    empty = lo_cell > hi_cell                     # disc0 outside the box: WALK, never snap
    above = empty & (disc0 > disc_hi)
    below = empty & (disc0 < disc_lo)
    lo_cell = np.where(above, glide_lo, lo_cell); hi_cell = np.where(above, disc0, hi_cell)
    lo_cell = np.where(below, disc0, lo_cell);    hi_cell = np.where(below, glide_hi, hi_cell)
    return lo_cell, hi_cell


def _apply_feedback(lo, hi, P, feedback):
    """Tighten a group's boxes with negotiation feedback. Locks override glide (an
    executive pin), min/max only ever TIGHTEN. Returns (#honored, lock-mask)."""
    n_hon = 0
    locked = np.zeros(P["n"], dtype=bool)
    if not feedback:
        return n_hon, locked
    cells = P["cells"]
    for i in range(P["n"]):
        key = (pe._clean_pid(cells.iloc[i]["product_id"]), str(cells.iloc[i]["city"]))
        if key not in feedback:
            continue
        act, val = feedback[key]; n_hon += 1
        if act == "lock" and val is not None:
            lo[i] = hi[i] = float(val); locked[i] = True
        elif act == "opt_out":
            lo[i] = hi[i] = float(P["disc0"][i]); locked[i] = True
        elif act == "max_disc" and val is not None:
            hi[i] = min(hi[i], float(val)); lo[i] = min(lo[i], hi[i])
        elif act == "min_disc" and val is not None:
            lo[i] = max(lo[i], float(val)); hi[i] = max(hi[i], lo[i])
    return n_hon, locked


def _ladder_repair(opt_disc, P, lo_cell, hi_cell, config):
    """Deterministic per-gram ladder repair — replicated from de_optimizer.optimize()
    (de_optimizer.py:461-485; soft DE penalty can leak tiny inversions, fix exactly)."""
    tol = float(config.get("ladder_tol", 1.0))
    for _ in range(len(P["ladder_pairs"]) + 1):
        changed = False
        for (si, bi) in P["ladder_pairs"]:
            pg_s, pg_b = P["pack_g"][si], P["pack_g"][bi]
            if not (np.isfinite(pg_s) and np.isfinite(pg_b)) or pg_s <= 0 or pg_b <= 0:
                continue
            price_s = P["mrp"][si] * (1.0 - opt_disc[si] / 100.0)
            price_b = P["mrp"][bi] * (1.0 - opt_disc[bi] / 100.0)
            if price_b / pg_b > tol * (price_s / pg_s) + 1e-9 and P["mrp"][bi] > 1e-9:
                req = 100.0 * (1.0 - (tol * (price_s / pg_s) * pg_b) / P["mrp"][bi])
                new = max(min(max(req, opt_disc[bi]), hi_cell[bi]), lo_cell[bi])
                if new > opt_disc[bi] + 1e-9:
                    opt_disc[bi] = new; changed = True
        if not changed:
            break
    return opt_disc


def optimize_menu(elast_df, cross_df, baseline_df, scenarios, base_config,
                  feedback=None, maxiter=40, popsize=12):
    """Run every scenario over the category x city decomposition, building each group's
    problem dict ONCE (runtime guard) and swapping only the config per scenario.
    Returns (P_all, disc_by_scenario {name: full-length vector}, diag {name: dict})."""
    P_all = de.build_problem(elast_df, cross_df, baseline_df, base_config)
    disc_by, diag = {}, {n: {"n_fb": 0, "n_glide_bound": 0, "failed_groups": 0, "elapsed_s": 0.0}
                         for n, _, _ in scenarios}
    for name, _, _ in scenarios:
        disc_by[name] = P_all["disc0"].copy()      # default: unmoved (also failure fallback)

    for (cat, city), gb in baseline_df.groupby(["category", "city"]):
        prods = set(gb["product_id"])
        se = elast_df[(elast_df["product_id"].isin(prods)) & (elast_df["city"] == city)]
        sc = cross_df[cross_df["product_i"].isin(prods) & cross_df["product_j"].isin(prods)] \
            if cross_df is not None and len(cross_df) else cross_df
        P = de.build_problem(se, sc, gb, base_config)   # ONCE per group, shared by all scenarios
        if P["n"] == 0:
            continue
        rows_all = [P_all["idx"][(r.product_id, r.city)] for r in P["cells"].itertuples()]
        base_price = np.maximum(P["mrp"] * (1.0 - P["disc0"] / 100.0), 1e-6)
        try:
            base_kpis = de._kpis(de.demand_model(P["disc0"], P), base_price, P)
        except Exception:
            for name, _, _ in scenarios:
                diag[name]["failed_groups"] += 1
            continue

        for name, kpi, preset in scenarios:
            t0 = time.time()
            cfg_s = dict(base_config, kpi=kpi, **PRESETS[preset])
            Pc = dict(P); Pc["config"] = cfg_s         # arrays shared; config swapped
            lo, hi = _cell_bounds(Pc, cfg_s)
            n_fb, locked = _apply_feedback(lo, hi, Pc, feedback)
            diag[name]["n_fb"] += n_fb
            try:
                bounds = [(float(lo[i]), float(max(hi[i], lo[i] + 1e-9)))
                          for i in range(Pc["n"])]     # epsilon keeps DE happy on locks
                res = differential_evolution(
                    de._penalized_objective, bounds, args=(Pc, base_kpis), seed=SEED,
                    maxiter=maxiter, popsize=popsize, tol=1e-6, mutation=(0.5, 1.0),
                    recombination=0.7, polish=True, init="latinhypercube",
                    updating="deferred")
                opt = np.clip(res.x, lo, hi)           # exact glide/box/lock feasibility
                opt = _ladder_repair(opt, Pc, lo, hi, cfg_s)   # locks unmovable: lo==hi there
                disc_by[name][rows_all] = opt
                max_ch = cfg_s["max_disc_change_ppt"]
                diag[name]["n_glide_bound"] += int(np.sum(
                    np.abs(np.abs(opt - P["disc0"]) - max_ch) < 1e-3))
            except Exception:
                diag[name]["failed_groups"] += 1       # cells stay at disc0 (honest fallback)
            diag[name]["elapsed_s"] += time.time() - t0
    return P_all, disc_by, diag


def financial_chain(disc_vec, P):
    """Full weekly financial chain for one discount vector through the SHARED kernel.
    Profit uses v4_config default costs (COGS 50% of MRP + 15% commission + ₹10/unit)
    — an assumption until true per-SKU costs exist, and flagged as such in the report."""
    disc = np.asarray(disc_vec, dtype=float)
    price = np.maximum(P["mrp"] * (1.0 - disc / 100.0), 1e-6)
    V = de.demand_model(disc, P)
    gross = float(np.sum(V * P["mrp"]))                 # what the shelf would bill at MRP
    revenue = float(np.sum(V * price))                  # post-discount shelf revenue
    spend = gross - revenue                             # brand-funded discount spend
    vc = P["mrp"] * COGS_PCT + COMM_PCT * price + FULFIL_FEE   # stage6 economics formula
    profit = float(np.sum(V * (price - vc)))
    pack_kg = np.where(np.isnan(P["pack_g"]), 0.0, P["pack_g"] / 1000.0)
    vol_kg = float(np.sum(V * pack_kg))
    return {
        "units_wk": float(np.sum(V)),
        "gross_wk": gross, "revenue_wk": revenue, "disc_spend_wk": spend,
        "spend_pct_gross": spend / gross * 100.0 if gross > 1e-9 else 0.0,
        "wavg_disc_pct": spend / gross * 100.0 if gross > 1e-9 else 0.0,
        "profit_wk": profit,
        "margin_pct": profit / revenue * 100.0 if revenue > 1e-9 else 0.0,
        "vol_kg_wk": vol_kg,
        "nrw_inr_kg": revenue / vol_kg if vol_kg > 1e-9 else 0.0,
    }


def whatif_check(elast_df, cross_df, baseline_df, P_all, disc_vec, chain):
    """Shared-kernel identity: re-derive the scenario through whatif.simulate (which
    builds its own P) and confirm the portfolio revenue delta matches. Any gap would
    mean the menu diverged from the validated kernel."""
    cells = P_all["cells"]
    edits = [{"product_id": cells.iloc[i]["product_id"], "city": cells.iloc[i]["city"],
              "new_disc": float(disc_vec[i])}
             for i in range(P_all["n"]) if abs(disc_vec[i] - P_all["disc0"][i]) > 1e-9]
    if not edits:
        return True
    try:
        sim = wi.simulate(elast_df, cross_df, baseline_df, edits)
        base = financial_chain(P_all["disc0"], P_all)
        ours = (chain["revenue_wk"] - base["revenue_wk"]) / base["revenue_wk"] * 100.0 \
            if base["revenue_wk"] > 1e-9 else 0.0
        return abs(sim["portfolio"]["revenue_delta_pct"] - ours) < 0.02
    except Exception:
        return False


# ── output writers ──────────────────────────────────────────────────────────
def _next_round_dir():
    base = os.path.join(OUT, "scenarios")
    os.makedirs(base, exist_ok=True)
    ns = [int(d.split("_")[1]) for d in os.listdir(base)
          if d.startswith("round_") and d.split("_")[1].isdigit()]
    rd = os.path.join(base, f"round_{(max(ns) + 1) if ns else 1:02d}")
    os.makedirs(rd, exist_ok=True)
    return rd, (max(ns) + 1) if ns else 1


def _reco_frame(P_all, disc_vec):
    """Per-cell plan in the champion pricing_reco.csv schema."""
    price0 = np.maximum(P_all["mrp"] * (1.0 - P_all["disc0"] / 100.0), 1e-6)
    price1 = np.maximum(P_all["mrp"] * (1.0 - np.asarray(disc_vec) / 100.0), 1e-6)
    V0 = de.demand_model(P_all["disc0"], P_all)
    V1 = de.demand_model(disc_vec, P_all)
    with np.errstate(divide="ignore", invalid="ignore"):
        du = np.where(V0 > 1e-9, (V1 - V0) / V0 * 100.0, 0.0)
        dr = np.where(V0 * price0 > 1e-9, (V1 * price1 - V0 * price0) / (V0 * price0) * 100.0, 0.0)
    return pd.DataFrame({
        "product_id": pe._clean_pid_series(P_all["cells"]["product_id"]),
        "city": P_all["cells"]["city"].values,
        "base_disc": np.round(P_all["disc0"], 4), "opt_disc": np.round(disc_vec, 4),
        "base_price": np.round(price0, 4), "opt_price": np.round(price1, 4),
        "pred_units_delta_pct": np.round(du, 4), "pred_rev_delta_pct": np.round(dr, 4)})


def _write_menu_md(menu_df, round_no, run_name, kpis_avail, n_fb, path):
    cur = menu_df[menu_df["scenario"] == "current"].iloc[0]
    L = ["# Scenario Menu — negotiation-ready optimized options\n",
         f"*Run `{run_name}` · round {round_no} · challenger artifact — the champion plan "
         f"(pricing_reco.csv, cut list, tracker) is untouched. All scenarios share the "
         f"validated demand kernel (de_optimizer.demand_model), so differences between rows "
         f"are pure objective/constraint choices, not model noise.*\n",
         "## The menu\n",
         f"Today (row 'current'): ₹{cur['revenue_wk']:,.0f}/wk revenue, "
         f"₹{cur['disc_spend_wk']:,.0f}/wk discount spend "
         f"({cur['spend_pct_gross']:.1f}% of gross), weighted-avg discount "
         f"{cur['wavg_disc_pct']:.1f}%.\n",
         "| Scenario | Objective | Preset (floor / max move) | Revenue ₹/wk (Δ%) | "
         "Units/wk (Δ%) | Disc spend ₹/wk (Δ₹) | Wavg disc | Profit* ₹/wk (Δ₹) | "
         "Cells up/down | Kernel check |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for _, r in menu_df.iterrows():
        if r["scenario"] == "current":
            preset = "—"
        else:
            preset = f"{r['revenue_floor_frac']*100:.0f}% / {r['max_change_ppt']:.0f}ppt"
        L.append(
            f"| {r['scenario']} | {r['kpi']} | {preset} "
            f"| ₹{r['revenue_wk']:,.0f} ({r['revenue_delta_pct']:+.2f}%) "
            f"| {r['units_wk']:,.0f} ({r['units_delta_pct']:+.2f}%) "
            f"| ₹{r['disc_spend_wk']:,.0f} ({r['spend_delta_inr']:+,.0f}) "
            f"| {r['wavg_disc_pct']:.1f}% "
            f"| ₹{r['profit_wk']:,.0f} ({r['profit_delta_inr']:+,.0f}) "
            f"| {int(r['n_cells_up'])}/{int(r['n_cells_down'])} "
            f"| {'OK' if r['whatif_match'] else 'MISMATCH'} |")
    over = menu_df[~menu_df["budget_cap_ok"]]["scenario"].tolist() \
        if "budget_cap_ok" in menu_df.columns else []
    L += ["",
          f"_Weekly discount-spend policy cap: {BUDGET_CAP_FRAC*100:.0f}% of gross "
          f"(v4_config.DEFAULT_BUDGET_PCT_CAP). "
          + (f"Above the cap this round: {', '.join(over)} — infeasible under current "
             f"rules as-is; note 'current' itself can be on this list."
             if over else "Every row, including 'current', is within the cap.") + "_",
          "",
          f"_*Profit uses default cost assumptions (COGS {COGS_PCT*100:.0f}% of MRP, "
          f"{COMM_PCT*100:.0f}% commission, ₹{FULFIL_FEE:.0f}/unit fulfillment) until true "
          f"per-SKU costs are supplied — treat profit DELTAS as directional, levels as rough._",
          "",
          f"_Objective KPIs available in the optimizer this run: {', '.join(kpis_avail)}. "
          + ("Profit/margin objectives were not available in de_optimizer._kpis, so those "
             "scenarios are absent; profit is still reported for every scenario above."
             if not {"profit", "margin"} & set(kpis_avail) else
             "Profit/margin objectives were available and included.") + "_",
          "",
          "## How to read this honestly\n",
          "- Deltas are small because the validated (confounder-controlled) elasticities say "
          "discount moves demand weakly on this portfolio. A menu that promised big scenario "
          "spreads would be fabricating demand the model does not believe in.",
          "- The optimizer only credits volume to a price cut where own-elasticity is "
          "*reliably* negative — same honesty clamp as the champion run.",
          "- 'Cells up/down' counts moves > 0.25ppt; glide caps keep every move executable "
          "in one week.\n",
          "## How a negotiation uses this\n",
          "1. Pick the scenario matching the counterpart's constraint (finance wants margin -> "
          "`revenue_tight`/`nrw_base`; trade wants volume -> `volume_base`) and hand the KAM "
          f"its per-cell sheet (`scenarios/round_{round_no:02d}/reco_<scenario>.csv`).",
          "2. KAM executes it as a glide-capped in-market test; counterpart pushback lands in "
          "`negotiation_feedback.csv` (lock/opt-out/max/min per cell) and the menu is re-run "
          "for the next round.",
          "3. Actuals feed back through the weekly tracker, elasticities refresh, and the next "
          "round's menu is re-optimized on measured — not assumed — response. That is the "
          "closed loop.\n"]
    if n_fb:
        L.append(f"_This round honored {n_fb} negotiation-feedback constraints "
                 f"(locks/opt-outs/caps) inside the optimizer bounds._\n")
    open(path, "w", encoding="utf-8").write("\n".join(L))


def run_menu(maxiter=40, popsize=12, feedback_path=None, only=None):
    os.makedirs(OUT, exist_ok=True)
    fact, run = pe._latest_fact_table()
    run_name = os.path.basename(run)
    print(f"[menu] fact_table: {run_name}")
    import pricing_panel as pp
    panel = pp.build_pricing_panel(fact)
    elast_df, cross_df, baseline_df, gates = pe.eh.estimate_elasticities(panel)
    print(f"[menu] {len(baseline_df)} cells | {baseline_df.groupby(['category','city']).ngroups} "
          f"category-city groups | elasticity method: {pe.ELAST_METHOD}")

    base_config = dict(pe.CONFIG)                    # champion anchor config
    P_probe = de.build_problem(elast_df, cross_df, baseline_df, base_config)
    kpis_avail = available_kpis(P_probe)
    scenarios = build_scenarios(kpis_avail, only)
    fb = load_feedback(feedback_path or os.path.join(OUT, "negotiation_feedback.csv"))
    print(f"[menu] objectives available: {kpis_avail} -> {len(scenarios)} scenarios "
          f"| feedback constraints: {len(fb)}")

    t0 = time.time()
    P_all, disc_by, diag = optimize_menu(elast_df, cross_df, baseline_df, scenarios,
                                         base_config, fb, maxiter, popsize)
    print(f"[menu] optimized {len(scenarios)} scenarios in {time.time()-t0:.0f}s "
          f"(maxiter={maxiter}, popsize={popsize}, seed={SEED})")

    # financial chain: current + each scenario
    cur_chain = financial_chain(P_all["disc0"], P_all)
    rows = [dict(scenario="current", kpi="—", preset="—", revenue_floor_frac=np.nan,
                 max_change_ppt=np.nan, **cur_chain,
                 revenue_delta_pct=0.0, units_delta_pct=0.0, spend_delta_inr=0.0,
                 profit_delta_inr=0.0, n_cells_up=0, n_cells_down=0, floor_slack_pct=np.nan,
                 n_feedback_honored=0, n_glide_bound=0, failed_groups=0,
                 whatif_match=True, elapsed_s=0.0)]
    rd, round_no = _next_round_dir()
    for name, kpi, preset in scenarios:
        dv = disc_by[name]
        ch = financial_chain(dv, P_all)
        d = diag[name]
        floor = PRESETS[preset]["revenue_floor_frac"]
        rows.append(dict(
            scenario=name, kpi=kpi, preset=preset, revenue_floor_frac=floor,
            max_change_ppt=PRESETS[preset]["max_disc_change_ppt"], **ch,
            revenue_delta_pct=(ch["revenue_wk"] / cur_chain["revenue_wk"] - 1) * 100.0,
            units_delta_pct=(ch["units_wk"] / cur_chain["units_wk"] - 1) * 100.0,
            spend_delta_inr=ch["disc_spend_wk"] - cur_chain["disc_spend_wk"],
            profit_delta_inr=ch["profit_wk"] - cur_chain["profit_wk"],
            n_cells_up=int(np.sum(dv > P_all["disc0"] + MOVE_PPT)),
            n_cells_down=int(np.sum(dv < P_all["disc0"] - MOVE_PPT)),
            floor_slack_pct=(ch["revenue_wk"] / (floor * cur_chain["revenue_wk"]) - 1) * 100.0,
            n_feedback_honored=d["n_fb"], n_glide_bound=d["n_glide_bound"],
            failed_groups=d["failed_groups"],
            whatif_match=whatif_check(elast_df, cross_df, baseline_df, P_all, dv, ch),
            elapsed_s=round(d["elapsed_s"], 1)))
        _reco_frame(P_all, dv).to_csv(os.path.join(rd, f"reco_{name}.csv"), index=False)

    menu_df = pd.DataFrame(rows).round(4)
    # 12% weekly spend-cap policy (v4_config.DEFAULT_BUDGET_PCT_CAP) as an explicit
    # feasibility flag — a scenario above the cap is "infeasible under current rules",
    # reported rather than silently clipped. NB: 'current' itself can fail this.
    menu_df["budget_cap_ok"] = menu_df["spend_pct_gross"] <= BUDGET_CAP_FRAC * 100 + 1e-9
    menu_df.to_csv(os.path.join(OUT, "scenario_menu.csv"), index=False)
    # to_json -> loads: NaN becomes null (strict JSON; bare NaN breaks non-Python readers)
    json.dump({"round": round_no, "run": run_name, "kpis_available": kpis_avail,
               "scenarios": json.loads(menu_df.to_json(orient="records"))},
              open(os.path.join(rd, "menu_kpis.json"), "w"), indent=2)
    _write_menu_md(menu_df, round_no, run_name, kpis_avail,
                   int(menu_df["n_feedback_honored"].max()),
                   os.path.join(OUT, "SCENARIO_MENU.md"))

    # append-only negotiation audit log
    fb_hash = ""
    fp = feedback_path or os.path.join(OUT, "negotiation_feedback.csv")
    if os.path.exists(fp):
        fb_hash = hashlib.md5(open(fp, "rb").read()).hexdigest()[:10]
    log_rows = [{"round": round_no, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "run": run_name, "feedback_hash": fb_hash, "scenario": r["scenario"],
                 "revenue_wk": r["revenue_wk"], "revenue_delta_pct": r["revenue_delta_pct"],
                 "disc_spend_wk": r["disc_spend_wk"], "profit_wk": r["profit_wk"],
                 "chosen_scenario": ""} for r in rows]
    log_path = os.path.join(OUT, "negotiation_log.csv")
    pd.DataFrame(log_rows).to_csv(log_path, mode="a", index=False,
                                  header=not os.path.exists(log_path))

    # honest terminal summary
    print(f"[menu] wrote {OUT}\\scenario_menu.csv, SCENARIO_MENU.md, "
          f"scenarios\\round_{round_no:02d}\\ ({len(scenarios)} reco files), negotiation_log.csv")
    for _, r in menu_df.iterrows():
        print(f"  {r['scenario']:<14} rev ₹{r['revenue_wk']:>11,.0f} ({r['revenue_delta_pct']:+.2f}%) "
              f"| spend ₹{r['disc_spend_wk']:>9,.0f} ({r['spend_delta_inr']:+9,.0f}) "
              f"| profit ₹{r['profit_wk']:>11,.0f} ({r['profit_delta_inr']:+9,.0f}) "
              f"| {int(r['n_cells_up'])}up/{int(r['n_cells_down'])}dn "
              f"| kernel {'OK' if r['whatif_match'] else 'MISMATCH'}")
    spread = menu_df.loc[menu_df["scenario"] != "current", "revenue_delta_pct"]
    if len(spread):
        print(f"[menu] honest read: scenario revenue spread is "
              f"{spread.min():+.2f}%..{spread.max():+.2f}% vs current — small, because the "
              f"validated elasticities say discount moves demand weakly on this portfolio.")
    return menu_df


# ── smoke test (synthetic 3-SKU portfolio, mirrors whatif.py's fixture) ──────
def _selfcheck():
    baseline_df = pd.DataFrame([
        {"product_id": "A", "city": "Mumbai", "category": "Atta", "base_product": "24M Atta",
         "pack_grams": 500.0, "q0_units_wk": 100.0, "p0_price": 90.0, "mrp": 100.0, "disc0": 10.0},
        {"product_id": "B", "city": "Mumbai", "category": "Atta", "base_product": "24M Atta",
         "pack_grams": 1000.0, "q0_units_wk": 60.0, "p0_price": 170.0, "mrp": 200.0, "disc0": 15.0},
        {"product_id": "C", "city": "Mumbai", "category": "Oil", "base_product": "24M Oil",
         "pack_grams": 1000.0, "q0_units_wk": 40.0, "p0_price": 380.0, "mrp": 400.0, "disc0": 5.0}])
    elast_df = pd.DataFrame([
        {"product_id": "A", "city": "Mumbai", "own_elast": -1.8, "own_sd": 0.3, "promo_elast": 0.5},
        {"product_id": "B", "city": "Mumbai", "own_elast": -1.2, "own_sd": 0.3, "promo_elast": 0.4},
        {"product_id": "C", "city": "Mumbai", "own_elast": -0.9, "own_sd": 0.2, "promo_elast": 0.3}])
    cross_df = pd.DataFrame([
        {"product_i": "A", "product_j": "B", "cross_elast": 0.4},
        {"product_i": "B", "product_j": "A", "cross_elast": 0.5}])
    base_config = dict(pe.CONFIG)
    P = de.build_problem(elast_df, cross_df, baseline_df, base_config)
    kpis = available_kpis(P)
    assert {"revenue", "volume"} <= set(kpis), kpis
    scen = build_scenarios(kpis)
    assert 1 <= len(scen) <= 8

    # (1) menu runs end-to-end on the synthetic portfolio
    P_all, disc_by, diag = optimize_menu(elast_df, cross_df, baseline_df, scen,
                                         base_config, None, maxiter=20, popsize=10)
    cur = financial_chain(P_all["disc0"], P_all)
    assert cur["units_wk"] > 0 and cur["revenue_wk"] > 0
    for name, kpi, preset in scen:
        dv = disc_by[name]
        max_ch = PRESETS[preset]["max_disc_change_ppt"]
        assert np.all(np.abs(dv - P_all["disc0"]) <= max_ch + 1e-6), \
            f"{name}: glide violated"                       # (2) per-preset glide honored
        ch = financial_chain(dv, P_all)
        assert whatif_check(elast_df, cross_df, baseline_df, P_all, dv, ch), \
            f"{name}: whatif kernel identity failed"        # (3) shared-kernel identity
    # (4) revenue_base respects its revenue floor
    ch = financial_chain(disc_by["revenue_base"], P_all)
    assert ch["revenue_wk"] >= 0.98 * cur["revenue_wk"] - 1e-6, "revenue floor violated"
    # (5) a locked cell comes back at its lock value in every scenario
    fb = {("A", "Mumbai"): ("lock", 12.0)}
    _, disc_fb, _ = optimize_menu(elast_df, cross_df, baseline_df, scen,
                                  base_config, fb, maxiter=20, popsize=10)
    i_a = P_all["idx"][("A", "Mumbai")]
    for name, _, _ in scen:
        assert abs(disc_fb[name][i_a] - 12.0) < 1e-6, f"{name}: lock not honored"
    # (6) distinct objectives can produce distinct plans (soft: report, don't force)
    n_distinct = len({tuple(np.round(v, 2)) for v in disc_by.values()})
    print(f"[selfcheck] {len(scen)} scenarios, {n_distinct} distinct plans "
          f"(distinctness is data-dependent, not asserted)")
    print("[selfcheck] scenario_menu OK")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Negotiation scenario menu (challenger)")
    ap.add_argument("--maxiter", type=int, default=40, help="DE generations per scenario-group")
    ap.add_argument("--popsize", type=int, default=12, help="DE population multiplier")
    ap.add_argument("--feedback", default=None,
                    help="negotiation_feedback.csv path (default DISCOUNT_PLAN/pricing/)")
    ap.add_argument("--scenarios", default=None,
                    help="comma-separated subset, e.g. revenue_base,volume_base")
    ap.add_argument("--selfcheck", action="store_true", help="run synthetic smoke test only")
    a = ap.parse_args()
    if a.selfcheck:
        _selfcheck()
        sys.exit(0)
    only = set(a.scenarios.split(",")) if a.scenarios else None
    run_menu(maxiter=a.maxiter, popsize=a.popsize, feedback_path=a.feedback, only=only)
