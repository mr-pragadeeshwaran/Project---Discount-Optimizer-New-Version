# Promo Calendar — MILP challenger (PromoAI-style, advisory)

*624 cells x 12 weeks on grid [0, 5, 10, 15, 20]% · decomposed per category x city · HiGHS via scipy.optimize.milp · run `20260717_174329` · total solve wall-clock **7.4s** across 186 subproblems.*

## The calendar in one paragraph

- Horizon net revenue of the chosen calendar: **Rs104,108,638** vs **Rs104,327,114** if every cell just held its current (grid-snapped) discount — **-0.21%**. (Note: that hold-current plan is itself NOT rule-feasible — holding a promo discount 12 straight weeks breaks the max-duration rule — so it is a reference point, not an available alternative.)
- Promo cell-weeks scheduled: **3212** of 7488 (43%). Weekly discount spend ranges Rs418,730–Rs614,097.
- Defense-held cells (kept at current level, all weeks): **3 cell-solves**.

**Read this honestly:** the demand kernel's validated honesty clamps credit volume from a discount only where own-price elasticity is *reliably* negative — which on this portfolio is almost nowhere. A net-revenue-max calendar therefore parks most cells at 0% discount and the 'calendar' structure you see comes from the constraints (holds, budget, duration rules), not from demand seasonality (the kernel is stationary across weeks). This is the same conclusion as the budget allocator and the confounder-controlled study: discount spend on this portfolio is mostly margin giveaway.

## Solver receipts (val_14: per-solve MIP gap, status, runtime)

- Gap target: **1.0%** relative; time limit 60.0s per subproblem.
- **184/186** subproblems solved to the gap target; **0** hit the time limit (kept incumbent, residual gap flagged below); **0** infeasible.
- Worst achieved gap: **0.9994%**.
- Total runtime: **7.4s**.

| Category | City | Cells | Status | Gap target | Achieved gap | Time (s) | Stop reason |
|---|---|---:|---:|---:|---:|---:|---|
| Wheat Atta | Bangalore | 5 | 0 | 1.0% | 0.1473% | 0.66 | target_gap_hit |
| Besan & Gram Flour | Bangalore | 1 | 0 | 1.0% | 0.0000% | 0.26 | target_gap_hit |
| Dal & Pulses | Chandigarh T | 9 | 0 | 1.0% | 0.8916% | 0.22 | target_gap_hit |
| Dal & Pulses | Hyderabad | 12 | 0 | 1.0% | 0.4812% | 0.19 | target_gap_hit |
| Dal & Pulses | Others | 17 | 0 | 1.0% | 0.6645% | 0.17 | target_gap_hit |
| Dal & Pulses | Kolkata | 12 | 0 | 1.0% | 0.1915% | 0.12 | target_gap_hit |
| Dal & Pulses | Delhi-NCR | 12 | 0 | 1.0% | 0.6990% | 0.11 | target_gap_hit |
| Dal & Pulses | Pune | 12 | 0 | 1.0% | 0.6908% | 0.11 | target_gap_hit |
| Dal & Pulses | Lucknow | 10 | 0 | 1.0% | 0.4187% | 0.09 | target_gap_hit |
| Dal & Pulses | Bangalore | 11 | 0 | 1.0% | 0.7253% | 0.09 | target_gap_hit |
| Dal & Pulses | Chennai | 10 | 0 | 1.0% | 0.3786% | 0.09 | target_gap_hit |
| Rice & Rice Products | Hyderabad | 10 | 0 | 1.0% | 0.0000% | 0.08 | target_gap_hit |
| Rice & Rice Products | Bangalore | 10 | 0 | 1.0% | 0.0000% | 0.08 | target_gap_hit |
| Rice & Rice Products | Kolkata | 9 | 0 | 1.0% | 0.0000% | 0.08 | target_gap_hit |
| Rice & Rice Products | Others | 10 | 0 | 1.0% | 0.0000% | 0.08 | target_gap_hit |

_(top 15 by solve time shown; full table in `promo_solver_report.csv` — 186 rows)_

## Active constraint templates (from promo_constraints.json)

| Template | Rows generated | Cells touched | Note |
|---|---:|---:|---|
| competitive_defense_hold | 0 | 3 | held at current level (defense_hold.csv) |
| promotion_exclusivity | 7488 | 624 | one level per cell-week |
| min_promo_duration | 7452 | 621 | min run 2 wk |
| max_promo_duration | 3726 | 621 | max run 6 wk |
| min_promo_spacing | 5589 | 621 | starts >= 4 wk apart |
| max_simultaneous_promos | 2208 | 621 | <= 3 live promos/wk |
| weekly_budget_cap | 2208 | 624 | spend <= 12% of group baseline revenue/wk (pro-rata per category x city) |

## How to read / operate

- `promo_calendar.csv`: one row per cell x week — the chosen discount level, plus the kernel's predicted units, net revenue and discount spend at that level. `held=True` rows are competitive-defense cells pinned at their current level.
- `promo_solver_report.csv`: one row per (category, city) MILP — the paper-style gap certificate. `achieved_gap` <= `gap_target` means the schedule is provably within that % of the best possible under these rules. `hit_time_limit=True` rows carry a residual gap: the incumbent is kept but is NOT certified to target.
- To onboard another market/brand: copy `promo_constraints.json`, edit params — zero code changes. Unknown template names fail loud.
- **This calendar is advisory (challenger).** Week-1 execution still goes through the champion cut list, the 3 ppt glide, and the weekly tracker. Cross-effects of simultaneous moves are frozen at baseline in the objective (PWL step), and demand carries no week-of-year seasonality — treat week-to-week structure as rule-driven.