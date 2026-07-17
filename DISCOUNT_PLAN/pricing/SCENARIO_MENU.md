# Scenario Menu — negotiation-ready optimized options

*Run `20260717_174329` · round 3 · challenger artifact — the champion plan (pricing_reco.csv, cut list, tracker) is untouched. All scenarios share the validated demand kernel (de_optimizer.demand_model), so differences between rows are pure objective/constraint choices, not model noise.*

## The menu

Today (row 'current'): ₹8,869,392/wk revenue, ₹1,826,731/wk discount spend (17.1% of gross), weighted-avg discount 17.1%.

| Scenario | Objective | Preset (floor / max move) | Revenue ₹/wk (Δ%) | Units/wk (Δ%) | Disc spend ₹/wk (Δ₹) | Wavg disc | Profit* ₹/wk (Δ₹) | Cells up/down | Kernel check |
|---|---|---|---|---|---|---|---|---|---|
| current | — | — | ₹8,869,392 (+0.00%) | 82,596 (+0.00%) | ₹1,826,731 (+0) | 17.1% | ₹1,364,964 (+0) | 0/0 | OK |
| revenue_base | revenue | 98% / 3ppt | ₹9,065,558 (+2.21%) | 82,694 (+0.12%) | ₹1,672,784 (-153,947) | 15.6% | ₹1,509,614 (+144,650) | 36/550 | OK |
| revenue_tight | revenue | 99% / 2ppt | ₹8,999,427 (+1.47%) | 82,579 (-0.02%) | ₹1,711,919 (-114,812) | 16.0% | ₹1,468,049 (+103,086) | 37/544 | OK |
| revenue_loose | revenue | 96% / 4ppt | ₹9,131,653 (+2.96%) | 82,820 (+0.27%) | ₹1,625,701 (-201,030) | 15.1% | ₹1,555,032 (+190,068) | 39/549 | OK |
| volume_base | volume | 98% / 3ppt | ₹8,978,624 (+1.23%) | 84,219 (+1.97%) | ₹1,905,645 (+78,914) | 17.5% | ₹1,347,505 (-17,459) | 163/307 | OK |
| nrw_base | nrw | 98% / 3ppt | ₹9,000,207 (+1.47%) | 81,037 (-1.89%) | ₹1,471,670 (-355,061) | 14.1% | ₹1,603,864 (+238,900) | 41/573 | OK |
| share_base | share | 98% / 3ppt | ₹8,979,361 (+1.24%) | 84,223 (+1.97%) | ₹1,905,210 (+78,479) | 17.5% | ₹1,347,945 (-17,019) | 160/309 | OK |
| profit_base | profit | 98% / 3ppt | ₹9,036,831 (+1.89%) | 81,444 (-1.39%) | ₹1,477,028 (-349,703) | 14.0% | ₹1,609,935 (+244,972) | 4/618 | OK |
| margin_base | margin | 98% / 3ppt | ₹8,988,654 (+1.34%) | 80,927 (-2.02%) | ₹1,469,769 (-356,962) | 14.1% | ₹1,601,872 (+236,909) | 44/572 | OK |

_Weekly discount-spend policy cap: 12% of gross (v4_config.DEFAULT_BUDGET_PCT_CAP). Above the cap this round: current, revenue_base, revenue_tight, revenue_loose, volume_base, nrw_base, share_base, profit_base, margin_base — infeasible under current rules as-is; note 'current' itself can be on this list._

_*Profit uses default cost assumptions (COGS 50% of MRP, 15% commission, ₹10/unit fulfillment) until true per-SKU costs are supplied — treat profit DELTAS as directional, levels as rough._

_Objective KPIs available in the optimizer this run: revenue, volume, nrw, share, spend, profit, margin. Profit/margin objectives were available and included._

## How to read this honestly

- Deltas are small because the validated (confounder-controlled) elasticities say discount moves demand weakly on this portfolio. A menu that promised big scenario spreads would be fabricating demand the model does not believe in.
- The optimizer only credits volume to a price cut where own-elasticity is *reliably* negative — same honesty clamp as the champion run.
- 'Cells up/down' counts moves > 0.25ppt; glide caps keep every move executable in one week.

## How a negotiation uses this

1. Pick the scenario matching the counterpart's constraint (finance wants margin -> `revenue_tight`/`nrw_base`; trade wants volume -> `volume_base`) and hand the KAM its per-cell sheet (`scenarios/round_03/reco_<scenario>.csv`).
2. KAM executes it as a glide-capped in-market test; counterpart pushback lands in `negotiation_feedback.csv` (lock/opt-out/max/min per cell) and the menu is re-run for the next round.
3. Actuals feed back through the weekly tracker, elasticities refresh, and the next round's menu is re-optimized on measured — not assumed — response. That is the closed loop.
