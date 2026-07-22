# Reverse-Engineering Blueprint: PepsiCo PromoAI + PricingAI
## A complete implementation specification for rebuilding the system from scratch

**Source document:** "PepsiCo Deploys AI-Driven Pricing and Promotion Optimization at Scale" — Llenas, Salazar-Treviño, Leskovar, Pons-Llopis, Todeschini, Gowda, Bofill, Anish, Cleavinger (PepsiCo Data Science & Advanced Analytics). INFORMS Journal on Applied Analytics, accepted May 18 2026, DOI 10.1287/inte.2025.0302, arXiv:2606.17941. 40 pages, 3 appendices, 11 tables, 1 figure, 1 code listing, ~39 numbered equations.

**Provenance convention used throughout this blueprint:**
- **[DOC p.N]** — explicitly documented in the paper, page N. Treat as ground truth.
- **[INF-High]** — not stated, but a near-certain enterprise practice given what is stated.
- **[INF-Med]** — reasonable inference; verify during discovery.
- **[INF-Low]** — plausible pattern; treat as a design proposal, not a fact.

The paper is an applied-OR journal article, not an engineering runbook: it fully documents the *mathematics, models, constraint systems, validation gates, and human workflows*, and only sketches the *platform* (Azure + Data Factory + Databricks + Gurobi + web UI + JSON). Everything platform-shaped below the math is therefore reconstructed here as an enterprise-standard implementation, with confidence levels.

---

# 0. Document Map — every page, figure, table, equation: what it is, why it exists, how it connects

This section walks the entire source document so nothing is skipped. Later sections expand each element into implementation detail.

| Pages | Content | What it means / why it exists / how it connects |
|---|---|---|
| p.1 | Title, authors, abstract | Declares the two systems: **PromoAI** (ML forecast + MILP for promotional calendars) and **PricingAI** (Bayesian hierarchical elasticities + nonlinear optimization for base prices). The abstract is the system contract: demand elasticity + competitor actions + channel constraints + financial objectives, at portfolio scale. Everything else in the paper elaborates these two pipelines. |
| p.2–3 | Introduction: the two business problems | Defines **trade promotions** (temporary price cuts negotiated with retailers: BOGO, "2 for $5"; a *scheduling* problem over limited slotting capacity, exclusivity, spacing, seasonal events) vs **base pricing** (strategic everyday price; semi-annual/annual; elasticity, cannibalization, switching, price ladders, psychological thresholds like $1.99/20 pesos). Why: these are different decision structures needing different math (discrete MILP vs continuous non-convex). Connects to: §2 methodology choices, Appendices A & C. |
| p.3–4 | Literature + positioning | Cites Marriott group pricing, Alibaba demand/price optimization, RL pricing, prescriptive optimization. Positions the contribution as *enterprise-scale deployment with governance and human-in-the-loop as first-class goals*. Why: justifies design choices that favor tractability + adoption over theoretical purity. |
| p.4–6 | §2 Methodology overview | The shared architectural pattern of both systems: **ML demand estimation → mathematical optimization**. PromoAI = LightGBM → MILP (Gurobi). PricingAI = Bayesian hierarchical (STAN/ADVI) → Differential Evolution (SciPy). Explains MILP (discrete decisions, linear constraints, provable gaps) and DE (population metaheuristic for non-convex continuous landscapes). Why documented: the paradigm split is the paper's core engineering lesson — promotions are data-rich/discrete/reversible; pricing is data-scarce/continuous/risky, needing uncertainty quantification. |
| p.6–7 | §2.1.1 PromoAI data preparation | Required inputs: weekly sales, pricing, promotional records, shelf placement (upper/middle/lower), trade spend, product cost. Documented pain: retailer heterogeneity (promo definitions, price recording, product hierarchies; item-level vs group-level promos), missing weeks, inconsistent promo labels, stale prices, SKU identifier churn → longitudinal product mappings. Documented controls: imputation, price-anomaly correction, validation of sales spikes against documented promos, automated quality checks flagging implausible price–volume combos and margin inconsistencies. Connects to: §4 (data flow) and §5 (database) of this blueprint. |
| p.7–8 | §2.1.2 Forecasting model | One **global LightGBM regression** across all PPGs (not per-product models). Features: temporal (week, month, holiday), promotional (discount depth, mechanic type, placement), product attributes (brand, category). Objective = **MAPE**; hyperparameters via randomized CV on a **temporal holdout** (most recent weeks). Feature importance ranking documented: PPG id > year/holiday/week-of-year/quarter > mechanic type, retailer base price, transaction price, promo depth, month. Why global: cross-PPG information sharing, robustness for sparse products. Connects to: PWL approximation (§2.1.3) which consumes this model. |
| p.8–9, Fig.1 | §2.1.3 Piecewise-linear approximation | **Figure 1** shows a demand curve vs "competitive discount pressure" (x∈[0,1], demand 0–120 units) with a 2-segment PWL fit and breakpoints. Method: evaluate LightGBM on a grid of discount-pressure values per (PPG, week, promo); jointly optimize breakpoint x/y coordinates via **SLSQP** minimizing MSE against the ML curve; choose segment count via **knee-point detection** on the error curve; 2–4 breakpoints suffice in practice; configuration retained across cycles unless demand behavior shifts. Why: MILP needs linearity; this is the bridge from ML to solver. Connects to: Eq. 6 (breakpoint vectors b, d) and the big-M activation (Eq. 7). |
| p.9–10 | §2.1.4 Business constraints overview | Objective balances PepsiCo vs retailer and revenue vs margin via **user-chosen weights in the UI**. Two constraint classes: **structural** (exclusivity per week; competitive discount pressure; PWL demand) and **business** (financial: revenue targets, trade-spend limits, margin preservation, market-share floors; calendar: seasonal rules, holiday alignment, min/max frequency, spacing; execution: competitor lockouts, ad-block linking, retail price restrictions, front-page exposure minimums). Rules are encoded in **JSON** and compiled into constraints. Connects to: Appendix A (math) and Appendix B (JSON). |
| p.10–12, Table 1 | §2.2.1–2.2.2 PricingAI data prep + elasticity model | Three inputs orchestrated by a **product master** (SKU → brand, sub-brand, size, taste, price line): historical weekly sales (price, volume, promo, distribution at SKU-banner-week), financials (costs, sell-in prices), optional **conjoint survey** priors. Feature engineering: **regular price = max observed price in an 8-week backward+forward sliding window**; **promo week = observed price >5% below regular**; aggregate store-region → semi-region with a **30% revenue-weighted promo threshold**. Elasticity model: multiplicative volume model, Bayesian hierarchical; **own-price elasticity = additive decomposition over grand/brand/size/taste/banner (Table 1)**; **cross-price = multiplicative over sub-brand/size/taste, bounded (0,1)**; plus promo-discount elasticity, promo flag, distribution, monthly seasonality, recency weighting, volume-importance weighting; conjoint = informative prior means (tight SD on brand/taste, wide on size/banner); **softmax penalty against positive own-price elasticities**; STAN + **ADVI** (hours-scale); validation gates **R² > 0.6, wMAPE < 0.4, |bias| < 10%**, elasticities in **(−2.5, 0)**, positive cross-elasticities for substitutes; end-to-end optimization test. Connects to: elasticity matrix E feeding Eq. 22–24. |
| p.13 | §2.2.3 DE optimization | SciPy differential evolution; objective assembled from UI config; KPI choice (revenue/profit/blend); constraints: price ladders + financial (revenue, margin, market share, profit, volume bounds), all with user-set lower/upper bounds. Connects to: Appendix C formulation. |
| p.13–14 | §3 System architecture | **The only architecture page.** Web UI → scenario config → **JSON** → **Azure Data Factory** pipelines → **Databricks notebooks** (compute; model training + optimization) → data layer. Three documented principles: **loose coupling** (UI/engine/data replaceable independently), **separation of concerns** (ML models trained & versioned separately from optimizers), **externalized business logic** (JSON config, not code; new market = new config file; business users edit rules without developers). Connects to: §2 (Architecture) and §13 (Automation) of this blueprint. |
| p.14–15, Table 2 | §3.1 PromoAI deployment scale | Markets: Canada, US, Mexico, Brazil, UK, Australia, South Africa ("dozens of markets"). Problem sizes: 6–8 to 90+ PPGs; 8–52-week horizons; **hundreds of thousands of variables and constraints**. Dedicated **Gurobi v12 server**; runtimes minutes → 7–9 hours; **callback-based early termination** on MILP-gap stagnation; market-specific target gaps **1% (high-value) to 5% (speed-priority)**. |
| p.15–16 | §3.2 PricingAI deployment | Markets: **Mexico and US**. 40+ PPGs including **competitor SKUs**. Fewer but nastier constraints (log/exp volume terms, ratio-based market share). US = continuous dollars; Mexico = **integer pesos**; psychological thresholds respected in both. DE has no optimality guarantee → **ensemble of up to 10 parallel seeded DE runs** with varied mutation/crossover/population, distributed on Databricks; **fixed seeds for reproducibility**; average runtime **< 30 min**; convergence on population-energy tolerance + max-runtime safeguard. |
| p.17–18 | §3.3 Solution validation + maintenance | Rolling-origin backtesting vs legacy benchmarks (MAPE at SKU–retailer–week); economic-consistency review of elasticities; constraint-satisfaction verification on historical + simulated scenarios; sensitivity analyses under perturbed demand/cost/trade-spend; structured scenario reviews with planners; pilot with selected retailers; **override behavior monitored** during pilot; retraining **quarterly (promo) / each pricing cycle (pricing)** behind the same validation gates; per-cycle parameter refresh (costs, trade agreements, retailer requirements); extensibility to new categories/retailers without redesign; **sequential prior updating** (previous posterior → next prior) to prevent destabilizing elasticity jumps between cycles. |
| p.18–19 | §4.1–4.2 User workflows | PromoAI: collect retailer rules → encode constraints → generate **multiple calendars under different objective configs** → present menu to retailer → gather feedback → re-encode → re-optimize (closed negotiation loop). Also a **simulation mode**: manually edit any promotional decision and immediately see predicted impact on revenue/margin/volume/frequency across the horizon. PricingAI: system generates optimized price vector under user constraints (min margin, revenue targets, volume bounds, ladders); **adjusted-scenario feature** = manually change individual prices, immediately see portfolio-wide volume/revenue/margin impact including cross-price effects. Why documented: the authors credit these interactive capabilities for adoption. |
| p.19–20 | §4.3 Adoption & impact | PromoAI: active across **all global markets**, **16 calendar refreshes/year**; **~85% of recommendations accepted and executed**; planner quote. Cycle compression: **weeks → minutes**. PricingAI live in US + Mexico, expansion underway. Impact confirmed by management; figures withheld; industry benchmark cited: **2.0–5.0% revenue and profit improvement** (BCG). Cultural shift: planning meetings now scenario-analysis sessions. |
| p.20–23, Table 3 | §5 Discussion | Table 3 side-by-side: MILP vs DE; binary vs continuous+integer variables; ~10⁵ linear constraints vs dozens of nonlinear ones; minutes–10h vs ~30min; single solver vs 10 parallel runs; 7+ vs 2 countries. §5.1: optimization (not forecasting) is the primary value driver in both systems — forecasts alone are descriptive. §5.2 implementation challenges: data quality/harmonization is the persistent cost; deliberate approximations (PWL, PPG aggregation, posterior means instead of full posteriors) trade fidelity for tractability; trust built via transparency + constraint editing + override simulation; **recommendations must be operationally executable at the retailer or they're worthless** — hence the re-optimization loop. |
| p.23–24 | §6 Conclusion | Three lessons: scalable optimization is feasible at enterprise scale; human-centric AI wins adoption; pragmatic approximations (PWL, PPG, JSON constraint modularity) are what make theory deployable. |
| p.24–31, Tables 4–8, Code 1 | **Appendix A + B: PromoAI math + JSON** | Table 4: LightGBM hyperparameters (search ranges + selected values: num_leaves 269, lr 0.0185, n_estimators 302, min_split_gain 0.00456, max_depth 184, reg_alpha 6.04, reg_lambda 4.95; MAPE objective; early stopping patience 50). Table 5: sets (P PPGs, P̃ PepsiCo-owned, W weeks, R_p promo options, R̃_p active promos, S_p same-size segment, L locked competitor slots). Table 6: parameters (discount fraction δ, unit revenue/margin ρ/μ for PepsiCo & retailer, objective weights ω1–ω4, margin floors γ, promo caps N, breakpoints b/d, market-share floor m, big-M). Tables 7–8: decision variable x_{p,w,r} ∈ {0,1} + auxiliaries (α discount, β pressure, q̂ conditional demand, q realized demand, S/Π sales & margin aggregates). Equations 2–14: full MILP (objective, exclusivity, discount fraction, pressure averaging, PWL demand, big-M activation triple, financial aggregation, margin ratios, promo-count caps, market-share floor, competitor locks, domains). Code 1: JSON for `WeeklyMaxPromo` (≤4 "Price"-mechanic promos/week) and `MinMaxPromoDuration` (PepsiCo promos 2–4 consecutive weeks) with a generic column-filter `data` block. Additional documented templates: ad-block linking, min/max duration, weekly caps by mechanic, seasonal/holiday locks, min spacing (anti pantry-loading). |
| p.31–38, Tables 9–11 | **Appendix C: PricingAI math** | Table 9: sets (A all products incl. competitors, P PepsiCo subset, C competitor followers, G pricing groups, L_k pricing lines, B_b brand units, T_t tiers, U(t) upper tiers, K_i PPP thresholds; mapping g(i) product→group). Table 10: parameters (baseline price p⁰, baseline volume q⁰, pack size s, sell-in price c^SIP, sell-in discount d, COGS, distribution cost, pass-through φ, elasticity matrix E, PPP thresholds τ & coefficients ψ, competitor follow ratio ρ & hero mapping h(i), KPI bounds α, avg-price-change bounds θ, ladder tolerance γ). Table 11: decision variable x_g (ℝ multiplicative factor in % mode / ℤ absolute price in Mexico mode). Equations 15–39: objective = one user-selected KPI ∈ {Revenue, Profit, Volume, Margin, MarketShare, NRW}; bounds; price computation incl. competitor-follow Δ_i = ρ_i·Δ_{h(i)}; volume function V_i = q⁰_i·exp(Σ_j E_ij·[ln p_j − ln p⁰_j] + ΔPPP_i); PPP step function; unit conversion U_i = V_i/s_i; sell-in update c^SIP·(1+Δ·φ); KPI definitions (27–32); KPI bounds (33); price-per-weight ladder (34); tier ordering (35); equal-increment pricing lines (36); average price-change band (37); volume-weighted average price-change band (38–39, endogenous weights). |
| p.38–40 | References | 25 citations — the method stack's academic provenance (LightGBM, STAN, ADVI, DE, MILP texts, revenue-management literature, BCG pricing-AI benchmark). Useful to the rebuilder as the canonical reading list. |

