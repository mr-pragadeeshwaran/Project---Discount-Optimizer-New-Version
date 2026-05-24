"""
MAPE-reduction experiments — May 2026.

Goal: reduce overall and per-product MAPE. Current state (baseline):
  Train log-R² 0.86, Test log-R² 0.27, Aggregated 3-ppt-bin MAPE 52.5%.

Each experiment trains the per-category Huber model with one tweak, then
reports overall + per-product aggregated MAPE and R² so we can rank
interventions by business impact.

Experiments:
  E0  baseline             : current production model
  E1  last_6mo_train       : train only on last 180 days (post-launch-ramp)
  E2  tighter_outliers_z2  : drop more outliers (|z|>2.0 instead of >3.0)
  E3  lagged_baseline      : add log(28d rolling mean units, lag 14d) as a
                             demand-baseline feature that absorbs secular
                             growth without correlating with current price
  E4  oil_floor_minus6     : allow Sunflower Oil elasticity floor of -6
                             (currently clipped at -4 — many cells pinned)
  E5  interaction_weekend  : add (log_price × is_weekend) interaction
  E6  combined_best        : apply E2 + E3 together (the safe combo)
"""
import os, sys, glob, warnings
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
import statsmodels.api as sm

warnings.filterwarnings("ignore")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
import v4_config as cfg
COLS = cfg.COL

# Plausibility clip (matches production)
EL_FLOOR, EL_CEIL = -4.0, -0.3


def load_features():
    runs = sorted(glob.glob(os.path.join(cfg.OUTPUT_DIR, "*", "features.csv")))
    p = runs[-1]
    print(f"Features file: {os.path.relpath(p, ROOT)}")
    return pd.read_csv(p, parse_dates=[COLS["date"]])


def add_cell_id(df):
    df = df.copy()
    if COLS["grammage"] in df.columns:
        df["sku_city"] = (df[COLS["product_id"]].astype(str) + "__"
                          + df[COLS["grammage"]].astype(str) + "__"
                          + df[COLS["city"]].astype(str))
    else:
        df["sku_city"] = (df[COLS["product_id"]].astype(str) + "__"
                          + df[COLS["city"]].astype(str))
    return df


def add_badge_resid(df):
    """Decorrelate discount_pct from log_price within each cell."""
    df = df.copy()
    def _badge(g):
        d  = g["discount_pct"].values.astype(float)
        lp = g["log_price"].values.astype(float)
        if len(g) < 5 or np.std(lp) < 1e-6:
            return pd.Series(d - d.mean(), index=g.index)
        X = np.column_stack([np.ones(len(g)), lp])
        coef = np.linalg.lstsq(X, d, rcond=None)[0]
        return pd.Series(d - X @ coef, index=g.index)
    df["badge_resid"] = df.groupby("sku_city", group_keys=False).apply(_badge)
    return df


def add_lagged_baseline(df, window=28, lag=14):
    """log(rolling mean of units over `window` days, lagged by `lag` days).
    A per-cell secular-level feature that doesn't correlate with today's price.
    """
    df = df.sort_values(["sku_city", COLS["date"]]).copy()
    def _roll(g):
        # Pure-historical rolling mean (lagged so it can't peek at today)
        s = g[COLS["offtake_qty"]].clip(lower=0.1)
        # Shift first to ensure we use data strictly before the prediction day
        roll = s.shift(lag).rolling(window=window, min_periods=7).mean()
        return np.log(roll.clip(lower=0.1))
    df["log_units_baseline"] = df.groupby("sku_city", group_keys=False).apply(_roll)
    # Fill leading NaN with the cell's overall log mean (the model can use it)
    df["log_units_baseline"] = df.groupby("sku_city")["log_units_baseline"].transform(
        lambda s: s.fillna(np.log(max(s.dropna().mean(), 0.1)) if s.notna().any() else 0)
    )
    return df


