"""
cross_price_v2.py — CHALLENGER: paper-faithful cross-price decomposition, full
elasticity matrix E, and competitor price-follow elasticity.
(PepsiCo PricingAI parity items price_06 + price_12 + price_18.)

CHAMPION/CHALLENGER DISCIPLINE
------------------------------
Nothing here edits or overwrites the validated champion outputs. This module
READS the same inputs the champion pricing run reads and WRITES new side-by-side
files. Consumers (pricing_engine / whatif / de_optimizer) keep reading
cross_price.csv; they switch to cross_price_v2.csv only via an explicit future
config flag — never automatically.

WHAT EACH PAPER ITEM BECOMES HERE
---------------------------------
price_06 — MULTIPLICATIVE cross decomposition (paper §2.2.2). The champion
  splits each category's total cross response UNIFORMLY over siblings:
  cross_ij = cross_cat / (n-1). The challenger replaces the uniform split with
  cross_ij = cross_cat * w_ij(theta), where w_ij is a row-normalized product of
  three similarity factors, each a damping multiplier bounded in (0,1):

     w_raw_ij = m_family^[base_product_i != base_product_j]
              * m_size  ^[size_bucket_i  != size_bucket_j]
              * m_ppk   ^[ppk_tier_i     != ppk_tier_j]
     w_ij     = w_raw_ij / sum_k(w_raw_ik)          (k over i's siblings)

  EXACT FACTOR FORMULAS (documented per spec):
   - family factor: base_product = TITLE with the size token stripped
     (pricing_panel.strip_size_token). Same family -> multiplier 1; different
     family -> multiplier m_family in (0,1).
   - size factor: two pack_grams are the SAME size bucket iff
     min(g_i,g_j)/max(g_i,g_j) in [0.75, 1.0]  (i.e. ratio band 0.75–1.333:
     450g vs 500g = same, 500g vs 1kg = different). NaN pack_grams counts as
     DIFFERENT (conservative damping). Different bucket -> multiplier m_size.
   - price-per-kg tier factor: ppk_i = p0_price / (pack_grams/1000). Same tier
     iff ppk ratio in [0.80, 1.25]. NaN -> different. Different -> m_ppk.
  Because sum_j w_ij = 1 per SKU, the challenger PRESERVES the champion's
  validated per-SKU total substitution mass exactly — it is a pure re-weighting.
  The three m's are estimated by weighted least squares on the residualized
  panel (same FWL residualization as elasticity_bayes: cell FE + promo + ln OSA
  + month dummies, weights = recency_w * volume_w), grid + local refinement,
  train/holdout split by week. Uniform split is NESTED at m=(1,1,1), so the
  holdout comparison vs the incumbent is apples-to-apples.

price_12 — full matrix E for the portfolio (long format, per city):
   - own diagonal  : production own_elast per SKU x city (elasticities.csv /
     estimate_elasticities — REUSED, never re-estimated).
   - within-category off-diagonals: the decomposed cross_ij above.
   - ACROSS categories: ZERO, structurally. Stated honestly: the codebase has
     never estimated cross-category interactions for this brand, and this
     challenger does not invent them. Absent (i,j) pairs in cross_price_v2.csv
     ARE those structural zeros.
   - competitor interaction: one pseudo-column 'COMP::<category>' per category
     holding the gated competitor-price elasticity from price_18.

price_18 — competitor price-follow elasticity per category. DATA REALITY
  (stated honestly): the fact table's per-SKU 'Competitor Price' column is
  100% EMPTY (0 of ~112k rows), so SKU-level RPI does not exist. RPI is built
  from DISCOUNT_PLAN/competitor_features.csv (RCA category x city x iso-week
  comp_median_price):  RPI_it = own_price_it / comp_median_price(cat,city,wk).
  TWO specs are run per category, both with the SAME confounder controls the
  codebase uses (cell FE, promo, ln OSA, month dummies, recency*volume
  weights; statsmodels WLS, HC1 robust SEs):
   - Spec A (the paper-brief form, REPORTED ONLY): resid ln(units) ~ resid
     ln(RPI). Measured finding on this data: because ln RPI = ln p_own -
     ln p_comp, this spec forces comp elasticity = -own elasticity, so the
     own-price response (~-1) contaminates it — it reads "significant" even
     when rivals do nothing. Reported for transparency, NEVER used in E.
   - Spec B (PRIMARY for E): resid ln(units) ~ resid ln(p_own) + resid
     ln(p_comp). comp elasticity = coef on ln(p_comp).
  GATE for entering E: Spec-B p < 0.05 AND comp_elast > +0.02 (substitutes
  sanity: rival price UP must not push our units DOWN; a significant negative
  is flagged implausible and zeroed). If everything gates to 0, SAY SO — that
  matches the challenger.py finding that competition barely moves this brand.

INPUTS (all existing, read-only)
--------------------------------
 - newest output/runs/2026*/fact_table.csv  (same glob as pricing_engine.py:101)
 - scripts/pricing/pricing_panel.build_pricing_panel  (weekly SKU x city panel)
 - elasticity_bayes (fallback elasticity_hier).estimate_elasticities — champion
   own elasticities + per-category total cross mass
 - DISCOUNT_PLAN/competitor_features.csv (category x city x iso-week)

OUTPUTS -> DISCOUNT_PLAN/pricing/
---------------------------------
 - cross_price_v2.csv        : long-format E. Columns: entity_i, entity_j,
   city, elast, block in {own_diag, within_cat_cross, comp_col}, w_ij,
   cross_uniform (champion's per-pair value, for diffing). Pairs not listed
   are structural zeros (cross-category).
 - cross_price_v2_components.json : m_family/m_size/m_ppk, train/holdout SSE
   challenger vs uniform, acceptance gates, competitor regression table.
 - CROSS_PRICE_V2.md         : business-readable verdict + matrix stats.

HOW TO READ THE VERDICT
-----------------------
ADOPT-READY means: signs unchanged vs champion (guaranteed by construction,
w_ij > 0), own diagonals all negative, and holdout SSE did not degrade vs the
uniform split. It does NOT mean the challenger is wired in — that stays a
deliberate one-line config change for a future run.

RUN:  python -X utf8 scripts/pricing/cross_price_v2.py [--holdout-frac 0.2]
      python -X utf8 scripts/pricing/cross_price_v2.py --selftest
"""
import os, sys, glob, json, argparse
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import pricing_panel as pp
try:
    import elasticity_bayes as eh          # champion elasticity source (bayes)
    ELAST_METHOD = "bayes"
