# PepsiCo Paper Parity Report — Final

*All approaches from the PepsiCo PromoAI/PricingAI paper: what is implemented, what was adapted, what was built today, and everything this codebase has that the paper does not.*

*Generated 2026-07-07 against the full 90-item audit (`audit_result.json`) and the post-build regression gate. Every claim carries a file reference.*

---

## 1. The 30-second verdict

The paper decomposes into **90 checklist items** (32 PromoAI, 32 PricingAI, 26 validation & monitoring). **86 are in scope** for this business; **4 are deliberately excluded** because they are cloud infrastructure (Azure/Databricks, web UI, distributed compute — you run ~10 brands from one machine, no cloud). Of the 86 in-scope items: **61 were already covered before today** (13 implemented essentially as-written, 48 adapted to an equivalent that fits one platform/one brand — each adaptation justified in Section 3), **24 were built today** as 10 new modules/upgrades that closed every confirmed gap the audit found, and **1 remains an honest partial** (spike-vs-promo cross-validation — named, small, and listed in Section 2.3). One audit "gap" (price_24, tier pricing) was **refuted on inspection** — it already existed as the pack-size price ladder. Post-build regression: all 5 gates pass, the champion decision engine is bit-identical, and day-one state is intact. One distinction runs through everything below: parity means the paper's *machinery* exists and runs on your real data — it does **not** mean the numbers are register-proven. Where a new module's first honest run on real data FAILED a gate (the rolling backtest and the elasticity fit gate did), the report says so.

**Headline: 85 of 86 in-scope paper elements implemented, adapted-equivalent, or built today (13 + 48 + 24), 1 honest partial, 4 infra items excluded by design.**

---

## 2. Parity tables

Status key: **implemented** = same mechanism as the paper · **adapted** = deliberate substitute serving the same purpose (see Section 3) · **built-today** = new module closing a confirmed gap · **partial** = works, named residual remains · **excluded-infra** = out of scope by owner rule (no cloud, no web stack).

### 2.1 PromoAI parity (promo_01–promo_32)

