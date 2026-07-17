# Sensitivity Report — do the cut calls survive shaking the inputs? (val_05)

*Run `20260711_221318` · 63 waste-cut cells x 200 Monte-Carlo draws (seed 42). No per-draw refits: the champion is fit once for its coefficient uncertainty; each draw re-scores the decision rule analytically.*

## What was shaken

- **Elasticity**: each cell's marginal discount effect drawn from N(beta, se) using the champion's own per-category standard error (independent per cell — the conservative choice; correlated draws would flip FEWER cells). Curvature (disc²) held at point value.
- **Costs**: COGS ±10% relative, commission ±3ppt (fulfillment ₹10 fixed) — these raise/lower the PROFIT break-even bar.
- **Baseline units**: ±10% — moves the rupee size of each win, not the sign.

## The verdict

- **Elasticity shake**: median flip rate 0.5%, max 3.0%. The CI cut-gate (needs the effect to sit 1.96 SD below break-even) already bounds this by construction for the 63/63 `reliably_waste` cells — the sweep confirms the gate does its job.
- **Cost shake**: max flip rate 0.0%. Expected: the profit break-even bar sits ABOVE the revenue bar at any cost level in the sweep, so cost uncertainty cannot un-justify a revenue-justified cut. The named val_05 residual (costs never swept) is now closed — and it changes nothing, which is the receipt.
- **Joint shake**: median 0.5%, max 3.0%. **0 cell(s) exceed the 20% fragility bar**, of which **0 are in the live first wave**.
- **Money at stake under the joint shake**: cut-wave saving p10 ₹705,886 / p50 ₹724,161 / p90 ₹739,449 per month (point estimate ₹725,069). The spread comes almost entirely from the ±10% units band — i.e. uncertainty about SIZE of the win, not WHETHER it is one.

No cell crossed the fragility bar — every cut call stands in >80% of joint draws.

## How to read this honestly

A low flip rate says the cut is robust to the uncertainties we can MODEL (coefficient noise, cost bands, units bands). It cannot rule out the uncertainties we can't — a competitor move or platform change mid-wave. That is what the weekly tracker's kill-switch is for; this report only certifies the starting call was not fragile.

_Rerun after each 4-weekly retrain: `python -X utf8 scripts/validation/sensitivity.py`. Fragility bar 20%, draws 200, seed 42._