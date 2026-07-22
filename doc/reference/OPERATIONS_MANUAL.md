# OPERATIONS MANUAL — Discount Optimization System, End to End
**Architecture · Execution · Weekly Ops · Learning Loop · Troubleshooting**
Version: 7 July 2026, written against git HEAD (`6fd3f1e`). Every path and command below refers to actual files in this repo.

---

# 0. What this system is, and why each part exists

You run one brand (24 Mantra Organic) on one platform (Blinkit), 84 SKUs × 11 cities = **585 cells** (a *cell* = one SKU in one city — the atomic unit of every decision). The system exists to answer four business questions, and every module maps to one of them:

| # | Business question | Subsystem that answers it |
|---|---|---|
| 1 | Where am I over-investing in discount (paying for volume that would come anyway)? | Analysis layer: confounder-controlled regression → buckets → DML confirmation → cut list |
| 2 | Where would MORE discount pay? | Reinvest detection (CI test + DML headroom) → pilot protocol |
| 3 | How should a fixed discount budget be spread across cells? | DE pricing engine (portfolio, cross-price aware) + guardrail budget cap (full allocator: planned) |
| 4 | Is the model right, week after week — and does it improve? | Weekly tracker: frozen baselines → actuals backfill → scorecard → kill-switch → (4-weekly) retrain |

**The architecture in one sentence:** *raw platform exports are cleaned into a daily fact table; two independent decision engines (per-cell causal analysis + portfolio cross-price optimizer) each produce recommendations; a weekly tracker executes only what both engines agree on, in small guarded steps, records predictions, backfills what actually happened, and automatically reverts anything that hurts — so every week of operation is also an experiment that grades the model.*

Design principles inherited from PepsiCo's PricingAI (the paper you supplied): ML demand estimation feeding a constrained optimizer; validation gates before any model is trusted; human approval before any execution; externalized config (thresholds live in config, not buried in code); honest uncertainty (low-confidence → test, never bank).

---

# 1. The architecture map

