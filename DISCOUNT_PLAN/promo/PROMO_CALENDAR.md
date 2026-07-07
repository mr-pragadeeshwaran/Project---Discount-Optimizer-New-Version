# Promo Calendar — MILP challenger (PromoAI-style, advisory)

*585 cells x 12 weeks on grid [0, 5, 10, 15, 20]% · decomposed per category x city · HiGHS via scipy.optimize.milp · run `20260705_161703` · total solve wall-clock **5.6s** across 184 subproblems.*

## The calendar in one paragraph

- Horizon net revenue of the chosen calendar: **Rs92,397,953** vs **Rs91,627,645** if every cell just held its current (grid-snapped) discount — **+0.84%**. (Note: that hold-current plan is itself NOT rule-feasible — holding a promo discount 12 straight weeks breaks the max-duration rule — so it is a reference point, not an available alternative.)
- Promo cell-weeks scheduled: **1448** of 7020 (21%). Weekly discount spend ranges Rs164,058–Rs251,831.
- Defense-held cells (kept at current level, all weeks): **3 cell-solves**.

**Read this honestly:** the demand kernel's validated honesty clamps credit volume from a discount only where own-price elasticity is *reliably* negative — which on this portfolio is almost nowhere. A net-revenue-max calendar therefore parks most cells at 0% discount and the 'calendar' structure you see comes from the constraints (holds, budget, duration rules), not from demand seasonality (the kernel is stationary across weeks). This is the same conclusion as the budget allocator and the confounder-controlled study: discount spend on this portfolio is mostly margin giveaway.

## Solver receipts (val_14: per-solve MIP gap, status, runtime)

- Gap target: **1.0%** relative; time limit 60.0s per subproblem.
- **184/184** subproblems solved to the gap target; **0** hit the time limit (kept incumbent, residual gap flagged below); **0** infeasible.
- Worst achieved gap: **0.8702%**.
- Total runtime: **5.6s**.

| Category | City | Cells | Status | Gap target | Achieved gap | Time (s) | Stop reason |
|---|---|---:|---:|---:|---:|---:|---|
| Wheat Atta | Delhi-NCR | 5 | 0 | 1.0% | 0.3841% | 0.14 | target_gap_hit |
| Whole Spices | Chandigarh T | 3 | 0 | 1.0% | 0.2451% | 0.11 | target_gap_hit |
| Wheat Atta | Hyderabad | 4 | 0 | 1.0% | 0.5251% | 0.11 | target_gap_hit |
| Dal & Pulses | Others | 16 | 0 | 1.0% | 0.0000% | 0.09 | target_gap_hit |
| Dal & Pulses | Chandigarh T | 9 | 0 | 1.0% | 0.0000% | 0.08 | target_gap_hit |
| Dal & Pulses | Delhi-NCR | 11 | 0 | 1.0% | 0.0000% | 0.08 | target_gap_hit |
| Dal & Pulses | Bangalore | 11 | 0 | 1.0% | 0.4598% | 0.08 | target_gap_hit |
| Dal & Pulses | Pune | 11 | 0 | 1.0% | 0.0000% | 0.06 | target_gap_hit |
| Rice & Rice Products | Delhi-NCR | 9 | 0 | 1.0% | 0.0000% | 0.06 | target_gap_hit |
| Rice & Rice Products | Hyderabad | 9 | 0 | 1.0% | 0.4440% | 0.06 | target_gap_hit |
| Dal & Pulses | Kolkata | 10 | 0 | 1.0% | 0.0000% | 0.06 | target_gap_hit |
| Rice & Rice Products | Kolkata | 8 | 0 | 1.0% | 0.4961% | 0.06 | target_gap_hit |
| Rice & Rice Products | Bangalore | 10 | 0 | 1.0% | 0.5888% | 0.06 | target_gap_hit |
| Rice & Rice Products | Mumbai | 8 | 0 | 1.0% | 0.4903% | 0.06 | target_gap_hit |
| Rice & Rice Products | Others | 10 | 0 | 1.0% | 0.3400% | 0.06 | target_gap_hit |

_(top 15 by solve time shown; full table in `promo_solver_report.csv` — 184 rows)_

## Active constraint templates (from promo_constraints.json)

| Template | Rows generated | Cells touched | Note |
|---|---:|---:|---|
| competitive_defense_hold | 0 | 3 | held at current level (defense_hold.csv) |
| promotion_exclusivity | 7020 | 585 | one level per cell-week |
| min_promo_duration | 6984 | 582 | min run 2 wk |
| max_promo_duration | 3492 | 582 | max run 6 wk |
| min_promo_spacing | 5238 | 582 | starts >= 4 wk apart |
| max_simultaneous_promos | 2208 | 582 | <= 3 live promos/wk |
| weekly_budget_cap | 2208 | 585 | spend <= 12% of group baseline revenue/wk (pro-rata per category x city) |

## How to read / operate

- `promo_calendar.csv`: one row per cell x week — the chosen discount level, plus the kernel's predicted units, net revenue and discount spend at that level. `held=True` rows are competitive-defense cells pinned at their current level.
- `promo_solver_report.csv`: one row per (category, city) MILP — the paper-style gap certificate. `achieved_gap` <= `gap_target` means the schedule is provably within that % of the best possible under these rules. `hit_time_limit=True` rows carry a residual gap: the incumbent is kept but is NOT certified to target.
- To onboard another market/brand: copy `promo_constraints.json`, edit params — zero code changes. Unknown template names fail loud.
- **This calendar is advisory (challenger).** Week-1 execution still goes through the champion cut list, the 3 ppt glide, and the weekly tracker. Cross-effects of simultaneous moves are frozen at baseline in the objective (PWL step), and demand carries no week-of-year seasonality — treat week-to-week structure as rule-driven.