---

# 1. Executive Overview

## 1.1 Overall objective [DOC p.1–4]
Build a two-engine, enterprise-scale **Revenue Growth Management (RGM) decision-support platform**:

1. **PromoAI** — given a retailer, a planning horizon (8–52 weeks), demand forecasts, and a library of business rules, produce the **profit/revenue-optimal promotional calendar** (which promo mechanic runs for which product group in which week), respecting every operational constraint, and support iterative re-optimization during retailer negotiation.
2. **PricingAI** — given elasticity estimates across a portfolio (own + competitor products), produce the **optimal base ("everyday") price vector** for the next pricing cycle that maximizes a chosen KPI subject to financial guardrails, price-architecture rules, and psychological thresholds.

The unifying pattern [DOC p.4–5]: **machine-learned demand response → mathematical optimization → human-in-the-loop scenario workflow**. Prediction is an input; optimization is the product; human approval is the gate.

## 1.2 Business problem solved [DOC p.2–3]
- Manual/spreadsheet promo planning cannot search millions of product–promotion–timing combinations under hundreds of thousands of constraints (slotting capacity, exclusivity, spacing, holiday alignment, budgets, margin floors).
- Base-price setting is high-risk and data-scarce (infrequent price changes), with portfolio interdependence (cannibalization within own portfolio, switching to competitors) that no human can reason about at 40+ product scale.
- Global inconsistency: each market/BU had its own ad-hoc method; the platform imposes a unified, configurable methodology [DOC p.3].

## 1.3 Users [DOC p.18–20]
| User | System | What they do |
|---|---|---|
| Trade/promo planner (RGM) | PromoAI | Collect retailer rules, configure scenarios, generate calendar menus, negotiate, re-optimize, hand off final calendar |
| Pricing manager (RGM) | PricingAI | Configure KPI + guardrails, generate price vectors, run adjusted scenarios, present to leadership |
| Sales / key-account manager | PromoAI outputs | Present calendar options to the retailer; bring back feedback [DOC p.18] |
| Retailer counterpart | Indirect | Reviews calendar menu; may opt out via formal negotiation [DOC p.2] |
| Data scientist | Both | Train/version forecast + elasticity models; maintain validation gates; retrain quarterly/cycle [DOC p.17] |
| Market/BU admin [INF-High] | Both | Owns the market JSON config; onboards new markets by authoring config files [DOC p.14 says config-driven onboarding; the named role is inferred] |
| Finance reviewer [INF-Med] | Both | Supplies costs/sell-in prices; validates margin outcomes (financial data is a documented input; the review role is inferred) |

## 1.4 Stakeholders [DOC p.19–20 + INF-High]
Senior management (verified impact claims), retailer partners (joint value: objective explicitly weights retailer sales/margin ω2, ω4), category management, supply chain (volume consequences), finance (margin floors), data science leadership, IT/platform (Azure estate).

## 1.5 Inputs
| Input | Grain | System | Provenance |
|---|---|---|---|
| Weekly sales history (units, value) | SKU/PPG × retailer × week | Both | [DOC p.7, p.11] |
| Price history (shelf + effective) | same | Both | [DOC p.7, p.11] |
| Promotional records (mechanic, depth, placement, dates) | event | PromoAI | [DOC p.7] |
| Shelf placement (upper/middle/lower) | store/segment | PromoAI (some markets) | [DOC p.7] |
| Trade spend + product cost | SKU/PPG | Both | [DOC p.7, p.11] |
| Sell-in prices (manufacturer→retailer) & sell-in discounts | SKU | PricingAI | [DOC p.11, Table 10] |
| Distribution metrics | SKU-banner-week | PricingAI | [DOC p.11] |
| Conjoint survey elasticity priors (optional) | attribute level | PricingAI | [DOC p.11–12] |
| Product master (SKU→brand, sub-brand, size, taste, price line, PPG) | SKU | Both | [DOC p.11; PPG mapping p.7] |
| Holiday/seasonal calendar | market × week | Both | [DOC p.7 features; p.10 constraints] |
| Business-rule JSON configuration | market/scenario | Both | [DOC p.10, 14, App. B] |
| Objective weights / KPI selection / bounds | scenario | Both | [DOC p.9, 13] |

## 1.6 Outputs
| Output | Contents | Provenance |
|---|---|---|
| Optimized promotional calendar(s) | promo option per PPG per week + predicted volume, revenue, margin for PepsiCo & retailer, per scenario | [DOC p.18] |
| Scenario menu | multiple calendars under different objective weightings for negotiation | [DOC p.18] |
| Optimized base-price vector | new price per pricing group + predicted volume/revenue/profit/margin/share/NRW impact incl. cross-effects | [DOC p.19] |
| Simulation deltas | KPI impact of any manual edit | [DOC p.18–19] |
| Elasticity matrix + posteriors | own/cross elasticities with uncertainty | [DOC p.11–13] |
| Validation artifacts | backtests, gate results, sensitivity analyses | [DOC p.17] |

## 1.7 Success metrics & KPIs [DOC p.19–20 unless noted]
- **Adoption/trust:** ~85% of PromoAI recommendations accepted & executed; override rate monitored during pilots [DOC p.17].
- **Cycle time:** planning tasks compressed from weeks to minutes; 16 promo-calendar refreshes/year sustained.
- **Financial:** management-verified revenue and margin improvement (figures withheld); industry benchmark 2.0–5.0% revenue & profit lift.
- **Model quality gates (PricingAI):** R² > 0.6, wMAPE < 0.4, |bias| < 10% at SKU-banner-week; own-price elasticities in (−2.5, 0) [DOC p.12].
- **Solver quality (PromoAI):** MILP gap 1–5% by market policy [DOC p.15].
- **Runtime SLOs:** PromoAI minutes–10h with early-termination callbacks; PricingAI < 30 min average [DOC p.15–16].
- [INF-High] Forecast MAPE vs legacy benchmark tracked per retrain (rolling-origin backtesting is documented; the standing dashboard is inferred).

## 1.8 Assumptions the system rests on
- Weekly grain is sufficient for both engines [DOC throughout].
- PPG aggregation preserves decision relevance [DOC p.22].
- Demand response is smooth/monotonic enough for 2–4 PWL breakpoints [DOC p.9].
- Posterior-mean elasticities (not full posteriors) are acceptable inside the optimizer [DOC p.22].
- Competitors follow PepsiCo "hero" price moves with a fixed ratio ρ [DOC Table 10, Eq. 20].
- Retailer executes what it accepts; acceptance is the value gate [DOC p.19, 23].
- Business rules are expressible as parameterized templates (JSON) [DOC p.10, 14].
- [INF-High] Costs/sell-in data from ERP are correct at cycle start; the optimizer treats them as constants per run.

---

# 2. Complete System Architecture

## 2.1 What the paper actually documents [DOC p.13–16]
Web UI (scenario configuration) → request serialized to **JSON** → **Azure Data Factory** pipelines orchestrate → **Databricks notebooks** execute model training + optimization against the data layer → PromoAI MILP solved on a **dedicated Gurobi 12 server**; PricingAI DE runs distributed as **up to 10 parallel seeded instances on Databricks**. Design principles: loose coupling, separation of concerns (models versioned separately from optimizers), externalized business logic (JSON config per market). Everything else below is the enterprise-standard completion of that skeleton.

## 2.2 Full target architecture (ASCII)

```
┌─────────────────────────────── USER LAYER ────────────────────────────────┐
│  Promo Planner   Pricing Manager   Sales/KAM   Data Scientist   BU Admin  │
└──────┬────────────────┬───────────────┬──────────────┬─────────────┬──────┘
       ▼                ▼               ▼              ▼             ▼
┌─────────────────────────────── UI LAYER ──────────────────────────────────┐
│  Scenario Builder │ Constraint Configurator │ Calendar Grid │ Price Board │
│  Simulation/Adjusted-Scenario Editor │ Run Monitor │ Approvals │ Reports  │
│  (SPA: React/Angular)                     [DOC: "web-based UI" p.13]      │
└──────┬─────────────────────────────────────────────────────────────────────┘
       ▼  OAuth2/OIDC (Entra ID) [INF-High]
┌──────────────────────────── API GATEWAY ──────────────────────────────────┐
│  REST API (FastAPI) · RBAC · rate limiting · audit interceptor [INF-High] │
└──────┬───────────────────────────────┬────────────────────────────────────┘
       ▼                               ▼
┌── SCENARIO/CONFIG SERVICE ──┐  ┌── ORCHESTRATION ──────────────────────────┐
│ scenario CRUD, JSON rule    │  │ Azure Data Factory pipelines [DOC p.13]   │
│ files, market configs      │─▶│ trigger: submit / schedule / data-arrival │
│ [DOC p.10,14, App.B]        │  │ retries, alerts [INF-High]                │
└─────────────────────────────┘  └───────┬───────────────────────────────────┘
                                         ▼
┌────────────────────────── COMPUTE LAYER (Databricks) [DOC p.14] ──────────┐
│  ETL & harmonization jobs → Delta tables [INF-High]                       │
│  Feature builder (regular price, promo flags, pressure grids)             │
│  ┌────────────┐  ┌──────────────────┐  ┌─────────────────────────────┐   │
│  │ LightGBM   │  │ STAN + ADVI       │  │ PWL breakpoint fitter       │   │
│  │ forecaster │  │ elasticity model  │  │ (SLSQP + knee-point)        │   │
│  └─────┬──────┘  └────────┬─────────┘  └──────────┬──────────────────┘   │
│        ▼                  ▼                        ▼                      │
│  ┌───────────────────────────────────────────────────────────────────┐   │
│  │ OPTIMIZATION ENGINES                                              │   │
│  │  PromoAI: MILP builder → Gurobi 12 dedicated server [DOC p.15]    │   │
│  │  PricingAI: DE ensemble ×10 seeded parallel runs [DOC p.16]       │   │
│  └───────────────────────────────────────────────────────────────────┘   │
└──────┬────────────────────────────────────────────────────────────────────┘
       ▼
┌── DATA PLATFORM ───────────────────────────────────────────────────────────┐
│  Raw zone (retailer/syndicated feeds) → Clean zone (harmonized weekly      │
│  facts) → Feature store → Model registry (MLflow) → Results store          │
│  Warehouse: Delta Lake / Synapse / Snowflake [INF-High]                    │
└──────┬─────────────────────────────────────────────────────────────────────┘
       ▼
┌── SERVING & BI ────────────────────────────────────────────────────────────┐
│  Results API → UI grids  ·  Power BI / Tableau dashboards [INF-Med]        │
│  Exports to trade-promotion-management & ERP systems [INF-Med]             │
└────────────────────────────────────────────────────────────────────────────┘
   Cross-cutting: Monitoring (App Insights/Grafana) · Logging · Audit trail ·
   Secrets (Key Vault) · CI/CD (Azure DevOps/GitHub Actions)  [INF-High]
```

## 2.3 Component-by-component rationale, data flow, technology options

| Component | Why it exists | Data in → out | Documented? | Primary tech | Alternatives (pros/cons) |
|---|---|---|---|---|---|
| User layer | Planners are the decision authority; system is decision support, not autopilot | intents → scenario configs | [DOC p.18–19] | Browser SPA | Desktop/Excel add-in (familiar but unscalable, the thing being replaced) |
| UI layer | Scenario building, constraint editing, calendar/price visualization, simulation | user edits → JSON scenario; results ← API | UI existence + capabilities [DOC p.9,13,18–19]; framework not named | React + AG-Grid | Angular/Vue equivalent; Streamlit/Dash (fast to build, weaker at enterprise UX/RBAC) |
| AuthN/AuthZ | Enterprise governance; per-market permissions | identity → roles/claims | [INF-High] | Microsoft Entra ID + OIDC | Okta/Auth0 (fine, but PepsiCo is Azure-native per paper) |
| REST API | Decouple UI from compute (documented principle: loose coupling) | JSON scenarios, run status, results | decoupling [DOC p.14]; REST specifics [INF-High] | FastAPI | Flask (simpler, less typed), Node/Express (JS stack), GraphQL (flexible reads, more complexity) |
| Scenario/config service | Externalized business logic is a first-class principle | rule templates + params → validated JSON | [DOC p.10,14, App.B] | Git-versioned JSON + Postgres metadata | DB-only rules (queryable but loses file-based portability the paper emphasizes) |
| Orchestrator | Connect UI submissions to compute; schedules; retries | scenario id → pipeline run | ADF [DOC p.13] | Azure Data Factory | Airflow (portable, code-first), Dagster (assets), Prefect (lightweight) |
| ETL layer | Retailer heterogeneity demands harmonization | raw feeds → clean weekly facts | challenges + checks [DOC p.7] | Databricks PySpark + dbt | Synapse pipelines, Fivetran+dbt (less custom logic room) |
| Warehouse | Single source of truth for facts/features/results | clean facts ↔ everything | implied "underlying data" [DOC p.14]; product not named | Delta Lake (lakehouse) | Snowflake (great SQL, extra copy), BigQuery/Redshift (non-Azure) |
| Feature store | Reuse regular-price/promo-flag/pressure features across train & inference consistently | features per SKU-banner-week | [INF-Med] — feature engineering documented, store not | Databricks Feature Store | Feast (OSS, more ops), plain Delta tables (simplest, adequate here) |
| Forecast model svc | Demand lift estimates for promo scheduling | features → ŷ demand per (PPG, week, promo, pressure grid) | [DOC p.7–9] | LightGBM (documented) | XGBoost/CatBoost (comparable; paper names LightGBM) |
| Elasticity model svc | Own/cross elasticities + uncertainty under data scarcity | panel + priors → posterior E matrix | [DOC p.11–13] | STAN ADVI (documented) | PyMC/NumPyro (Pythonic; ADVI/SVI equivalents), full MCMC (better UQ, too slow at this scale [DOC p.12]) |
| PWL fitter | Bridge nonlinear ML → linear MILP | ML curve grid → breakpoints (b, d) | [DOC p.8–9] | SciPy SLSQP + knee detection (documented) | pwlf library; fixed uniform grids (simpler, worse fit-per-segment) |
| Pricing engine (Promo) | Search 10⁶⁺ combinations under 10⁵ constraints with bounded gap | forecasts + rules → calendar | [DOC p.9–10,15, App.A] | Gurobi 12 (documented) | CPLEX (peer), HiGHS/CBC (free; slower at this scale — risk on 10⁵-constraint instances), OR-Tools CP-SAT (different modeling style) |
| Pricing engine (Price) | Global search over non-convex, discontinuous (PPP) landscape | E matrix + config → price vector | [DOC p.13,15–16, App.C] | SciPy DE, seeded ensemble (documented) | CMA-ES, simulated annealing, Optuna (comparable heuristics); NLP solvers like IPOPT (get stuck: non-convex + step functions) |
| Rule engine | Compile JSON templates → solver constraints | JSON → constraint objects | [DOC p.10,14, App.B] | In-house Python template library (documented pattern) | Drools/OPA (generic rule engines — poor fit for math-programming constraints) |
| Workflow scheduler | 16 refreshes/yr, quarterly retrains, cycle-based pricing runs | calendar → triggered pipelines | cadence [DOC p.17,19]; scheduler product [INF-High] | ADF triggers | Airflow cron; Databricks Jobs |
| Alerting/monitoring | Long-running solves (10h) and data feeds need observability | run metrics → alerts | callbacks/gap monitoring [DOC p.15]; alert stack [INF-High] | Azure Monitor + App Insights | Prometheus/Grafana, Datadog |
| Logging/audit | Regulated pricing decisions; scenario JSON = natural audit record | every run: who/what/when/config hash | [INF-High; JSON configs documented make this trivial] | Log Analytics + immutable run store | ELK stack |
| BI layer | Post-hoc performance review; realized vs predicted | results + actuals → dashboards | [INF-Med] | Power BI | Tableau/Looker |

