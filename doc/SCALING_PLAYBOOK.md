# Scaling Playbook — how to onboard a new brand

> The week-by-week procedure for taking a new brand (or a new set of SKUs from an existing brand) from "we received their data" to "they're acting on weekly recommendations". Designed for repeatability across many brands.

The core principle: **lead with the Data Readiness Report.** Never promise an accuracy number to a new client. Run the readiness report on their data first, then show them what *their own* data can support.

---

## Phase 0 — Before any data arrives (sales conversation)

What to tell a prospect:

> "We don't promise an accuracy number until we see your data. Step one of any engagement is a one-week diagnostic that produces a one-page report telling you, per product and per city, exactly how much accuracy your data can support and where you'd need a structured price test before acting. That report is yours to keep whether or not we move forward."

What you need from them:
- Daily sales data per (SKU × city) for at least the last 6 months — Excel or CSV.
- A list of their own brand names (`OWN_BRAND_PATTERNS`) so the system can separate own-brand from competitor rows.
- An event calendar for the period (festivals, BBD, etc.) — optional but improves results.
- Cost structure (COGS %, platform commission %, fulfilment fee) — optional; system has sensible defaults.

---

## Phase 1 — Week 1: Data Readiness

### Setup (≈ 30 minutes)

1. Drop the brand's Excel files into `input_data/`.
2. Edit `v4_config.py`:
   ```python
   BRAND_NAME = "Acme Foods"
   OWN_BRAND_PATTERNS = ["Acme", "Acme Foods"]
   SALES_DATA_DIR = r"path/to/input_data"
   ```
3. (Optional) Add their festival dates / event windows to `FESTIVAL_DATES` and `PLATFORM_EVENT_WINDOWS`.
4. (Optional) Update `DEFAULT_COGS_PCT`, `DEFAULT_COMMISSION_PCT`, `DEFAULT_FULFILLMENT_FEE` if they shared cost figures.

### Run (≈ 1–2 minutes)

```bash
python -X utf8 scripts/diagnostics/data_readiness_report.py
```

### Deliverable

Hand the client `v4_outputs/_readiness/DATA_READINESS_REPORT.md` (Markdown — convertible to PDF or pasted into a deck). It contains:

1. Verdict (GREEN / YELLOW / RED) at the top.
2. Numbers at a glance — model accuracy this data supports.
3. Per-cell confidence breakdown.
4. By-product table — where to start.
5. By-city table — where to start.
6. Gap analysis — exactly which cells need what fix.
7. Verdict-specific next steps.

Also share the three audit CSVs (`per_cell_assessment.csv`, etc.) so their analytics team can sanity-check.

### Brand-facing talking points by verdict

**GREEN:** *"Your data is ready. ~88 % of your cells can be acted on with confidence right now. The remaining ~12 % we're going to park behind a 4-week price test. We can start your first weekly cycle next Monday."*

**YELLOW:** *"Your data is partially ready. We can ship recommendations on ~50 % of cells immediately. For the rest, we'll design a structured 6–8 week price-test programme so we have signal to act on them. Expect full coverage in 2–3 months."*

**RED:** *"Your data isn't ready for production pricing yet. The good news is the readiness report tells us exactly what to fix — typically not enough price variation in too many cells. We'd recommend a structured 8–12 week price-test programme before activating the pricing engine. We can design that test for you in week 2."*

In all three cases, **the report itself is the deliverable.** The client got a measurable, defensible answer in week 1.

---

## Phase 2 — Week 2 onward: Production (if GREEN or YELLOW)

### Initial pipeline run

```bash
python -X utf8 pipeline.py
```

Output: `v4_outputs/<timestamp>/` with the dashboard, Excel report, and CSVs.

### What goes to the brand team

1. **`BRAND_DASHBOARD.html`** — open this together in the first weekly meeting to walk them through the recommendations interactively.
2. **`WASTE_REINVEST_REPORT.xlsx`** — the formula-driven Excel they'll work with day-to-day.
3. **`WASTE_REINVEST_REPORT.md`** — a plain-Markdown version for quick mobile reading / Slack paste.

### Weekly cadence

| Day | Step | Owner |
|---|---|---|
| Mon AM | Fresh data exported into `input_data/` | Brand-side analyst |
| Mon AM | `python pipeline.py` runs | You (or scheduled job) |
| Mon AM | Walk through `BRAND_DASHBOARD.html` in 30-min sync | Both |
| Mon PM | Brand team approves Strong Cut tier as-is, queues Trade-off for individual review | Brand team |
| Tue–Wed | Approved discount changes go live on the platform | Brand operations |
| Thu | Mid-week sanity check on early actuals vs predicted | Both |
| Next Mon | Cycle repeats with another week of actual data | — |

### Monthly cadence

On the first Monday of every month, also run the readiness report:

