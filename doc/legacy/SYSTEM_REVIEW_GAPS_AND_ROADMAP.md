# System Review: What You Have, What's Missing, What To Build
**Date:** 6 July 2026 · **Audience:** Business owner + whoever implements next
**Scope:** Logic and workflow only — no code. Every claim below was verified against the actual files.

---

## 1. What your system does today (plain English)

You feed it 6 monthly Blinkit RCA exports (Jan–Jun 2026, daily data, all brands). It filters to your 89 SKUs across 11 cities = **585 SKU×city "cells"** in 19 categories, then runs **two engines**:

**Engine 1 — the 8-stage pipeline** (`pipeline.py`, stages 1–8): cleans data, estimates price elasticity per category, builds discount-response curves, computes margin economics, applies guardrails, and outputs the Excel waste/reinvest report + HTML dashboard.

**Engine 2 — the analysis layer** (`scripts/analysis/` + `scripts/tracker/`): the newer, statistically stricter engine. It controls for stock-outs, ad visibility, competition and momentum; confirms findings with Double ML (a debiasing method); and produces the actual decision files: cut list, reinvest list, test-unlock list, plus the weekly tracker workbook.

**Current verdict (the live 5 July run):** 63 cells are confirmed discount waste worth **₹6.98L/month** (₹7.25L all-in), reinvest opportunity only **₹33k/month** (Oil, Wheat/Daliya), and Week 1's guardrailed plan projects **₹39,342/week** savings via 3ppt glide steps.

The design philosophy is genuinely good: never cut more than 3ppt/week, never go below a cell's own proven historical discount floor, never bank savings the statistics can't defend, and "the register is the final proof."

---

## 2. The one-line diagnosis

**Your system is excellent at making a plan and structurally incapable of learning whether the plan worked.** Everything below flows from that.

---

## 3. What's missing — ranked by impact on your goals

Your goals: save discount spend, reinvest it, track every week, improve the model. Here is each gap, why it matters, the logic to implement, and the workflow around it.

---

### GAP 1 — The feedback loop is broken: actuals are never filled in (CRITICAL)

**What I found:** `tracker_history.csv` has 585 prediction rows for W1 — and **zero actuals**. The code comment says actuals "could be back-filled when a fresh export arrives," but that logic was never written. So the Accuracy Scorecard will say "no actuals yet" forever, the hit-rate/realized-savings numbers can never compute, and the "golden rule" (revert a cut that loses sales 2 weeks straight) can never fire. Your ₹6.98L is a forecast that nothing in the system will ever confirm or deny.

