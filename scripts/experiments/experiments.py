"""
Experiment harness — compares model variants to find why MAPE is high / R^2 is low.

Re-uses features.csv from the latest pipeline run. Each experiment trains on
the same train/test split and reports:
  - Train log-R^2
  - Test log-R^2
  - Test log-MAE
  - Test raw-unit MAPE (excludes zero-demand)
  - Global price elasticity recovered (if applicable)

Experiments
-----------
E0  baseline           : current formula (log_price + discount_pct + bunch of controls)
E1  drop_collinear     : drop log1p_discount, discount_surprise, is_deep_promo,
                         price_surprise   (all mechanically tied to price/discount)
E2  cell_fixed_effects : OLS with C(sku_city) dummies, only log_price + osa + ad
                         + month  (no discount because log_price already encodes it)
E3  discount_only      : within-cell, drop log_price, keep discount_pct
                         (no log_price-vs-MRP cross-product confound)
E4  random_slope_lp    : MixedLM with random *slope* on log_price per sku_city
                         (current code uses random intercept only)
E5  per_category_ols   : 3 separate models (Jaggery, Dal, Oil) with cell FE
E6  fe_plus_seasonality: cell FE + DOW dummies + log_price (within-cell variation only)
E7  trimmed_outliers   : E2 but drop top/bottom 1% of log_units (heavy-tail tamer)

The harness prints a table at the end so you can see which intervention helps.
"""
import os, sys, glob, warnings
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf

warnings.filterwarnings("ignore")

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import v4_config as cfg

COLS = cfg.COL  # renamed: patsy uses C() for categoricals


def load_features():
    """Use the most recent features.csv from output/runs/."""
    runs = sorted(glob.glob(os.path.join(cfg.OUTPUT_DIR, "*", "features.csv")))
    assert runs, "No features.csv found — run pipeline first"
    latest = runs[-1]
    print(f"Using features from: {os.path.relpath(latest, ROOT)}")
    df = pd.read_csv(latest, parse_dates=[COLS["date"]])
    return df


def make_split(df):
    """Same time-based 80/20 split as Stage 4, on regular days only."""
    reg = df[df["is_regular_day"] == 1].copy()
    if COLS["grammage"] in reg.columns:
        reg["sku_city"] = (reg[COLS["product_id"]].astype(str) + "__"
                          + reg[COLS["grammage"]].astype(str) + "__"
                          + reg[COLS["city"]].astype(str))
    else:
        reg["sku_city"] = reg[COLS["product_id"]].astype(str) + "__" + reg[COLS["city"]].astype(str)

    dates = sorted(reg[COLS["date"]].unique())
    split_date = dates[int(len(dates) * 0.8)]
    train = reg[reg[COLS["date"]] <= split_date].copy()
    test  = reg[reg[COLS["date"]] >  split_date].copy()
    # FE models cannot predict unseen cells; restrict test to cells present in train
    seen = set(train["sku_city"].unique())
    test = test[test["sku_city"].isin(seen)].copy()
    return train, test


def score(name, model, train, test, price_var=None):
    """Compute train R2, test R2/MAE/MAPE; pull price elasticity if present."""
    yp_tr = np.asarray(model.predict(train))
    yp_te = np.asarray(model.predict(test))
    y_tr  = train["log_units"].values
    y_te  = test["log_units"].values

    def r2(y, p):
        m = np.isfinite(y) & np.isfinite(p)
        if m.sum() < 2: return float("nan")
        ss_res = ((y[m] - p[m])**2).sum()
        ss_tot = ((y[m] - y[m].mean())**2).sum()
        return 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    r2_tr = r2(y_tr, yp_tr)
    r2_te = r2(y_te, yp_te)

    m = np.isfinite(y_te) & np.isfinite(yp_te)
    log_mae = float(np.mean(np.abs(y_te[m] - yp_te[m])))

    act_u = np.exp(y_te[m]); prd_u = np.exp(yp_te[m])
    mape = float(np.mean(np.abs((act_u - prd_u) / np.maximum(act_u, 0.5))) * 100)

    elast = float("nan")
    if price_var and price_var in model.params.index:
        elast = float(model.params[price_var])

    return {
        "experiment": name,
        "train_R2_log": round(r2_tr, 3),
        "test_R2_log":  round(r2_te, 3),
        "test_logMAE":  round(log_mae, 3),
        "test_MAPE_%":  round(mape, 1),
        "elasticity":   round(elast, 3) if not np.isnan(elast) else None,
    }