except Exception:                          # pragma: no cover
    import elasticity_hier as eh
    ELAST_METHOD = "hier"

OUT_DEFAULT = os.path.join(ROOT, "DISCOUNT_PLAN", "pricing")
COMP_FEATURES = os.path.join(ROOT, "DISCOUNT_PLAN", "competitor_features.csv")

# ── documented constants ─────────────────────────────────────────────────────
SIZE_SAME_LO = 0.75      # pack-gram ratio (min/max) >= 0.75  -> same size bucket
PPK_SAME_LO = 0.80       # price-per-kg ratio (min/max) >= 0.80 -> same ppk tier
HOLDOUT_FRAC = 0.20      # last 20% of weeks held out for the acceptance gate
COARSE_GRID = (0.10, 0.30, 0.50, 0.70, 0.90)   # per-factor damping candidates
REFINE_STEP = 0.05       # local refinement half-grid around the coarse best
MIN_M, MAX_M = 0.02, 1.0 # factors live in (0,1]; 1.0 = "attribute irrelevant"
COMP_MIN_SKUS = 3        # need >=3 rival SKUs behind comp_median_price
COMP_MIN_ROWS = 30       # min residualized rows per category for the regression
COMP_P_GATE = 0.05       # significance gate for using the comp elasticity in E
COMP_MIN_ABS = 0.02      # materiality gate (same spirit as challenger.py)
CROSS_EPS = 1e-4         # |cross_cat| below this = no cross mass (champion rule)


def _clean_pid(v):
    """Same id hygiene as pricing_engine._clean_pid: never emit '532393.0'."""
    if v is None:
        return ""
    if isinstance(v, float):
        return str(int(v)) if float(v).is_integer() else str(v)
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        return s[:-2]
    return s


def _latest_fact_table():
    """Newest output/runs/2026*/fact_table.csv (pattern from pricing_engine.py:101)."""
    for r in sorted(glob.glob(os.path.join(ROOT, "output", "runs", "2026*")), reverse=True):
        f = os.path.join(r, "fact_table.csv")
        if os.path.exists(f):
            return f, r
    raise SystemExit("no fact_table.csv — run pipeline.py first")


def _per_category_fit(gates):
    """{category: (own, cross_total)} from either elasticity module's gates."""
    per = gates.get("per_category") or gates.get("coverage") or {}
    return {c: (float(d.get("own", np.nan)), float(d.get("cross", np.nan)))
            for c, d in per.items()}


# ─────────────────────────────────────────────────────────────────────────────
# Residualization (mirrors elasticity_bayes: cell FE + promo/ln_osa/month, WLS)
# ─────────────────────────────────────────────────────────────────────────────
def _prep_frame(panel):
    """Estimation frame: logs, controls, weights. Rows with units/price > 0 only."""
    from sklearn.linear_model import LinearRegression  # noqa: F401 (used below)
    p = panel.copy()
    p = p[(pd.to_numeric(p["units"], errors="coerce") > 0)
          & (pd.to_numeric(p["price"], errors="coerce") > 0)].copy()
    p["cell"] = p["product_id"].astype(str) + "|" + p["city"].astype(str)
    p["ln_u"] = np.log(p["units"].astype(float))
    p["ln_p"] = np.log(p["price"].astype(float))
    p["ln_osa"] = np.log(pd.to_numeric(p["osa"], errors="coerce").clip(lower=1.0).fillna(1.0))
    p["promo"] = p["is_promo"].astype(float)
    p["w"] = (p["recency_w"] * p["volume_w"]).clip(lower=1e-6)
    md = pd.get_dummies(p["month"], prefix="m", drop_first=True).astype(float)
    for c in md.columns:
        p[c] = md[c].values
    p["_controls"] = ""  # marker only
    p.attrs["controls"] = ["promo", "ln_osa"] + list(md.columns)
    return p.reset_index(drop=True)


def _residualize(df, values, controls, w):
    """FWL: demean per cell (absorb FE) then partial out controls by WLS.
    `values` is a Series aligned to df.index. Same recipe as elasticity_bayes."""
    from sklearn.linear_model import LinearRegression
    d = values - values.groupby(df["cell"]).transform("mean")
    X = df[controls].values
    if X.shape[1]:
        d = d - LinearRegression().fit(X, d, sample_weight=w).predict(X)
    return d.to_numpy(dtype=float)


# ─────────────────────────────────────────────────────────────────────────────
# price_06 — multiplicative pair weights + theta estimation
# ─────────────────────────────────────────────────────────────────────────────
def _mismatch_masks(fam, grams, ppk):
    """Boolean n x n mismatch matrices (diag irrelevant) for the three factors."""
    fam = np.asarray(fam, dtype=object)
    g = np.asarray(grams, dtype=float)
    k = np.asarray(ppk, dtype=float)
    fam_mis = fam[:, None] != fam[None, :]
    with np.errstate(invalid="ignore", divide="ignore"):
        gr = np.minimum(g[:, None], g[None, :]) / np.maximum(g[:, None], g[None, :])
        kr = np.minimum(k[:, None], k[None, :]) / np.maximum(k[:, None], k[None, :])
    size_mis = ~(gr >= SIZE_SAME_LO)   # NaN ratio -> True (different) — conservative
    ppk_mis = ~(kr >= PPK_SAME_LO)
    return fam_mis, size_mis, ppk_mis


def _pair_weight_matrix(fam_mis, size_mis, ppk_mis, m_family, m_size, m_ppk):
    """Unnormalized w_raw: product of damping factors; diagonal zeroed."""
    W = (np.where(fam_mis, m_family, 1.0)
         * np.where(size_mis, m_size, 1.0)
         * np.where(ppk_mis, m_ppk, 1.0))
    np.fill_diagonal(W, 0.0)
    return W


