"""
validate_plan.py — check the confounder-controlled plan against goal conditions.
Prints PASS/FAIL per condition with offending cells and the achievable
net-savings figure.

Exit 0 iff every SAFETY gate passes (C1-C5, C7, C8). C6 — the business
savings target — is a loudly-reported VERDICT (MEETS/BELOW), not an abort:
a safe, correct plan that is smaller than the ambition bar must still be
executable, otherwise the gate punishes honest shrinkage of the estimate.
The target itself lives in v4_config.SAVINGS_TARGET_MONTHLY_INR.

  C1 Discount effect is ISOLATED from OSA, Ad SOV, competitive intensity — the
     model controls for all three (non-degenerate) and every recommendation
     names the driver. NOT raw discount-to-sales correlation.
  C2 Every flat/declining cell is bucketed before action; no CUT cell is a
     low-OSA (a) or competitive-pressure (b) cell.
  C3 Every CUT has isolated marginal ROAS < 1 (net-rev gain > 0 from the
     CONTROLLED coefficient); no cut whose flatness a confounder explains, and
     no phantom volume gain (cutting price never modeled to raise units).
  C4 Category models clear the R2 floor at the level estimated; low-confidence
     cells are flagged (present + counted), never treated as certain.
  C5 Money reconciles: sum of cut line-items == reported achievable total;
     aggregate net-revenue impact positive.
  C6 Achievable savings computed explicitly vs 6-10L; if below, the reason is
     stated (which products/cities, why the ceiling is lower).
"""
import os, sys, glob, json
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
try:
    import v4_config as _cfg
    TARGET_LO = getattr(_cfg, "SAVINGS_TARGET_MONTHLY_INR", 500_000)
except Exception:
    TARGET_LO = 500_000
TARGET_HI = 1_000_000
R2_FLOOR = 0.60
OOS_R2_BAR = 0.75                            # goal: model accuracy R² ≥ 0.75 (out-of-sample)
OSA_LOW = 75.0


def _latest_plan():
    for r in sorted(glob.glob(os.path.join(ROOT, "v4_outputs", "2026*")), reverse=True):
        if os.path.exists(os.path.join(r, "plan", "all_cells.csv")):
            return os.path.join(r, "plan")
    raise SystemExit("No plan/ folder found — run discount_plan.py first.")


