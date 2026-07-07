# Discount Optimization Engine — Critical Design Review
**Your 5 objectives vs. what senior FMCG data-science practice says — and vs. what you already own**
6 July 2026 · All claims cross-checked against your repo and the PepsiCo INFORMS paper you supplied.

---

## 0. The verdict, up front

**Direction: correct. Tool choice: correct. Tech stack: correct for your scale.** Discount-response modeling + constrained optimization + weekly feedback is exactly how PepsiCo frames the problem (their paper's core pattern: "ML-based demand estimation followed by a mathematical optimization stage", §2). Python + laptop + Excel outputs is *right*, not a compromise — PepsiCo needs Azure/Databricks/Gurobi because they run 90+ product groups × 50+ markets × hundreds of thousands of constraints. You have 84 SKUs × 11 cities = 585 cells; your full pricing-engine run finished in minutes on your laptop yesterday. Scaling to 15 brands (~8,000–9,000 cells) is still comfortably laptop-territory.

**The uncomfortable finding: you are not missing a tool — you already built ~80% of what you just asked for.** Objectives 1, 2 and most of 4 exist in your repo today and produce the proofs you're asking for. What's genuinely missing: the budget-reallocation optimizer (Objective 3 — buildable in days on top of `de_optimizer.py`), a live actuals loop (Objective 4 is built but has never eaten a week of real data, and has one known bug), and discipline items (one decision engine, versioned runs, tests for the confounded categories).

**The one thing you're doing wrong conceptually:** expecting "statistical proof" from historical data alone. Your own data already showed why that's impossible — 8 of your 19 categories have *positive* raw price-demand slopes (Honey +3.8, Sooji +5.0…), which is confounding, not consumer behavior. Real proof has a ladder (see §3), and its top rung is a controlled test, not a regression. PepsiCo says this themselves: elasticities pass validation gates, then pilots, then override monitoring — and your own 5L_VERDICT.md already concludes "the final proof is the register."

---

## 1. Objective-by-objective review

### Objective 1 — Find over-investment (discount not lifting sales / poor ROI)

**Your framing is right, with one refinement.** "No uplift beyond ₹74" and "uplift exists but at poor ROI" are two different failure modes, and you must test both, because a discount can *increase sales and still be waste*.

**The two bars every cell must clear (define them explicitly):**

1. **Net-revenue bar** (your current tool's rule): cutting the discount must not reduce net revenue = units × selling price. In continuous terms the break-even is unit-elasticity −1: below |e| = 1, any discount reduction *raises* revenue. Your `discount_plan.py` already implements exactly this as `be_beta = 1/(100 − d)` with a CI test (`reliably_waste` only if even the optimistic CI edge fails break-even).
2. **ROI-on-spend bar** (what you're asking for now, and it's stricter): incremental net revenue ÷ incremental discount spend ≥ 1, where discount spend = (MRP − SP) × units. This is stricter because deepening a discount pays the extra discount on *every* unit, including the ones you'd have sold anyway.

**Your Jaggery example, worked exactly (MRP ₹90, baseline 100 units/wk at SP ₹74):**

| Move | Elasticity assumed | Units | Net revenue | Discount spend | ΔNR | ΔSpend | **Marginal ROI** |
|---|---|---|---|---|---|---|---|
| ₹74 (17.8% disc) | — | 100 | ₹7,400 | ₹1,600 | — | — | — |
| ₹74 → ₹65 (27.8%) | e = −1.5 (elastic!) | 121.5 | ₹7,898 | ₹3,038 | +₹498 | +₹1,438 | **0.35** |
| ₹74 → ₹65 | e = −2.5 (edge of plausible CPG) | 138.5 | ₹9,002 | ₹3,462 | +₹1,602 | +₹1,862 | **0.86** |

Read that carefully: even at elasticity −2.5 — the very edge of the plausible CPG band your gates use — going from ₹74 to ₹65 returns 86 paise per extra rupee of discount. **At deep discounts, "sales went up" is almost never proof the discount paid.** That is the mathematical form of your instinct, and it's why the model must report marginal ROI per discount step, not just uplift. (Formula for your Excel: units₂ = units₁ × (P₂/P₁)^e; NR = units × SP; spend = units × (MRP−SP); ROI = ΔNR/ΔSpend.)

**What you already have:** response curves per cell + elbow at marginal ROI ≥ 1 (`stage6_economics`), CI-based waste test + confounder buckets (`scripts/analysis/discount_plan.py` — 63 cells, ₹6.98L/mo), DML causal confirmation of all 10 cut categories (`dml_estimate.py`), and the new portfolio check that the cuts survive cross-price/cannibalization (+1.67% portfolio revenue, `DISCOUNT_PLAN/pricing/PRICING_PLAN.md`).
**What to add:** a per-cell **discount ladder table** (like the Jaggery table above) at product and product×city level — every observed discount level with units, NR, spend, marginal ROI, and a flag for the last ROI ≥ 1 step. This is a reporting view over models you already have, and it's the "proof" artifact you show anyone who challenges a cut.

### Objective 2 — Find under-investment (high-ROI cells not getting discount)

**Right question, and the danger is symmetric:** the same confounding that fakes "discount doesn't work" can fake "discount works great." A cell that got discounted only during festivals will look brilliantly responsive.

**What you already have:** `reliably_pays` (CI floor above break-even) + headroom logic → `reinvest_list.csv`; the DE optimizer raises discounts only where elasticity is *reliably* negative (own + 1.64σ < 0 — the honesty clamp in `de_optimizer.py`); yesterday's run: 133 cells recommended more discount. Expected uplift and confidence come free: uplift = (P₂/P₁)^e − 1, confidence from the bootstrap SD (`own_sd`).
**What's missing:** the **execution path**. Your tracker deliberately never auto-raises discounts, so reinvest recommendations die on paper. Fix = the pilot protocol you already wrote in MEASUREMENT_SPEC.md: top 3–5 reinvest cells, +3ppt, 1–2 cities, 3 weeks, same-SKU other cities as control, promote or revert on the readout. Under-investment claims graduate to "proven" only through this gate.

### Objective 3 — Allocate a fixed discount budget (e.g., 10% of revenue) for maximum growth

**This is the one genuinely new build, and your framing needs two corrections:**

1. **"10% of revenue" is circular** — spend changes revenue. Define the budget on *baseline* revenue (trailing 4-week average, frozen weekly): B = 0.10 × R_baseline. Recompute the baseline weekly, never mid-week.
2. This is not "give everyone 10%" vs "give some more" — it's a **constrained portfolio optimization**, the same class PepsiCo solves (their PricingAI: maximize chosen KPI subject to financial bounds; Appendix C Eq. 15–39):

```
maximize   Σᵢ  units_i(d_i) × SP_i(d_i)                    (portfolio net revenue)
subject to Σᵢ (MRP_i − SP_i(d_i)) × units_i(d_i)  ≤  B     (budget = 10% of baseline revenue)
           |d_i − d_i,current| ≤ 3 ppt                      (weekly glide — you have this)
           d_lo ≤ d_i ≤ d_hi                                (box bounds — you have this)
           price-per-kg ladder within a product family      (you have this)
           units_i(d_i) via your clamped demand kernel      (you have this)
```

**Implementation is one new penalty term in `de_optimizer.py`** (budget overshoot penalty, exactly like the existing revenue-floor penalty) plus a config flag. Two days of work, not a new system.

**The Excel-checkable equivalent (greedy waterline), so you can verify the optimizer:** compute each cell's marginal ROI for its next +1ppt step (from the ladder table of Objective 1); sort descending; fund steps from the top until B is exhausted; cells whose marginal ROI < 1 get *defunded* toward their elbow first, releasing budget for the top of the list. Economics guarantee: at the optimum all funded cells sit at (approximately) equal marginal ROI — that "waterline" number is your single most useful management KPI ("our last funded rupee returns ₹1.4"). If the DE result and the greedy waterline disagree materially, one of them is wrong — that's your built-in audit.

### Objective 4 — Weekly tracking, predicted-vs-actual, self-improving model

**You designed this correctly and already built it — it has just never been fed.** What exists: frozen pre-action baselines (`actuals.py` + `baselines.json`), actuals backfill by cell×week, applied-only scoring (execution log), scorecard (hit rate, R², MAPE, revenue bias, cumulative *realized* savings — `scorecard.py`), kill-switch with confounder-first logic + 2-strikes revert + drift brake (`killswitch.py`), festival exclusions, and the end-to-end simulation proof (`verify_loop.py`).

**What "the model improves weekly" means concretely (your Objective 4 checklist → mechanism):**

| Your requirement | Mechanism | Status |
|---|---|---|
| Predicted vs actual sales | actuals backfill → scorecard MAPE/R² per week | built, dormant |
| Predicted vs actual uplift | pred vs actual net-revenue delta vs frozen baseline | built, dormant |
| Where assumptions failed | kill-switch strikes + "model miss" tags; confounded weeks excused (OSA/ad-visibility drops) | built, **Issue A bug open** |
| Learn from new data | 4-weekly refit on extended panel; champion/challenger: new model replaces old only if backtest MAPE improves; sequential shrinkage (last cycle's estimates as this cycle's prior) | **not built — the real gap** |
| Update recommendations automatically | weekly plan regeneration from latest passed model | built (tracker), needs the retrain trigger above |

**Blockers to fix before Week 2's export (all previously reported, still open):** (A) kill-switch evaluates cells you never touched — with 438 zero-prediction holds it will trip the drift brake immediately and spray phantom reverts; it must score only applied cut/reinvest cells. (B) No execution-log template is generated for your KAM to tick. (C) Readout on disk (63 cuts/585 cells) disagrees with tracker history (53 cuts/492 rows) — re-run end-to-end and reconcile. Until A–C are done, the learning loop you're asking for cannot start learning.

### Objective 5 — "Implement the PepsiCo architecture"

**Clarification you need:** PepsiCo's system is internal — there is nothing "already built in the market" you can license from them. What's public is the *method* (their INFORMS paper, which you have) — and you've already replicated the relevant half. PromoAI (retailer promo-calendar MILP with slotting/exclusivity constraints, Gurobi) does **not** apply to you: you don't negotiate weekly promo calendars with Blinkit under slotting constraints. PricingAI (elasticity → constrained optimizer → scenario workflow → validation gates → retrain per cycle) **is** your problem, and here's the honest mapping:

| PepsiCo component (paper ref) | Your equivalent | Status |
|---|---|---|
| Weekly SKU-banner panel, regular price = 8-wk window max, promo flag >5% below (§2.2.1) | `pricing_panel.py` (faithful, tested) | ✓ |
| Bayesian hierarchical elasticity, additive decomposition, priors, (−2.5, 0) gate, R²/wMAPE/bias gates (§2.2.2) | `elasticity_hier.py` ridge/partial-pooling stand-in + `gates.json` (all passing) | ✓ (upgrade path: PyMC posteriors) |
| DE optimizer, KPI choice, bounds/ladders/PPP, seeded ensemble (§2.2.3, App. C) | `de_optimizer.py` + category×city decomposition | ✓ |
| Adjusted-scenario instant what-if (§4.2) | `whatif.py` (shared demand kernel) | ✓ |
| Human-in-the-loop approval; ~85% acceptance tracked (§4.3) | weekly readout + execution log + decision tracking | built, dormant |
| Validation → pilot → override monitoring → retrain each cycle with same gates (§3.3) | scorecard + kill-switch + (missing) retrain cadence | **the gap** |
| Azure/Databricks/ADF/Gurobi platform (§3) | your laptop + CSV/MD outputs | correctly skipped at your scale |
| Budget-constrained allocation | — | **build (Objective 3)** |

The full 22-section enterprise version is already in your `PEPSICO_PRICING_REVERSE_ENGINEERING_BLUEPRINT.md`; the table above is the 15-brand laptop subset worth building.

---

## 2. Gaps in your thinking (the critical review you asked for)

1. **"Proof" needs a ladder, not a regression.** Levels of evidence, weakest → strongest: (i) observational fit with confounder controls (stock-outs, ad share, seasonality, momentum — you do this); (ii) causal debiasing (DML — you do this); (iii) out-of-time backtest (built, in `_proof_loop`); (iv) **live glide with kill-switch = every cut is itself a small experiment** (your design); (v) city-split A/B for contested cells (specified, never run). Anyone demanding "proof" gets shown all five rungs. No FMCG system on earth proves elasticity from history alone — PepsiCo runs gates + pilots + override monitoring for exactly this reason.
2. **ROI without COGS is fine — but say what it means.** Your ROI = Δ net revenue / Δ discount spend. It treats revenue as the prize, which *overstates* true profit ROI (every incremental unit also carries product cost). Practical consequence: your ROI ≥ 1 bar is *lenient*, so cells that fail it are failing generously — cuts are extra-safe; reinvestments deserve the stricter pilot gate. Revisit COGS only when finance can give even category-level %s.
3. **Two engines now disagree and nothing arbitrates.** `discount_plan.py` buckets (63 cuts) and `pricing_reco.csv` (446 down / 133 up) overlap but aren't identical, and the tracker only reads the first. Decide one rule and encode it: *a cell is cut only if both agree; pricing-engine "up" cells feed the reinvest pilot queue; disagreements go to a "review" tab.* Without this you'll one day execute contradictory advice.
4. **The clipping is hiding a business insight.** 53% of cells clipped to zero elasticity isn't a modeling nuisance — it's a finding: *in half your portfolio, discount depth shows no clean causal link to demand* (visibility, availability and festivals drive it instead). Those cells are precisely where budget should be pulled first and where ad/OSA investment likely beats discount. Report the clip rate; don't bury it.
5. **Excel's role.** Excel as the interface (readouts, ladder tables, KAM tick-sheet) — yes. Excel as the database — no. Keep the store as versioned CSVs (or one DuckDB file) written by code; every run stamped into a dated folder (your `v4_outputs/` already does this; `DISCOUNT_PLAN/pricing/` currently overwrites — fix).
6. **At 15 brands the binding constraint is process, not compute.** ~9,000 cells still solves in minutes. What breaks is ops: weekly exports for 15 brands, KAM confirmations, per-brand configs. Solve with the one-command run + config-per-brand pattern (PepsiCo's "new market = new JSON config, zero code" principle — §3, "Externalized Business Logic").

---

## 3. Target architecture (laptop edition) — what runs when

```
MONDAY  (one command: run_week.bat)
  Blinkit weekly export → input_data/
    → pipeline (fact table)                       [exists]
    → actuals backfill + scorecard + kill-switch  [exists; fix Issue A first]
    → 4-weekly: refit elasticities + gates; champion/challenger  [BUILD]
    → budget allocator (10% of baseline revenue → per-cell discounts)  [BUILD on de_optimizer]
    → reconcile with discount_plan buckets (agreement rule)  [BUILD - small]
    → outputs: WEEKLY_READOUT.md (REVERT/ALERT first) + WEEKLY_TRACKER.xlsx
               + execution-log template for KAM + run-stamped folder
TUESDAY   you review 15 min → approve → KAM applies on Blinkit
FRIDAY    KAM returns Applied Y/N column
4-WEEKLY  retrain + realized-vs-forecast review (did the ₹6.98L land?)
QUARTERLY festival calendar, budget %, hero-SKU list refresh
```

Stack verdict: **keep** Python (pandas/numpy/scipy/statsmodels/sklearn/LightGBM), CSV+Excel outputs, git for versioning. **Add nothing else now.** Upgrade triggers: >2 users → move outputs to a shared drive + one scheduled machine; >50k cells or hourly cadence → DuckDB/Postgres + Prefect; want uncertainty bands → PyMC posterior version of `elasticity_hier` (drop-in, noted in your own file header). Cloud/Airflow/Kubernetes at your scale is cost without benefit.

---

## 4. What successful FMCG companies actually do (evidence)

- **PepsiCo (your PDF; INFORMS J. Applied Analytics, 2026, DOI 10.1287/inte.2025.0302):** the architecture you're copying — ML demand → constrained optimizer → scenario UI → human approval → gates → per-cycle retrain. Results: ~85% of recommendations accepted/executed, planning cycles weeks→minutes, management-verified revenue/margin gains; cites BCG's benchmark of **2–5% revenue and profit lift** from AI pricing (Hazan et al., BCG 2021).
- **Alibaba (Deng et al., INFORMS J. Applied Analytics 53(1), 2023):** integrated demand forecasting + price optimization at scale; same predict-then-optimize pattern.
- **Oracle/grocery promotions (Cohen, Leung, Panchamgam, Perakis, Smith, *Operations Research* 65(2), 2017, "The Impact of Linear Optimization on Promotion Planning"):** supermarket promotion planning as constrained optimization over promo effects with business rules — the PromoAI ancestor; documented multi-percent profit improvements in field use.
- **Ito & Fujimaki (KDD 2017, "Optimization Beyond Prediction"):** prescriptive price optimization — ML demand models embedded directly in the optimizer; the academic name for exactly your pipeline.
- **Rue La La (Ferreira, Lee, Simchi-Levi, *M&SOM* 18(1), 2016):** demand prediction + price optimization for products with little price variation — their answer to your sparse-data problem is structured curves + optimization, like yours.
- **Bertsimas & Kallus (*Management Science* 66(3), 2020, "From Predictive to Prescriptive Analytics"):** the theoretical backbone (cited by PepsiCo) for why prediction alone (your Objective 1–2 analytics) is worthless without the optimization layer (your Objective 3).
- **Offer experimentation practice (e.g., Eversight, used publicly by PepsiCo/Frito-Lay in 2018–2021 case studies):** industry's answer to confounding is *live micro-tests of offers*, not better regressions — supporting §2.1's evidence ladder and your city-split protocol.
- **Trade-promotion effectiveness studies (NielsenIQ and consultancies, various years):** repeatedly find that a large share — often cited near or above half — of trade promotions do not pay out; your 53%-clipped finding is that industry fact showing up in your own data.

---

## 5. Execution order (2 weeks of work, then the loop runs)

1. **Fix Issue A** (kill-switch scores only applied cut/reinvest cells) + emit the KAM execution-log template + re-run tracker to reconcile readout (Issues B, C). *Unblocks all of Objective 4.*
2. **Build the budget allocator** on `de_optimizer.py` (budget penalty + baseline-revenue budget config) + the greedy-waterline Excel cross-check. *Delivers Objective 3.*
3. **Ship the ladder-table report** (per product & product×city: discount level → units, NR, spend, marginal ROI, elbow flag) + clip-rate transparency in PRICING_PLAN.md. *Delivers Objective 1's proof artifact.*
4. **Encode the two-engine agreement rule**; run-stamp `DISCOUNT_PLAN/pricing/` outputs.
5. **Start the weekly ritual** (Monday run → Tuesday approve → Friday KAM Y/N). After 4 weeks of actuals: first champion/challenger retrain; first realized-vs-forecast review; launch the first reinvest pilots (Objective 2) and city-split tests on contested cells.

Everything above names the exact file it lands in. Nothing requires new infrastructure, new vendors, or a bigger machine.
