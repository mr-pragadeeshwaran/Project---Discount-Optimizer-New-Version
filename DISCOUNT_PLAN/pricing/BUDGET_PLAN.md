# Budget Allocator — marginal-ROI waterline (Objectives 1 & 3)

*Cap discount spend at **10% of baseline revenue** (₹770,080/wk); spend it on the highest marginal-ROI discount first. Same demand kernel as the optimizer.*

## The budget picture

- Baseline revenue: **₹7,700,798/week**.
- Current discount spend: **₹1,254,156/wk (16.3% of revenue)** — vs the 10% cap.
- Under the cap the allocator spends **₹793/wk**, at a **waterline marginal ROI of 1.00** (every rupee of discount kept returns ≥₹1.00).
- Result: **557 cells cut · 1 raised · 27 held**.

**Read this honestly:** the allocator spends almost nothing (₹793 of a ₹770,080 cap) because — under these elasticities — discount barely clears break-even *anywhere*. Only a handful of cells have a discount step whose marginal ROI reaches 1; for the rest, once volume goes flat, marginal ROI sits at −1 (every rupee of discount is a rupee of pure margin given away). So the profit-optimal discount is near-zero — the *fourth* independent confirmation that discount is mostly waste on this portfolio, and an even more aggressive read than the ₹6.98L cut list.

**But do NOT slash all discount overnight.** This rests on the wide-band (≈unit-elastic) Bayesian elasticities — it's a directional cross-check, not an execution plan. The glide, reliability gates, engine-agreement, and in-market tests exist precisely because these estimates are uncertain.

## Marginal-ROI ladder (Objective 1 proof artifact)

`roi_ladder.csv` has every cell's full curve. The **elbow** is where marginal ROI crosses 1 — beyond it, more discount destroys net revenue. Example elbows:

| SKU | City | Elbow discount | Units there | Marginal ROI |
|---|---|---:|---:|---:|
| 3595 | Pune | 2% | 2 | 1.04 |
| 21491 | Delhi-NCR | 8% | 85 | 1.22 |
| 21491 | Hyderabad | 8% | 31 | 1.03 |
| 21752 | Pune | 10% | 5 | 1.01 |
| 286878 | Mumbai | 8% | 11 | 1.12 |
| 521140 | Delhi-NCR | 8% | 50 | 1.15 |
| 521146 | Others | 2% | 13 | 1.08 |
| 532389 | Bangalore | 2% | 173 | 1.04 |
| 532389 | Pune | 2% | 21 | 1.14 |
| 545408 | Bangalore | 2% | 21 | 1.09 |

_Budget % is set with `--budget_pct` (default 0.10). This is a separate constraint mode from the KPI optimizer; run it when you want a hard spend ceiling rather than a revenue floor._