**Data flowing between components (the two golden paths):**
1. **Promo path:** retailer/syndicated feeds → ETL harmonization → weekly fact tables → LightGBM training (temporal CV) → model registry → scenario submitted (JSON) → ADF → Databricks job: evaluate forecast on discount-pressure grid → fit PWL breakpoints → build MILP (rules from JSON) → Gurobi server → calendar + KPIs → results store → UI grid → planner edits/simulates → (loop) → accepted calendar → export + audit.
2. **Pricing path:** sales/financial/conjoint + product master → panel builder (regular price, promo flags, semi-region aggregation) → STAN/ADVI fit → validation gates → elasticity matrix E → scenario (KPI + bounds JSON) → DE ensemble ×10 → best feasible price vector + KPI deltas → UI → adjusted scenarios → approved price list → export + audit.

---

# 3. Reverse-Engineering Every Workflow

The paper documents seven distinct operational workflows. Each is reconstructed below in the requested flow template. Steps marked ⊙ are documented; ◇ are inferred glue [INF-High unless noted].

## 3.1 PromoAI — calendar generation & retailer negotiation loop [DOC p.18]

```
START
  ↓ TRIGGER      ⊙ planning-cycle kickoff for a retailer (quarterly/semiannual/annual;
  │                16 refreshes/yr across markets) or ad-hoc negotiation round
  ↓ INPUT        ⊙ retailer business rules gathered by planner (budgets, promo frequency,
  │                execution windows, exclusivity), harmonized history, trained forecast
  ↓ VALIDATION   ⊙ data-quality checks (implausible price-volume combos, margin
  │                inconsistencies, missing weeks) ◇ config-schema validation of JSON
  ↓ BUSINESS     ⊙ JSON rule file assembled from template library (financial /
  │  RULES         calendar / execution categories) per market config
  ↓ TRANSFORM    ⊙ forecast evaluated on discount-pressure grid per (PPG, week, promo)
  │              ⊙ PWL breakpoints fitted (SLSQP + knee-point; 2–4 segments)
  ↓ PREDICTION   ⊙ demand vectors d at breakpoints b feed MILP data
  ↓ DECISION     ⊙ Gurobi solves MILP (objective weights ω1..ω4 from UI); callbacks
  │                terminate early on gap stagnation; target gap 1–5% by market
  ↓ HUMAN        ⊙ planner reviews scenario menu (multiple objective configs);
  │  APPROVAL      presents to retailer; retailer accepts / requests changes / opts out
  │                └─ if changes: encode new constraints → re-run (loop to BUSINESS RULES)
  ↓ PUBLISHING   ⊙ agreed calendar becomes the formal PepsiCo–retailer agreement
  │              ◇ export to trade-promotion-management / execution systems
  ↓ MONITORING   ⊙ acceptance & override tracking (85% accept rate); pilot-phase
  │                override monitoring ◇ in-flight sales tracking vs forecast
  ↓ FEEDBACK     ⊙ quarterly retrain of forecast on new data behind validation gates;
  │  LOOP          breakpoint config re-evaluated only if demand behavior shifts
END
```

## 3.2 PromoAI — manual simulation ("what-if") workflow [DOC p.18]

```
START → TRIGGER: planner edits any single promotional decision in a generated calendar
 → INPUT: current calendar + edit → VALIDATION: edit within allowed mechanics ◇
 → TRANSFORM: recompute affected PWL demand + financial aggregates (no re-solve) ⊙
 → PREDICTION: full-horizon KPIs (revenue, margin, volume, frequency) ⊙
 → DECISION: planner keeps edit or reverts, seeing quantified deltas ⊙
 → APPROVAL: edited calendar can proceed to negotiation like any scenario ⊙
 → END. Purpose: absorb qualitative knowledge (last-minute retailer asks,
   competitive responses) without losing financial visibility ⊙
```

## 3.3 PricingAI — pricing cycle workflow [DOC p.11–13, 19]

```
START
  ↓ TRIGGER      ⊙ pricing cycle (semiannual/annual, market-dependent)
  ↓ INPUT        ⊙ weekly sales panel, financials (COGS, sell-in, distribution),
  │                conjoint priors (if any), product master
  ↓ VALIDATION   ⊙ panel feature engineering: regular price = max in ±8-week window;
  │                promo flag if price >5% below; semi-region aggregation with 30%
  │                revenue-weighted promo threshold
  ↓ BUSINESS     ⊙ scenario config: KPI selection {R, Π, Vol, Margin, MS, NRW},
  │  RULES         KPI bounds α, price-change band θ, ladders γ, tiers, lines,
  │                PPP thresholds, group bounds — all via UI → JSON
  ↓ TRANSFORM    ⊙ Bayesian hierarchical fit (STAN/ADVI, hours) → posterior means
  │                assembled into elasticity matrix E (n×n incl. competitors)
  ↓ VALIDATION-2 ⊙ gates: R²>0.6, wMAPE<0.4, |bias|<10%; elasticities in (−2.5,0);
  │                cross-price signs economically consistent; end-to-end pipeline test
  ↓ PREDICTION   ⊙ volume function V_i = q⁰·exp(Σ E_ij Δln p_j + ΔPPP_i)
  ↓ DECISION     ⊙ DE ensemble (≤10 seeded parallel runs, varied mutation/crossover/
  │                population) maximizes chosen KPI; tolerance-based stop; <30 min
  ↓ HUMAN        ⊙ planner reviews price vector + portfolio KPI deltas; runs adjusted
  │  APPROVAL      scenarios (manual price edits with instant cross-effect readout);
  │                leadership sign-off ◇
  ↓ PUBLISHING   ◇ approved price list → ERP/price-master systems; retailer sell-in
  ↓ MONITORING   ⊙ realized vs predicted reviewed against historical cycles;
  │                sensitivity analyses on demand/cost assumptions
  ↓ FEEDBACK     ⊙ next cycle: sequential prior updating (old posterior → new prior)
  │                stabilizes elasticity refresh; parameters (costs, agreements) updated
END
```

## 3.4 Data ingestion & harmonization workflow [DOC p.7]

```
START → TRIGGER: retailer/syndicated feed arrival (weekly ◇) → INPUT: raw sales/price/
 promo files per retailer → VALIDATION: schema checks; missing-week detection;
 promo-label consistency; price-anomaly detection (stale system prices); SKU-id
 churn resolved via longitudinal product mappings ⊙ → RULES: market-specific promo
 definitions; item-level vs group-level promo normalization ⊙ → TRANSFORM: impute,
 correct, validate spikes against documented promos; flag implausible price-volume
 and margin inconsistencies ⊙ → PUBLISH: harmonized weekly fact tables → MONITOR:
 DQ dashboards ◇ → FEEDBACK: new retailer quirks become new harmonization rules ⊙
 ("ongoing market-specific adaptation") → END
```

## 3.5 Model retraining & release workflow [DOC p.17–18]

```
START → TRIGGER: quarterly (promo) / each pricing cycle (pricing) OR demand-behavior
 shift ⊙ → INPUT: extended history → TRANSFORM: retrain LightGBM / re-fit STAN with
 sequential priors → VALIDATION: rolling-origin backtests vs legacy benchmark;
 same gates as initial deployment; elasticity plausibility review → DECISION:
 release to planning use only if gates pass ⊙ → PUBLISHING: model registry version
 bump ◇ → MONITORING: override behavior + forecast error tracked ⊙ → END
```

## 3.6 Pilot / market-onboarding workflow [DOC p.14, 17]

```
START → TRIGGER: new market or retailer → INPUT: market data audit → RULES: author
 market JSON config instantiating existing constraint templates with local params ⊙
 (no new code) → TRANSFORM: calibrate promo flagging / price derivation / SKU matching
 for the market ⊙ → VALIDATION: backtests + business review of recommendations vs
 historical decisions; investigate deviations (forecast updates? constraint
 interactions? volume-margin trade-offs?) ⊙ → HUMAN: limited pilot with selected
 retailers; monitor user feedback + overrides ⊙ → PUBLISHING: phased expansion ⊙
 → END
```

## 3.7 Solver-run monitoring workflow [DOC p.15–16]

```
START → TRIGGER: optimization job launched → MONITOR: Gurobi callbacks watch MILP-gap
 trajectory ⊙; DE monitors population-energy convergence tolerance ⊙ → DECISION:
 early-terminate on gap stagnation (Promo) or tolerance/max-runtime (Pricing) ⊙ →
 PUBLISH: solution + gap/convergence metadata stored with run record ◇ → END
```

---

# 4. Data Flow — every source, field, and relationship

Sources marked [DOC] are explicitly named inputs in the paper; the rest are the standard CPG-RGM data estate an enterprise team would wire in [INF as marked]. Frequency/owner/checks synthesize documented statements with standard practice.

## 4.1 Source inventory

| # | Source | Documented? | Key fields | Frequency | Owner | Quality checks |
|---|---|---|---|---|---|---|
| 1 | Retailer POS / EPOS sales | [DOC p.7,11] | retailer_id, store/region, sku_id, week, units, sales_value, shelf_price, promo_flag | Weekly | Retailer / sales ops | missing weeks; spike-vs-promo validation; price-volume plausibility [DOC p.7] |
| 2 | Syndicated market data (Nielsen/IRI/Circana) | [INF-High — competitor volumes/prices are modeled (PricingAI includes competitor SKUs), vendor unnamed] | market, category, sku, week, units, value, avg_price, distribution (%ACV), share | Weekly | Insights team | vendor restatements; category coverage; SKU-mapping drift |
| 3 | Promotional event records | [DOC p.7] | event_id, retailer, ppg/sku, start/end week, mechanic_type, discount_depth, placement (front page / end-aisle / shelf tier), trade_spend | Per event; consolidated weekly | Trade marketing | inconsistent labels; item-vs-group granularity normalization [DOC p.7] |
| 4 | Shelf/display placement | [DOC p.7 "some markets"] | store/segment, sku, week, shelf_position (upper/mid/lower), display_type | Weekly/monthly | Field sales / retailer | coverage gaps by market |
| 5 | Financials — costs | [DOC p.7,11] | sku_id, cogs_per_unit, distribution_cost_per_unit, effective_date | Per cycle | Finance | stale costs at cycle start; currency |
| 6 | Financials — sell-in | [DOC p.11, Table 10] | sku_id, sell_in_price, sell_in_discount_pct, pass_through_factor φ, effective_date | Per cycle | Finance / commercial | consistency with trade agreements |
| 7 | Trade spend / budgets | [DOC p.7,10] | retailer, period, budget_amount, spend_to_date | Monthly/cycle | Commercial finance | budget vs actual reconciliation |
| 8 | Conjoint survey results | [DOC p.11–12, optional] | attribute (brand/size/taste/banner), elasticity_prior_mean, prior_sd, study_date | Per study (annual-ish) | Insights | study recency; sample representativeness |
| 9 | Product master | [DOC p.11; PPG mapping p.7] | sku_id, item_id, brand, sub_brand, size, taste/flavor, price_line, tier, ppg_id, pricing_group g(i), pack_size s_i, pepsico_flag | On change (SCD) | Master data mgmt | SKU churn → longitudinal mapping [DOC p.7]; orphan SKUs |
| 10 | Holiday/seasonal calendar | [DOC p.7 features; p.10 constraints] | market, week, holiday_name, event_flag (Super Bowl etc.) | Annual + edits | RGM ops | market-specific accuracy |
| 11 | Business-rule configs | [DOC p.10,14, App.B] | market, template_name, filter (column,value), params, version | On change | BU admin | JSON schema validation; version control |
| 12 | Competitor price actions (tactical) | [INF-Med — follow-ratios ρ documented as parameters; a live competitor-price feed is not] | competitor_sku, week, price, promo | Weekly | Insights | vendor lag |
| 13 | ERP (SAP or similar) | [INF-High — cost/sell-in data must originate somewhere; system unnamed] | material master, list prices, costs, customer hierarchy | Daily/cycle | IT/ERP team | material↔SKU mapping |
| 14 | Inventory / supply | [INF-Low — NOT used by either engine per the paper; standard adjacent feed for feasibility checks] | sku, dc/store, on_hand, in_transit | Daily | Supply chain | — |
| 15 | Weather / demand drivers | [INF-Low — not mentioned; common in CPG forecasting extensions] | region, week, temp, precip | Weekly | External API | — |
| 16 | CRM / customer hierarchy | [INF-Med — retailer/banner hierarchy is required for banner elasticity; a CRM per se is not documented] | banner_id, retailer parent, channel, semi_region mapping | On change | Sales ops | hierarchy changes mid-cycle |

## 4.2 Canonical grain and keys
- **Facts:** SKU (or PPG) × banner/retailer × geography (semi-region) × ISO week. [DOC p.11: "SKU-banner-week"; semi-region aggregation p.11]
- **Primary keys:** surrogate ids per dimension (sku_key, banner_key, week_key, geo_key); fact PK = composite of those FKs.
- **Foreign keys / relationships:**
  - product_master.sku_id 1—* weekly_sales.sku_id
  - product_master.ppg_id groups SKUs for PromoAI [DOC p.7]; pricing_group g(i) groups for PricingAI [DOC Table 9]
  - promo_event 1—* weekly_sales weeks it covers (via retailer+sku+week overlap)
  - elasticity_matrix rows/cols FK → product/pricing-group
  - scenario 1—* optimization_run 1—1 result set; scenario FK → market config version
- **Longitudinal SKU mapping table** (old_sku → new_sku, valid_from/to) is documented as necessary [DOC p.7] — treat as a first-class table, not a patch.

---

# 5. Database Design

Star schema on the clean zone; snowflake only where the product hierarchy genuinely branches (brand → sub-brand → SKU). [INF-High throughout — the paper documents required data content, not DDL.]

## 5.1 Dimensions

