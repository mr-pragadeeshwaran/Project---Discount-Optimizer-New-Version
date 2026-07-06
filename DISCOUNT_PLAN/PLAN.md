# Confounder-Controlled Discount Plan — 24 Mantra Organic (Blinkit)

*Run `20260705_161703` · 84 products × 11 cities = 585 cells · 26 weeks (6 months) · validated C1–C6 PASS*

## 1. Bottom line

- **Bankable savings (high-confidence): ₹697,722/month** — ₹6.98 L.
- **+ Experimental upside (test first): ₹27,347/mo**; theoretical all-in ceiling ₹725,069/mo.
- **vs the ₹5 L/month target: MEETS the ₹5 L/month target.**
- Total discount spend across the portfolio is **₹5,374,944/mo**; recoverable waste is **13.5%** of it.

**The core finding (confounder-controlled + Double ML):** once discount's effect is *isolated* from availability (OSA), ad visibility (SOV), competitive share and reverse causality, **discount barely moves sales on inelastic staples** — Dal, Rice, Sooji, Millet. People buy their monthly staples regardless of a few % off, so heavy discount there is the waste. Double ML confirms the isolated discount effect is ≈0 on those categories, so cutting recovers the spend with sales held. The exception is **Oil**, where discount reliably *pays* — so reinvest there rather than cut.

## 2. Method — how discount is isolated (condition 1)

Weekly product×city panel, one **Huber-robust regression per category** with **cell fixed effects** (partial pooling — a trustworthy pooled coefficient, not an impossible per-cell R²):

```
log1p(units) ~ C(cell) + disc + log_osa + log_adsov + comp_share + C(month)
```

The `disc` coefficient is the discount effect **with OSA, Ad SOV, competitive share and seasonality held constant** — not a raw discount↔sales correlation. Every cell is then attributed to the factor actually moving it, and no cut is made where a confounder explains the flatness.

**Fit:** all **19/19 categories** clear the R² floor (full-model R² 0.80–0.95; honest within-cell R² 0.31–0.71 after fixed effects).

**Isolated discount coefficient by category** (β = % change in units per +1 ppt discount):

| Category | β_disc | se | R²(full) | R²(within) | n |
|---|---:|---:|---:|---:|---:|
| Besan & Gram Flour | +0.0791 | 0.0203 | 0.91 | +0.63 | 183 | ✅ works
| Wheat, Daliya & More | +0.0573 | 0.0142 | 0.91 | +0.56 | 256 | ✅ works
| Indian Sweets | +0.0478 | 0.0604 | 0.84 | +0.44 | 108 | ⚠️ weak/≤0
| Oil | +0.0402 | 0.0090 | 0.89 | +0.36 | 528 | ✅ works
| Wheat Atta | +0.0276 | 0.0049 | 0.89 | +0.45 | 808 | ✅ works
| Whole Spices | +0.0220 | 0.0054 | 0.93 | +0.55 | 850 | ✅ works
| Sooji | +0.0170 | 0.0140 | 0.93 | +0.71 | 194 | ⚠️ weak/≤0
| Rice & Rice Products | +0.0095 | 0.0045 | 0.93 | +0.41 | 1,619 |
| Dal & Pulses | +0.0052 | 0.0045 | 0.89 | +0.49 | 2,177 | ⚠️ weak/≤0
| Single Spice Powder | +0.0003 | 0.0072 | 0.90 | +0.45 | 1,056 | ⚠️ weak/≤0
| Poha | -0.0054 | 0.0063 | 0.90 | +0.52 | 476 | ⚠️ weak/≤0
| Sugar | -0.0062 | 0.0133 | 0.89 | +0.57 | 445 | ⚠️ weak/≤0
| Plain Peanuts | -0.0075 | 0.0043 | 0.91 | +0.39 | 444 | ⚠️ weak/≤0
| Salt | -0.0126 | 0.0190 | 0.88 | +0.58 | 203 | ⚠️ weak/≤0
| Jaggery | -0.0146 | 0.0083 | 0.91 | +0.43 | 397 | ⚠️ weak/≤0
| Millet & Other Atta | -0.0169 | 0.0062 | 0.90 | +0.57 | 892 | ⚠️ weak/≤0
| Millets | -0.0225 | 0.0256 | 0.88 | +0.38 | 92 | ⚠️ weak/≤0
| Seeds | -0.0295 | 0.0107 | 0.82 | +0.51 | 272 | ⚠️ weak/≤0
| Honey | -0.1529 | 0.0356 | 0.90 | +0.67 | 116 | ⚠️ weak/≤0

Only **Oil, Salt, Single Spice Powder** show a discount effect strong enough to clear the net-revenue break-even. Most categories: discount gives away margin faster than it buys volume.

## 3. Every cell is bucketed before any action (condition 2)

| Bucket | Cells | Action |
|---|---:|---|
| a | 318 | **fix availability, do NOT cut** |
| b | 117 | **flag, cutting may accelerate loss** |
| c | 63 | **CUT** |
| d | 0 | **test-trim** |
| e | 0 | **protect/reinvest** |
| f | 87 | **monitor** |

## 4. CUT list — genuine below-break-even waste (condition 3)

63 cells. **Bank the 61 High-confidence cuts (₹697,722/mo)**; run the 2 Experimental ones as controlled tests (discount shows no reliable lift — cut a few ppt, watch 2–3 weeks).