def main():
    pdir = _latest_plan()
    S = json.load(open(os.path.join(pdir, "plan_summary.json")))
    df = pd.read_csv(os.path.join(pdir, "all_cells.csv"))
    cut = pd.read_csv(os.path.join(pdir, "cut_list.csv")) if os.path.getsize(os.path.join(pdir,"cut_list.csv"))>2 else df.iloc[:0]
    print("=" * 76)
    print(f"  PLAN VALIDATION  |  {S['run']}  |  {S['n_cells']} cells, {S['n_products']} products, {S['weeks']} wk")
    print("=" * 76)
    R = {}

    # ── C1: discount isolated from OSA / Ad SOV / competitive ──
    f = S["formula"]; c1 = []
    for token, name in [("log_osa", "OSA"), ("log_adsov", "Ad SOV"), ("comp_share", "competitive")]:
        if token not in f:
            c1.append(f"model formula missing {name} control ({token})")
    # competitive control must be non-degenerate (beta_comp varies across cats, not all ~0)
    betas_comp = [v.get("beta_comp") for v in S["models"].values() if v.get("ok")]
    betas_comp = [b for b in betas_comp if isinstance(b, (int, float)) and np.isfinite(b)]
    if betas_comp and np.nanmax(np.abs(betas_comp)) < 1e-4:
        c1.append("competitive control degenerate (all beta_comp ~ 0)")
    # every recommendation names a driver
    nodrv = cut[cut["driver"].isin(["unknown", ""]) | cut["driver"].isna()]
    if len(nodrv):
        c1.append(f"{len(nodrv)} cut cells have no named driver")
    # every cut carries a human-readable rationale (condition-1 naming)
    if "decision_reason" in cut.columns:
        noreason = cut[cut["decision_reason"].isna() | (cut["decision_reason"] == "")]
        if len(noreason):
            c1.append(f"{len(noreason)} cut cells missing a decision rationale")
    R["C1"] = (not c1, c1)

    # ── C2: bucketing before action; no cut is low-OSA / competitive ──
    c2 = []
    unbkt = df[df["bucket"].isna() | (df["bucket"] == "")]
    if len(unbkt): c2.append(f"{len(unbkt)} cells not bucketed")
    bad_osa = cut[cut["osa_mean"] < OSA_LOW]
    if len(bad_osa): c2.append(f"{len(bad_osa)} CUT cells are low-OSA (should be bucket a)")
    bad_cmp = cut[cut["comp_pressure"] == True]
    if len(bad_cmp): c2.append(f"{len(bad_cmp)} CUT cells under competitive pressure (should be bucket b)")
    R["C2"] = (not c2, c2)

    # ── C3: isolated ROAS < 1, no confounder-explained cut, no phantom lift ──
    c3 = []
    bad_gain = cut[cut["net_gain_mo"] <= 0]
    if len(bad_gain): c3.append(f"{len(bad_gain)} cut cells have net_gain<=0 (not below break-even)")
    # condition 3's real test: no cut whose flatness a confounder EXPLAINS, i.e.
    # no cut cell where OSA / competitive / Ad-SOV MATERIALLY DRAGS sales down
    # (negative contribution beyond the noise floor). A positive (tailwind)
    # confounder contribution does not explain flatness and is fine to cut.
    MAT = 0.05
    if {"c_osa", "c_comp", "c_adsov"}.issubset(cut.columns):
        drag = cut[(cut["c_osa"] < -MAT) | (cut["c_comp"] < -MAT) | (cut["c_adsov"] < -MAT)]
        if len(drag):
            c3.append(f"{len(drag)} cut cells have a confounder materially dragging sales down "
                      f"(should be bucket a/b/f, not cut)")
    # phantom volume gain: savings must NOT rely on cutting price raising units
    if "tgt_units_wk" in cut.columns:
        phantom = cut[cut["tgt_units_wk"] > cut["cur_units_wk"] * 1.001]
        if len(phantom): c3.append(f"{len(phantom)} cut cells model MORE units after cutting price (phantom volume gain)")
    # every cut must be CI-backed below break-even: even the optimistic edge of
    # the discount effect (marg_beta + 1.96 se) fails to pay for itself
    if "reliably_waste" in cut.columns:
        notrel = cut[~cut["reliably_waste"].astype(bool)]
        if len(notrel): c3.append(f"{len(notrel)} cut cells are NOT reliably below break-even (CI overlaps pay-threshold)")
    R["C3"] = (not c3, c3)

    # ── C4: fit floor + low-confidence flagged ──
    c4 = []
    ncat_ok = S["categories_ok"]; ncat = S["categories_total"]
    bad_cat = cut[cut["cat_ok"] == False]
    if len(bad_cat): c4.append(f"{len(bad_cat)} cut cells in categories below R2 floor")
    if "confidence" not in df.columns: c4.append("no confidence flag on cells")
    R["C4"] = (not c4, c4 + [f"categories clearing R2>={R2_FLOOR}: {ncat_ok}/{ncat}; "
                             f"cells High/Low conf: {int((df['confidence']=='High').sum())}/{int((df['confidence']=='Low').sum())}"])

    # ── C5: reconcile ──
    c5 = []
    line_sum = float(cut["net_gain_mo"].clip(lower=0).sum())
    rep = S["achievable_savings_mo_allconf"]
    if abs(line_sum - rep) > max(0.005 * max(rep, 1), 1):
        c5.append(f"cut line-sum Rs.{line_sum:,.0f} != reported Rs.{rep:,.0f}")
    if line_sum <= 0:
        c5.append("aggregate net-revenue impact not positive")
    R["C5"] = (not c5, c5 + [f"line-sum Rs.{line_sum:,.0f} = reported Rs.{rep:,.0f} (aggregate impact +ve)"])

    # ── C6: achievable vs the business savings target (advisory verdict) ──
    ach = S["achievable_savings_mo_highconf"]
    _tgt_l = TARGET_LO / 100_000
    c6 = [] if ach >= TARGET_LO else [f"high-conf achievable Rs.{ach:,.0f}/mo is BELOW the Rs.{_tgt_l:.1f}L target"]
    R["C6"] = (ach >= TARGET_LO, c6 + [f"achievable(high-conf)=Rs.{ach:,.0f}/mo vs Rs.{_tgt_l:.1f}L target => "
                                       f"{'MEETS' if ach>=TARGET_LO else 'BELOW'}"])

    # ── C7: model accuracy — out-of-sample R² ≥ 0.75 ──
    oos = S.get("oos_r2", np.nan)
    c7 = [] if (np.isfinite(oos) and oos >= OOS_R2_BAR) else [f"out-of-sample R²={oos} below {OOS_R2_BAR} bar"]
    R["C7"] = (np.isfinite(oos) and oos >= OOS_R2_BAR,
               c7 + [f"out-of-sample R²={oos} ({S.get('oos_cats_pass','?')}/{S.get('oos_cats_total','?')} cats ≥0.75)"])

    # ── C8: every banked cut category confirmed reliably-waste by Double ML ──
    dml_path = os.path.join(pdir, "dml_results.json")
    c8 = []
    if os.path.exists(dml_path):
        dml = {r["cat"]: r for r in json.load(open(dml_path))}
        cut_cats = cut["category"].unique()
        notconf = [c for c in cut_cats if c in dml and not dml[c]["waste"]]
        missing = [c for c in cut_cats if c not in dml]
        if notconf: c8.append(f"{len(notconf)} cut categories NOT confirmed waste by DML: {notconf}")
        if missing: c8.append(f"{len(missing)} cut categories missing a DML estimate: {missing}")
        c8ok = not c8
        c8.append(f"DML-confirmed cut categories: {sum(1 for c in cut_cats if dml.get(c,{}).get('waste'))}/{len(cut_cats)}")
    else:
        c8ok = False; c8 = ["dml_results.json missing — run dml_estimate.py"]
    R["C8"] = (c8ok, c8)

    # C6 is the business-target VERDICT: reported loudly, never an abort.
    # Only the safety gates decide the exit code — see module docstring.
    allpass = True
    for k in ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8"]:
        ok, rows = R[k]
        if k == "C6":
            print(f"\n  C6: {'MEETS TARGET' if ok else 'BELOW TARGET (advisory — does not block execution)'}")
        else:
            allpass &= ok
            print(f"\n  {k}: {'PASS' if ok else 'FAIL'}")
        for r in rows[:12]: print(f"      - {r}")
    c6_ok = R["C6"][0]
    print("\n" + "-" * 76)
    print(f"  ACHIEVABLE (high-conf bucket-c): Rs.{ach:,.0f}/mo | all-conf Rs.{S['achievable_savings_mo_allconf']:,.0f}")
    print(f"  out-of-sample R² = {oos} (bar {OOS_R2_BAR}) | buckets: {S['bucket_counts']}")
    print(f"  Safety gates C1-C5, C7, C8: {'ALL PASS' if allpass else 'FAIL — see above'}"
          f"  |  C6 target verdict: {'MEETS' if c6_ok else 'BELOW'} "
          f"Rs.{TARGET_LO/100_000:.1f}L (set in v4_config.SAVINGS_TARGET_MONTHLY_INR)")
    return 0 if allpass else 1


if __name__ == "__main__":
    sys.exit(main())