```sql
CREATE TABLE dim_product (
  sku_key        BIGINT IDENTITY PRIMARY KEY,
  sku_id         VARCHAR(40) NOT NULL,          -- natural key (retailer/vendor id)
  item_id        VARCHAR(40),
  title          VARCHAR(255),
  brand          VARCHAR(80),  sub_brand VARCHAR(80),
  size_desc      VARCHAR(40),  pack_size_units DECIMAL(10,3),  -- s_i (weight/vol)
  taste          VARCHAR(60),
  tier           VARCHAR(40),                    -- premium/mainstream/value (Eq.35)
  price_line     VARCHAR(40),                    -- L_k equal-increment group (Eq.36)
  ppg_id         VARCHAR(40),                    -- PromoAI grouping
  pricing_group  VARCHAR(40),                    -- PricingAI g(i)
  is_pepsico     BOOLEAN NOT NULL,
  follows_hero_sku VARCHAR(40), follow_ratio DECIMAL(6,4),  -- h(i), rho (Eq.20)
  valid_from DATE, valid_to DATE, is_current BOOLEAN        -- SCD-2
);

CREATE TABLE dim_sku_mapping (          -- longitudinal identifier churn [DOC p.7]
  old_sku_id VARCHAR(40), new_sku_id VARCHAR(40),
  reason VARCHAR(40),                   -- rationalization / relaunch
  valid_from DATE, PRIMARY KEY (old_sku_id, valid_from)
);

CREATE TABLE dim_retailer (
  banner_key BIGINT IDENTITY PRIMARY KEY,
  banner_id  VARCHAR(40) NOT NULL, banner_name VARCHAR(120),
  channel VARCHAR(40), market VARCHAR(8), parent_retailer VARCHAR(120)
);

CREATE TABLE dim_geography (
  geo_key BIGINT IDENTITY PRIMARY KEY,
  store_region VARCHAR(60), semi_region VARCHAR(60), market VARCHAR(8)
);

CREATE TABLE dim_week (
  week_key INT PRIMARY KEY,             -- yyyyww
  week_start DATE, iso_week SMALLINT, month SMALLINT, quarter SMALLINT,
  year SMALLINT, holiday_name VARCHAR(60), is_holiday BOOLEAN,
  is_major_event BOOLEAN                -- Super Bowl etc. [DOC p.2]
);

CREATE TABLE dim_promo_mechanic (
  mechanic_key BIGINT IDENTITY PRIMARY KEY,
  mechanic_type VARCHAR(40),            -- Price / MultiBuy / BOGO / TPR...
  description VARCHAR(200), discount_fraction DECIMAL(5,4)   -- delta_{p,r}
);

CREATE TABLE dim_scenario (
  scenario_key BIGINT IDENTITY PRIMARY KEY,
  scenario_name VARCHAR(120), market VARCHAR(8), engine VARCHAR(10), -- PROMO/PRICE
  objective_kpi VARCHAR(12),            -- R / PI / VOL / MARGIN / MS / NRW (Eq.15)
  w1 DECIMAL(4,3), w2 DECIMAL(4,3), w3 DECIMAL(4,3), w4 DECIMAL(4,3), -- Eq.2 weights
  config_json VARCHAR(MAX) NOT NULL,    -- full JSON rules — the audit record
  config_hash CHAR(64), created_by VARCHAR(80), created_at TIMESTAMP
);
```

## 5.2 Facts

```sql
CREATE TABLE fact_weekly_sales (        -- harmonized panel [DOC p.7,11]
  sku_key BIGINT REFERENCES dim_product,
  banner_key BIGINT REFERENCES dim_retailer,
  geo_key BIGINT REFERENCES dim_geography,
  week_key INT REFERENCES dim_week,
  units DECIMAL(14,2), sales_value DECIMAL(16,2),
  shelf_price DECIMAL(10,4), effective_price DECIMAL(10,4),
  regular_price DECIMAL(10,4),          -- max in ±8-wk window [DOC p.11]
  is_promo_week BOOLEAN,                -- >5% below regular  [DOC p.11]
  distribution_pct DECIMAL(6,3),        -- %ACV
  trade_spend DECIMAL(14,2),
  PRIMARY KEY (sku_key, banner_key, geo_key, week_key)
) PARTITION BY (week_key) ;             -- + cluster by market

CREATE TABLE fact_promo_event (         -- [DOC p.7]
  event_id BIGINT PRIMARY KEY,
  banner_key BIGINT, ppg_id VARCHAR(40), mechanic_key BIGINT,
  week_start INT, week_end INT,
  discount_depth DECIMAL(5,4), placement VARCHAR(30), trade_spend DECIMAL(14,2)
);

CREATE TABLE fact_elasticity (          -- posterior means [DOC p.13, App.C.1]
  run_id BIGINT, product_i VARCHAR(40), product_j VARCHAR(40),
  elasticity DECIMAL(8,5),              -- E_ij ; i=j own-price
  posterior_sd DECIMAL(8,5),
  PRIMARY KEY (run_id, product_i, product_j)
);

CREATE TABLE fact_forecast (            -- LightGBM outputs on pressure grid
  model_version VARCHAR(40), ppg_id VARCHAR(40), week_key INT,
  mechanic_key BIGINT, discount_pressure DECIMAL(5,4),
  predicted_units DECIMAL(14,2),
  PRIMARY KEY (model_version, ppg_id, week_key, mechanic_key, discount_pressure)
);

CREATE TABLE fact_pwl_breakpoints (     -- b, d vectors [DOC p.8–9, Eq.6]
  model_version VARCHAR(40), ppg_id VARCHAR(40), week_key INT, mechanic_key BIGINT,
  bp_index SMALLINT, pressure_x DECIMAL(6,4), demand_y DECIMAL(14,2),
  PRIMARY KEY (model_version, ppg_id, week_key, mechanic_key, bp_index)
);

CREATE TABLE fact_optimization_run (
  run_id BIGINT IDENTITY PRIMARY KEY,
  scenario_key BIGINT REFERENCES dim_scenario,
  engine VARCHAR(10), status VARCHAR(16),
  started_at TIMESTAMP, ended_at TIMESTAMP,
  solver VARCHAR(20), solver_version VARCHAR(12),
  milp_gap DECIMAL(6,4),                -- final gap (Promo) [DOC p.15]
  de_seed INT, de_energy DECIMAL(18,6), -- per DE instance (Price) [DOC p.16]
  runtime_sec INT, objective_value DECIMAL(18,4)
);

CREATE TABLE fact_calendar_result (     -- PromoAI decision x_{p,w,r}=1 rows
  run_id BIGINT, ppg_id VARCHAR(40), week_key INT, mechanic_key BIGINT,
  discount_fraction DECIMAL(5,4),
  pred_units DECIMAL(14,2),
  pepsico_sales DECIMAL(16,2), pepsico_margin DECIMAL(16,2),   -- Eq.8
  retailer_sales DECIMAL(16,2), retailer_margin DECIMAL(16,2),
  is_manual_override BOOLEAN DEFAULT FALSE,                    -- simulation edits
  PRIMARY KEY (run_id, ppg_id, week_key)
);

CREATE TABLE fact_price_result (        -- PricingAI price vector
  run_id BIGINT, pricing_group VARCHAR(40), sku_id VARCHAR(40),
  baseline_price DECIMAL(10,4), new_price DECIMAL(10,4),
  pct_change DECIMAL(7,4),
  pred_volume DECIMAL(14,2), pred_revenue DECIMAL(16,2),
  pred_profit DECIMAL(16,2), pred_margin DECIMAL(7,4),
  pred_market_share DECIMAL(7,4), pred_nrw DECIMAL(10,4),      -- Eq.27–32
  PRIMARY KEY (run_id, sku_id)
);

CREATE TABLE fact_decision_log (        -- acceptance/override tracking [DOC p.17,19]
  run_id BIGINT, entity_id VARCHAR(60),  -- ppg-week or pricing group
  recommended VARCHAR(120), decided VARCHAR(120),
  decision VARCHAR(12),                  -- ACCEPT / OVERRIDE / REJECT
  decided_by VARCHAR(80), decided_at TIMESTAMP, reason VARCHAR(400)
);
```

## 5.3 Physical design
- **Partitioning:** all weekly facts by week_key (and market for multi-tenant tables). Retention: raw 3+ years (elasticity estimation needs long panels of infrequent price changes [DOC p.10]).
- **Indexes:** composite (sku_key, week_key) on sales; (run_id) on all result tables; hash on config_hash for scenario dedup.
- **Views:** `vw_panel_pricing` (semi-region aggregated panel with regular price + promo flags — exactly the documented feature engineering, so the model reads a view, not ad-hoc code); `vw_calendar_current` (latest accepted calendar per retailer).
- **Materialized views:** KPI baselines per scenario (K⁰ values for Eq.33 bounds); latest elasticity matrix pivoted n×n for the optimizer; refresh per cycle.
- **Normalization stance:** dimensions denormalized (star) except product brand hierarchy snowflaked one level (brand ↔ sub-brand) because both PromoAI features and PricingAI hierarchical priors consume it at different levels [DOC p.7, Table 1].

---

# 6. Pricing Engine — full mathematical reverse-engineering

Everything in this section is **[DOC, Appendices A & C]** unless flagged. Notation is rewritten in plain ASCII.

## 6.1 PromoAI: the MILP

**Sets** (Table 5): P = PPGs; P̃ ⊆ P PepsiCo-owned; W = weeks; R_p = promo options for PPG p (includes "no promo" base); R̃_p = active (non-base) promos; S_p = PPGs in the same size/format segment as p (excluding p); L = locked competitor promo slots (p,w,r).

**Decision variable:** x[p,w,r] ∈ {0,1} — 1 if promotion r is selected for PPG p in week w.

**Objective (Eq. 2)** — user-weighted blend of PepsiCo and retailer outcomes:

```
max  w1·[ (1−w3)·S_P + w3·Pi_P ]  +  w2·[ (1−w4)·S_R + w4·Pi_R ]
     with w1 + w2 = 1
S_P / Pi_P = total PepsiCo sales / margin ;  S_R / Pi_R = retailer sales / margin
w1=1, w3=0  →  maximize PepsiCo revenue only (documented example)
```

**Constraints:**

```
(3)  Exclusivity:            Σ_{r∈R_p} x[p,w,r] = 1              ∀ p,w
     (every PPG gets exactly one option per week, incl. "no promo")

(4)  Effective discount:     alpha[p,w] = Σ_r delta[p,r]·x[p,w,r]

(5)  Competitive discount    beta[p,w] = (1/|S_p|)·Σ_{p'∈S_p} alpha[p',w]
     pressure:               (average promo aggressiveness of same-segment rivals —
                              this is how cannibalization enters demand)

(6)  PWL demand:             qhat[p,w,r] = PWL(beta[p,w]; b[p,w,r], d[p,w,r])
     (piecewise-linear interpolation of the LightGBM forecast in beta,
      breakpoints b (x-coords) and demand values d (y-coords))

(7)  Big-M activation (realized demand = conditional demand iff selected):
     (7a) qhat[p,w,r] − q[p,w,r] ≤ M·(1 − x[p,w,r])
     (7b) q[p,w,r] ≤ M·x[p,w,r]
     (7c) q[p,w,r] ≤ qhat[p,w,r]
     → at optimality q = qhat·x (objective pushes q upward)

(8)  Per-cell financials:    S_P[p,w] = Σ_r q[p,w,r]·rho_P[p,w,r]
                             Pi_P[p,w] = Σ_r q[p,w,r]·mu_P[p,w,r]
                             (same for retailer with rho_R, mu_R)
(9)  Totals:                 S_P = Σ_p Σ_w S_P[p,w]   etc.

(10) Margin conservation:    Pi_P ≥ gamma_P · S_P ;  Pi_R ≥ gamma_R · S_R
     (minimum margin-to-sales ratios for both parties)

(11) Promo-count caps:       Σ_{p,w,r∈R̃_p} x ≤ N        (all PPGs)
                             Σ_{p∈P̃,w,r∈R̃_p} x ≤ N_P   (PepsiCo-owned only)

(12) Market-share floor:     Σ_{p∈P̃,w} S_R[p,w] ≥ m · S_R
     (PepsiCo share of the retailer's total sales ≥ m)

(13) Competitor locks:       x[p,w,r] = 1  ∀ (p,w,r) ∈ L
     (benchmark calendar slots reserved for competitor promos)

(14) Domains: x binary; alpha, beta, qhat, q ≥ 0
```

**Modular JSON constraint templates** (App. A.6.12 + B): ad-block linking (complementary packs promoted together), min/max consecutive promo duration (`MinMaxPromoDuration`: 2–4 weeks for pepsico_flag=true), weekly caps per mechanic (`WeeklyMaxPromo`: ≤4 "Price" promos/week), seasonal/holiday locks or exclusions, minimum spacing of k weeks between successive promos (anti pantry-loading). Each template = a Python class instantiated from JSON `data` filters (arbitrary column predicates) + parameters. **This is the rule engine**: JSON → template instantiation → rows of MILP constraints.

**Solver operations [DOC p.15]:** Gurobi 12 on a dedicated server; hundreds of thousands of variables/constraints; runtimes minutes → 7–9h; callback monitors incumbent/bound trajectory and stops when gap improvement stagnates; market-policy target gaps 1% (precision-critical) to 5% (turnaround-critical).

**Financial semantics:** rho (unit revenue) and mu (unit margin) are *pre-computed per (p,w,r)* from price, cost, and trade-spend data — margins are input parameters, not solver-computed [DOC Table 6]. Trade-spend limits and revenue targets enter as additional financial constraints [DOC p.10].

## 6.2 PricingAI: the nonlinear program solved by DE

**Decision variable:** x_g per pricing group g ∈ G (products in a group move together via mapping g(i)):
- Percentage mode (US): x_g ∈ ℝ, multiplicative factor → p_new_i = x_g(i)·p0_i (Eq. 17)
- Absolute mode (Mexico): x_g ∈ ℤ, integer pesos → p_new_i = x_g(i) (Eq. 18)
- Bounds: x_lo ≤ x_g ≤ x_hi (Eq. 16)

**Competitor reaction (Eq. 19–21):**
```
Delta_i = (p_new_i − p0_i)/p0_i                      (PepsiCo products)
Delta_i = rho_i · Delta_{h(i)}                       (competitor i follows hero h(i))
p_final_i = (1 + Delta_i) · p0_i                     (all products)
```

**Demand model (Eq. 22–24)** — the core of the engine:
```
E_i(p) = Σ_j E[i,j] · ( ln p_j − ln p0_j )           cross-price log-response
PPP_i(p) = Σ_{k∈K_i : p_i ⊵ tau_ik} psi_ik           psychological step function
D_i(p) = PPP_i(p) − PPP_i(p0)                        threshold-crossing delta
V_i(p_final) = q0_i · exp( E_i(p_final) + D_i(p_final) )
```
At baseline prices both terms vanish → V_i = q0_i (self-consistency). E[i,i] < 0 (own-price), E[i,j] > 0 for substitutes, < 0 for complements. PPP makes the surface **discontinuous** — crossing $1.99 or 20 pesos causes a demand jump of magnitude psi — which, with exp/log terms, is exactly why DE (not gradient NLP) is used [DOC C.6.1].

