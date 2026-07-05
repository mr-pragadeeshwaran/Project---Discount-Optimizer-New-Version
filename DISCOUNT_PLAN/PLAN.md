# Confounder-Controlled Discount Plan — 24 Mantra Organic (Blinkit)

*Run `20260705_161703` · 84 products × 11 cities = 585 cells · 26 weeks (6 months) · validated C1–C6 PASS*

## 1. Bottom line

- **Bankable savings (high-confidence): ₹37,972/month** — ₹0.38 L.
- **+ Experimental upside (test first): ₹24,593/mo**; theoretical all-in ceiling ₹87,672/mo.
- **vs the ₹6–10 L/month target: FAR BELOW.** Even the aggressive all-in figure (₹0.88 L) is ~1/7th of the low end.
- Total discount spend across the portfolio is **₹5,374,944/mo**; genuine recoverable waste is **1.6%** of it.

**Why the ceiling is this low — the core finding:** once discount's effect is *isolated* from availability (OSA), ad visibility (SOV) and competitive share, **discount barely moves sales** for this brand. Sales are driven by being *in stock* and *visible*, not by discounting. So most apparent 'discount waste' is really **availability-constrained cells** (where discount was never the lever) or cells where discount already sits near break-even. There is no ₹6 L of pure discount waste to cut — cutting that much would destroy volume on the SKUs where discount *does* work.

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
| Oil | +0.0210 | 0.0023 | 0.88 | +0.31 | 574 | ✅ works
| Salt | +0.0117 | 0.0044 | 0.88 | +0.55 | 225 |
| Single Spice Powder | +0.0101 | 0.0024 | 0.90 | +0.41 | 1,154 |
| Wheat Atta | +0.0058 | 0.0016 | 0.89 | +0.38 | 883 |
| Besan & Gram Flour | +0.0044 | 0.0042 | 0.89 | +0.58 | 199 | ⚠️ weak/≤0
| Dal & Pulses | +0.0043 | 0.0017 | 0.89 | +0.46 | 2,389 |
| Rice & Rice Products | +0.0040 | 0.0014 | 0.93 | +0.38 | 1,776 |
| Plain Peanuts | +0.0039 | 0.0015 | 0.91 | +0.35 | 489 |
| Jaggery | +0.0013 | 0.0023 | 0.90 | +0.41 | 431 | ⚠️ weak/≤0
| Poha | -0.0030 | 0.0025 | 0.89 | +0.51 | 516 | ⚠️ weak/≤0
| Wheat, Daliya & More | -0.0036 | 0.0049 | 0.90 | +0.51 | 278 | ⚠️ weak/≤0
| Whole Spices | -0.0054 | 0.0023 | 0.93 | +0.48 | 942 | ⚠️ weak/≤0
| Sugar | -0.0079 | 0.0027 | 0.89 | +0.54 | 489 | ⚠️ weak/≤0
| Millets | -0.0089 | 0.0091 | 0.88 | +0.33 | 100 | ⚠️ weak/≤0
| Millet & Other Atta | -0.0102 | 0.0020 | 0.90 | +0.51 | 982 | ⚠️ weak/≤0
| Seeds | -0.0368 | 0.0061 | 0.80 | +0.45 | 298 | ⚠️ weak/≤0
| Sooji | -0.0476 | 0.0057 | 0.95 | +0.71 | 219 | ⚠️ weak/≤0
| Indian Sweets | -0.0728 | 0.0194 | 0.93 | +0.58 | 154 | ⚠️ weak/≤0
| Honey | -0.0941 | 0.0204 | 0.86 | +0.53 | 128 | ⚠️ weak/≤0

Only **Oil, Salt, Single Spice Powder** show a discount effect strong enough to clear the net-revenue break-even. Most categories: discount gives away margin faster than it buys volume.

## 3. Every cell is bucketed before any action (condition 2)

| Bucket | Cells | Action |
|---|---:|---|
| a | 212 | **fix availability, do NOT cut** |
| b | 104 | **flag, cutting may accelerate loss** |
| c | 13 | **CUT** |
| d | 183 | **test-trim** |
| e | 1 | **protect/reinvest** |
| f | 72 | **monitor** |

## 4. CUT list — genuine below-break-even waste (condition 3)

13 cells. **Bank the 5 High-confidence cuts (₹37,972/mo)**; run the 7 Experimental ones as controlled tests (discount shows no reliable lift — cut a few ppt, watch 2–3 weeks).

