# Optimizer Parameter Review — W1 (2026-07-22)

These are the knobs the discount engine TRUSTS WITHOUT QUESTION. The model is retrained on data; these are not — a stale cap or a wrong cost assumption flows straight into every recommendation. Review cadence: budget cap & calendar every planning cycle (28d), everything else quarterly (91d).

## ⚠ Changed since last snapshot (verify each edit was deliberate)

- **fulfillment_fee_inr**: `10` → `10.0`
- **min_change_ppt**: `3` → `3.0`
- **hist_floor_pctile**: `25` → `25.0`
- **festival_calendar**: `{'n_config_festivals': 15, 'n_platform_events': 3, 'n_tracker_windows': 12, 'latest_covered_date': '2026-12-31', 'content_sha1': '3aecd241f69b'}` → `{'n_config_festivals': 25, 'n_platform_events': 3, 'n_tracker_windows': 12, 'latest_covered_date': '2026-12-31', 'content_sha1': '8c6dfa70b273'}`

## ⚠ Standing warnings

- COGS is still the 0.50-of-MRP PROXY — per-SKU costs never supplied (add input_data/cogs_per_sku.csv). Every profit number inherits this assumption.

## Full register

| item | current value | status | days since review | horizon (d) | what to check |
|---|---|---|---|---|---|
| cogs_pct | `0.5` | OK | 14 | 91 | Did procurement cost move? Still a 50%-of-MRP PROXY until per-SKU COGS is supplied. |
| commission_pct | `0.15` | OK | 14 | 91 | Has Blinkit changed its take-rate? |
| fulfillment_fee_inr | `10.0` | CHANGED | 0 | 91 | Has the per-unit fulfillment fee changed? |
| budget_pct_cap | `0.12` | OK | 14 | 28 | Is 12% of gross still the agreed weekly discount-spend ceiling? |
| min_margin_pct | `0.05` | OK | 14 | 91 | Is a 5% floor above variable cost still right? |
| max_comp_premium | `0.1` | OK | 14 | 91 | Max 10% above competitor — still the positioning rule? |
| use_dynamic_glide | `True` | OK | 14 | 91 | Dynamic per-cell glide still wanted vs a flat cap? |
| target_timeline_wks | `12` | OK | 14 | 91 | Is ~3 months still the agreed time to close every discount gap? |
| min_change_ppt | `3.0` | CHANGED | 0 | 91 | Is 3ppt still the smallest customer-visible move? |
| use_hist_floor | `True` | OK | 14 | 91 | Glide target = historical floor (safe) vs elbow (aggressive) — still floor? |
| hist_floor_pctile | `25.0` | CHANGED | 0 | 91 | p25 of trailing 90d as the proven-safe floor — still right? |
| hist_floor_lookback | `90` | OK | 14 | 91 | 90-day floor lookback still representative? |
| strategic_skus | `[]` | OK | 14 | 91 | Hero/flagship SKUs that must never be auto-cut — is the list current? (empty = no hero protection) |
| target_weighted_disc | `9.0` | OK | 14 | 91 | Portfolio flywheel target (9%) — still the strategy? |
| target_disc_pct | `10.0` | OK | 14 | 91 | Dashboard target discount — still the goal? |
| target_quarter | `'Q4 2026'` | OK | 14 | 91 | Target quarter label — still the horizon? |
| marginal_roi_thr | `1.0` | OK | 14 | 91 | Elbow at marginal ROI = 1.0 — any reason to demand more? |
| tier_increase_roi | `2.0` | OK | 14 | 91 | ROI > 2 = under-discounted — still the bar? |
| reinvest_min_lift | `5.0` | OK | 14 | 91 | Reinvest needs >= 5% volume lift per +3ppt — still right? |
| reinvest_max_sac | `10.0` | OK | 14 | 91 | Reinvest margin sacrifice cap (10%) — still right? |
| reinvest_min_elast | `2.0` | OK | 14 | 91 | |elasticity| >= 2 to reinvest — still right? |
| inelastic_thr | `1.0` | OK | 14 | 91 | |e| <= 1 can't pay (theorem boundary) — leave alone unless costs change. |
| vol_drop_tolerance | `5.0` | OK | 14 | 91 | Kill-switch volume-drop tolerance (5%) — still the pain threshold? |
| train_lookback_days | `180` | OK | 14 | 91 | 180d training window — retune only with a backtest receipt. |
| outlier_z | `2.0` | OK | 14 | 91 | Outlier z=2.0 was empirically tuned — retune only with a backtest receipt. |
| festival_calendar | `{'n_config_festivals': 25, 'n_platform_events': 3, 'n_tracke …` | CHANGED | 0 | 28 | Does the calendar cover the next 8 weeks? Add windows before they run out. |
| pricing_engine.CONFIG | `{'kpi': 'revenue', 'disc_lo': 0.0, 'disc_hi': 45.0, 'max_dis …` | OK | 14 | 28 | Optimizer run config (kpi, bounds, glide, revenue floor, psych prices) — is each edit deliberate? |
| de_optimizer.DEFAULT_CONFIG | `{'disc_lo': 0.0, 'disc_hi': 45.0, 'psych_prices': [49, 99, 1 …` | OK | 14 | 91 | Kernel defaults (reachable-discount bounds, PPP thresholds) used by whatif/allocator. |

## Quarterly review checklist

- [ ] COGS: confirm procurement cost per SKU (or accept the 50% proxy for another quarter — knowingly).
- [ ] Commission & fulfillment: confirm Blinkit's current take-rate and per-unit fee.
- [ ] Budget cap: confirm the 12% weekly discount-spend ceiling with finance.
- [ ] Strategic SKUs: confirm the never-auto-cut hero list (currently EMPTY — no hero protection).
- [ ] Festival calendar: covers the next 8 weeks of windows.
- [ ] Glide: timeline (12 wks) and step (3 ppt) still match how fast the brand wants to move.
- [ ] Thresholds: ROI / reinvest gates still match strategy (growth vs margin).
- [ ] Runtime CONFIGs: any pricing_engine/de_optimizer edits were deliberate and logged.

When done: `python -X utf8 scripts/tracker/params_review.py --ack --note "Q-review by <name>"`
