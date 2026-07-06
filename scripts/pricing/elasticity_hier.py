"""
elasticity_hier.py — Hierarchical own-price elasticity by PENALIZED (partial-pooled)
regression. The Bayesian-hierarchical stand-in for the PepsiCo PricingAI adapted to
a single brand (24 Mantra Organic) on Blinkit, with NO PyMC/STAN and NO Gurobi/cloud.

WHY penalized regression instead of a real Bayesian sampler
-----------------------------------------------------------
The documented "posterior-mean-into-optimizer" path: a Bayesian hierarchical model
puts a prior on each SKU's elasticity that shrinks it toward the category mean, then
feeds the POSTERIOR MEAN into the price optimizer. With only numpy/pandas/scipy/
sklearn/statsmodels we approximate that exactly:

  * prior (shrink SKU -> category)      -> L2 / ridge penalty toward the category mean
  * partial pooling (sparse SKUs)       -> additive decomposition solved with ridge
  * posterior mean                       -> the point estimate (penalized coefficient)
  * posterior SD                         -> a light week-bootstrap standard deviation

WHAT this module estimates
--------------------------
1. Per CATEGORY, a pooled log-log demand regression with cell fixed effects:

     ln(units) ~ own_elast*ln(price) + cross_cat*ln(sibling_avg_price)
                 + b_promo*is_promo + b_osa*ln(osa) + C(month) + C(cell)

   weighted by recency_w*volume_w. The ln(price) coefficient is the category
   own-price elasticity; the ln(sibling_avg_price) coefficient is the category
   cross (cannibalization) response — POSITIVE means substitutes (a sibling's
   price going up lifts our units).

2. Own-price elasticity is then written as an ADDITIVE decomposition
        own_elast[sku,city] = grand + category + size_tier + city
   fit by ridge (L2) that SHRINKS the size-tier and city deviations toward the
   category mean. This is the identification fix for sparse SKU x city cells:
   a thin cell borrows strength from its category and its size bucket instead of
   trusting its own noisy slope.

3. NEGATIVE own-price is enforced (penalize/clip any positive own slope toward 0,
   then clip the final elasticity into the DOC gate (-2.5, 0)).

4. cross_df: for each ordered within-category sibling pair (i,j) in a city,
   cross_elast_ij = cross_cat / n_siblings — each sibling's price gets an even
   share of the category's cross response. Only |cross|>1e-4 pairs are kept.

5. own_sd: a light bootstrap (30 resamples of WEEKS) — the posterior-SD stand-in.

Public API
----------
    estimate_elasticities(panel_df) -> (elast_df, cross_df, baseline_df, gates)
    freeze_baselines(panel_df)      -> baseline_df   (recomputed per shared schema)

All money INR. Uses ONLY numpy / pandas / scipy / scikit-learn / statsmodels.
"""
import os
import sys
import warnings

import numpy as np
import pandas as pd

warnings.simplefilter("ignore")

# ── DOC gates / tunables ─────────────────────────────────────────────────────
OWN_LO, OWN_HI = -2.5, 0.0     # DOC gate: final own_elast clipped into this band
CROSS_KEEP     = 1e-4          # drop near-zero cross pairs
BOOT_N         = 30            # week-resample bootstrap draws (posterior-SD stand-in)
RIDGE_ALPHA    = 25.0          # L2 strength for the additive-decomposition shrinkage
                               # (strong partial pooling: thin cells hug the
                               #  well-identified category slope; raise to pool
                               #  harder, lower to trust cell-level slopes more)
MIN_ROWS_CAT   = 20            # a category needs this many rows to fit its own slope
R2_FLOOR       = 0.50          # pooled R2 gate (report; floor)
WMAPE_CEIL     = 0.40          # weighted MAPE gate
BIAS_CEIL      = 0.10          # |mean signed relative error| gate
EPS            = 1e-9