def _build_groups(frame):
    """Per (category, city) group with >=2 SKUs: price matrix + row lookup.

    Returns list of dicts with: category, skus, attrs (fam/grams/ppk),
    LNP (T x n, NaN absent), PRES (T x n bool), rows (frame row idx),
    row_pos (week-idx, sku-idx per frame row)."""
    groups = []
    for (cat, city), g in frame.groupby(["category", "city"], sort=True):
        skus = sorted(g["product_id"].unique().tolist())
        if len(skus) < 2:
            continue
        weeks = sorted(g["week"].unique().tolist())
        s_ix = {s: i for i, s in enumerate(skus)}
        w_ix = {w: i for i, w in enumerate(weeks)}
        LNP = np.full((len(weeks), len(skus)), np.nan)
        for wk, pid, lnp in zip(g["week"], g["product_id"], g["ln_p"]):
            LNP[w_ix[wk], s_ix[pid]] = lnp
        PRES = np.isfinite(LNP)
        # static attributes per SKU (mode within the group)
        fam, grams, ppk = [], [], []
        for s in skus:
            gs = g[g["product_id"] == s]
            fam.append(str(gs["base_product"].iloc[0]))
            gg = pd.to_numeric(gs["pack_grams"], errors="coerce").dropna()
            grams.append(float(gg.iloc[0]) if len(gg) else np.nan)
            pr = float(pd.to_numeric(gs["price"], errors="coerce").median())
            ppk.append(pr / (grams[-1] / 1000.0) if np.isfinite(grams[-1]) and grams[-1] > 0
                       else np.nan)
        rows = g.index.to_numpy()
        row_pos = np.array([[w_ix[wk], s_ix[pid]] for wk, pid in zip(g["week"], g["product_id"])])
        groups.append({"category": cat, "city": city, "skus": skus,
                       "masks": _mismatch_masks(fam, grams, ppk),
                       "LNP": LNP, "PRES": PRES, "rows": rows, "row_pos": row_pos})
    return groups


def _sib_index(groups, n_rows, m_family, m_size, m_ppk):
    """ln sibling-price index per frame row for the given thetas (NaN if the row's
    SKU has no sibling present that week)."""
    out = np.full(n_rows, np.nan)
    for gr in groups:
        W = _pair_weight_matrix(*gr["masks"], m_family, m_size, m_ppk)
        LNP0 = np.where(gr["PRES"], gr["LNP"], 0.0)
        P = gr["PRES"].astype(float)
        NUM = LNP0 @ W.T          # NUM[t,i] = sum_j w_ij * lnp_jt (present j)
        DEN = P @ W.T
        with np.errstate(invalid="ignore", divide="ignore"):
            SIB = np.where(DEN > 0, NUM / DEN, np.nan)
        wi, si = gr["row_pos"][:, 0], gr["row_pos"][:, 1]
        out[gr["rows"]] = SIB[wi, si]
    return out


def _evaluate_thetas(frame, groups, per_cat, resid2, train_mask, valid,
                     m_family, m_size, m_ppk):
    """Weighted SSE of resid2 ~ beta_cat * resid(ln_sib) — beta fit on TRAIN,
    SSE evaluated on train AND holdout with the TRAIN beta. Lower = better."""
    controls = frame.attrs["controls"]
    sib = _sib_index(groups, len(frame), m_family, m_size, m_ppk)
    sib_s = pd.Series(sib, index=frame.index)
    ok = valid & np.isfinite(sib)
    sub = frame[ok]
    r_sib = _residualize(sub, sib_s[ok], controls, sub["w"].values)
    y = resid2[ok]; w = sub["w"].values
    tr = train_mask[ok]; ho = ~tr
    sse_tr = sse_ho = 0.0
    betas = {}
    for cat, gidx in sub.groupby("category").indices.items():
        xt, yt, wt = r_sib[gidx], y[gidx], w[gidx]
        t = tr[gidx]
        denom = float(np.sum(wt[t] * xt[t] ** 2))
        beta = float(np.sum(wt[t] * xt[t] * yt[t]) / denom) if denom > 1e-12 else 0.0
        betas[cat] = beta
        e = yt - beta * xt
        sse_tr += float(np.sum(wt[t] * e[t] ** 2))
        sse_ho += float(np.sum(wt[~t] * e[~t] ** 2))
    return sse_tr, sse_ho, betas


