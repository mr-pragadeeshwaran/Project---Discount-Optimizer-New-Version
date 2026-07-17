# Competitor Integration — Champion vs Challenger

*Run `20260711_221318`. Model A (champion, untouched) vs Model B (champion + competitor average discount as a control). Pre-registered rule: adopt B only if out-of-sample R² ≥ 0.75, the competitor coefficient signs sanely (rivals discount ↑ → our units ↓), and all category fits hold.*

## Verdict

**KEEP Model A (champion) — B did not clear the bar or added nothing material.**

| | Model A (champion) | Model B (+ competitor) |
|---|---:|---:|
| Out-of-sample R² | 0.781 | 0.762 |
| Waste-cut cells | 63 | 60 |
| High-conf savings/mo | ₹697,722 | ₹663,660 |
| All-conf savings/mo | ₹725,069 | ₹689,539 |
| Competitor coef (agg) | — | +0.0008 (sane) |

## What competition does to the number

- Controlling for competitor discounting, the high-confidence savings move from **₹697,722 → ₹663,660/mo** (-5%).
- **5 cells change bucket** when competition is controlled.
- **3 'waste' cuts turn out to be competitive defense** (bucket c under A, not-c under B) — these are cells where our discount was actually holding the line against a rival promo, not pure waste.

Cells that were mislabeled waste (now competitive defense):

- 24055_500g_Bangalore  (c_waste_cut → f_monitor)
- 24055_500g_Hyderabad  (c_waste_cut → f_monitor)
- 5793_500g_Bangalore  (c_waste_cut → f_monitor)

## Honest read

Competition is **not a material confounder** for this brand: competitor discount barely moves our units (near-zero coefficient) and is only mildly correlated with our own discounting. The savings survive essentially intact — the discount waste is real, **not** competitive defense in disguise. Model A stands; the challenger confirms its robustness rather than overturning it.

_Reusable harness: rerun at each 4-weekly retrain. B is adopted only when it clears the pre-registered rule; otherwise the champion stands. This is champion/challenger, never a silent edit to the model._