**Financial KPIs (Eq. 25–32)** over subset S (usually PepsiCo products):
```
U_i  = V_i / s_i                                     units from volume via pack size
cSIP_new_i = cSIP_i · (1 + Delta_i·phi)              sell-in pass-through
R_S  = Σ_{i∈S} U_i · cSIP_new_i · (1 − d_i)          revenue (net of sell-in discount)
Pi_S = Σ_{i∈S} [ R_i − (cCOGS_i + cDist_i)·U_i ]     profit
Margin_S = Pi_S / R_S
Vol_S = Σ V_i
MS_S  = Σ_{i∈S} U_i·p_final_i / Σ_{i∈A} U_i·p_final_i    market share (ratio → nonlinear)
NRW_S = R_S / Vol_S                                  net revenue per unit weight
```
**Objective (Eq. 15):** maximize ONE user-selected KPI ∈ {R, Pi, Vol, Margin, MS, NRW}.

**Constraints:**
```
(33) KPI bounds:      alpha_K_lo · K0 ≤ K(x) ≤ alpha_K_hi · K0    for any chosen KPIs
     (e.g., alpha_R_lo = 0.95 → revenue may not fall >5% below baseline)
(34) Price ladder:    p_new_i/s_i ≥ gamma · p_new_{i+1}/s_{i+1}
     within each brand unit, packs sorted by size desc —
     bigger packs must be cheaper per unit weight (gamma=1 strict, ≤1 tolerance)
(35) Tier ordering:   p_new_u ≥ p_new_t  ∀ u ∈ UpperTiers(t)      premium ≥ value
(36) Pricing lines:   |p_new_i − p0_i| = |p_new_j − p0_j|  ∀ i,j in line L_k
     (equal absolute increments across a commercial line)
(37) Avg price band:  theta_lo ≤ 100·Σ(p_new−p0)/Σp0 ≤ theta_hi   (unweighted)
(38–39) Volume-weighted avg price band: same with weights V_i(p_final) —
     endogenous (weights depend on decision) → extra nonlinearity
```

**DE solver operations [DOC p.16]:** SciPy implementation; ensemble of ≤10 parallel instances with different seeds + hyperparameters (mutation factor, crossover probability, population size), distributed on Databricks; fixed seeds → reproducible; stop on population-energy relative tolerance (set very low) with max-runtime safeguard; average < 30 min. Constraint handling [INF-High]: penalty terms added to the objective for violated constraints + bounds handled natively — standard SciPy DE practice; the paper says "natural handling of constraints" without naming the mechanism.

## 6.3 Elasticity, margin, promotion, forecasting, inventory, region, segmentation — where each lives

| Concern | PromoAI | PricingAI |
|---|---|---|
| Elasticity | implicit in LightGBM demand curve vs discount pressure | explicit Bayesian E matrix (own + cross) [DOC] |
| Margin | input parameters mu per (p,w,r); floors Eq.10 | computed from COGS+distribution vs sell-in Eq.28–29 [DOC] |
| Revenue | Eq.8–9 | Eq.27 [DOC] |
| Promotion impact | the entire decision space | promo weeks flagged & excluded from regular-price estimation; promo-discount elasticity term in model [DOC p.11–12] |
| Demand forecasting | LightGBM global model | multiplicative Bayesian volume model [DOC] |
| Inventory impact | **absent** — no inventory constraint anywhere in either formulation [DOC by omission; add as extension if needed] | absent |
| Regional pricing | market-level deployments | semi-region aggregation; market modes (US %/continuous, MX integer pesos) [DOC] |
| Customer segmentation | channel/retailer-level (per-retailer calendars) | banner elasticity component (Table 1); **no shopper-level segmentation** [DOC by omission] |
| Price recommendation | calendar (mechanic × week) | price vector per pricing group [DOC] |

If you extend with inventory: add `q[p,w,r] ≤ available_supply[p,w]` to PromoAI (linear, trivial) and a volume-feasibility post-check to PricingAI [INF-Med design suggestion].

---

# 7. AI Components — every model, in depth

## 7.1 Model inventory

| # | Model | Type | Documented? |
|---|---|---|---|
| 1 | Promotional demand forecaster | LightGBM global regression | [DOC p.7–8, Table 4] |
| 2 | Elasticity estimator | Bayesian hierarchical (STAN + ADVI) | [DOC p.11–13] |
| 3 | PWL breakpoint fitter | SLSQP curve approximation + knee-point selection | [DOC p.8–9] |
| 4 | Promo calendar optimizer | MILP (Gurobi) | [DOC — OR, not ML] |
| 5 | Price optimizer | Differential Evolution ensemble | [DOC — metaheuristic] |
| — | LLMs, embeddings, vector search, RAG, knowledge graphs, agents | **None present in the documented system** | [DOC by omission — see §14 for optional modern extensions] |

## 7.2 Model 1: LightGBM promotional forecaster [DOC]
- **Input features:** temporal (week-of-year, month, quarter, year, holiday indicators), promotional (discount depth, mechanic type, placement, retailer base price, actual transaction price), product (PPG id, brand, category), competitive discount pressure (the beta variable — evaluated on a grid at inference).
- **Target:** weekly unit sales per PPG. **Objective/loss: MAPE** (robust to outliers, scale-free across heterogeneous PPG volumes).
- **Training protocol:** single global model pooling all PPGs (cross-learning for sparse products); randomized hyperparameter search with **temporal holdout CV** (most recent weeks reserved — mimics forward planning); early stopping patience 50.
- **Documented hyperparameters (Table 4):** num_leaves 269 (range 50–500); learning_rate 0.0185 (loguniform e⁻⁴…e²); n_estimators 302 (300–600); min_split_gain 0.00456 (0.001–0.01); max_depth 184 (100–250); reg_alpha 6.04 (0–10); reg_lambda 4.95 (0–10).
- **Documented feature-importance ordering:** PPG id ≫ year, holiday, week-of-year, quarter ≫ mechanic type, retailer base price, transaction price, promo depth, month.
- **Evaluation:** rolling-origin backtesting vs legacy benchmark, MAPE at SKU–retailer–week [DOC p.17].
- **Retraining:** quarterly, gated by the same validation thresholds as initial release [DOC p.17].
- **Inference pattern:** batch evaluation over a (PPG × week × promo option × pressure-grid) lattice to produce curves for PWL fitting — not a real-time endpoint [DOC p.8; INF-High on batch framing].
- **Drift handling:** breakpoint configs retained across cycles "unless a significant change in demand behavior warrants re-evaluation" [DOC p.9] — i.e., drift is reviewed at retrain time; plus pilot-phase override monitoring as a behavioral drift signal [DOC p.17]. A statistical drift monitor (PSI on features, error trending) is an [INF-High] addition any rebuild should include.

## 7.3 Model 2: Bayesian hierarchical elasticity model [DOC]
- **Data:** SKU-banner-week panel; multiplicative volume model. Regressors: own log-price, cross log-prices, promotional discount elasticity, binary promo flag, distribution, monthly seasonality. Observation weighting: recency (recent weeks weigh more) and volume importance (bigger SKUs weigh more).
- **Hierarchical structure:** own-price elasticity = **additive** sum of grand (category) + brand + size + taste + banner components (Table 1) — collapses one-coefficient-per-SKU-banner into a small shared set, solving the "more parameters than price events" identification problem. Cross-price elasticities = **multiplicative** decomposition over sub-brand/size/taste, bounded (0,1).
- **Priors:** conjoint results (when available) provide prior means; tight prior SD on brand & taste (conjoint trusted), wide on size & banner (data trusted). **Softmax penalty** pushes own-price elasticities negative regardless of sparsity. Sequential updating: previous cycle's posterior → next cycle's prior [DOC p.18].
- **Inference:** STAN with **ADVI** (variational) — hours, tractable at portfolio scale; full MCMC rejected implicitly for cost [DOC p.12].
- **Outputs:** posterior distributions; **posterior means** assembled into E (n×n incl. competitors) for the optimizer — a documented, deliberate tractability trade-off [DOC p.22].
- **Validation gates [DOC p.12]:** (i) in-sample R²>0.6, wMAPE<0.4, |bias|<10% at SKU-banner-week; (ii) business review: own-price in (−2.5,0), cross-price positive for substitutes at brand-unit/size level; (iii) end-to-end optimization run on the validated matrix.
- **Retraining:** each pricing cycle, gates re-applied; sequential priors damp cycle-to-cycle swings [DOC p.17–18].

## 7.4 Model 3: PWL breakpoint fitter [DOC p.8–9]
Two-stage: (1) for fixed segment count k, jointly optimize breakpoint (x,y) coordinates by **SLSQP** minimizing MSE against the LightGBM curve; (2) sweep k, pick via **knee-point detection** on error-vs-k (diminishing returns). Result: 2–4 breakpoints suffice; config cached per market across cycles.

## 7.5 MLOps around the models
- **Versioning & separation:** models trained/versioned separately from optimization code — documented principle [DOC p.14]. Implement with MLflow registry [INF-High].
- **Monitoring:** solver gap trajectories (documented callbacks), forecast error per retrain (documented backtests), acceptance/override rates (documented) — plus standing dashboards [INF-High].
- **Reproducibility:** DE seeds fixed (documented); pin model versions + config hash per run [INF-High].

---

# 8. Business Rules — complete IF/THEN/ELSE catalog

All rules below are documented unless flagged. "Depends on" lists the data/config the rule needs; edge cases synthesize documented statements and standard failure modes.

**R1 — Weekly exclusivity [DOC Eq.3]**
IF week w in horizon AND PPG p active THEN exactly one option r ∈ R_p is assigned (base "no promo" counts) ELSE calendar invalid. Depends: promo option catalog per PPG. Edge: PPG delisted mid-horizon → shrink W for that PPG [INF-High].

**R2 — Margin floors [DOC Eq.10]**
IF total PepsiCo margin < gamma_P × PepsiCo sales THEN solution infeasible → solver must pick different calendar ELSE ok. Same for retailer with gamma_R. Depends: unit margins mu (finance data). Exception: weights may prioritize sales (w3=0) but the floor still binds. Edge: gamma set too high → infeasible model; return diagnostic listing binding constraints [INF-High].

**R3 — Promo-count caps [DOC Eq.11]**
IF total active promos > N (all) or > N_P (PepsiCo-owned) THEN infeasible ELSE ok. Depends: slotting capacity negotiated with retailer [DOC p.2].

**R4 — Market-share floor [DOC Eq.12]**
IF PepsiCo share of retailer sales < m THEN infeasible ELSE ok.

**R5 — Competitor locked slots [DOC Eq.13]**
IF (p,w,r) ∈ locked benchmark set L THEN x=1 forced (competitor promo assumed to run) ELSE free. Why: models known competitor calendar commitments.

**R6 — Weekly cap per mechanic [DOC App.B `WeeklyMaxPromo`]**
IF count of active promos with mechanic_type="Price" in week w > 4 THEN infeasible ELSE ok. Configurable filter + threshold per market JSON.

**R7 — Promo duration window [DOC App.B `MinMaxPromoDuration`]**
IF a promo block for pepsico_flag=true runs < 2 or > 4 consecutive weeks THEN infeasible ELSE ok.

**R8 — Minimum spacing (anti pantry-loading) [DOC p.31]**
IF two successive promos for PPG p are < k weeks apart THEN infeasible ELSE ok. Why: consumers stock up and delay purchases [DOC p.2].

**R9 — Ad-block linking [DOC p.30]**
IF product A promoted in week w AND A is ad-block-linked to B THEN B must also be promoted in w ELSE infeasible.

**R10 — Seasonal/holiday alignment [DOC p.10, 30]**
IF week ∈ locked seasonal window THEN required promo forced / excluded ELSE free. Depends: dim_week holiday flags.

**R11 — Front-page exposure minimum [DOC p.10]**
IF front-page appearances for a category < contracted minimum THEN infeasible ELSE ok.

**R12 — Regular-price derivation [DOC p.11]**
regular_price(sku,week) = max(observed price in ±8-week window). IF observed price < 0.95 × regular_price THEN week flagged promotional ELSE regular.

**R13 — Semi-region promo aggregation [DOC p.11]**
IF > 30% of regional revenue in week w sold under promo THEN SKU-banner-week classified promotional ELSE regular.

**R14 — Elasticity acceptance gates [DOC p.12]**
IF R² > 0.6 AND wMAPE < 0.4 AND |bias| < 10% AND own-price ∈ (−2.5, 0) AND cross-price signs consistent THEN elasticity matrix released to optimizer ELSE model rejected → investigate/refit.

**R15 — KPI guardrails [DOC Eq.33]**
IF K(x) < alpha_lo·K0 OR > alpha_hi·K0 for any configured KPI THEN price vector rejected (penalized) ELSE feasible. Example documented: alpha_R_lo=0.95.

**R16 — Price ladder [DOC Eq.34]**
IF price-per-weight of larger pack > gamma × price-per-weight of next smaller pack THEN violation ELSE ok (gamma=1 default strict).

**R17 — Tier ordering [DOC Eq.35]** — premium tier price ≥ lower tier price, always.

**R18 — Pricing-line equal increments [DOC Eq.36]** — all products in a line move by the same absolute amount.

**R19 — Portfolio price-change band [DOC Eq.37–39]** — average (and volume-weighted average) % price change within [theta_lo, theta_hi].

**R20 — Psychological thresholds [DOC Eq.23]**
IF new price crosses threshold tau_ik (e.g., $1.99 / 20 pesos) THEN demand shifts by psi_ik (step) — optimizer sees the cliff; planners keep prices on the "right" side unless the KPI gain beats the cliff.

**R21 — Competitor follow [DOC Eq.20]**
IF PepsiCo hero h(i) changes price by Delta THEN competitor i's price moves by rho_i × Delta in the simulation ELSE competitor static. Edge: rho is an assumption — sensitivity-test it [DOC p.17 sensitivity analyses].

**R22 — Integer pricing (Mexico) [DOC p.15, Table 11]**
IF market mode = absolute THEN prices ∈ ℤ pesos ELSE continuous.

**R23 — Solver stopping [DOC p.15–16]**
PromoAI: IF MILP gap ≤ market target (1–5%) OR gap improvement stagnant (callback) THEN stop, return incumbent. PricingAI: IF population-energy relative improvement < tol THEN converged ELSE continue until max runtime.

**R24 — Model release gate [DOC p.17]**
IF retrained model passes same validation gates as initial deployment THEN release for planning ELSE keep previous version.

**R25 — Retailer opt-out [DOC p.2]**
IF retailer unwilling/unable to execute a promo THEN opt-out through formal negotiation → constraints updated → re-optimize. (This is a workflow rule, not a solver rule.)

**R26 — Acceptance gating [DOC p.19,23]**
IF recommendation not accepted+executed THEN it produces zero value — measure acceptance rate (~85%) and treat overrides as feedback data [DOC p.17].