**Logic to implement:**
1. When a new export lands, aggregate it to the same grain as predictions: per cell per week → actual units, actual net revenue, actual avg discount, actual OSA, actual Ad SOV.
2. For every prior week in tracker history whose actuals are empty and whose week has fully elapsed in the new data: join on cell_id + week, fill `actual_units` and `actual_net_rev_delta` (actual net revenue minus that cell's pre-action baseline — define baseline once, as the 4-week average before the first action, and freeze it).
3. Scorecard then computes automatically: hit rate, prediction error, revenue bias, and **cumulative realized savings** — the only number that belongs in a P&L conversation.

**Workflow:** every Monday, drop last week's export → run → actuals fill → scorecard updates. No human math.

---

### GAP 2 — No kill-switch: the safety rule is a sentence, not a mechanism (CRITICAL)

**What I found:** The "revert if a cut loses sales 2 weeks straight" rule exists only as text in the readout and Excel "How to use" sheet. `stage8_monitoring/` is an **empty folder**. The config values `VOLUME_DROP_TOLERANCE_PCT = 5%` and `DRIFT_ALERT_THRESHOLD = 15%` are defined but **no code anywhere reads them**. Once a cut is live, nothing watches it.

**Logic to implement (per acted-on cell, runs after actuals fill):**
1. **Confounder check first** (already specified in your own MEASUREMENT_SPEC.md, just not built): if OSA or Ad SOV dropped >10% vs baseline that week → mark the week "confounded read," don't count it as a strike, don't revert.
2. **Strike rule:** actual units below prediction by more than the 5% tolerance AND actual net revenue delta negative → strike. Two consecutive strikes → the next weekly plan **automatically shows REVERT to the prior discount** for that cell, freezes it for 4 weeks, and tags it "model miss" for the next retrain.
3. **Portfolio drift alert:** once ≥30 cells are scored, if hit rate <60% or prediction R² is negative for 2 straight weeks → block all NEW cuts, flag "retrain required." Existing holds continue.

**Workflow:** these flags appear automatically in the weekly readout as a "REVERT/ALERT" section at the top. You approve reverts the same way you approve cuts.

---

### GAP 3 — No execution log: the system assumes you applied its prices (CRITICAL)

**What I found:** Nothing records what you actually changed on Blinkit. If you apply 40 of 63 cuts, the scorecard (once fixed) would blame the model for 23 cells where nothing happened.

**Logic to implement:** a simple weekly applied-actions log: week, cell, recommended discount, applied discount, date applied, reason if skipped. Scorecard scores **only applied cells**; unapplied recommendations are reported separately as "not executed" (that's an ops metric, not a model metric).

**Workflow (per your setup — KAM/brand team executes):** you share the Weekly Plan sheet with the KAM/brand team; they return it with an "Applied? (Y/N/Partial) + date" column filled before Friday. That confirmation is non-negotiable — without it, the scorecard can't distinguish "model was wrong" from "change was never made," and all tracking becomes unreliable. Make it part of the weekly handoff ritual, not a favor.

---

### GAP 4 — All margin math uses assumed costs, not your real costs (downgraded per your answer: costs are hard to get)

**What I found:** `data/master/` is empty. Every margin, break-even and "optimal discount" number in Engine 1 uses defaults: **COGS = 50% of MRP, commission = 15%, fulfillment = ₹10/unit** — for every SKU. Engine 2 sidesteps this by optimizing net revenue instead of margin.

**Your situation:** real per-SKU costs are hard to get. So the practical path is:
1. **Stay on net-revenue optimization** (Engine 2's approach) as the decision basis — it's honest without cost data.
2. **Stop showing Engine 1's margin/contribution numbers in any business-facing report** — they're built on assumptions and will mislead. Label them "illustrative" or hide them.
3. If you ever get even rough category-level cost percentages (e.g., "dals ~35%, oils ~20% margin"), plug those in — better than one global 50% guess. The loading hook already exists; only the file is missing.
4. The real safety here: net-revenue break-even is *more conservative on cuts* than you might fear — a cut only counts as waste if revenue holds or rises without the discount. Cost data would mainly make the reinvest decisions sharper.

---

### GAP 5 — Stale and contradictory reports: two versions of the truth (HIGH)

**What I found (verified byte-for-byte):**
- `DISCOUNT_PLAN/PLAN.md`, `DATA_GAPS.md`, `MEASUREMENT_SPEC.md`, `cut_list.csv` are **frozen from an older run: 13 cuts, ₹38k/month**.
- `5L_VERDICT.md`, `dml_results.json`, the tracker and the live run folder say **63 cuts, ₹6.98L/month**.
- Anyone reading PLAN.md today gets a number 18× too pessimistic and a cut list that disagrees with what the tracker is actually doing.
- Separately, the HTML dashboard's "last week performance" block (38 cells acted, −3.1% vs −3.4%, green checkmark) is **hardcoded fake data** — the code literally comments it "mock." It looks like a real tracked result. Remove or clearly label it before anyone sees it.

**Logic to implement:**
1. **One weekly run command** that always executes the full chain in order (plan → DML → validate → report → tracker) so no artifact can go stale.
2. Every report stamped with run ID + timestamp; a consistency check at the end that **fails loudly** if the cut counts/savings in PLAN.md, the verdict, and the tracker disagree.
3. Declare Engine 2 the single decision engine. Engine 1 remains data-prep + diagnostics. One weekly deliverable set, not two competing Excel reports.

---

### GAP 6 — Weekly loop, monthly data (HIGH — this decides whether "track every week" is even possible)

**What I found:** Your inputs are monthly files; the tracker is weekly; week labels are typed by hand (`--week W1`) with no auto-increment, and a duplicate label is silently ignored. With monthly exports, you'd fill actuals for 4 weeks at once, a month late — the kill-switch would fire 3–5 weeks after the damage.

**Logic to implement:**
1. **Weekly RCA exports — confirmed possible on your side.** Same format, shorter window; make it a fixed Monday-morning ritual (yours or the KAM's).
2. Auto-derive the week label and date from the data itself (latest complete week in the export), never from a human typing W1/W2. Warn loudly on duplicates instead of silently skipping.

**Workflow — the target operating rhythm:**
- **Every Monday (30 min):** export last week's data → drop in folder → run the one command → read the readout → approve cuts/reverts/pilots → apply on Blinkit portal → tick the execution log.
- **Every 4 weeks:** model refit + backtest (Gap 9), readiness report, verdict regenerated.
- **Quarterly:** refresh cost master, festival calendar (hardcoded H2-2026 only, needs annual manual update), and the budget number.

---

### GAP 7 — Reinvest never actually happens (HIGH for your growth goal)

**What I found:** The tracker **by design never raises a discount** — reinvest cells are "surfaced," not executed. W1 plan: cut 63, hold 522, **reinvest 0**. Your flywheel (save → reinvest → grow) has no second half. Also, the budget cap defaults to whatever you already spend — so it's satisfied by definition and caps nothing.

**Logic to implement — the reinvest pilot loop:**
1. Candidates: cells where the discount effect is statistically positive AND there's headroom to break-even (today: Oil, Wheat/Daliya — ₹33k/month).
2. Pilot: top 3 cells, +3ppt for 3 weeks, in 1–2 cities only. Control = same SKU in untouched cities.
3. Success test: net revenue up vs control AND incremental margin ≥ extra discount cost (needs Gap 4's cost data).
4. Pass → extend cities or add +3ppt, funded **only from banked, realized savings** (the scorecard's cumulative number, not the forecast). Fail → revert, done.
5. Set a **real budget number** (e.g., "discount spend ≤ 12.5% of gross sales") instead of the self-referential default; the guardrail code that enforces it already exists.

---

### GAP 8 — "Needs a test" is a recommendation with no test machinery (MEDIUM)

**What I found:** A large pool of cells sits in "test to unlock" (explicitly not bankable, 23–47% bootstrap stability) and dozens more are tiered "Needs Experiment." The system tells you to run an A/B test; nothing designs, tracks or evaluates one.

**Logic to implement — city-split test protocol:**
1. For a test SKU, split its cities into test/control matched on size and trend.
2. Cut 3ppt in test cities only, hold control. Run 3 weeks (per your own measurement spec).
3. Read: test vs control difference in units and net revenue, with the same confounder checks (OSA/SOV moved >10% → invalid read, extend a week).
4. Pass → the cell graduates into the bankable cut list next retrain. Fail → mark it "discount is working," hold.
5. Cap concurrent tests (e.g., 10 SKUs) so reads stay clean; the tracker workbook gets a "Tests Running" sheet with start date, cities, and week counter.

This is how the unbanked pool (potentially several lakh/month) converts to banked savings with evidence.

---

### GAP 9 — The model never retrains, and DML isn't wired into the weekly loop (MEDIUM)

**What I found:** Nothing triggers re-estimation — the tracker will happily reuse a July model in December. The DML confirmation and the test-unlock estimates are standalone scripts whose outputs the tracker **never reads**.

**Logic to implement:**
1. **Retrain cadence:** every 4 weeks, or immediately when triggered by the drift alert (Gap 2) or a new month of data.
2. **Champion/challenger:** refit → backtest on the last 8 weeks → the new model replaces the old **only if** held-out accuracy improves. Log model version in every output so you can see "v3 recommended this."
3. **Wire DML in:** a cut may only appear in the weekly plan if its category is DML-confirmed in the latest `dml_results.json` (today this is a manual cross-check; make it a hard gate).
4. Feed "model miss" cells (from the kill-switch) into retrain review — that's your model-improvement loop, driven by real outcomes.

---

### GAP 10 — Competitor data: you're deleting it at the door (MEDIUM, cheap win)

**What I found:** DATA_GAPS.md ranks competitor price/discount as the **#1 missing data**, because today "losing to a competitor's promo" and "wasted discount" can't be separated. But your RCA files **already contain every competitor brand's daily price, discount, MRP and share** — the ingestion step filters to your brand and throws the rest away.

**Logic to implement:** before filtering to own brand, aggregate competitor rows per category×city×week: median competitor selling price, average competitor discount, top-3 competitors' deepest discount, competitor OSA. Feed these as model features and as the trigger for the "competitive pressure" bucket (replacing the current proxy of your own share drop). No new data purchase needed — it's in the files you already have.

**Still worth requesting externally:** promo-type flags (bank offer vs brand-funded vs platform event) — your docs correctly call reverse causality the biggest remaining threat to the discount estimates; a promo calendar is the cleanest fix.

---

### Housekeeping (LOW, but do them)

- **STRATEGIC_SKUS is an empty list** — populate it with hero/flagship SKUs that should never be auto-cut regardless of the math.
- **Two confidence systems** (Stage 4 score and Stage 5 label) sit side-by-side in outputs and can disagree — pick one for business-facing reports.
- Several config values are dead (old tier thresholds, unused monitoring constants, `MODEL_TYPE` label is stale) — clean up so the config tells the truth.
- Festival calendar: approximate windows, H2 2026 only — set a yearly reminder to refresh.

---

## 4. The complete target workflow (what "done" looks like)

**MONDAY LOOP (weekly, ~30 min of your time)**
1. Drop last week's export into `input_data`.
2. Run the one command. It automatically: fills actuals for prior weeks → updates scorecard → runs kill-switch checks → regenerates plan with guardrails → writes readout + workbook, all stamped with the same run ID.
3. Read one page: REVERTS/ALERTS first, then this week's cuts (glide steps), then reinvest pilots and tests in flight.
4. Apply approved changes on the Blinkit portal. Tick the execution log.

**MONTHLY LOOP**
5. Retrain (champion/challenger), DML re-confirmation, regenerate verdict. Review "model miss" cells.
6. Review realized vs forecast savings: is the ₹6.98L materializing? Reallocate banked savings to reinvest pilots.

**QUARTERLY**
7. Refresh cost master, festival calendar, budget %, strategic SKU list.

**The flywheel this creates:** cuts → actuals confirm → scorecard builds trust → banked savings fund reinvest pilots → tests unlock more cuts → retrains sharpen the model. Every piece exists as intent in your codebase; Gaps 1–3 are what make it spin.

---

## 5. Build order (30/60/90)

**Days 0–30 — close the loop (Gaps 1, 2, 3, 5, 6):** actuals backfill, kill-switch, KAM execution-confirmation log, one-command run + consistency check, remove mock dashboard block, regenerate stale PLAN.md, auto week labels, start weekly export ritual. *Until this is done, don't scale cuts beyond the current glide plan.*
**Days 31–60 — growth (Gaps 7, 8, 10):** first reinvest pilots (Oil), first 5–10 city-split tests, competitor features from existing files; hide Engine 1's assumption-based margin numbers from business reports (Gap 4).
**Days 61–90 — learning machine (Gap 9):** retrain cadence + champion/challenger, DML as a hard gate, first realized-vs-forecast review of the ₹6.98L claim.

---

## 6. Data/asks summary

| Ask | From | Unlocks |
|---|---|---|
| Weekly RCA export (confirmed possible) | Blinkit portal / KAM | The entire weekly loop |
| Weekly "applied Y/N" confirmation | KAM / brand team | Trustworthy scorecard (Gap 3) |
| Promo calendar / deal-type flags | Platform team / KAM | Kills the reverse-causality risk |
| Rough category-level cost %s (if ever available) | Finance | Sharper reinvest decisions (Gap 4) |
| Real budget % ceiling | You | Makes the budget guardrail real |
| Hero SKU list | You | Protects strategic items from cuts |
| Competitor aggregates | Nobody — already in your files | Competitive-pressure detection (Gap 10) |