| # | Paper element | Status | Where | Plain-English note |
|---|---|---|---|---|
| promo_01 | Data harmonization across sources | adapted | `v4_config.py:18-39`, `stage2_preparation/prepare.py:37-44`, `stage1_ingestion/ingest.py:428-436` | One retailer (Blinkit), so no multi-retailer harmonizing needed; every export column is mapped once, products keyed stably across pack sizes, costs from a master file. |
| promo_02 | Data cleaning + automated quality checks | implemented | `stage1_ingestion/validate.py:25-86`, `prepare.py:47-216` | Hard-fails on missing columns, flags implausible values, imputes, corrects price anomalies, removes outliers with an audit trail — all before any modeling. |
| promo_03 | SKU aggregation into promo groups (PPGs) | adapted | `scripts/analysis/discount_plan.py:121-135`, `scripts/pricing/elasticity_hier.py:281-313` | Instead of collapsing SKUs into groups, info is pooled at category level while decisions stay per SKU×city — same borrowing-strength idea, finer decisions. |
| promo_04 | Global pooled LightGBM demand model | adapted | `discount_plan.py:124-135`, `scripts/analysis/dml_estimate.py:49-52` | Pooled robust regression with fixed effects instead of a GBM — because the cut rules need confidence intervals; gradient boosting is still used where it helps (DML nuisances). |
| promo_05 | Forecast feature set (demand drivers) | adapted | `discount_plan.py:113-124`, `v4_config.py:91-118`, `pricing_panel.py:257-264` | Temporal, promotional and product features all present; adds confounders the paper lacks (OSA, ad SOV, competitor share); shelf placement doesn't exist on Blinkit. |
| promo_06 | MAPE training objective | adapted | `discount_plan.py:135`, `scripts/experiments/experiments_mape.py:144-204`, `elasticity_hier.py:72` | Log-space + Huber robust fit gives the same relative-error/outlier-robust behavior; MAPE/wMAPE is the selection and acceptance metric. |
| promo_07 | Hyperparameter tuning on temporal hold-out | adapted | `experiments_mape.py`, `v4_config.py:74-88`, `discount_plan.py:188-216` | The few knobs that exist (lookback, outlier-z) were tuned by scripted experiments on forward time splits — documented in config with the receipts. |
| promo_08 | Feature-importance characterization | adapted | `discount_plan.py:158-166, 253-267`, `scripts/analysis/validate_plan.py:50-69` | Coefficient inspection + per-cell contribution attribution (with CIs) — a driver must be named for every cell, audited by gate C1. |
| promo_09 | Piecewise-linear demand approximation | adapted | `scripts/pricing/de_optimizer.py:308-355`, `scripts/pricing/budget_allocator.py:31-62` | No approximation needed: the optimizer evaluates the exact nonlinear demand curve directly; grids (0.25–2.5ppt) exist where ladders are needed. |
| promo_10 | Automated breakpoint selection (SLSQP + knee) | adapted | `budget_allocator.py:56-61`, `stage6_economics/economics.py:66-74` | No PWL means no breakpoints to fit; the knee-point idea survives as the marginal-ROI ≥ 1 "elbow" in the economics layer. |
| promo_11 | MILP decision variables x(p,w,r) | adapted | `de_optimizer.py:376-405` | One mechanic on Blinkit (percent discount), so the binary option choice collapses to a continuous depth per SKU×city with per-cell bounds. |
| promo_12 | Bi-stakeholder weighted objective | adapted | `de_optimizer.py:296-305, 324-327`, `v4_config.py:147` | Blinkit takes a fixed 15% commission, so retailer-vs-brand weighting is void; the sales-vs-margin trade-off is a configurable KPI switch. |
| promo_13 | Promotion exclusivity (one option per week) | adapted | `de_optimizer.py:379-405`, `scripts/tracker/guardrail.py:39-69` | Satisfied by construction: exactly one discount value per cell per week, guaranteed structurally. |
| promo_14 | Discount-fraction linking constraint | adapted | `de_optimizer.py:250, 320`, `scripts/pricing/whatif.py:64-66` | The discount IS the decision variable, so the link is the identity: price = MRP×(1−disc/100) everywhere. |
| promo_15 | Competitive discount pressure (cannibalization) | adapted | `de_optimizer.py:176-188, 268-271`, `scripts/analysis/challenger.py:45-48` | Sibling discounts are simultaneous decision variables linked by cross-price terms — each cell's demand depends on what its category siblings do. |
| promo_16 | Big-M demand activation linearization | adapted | `de_optimizer.py:308-322` | Nothing to linearize: demand is evaluated directly at every candidate solution, so the property Big-M enforces holds exactly. |
| promo_17 | Financial aggregation equations | adapted | `de_optimizer.py:296-305`, `stage6_economics/economics.py:40-74`, `validate_plan.py:115-123` | Revenue/volume/NRW/margin computed from realized demand; line items reconcile to headline within 0.5% (gate C5). |
| promo_18 | Margin conservation constraints | adapted | `de_optimizer.py:332-335`, `stage7_guardrails/guardrails.py:54-56`, `guardrail.py:143-149` | Three enforcement points: revenue floor in the optimizer, per-unit margin floor, weekly discount-spend budget cap. |
| promo_19 | Promotion frequency limits | adapted | `guardrail.py:143-176`, `budget_allocator.py:65-110`, `v4_config.py:206` | Count caps don't map to an always-on platform; the intensity-limiting purpose is served by the 12% spend cap and the ROI waterline. |
| promo_20 | Market share constraint | adapted | `de_optimizer.py:332-335`, `guardrail.py:102-129`, `discount_plan.py:243-246` | Revenue floor + protective holds + kill-switch play the protective role; share-losing cells are never cut (b_competitive bucket). |
| promo_21 | Competitor lock slots (fixed calendar) | adapted | `scripts/analysis/competitor_features.py`, `challenger.py:45-48, 107-174` | Competitors are exogenous, locked data — the observed competitor calendar conditions the model, and defense_hold blocks cuts that are really competitive defense. |
| promo_22 | Modular constraint-template library (3 categories) | **built-today** | `scripts/pricing/constraints_lib.py`, `scripts/promo/promo_calendar_milp.py`, `scripts/promo/promo_constraints.json` | NEW: financial/calendar/execution templates (duration, spacing, simultaneous-promo caps, budget, defense holds) declared per market, fail-loud on unknown names. |
| promo_23 | JSON-declarative constraints compiled to solver | **built-today** | `DISCOUNT_PLAN/pricing/pricing_constraints.json`, `constraints_lib.py` (compile_constraints), `promo_constraints.json` | NEW: business rules live in JSON, compiled to optimizer penalties / MILP rows at runtime; onboarding another brand = edit a JSON file, zero code. |
| promo_24 | MILP solver strategy (gap targets, early stop) | adapted | `pricing_engine.py:68-98`, `de_optimizer.py:432-446` | Exact category×city decomposition + DE convergence tolerance served the purpose; the literal MILP gap discipline now also exists (see val_14). |
| promo_25 | Rolling-origin backtest vs legacy benchmark | **built-today** | `scripts/validation/backtest_rolling.py`, `DISCOUNT_PLAN/validation/BACKTEST_REPORT.md` | NEW: 5 walk-forward origins, champion (honest recursive + 1-step) vs seasonal-naive and last-week benchmarks. First run: honest FAIL — see Section 5. |
| promo_26 | Optimizer validation (constraints + sensitivity) | adapted | `de_optimizer.py:513-539`, `validate_plan.py:50-161`, `scripts/analysis/unlock_estimate.py:29-52` | Every solution post-verifies its own constraints; bootstrap P(stay-cut) measures whether recommendations survive resampling. |
| promo_27 | Structured scenario review, deviation diagnosis | adapted | `discount_plan.py:344-359`, `challenger.py:107-174`, `weekly_tracker.py` (execution log) | Every cell names its decision reason; challenger diffs diagnose competitor-driven deviations; the execution log tracks human overrides. |
| promo_28 | Retraining cadence with validation gates | adapted | `v4_config.py:88`, `challenger.py:174`, `validate_plan.py` | Rolling 180-day retrain each run; every retrain must clear the identical C1–C8 gate; challenger adoption is pre-registered. |
| promo_29 | Negotiation loop with multi-objective scenario menus | **built-today** | `scripts/pricing/scenario_menu.py`, `DISCOUNT_PLAN/pricing/SCENARIO_MENU.md` | NEW: 8 optimized scenarios (objective × constraint tightness) side by side with full financials; counterpart feedback re-runs the menu. |
| promo_30 | What-if simulation of manual overrides | implemented | `whatif.py:72-207` | Manual discount edits → full portfolio impact including cannibalization, arithmetically identical to the optimizer's beliefs. |
| promo_31 | Azure/Databricks deployment | excluded-infra | — | Out of scope by owner rule; the methodological residue (config externalization, run versioning) exists locally. |
| promo_32 | Web UI / scenario interface | excluded-infra | — | Out of scope; the capabilities a UI would expose exist as code/config + Excel workbook + markdown readouts. |

