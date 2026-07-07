"""
de_optimizer.py — PricingAI differential-evolution price optimizer, adapted to Blinkit.

WHAT THIS IS (business framing):
  This is the "decide the discount" engine. Given how each product's sales respond to price
  (own elasticity), how substitutes steal from each other (cross elasticity), and where each
  cell is priced today (baseline), it searches for the discount % per (product, city) that
  maximizes the chosen business KPI — revenue, volume, margin-proxy (nrw), or portfolio share —
  WITHOUT letting revenue fall below a floor, without lurching discounts (glide constraint),
  and while keeping bigger packs cheaper per gram than smaller packs (price ladder).

FAITHFUL TO PepsiCo PricingAI:
  - Log-linear demand with own + cross elasticity  (DOC Eq. 22-24)
  - Psychological-price-point (PPP) threshold bonus/penalty near round prices
  - Differential-evolution optimizer over a bounded discount vector, constraints as penalties
  - Full KPI menu incl. contribution profit (Eq. 28) and margin ratio (Eq. 29), using the
    stage6 cost economics (v4_config 50%/15%/₹10 defaults, per-SKU override columns honored)
  - Multi-run ensemble with VARIED algorithmic configurations (ROBUST_GRID, paper §3.2),
    agreement-based early stop, per-run wall-clock cap; convergence report merged into
    gates.json under 'de_robustness' during a real pricing_engine run
  - Declarative extra constraints via config['constraints'] compiled by constraints_lib
    (KPI bounds, pricing-line Eq. 36, portfolio price-change bands Eq. 37-39) — all
    DISABLED by default so the validated champion behaviour is unchanged
  - No Gurobi, no cloud. Only numpy/pandas/scipy/sklearn/statsmodels.

HONESTY CLAMPS (the whole point — no free lunch):
  - A price CUT is only credited extra volume when own_elast is *reliably* negative
    (own_elast + 1.64*own_sd < 0). Otherwise the cell is treated as inelastic: no volume upside
    from cutting price, so the optimizer won't "discover" fake demand and burn margin.
  - Predicted volume is capped at the pure power-law response q0*(price/p0)**own_elast times a
    bounded sibling multiplier — the exp() can never run away.
  - Delta-ln-price is bounded to the discount range actually reachable, so no extrapolation.

DECISION VARIABLE: discount % per (product_id, city) cell.

Author note for the integrator: this file is self-contained and has a synthetic smoke test at the
bottom. Run:  python -X utf8 scripts/pricing/de_optimizer.py
"""

from __future__ import annotations

import json
import os
import sys
import time

import numpy as np
import pandas as pd
from scipy.optimize import differential_evolution

# ---------------------------------------------------------------------------
# Cost economics for the profit / margin KPIs (paper Eq. 28-29).
# Defaults come from v4_config (DEFAULT_COGS_PCT / DEFAULT_COMMISSION_PCT /
# DEFAULT_FULFILLMENT_FEE) — the same constants stage6_economics uses:
#     variable_cost = cogs + commission_pct * price + fulfillment      (economics.py:48)
# with cogs defaulting to mrp * DEFAULT_COGS_PCT. If v4_config is unreachable
# (standalone import from an odd cwd) the literal repo defaults are used, so the
# module stays self-contained. Per-SKU overrides: if baseline_df carries columns
# 'cogs' / 'commission_pct' / 'fulfillment_fee', those win (same convention as
# stage6_economics._get_costs).
# ---------------------------------------------------------------------------
_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
try:
    import v4_config as _v4cfg
except Exception:
    try:
        sys.path.insert(0, _ROOT)
        import v4_config as _v4cfg
    except Exception:
        _v4cfg = None
DEFAULT_COGS_PCT = getattr(_v4cfg, "DEFAULT_COGS_PCT", 0.50)
DEFAULT_COMMISSION_PCT = getattr(_v4cfg, "DEFAULT_COMMISSION_PCT", 0.15)
DEFAULT_FULFILLMENT_FEE = getattr(_v4cfg, "DEFAULT_FULFILLMENT_FEE", 10.0)

# ---------------------------------------------------------------------------
# Module-level state shared with demand_model().
#
# demand_model(disc_vec, P) must have the fixed signature required by the spec,
# but it needs the full problem context (elasticities, cross pairs, baselines,
# config). We stash that context in P (a dict "problem") so the function stays
# pure w.r.t. its arguments and there are no globals to get stale.
# ---------------------------------------------------------------------------

Z_RELIABLE = 1.64  # ~95% one-sided: own_elast must be reliably negative to credit a cut

# Default problem config used when a caller (e.g. whatif.simulate) only wants to
# assemble the problem dict P and evaluate demand_model — NOT run the optimizer.
# These affect only the dln bounds (reachable discount range) and the PPP thresholds,
# both of which the shared demand kernel reads out of P. The optimizer always passes
# its OWN full config, so this default never changes optimize()'s behaviour.
DEFAULT_CONFIG = {
    "disc_lo": 0.0,
    "disc_hi": 45.0,
    "psych_prices": [49, 99, 149, 199, 249, 299, 349, 399, 449, 499, 599, 699, 799, 999],
}


def _psych_multiplier(price, mrp, psych_prices, psi=0.03):
    """
    Psychological price-point (PPP) step. Returns a MULTIPLIER (not a log term) so the
    caller can difference it cleanly: exp(PPP(price)-PPP(p0)) == mult(price)/mult(p0).

    Behaviour, per spec: landing just UNDER a threshold (e.g. 195-199 under 199) gets a small
    bonus (+psi); sitting just ABOVE a threshold (e.g. 200-205 above 199) gets a small penalty
    (-psi). Effect is modest (default +/-3%) and only near thresholds.

    Vectorized over price/mrp arrays.
    """
    price = np.asarray(price, dtype=float)
    mult = np.ones_like(price)
    if not psych_prices:
        return mult
    # Window width scales with price so 49 and 499 both get a sensible band.
    for i in range(price.shape[0]):
        p = price[i]
        if p <= 0:
            continue
        band = max(2.0, 0.03 * p)  # e.g. +/-3% of the threshold, min 2 rupees
        best = 0.0
        for t in psych_prices:
            if t <= 0:
                continue
            if (p <= t) and (t - p <= band):
                # just under the threshold -> bonus, strongest right at the threshold
                strength = 1.0 - (t - p) / band
                best = max(best, +psi * strength)
            elif (p > t) and (p - t <= band):
                # just over the threshold -> penalty
                strength = 1.0 - (p - t) / band
                best = min(best, -psi * strength) if best <= 0 else best
                # combine: take the signed effect closest to threshold; simplest is to
                # let the nearest threshold win. We approximate by keeping the max-magnitude.
                if abs(-psi * strength) > abs(best):
                    best = -psi * strength
        mult[i] = 1.0 + best
    return mult


