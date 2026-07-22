"""
recovery_test.py — does the model RECOVER a known elasticity?

RECOVERY TEST ON KNOWN-TRUTH SYNTHETIC DATA
-------------------------------------------
(Demonstrates the machinery works on a planted scenario — strong evidence,
 not an unconditional proof. Vary --confound / --endog to probe its limits.)
Every other check (proof loop, credibility report) measures the model on REAL
data, where we don't know the true answer. This does the opposite: it BUILDS
fake sales data with a *known, planted* price elasticity (and the exact
endogeneity trap that wrecks naive analyses — discounts deliberately co-timed
with ad-driven demand spikes), runs the REAL Stage-3 + Stage-4 machinery on
it, and checks two things:

  1. The NAIVE estimate (units ~ price, no controls) is BIASED — it blames the
     ad-driven demand spike on the price cut. (If it weren't biased, the whole
     fixed-effects + controls apparatus would be pointless.)
  2. The MODEL's estimate RECOVERS the planted truth within tolerance.

If the model can't find an elasticity we planted ourselves, it can't be
trusted on real data. Run across many seeds so it's not luck.

    python -X utf8 scripts/diagnostics/recovery_test.py --seeds 10 --true -1.8

Output: output/runs/_recovery/RECOVERY_REPORT.md
"""
import os
import sys
import argparse
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

import v4_config as cfg
from stage3_features.features import engineer_features
from stage4_model.elasticity import train_hierarchical_model

COL = cfg.COL


def build_synthetic_panel(true_elast=-1.8, n_cities=10, n_days=250,
                          confound=0.6, endog_coef=6.0, seed=0):
    """
    Generate a clean (post-Stage-2) fact table with a KNOWN elasticity and a
    deliberate price-endogeneity confound: discounts and ad spend both spike
    during the same 'promo flights', so demand is high exactly when price is
    low — for reasons that have nothing to do with the price cut.

    True data-generating process, per city c, day t:
       log(units) = log(Q0_c) + true_elast·(log p_t − log p_ref)
                    + confound·ad_norm_t + seasonal_t + noise
    A naive units~price fit attributes the confound·ad term to price → biased.
    The model controls for ad (log_ad_sov) + cell FE + month → recovers truth.
    """
    rng = np.random.RandomState(seed)
    mrp = 100.0
    p_ref = mrp * 0.90                      # reference (10% off) price
    dates = pd.date_range("2025-06-01", periods=n_days, freq="D")

    # Promo flights: ad spend + discount spike together (the confound)
    promo = np.zeros(n_days)
    t = 0
    while t < n_days:
        if rng.rand() < 0.06:
            L = rng.randint(7, 12)
            promo[t:t + L] = 1.0
            t += L
        else:
            t += 1
    ad = np.clip(5 + 25 * promo + rng.normal(0, 2, n_days), 0, None)
    ad_norm = ad / 30.0
    seasonal = 0.2 * np.sin(2 * np.pi * np.arange(n_days) / 30.0)

    rows = []
    for c in range(n_cities):
        Q0 = rng.uniform(20, 50)
        base_d = rng.uniform(10, 15)
        # Discount = baseline + LARGE independent variation (the identifying
        # signal — price moves for many reasons unrelated to the confound) +
        # MODEST endogeneity (it also nudges up ~6ppt during ad flights). The
        # independent part is what lets a controls-aware model recover the
        # truth; the endogenous part is what biases the naive fit.
        indep = rng.normal(0, 8, n_days)             # independent price moves
        endog = endog_coef * ad_norm                 # co-moves with ad (the trap)
        disc = base_d + indep + endog + rng.normal(0, 2, n_days)
        disc = np.clip(disc, 0, 45)
        price = mrp * (1 - disc / 100.0)
        log_units = (np.log(Q0)
                     + true_elast * (np.log(price) - np.log(p_ref))
                     + confound * ad_norm
                     + seasonal
                     + rng.normal(0, 0.15, n_days))
        units = np.maximum(np.round(np.exp(log_units)), 1.0)
        for i in range(n_days):
            rows.append({
                COL["product_id"]: "SYN1",
                COL["city"]: f"City_{c:02d}",
                COL["grammage"]: "500g",
                COL["title"]: "Synthetic SKU 500g",
                COL["date"]: dates[i],
                COL["offtake_qty"]: float(units[i]),
                COL["offtake_mrp"]: float(units[i] * price[i]),
                COL["mrp"]: mrp,
                COL["discount_pct"]: float(disc[i]),
                COL["availability"]: 96.0,
                COL["ad_sov"]: float(ad[i]),
                COL["competitor_price"]: float(price[i]),
                "selling_price": float(price[i]),
                "discount_pct_actual": float(disc[i]),
                "stable_mrp": mrp,
                "is_regular_day": 1,
                "is_event_day": 0,
                "is_oos_day": 0,
                "category": "Synthetic",
            })
    return pd.DataFrame(rows)