```
┌──────────────────────── LAYER 0 · INPUT DATA ────────────────────────────┐
│ input_data/                                                              │
│   JAN..JUNE_2026_BLINKIT_RCA.csv   daily, ALL brands, ~750k rows/month   │
│   MY SKU.csv                       your 89-SKU master (allowlist)        │
└───────────────┬───────────────────────────────────────────────────────────┘
                ▼  python -X utf8 pipeline.py
┌──────────────────────── LAYER 1 · DATA PIPELINE (8 stages) ──────────────┐
│ stage1_ingestion   read chunked CSVs → filter own brand (word-boundary   │
│                    regex, over/under-match guards) → validate columns    │
│ stage2_preparation stable_mrp (p90 of MRP) → selling_price → OOS flags   │
│                    (<50% availability) → festival flags → z>2 outliers   │
│                    → is_regular_day → cell_id → **fact_table.csv**       │
│ stage3_features    log transforms, lags, rolling means, badge residual   │
│ stage4_model       per-category Huber elasticity + confidence tiers      │
│ stage5_curves      discount-response curves + 4PL fit                    │
│ stage6_economics   marginal-ROI ladder → elbow (last step with ROI ≥ 1)  │
│ stage7_guardrails  floor price, glide steps, tiering (Strong Cut..Hold)  │
│ stage8_output      WASTE_REINVEST_REPORT.xlsx/.md, leakage decomposition │
│ OUT → v4_outputs/<YYYYMMDD_HHMMSS>/   (run-stamped, never overwritten)   │
└───────────────┬───────────────────────────────────────────────────────────┘
                ▼ fact_table.csv is THE handoff artifact
┌────────────── LAYER 2A · CAUSAL ANALYSIS (decision engine #1) ───────────┐
│ scripts/analysis/discount_plan.py                                        │
│   weekly panel (vol-weighted price/disc per cell-week, is_regular only)  │
│   per-category Huber WLS: ln(units) ~ C(cell) + disc + disc² + ln(OSA)   │
│     + ln(AdSOV) + cat_share + lag1,lag2 + C(month)                       │
│   → buckets: a_stock / b_competitive / c_waste_cut / e_reinvest /        │
│     f_monitor  (CI-based break-even: reliably_waste / reliably_pays)     │
│ scripts/analysis/dml_estimate.py    Double ML (GBM nuisances, cross-fit, │
│   cluster-robust θ) — confirms each cut category causally                │
│ scripts/analysis/validate_plan.py   gates C1–C8 (C8 = every banked cut   │
│   category DML-confirmed)                                                │
│ scripts/analysis/build_report.py    → DISCOUNT_PLAN/PLAN.md, DATA_GAPS,  │
│   MEASUREMENT_SPEC                                                       │
│ OUT → v4_outputs/<run>/plan/ : all_cells.csv, cut_list.csv,              │
│   reinvest_list.csv, test_unlock_list.csv, dml_results.json,             │
│   plan_summary.json                                                      │
└───────────────┬───────────────────────────────────────────────────────────┘
                ▼
┌────────────── LAYER 2B · PORTFOLIO PRICING (decision engine #2) ─────────┐
│ scripts/pricing/pricing_engine.py  (orchestrator)                        │
│   pricing_panel.py      weekly panel + regular_price (±8-wk max) +       │
│                         promo flag (>5% below) + pack_grams + weights    │
│   elasticity_bayes.py   TRUE Bayesian own/cross elasticities             │
│                         (informative negative prior, hierarchical        │
│                         shrinkage, posterior SD, NO clip)                │
│                         [fallback: elasticity_hier.py, penalized ridge]  │
│   de_optimizer.py       differential evolution per category×city group:  │
│                         maximize KPI s.t. revenue floor 98%, glide ≤3ppt,│
│                         price-per-kg ladder, PPP ₹-thresholds; honesty   │
│                         clamps (cuts credited only if reliably elastic)  │
│   whatif.py             /simulate — same demand kernel, no solver        │
│   cannibalization check: pushes engine-#1's cut list through the         │
│                         cross-price model (do cuts leak to siblings?)    │
│ OUT → DISCOUNT_PLAN/pricing/ : elasticities.csv, cross_price.csv,        │
│   pricing_reco.csv, gates.json, PRICING_PLAN.md, **agreement.csv**,      │
│   history/<run>/ (stamped archive)                                       │
└───────────────┬───────────────────────────────────────────────────────────┘
                ▼ agreement.csv = the wiring between the two engines
┌────────────── LAYER 3 · WEEKLY EXECUTION LOOP (the tracker) ─────────────┐
│ scripts/tracker/weekly_tracker.py  (orchestrator, run every Monday)      │
│   1 build_plan_df      map all_cells.csv → tracker contract; STRATEGIC_  │
│                        SKUS never auto-cut                               │
│   2 apply_agreement    cut ONLY if engine#1 bucket=c_waste_cut AND       │
│                        engine#2 agrees (else hold: "engines disagree")   │
│   3 actuals.py         freeze pre-action baselines ONCE (baselines.json);│
│                        backfill actual units/net-rev/OSA/SOV for prior   │
│                        weeks from the fresh fact_table (never overwrite) │
│   4 killswitch.py      ONLY applied cut/reinvest cells: confounder check │
│                        first (OSA/SOV −10% ⇒ excused) → strike if units  │
│                        miss >5% AND net-rev negative → 2 strikes ⇒       │
│                        auto-REVERT + 4-wk freeze; portfolio drift brake  │
│                        (hit-rate <60% over ≥30 cells ⇒ block new cuts)   │
│   5 seasonality.py     festival windows: exclude from waste scoring,     │
│                        relax budget cap                                  │
│   6 guardrail.py       glide ≤3 ppt/wk; revenue-protective revert;       │
│                        budget cap GREEN/AMBER/RED                        │
│   7 append_history     log predictions → DISCOUNT_PLAN/tracker_history   │
│   8 apply_execution_log  only KAM-confirmed cells count for scoring      │
│   9 scorecard.py       hit rate, pred-vs-actual R², MAPE, revenue bias,  │
│                        cumulative REALIZED savings                       │
│  10 workbook/readout   WEEKLY_TRACKER.xlsx + WEEKLY_READOUT.md +         │
│                        execution_log_template.csv (for the KAM)          │
└───────────────┬───────────────────────────────────────────────────────────┘
                ▼
┌────────────── LAYER 4 · HUMANS ──────────────────────────────────────────┐
│ YOU: read readout (REVERT/ALERT first) → approve  (15 min)               │
│ KAM/brand team: apply on Blinkit portal → return execution_log.csv Y/N   │
│ Next Monday: their actions become data; the loop closes                  │
└───────────────────────────────────────────────────────────────────────────┘
```