def build_problem(elast_df, cross_df, baseline_df, config=None):
    """
    Assemble a compact, index-aligned problem dict `P` from the shared schemas — the
    EXACT problem context both optimize() and demand_model() operate on.

    This is the shared entry point so callers (e.g. whatif.simulate) can build the same
    P the optimizer uses internally and feed a discount vector straight into
    demand_model(disc_vec, P). That guarantees the what-if readout uses the optimizer's
    own clamped demand kernel — no divergent copy.

    Parameters
    ----------
    elast_df    : own_elast (NEGATIVE), own_sd, promo_elast per (product_id, city).
    cross_df    : sparse within-category substitute pairs (product_i, product_j, cross_elast).
    baseline_df : q0_units_wk, p0_price, mrp, disc0, pack_grams, base_product per cell.
    config      : optimizer/problem dict. Only disc_lo / disc_hi (reachable dln bounds) and
                  psych_prices (PPP thresholds) are read here. If None, DEFAULT_CONFIG is
                  used (sensible full-range bounds) — appropriate for a what-if evaluation
                  that only needs demand_model, not the optimizer.

    Returns
    -------
    P : dict with n, cells, idx, q0/p0/mrp/disc0/own/own_sd/promo/pack_g, reliable_neg mask,
        cross_pairs, dln_lo/dln_hi bounds, ladder_pairs, and config.
    """
    if config is None:
        config = DEFAULT_CONFIG
    b = baseline_df.copy()
    e = elast_df.copy()

    # Join baseline with elasticity on (product_id, city).
    cells = b.merge(
        e[["product_id", "city", "own_elast", "own_sd", "promo_elast"]],
        on=["product_id", "city"],
        how="left",
    ).reset_index(drop=True)

    # Fill any missing elasticity: treat as inelastic (0), high uncertainty -> not reliable.
    cells["own_elast"] = cells["own_elast"].fillna(0.0)
    cells["own_sd"] = cells["own_sd"].fillna(1e9)
    cells["promo_elast"] = cells["promo_elast"].fillna(0.0)

    n = len(cells)
    idx = {(r.product_id, r.city): i for i, r in enumerate(cells.itertuples())}

    # pack_grams may live on baseline_df; if absent, default to NaN -> ladder skipped for it.
    if "pack_grams" not in cells.columns:
        cells["pack_grams"] = np.nan

    # base_product for ladder grouping; if absent, no ladder pairs.
    if "base_product" not in cells.columns:
        cells["base_product"] = cells["product_id"].astype(str)

    P = {
        "n": n,
        "cells": cells,
        "idx": idx,
        "q0": cells["q0_units_wk"].to_numpy(dtype=float),
        "p0": cells["p0_price"].to_numpy(dtype=float),
        "mrp": cells["mrp"].to_numpy(dtype=float),
        "disc0": cells["disc0"].to_numpy(dtype=float),
        "own": cells["own_elast"].to_numpy(dtype=float),
        "own_sd": cells["own_sd"].to_numpy(dtype=float),
        "promo": cells["promo_elast"].to_numpy(dtype=float),
        "pack_g": cells["pack_grams"].to_numpy(dtype=float),
        "config": config,
    }

    # Reliability mask: a price cut only earns volume if own_elast is reliably negative.
    P["reliable_neg"] = (P["own"] + Z_RELIABLE * P["own_sd"]) < 0.0

    # Cross-elasticity as a sparse list of (i, j, cross) using the aligned index.
    cross_pairs = []
    if cross_df is not None and len(cross_df) > 0:
        # cross_df has product_i, product_j, cross_elast — but our cells are per (product, city).
        # We apply a cross pair within the SAME city (substitution happens where the shopper is).
        by_city = {}
        for r in cells.itertuples():
            by_city.setdefault(r.city, {})[r.product_id] = P["idx"][(r.product_id, r.city)]
        for r in cross_df.itertuples():
            for city, pid_map in by_city.items():
                i = pid_map.get(r.product_i)
                j = pid_map.get(r.product_j)
                if i is not None and j is not None and i != j:
                    cross_pairs.append((i, j, float(r.cross_elast)))
    P["cross_pairs"] = cross_pairs

    # Bound Delta-ln-price to the reachable discount range so demand never extrapolates.
    disc_lo = float(config["disc_lo"])
    disc_hi = float(config["disc_hi"])
    price_at_hi = P["mrp"] * (1.0 - disc_hi / 100.0)  # lowest price -> most negative dln
    price_at_lo = P["mrp"] * (1.0 - disc_lo / 100.0)  # highest price -> most positive dln
    with np.errstate(divide="ignore", invalid="ignore"):
        dln_min = np.log(np.maximum(price_at_hi, 1e-9)) - np.log(np.maximum(P["p0"], 1e-9))
        dln_max = np.log(np.maximum(price_at_lo, 1e-9)) - np.log(np.maximum(P["p0"], 1e-9))
    P["dln_lo"] = np.minimum(dln_min, dln_max)
    P["dln_hi"] = np.maximum(dln_min, dln_max)

    # Ladder pairs: within a base_product, a bigger pack must be <= tol * per-gram price of the
    # next-smaller pack. Precompute ordered (small_i, big_i) pairs per base_product.
    ladder_pairs = []
    grp = cells.reset_index().rename(columns={"index": "row"})
    for (_bp, _city), sub in grp.groupby(["base_product", "city"]):
        sub = sub.dropna(subset=["pack_grams"])
        sub = sub.sort_values("pack_grams")
        rows = sub["row"].tolist()
        for k in range(1, len(rows)):
            ladder_pairs.append((rows[k - 1], rows[k]))  # (smaller, bigger)
    P["ladder_pairs"] = ladder_pairs

    # ── Cost economics (Eq. 28-29 inputs) ────────────────────────────────────
    # Per-unit COGS (₹), commission (% of shelf price), fulfillment fee (₹/unit).
    # Per-SKU columns on baseline_df override the v4_config defaults; otherwise
    # cogs = mrp * DEFAULT_COGS_PCT (the documented 50/15/₹10 proxy — an HONEST
    # default, flagged as such until the owner supplies true per-SKU costs).
    if "cogs" in cells.columns:
        P["cogs"] = cells["cogs"].fillna(P["mrp"] * DEFAULT_COGS_PCT).to_numpy(dtype=float)
    else:
        P["cogs"] = P["mrp"] * DEFAULT_COGS_PCT
    if "commission_pct" in cells.columns:
        P["comm_pct"] = cells["commission_pct"].fillna(DEFAULT_COMMISSION_PCT).to_numpy(dtype=float)
    else:
        P["comm_pct"] = np.full(n, float(DEFAULT_COMMISSION_PCT))
    if "fulfillment_fee" in cells.columns:
        P["fulfil"] = cells["fulfillment_fee"].fillna(DEFAULT_FULFILLMENT_FEE).to_numpy(dtype=float)
    else:
        P["fulfil"] = np.full(n, float(DEFAULT_FULFILLMENT_FEE))

    # ── Declarative extra constraints (constraints_lib) ──────────────────────
    # config['constraints'] is an OPTIONAL dict (schema: DISCOUNT_PLAN/pricing/
    # pricing_constraints.json). When present, constraints_lib compiles it into
    # penalty callables f(disc_vec, P, ctx) that _penalized_objective adds on top
    # of the champion penalties. Absent (the default), extra_penalties == [] and
    # behaviour is byte-identical to the pre-constraints code path.
    P["extra_penalties"] = []
    cons_cfg = (config or {}).get("constraints")
    if cons_cfg:
        try:
            import constraints_lib  # sibling module; lazy so de_optimizer stays standalone
        except ImportError:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import constraints_lib
        P["extra_penalties"] = constraints_lib.compile_constraints(cons_cfg, P)

    return P


