# PricingAI — Portfolio Elasticity & Optimized Discount Plan

*Adapted from PepsiCo PricingAI (hierarchical elasticity → differential-evolution optimizer). Run `20260717_214500` · 93 SKUs × 11 cities · no Gurobi, no cloud — runs on your laptop.*

## 1. What this adds over the per-cell tool

Your current tool judges each SKU×city **in isolation**. This adds the missing portfolio physics: **cross-price elasticity (cannibalization)** — cutting one SKU's discount changes its siblings' sales. That's the difference between 'this SKU's sales held' and 'the *portfolio* gained'.

## 2. Elasticities (conjugate_bayes_empirical_hierarchical)

- Own-price: median **-1.02** with median posterior SD **±0.71** — **true Bayesian bands, no hard clip**. An informative negative prior + hierarchical shrinkage replaces the old clip.
- **19/19 categories are LOW-CONFIDENCE** (wide band): once confounders are controlled, within-cell price variation barely identifies own-price. That's the honest signal — the same weak-identification wall, now shown as uncertainty instead of a fabricated point estimate.
- Cross-price substitute links: **2978** (positive = siblings gain when a SKU's price rises).

**Per-category confidence** (own-price posterior; low-confidence = wide band, act via TEST only — do NOT bank the saving):

| Category | Own-price | ± SD | Confidence |
|---|---:|---:|---|
| Seeds | -1.04 | 0.80 | LOW — test only |
| Honey | -1.05 | 0.80 | LOW — test only |
| Salt | -1.04 | 0.80 | LOW — test only |
| Indian Sweets | -0.97 | 0.79 | LOW — test only |
| Millets | -1.06 | 0.79 | LOW — test only |
| Besan & Gram Flour | -0.98 | 0.79 | LOW — test only |
| Wheat, Daliya & More | -0.96 | 0.78 | LOW — test only |
| Oil | -1.10 | 0.77 | LOW — test only |
| Single Spice Powder | -0.95 | 0.76 | LOW — test only |
| Plain Peanuts | -0.97 | 0.76 | LOW — test only |
| Poha | -1.32 | 0.74 | LOW — test only |
| Sugar | -1.07 | 0.74 | LOW — test only |
| Sooji | -1.22 | 0.73 | LOW — test only |
| Millet & Other Atta | -1.02 | 0.71 | LOW — test only |
| Whole Spices | -1.01 | 0.71 | LOW — test only |
| Dal & Pulses | -1.23 | 0.70 | LOW — test only |
| Jaggery | -0.77 | 0.70 | LOW — test only |
| Wheat Atta | -1.26 | 0.69 | LOW — test only |
| Rice & Rice Products | -0.89 | 0.66 | LOW — test only |

**19/19 categories are low-confidence.** The Bayesian path applies NO clip — a wide band is reported honestly as uncertainty, not squeezed into a fabricated point estimate. **Low-confidence cells should be acted on only via a live test, never banked as a booked saving.**

**Strongest cannibalization links** (cut one → the other absorbs it):

- 21831 ↔ 495081: cross-elast +0.21
- 21831 ↔ 495081: cross-elast +0.21
- 495081 ↔ 21831: cross-elast +0.21
- 21831 ↔ 64011: cross-elast +0.21
- 64011 ↔ 21831: cross-elast +0.21
- 21831 ↔ 64011: cross-elast +0.21
- 64011 ↔ 21831: cross-elast +0.21
- 495081 ↔ 21831: cross-elast +0.21

## 3. The honesty check — does the ₹6.98L cut list survive cross-price?

- Simulated the existing **38 waste-cuts** through the cross-price model.
- Portfolio revenue impact: **+0.13%**; 89 sibling cells gain volume.
- **Verdict: cuts hold at PORTFOLIO level.**

## 4. Optimized discount plan (portfolio-aware)

Objective = **revenue**, subject to: revenue ≥ 98% of baseline, ≤3ppt weekly change, price-per-kg ladders (bigger pack cheaper/kg), psychological ₹-thresholds.

- Projected: revenue **+2.2%**, volume **+0.1%**, NRW **+2.1%**.
- 41 cells get more discount, 566 get less.

| SKU | City | Disc now→opt | Pred rev Δ% |
|---|---|---|---:|
| 21831 | Mumbai | 4%→1% | +9.1% |
| 21831 | Bangalore | 10%→10% | +9.0% |
| 21831 | Delhi-NCR | 11%→10% | +9.0% |
| 21831 | Kolkata | 10%→10% | +8.7% |
| 64011 | Pune | 18%→15% | +6.9% |
| 64011 | Chennai | 19%→16% | +6.9% |
| 64011 | Hyderabad | 21%→21% | +6.8% |
| 64011 | Mumbai | 19%→16% | +6.7% |
| 21831 | Chennai | 10%→7% | +6.1% |
| 21831 | Pune | 10%→7% | +6.1% |

## 5. Reinvest — where discount reliably PAYS

The optimizer can raise discount, but the Bayesian own-price bands are too wide to *bank* a reinvest on. The confidence comes instead from the DML-confirmed reliable-positive cells — **63 cells, mostly Dal & Pulses** — where discount demonstrably drives net-accretive volume and current discount sits BELOW its break-even.

- Headroom to reinvest profitably: **~₹205,112/month**.
- Play: fund it from the banked waste-cuts (cut inelastic staples → reinvest into Oil). Glide +3ppt, watch 2 weeks, scale only if the register confirms.

| SKU | City | Disc now → break-even | Headroom |
|---|---|---|---:|
| 443581 | Hyderabad | 17% → 31% | +14ppt |
| 443581 | Delhi-NCR | 17% → 31% | +14ppt |
| 443581 | Bangalore | 17% → 31% | +14ppt |
| 443581 | Others | 17% → 31% | +14ppt |
| 443581 | Kolkata | 18% → 31% | +13ppt |
| 443581 | Mumbai | 18% → 31% | +13ppt |
| 443581 | Lucknow | 18% → 31% | +13ppt |
| 443581 | Pune | 18% → 31% | +13ppt |

## Engine agreement

Of the **38 discount_plan waste-cuts**, the pricing optimizer independently agrees to **cut 28**. On the rest it would instead **hold 2** and **raise 8** `agreement.csv` records this per cell; the tracker only actually cuts a waste cell when the optimizer also says cut (`agree_with_cut=True`) — otherwise it HOLDs and tests first, so the two engines never quietly contradict each other.

- Both engines cut: **28/38**
- Pricing engine would HOLD (test first): **2**
- Pricing engine would RAISE discount: **8**

_Schema: `agreement.csv` = cell_id, product_id, city, pricing_action ('cut'|'raise'|'hold'), agree_with_cut (bool). agree_with_cut = (cell in waste cut_list) AND (pricing_action=='cut')._


_Elasticities are TRUE Bayesian posteriors (conjugate, informative negative prior, empirical-Bayes hierarchical shrinkage) — mean **and** SD, no hard clip. PyMC was attempted but forces numpy≥2 which binary-breaks the repo's sklearn stack; the analytic conjugate posterior is the same Bayesian object without the dependency conflict._