**Why two decision engines?** Engine #1 is *causal and per-cell* (is this cell's discount wasted, after controlling for stock-outs, ads, competition, momentum, seasonality — confirmed by DML?). Engine #2 is *portfolio and cross-price* (if I cut this cell, do its siblings absorb the volume? What does the whole portfolio gain?). Each can be fooled in a different way; a cut executes only when **both** say yes (51 of 63 currently agree; 12 held for testing). This is deliberate redundancy — the same reason aircraft have two independent instruments.

---

# 2. Data contracts — the files that matter, exactly

| File | Grain / key columns | Produced by | Consumed by | Purpose |
|---|---|---|---|---|
| `input_data/*_BLINKIT_RCA.csv` | product×city×day, 29 cols (Offtake Qty, Wt. Discount %, MRP, Selling Price, Wt. OSA %, Ad SOV, Category…) | you (portal export) | stage1 | raw truth, all brands |
| `input_data/MY SKU.csv` | 89 rows: Product ID, Title, Grammage, Brand | you (static) | stage1 | own-brand allowlist |
| `v4_outputs/<run>/fact_table.csv` | cell×day + stable_mrp, selling_price, discount_pct_actual, is_regular_day, is_oos_day, festival flags | stage2 | everything downstream | the cleaned single source of truth |
| `<run>/plan/all_cells.csv` | one row per cell: bucket, confidence, cur_disc, tgt_disc, cur/tgt units, net_gain_mo, decision_reason | discount_plan.py | tracker, pricing engine | engine #1's full verdict |
| `<run>/plan/dml_results.json` | per category: θ, SE, waste?, ₹save | dml_estimate.py | validate_plan (C8), reinvest | causal confirmation |
| `DISCOUNT_PLAN/pricing/agreement.csv` | 585 rows: cell_id, pricing_action (cut/raise/hold), agree_with_cut (bool) | pricing_engine | tracker `apply_agreement` | the two-engine handshake |
| `DISCOUNT_PLAN/pricing/gates.json` | pooled R², wMAPE, bias, band checks, per-category coverage | elasticity module | you (trust check) | model quality certificate |
| `DISCOUNT_PLAN/tracker_history.csv` | cell×week: pred_net_rev_delta, actual_*, applied, week_action, strikes, cell_status, baselines | tracker | scorecard, kill-switch | the register — the system's memory |
| `DISCOUNT_PLAN/baselines.json` | cell → frozen pre-action baseline (net-rev/units/OSA/SOV, last-4-wk mean) | actuals.py (once) | backfill | the fixed yardstick actuals are measured against |
| `DISCOUNT_PLAN/execution_log_template.csv` → `execution_log.csv` | week, cell_id, action, applied Y/N | tracker emits template; **KAM fills**, save-as without `_template` | tracker | separates "model wrong" from "never applied" |
| `DISCOUNT_PLAN/WEEKLY_READOUT.md` / `WEEKLY_TRACKER.xlsx` | human-readable weekly plan | tracker | you + KAM | the decision document |
| `DISCOUNT_PLAN/competitor_features.csv` | category×city×iso-week: comp median price, avg/max/p75 discount, OSA (6,534 rows) | competitor_features.py | (pending: DML challenger pass) | competition controls |

**Why baselines are frozen:** if the "before" number re-computed each week, a cell that naturally drifts would fake wins/losses. Freeze once at action start (persisted in `baselines.json`), measure every later week against that fixed yardstick. Never overwritten — verified in code (`backfill_actuals` skips filled rows).

