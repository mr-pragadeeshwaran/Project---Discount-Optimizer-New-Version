# Outlier vs Promo Audit — were the removed spikes really noise? (val_16 residual)

*Run `20260717_174329` · 3483 outlier days removed by the |z|>2.0 filter, cross-checked against the festival calendar, platform-event windows, stock-outs, and each cell's own promo depth. ADVISORY: this audits the filter, it does not change it — the champion's training data stays exactly as validated.*

## What explains the removed days

| Explanation | Days | Share | Reading |
|---|---:|---:|---|
| Stock-out (LOW, availability <50%) | 0 | 0% | Couldn't sell — right to exclude |
| Deep promo (HIGH, ≥5ppt above cell's median discount) | 475 | 14% | A documented promo did what promos do — right to exclude from *regular-day* training |
| Festival window (±2d) | 0 | 0% | Calendar-driven demand |
| Platform event (BBD etc.) | 0 | 0% | Platform-driven demand |
| **Unexplained** | 3008 | 86% | Statistical noise or undocumented events |

## How to read this honestly

- **Zero stock-out / festival / platform-event hits is CORRECT, not a bug**: stage-2 excludes event days and out-of-stock days from training BEFORE the z-filter runs (`prepare.py` is_regular_day), so those spikes can never be mistaken for noise — the paper's concern is handled structurally upstream. This audit proves it (min availability among removed outliers = exactly 50%).
- Of the regular days the filter removed, 475 (14%) coincide with the cell's own deep-discount days — documented promo behavior, correctly kept out of *regular-day* training.
- The 3008 unexplained days (86%) are the statistical tail the filter exists to remove: 3483 removals = 2.8% of all rows, consistent with a |z|>2 cut, not with silently eaten demand signal. Some may be promos nobody logged — if a big cut decision ever hinges on one cell, check its unexplained outliers in `outlier_promo_audit.csv` first.
- This closes the paper's 'validate spikes against documented promotional activity' check as an audit receipt. Changing the filter itself would alter the champion's training data and is deliberately NOT done.

_Rerun after each pipeline refresh: `python -X utf8 scripts/validation/outlier_promo_audit.py`._