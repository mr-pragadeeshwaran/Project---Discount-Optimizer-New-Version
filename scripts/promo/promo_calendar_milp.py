"""
promo_calendar_milp.py — PromoAI-style promotional-calendar MILP (challenger, advisory).

WHAT THIS IS (business framing):
  The champion decides ONE discount per cell for THIS week. This module plans a whole
  T-week promo CALENDAR (default 12 weeks): for every (product, city) cell and every week,
  pick one discount level off a small grid (default 0/5/10/15/20%) so total predicted net
  revenue over the horizon is maximized, subject to the promo business rules a KAM actually
  negotiates: how long a promo must/may run, how far apart promo waves must be (pantry-loading
  guard), how many promos a category may run at once, a weekly discount-spend budget cap, and
  competitive-defense cells that must stay where they are.

FAITHFUL TO PepsiCo PromoAI (paper Sec 2.1 / 3.1):
  - Decision binaries x[cell, week, level] over a discount-level grid, T-week horizon.
  - Demand per (cell, level) comes from PIECEWISE-LINEARIZING the SHARED demand kernel:
    de_optimizer.build_problem + demand_model, evaluating each cell at each grid level with
    siblings held at baseline — exactly budget_allocator.build_ladders' pattern. Champion
    and challenger therefore share ONE demand physics (incl. the honesty clamps).
  - Modular constraint-template registry: every constraint family is declared/toggled in a
    JSON config (scripts/promo/promo_constraints.json). Onboarding another market/brand =
    edit the JSON, zero code changes. Unknown constraint names FAIL LOUD.
  - Solved with scipy.optimize.milp (bundled open-source HiGHS; scipy>=1.9 required — this
    repo ships 1.16). No Gurobi, no cloud.
  - Gap discipline (paper's guardrail): per-subproblem MIP relative-gap target (default 1%),
    a wall-clock time limit per solve, and an honest per-solve report: achieved gap, dual
    bound, node count, runtime, and whether the solve hit the time limit (residual gap flagged).

SCALE: decomposed per (category, city) exactly like pricing_engine.optimize_decomposed —
  cross-price never links across groups, so each MILP is a few hundred to ~1.5k binaries.

HONESTY NOTES (read before trusting the calendar):
  1. Demand is STATIONARY over the horizon (no per-week seasonality in the kernel), so any
     week-to-week variation in the calendar comes from the CONSTRAINTS, not from demand.
  2. Objective coefficients are evaluated one-cell-at-a-time with siblings at baseline
     (the PWL step). Cross-effects of simultaneous moves are ignored in the objective —
     same approximation budget_allocator.build_ladders makes.
  3. Under the validated honesty clamps most cells earn nothing from a discount, so a
     net-revenue-max calendar will park most cells at 0% wherever the rules allow. That is
     the model being blunt, not broken.
  4. The calendar is ADVISORY. Week-1 execution still goes through the champion's glide
     (max 3 ppt/week) and the weekly tracker — a 11%->0% jump here is a direction, not an
     executable Monday move.

INPUTS (all existing artifacts, read-only):
  - newest output/runs/2026*/fact_table.csv -> pricing_panel.build_pricing_panel
  - elasticity_bayes (fallback elasticity_hier).estimate_elasticities -> elast/cross/baseline
  - v4_config.DEFAULT_BUDGET_PCT_CAP (12%) for the weekly discount-spend cap default
  - DISCOUNT_PLAN/defense_hold.csv for the competitive-defense hold-out cells

OUTPUTS -> DISCOUNT_PLAN/promo/:
  - promo_calendar.csv       cell_id, product_id, city, category, week, disc_level_pct,
                             price, units, net_rev_wk, disc_spend_wk, on_promo, held
  - promo_solver_report.csv  per (category, city) solve: status, stop_reason, gap_target,
                             achieved_gap, dual_bound, objective, nodes, solve_time_s
  - PROMO_CALENDAR.md        honest summary incl. MIP gaps + how-to-read

RUN:  python -X utf8 scripts/promo/promo_calendar_milp.py
      [--config scripts/promo/promo_constraints.json] [--weeks 12] [--gap-target 0.01]
      [--time-limit 60] [--max-groups N] [--selftest]
"""
import os, sys, json, glob, time, argparse
import numpy as np, pandas as pd
from scipy import sparse
from scipy.optimize import milp, LinearConstraint, Bounds

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts", "pricing"))
sys.path.insert(0, ROOT)
import de_optimizer as de            # shared demand kernel — imported, NEVER edited
import v4_config as cfg

OUT = os.path.join(ROOT, "output", "DISCOUNT_PLAN", "promo")
DEFAULT_CONFIG_PATH = os.path.join(HERE, "promo_constraints.json")

# Same kernel config as pricing_engine.CONFIG (dln bounds + PPP thresholds only).
KERNEL_CONFIG = {
    "disc_lo": 0.0, "disc_hi": 45.0,
    "psych_prices": [49, 99, 149, 199, 249, 299, 349, 399, 449, 499, 599, 699, 799, 999],
}


