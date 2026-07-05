# Week-by-Week Measurement Spec (condition 7)

*Run `20260705_161703`. For every cell you act on, log these weekly and compare to the pre-cut baseline (trailing 4 weeks). The rule: **a cut is confirmed right only if net revenue holds or rises while discount spend falls.** If units fall faster than the model predicted, the discount was working — restore it.*

## Track per acted-on cell, every week

| Metric | Source field | What confirms the call | Red flag → revert |
|---|---|---|---|
| Units sold | `OFFTAKE_QTY` | within ~5% of model-predicted units at new discount | units drop > predicted → discount was working |
| Net revenue | units × `selling_price` | flat or up vs baseline | falls > 2% for 2 wks |
| Discount % | `discount_pct_actual` | at/near target | drifting back up |
| OSA | `WT_AVAILABILITY_PCT` | ≥ baseline (isolate the cut from stock noise) | OSA drops → result is confounded, pause |
| Ad SOV | `MONTHLY_AD_SOV` | ≥ baseline | SOV collapse confounds the read |
| Category share | `MONTHLY_CAT_SHARE_MRP` | stable | falling → competitor reacting, watch |

## Cadence by bucket

- **c (High-conf cuts):** cut in one 3-ppt step; hold 2 weeks; if net revenue holds, take the next step toward target. Full glide over 4–6 weeks.
- **c (Experimental cuts):** cut ONE 3-ppt step in HALF the cells (A/B); compare treated vs held for 3 weeks before rolling out.
- **a (stock):** don't touch discount; track OSA weekly; re-evaluate once OSA > 85%.
- **b (competitive):** hold discount; track competitor price/share weekly; act only if you have competitor data.
- **e (reinvest, Oil/Salt):** raise discount ONE 3-ppt step in a few cells; confirm units rise enough that net revenue rises before scaling.

## Decision rule each week

```
if net_revenue >= baseline and discount_spend < baseline:  keep going (call confirmed)
elif units_drop > 1.5x model_prediction:                   revert (discount was working)
elif OSA or SOV moved > 10%:                                pause (read is confounded)
```