def restrict_to_recent(df, days=180):
    max_date = df[COLS["date"]].max()
    cutoff = max_date - pd.Timedelta(days=days)
    return df[df[COLS["date"]] >= cutoff].copy()


def filter_outliers(df, z_thresh=2.0, min_obs=30):
    """Drop rows whose log_units is > z_thresh sigma from cell mean (regular days)."""
    df = df.copy()
    reg = df[df["is_regular_day"] == 1]
    drop_idx = set()
    for cell, g in reg.groupby("sku_city"):
        if len(g) < min_obs: continue
        log_q = np.log(g[COLS["offtake_qty"]].clip(lower=0.1))
        mu, sig = log_q.mean(), log_q.std()
        if sig < 1e-6: continue
        z = (log_q - mu) / sig
        drop_idx.update(g.index[(z.abs() > z_thresh)])
    df.loc[list(drop_idx), "is_regular_day"] = 0
    return df


def split_train_test(df, test_pct=0.20):
    reg = df[df["is_regular_day"] == 1].copy()
    if len(reg) < 50:
        return reg.iloc[:0], reg.iloc[:0]
    dates = sorted(reg[COLS["date"]].unique())
    sd = dates[int(len(dates) * (1 - test_pct))]
    tr = reg[reg[COLS["date"]] <= sd].copy()
    te = reg[reg[COLS["date"]] >  sd].copy()
    seen = set(tr["sku_city"].unique())
    te = te[te["sku_city"].isin(seen)].copy()
    return tr, te


def fit_per_category(train, formula):
    """Fit one Huber-robust OLS per category. Returns dict{category: model}."""
    models = {}
    for cat, sub in train.groupby("category"):
        if len(sub) < 200: continue
        try:
            m = smf.rlm(formula, data=sub, M=sm.robust.norms.HuberT()).fit()
            models[cat] = m
        except Exception:
            try:
                models[cat] = smf.ols(formula, data=sub).fit()
            except Exception:
                pass
    return models