def _clean_pid(v):
    """'532393.0' / 532393.0 -> '532393' (same convention as pricing_engine)."""
    if v is None:
        return ""
    if isinstance(v, float):
        return str(int(v)) if float(v).is_integer() else str(v)
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        return s[:-2]
    return s


def _latest_fact_table():
    for r in sorted(glob.glob(os.path.join(ROOT, "output", "runs", "2026*")), reverse=True):
        f = os.path.join(r, "fact_table.csv")
        if os.path.exists(f):
            return f, r
    raise SystemExit("no fact_table.csv — run pipeline.py first")


def load_config(path):
    ccfg = json.load(open(path, encoding="utf-8"))
    unknown = set(ccfg.get("constraints", {})) - set(CONSTRAINT_BUILDERS)
    if unknown:
        raise ValueError(f"unknown constraint template(s) in {os.path.basename(path)}: "
                         f"{sorted(unknown)} — known: {sorted(CONSTRAINT_BUILDERS)}")
    return ccfg


def load_defense_hold(path):
    """-> set of (clean product_id, city) that must keep their current discount level."""
    if not os.path.exists(path):
        return set()
    h = pd.read_csv(path)
    if not len(h) or not {"product_id", "city"} <= set(h.columns):
        return set()
    return {(_clean_pid(p), str(c)) for p, c in zip(h["product_id"], h["city"])}


def build_level_ladder(P, levels):
    """PWL step: evaluate the SHARED kernel per (cell, level), siblings at baseline
    (same pattern as budget_allocator.build_ladders). Returns (units, rev, spend, price)
    each shaped [n_cells, n_levels]."""
    n, L = P["n"], len(levels)
    units = np.zeros((n, L)); rev = np.zeros((n, L)); spend = np.zeros((n, L)); price = np.zeros((n, L))
    for i in range(n):
        for k, d in enumerate(levels):
            dv = P["disc0"].copy(); dv[i] = d          # vary cell i only
            V = float(de.demand_model(dv, P)[i])
            pr = float(P["mrp"][i] * (1.0 - d / 100.0))
            units[i, k] = V; price[i, k] = pr
            rev[i, k] = V * pr                          # net revenue at this level
            spend[i, k] = V * (P["mrp"][i] - pr)        # discount funding at this level
    return units, rev, spend, price


# ────────────────────────────────────────────────────────────────────────────
# Constraint-template registry (the paper's modular template library).
# Each builder(acc, ctx, params) appends linear rows to `acc` and/or tightens
# variable bounds in ctx; returns an audit dict {rows, cells, note}.
# Variables per group: x[i,t,l] binaries (level assignment), plus — only when a
# calendar rule needs them — y[i,t] (on-promo) and s[i,t] (promo-start) binaries.
# ────────────────────────────────────────────────────────────────────────────
class _RowAccum:
    def __init__(self):
        self.r, self.c, self.v, self.lb, self.ub, self.n = [], [], [], [], [], 0

    def add(self, coefs, lb, ub):
        """coefs = dict {var_index: coefficient}."""
        for j, a in coefs.items():
            self.r.append(self.n); self.c.append(j); self.v.append(float(a))
        self.lb.append(lb); self.ub.append(ub); self.n += 1

    def to_constraint(self, nvar):
        A = sparse.coo_matrix((self.v, (self.r, self.c)), shape=(self.n, nvar)).tocsr()
        return LinearConstraint(A, np.array(self.lb), np.array(self.ub))


def _c_exclusivity(acc, ctx, params):
    """(1) exactly one level per cell-week — structural; enforced even if disabled."""
    for i in range(ctx["n"]):
        for t in range(ctx["T"]):
            acc.add({ctx["xidx"](i, t, l): 1.0 for l in range(ctx["L"])}, 1.0, 1.0)
    return {"rows": ctx["n"] * ctx["T"], "cells": ctx["n"], "note": "one level per cell-week"}


def _c_min_duration(acc, ctx, params):
    """(2a) a promo run started at t must stay on >= min_weeks (window truncated at the
    horizon edge, so a run starting in the last weeks only needs the remaining weeks)."""
    Lw, rows = int(params.get("min_weeks", 2)), 0
    if Lw <= 1:
        return {"rows": 0, "cells": 0, "note": "min_weeks<=1 — vacuous"}
    for i in range(ctx["n"]):
        if ctx["held"][i]:
            continue                                   # held cells are not discretionary promos
        for t in range(ctx["T"]):
            w = min(Lw, ctx["T"] - t)
            coefs = {ctx["yidx"](i, j): 1.0 for j in range(t, t + w)}
            coefs[ctx["sidx"](i, t)] = -float(w)       # sum y >= w * s
            acc.add(coefs, 0.0, np.inf); rows += 1
    return {"rows": rows, "cells": int((~ctx["held"]).sum()), "note": f"min run {Lw} wk"}


