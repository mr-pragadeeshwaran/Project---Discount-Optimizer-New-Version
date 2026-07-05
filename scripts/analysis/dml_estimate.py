"""
dml_estimate.py — Double/Debiased Machine Learning estimate of the discount
effect, per category. Uses gradient-boosted nuisance functions to flexibly
strip NONLINEAR confounding (OSA, Ad SOV, competitive share, lagged sales,
season) that the linear fixed-effects model can't, then a Neyman-orthogonal
score for a debiased discount semi-elasticity with a valid CI.

Partially-linear model:   y = θ·t + g(X) + e,   E[e|X,t]=0
  y = log1p(units)   (within-cell demeaned to absorb cell fixed effects)
  t = discount %     (within-cell demeaned)
  X = [log_osa, log_adsov, comp_share, lag1_lu, lag2_lu, month]
Cross-fitted (K folds): ỹ = y − Ê[y|X],  t̃ = t − Ê[t|X];  θ = Σ t̃ỹ / Σ t̃².

Decision: discount is reliably WASTE at operating level d if θ + 1.96·se < 1/(100−d).
Compares to the linear-FE verdict and reports the DML-locked savings.
"""
import os, sys, glob, json, warnings
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.model_selection import KFold
warnings.simplefilter("ignore")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
import importlib.util
spec = importlib.util.spec_from_file_location("dp", os.path.join(ROOT, "scripts/analysis/discount_plan.py"))
dp = importlib.util.module_from_spec(spec); spec.loader.exec_module(dp)

XCOLS = ["log_osa", "log_adsov", "comp_share", "lag1_lu", "lag2_lu", "month"]
K = 5


def _demean(df, cols):
    out = df.copy()
    for c in cols:
        out[c] = out[c] - out.groupby("cell_id")[c].transform("mean")
    return out


def dml_theta(sub):
    """Cross-fitted DML partially-linear estimate + Neyman-orthogonal se."""
    d = sub.dropna(subset=XCOLS + ["disc", "units"]).copy()
    if len(d) < 80 or d["cell_id"].nunique() < 2:
        return np.nan, np.nan, len(d)
    d["y"] = np.log1p(d["units"]); d["t"] = d["disc"]
    d = _demean(d, ["y", "t"] + XCOLS)      # absorb cell FE by within-transform
    X = d[XCOLS].values; y = d["y"].values; t = d["t"].values
    ry = np.zeros_like(y); rt = np.zeros_like(t)
    kf = KFold(n_splits=min(K, max(2, len(d)//40)), shuffle=True, random_state=0)
    for tr, te in kf.split(X):
        gy = HistGradientBoostingRegressor(max_depth=3, max_iter=200, learning_rate=0.05,
                                           l2_regularization=1.0, random_state=0).fit(X[tr], y[tr])
        gt = HistGradientBoostingRegressor(max_depth=3, max_iter=200, learning_rate=0.05,
                                           l2_regularization=1.0, random_state=0).fit(X[tr], t[tr])
        ry[te] = y[te] - gy.predict(X[te])
        rt[te] = t[te] - gt.predict(X[te])
    denom = np.sum(rt * rt)
    if denom <= 0:
        return np.nan, np.nan, len(d)
    theta = np.sum(rt * ry) / denom
    eps = ry - theta * rt
    # Neyman-orthogonal variance (cluster-robust by cell)
    d["score"] = rt * eps
    psi = d.groupby("cell_id")["score"].sum().values          # cluster sums
    J = denom / len(d)
    var = (np.sum(psi**2) / len(d)) / (J**2) / len(d)
    return float(theta), float(np.sqrt(max(var, 0))), len(d)


def main():
    run, fact = dp._latest_facttable()
    panel = dp.build_panel(fact).dropna(subset=["lag1_lu", "lag2_lu"])
    cut = pd.read_csv(os.path.join(run, "plan", "cut_list.csv"))
    cutcur = cut.groupby("category")["cur_disc"].median().to_dict()
    save = cut.groupby("category")["net_gain_mo"].sum().to_dict()
    print("DOUBLE ML discount effect (gradient-boosted confounder control), per category:")
    print(f"{'category':22s} {'DML θ':>9s} {'se':>8s} {'θ+1.96se':>9s} {'be_thr':>8s} {'verdict':>11s} {'₹/mo':>10s}")
    locked = 0.0; rows = []
    for cat in sorted(cutcur, key=lambda c: -save.get(c, 0)):
        sub = panel[panel["category"] == cat]
        th, se, n = dml_theta(sub)
        if not np.isfinite(th):
            print(f"{cat[:22]:22s}   (insufficient data)"); continue
        d = cutcur[cat]; up = th + 1.96*se; be = 1.0/(100-d)
        waste = up < be
        if waste: locked += save.get(cat, 0)
        rows.append((cat, th, se, up, be, waste, save.get(cat, 0)))
        print(f"{cat[:22]:22s} {th:+9.4f} {se:8.4f} {up:+9.4f} {be:8.4f} "
              f"{'WASTE lock' if waste else 'uncertain':>11s} {save.get(cat,0):>10,.0f}")
    tot = sum(r[6] for r in rows)
    print(f"\n  DML-locked savings (reliably-waste under debiased ML): ₹{locked:,.0f}/mo of ₹{tot:,.0f}/mo cut total")
    json.dump([{'cat':r[0],'theta':r[1],'se':r[2],'waste':bool(r[5]),'save':r[6]} for r in rows],
              open(os.path.join(run, "plan", "dml_results.json"), "w"), indent=2)


if __name__ == "__main__":
    main()
