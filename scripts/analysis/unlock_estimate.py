"""
unlock_estimate.py — risk-weight the 'test-to-unlock' discount by bootstrap
stability, so the path toward the ₹5L target is an EXPECTED value, not a fantasy.

For each cell whose model says trim (optimum < current) but which isn't a
confident cut, estimate P(stay-cut) = fraction of week-bootstrap refits where the
category optimum is still below the cell's current discount. Then
   expected_unlock = Σ  clamped_gain_i × P(stay-cut)_i
This is what a disciplined test program would realistically confirm.
"""
import os, sys, glob, json, warnings
import numpy as np, pandas as pd
import statsmodels.api as sm, statsmodels.formula.api as smf
warnings.simplefilter("ignore")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
import importlib.util
spec = importlib.util.spec_from_file_location("dp", os.path.join(ROOT, "scripts/analysis/discount_plan.py"))
dp = importlib.util.module_from_spec(spec); spec.loader.exec_module(dp)

run, fact = dp._latest_facttable()
panel = dp.build_panel(fact)
o = pd.read_csv(os.path.join(run, "plan", "optimization.csv"))
FQ = "np.log1p(units) ~ C(cell_id) + disc + disc_sq + log_osa + log_adsov + comp_share"
wks = np.array(sorted(panel["week"].unique()))
rng = np.random.RandomState(0)
B = 30
cap = panel.groupby("category")["disc"].quantile(0.95).to_dict()

# bootstrap: per category, distribution of the net-rev-optimal discount
opt_draws = {c: [] for c in panel["category"].unique()}
for _ in range(B):
    draw = wks[rng.randint(0, len(wks), len(wks))]
    bp = pd.concat([panel[panel["week"] == w] for w in draw])
    for cat, a in bp.groupby("category"):
        if len(a) < 60 or a["cell_id"].nunique() < 2: continue
        try:
            m = smf.rlm(FQ, data=a, M=sm.robust.norms.HuberT()).fit()
            b1 = m.params.get("disc", np.nan); b2 = m.params.get("disc_sq", 0.0)
            hi = float(min(cap.get(cat, 40), 60.0))
            grid = np.arange(0.0, hi + 0.25, 0.25)
            rr = (1 - grid/100.0) * dp._units_factor(grid, b1, b2)
            opt_draws[cat].append(float(grid[int(np.argmax(rr))]))
        except Exception:
            pass

test = o[o["action2"] == "test_cut"].copy()
def stay(row):
    ds = opt_draws.get(row["category"], [])
    if not ds: return 0.0
    return float(np.mean([od < row["cur_disc"] - 0.5 for od in ds]))
test["p_stay"] = test.apply(stay, axis=1)
test["exp_gain"] = test["delta_nr_mo"].clip(lower=0) * test["p_stay"]

full = float(test["delta_nr_mo"].clip(lower=0).sum())
exp  = float(test["exp_gain"].sum())
# realistic pass rate tiers
for thr in (0.8, 0.6, 0.5):
    sub = test[test["p_stay"] >= thr]
    print(f"  cells with P(stay-cut) ≥ {thr:.0%}: {len(sub):3d}  →  ₹{sub['delta_nr_mo'].clip(lower=0).sum():>9,.0f}/mo if confirmed")
print(f"\n  test-to-unlock pool (theoretical, all hold): ₹{full:,.0f}/mo across {len(test)} cells")
print(f"  EXPECTED unlock (risk-weighted by bootstrap stability): ₹{exp:,.0f}/mo")

conf_all = float(o[o["action2"] == "reinvest"]["delta_nr_mo"].clip(lower=0).sum() +
                 o[o["action2"] == "cut"]["delta_nr_mo"].clip(lower=0).sum())
print(f"\n  confident (bankable now):        ₹{conf_all:,.0f}/mo")
print(f"  + expected test-unlock:          ₹{exp:,.0f}/mo")
print(f"  = realistic 8–12 wk target:      ₹{conf_all+exp:,.0f}/mo  vs ₹5,00,000 → "
      f"{'REACHES' if conf_all+exp>=500000 else f'{(conf_all+exp)/500000*100:.0f}% of target'}")
test.sort_values("exp_gain", ascending=False).to_csv(os.path.join(run, "plan", "test_unlock_list.csv"), index=False)
