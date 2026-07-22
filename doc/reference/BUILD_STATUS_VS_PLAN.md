# Build Status vs Plan
## FINAL AUDIT — 6 July 2026, 23:00 · **Completion: 76/100** (machine ≈94% built; remaining points = operating it)

New since last check (both verified): **`2f40488` budget allocator + marginal-ROI ladder** — Objectives 1 & 3 delivered on the shared demand kernel; `roi_ladder.csv` (11,115 rows: every cell's discount steps → units, net-rev, spend, marginal ROI, elbow flag) + waterline allocation at 10% of baseline revenue with an honest "directional, not execution plan" caveat. **`12b1e66` competitor champion/challenger** — Model A untouched vs Model B (+ competitor avg discount), pre-registered rule; verdict: KEEP A, out-of-sample R² 0.781 vs 0.762; savings survive ₹6.98L → ₹6.64L (−5%) with competition controlled; competition is NOT a material confounder; 3 cells reclassified as competitive defense (24055_500g Bangalore & Hyderabad, 5793_500g Bangalore) — **action: hold these 3 out of the cut list / route to test**. Reusable harness re-runs at every 4-weekly retrain.

**Remaining (nothing left to build):** live actuals + scorecard (starts with W2's export) · KAM Y/N ritual · STRATEGIC_SKUS hero list (still `[]`) + confirmed budget % · TEST_REGISTER + first geo-tests (Dal + the 3 reclassified cells are the natural first wave) · optional: auto-trigger for the retrain ritual · verify locally that tracker_history shows 585 W1 rows (my synced view shows 494 — same sync-staleness pattern as before) and commit the pending doc edits.

---
## PREVIOUS RE-VERIFICATION — 6 July 2026, late night (verified against git HEAD, commits `82ea05a` + `6fd3f1e` + `74d7bb0`)

### Confirmed DONE (independently verified, not just claimed)

| Item | Evidence |
|---|---|
| **Issue A — kill-switch scope** | `killswitch.evaluate` now judges only applied cut/reinvest cells (eligibility filter, backward-compatible fallback); unacted holds excluded from strikes AND drift denominator. HEAD version passes its full smoke test in my run. Commit note verifies: 522 holds no longer trip the drift brake (was DRIFT BRAKE ON + 31 phantom strikes). |
| **Issue B — execution-log template** | `write_execution_log_template()` in weekly_tracker; `DISCOUNT_PLAN/execution_log_template.csv` generated for the KAM. |
| **Issue C — readout reconciled** | New readout covers all 585 cells (cut 51 · hold 534) and reflects agreement gating. |
| **Engine wiring (two-engine agreement rule)** | Producer `_write_agreement` in pricing_engine → `agreement.csv` (585 rows); consumer `apply_agreement` in tracker — a waste-cut executes only if BOTH engines agree; 51/63 agreed, 12 held "engines disagree — test first". Float-safe keys; backward compatible. |
| **Run-stamped pricing outputs** | `DISCOUNT_PLAN/pricing/history/<run>/` — no more overwrites. |
| **Quality fixes** | Glide: out-of-box cells now WALK ≤3ppt/week (no more 4.05 snap). Collinearity: sibling regressor zeroed at row level for single-SKU rows (no more own==cross artifacts). Float product IDs fixed in all writers. |
| **Clip problem solved properly** | New `elasticity_bayes.py` (primary path, `elasticity_hier` as fallback): informative negative prior + hierarchical shrinkage, **no hard clip**; median own −1.01 ± 0.76 posterior SD; report now honestly states **19/19 categories low-confidence → act only via live test, never bank**. Materially more honest than the old clipped version. |
| **Reinvest surfaced** | Pricing engine now reports DML-confirmed reinvest headroom (Oil/Salt) instead of wide-band guesses. |
| **Git history** | Clean, well-messaged commits — the audit trail the roadmap asked for. |

### ⚠️ New issue found: sync truncation in this folder copy
The mounted "- Copy" folder shows **4 files truncated mid-file vs git HEAD** (killswitch.py ends in an unterminated string at line 614; weekly_tracker.py, pricing_engine.py, de_optimizer.py also short by ~700 lines total). HEAD compiles and passes tests — the damage is in the synced copy, not the commits. **Action: on the machine where you actually run the code, do `git status` + `git diff --stat`; if those files show deletions there too, `git restore` them before Monday's run** (the tracker would crash on import otherwise). Also: `tracker_history.csv` shows 494 W1 rows in this view vs 585 expected — likely the same sync staleness; confirm locally. Many other "modified" files in git status are just BOM/line-ending noise — add a `.gitattributes` (`* text=auto`) and commit the real doc updates (DATA_GAPS, MEASUREMENT_SPEC, PLAN).

### Still planned, not yet built (in priority order)

1. **Budget allocator (Objective 3)** — "10% of baseline revenue" constraint mode in `de_optimizer` + the greedy marginal-ROI waterline cross-check. The one genuinely new build remaining.
2. **Ladder/marginal-ROI table report (Objective 1 proof artifact)** — per product & product×city: each discount step → units, net revenue, spend, marginal ROI, elbow flag.
3. **Retrain cadence + champion/challenger (Objective 4's learning half)** — 4-weekly refit; challenger replaces champion only if backtest improves; model version stamped in outputs.
4. **Test machinery (Gaps 7/8)** — test register sheet, city-split assignment helper, DiD readout; reinvest pilots executed through it once actuals flow.
5. **Competitor features → DML model** — deliberately deferred; do as an isolated champion/challenger pass (see §below).
6. **Your inputs still pending:** hero SKUs in `STRATEGIC_SKUS`, a real budget % (`--budget_pct`), weekly export ritual + KAM Y/N.

### Competitor-integration pass — the agreed plan
The deferral reasoning is correct: it touches the validated DML model behind the ₹6.98L and must not be bolted on. Do it as **champion vs challenger, never an edit**: (1) fit Model B = current model + competitor controls (comp median price, avg/max discount per category×city×week from `competitor_features.csv`) alongside untouched Model A; (2) pre-registered acceptance rule — B replaces A only if out-of-sample R² holds (≥0.75), all C1–C8 gates + DML re-confirmation pass, and competitor coefficients have sane signs (their discount up → our units down); (3) deliverable = a **delta report**: which cells change bucket, how much of the ₹6.98L survives with competition controlled, which "waste" cuts turn out to be competitive defense; (4) expect the number to move — possibly down — and that is the point: cheaper to learn in a model pass than at the register. It does NOT block the weekly loop — start the Monday ritual regardless; adopt B (if it wins) at the first 4-weekly retrain.

---

# ORIGINAL STATUS — 6 July 2026 (evening check, superseded above)

Two plans exist: the **PepsiCo blueprint** (new pricing engine) and the **tracker roadmap/verification** (weekly loop). Verdict for each, verified against actual code and outputs.

---

## A. New pricing engine (`scripts/pricing/`) vs the PepsiCo blueprint

### Built and verified ✓

| Blueprint component | Status | Evidence |
|---|---|---|
| Weekly panel + faithful feature engineering (±8-wk regular price, 5%-below promo flag, recency/volume weights) | ✓ | `pricing_panel.py`; smoke test passes in my run |
| Hierarchical elasticity with partial pooling (additive grand+category+size+city decomposition, ridge as Bayesian stand-in, bootstrap SD as posterior-SD stand-in, negative-own enforcement) | ✓ | `elasticity_hier.py`; honest docstring about the approximation |
| Validation gates before optimizer use (R², wMAPE, bias, band checks) | ✓ | `gates.json`: R² 0.892, wMAPE 0.255, bias 0.066 — all_pass true, 19 categories |
| DE optimizer: KPI choice (revenue/volume/NRW/share), penalty constraints, hard glide bounds, price-per-kg ladder + deterministic ladder repair, PPP thresholds, multi-seed ensemble, honesty clamps (cuts only credited when reliably elastic; capped exp; no extrapolation) | ✓ | `de_optimizer.py`; category×city decomposition in `pricing_engine.py` makes 526-dim tractable |
| /simulate what-if with SHARED demand kernel (never re-solves, never diverges from optimizer) | ✓ | `whatif.py` imports `build_problem`/`demand_model` — exactly the blueprint's §19 rule |
| Cannibalization honesty check of the existing ₹6.98L cut list | ✓ | verdict: cuts hold at portfolio level, +1.67% revenue, 83 sibling cells absorb volume — this answers the question the per-cell tool couldn't |
| Report + artifacts | ✓ | `DISCOUNT_PLAN/pricing/`: elasticities.csv, cross_price.csv, pricing_reco.csv, gates.json, PRICING_PLAN.md |

Sensible local adaptations (not gaps): no Gurobi/cloud/STAN (laptop-scale), no conjoint priors (no data), no competitor follow-ratios (single-brand scope), no profit/margin KPI (no COGS — consistent with the deferred cost decision).

### Quality issues found in the build ⚠️

1. **53% of cells are clipped to zero elasticity, and the report hides it.** 279 of 526 cells have own_elast exactly 0 (raw category slopes were positive in 8 of 19 categories — Honey +3.8, Sooji +5.0, Poha +1.2, etc. — and got forced to 0); 28 more sit clipped at the −2.5 floor. Only 219 cells (42%) carry a real interior estimate. PRICING_PLAN.md says "all in the (−2.5, 0) sanity band" — true only because of clipping. The gate now measures the clip, not the model. **Fix logic:** report % clipped per category; treat clipped-to-0 categories as "price signal confounded — act only via test," not as estimated.
2. **Single-sibling collinearity.** Where a category-city has one SKU, sibling price falls back to own price, making the own and cross regressors identical — the fit splits arbitrarily (visible in coverage: Besan own = cross = −0.83; Honey both +3.84; Wheat-Daliya both −1.30). **Fix logic:** exclude single-SKU category-cities from cross estimation (set cross = 0 there), keep own only.
3. **Glide exceeded for out-of-range cells.** Config says ≤3 ppt/week, but max observed change is 4.05 ppt: cells whose current discount is above the 45% box get forced down to the box edge in one step (documented "box wins" choice in code). **Fix logic:** for out-of-box cells, walk to the box over multiple weeks like every other glide move.
4. **Product IDs render as floats** ("532393.0") in the report — cosmetic.

### Missing vs the blueprint (deliberate scope or still to do)

| Item | Blueprint ref | Status |
|---|---|---|
| **Wiring into the weekly loop** — nothing consumes `pricing_reco.csv`; the tracker still acts only on `discount_plan.py` buckets. Two engines can now disagree with no reconciliation rule | §2 golden paths | **MISSING — the important one.** Decide the rule: e.g., a cell may only be cut if BOTH engines agree; pricing_reco's "more discount" cells feed the reinvest pilot list |
| Sequential prior updating between cycles (old posterior → new prior) | §7.3 | Missing — each run refits from scratch |
| Run versioning — `DISCOUNT_PLAN/pricing/` is overwritten every run; no run-stamped history | §13 audit | Missing |
| Tier ordering (Eq.35), pricing lines (Eq.36), portfolio avg-price-change bands (Eq.37–39) | §6.2 | Not built (ladder + glide + revenue floor only). Acceptable at this scale; note it |
| Competitor data in the model (you already mined `competitor_features.csv` — 6,534 rows) | §4 / Gap 10 | Still not fed into ANY model (neither discount_plan nor pricing engine) |
| UI / API / orchestration / scheduler / BI | §2, §11–13 | Not built — expected at laptop scale; the .md/.csv outputs are the stand-in |

---

## B. Tracker roadmap — carry-over items, re-checked: **all still open**

| Item | Status |
|---|---|
| **Issue A — kill-switch judges unacted cells** (drift brake will trip on 438 zero-prediction holds the first time actuals arrive; phantom reverts) | **STILL OPEN** — killswitch.py unchanged, no applied/action filter |
| **Issue B — no execution-log template generated for the KAM** | STILL OPEN — weekly_tracker only reads the file if it exists |
| **Issue C — readout (63 cuts/585 cells) disagrees with history (53 cuts/492 rows)**; re-run tracker end-to-end and reconcile | STILL OPEN |
| Gap 7 reinvest pilot loop / Gap 8 A/B machinery / Gap 9 retrain cadence | Still pending (waiting on actuals — acknowledged) |
| Stale DATA_GAPS.md / MEASUREMENT_SPEC.md; duplicate-week silent skip; STRATEGIC_SKUS empty; real budget % | Still open |

---

## Priority order

1. Fix **Issue A** (kill-switch scope) — it will misfire on the first real actuals, before anything else matters.
2. Fix pricing-engine quality items 1–3 above (clip transparency, collinearity, glide) — cheap, and they protect the credibility of the new numbers.
3. **Wire the two engines together** with an explicit agreement rule + run-stamped outputs.
4. Issue B (exec-log template) + Issue C (reconcile readout).
5. Feed competitor_features into the models; then the pending Gaps 7/8/9 as actuals accumulate.