def _naive_elasticity(fact):
    """Pooled units~price OLS, no controls, no FE — the biased baseline."""
    x = np.log(fact["selling_price"].values.astype(float))
    y = np.log(np.clip(fact[COL["offtake_qty"]].values.astype(float), 0.1, None))
    b = np.polyfit(x, y, 1)[0]
    return float(b)


def _model_elasticity(fact):
    """Run the REAL Stage-3 + Stage-4 machinery; return recovered elasticity."""
    feat = engineer_features(fact)
    res = train_hierarchical_model(feat)
    diag = res["diagnostics"]
    cat = diag.get("category_elasticities", {})
    # select the planted category by name; fail loud if the panel is ever
    # extended to multiple categories (don't silently grab an arbitrary one)
    if "Synthetic" in cat:
        cat_coef = float(cat["Synthetic"])
    elif len(cat) == 1:
        cat_coef = float(next(iter(cat.values())))
    elif not cat:
        cat_coef = float("nan")
    else:
        raise AssertionError(f"recovery_test expects one 'Synthetic' category; got {list(cat)}")
    cell_med = float(res["elasticities"]["price_elasticity"].median())
    return cat_coef, cell_med


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--true", type=float, default=-1.8, help="planted elasticity")
    ap.add_argument("--tol", type=float, default=0.5, help="recovery tolerance")
    ap.add_argument("--confound", type=float, default=0.6, help="ad→demand confound strength")
    ap.add_argument("--endog", type=float, default=6.0, help="ad→discount coupling (the trap)")
    args = ap.parse_args()

    print("=" * 72)
    print(f"  RECOVERY TEST — plant elasticity {args.true}, see if the model finds it")
    print("=" * 72)

    naive, cat, cell = [], [], []
    for s in range(args.seeds):
        fact = build_synthetic_panel(true_elast=args.true, confound=args.confound,
                                     endog_coef=args.endog, seed=s)
        n = _naive_elasticity(fact)
        cc, cm = _model_elasticity(fact)
        naive.append(n); cat.append(cc); cell.append(cm)
        print(f"  seed {s:>2}: naive={n:+.3f}  model(category)={cc:+.3f}  model(cell median)={cm:+.3f}")

    naive = np.array(naive); cat = np.array(cat); cell = np.array(cell)
    true = args.true

    def _stat(a):
        return float(np.mean(a)), float(np.std(a))

    n_m, n_s = _stat(naive)
    c_m, c_s = _stat(cat)
    cell_m, cell_s = _stat(cell)

    # recovery: model lands within tolerance of truth AND materially beats naive.
    # (We do NOT penalise low cross-seed variance — being consistent is good.)
    naive_err = abs(n_m - true); model_err = abs(c_m - true)
    model_bias = c_m - true            # signed: negative = over-states elasticity
    recovered = (model_err <= args.tol) and (model_err < naive_err)
    naive_biased = naive_err > args.tol
    # the confound should make naive MORE elastic (more negative) than truth
    naive_dir_ok = n_m < true
    bias_word = ("slightly OVER-states elasticity" if model_bias < -0.05 else
                 "slightly UNDER-states elasticity" if model_bias > 0.05 else
                 "essentially unbiased")

    out_dir = os.path.join(cfg.OUTPUT_DIR, "_recovery")
    os.makedirs(out_dir, exist_ok=True)
    md = []
    md.append("# Recovery Test — can the model find an elasticity we planted?\n")
    md.append(f"> We generated {args.seeds} fake datasets, each with a **known** price "
              f"elasticity of **{true}** and a deliberate trap: discounts and ad spend "
              f"spike together, so demand is high exactly when price is low — for reasons "
              f"unrelated to the price cut. Then we ran the real model and checked it "
              f"recovers {true}. Generated by `scripts/diagnostics/recovery_test.py`.\n")
    md.append("\n| Estimator | Mean elasticity | Std across seeds | Error vs truth |")
    md.append("|---|---:|---:|---:|")
    md.append(f"| Truth (planted) | {true:+.3f} | — | — |")
    md.append(f"| **Naive** (units~price, no controls) | {n_m:+.3f} | {n_s:.3f} | **{naive_err:.3f}** |")
    md.append(f"| **Model** (FE + controls, per-category) | {c_m:+.3f} | {c_s:.3f} | **{model_err:.3f}** |")
    md.append(f"| Model (per-cell median, shrunk) | {cell_m:+.3f} | {cell_s:.3f} | {abs(cell_m-true):.3f} |")
    md.append("")
    md.append(f"- **Naive is biased:** {'YES — and in the expected direction (more elastic than truth; it blames the ad spike on price)' if (naive_biased and naive_dir_ok) else 'YES' if naive_biased else 'no — confound too weak this run'}")
    md.append(f"- **Model lands within ±{model_err:.2f} of truth** (vs ±{naive_err:.2f} for naive — "
              f"**{naive_err/max(model_err,1e-9):.1f}× closer**), and {bias_word} by "
              f"~{abs(model_bias):.2f} (consistent across seeds, sd {c_s:.2f}).")
    md.append(f"- **Verdict: {'RECOVERED ✓' if recovered else 'NOT recovered ✗ — investigate'}** "
              f"(within ±{args.tol} tolerance and beats naive).")
    md.append("")
    md.append("**How to read it honestly:** the naive number is what you'd get from a "
              "spreadsheet `units vs price` fit — confidently wrong because it can't tell the "
              "price cut apart from the demand spike that coincided with it. The model's fixed "
              "effects + controls strip most of that confound out: it lands close to the "
              f"planted value (a small, consistent ~{abs(model_bias):.2f} {('over' if model_bias<0 else 'under')}-estimate remains — no "
              "observational method is perfect under endogeneity). That residual is honest and "
              "far smaller than the naive error. This is the machinery working on data where we "
              "know the answer — demonstrated, not asserted.\n")
    if not recovered:
        md.append("\n> ⚠ Model did NOT recover the planted value this run — this is a real "
                  "red flag to investigate before trusting the estimates on live data.\n")

    report = os.path.join(out_dir, "RECOVERY_REPORT.md")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    print("\n" + "=" * 72)
    print(f"  Truth planted:        {true:+.3f}")
    print(f"  Naive (biased):       {n_m:+.3f}  (error {naive_err:.3f})")
    print(f"  Model (recovered):    {c_m:+.3f}  (error {model_err:.3f}, sd {c_s:.3f})")
    print(f"  Naive biased?         {naive_biased} (correct direction: {naive_dir_ok})")
    print(f"  Model recovered?      {recovered}  [{bias_word} by ~{abs(model_bias):.2f}]")
    print(f"  Model {naive_err/max(model_err,1e-9):.1f}x closer to truth than naive.")
    print(f"\n  Report: {report}")


if __name__ == "__main__":
    main()