### 2.2 PricingAI parity (price_01–price_32)

| # | Paper element | Status | Where | Plain-English note |
|---|---|---|---|---|
| price_01 | Three-source input orchestration | adapted | `ingest.py:59+, 428-436`, `v4_config.py:18-39, 146-149` | Sales/pricing/distribution exports + master cost file; the third source (conjoint surveys) doesn't exist for this business — replaced deliberately (see price_09). |
| price_02 | Regular-price derivation + promo flagging | implemented | `pricing_panel.py:61-62, 161-175, 257-264` | Faithful to the paper: regular price = max over a centered ±8-week window; promo = price >5% below it. |
| price_03 | Semi-regional aggregation, weighted promo threshold | adapted | `pricing_panel.py:147-158, 230-245, 264` | City IS the semi-region on Blinkit; day→week rollup uses volume-weighted prices, so promo classification is implicitly volume-weighted. |
| price_04 | Multiplicative Bayesian hierarchical volume model | adapted | `scripts/pricing/elasticity_bayes.py:12-17, 45-59, 101-107` | Log-log model with genuine Bayesian posteriors in exact closed form (conjugate, after FWL residualization) — no sampler needed. |
| price_05 | Additive own-elasticity decomposition (5 components) | adapted | `elasticity_hier.py:281-313` | Additive decomposition over the attributes this catalog actually has: grand + category + size-tier + city, ridge-shrunk. |
| price_06 | Multiplicative cross-price decomposition in (0,1) | **built-today** | `scripts/pricing/cross_price_v2.py`, `DISCOUNT_PLAN/pricing/CROSS_PRICE_V2.md` | NEW: similarity-weighted (family × size × price-tier) split of cross mass, each factor bounded in (0,1). First run: decomposition lost on holdout — shipped the safe uniform split (see Section 5). |
| price_07 | Additional demand drivers | adapted | `elasticity_bayes.py:67-72`, `elasticity_hier.py:161-190` | Promo flag, availability/distribution, seasonality dummies all in the regression. |
| price_08 | Recency + volume observation weighting | implemented | `pricing_panel.py:60, 266-274` | Exponential recency decay (8-week half-life) × volume importance, multiplied into the regression weights. |
| price_09 | Conjoint-informed priors with per-dimension confidence | adapted | `elasticity_bayes.py:25-26, 88-99` | No conjoint data exists; the prior-blending mechanism is fed from domain priors (own ~ N(−1.0, 0.8²)) with dimension-specific confidence. |
| price_10 | Softmax sign-consistency penalty | adapted | `elasticity_bayes.py:25, 88-99`, `elasticity_hier.py:362, 391-393` | Sign discipline via informative negative prior (Bayes engine) and negative-own enforcement (hier engine) — same guardrail, different device. |
| price_11 | STAN + ADVI variational inference | adapted | `elasticity_bayes.py:45-59` | Model restructured so the posterior is exact and instant (conjugate normal-normal) — no approximate sampler required. See Section 3. |
| price_12 | Full n×n cross-price elasticity matrix E | **built-today** | `cross_price_v2.py`, `DISCOUNT_PLAN/pricing/cross_price_v2.csv` | NEW: assembled matrix over 84 SKUs × 11 cities (585 own-diagonal, 2,594 cross pairs, 585 competitor columns); cross-category entries are honest structural zeros. |
| price_13 | Three-stage elasticity validation thresholds | implemented | `elasticity_hier.py:71-74, 442-496` | Explicit fit gates (R², wMAPE, bias), business-magnitude checks, plus the stricter downstream gate built today (val_02). |
| price_14 | Differential Evolution optimizer via SciPy | implemented | `de_optimizer.py:37, 432-446` | Literally the paper's choice: scipy differential_evolution over bounded discount vectors with penalty constraints. |
| price_15 | Ensemble of seeded DE runs, varied configs | **built-today** | `de_optimizer.py:505-512` (ROBUST_GRID) | NEW: 8 distinct DE configurations (mutation/recombination/popsize/init vary per run, incl. Sobol inits); run 0 is bit-identical to the old champion behavior. |
| price_16 | User-selected KPI objective (all 6 KPIs) | **built-today** | `de_optimizer.py:422-442` | NEW: profit, margin and the revenue/profit 'combo' blend added to revenue/volume/nrw/share — the full paper menu, exercised in the scenario menu run. |
| price_17 | Decision variables: group prices, %/absolute modes | adapted | `de_optimizer.py:27, 250, 379-405` | Percentage mode re-parameterized as discount on a fixed MRP — the owner-sanctioned decision variable on this platform (see Section 3). |
| price_18 | Competitor price-follow mechanism | **built-today** | `cross_price_v2.py` (§3 of its report) | NEW: measured competitor-price elasticity per cell (rival price from RCA data — the fact-table competitor column is 100% empty, stated honestly), giving the optimizer a competitor-interaction input. |
| price_19 | Exponential volume function, log-log cross terms | implemented | `de_optimizer.py:220-293` | Exactly the paper's Eq 22a/24 structure: V = q0·exp(own·Δln p + Σ cross·Δln p_sibling + PPP term); V=V0 at baseline by construction. |
| price_20 | Psychological price points (step demand shifts) | adapted | `de_optimizer.py:62-100, 273-279` | Configurable ₹49/99/…999 thresholds with a ±3% effect entering the volume exponent as a differenced term. |
| price_21 | Full financial KPI chain (incl. profit, margin) | **built-today** | `de_optimizer.py:296-305 + KPI additions`, `scenario_menu.py` (profit column) | NEW: rupee profit and margin now computed in the optimizer/simulator chain from COGS+commission+fulfillment; profit levels flagged as directional while COGS is a 50% proxy. |
| price_22 | KPI bound constraints (any KPI, both sides) | **built-today** | `constraints_lib.py` (kpi_bounds), `pricing_constraints.json` | NEW: declarative min_frac/max_frac bounds vs baseline for revenue/volume/nrw/share/spend/profit/margin — the paper's uniform template, previously only a hardcoded revenue floor. |
| price_23 | Price ladder constraint (price-per-weight hierarchy) | implemented | `de_optimizer.py:203-211, 342-353, 454-485` | Bigger packs must be cheaper per gram; enforced as penalty + deterministic post-hoc repair + honest residual reporting. |
| price_24 | Tier pricing constraint (premium ≥ value) | implemented *(audit gap refuted)* | `de_optimizer.py:201-211, 342-353, 454-462` | The auditor flagged this missing; verification proved it exists as the pack-size ladder — the only real "tiers" in a single-brand portfolio. |
| price_25 | Pricing line constraint (equal increments) | **built-today** | `constraints_lib.py` (pricing_line) | NEW: within a pack family, every member's absolute price move must be equal within a rupee tolerance — no pack drifts alone. |
| price_26 | Portfolio average price-change bound | **built-today** | `constraints_lib.py` (portfolio_avg_band) | NEW: declarative band on the portfolio's average percentage price change per weekly cycle (Eq 37). |
| price_27 | Volume-weighted avg price-change bound (endogenous weights) | **built-today** | `constraints_lib.py` (vw_avg_band) | NEW: the band is weighted by the OPTIMIZED volumes at candidate discounts — the weights move with the decision, exactly Eq 38-39, enforced as a soft DE penalty per the paper. |
| price_28 | Adjusted-scenario simulation with cross-price impact | implemented | `whatif.py:72-207` | Manual overrides re-simulated through the optimizer's exact kernel, cannibalization included, instant. |
| price_29 | System-level solution validation (5 pillars) | adapted | `scripts/diagnostics/proof_loop.py:37-126`, `validate_plan.py`, `unlock_estimate.py` | Backtesting, economic consistency, constraint verification, sensitivity, monitored pilot — all present in scale-adapted form. |
| price_30 | Retraining cadence + sequential prior updating | **built-today** | `scripts/pricing/prior_store.py`, `DISCOUNT_PLAN/pricing/priors.json` | NEW: refresh N's posterior seeds refresh N+1's prior, with a forgetting factor so stale certainty decays; a failed-gates run is never used to seed. |
| price_31 | Externalized business-rule configuration | adapted | `v4_config.py`, `pricing_engine.py:57-65`, + today's JSON rulebooks | Central Python config was already the pattern; the two JSON constraint rulebooks built today complete the declarative form. |
| price_32 | Cloud deployment stack | excluded-infra | — | Out of scope; everything runs locally on numpy/pandas/scipy/sklearn/statsmodels. |

