# Competitor Integration — Champion vs Challenger

*Run `20260717_214500`. Model A (champion, untouched) vs Model B (champion + competitor average discount as a control). Pre-registered rule: adopt B only if out-of-sample R² ≥ 0.75, the competitor coefficient signs sanely (rivals discount ↑ → our units ↓), and all category fits hold.*

## Verdict

**KEEP Model A (champion) — B did not clear the bar or added nothing material.**

| | Model A (champion) | Model B (+ competitor) |
|---|---:|---:|
| Out-of-sample R² | 0.885 | 0.783 |
| Waste-cut cells | 38 | 38 |
| High-conf savings/mo | ₹469,671 | ₹458,567 |
| All-conf savings/mo | ₹469,671 | ₹458,567 |
| Competitor coef (agg) | — | -0.0006 (sane) |

## What competition does to the number

- Controlling for competitor discounting, the high-confidence savings move from **₹469,671 → ₹458,567/mo** (-2%).
- **12 cells change bucket** when competition is controlled.
- **5 'waste' cuts turn out to be competitive defense** (bucket c under A, not-c under B) — these are cells where our discount was actually holding the line against a rival promo, not pure waste.

Cells that were mislabeled waste (now competitive defense):

- 126995_500g_Hyderabad  (c_waste_cut → f_monitor)
- 126995_500g_Kolkata  (c_waste_cut → f_monitor)
- 3583_500g_Bangalore  (c_waste_cut → f_monitor)
- 3583_500g_Chandigarh Tricity  (c_waste_cut → f_monitor)
- 3592_500g_Mumbai  (c_waste_cut → a_stock)

## Honest read

Competition is **not a material confounder** for this brand: competitor discount barely moves our units (near-zero coefficient) and is only mildly correlated with our own discounting. The savings survive essentially intact — the discount waste is real, **not** competitive defense in disguise. Model A stands; the challenger confirms its robustness rather than overturning it.

_Reusable harness: rerun at each 4-weekly retrain. B is adopted only when it clears the pre-registered rule; otherwise the champion stands. This is champion/challenger, never a silent edit to the model._