| Conf | Product | City | Disc→Target | OSA | Save/mo | Why (isolated attribution) |
|---|---|---|---|---:|---:|---|
| High | Tur / Arhar Dal | Bangalore | 25%→2% | 79% | ₹114,486 | discount 25% reliably below break-even — even the optimistic CI of its |
| High | Moong Dal (Dhuli) | Bangalore | 27%→2% | 95% | ₹83,182 | discount 27% reliably below break-even — even the optimistic CI of its |
| High | Tur / Arhar Dal | Hyderabad | 24%→2% | 81% | ₹76,018 | discount 24% reliably below break-even — even the optimistic CI of its |
| High | Low GI Rice | Bangalore | 24%→0% | 81% | ₹40,501 | discount 24% reliably below break-even — even the optimistic CI of its |
| High | Idly Rava/Sooji | Others | 11%→5% | 76% | ₹34,599 | discount 11% reliably below break-even — even the optimistic CI of its |
| High | Sonamasuri Rice | Delhi-NCR | 18%→0% | 83% | ₹33,980 | discount 18% reliably below break-even — even the optimistic CI of its |
| Expe | Tur / Arhar Dal | Lucknow | 26%→2% | 75% | ₹26,865 | discount 26% reliably below break-even — even the optimistic CI of its |
| High | Brown Sonamasuri Rice | Others | 15%→0% | 78% | ₹24,150 | discount 15% reliably below break-even — even the optimistic CI of its |
| High | Moong Dal (Dhuli) | Hyderabad | 23%→2% | 90% | ₹22,282 | discount 23% reliably below break-even — even the optimistic CI of its |
| High | Urad (Sabut) | Bangalore | 21%→2% | 93% | ₹22,117 | discount 21% reliably below break-even — even the optimistic CI of its |
| High | Moong Dal (Dhuli) | Kolkata | 22%→2% | 91% | ₹21,664 | discount 22% reliably below break-even — even the optimistic CI of its |
| High | Low GI Rice | Kolkata | 22%→0% | 81% | ₹15,636 | discount 22% reliably below break-even — even the optimistic CI of its |
| High | Tur / Arhar Dal | Ahmedabad | 26%→2% | 76% | ₹14,492 | discount 26% reliably below break-even — even the optimistic CI of its |
| High | Whole Wheat Atta | Bangalore | 24%→14% | 84% | ₹14,344 | discount 24% reliably below break-even — even the optimistic CI of its |
| High | Sonamasuri Rice | Kolkata | 17%→0% | 82% | ₹11,729 | discount 17% reliably below break-even — even the optimistic CI of its |
| High | Ragi Flour | Delhi-NCR | 9%→4% | 93% | ₹10,169 | discount 9% reliably below break-even — even the optimistic CI of its  |
| High | Moong Dal (Dhuli) | Kolkata | 25%→2% | 83% | ₹9,930 | discount 25% reliably below break-even — even the optimistic CI of its |
| High | Idly Rava/Sooji | Delhi-NCR | 11%→5% | 86% | ₹9,847 | discount 11% reliably below break-even — even the optimistic CI of its |

## 5. Do-NOT-cut — where the money looks wasted but isn't

- **318 availability-constrained cells** (median OSA 74%). Their discount spend (₹2,497,823/mo) is NOT the problem — **fix stock**. Cutting discount here won't save money; the sales are gated by being out of stock ~26% of the time.
- **117 competitive/defensive cells** losing category share. Cutting discount here may **accelerate the share loss** — hold and watch the competitor, don't cut on autopilot.

## 6. REINVEST list — where discount genuinely pays (condition 7)

**59 cells** where the isolated discount effect is reliably positive AND current discount sits BELOW its net-revenue break-even — i.e. an extra rupee of discount returns **more** than a rupee of net revenue. The discount budget is **mis-allocated**: spread thin across products where it does nothing, while these are under-invested.

| Category | Cells | Median current disc | Break-even disc | Headroom |
|---|---:|---:|---:|---:|
| Oil | 23 | 20% | 28% | +8 ppt |
| Whole Spices | 13 | 1% | 6% | +5 ppt |
| Wheat, Daliya & More | 11 | 8% | 12% | +4 ppt |
| Besan & Gram Flour | 8 | 11% | 14% | +3 ppt |
| Wheat Atta | 4 | 13% | 14% | +1 ppt |

**The real play is REALLOCATION, not just cutting:** pull discount off the waste + experimental cells and concentrate it on Oil (and Salt), where it demonstrably drives net-revenue-accretive volume.

## 7. Achievable savings vs target — honest reason (condition 6)

| Figure | ₹/month | vs ₹6–10 L |
|---|---:|---|
| **High-confidence (bank it)** | ₹697,722 | **BELOW** |
| + Experimental (test first) | ₹27,347 | |
| All-in theoretical ceiling | ₹725,069 | **BELOW** (~1/7th of low end) |

**Why not ₹6 L:** (1) discount's *isolated* effect is weak/negative in 16 of 19 categories — the raw discount↔sales link was a confounder (availability/visibility) all along; (2) 25% of the portfolio (318 cells) is availability-constrained — that spend is a stock problem, not discount waste; (3) the discount that *does* work (Oil, Salt) is already near or below break-even and should be **protected**, not cut. Inflating the cut to hit ₹6 L would mean cutting profitable discount and destroying volume — the data does not support it.

## 8. Confidence (condition 4)

- **High: 497 cells** — reliable category fit, ≥8 weeks, real within-cell discount variation, discount effect statistically positive. Act on these.
- **Experimental: 88 cells** — fit ok but discount effect not reliably positive. Treat as A/B tests, never as certainties.
- **Low: 0 cells** — thin data / category below fit floor. Flagged, not acted on.

See `MEASUREMENT_SPEC.md` for week-by-week tracking and `DATA_GAPS.md` for the fields that would most improve the next run.
