"""
constraints_lib.py — declarative pricing-constraint registry for the DE optimizer.

WHAT THIS IS (business framing):
  A rulebook the optimizer must respect, written as a JSON file instead of code.
  Finance says "volume can't fall more than 5%"? Sales says "don't move the whole
  portfolio's average price by more than 2% a week"? Those become entries in
  DISCOUNT_PLAN/pricing/pricing_constraints.json — no code edits, and the same
  file onboards the next brand.

HOW IT PLUGS IN (champion/challenger safe):
  de_optimizer.build_problem(config) reads config['constraints'] (a dict with this
  file's schema), calls compile_constraints(cfg, P) here, and stores the result in
  P['extra_penalties']. de_optimizer._penalized_objective then adds each callable's
  penalty on top of the champion penalties (revenue floor / glide / ladder, which
  are NOT touched). Every family ships enabled:false, so the default engine run is
  behaviourally identical to the pre-constraints champion.

CONSTRAINT FAMILIES (PepsiCo paper App. C.5):
  kpi_bounds          Eq. 33 — relative-to-baseline bounds min_frac*K0 <= K(x) <=
                      max_frac*K0 on any KPI in the chain (revenue, volume, nrw,
                      share, spend, profit, margin). null = unbounded side.
  pricing_line        Eq. 36 — within a base_product x city pack family, every
                      member's absolute price increment (p_new - p0, ₹) must be
                      equal within tol_rupees (uniform family price moves).
  portfolio_avg_band  Eq. 37 (plain-average version) — the portfolio average price
                      change 100*Σ(p_new-p0)/Σp0 must stay in [theta_lo, theta_hi] %.
  vw_avg_band         Eq. 38-39 — volume-WEIGHTED average price change with
                      ENDOGENOUS weights: the weights are the OPTIMIZED volumes
                      from demand_model at the candidate discounts (the paper's
                      rationale for handling this inside the metaheuristic), not
                      the baseline volumes.

CONTRACT:
  compile_constraints(config_json, P) -> list of penalty callables
      f(disc_vec, P, ctx=None) -> float penalty (0.0 when satisfied)
  ctx, when provided by _penalized_objective, carries the already-computed
  {"V", "price", "kpis", "base_kpis"} so no second demand_model evaluation is
  needed inside the DE loop. When ctx is None (standalone evaluation) the
  callable recomputes them through the shared de_optimizer kernel.
  Unknown family names or unknown KPI names FAIL LOUD (ValueError) — repo rule.

Penalty scale matches de_optimizer._penalized_objective exactly: BIG=100 times a
squared, baseline-normalized violation, so the new families neither dominate nor
vanish next to the champion penalties.

Run the self-test:  python -X utf8 scripts/pricing/constraints_lib.py
Validate a config:  python -X utf8 scripts/pricing/constraints_lib.py --config DISCOUNT_PLAN/pricing/pricing_constraints.json
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

BIG = 100.0  # same soft-penalty scale as de_optimizer._penalized_objective

# KPI names a kpi_bounds entry may reference (must be keys of de_optimizer._kpis).
_KPI_NAMES = {"revenue", "volume", "nrw", "share", "spend", "profit", "margin"}

FAMILIES = ("kpi_bounds", "pricing_line", "portfolio_avg_band", "vw_avg_band")


def _de():
    """Lazy import of the sibling kernel (avoids a hard circular import:
    de_optimizer imports this module lazily inside build_problem)."""
    try:
        import de_optimizer
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import de_optimizer
    return de_optimizer


def _eval_state(disc, P, ctx):
    """Return (V, price, kpis) — from ctx when the objective already computed them,
    else recomputed through the shared demand kernel (identical arithmetic)."""
    if ctx is not None and all(k in ctx for k in ("V", "price", "kpis")):
        return ctx["V"], ctx["price"], ctx["kpis"]
    de = _de()
    disc = np.asarray(disc, dtype=float)
    price = np.maximum(P["mrp"] * (1.0 - disc / 100.0), 1e-6)
    V = de.demand_model(disc, P)
    return V, price, de._kpis(V, price, P)


def _baseline_kpis(P):
    """KPI chain at the current (disc0) state — the K0 every bound is relative to."""
    de = _de()
    disc0 = np.asarray(P["disc0"], dtype=float)
    price0 = np.maximum(P["mrp"] * (1.0 - disc0 / 100.0), 1e-6)
    V0 = de.demand_model(disc0, P)
    return de._kpis(V0, price0, P)


# ---------------------------------------------------------------------------
# Family compilers. Each returns a list of callables (possibly empty).
# ---------------------------------------------------------------------------

def _compile_kpi_bounds(params, P):
    bounds = params.get("bounds") or {}
    unknown = set(bounds) - _KPI_NAMES
    if unknown:
        raise ValueError(f"kpi_bounds: unknown KPI name(s) {sorted(unknown)}; "
                         f"allowed: {sorted(_KPI_NAMES)}")
    base = _baseline_kpis(P)
    checks = []  # (kpi, min_frac, max_frac, K0)
    for kpi, b in bounds.items():
        lo = b.get("min_frac")
        hi = b.get("max_frac")
        if lo is None and hi is None:
            continue
        checks.append((kpi, lo, hi, float(base[kpi])))
    if not checks:
        return []

    def kpi_bounds_penalty(disc, P, ctx=None):
        _, _, k = _eval_state(disc, P, ctx)
        pen = 0.0
        for kpi, lo, hi, K0 in checks:
            K = k[kpi]
            denom = max(abs(K0), 1e-9)
            if lo is not None and K < lo * K0:
                pen += BIG * ((lo * K0 - K) / denom) ** 2
            if hi is not None and K > hi * K0:
                pen += BIG * ((K - hi * K0) / denom) ** 2
        return pen

    kpi_bounds_penalty.family = "kpi_bounds"
    kpi_bounds_penalty.detail = {c[0]: (c[1], c[2]) for c in checks}
    return [kpi_bounds_penalty]


def _compile_pricing_line(params, P):
    tol = float(params.get("tol_rupees", 1.0))
    cells = P["cells"]
    families = []  # (row_indices asc by pack, mean p0 scale)
    if "base_product" in cells.columns and "pack_grams" in cells.columns:
        grp = cells.reset_index().rename(columns={"index": "row"})
        for (_bp, _city), sub in grp.groupby(["base_product", "city"]):
            sub = sub.dropna(subset=["pack_grams"]).sort_values("pack_grams")
            rows = sub["row"].tolist()
            if len(rows) >= 2:
                scale = max(float(np.mean(P["p0"][rows])), 1.0)
                families.append((np.asarray(rows, dtype=int), scale))
    if not families:
        return []

    def pricing_line_penalty(disc, P, ctx=None):
        # Eq. 36: equal absolute ₹ increments within a pack family. Spread of the
        # per-member increments (max-min) beyond tol_rupees is penalized — spread
        # <= tol implies every adjacent pack-size gap is within tol too.
        _, price, _ = _eval_state(disc, P, ctx)
        incr = price - P["p0"]
        pen = 0.0
        for rows, scale in families:
            spread = float(np.max(incr[rows]) - np.min(incr[rows]))
            viol = spread - tol
            if viol > 0:
                pen += BIG * (viol / scale) ** 2
        return pen

    pricing_line_penalty.family = "pricing_line"
    pricing_line_penalty.detail = {"n_families": len(families), "tol_rupees": tol}
    return [pricing_line_penalty]


def _compile_portfolio_avg_band(params, P):
    theta_lo = params.get("theta_lo_pct")
    theta_hi = params.get("theta_hi_pct")
    if theta_lo is None and theta_hi is None:
        return []
    p0_sum = max(float(np.sum(P["p0"])), 1e-9)

    def portfolio_avg_band_penalty(disc, P, ctx=None):
        # Eq. 37 (plain-average version): chg% = 100 * Σ(p_new - p0) / Σ p0.
        _, price, _ = _eval_state(disc, P, ctx)
        chg = 100.0 * float(np.sum(price - P["p0"])) / p0_sum
        pen = 0.0
        if theta_lo is not None and chg < theta_lo:
            pen += BIG * ((theta_lo - chg) / 100.0) ** 2
        if theta_hi is not None and chg > theta_hi:
            pen += BIG * ((chg - theta_hi) / 100.0) ** 2
        return pen

    portfolio_avg_band_penalty.family = "portfolio_avg_band"
    portfolio_avg_band_penalty.detail = {"theta_lo_pct": theta_lo, "theta_hi_pct": theta_hi}
    return [portfolio_avg_band_penalty]


def _compile_vw_avg_band(params, P):
    theta_lo = params.get("theta_lo_pct")
    theta_hi = params.get("theta_hi_pct")
    if theta_lo is None and theta_hi is None:
        return []
    # Eq. 38a: baseline volume-weighted average price, weights = BASELINE volumes.
    w0 = np.maximum(np.nan_to_num(P["q0"], nan=0.0), 0.0)
    p_bar_0 = float(np.sum(w0 * P["p0"])) / max(float(np.sum(w0)), 1e-9)

    def vw_avg_band_penalty(disc, P, ctx=None):
        # Eq. 38b with ENDOGENOUS weights: the new average is weighted by the
        # OPTIMIZED volumes V(disc) from demand_model — volumes shift with price,
        # so the weights move with the decision variable (paper's rationale for
        # enforcing this inside the metaheuristic rather than as a linear row).
        V, price, _ = _eval_state(disc, P, ctx)
        p_bar_new = float(np.sum(V * price)) / max(float(np.sum(V)), 1e-9)
        chg = 100.0 * (p_bar_new - p_bar_0) / max(abs(p_bar_0), 1e-9)  # Eq. 39
        pen = 0.0
        if theta_lo is not None and chg < theta_lo:
            pen += BIG * ((theta_lo - chg) / 100.0) ** 2
        if theta_hi is not None and chg > theta_hi:
            pen += BIG * ((chg - theta_hi) / 100.0) ** 2
        return pen

    vw_avg_band_penalty.family = "vw_avg_band"
    vw_avg_band_penalty.detail = {"theta_lo_pct": theta_lo, "theta_hi_pct": theta_hi,
                                  "p_bar_0": round(p_bar_0, 4)}
    return [vw_avg_band_penalty]


_COMPILERS = {
    "kpi_bounds": _compile_kpi_bounds,
    "pricing_line": _compile_pricing_line,
    "portfolio_avg_band": _compile_portfolio_avg_band,
    "vw_avg_band": _compile_vw_avg_band,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compile_constraints(config_json, P):
    """
    Compile a declarative constraint config into penalty callables.

    Parameters
    ----------
    config_json : dict — schema of DISCOUNT_PLAN/pricing/pricing_constraints.json:
        {"<family>": {"enabled": bool, ...family params...}, ...}
        Keys starting with "_" are documentation and ignored.
    P : problem dict from de_optimizer.build_problem (baselines already attached).

    Returns
    -------
    list of callables f(disc_vec, P, ctx=None) -> float penalty. Disabled families
    compile to nothing; an all-disabled config returns [] (champion behaviour).

    Raises
    ------
    ValueError on unknown family or KPI names (fail loud — repo convention).
    """
    if not isinstance(config_json, dict):
        raise ValueError("constraints config must be a dict "
                         f"(got {type(config_json).__name__})")
    unknown = [k for k in config_json if not k.startswith("_") and k not in _COMPILERS]
    if unknown:
        raise ValueError(f"unknown constraint family(ies) {unknown}; "
                         f"known: {sorted(_COMPILERS)}")
    penalties = []
    for fam, compiler in _COMPILERS.items():
        params = config_json.get(fam)
        if not params or not params.get("enabled", False):
            continue
        penalties.extend(compiler(params, P))
    return penalties


def load_config(path):
    """Read a pricing_constraints.json file. Returns {} when the file is absent
    (missing config == no extra constraints, never a crash)."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Self-test: synthetic 3-cell problem, exercises every family. Exit 0 on pass.
