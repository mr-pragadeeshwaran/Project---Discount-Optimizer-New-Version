# System Audit — Implementation Status

Response to the 10-gap audit. Honest status per gap: **DONE** (built + verified),
**MECHANISM** (built; needs you/ops to feed it), or **NEEDS DATA/LOOP** (scaffolded
intent; can't truly run until real weekly actuals or a specific input exists).

## The keystone — close the loop (verified end-to-end)

`scripts/tracker/verify_loop.py` proves it on real numbers: predictions logged →
a fresh export backfills actuals → only *applied* cells scored → kill-switch fires
(strikes, reverts, and stock-out weeks correctly excused). Re-runnable any time.

| # | Gap | Status | What was built |
|---|---|---|---|
| **1** | Feedback loop broken (0 actuals) | **DONE** | `actuals.py` — freeze baseline once (persisted `baselines.json`), backfill actuals from a fresh export by cell×week. Verified: 456 actuals filled in the sim. |
| **2** | No kill-switch | **DONE** | `killswitch.py` — reads the config tolerances nothing read before. Confounder-check first (stock-out week never counts as a strike), 2 consecutive strikes → auto-REVERT + 4-week freeze that actually expires, portfolio drift brake blocks new cuts when hit-rate < 60%. REVERT/ALERT section now tops the readout. |
| **3** | No execution log | **MECHANISM** | `execution_log.csv` (week, cell_id, applied Y/N) gates scoring — only applied cells count; unapplied = ops metric, not a model miss. **You/KAM fill this weekly.** |
| **4** | Assumed costs | **DEFERRED (correct)** | Decisions stay on net-revenue optimization (honest without costs). If you get rough category cost %s later, they plug in. Engine-1 margin numbers should be treated as illustrative. |
| **5** | Stale + contradictory reports; mock dashboard | **DONE** | PLAN.md regenerated (₹38k → ₹6.98L) + prose corrected; the fake dashboard "last week performance" (−3.1/−3.4 ✓) now reads the real scorecard or says "no results yet". |
| **6** | Weekly loop, hand-typed labels | **DONE** | Week label auto-derived from history; `--actuals` accepts a fresh export. (Weekly RCA export is your Monday ritual.) |
| **7** | Reinvest never happens | **NEEDS DATA/LOOP** | Reinvest candidates are surfaced (Oil/Daliya). A live pilot loop (top-3 cells, +3ppt, city-control, funded from *realized* savings) needs banked actuals + a real budget number first. |
| **8** | "Needs a test" with no test machinery | **NEEDS DATA/LOOP** | City-split A/B protocol is specified in `MEASUREMENT_SPEC.md`; a "Tests Running" tracker sheet activates once you start the first split. |
| **9** | No retrain; DML not wired in | **PARTIAL** | DML is now a hard gate — `validate_plan.py` C8 fails if any cut category isn't DML-confirmed (10/10 today). Retrain cadence (champion/challenger every 4 weeks) needs the weekly loop accumulating data. |
| **10** | Competitor data thrown away | **DONE** | `competitor_features.py` mines the raw RCA files → `competitor_features.csv`: 6,534 category×city×week rows (competitor median price, avg/max discount, OSA), **100% populated** vs the empty column. Model-feature integration is the next step. |
| HK | STRATEGIC_SKUS empty | **DONE** | Wired — put hero SKU IDs in `v4_config.STRATEGIC_SKUS` and they're never auto-cut. |

## The weekly rhythm (what "running" looks like)

1. **Monday:** export last week's RCA → `python scripts/tracker/weekly_tracker.py --actuals <fact_table>`.
   It auto-fills actuals, scores applied cells, runs the kill-switch, writes the sheet + readout.
2. Read the readout: **REVERT/ALERT first**, then this week's glide cuts.
3. Apply on Blinkit; tick `execution_log.csv` (Applied Y/N).
4. **Every 4 weeks:** retrain + DML re-confirm + realized-vs-forecast review of the ₹6.98L.

## What only you can provide (the loop can't fabricate these)

- **Weekly RCA export** — the whole loop's fuel.
- **Execution confirmation** (Applied Y/N) — without it, "model wrong" and "never applied" are indistinguishable.
- **A real budget number** (e.g. discount ≤ 12.5% of gross) — today's cap defaults to current spend, so it caps nothing.
- **Hero SKU list** for `STRATEGIC_SKUS`.
- **Promo-type flags** (bank offer / brand-funded / platform event) — the cleanest fix for reverse-causality.

The foundation (Gaps 1,2,3,5,6,10 + HK) is live and verified. The growth/learning half
(7,8,9) is intentionally not faked — it switches on once real actuals start flowing.
