# Elasticity Validation Gates — 3-Stage Protocol

*Run `20260717_174329` · matrix `DISCOUNT_PLAN\pricing\elasticities.csv` · 627 SKU×city cells · generated 2026-07-17 18:53*

## Verdict: **PASS**

## Stage 1 — statistical fit (holdout 4 weeks, 2026-06-22..2026-07-13) — **PASS**

| Metric | Measured | Threshold | Verdict |
|---|---:|---:|---|
| Holdout R² (log space, weighted) | 0.874 | ≥ 0.50 | PASS |
| wMAPE (units) | 0.285 | ≤ 0.40 | PASS |
| \|bias\| (units) | 0.016 (signed -0.016) | ≤ 0.05 | PASS |

- Scored 1843 holdout cell-weeks (skipped: 51 thin-history, 0 no elasticity row).
- Prediction = per-cell weighted log anchor + own/cross elasticity terms; wMAPE/bias in units space follow the estimator's own gate convention (exp of the log fit; the log-anchor Jensen effect pulls bias slightly negative, so it is not the excuse here) — reported, not corrected away.

## Stage 2 — sign & magnitude sanity — **PASS**

| Check | Measured | Threshold | Verdict |
|---|---:|---:|---|
| Own elasticity negative (share of 627 cells) | 1.000 | ≥ 0.95 | PASS |
| \|own\| in [0.05, 5.0] | 1.000 | ≥ 0.95 | PASS |
| Cross pairs in [-1.0, 1.0] (n=2978) | 1.000 | ≥ 0.99 | PASS |
| Cross pairs ≥ 0 (substitutes) | 1.000 | ≥ 0.50 | PASS |
| Cells pinned at the prior (flag only, not gated) | 1.000 | reported | FLAGGED |

**100% of cells are pinned at the prior** (posterior SD > 0.6): the data barely moves the estimate off the prior mean (-1.026). Signs and magnitudes above pass largely BECAUSE of the prior, not because the data identified them. That is the honest weak-identification signal — treat point estimates as test hypotheses, not bankable numbers.

## Stage 3 — stability (half_split, refit = bayes) — **PASS**

- Window A: 2025-12-29..2026-04-06 (569 cells) → median own **-1.002**
- Window B: 2026-04-13..2026-07-13 (571 cells) → median own **-1.151**
- Drift **0.149** vs threshold ≤ 0.30 → PASS (production matrix median: -1.018)
- Caveat: with most cells pinned at the prior, low drift is partly the PRIOR being stable across windows, not evidence the data pins the elasticity.

## What to do with this

All three stages pass — the matrix may feed the optimizer. Keep the pinned-at-prior share in view: wide-band cells should still be moved via glide + live test, never banked as a saving.

_Thresholds: wMAPE/R² reuse the codebase's own gates (elasticity_hier), |bias| ≤ 5% is the paper-strict bar (codebase's is 10%). Stage 3 refits with the production estimator chain (bayes → hier fallback), identical to pricing_engine._