def _c_max_duration(acc, ctx, params):
    """(2b) no promo run longer than max_weeks: every (max_weeks+1)-week window <= max_weeks
    on-promo weeks. Runs already live at week 0 are bounded only within the horizon (prior
    streak length is unknown to the MILP — documented approximation)."""
    U, rows = int(params.get("max_weeks", 6)), 0
    for i in range(ctx["n"]):
        if ctx["held"][i]:
            continue
        for t in range(0, ctx["T"] - U):
            acc.add({ctx["yidx"](i, j): 1.0 for j in range(t, t + U + 1)}, -np.inf, float(U))
            rows += 1
    return {"rows": rows, "cells": int((~ctx["held"]).sum()), "note": f"max run {U} wk"}


def _c_min_spacing(acc, ctx, params):
    """(3) pantry-loading guard: at most one promo START in any spacing_weeks window
    (starts >= k weeks apart)."""
    k, rows = int(params.get("spacing_weeks", 4)), 0
    if k <= 1:
        return {"rows": 0, "cells": 0, "note": "spacing<=1 — vacuous"}
    for i in range(ctx["n"]):
        if ctx["held"][i]:
            continue
        for t in range(0, ctx["T"] - k + 1):
            acc.add({ctx["sidx"](i, j): 1.0 for j in range(t, t + k)}, -np.inf, 1.0)
            rows += 1
    return {"rows": rows, "cells": int((~ctx["held"]).sum()), "note": f"starts >= {k} wk apart"}


def _c_max_simultaneous(acc, ctx, params):
    """(4) max simultaneous promos per category per week. Decomposition = one category x
    city per MILP, so the cap applies per (category, city). Defense-held cells are exempt
    (their level is imposed, not a discretionary slot) — documented."""
    cap, rows = int(params.get("max_per_category_week", 3)), 0
    free = [i for i in range(ctx["n"]) if not ctx["held"][i]]
    if not free:
        return {"rows": 0, "cells": 0, "note": "all cells held"}
    for t in range(ctx["T"]):
        acc.add({ctx["yidx"](i, t): 1.0 for i in free}, -np.inf, float(cap))
        rows += 1
    return {"rows": rows, "cells": len(free), "note": f"<= {cap} live promos/wk"}


def _c_weekly_budget(acc, ctx, params):
    """(5) weekly discount spend <= budget_pct x group baseline weekly revenue.
    budget_pct=None -> v4_config.DEFAULT_BUDGET_PCT_CAP (12%). Group budget is the
    pro-rata share (cap x this group's own baseline revenue) — the one deviation from a
    monolithic portfolio MILP, same rationale as the decomposition itself."""
    pct = params.get("budget_pct")
    pct = float(cfg.DEFAULT_BUDGET_PCT_CAP if pct is None else pct)
    budget = pct * ctx["base_rev_grp"]
    rows = 0
    for t in range(ctx["T"]):
        coefs = {}
        for i in range(ctx["n"]):
            for l in range(ctx["L"]):
                sp = ctx["spend"][i, l]
                if sp > 1e-9:
                    coefs[ctx["xidx"](i, t, l)] = sp
        if coefs:
            acc.add(coefs, -np.inf, budget); rows += 1
    ctx["budget_wk"] = budget; ctx["budget_pct"] = pct
    return {"rows": rows, "cells": ctx["n"],
            "note": f"spend <= {pct:.0%} of group baseline revenue/wk (pro-rata per category x city)"}


def _c_defense_hold(acc, ctx, params):
    """(6) competitive-pressure hold-out: cells listed in defense_hold.csv are FIXED at the
    grid level nearest their current discount, all weeks (bounds, not rows)."""
    n_held = 0
    for i in range(ctx["n"]):
        if not ctx["held"][i]:
            continue
        n_held += 1
        for t in range(ctx["T"]):
            for l in range(ctx["L"]):
                j = ctx["xidx"](i, t, l)
                if l == ctx["snap0"][i]:
                    ctx["vlb"][j] = 1.0                # forced on
                else:
                    ctx["vub"][j] = 0.0                # forced off
    return {"rows": 0, "cells": n_held, "note": "held at current level (defense_hold.csv)"}


CONSTRAINT_BUILDERS = {
    "promotion_exclusivity":    _c_exclusivity,
    "min_promo_duration":       _c_min_duration,
    "max_promo_duration":       _c_max_duration,
    "min_promo_spacing":        _c_min_spacing,
    "max_simultaneous_promos":  _c_max_simultaneous,
    "weekly_budget_cap":        _c_weekly_budget,
    "competitive_defense_hold": _c_defense_hold,
}
_NEEDS_YS = {"min_promo_duration", "max_promo_duration", "min_promo_spacing",
             "max_simultaneous_promos"}