```bash
python -X utf8 scripts/diagnostics/data_readiness_report.py
```

This tracks whether the % actionable is growing as more data arrives. Two patterns to look for:

- **Actionable % rising over time** = engagement is healthy; price-test programme is paying off.
- **Same cells stuck in LOW / DO_NOT_ACT month after month** = those cells genuinely lack price variation. Either accept they'll never be auto-tier-able (rare-purchase items, low-traffic cities) or design a more aggressive price test for them.

---

## Phase 3 — When the readiness verdict is YELLOW or RED

A structured price-test programme is the next deliverable.

### The mechanics of a price test

For each LOW or DO_NOT_ACT cell, the test rotates the cell through **3–4 distinct discount levels** for **≥ 2 weeks each**. That's the variation the elasticity model needs.

Suggested levels (relative to the cell's current discount):

| Week | Discount level |
|---|---|
| 1–2 | Current discount − 3 ppt |
| 3–4 | Current discount + 3 ppt |
| 5–6 | Current discount (baseline) |
| 7–8 | Current discount + 6 ppt |

After the test, re-run the readiness report. Those cells should now have ≥ 7 distinct discount levels and enough days at each — they'll mostly migrate from LOW → HIGH/MEDIUM.

### Designing the test from the gap analysis

The gap-analysis section of the readiness report lists three categories of cells:

1. **Thin-data cells** — fix is "wait for more days" or "include in price test for accelerated data collection".
2. **Low-variation cells** — these are the prime price-test candidates. Their discount has been flat for months.
3. **Poor-fit cells** — usually indicates upstream data issues (a SKU re-launch, a platform glitch). Investigate the data before testing.

Build the price-test programme from the **Low-variation cells** list directly.

---

## Phase 4 — Tuning per brand (after 4–6 weeks of operation)

By this point the brand team has approved 4–6 weeks of recommendations and you have real actual-vs-predicted feedback. Tune these in `v4_config.py`:

- `TARGET_WEIGHTED_DISCOUNT_PCT` — the portfolio target. Often raised or lowered after the brand sees the first month of pace.
- `TIER_STRONG_CUT_MIN_SAVINGS` — if the brand wants a higher bar for fast-track approval, raise this.
- `TARGET_TIMELINE_WEEKS` — pace of the per-cell journey toward the target. Aggressive brands want 8 weeks; cautious brands want 16+.
- `MIN_DISCOUNT_CHANGE_PPT` — if the brand operations team has trouble executing 3 ppt moves, raise it to 5 ppt so each week's change is more meaningful.

The confidence thresholds in `_add_cell_confidence` should NOT need tuning per brand — they're calibrated to the underlying statistics of any CPG daily-sales data and are scale-invariant.

---

## Phase 5 — Cross-brand learnings (after multiple brands)

Things worth tracking in a master spreadsheet across brands:

| Brand | Categories | Cells | Readiness verdict | Median actionable % | Aggregated R² | Notes |
|---|---|---|---|---|---|---|
| 24 Mantra Organic | 3 | 33 | GREEN | 88 % | 0.97 | Baseline reference brand |
| (next brand) | … | … | … | … | … | … |

Patterns to look for:

- **Categories with consistently low actionable %** across brands — these probably need category-specific feature engineering (e.g. a category where discount badges drive demand differently from price).
- **Cities with consistently low actionable %** across brands — likely a tier-2 / tier-3 city where Blinkit traffic itself is too low for reliable inference. Set realistic expectations upfront.
- **Brands whose readiness goes GREEN → YELLOW** over time — usually a data hygiene regression on the brand's side (the export started missing days, or a new platform changed columns).

---

## A note on selling this

The product the brand is buying is **not "a machine-learning model"**. It's a *process*:

1. We measure what your data can deliver. (Readiness Report)
2. We act only where your data lets us act with confidence. (Confidence gate)
3. We tell you exactly what to do to grow the actionable share. (Gap analysis + price-test programme)
4. We re-measure monthly so progress is auditable. (Readiness re-run)

A brand can compare two pricing vendors: one that says "our model is 90 % accurate" and one that says "here's a one-page report showing what *your* data can deliver". The second one wins every time because it's the one whose numbers the brand can verify.

---

## Cheat sheet — every command you actually need

```bash
# Onboarding — week 1
python -X utf8 scripts/diagnostics/data_readiness_report.py

# Weekly production
python -X utf8 pipeline.py

# After cost changes only (skip model retrain)
python -X utf8 pipeline.py --stages 6 7 8

# After event-calendar changes (re-flag from Stage 2)
python -X utf8 pipeline.py

# Just regenerate the report (no model change)
python -X utf8 pipeline.py --stages 8

# Monthly tracking
python -X utf8 scripts/diagnostics/data_readiness_report.py

# When debugging where the model is weak
python -X utf8 scripts/diagnostics/baseline_breakdown.py
```