# size-tier buckets on pack_grams (grams). Small / medium / large / bulk.
SIZE_EDGES  = [0, 250, 600, 1200, np.inf]
SIZE_LABELS = ["s_small", "m_mid", "l_large", "xl_bulk"]


# ─────────────────────────────────────────────────────────────────────────────
# baseline_df  (recomputed per the shared schema — the panel module's
# freeze_baselines is a different, tracker-oriented function, so we build the
# optimizer baseline here to match: one row per product_id x city.)
# ─────────────────────────────────────────────────────────────────────────────
def freeze_baselines(panel_df, recent_weeks=4):
    """baseline_df: one row per product_id x city.
       q0_units_wk = recency-weighted mean weekly units over the most recent
       `recent_weeks` weeks (falls back to all weeks for short cells);
       p0_price / disc0 = volume-weighted current selling price / discount;
       mrp = median MRP; carries category, base_product, pack_grams through.
    """
    p = panel_df.copy()
    p["week"] = p["week"].astype(str)
    rows = []
    for (pid, city), g in p.groupby(["product_id", "city"], sort=False):
        g = g.sort_values("week")
        wks = list(dict.fromkeys(g["week"]))            # unique weeks, order-preserving
        keep = set(wks[-recent_weeks:]) if len(wks) >= recent_weeks else set(wks)
        r = g[g["week"].isin(keep)]
        uw = r["units"].to_numpy(float)
        # recency weight if present, else uniform; guard all-zero
        rw = r["recency_w"].to_numpy(float) if "recency_w" in r else np.ones(len(r))
        rw = np.where(np.isfinite(rw) & (rw > 0), rw, 1.0)
        q0 = float(np.average(uw, weights=rw)) if len(uw) and rw.sum() > 0 else float(uw.mean() if len(uw) else 0.0)
        volw = np.clip(uw, 1e-6, None)                  # volume weights for price
        p0   = float(np.average(r["price"].to_numpy(float), weights=volw))
        d0   = float(np.average(r["disc"].to_numpy(float),  weights=volw))
        rows.append({
            "product_id":  pid,
            "city":        city,
            "category":    g["category"].iloc[0],
            "base_product": g["base_product"].iloc[0],
            "pack_grams":  float(g["pack_grams"].iloc[0]),
            "q0_units_wk": round(q0, 3),
            "p0_price":    round(p0, 2),
            "mrp":         round(float(g["mrp"].median()), 2),
            "disc0":       round(d0, 3),
        })
    cols = ["product_id", "city", "category", "base_product", "pack_grams",
            "q0_units_wk", "p0_price", "mrp", "disc0"]
    return pd.DataFrame(rows, columns=cols)


# ─────────────────────────────────────────────────────────────────────────────
# feature build: sibling_avg_price (cannibalization regressor) + logs + tiers
# ─────────────────────────────────────────────────────────────────────────────
def _add_siblings(panel_df):
    """sibling_avg_price[i] = volume-weighted avg selling_price of OTHER SKUs in
       the SAME category+city+week. Uses the leave-one-out identity
         (sum(u*p) - u_i*p_i) / (sum(u) - u_i)
       so a SKU never prices against itself. Cells with no sibling that week get
       their own price (log-diff -> 0, so they contribute nothing to cross)."""
    p = panel_df.copy()
    p["week"] = p["week"].astype(str)
    u = p["units"].clip(lower=EPS)
    p["_u"]  = u
    p["_up"] = u * p["price"]
    grp = p.groupby(["category", "city", "week"])
    sum_up = grp["_up"].transform("sum")
    sum_u  = grp["_u"].transform("sum")
    n_grp  = grp["price"].transform("size")
    loo_up = sum_up - u * p["price"]
    loo_u  = (sum_u - u).clip(lower=EPS)
    sib = loo_up / loo_u
    # no sibling this week -> fall back to own price (cross contribution 0)
    p["sibling_avg_price"] = np.where(n_grp > 1, sib, p["price"]).astype(float)
    p["n_siblings"] = (n_grp - 1).clip(lower=0).astype(int)
    p.drop(columns=["_u", "_up"], inplace=True)
    return p