def solve_group(cat, city, se, sc, gb, ccfg, hold_set):
    """Build + solve one (category, city) promo-calendar MILP. Returns
    (calendar_rows, report_row, audits)."""
    t0 = time.monotonic()
    levels = [float(x) for x in ccfg.get("discount_levels_pct", [0, 5, 10, 15, 20])]
    T = int(ccfg.get("horizon_weeks", 12))
    thr = float(ccfg.get("promo_threshold_pct", 5.0))
    rules = ccfg.get("constraints", {})
    enabled = {name for name, spec in rules.items() if spec.get("enabled", False)}
    if "promotion_exclusivity" not in enabled:
        print("[promo]  WARNING: promotion_exclusivity disabled in config — it is structural "
              "(demand is undefined without a chosen level); enforcing anyway.")
        enabled.add("promotion_exclusivity")

    P = de.build_problem(se, sc, gb, KERNEL_CONFIG)     # shared kernel, read-only
    n, L = P["n"], len(levels)
    units, rev, spend, price = build_level_ladder(P, np.asarray(levels))

    snap0 = np.array([int(np.argmin(np.abs(np.asarray(levels) - d))) for d in P["disc0"]])
    on0 = (P["disc0"] >= thr).astype(float)
    pid_clean = [_clean_pid(p) for p in P["cells"]["product_id"]]
    held = np.array([(pid_clean[i], str(P["cells"]["city"].iloc[i])) in hold_set
                     for i in range(n)])

    needs_ys = bool(enabled & _NEEDS_YS)
    nx = n * T * L
    ny = ns = n * T if needs_ys else 0
    nvar = nx + ny + ns
    xidx = lambda i, t, l: (i * T + t) * L + l
    yidx = lambda i, t: nx + i * T + t
    sidx = lambda i, t: nx + ny + i * T + t

    ctx = {"n": n, "T": T, "L": L, "levels": levels, "xidx": xidx, "yidx": yidx,
           "sidx": sidx, "held": held, "snap0": snap0, "spend": spend, "rev": rev,
           "base_rev_grp": float(np.sum(P["q0"] * P["p0"])),
           "vlb": np.zeros(nvar), "vub": np.ones(nvar)}

    acc = _RowAccum()
    # Linking rows for y (on-promo) and s (start) — only when a calendar rule needs them.
    promo_lvls = [l for l in range(L) if levels[l] >= thr]
    if needs_ys:
        for i in range(n):
            for t in range(T):
                coefs = {yidx(i, t): 1.0}
                for l in promo_lvls:
                    coefs[xidx(i, t, l)] = -1.0
                acc.add(coefs, 0.0, 0.0)                # y = sum of promo-level x
                if t == 0:                              # y0 - s0 <= on0 (start vs initial state)
                    acc.add({yidx(i, 0): 1.0, sidx(i, 0): -1.0}, -np.inf, float(on0[i]))
                else:                                   # y_t - y_{t-1} - s_t <= 0
                    acc.add({yidx(i, t): 1.0, yidx(i, t - 1): -1.0, sidx(i, t): -1.0},
                            -np.inf, 0.0)

    audits = {}
    order = ["competitive_defense_hold", "promotion_exclusivity", "min_promo_duration",
             "max_promo_duration", "min_promo_spacing", "max_simultaneous_promos",
             "weekly_budget_cap"]        # holds first so later builders see the held mask
    for name in order:
        if name in enabled:
            audits[name] = CONSTRAINT_BUILDERS[name](acc, ctx, rules[name].get("params", {}))

    # Objective: maximize horizon net revenue  ->  minimize -sum rev[i,l] * x[i,t,l].
    c = np.zeros(nvar)
    for i in range(n):
        for t in range(T):
            for l in range(L):
                c[xidx(i, t, l)] = -rev[i, l]

    gap_target = float(ccfg.get("solver", {}).get("gap_target", 0.01))
    time_limit = float(ccfg.get("solver", {}).get("time_limit_s", 60.0))
    res = milp(c=c, constraints=[acc.to_constraint(nvar)], integrality=np.ones(nvar),
               bounds=Bounds(ctx["vlb"], ctx["vub"]),
               options={"mip_rel_gap": gap_target, "time_limit": time_limit, "presolve": True})
    elapsed = time.monotonic() - t0

    gap = getattr(res, "mip_gap", np.nan)
    dual = getattr(res, "mip_dual_bound", np.nan)
    nodes = getattr(res, "mip_node_count", np.nan)
    has_x = res.x is not None
    if res.status == 0:
        stop = "target_gap_hit"
    elif res.status == 1 and has_x:
        stop = "time_limit_incumbent_kept"
    elif res.status == 2:
        stop = "infeasible"
    else:
        stop = f"no_solution(status={res.status})"

    cal_rows = []
    if has_x:
        xv = np.asarray(res.x[:nx]).reshape(n, T, L)
        pick = xv.argmax(axis=2)                        # near-integral -> chosen level
    else:                                               # honest fallback: hold at snapped current
        pick = np.tile(snap0[:, None], (1, T))
    for i in range(n):
        for t in range(T):
            l = int(pick[i, t])
            cal_rows.append({
                "cell_id": f"{pid_clean[i]}_{P['cells']['city'].iloc[i]}",
                "product_id": pid_clean[i], "city": str(P["cells"]["city"].iloc[i]),
                "category": cat, "week": t + 1, "disc_level_pct": levels[l],
                "price": round(price[i, l], 2), "units": round(units[i, l], 1),
                "net_rev_wk": round(rev[i, l], 0), "disc_spend_wk": round(spend[i, l], 0),
                "on_promo": bool(levels[l] >= thr), "held": bool(held[i]),
            })

    obj = float(-res.fun) if (has_x and res.fun is not None) else float(
        sum(rev[i, snap0[i]] for i in range(n)) * T)
    base_hold_obj = float(sum(rev[i, snap0[i]] for i in range(n)) * T)
    report = {
        "category": cat, "city": city, "n_cells": n, "n_vars": nvar, "n_rows": acc.n,
        "status": int(res.status), "stop_reason": stop, "gap_target": gap_target,
        "achieved_gap": round(float(gap), 6) if np.isfinite(gap) else np.nan,
        "hit_time_limit": bool(res.status == 1),
        "dual_bound": round(float(dual), 0) if np.isfinite(dual) else np.nan,
        "objective_net_rev": round(obj, 0),
        "baseline_hold_net_rev": round(base_hold_obj, 0),
        "nodes": nodes, "solve_time_s": round(elapsed, 2),
        "budget_wk": round(ctx.get("budget_wk", np.nan), 0) if "budget_wk" in ctx else np.nan,
        "n_held_cells": int(held.sum()),
    }
    return cal_rows, report, audits