### 2.3 Validation & monitoring parity (val_01–val_26)

| # | Paper element | Status | Where | Plain-English note |
|---|---|---|---|---|
| val_01 | Rolling-origin backtest vs benchmark | **built-today** | `scripts/validation/backtest_rolling.py`, `DISCOUNT_PLAN/validation/BACKTEST_REPORT.md` | NEW: 5 origins × 4-week horizon, champion vs two naive benchmarks on identical cell-weeks. Verdict on real data: FAIL on headline wMAPE — reported, not hidden (Section 5). |
| val_02 | Three-stage elasticity acceptance protocol | **built-today** | `scripts/validation/elasticity_gates.py`, `DISCOUNT_PLAN/validation/ELASTICITY_GATES.md` | NEW: hard fit/sanity/stability gates on the production matrix with exit-code semantics. First run: Stage 1 FAIL — do not bank savings from the matrix, act via live tests (Section 5). |
| val_03 | Economic-consistency review of elasticities | adapted | `elasticity_hier.py:362, 391-393`, `elasticity_bayes.py:145` | Negative-own and reasonable-cross checks automated; owner-facing reports carry the review layer. |
| val_04 | Constraint-satisfaction verification | implemented | `de_optimizer.py:517-537`, `validate_plan.py` | The solver's output is audited, not trusted: floors and ladders re-verified after every solve, violations reported. |
| val_05 | Sensitivity analysis under input perturbation | **built-today** | `scripts/validation/sensitivity.py`, `DISCOUNT_PLAN/validation/SENSITIVITY_REPORT.md` | NEW: 200-draw Monte-Carlo shake of elasticity (champion SEs), COGS ±10%, commission ±3ppt, units ±10%. First run: **0 of 63 cut cells fragile** (max joint flip 3%); saving holds at ₹706k–739k/mo around the ₹725k point. |
| val_06 | Scenario review vs historical decisions | adapted | `discount_plan.py:344-359`, `challenger.py:107-174` | Every departure from the incumbent discount names its cause; challenger diffs diagnose deviations. |
| val_07 | Pilot with override monitoring | adapted | `weekly_tracker.py:255-324` | The weekly tracker is the pilot: KAM marks applied Y/N per cell; only human-accepted cells are scored. |
| val_08 | Retraining gated by deployment-grade criteria | adapted | `validate_plan.py:33-37 + C1-C8` | Every retrain faces the identical automated gate as initial deployment, exit code 0 iff all pass. |
| val_09 | Optimization-parameter review each cycle | **built-today** | `scripts/tracker/params_review.py`, `DISCOUNT_PLAN/PARAMS_REVIEW.md` | NEW: 28 knobs snapshotted, diffed, aged against review horizons, with `--ack` human sign-off. First run surfaced two real standing warnings (Section 5). |
| val_10 | Sequential Bayesian prior updating for stability | **built-today** | `prior_store.py`, `elasticity_bayes.py` (stability gate) | NEW: seeded refreshes carry a max-shift ≤ 0.5 stability release gate; a whipsawing refresh saves as all_pass=false and is refused as the next seed. Real-data receipt: max shift 0.199, PASS. |
| val_11 | Temporal hold-out CV with MAPE (forecaster) | adapted | `discount_plan.py:188-216`, `experiments_mape.py` | Forward-looking temporal validation and scale-robust error metrics enforced throughout; no GBM by design. |
| val_12 | PWL approximation quality control | adapted | `de_optimizer.py:220-293` | Dissolved: no PWL surrogate exists because every optimizer evaluates the exact demand kernel. |
| val_13 | Configuration retention, change-triggered re-eval | adapted | `challenger.py` (pre-registered adoption), `killswitch.py` (drift brake) | The champion is frozen; it changes only via the challenger's pre-registered bar, and drift triggers a retrain signal. |
| val_14 | MILP optimality-gap targets + stagnation callbacks | **built-today** | `scripts/promo/promo_calendar_milp.py`, `DISCOUNT_PLAN/promo/promo_solver_report.csv` | NEW: HiGHS MILP with a 1% relative-gap target and per-solve time limit; every subproblem reports achieved gap, status and stop reason — the paper's gap certificate. |
| val_15 | DE robustness: parallel seeds, varied configs | **built-today** | `de_optimizer.py:505-512, 636-692` | NEW: multi-seed ensemble now varies mutation/recombination/popsize/init per run (8-config grid), keeping the best feasible solution; per-run receipts logged. |
| val_16 | Data-quality + anomaly validation in pipeline | **partial** | `validate.py`, `prepare.py:132-216`, `ingest.py:443-469` | Base checks, outlier removal with audit CSV, and event-calendar exclusion all run; honest residual: spikes are removed statistically without cross-checking whether a documented promo explains them. |
| val_17 | Recommendation acceptance rate (~85%) metric | **built-today** | `scripts/tracker/scorecard.py:267+` (acceptance_history) | NEW: weekly + cumulative + value-weighted acceptance rate from the execution log, bucketed against the paper's ~85% deployed benchmark. |
| val_18 | Business-impact measurement | adapted | `scripts/tracker/actuals.py`, `scorecard.py` | Strictly stronger than the paper's attestation: frozen pre-action baselines + realized (not predicted) savings accounting. |
| val_19 | Override simulation with quantified consequences | implemented | `whatif.py:72-207` | Planner edits come back as quantified per-cell and portfolio consequences, cross-price effects included. |
| val_20 | Closed-loop negotiation (menu → feedback → re-optimize) | **built-today** | `scenario_menu.py`, `DISCOUNT_PLAN/pricing/negotiation_log.csv` | NEW: multi-scenario menu per round; lock/opt-out/max/min feedback per cell lands in `negotiation_feedback.csv` and every scenario re-runs honoring it. |
| val_21 | Guardrails bounding every recommendation | implemented | `de_optimizer.py:332-353, 379-405`, `guardrail.py` | "Do nothing crazy" limits at both the optimizer layer and the weekly execution layer, all post-verified. |
| val_22 | Sign-consistency statistical guardrail | adapted | `elasticity_bayes.py:25, 88-99` | Sign discipline built into the prior instead of a softmax penalty — same guardrail, cleaner device. |
| val_23 | Forecasting-vs-optimization contribution analysis | adapted | `scripts/diagnostics/model_credibility_report.py` | More concrete than the paper's qualitative argument: the decision curve is graded separately from the flattering full model. |
| val_24 | Market-specific recalibration of data assumptions | adapted | `pricing_panel.py:60-62`, `v4_config.py` | Every assumption the paper says needs per-market calibration is an externalized, revisitable knob. |
| val_25 | Separate versioning of models vs optimizers | adapted | `pricing_engine.py:333-347`, timestamped `v4_outputs/<stamp>/` | Every run stamped; models iterate without destabilizing decision code (champion/challenger). |
| val_26 | Distributed parallel DE on cloud | excluded-infra | — | Out of scope; the multi-start methodology it distributes runs sequentially on one machine (val_15). |