def evaluate(models, test, name):
    """Compute overall + per-category aggregated MAPE and R²."""
    if test.empty:
        return {"name": name, "n_test": 0, "agg_mape": float("nan"), "agg_r2": float("nan"),
                "log_r2": float("nan"), "per_product": {}}

    parts = []
    for cat, m in models.items():
        sub = test[test["category"] == cat]
        if sub.empty: continue
        try:
            sub = sub.copy()
            sub["yhat_log"] = m.predict(sub)
            sub["yhat_units"] = np.exp(sub["yhat_log"].clip(-3, 10))
            parts.append(sub)
        except Exception:
            continue
    if not parts:
        return {"name": name, "n_test": 0, "agg_mape": float("nan"), "agg_r2": float("nan"),
                "log_r2": float("nan"), "per_product": {}}
    pred = pd.concat(parts)

    # Overall log R²
    y = pred["log_units"].values
    yh = pred["yhat_log"].values
    m = np.isfinite(y) & np.isfinite(yh)
    ss_res = ((y[m] - yh[m])**2).sum()
    ss_tot = ((y[m] - y[m].mean())**2).sum()
    log_r2 = float(1 - ss_res/ss_tot) if ss_tot > 0 else 0.0

    # Aggregated by cell × 3% discount bin
    pred["disc_bin"] = (pred["discount_pct"] // 3 * 3).astype(int)
    grp = pred.groupby(["sku_city", "disc_bin"], as_index=False).agg(
        n=(COLS["offtake_qty"], "size"),
        actual=(COLS["offtake_qty"], "mean"),
        pred=("yhat_units", "mean"),
        product_id=(COLS["product_id"], "first"),
        grammage=(COLS["grammage"], "first") if COLS["grammage"] in pred.columns else (COLS["product_id"], "first"),
        category=("category", "first"),
    )
    grp = grp[grp["n"] >= 3]
    overall_mape = float(((grp["actual"] - grp["pred"]).abs() / grp["actual"].clip(lower=0.5)).mean() * 100)
    ss_res_u = ((grp["actual"] - grp["pred"])**2).sum()
    ss_tot_u = ((grp["actual"] - grp["actual"].mean())**2).sum()
    overall_r2_units = float(1 - ss_res_u/ss_tot_u) if ss_tot_u > 0 else 0.0

    # Per-product breakdown
    per_product = {}
    for (pid, grm), gg in grp.groupby(["product_id", "grammage"]):
        if len(gg) < 3: continue
        pkey = f"{pid}|{grm}"
        mape = float(((gg["actual"] - gg["pred"]).abs() / gg["actual"].clip(lower=0.5)).mean() * 100)
        ss_r = ((gg["actual"] - gg["pred"])**2).sum()
        ss_t = ((gg["actual"] - gg["actual"].mean())**2).sum()
        r2u = float(1 - ss_r/ss_t) if ss_t > 0 else 0.0
        per_product[pkey] = {"n_bins": len(gg), "mape": round(mape, 1), "r2": round(r2u, 3)}

    return {
        "name": name,
        "n_train": len(test),
        "log_r2": round(log_r2, 3),
        "agg_mape": round(overall_mape, 1),
        "agg_r2": round(overall_r2_units, 3),
        "per_product": per_product,
    }


def run_all():
    df = load_features()
    df = add_cell_id(df)
    months = [f"month_{m}" for m in range(2, 13) if f"month_{m}" in df.columns]
    base_formula = ("log_units ~ C(sku_city) + log_price + badge_resid "
                    "+ osa_rolling_7d + log_ad_sov + rpi + is_weekend + "
                    + " + ".join(months))

    results = []

    # ── E0: baseline ──────────────────────────────────────────────────
    df0 = add_badge_resid(df)
    tr0, te0 = split_train_test(df0)
    m0 = fit_per_category(tr0, base_formula)
    results.append(evaluate(m0, te0, "E0_baseline"))

    # ── E1: last 6 months only ────────────────────────────────────────
    df1 = restrict_to_recent(add_badge_resid(df), days=180)
    tr1, te1 = split_train_test(df1)
    m1 = fit_per_category(tr1, base_formula)
    results.append(evaluate(m1, te1, "E1_last_6mo"))

    # ── E2: tighter outlier filter (z=2.0) ────────────────────────────
    df2 = filter_outliers(add_badge_resid(df), z_thresh=2.0)
    tr2, te2 = split_train_test(df2)
    m2 = fit_per_category(tr2, base_formula)
    results.append(evaluate(m2, te2, "E2_outliers_z2"))

    # ── E3: lagged demand-baseline feature ────────────────────────────
    df3 = add_lagged_baseline(add_badge_resid(df))
    tr3, te3 = split_train_test(df3)
    formula3 = base_formula + " + log_units_baseline"
    m3 = fit_per_category(tr3, formula3)
    results.append(evaluate(m3, te3, "E3_lagged_baseline"))

    # ── E4: per-category looser bound (Sunflower Oil only) ────────────
    # No formula change — clip changes happen at the per-cell step. For this
    # harness (which uses the pooled per-category coefficient for prediction),
    # E4 has no effect. We skip it here and note in the report.
    # The intervention is meaningful in the per-cell SHRUNK elasticity step.
    results.append({"name": "E4_oil_floor_minus6",
                    "note": "Skipped — affects per-cell clipping only, not pooled coefficient. "
                            "Will be tested via production pipeline if E3/E2 win.",
                    "log_r2": None, "agg_mape": None, "agg_r2": None, "per_product": {}})

    # ── E5: log_price × is_weekend interaction ────────────────────────
    df5 = add_badge_resid(df)
    tr5, te5 = split_train_test(df5)
    formula5 = base_formula + " + log_price:is_weekend"
    m5 = fit_per_category(tr5, formula5)
    results.append(evaluate(m5, te5, "E5_lp_weekend_interaction"))

    # ── E6: COMBINED — outlier z=2 + lagged baseline ──────────────────
    df6 = filter_outliers(add_badge_resid(df), z_thresh=2.0)
    df6 = add_lagged_baseline(df6)
    tr6, te6 = split_train_test(df6)
    formula6 = base_formula + " + log_units_baseline"
    m6 = fit_per_category(tr6, formula6)
    results.append(evaluate(m6, te6, "E6_combined_z2_lagged"))

    # ── E7: WINNER STACK — last 6mo + tighter outliers (z=2) ──────────
    df7 = restrict_to_recent(add_badge_resid(df), days=180)
    df7 = filter_outliers(df7, z_thresh=2.0)
    tr7, te7 = split_train_test(df7)
    m7 = fit_per_category(tr7, base_formula)
    results.append(evaluate(m7, te7, "E7_recent_plus_z2"))

    # ── E8: last 6mo + lagged baseline ────────────────────────────────
    df8 = restrict_to_recent(add_badge_resid(df), days=180)
    df8 = add_lagged_baseline(df8)
    tr8, te8 = split_train_test(df8)
    formula8 = base_formula + " + log_units_baseline"
    m8 = fit_per_category(tr8, formula8)
    results.append(evaluate(m8, te8, "E8_recent_plus_lagged"))

    # ── E9: ALL three stacked ─────────────────────────────────────────
    df9 = restrict_to_recent(add_badge_resid(df), days=180)
    df9 = filter_outliers(df9, z_thresh=2.0)
    df9 = add_lagged_baseline(df9)
    tr9, te9 = split_train_test(df9)
    formula9 = base_formula + " + log_units_baseline"
    m9 = fit_per_category(tr9, formula9)
    results.append(evaluate(m9, te9, "E9_all_three"))

    # ── Output comparison table ───────────────────────────────────────
    print("\n" + "=" * 90)
    print("OVERALL RESULTS")
    print("=" * 90)
    print(f"{'experiment':30s} {'log_R2':>8s} {'agg_MAPE':>10s} {'agg_R2(units)':>14s}")
    for r in results:
        if r.get("agg_mape") is None:
            print(f"{r['name']:30s} {'—':>8s} {'—':>10s} {'—':>14s}   {r.get('note', '')}")
        else:
            print(f"{r['name']:30s} {r['log_r2']:>8.3f} {r['agg_mape']:>9.1f}% {r['agg_r2']:>14.3f}")

    print("\n" + "=" * 90)
    print("PER-PRODUCT MAPE  (lower = better)")
    print("=" * 90)
    # Build product key set
    all_pkeys = set()
    for r in results:
        all_pkeys.update(r.get("per_product", {}).keys())
    if all_pkeys:
        cols = sorted(all_pkeys)
        header = f"{'experiment':30s} " + " ".join(f"{p:>14s}" for p in cols)
        print(header)
        for r in results:
            if not r.get("per_product"): continue
            row = f"{r['name']:30s} "
            for p in cols:
                v = r["per_product"].get(p)
                if v is None:
                    row += f"{'—':>14s} "
                else:
                    row += f"{v['mape']:>13.1f}% "
            print(row)

    # Save results
    out_csv = os.path.join(ROOT, "scripts", "experiments", "mape_experiment_results.csv")
    rows = []
    for r in results:
        if not r.get("per_product"):
            rows.append({"experiment": r["name"], "scope": "overall",
                         "log_r2": r.get("log_r2"), "mape": r.get("agg_mape"),
                         "r2_units": r.get("agg_r2")})
        else:
            rows.append({"experiment": r["name"], "scope": "overall",
                         "log_r2": r.get("log_r2"), "mape": r.get("agg_mape"),
                         "r2_units": r.get("agg_r2")})
            for pk, pv in r["per_product"].items():
                rows.append({"experiment": r["name"], "scope": pk,
                             "mape": pv["mape"], "r2_units": pv["r2"]})
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    print(f"\nResults saved to: {os.path.relpath(out_csv, ROOT)}")


if __name__ == "__main__":
    run_all()