def run_calendar(elast_df, cross_df, baseline_df, ccfg, hold_set, max_groups=None):
    """Decompose per (category, city), solve every subproblem, aggregate.
    Returns (calendar_df, report_df, audit_totals)."""
    cal, reps, audit_tot = [], [], {}
    groups = list(baseline_df.groupby(["category", "city"]))
    if max_groups:
        groups = groups[:max_groups]
    for (cat, city), gb in groups:
        prods = set(gb["product_id"])
        se = elast_df[(elast_df["product_id"].isin(prods)) & (elast_df["city"] == city)]
        sc = cross_df[cross_df["product_i"].isin(prods) & cross_df["product_j"].isin(prods)] \
            if cross_df is not None and len(cross_df) else cross_df
        try:
            rows, rep, audits = solve_group(cat, city, se, sc, gb, ccfg, hold_set)
        except Exception as e:                          # thin/broken group: skip, report
            reps.append({"category": cat, "city": city, "n_cells": len(gb), "status": -1,
                         "stop_reason": f"build_failed: {e}", "solve_time_s": 0.0})
            continue
        cal.extend(rows); reps.append(rep)
        for k, a in audits.items():
            t = audit_tot.setdefault(k, {"rows": 0, "cells": 0, "note": a.get("note", "")})
            t["rows"] += a.get("rows", 0); t["cells"] += a.get("cells", 0)
    return pd.DataFrame(cal), pd.DataFrame(reps), audit_tot