# Backward-compatible internal alias (kept so existing call sites / tests keep working).
_build_problem = build_problem


def demand_model(disc_vec, P):
    """
    Log-linear PricingAI demand with own + cross elasticity + PPP, plus honesty clamps.

    Parameters
    ----------
    disc_vec : array-like, length n
        Discount % per cell (the decision variable).
    P : dict
        Problem context from build_problem(): elasticities, baselines, cross pairs, config.

    Returns
    -------
    V : np.ndarray, length n
        Predicted weekly units per cell.

    Math (DOC Eq. 22-24):
        price_i   = mrp_i * (1 - disc_i/100)
        dln p_i   = ln(price_i) - ln(p0_i)                       (bounded to reachable range)
        V_i = q0_i * exp( own_i*dln p_i + Sum_j cross_ij*dln p_j
                          + ln PPP(price_i) - ln PPP(p0_i) )
        capped at q0_i * (price_i/p0_i)**own_i * sibling_mult    (no runaway exp)
    """
    disc = np.asarray(disc_vec, dtype=float)
    n = P["n"]
    mrp = P["mrp"]
    p0 = P["p0"]
    q0 = P["q0"]
    own = P["own"]

    price = mrp * (1.0 - disc / 100.0)
    price = np.maximum(price, 1e-6)

    with np.errstate(divide="ignore", invalid="ignore"):
        dln = np.log(price) - np.log(np.maximum(p0, 1e-9))
    # Bound dln to the reachable range (no extrapolation beyond observed discounts).
    dln = np.clip(dln, P["dln_lo"], P["dln_hi"])

    # Honesty clamp #1: a price CUT (dln < 0) only earns volume where own_elast is reliably
    # negative. Where it isn't, zero out the negative-side own effect so cutting can't
    # manufacture demand. Price RAISES (dln > 0) always shrink volume (kept honest downside).
    own_eff = own.copy()
    cut_mask = dln < 0.0
    not_reliable = ~P["reliable_neg"]
    kill = cut_mask & not_reliable
    # For those cells, treat own effect on the cut as ~0 (inelastic to a cut).
    own_term = np.where(kill, 0.0, own_eff * dln)

    # Cross term: Sum_j cross_ij * dln p_j.
    cross_term = np.zeros(n)
    for (i, j, c) in P["cross_pairs"]:
        cross_term[i] += c * dln[j]

    # Psychological price-point differenced multiplier.
    psych_prices = P["config"].get("psych_prices", [])
    ppp_now = _psych_multiplier(price, mrp, psych_prices)
    ppp_base = _psych_multiplier(p0, mrp, psych_prices)
    with np.errstate(divide="ignore", invalid="ignore"):
        ppp_log = np.log(np.maximum(ppp_now, 1e-9)) - np.log(np.maximum(ppp_base, 1e-9))

    V = q0 * np.exp(own_term + cross_term + ppp_log)

    # Honesty clamp #2: cap at pure power-law own response * bounded sibling multiplier.
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.maximum(price, 1e-9) / np.maximum(p0, 1e-9)
    # power-law cap uses the (possibly killed) own effect so it stays consistent with the clamp
    own_for_cap = np.where(kill, 0.0, own)
    power_cap = q0 * np.power(ratio, own_for_cap)
    sibling_mult = np.exp(np.clip(cross_term, -0.5, 0.5))  # sibling swing bounded to +/-~65%
    cap = power_cap * sibling_mult
    # Cap only bites on the upside (don't let exp overshoot); downside stays as-is.
    V = np.minimum(V, np.maximum(cap, 0.0))
    V = np.maximum(V, 0.0)
    return V


def _kpis(V, price, P):
    """
    Compute the KPI chain from a volume/price vector.

    Keys (the full plan-summary chain — units, revenue, spend, profit, margin, share proxy):
      revenue : Σ V·price                              (shelf/GMV revenue)
      volume  : Σ V                                    (units — 'units' in the chain report)
      nrw     : revenue / Σ V·pack_kg                  (net revenue per weight, ₹/kg)
      share   : Σ V / Σ q0                             (portfolio volume-retention proxy;
                true market share needs competitor volumes, which are not observed)
      spend   : Σ V·(mrp − price)                      (weekly discount spend, ₹)
      profit  : Σ V·(price − cogs − comm_pct·price − fulfil)   (Eq. 28, contribution profit;
                variable-cost formula identical to stage6_economics/economics.py:48)
      margin  : profit / revenue                       (Eq. 29, portfolio margin ratio)

    HONESTY NOTE: profit/margin use the v4_config 50%/15%/₹10 cost proxies unless
    baseline_df supplied per-SKU cogs / commission_pct / fulfillment_fee columns.
    """
    revenue = float(np.sum(V * price))
    volume = float(np.sum(V))
    pack_kg = P["pack_g"] / 1000.0
    weight_sold = float(np.sum(V * np.where(np.isnan(pack_kg), 0.0, pack_kg)))
    nrw = revenue / weight_sold if weight_sold > 1e-9 else 0.0  # net revenue per weight (INR/kg)
    q0_total = float(np.sum(P["q0"]))
    share = volume / q0_total if q0_total > 1e-9 else 0.0  # portfolio volume retention proxy
    # Discount spend: gap to MRP funded per unit sold.
    spend = float(np.sum(V * np.maximum(P["mrp"] - price, 0.0)))
    # Eq. 28: contribution profit with the stage6 variable-cost formula.
    cogs = P.get("cogs")
    if cogs is None:  # P built by an older caller — derive the same defaults on the fly
        cogs = P["mrp"] * DEFAULT_COGS_PCT
    comm = P.get("comm_pct", DEFAULT_COMMISSION_PCT)
    fulfil = P.get("fulfil", DEFAULT_FULFILLMENT_FEE)
    vc = cogs + comm * price + fulfil
    profit = float(np.sum(V * (price - vc)))
    margin = profit / revenue if abs(revenue) > 1e-9 else 0.0  # Eq. 29
    return {"revenue": revenue, "volume": volume, "nrw": nrw, "share": share,
            "spend": spend, "profit": profit, "margin": margin}


