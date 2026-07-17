# Elasticity Validation Gates — 3-Stage Protocol

*Run `20260711_221318` · matrix `DISCOUNT_PLAN\pricing\elasticities.csv` · 585 SKU×city cells · generated 2026-07-11 22:58*

## Verdict: **FAIL** — do NOT bank savings from this matrix; act via live tests only

## Stage 1 — statistical fit (holdout 4 weeks, 2026-06-08..2026-06-29) — **FAIL**

| Metric | Measured | Threshold | Verdict |
|---|---:|---:|---|
| Holdout R² (log space, weighted) | 0.715 | ≥ 0.50 | PASS |
| wMAPE (units) | 0.454 | ≤ 0.40 | FAIL |
| \|bias\| (units) | 0.108 (signed +0.108) | ≤ 0.05 | FAIL |

- Scored 1786 holdout cell-weeks (skipped: 31 thin-history, 0 no elasticity row).
- Prediction = per-cell weighted log anchor + own/cross elasticity terms; wMAPE/bias in units space follow the estimator's own gate convention (exp of the log fit; the log-anchor Jensen effect pulls bias slightly negative, so it is not the excuse here) — reported, not corrected away.
- Signed bias is **+10.8%** = systematic OVER-prediction: the holdout weeks sold below what the trailing anchor + elasticity model expects. The matrix is not tracking the recent demand level, let alone the price response.

## Stage 2 — sign & magnitude sanity — **PASS**

| Check | Measured | Threshold | Verdict |
|---|---:|---:|---|
| Own elasticity negative (share of 585 cells) | 1.000 | ≥ 0.95 | PASS |
| \|own\| in [0.05, 5.0] | 1.000 | ≥ 0.95 | PASS |
| Cross pairs in [-1.0, 1.0] (n=2594) | 1.000 | ≥ 0.99 | PASS |
| Cross pairs ≥ 0 (substitutes) | 0.770 | ≥ 0.50 | PASS |
| Cells pinned at the prior (flag only, not gated) | 1.000 | reported | FLAGGED |

**100% of cells are pinned at the prior** (posterior SD > 0.6): the data barely moves the estimate off the prior mean (-1.005). Signs and magnitudes above pass largely BECAUSE of the prior, not because the data identified them. That is the honest weak-identification signal — treat point estimates as test hypotheses, not bankable numbers.

## Stage 3 — stability (half_split, refit = bayes) — **PASS**

- Window A: 2025-12-29..2026-03-30 (569 cells) → median own **-1.008**
- Window B: 2026-04-06..2026-06-29 (516 cells) → median own **-0.946**
- Drift **0.062** vs threshold ≤ 0.30 → PASS (production matrix median: -1.006)
- Caveat: with most cells pinned at the prior, low drift is partly the PRIOR being stable across windows, not evidence the data pins the elasticity.

## What to do with this

**Stage 1 fit failed.** Do not promote this matrix as a demand forecaster. The validated use remains directional + conservative: glide moves, 2-week watch, scale only on register receipts. Exit code 1 lets a cron retrain refuse to auto-promote.

_Thresholds: wMAPE/R² reuse the codebase's own gates (elasticity_hier), |bias| ≤ 5% is the paper-strict bar (codebase's is 10%). Stage 3 refits with the production estimator chain (bayes → hier fallback), identical to pricing_engine._