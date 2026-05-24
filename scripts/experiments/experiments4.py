"""
Tune the new Stage 4 — time-trend variants and per-cell elasticity quality.

V1: current production (cell FE + log_price + badge_resid + time_trend + Huber)
V2: drop time_trend
V3: keep time_trend but use log(1+t)  (gentler)
V4: capped time_trend (cap at 180 days so post-launch growth still attributable to price)
V5: month-aware trend: time_trend * is_first_year_of_data  (no trend after launch period)
V6: drop time_trend AND drop month dummies — let cell FE + log_price do the work

Evaluation:
  - Per-category log_price slope (should be plausible, in [-3, -0.5])
  - Aggregated 3% discount-bin MAPE  (what curve consumes)
  - Per-cell elasticity distribution
"""
import os, sys, glob, warnings
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
import statsmodels.api as sm

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import v4_config as cfg
COLS = cfg.COL


def load():
    runs = sorted(glob.glob(os.path.join(cfg.OUTPUT_DIR, "*", "features.csv")))
    return pd.read_csv(runs[-1], parse_dates=[COLS["date"]])


def split(df):
    reg = df[df["is_regular_day"] == 1].copy()
    reg["sku_city"] = (reg[COLS["product_id"]].astype(str) + "__"
                      + reg[COLS["grammage"]].astype(str) + "__"
                      + reg[COLS["city"]].astype(str))
    # add time_trend + badge_resid
    reg = reg.sort_values([COLS["product_id"], COLS["city"], COLS["date"]])
    reg["time_trend"] = reg.groupby("sku_city")[COLS["date"]].transform(
        lambda s: (s - s.min()).dt.days
    ).astype(float)
    def _badge(g):
        d  = g["discount_pct"].values.astype(float)
        lp = g["log_price"].values.astype(float)
        if len(g) < 5 or np.std(lp) < 1e-6:
            return pd.Series(d - d.mean(), index=g.index)
        X = np.column_stack([np.ones(len(g)), lp])
        coef = np.linalg.lstsq(X, d, rcond=None)[0]
        return pd.Series(d - X @ coef, index=g.index)
    reg["badge_resid"] = reg.groupby("sku_city", group_keys=False).apply(_badge)

    # variant features
    reg["log_time"]    = np.log1p(reg["time_trend"])
    reg["time_capped"] = reg["time_trend"].clip(upper=180)

    dates = sorted(reg[COLS["date"]].unique())
    sd = pd.Timestamp(dates[int(len(dates) * 0.8)])
    tr = reg[reg[COLS["date"]] <= sd].copy()
    te = reg[reg[COLS["date"]] >  sd].copy()
    te = te[te["sku_city"].isin(tr["sku_city"].unique())].copy()
    return tr, te


def fit_huber(formula, tr):
    return smf.rlm(formula, data=tr, M=sm.robust.norms.HuberT()).fit()


def agg_mape(model, te):
    yp = np.asarray(model.predict(te))
    t = te.copy(); t["pu"] = np.exp(np.clip(yp, -3, 10))
    t["bin"] = (t["discount_pct"] // 3 * 3).astype(int)
    g = t.groupby(["sku_city", "bin"], as_index=False).agg(
        n=(COLS["offtake_qty"], "size"),
        a=(COLS["offtake_qty"], "mean"),
        p=("pu", "mean"),
    )
    g = g[g["n"] >= 3]
    if g.empty: return float("nan"), float("nan")
    ae = (g["a"] - g["p"]).abs()
    mape = float((ae / g["a"].clip(lower=0.5)).mean() * 100)
    ss_res = ((g["a"] - g["p"])**2).sum()
    ss_tot = ((g["a"] - g["a"].mean())**2).sum()
    r2 = float(1 - ss_res/ss_tot) if ss_tot > 0 else 0.0
    return mape, r2


def show(name, models, tr, te):
    """Print per-category elasticities + aggregated MAPE/R^2 across categories."""
    rows = []
    print(f"\n=== {name} ===")
    print(f"{'category':14s} {'log_p':>8s} {'p_se':>8s} {'badge':>10s} "
          f"{'tr_R2':>7s} {'te_R2':>7s} {'agg_MAPE':>9s} {'agg_R2u':>9s}")
    for cat, m in models.items():
        sub_tr = tr[tr["category"] == cat]; sub_te = te[te["category"] == cat]
        ytr = sub_tr["log_units"].values; ptr = np.asarray(m.predict(sub_tr))
        yte = sub_te["log_units"].values; pte = np.asarray(m.predict(sub_te))
        def _r2(y, p):
            mask = np.isfinite(y) & np.isfinite(p)
            ss_res = ((y[mask]-p[mask])**2).sum(); ss_tot = ((y[mask]-y[mask].mean())**2).sum()
            return float(1 - ss_res/ss_tot) if ss_tot > 0 else 0.0
        tr_r2 = _r2(ytr, ptr); te_r2 = _r2(yte, pte)
        mape, r2u = agg_mape(m, sub_te)
        pe = m.params.get("log_price", float("nan"))
        bs = m.params.get("badge_resid", float("nan"))
        se = m.bse.get("log_price", float("nan"))
        print(f"{cat:14s} {pe:+8.3f} {se:8.3f} {bs:+10.4f} "
              f"{tr_r2:7.3f} {te_r2:7.3f} {mape:8.1f}% {r2u:9.3f}")


def main():
    df = load(); tr, te = split(df)
    months = " + ".join([f"month_{m}" for m in range(2, 13) if f"month_{m}" in tr.columns])

    base = ("log_units ~ C(sku_city) + log_price + badge_resid "
            "+ osa_rolling_7d + log_ad_sov + rpi + is_weekend")

    variants = {
        "V1 with_time_trend":      base + " + time_trend + " + months,
        "V2 NO_time_trend":        base + " + " + months,
        "V3 log_time":             base + " + log_time + " + months,
        "V4 time_capped_180d":     base + " + time_capped + " + months,
        "V6 NO_time_NO_months":    base,
    }

    for name, f in variants.items():
        models = {}
        for cat, sub in tr.groupby("category"):
            if len(sub) < 200: continue
            try:
                models[cat] = fit_huber(f, sub)
            except Exception as e:
                print(f"{name} / {cat}: failed: {e}")
        show(name, models, tr, te)

    # Also evaluate ALL-data (no split) per-cell elasticity from cell-level OLS
    print("\n\n=== PER-CELL OLS ELASTICITIES (sanity check — all data, no train/test) ===")
    full = pd.concat([tr, te])
    rows = []
    for cell, g in full.groupby("sku_city"):
        if len(g) < 30 or g["log_price"].std() < 0.01: continue
        y = g["log_units"].values
        X = np.column_stack([np.ones(len(g)), g["log_price"].values])
        try:
            slope = float(np.linalg.lstsq(X, y, rcond=None)[0][1])
            rows.append({"cell": cell, "n": len(g),
                         "slope": round(slope, 2),
                         "lp_std": round(g["log_price"].std(), 3)})
        except Exception:
            continue
    pc = pd.DataFrame(rows)
    print(pc.sort_values("slope").to_string(index=False))
    print(f"\nMedian per-cell slope: {pc['slope'].median():.2f}")
    print(f"Mean per-cell slope:   {pc['slope'].mean():.2f}")
    print(f"Per-category median per-cell slope:")
    pc["category"] = pc["cell"].str.split("__").str[0]
    print(pc.groupby("category")["slope"].agg(["median", "mean", "count"]).round(2))


if __name__ == "__main__":
    main()
