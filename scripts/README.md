# scripts/

Ad-hoc diagnostic and experiment scripts. **Not** part of the production
pipeline — feel free to delete files in here without breaking anything.

## diagnostics/

Throwaway data-quality probes. Useful when validating new input files or
investigating anomalies in a specific cell.

| File | What it does |
|------|--------------|
| `data_diagnostic.py` | Per-file shape / column / brand / discount distribution dump. Run when a new Excel arrives. |
| `diag2.py`, `diag3.py`, `diag4.py` | Older incremental probes from earlier debugging sessions. |
| `diag_dal.py` | Investigation of the Moong Dal demand-explosion anomaly (Jan 2025 → Mar 2026, 16× growth). Shows why a per-cell time trend was tested then rejected. |

## experiments/

Model-comparison harnesses used during the May 2026 rewrite. Re-run when
considering a structural change to Stage 4.

| File | What it does |
|------|--------------|
| `experiments.py` | Compares 8 variants (baseline MixedLM, cell-FE OLS, discount-only, random-slope, per-category, Huber, trimmed). Output: `experiment_results.csv`. |
| `experiments4.py` | Time-trend variants (with / without / log / capped). Shows why `time_trend` was dropped from production. |

Run them as one-offs:

```bash
python -X utf8 scripts/experiments/experiments.py
```

They read the most recent `features.csv` from `v4_outputs/`, so you must
have run the main pipeline at least once first.