Dependency graph (major): R12→R13→R14→(E matrix)→R15–R22 (PricingAI chain); forecast→PWL→R1–R11 (PromoAI chain); R23–R26 govern both.

---

# 9. User Journey — role by role

**Trade/Promo Planner (PromoAI)** [DOC p.18]
Does: gathers retailer rules → configures scenario (objective weights, constraint set) → runs optimization → reviews calendar menu → simulates manual edits → presents to retailer → re-optimizes on feedback → finalizes calendar. Screens: scenario builder, constraint configurator, calendar grid, KPI panel, comparison view. Permissions [INF-High]: create/run scenarios in own market; edit market JSON params within allowed templates; cannot alter model versions. Decisions: which scenario to present; whether to accept model recommendation per cell (85% accept). AI interaction: consumes forecasts implicitly through calendars; sees predicted KPI deltas for every manual change.

**Pricing Manager (PricingAI)** [DOC p.19]
Does: sets KPI objective + guardrail bounds → runs optimization → reviews price vector & portfolio impacts → adjusted scenarios (manual price edits with instant cross-effect readout) → sign-off package for leadership. Screens: pricing scenario builder, price board, elasticity explorer, adjusted-scenario editor. Permissions: same model as planner. Decisions: final recommended price list. AI interaction: sees elasticity-driven volume/revenue/margin/share predictions per what-if.

**Sales / Key Account Manager** [DOC p.18–19]
Does: presents calendar menu to retailer; collects feedback ("menu of analytically grounded options... structured commercial negotiation"); relays opt-outs. Screens: read-only calendar/scenario comparison; export. Decisions: negotiation strategy. AI interaction: indirect — uses scenario KPIs as negotiation evidence (documented retailer quote praises exactly this).

**Data Scientist** [DOC p.14, 17]
Does: trains/versions models, monitors gates, runs backtests, tunes breakpoints, investigates recommendation anomalies during validation reviews. Screens: notebooks, model registry, validation dashboards. Permissions: full model + pipeline access; cannot approve business scenarios [INF-High]. AI interaction: builds it.

**BU / Market Admin** [INF-High, role implied by config-driven onboarding DOC p.14]
Does: authors market JSON configs; onboards markets; maintains constraint template parameters. Screens: config editor with schema validation, version history.

**Finance Reviewer [INF-Med]**
Does: supplies costs/sell-in; reviews margin outcomes vs floors. Screens: BI dashboards, results export.

---

# 10. UI Reverse-Engineering

The paper documents UI *capabilities* (scenario configuration, objective weight selection, constraint bounds entry, calendar review, manual edits with live KPI recalculation, adjusted price scenarios) [DOC p.9,13,18–19] but no screenshots. Wireframes below are [INF-High] reconstructions of the documented behaviors.

**Screen 1 — Scenario Builder (both engines)**
```
┌ Scenario: "Q3 Retailer-X balanced" ────────────────────────────┐
│ Market [MX ▾]  Retailer [X ▾]  Horizon [26 wks ▾]  Engine [Promo]│
│ Objective:  PepsiCo w1 [0.6]══╪══ Retailer w2 [0.4]             │
│             Sales (1−w3) ══╪══ Margin w3 [0.3]   (same for w4)  │
│ Constraint set: [Market default ▾] + overrides:                 │
│   ▸ Financial (3 active)  ▸ Calendar (5)  ▸ Execution (2)       │
│ [Validate config] [Run optimization]        est. runtime ~2h    │
└─────────────────────────────────────────────────────────────────┘
```
Widgets: weight sliders (Eq.2), template pickers, JSON preview toggle. Buttons: validate (schema + feasibility pre-checks), run, clone scenario.

**Screen 2 — Constraint Configurator**
```
┌ WeeklyMaxPromo ────────────── [ON] ┐  ┌ MinMaxPromoDuration ── [ON] ┐
│ filter: mechanic_type = Price      │  │ filter: pepsico_flag = true │
│ week_max_promo: [4]                │  │ min [2]  max [4] weeks      │
└────────────────────────────────────┘  └─────────────────────────────┘
[+ Add rule from template library ▾]           [View compiled JSON]
```
Mirrors Code 1 exactly — every form field maps 1:1 to the JSON template schema.

**Screen 3 — Calendar Grid (PromoAI core screen)**
```
        W1   W2   W3   W4   W5   W6 ...          KPIs (scenario vs baseline)
PPG-A  [ - ][20%][20%][ - ][ - ][BOGO]           Revenue  +4.2%  ▲
PPG-B  [2/$5][ - ][ - ][2/$5][ - ][ - ]          Margin   +1.1%  ▲
PPG-C  [ - ][ - ][15%][15%][15%][ - ]            Volume   +6.0%  ▲
...                                              Promo count 37/40
Cell click → edit promo → all KPIs recalc instantly (simulation mode)
Legend: color by mechanic; lock icon = seasonal/competitor lock
```
Filters: PPG, brand, mechanic, week range. Actions: edit cell, compare scenarios side-by-side, export, submit-to-retailer package.

**Screen 4 — Price Board (PricingAI core screen)**
```
Group      Baseline  New     Δ%    Vol Δ%   Rev Δ%   Margin Δpp  Ladder
Brand-A 2L   $2.29   $2.49  +8.7%  −5.2%    +3.1%    +0.8        ✓
Brand-A 1L   $1.79   $1.89  +5.6%  −2.9%    +2.3%    +0.5        ✓
Brand-B 2L   $1.99   $1.99   0%    +1.4%*   +1.4%    0           ✓
* cross-price gain from Brand-A increase        [Adjust price ▸ live re-sim]
Portfolio: Revenue +2.8% | Profit +3.4% | Share −0.3pp | Avg Δ +4.1% (band 0–5)
```
Adjusted-scenario editor: type any price → immediate portfolio-wide recompute incl. cross-effects [DOC p.19]. Warnings: PPP threshold crossings highlighted ("crosses $1.99 → demand step −ψ").

**Screen 5 — Run Monitor** — gap/energy trajectory chart, ETA, early-stop reason [DOC p.15–16 callbacks], run history with config hash.
**Screen 6 — Elasticity Explorer [INF-Med]** — heatmap of E matrix, posterior intervals, gate status per component.
**Screen 7 — Approvals & Audit [INF-High]** — decision log per recommendation (accept/override/reason), scenario diff viewer.

Navigation: Market → Engine → Scenario list → (Builder | Results | Compare | Approvals). Dashboard landing: cycle calendar, runs in flight, acceptance-rate KPI, model-version banner.

---

# 11. API Design [INF-High — no API is documented; this is the standard decoupled implementation of the documented UI→JSON→pipeline flow]

Base: `https://rgm.{company}.com/api/v1` · AuthN: OAuth2 client-credentials / OIDC bearer (Entra ID) · All responses JSON · Errors RFC-7807 problem+json · Pagination: `?page=&page_size=` + `X-Total-Count` · Versioning: URI (`/v1`) + additive-only within a version · Idempotency: `Idempotency-Key` header on POSTs.

| Endpoint | Method | Purpose |
|---|---|---|
| /scenarios | GET/POST | list/create scenarios (body = config JSON incl. weights, rules) |
| /scenarios/{id} | GET/PUT/DELETE | fetch/update/archive |
| /scenarios/{id}/validate | POST | schema + pre-feasibility check |
| /scenarios/{id}/runs | POST | launch optimization (returns run_id; async) |
| /runs/{id} | GET | status, gap/energy, ETA |
| /runs/{id}/results | GET | calendar rows or price vector (paginated) |
| /runs/{id}/simulate | POST | manual edit(s) → recomputed KPIs (synchronous, no solver) |
| /elasticities/{version} | GET | E matrix + gate metadata |
| /forecasts/{version}/curves | GET | demand curves / breakpoints per PPG-week |
| /decisions | POST | record accept/override with reason |
| /configs/{market} | GET/PUT | market rule JSON (versioned) |
| /models | GET | registry: versions, gates passed, training window |
| /webhooks/run-completed | — | outbound event to subscribers |

OpenAPI sketch:
```yaml
openapi: 3.0.3
info: {title: RGM Optimization API, version: 1.0.0}
paths:
  /scenarios/{id}/runs:
    post:
      security: [{oauth2: [rgm.write]}]
      parameters: [{name: id, in: path, required: true, schema: {type: string}}]
      responses:
        "202": {description: Accepted,
                content: {application/json: {schema:
                  {type: object, properties:
                    {run_id: {type: string}, status: {enum: [QUEUED]}}}}}}
        "409": {description: Duplicate Idempotency-Key}
        "422": {description: Config failed validation}
  /runs/{id}/simulate:
    post:
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                edits:
                  type: array
                  items: {type: object, properties:
                    {entity: {type: string},        # "PPG-A|W23" or "group-12"
                     value:  {type: string}}}       # mechanic id or price
      responses:
        "200": {description: KPI deltas for full horizon/portfolio}
```
Error codes: 400 malformed; 401/403 auth; 404; 409 idempotency/version conflict; 422 rule-schema violation (return the offending template + field); 429 rate limit; 500/503 with run-id correlation. Security: scopes rgm.read / rgm.write / rgm.admin per market claim; payload size limits; audit interceptor writes every mutating call to the decision log.

---

# 12. Event Flow — what triggers what

| Event | Source | Triggers | Documented? |
|---|---|---|---|
| Planning-cycle date reached | scheduler | data refresh → retrain check → scenario prep (16 promo refreshes/yr; semiannual/annual pricing) | [DOC p.3,19] |
| Scenario submitted | UI/API | ADF pipeline → compute job | [DOC p.13] |
| Retailer feedback received | planner | constraint edit → re-optimization loop | [DOC p.18] |
| Manual edit in calendar/price board | UI | synchronous simulate recompute | [DOC p.18–19] |
| Data feed arrival | retailer/vendor | ETL + DQ checks → panel refresh | [DOC p.7; scheduling INF-High] |
| Retrain window (quarterly / cycle) | scheduler | train → gates → registry release or hold | [DOC p.17] |
| Demand-behavior shift detected | DS review / monitor | breakpoint re-evaluation | [DOC p.9] |
| Gap stagnation | Gurobi callback | early termination of solve | [DOC p.15] |
| DE tolerance met / max runtime | solver | stop, select best ensemble member | [DOC p.16] |
| Recommendation decided | planner | decision log entry (accept/override) | [DOC p.17,19] |
| Price change executed (competitor) | market intelligence | scenario re-run with updated follow params | [INF-Med] |
| Approval granted | approver | publish/export to execution systems | [INF-High] |
| Run failed | orchestrator | retry policy → alert | [INF-High] |

---

# 13. Automation Architecture

- **Orchestration:** ADF pipelines connect UI → Databricks jobs [DOC p.13]. Pipeline stages: validate-config → prepare-data → (train-if-stale) → evaluate-curves/fit-PWL or fit-elasticities → build-model → solve → persist-results → notify. [Stage decomposition INF-High.]
- **Scheduling:** cycle calendars (promo refresh cadence, pricing cycles, quarterly retrains) [DOC p.17,19]; implement as ADF triggers/Databricks Jobs cron [INF-High].
- **Retries:** transient-failure retries with backoff at orchestrator level; solver runs resume-from-incumbent where possible (Gurobi warm start) [INF-High].
- **Error handling:** config validation errors returned to UI pre-run (fail fast); infeasible models return IIS (irreducible infeasible subsystem) diagnostics naming the binding rules [INF-High — essential for a rule-driven system].
- **Notifications:** run completion/failure → email/Teams webhook to scenario owner [INF-High; channel unspecified in paper].
- **Logging:** structured logs per run keyed by run_id + config_hash; solver logs archived [INF-High].
- **Monitoring:** gap/energy trajectories (documented callbacks), pipeline SLAs, data-freshness sensors [DOC p.15–16 + INF-High].
- **Audit trail:** scenario JSON is the natural, complete audit record of every business decision input [DOC p.14 externalized config]; decision log records human accept/override [DOC p.17]; immutable storage [INF-High].
- **Rollback/recovery:** model registry keeps prior versions (release gate implies this [DOC p.17]); calendars/price lists versioned per run — "rollback" = re-publish previous accepted run [INF-High]; sequential priors prevent silent elasticity regime jumps [DOC p.18].

---

# 14. AI Agent Architecture