def fit_decomposition(frame, groups, per_cat, holdout_frac=HOLDOUT_FRAC):
    """Estimate (m_family, m_size, m_ppk) by coarse grid + local refinement on
    the TRAIN weeks; compare vs the nested uniform split (m=1,1,1) on HOLDOUT.
    Returns a result dict (incl. graceful 'no signal' path on thin data)."""
    controls = frame.attrs["controls"]
    weeks = sorted(frame["week"].unique())
    n_ho = max(1, int(round(len(weeks) * holdout_frac)))
    ho_weeks = set(weeks[-n_ho:])
    train_mask = ~frame["week"].isin(ho_weeks).to_numpy()

    # rows usable for the cross signal: SKU has >=1 sibling present that week
    # (theta-independent: presence does not depend on m). Mirrors the
    # has_real_sibling discipline in elasticity_bayes.py:76-84.
    sib_any = _sib_index(groups, len(frame), 1.0, 1.0, 1.0)
    valid = np.isfinite(sib_any)
    n_valid = int(valid.sum())
    if n_valid < 50 or len(weeks) < 6:
        return {"estimated": False,
                "reason": f"thin data: {n_valid} sibling rows / {len(weeks)} weeks",
                "m_family": 1.0, "m_size": 1.0, "m_ppk": 1.0}

    # target: residual ln units net of the champion's own-price term
    ry = _residualize(frame, frame["ln_u"], controls, frame["w"].values)
    rp = _residualize(frame, frame["ln_p"], controls, frame["w"].values)
    own_by_cat = frame["category"].map(lambda c: per_cat.get(c, (np.nan, np.nan))[0])
    own_arr = own_by_cat.fillna(np.nanmedian(list(v[0] for v in per_cat.values()))
                                if per_cat else -1.0).to_numpy(dtype=float)
    resid2 = ry - own_arr * rp

    def ev(mf, ms, mk):
        return _evaluate_thetas(frame, groups, per_cat, resid2, train_mask, valid,
                                mf, ms, mk)

    # uniform benchmark (nested at m = 1,1,1)
    u_tr, u_ho, _ = ev(1.0, 1.0, 1.0)

    # coarse grid on TRAIN SSE
    best = (1.0, 1.0, 1.0); best_tr = u_tr
    n_evals = 1
    for mf in COARSE_GRID:
        for ms in COARSE_GRID:
            for mk in COARSE_GRID:
                tr, _, _ = ev(mf, ms, mk)
                n_evals += 1
                if tr < best_tr - 1e-12:
                    best_tr, best = tr, (mf, ms, mk)
    # local refinement around the coarse best
    def around(v):
        return sorted({min(max(v + d, MIN_M), MAX_M)
                       for d in (-2 * REFINE_STEP, -REFINE_STEP, 0.0,
                                 REFINE_STEP, 2 * REFINE_STEP)})
    for mf in around(best[0]):
        for ms in around(best[1]):
            for mk in around(best[2]):
                tr, _, _ = ev(mf, ms, mk)
                n_evals += 1
                if tr < best_tr - 1e-12:
                    best_tr, best = tr, (mf, ms, mk)

    c_tr, c_ho, betas = ev(*best)
    return {"estimated": True, "n_evals": n_evals,
            "m_family": best[0], "m_size": best[1], "m_ppk": best[2],
            "n_sibling_rows": n_valid, "n_weeks": len(weeks),
            "n_holdout_weeks": n_ho,
            "sse_train_uniform": u_tr, "sse_train_challenger": c_tr,
            "sse_holdout_uniform": u_ho, "sse_holdout_challenger": c_ho,
            "holdout_improvement_pct": (u_ho - c_ho) / u_ho * 100.0 if u_ho > 0 else 0.0,
            "refit_betas_by_category": {k: round(v, 4) for k, v in betas.items()}}


def decompose_cross(baseline, per_cat, m_family, m_size, m_ppk):
    """Per (category, city) sibling set (from the baseline, matching the champion's
    cross_price.csv coverage rule): cross_ij = cross_cat * w_ij. Also emits the
    champion's uniform value per pair for diffing. Skips |cross_cat| < CROSS_EPS
    and single-SKU groups — exactly like elasticity_bayes.py:124-134."""
    rows = []
    for (cat, city), g in baseline.groupby(["category", "city"]):
        pl = sorted(g["product_id"].unique().tolist())
        if len(pl) < 2:
            continue
        cross_cat = per_cat.get(cat, (np.nan, np.nan))[1]
        if not np.isfinite(cross_cat) or abs(cross_cat) < CROSS_EPS:
            continue
        gg = g.drop_duplicates("product_id").set_index("product_id")
        fam = [str(gg.loc[s, "base_product"]) for s in pl]
        grams = [float(gg.loc[s, "pack_grams"]) if np.isfinite(gg.loc[s, "pack_grams"])
                 else np.nan for s in pl]
        ppk = [float(gg.loc[s, "p0_price"]) / (gr / 1000.0)
               if np.isfinite(gr) and gr > 0 else np.nan
               for s, gr in zip(pl, grams)]
        W = _pair_weight_matrix(*_mismatch_masks(fam, grams, ppk),
                                m_family, m_size, m_ppk)
        rs = W.sum(axis=1)
        n = len(pl)
        for i, si in enumerate(pl):
            if rs[i] <= 0:
                continue
            for j, sj in enumerate(pl):
                if i == j:
                    continue
                w_ij = W[i, j] / rs[i]
                rows.append({"entity_i": _clean_pid(si), "entity_j": _clean_pid(sj),
                             "city": city, "category": cat,
                             "elast": round(cross_cat * w_ij, 5),
                             "w_ij": round(w_ij, 5),
                             "cross_uniform": round(cross_cat / (n - 1), 5),
                             "block": "within_cat_cross"})
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# price_18 — competitor price-follow elasticity per category (RPI regression)
# ─────────────────────────────────────────────────────────────────────────────
def competitor_elasticity(frame, comp_path=COMP_FEATURES):
    """Per category, two specs (see module docstring):
      Spec A (reported only): resid ln(units) ~ resid ln(RPI). Contaminated by
        own-price response by construction — never used in E.
      Spec B (primary):       resid ln(units) ~ resid ln(p_own) + resid ln(p_comp).
        comp_elast = coef on ln(p_comp); gate = p<0.05 AND comp_elast>+0.02
        (substitutes sign sanity).
    Returns (comp_df, note). comp_df columns: category, rpi_coef, rpi_p (Spec A),
    comp_elast, se, p_value (Spec B), comp_elast_used (gated), n_rows,
    n_city_weeks, gate_reason."""
    import statsmodels.api as sm
    cols = ["category", "rpi_coef", "rpi_p", "comp_elast", "se", "p_value",
            "comp_elast_used", "n_rows", "n_city_weeks", "gate_reason"]
    if not os.path.exists(comp_path):
        return pd.DataFrame(columns=cols), f"competitor_features.csv missing at {comp_path}"
    cf = pd.read_csv(comp_path).rename(columns={"Category": "category", "City": "city"})
    cf = cf[pd.to_numeric(cf["n_comp_skus"], errors="coerce") >= COMP_MIN_SKUS]
    cf["comp_median_price"] = pd.to_numeric(cf["comp_median_price"], errors="coerce")
    cf = cf.dropna(subset=["comp_median_price"])
    cf = cf[cf["comp_median_price"] > 0]

    p = frame.copy()
    wk = pd.to_datetime(p["week"]).dt.isocalendar()
    p["iso_week"] = wk["year"].astype(str) + "-W" + wk["week"].astype(int).map("{:02d}".format)
    p = p.merge(cf[["category", "city", "iso_week", "comp_median_price"]],
                on=["category", "city", "iso_week"], how="left")
    p = p[np.isfinite(pd.to_numeric(p["comp_median_price"], errors="coerce"))].copy()
    if not len(p):
        return pd.DataFrame(columns=cols), "no (category, city, week) overlap with competitor features"
    p["ln_comp"] = np.log(p["comp_median_price"].astype(float))
    p["ln_rpi"] = p["ln_p"] - p["ln_comp"]
    controls = frame.attrs["controls"]

    rows = []
    for cat, g in p.groupby("category"):
        if len(g) < COMP_MIN_ROWS or g["ln_comp"].nunique() < 5:
            rows.append({"category": cat, "rpi_coef": np.nan, "rpi_p": np.nan,
                         "comp_elast": np.nan, "se": np.nan, "p_value": np.nan,
                         "comp_elast_used": 0.0, "n_rows": int(len(g)),
                         "n_city_weeks": int(g.groupby(["city", "iso_week"]).ngroups),
                         "gate_reason": "thin data (<30 rows or <5 distinct comp prices)"})
            continue
        g = g.reset_index(drop=True)
        w = g["w"].values
        ry = _residualize(g, g["ln_u"], controls, w)
        rr = _residualize(g, g["ln_rpi"], controls, w)
        rp = _residualize(g, g["ln_p"], controls, w)
        rc = _residualize(g, g["ln_comp"], controls, w)
        try:  # Spec A — paper-brief RPI form (reported only; own-price contaminated)
            mA = sm.WLS(ry, sm.add_constant(rr), weights=w).fit(cov_type="HC1")
            bA, pA = float(mA.params[1]), float(mA.pvalues[1])
        except Exception:
            bA = pA = np.nan
        try:  # Spec B — own and comp prices free; comp coef is the honest measure
            X2 = sm.add_constant(np.column_stack([rp, rc]))
            mB = sm.WLS(ry, X2, weights=w).fit(cov_type="HC1")
            gamma, se, pv = float(mB.params[2]), float(mB.bse[2]), float(mB.pvalues[2])
        except Exception:
            gamma = se = pv = np.nan
        sig = bool(np.isfinite(pv) and pv < COMP_P_GATE)
        sane = bool(np.isfinite(gamma) and gamma > COMP_MIN_ABS)   # substitutes: > 0
        gated = sig and sane
        if gated:
            reason = "significant, material, sign-sane"
        elif sig and np.isfinite(gamma) and gamma < -COMP_MIN_ABS:
            reason = f"WRONG SIGN (gamma={gamma:+.2f}, p={pv:.3f}) — implausible, zeroed"
        elif np.isfinite(pv):
            reason = f"null signal (p={pv:.3f})"
        else:
            reason = "fit failed"
        rows.append({"category": cat,
                     "rpi_coef": round(bA, 4) if np.isfinite(bA) else np.nan,
                     "rpi_p": round(pA, 4) if np.isfinite(pA) else np.nan,
                     "comp_elast": round(gamma, 4) if np.isfinite(gamma) else np.nan,
                     "se": round(se, 4) if np.isfinite(se) else np.nan,
                     "p_value": round(pv, 4) if np.isfinite(pv) else np.nan,
                     "comp_elast_used": round(gamma, 4) if gated else 0.0,
                     "n_rows": int(len(g)),
                     "n_city_weeks": int(g.groupby(["city", "iso_week"]).ngroups),
                     "gate_reason": reason})
    return pd.DataFrame(rows, columns=cols), None


