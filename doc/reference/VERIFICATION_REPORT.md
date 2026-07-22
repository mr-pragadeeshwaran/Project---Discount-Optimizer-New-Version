# Verification Report — Implementation Check
**Date:** 6 July 2026 · Verified against actual code + data, not the status doc.

## Verified DONE ✓

| Gap | Evidence |
|---|---|
| 1 — Actuals backfill | `actuals.py`: baselines frozen once (persisted `baselines.json`), actuals never overwritten, idempotent re-runs, malformed-input guards. Smoke test passes. Wired into tracker via `--actuals`. |
| 2 — Kill-switch (core) | `killswitch.py`: confounder-check first (OSA/SOV drop excused), 2 strikes → revert, freeze window that really expires, portfolio drift brake, reads `VOLUME_DROP_TOLERANCE_PCT` (previously dead config). Smoke test passes. Readout puts REVERT/ALERT first. **But see Issue A.** |
| 3 — Execution gating | `apply_execution_log()` + applied-only scoring wired into tracker. **But see Issue B.** |
| 5 — Stale reports | PLAN.md now shows the correct ₹6.98L / 63-cut run. Dashboard's fake "last week" block replaced with a live scorecard read that honestly says "no results yet". |
| 6 — Week labels | Auto-derived from history (`_auto_week_label`); `--actuals` flag exists. |
| 10 — Competitor data (half) | `competitor_features.py` → 6,534 category×city×week rows, 0% nulls (median price, avg/max/p75 discount, OSA). |
| HK — Strategic SKUs | Wired: cells in `STRATEGIC_SKUS` are never auto-cut. List itself still empty (your action). |
| Loop proof | `verify_loop.py` end-to-end sim exists; both module smoke tests pass in my run. |

## Issues found — fix before Week 2 ⚠️

**A. Kill-switch judges cells that were never acted on (real logic bug).**
`evaluate()` scores every row that has actuals — it never checks `applied` or the action type. Your current W1 history has 438 holds with predicted delta exactly 0 and 53 cuts. Consequences the first time actuals flow:
- Drift hit-rate compares sign(0) vs actual sign for all 438 holds → near-guaranteed miss on every hold → hit-rate collapses → **drift brake trips immediately and permanently blocks new cuts**, even if all 53 cuts perform perfectly.
- Holds accumulate phantom strikes → bogus "auto-REVERTED" alerts for cells whose discount was never changed.
**Fix logic:** kill-switch (strikes + drift metric) must evaluate only rows where action ∈ {cut, reinvest} AND applied = Y. (verify_loop passed because it only asserts "actuals filled & scored > 0" — it never asserts the drift brake stayed off.)

**B. No execution-log template is ever generated.**
The tracker reads `execution_log.csv` if present, but nothing creates it. Your KAM would have to hand-type exact cell_ids into a CSV. **Fix logic:** each weekly run should emit the week's template (week, cell_id, product, city, action, applied=blank) for cut/reinvest cells only; KAM just fills Y/N.

**C. On-disk outputs inconsistent with the new history.**
`tracker_history.csv` has the new schema with 492 cells / 53 cuts, but `WEEKLY_READOUT.md` on disk still shows the old 63-cut / 585-cell output. Also unexplained: why 492 of 585 cells and 53 of 63 cuts were logged. Re-run the tracker once end-to-end and confirm the readout regenerates and the counts reconcile (may be a file-sync artifact or an interrupted last run).

## Acknowledged pending (status doc is honest about these)

- **Gap 7** reinvest pilot loop — waits on banked actuals + a real budget number.
- **Gap 8** A/B machinery — spec still the old one-liner; no "Tests Running" sheet yet.
- **Gap 9** retrain cadence / champion-challenger — not built; DML gate exists in `validate_plan.py` (C8) but predates this work, and the weekly tracker still doesn't read `dml_results.json` itself.
- **Gap 10 second half** — competitor features not yet fed into the model regression / bucket logic.
- One-command weekly chain — `run.bat` still only runs Engine 1; no cross-artifact consistency check.
- `DATA_GAPS.md` / `MEASUREMENT_SPEC.md` still carry old-run bucket counts.
- Duplicate week label still silently skipped (should warn).

## Only you can provide

Weekly export ritual · KAM's applied Y/N each week · real budget % (`--budget_pct`) · hero SKU IDs in `STRATEGIC_SKUS` · promo-type flags from the platform team.