**Documented reality: there are no LLM agents in this system.** [DOC by omission — the paper's "AI" = ML forecasting + Bayesian inference + OR optimization.] The human planner performs the roles an agent framework would automate. This section therefore (a) states that clearly, and (b) specifies an optional agent layer a modern rebuild could add on top — all [INF-Low/Med design proposal], clearly not part of the original.

If you add an agent layer, map it to the documented seams (they are natural tool boundaries):

| Agent | Wraps documented capability | Tools it calls |
|---|---|---|
| Planner Agent | scenario construction workflow (p.18) | /scenarios, /configs, template library |
| Research Agent | data-gap + market-context gathering (p.7 harmonization pain) | DQ reports, feed status, market news [ext] |
| Pricing Agent | scenario sweep + candidate generation | /runs, /simulate |
| Validation Agent | gate checks + anomaly triage (p.12,17) | /models, backtest reports, IIS diagnostics |
| Approval Agent | routing + summarization for sign-off | /decisions, notification channels |
| Reporting Agent | post-cycle realized-vs-predicted narrative | BI queries, decision log |

Memory: scenario history + decision log (already first-class tables). Knowledge base: constraint-template docs + market configs (RAG over JSON/docs). Communication: orchestrator-mediated (each agent = a step emitting artifacts, human approval remains the documented hard gate — do NOT let agents auto-publish; the 85%-acceptance human-in-the-loop pattern is the system's proven trust mechanism [DOC p.19,23]). Prompt flow: system prompt = role + market config summary; context = current scenario JSON + latest run KPIs; guardrail = may only propose, never execute price changes.

An immediately valuable, low-risk LLM addition [INF-Med]: **natural-language → constraint-JSON authoring copilot** ("no more than 3 price promos in any week for PepsiCo CSDs, and never in the two weeks after Super Bowl" → validated `WeeklyMaxPromo` + spacing templates), since the JSON template schema is small, closed, and validatable.

---

# 15. Tech Stack Recommendation

**Documented anchors [DOC]:** Azure cloud · Azure Data Factory · Databricks notebooks · Gurobi 12 (dedicated server) · Python · LightGBM · STAN + ADVI · SciPy (differential_evolution, SLSQP) · JSON configuration · web UI.

**Recommended build-out (faithful-to-source column) with alternatives:**

| Layer | Faithful (Azure) | AWS equivalent | GCP equivalent | Notes |
|---|---|---|---|---|
| Cloud + identity | Azure + Entra ID | AWS + IAM/Cognito | GCP + Cloud Identity | paper is Azure-native |
| Orchestration | ADF (+ Databricks Jobs) | MWAA (Airflow) / Step Functions | Composer (Airflow) | Airflow if you want cloud-portable |
| Compute | Databricks (PySpark + jobs) | EMR/Databricks | Dataproc/Databricks | Databricks documented |
| Warehouse/lake | Delta Lake + Unity Catalog; Synapse or Snowflake for BI serving | S3+Athena/Redshift/Snowflake | BigQuery | any columnar MPP works; keep panel + results co-located |
| Transformations | dbt on the clean zone | dbt | dbt | codifies the documented harmonization rules |
| Streaming (optional) | Event Hubs / Kafka | MSK | Pub/Sub | only if feeds become intraweek; weekly batch documented |
| Feature store | Databricks FS or plain Delta | SageMaker FS / Feast | Vertex FS / Feast | modest needs — plain tables suffice |
| ML | LightGBM; MLflow registry | same | same | LightGBM documented; MLflow native on Databricks |
| Bayesian | CmdStanPy (STAN/ADVI) | same | same | PyMC/NumPyro acceptable substitutes |
| MILP solver | Gurobi (commercial license, ~$50–100k/yr scale) | same | same | free fallback HiGHS/CBC — benchmark first; 10⁵-constraint instances are where free solvers hurt |
| Metaheuristic | SciPy DE (+ joblib/Spark parallel ensemble) | same | same | documented |
| API | FastAPI + Pydantic (schema-validated configs) | same | same | Pydantic models mirror JSON templates |
| UI | React + TypeScript + AG-Grid + Recharts | same | same | grid-heavy UX (calendar, price board) |
| Cache/queue | Redis (simulate-endpoint memoization, run-status) | ElastiCache | Memorystore | keeps what-if latency interactive |
| Vector DB / LLM | none required (see §14); optional: Azure OpenAI + pgvector for the constraint copilot | Bedrock | Vertex | extension only |
| Containers | Docker + AKS (API/UI); solver on dedicated VM (license) | EKS | GKE | dedicated Gurobi server documented |
| IaC | Terraform + azurerm | Terraform | Terraform | modules per env |
| CI/CD | GitHub Actions / Azure DevOps: lint→unit→model-gates→integration→deploy | same | same | model gates in CI = the documented release discipline |
| Monitoring | Azure Monitor + App Insights + Grafana; MLflow for model metrics | CloudWatch | Cloud Ops | plus solver-gap dashboards |
| BI | Power BI | QuickSight | Looker | realized-vs-predicted, acceptance rates |

---

# 16. Security [entirely INF-High — the paper says nothing about security; this is the enterprise baseline such a system requires]

- **AuthN:** OIDC via Entra ID; SSO; service principals for pipelines; no local accounts.
- **AuthZ / RBAC:** roles = planner, pricing-manager, market-admin, data-scientist, viewer, approver; scoped by market claim (a MX planner cannot run US scenarios). Enforce at API gateway + row-level security in BI (market column).
- **Encryption:** TLS 1.2+ in transit; at-rest via platform keys (Storage/Databricks encryption); customer-managed keys if policy requires.
- **Secrets:** Azure Key Vault (solver license, DB creds, webhook tokens); no secrets in JSON configs or notebooks; rotate service credentials.
- **PII:** essentially none — data is B2B commercial (SKU×retailer×week aggregates). Treat retailer commercial terms (trade spend, margins, sell-in prices) as **highly confidential business data**: restrict margin fields to finance-scoped roles; watermark exports.
- **Audit logs:** immutable decision log + scenario JSON + run metadata (who/what/when/config-hash); solver logs retained; export events logged.
- **Compliance:** SOC 2 Type II controls (access reviews, change management, monitoring) if offered as a service; GDPR is low-touch (no consumer personal data) but applies to user accounts; antitrust caution [important]: competitor price data must come from lawful public/syndicated sources — never from competitor coordination; document data provenance.
- **Model governance:** validation gates + release approvals recorded (documented practice [DOC p.17] — formalize as sign-off records).

---

# 17. Implementation Roadmap

Team assumed: 1 PM, 1 product designer, 2 data engineers, 2 ML/OR engineers, 2 backend, 1 frontend, 0.5 DevOps ("squad of ~9"). Timelines assume one pilot market + one retailer first (the documented rollout pattern [DOC p.17]).

**Phase 1 — Foundations & data (weeks 1–8)**
Deliverables: cloud landing zone (IaC), raw/clean zones, harmonization ETL for pilot retailer (missing weeks, promo-label normalization, SKU longitudinal mapping), product master + PPG/pricing-group tables, DQ checks + dashboards, dim/fact schema live. Dependencies: retailer/syndicated feed contracts; finance cost extract. Risks: data heterogeneity worse than expected (the paper's #1 documented pain) → mitigate with per-market calibration budget. Effort: ~2 DE full-time + 1 backend. Exit: panel view passes DQ; 2+ years clean history.

**Phase 2 — Models (weeks 6–16, overlaps)**
Deliverables: global LightGBM forecaster + temporal-CV tuning + MAPE backtests vs naive/legacy baseline; PWL fitter (SLSQP + knee) with cached breakpoint configs; STAN/ADVI hierarchical elasticity model + validation gates + review notebook; MLflow registry + release gating. Dependencies: Phase 1 panel. Risks: elasticity identification weak if price variation minimal → conjoint priors (documented mitigation); ADVI convergence tuning. Effort: 2 ML/OR. Exit: gates R²>0.6/wMAPE<0.4/|bias|<10% met on pilot data; forecast beats baseline.

**Phase 3 — Optimization engines (weeks 12–24)**
Deliverables: MILP builder (structural constraints + template library: exclusivity, pressure, PWL, big-M, margin floors, caps, share floor, locks, spacing, duration, weekly-mechanic caps, ad-block, seasonal) compiled from JSON; Gurobi integration + callbacks + gap policy; DE ensemble runner (10 seeded parallel instances, penalty-based constraints, tolerance stop); KPI calculator (Eq.25–32); infeasibility diagnostics (IIS → rule names). Dependencies: Phase 2 outputs. Risks: solver runtimes → PWL segment tuning, PPG aggregation (documented levers); Gurobi licensing lead time. Effort: 2 ML/OR + 1 backend. Exit: reproduce a historical quarter's plan; constraints verified satisfied; runtime within SLO.

**Phase 4 — Platform & UI (weeks 16–30)**
Deliverables: FastAPI service (scenarios, runs, results, simulate, decisions, configs), orchestrated pipelines (ADF/Airflow), scenario builder + constraint configurator + calendar grid + price board + adjusted-scenario editor + run monitor, RBAC, audit/decision log, notifications. Dependencies: Phase 3 engines callable as jobs. Risks: what-if latency (must feel instant — documented adoption driver) → cache demand curves; simulate endpoint recomputes financial algebra only, never re-solves. Effort: 2 backend + 1 frontend + designer. Exit: planner completes an end-to-end scenario→negotiation→re-run cycle unaided.

**Phase 5 — Pilot, hardening, scale (weeks 28–40+)**
Deliverables: pilot with one retailer (documented pattern); override monitoring; realized-vs-predicted BI; retraining automation (quarterly/cycle) with gates; sequential-prior updating; market-onboarding playbook (JSON config authoring); second market via config only — target zero engine code changes (the documented scalability claim); SOC2-track controls. Risks: trust/adoption (documented as the hardest problem) → invest in transparency features first; recommendation infeasibility at retailer → keep re-optimization loop fast. Exit: acceptance rate trending toward the documented ~85%; cycle time weeks→minutes demonstrated.

---

# 18. Production Repository Structure

```
rgm-platform/
├── README.md
├── pyproject.toml                    # single Python workspace (uv/poetry)
├── docker/
│   ├── api.Dockerfile
│   ├── solver-worker.Dockerfile      # gurobi runtime + license mount
│   └── ui.Dockerfile
├── terraform/
│   ├── modules/{network,databricks,adf,keyvault,aks,storage,monitor}/
│   └── envs/{dev,stage,prod}/
├── config/
│   ├── schemas/                      # JSON Schema for every constraint template
│   │   ├── weekly_max_promo.schema.json
│   │   ├── min_max_promo_duration.schema.json
│   │   └── pricing_scenario.schema.json
│   └── markets/
│       ├── mx/{promo.json,pricing.json,calendar.json}
│       └── us/{...}
├── backend/
│   ├── api/
│   │   ├── main.py                   # FastAPI app factory
│   │   ├── routers/{scenarios,runs,simulate,configs,decisions,models}.py
│   │   ├── auth/{oidc.py,rbac.py}
│   │   └── schemas/                  # Pydantic mirrors of config/schemas
│   ├── services/{scenario_service.py,run_service.py,audit.py,notify.py}
│   └── repositories/                 # warehouse/postgres access
├── etl/
│   ├── ingestion/{retailer_feeds.py,syndicated.py,finance.py,conjoint.py}
│   ├── harmonization/{promo_labels.py,price_anomalies.py,sku_mapping.py}
│   ├── quality/{checks.py,expectations/}          # price-volume plausibility etc.
│   └── dbt/{models/{staging,marts}/,tests/}
├── models/                           # ML (versioned via MLflow)
│   ├── forecasting/{train_lgbm.py,features.py,backtest.py,tuning.py}
│   ├── elasticity/{model.stan,fit_advi.py,priors.py,gates.py,sequential.py}
│   └── pwl/{fit_breakpoints.py,knee.py}
├── engines/                          # OR (versioned via releases)
│   ├── promo_milp/
│   │   ├── model_builder.py          # sets/params/vars/Eq.2–14
│   │   ├── constraints/              # one module per template (rule engine)
│   │   │   ├── base.py               # Template ABC + JSON filter compiler
│   │   │   ├── weekly_max_promo.py
│   │   │   ├── min_max_duration.py
│   │   │   ├── spacing.py  ad_block.py  seasonal.py  financial.py
│   │   ├── solver.py                 # gurobi run + callbacks + gap policy
│   │   └── postprocess.py            # calendar + KPI extraction
│   └── pricing_de/
│       ├── objective.py              # V_i, KPIs (Eq.22–32)
│       ├── constraints.py            # ladders/tiers/lines/bands/PPP penalties
│       ├── ensemble.py               # 10 seeded parallel DE runs
│       └── postprocess.py
├── pipelines/                        # orchestration definitions
│   ├── adf/ or airflow/dags/
│   │   ├── promo_cycle.py  pricing_cycle.py  retrain_quarterly.py
│   │   └── data_refresh.py
├── agents/                           # OPTIONAL §14 extension (empty by default)
│   └── prompts/constraint_copilot.md
├── frontend/
│   └── src/{pages,components/{CalendarGrid,PriceBoard,ScenarioBuilder,
│        ConstraintForm,RunMonitor},api,auth}/
├── monitoring/
│   ├── dashboards/{solver_gaps.json,forecast_error.json,acceptance.json}
│   └── alerts/{pipeline_sla.yaml,run_failure.yaml}
├── tests/
│   ├── unit/{engines,models,etl,api}/
│   ├── integration/{end_to_end_promo.py,end_to_end_pricing.py}
│   └── fixtures/{mini_market/}        # tiny synthetic market for CI solves
├── docs/
│   ├── architecture.md  onboarding_playbook.md  constraint_catalog.md
│   └── runbooks/{infeasible_model.md,slow_solve.md,gate_failure.md}
└── .github/workflows/{ci.yaml,deploy.yaml,model-gates.yaml}
```

---

# 19. Code Modules — responsibilities, interactions, patterns

| Module | Responsibility | Interacts with | Design pattern |
|---|---|---|---|
| etl.harmonization.* | retailer-specific normalization (promo labels, price anomalies, SKU mapping) | raw zone → clean zone | **Strategy** per retailer; shared interface `Harmonizer.transform(df)` |
| etl.quality.checks | implausible price-volume, margin inconsistencies, missing weeks [DOC p.7] | clean zone, alerting | Specification pattern; Great-Expectations-style declarative suites |
| models.forecasting.features | temporal/promo/product feature building | panel view | pure functions; single source shared by train & inference (prevents skew) |
| models.forecasting.train_lgbm | global model training, temporal CV, MAPE tuning [DOC Table 4] | MLflow registry | Template Method (train→validate→register) |
| models.pwl.fit_breakpoints | SLSQP joint breakpoint fit; knee-point segment selection [DOC p.8–9] | forecaster, engines.promo_milp | pure numeric module; memoized per (market, cycle) |
| models.elasticity.* | STAN fit via ADVI; priors incl. conjoint; gates; sequential updating [DOC p.11–13,18] | panel view, registry, engines.pricing_de | Pipeline of stages; gates as **Chain of Responsibility** |
| engines.promo_milp.constraints.base | compile JSON template + `data` filters → constraint rows [DOC App.B] | all template modules | **Factory + Strategy** — template name → class; THE rule engine |
| engines.promo_milp.model_builder | sets/params/vars, Eq.2–14 assembly | gurobipy, constraints.* | **Builder** |
| engines.promo_milp.solver | solve, callbacks, gap policy, IIS diagnostics [DOC p.15] | Gurobi server | Adapter around solver API (swap CPLEX/HiGHS) |
| engines.pricing_de.objective | price computation, volume function, KPIs (Eq.17–32) | elasticity matrix, config | vectorized NumPy; pure function of (x, params) — critical for DE speed |
| engines.pricing_de.ensemble | ≤10 seeded parallel DE runs, best-feasible selection [DOC p.16] | Databricks/joblib | Fan-out/fan-in; fixed-seed reproducibility |
| backend.api.routers.* | REST endpoints §11 | services | thin controllers |
| backend.services.run_service | scenario → pipeline launch → status | orchestrator SDK | **Facade** over ADF/Airflow |
| backend.services.simulate | manual-edit KPI recompute WITHOUT re-solving [DOC p.18–19] | cached curves + financial algebra | Command pattern (edit list → deltas); Redis memoization |
| backend.services.audit | decision log, config hashing | DB | Interceptor/middleware |
| frontend.CalendarGrid / PriceBoard | the two core interactive surfaces | /runs/results, /simulate | optimistic UI + server reconciliation |
| pipelines.* | cycle DAGs: refresh → train? → curves → solve → persist → notify | everything | orchestrator-native DAGs |

Key rules of the codebase (documented principles made concrete): (1) models never import engines and vice versa — they meet only at data artifacts (separation of concerns [DOC p.14]); (2) every business rule lives in `config/` + one template class — never inline in model_builder (externalized logic [DOC p.14]); (3) simulate must not call the solver — interactivity is an adoption requirement [DOC p.18–19,23].

---

# 20. Hidden Assumptions (not written, but load-bearing)

| # | Assumption | Why reasonable | Confidence |
|---|---|---|---|
| 1 | Weekly buckets are the universal planning grain; no intraweek pricing | every documented dataset and constraint is weekly | High |
| 2 | Data feeds are batch (weekly/cycle), not streaming | promo cycles quarterly-annual; pricing semiannual [DOC]; nothing needs sub-day latency | High |
| 3 | PPG→SKU mapping is business-maintained master data and politically negotiated | PPGs are "how business users think" [DOC p.22]; someone must own the grouping | High |
| 4 | rho/mu (unit revenue/margin) precomputed per promo option assume full pass-through of promo price to consumer | Table 6 treats them as parameters; no pass-through model in PromoAI | Medium |
| 5 | Elasticity matrix is static during optimization (no within-cycle learning) | posterior means documented as the optimizer input | High |
| 6 | Competitor behavior reduces to fixed follow-ratios ρ vs hero products; no game theory | Eq.20 is the only competitor dynamic | High (that it's the design), Medium (that it's adequate — hence documented sensitivity analyses) |
| 7 | Baseline volumes q⁰ come from a recent clean (non-promo) period; baseline correctness is assumed | Eq.24 anchors everything to q⁰; derivation not specified | Medium |
| 8 | Solver licensing (Gurobi) is an accepted enterprise cost; dedicated server implies license-bound deployment | named product + dedicated server [DOC p.15] | High |
| 9 | Human approval is mandatory before any execution; the system never auto-publishes prices | entire §4 workflow design; 85% acceptance implies 15% rejection is normal | High |
| 10 | Trade-spend accounting (who funds the discount) is resolved upstream in finance data | trade spend listed as input; no funding-split model | Medium |
| 11 | Retailer executes accepted calendars with fidelity; no execution-compliance monitoring loop is described | acceptance is the documented terminal metric | Medium (a compliance feed is a likely real-world addition) |
| 12 | One scenario = one retailer (PromoAI); cross-retailer cannibalization not modeled | calendars are retailer-level agreements [DOC p.2] | High |
| 13 | Currency/FX handled outside the optimizer (single-currency per market run) | market-scoped runs; integer pesos vs dollars | High |
| 14 | The "UI" is an internal enterprise web app, not customer-facing SaaS | internal planners are the users | High |
| 15 | Elasticities assumed stable over the pricing horizon (no time-varying elasticity) | single E matrix per cycle; sequential priors smooth *between* cycles | High |

---

# 21. Missing Information — and how a team would fill each gap

| Gap | What's unknown | How to fill |
|---|---|---|
| Exact UI implementation | framework, screens, navigation | product-discovery workshops with planners; §10 wireframes as straw-man |
| API contracts | none documented | design-first OpenAPI (§11); contract-test against UI needs |
| Physical schemas | content documented, DDL not | §5 as baseline; profile actual feeds in a 2-week data audit |
| PPG / pricing-group definitions | grouping criteria unspecified ("similar characteristics") | workshop with category management; start = brand×size×mechanic-participation clusters; validate with forecast error |
| Discount-pressure segment definition S_p | "similar size or format segments" — exact segmentation rule unstated | derive candidate segmentations from product master; backtest which best explains cannibalization |
| Big-M values | not given | per-(p,w,r) tight bounds: M = max demand at any breakpoint × safety 1.1 (standard practice; loose M kills MILP performance) |
| PWL grid density | "a grid" of pressure values | start 21 points on [0,1]; verify SLSQP fit MSE stable |
| DE hyperparameters | "varied mutation factors, crossover probabilities, population sizes" — values unstated | SciPy defaults ± sweep: mutation (0.5,1.0), recombination 0.7–0.9, popsize 15–40×dim; keep the documented 10-seed ensemble |
| Constraint handling in DE | mechanism unnamed | penalty method with adaptive weights; feasibility-first selection tie-break |
| Regular-price window edge behavior | ±8-week window at series ends | truncate window; flag low-confidence regular prices |
| Conjoint study design | attributes/levels unknown | commission standard CBC study per category if priors wanted; system runs without (documented "when available") |
| Baseline q⁰ / K⁰ derivation | reference period unspecified | trailing 13/26-week non-promo average; freeze per scenario and store (K⁰ needed for Eq.33) |
| Trade-spend mechanics | how spend maps into mu per mechanic | finance workshop; encode per-mechanic funding rules |
| Retraining automation degree | "retrained periodically... following validation gates" — manual vs automated unclear | build gated automation with manual release approval (matches documented governance) |
| Monitoring/alerting stack | absent | §13/§15 baseline |
| Financial results | withheld | run your own baseline-vs-pilot uplift measurement (holdout retailers/markets) |
| Override taxonomy | overrides monitored but categories unknown | add reason codes to decision log from day one |
| Missing diagrams | no architecture/ER/sequence diagrams in paper | this blueprint's §2.2, §5, §22 supply them |
| Missing datasets | no sample data | build `tests/fixtures/mini_market` synthetic generator (5 PPGs × 26 weeks) for CI |
| Missing APIs | retailer feed specs vary | per-retailer ingestion adapters (Strategy) + data contracts |

---

# 22. Build Guide — step-by-step implementation

## 22.1 Sequence overview

```
[1] Infra (Terraform)          → landing zone, Databricks, ADF, Key Vault, AKS
[2] Data spine                 → raw→clean→panel views + DQ + product master
[3] Forecaster                 → LightGBM global + backtests
[4] PWL fitter                 → curves → breakpoints
[5] Promo MILP                 → builder + templates + Gurobi + callbacks
[6] Elasticity model           → STAN/ADVI + gates
[7] Pricing DE                 → objective + constraints + ensemble
[8] API + orchestration        → scenarios/runs/simulate + pipelines
[9] UI                         → builder, grids, boards, monitor
[10] Pilot + BI + retraining   → decision log, dashboards, gated automation
```

## 22.2 End-to-end sequence diagram (promo cycle)

```
Planner        UI          API         ADF        Databricks      Gurobi       Store
  │ configure   │           │           │             │              │           │
  ├────────────▶│ POST /scenarios       │             │              │           │
  │             ├──────────▶│ validate+persist        │              │           │
  │ run         ├──────────▶│ POST /runs│             │              │           │
  │             │           ├──────────▶│ trigger     │              │           │
  │             │           │           ├────────────▶│ curves+PWL   │           │
  │             │           │           │             ├─────────────▶│ solve     │
  │             │           │           │             │◀─ gap cb ────┤           │
  │             │           │           │             ├─ results ───────────────▶│
  │ poll        ├──────────▶│ GET /runs/{id} ─────────┴──────────────┴──────────▶│
  │ review grid │◀─ results─┤           │                                        │
  │ edit cell   ├──────────▶│ POST /simulate (no solver, algebra+cached curves)  │
  │ decide      ├──────────▶│ POST /decisions (accept/override + reason)         │
```

## 22.3 Decision tree — which engine/path for a request

```
Request touches promotion timing/mechanics? ──yes──▶ PromoAI MILP path
        │no
Price-level change across portfolio? ──yes──▶ PricingAI DE path
        │no
Pure what-if on existing run? ──yes──▶ /simulate (algebra only)
        │no
Data/elasticity question? ──▶ BI / elasticity explorer
```

## 22.4 Core pseudo-code

**PWL fitting [DOC p.8–9]:**
```python
def fit_pwl(curve_fn, n_grid=21, k_range=range(2, 6)):
    xs = np.linspace(0, 1, n_grid); ys = curve_fn(xs)      # LightGBM on pressure grid
    fits = {}
    for k in k_range:                                       # stage 1: fixed k
        theta0 = init_breakpoints(xs, ys, k)
        res = minimize(mse_pwl(xs, ys), theta0, method="SLSQP",
                       constraints=monotone_x_order(k))
        fits[k] = (res.x, res.fun)
    k_star = knee_point([fits[k][1] for k in k_range])      # stage 2: diminishing returns
    return breakpoints(fits[k_star][0])                     # vectors b (x), d (y)
```

**Promo MILP skeleton (gurobipy) [DOC App.A]:**
```python
m = gp.Model("promoai")
x = m.addVars(P, W, R, vtype=GRB.BINARY)                        # Eq.14
alpha = m.addVars(P, W); beta = m.addVars(P, W)
qhat = m.addVars(P, W, R); q = m.addVars(P, W, R)
m.addConstrs(x.sum(p, w, "*") == 1 for p in P for w in W)       # Eq.3
m.addConstrs(alpha[p,w] == gp.quicksum(delta[p,r]*x[p,w,r] for r in Rp[p]) ...)   # Eq.4
m.addConstrs(beta[p,w] == gp.quicksum(alpha[q_,w] for q_ in S[p])/len(S[p]) ...)  # Eq.5
for p,w,r in cells:                                             # Eq.6 PWL
    m.addGenConstrPWL(beta[p,w], qhat[p,w,r], b[p,w,r], d[p,w,r])
m.addConstrs(qhat[p,w,r]-q[p,w,r] <= M[p,w,r]*(1-x[p,w,r]) ...) # Eq.7a
m.addConstrs(q[p,w,r] <= M[p,w,r]*x[p,w,r] ...)                 # Eq.7b
m.addConstrs(q[p,w,r] <= qhat[p,w,r] ...)                       # Eq.7c
S_P = ...; Pi_P = ...; S_R = ...; Pi_R = ...                    # Eq.8–9
m.addConstr(Pi_P >= gamma_P * S_P); m.addConstr(Pi_R >= gamma_R * S_R)  # Eq.10
for tpl in load_templates(market_json): tpl.apply(m, x, data)   # rule engine
m.setObjective(w1*((1-w3)*S_P + w3*Pi_P) + w2*((1-w4)*S_R + w4*Pi_R), GRB.MAXIMIZE)
m.optimize(gap_stagnation_callback)                             # [DOC p.15]
```

**Pricing objective + DE ensemble [DOC App.C, p.16]:**
```python
def kpis(x, prm):
    dlt = np.zeros(prm.n)
    dlt[prm.pepsi] = x[prm.g_of_i[prm.pepsi]] - 1 if prm.pct_mode \
                     else (x[prm.g_of_i[prm.pepsi]] - prm.p0[prm.pepsi])/prm.p0[prm.pepsi]
    dlt[prm.comp] = prm.rho[prm.comp] * dlt[prm.hero_of[prm.comp]]      # Eq.20
    p = (1 + dlt) * prm.p0                                              # Eq.21
    E_term = prm.E @ (np.log(p) - np.log(prm.p0))                       # Eq.22a
    D_term = ppp(p, prm) - ppp(prm.p0, prm)                             # Eq.22b–23
    V = prm.q0 * np.exp(E_term + D_term)                                # Eq.24
    U = V / prm.s
    sip = prm.sip * (1 + dlt * prm.phi)                                 # Eq.26
    R  = (U * sip * (1 - prm.d))[prm.pepsi].sum()                       # Eq.27
    Pi = R - ((prm.cogs + prm.dist) * U)[prm.pepsi].sum()               # Eq.28
    ...
def objective(x, prm, cfg):
    k = kpis(x, prm)
    return -(k[cfg.kpi]) + penalty(k, x, cfg)     # ladders/tiers/lines/bands/KPI bounds
best = min((differential_evolution(objective, bounds, seed=s, tol=1e-8,
             maxiter=..., mutation=mu_s, recombination=cr_s, popsize=ps_s,
             args=(prm, cfg)) for s, (mu_s, cr_s, ps_s) in SEED_CONFIGS),   # ≤10 parallel
           key=lambda r: r.fun)                                          # [DOC p.16]
```

**Sample SQL — realized vs predicted (the BI heart):**
```sql
SELECT r.run_id, d.week_start, p.ppg_id,
       c.pred_units, SUM(f.units) AS actual_units,
       SUM(f.units) / NULLIF(c.pred_units,0) - 1 AS unit_error_pct,
       dl.decision
FROM fact_calendar_result c
JOIN fact_weekly_sales f USING (week_key)          -- + sku→ppg rollup
JOIN dim_week d USING (week_key)
JOIN dim_product p ON p.sku_key = f.sku_key AND p.ppg_id = c.ppg_id
LEFT JOIN fact_decision_log dl
       ON dl.run_id = c.run_id AND dl.entity_id = c.ppg_id||'|'||c.week_key
WHERE c.run_id = :accepted_run
GROUP BY 1,2,3,c.pred_units,dl.decision;
```

## 22.5 Deployment, CI/CD, testing
- **Environments:** dev → stage (synthetic mini-market fixtures) → prod; solver worker isolated on licensed VM/pool; API+UI on AKS with blue-green.
- **CI (every PR):** lint/type → unit (constraint templates get *table-driven tests*: JSON in → expected constraint rows out) → mini-market integration solve (must reach documented gap on toy instance in <2 min) → API contract tests.
- **Model CD:** retrain job → gates (R²/wMAPE/bias/elasticity ranges [DOC p.12]; forecast-vs-baseline) → human release approval → registry promote (documented governance made executable).
- **Testing pyramid specifics:** golden-file tests for PWL fits; property tests for KPI algebra (baseline prices ⇒ V=q⁰ exactly — Eq.24's self-consistency is a free invariant test); seed-fixed DE regression tests; infeasibility-diagnostic tests (deliberately conflicting rules → named culprits).

## 22.6 Monitoring, scaling, cost
- **Monitor:** pipeline SLAs; solver gap/time distributions per market; forecast MAPE per retrain; elasticity gate history; acceptance/override rates (the documented north-star trust metric); simulate-endpoint p95 latency.
- **Scale:** PromoAI scales by market sharding (one MILP per retailer-scenario — embarrassingly parallel across scenarios); watch PWL segment count & big-M tightness (documented tractability levers); PricingAI scales by DE population parallelism.
- **Cost:** Gurobi license = the big line item (alternatives benchmarked in §15); Databricks job clusters auto-terminate; results cheap to store; UI/API negligible. Cache demand curves per (model_version, market, cycle) — they're reused by every scenario re-run in a negotiation loop.

## 22.7 Maintenance & future enhancements
- **Maintenance (documented):** quarterly/cycle retrains behind gates; per-cycle parameter refresh (costs, agreements); breakpoint re-evaluation on demand shifts; new market = new JSON config; sequential priors for elasticity stability.
- **Enhancements (natural next steps, all [INF]):** inventory-feasibility constraints; retailer execution-compliance feedback loop; full-posterior (robust/stochastic) optimization instead of posterior means; cross-retailer portfolio coordination; RL for within-cycle tactical adjustments; the §14 LLM constraint-authoring copilot; automated competitor-price ingestion to update follow-ratios ρ.

---

# Closing note on fidelity

Everything mathematically essential to *rebuild the decision engines* — both full formulations (Eq. 2–14, 15–39), the feature engineering (8-week regular-price window, 5% promo flag, 30% aggregation threshold), the model designs (global LightGBM + Table 4 hyperparameters; additive/multiplicative Bayesian decomposition + gates), the tractability tricks (PWL + SLSQP + knee, big-M, PPG aggregation, posterior means, seeded DE ensemble), and the human workflow that makes it adopted (scenario menus, negotiation re-optimization, instant what-if, override monitoring, validation gates) — **is documented in the source paper and faithfully transcribed here**. What this blueprint adds is the platform engineering around it (APIs, schemas, security, DevOps), each piece flagged with its confidence level so your team knows what is PepsiCo's design versus enterprise-standard reconstruction.

