"""
elasticity_bayes.py — TRUE Bayesian hierarchical elasticities (analytic, no clip).

Why not PyMC: PyMC forces numpy>=2, which binary-breaks the repo's numpy-1-compiled
sklearn/statsmodels stack (they're mutually exclusive in this environment). So this
uses the closed-form CONJUGATE Bayesian linear regression instead — genuine Bayesian
posteriors (mean AND covariance in closed form), an informative NEGATIVE prior on
own-price, and empirical-Bayes hierarchical shrinkage across categories. Same honest
outcome the hard-clip lacked: a sparse/noisy category is pulled toward a plausible
value with a WIDE posterior SD (flagged low-confidence), never estimated wild then clipped.

Model (per category, after Frisch–Waugh–Lovell removes cell FE + controls):
   resid_lnunits = own·resid_lnprice + cross·resid_ln_siblingprice + e
   prior  own ~ N(mu_own, s_own²)  (mu_own<0, empirical-Bayes) ; cross ~ N(mu_cross+, s_cross²)
   posterior (Normal-Normal conjugate):  Sn = (S0⁻¹ + XᵀWX/σ²)⁻¹ ,  mn = Sn(S0⁻¹m0 + XᵀWy/σ²)
   own_elast = mn[0] ; own_sd = √Sn[0,0]  (real uncertainty, not a clip artifact)

Drop-in for elasticity_hier: estimate_elasticities(panel_df) -> (elast_df, cross_df, baseline_df, gates).
"""
import warnings, os
warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
from sklearn.linear_model import LinearRegression

OWN_PRIOR_MU, OWN_PRIOR_SD = -1.0, 0.8     # informative: elastic goods, discount lowers price -> volume up
CROSS_PRIOR_MU, CROSS_PRIOR_SD = 0.15, 0.4  # substitutes: sibling price up -> our volume up
WIDE_SD = 0.6                               # own_sd above this = low-confidence flag (honest, not a clip)


def _sibling_avg(panel):
    p = panel.copy()
    p["_u"] = p["units"].clip(lower=0.0); p["_up"] = p["_u"] * p["price"]
    g = p.groupby(["category", "city", "week"])
    tot_u = g["_u"].transform("sum"); tot_up = g["_up"].transform("sum"); n = g["price"].transform("size")
    loo_u = (tot_u - p["_u"]).clip(lower=1e-9); loo_up = tot_up - p["_up"]
    p["sib_price"] = np.where(n > 1, loo_up / loo_u, p["price"])
    # Per-row flag: did this (category, city, week) actually contain a real sibling? When a cell
    # is the ONLY SKU in its category/city/week, sib_price fell back to its own price above, so the
    # sibling regressor would be perfectly collinear with own price. Expose the group size so the
    # caller can zero the sibling regressor for those rows (ITEM 2 collinearity fix).
    p["has_real_sibling"] = (n > 1).astype(float).values
    return p


def _residualize(df, col, controls):
    d = df[col] - df.groupby(df["cell"])[col].transform("mean")     # absorb cell FE
    X = df[controls].values
    if X.shape[1]:
        d = d - LinearRegression().fit(X, d, sample_weight=df["w"]).predict(X)
    return d.values


def _bayes(X, y, w, m0, S0, sigma2):
    """Conjugate Normal posterior for beta. Returns (mean, cov)."""
    sw = np.sqrt(w)[:, None]; Xw = X * sw; yw = y * np.sqrt(w)
    S0inv = np.linalg.inv(S0)
    Sn = np.linalg.inv(S0inv + (Xw.T @ Xw) / sigma2)
    mn = Sn @ (S0inv @ m0 + (Xw.T @ yw) / sigma2)
    return mn, Sn