---

# 3. RUNBOOKS — exact execution, step by step

> All commands from the repo root. Use `python -X utf8` on Windows (the reports contain ₹ and arrows). Approximate runtimes from your actual runs.

## R0 · One-time sanity check (run once now, and after any machine/sync change)

```
git status                         # expect: clean or known doc edits only
git diff --stat                    # if code files show big deletions → SYNC DAMAGE:
git restore scripts/ stage*/       #   restore from HEAD before running anything
python -X utf8 scripts/tracker/killswitch.py        # smoke test → "All ... passed"
python -X utf8 scripts/tracker/actuals.py           # smoke test → "All ... passed"
python -X utf8 scripts/pricing/de_optimizer.py      # smoke test (needs scipy)
python -X utf8 scripts/tracker/verify_loop.py       # END-TO-END proof (~2-3 min)
```
`verify_loop.py` simulates the whole weekly cycle against a real historical week: logs predictions → writes an execution log → backfills actuals → runs kill-switch → scores. Expected: `LOOP CLOSED: YES ✓`. **Warning:** it resets `tracker_history.csv`/`baselines.json`/`execution_log.csv` — run it only before go-live or on a copy, never mid-season.
*Why:* proves the machinery end-to-end without risking a real week; catches environment breakage (like the sync truncation found on 6 July) in minutes.

## R1 · MONDAY WEEKLY RUN (~30–40 min machine time, 15 min of yours)

**Step 1 — Export & drop data (you, ~10 min).**
Export last week's RCA from the Blinkit portal (same 29-column format, Monday–Sunday window). Save as e.g. `input_data/W28_2026_BLINKIT_RCA.csv`. Do not delete old months — elasticity needs long history.
*Why weekly, not monthly:* actuals must land within days of the action, or the kill-switch reacts a month late.