# ─────────────────────────────────────────────────────────────────────────────
# price_12 — assemble the full long-format matrix E
# ─────────────────────────────────────────────────────────────────────────────
def assemble_matrix(elast_df, cross_v2, comp_df, baseline):
    """Long-format E: own diagonal (production values, reused byte-for-byte),
    decomposed within-category cross, gated competitor columns. Cross-category
    own-brand pairs are STRUCTURAL ZEROS and are not written (documented)."""
    rows = []
    for _, r in elast_df.iterrows():
        rows.append({"entity_i": _clean_pid(r["product_id"]),
                     "entity_j": _clean_pid(r["product_id"]),
                     "city": r["city"], "category": "", "elast": r["own_elast"],
                     "w_ij": np.nan, "cross_uniform": np.nan, "block": "own_diag"})
    if len(cross_v2):
        rows.extend(cross_v2.to_dict("records"))
    used = {r["category"]: r["comp_elast_used"] for _, r in comp_df.iterrows()} \
        if len(comp_df) else {}
    for _, b in baseline.iterrows():
        cat = b["category"]
        if cat in used:
            rows.append({"entity_i": _clean_pid(b["product_id"]),
                         "entity_j": f"COMP::{cat}", "city": b["city"],
                         "category": cat, "elast": used[cat],
                         "w_ij": np.nan, "cross_uniform": np.nan, "block": "comp_col"})
    E = pd.DataFrame(rows, columns=["entity_i", "entity_j", "city", "category",
                                    "elast", "w_ij", "cross_uniform", "block"])
    n_sku = elast_df["product_id"].nunique()
    n_city = elast_df["city"].nunique()
    cells = int((elast_df.groupby("city")["product_id"].nunique() ** 2).sum())
    nz = int((E["block"] != "comp_col").sum())
    stats = {"n_skus": int(n_sku), "n_cities": int(n_city),
             "n_own_diag": int((E["block"] == "own_diag").sum()),
             "n_within_cat_cross": int((E["block"] == "within_cat_cross").sum()),
             "n_comp_cols": int((E["block"] == "comp_col").sum()),
             "own_brand_matrix_cells_all_cities": cells,
             "nonzero_own_brand_entries": nz,
             "fill_pct_own_brand": round(nz / cells * 100.0, 2) if cells else 0.0,
             "cross_category_entries": "structural zeros (never estimated — stated honestly)"}
    return E, stats


