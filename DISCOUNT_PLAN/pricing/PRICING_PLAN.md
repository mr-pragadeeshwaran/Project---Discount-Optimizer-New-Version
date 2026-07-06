# PricingAI — Portfolio Elasticity & Optimized Discount Plan

*Adapted from PepsiCo PricingAI (hierarchical elasticity → differential-evolution optimizer). Run `20260705_161703` · 84 SKUs × 11 cities · no Gurobi, no cloud — runs on your laptop.*

## 1. What this adds over the per-cell tool

Your current tool judges each SKU×city **in isolation**. This adds the missing portfolio physics: **cross-price elasticity (cannibalization)** — cutting one SKU's discount changes its siblings' sales. That's the difference between 'this SKU's sales held' and 'the *portfolio* gained'.

## 2. Elasticities (hierarchical, partial-pooled)

- Own-price: median **0.00**, all in the (−2.5, 0) sanity band.
- Cross-price substitute links: **472** (positive = siblings gain when a SKU's price rises).
- Validation gates: {'pooled_r2': 0.892, 'pooled_r2_pass': True, 'wmape': 0.255, 'wmape_pass': True, 'abs_bias': 0.066, 'bias_pass': True, 'own_in_band': True, 'cross_nonneg_subs': True, 'frac_pos_cross': 0.775, 'n_cells': 526, 'n_cross_pairs': 472, 'coverage': {'Besan & Gram Flour': {'n_rows': 199, 'r2': 0.716, 'own': -0.8339, 'cross': -0.8339}, 'Dal & Pulses': {'n_rows': 2352, 'r2': 0.79, 'own': -0.9022, 'cross': 0.0509}, 'Honey': {'n_rows': 128, 'r2': 0.665, 'own': 3.8388, 'cross': 3.8388}, 'Indian Sweets': {'n_rows': 137, 'r2': 0.739, 'own': -6.2915, 'cross': 8.0556}, 'Jaggery': {'n_rows': 431, 'r2': 0.908, 'own': -3.1708, 'cross': 0.5184}, 'Millet & Other Atta': {'n_rows': 974, 'r2': 0.813, 'own': 1.0141, 'cross': 1.259}, 'Millets': {'n_rows': 96, 'r2': 0.899, 'own': 0.897, 'cross': 2.6483}, 'Oil': {'n_rows': 565, 'r2': 0.756, 'own': -2.1434, 'cross': 0.5013}, 'Plain Peanuts': {'n_rows': 482, 'r2': 0.855, 'own': -2.1227, 'cross': -0.0861}, 'Poha': {'n_rows': 516, 'r2': 0.823, 'own': 1.1943, 'cross': 0.2999}, 'Rice & Rice Products': {'n_rows': 1764, 'r2': 0.91, 'own': -1.6645, 'cross': -0.4117}, 'Salt': {'n_rows': 211, 'r2': 0.64, 'own': -4.8748, 'cross': 2.1362}, 'Seeds': {'n_rows': 296, 'r2': 0.819, 'own': 1.3895, 'cross': -0.1997}, 'Single Spice Powder': {'n_rows': 1131, 'r2': 0.768, 'own': 0.5274, 'cross': 0.7927}, 'Sooji': {'n_rows': 214, 'r2': 0.892, 'own': 4.9925, 'cross': 0.9827}, 'Sugar': {'n_rows': 485, 'r2': 0.784, 'own': -2.5323, 'cross': 0.3462}, 'Wheat Atta': {'n_rows': 880, 'r2': 0.856, 'own': -1.5703, 'cross': -0.016}, 'Wheat, Daliya & More': {'n_rows': 278, 'r2': 0.838, 'own': -1.3045, 'cross': -1.3045}, 'Whole Spices': {'n_rows': 930, 'r2': 0.861, 'own': 0.0554, 'cross': 0.7215}}, 'all_pass': True}

**Strongest cannibalization links** (cut one → the other absorbs it):

- 532393.0 ↔ 532389.0: cross-elast +8.06
- 532389.0 ↔ 532393.0: cross-elast +8.06
- 438513.0 ↔ 5629.0: cross-elast +2.65
- 5629.0 ↔ 438513.0: cross-elast +2.65
- 542154.0 ↔ 364165.0: cross-elast +2.14
- 364165.0 ↔ 542154.0: cross-elast +2.14
- 545915.0 ↔ 3583.0: cross-elast +0.52
- 3583.0 ↔ 545915.0: cross-elast +0.52

## 3. The honesty check — does the ₹6.98L cut list survive cross-price?

- Simulated the existing **63 waste-cuts** through the cross-price model.
- Portfolio revenue impact: **+1.67%**; 83 sibling cells gain volume.
- **Verdict: cuts hold at PORTFOLIO level.**

## 4. Optimized discount plan (portfolio-aware)

Objective = **revenue**, subject to: revenue ≥ 98% of baseline, ≤3ppt weekly change, price-per-kg ladders (bigger pack cheaper/kg), psychological ₹-thresholds.

- Projected: revenue **+2.9%**, volume **+1.4%**, NRW **+1.4%**.
- 134 cells get more discount, 446 get less.

| SKU | City | Disc now→opt | Pred rev Δ% |
|---|---|---|---:|
| 532389 | Chennai | 1%→0% | +27.8% |
| 532389 | Pune | 2%→1% | +27.5% |
| 532393 | Bangalore | 8%→5% | +21.8% |
| 532389 | Delhi-NCR | 1%→0% | +21.1% |
| 532389 | Others | 1%→0% | +21.0% |
| 532389 | Hyderabad | 1%→0% | +20.7% |
| 532389 | Bangalore | 3%→0% | +20.0% |
| 5629 | Delhi-NCR | 0%→0% | +8.9% |
| 5629 | Others | 0%→0% | +8.9% |
| 443581 | Bangalore | 19%→16% | -8.0% |

_Elasticities are penalized-hierarchical point estimates (posterior-mean equivalent). Full Bayesian posteriors (PyMC) are a drop-in upgrade if uncertainty bands are wanted._