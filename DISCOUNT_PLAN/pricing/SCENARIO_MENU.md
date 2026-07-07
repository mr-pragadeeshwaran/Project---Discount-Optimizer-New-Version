# Scenario Menu — negotiation-ready optimized options

*Run `20260705_161703` · round 1 · challenger artifact — the champion plan (pricing_reco.csv, cut list, tracker) is untouched. All scenarios share the validated demand kernel (de_optimizer.demand_model), so differences between rows are pure objective/constraint choices, not model noise.*

## The menu

Today (row 'current'): ₹7,708,274/wk revenue, ₹1,256,159/wk discount spend (14.0% of gross), weighted-avg discount 14.0%.

| Scenario | Objective | Preset (floor / max move) | Revenue ₹/wk (Δ%) | Units/wk (Δ%) | Disc spend ₹/wk (Δ₹) | Wavg disc | Profit* ₹/wk (Δ₹) | Cells up/down | Kernel check |
|---|---|---|---|---|---|---|---|---|---|
| current | — | — | ₹7,708,274 (+0.00%) | 69,379 (+0.00%) | ₹1,256,159 (+0) | 14.0% | ₹1,376,024 (+0) | 0/0 | OK |
| revenue_base | revenue | 98% / 3ppt | ₹7,944,589 (+3.07%) | 70,418 (+1.50%) | ₹1,098,641 (-157,518) | 12.1% | ₹1,527,110 (+151,086) | 59/432 | OK |
| revenue_tight | revenue | 99% / 2ppt | ₹7,869,393 (+2.09%) | 70,089 (+1.02%) | ₹1,152,026 (-104,133) | 12.8% | ₹1,477,385 (+101,361) | 58/433 | OK |
| revenue_loose | revenue | 96% / 4ppt | ₹8,023,159 (+4.08%) | 70,809 (+2.06%) | ₹1,053,619 (-202,539) | 11.6% | ₹1,573,204 (+197,180) | 58/434 | OK |
| volume_base | volume | 98% / 3ppt | ₹7,858,481 (+1.95%) | 71,243 (+2.69%) | ₹1,279,445 (+23,286) | 14.0% | ₹1,398,313 (+22,289) | 150/283 | OK |
| nrw_base | nrw | 98% / 3ppt | ₹7,849,713 (+1.83%) | 68,516 (-1.24%) | ₹991,754 (-264,405) | 11.2% | ₹1,566,359 (+190,335) | 41/508 | OK |
| share_base | share | 98% / 3ppt | ₹7,859,102 (+1.96%) | 71,246 (+2.69%) | ₹1,279,164 (+23,005) | 14.0% | ₹1,398,643 (+22,619) | 150/283 | OK |
| profit_base | profit | 98% / 3ppt | ₹7,889,910 (+2.36%) | 69,062 (-0.46%) | ₹993,419 (-262,740) | 11.2% | ₹1,574,138 (+198,114) | 20/533 | OK |
| margin_base | margin | 98% / 3ppt | ₹7,857,617 (+1.94%) | 68,678 (-1.01%) | ₹987,654 (-268,505) | 11.2% | ₹1,569,554 (+193,530) | 33/527 | OK |

_Weekly discount-spend policy cap: 12% of gross (v4_config.DEFAULT_BUDGET_PCT_CAP). Above the cap this round: current, revenue_base, revenue_tight, volume_base, share_base — infeasible under current rules as-is; note 'current' itself can be on this list._

_*Profit uses default cost assumptions (COGS 50% of MRP, 15% commission, ₹10/unit fulfillment) until true per-SKU costs are supplied — treat profit DELTAS as directional, levels as rough._

_Objective KPIs available in the optimizer this run: revenue, volume, nrw, share, spend, profit, margin. Profit/margin objectives were available and included._

## How to read this honestly

- Deltas are small because the validated (confounder-controlled) elasticities say discount moves demand weakly on this portfolio. A menu that promised big scenario spreads would be fabricating demand the model does not believe in.
- The optimizer only credits volume to a price cut where own-elasticity is *reliably* negative — same honesty clamp as the champion run.
- 'Cells up/down' counts moves > 0.25ppt; glide caps keep every move executable in one week.

## How a negotiation uses this

1. Pick the scenario matching the counterpart's constraint (finance wants margin -> `revenue_tight`/`nrw_base`; trade wants volume -> `volume_base`) and hand the KAM its per-cell sheet (`scenarios/round_01/reco_<scenario>.csv`).
2. KAM executes it as a glide-capped in-market test; counterpart pushback lands in `negotiation_feedback.csv` (lock/opt-out/max/min per cell) and the menu is re-run for the next round.
3. Actuals feed back through the weekly tracker, elasticities refresh, and the next round's menu is re-optimized on measured — not assumed — response. That is the closed loop.