---

## 3. Adapted, and why — the deliberate substitutions

These are not shortcuts; each was a considered trade the paper's authors did not have to make (they had cloud clusters, retailer negotiations, and multiple promo mechanics; you have one platform, ~10 brands, and one machine).

| Paper's choice | This codebase's choice | Why (one line) |
|---|---|---|
| Global LightGBM demand forecaster | Pooled Huber-robust regression + cell fixed effects (`discount_plan.py:124-135`) | The cut rules need confidence intervals (β±1.96se) to say "reliably below break-even" — a GBM point forecast can't supply them; GBMs are still used where they help (DML nuisances, `dml_estimate.py:49-52`). |
| STAN MCMC / ADVI variational Bayes | Exact conjugate Bayes after FWL residualization (`elasticity_bayes.py:45-59`) | PyMC broke the numpy stack on this machine; restructuring the model made the posterior exact and instant instead of approximate and hours-long — a strictly better answer, not a compromise. |
| Commercial MILP/NLP solver (Gurobi-class) | scipy Differential Evolution + exact category×city decomposition (`de_optimizer.py:432-446`, `pricing_engine.py:68-98`) | No license cost, no cloud; the decomposition makes 526 dimensions tractable, and the new MILP calendar challenger (HiGHS, also free) now supplies the gap certificates where they matter. |
| Base shelf price as the decision variable | Discount % off a fixed MRP (`de_optimizer.py:27, 250`) | On Blinkit the brand controls exactly one lever — the discount; MRP is printed on the pack. Same math (price = MRP×(1−d/100)), honest about what's actually executable. |
| SKUs collapsed into PPGs | Information pooled at category level, decisions kept at SKU×city (`discount_plan.py:121-135`) | With 84 SKUs the dimensionality motive for PPGs is absent; pooling gives thin cells borrowed strength while keeping finer decisions than the paper could offer. |
| PWL approximation of the demand curve | Direct evaluation of the exact nonlinear kernel (`de_optimizer.py:308-355`) | The paper approximates only because a linear solver can't ingest the GBM; a metaheuristic evaluates the true curve — zero approximation error, and the PWL quality-control machinery becomes unnecessary. |