def _write_report(cal, reps, audit_tot, ccfg, total_s, run_name):
    T = int(ccfg.get("horizon_weeks", 12))
    levels = ccfg.get("discount_levels_pct", [0, 5, 10, 15, 20])
    ok = reps[reps["status"] == 0] if "status" in reps else reps
    tl = reps[reps.get("hit_time_limit", False) == True] if "hit_time_limit" in reps else reps.iloc[0:0]
    infeas = reps[reps["stop_reason"] == "infeasible"] if "stop_reason" in reps else reps.iloc[0:0]
    tot_obj = float(reps["objective_net_rev"].sum()) if "objective_net_rev" in reps else 0.0
    tot_base = float(reps["baseline_hold_net_rev"].sum()) if "baseline_hold_net_rev" in reps else 0.0
    d_pct = (tot_obj - tot_base) / tot_base * 100.0 if tot_base > 0 else 0.0
    wk_spend = cal.groupby("week")["disc_spend_wk"].sum() if len(cal) else pd.Series(dtype=float)
    n_promo_cw = int(cal["on_promo"].sum()) if len(cal) else 0
    n_cells = cal["cell_id"].nunique() if len(cal) else 0
    worst = reps["achieved_gap"].max() if "achieved_gap" in reps and reps["achieved_gap"].notna().any() else np.nan

    L = [
        "# Promo Calendar — MILP challenger (PromoAI-style, advisory)\n",
        f"*{n_cells} cells x {T} weeks on grid {levels}% · decomposed per category x city · "
        f"HiGHS via scipy.optimize.milp · run `{run_name}` · total solve wall-clock "
        f"**{total_s:.1f}s** across {len(reps)} subproblems.*\n",
        "## The calendar in one paragraph\n",
        f"- Horizon net revenue of the chosen calendar: **Rs{tot_obj:,.0f}** vs "
        f"**Rs{tot_base:,.0f}** if every cell just held its current (grid-snapped) discount "
        f"— **{d_pct:+.2f}%**. (Note: that hold-current plan is itself NOT rule-feasible — "
        f"holding a promo discount 12 straight weeks breaks the max-duration rule — so it is a "
        f"reference point, not an available alternative.)",
        f"- Promo cell-weeks scheduled: **{n_promo_cw}** of {len(cal)} "
        f"({n_promo_cw / max(len(cal), 1) * 100:.0f}%). Weekly discount spend ranges "
        f"Rs{wk_spend.min():,.0f}–Rs{wk_spend.max():,.0f}." if len(cal) else "- (no calendar rows)",
        f"- Defense-held cells (kept at current level, all weeks): "
        f"**{int(reps['n_held_cells'].sum()) if 'n_held_cells' in reps else 0} cell-solves**.\n",
        "**Read this honestly:** the demand kernel's validated honesty clamps credit volume "
        "from a discount only where own-price elasticity is *reliably* negative — which on this "
        "portfolio is almost nowhere. A net-revenue-max calendar therefore parks most cells at "
        "0% discount and the 'calendar' structure you see comes from the constraints (holds, "
        "budget, duration rules), not from demand seasonality (the kernel is stationary across "
        "weeks). This is the same conclusion as the budget allocator and the confounder-"
        "controlled study: discount spend on this portfolio is mostly margin giveaway.\n",
        "## Solver receipts (val_14: per-solve MIP gap, status, runtime)\n",
        f"- Gap target: **{ccfg.get('solver', {}).get('gap_target', 0.01):.1%}** relative; "
        f"time limit {ccfg.get('solver', {}).get('time_limit_s', 60)}s per subproblem.",
        f"- **{len(ok)}/{len(reps)}** subproblems solved to the gap target; "
        f"**{len(tl)}** hit the time limit (kept incumbent, residual gap flagged below); "
        f"**{len(infeas)}** infeasible.",
        f"- Worst achieved gap: **{worst:.4%}**." if np.isfinite(worst) else
        "- Achieved gap: n/a (solver did not report).",
        f"- Total runtime: **{total_s:.1f}s**.\n",
        "| Category | City | Cells | Status | Gap target | Achieved gap | Time (s) | Stop reason |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    show = reps.sort_values("solve_time_s", ascending=False).head(15) if len(reps) else reps
    for _, r in show.iterrows():
        g = r.get("achieved_gap")
        gs = f"{g:.4%}" if pd.notna(g) else "n/a"
        L.append(f"| {str(r.get('category'))[:22]} | {str(r.get('city'))[:12]} | "
                 f"{r.get('n_cells', 0)} | {r.get('status')} | "
                 f"{r.get('gap_target', np.nan):.1%} | {gs} | "
                 f"{r.get('solve_time_s', 0):.2f} | {r.get('stop_reason')} |")
    if len(reps) > 15:
        L.append(f"\n_(top 15 by solve time shown; full table in `promo_solver_report.csv` — "
                 f"{len(reps)} rows)_")
    L += [
        "\n## Active constraint templates (from promo_constraints.json)\n",
        "| Template | Rows generated | Cells touched | Note |", "|---|---:|---:|---|",
    ]
    for k, a in audit_tot.items():
        L.append(f"| {k} | {a['rows']} | {a['cells']} | {a['note']} |")
    L += [
        "\n## How to read / operate\n",
        "- `promo_calendar.csv`: one row per cell x week — the chosen discount level, plus the "
        "kernel's predicted units, net revenue and discount spend at that level. `held=True` "
        "rows are competitive-defense cells pinned at their current level.",
        "- `promo_solver_report.csv`: one row per (category, city) MILP — the paper-style gap "
        "certificate. `achieved_gap` <= `gap_target` means the schedule is provably within "
        "that % of the best possible under these rules. `hit_time_limit=True` rows carry a "
        "residual gap: the incumbent is kept but is NOT certified to target.",
        "- To onboard another market/brand: copy `promo_constraints.json`, edit params — "
        "zero code changes. Unknown template names fail loud.",
        "- **This calendar is advisory (challenger).** Week-1 execution still goes through the "
        "champion cut list, the 3 ppt glide, and the weekly tracker. Cross-effects of "
        "simultaneous moves are frozen at baseline in the objective (PWL step), and demand "
        "carries no week-of-year seasonality — treat week-to-week structure as rule-driven.",
    ]
    open(os.path.join(OUT, "PROMO_CALENDAR.md"), "w", encoding="utf-8").write("\n".join(L))


def main(args):
    os.makedirs(OUT, exist_ok=True)
    ccfg = load_config(args.config)
    if args.weeks:
        ccfg["horizon_weeks"] = args.weeks
    if args.gap_target is not None:
        ccfg.setdefault("solver", {})["gap_target"] = args.gap_target
    if args.time_limit is not None:
        ccfg.setdefault("solver", {})["time_limit_s"] = args.time_limit

    fact, run = _latest_fact_table()
    print(f"[promo] fact_table: {os.path.basename(run)}")
    import pricing_panel as pp
    try:
        import elasticity_bayes as eh
    except Exception:
        import elasticity_hier as eh
    panel = pp.build_pricing_panel(fact)
    elast_df, cross_df, baseline_df, _gates = eh.estimate_elasticities(panel)
    print(f"[promo] panel: {baseline_df['product_id'].nunique()} SKUs x "
          f"{baseline_df['city'].nunique()} cities = {len(baseline_df)} cells | "
          f"horizon {ccfg['horizon_weeks']} wk | grid {ccfg['discount_levels_pct']}%")

    hold_path = ccfg.get("constraints", {}).get("competitive_defense_hold", {}) \
                    .get("params", {}).get("hold_file", "output/DISCOUNT_PLAN/defense_hold.csv")
    hold_set = load_defense_hold(os.path.join(ROOT, hold_path))
    print(f"[promo] defense hold-outs: {len(hold_set)} cells from {hold_path}")

    t0 = time.monotonic()
    cal, reps, audit_tot = run_calendar(elast_df, cross_df, baseline_df, ccfg, hold_set,
                                        max_groups=args.max_groups)
    total_s = time.monotonic() - t0

    cal.to_csv(os.path.join(OUT, "promo_calendar.csv"), index=False)
    reps.to_csv(os.path.join(OUT, "promo_solver_report.csv"), index=False)
    _write_report(cal, reps, audit_tot, ccfg, total_s, os.path.basename(run))

    ok = int((reps["status"] == 0).sum()) if len(reps) else 0
    tl = int(reps["hit_time_limit"].sum()) if "hit_time_limit" in reps else 0
    worst = reps["achieved_gap"].max() if "achieved_gap" in reps else np.nan
    tot_obj = reps["objective_net_rev"].sum() if "objective_net_rev" in reps else 0
    tot_base = reps["baseline_hold_net_rev"].sum() if "baseline_hold_net_rev" in reps else 0
    print(f"[promo] solved {len(reps)} subproblems in {total_s:.1f}s | {ok} at gap target | "
          f"{tl} hit time limit | worst gap "
          f"{worst:.4%}" if np.isfinite(worst) else
          f"[promo] solved {len(reps)} subproblems in {total_s:.1f}s | {ok} at gap target")
    if tot_base > 0:
        print(f"[promo] horizon net revenue: Rs{tot_obj:,.0f} vs hold-current Rs{tot_base:,.0f} "
              f"({(tot_obj - tot_base) / tot_base * 100:+.2f}%) | promo cell-weeks: "
              f"{int(cal['on_promo'].sum())}/{len(cal)}")
    print(f"[promo] wrote {OUT}\\promo_calendar.csv, promo_solver_report.csv, PROMO_CALENDAR.md")


# ────────────────────────────────────────────────────────────────────────────
# Self-test: synthetic 3-cell fixture (de_optimizer's smoke-test cast) exercising
# every constraint family + fail-loud on unknown template names. Exit 0 = pass.
# ────────────────────────────────────────────────────────────────────────────
def _selftest():
    baseline_df = pd.DataFrame(
        [["RICE1", "BLR", "Staples", "Sonamasuri Rice", 1000.0, 120.0, 110.0, 130.0, 15.4],
         ["RICE5", "BLR", "Staples", "Sonamasuri Rice", 5000.0, 40.0, 520.0, 620.0, 16.1],
         ["DAL1",  "BLR", "Staples", "Toor Dal",        1000.0, 80.0, 150.0, 180.0, 16.7]],
        columns=["product_id", "city", "category", "base_product", "pack_grams",
                 "q0_units_wk", "p0_price", "mrp", "disc0"])
    elast_df = pd.DataFrame(
        [["RICE1", "BLR", -1.8, 0.30, -1.2],    # reliably elastic -> promo pays
         ["RICE5", "BLR", -0.4, 0.50, -0.3],    # not reliable -> clamp kills promo upside
         ["DAL1",  "BLR", -2.2, 0.40, -1.5]],
        columns=["product_id", "city", "own_elast", "own_sd", "promo_elast"])
    cross_df = pd.DataFrame([["RICE1", "DAL1", 0.25], ["DAL1", "RICE1", 0.25]],
                            columns=["product_i", "product_j", "cross_elast"])

    T, U, k = 6, 3, 4
    ccfg = {"horizon_weeks": T, "discount_levels_pct": [0, 5, 10, 15, 20],
            "promo_threshold_pct": 5.0, "solver": {"gap_target": 0.01, "time_limit_s": 20},
            "constraints": {
                "promotion_exclusivity":    {"enabled": True, "params": {}},
                "min_promo_duration":       {"enabled": True, "params": {"min_weeks": 2}},
                "max_promo_duration":       {"enabled": True, "params": {"max_weeks": U}},
                "min_promo_spacing":        {"enabled": True, "params": {"spacing_weeks": k}},
                "max_simultaneous_promos":  {"enabled": True, "params": {"max_per_category_week": 2}},
                "weekly_budget_cap":        {"enabled": True, "params": {"budget_pct": 0.12}},
                "competitive_defense_hold": {"enabled": True, "params": {}},
            }}
    hold_set = {("DAL1", "BLR")}
    cal, reps, audits = run_calendar(elast_df, cross_df, baseline_df, ccfg, hold_set)
    r = reps.iloc[0]
    print(f"[selftest] status={r['status']} stop={r['stop_reason']} gap={r['achieved_gap']} "
          f"time={r['solve_time_s']}s obj=Rs{r['objective_net_rev']:,.0f}")
    assert r["status"] == 0, "toy MILP should solve to optimality"
    assert (pd.isna(r["achieved_gap"])) or r["achieved_gap"] <= 0.01 + 1e-9, "gap target missed"

    # (1) exclusivity: exactly one level per cell-week.
    assert len(cal) == 3 * T and not cal.duplicated(["cell_id", "week"]).any()

    # (6) defense hold: DAL1 pinned at grid level nearest disc0=16.7 -> 15%, all weeks.
    dal = cal[cal["product_id"] == "DAL1"]
    assert (dal["disc_level_pct"] == 15.0).all() and dal["held"].all(), "defense hold broken"

    # (2)/(3): duration + spacing on the free cells.
    for pid in ["RICE1", "RICE5"]:
        y = cal[cal["product_id"] == pid].sort_values("week")["on_promo"].astype(int).tolist()
        on0 = 1  # both start >= threshold
        # max duration: any U+1 consecutive weeks contain <= U on-weeks
        for t in range(T - U):
            assert sum(y[t:t + U + 1]) <= U, f"{pid}: max duration violated"
        starts = [t for t in range(T) if y[t] == 1 and (y[t - 1] if t else on0) == 0]
        # min duration (2 wk, truncated at horizon): every start runs >= min(2, T-t) weeks
        for t in starts:
            w = min(2, T - t)
            assert sum(y[t:t + w]) == w, f"{pid}: min duration violated at start {t}"
        # spacing: no two starts within k weeks
        for a, b in zip(starts, starts[1:]):
            assert b - a >= k, f"{pid}: spacing violated ({a}->{b})"

    # (5) budget: weekly spend <= 12% of group baseline revenue.
    base_rev = float((baseline_df["q0_units_wk"] * baseline_df["p0_price"]).sum())
    wk = cal.groupby("week")["disc_spend_wk"].sum()
    assert (wk <= 0.12 * base_rev + 1.0).all(), "budget cap violated"

    # (4) max simultaneous: <= 2 non-held promos per week (DAL1 held, exempt).
    live = cal[~cal["held"]].groupby("week")["on_promo"].sum()
    assert (live <= 2).all(), "max simultaneous violated"

    # Kernel parity: calendar net_rev must equal a direct shared-kernel evaluation.
    P = de.build_problem(elast_df, cross_df, baseline_df, KERNEL_CONFIG)
    row = cal[(cal["product_id"] == "RICE1") & (cal["week"] == 1)].iloc[0]
    dv = P["disc0"].copy(); dv[0] = row["disc_level_pct"]
    v = float(de.demand_model(dv, P)[0]); pr = P["mrp"][0] * (1 - row["disc_level_pct"] / 100.0)
    assert abs(v * pr - row["net_rev_wk"]) <= 0.5 + 1e-9, "kernel parity broken"

    # Declarative property: tightening the budget in JSON alone changes the plan.
    ccfg_tight = json.loads(json.dumps(ccfg))
    ccfg_tight["constraints"]["weekly_budget_cap"]["params"]["budget_pct"] = 0.005
    cal2, reps2, _ = run_calendar(elast_df, cross_df, baseline_df, ccfg_tight, set())
    assert cal2.groupby("week")["disc_spend_wk"].sum().max() <= 0.005 * base_rev + 1.0
    assert int(cal2["on_promo"].sum()) <= int(cal["on_promo"].sum()), \
        "tighter budget should not schedule MORE promo weeks"

    # Fail-loud: unknown template name must raise.
    try:
        bad = json.loads(json.dumps(ccfg)); bad["constraints"]["made_up_rule"] = {"enabled": True}
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(bad, f); p = f.name
        try:
            load_config(p)
            raise AssertionError("unknown template name did NOT fail loud")
        finally:
            os.unlink(p)
    except ValueError as e:
        print(f"[selftest] fail-loud OK: {e}")

    print("[selftest] all promo-calendar MILP assertions passed. Exit 0.")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="PromoAI-style promo-calendar MILP (challenger)")
    ap.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                    help="constraint config JSON (default: scripts/promo/promo_constraints.json)")
    ap.add_argument("--weeks", type=int, default=None, help="override horizon_weeks")
    ap.add_argument("--gap-target", type=float, default=None, help="override MIP relative gap target")
    ap.add_argument("--time-limit", type=float, default=None, help="override per-solve time limit (s)")
    ap.add_argument("--max-groups", type=int, default=None, help="solve only the first N groups (debug)")
    ap.add_argument("--selftest", action="store_true", help="run synthetic self-test and exit")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        sys.exit(0)
    main(args)