# ─────────────────────────────────────────────────────────────────────────────
# gates + report
# ─────────────────────────────────────────────────────────────────────────────
def acceptance_gates(elast_df, cross_v2, fit, champ_gates, adopted):
    """Gates on the SHIPPED matrix. If the estimated decomposition degraded
    holdout SSE, the shipped matrix fell back to the uniform split (identical
    to the champion's), so 'no holdout degradation' holds by construction —
    the beats-uniform result is still reported honestly as its own field."""
    own = pd.to_numeric(elast_df["own_elast"], errors="coerce").to_numpy(float)
    own_neg = bool(np.all(own < 0))
    sign_preserved = True
    if len(cross_v2):
        sign_preserved = bool(np.all(np.sign(cross_v2["elast"]) ==
                                     np.sign(cross_v2["cross_uniform"])))
    frac_nonneg = float((cross_v2["elast"] >= 0).mean()) if len(cross_v2) else np.nan
    beats = bool(fit.get("estimated")
                 and fit["sse_holdout_challenger"] <= fit["sse_holdout_uniform"] * 1.0001)
    gates = {
        "own_diag_all_negative": own_neg,
        "cross_signs_match_champion": sign_preserved,
        "frac_nonneg_cross": round(frac_nonneg, 3) if np.isfinite(frac_nonneg) else None,
        "champion_frac_pos_cross_by_category": champ_gates.get("frac_pos_cross"),
        "decomposition_estimated": bool(fit.get("estimated")),
        "decomposition_beats_uniform_on_holdout": beats,
        "shipped_weights": "decomposed" if adopted else "uniform_fallback",
        "holdout_no_degradation_of_shipped_matrix": bool(beats if adopted else True),
    }
    gates["all_pass"] = bool(own_neg and sign_preserved
                             and gates["holdout_no_degradation_of_shipped_matrix"])
    return gates


