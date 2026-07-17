# Budget Allocator — marginal-ROI waterline (Objectives 1 & 3)

*Cap discount spend at **12% of baseline revenue** (₹1,064,129/wk); spend it on the highest marginal-ROI discount first. Same demand kernel as the optimizer.*

## The budget picture

- Baseline revenue: **₹8,867,744/week**.
- Current discount spend: **₹1,826,247/wk (20.6% of revenue)** — vs the 12% cap.
- Under the cap the allocator spends **₹8,814/wk**, at a **waterline marginal ROI of 1.00** (every rupee of discount kept returns ≥₹1.00).
- Result: **621 cells cut · 0 raised · 6 held**.

**Read this honestly:** the allocator spends almost nothing (₹8,814 of a ₹1,064,129 cap) because — under these elasticities — discount barely clears break-even *anywhere*. Only a handful of cells have a discount step whose marginal ROI reaches 1; for the rest, once volume goes flat, marginal ROI sits at −1 (every rupee of discount is a rupee of pure margin given away). So the profit-optimal discount is near-zero — the *fourth* independent confirmation that discount is mostly waste on this portfolio, and an even more aggressive read than the ₹6.98L cut list.

**But do NOT slash all discount overnight.** This rests on the wide-band (≈unit-elastic) Bayesian elasticities — it's a directional cross-check, not an execution plan. The glide, reliability gates, engine-agreement, and in-market tests exist precisely because these estimates are uncertain.

## Marginal-ROI ladder (Objective 1 proof artifact)

`roi_ladder.csv` has every cell's full curve. The **elbow** is where marginal ROI crosses 1 — beyond it, more discount destroys net revenue. Example elbows:

| SKU | City | Elbow discount | Units there | Marginal ROI |
|---|---|---:|---:|---:|
| 3588 | Ahmedabad | 2% | 30 | 1.20 |
| 3588 | Bangalore | 2% | 78 | 1.20 |
| 3588 | Chandigarh | 2% | 4 | 1.20 |
| 3588 | Chennai | 2% | 52 | 1.20 |
| 3588 | Delhi-NCR | 2% | 106 | 1.20 |
| 3588 | Hyderabad | 2% | 84 | 1.20 |
| 3588 | Kolkata | 2% | 81 | 1.20 |
| 3588 | Lucknow | 2% | 21 | 1.20 |
| 3588 | Mumbai | 2% | 25 | 1.20 |
| 3588 | Others | 2% | 109 | 1.20 |

_Budget % is set with `--budget_pct` (default 0.10). This is a separate constraint mode from the KPI optimizer; run it when you want a hard spend ceiling rather than a revenue floor._