# Discount Waste Recovery — Pilot Offer

*A done-for-you weekly pricing service for CPG brands on quick-commerce.*

---

## The problem you already know you have

You spend lakhs every month on discounts across Blinkit / quick-commerce.
Some of that discount genuinely drives volume. A lot of it doesn't — customers
would have bought at a higher price, and the difference is margin you gave away
for nothing. You can't see which is which, city by city, so the safe move has
been to keep discounting. That safety costs real money every month.

**Reference numbers from our live deployment** (an organic CPG brand —
84 products × 11 cities on Blinkit, 585 product×city cells, 6 months of
daily data):

| Metric | Value |
|---|---|
| Gross sales | ₹5.17 Cr / month |
| Discount spend | **₹81.8 L / month** (15.8% weighted) |
| Recoverable waste identified by the engine | **≈ ₹6.98 L / month** across 61 high-confidence product×city cells (₹7.25 L including 2 experimental) — every cut category independently confirmed by Double ML |

Waste concentrates in inelastic staples (Dal, Rice, Sooji, Millet) where
discount does not move volume once availability, visibility and competition
are controlled for. A brand spending ₹1–5 Cr/month on discounts typically
carries proportionally more.

## What the service does

Every Monday you receive one Excel workbook that tells your team, for **each
product in each city**:

- the exact selling price to set this week,
- where discount is being **wasted** (raise price safely, week by week),
- where deeper discount **actually grows volume** (reinvest),
- which cells to **leave alone** (inelastic — discounting there can't pay),
- and how much of each promo's "lift" is **real vs. borrowed vs. stolen**
  (pull-forward and cannibalization netted out — most tools don't do this).

Every move glides gradually (≈3 percentage points/week) to a price level the
cell has already proven it can survive — never a price your customers haven't seen.

## Why you can trust it (the receipts, not promises)

We don't ask you to trust a model. Every claim below is generated from data and
re-checked on every run:

1. **Out-of-sample R² = 0.78** — the decision model predicts weeks it never
   saw during training, with availability, ad visibility, competition and
   reverse-causality controlled. We publish the error instead of hiding it.
2. **Every cut is double-checked by a second, independent method.** Each cut
   category must be confirmed reliably-below-break-even by Double ML
   (gradient-boosted causal check) — currently 10/10 — and a separate
   Bayesian pricing optimizer must independently agree before a cut executes.
3. **Forward validation:** trained on old data, tested on weeks it never saw.
   Where price actually rose, the engine predicted a **13.8%** volume drop —
   reality was only **8.8%**. The engine is **conservative**: following it is
   *safer* than it claims, not riskier.
4. **Ground-truth recovery test:** we plant a known price-sensitivity in
   synthetic data with a deliberate trap (discounts co-timed with ad spikes).
   A naive spreadsheet analysis gets it badly wrong; our engine lands **~3.6×
   closer to the true answer** — proof the machinery works, demonstrated, not asserted.
5. **It refuses to act on weak data.** Of 585 cells, only 63 qualify as
   confirmed waste; 318 are flagged "fix availability, don't cut" and thin
   cells are flagged "Needs Test", not given invented numbers. Automated
   tests guard every release.

**What it is not:** it is a price-*response* engine, not a demand forecaster —
it won't predict your total sales next month, and we say so. Savings are quoted
as ranges, directionally validated.

## The pilot (how we start)

| Phase | What happens | You pay |
|---|---|---|
| **Week 1 — Data Readiness Report** | You share 6–12 months of daily platform data. We return a one-page verdict: what % of your portfolio can be acted on with confidence *right now*, per product and city. Yours to keep either way. | ₹25,000 (one-time, credited to month 1 if you continue) |
| **Months 1–3 — Weekly service** | Weekly price sheet + 30-min Monday review. We track **predicted vs. actual** on every move — the tool has a built-in Track Record sheet that fills with your own receipts. | **₹1,00,000 / month** |
| **After month 3** | Continue, renegotiate on documented savings, or stop. The track record is yours. | — |

**Why ₹1L is fair:** on our live reference portfolio the engine identifies
≈₹6.98 L/month of recoverable waste — roughly **7× the fee**, every month, on a
brand spending ₹82 L/month on discounts. On a ₹1–5 Cr/month discount budget the
fee pays for itself if we recover even **1–2%**. If the Week-1 readiness report
shows your data can't support action, we tell you that instead of taking the
retainer.

**Honest caveat we volunteer before you ask:** the ₹6.98 L figure is
*model-validated* (confounder-controlled, Double-ML-confirmed), **not yet
register-proven** — that is exactly what the weekly Track Record sheet exists
to prove or disprove, on your own numbers, within the pilot.

## What we need from you

- Daily sales export per SKU × city (Excel/CSV) — 6+ months.
- Your brand name(s) as listed on the platform.
- Optional: festival/promo calendar, cost structure (improves precision; not required).

Setup on our side is ~1 day. First readiness verdict inside week 1.

---

*Every accuracy figure in this document is regenerated from code on every run
(`scripts/diagnostics/` — credibility report, forward proof loop, recovery test)
and can be demonstrated live in the first meeting.*