def _penalized_objective(disc_vec, P, base_kpis):
    """
    Objective for DE: minimize  -(chosen KPI, normalized)  + penalties.

    Penalties (soft constraints):
      (a) revenue floor:  revenue >= floor_frac * baseline_revenue
      (b) glide:          |disc_i - disc0_i| <= max_disc_change_ppt
      (c) price ladder:   bigger pack per-gram <= tol * smaller pack per-gram
      (d) box bounds handled by DE bounds directly.
      (e) EXTRA declarative penalties from P['extra_penalties'] (compiled by
          constraints_lib from config['constraints']; empty by default so the
          champion behaviour is unchanged). Each callable gets (disc, P, ctx)
          where ctx carries the already-computed V/price/kpis/base_kpis so no
          second demand_model evaluation is needed.

    The objective KPI (config['kpi']) may be any _kpis key: revenue, volume,
    nrw, share, spend, profit, margin — or 'combo' (Sec 2.2.3), the blend
    combo_alpha*revenue_norm + (1-combo_alpha)*profit_norm (default alpha 0.5).
    """
    cfg = P["config"]
    disc = np.asarray(disc_vec, dtype=float)
    price = np.maximum(P["mrp"] * (1.0 - disc / 100.0), 1e-6)
    V = demand_model(disc, P)
    k = _kpis(V, price, P)

    # Normalization uses |baseline| so the DE's sense (maximize the KPI) is preserved
    # even when a baseline is NEGATIVE (possible for profit/margin under the 50% COGS
    # proxy on deep-discount groups). For every positive baseline this is identical to
    # dividing by the baseline itself, so the champion revenue path is unchanged.
    def _norm(name):
        base_val = base_kpis[name]
        return k[name] / max(abs(base_val), 1e-9) if abs(base_val) > 1e-9 else k[name]

    kpi_name = cfg.get("kpi", "revenue")
    if kpi_name == "combo":
        # Sec 2.2.3 blended objective: alpha*revenue_norm + (1-alpha)*profit_norm.
        alpha = float(cfg.get("combo_alpha", 0.5))
        reward = alpha * _norm("revenue") + (1.0 - alpha) * _norm("profit")
    else:
        reward = _norm(kpi_name)  # normalized so all KPIs are ~O(1)

    pen = 0.0
    BIG = 100.0

    # (a) revenue floor
    floor = cfg.get("revenue_floor_frac", 0.98) * base_kpis["revenue"]
    if k["revenue"] < floor and base_kpis["revenue"] > 1e-9:
        pen += BIG * ((floor - k["revenue"]) / base_kpis["revenue"]) ** 2

    # (b) glide constraint
    max_ch = cfg.get("max_disc_change_ppt", 100.0)
    over = np.maximum(np.abs(disc - P["disc0"]) - max_ch, 0.0)
    pen += BIG * float(np.sum((over / max(max_ch, 1e-6)) ** 2))

    # (c) price ladder (per-gram): bigger pack must be <= tol * smaller pack per-gram
    tol = cfg.get("ladder_tol", 1.0)
    for (small_i, big_i) in P["ladder_pairs"]:
        pg_s = P["pack_g"][small_i]
        pg_b = P["pack_g"][big_i]
        if not (np.isfinite(pg_s) and np.isfinite(pg_b)) or pg_s <= 0 or pg_b <= 0:
            continue
        ppg_small = price[small_i] / pg_s
        ppg_big = price[big_i] / pg_b
        violation = ppg_big - tol * ppg_small
        if violation > 0:
            pen += BIG * (violation / max(ppg_small, 1e-6)) ** 2

    # (e) extra declarative penalties (constraints_lib) — no-op when list is empty
    extra = P.get("extra_penalties") or []
    if extra:
        ctx = {"V": V, "price": price, "kpis": k, "base_kpis": base_kpis}
        for f in extra:
            pen += float(f(disc, P, ctx))

    return -reward + pen


# ---------------------------------------------------------------------------
# DE ROBUSTNESS PROTOCOL (paper §3.2, scaled so production runtime is unchanged)
#
# Instead of n_seeds identical-config runs, each run s draws its algorithmic
# configuration from ROBUST_GRID[s % len(ROBUST_GRID)] with seed=s. Entry 0 is
# EXACTLY the pre-protocol settings (mutation (0.5,1.0), recombination 0.7,
# popsize 15, latinhypercube), so run 0 is bit-identical to the old behaviour
# and the default n_seeds=2 adds only one varied run. Stopping:
#   - agreement: once >=2 runs completed, stop early if their best objectives
#     agree within config['de_agree_tol'] (relative; default 1e-3). NOTE: the
#     build spec said "within ladder_tol", but ladder_tol is a per-gram price
#     ratio (default 1.0) — the wrong unit for an O(1) normalized objective —
#     so a dedicated relative tolerance is used instead (honest deviation).
#   - time cap: each run is halted via callback after config['de_time_cap_s']
#     seconds (default 120 s/run) and returns its best-so-far.
# The per-group convergence report lands in kpi_summary['robustness'] and is
# merged into DISCOUNT_PLAN/pricing/gates.json under 'de_robustness' — but ONLY
# when gates.json was (re)written during this same process (i.e. a real
# pricing_engine.py run, which writes gates.json before optimizing). Standalone
# smoke tests therefore never pollute the production gates file.
# ---------------------------------------------------------------------------
ROBUST_GRID = [
    {"mutation": (0.5, 1.0), "recombination": 0.70, "popsize": 15, "init": "latinhypercube"},  # 0 = champion anchor
    {"mutation": (0.3, 0.7), "recombination": 0.90, "popsize": 20, "init": "latinhypercube"},
    {"mutation": (0.8, 1.4), "recombination": 0.50, "popsize": 10, "init": "latinhypercube"},
    {"mutation": (0.7, 1.0), "recombination": 0.95, "popsize": 12, "init": "latinhypercube"},
    {"mutation": 0.9,        "recombination": 0.40, "popsize": 18, "init": "latinhypercube"},
    {"mutation": 0.6,        "recombination": 0.80, "popsize": 25, "init": "sobol"},
    {"mutation": (0.4, 1.2), "recombination": 0.60, "popsize": 15, "init": "sobol"},
    {"mutation": (0.2, 0.9), "recombination": 0.70, "popsize": 30, "init": "sobol"},
]

_IMPORT_TS = time.time()          # process start; gates merge requires gates.json newer than this
_GATES_PATH = os.path.join(_ROOT, "DISCOUNT_PLAN", "pricing", "gates.json")
_ROBUSTNESS_GROUPS = {}           # label -> per-group convergence record (process lifetime)


class _TimeBudget:
    """DE callback: returns True (halt, keep best-so-far) once the wall-clock
    budget is exhausted. budget_s=None disables the cap."""

    def __init__(self, budget_s):
        self.budget = budget_s
        self.t0 = time.monotonic()
        self.tripped = False

    def __call__(self, xk, convergence=0.0):
        if self.budget is not None and (time.monotonic() - self.t0) > float(self.budget):
            self.tripped = True
            return True
        return False


def _robustness_summary():
    """Aggregate the per-group records into the gates.json 'de_robustness' block."""
    groups = _ROBUSTNESS_GROUPS
    n_runs = sum(g["n_completed"] for g in groups.values())
    spreads = [g["spread_rel"] for g in groups.values() if g["spread_rel"] is not None]
    return {
        "protocol": "varied-config multi-run DE (ROBUST_GRID) + agreement stop + per-run time cap",
        "n_groups": len(groups),
        "n_runs_total": n_runs,
        "n_groups_agreed": sum(1 for g in groups.values() if g["agreed"]),
        "n_groups_stopped_early": sum(1 for g in groups.values() if g["stopped_early"]),
        "n_runs_timed_out": sum(
            sum(1 for r in g["runs"] if r["stopped_by"] == "time_cap") for g in groups.values()),
        "max_spread_rel": max(spreads) if spreads else None,
        "groups": groups,
    }