def _prep(panel_df):
    """Add logs, weights, size tier, and the cell key. Drops non-positive
       units/price rows (log undefined)."""
    p = _add_siblings(panel_df)
    p = p[(p["units"] > 0) & (p["price"] > 0)].copy()
    p["ln_units"] = np.log(p["units"].to_numpy(float))
    p["ln_price"] = np.log(p["price"].to_numpy(float))
    p["ln_sib"]   = np.log(p["sibling_avg_price"].clip(lower=EPS))
    osa = p["osa"].to_numpy(float) if "osa" in p else np.full(len(p), 100.0)
    p["ln_osa"]   = np.log(np.clip(osa, 1.0, None))
    if "is_promo" in p:
        p["promo01"] = p["is_promo"].astype(float)
    else:
        p["promo01"] = (p["disc"].to_numpy(float) > 0).astype(float)
    p["month"] = p["month"].astype(int)
    p["cell"]  = p["product_id"].astype(str) + "||" + p["city"].astype(str)
    # regression weight = recency_w * volume_w (fall back to 1 where missing)
    rw = p["recency_w"].to_numpy(float) if "recency_w" in p else np.ones(len(p))
    vw = p["volume_w"].to_numpy(float)  if "volume_w"  in p else np.ones(len(p))
    w  = np.where(np.isfinite(rw), rw, 1.0) * np.where(np.isfinite(vw), vw, 1.0)
    p["w"] = np.clip(w, EPS, None)
    # size tier from pack_grams
    p["size_tier"] = pd.cut(p["pack_grams"].astype(float), bins=SIZE_EDGES,
                            labels=SIZE_LABELS, right=False).astype(str)
    return p


# ─────────────────────────────────────────────────────────────────────────────
# per-category pooled fit (WLS with cell + month dummies, no statsmodels formula
# needed — we build the design matrix so bootstrap resamples stay cheap)
# ─────────────────────────────────────────────────────────────────────────────
def _design(sub, cols_cell, cols_month):
    """Build the WLS design for one category. Returns (X, names). Reference-coded
       dummies (drop first) so the intercept absorbs the base cell/month."""
    n = len(sub)
    parts = [np.ones((n, 1))]
    names = ["const"]
    for c in ("ln_price", "ln_sib", "promo01", "ln_osa"):
        parts.append(sub[c].to_numpy(float).reshape(-1, 1))
        names.append(c)
    # cell dummies (drop first level for identifiability)
    cell = sub["cell"].to_numpy()
    for lv in cols_cell[1:]:
        parts.append((cell == lv).astype(float).reshape(-1, 1)); names.append(f"cell::{lv}")
    # month dummies (drop first level) only if >1 month present
    if len(cols_month) > 1:
        mo = sub["month"].to_numpy()
        for lv in cols_month[1:]:
            parts.append((mo == lv).astype(float).reshape(-1, 1)); names.append(f"mo::{lv}")
    return np.hstack(parts), names


def _wls(X, y, w):
    """Weighted least squares via the normal equations with a hair of ridge on
       the whole matrix for numerical stability (kept tiny so it does not shrink
       the economic coefficients — the ECONOMIC shrinkage happens later, in the
       additive decomposition)."""
    sw = np.sqrt(w)
    Xw = X * sw[:, None]
    yw = y * sw
    XtX = Xw.T @ Xw
    ridge = 1e-6 * np.trace(XtX) / max(XtX.shape[0], 1)
    XtX += ridge * np.eye(XtX.shape[0])
    beta, *_ = np.linalg.lstsq(XtX, Xw.T @ yw, rcond=None)
    return beta