def run_experiments():
    df = load_features()
    train, test = make_split(df)
    print(f"Train rows: {len(train):,}  |  Test rows: {len(test):,}")
    print(f"Train date max: {train[COLS['date']].max().date()}  |  Test date min: {test[COLS['date']].min().date()}")
    print()

    months = " + ".join([f"month_{m}" for m in range(2, 13) if f"month_{m}" in train.columns])

    results = []

    # ── E0 baseline ────────────────────────────────────────────────────
    f0 = ("log_units ~ C(category) + log_price + discount_pct + log1p_discount "
          "+ price_surprise + osa_rolling_7d + log_ad_sov + rpi + discount_surprise "
          f"+ is_weekend + is_deep_promo + {months}")
    m0 = smf.mixedlm(f0, data=train, groups=train["sku_city"]).fit(reml=True, method="lbfgs", maxiter=400)
    results.append(score("E0_baseline_current", m0, train, test, price_var="log_price"))

    # ── E1 drop collinear price features ────────────────────────────────
    f1 = ("log_units ~ C(category) + log_price + discount_pct "
          "+ osa_rolling_7d + log_ad_sov + rpi "
          f"+ is_weekend + {months}")
    m1 = smf.mixedlm(f1, data=train, groups=train["sku_city"]).fit(reml=True, method="lbfgs", maxiter=400)
    results.append(score("E1_drop_collinear", m1, train, test, price_var="log_price"))

    # ── E2 OLS with cell FE; identification only from within-cell price moves ─
    f2 = ("log_units ~ C(sku_city) + log_price "
          f"+ osa_rolling_7d + log_ad_sov + rpi + is_weekend + {months}")
    m2 = smf.ols(f2, data=train).fit()
    results.append(score("E2_cell_FE_OLS", m2, train, test, price_var="log_price"))

    # ── E3 discount_pct only, with cell FE ──────────────────────────────
    f3 = ("log_units ~ C(sku_city) + discount_pct "
          f"+ osa_rolling_7d + log_ad_sov + rpi + is_weekend + {months}")
    m3 = smf.ols(f3, data=train).fit()
    results.append(score("E3_disc_only_FE", m3, train, test, price_var="discount_pct"))

    # ── E4 random slope on log_price ────────────────────────────────────
    # MixedLM: groups + re_formula='~log_price' for random slope
    f4 = ("log_units ~ C(category) + log_price + discount_pct "
          f"+ osa_rolling_7d + log_ad_sov + rpi + is_weekend + {months}")
    try:
        m4 = smf.mixedlm(
            f4, data=train, groups=train["sku_city"], re_formula="~log_price"
        ).fit(reml=True, method="lbfgs", maxiter=400)
        results.append(score("E4_random_slope_lp", m4, train, test, price_var="log_price"))
    except Exception as e:
        print(f"E4 failed: {e}")
        results.append({"experiment": "E4_random_slope_lp", "train_R2_log": None,
                         "test_R2_log": None, "test_logMAE": None,
                         "test_MAPE_%": None, "elasticity": None})

    # ── E5 per-category OLS with cell FE ───────────────────────────────
    cat_metrics = []
    for cat, sub_tr in train.groupby("category"):
        sub_te = test[test["category"] == cat]
        if len(sub_tr) < 200 or sub_te.empty:
            continue
        f5 = ("log_units ~ C(sku_city) + log_price "
              f"+ osa_rolling_7d + log_ad_sov + rpi + is_weekend + {months}")
        try:
            m5 = smf.ols(f5, data=sub_tr).fit()
            s = score(f"E5__{cat}", m5, sub_tr, sub_te, price_var="log_price")
            cat_metrics.append(s)
        except Exception as e:
            print(f"E5 {cat} failed: {e}")
    if cat_metrics:
        n_te = sum(len(test[test["category"] == c["experiment"][4:]]) for c in cat_metrics)
        # weight by test rows for combined view
        combo_r2 = np.nan
        results.extend(cat_metrics)
        # also a combined pooled metric (concatenated predictions)
        # rebuild predictions and aggregate
        y_tr_all, p_tr_all, y_te_all, p_te_all = [], [], [], []
        for cat, sub_tr in train.groupby("category"):
            sub_te = test[test["category"] == cat]
            if len(sub_tr) < 200 or sub_te.empty: continue
            f5 = ("log_units ~ C(sku_city) + log_price "
                  f"+ osa_rolling_7d + log_ad_sov + rpi + is_weekend + {months}")
            m5 = smf.ols(f5, data=sub_tr).fit()
            y_tr_all.append(sub_tr["log_units"].values); p_tr_all.append(np.asarray(m5.predict(sub_tr)))
            y_te_all.append(sub_te["log_units"].values); p_te_all.append(np.asarray(m5.predict(sub_te)))
        y_tr_c = np.concatenate(y_tr_all); p_tr_c = np.concatenate(p_tr_all)
        y_te_c = np.concatenate(y_te_all); p_te_c = np.concatenate(p_te_all)
        def r2(y, p):
            m = np.isfinite(y) & np.isfinite(p)
            ss_res = ((y[m]-p[m])**2).sum(); ss_tot = ((y[m]-y[m].mean())**2).sum()
            return 1 - ss_res/ss_tot if ss_tot > 0 else float("nan")
        log_mae = float(np.mean(np.abs(y_te_c - p_te_c)))
        act_u = np.exp(y_te_c); prd_u = np.exp(p_te_c)
        mape = float(np.mean(np.abs((act_u-prd_u)/np.maximum(act_u,0.5)))*100)
        results.append({
            "experiment": "E5_per_cat_COMBINED",
            "train_R2_log": round(r2(y_tr_c, p_tr_c), 3),
            "test_R2_log":  round(r2(y_te_c, p_te_c), 3),
            "test_logMAE":  round(log_mae, 3),
            "test_MAPE_%":  round(mape, 1),
            "elasticity":   None,
        })

    # ── E6 cell FE + DOW dummies, log_price + osa + ad ──────────────────
    # DOW: derive from date
    for d in (train, test):
        d["dow"] = pd.to_datetime(d[COLS["date"]]).dt.dayofweek
    f6 = ("log_units ~ C(sku_city) + log_price "
          f"+ osa_rolling_7d + log_ad_sov + rpi + C(dow) + {months}")
    m6 = smf.ols(f6, data=train).fit()
    results.append(score("E6_cellFE_DOW", m6, train, test, price_var="log_price"))

    # ── E7 trimmed outliers ────────────────────────────────────────────
    lo, hi = np.quantile(train["log_units"], [0.01, 0.99])
    tr_trim = train[(train["log_units"] >= lo) & (train["log_units"] <= hi)].copy()
    f7 = ("log_units ~ C(sku_city) + log_price "
          f"+ osa_rolling_7d + log_ad_sov + rpi + is_weekend + {months}")
    m7 = smf.ols(f7, data=tr_trim).fit()
    results.append(score("E7_FE_trim1pct", m7, tr_trim, test, price_var="log_price"))

    # ── Diagnostic: check multicollinearity on baseline ─────────────────
    print("\n— MULTICOLLINEARITY CHECK on raw price/discount features —")
    cols = ["log_price", "discount_pct", "log1p_discount", "price_surprise",
            "discount_surprise", "is_deep_promo"]
    cor = train[cols].corr().round(3)
    print(cor.to_string())

    # ── Per-cell log-price variation ────────────────────────────────────
    print("\n— Within-cell log_price std (need >0.05 for elasticity to be identifiable) —")
    lp_std = train.groupby("sku_city")["log_price"].std().describe().round(3)
    print(lp_std.to_string())
    n_zero = (train.groupby("sku_city")["log_price"].std() < 0.01).sum()
    print(f"Cells with near-zero within-cell log_price variation: {n_zero}/{train['sku_city'].nunique()}")

    # ── Results table ──────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print("EXPERIMENT RESULTS")
    print("=" * 90)
    res_df = pd.DataFrame(results)
    print(res_df.to_string(index=False))
    res_df.to_csv(os.path.join(ROOT, "experiment_results.csv"), index=False)
    print("\nSaved to: experiment_results.csv")


if __name__ == "__main__":
    run_experiments()