def _merge_robustness_into_gates(config):
    """Write the accumulated convergence report into gates.json['de_robustness'].
    Fires only when gates.json exists AND was written during this process (a real
    pricing_engine run writes it just before optimizing); config['gates_robustness']
    = False disables it explicitly (used by the smoke test)."""
    if not config.get("gates_robustness", True):
        return False
    try:
        if not os.path.exists(_GATES_PATH):
            return False
        if os.path.getmtime(_GATES_PATH) < _IMPORT_TS - 1.0:
            return False  # stale file from an earlier run — don't rewrite history
        with open(_GATES_PATH, "r", encoding="utf-8") as fh:
            gates = json.load(fh)
        gates["de_robustness"] = _robustness_summary()
        with open(_GATES_PATH, "w", encoding="utf-8") as fh:
            json.dump(gates, fh, indent=2, default=str)
        return True
    except Exception:
        return False  # reporting must never break the optimizer


def optimize(elast_df, cross_df, baseline_df, config):
    """
    Search for the discount % per cell that maximizes config['kpi'] subject to constraints.

    Parameters
    ----------
    elast_df    : own_elast (NEGATIVE), own_sd, promo_elast per (product_id, city).
    cross_df    : sparse within-category substitute pairs (product_i, product_j, cross_elast>0).
    baseline_df : q0_units_wk, p0_price, mrp, disc0, pack_grams, base_product per cell.
                  Optional per-SKU cost columns: cogs, commission_pct, fulfillment_fee.
    config      : optimizer dict (kpi in {revenue, volume, nrw, share, spend, profit, margin,
                  combo}, combo_alpha, disc_lo, disc_hi, max_disc_change_ppt,
                  revenue_floor_frac, psych_prices, ladder_tol, n_seeds, de_agree_tol,
                  de_time_cap_s, constraints).

    Returns
    -------
    reco_df : per cell — product_id, city, base_disc, opt_disc, base_price, opt_price,
              pred_units_delta_pct, pred_rev_delta_pct.
    kpi_summary : dict — baseline vs optimized KPI chain (units/volume, revenue, spend,
              profit, margin, nrw, share proxy) + n_cells_up/down + feasibility flags +
              'robustness' (per-run convergence report of the varied-config ensemble).
    """
    P = build_problem(elast_df, cross_df, baseline_df, config)
    n = P["n"]

    disc_lo = float(config["disc_lo"])
    disc_hi = float(config["disc_hi"])
    max_ch = float(config.get("max_disc_change_ppt", 100.0))

    # Per-cell box bounds. The GLIDE constraint is the hard, always-honored limit: every
    # returned discount must satisfy |opt_disc - disc0| <= max_ch, even for cells whose disc0
    # sits OUTSIDE [disc_lo, disc_hi]. So build the glide window FIRST, then intersect it with
    # the global [disc_lo, disc_hi] only where that intersection is non-empty.
    disc0 = P["disc0"]
    glide_lo = disc0 - max_ch
    glide_hi = disc0 + max_ch
    # Intersection of the glide window with the global box.
    lo_cell = np.maximum(glide_lo, disc_lo)
    hi_cell = np.minimum(glide_hi, disc_hi)
    # Where the intersection is EMPTY, disc0 is too far outside [disc_lo, disc_hi] to reach the
    # box in a single glide step. Keep the cell inside its glide window and let it WALK toward
    # the box by at most max_ch this week (converges over multiple weeks) — never snapping:
    #   disc0 above disc_hi -> walk DOWN, window [disc0 - max_ch, disc0]
    #   disc0 below disc_lo -> walk UP,   window [disc0, disc0 + max_ch]
    empty = lo_cell > hi_cell
    above = empty & (disc0 > disc_hi)
    below = empty & (disc0 < disc_lo)
    lo_cell = np.where(above, glide_lo, lo_cell)
    hi_cell = np.where(above, disc0, hi_cell)
    lo_cell = np.where(below, disc0, lo_cell)
    hi_cell = np.where(below, glide_hi, hi_cell)
    bounds = [(float(lo_cell[i]), float(hi_cell[i])) for i in range(n)]

    # Baseline KPIs at current discounts.
    base_price = np.maximum(P["mrp"] * (1.0 - P["disc0"] / 100.0), 1e-6)
    base_V = demand_model(P["disc0"], P)
    base_kpis = _kpis(base_V, base_price, P)

    # Multi-run DE ensemble (robustness protocol — see ROBUST_GRID note above):
    # run s uses seed=s and ROBUST_GRID[s % 8]'s mutation/recombination/popsize/init.
    # Keep the best (lowest penalized objective); stop early on agreement.
    n_seeds = int(config.get("n_seeds", 4))
    n_seeds = max(1, min(n_seeds, 8))
    agree_tol = float(config.get("de_agree_tol", 1e-3))     # relative objective agreement
    time_cap = config.get("de_time_cap_s", 120.0)           # seconds per run; None = uncapped

    best_obj = np.inf
    best_x = P["disc0"].copy()
    run_log = []
    stopped_early = False

    if n == 0:
        reco_df = pd.DataFrame(
            columns=[
                "product_id", "city", "base_disc", "opt_disc", "base_price", "opt_price",
                "pred_units_delta_pct", "pred_rev_delta_pct",
            ]
        )
        kpi_summary = {
            "baseline": base_kpis, "optimized": base_kpis,
            "n_cells_up": 0, "n_cells_down": 0,
        }
        return reco_df, kpi_summary

    for s in range(n_seeds):
        g = ROBUST_GRID[s % len(ROBUST_GRID)]
        budget = _TimeBudget(time_cap)
        t0 = time.monotonic()
        result = differential_evolution(
            _penalized_objective,
            bounds,
            args=(P, base_kpis),
            seed=s,
            maxiter=60,
            popsize=g["popsize"],
            tol=1e-6,
            mutation=g["mutation"],
            recombination=g["recombination"],
            polish=True,
            init=g["init"],
            updating="deferred",
            callback=budget,
        )
        elapsed = time.monotonic() - t0
        if budget.tripped:
            stopped_by = "time_cap"
        elif result.success:
            stopped_by = "converged"
        else:
            stopped_by = "maxiter"
        run_log.append({
            "run": s, "seed": s, "mutation": g["mutation"],
            "recombination": g["recombination"], "popsize": g["popsize"], "init": g["init"],
            "objective": float(result.fun), "nit": int(getattr(result, "nit", -1)),
            "nfev": int(getattr(result, "nfev", -1)), "elapsed_s": round(elapsed, 3),
            "stopped_by": stopped_by,
        })
        if result.fun < best_obj:
            best_obj = result.fun
            best_x = result.x.copy()
        # Agreement stop: if the completed runs' best objectives already agree
        # within tolerance, more varied runs are unlikely to move the answer.
        if len(run_log) >= 2 and s < n_seeds - 1:
            objs = [r["objective"] for r in run_log]
            spread_rel = (max(objs) - min(objs)) / max(abs(min(objs)), 1e-9)
            if spread_rel <= agree_tol:
                stopped_early = True
                break

    # Clip to the per-cell glide window (guarantees hard feasibility on bounds + glide).
    opt_disc = np.clip(best_x, lo_cell, hi_cell)

    # Deterministic LADDER REPAIR (feasibility projection):
    # DE's ladder penalty is soft, so tiny per-gram inversions can leak through. Fix them
    # exactly: for each (smaller, bigger) pack pair, if the bigger pack's per-gram price
    # exceeds tol * smaller's, deepen the bigger pack's discount just enough to comply,
    # but never past its own glide/box floor (lo_cell). If the required discount is out of
    # reach, we go as deep as allowed (residual violation is then reported, not hidden).
    # Iterate a few times because packs can chain (250g < 500g < 1kg < 5kg).
    tol = float(config.get("ladder_tol", 1.0))
    for _ in range(len(P["ladder_pairs"]) + 1):
        changed = False
        for (small_i, big_i) in P["ladder_pairs"]:
            pg_s = P["pack_g"][small_i]
            pg_b = P["pack_g"][big_i]
            if not (np.isfinite(pg_s) and np.isfinite(pg_b)) or pg_s <= 0 or pg_b <= 0:
                continue
            price_s = P["mrp"][small_i] * (1.0 - opt_disc[small_i] / 100.0)
            price_b = P["mrp"][big_i] * (1.0 - opt_disc[big_i] / 100.0)
            ppg_s = price_s / pg_s
            ppg_b = price_b / pg_b
            if ppg_b > tol * ppg_s + 1e-9:
                # Required big-pack price so ppg_b == tol * ppg_s.
                target_price_b = tol * ppg_s * pg_b
                # disc that yields target_price_b: disc = 100*(1 - target/mrp)
                if P["mrp"][big_i] > 1e-9:
                    req_disc = 100.0 * (1.0 - target_price_b / P["mrp"][big_i])
                    new_disc = min(max(req_disc, opt_disc[big_i]), hi_cell[big_i])
                    new_disc = max(new_disc, lo_cell[big_i])
                    if new_disc > opt_disc[big_i] + 1e-9:
                        opt_disc[big_i] = new_disc
                        changed = True
        if not changed:
            break
    opt_price = np.maximum(P["mrp"] * (1.0 - opt_disc / 100.0), 1e-6)
    opt_V = demand_model(opt_disc, P)
    opt_kpis = _kpis(opt_V, opt_price, P)

    # Per-cell deltas.
    with np.errstate(divide="ignore", invalid="ignore"):
        units_delta_pct = np.where(base_V > 1e-9, (opt_V - base_V) / base_V * 100.0, 0.0)
        base_rev_cell = base_V * base_price
        opt_rev_cell = opt_V * opt_price
        rev_delta_pct = np.where(
            base_rev_cell > 1e-9, (opt_rev_cell - base_rev_cell) / base_rev_cell * 100.0, 0.0
        )

    cells = P["cells"]
    reco_df = pd.DataFrame(
        {
            "product_id": cells["product_id"].values,
            "city": cells["city"].values,
            "base_disc": np.round(P["disc0"], 4),
            "opt_disc": np.round(opt_disc, 4),
            "base_price": np.round(base_price, 4),
            "opt_price": np.round(opt_price, 4),
            "pred_units_delta_pct": np.round(units_delta_pct, 4),
            "pred_rev_delta_pct": np.round(rev_delta_pct, 4),
        }
    )

    n_up = int(np.sum(opt_disc > P["disc0"] + 1e-6))
    n_down = int(np.sum(opt_disc < P["disc0"] - 1e-6))

    # Report any residual ladder violation left after repair (honest, not hidden).
    ladder_ok = True
    for (small_i, big_i) in P["ladder_pairs"]:
        pg_s, pg_b = P["pack_g"][small_i], P["pack_g"][big_i]
        if not (np.isfinite(pg_s) and np.isfinite(pg_b)) or pg_s <= 0 or pg_b <= 0:
            continue
        ppg_s = opt_price[small_i] / pg_s
        ppg_b = opt_price[big_i] / pg_b
        if ppg_b > config.get("ladder_tol", 1.0) * ppg_s + 1e-6:
            ladder_ok = False
            break

    # ── Convergence report (robustness protocol receipt) ─────────────────────
    objs = [r["objective"] for r in run_log]
    spread_rel = ((max(objs) - min(objs)) / max(abs(min(objs)), 1e-9)) if len(objs) >= 2 else None
    for r in run_log:
        r["is_best"] = bool(abs(r["objective"] - best_obj) < 1e-12)
    robustness = {
        "n_planned": n_seeds,
        "n_completed": len(run_log),
        "stopped_early": stopped_early,
        "agreed": bool(spread_rel is not None and spread_rel <= agree_tol),
        "spread_rel": spread_rel,
        "agree_tol": agree_tol,
        "time_cap_s": time_cap,
        "runs": run_log,
    }
    # Register this group in the process-level accumulator and (only during a real
    # pricing_engine run — see _merge_robustness_into_gates) update gates.json.
    label_cat = str(cells["category"].iloc[0]) if "category" in cells.columns and len(cells) else "?"
    label_city = str(cells["city"].iloc[0]) if len(cells) else "?"
    _ROBUSTNESS_GROUPS[f"{label_cat}|{label_city}"] = dict(robustness, n_cells=int(n))
    _merge_robustness_into_gates(config)

    kpi_summary = {
        "baseline": {k: round(v, 4) for k, v in base_kpis.items()},
        "optimized": {k: round(v, 4) for k, v in opt_kpis.items()},
        "n_cells_up": n_up,     # cells where discount was INCREASED
        "n_cells_down": n_down, # cells where discount was DECREASED
        "kpi_target": config.get("kpi", "revenue"),
        "revenue_floor_ok": bool(
            opt_kpis["revenue"] >= config.get("revenue_floor_frac", 0.98) * base_kpis["revenue"]
            - 1e-6
        ),
        "ladder_ok": bool(ladder_ok),
        "robustness": robustness,
    }
    return reco_df, kpi_summary


