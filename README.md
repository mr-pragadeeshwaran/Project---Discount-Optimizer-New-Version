# Optimal Price Finder — Discount Optimisation for Quick-Commerce

Turns a CPG brand's raw sales export into an honest, week-by-week discount plan:
**which discounts are wasted, which actually drive sales, how much you can safely
save, and proof — on your own register — that the calls were right.**

Currently configured for **24 Mantra Organic on Blinkit** (6 months, ~84 products
× 11 cities). Designed to generalise to any brand × platform with daily sales data.

---

## Bottom line (current run)

- **Model accuracy: out-of-sample R² = 0.78** — it predicts weeks it never saw.
- **Recoverable discount waste: ~₹5–7 lakh/month**, concentrated in inelastic
  **staples** (Dal, Rice, Sooji, Millet) where discount does not move volume once
  availability, visibility and competition are controlled for.
- **Reinvest**, don't just cut: Oil is the one category where discount reliably
  *pays* — shift budget there.
- **Honest caveat:** this is a *model-validated* number (confounder-controlled +
  Double ML), **not yet register-proven.** The weekly tracker turns it into proof.
  Full verdict: [`DISCOUNT_PLAN/5L_VERDICT.md`](DISCOUNT_PLAN/5L_VERDICT.md).

---

## How it works (the flow)

```
  raw RCA exports (input_data/)
        │
        ▼   pipeline.py  — stages 1–3
  fact_table.csv           cleaned daily panel: units, price, discount,
  (v4_outputs/<run>/)      OSA, Ad SOV, competitive share, per SKU×city
        │
        ▼   scripts/analysis/  — the decision layer
  confounder-controlled + Double-ML model
    • isolates discount's effect from OSA / SOV / competition (+ reverse-causality lags)
    • cuts ONLY where discount is reliably below break-even
    • DISCOUNT_PLAN/  (PLAN.md, 5L_VERDICT.md, cut_list.csv, reinvest_list.csv, …)
        │
        ▼   scripts/tracker/  — the weekly loop
  WEEKLY_TRACKER.xlsx        guardrailed, gliding weekly price suggestions +
                             a predicted-vs-actual accuracy scorecard (the trust engine)
```

The **pipeline** (stages 1–8) is the data-prep + original per-cell tiering engine.
The **analysis** layer (`scripts/analysis/`) is the newer, rigorously-validated
decision model built on top of `fact_table.csv`. The **tracker** (`scripts/tracker/`)
is what you run every week.

---

## Directory map

| Path | What it is |
|---|---|
| `pipeline.py`, `v4_config.py` | 8-stage pipeline entry point + config |
| `stage1_ingestion/ … stage8_output/` | Pipeline stages (ingest → prepare → features → model → curves → economics → guardrails → report). **Do not move** — imported by `pipeline.py`. |
| **`scripts/analysis/`** | **The decision model.** `discount_plan.py` (confounder model), `dml_estimate.py` (Double ML), `optimize_plan.py`, `validate_plan.py` (C1–C8 checks), `build_report.py` |
| **`scripts/tracker/`** | **The weekly tracker.** `weekly_tracker.py` (orchestrator) + `guardrail.py`, `scorecard.py`, `seasonality.py`, `workbook.py` |
| `scripts/diagnostics/`, `scripts/experiments/` | Analysis history / data-quality probes (catalogued in `scripts/README.md`) |
| **`DISCOUNT_PLAN/`** | **Deliverables** — the plan, verdict, cut/reinvest lists, measurement spec, data gaps, and `WEEKLY_TRACKER.xlsx` |
| `doc/` | Deeper design docs (ARCHITECTURE, MODEL, MODEL_LOGIC, FLYWHEEL, …) |
| `tests/` | Test suite (`pytest -m "not slow"`) |
| `input_data/` | Raw brand exports (git-ignored, proprietary) |
| `v4_outputs/` | Per-run outputs (git-ignored) |
| `archive/` | Local junk drawer — marketing, logs, superseded docs (git-ignored) |

---

## Quick start

```bash
# 1. Build the cleaned panel + base outputs from the raw exports in input_data/
python -X utf8 pipeline.py

# 2. Run the confounder-controlled + Double-ML decision model
python -X utf8 scripts/analysis/discount_plan.py     # -> v4_outputs/<run>/plan/
python -X utf8 scripts/analysis/dml_estimate.py      # Double-ML confirmation
python -X utf8 scripts/analysis/validate_plan.py     # C1–C8 acceptance checks
python -X utf8 scripts/analysis/build_report.py      # -> DISCOUNT_PLAN/*.md

# 3. Produce this week's tracker + plain-English readout
python -X utf8 scripts/tracker/weekly_tracker.py --week W1 --date 2026-07-06
# -> DISCOUNT_PLAN/WEEKLY_TRACKER.xlsx  +  DISCOUNT_PLAN/WEEKLY_READOUT.md
```

`run.bat` double-clicks the pipeline for non-technical users.

---

## The weekly loop

1. Export the week's sales data (same RCA format as `input_data/`).
2. Run `weekly_tracker.py` (or send the export to be run for you).
3. Open **`WEEKLY_TRACKER.xlsx`**:
   - **Weekly Plan** — per SKU×city: current vs suggested price, action, one-line reason.
   - **Guardrail** — red/green budget check (discount stays inside its % of sales,
     moves ≤3 points/week, never a cut that costs net revenue).
   - **Accuracy Scorecard** — predicted vs actual, week by week. *This is the receipt.*
   - **Seasonality** — festival calendar; the budget auto-relaxes on flagged weeks.
   - **How to use** — plain-English steps.
4. **Golden rule:** if a cut loses sales for 2 straight weeks, revert it.

---

## Honest notes

- The savings figure is **model-validated, not yet register-proven.** The scorecard
  fills in from week 2 and converts the estimate into evidence.
- **Two data gaps** would materially tighten it (see [`DISCOUNT_PLAN/DATA_GAPS.md`](DISCOUNT_PLAN/DATA_GAPS.md)):
  the `Competitor Price` column is 100% empty, and there is no COGS / promo-calendar feed.
- Fit is a *trust floor*, never the objective. The goal is correct cut/keep/reinvest
  decisions that grow net revenue — not a big headline number.

Deeper reading: [`doc/`](doc/) (architecture & model logic), [`scripts/README.md`](scripts/README.md)
(script catalogue), [`DISCOUNT_PLAN/`](DISCOUNT_PLAN/) (the deliverables).