def write_report(out_dir, run, fit, gates, mstats, comp_df, cross_v2, comp_note):
    if not gates["all_pass"]:
        ver = "DO NOT ADOPT — a gate failed"
    elif gates.get("shipped_weights") == "uniform_fallback":
        ver = ("ADOPT-READY, but note: the similarity decomposition failed its holdout gate, so "
               "the shipped cross block is the champion's own uniform split — the NET new content "
               "is the assembled matrix E and the measured competitor columns")
    else:
        ver = "ADOPT-READY (still a challenger — switch is a deliberate config flag)"
    L = ["# Cross-Price v2 — decomposed matrix E + competitor interaction (CHALLENGER)\n",
         f"*Run `{os.path.basename(run)}` · elasticity source: {ELAST_METHOD} · "
         f"champion files untouched — consumers switch only via an explicit future config flag.*\n",
         f"## Verdict: {ver}\n"]
    for k, v in gates.items():
        L.append(f"- {k}: **{v}**")
    L.append("\n## 1. What changed vs the champion (price_06)\n")
    if fit.get("estimated"):
        L.append(f"The champion splits each category's total cross response **uniformly** over "
                 f"siblings. The challenger re-weights the SAME total mass by similarity "
                 f"(estimated on train weeks, {fit['n_holdout_weeks']} holdout weeks):\n")
        L.append(f"- m_family = **{fit['m_family']:.2f}** (different base product damps to this fraction)")
        L.append(f"- m_size   = **{fit['m_size']:.2f}** (different size bucket, ratio band 0.75–1.33)")
        L.append(f"- m_ppk    = **{fit['m_ppk']:.2f}** (different price-per-kg tier, band 0.80–1.25)")
        L.append(f"- Holdout weighted SSE: decomposed {fit['sse_holdout_challenger']:.2f} vs uniform "
                 f"{fit['sse_holdout_uniform']:.2f} → **{fit['holdout_improvement_pct']:+.2f}%** "
                 f"(positive = decomposition fits held-out weeks better).")
        if fit.get("decomposition_adopted_in_output"):
            L.append("- The decomposition beat the uniform split on holdout, so "
                     "`cross_price_v2.csv` carries the DECOMPOSED weights.")
        else:
            L.append("- **The decomposition LOST on holdout — it overfits the training weeks.** "
                     "Honest call: the within-category cross signal in this data is too weak to "
                     "support similarity re-weighting, so `cross_price_v2.csv` ships the SAFE "
                     "uniform split (identical numbers to the champion's cross_price.csv). The "
                     "estimated m's above are reported for the receipt, not used.")
        L.append(f"- Scale note: per-SKU total substitution mass is preserved exactly "
                 f"(sum of w_ij = 1), so portfolio conclusions from the champion "
                 f"(e.g. the cannibalization honesty check) cannot flip either way.")
    else:
        L.append(f"Decomposition NOT estimated ({fit.get('reason')}); challenger falls back to the "
                 f"uniform split — no pretend precision on thin data.")
    if len(cross_v2) and gates.get("shipped_weights") == "decomposed":
        d = cross_v2.assign(diff=(cross_v2["elast"] - cross_v2["cross_uniform"]).abs())
        top = d.sort_values("diff", ascending=False).head(8)
        L.append("\n**Pairs most re-weighted vs the uniform split:**\n")
        L.append("| SKU i | SKU j | City | uniform | decomposed |")
        L.append("|---|---|---|---:|---:|")
        for _, r in top.iterrows():
            L.append(f"| {r['entity_i'][:14]} | {r['entity_j'][:14]} | {str(r['city'])[:10]} "
                     f"| {r['cross_uniform']:+.4f} | {r['elast']:+.4f} |")
    L.append("\n## 2. The full matrix E (price_12)\n")
    for k, v in mstats.items():
        L.append(f"- {k}: {v}")
    L.append("\nHonest structure statement: own-brand **cross-category** entries are ZERO by "
             "construction — they have never been estimated for this brand and this challenger "
             "does not invent them. Competitor DEMAND rows are absent entirely: rival unit sales "
             "are not observed anywhere in the data, so only competitor price COLUMNS (our "
             "demand's response to rival prices) are identifiable.\n")
    L.append("## 3. Competitor price-follow elasticity (price_18)\n")
    if comp_note:
        L.append(f"_{comp_note}_")
    elif len(comp_df):
        L.append("Fact-table per-SKU `Competitor Price` is **100% empty**, so rival price comes "
                 "from RCA `competitor_features.csv` (category x city x week median). Confounder "
                 "controls identical to the production elasticity modules (cell FE, promo, OSA, "
                 "month, recency x volume weights), HC1 SEs.\n")
        L.append("**Why two specs**: the paper-brief RPI form (`ln units ~ ln RPI`, "
                 "RPI = own/comp price) mechanically forces comp elasticity = −own elasticity, so "
                 "our own ~−1 price response leaks into it and it reads 'significant' even when "
                 "rivals do nothing — we measured exactly that (median RPI coef ≈ −1). It is "
                 "reported below for transparency but NEVER used. Spec B frees own and comp "
                 "prices; its comp coefficient is the honest measure, gated on significance, "
                 "materiality and substitutes sign (rival price up must not push our units down).\n")
        L.append("| Category | RPI coef (A) | comp elast (B) | ±SE | p (B) | used in E | verdict |")
        L.append("|---|---:|---:|---:|---:|---:|---|")
        for _, r in comp_df.iterrows():
            def _f(x):
                return f"{x}" if pd.notna(x) else "n/a"
            L.append(f"| {str(r['category'])[:24]} | {_f(r['rpi_coef'])} | {_f(r['comp_elast'])} | "
                     f"{_f(r['se'])} | {_f(r['p_value'])} | {r['comp_elast_used']} | {r['gate_reason']} |")
        n_used = int((comp_df["comp_elast_used"] != 0).sum())
        if n_used == 0:
            L.append("\n**Verdict: NULL across the board — no category shows a statistically real, "
                     "material, sign-sane response to rival prices once confounders are "
                     "controlled.** That is not a failure of the module; it MATCHES the "
                     "challenger.py finding that competition barely moves this brand. The E matrix "
                     "carries honest zeros in the competitor columns, turning a silent assumption "
                     "into a measured claim.")
        else:
            sig = comp_df[comp_df["comp_elast_used"] != 0]
            L.append(f"\n**{n_used}/{len(comp_df)} categories show a real, sign-sane "
                     f"competitor-price response** ({', '.join(sig['category'].astype(str))}). "
                     f"These enter E's competitor columns; all others are honest zeros.")
    L.append("\n## How to read / adopt\n")
    L.append("- `cross_price_v2.csv` = long-format E: `own_diag` rows (production own "
             "elasticities, reused untouched), `within_cat_cross` rows (decomposed), `comp_col` "
             "rows (gated). Unlisted own-brand pairs are structural zeros.")
    L.append("- Adoption is NOT automatic. If the verdict is ADOPT-READY, a future pricing run may "
             "point at cross_price_v2.csv via an explicit config flag — a deliberate one-line "
             "change, never done by this build.")
    with open(os.path.join(out_dir, "CROSS_PRICE_V2.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────
def main(holdout_frac=HOLDOUT_FRAC, out_dir=OUT_DEFAULT):
    os.makedirs(out_dir, exist_ok=True)
    fact, run = _latest_fact_table()
    print(f"[xp2] fact_table: {os.path.basename(run)}")
    panel = pp.build_pricing_panel(fact)
    print(f"[xp2] panel: {len(panel)} cell-weeks | {panel['product_id'].nunique()} SKUs | "
          f"{panel['city'].nunique()} cities")

    elast_df, cross_df, baseline, champ_gates = eh.estimate_elasticities(panel)
    per_cat = _per_category_fit(champ_gates)
    print(f"[xp2] champion ({ELAST_METHOD}): own median {elast_df['own_elast'].median():+.2f} | "
          f"{len(cross_df)} uniform cross pairs | {len(per_cat)} categories")

    frame = _prep_frame(panel)
    groups = _build_groups(frame)
    fit = fit_decomposition(frame, groups, per_cat, holdout_frac)
    adopted = bool(fit.get("estimated")
                   and fit["sse_holdout_challenger"] <= fit["sse_holdout_uniform"] * 1.0001)
    fit["decomposition_adopted_in_output"] = adopted
    if fit.get("estimated"):
        print(f"[xp2] price_06 decomposition ({fit['n_evals']} grid evals, "
              f"{fit['n_sibling_rows']} sibling rows): m_family={fit['m_family']:.2f} "
              f"m_size={fit['m_size']:.2f} m_ppk={fit['m_ppk']:.2f} | holdout SSE "
              f"{fit['sse_holdout_challenger']:.2f} vs uniform {fit['sse_holdout_uniform']:.2f} "
              f"({fit['holdout_improvement_pct']:+.2f}%)")
        if not adopted:
            print("[xp2] price_06: decomposition LOSES on holdout (overfits train) — "
                  "shipped matrix falls back to the champion's uniform split. Honest verdict: "
                  "the cross signal here is too weak to support similarity re-weighting.")
    else:
        print(f"[xp2] price_06: NOT estimated — {fit.get('reason')} (uniform fallback)")

    mf, ms, mk = ((fit["m_family"], fit["m_size"], fit["m_ppk"]) if adopted
                  else (1.0, 1.0, 1.0))
    cross_v2 = decompose_cross(baseline, per_cat, mf, ms, mk)
    comp_df, comp_note = competitor_elasticity(frame)
    if comp_note:
        print(f"[xp2] price_18: {comp_note}")
    elif len(comp_df):
        n_used = int((comp_df["comp_elast_used"] != 0).sum())
        med = comp_df["comp_elast"].median()
        print(f"[xp2] price_18 competitor-price elasticity (Spec B, own price controlled): "
              f"median {med:+.3f}, {n_used}/{len(comp_df)} categories pass the "
              f"significance+materiality+sign gate"
              + (" — NULL, matches challenger.py (competition barely moves this brand)"
                 if n_used == 0 else ""))
        comp_df.to_csv(os.path.join(out_dir, "competitor_price_elasticity.csv"), index=False)

    E, mstats = assemble_matrix(elast_df, cross_v2, comp_df, baseline)
    gates = acceptance_gates(elast_df, cross_v2, fit, champ_gates, adopted)
    E.to_csv(os.path.join(out_dir, "cross_price_v2.csv"), index=False)
    comps = {"method": "multiplicative decomposition, grid+refine WLS on FWL residuals",
             "elasticity_source": ELAST_METHOD,
             "factor_formulas": {
                 "family": "m_family if base_product differs else 1",
                 "size": f"m_size if pack-gram ratio < {SIZE_SAME_LO} else 1 (NaN grams -> differ)",
                 "ppk": f"m_ppk if price-per-kg ratio < {PPK_SAME_LO} else 1 (NaN -> differ)",
                 "normalization": "w_ij = w_raw_ij / sum_k w_raw_ik (row sums to 1; "
                                  "champion total cross mass preserved exactly)"},
             "fit": fit, "acceptance_gates": gates, "matrix_stats": mstats}
    with open(os.path.join(out_dir, "cross_price_v2_components.json"), "w",
              encoding="utf-8") as f:
        json.dump(comps, f, indent=2, default=str)
    write_report(out_dir, run, fit, gates, mstats, comp_df, cross_v2, comp_note)
    print(f"[xp2] E matrix: {mstats['n_own_diag']} own diag + "
          f"{mstats['n_within_cat_cross']} within-cat cross + {mstats['n_comp_cols']} comp-col rows "
          f"(fill {mstats['fill_pct_own_brand']}% of own-brand cells; cross-category = structural 0)")
    print(f"[xp2] gates: {gates}")
    print(f"[xp2] wrote cross_price_v2.csv, cross_price_v2_components.json, CROSS_PRICE_V2.md "
          f"-> {out_dir}")
    return {"fit": fit, "gates": gates, "matrix_stats": mstats}


# ─────────────────────────────────────────────────────────────────────────────
# self-test: planted-weight recovery on a synthetic panel
# ─────────────────────────────────────────────────────────────────────────────
def _selftest():
    """Synthetic 1-category, 1-city, 4-SKU panel (2 families x 2 sizes) with a
    PLANTED weight structure (m_family=0.25, m_size=0.75; ppk constructed equal
    across SKUs so the ppk factor is deliberately unidentified and ignored).
    Asserts the estimator recovers family/size damping and that the challenger
    beats the nested uniform split on holdout."""
    rng = np.random.RandomState(11)
    skus = [("A500", "FamA", 500.0, 50.0), ("A1000", "FamA", 1000.0, 100.0),
            ("B500", "FamB", 500.0, 50.0), ("B1000", "FamB", 1000.0, 100.0)]
    true_mf, true_ms, own, cross_tot = 0.25, 0.75, -1.2, 0.6
    T = 60
    lnp = {}
    for pid, fam, g, base in skus:
        disc = np.clip(np.cumsum(rng.normal(0, 0.06, T)), -0.35, 0.15)
        lnp[pid] = np.log(base) + disc
    fam = [s[1] for s in skus]; grams = [s[2] for s in skus]
    ppk = [s[3] / (s[2] / 1000.0) for s in skus]   # all 100 -> same tier everywhere
    W = _pair_weight_matrix(*_mismatch_masks(fam, grams, ppk), true_mf, true_ms, 1.0)
    W = W / W.sum(axis=1, keepdims=True)
    rows = []
    start = pd.Timestamp("2025-01-06")
    for t in range(T):
        wk = (start + pd.Timedelta(weeks=t)).strftime("%Y-%m-%d")
        month = (start + pd.Timedelta(weeks=t)).month
        for i, (pid, fm, g, base) in enumerate(skus):
            sib = float(np.dot(W[i], [lnp[s[0]][t] for s in skus]))
            lu = 5.0 + own * (lnp[pid][t] - np.log(base)) \
                + cross_tot * (sib - float(np.dot(W[i], [np.log(s[3]) for s in skus]))) \
                + rng.normal(0, 0.05)
            rows.append({"product_id": pid, "city": "X", "category": "Cat",
                         "base_product": fm, "pack_grams": g, "title": pid,
                         "week": wk, "month": month, "units": float(np.exp(lu)),
                         "price": float(np.exp(lnp[pid][t])), "mrp": base * 1.2,
                         "disc": 0.0, "regular_price": base, "is_promo": False,
                         "osa": 95.0, "recency_w": 1.0, "volume_w": 1.0})
    panel = pd.DataFrame(rows)
    frame = _prep_frame(panel)
    groups = _build_groups(frame)
    per_cat = {"Cat": (own, cross_tot)}
    fit = fit_decomposition(frame, groups, per_cat, holdout_frac=0.2)
    assert fit["estimated"], fit
    print(f"[selftest] planted m_family={true_mf} m_size={true_ms} -> recovered "
          f"m_family={fit['m_family']:.2f} m_size={fit['m_size']:.2f} "
          f"(m_ppk unidentified by design: {fit['m_ppk']:.2f})")
    assert abs(fit["m_family"] - true_mf) <= 0.20, f"family recovery off: {fit['m_family']}"
    assert abs(fit["m_size"] - true_ms) <= 0.25, f"size recovery off: {fit['m_size']}"
    assert fit["sse_holdout_challenger"] <= fit["sse_holdout_uniform"], \
        "challenger must beat nested uniform on synthetic holdout"
    # weights normalize to 1 and uniform is nested at m=(1,1,1)
    base = pp.freeze_baselines(panel) if hasattr(pp, "freeze_baselines") else None
    cv = decompose_cross(base, per_cat, fit["m_family"], fit["m_size"], fit["m_ppk"])
    sums = cv.groupby(["entity_i", "city"])["w_ij"].sum()
    assert np.allclose(sums.values, 1.0, atol=1e-6), "w_ij rows must sum to 1"
    cu = decompose_cross(base, per_cat, 1.0, 1.0, 1.0)
    assert np.allclose(cu["elast"], cu["cross_uniform"], atol=1e-5), \
        "m=(1,1,1) must reproduce the champion uniform split exactly"
    print("[selftest] PASS — recovery within tolerance, uniform nested, rows sum to 1")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Cross-price v2 challenger (price_06/12/18)")
    ap.add_argument("--holdout-frac", type=float, default=HOLDOUT_FRAC,
                    help="fraction of most-recent weeks held out for the gate")
    ap.add_argument("--out", default=OUT_DEFAULT, help="output directory")
    ap.add_argument("--selftest", action="store_true",
                    help="run the planted-weight synthetic recovery test only")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    else:
        main(holdout_frac=a.holdout_frac, out_dir=a.out)
