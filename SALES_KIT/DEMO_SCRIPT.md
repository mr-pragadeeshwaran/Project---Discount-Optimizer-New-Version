# 15-Minute Demo Script — Discount Waste Recovery

*Run `run.bat` before the meeting so the latest `WASTE_REINVEST_REPORT.xlsx`
is open. Have `SALES_KIT/PILOT_OFFER.md` ready to hand over at the end.*

---

## Minute 0–2 · The hook (say this, don't show anything yet)

> "You're spending roughly ₹___ a month on discounts. Some of it drives volume.
> Some of it is pure giveaway — customers who'd have bought anyway. Today I'll
> show you a system that tells you, product by product, city by city, which is
> which — and the exact price to set every Monday. And I'll show you the proof
> it works *before* asking you to trust it."

Ask one question and note the answer: **"What's your monthly discount spend
across quick-commerce?"** — every number that follows gets anchored to it.

## Minute 2–5 · Summary sheet (the money view)

Open **Sheet 1 — Summary**. Point at three rows only:

1. Discount spend/month (our live reference: **₹81.8L/month** across 84 products × 11 cities).
2. "After cuts" column — spend goes down while units barely move.
3. Model accuracy block — **read the tier and R² live off the sheet; never quote
   from memory** (the system self-rates, currently "Moderate", and publishes
   its own error rate).

> "Notice it calls itself *Moderate*, not *excellent*. This system publishes
> its own error rate. Everything I show you is built to survive your data
> scientist's scrutiny, not to impress you."

## Minute 5–8 · Track Record (the receipts — most important 3 minutes)

Open **Sheet 3 — Track Record**.

> "We trained the model on old data, then made it predict 8 weeks it had never
> seen. Where price actually went up, it predicted a 13.8% volume drop — the
> real drop was 8.8%. It's *conservative*: reality was safer than its warning.
> When it says 'raise this price, you'll keep the volume' — the risk runs in
> your favour."

Then Part B: "Once you act on a recommendation, this section fills with *your*
predicted-vs-actual results. Month 3 renewal is based on this sheet, not my word."

## Minute 8–10 · Leakage (the thing nobody else shows)

Open **Sheet 4 — Leakage**. Pick the top Borrowed-% row (currently Sunflower
Oil, ~12–18% in the worst cities — **read the live number off the sheet**):

> "This product's promo bumps look great — but [read %] of that bump is customers
> *stockpiling*: sales borrowed from next month, not new demand. And these cells
> here are *inelastic* — discounting them mathematically can't pay, so we say
> 'hold or raise', not 'spend more'. Most agencies count every promo unit as a win.
> We net out the borrowed and stolen units first."

## Minute 10–12 · By Product (what your team actually uses)

Open **Sheet 6 — Price Lifts** and take the TOP row (the biggest ₹/month), then
show the same city on **Sheet 5 — By Product** for its week-by-week prices.
**Read the live numbers off the sheet — never quote from memory; the data moves
weekly and a stale number in this meeting kills the credibility story.**

> "Current price ₹__ → target ₹__ over __ weeks — the exact weekly steps are
> right here. This single row is worth ₹__/month; across the portfolio the
> engine currently identifies ≈₹6.98L/month of confirmed waste (63 product×city
> cells, each double-checked by an independent causal method). Your ops team
> executes these rows on Monday; that's the whole workflow. Cells the model
> isn't sure about say 'Needs Test' — it refuses to guess."

## Minute 12–14 · The skeptic's pack (only if their analyst is present)

> "Three documents your data team can tear apart: a credibility report
> separating real accuracy from statistical inflation; the forward backtest;
> and a recovery test where we plant a known answer in synthetic data with a
> deliberate trap — a naive analysis gets it wrong by 3–4×, this engine finds it.
> All regenerated from code on every run. I'll send all three today."

## Minute 14–15 · Close

Hand over the pilot offer:

> "Week one is a ₹25k readiness report on *your* data — per product, per city,
> what can be acted on now. Yours to keep either way. If your data can't support
> action, the report says so and we stop there. If it can, the weekly service is
> ₹1L/month and every claim gets tracked against reality on the Track Record
> sheet. When can you share the data export?"

**The only goal of this meeting: leave with the data export agreed.**

---

## Objection cheat-sheet

| They say | You say |
|---|---|
| "Our agency already optimizes discounts" | "Ask them for their held-out accuracy and their predicted-vs-actual sheet. We publish both — and our forward test shows we *under*-promise." |
| "That R² isn't perfect" | "Correct — and it says so itself. The alternative isn't a perfect model, it's gut feel. Directionally validated + conservative beats confident guessing." |
| "What if it's wrong for a city?" | "Every move is 3 points/week to a price the city has already survived, with a weekly review. Worst case for one cell ≈ one week of small mispricing; upside is permanent." |
| "Why not build in-house?" | "You could — in 3–6 months of a data scientist's time. The pilot costs less than one of those months and starts Monday." |
| "Price feels high" | "Anchor it to your discount line, not software: recovering 5–10% of a ₹1Cr+/month discount budget pays the fee many times over. And month 1 credits the readiness fee." |