# ---------------------------------------------------------------------------

def _selftest():
    import pandas as pd
    de = _de()

    baseline_df = pd.DataFrame(
        [
            ["RICE1", "BLR", "Staples", "Sonamasuri Rice", 1000.0, 120.0, 110.0, 130.0, 15.4],
            ["RICE5", "BLR", "Staples", "Sonamasuri Rice", 5000.0, 40.0, 520.0, 620.0, 16.1],
            ["DAL1",  "BLR", "Staples", "Toor Dal",        1000.0, 80.0, 150.0, 180.0, 16.7],
        ],
        columns=["product_id", "city", "category", "base_product", "pack_grams",
                 "q0_units_wk", "p0_price", "mrp", "disc0"],
    )
    elast_df = pd.DataFrame(
        [["RICE1", "BLR", -1.8, 0.30, -1.2],
         ["RICE5", "BLR", -0.4, 0.50, -0.3],
         ["DAL1",  "BLR", -2.2, 0.40, -1.5]],
        columns=["product_id", "city", "own_elast", "own_sd", "promo_elast"],
    )
    config = {"disc_lo": 0.0, "disc_hi": 45.0, "psych_prices": []}
    P = de.build_problem(elast_df, None, baseline_df, config)
    disc0 = np.asarray(P["disc0"], dtype=float)

    # 1) all-disabled config compiles to nothing (backward-compat guarantee)
    all_off = {f: {"enabled": False} for f in FAMILIES}
    assert compile_constraints(all_off, P) == [], "disabled families must compile to []"
    print("[selftest] all-disabled config -> 0 callables (champion behaviour) OK")

    # 2) unknown family / KPI names fail loud
    for bad in ({"no_such_family": {"enabled": True}},
                {"kpi_bounds": {"enabled": True, "bounds": {"ebitda": {"min_frac": 1.0}}}}):
        try:
            compile_constraints(bad, P)
            raise AssertionError(f"should have raised on {bad}")
        except ValueError:
            pass
    print("[selftest] unknown family/KPI -> ValueError (fail loud) OK")

    # 3) kpi_bounds: volume floor — zero at baseline, positive when volume tanks
    fns = compile_constraints(
        {"kpi_bounds": {"enabled": True, "bounds": {"volume": {"min_frac": 0.95}}}}, P)
    assert len(fns) == 1
    assert fns[0](disc0, P) < 1e-12, "no penalty at baseline"
    disc_raise = np.array([5.0, 5.0, 5.0])  # discounts slashed -> prices up -> volume down
    assert fns[0](disc_raise, P) > 0.0, "volume floor must penalize a volume collapse"
    print("[selftest] kpi_bounds volume floor binds correctly OK")

    # 4) pricing_line: equal ₹ increments pass, unequal ones penalized
    fns = compile_constraints({"pricing_line": {"enabled": True, "tol_rupees": 1.0}}, P)
    assert len(fns) == 1
    assert fns[0](disc0, P) < 1e-12, "baseline (zero increments) must pass"
    # RICE1 +₹6.5 (disc 15.4->10.4), RICE5 +₹0 -> unequal within the Rice family
    disc_unequal = disc0.copy(); disc_unequal[0] = 10.4
    assert fns[0](disc_unequal, P) > 0.0, "unequal family increments must be penalized"
    # Same ₹ move on both Rice packs -> equal increments -> no penalty
    d_rupees = 6.5
    disc_equal = disc0.copy()
    disc_equal[0] = 100.0 * (1.0 - (P["p0"][0] + d_rupees) / P["mrp"][0])
    disc_equal[1] = 100.0 * (1.0 - (P["p0"][1] + d_rupees) / P["mrp"][1])
    assert fns[0](disc_equal, P) < 1e-12, "equal ₹ increments must pass"
    print("[selftest] pricing_line (Eq.36) equal-increment rule OK")

    # 5) portfolio_avg_band: +2% cap trips when all prices jump ~6%
    fns = compile_constraints(
        {"portfolio_avg_band": {"enabled": True, "theta_lo_pct": -2.0, "theta_hi_pct": 2.0}}, P)
    assert fns[0](disc0, P) < 1e-12
    assert fns[0](np.array([8.0, 9.0, 9.0]), P) > 0.0, "over-band avg price rise must be penalized"
    print("[selftest] portfolio_avg_band (Eq.37) OK")

    # 6) vw_avg_band with ENDOGENOUS weights: differs from baseline-weighted version
    fns = compile_constraints(
        {"vw_avg_band": {"enabled": True, "theta_lo_pct": -1.0, "theta_hi_pct": 1.0}}, P)
    assert fns[0](disc0, P) < 1e-12
    disc_shift = np.array([25.0, 16.1, 5.0])  # cheapen elastic RICE1, raise DAL1
    V = de.demand_model(disc_shift, P)
    price = P["mrp"] * (1.0 - disc_shift / 100.0)
    p_bar_endog = float(np.sum(V * price)) / float(np.sum(V))
    p_bar_fixed = float(np.sum(P["q0"] * price)) / float(np.sum(P["q0"]))
    assert abs(p_bar_endog - p_bar_fixed) > 0.01, \
        "endogeneity check: optimized-volume weights must differ from baseline weights"
    print(f"[selftest] vw_avg_band endogenous weights OK "
          f"(p_bar endog {p_bar_endog:.2f} vs fixed {p_bar_fixed:.2f})")

    # 7) end-to-end: optimizer with a tight vw band stays (approximately) inside it
    cfg_free = {"kpi": "revenue", "disc_lo": 0.0, "disc_hi": 45.0,
                "max_disc_change_ppt": 8.0, "revenue_floor_frac": 0.90,
                "psych_prices": [], "ladder_tol": 1.0, "n_seeds": 2,
                "gates_robustness": False}
    # Band chosen TIGHTER than the free solution's excursion so it actually binds.
    cfg_band = dict(cfg_free, constraints={
        "vw_avg_band": {"enabled": True, "theta_lo_pct": -0.05, "theta_hi_pct": 0.05}})

    def _wavg_chg(reco):
        d = reco["opt_disc"].to_numpy(dtype=float)
        Pl = de.build_problem(elast_df, None, baseline_df, cfg_free)
        Vn = de.demand_model(d, Pl)
        pn = Pl["mrp"] * (1.0 - d / 100.0)
        pb0 = float(np.sum(Pl["q0"] * Pl["p0"])) / float(np.sum(Pl["q0"]))
        pbn = float(np.sum(Vn * pn)) / max(float(np.sum(Vn)), 1e-9)
        return 100.0 * (pbn - pb0) / pb0

    reco_free, _ = de.optimize(elast_df, None, baseline_df, cfg_free)
    reco_band, _ = de.optimize(elast_df, None, baseline_df, cfg_band)
    chg_free, chg_band = _wavg_chg(reco_free), _wavg_chg(reco_band)
    print(f"[selftest] end-to-end: wavg price chg free {chg_free:+.3f}% -> banded {chg_band:+.3f}% "
          f"(band ±0.05%, soft penalty)")
    assert abs(chg_free) > 0.05, \
        "test setup broken: free solution should exceed the tight band"
    assert abs(chg_band) < abs(chg_free), "tight band must actually pull the solution in"
    assert abs(chg_band) <= 0.05 + 0.10, \
        f"banded solution should land near the ±0.05% band (soft penalty), got {chg_band:+.3f}%"

    print("\nconstraints_lib self-test: ALL PASS. Exit 0.")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Declarative constraint library — "
                                             "self-test or config validation.")
    ap.add_argument("--config", help="validate a pricing_constraints.json "
                                     "(compile against a synthetic problem)")
    args = ap.parse_args()
    if args.config:
        cfg = load_config(args.config)
        print(f"loaded {args.config}: families present = "
              f"{[k for k in cfg if not k.startswith('_')]}")
        # compile against a tiny synthetic problem to prove validity
        import pandas as pd
        de = _de()
        bdf = pd.DataFrame(
            [["A", "X", "Cat", "Base", 1000.0, 10.0, 100.0, 120.0, 15.0],
             ["B", "X", "Cat", "Base", 5000.0, 5.0, 450.0, 560.0, 18.0]],
            columns=["product_id", "city", "category", "base_product", "pack_grams",
                     "q0_units_wk", "p0_price", "mrp", "disc0"])
        edf = pd.DataFrame([["A", "X", -1.5, 0.3, -1.0], ["B", "X", -1.2, 0.4, -0.8]],
                           columns=["product_id", "city", "own_elast", "own_sd", "promo_elast"])
        P = de.build_problem(edf, None, bdf, {"disc_lo": 0.0, "disc_hi": 45.0,
                                              "psych_prices": []})
        fns = compile_constraints(cfg, P)
        print(f"compiled OK: {len(fns)} active penalty callable(s) "
              f"({[getattr(f, 'family', '?') for f in fns]})")
        enabled = [k for k, v in cfg.items()
                   if not k.startswith("_") and isinstance(v, dict) and v.get("enabled")]
        print(f"enabled families: {enabled or 'NONE (champion behaviour unchanged)'}")
        sys.exit(0)
    _selftest()
    sys.exit(0)
