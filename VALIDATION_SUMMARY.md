# Discount Report — Validation Summary (Condition 6 statement)

**Run:** `20260705_155039` · **Data:** 24 Mantra Organic, Blinkit, 90 days, 89 SKUs
**Validator:** `scripts/diagnostics/validate_report.py` → **C1–C5 ALL PASS**

## The honest number

| Figure | Value |
|---|---|
| **Achievable net savings** | **₹57,387 / month** (≈ ₹0.57 lakh) |
| vs ₹6–10 lakh target | **BELOW** |
| Where it comes from | 25 discount slices, 4 SKUs, 11 cities — all below break-even, all in SKUs the model can trust |

This is the sum of every discount slice whose removal **raises** net revenue (units × selling price),
restricted to SKUs whose response model clears R² ≥ 0.80. Nothing above break-even was cut to
reach a bigger headline — per the rule, the data sets the ceiling.

## Why the ceiling is ₹57k, not ₹6 lakh (data-driven reason)

1. **Most discount is already working, not wasted.** 455 of 516 cells sit **at or above break-even** —
   the discount there buys enough extra volume to more than pay for itself. Cutting it loses more
   revenue than it saves. Only 61 cells are genuinely below break-even.

2. **Model trust floor.** Only **19 of 69 SKUs** have a response model clearing R² ≥ 0.80 at the SKU
   grain (pooling a product's cities at the 3-ppt discount grain). The rest are low-volume / spiky
   (many spices, honey, sweets) where 90 days isn't enough signal to trust a per-SKU cut. We do **not**
   act on those — R² is a trust floor, never a lever to inflate savings.

3. **The two filters barely overlap.** A slice must be *both* below break-even *and* in a trustworthy
   SKU. Only 25 slices in 4 SKUs qualify.

## Where the real (safe) savings are

| SKU | Cells | R² | Net savings/mo |
|---|---|---|---|
| Methi Seeds / Fenugreek | 8 | 0.91 | ₹33,289 |
| Ragi Flour | 7 | 0.88 | ₹17,639 |
| Cumin Seeds / Jeera | 9 | 0.90 | ₹6,242 |
| Honey | 1 | 0.89 | ₹217 |
| **Total** | **25** | | **₹57,387** |

## Condition scorecard

- **C1** Every reported total recomputes from source within 0.5% (volume-weighted, single window). ✅
- **C2** Every recommended cut is below break-even (net-rev gain > 0, marginal ROAS < 1.0). ✅
- **C3** Every acted-on cut sits in a SKU clearing R² ≥ 0.80. ✅
- **C4** Totals reconcile; aggregate net-revenue impact is **positive** (+₹94k/mo). ✅
- **C5** Achievable ceiling computed explicitly = ₹57,387/mo. ✅
- **C6** Actual figure and reason stated above — **no cuts inflated past break-even.** ✅

**Bottom line:** the tool is honest and internally consistent. On this 90-day Blinkit window, the
defensible discount recovery is ~₹57k/month, not ₹6–10 lakh — because on this brand most of the
discount is already earning its keep, and only a quarter of the SKUs have enough signal to cut safely.