---

## 4. Beyond the paper — what this codebase has that PepsiCo's does not

The paper is a planning system. This is a planning system **plus a causal-inference layer, a production safety system, and an honesty layer** — the things a 10-brand operator without a data-science team actually needs.

| Capability | File | Why it matters for a 10-brand operator |
|---|---|---|
| Double ML causal confirmation as a hard gate | `scripts/analysis/dml_estimate.py`, `validate_plan.py` (C8) | No cut is banked unless an independent causal method (cross-fitted, cluster-robust) separately confirms the discount is waste — two different methods must agree before money moves. |
| CI-gated "reliably below break-even" cut rule | `discount_plan.py:269-277` | Cuts happen only when the confidence interval — not the point estimate — clears break-even; statistical humility is wired into the decision, not left to judgment. |
| Champion/challenger discipline with pre-registered adoption | `scripts/analysis/challenger.py` | The production model is never silently edited; a challenger replaces it only by clearing a bar written down before the test ran. |
| Competitive-defense hold | `challenger.py` → `defense_hold.csv` → `weekly_tracker.apply_defense_hold` | Cells that look like waste but are actually defending against a competitor's promo are held, not slashed. |
| Dual-engine agreement gate | `pricing_engine.py` (_build_agreement) → `agreement.csv` | A cut executes only when the analysis model AND the independent DE optimizer both say cut; disagreement = hold and test. |
| Kill-switch: two strikes, confounder excusal, freeze, drift brake | `scripts/tracker/killswitch.py` | Bad calls self-revert; stockout weeks don't count against a cut; a portfolio-wide hit-rate drop below 60% blocks all new cuts and signals retrain. |
| Immutable baseline freeze | `scripts/tracker/actuals.py` | Pre-action baselines are frozen once — mean reversion can never be dressed up as a win. |
| Execution-log gating (ops-miss vs model-miss) | `weekly_tracker.py:255-324` | Only recommendations the KAM actually applied are scored against the model; unexecuted rows are an ops metric — the model is graded on its own decisions only. |
| Honest accuracy scorecard + realized savings | `scripts/tracker/scorecard.py` | Direction hit-rate, unclamped R², rupee bias, and cumulative REALIZED savings — receipts, not reassurance. |
| Honesty clamps in the demand kernel | `de_optimizer.py` (reliability kill-mask, extrapolation bound, capped cross multiplier) | A price cut earns volume only where elasticity is reliably negative; no extrapolation past observed discounts; the model cannot manufacture phantom volume. |
| Anti-phantom clamp in the analysis layer | `discount_plan.py` / `optimize_plan.py` | Cutting a discount can never be modeled to RAISE units — reverse-causality free lunches are killed at the source. |
| Bootstrap risk-weighted expected unlock | `scripts/analysis/unlock_estimate.py` | P(stay-cut) per cell turns the unverified pool into an expected value for a disciplined test program, not a fantasy ceiling. |
| Cannibalization honesty check | `pricing_engine.py` (_cannibalization_check) | Per-SKU "sales held" claims are re-simulated portfolio-wide to verify they don't net to zero across siblings. |
| Budget allocator marginal-ROI waterline | `scripts/pricing/budget_allocator.py` | Greedy ROI≥1 allocation under a hard spend ceiling with a full per-cell ladder and elbow proof artifact. |
| C1–C8 automated acceptance gates | `scripts/analysis/validate_plan.py` | Exit-code-driven plan validation: controls present, no confounded cuts, CI logic, money reconciliation, OOS R², DML confirmation. |
| Known-truth recovery testing | `scripts/diagnostics/recovery_test.py` | A synthetic world with a planted elasticity and a deliberate endogeneity trap proves the production model recovers truth where the naive one is biased. |
| Model credibility split | `scripts/diagnostics/model_credibility_report.py` | Separates the flattering full-model R² from the honest decision-curve accuracy — the number quoted to buyers is the one that sets prices. |
| Data-readiness discovery gate | `scripts/diagnostics/data_readiness_report.py` | A green/yellow/red pre-engagement verdict on any new brand's data BEFORE recommendations — productized onboarding for the next 9 brands. |
| Out-of-time proof loop | `scripts/diagnostics/proof_loop.py`, `stage8_output/track_record.py` | Forward backtest framed honestly as a price-response test, not a demand-forecast test. |
| Festival/seasonality protection | `scripts/tracker/seasonality.py` | Planned festival discounting is never misread as waste; budget cap relaxes inside windows. |
| Hero-SKU protection | `v4_config.py` (STRATEGIC_SKUS), `weekly_tracker.py:91-97` | Flagship SKUs can never be auto-cut regardless of the math. |
| Run-stamped audit trail | `pricing_engine.py:333-347` | Every run's outputs snapshotted; nothing is silently overwritten. |
| Honest waste-ceiling framing | `DISCOUNT_PLAN/5L_VERDICT.md`, build_report reconciliation | The system tells you the defensible number (₹38k–88k/mo class), not the flattering one (₹6L) — the single most sale-critical honesty feature. |

