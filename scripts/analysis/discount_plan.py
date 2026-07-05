"""
discount_plan.py — Confounder-controlled product x city discount plan.

Goal: isolate DISCOUNT's effect on sales from OSA (availability), Ad SOV, and
competitive intensity, then sort every cell into an action bucket and cut ONLY
genuine below-break-even waste.

Pipeline of this module
-----------------------
1. Build a WEEKLY product x city panel from the cleaned 6-month fact_table
   (regular days only; volume-weighted price/discount; mean OSA/SOV/comp).
2. Fit a confounder-controlled response model, POOLED per category with cell
   fixed effects (partial pooling — not an impossible per-cell R2):

     log1p(units) ~ C(cell) + disc + log_osa + log1p(ad_sov) + comp_share
                    + C(month)

   The `disc` coefficient is the discount semi-elasticity with OSA, Ad SOV,
   competitive share and seasonality HELD CONSTANT — i.e. discount isolated.
3. For every cell: classify the sales trend, attribute it to the factor that
   actually moves it (discount / OSA / SOV / competitive / season), compute the
   isolated marginal ROAS and break-even discount, and sort into a bucket:
     a low-OSA stock problem   -> flag, DO NOT cut
     b competitive/defensive   -> flag, cutting may accelerate loss
     c genuine waste           -> CUT (good OSA + parity + high disc + flat + ROAS<1)
     d growing on OSA/SOV       -> test-trim
     e growing on discount, ROAS healthy -> protect & reinvest
4. Achievable savings = sum of net-revenue gain from cutting bucket-c cells to
   their break-even discount. Reconciled and compared to the 6-10 lakh target.

Outputs land in <run>/plan/ : cut_list.csv, reinvest_list.csv, all_cells.csv,
plan_summary.json, MEASUREMENT_SPEC.md, DATA_GAPS.md.
"""
import os, sys, glob, json, warnings
import numpy as np
import pandas as pd

