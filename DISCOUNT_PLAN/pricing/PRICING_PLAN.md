# PricingAI — Portfolio Elasticity & Optimized Discount Plan

*Adapted from PepsiCo PricingAI (hierarchical elasticity → differential-evolution optimizer). Run `20260705_161703` · 84 SKUs × 11 cities · no Gurobi, no cloud — runs on your laptop.*

## 1. What this adds over the per-cell tool

Your current tool judges each SKU×city **in isolation**. This adds the missing portfolio physics: **cross-price elasticity (cannibalization)** — cutting one SKU's discount changes its siblings' sales. That's the difference between 'this SKU's sales held' and 'the *portfolio* gained'.

## 2. Elasticities (conjugate_bayes_empirical_hierarchical)

- Own-price: median **-1.01** with median posterior SD **±0.76** — **true Bayesian bands, no hard clip**. An informative negative prior + hierarchical shrinkage replaces the old clip.
- **19/19 categories are LOW-CONFIDENCE** (wide band): once confounders are controlled, within-cell price variation barely identifies own-price. That's the honest signal — the same weak-identification wall, now shown as uncertainty instead of a fabricated point estimate.
- Cross-price substitute links: **2594** (positive = siblings gain when a SKU's price rises).

**Strongest cannibalization links** (cut one → the other absorbs it):

- 532393 ↔ 532389: cross-elast +0.20
- 532389 ↔ 532393: cross-elast +0.20
- 532389 ↔ 532393: cross-elast +0.20
- 532393 ↔ 532389: cross-elast +0.20
- 545915 ↔ 3583: cross-elast +0.19
- 545915 ↔ 3583: cross-elast +0.19
- 3583 ↔ 545915: cross-elast +0.19
- 545915 ↔ 3583: cross-elast +0.19

## 3. The honesty check — does the ₹6.98L cut list survive cross-price?

- Simulated the existing **63 waste-cuts** through the cross-price model.
- Portfolio revenue impact: **+0.61%**; 86 sibling cells gain volume.
- **Verdict: cuts hold at PORTFOLIO level.**

## 4. Optimized discount plan (portfolio-aware)

Objective = **revenue**, subject to: revenue ≥ 98% of baseline, ≤3ppt weekly change, price-per-kg ladders (bigger pack cheaper/kg), psychological ₹-thresholds.

- Projected: revenue **+3.0%**, volume **+1.5%**, NRW **+1.5%**.
- 76 cells get more discount, 474 get less.

| SKU | City | Disc now→opt | Pred rev Δ% |
|---|---|---|---:|
| 98567 | Hyderabad | 13%→10% | +9.2% |
| 86959 | Hyderabad | 13%→10% | +9.2% |
| 98567 | Delhi-NCR | 13%→10% | +8.4% |
| 86959 | Delhi-NCR | 13%→10% | +8.4% |
| 86959 | Bangalore | 13%→10% | +8.3% |
| 3596 | Hyderabad | 2%→0% | +8.3% |
| 98567 | Bangalore | 14%→11% | +8.3% |
| 108382 | Hyderabad | 13%→10% | +8.2% |
| 108382 | Delhi-NCR | 9%→6% | +8.1% |
| 3596 | Bangalore | 2%→0% | +7.9% |

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

_Elasticities are TRUE Bayesian posteriors (conjugate, informative negative prior, empirical-Bayes hierarchical shrinkage) — mean **and** SD, no hard clip. PyMC was attempted but forces numpy≥2 which binary-breaks the repo's sklearn stack; the analytic conjugate posterior is the same Bayesian object without the dependency conflict._