---

## 5. What was built today — 10 new modules/upgrades, with their honest first-run findings

Every module runs champion/challenger style: the validated decision engine (`scripts/analysis/discount_plan.py`, `scripts/tracker/killswitch.py`) was never edited — verified by git diff in the regression gate.

**1. `scripts/pricing/constraints_lib.py` + `DISCOUNT_PLAN/pricing/pricing_constraints.json`** — closes promo_22, promo_23, price_22, price_25, price_26, price_27.
Declarative constraint rulebook for the DE optimizer: KPI bounds (any of 7 KPIs, both sides), pricing lines, portfolio and volume-weighted average price-change bands — all in JSON, compiled to penalties at runtime, fail-loud on unknown names.
*First-run finding:* every family ships `enabled:false`, and the smoke test proves all-disabled → 0 extra penalties — the production engine is behaviorally identical to the validated champion until a rule is deliberately switched on.

**2. `scripts/promo/promo_calendar_milp.py` + `promo_constraints.json`** — closes promo_22 (calendar templates), promo_23, val_14.
A true PromoAI-style MILP: 585 cells × 12 weeks on a 0/5/10/15/20% grid, duration/spacing/simultaneity/budget/defense-hold templates from JSON, solved with open-source HiGHS, with per-solve gap certificates.
*First-run finding:* 184/184 subproblems solved to the 1% gap target (worst 0.87%) in 5.6s total; chosen calendar is +0.84% horizon net revenue vs hold-current. Honest read printed in the report itself: the calendar's structure comes from the constraints, not demand seasonality — on this portfolio the kernel credits discounts with volume almost nowhere, the same conclusion as the confounder-controlled study.

**3. `scripts/pricing/cross_price_v2.py`** — closes price_06, price_12, price_18.
Paper-faithful multiplicative cross-price decomposition, the assembled full elasticity matrix E, and a measured competitor price-follow elasticity — side-by-side challenger files, champion untouched.
*First-run finding (honest FAIL inside a pass):* the similarity decomposition LOST to the uniform split on holdout (−0.22%), so the shipped matrix keeps the safe uniform weights — the estimated similarity factors are reported as a receipt, not used. Also stated plainly: the fact table's competitor-price column is 100% empty (rival prices come from RCA category medians), and competitor demand rows are unknowable from this data.

**4. `scripts/pricing/prior_store.py` + stability hook in `elasticity_bayes.py`** — closes price_30, val_10.
Sequential Bayesian prior store: each 4-weekly refresh's posterior seeds the next refresh's prior, with a ×1.25 forgetting factor capped at the diffuse prior; a run that failed release gates is never used to seed; seeded runs carry a max-shift ≤ 0.5 stability release gate. OFF by default — champion bit-identical until the flag is set.
*First-run finding:* two-run receipt on real data — 19/19 categories seeded, median own elasticity −1.006 → −1.007, max per-category shift 0.199, stability gate PASS.

**5. `scripts/validation/backtest_rolling.py`** — closes promo_25, val_01.
Rolling-origin walk-forward backtest of the PRODUCTION champion (imported read-only): 5 origins × 4-week horizon, honest recursive forecasts vs a flattered 1-step variant, benchmarked against seasonal-naive and last-week on identical cell-weeks.
*First-run finding (honest FAIL):* **the champion does NOT beat the naive benchmarks on pooled wMAPE** — champion(recursive) 0.274 vs seasonal-naive 0.249 and last-week 0.252, fold wins 2/5 (both on the longest training windows). The champion's real edge is bias: +0.3% vs +21–29% for the naives. The report says what this means: the champion's validated job is decision-making (isolating the discount effect), not beating naive forecasters — but a buyer should see this table. (A trailing partial-week contamination bug found during verification — the fact table ends mid-week — was fixed before these numbers were final.)

**6. `scripts/validation/elasticity_gates.py`** — closes val_02.
The paper's three-stage elasticity acceptance protocol run as a hard downstream gate on the production matrix, with exit-code semantics so a cron retrain can refuse to auto-promote.
*First-run finding (honest FAIL):* **Verdict FAIL — do not bank savings from this matrix.** Stage 1 fit: holdout R² 0.715 PASS, but wMAPE 0.454 > 0.40 FAIL and bias +10.8% > 5% FAIL. Stage 2 signs/magnitudes PASS — but flagged that 100% of cells are pinned at the prior (the data barely moves the estimate), so the passes are the prior's doing. Stage 3 stability PASS (drift 0.062). Operational meaning, printed in the report: keep using the matrix directionally with glide moves and register receipts; never promote it as a demand forecaster.