# ---------------------------------------------------------------------------
# Smoke test: tiny synthetic panel, prints results, exits 0.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    # Two base products x one city. 24 Mantra-style: Sonamasuri Rice 1kg & 5kg (a ladder pair),
    # plus Toor Dal 1kg (a substitute cross pair with Rice 1kg).
    baseline_df = pd.DataFrame(
        [
            # product_id, city, category, base_product, pack_grams, q0_units_wk, p0_price, mrp, disc0
            ["RICE1", "BLR", "Staples", "Sonamasuri Rice", 1000.0, 120.0, 110.0, 130.0, 15.4],
            ["RICE5", "BLR", "Staples", "Sonamasuri Rice", 5000.0, 40.0, 520.0, 620.0, 16.1],
            ["DAL1",  "BLR", "Staples", "Toor Dal",        1000.0, 80.0, 150.0, 180.0, 16.7],
        ],
        columns=[
            "product_id", "city", "category", "base_product", "pack_grams",
            "q0_units_wk", "p0_price", "mrp", "disc0",
        ],
    )

    elast_df = pd.DataFrame(
        [
            # product_id, city, own_elast (NEG), own_sd, promo_elast
            ["RICE1", "BLR", -1.8, 0.30, -1.2],  # reliably elastic -> cuts earn volume
            ["RICE5", "BLR", -0.4, 0.50, -0.3],  # NOT reliably negative -> cuts won't be credited
            ["DAL1",  "BLR", -2.2, 0.40, -1.5],  # reliably elastic
        ],
        columns=["product_id", "city", "own_elast", "own_sd", "promo_elast"],
    )

    cross_df = pd.DataFrame(
        [
            # Rice 1kg and Dal 1kg are weak substitutes.
            ["RICE1", "DAL1", 0.25],
            ["DAL1", "RICE1", 0.25],
        ],
        columns=["product_i", "product_j", "cross_elast"],
    )

    config = {
        "kpi": "revenue",
        "disc_lo": 5.0,
        "disc_hi": 30.0,
        "max_disc_change_ppt": 8.0,
        "revenue_floor_frac": 0.98,
        "psych_prices": [49, 99, 149, 199, 249, 299, 399, 499],
        "ladder_tol": 1.0,
        "n_seeds": 3,
        "gates_robustness": False,  # synthetic run — never touch the production gates.json
    }

    print("=== demand_model sanity check ===")
    P = build_problem(elast_df, cross_df, baseline_df, config)
    V_base = demand_model(P["disc0"], P)
    print("baseline units per cell:", np.round(V_base, 2), "(expect ~[120,40,80])")

    # Push a cut on RICE5 (inelastic-unreliable): volume should NOT balloon.
    disc_try = P["disc0"].copy()
    disc_try[1] = 30.0  # big discount on RICE5
    V_try = demand_model(disc_try, P)
    print("RICE5 units after big cut:", round(float(V_try[1]), 2),
          "(clamp: must stay ~40, NOT free-lunch up)")
    assert V_try[1] <= V_base[1] * 1.05 + 1e-6, "CLAMP FAILED: unreliable cut created volume"

    # Push a cut on RICE1 (reliably elastic): volume SHOULD rise.
    disc_try2 = P["disc0"].copy()
    disc_try2[0] = 30.0
    V_try2 = demand_model(disc_try2, P)
    print("RICE1 units after big cut:", round(float(V_try2[0]), 2),
          "(elastic: should rise above 120)")
    assert V_try2[0] > V_base[0], "elastic cut should raise volume"

    print("\n=== optimize (kpi=revenue) ===")
    reco_df, kpi_summary = optimize(elast_df, cross_df, baseline_df, config)
    print(reco_df.to_string(index=False))
    print("\nKPI summary:")
    for k, v in kpi_summary.items():
        print(f"  {k}: {v}")

    # Feasibility checks.
    assert kpi_summary["revenue_floor_ok"], "revenue floor violated"
    glide_ok = (np.abs(reco_df["opt_disc"] - reco_df["base_disc"])
                <= config["max_disc_change_ppt"] + 1e-3).all()
    assert glide_ok, "glide constraint violated"
    bounds_ok = ((reco_df["opt_disc"] >= config["disc_lo"] - 1e-6)
                 & (reco_df["opt_disc"] <= config["disc_hi"] + 1e-6)).all()
    assert bounds_ok, "discount bounds violated"

    # Ladder check: RICE5 per-gram <= RICE1 per-gram (tol=1.0).
    rice1_ppg = float(reco_df.loc[reco_df.product_id == "RICE1", "opt_price"].iloc[0]) / 1000.0
    rice5_ppg = float(reco_df.loc[reco_df.product_id == "RICE5", "opt_price"].iloc[0]) / 5000.0
    print(f"\nladder check: RICE1 {rice1_ppg:.4f}/g  RICE5 {rice5_ppg:.4f}/g "
          f"(5kg must be <= 1kg)")
    assert rice5_ppg <= config["ladder_tol"] * rice1_ppg + 1e-6, "ladder violated"

    # --- Out-of-box glide check (ITEM 3) --------------------------------------------------
    # A cell whose current discount sits OUTSIDE [disc_lo, disc_hi] must still respect the
    # glide cap: it may WALK toward the box by at most max_disc_change_ppt this week, never
    # snapping to the box edge in one step.
    print("\n=== out-of-box glide check ===")
    oob_baseline = pd.DataFrame(
        [
            # DAL1 starts at disc0=40, far ABOVE disc_hi=30 -> must step down by <= max_ch only.
            ["RICE1", "BLR", "Staples", "Sonamasuri Rice", 1000.0, 120.0, 110.0, 130.0, 15.4],
            ["DAL1",  "BLR", "Staples", "Toor Dal",        1000.0, 80.0, 108.0, 180.0, 40.0],
        ],
        columns=[
            "product_id", "city", "category", "base_product", "pack_grams",
            "q0_units_wk", "p0_price", "mrp", "disc0",
        ],
    )
    oob_elast = elast_df[elast_df.product_id.isin(["RICE1", "DAL1"])].reset_index(drop=True)
    reco_oob, _ = optimize(oob_elast, None, oob_baseline, config)
    dal_row = reco_oob.loc[reco_oob.product_id == "DAL1"].iloc[0]
    dal_move = abs(float(dal_row["opt_disc"]) - float(dal_row["base_disc"]))
    print(f"DAL1 disc0={dal_row['base_disc']:.2f} (above disc_hi={config['disc_hi']}) "
          f"-> opt_disc={dal_row['opt_disc']:.2f}  move={dal_move:.4f}ppt "
          f"(cap={config['max_disc_change_ppt']})")
    glide_oob_ok = (np.abs(reco_oob["opt_disc"] - reco_oob["base_disc"])
                    <= config["max_disc_change_ppt"] + 1e-3).all()
    assert glide_oob_ok, "glide constraint violated for out-of-box cell"
    assert dal_move <= config["max_disc_change_ppt"] + 1e-3, (
        f"out-of-box cell snapped past glide cap: moved {dal_move:.4f} > "
        f"{config['max_disc_change_ppt']}")
    # And it should actually move DOWN toward the box (not stay pinned at disc0).
    assert float(dal_row["opt_disc"]) <= float(dal_row["base_disc"]) + 1e-6, (
        "out-of-box cell above disc_hi should not increase its discount")

    # Try a second KPI to exercise the objective switch.
    print("\n=== optimize (kpi=nrw) ===")
    cfg2 = dict(config, kpi="nrw")
    reco2, summ2 = optimize(elast_df, cross_df, baseline_df, cfg2)
    print("nrw baseline:", summ2["baseline"]["nrw"], "-> optimized:", summ2["optimized"]["nrw"])
    assert summ2["optimized"]["nrw"] >= summ2["baseline"]["nrw"] - 1e-6, "nrw should not worsen"

    # --- KPI chain completeness (units/volume, revenue, spend, profit, margin, share) ---
    print("\n=== KPI chain check (Eq.28 profit / Eq.29 margin) ===")
    chain = {"revenue", "volume", "nrw", "share", "spend", "profit", "margin"}
    assert chain <= set(kpi_summary["baseline"]), \
        f"KPI chain incomplete: {chain - set(kpi_summary['baseline'])}"
    b = kpi_summary["baseline"]
    print(f"baseline chain: units {b['volume']:.1f} | revenue ₹{b['revenue']:.0f} | "
          f"spend ₹{b['spend']:.0f} | profit ₹{b['profit']:.0f} | margin {b['margin']*100:.1f}% "
          f"| share {b['share']:.3f}")
    # Hand-check profit on the baseline of one cell (RICE1): V*(p - 0.5*mrp - 0.15*p - 10)
    P0 = build_problem(elast_df, cross_df, baseline_df, config)
    V0 = demand_model(P0["disc0"], P0)
    p0v = np.maximum(P0["mrp"] * (1.0 - P0["disc0"] / 100.0), 1e-6)
    prof_hand = float(np.sum(V0 * (p0v - (P0["mrp"] * DEFAULT_COGS_PCT
                                          + DEFAULT_COMMISSION_PCT * p0v
                                          + DEFAULT_FULFILLMENT_FEE))))
    assert abs(prof_hand - _kpis(V0, p0v, P0)["profit"]) < 1e-6, "Eq.28 arithmetic mismatch"

    print("\n=== optimize (kpi=profit) ===")
    cfg3 = dict(config, kpi="profit")
    reco3, summ3 = optimize(elast_df, cross_df, baseline_df, cfg3)
    print("profit baseline:", summ3["baseline"]["profit"], "-> optimized:",
          summ3["optimized"]["profit"])
    assert summ3["optimized"]["profit"] >= summ3["baseline"]["profit"] - 1e-6, \
        "profit objective should not worsen profit"
    assert summ3["revenue_floor_ok"], "profit-chasing must not torch the revenue floor"

    print("\n=== optimize (kpi=margin) ===")
    cfg4 = dict(config, kpi="margin")
    reco4, summ4 = optimize(elast_df, cross_df, baseline_df, cfg4)
    print("margin baseline:", summ4["baseline"]["margin"], "-> optimized:",
          summ4["optimized"]["margin"])
    assert summ4["optimized"]["margin"] >= summ4["baseline"]["margin"] - 1e-6, \
        "margin objective should not worsen margin"
    assert summ4["revenue_floor_ok"], "margin-chasing must not torch the revenue floor"

    print("\n=== optimize (kpi=combo, alpha=0.5) ===")
    cfg5 = dict(config, kpi="combo", combo_alpha=0.5)
    reco5, summ5 = optimize(elast_df, cross_df, baseline_df, cfg5)
    b5, o5 = summ5["baseline"], summ5["optimized"]
    combo_base = 0.5 * 1.0 + 0.5 * 1.0  # baseline of each normalized term is 1 by construction
    combo_opt = 0.5 * (o5["revenue"] / b5["revenue"]) + 0.5 * (o5["profit"] / b5["profit"])
    print(f"combo (0.5*rev_norm + 0.5*profit_norm): baseline {combo_base:.4f} "
          f"-> optimized {combo_opt:.4f}")
    assert combo_opt >= combo_base - 1e-6, "combo objective should not worsen the blend"
    assert summ5["revenue_floor_ok"], "combo objective must respect the revenue floor"

    # Sign-safety of the normalization: a NEGATIVE baseline KPI must still be MAXIMIZED
    # (reward uses |baseline| as denominator, never the signed baseline).
    P_neg = build_problem(elast_df, cross_df, baseline_df, dict(config, kpi="profit"))
    fake_base = dict(_kpis(demand_model(P_neg["disc0"], P_neg),
                           np.maximum(P_neg["mrp"] * (1.0 - P_neg["disc0"] / 100.0), 1e-6),
                           P_neg))
    fake_base["profit"] = -1000.0  # pretend the baseline profit was negative
    obj_better = _penalized_objective(P_neg["disc0"], P_neg, fake_base)
    worse = P_neg["disc0"] + 2.0   # deeper discounts -> lower profit on this fixture
    obj_worse = _penalized_objective(worse, P_neg, fake_base)
    assert obj_better < obj_worse, \
        "normalization sign bug: higher profit must give a LOWER (better) objective " \
        "even when the baseline profit is negative"
    print("  negative-baseline normalization: sign-safe (higher profit -> better objective)")

    # --- Robustness protocol receipt ---
    print("\n=== DE robustness report ===")
    rob = kpi_summary["robustness"]
    assert rob["n_completed"] >= 1 and len(rob["runs"]) == rob["n_completed"]
    assert rob["runs"][0]["mutation"] == (0.5, 1.0) and rob["runs"][0]["popsize"] == 15, \
        "run 0 must keep the champion configuration (backward-compat anchor)"
    assert any(r["is_best"] for r in rob["runs"])
    for r in rob["runs"]:
        print(f"  run {r['run']}: obj {r['objective']:.6f} | pop {r['popsize']} "
              f"mut {r['mutation']} rec {r['recombination']} | {r['elapsed_s']}s "
              f"| {r['stopped_by']}{' | BEST' if r['is_best'] else ''}")
    print(f"  spread_rel={rob['spread_rel']}, agreed={rob['agreed']}, "
          f"stopped_early={rob['stopped_early']}")
    # time-cap path: a ~zero budget must halt cleanly and still return a feasible answer
    cfg_cap = dict(config, de_time_cap_s=1e-6, n_seeds=1)
    reco_cap, summ_cap = optimize(elast_df, cross_df, baseline_df, cfg_cap)
    assert summ_cap["robustness"]["runs"][0]["stopped_by"] == "time_cap", \
        "time cap did not fire"
    glide_cap_ok = (np.abs(reco_cap["opt_disc"] - reco_cap["base_disc"])
                    <= config["max_disc_change_ppt"] + 1e-3).all()
    assert glide_cap_ok, "time-capped run returned an infeasible plan"
    print("  time-cap safeguard: fires cleanly, plan stays glide-feasible")

    # --- Declarative constraints: default config file must be a no-op ---
    print("\n=== constraints hook (default = disabled = champion behaviour) ===")
    cons_path = os.path.join(_ROOT, "DISCOUNT_PLAN", "pricing", "pricing_constraints.json")
    if os.path.exists(cons_path):
        with open(cons_path, "r", encoding="utf-8") as fh:
            cons_cfg = json.load(fh)
        P_cons = build_problem(elast_df, cross_df, baseline_df,
                               dict(config, constraints=cons_cfg))
        assert P_cons["extra_penalties"] == [], \
            "default pricing_constraints.json (all disabled) must compile to no penalties"
        print(f"  {cons_path}: all families disabled -> 0 extra penalties (backward compatible)")
    else:
        print("  pricing_constraints.json not found — skipped (constraints_lib has its own test)")

    print("\nAll smoke-test assertions passed. Exit 0.")
    sys.exit(0)