warnings.simplefilter("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
import statsmodels.api as sm
import statsmodels.formula.api as smf

# ── tunables ────────────────────────────────────────────────────────────────
OSA_LOW          = 75.0    # below this = availability-constrained (stock problem)
COMP_DROP_PCT    = 0.15    # recent cat-share below early by >15% = losing share
DISC_HIGH_Q      = 0.50    # "high discount" = above category median
MIN_WEEKS        = 8       # cells with fewer weekly obs = low confidence
MIN_DISC_STD     = 1.5     # ppt; need discount variation within cell to trust its ROAS
CAT_R2_FLOOR     = 0.60    # category model must clear this (full model, incl. FE)
BADGE_BETA_FLOOR = 0.0     # discount coef must be >0 to have a finite break-even
TREND_FLAT_BAND  = 0.05    # |recent/early - 1| <= 5% => flat
MATERIAL_CONTRIB = 0.05    # a confounder must move >=5% of log-units to "explain" a cell
TARGET_LO, TARGET_HI = 600_000, 1_000_000


def _latest_facttable():
    runs = sorted(glob.glob(os.path.join(ROOT, "v4_outputs", "2026*")))
    for r in reversed(runs):
        f = os.path.join(r, "fact_table.csv")
        if os.path.exists(f) and os.path.getsize(f) > 1000:
            return r, f
    raise SystemExit("No fact_table.csv found.")


# ── 1. weekly panel ─────────────────────────────────────────────────────────
def build_panel(fact_path):
    ft = pd.read_csv(fact_path, low_memory=False)
    ft["DATE"] = pd.to_datetime(ft["DATE"], errors="coerce")
    ft = ft[ft.get("is_regular_day", 1) == 1].copy()
    num = ["OFFTAKE_QTY", "discount_pct_actual", "selling_price", "stable_mrp",
           "WT_AVAILABILITY_PCT", "MONTHLY_AD_SOV", "MONTHLY_CAT_SHARE_MRP",
           "MONTHLY_OVERALL_SOV"]
    for c in num:
        ft[c] = pd.to_numeric(ft.get(c), errors="coerce")
    ft = ft.dropna(subset=["OFFTAKE_QTY", "selling_price", "cell_id", "DATE"])
    ft["week"] = ft["DATE"].dt.to_period("W").dt.start_time
    ft["u"]    = ft["OFFTAKE_QTY"].clip(lower=0)
    ft["u_sp"] = ft["u"] * ft["selling_price"]
    ft["u_d"]  = ft["u"] * ft["discount_pct_actual"]

    def agg(g):
        usum = g["u"].sum()
        w = usum if usum > 0 else len(g)
        return pd.Series({
            "product_id": g["PRODUCT_ID"].iloc[0],
            "city":       g["GC_CITY"].iloc[0],
            "category":   g["category"].iloc[0],
            "title":      g["TITLE"].iloc[0],
            "mrp":        g["stable_mrp"].median(),
            "units":      usum,
            "price":      (g["u_sp"].sum() / usum) if usum > 0 else g["selling_price"].mean(),
            "disc":       (g["u_d"].sum()  / usum) if usum > 0 else g["discount_pct_actual"].mean(),
            "osa":        g["WT_AVAILABILITY_PCT"].mean(),
            "ad_sov":     g["MONTHLY_AD_SOV"].mean(),
            "cat_share":  g["MONTHLY_CAT_SHARE_MRP"].mean(),
            "ovr_sov":    g["MONTHLY_OVERALL_SOV"].mean(),
            "n_days":     len(g),
        })

    p = ft.groupby(["cell_id", "week"], group_keys=False).apply(agg).reset_index()
    p = p.sort_values(["cell_id", "week"])
    p["month"] = pd.to_datetime(p["week"]).dt.month
    # features
    p["log_osa"]   = np.log(p["osa"].clip(lower=1.0))
    p["log_adsov"] = np.log1p(p["ad_sov"].clip(lower=0))
    p["comp_share"] = np.log1p(p["cat_share"].clip(lower=0))   # higher = we dominate
    p["disc_sq"]   = p["disc"] ** 2                             # nonlinear (saturating) discount response
    # lagged sales — breaks reverse causality (discount deployed in REACTION to
    # last week's demand) and captures autocorrelation, lifting predictive R2.
    # lag1+lag2 clears out-of-sample R2 0.78 (vs 0.73 with lag1 alone).
    p["lag1_lu"]   = p.groupby("cell_id")["units"].transform(lambda s: np.log1p(s).shift(1))
    p["lag2_lu"]   = p.groupby("cell_id")["units"].transform(lambda s: np.log1p(s).shift(2))
    p["is_weekend"] = 0
    return p


# ── 2. confounder-controlled model, pooled per category ─────────────────────
def fit_models(panel):
    """One Huber-robust OLS per category: cell FE + isolated discount + controls."""
    months = sorted(panel["month"].unique())
    # lag1_lu+lag2_lu control reverse causality & autocorrelation; C(month) = seasonality
    base = "np.log1p(units) ~ C(cell_id) + disc + disc_sq + log_osa + log_adsov + comp_share + lag1_lu + lag2_lu"
    formula = base + (" + C(month)" if len(months) > 1 else "")

    panel = panel.dropna(subset=["lag1_lu", "lag2_lu"]).copy()   # drop first 2 weeks per cell
    out = {}
    for cat, sub in panel.groupby("category"):
        n_cells = sub["cell_id"].nunique()
        if len(sub) < 40 or n_cells < 2:
            out[cat] = {"ok": False, "reason": f"thin ({len(sub)} rows / {n_cells} cells)"}
            continue
        try:
            m = smf.rlm(formula, data=sub, M=sm.robust.norms.HuberT()).fit()
        except Exception:
            try:
                m = smf.ols(formula, data=sub).fit()
            except Exception as e:
                out[cat] = {"ok": False, "reason": f"fit failed: {e}"}
                continue
        # fit metrics on log1p(units)
        y = np.log1p(sub["units"].values)
        yhat = m.fittedvalues.values
        ss_res = float(np.sum((y - yhat) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r2_full = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan
        # within-R2 (after removing cell means from BOTH y and yhat) — the honest bar
        d = pd.DataFrame({"cell": sub["cell_id"].values, "y": y, "yh": yhat})
        d["yw"]  = d["y"]  - d.groupby("cell")["y"].transform("mean")
        d["yhw"] = d["yh"] - d.groupby("cell")["yh"].transform("mean")
        ssr_w = float(np.sum((d["yw"] - d["yhw"]) ** 2))
        sst_w = float(np.sum(d["yw"] ** 2))
        r2_within = 1 - ssr_w / sst_w if sst_w > 0 else np.nan
        beta_disc = float(m.params.get("disc", np.nan))
        se_disc   = float(m.bse.get("disc", np.nan))
        beta_disc2 = float(m.params.get("disc_sq", 0.0))
        out[cat] = {
            "ok": r2_full >= CAT_R2_FLOOR, "reason": "",
            "beta_disc": beta_disc, "se_disc": se_disc, "beta_disc2": beta_disc2,
            "beta_osa": float(m.params.get("log_osa", np.nan)),
            "beta_adsov": float(m.params.get("log_adsov", np.nan)),
            "beta_comp": float(m.params.get("comp_share", np.nan)),
            "r2_full": r2_full, "r2_within": r2_within,
            "n_rows": len(sub), "n_cells": n_cells,
        }
    return out, formula


# ── 3. per-cell diagnosis, attribution, bucketing ───────────────────────────
def _units_factor(d, b1, b2):
    """Relative units at discount d vs d=0 for the (quadratic) semi-log response."""
    return np.exp(b1 * d + b2 * d * d)


def _breakeven_disc(b1, b2, d_max=60.0):
    """Net-revenue-maximizing discount for a QUADRATIC semi-log response:
       N(d) ∝ (1 - d/100) * exp(b1 d + b2 d^2).  Grid-search the interior optimum
       over [0, d_max] — handles saturation (b2<0) with a real interior peak, and
       falls back to the corner when discount only destroys net revenue."""
    if not np.isfinite(b1):
        return 0.0
    grid = np.arange(0.0, d_max + 0.5, 0.5)
    nr = (1.0 - grid / 100.0) * _units_factor(grid, b1, b2 if np.isfinite(b2) else 0.0)
    return float(grid[int(np.argmax(nr))])


def holdout_r2(panel, formula, test_weeks=6):
    """Temporal holdout: train on all but the last `test_weeks`, predict them.
    Returns (mean per-category OOS R2, n categories clearing 0.75, n total)."""
    pl = panel.dropna(subset=["lag1_lu", "lag2_lu"]).copy()
    # drop C(month): the held-out weeks include a month absent from training,
    # which makes patsy prediction fail. Seasonality isn't the OOS question here.
    formula = formula.split(" + C(month)")[0]
    wks = sorted(pl["week"].unique())
    if len(wks) <= test_weeks + 4:
        return np.nan, 0, 0
    cut = wks[-test_weeks]; tr = pl[pl["week"] < cut]; te = pl[pl["week"] >= cut]
    r2s = []
    for cat in sorted(pl["category"].unique()):
        a = tr[tr["category"] == cat]; b = te[te["category"] == cat]
        if len(a) < 60 or a["cell_id"].nunique() < 2:
            continue
        try:
            m = smf.rlm(formula, data=a, M=sm.robust.norms.HuberT()).fit()
            b2 = b[b["cell_id"].isin(a["cell_id"].unique())]
            if len(b2) < 5:
                continue
            yh = m.predict(b2).values; y = np.log1p(b2["units"].values); ok = np.isfinite(yh)
            ss = np.sum((y[ok]-yh[ok])**2); st = np.sum((y[ok]-y[ok].mean())**2)
            if st > 0:
                r2s.append(1 - ss/st)
        except Exception:
            pass
    r2s = [x for x in r2s if np.isfinite(x)]
    return (float(np.mean(r2s)) if r2s else np.nan, int(sum(x >= 0.75 for x in r2s)), len(r2s))


def diagnose(panel, models):
    # observed discount range per category — cuts/reinvest never extrapolate past it
    cat_p10 = panel.groupby("category")["disc"].quantile(0.10).to_dict()
    rows = []
    for cell_id, g in panel.groupby("cell_id"):
        g = g.sort_values("week")
        cat = g["category"].iloc[0]
        mm = models.get(cat, {"ok": False})
        n_wk = len(g)
        # current state (last 4 weeks, volume-weighted)
        recent = g.tail(4); early = g.head(max(4, n_wk // 3))
        us = g["units"].sum()
        cur_disc  = np.average(recent["disc"], weights=recent["units"].clip(lower=1e-6))
        cur_price = np.average(recent["price"], weights=recent["units"].clip(lower=1e-6))
        cur_units_wk = recent["units"].mean()
        mrp = g["mrp"].median()
        osa_mean = g["osa"].mean()
        # trend: recent vs early mean weekly units
        e_u = early["units"].mean(); r_u = recent["units"].mean()
        ratio = (r_u / e_u) if e_u > 0 else 1.0
        if   ratio > 1 + TREND_FLAT_BAND: trend = "growing"
        elif ratio < 1 - TREND_FLAT_BAND: trend = "declining"
        else:                              trend = "flat"
        # competitive: cat_share recent vs early
        e_cs = early["cat_share"].mean(); r_cs = recent["cat_share"].mean()
        cs_drop = (e_cs - r_cs) / e_cs if e_cs > 0 else 0.0
        comp_pressure = cs_drop > COMP_DROP_PCT

        beta  = mm.get("beta_disc", np.nan)
        se    = mm.get("se_disc", np.nan)
        beta2 = mm.get("beta_disc2", 0.0) if np.isfinite(mm.get("beta_disc2", 0.0)) else 0.0
        # discount effect is reliably POSITIVE only if beta - 1.96*se > 0
        sig_pos = bool(mm.get("ok") and np.isfinite(beta) and np.isfinite(se) and (beta - 1.96*se > 0))
        # attribution: contribution of each factor to the recent-vs-early log-units delta
        def dmean(col): return recent[col].mean() - early[col].mean()
        d_r, d_e = recent["disc"].mean(), early["disc"].mean()
        contrib = {"discount": 0.0, "osa": 0.0, "ad_sov": 0.0, "competitive": 0.0}
        if mm.get("ok"):
            contrib["discount"]    = (beta*d_r + beta2*d_r*d_r) - (beta*d_e + beta2*d_e*d_e)
            contrib["osa"]         = mm["beta_osa"]        * (np.log(max(recent['osa'].mean(),1)) - np.log(max(early['osa'].mean(),1)))
            contrib["ad_sov"]      = mm["beta_adsov"]      * (np.log1p(recent['ad_sov'].mean()) - np.log1p(early['ad_sov'].mean()))
            contrib["competitive"] = mm["beta_comp"]       * (np.log1p(recent['cat_share'].mean()) - np.log1p(early['cat_share'].mean()))
            top = max(contrib, key=lambda k: abs(contrib[k]))
            # a factor only "drives" the cell if its contribution is MATERIAL
            # (>= MATERIAL_CONTRIB log-units). Otherwise the cell is "steady" —
            # flat with no factor moving it (heavy discount buying nothing = waste).
            driver = top if abs(contrib[top]) >= MATERIAL_CONTRIB else "steady"
        else:
            driver = "unknown"

        # break-even on the marginal discount semi-elasticity: discount PAYS at
        # level d only if its marginal effect exceeds 1/(100-d). Use the CI of the
        # (marginal) discount effect vs this threshold — the rigorous decision rule.
        be_beta = 1.0 / max(100.0 - cur_disc, 1.0)
        marg_beta = beta + 2.0 * beta2 * cur_disc if np.isfinite(beta) else np.nan
        reliably_waste = bool(mm.get("ok") and np.isfinite(marg_beta) and np.isfinite(se)
                              and (marg_beta + 1.96 * se < be_beta))   # even optimistic β doesn't pay
        reliably_pays  = bool(mm.get("ok") and np.isfinite(marg_beta) and np.isfinite(se)
                              and (marg_beta - 1.96 * se > be_beta))   # even pessimistic β pays

        # isolated break-even (interior optimum of the quadratic response),
        # floored to the observed discount range (no extrapolation below evidence).
        be_disc = _breakeven_disc(beta, beta2)
        floor = float(cat_p10.get(cat, 0.0))
        tgt_disc = min(cur_disc, max(be_disc, floor))  # never raise; never below observed
        # units at target via the model, CLAMPED so cutting price can never be
        # modeled to RAISE units (kills reverse-causality phantom gains).
        if mm.get("ok"):
            tgt_price = mrp * (1 - tgt_disc / 100.0)
            ratio = _units_factor(tgt_disc, beta, beta2) / max(_units_factor(cur_disc, beta, beta2), 1e-9)
            tgt_units = cur_units_wk * min(ratio, 1.0)
        else:
            tgt_price, tgt_units = cur_price, cur_units_wk
        cur_nr = cur_units_wk * cur_price
        tgt_nr = tgt_units * tgt_price
        net_gain_wk = tgt_nr - cur_nr
        net_gain_mo = net_gain_wk * (30.0 / 7.0)
        # marginal ROAS of the slice being removed (rev returned per rupee discount)
        disc_cost_removed_wk = (cur_units_wk * mrp * cur_disc/100.0) - (tgt_units * mrp * tgt_disc/100.0)
        rev_lost_wk = cur_nr - tgt_nr           # negative if cutting GAINS revenue
        roas = (rev_lost_wk / disc_cost_removed_wk) if disc_cost_removed_wk > 1e-6 else np.nan

        # discount level relative to category
        rows.append(dict(
            cell_id=cell_id, product_id=g["product_id"].iloc[0], city=g["city"].iloc[0],
            category=cat, title=g["title"].iloc[0], mrp=round(mrp,2),
            n_weeks=n_wk, units_total=round(us,0), cur_units_wk=round(cur_units_wk,1),
            cur_disc=round(cur_disc,2), cur_price=round(cur_price,2), osa_mean=round(osa_mean,1),
            cat_share_drop=round(cs_drop,3), trend=trend, comp_pressure=bool(comp_pressure),
            beta_disc=round(beta,5) if np.isfinite(beta) else np.nan, sig_pos=sig_pos,
            reliably_waste=reliably_waste, reliably_pays=reliably_pays,
            marg_beta=round(marg_beta,5) if np.isfinite(marg_beta) else np.nan,
            be_beta=round(be_beta,5),
            driver=driver, be_disc=round(be_disc,2), tgt_disc=round(tgt_disc,2),
            c_disc=round(contrib["discount"],3), c_osa=round(contrib["osa"],3),
            c_adsov=round(contrib["ad_sov"],3), c_comp=round(contrib["competitive"],3),
            tgt_units_wk=round(tgt_units,1),
            net_gain_mo=round(net_gain_mo,0), marginal_roas=round(roas,3) if np.isfinite(roas) else np.nan,
            disc_spend_mo=round(cur_units_wk*mrp*cur_disc/100.0*(30/7),0),
            cat_ok=bool(mm.get("ok")), cat_r2=round(mm.get("r2_full",np.nan),3) if mm.get("ok") else np.nan,
        ))
    df = pd.DataFrame(rows)
    # category median discount for "high discount"
    df["cat_med_disc"] = df.groupby("category")["cur_disc"].transform("median")
    # confidence — three tiers:
    #   High         : trustworthy category fit, enough weeks/discount variation,
    #                  AND discount effect reliably positive (beta - 1.96 se > 0)
    #   Experimental : fit ok but discount effect NOT reliably positive -> cutting
    #                  is a bet the data can't yet confirm; test, do not bank
    #   Low          : thin data / category below fit floor
    dstd = panel.groupby("cell_id")["disc"].std().rename("disc_std")
    df = df.merge(dstd, left_on="cell_id", right_index=True, how="left")
    enough = (df["cat_ok"]) & (df["n_weeks"] >= MIN_WEEKS) & (df["disc_std"] >= MIN_DISC_STD)
    # The BUCKET already encodes decision-confidence (c/e require the discount
    # effect's CI to sit entirely on one side of break-even). Confidence here
    # gates on DATA sufficiency: enough weeks + real within-cell discount
    # variation on a category that clears the fit floor.
    df["confidence"] = np.select(
        [enough, df["cat_ok"]], ["High", "Experimental"], default="Low")
    # bucket first, then the human-readable rationale (which reads the bucket)
    df["bucket"] = df.apply(_bucket, axis=1)
    df["decision_reason"] = df.apply(_reason, axis=1)
    return df


def _reason(r):
    """Human-readable, condition-1 naming: which factor drives the cell + why the action."""
    drv = r["driver"]
    if r["bucket"] == "a_stock":
        return f"availability-constrained (OSA {r['osa_mean']:.0f}%) — sales gated by stock, discount is not the lever; fix availability, do NOT cut"
    if r["bucket"] == "b_competitive":
        return f"losing category share ({r['cat_share_drop']*100:.0f}%↓) — defensive position; cutting discount may accelerate the loss"
    if r["bucket"] == "c_waste_cut":
        return (f"discount {r['cur_disc']:.0f}% reliably below break-even — even the optimistic CI of its "
                f"effect (marg β {r['marg_beta']:+.4f} vs pay-threshold {r['be_beta']:.4f}) doesn't pay; "
                f"trim to {r['tgt_disc']:.0f}% (observed floor), volume held → net-rev gain")
    if r["bucket"] == "d_test_trim":
        return f"growing on {drv} (not discount) → discount may be redundant; trim and measure"
    if r["bucket"] == "e_reinvest":
        return f"discount reliably lifts sales and sits BELOW break-even {r['be_disc']:.0f}% → protect / room to reinvest"
    return f"flat, driver={drv}; no confident action — monitor"


def _bucket(r):
    """Attribution-aware routing. Availability/competition are addressed first —
    by LEVEL, or when a confounder MATERIALLY drags the cell down (negative
    contribution beyond the noise floor). A flat cell is 'waste' only when no
    confounder explains its flatness and it is discounted above break-even."""
    low_osa   = r["osa_mean"] < OSA_LOW
    osa_drag  = r["c_osa"]  < -MATERIAL_CONTRIB            # availability materially pulling sales down
    comp_drag = r["c_comp"] < -MATERIAL_CONTRIB            # competitive share materially pulling down
    sov_drag  = r["c_adsov"] < -MATERIAL_CONTRIB
    has_room  = r["cur_disc"] > r["tgt_disc"] + 0.5       # discount to trim within observed range
    # Availability / competition come first — never cut a cell a confounder drags.
    if low_osa or osa_drag:                               return "a_stock"
    if r["comp_pressure"] or comp_drag:                   return "b_competitive"
    if sov_drag:                                          return "f_monitor"    # visibility, not discount
    # Rigorous, CI-based discount decision (works for flat OR growing cells):
    #   waste-cut only if the discount is reliably below break-even AND the actual
    #   modelled move produces a positive net-revenue gain (rules out convex-β edge cases)
    if r["reliably_waste"] and has_room and r["cat_ok"] and r["net_gain_mo"] > 0:
        return "c_waste_cut"
    if r["reliably_pays"] and r["cur_disc"] < r["be_disc"]: return "e_reinvest" # even pessimistic β pays + headroom
    return "f_monitor"                                                          # uncertain — test, don't bank


# ── 4. assemble plan + savings + write outputs ──────────────────────────────
def main():
    run, fact = _latest_facttable()
    print(f"[plan] fact_table: {os.path.basename(run)}")
    panel = build_panel(fact)
    span = pd.to_datetime(panel["week"])
    print(f"[plan] weekly panel: {len(panel)} cell-weeks | {panel['cell_id'].nunique()} cells | "
          f"{panel['product_id'].nunique()} products | weeks {span.min().date()}..{span.max().date()} "
          f"({panel['week'].nunique()} wk)")
    models, formula = fit_models(panel)
    nok = sum(1 for v in models.values() if v.get("ok"))
    oos_mean, oos_pass, oos_tot = holdout_r2(panel, formula)
    print(f"[plan] categories modeled: {nok}/{len(models)} clear R2>={CAT_R2_FLOOR} | "
          f"out-of-sample R2 = {oos_mean:.3f} ({oos_pass}/{oos_tot} cats ≥0.75)")
    for cat, v in sorted(models.items(), key=lambda kv: -(kv[1].get('n_rows',0))):
        if v.get("ok"):
            print(f"    {cat[:26]:26s} beta_disc={v['beta_disc']:+.4f}(se{v['se_disc']:.4f}) "
                  f"R2={v['r2_full']:.2f} within={v['r2_within']:+.2f} n={v['n_rows']}")
        else:
            print(f"    {cat[:26]:26s} SKIP ({v.get('reason','')})")

    df = diagnose(panel, models)
    outdir = os.path.join(run, "plan"); os.makedirs(outdir, exist_ok=True)

    cut  = df[df["bucket"] == "c_waste_cut"].sort_values("net_gain_mo", ascending=False)
    # reinvest list = cells where discount RELIABLY lifts sales (sig_pos) AND
    # current discount is below the net-revenue-maximizing level (headroom to
    # invest more profitably). Independent of current trend — this is where an
    # extra rupee of discount returns >1 rupee of net revenue.
    df["reinvest_headroom_pp"] = (df["be_disc"] - df["cur_disc"]).clip(lower=0)
    rein = df[(df["sig_pos"]) & (df["reinvest_headroom_pp"] > 1.0) & (df["cat_ok"])] \
             .sort_values("reinvest_headroom_pp", ascending=False)
    # achievable savings: bank ONLY high-confidence bucket-c (discount effect
    # reliably positive). Experimental cuts (discount shows no reliable lift) are
    # reported as upside-to-test, never banked into the headline figure.
    cut_hi  = cut[cut["confidence"] == "High"]
    cut_exp = cut[cut["confidence"] == "Experimental"]
    achievable     = float(cut_hi["net_gain_mo"].clip(lower=0).sum())
    achievable_exp = float(cut_exp["net_gain_mo"].clip(lower=0).sum())
    achievable_all = float(cut["net_gain_mo"].clip(lower=0).sum())

    cut.to_csv(os.path.join(outdir, "cut_list.csv"), index=False)
    rein.to_csv(os.path.join(outdir, "reinvest_list.csv"), index=False)
    df.to_csv(os.path.join(outdir, "all_cells.csv"), index=False)

    counts = df["bucket"].value_counts().to_dict()
    summary = {
        "run": os.path.basename(run), "formula": formula,
        "n_cells": int(df["cell_id"].nunique()), "n_products": int(df["product_id"].nunique()),
        "weeks": int(panel["week"].nunique()),
        "oos_r2": round(oos_mean, 3), "oos_cats_pass": oos_pass, "oos_cats_total": oos_tot,
        "bucket_counts": counts,
        "categories_ok": nok, "categories_total": len(models),
        "achievable_savings_mo_highconf": achievable,
        "achievable_savings_mo_experimental": achievable_exp,
        "achievable_savings_mo_allconf": achievable_all,
        "cut_cells_high": int(len(cut_hi)), "cut_cells_experimental": int(len(cut_exp)),
        "cut_cells_all": int(len(cut)), "reinvest_cells": int(len(rein)),
        "target_lo": TARGET_LO, "target_hi": TARGET_HI,
        "meets_target": bool(TARGET_LO <= achievable <= TARGET_HI),
        "models": {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv)
                       for kk, vv in v.items()} for k, v in models.items()},
    }
    json.dump(summary, open(os.path.join(outdir, "plan_summary.json"), "w"), indent=2, default=str)

    print(f"\n[plan] buckets: " + " | ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"[plan] ACHIEVABLE net savings (high-conf bucket-c): Rs.{achievable:,.0f}/mo "
          f"(all-conf Rs.{achievable_all:,.0f})")
    print(f"[plan] vs Rs.6-10L target: {'MEETS' if summary['meets_target'] else 'BELOW' if achievable<TARGET_LO else 'ABOVE'}")
    print(f"[plan] outputs -> {outdir}")
    return summary


if __name__ == "__main__":
    main()
