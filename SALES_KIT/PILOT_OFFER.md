# Discount Waste Recovery — Pilot Offer

*A done-for-you weekly pricing service for CPG brands on quick-commerce.*

---

## The problem you already know you have

You spend lakhs every month on discounts across Blinkit / quick-commerce.
Some of that discount genuinely drives volume. A lot of it doesn't — customers
would have bought at a higher price, and the difference is margin you gave away
for nothing. You can't see which is which, city by city, so the safe move has
been to keep discounting. That safety costs real money every month.

**Reference numbers from our live deployment** (a 4-SKU organic brand,
11 cities on Blinkit):

| Metric | Value |
|---|---|
| Gross sales | ₹78.9 L / month |
| Discount spend | **₹18.4 L / month** (23.3% weighted) |
| Recoverable waste identified by the engine | **≈ ₹1.76 L / month** across 29 product×city cells |

That's on just **4 SKUs**. A 30–50 SKU portfolio typically spends
₹1–5 Cr/month on discounts — and carries proportionally more waste.

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

1. **Held-out accuracy of the price engine: R² ≈ 0.87** at the decision grain
   (honestly self-rated "Moderate" — we publish the error, 25.8%, instead of
   hiding it).
2. **Forward validation:** trained on old data, tested on 8 weeks it never saw.
   Where price actually rose, the engine predicted a **13.8%** volume drop —
   reality was only **8.8%**. The engine is **conservative**: following it is
   *safer* than it claims, not riskier.
3. **Ground-truth recovery test:** we plant a known price-sensitivity in
   synthetic data with a deliberate trap (discounts co-timed with ad spikes).
   A naive spreadsheet analysis gets it badly wrong; our engine lands **~3.6×
   closer to the true answer** — proof the machinery works, demonstrated, not asserted.
4. **It refuses to act on weak data.** Cells with thin history are flagged
   "Needs Test", not given invented numbers. 32 automated tests guard every release.

**What it is not:** it is a price-*response* engine, not a demand forecaster —
it won't predict your total sales next month, and we say so. Savings are quoted
as ranges, directionally validated.

## The pilot (how we start)

| Phase | What happens | You pay |
|---|---|---|
| **Week 1 — Data Readiness Report** | You share 6–12 months of daily platform data. We return a one-page verdict: what % of your portfolio can be acted on with confidence *right now*, per product and city. Yours to keep either way. | ₹25,000 (one-time, credited to month 1 if you continue) |
| **Months 1–3 — Weekly service** | Weekly price sheet + 30-min Monday review. We track **predicted vs. actual** on every move — the tool has a built-in Track Record sheet that fills with your own receipts. | **₹1,00,000 / month** |
| **After month 3** | Continue, renegotiate on documented savings, or stop. The track record is yours. | — |

**Why ₹1L is fair:** on our small reference portfolio the engine identifies
≈₹1.76 L/month recoverable — the fee pays for itself if we recover just **5–10%
of a typical mid-size discount budget**. If the Week-1 readiness report shows your
data can't support action, we tell you that instead of taking the retainer.

## What we need from you

- Daily sales export per SKU × city (Excel/CSV) — 6+ months.
- Your brand name(s) as listed on the platform.
- Optional: festival/promo calendar, cost structure (improves precision; not required).

Setup on our side is ~1 day. First readiness verdict inside week 1.

---

*Every accuracy figure in this document is regenerated from code on every run
(`scripts/diagnostics/` — credibility report, forward proof loop, recovery test)
and can be demonstrated live in the first meeting.*
