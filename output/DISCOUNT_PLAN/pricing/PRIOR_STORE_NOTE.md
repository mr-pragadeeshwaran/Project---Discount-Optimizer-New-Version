# Sequential prior store — two-run receipt (price_30 / val_10)

**What this is.** The elasticity model's posterior from one 4-weekly refresh now seeds the prior of the next (the PepsiCo paper's stability mechanism), instead of every refresh restarting from the same fixed constants. OFF by default — the champion path is unchanged until the flag (`ELASTICITY_SEQ_PRIORS=1`) is set deliberately.

- Forgetting factor: stored SDs are inflated x1.25 per 4-week period elapsed, capped at the original diffuse prior SD (0.8 own / 0.4 cross), floored at 0.15 — stale certainty decays, the system can always keep learning.
- Retraining-cadence gate: `python -X utf8 scripts/pricing/prior_store.py --check` flags when the 4-week retrain is due (stamp in priors.json).
- A store whose run failed release gates (`all_pass=false`) is never used to seed.

## Proof run (2026-07-07, data run 20260705_161703, window 2025-12-29 .. 2026-06-29)

| metric | run 1 (fixed priors) | run 2 (seeded from run 1) |
|---|---|---|
| median own elasticity | -1.006 | -1.007 |
| median posterior SD | 0.760 | 0.760 |
| categories seeded | — | 19/19 |
| max abs per-category shift | — | 0.199 |
| stability gate (max shift <= 0.5) | n/a (no prior) | PASS |
| gates all_pass | True | True |

Per-category detail:

| category             |   own_run1 |   own_sd_run1 |   prior_sd_used_run2 |   own_run2 |   own_sd_run2 |   shift |
|:---------------------|-----------:|--------------:|---------------------:|-----------:|--------------:|--------:|
| Besan & Gram Flour   |     -1.03  |         0.795 |                  0.8 |     -1.055 |         0.795 |  -0.025 |
| Dal & Pulses         |     -0.953 |         0.742 |                  0.8 |     -0.907 |         0.742 |   0.046 |
| Honey                |     -1.006 |         0.799 |                  0.8 |     -1.007 |         0.799 |  -0.001 |
| Indian Sweets        |     -0.925 |         0.796 |                  0.8 |     -0.846 |         0.796 |   0.079 |
| Jaggery              |     -1.244 |         0.73  |                  0.8 |     -1.443 |         0.73  |  -0.199 |
| Millet & Other Atta  |     -0.836 |         0.76  |                  0.8 |     -0.686 |         0.76  |   0.15  |
| Millets              |     -1.009 |         0.797 |                  0.8 |     -1.013 |         0.797 |  -0.004 |
| Oil                  |     -1.039 |         0.783 |                  0.8 |     -1.072 |         0.783 |  -0.033 |
| Plain Peanuts        |     -0.995 |         0.779 |                  0.8 |     -0.987 |         0.779 |   0.008 |
| Poha                 |     -1.074 |         0.775 |                  0.8 |     -1.136 |         0.775 |  -0.062 |
| Rice & Rice Products |     -1.076 |         0.719 |                  0.8 |     -1.113 |         0.719 |  -0.037 |
| Salt                 |     -1.013 |         0.798 |                  0.8 |     -1.021 |         0.798 |  -0.008 |
| Seeds                |     -1.006 |         0.799 |                  0.8 |     -1.007 |         0.799 |  -0.001 |
| Single Spice Powder  |     -0.829 |         0.777 |                  0.8 |     -0.668 |         0.777 |   0.161 |
| Sooji                |     -0.857 |         0.782 |                  0.8 |     -0.716 |         0.782 |   0.141 |
| Sugar                |     -1.009 |         0.759 |                  0.8 |     -1.012 |         0.759 |  -0.003 |
| Wheat Atta           |     -1.171 |         0.74  |                  0.8 |     -1.313 |         0.74  |  -0.142 |
| Wheat, Daliya & More |     -1.03  |         0.791 |                  0.8 |     -1.054 |         0.791 |  -0.024 |
| Whole Spices         |     -1.027 |         0.761 |                  0.8 |     -1.038 |         0.761 |  -0.011 |

## Honest caveats

1. **This proof reuses the same data window twice** (only one window exists yet), so run 2 counts the data twice: estimates move FURTHER in the direction the data pulls, away from the fixed -1.0 anchor (biggest: Jaggery -1.24 -> -1.44). That is correct Bayesian mechanics, but it is a double-dip, NOT a stability demo — the damping benefit only shows at the next refresh, when the prior carries OLD weeks' information against NEW weeks' noise. Do not turn the flag on twice within one data window.
2. **The stored posteriors are barely tighter than the diffuse prior** (median own SD 0.76 vs diffuse 0.8), so after x1.25 inflation every category's carried prior SD hit the 0.8 cap — today the store carries the MEAN forward, no extra certainty, and run-2 SDs are unchanged (0.76). That is what the data supports: weekly price variation is thin, so the model has genuinely learned little beyond the prior. Expect the store to matter more as price-change weeks accumulate.
3. Champion gates carry no cross SD, so cross priors reuse the diffuse SD until a seq-aware run writes its own (flagged `cross_sd_source` in priors.json).
4. Per-cell posteriors are stored, but the champion estimator pools at category level, so per-cell rows currently duplicate their category value.