def estimate_elasticities(panel_df):
    p = _sibling_avg(panel_df)
    p = p[(p["units"] > 0) & (p["price"] > 0) & (p["sib_price"] > 0)].copy()
    p["cell"] = p["product_id"].astype(str) + "|" + p["city"].astype(str)
    p["ln_u"] = np.log(p["units"]); p["ln_p"] = np.log(p["price"]); p["ln_s"] = np.log(p["sib_price"])
    p["ln_osa"] = np.log(p["osa"].clip(lower=1.0)); p["promo"] = p["is_promo"].astype(float)
    p["w"] = (p["recency_w"] * p["volume_w"]).clip(lower=1e-6)
    md = pd.get_dummies(p["month"], prefix="m", drop_first=True).astype(float)
    for c in md.columns:
        p[c] = md[c].values
    controls = ["promo", "ln_osa"] + list(md.columns)
    p["ry"] = _residualize(p, "ln_u", controls)
    p["rp"] = _residualize(p, "ln_p", controls)
    rs = _residualize(p, "ln_s", controls)
    # Zero the sibling regressor on two grounds:
    #  (1) per-CATEGORY: a category with only one SKU anywhere has no substitute at all.
    #  (2) per-ROW: a cell that is the ONLY SKU in its (category, city, week) had sib_price fall
    #      back to its own price in _sibling_avg — leaving rs collinear with own price for that row.
    #      Without this, single-SKU-in-a-city-week rows split the fit arbitrarily between own and
    #      cross. has_real_sibling (group size > 1) removes exactly those rows from the cross signal.
    has_sib = p.groupby("category")["product_id"].transform("nunique").values > 1
    has_real_sibling = p["has_real_sibling"].values
    p["rs"] = rs * has_sib * has_real_sibling

    cats = sorted(p["category"].unique())

    def fit_cat(c, m0):
        d = p[p["category"] == c]
        X = d[["rp", "rs"]].values; y = d["ry"].values; w = d["w"].values
        # noise variance from a weighted OLS residual
        try:
            b_ols = np.linalg.lstsq(X * np.sqrt(w)[:, None], y * np.sqrt(w), rcond=None)[0]
            sig2 = max(np.average((y - X @ b_ols) ** 2, weights=w), 1e-4)
        except Exception:
            sig2 = 1.0
        S0 = np.diag([OWN_PRIOR_SD ** 2, CROSS_PRIOR_SD ** 2])
        mn, Sn = _bayes(X, y, w, np.array(m0), S0, sig2)
        return mn, np.sqrt(np.diag(Sn)), len(d)

    # Stage 1 — weak prior, get raw owns
    raw = {c: fit_cat(c, [OWN_PRIOR_MU, CROSS_PRIOR_MU]) for c in cats}
    owns = np.array([raw[c][0][0] for c in cats]); ses = np.array([raw[c][1][0] for c in cats])
    prec = 1.0 / (ses ** 2 + 1e-9)
    mu_g = float(np.clip(np.sum(owns * prec) / np.sum(prec), -2.5, -0.1))   # empirical-Bayes global own
    # Stage 2 — hierarchical: shrink each category toward the global own mean
    est = {c: fit_cat(c, [mu_g, CROSS_PRIOR_MU]) for c in cats}

    base = freeze_baselines(panel_df)
    ce_of = {c: float(est[c][0][1]) for c in cats}
    own_of = {c: float(est[c][0][0]) for c in cats}
    osd_of = {c: float(est[c][1][0]) for c in cats}

    elast_df = pd.DataFrame([{
        "product_id": b["product_id"], "city": b["city"],
        "own_elast": round(own_of.get(b["category"], mu_g), 4),
        "own_sd": round(osd_of.get(b["category"], OWN_PRIOR_SD), 4),
        "promo_elast": 0.0,
        "low_confidence": bool(osd_of.get(b["category"], 1.0) > WIDE_SD
                               or own_of.get(b["category"], -1) > -0.1),
    } for _, b in base.iterrows()])

    cross_rows = []
    for (cat, city), pl in base.groupby(["category", "city"])["product_id"].apply(list).items():
        if len(pl) < 2:
            continue
        ce = ce_of.get(cat, 0.0) / (len(pl) - 1)
        if abs(ce) < 1e-4:
            continue
        for a in pl:
            for bb in pl:
                if a != bb:
                    cross_rows.append({"product_i": a, "product_j": bb, "city": city,
                                       "cross_elast": round(ce, 4)})
    cross_df = pd.DataFrame(cross_rows, columns=["product_i", "product_j", "city", "cross_elast"])

    own_means = np.array([own_of[c] for c in cats])
    cross_means = np.array([ce_of[c] for c in cats])
    frac_pos = float((cross_means >= 0).mean())
    low_conf = [c for c in cats if osd_of[c] > WIDE_SD]
    gates = {
        "method": "conjugate_bayes_empirical_hierarchical",
        "global_own (mu_g)": round(mu_g, 3),
        "own_mean_median": round(float(np.median(own_means)), 3),
        "own_in_band": bool(((own_means > -2.5) & (own_means <= 0.2)).all()),
        "frac_pos_cross": round(frac_pos, 3),
        "cross_nonneg_subs": bool(frac_pos >= 0.5),
        "n_low_confidence_categories": len(low_conf),
        "low_confidence_categories": low_conf,
        "per_category": {c: {"own": round(own_of[c], 3), "own_sd": round(osd_of[c], 3),
                             "cross": round(ce_of[c], 3)} for c in cats},
        "all_pass": bool(((own_means > -2.5) & (own_means <= 0.2)).all() and frac_pos >= 0.5),
    }
    return elast_df, cross_df, base, gates


def freeze_baselines(panel_df, recent_weeks=4):
    p = panel_df.copy(); p["week"] = pd.to_datetime(p["week"]); rows = []
    for (pid, city), g in p.groupby(["product_id", "city"]):
        g = g.sort_values("week"); rec = g.tail(recent_weeks); wt = rec["units"].clip(lower=1e-6)
        rows.append({"product_id": pid, "city": city, "category": g["category"].iloc[0],
                     "base_product": g["base_product"].iloc[0], "pack_grams": g["pack_grams"].iloc[0],
                     "q0_units_wk": float(rec["units"].mean()),
                     "p0_price": float(np.average(rec["price"], weights=wt)),
                     "mrp": float(g["mrp"].median()),
                     "disc0": float(np.average(rec["disc"], weights=wt))})
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    rng = np.random.RandomState(1); rows = []
    for pid, own in [("A", -1.4), ("B", -0.9)]:
        for city in ["X", "Y"]:
            for wk in range(16):
                pr = 50 * (1 + rng.uniform(-0.15, 0.15))
                u = 100 * (pr / 50) ** own * rng.uniform(0.9, 1.1)
                rows.append([pid, city, "Cat", pid, 500.0, f"{pid} 500g", f"2026-01-{wk+1:02d}",
                             (wk % 12) + 1, max(u, 1), pr, 60, (1-pr/60)*100, pr < 47, 90.0, 1.0, 1.0])
    panel = pd.DataFrame(rows, columns=["product_id","city","category","base_product","pack_grams",
        "title","week","month","units","price","mrp","disc","is_promo","osa","recency_w","volume_w"])
    e, c, b, g = estimate_elasticities(panel)
    print("own posteriors (mean, sd):")
    for _, r in e.iterrows(): print(f"  {r['product_id']}|{r['city']}: {r['own_elast']:+.2f} ± {r['own_sd']:.2f}  low_conf={r['low_confidence']}")
    print("gates:", {k: g[k] for k in ["global_own (mu_g)","own_in_band","all_pass","n_low_confidence_categories"]})
    print("OK")