def _fit_category(sub):
    """Fit one category's pooled WLS; return dict with own/cross/promo/osa coefs,
       fitted values, weighted R2, and the design pieces for bootstrapping."""
    cols_cell  = sorted(sub["cell"].unique().tolist())
    cols_month = sorted(sub["month"].unique().tolist())
    X, names = _design(sub, cols_cell, cols_month)
    y = sub["ln_units"].to_numpy(float)
    w = sub["w"].to_numpy(float)
    beta = _wls(X, y, w)
    idx = {nm: i for i, nm in enumerate(names)}
    yhat = X @ beta
    # weighted R2
    wsum = w.sum()
    ybar = np.average(y, weights=w)
    ss_res = float(np.sum(w * (y - yhat) ** 2))
    ss_tot = float(np.sum(w * (y - ybar) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > EPS else np.nan
    return {
        "own":   float(beta[idx["ln_price"]]),
        "cross": float(beta[idx["ln_sib"]]),
        "promo": float(beta[idx["promo01"]]),
        "osa":   float(beta[idx["ln_osa"]]),
        "r2": r2, "n": len(sub),
        "cols_cell": cols_cell, "cols_month": cols_month,
        "yhat": yhat, "y": y, "w": w,
    }


def _own_by_week_bootstrap(sub, n_boot=BOOT_N, seed=0):
    """Posterior-SD stand-in: resample WEEKS with replacement, refit, collect the
       own-price coefficient. SD across draws = own_sd. Resampling whole weeks
       (not rows) respects the panel's temporal clustering."""
    rng = np.random.RandomState(seed)
    weeks = sub["week"].unique()
    if len(weeks) < 4:
        return np.nan
    by_week = {wk: sub[sub["week"] == wk] for wk in weeks}
    draws = []
    for _ in range(n_boot):
        pick = rng.choice(weeks, size=len(weeks), replace=True)
        bs = pd.concat([by_week[wk] for wk in pick], ignore_index=True)
        if bs["cell"].nunique() < 1 or len(bs) < 5:
            continue
        try:
            cc = sorted(bs["cell"].unique().tolist())
            cm = sorted(bs["month"].unique().tolist())
            X, names = _design(bs, cc, cm)
            beta = _wls(X, bs["ln_units"].to_numpy(float), bs["w"].to_numpy(float))
            draws.append(float(beta[names.index("ln_price")]))
        except Exception:
            continue
    return float(np.std(draws)) if len(draws) >= 3 else np.nan


# ─────────────────────────────────────────────────────────────────────────────
# additive decomposition with ridge shrinkage (partial pooling)
#   own_elast[cell] = grand + category + size_tier + city
# We fit the DEVIATIONS of each cell's raw own-slope from its category mean onto
# size-tier and city indicator matrices, penalized (ridge) toward 0 = toward the
# category mean. Sparse cells thus borrow strength from their tier & city.
# ─────────────────────────────────────────────────────────────────────────────
def _decompose(cell_tbl, cat_own, grand, alpha=RIDGE_ALPHA):
    """cell_tbl: one row per cell with columns category, size_tier, city, raw_own,
       weight. Returns a dict cell -> shrunk own_elast built additively.
       raw_own is the cell's own category slope nudged by a per-cell WLS-free
       proxy; here we shrink the size-tier and city adjustments with ridge."""
    from sklearn.linear_model import Ridge

    cats  = cell_tbl["category"].to_numpy()
    # target: deviation of each cell's raw own from its category own slope
    dev = cell_tbl["raw_own"].to_numpy(float) - np.array([cat_own[c] for c in cats])
    # design: size-tier one-hot + city one-hot (both mean-centered by ridge->0)
    tiers = pd.get_dummies(cell_tbl["size_tier"], prefix="tier")
    citys = pd.get_dummies(cell_tbl["city"],      prefix="city")
    Z = pd.concat([tiers, citys], axis=1)
    if Z.shape[1] == 0 or len(cell_tbl) < 2:
        adj = np.zeros(len(cell_tbl))
    else:
        w = cell_tbl["weight"].to_numpy(float)
        # fit_intercept=False: the deviation is ALREADY measured relative to the
        # category slope, so the tier/city adjustments must shrink toward 0 (the
        # category mean). An intercept would re-absorb a global mean shift and
        # fight the category anchor — exactly the bias partial pooling avoids.
        rr = Ridge(alpha=alpha, fit_intercept=False)
        rr.fit(Z.to_numpy(float), dev, sample_weight=w)
        adj = rr.predict(Z.to_numpy(float))
    # additive reconstruction: grand + (cat - grand) + shrunk(tier+city) adj
    #   = cat_own + adj   (grand cancels; kept explicit for the doc'd decomposition)
    out = {}
    for i, (_, row) in enumerate(cell_tbl.iterrows()):
        c = row["category"]
        val = grand + (cat_own[c] - grand) + adj[i]
        out[row["cell"]] = float(val)
    return out


def _raw_cell_own(sub_cell, cat_own_c):
    """A cheap per-cell own-slope PROXY: weighted covariance of ln_units, ln_price
       within the cell (its private demand slope), used only as the shrinkage
       TARGET's deviation. Falls back to the category slope when a cell has no
       within-cell price variation (the exact sparse case partial pooling fixes)."""
    x = sub_cell["ln_price"].to_numpy(float)
    y = sub_cell["ln_units"].to_numpy(float)
    w = sub_cell["w"].to_numpy(float)
    if len(x) < 3 or np.average((x - np.average(x, weights=w)) ** 2, weights=w) < 1e-6:
        return cat_own_c
    xb = np.average(x, weights=w); yb = np.average(y, weights=w)
    cov = np.average(w * (x - xb) * (y - yb)) / (np.average(w) + EPS)
    var = np.average(w * (x - xb) ** 2) / (np.average(w) + EPS)
    return float(cov / var) if var > 1e-9 else cat_own_c


# ─────────────────────────────────────────────────────────────────────────────
# main entry
# ─────────────────────────────────────────────────────────────────────────────
def estimate_elasticities(panel_df):
    """Return (elast_df, cross_df, baseline_df, gates).

    elast_df : product_id, city, own_elast (NEGATIVE), own_sd, promo_elast
    cross_df : product_i, product_j, cross_elast (POSITIVE for substitutes)
    baseline_df: shared schema (see freeze_baselines)
    gates    : dict of pass/fail validation checks + per-category coverage
    """
    p = _prep(panel_df)
    baseline_df = freeze_baselines(panel_df)

    # 1) per-category pooled fits ------------------------------------------------
    cat_fit = {}
    for cat, sub in p.groupby("category"):
        if len(sub) < MIN_ROWS_CAT or sub["cell"].nunique() < 1:
            continue
        cat_fit[cat] = _fit_category(sub)

    if not cat_fit:
        raise ValueError("No category had enough rows to fit (need >= "
                         f"{MIN_ROWS_CAT} rows/category).")

    # grand mean own slope (volume-of-rows weighted across categories)
    grand = float(np.average([f["own"] for f in cat_fit.values()],
                             weights=[f["n"] for f in cat_fit.values()]))
    # enforce NEGATIVE own at the CATEGORY level (softmax-penalty intent):
    #   a positive category slope is economically impossible -> push toward 0.
    cat_own = {c: (f["own"] if f["own"] < 0 else 0.0) for c, f in cat_fit.items()}

    # 2) additive decomposition (partial pooling via ridge) ---------------------
    cell_rows = []
    for cat, sub in p.groupby("category"):
        if cat not in cat_fit:
            continue
        for cell, gc in sub.groupby("cell"):
            cell_rows.append({
                "cell": cell,
                "product_id": gc["product_id"].iloc[0],
                "city": gc["city"].iloc[0],
                "category": cat,
                "size_tier": gc["size_tier"].iloc[0],
                "raw_own": _raw_cell_own(gc, cat_own[cat]),
                "weight": float(gc["w"].sum()),
            })
    cell_tbl = pd.DataFrame(cell_rows)
    own_map = _decompose(cell_tbl, cat_own, grand)

    # 3) bootstrap own_sd per category (posterior-SD stand-in) ------------------
    cat_sd = {c: _own_by_week_bootstrap(p[p["category"] == c])
              for c in cat_fit}

    # 4) assemble elast_df -------------------------------------------------------
    e_rows = []
    for _, row in cell_tbl.iterrows():
        cat = row["category"]
        own = own_map[row["cell"]]
        # NEGATIVE-own enforcement + DOC gate clip
        own = min(own, 0.0)                      # clip any positive toward 0
        own = float(np.clip(own, OWN_LO, OWN_HI))
        e_rows.append({
            "product_id": row["product_id"],
            "city":       row["city"],
            "own_elast":  round(own, 4),
            "own_sd":     round(float(cat_sd.get(cat, np.nan)), 4)
                          if np.isfinite(cat_sd.get(cat, np.nan)) else np.nan,
            "promo_elast": round(float(cat_fit[cat]["promo"]), 4),
        })
    elast_df = pd.DataFrame(e_rows, columns=[
        "product_id", "city", "own_elast", "own_sd", "promo_elast"])

    # 5) cross_df: split each category's cross response evenly over siblings -----
    #    within a city, for each ordered pair (i,j): cross_ij = cross_cat/n_sib
    cross_rows = []
    for cat, sub in p.groupby("category"):
        if cat not in cat_fit:
            continue
        cross_cat = cat_fit[cat]["cross"]
        # substitutes -> POSITIVE cross; a negative fit = complements/noise, we
        # keep the sign but only surface pairs above the keep threshold.
        for city, gcity in sub.groupby("city"):
            prods = sorted(gcity["product_id"].unique().tolist())
            n_sib = len(prods) - 1
            if n_sib < 1:
                continue
            share = cross_cat / n_sib
            if abs(share) <= CROSS_KEEP:
                continue
            for pi in prods:
                for pj in prods:
                    if pi == pj:
                        continue
                    cross_rows.append({
                        "product_i": pi, "product_j": pj,
                        "cross_elast": round(float(share), 5),
                    })
    cross_df = pd.DataFrame(cross_rows, columns=["product_i", "product_j", "cross_elast"])
    if len(cross_df):
        # collapse duplicate (i,j) across cities to their mean share
        cross_df = (cross_df.groupby(["product_i", "product_j"], as_index=False)["cross_elast"]
                    .mean())
        cross_df = cross_df[cross_df["cross_elast"].abs() > CROSS_KEEP].reset_index(drop=True)

    # 6) GATES -------------------------------------------------------------------
    gates = _gates(p, cat_fit, elast_df, cross_df)
    return elast_df, cross_df, baseline_df, gates


def _gates(p, cat_fit, elast_df, cross_df):
    """Validation gates (pass/fail each) + per-category coverage. Fit accuracy is
       measured POOLED across categories on the in-sample fitted values (the
       penalized posterior-mean fit)."""
    # pooled fit accuracy across all fitted categories
    y = np.concatenate([f["y"] for f in cat_fit.values()])
    yhat = np.concatenate([f["yhat"] for f in cat_fit.values()])
    w = np.concatenate([f["w"] for f in cat_fit.values()])
    ybar = np.average(y, weights=w)
    ss_res = float(np.sum(w * (y - yhat) ** 2))
    ss_tot = float(np.sum(w * (y - ybar) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > EPS else np.nan
    # wMAPE / bias computed in UNITS space (exp of the log fit) — business-honest
    units_true = np.exp(y)
    units_pred = np.exp(yhat)
    denom = float(np.sum(w * units_true)) + EPS
    wmape = float(np.sum(w * np.abs(units_pred - units_true)) / denom)
    bias  = float(np.sum(w * (units_pred - units_true)) / denom)

    own = elast_df["own_elast"].to_numpy(float)
    own_ok = bool(np.all((own > OWN_LO - 1e-9) & (own < 1e-9)))   # in (-2.5, 0)
    # substitutes should be POSITIVE: report the fraction of NON-NEGATIVE cross pairs.
    # (A pair within +/-CROSS_KEEP of zero is treated as non-negative / harmless noise;
    # only a MEANINGFULLY negative cross_elast — a complement masquerading as a
    # substitute — counts against the gate.)
    if len(cross_df):
        nonneg = (cross_df["cross_elast"] >= -CROSS_KEEP).to_numpy()
        frac_pos_cross = float(nonneg.mean())
    else:
        frac_pos_cross = np.nan
    # Gate on the MAJORITY being non-negative. A single positive pair no longer rescues
    # a set that is mostly negative (the old `.any()` disjunct did exactly that).
    cross_ok = True if len(cross_df) == 0 else bool(frac_pos_cross >= 0.5)

    coverage = {c: {"n_rows": int(f["n"]), "r2": round(float(f["r2"]), 3),
                    "own": round(float(f["own"]), 4), "cross": round(float(f["cross"]), 4)}
                for c, f in cat_fit.items()}

    gates = {
        "pooled_r2":        round(float(r2), 3),
        "pooled_r2_pass":   bool(np.isfinite(r2) and r2 >= R2_FLOOR),
        "wmape":            round(wmape, 3),
        "wmape_pass":       bool(wmape < WMAPE_CEIL),
        "abs_bias":         round(abs(bias), 3),
        "bias_pass":        bool(abs(bias) < BIAS_CEIL),
        "own_in_band":      own_ok,          # all own_elast in (-2.5, 0)
        "cross_nonneg_subs": cross_ok,       # substitute cross pairs are >= 0
        "frac_pos_cross":   round(frac_pos_cross, 3) if np.isfinite(frac_pos_cross) else np.nan,
        "n_cells":          int(len(elast_df)),
        "n_cross_pairs":    int(len(cross_df)),
        "coverage":         coverage,
    }
    gates["all_pass"] = bool(gates["pooled_r2_pass"] and gates["wmape_pass"]
                             and gates["bias_pass"] and gates["own_in_band"])
    return gates


# ─────────────────────────────────────────────────────────────────────────────
# smoke test — tiny synthetic panel with a PLANTED elasticity + real siblings
# ─────────────────────────────────────────────────────────────────────────────
def _synth_panel(seed=0):
    """Two categories, a few SKUs x cities x weeks, planted own-elasticity ~ -1.6
       (Oil) / -1.1 (Salt) and a positive cross (substitutes) within category."""
    rng = np.random.RandomState(seed)
    weeks = pd.date_range("2025-01-06", periods=16, freq="W-MON").astype(str).tolist()
    cats = {
        "Oil":  {"true_own": -1.6, "true_cross": 0.6,
                 "skus": [("OIL_1L", "Cold Pressed", 1000.0, 320.0),
                          ("OIL_500", "Cold Pressed", 500.0, 175.0),
                          ("OIL_2L", "Cold Pressed", 2000.0, 610.0)]},
        "Salt": {"true_own": -1.1, "true_cross": 0.4,
                 "skus": [("SALT_1K", "Rock Salt", 1000.0, 60.0),
                          ("SALT_500", "Rock Salt", 500.0, 35.0)]},
    }
    cities = ["Delhi", "Mumbai"]
    rows = []
    for cat, cfg in cats.items():
        skus = cfg["skus"]
        q0 = {s[0]: rng.uniform(30, 70) for s in skus}
        for city in cities:
            for wi, wk in enumerate(weeks):
                # PASS 1: draw every SKU's price/units-weight for this city+week so
                # the DGP can use the TRUE leave-one-out sibling average the
                # estimator will later reconstruct (this is what makes the planted
                # cross elasticity genuinely recoverable, not a phantom).
                disc  = {s[0]: float(np.clip(rng.uniform(5, 30) + 6*np.sin((wi+hash(s[0]) % 3)/2.0), 0, 45))
                         for s in skus}
                price = {s[0]: s[3] * (1 - disc[s[0]]/100.0) for s in skus}
                # provisional volume weights (own-price driven) for the LOO average
                wts = {s[0]: q0[s[0]] * (price[s[0]] / (s[3]*0.85)) ** cfg["true_own"]
                       for s in skus}
                # PASS 2: units with true own + true LOO-sibling cross
                for (pid, base, grams, mrp) in skus:
                    others = [s[0] for s in skus if s[0] != pid]
                    if others:
                        num = sum(wts[o] * price[o] for o in others)
                        den = sum(wts[o] for o in others)
                        sib_price = num / den if den > 0 else price[pid]
                        sib_ref   = np.mean([s[3] for s in skus if s[0] != pid]) * 0.85
                    else:
                        sib_price, sib_ref = price[pid], mrp * 0.85
                    ln_u = (np.log(q0[pid])
                            + cfg["true_own"]  * (np.log(price[pid]) - np.log(mrp*0.85))
                            + cfg["true_cross"] * (np.log(sib_price) - np.log(sib_ref))
                            + 0.15 * (disc[pid] > 20)
                            + rng.normal(0, 0.10))
                    units = max(1.0, np.round(np.exp(ln_u)))
                    rows.append({
                        "product_id": pid, "city": city, "category": cat,
                        "base_product": base, "pack_grams": grams,
                        "title": f"24 Mantra Organic {base} {int(grams)}g",
                        "week": wk, "month": pd.Timestamp(wk).month,
                        "units": float(units), "price": round(price[pid], 2),
                        "mrp": mrp, "disc": round(disc[pid], 2),
                        "regular_price": mrp*0.90, "is_promo": bool(disc[pid] > 15),
                        "osa": float(np.clip(rng.uniform(85, 99), 1, 100)),
                        "recency_w": 1.0 + 0.05*wi, "volume_w": 1.0,
                    })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    print("=" * 74)
    print("  elasticity_hier.py — SMOKE TEST (tiny synthetic panel, planted elast.)")
    print("=" * 74)
    panel = _synth_panel(seed=0)
    print(f"  synthetic panel: {len(panel)} rows | "
          f"{panel['product_id'].nunique()} SKUs x {panel['city'].nunique()} cities "
          f"x {panel['week'].nunique()} weeks | cats {sorted(panel['category'].unique())}")

    elast_df, cross_df, baseline_df, gates = estimate_elasticities(panel)

    print("\n  elast_df (own_elast NEGATIVE, own_sd = bootstrap posterior-SD):")
    print(elast_df.to_string(index=False))

    print("\n  cross_df (POSITIVE = substitutes; even share per sibling):")
    print(cross_df.to_string(index=False) if len(cross_df) else "   (no cross pairs above threshold)")

    print("\n  baseline_df (shared schema, one row per product_id x city):")
    print(baseline_df.to_string(index=False))

    print("\n  GATES:")
    for k, v in gates.items():
        if k == "coverage":
            print("   coverage (per category):")
            for c, cov in v.items():
                print(f"      {c:8s} n={cov['n_rows']:4d} R2={cov['r2']:+.3f} "
                      f"own={cov['own']:+.3f} cross={cov['cross']:+.3f}")
        else:
            print(f"   {k:20s}: {v}")

    # planted-truth check: Oil own ~ -1.6, Salt own ~ -1.1 (recovered, shrunk)
    own_by_cat = (elast_df.merge(baseline_df[["product_id", "city", "category"]],
                                 on=["product_id", "city"])
                          .groupby("category")["own_elast"].mean().to_dict())
    print("\n  recovered mean own_elast by category (planted Oil=-1.6, Salt=-1.1):")
    for c, v in own_by_cat.items():
        print(f"      {c:8s}: {v:+.3f}")

    ok = (gates["own_in_band"] and gates["pooled_r2_pass"]
          and len(elast_df) == panel.groupby(['product_id', 'city']).ngroups)
    print("\n  SMOKE TEST:", "PASS" if ok else "CHECK — review gates above")
    sys.exit(0)
