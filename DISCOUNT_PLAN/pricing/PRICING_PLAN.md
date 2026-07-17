# PricingAI — Portfolio Elasticity & Optimized Discount Plan

*Adapted from PepsiCo PricingAI (hierarchical elasticity → differential-evolution optimizer). Run `20260711_221318` · 84 SKUs × 11 cities · no Gurobi, no cloud — runs on your laptop.*

## 1. What this adds over the per-cell tool

Your current tool judges each SKU×city **in isolation**. This adds the missing portfolio physics: **cross-price elasticity (cannibalization)** — cutting one SKU's discount changes its siblings' sales. That's the difference between 'this SKU's sales held' and 'the *portfolio* gained'.

## 2. Elasticities (conjugate_bayes_empirical_hierarchical)

- Own-price: median **-1.01** with median posterior SD **±0.76** — **true Bayesian bands, no hard clip**. An informative negative prior + hierarchical shrinkage replaces the old clip.
- **19/19 categories are LOW-CONFIDENCE** (wide band): once confounders are controlled, within-cell price variation barely identifies own-price. That's the honest signal — the same weak-identification wall, now shown as uncertainty instead of a fabricated point estimate.
- Cross-price substitute links: **2594** (positive = siblings gain when a SKU's price rises).

**Per-category confidence** (own-price posterior; low-confidence = wide band, act via TEST only — do NOT bank the saving):

| Category | Own-price | ± SD | Confidence |
|---|---:|---:|---|
| Honey | -1.01 | 0.80 | LOW — test only |
| Seeds | -1.01 | 0.80 | LOW — test only |
| Salt | -1.01 | 0.80 | LOW — test only |
| Millets | -1.01 | 0.80 | LOW — test only |
| Indian Sweets | -0.93 | 0.80 | LOW — test only |
| Besan & Gram Flour | -1.03 | 0.80 | LOW — test only |
| Wheat, Daliya & More | -1.03 | 0.79 | LOW — test only |
| Oil | -1.04 | 0.78 | LOW — test only |
| Sooji | -0.86 | 0.78 | LOW — test only |
| Plain Peanuts | -0.99 | 0.78 | LOW — test only |
| Single Spice Powder | -0.83 | 0.78 | LOW — test only |
| Poha | -1.07 | 0.78 | LOW — test only |
| Whole Spices | -1.03 | 0.76 | LOW — test only |
| Millet & Other Atta | -0.84 | 0.76 | LOW — test only |
| Sugar | -1.01 | 0.76 | LOW — test only |
| Dal & Pulses | -0.95 | 0.74 | LOW — test only |
| Wheat Atta | -1.17 | 0.74 | LOW — test only |
| Jaggery | -1.24 | 0.73 | LOW — test only |
| Rice & Rice Products | -1.08 | 0.72 | LOW — test only |

**19/19 categories are low-confidence.** The Bayesian path applies NO clip — a wide band is reported honestly as uncertainty, not squeezed into a fabricated point estimate. **Low-confidence cells should be acted on only via a live test, never banked as a booked saving.**

**Strongest cannibalization links** (cut one → the other absorbs it):

- 21831 ↔ 495081: cross-elast +0.21
- 495081 ↔ 21831: cross-elast +0.21
- 64011 ↔ 21831: cross-elast +0.21
- 21831 ↔ 64011: cross-elast +0.21
- 21831 ↔ 64011: cross-elast +0.21
- 64011 ↔ 21831: cross-elast +0.21
- 21831 ↔ 64011: cross-elast +0.21
- 64011 ↔ 21831: cross-elast +0.21

## 3. The honesty check — does the ₹6.98L cut list survive cross-price?

- Simulated the existing **63 waste-cuts** through the cross-price model.
- Portfolio revenue impact: **+0.62%**; 86 sibling cells gain volume.
- **Verdict: cuts hold at PORTFOLIO level.**

## 4. Optimized discount plan (portfolio-aware)

Objective = **revenue**, subject to: revenue ≥ 98% of baseline, ≤3ppt weekly change, price-per-kg ladders (bigger pack cheaper/kg), psychological ₹-thresholds.

- Projected: revenue **+3.1%**, volume **+1.5%**, NRW **+1.6%**.
- 69 cells get more discount, 481 get less.

| SKU | City | Disc now→opt | Pred rev Δ% |
|---|---|---|---:|
| 98567 | Hyderabad | 13%→10% | +9.2% |
| 86959 | Hyderabad | 13%→10% | +9.2% |
| 86959 | Delhi-NCR | 13%→10% | +8.4% |
| 98567 | Delhi-NCR | 13%→10% | +8.4% |
| 86959 | Bangalore | 13%→10% | +8.3% |
| 3596 | Hyderabad | 2%→0% | +8.3% |
| 98567 | Bangalore | 14%→11% | +8.3% |
| 108382 | Hyderabad | 13%→10% | +8.2% |
| 108382 | Delhi-NCR | 9%→6% | +8.1% |
| 21831 | Mumbai | 3%→0% | +8.0% |

## 5. Reinvest — where discount reliably PAYS

The optimizer can raise discount, but the Bayesian own-price bands are too wide to *bank* a reinvest on. The confidence comes instead from the DML-confirmed reliable-positive cells — **59 cells, mostly Oil** — where discount demonstrably drives net-accretive volume and current discount sits BELOW its break-even.

- Headroom to reinvest profitably: **~₹157,039/month**.
- Play: fund it from the banked waste-cuts (cut inelastic staples → reinvest into Oil). Glide +3ppt, watch 2 weeks, scale only if the register confirms.

| SKU | City | Disc now → break-even | Headroom |
|---|---|---|---:|
| 443581 | Mumbai | 17% → 28% | +11ppt |
| 21752 | Lucknow | 18% → 28% | +10ppt |
| 21752 | Pune | 19% → 28% | +9ppt |
| 443581 | Others | 19% → 28% | +9ppt |
| 443581 | Bangalore | 19% → 28% | +9ppt |
| 21752 | Bangalore | 20% → 28% | +8ppt |
| 21752 | Others | 20% → 28% | +8ppt |
| 443581 | Delhi-NCR | 20% → 28% | +8ppt |

## Engine agreement

Of the **63 discount_plan waste-cuts**, the pricing optimizer independently agrees to **cut 51**. On the rest it would instead **hold 9** and **raise 3** `agreement.csv` records this per cell; the tracker only actually cuts a waste cell when the optimizer also says cut (`agree_with_cut=True`) — otherwise it HOLDs and tests first, so the two engines never quietly contradict each other.

- Both engines cut: **51/63**
- Pricing engine would HOLD (test first): **9**
- Pricing engine would RAISE discount: **3**

_Schema: `agreement.csv` = cell_id, product_id, city, pricing_action ('cut'|'raise'|'hold'), agree_with_cut (bool). agree_with_cut = (cell in waste cut_list) AND (pricing_action=='cut')._


_Elasticities are TRUE Bayesian posteriors (conjugate, informative negative prior, empirical-Bayes hierarchical shrinkage) — mean **and** SD, no hard clip. PyMC was attempted but forces numpy≥2 which binary-breaks the repo's sklearn stack; the analytic conjugate posterior is the same Bayesian object without the dependency conflict._