**7. `scripts/pricing/scenario_menu.py`** — closes promo_29, val_20.
The negotiation scenario menu: 8 optimized scenarios (revenue/volume/nrw/share/profit/margin × tight/base/loose constraints) side by side with the full financial chain, per-cell executable sheets, and a feedback file that re-runs the menu honoring counterpart locks.
*First-run finding:* deltas are honest and small (+1.8% to +4.1% revenue) because the confounder-controlled elasticities say discounts move demand weakly here; the report notes that 'current' itself breaches the 12% spend cap, and flags all profit levels as directional until per-SKU COGS replaces the 50% proxy.

**8. `scripts/tracker/params_review.py`** — closes val_09 (and feeds val_17's acceptance metric in `scorecard.py:267+`).
Scheduled, logged review of the 28 knobs the engine trusts without question: snapshot, diff vs history, staleness aging per horizon, `--ack` human sign-off, written to `DISCOUNT_PLAN/PARAMS_REVIEW.md`.
*First-run finding:* two real standing warnings surfaced immediately — COGS is still the 0.50-of-MRP proxy (every profit number inherits it), and `v4_config.FESTIVAL_DATES` ends 2026-03-03, so festival spikes since March have been treated as regular days in training. Also flagged: STRATEGIC_SKUS is empty — hero protection is configured but unused.

**9. Upgrades to `scripts/pricing/de_optimizer.py`** — closes price_15, price_16, price_21, val_15; one latent bug fixed.
The full KPI menu (profit, margin, and the revenue/profit 'combo' blend at `de_optimizer.py:440-442`), the 8-configuration robust DE ensemble (`ROBUST_GRID`, lines 505-512 — run 0 is bit-identical to the old champion), the declarative-constraints hook, and a **sign bug fix**: the objective normalized by the SIGNED baseline, so a group with negative baseline profit would have had its profit MINIMIZED; now divides by |baseline|. The champion revenue path was verified bit-identical before/after (identical DE objectives to 9 decimal places), and both fixes carry passing smoke tests.

**10. `scripts/validation/sensitivity.py`** — closes val_05 (the last open gap).
A 200-draw Monte-Carlo shake of every material input — elasticity (drawn from the champion's own standard errors), COGS ±10%, commission ±3ppt, baseline units ±10% — re-scoring the cut/hold rule per draw with no refits. First honest run: **zero of the 63 waste-cut cells are fragile** (max joint flip rate 3%, and the two least-stable cells are the defense-hold cells already excluded from the wave); the cost sweep flips nothing because the profit break-even bar sits above the revenue bar at any cost in the band; the cut-wave saving holds at **₹706k–739k/mo (p10–p90) around the ₹725k point**. Receipts in `DISCOUNT_PLAN/validation/SENSITIVITY_REPORT.md` + `sensitivity_cells.csv`.

**The one honest residual (not built):** val_16's spike-vs-promo cross-validation (outliers are removed statistically without checking whether a documented promo explains them). Small, named, and cheap to add.

---

## 6. Production-readiness receipts — the regression gate

Run after all builds and fixes, on real data. **All 5 checks PASS; day-one state intact.**

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Compile: every Python file | **PASS** | All 53 `.py` files under `scripts/` plus `pipeline.py` and `v4_config.py` compile clean (0 failures). |
| 2 | Smoke tests | **PASS 4/4** | `killswitch.py`, `actuals.py`, `elasticity_bayes.py` (own posterior −1.07±0.13, in-band), `de_optimizer.py` — each exit 0; new constraints hook confirmed backward compatible ("all families disabled → 0 extra penalties"). |
| 3 | End-to-end loop | **PASS** | `verify_loop.py` exit 0: "LOOP CLOSED: YES — actuals fill, only-applied scoring, and the kill-switch all ran on real numbers" (W1 585 rows, 456 actuals filled, 42 scored). Day-one state then restored and re-verified: fresh `weekly_tracker.py` run → 585 cells, cut 48 / hold 537, projected saving ₹32,164/wk under the 12% cap. |
| 4 | Plan acceptance gates | **PASS** | `validate_plan.py` exit 0, C1–C8 ALL PASS; C5 money reconciliation: line-sum ₹725,069 = reported ₹725,069. |
| 5 | Champion integrity | **PASS** | `git diff` shows zero changes to `scripts/analysis/discount_plan.py` and `scripts/tracker/killswitch.py`; all new modules coexist without breaking the suite. |

Per-build verdicts from the verification pass: `constraints_lib`/MILP calendar and `cross_price_v2` were **production-ready as built**; `de_optimizer` (sign bug + missing combo KPI), `prior_store` (missing stability release gate + a demo-path check bug), `backtest_rolling` (trailing partial-week contamination that inflated all models' errors), and `elasticity_gates`' estimator (self-check determinism + a latent NaN-handling fallback) were **fixed during verification and re-verified** — in every case the champion path was confirmed bit-identical after the fix.

---

*Bottom line for a buyer: the paper's method is here — 85 of 86 in-scope elements — running on one machine with no cloud and no license fees, wrapped in a causal-confirmation, safety, and honesty layer the paper never had. And when its own new validation modules failed a gate on real data, the system said so in writing. That is the product.*