**Step 2 — Refresh the fact table (~10–20 min).**
```
python -X utf8 pipeline.py
```
Watch for: `[stage1] rows after own-brand filter`, `[stage2] fact_table written`, warnings about columns/categories. A new `v4_outputs/<timestamp>/` appears. *Why:* the tracker's actuals-backfill needs last week's cleaned rows; stages 3–8 also refresh diagnostics but the **decision models are NOT refit weekly** (that's R3) — separating the fast execution cycle from the slow learning cycle, exactly PepsiCo's cadence split.

**Step 3 — Run the tracker with actuals (~2–5 min).**
```
python -X utf8 scripts/tracker/weekly_tracker.py --actuals v4_outputs/<NEW_RUN>/fact_table.csv
```
What happens, in order (and what the console tells you): backfills actuals for every open prior week (`actuals filled: N`); kill-switch evaluates **only applied cut/reinvest cells** (`REVERTS: …` if any); agreement gate holds any cut engine #2 disputes (`engines disagree — test first`); seasonality (festival weeks excluded from waste-scoring); guardrail (glide ≤3 ppt, budget status GREEN/AMBER/RED); appends this week's predictions (auto week label W2, W3…); scores only KAM-confirmed cells; writes the three outputs.
Flags you may add: `--budget_pct 0.125` (a REAL budget cap — set one; default is merely current spend), `--week`/`--date` only to override the auto-derivation.

**Step 4 — Read the readout (you, 10 min).** Open `DISCOUNT_PLAN/WEEKLY_READOUT.md`. Read in this order:
1. **⚠ REVERT/ALERT block** (if present) — cells to put BACK, drift-brake status, confounded weeks. This outranks everything: reverts are the system admitting a mistake before it compounds.
2. Budget line (status + headroom).
3. This week's cuts (top moves, each a ≤3-ppt glide step with projected ₹/wk).
4. Track-record line (hit rate, realized ₹ — fills from W2 onward).
Cross-check once a month: `gates.json` all_pass, and `plan_summary.json` matches what the readout claims.

**Step 5 — Approve & hand off (you, 5 min).** Send KAM the `WEEKLY_TRACKER.xlsx` (Weekly Plan sheet) + `execution_log_template.csv`. Rule: they change ONLY listed cells, by exactly the listed step.

**Step 6 — KAM applies & confirms (them, by Friday).** They fill `applied` = Y/N (+ date, reason if skipped) and return; save as `DISCOUNT_PLAN/execution_log.csv`. *Why non-negotiable:* unapplied ≠ model miss. Without this file the scorecard refuses to grade (honest-by-design: `scored cells: 0`).

## R2 · 4-WEEKLY MODEL REFRESH (the learning half; ~1–2 hrs machine time)

Run after 4 new weeks of data, in this exact order (each step feeds the next):
```
python -X utf8 pipeline.py                                   # 1 fresh fact table
python -X utf8 scripts/analysis/discount_plan.py             # 2 refit buckets/cuts
python -X utf8 scripts/analysis/dml_estimate.py              # 3 causal re-confirmation
python -X utf8 scripts/analysis/validate_plan.py             # 4 gates C1–C8 (MUST pass)
python -X utf8 scripts/analysis/build_report.py              # 5 regenerate PLAN/DATA_GAPS/SPEC
python -X utf8 scripts/pricing/pricing_engine.py             # 6 refit elasticities + agreement.csv
python -X utf8 scripts/diagnostics/proof_loop.py             # 7 backtest: did last 8 wks predict?
git add -A && git commit -m "refresh: <date> models + plan"  # 8 audit trail
```
**Acceptance discipline (champion/challenger — currently manual, your job):** the new model set is adopted ONLY if (a) validate_plan passes all gates, (b) proof_loop's out-of-time accuracy is no worse than the previous refresh, (c) elasticity gates hold. If it fails, keep operating on the previous run's plan (the tracker reads the latest *valid* `plan/all_cells.csv` — just don't leave a failed run as the newest folder; delete or rename it). Also review **model-miss cells** (kill-switch strikes) — they are your list of "where assumptions failed."
*Why 4-weekly:* weekly refits chase noise; quarterly refits go stale. Four weeks adds ~2,300 cell-weeks of signal — enough to matter, PepsiCo refreshes promo models on the same order.

## R3 · QUARTERLY MAINTENANCE (30 min)
Update `scripts/tracker/seasonality.py` festival calendar (next 2 quarters, approximate windows are fine); revisit `--budget_pct`; populate/refresh `STRATEGIC_SKUS` in `v4_config.py` (hero SKUs never auto-cut); review DATA_GAPS.md — decide which gap to close next quarter; archive old `v4_outputs/` runs (keep last 6 + any run referenced by tracker history).

## R4 · GEO-TEST PROTOCOL (when the readout or agreement gate says "test first")
1. Pick from the queue: `test_unlock_list.csv` order (it is already expected-value-ranked), plus any "engines disagree" holds, plus reinvest candidates.
2. Design: one category per test; rank its cities by volume+trend, pair similar ones, coin-flip each pair into test/control (≥2+2). Write the decision rule BEFORE starting (e.g., "DiD net-rev ≥ 0 ⇒ roll out; < −2% ⇒ revert, tag model-miss").
3. Register it: add a row to a simple `DISCOUNT_PLAN/TEST_REGISTER.csv` (test_id, hypothesis, cells, start, end, rule, status) — create it on first use; this file is currently manual by design.
4. Run 3 weeks. The tracker keeps logging those cells automatically; control cities are simply cells you did not change.
5. Measure difference-in-differences: (test_after − test_before) − (control_after − control_before), using `tracker_history.csv` actuals. Validity check first: OSA/Ad-SOV moved <10% on both sides (else extend a week).
6. Act per the pre-written rule; feed the result into the next R2 refresh (the test weeks are now training data).
Cap: 3–4 concurrent tests, one per category, ≤10% of weekly discount spend exposed.

## R5 · COMPETITOR CHAMPION/CHALLENGER PASS (planned next; do NOT bolt on)
Fit Model B = engine #1 + competitor controls (`competitor_features.csv`: comp median price, avg/max discount per category×city×week) beside untouched Model A. Adopt B only if: out-of-sample R² ≥ 0.75 holds, C1–C8 + DML re-confirm pass, competitor coefficients have sane signs. Deliverable = delta report: which cells change bucket, how much of the ₹6.98L survives. Run it as its own session; the weekly loop does not wait for it.

---

# 4. Decision rules — every threshold in one table

| Rule | Value | Where | Why this value |
|---|---|---|---|
| Weekly glide step | ≤ 3 ppt | guardrail R1, de_optimizer bounds | small enough to revert cheaply; big enough to reach targets in ~12 wks |
| Revenue-protective revert | scaled pred Δ < 0 ⇒ hold | guardrail R2 | never take a step the model itself predicts loses money |
| Budget cap | `--budget_pct` (set it!); default = current ratio | guardrail R3 | blocks reinvest increases when over cap; never invents cuts |
| Budget status | GREEN ≤95% · AMBER 95–105% · RED >105% of cap | guardrail | early-warning bands |
| Kill-switch strike | actual units < pred×0.95 AND actual net-rev Δ < 0 | killswitch (`VOLUME_DROP_TOLERANCE_PCT=5`) | both legs required: a cheap unit-miss that still makes money is not a failure |
| Confounder excuse | OSA or Ad-SOV < baseline×0.90 that week | killswitch, first check | an out-of-stock week says nothing about the discount |
| Revert | 2 consecutive strikes → restore discount, freeze 4 wks | killswitch | one bad week is noise; two is a pattern; freeze stops thrashing |
| Drift brake | hit-rate < 60% over ≥ 30 scored cells → block NEW cuts | killswitch | if the engine is mis-aiming portfolio-wide, stop rollout, force retrain |
| Cut eligibility | bucket = c_waste_cut AND `reliably_waste` (CI) AND engine #2 `agree_with_cut` AND not STRATEGIC_SKU | discount_plan + agreement | quadruple lock before any cut executes |
| Waste test | optimistic CI edge of marginal β still below break-even 1/(100−d) | discount_plan | only bank what survives the *favourable* reading |
| Reinvest test | pessimistic CI edge clears break-even + headroom > 1 ppt | discount_plan | symmetric honesty for spending more |
| Elasticity gates | pooled R² ≥ floor, wMAPE < 0.40, |bias| < 10%, own ∈ (−2.5, 0) | gates.json | PepsiCo's published acceptance bands |
| DML confirmation | θ + 1.96·SE < break-even per cut category | dml_estimate → C8 | cuts must survive nonlinear-confounder stripping |
| DE constraints | revenue ≥ 98% baseline; ladder ₹/kg; PPP thresholds; box 0–45% | de_optimizer | portfolio safety fences |
| Festival weeks | excluded from waste scoring; budget +50% allowance | seasonality | planned festive discounts are strategy, not waste |
| Test decision | pre-registered DiD rule, 3 weeks, ≥2+2 cities | R4 | evidence, not vibes |

---

# 5. The learning loop, explained mechanically

```
Week N   plan (pred_net_rev_delta per cell)  ──►  tracker_history (open rows)
Week N   KAM applies subset                  ──►  execution_log (applied=Y)
Week N+1 fresh export                        ──►  actuals backfilled vs FROZEN baseline
         scorecard: hit-rate · MAPE · bias · cumulative REALIZED ₹
         killswitch: strikes → revert/freeze · drift → block new cuts
Week N+4 retrain on all data incl. test weeks; model-miss cells reviewed;
         champion kept unless challenger beats it out-of-sample
```
Objectives mapped: prediction accuracy (**"compare predicted vs actual"**) = scorecard; assumption failure (**"identify where assumptions failed"**) = strike/model-miss tags + confounded-week excusals; learning (**"update recommendations automatically"**) = R2 refresh regenerating all_cells + agreement, which next Monday's tracker consumes automatically. The system's own KPI set: hit rate (target: trending to PepsiCo's ~85% acceptance-quality), decision-model MAPE at 3-ppt bins, revenue bias (±, watch for systematic over-promise), cumulative realized ₹ vs the ₹6.98L/mo claim, % recommendations applied (ops health), reverts per month (model health).

---

# 6. Failure modes & troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `SyntaxError` importing killswitch/tracker | sync-truncated files (seen 6 Jul) | `git status` → `git restore <files>`; re-run R0 smoke tests |
| `No all_cells.csv — run discount_plan first` | tracker before analysis chain | run R2 steps 1–2 |
| Readout says `scored cells: 0` forever | `execution_log.csv` missing/not renamed from template | get KAM file in place; check `applied` values are Y/N |
| Same week silently skipped | week label already in history (idempotent append) | intended: safe re-runs; to redo a week, delete its rows from tracker_history first |
| Everything marked confounded | OSA collapse (platform stock issue) | correct behavior — fix availability before judging discounts |
| Drift brake ON immediately | scoring unapplied/hold cells (pre-fix behavior) | fixed at HEAD (commit 82ea05a); verify killswitch smoke test |
| Gates fail after refresh | thin/odd new data; category churn | keep previous model (champion stays); inspect `coverage` per category in gates.json |
| DE `groups_failed` > 0 | a category×city group with degenerate data | check that group's baseline_df rows; usually 1-SKU groups — safe to ignore if small |
| Two engines disagree a lot | expected on confounded categories | that's the design: those cells route to R4 tests, not to cuts |
| Festival week looks terrible | scoring not excluded? check seasonality calendar covers the date | update calendar (R3); confounded weeks don't strike anyway |

---

# 7. Glossary (technical terms used above)

**Cell** SKU×city, the decision unit. · **stable_mrp** 90th-percentile MRP per SKU-grammage — a de-noised list price. · **is_regular_day** non-festival, non-OOS, non-outlier day — the only rows models train on. · **Bucket** engine #1's diagnosis: a_stock (fix availability, don't cut), b_competitive (share loss — cutting may accelerate), c_waste_cut (reliably wasted discount), e_reinvest (discount reliably pays), f_monitor (uncertain). · **Break-even β** 1/(100−d): the demand slope where one more discount point exactly pays for itself. · **DML (Double ML)** cross-fitted gradient-boosting residualization; removes nonlinear confounding before estimating the discount effect (Neyman-orthogonal θ, cluster-robust SE). · **Posterior SD** the Bayesian uncertainty band around an elasticity; wide = act only via test. · **DE (differential evolution)** population metaheuristic for non-convex objectives (the PPP steps make the surface discontinuous — gradient methods stall). · **PPP** psychological price points (₹49/99/199…) — small demand steps at thresholds. · **Ladder** bigger pack must be cheaper per kg. · **Glide** move in ≤3-ppt weekly steps so every move is cheaply reversible. · **Frozen baseline** the fixed pre-action reference actuals are compared to. · **Strike / freeze** kill-switch bookkeeping: 2 strikes = revert, then 4-week cooling-off. · **Drift brake** portfolio-level stop on new cuts when live hit-rate collapses. · **DiD** difference-in-differences: (test change) − (control change) = causal effect. · **Champion/challenger** new model replaces old only by beating it out-of-sample. · **Agreement gate** both engines must independently endorse a cut before execution.

---

# 8. What is still manual / planned (so this manual stays honest)

1. **Budget allocator** (Objective 3's full version): "spend exactly X% of baseline revenue, allocated by marginal ROI" as a DE constraint + greedy-waterline cross-check — planned build.
2. **Marginal-ROI ladder report** per cell (the Objective-1 proof artifact for brand presentations) — planned.
3. **Champion/challenger automation** — currently the R2 acceptance discipline is you comparing proof_loop numbers by hand.
4. **Test register** — a CSV you maintain manually until test volume justifies tooling.
5. **Competitor pass** — R5, its own session.
6. **Your inputs:** real `--budget_pct`, `STRATEGIC_SKUS`, weekly export ritual, KAM's Friday file.

One-line summary of the whole operation: **Monday: data in, plan out. Friday: KAM's Y/N in. Every 4 weeks: models re-earn their job. Every quarter: calendars and budgets refreshed. Every claim: gated, agreed by two engines, glided in 3-point steps, watched by a kill-switch, and graded by the register.**