| Conf | Product | City | Disc→Target | OSA | Save/mo | Why (isolated attribution) |
|---|---|---|---|---:|---:|---|
| Low | Tur / Arhar Dal | Lucknow | 26%→0% | 75% | ₹25,107 | flat despite 26% discount, no confounder explains it → discount buying |
| High | 7 Grain Organic Atta | Delhi-NCR | 18%→0% | 81% | ₹20,554 | sells on osa (not discount); discount 18% is above break-even 0% → red |
| High | Premium Whole Wheat Atta | Bangalore | 21%→0% | 89% | ₹16,084 | discount 21% is the main lever but sits ABOVE break-even 0% (marginal  |
| Expe | Ragi Flour | Bangalore | 9%→0% | 94% | ₹13,542 | sells on share (not discount); discount 9% is above break-even 0% → re |
| Expe | Jowar Flour | Bangalore | 9%→0% | 92% | ₹6,457 | flat despite 9% discount, no confounder explains it → discount buying  |
| Expe | Cumin Seeds / Jeera Seed | Pune | 14%→0% | 86% | ₹1,345 | sells on osa (not discount); discount 14% is above break-even 0% → red |
| Expe | Ragi Flour | Chandigarh Tricity | 9%→0% | 94% | ₹1,128 | sells on share (not discount); discount 9% is above break-even 0% → re |
| Expe | Cumin Seeds / Jeera Seed | Ahmedabad | 14%→0% | 84% | ₹806 | sells on osa (not discount); discount 14% is above break-even 0% → red |
| Expe | Cumin Seeds / Jeera Seed | Chandigarh Tricity | 15%→0% | 92% | ₹779 | sells on share (not discount); discount 15% is above break-even 0% → r |
| High | Turmeric Powder | Delhi-NCR | 17%→1% | 81% | ₹677 | sells on ad visibility (not discount); discount 17% is above break-eve |
| Expe | Besan | Chandigarh Tricity | 12%→0% | 90% | ₹536 | flat despite 12% discount, no confounder explains it → discount buying |
| High | Turmeric Powder | Kolkata | 17%→1% | 91% | ₹435 | sells on share (not discount); discount 17% is above break-even 1% → r |
| High | Turmeric Powder | Chennai | 16%→1% | 82% | ₹222 | sells on share (not discount); discount 16% is above break-even 1% → r |

## 5. Do-NOT-cut — where the money looks wasted but isn't

- **212 availability-constrained cells** (median OSA 74%). Their discount spend (₹878,045/mo) is NOT the problem — **fix stock**. Cutting discount here won't save money; the sales are gated by being out of stock ~26% of the time.
- **104 competitive/defensive cells** losing category share. Cutting discount here may **accelerate the share loss** — hold and watch the competitor, don't cut on autopilot.

## 6. REINVEST list — where discount genuinely pays (condition 7)

**25 cells** where the isolated discount effect is reliably positive AND current discount sits BELOW its net-revenue break-even — i.e. an extra rupee of discount returns **more** than a rupee of net revenue. The discount budget is **mis-allocated**: spread thin across products where it does nothing, while these are under-invested.

| Category | Cells | Median current disc | Break-even disc | Headroom |
|---|---:|---:|---:|---:|
| Oil | 23 | 20% | 52% | +32 ppt |
| Salt | 2 | 8% | 15% | +7 ppt |

**The real play is REALLOCATION, not just cutting:** pull discount off the waste + experimental cells and concentrate it on Oil (and Salt), where it demonstrably drives net-revenue-accretive volume.

## 7. Achievable savings vs target — honest reason (condition 6)

| Figure | ₹/month | vs ₹6–10 L |
|---|---:|---|
| **High-confidence (bank it)** | ₹37,972 | **BELOW** |
| + Experimental (test first) | ₹24,593 | |
| All-in theoretical ceiling | ₹87,672 | **BELOW** (~1/7th of low end) |

**Why not ₹6 L:** (1) discount's *isolated* effect is weak/negative in 16 of 19 categories — the raw discount↔sales link was a confounder (availability/visibility) all along; (2) 25% of the portfolio (212 cells) is availability-constrained — that spend is a stock problem, not discount waste; (3) the discount that *does* work (Oil, Salt) is already near or below break-even and should be **protected**, not cut. Inflating the cut to hit ₹6 L would mean cutting profitable discount and destroying volume — the data does not support it.

## 8. Confidence (condition 4)

- **High: 302 cells** — reliable category fit, ≥8 weeks, real within-cell discount variation, discount effect statistically positive. Act on these.
- **Experimental: 195 cells** — fit ok but discount effect not reliably positive. Treat as A/B tests, never as certainties.
- **Low: 88 cells** — thin data / category below fit floor. Flagged, not acted on.

See `MEASUREMENT_SPEC.md` for week-by-week tracking and `DATA_GAPS.md` for the fields that would most improve the next run.
