# Can we save ₹5 lakh/month? — the honest verdict

*24 Mantra Organic · Blinkit · 6 months / 26 weeks / 84 products · confounder-controlled
model with reverse-causality control · out-of-sample R² = 0.78 (≥0.75 bar MET)*

## Short answer

**The accuracy goal is met (R² = 0.78 out-of-sample). The ₹5L saving is *reachable in
expectation but not locked* — it hinges almost entirely on one category, Dal & Pulses,
whose signal is strong but not rock-solid.** I will not report a clean "₹5L achieved,"
because the receipts don't support banking it as a certainty today.

## The savings, by how much you can trust them

| Tier | ₹/month | What it is |
|---|---:|---|
| **Rock-solid (bank today)** | **~₹2.9 L** | Cuts stable in ≥80% of bootstrap resamples: Rice, Sooji, Millet Atta, Whole Spices, Wheat Atta, Jaggery. Discount reliably doesn't drive volume; trims are moderate + well-supported. |
| **Central estimate (expected)** | **~₹5.0–5.9 L** | Every cut weighted by its statistical stability × savings. This clears ₹5L — but it *includes* the shakier Dal cuts at their success probability. |
| **Full model ceiling** | **~₹7.2 L** | All identified waste, cut deep to the observed floor. Requires the aggressive Dal 22%→2% trims. |

## Why it hinges on Dal & Pulses

- Dal is **₹4.17 L/mo — 58% of the total** — because it's your highest-volume staple, so it
  carries the most discount spend, and staples are the *least* discount-sensitive (people buy
  their usual dal regardless of a few % off). Economically this is the most believable waste.
- **But** Dal's "discount is waste" signal is **73% stable** (vs 97–100% for Rice/Millet/Spices):
  in ~1 of 4 resamples its effect CI touches the break-even line. And the ₹4.17L assumes cutting
  22%→2% — a deep extrapolation.
- Two risk factors stacked on the one category that decides ₹5L. That's a **test-first**, not a
  bank-first, opportunity.

## Confirmed three independent ways — ₹5L is not lockable from this data

After the first pass I pushed the method further (as the goal demands). All three agree:

1. **Week block-bootstrap:** Dal stays "waste" in only **73%** of resamples (vs 97–100% for Rice/Millet/Spices).
2. **Cluster-robust standard errors** (the proper panel tool, clustered by cell): Dal's marginal
   discount effect CI is [+0.0026 ± 0.011] → **upper bound +0.0133, which sits ABOVE the break-even
   threshold 0.0128.** Under correct inference we cannot rule out that Dal discount *works*.
   The other categories clear it → cluster-robust locked savings = **₹2.91 L/mo** (matches the bootstrap).
3. **Raw Dal data** — mean weekly units by discount band: 0–5% → 38 units, 10–15% → 102, 20–25% → 119,
   25%+ → 195. **Volume rises ~5× with discount.** Cutting 22%→2% lands where observed volume is ~70%
   lower. The model blames confounders for most of that, but "most" ≠ "all", and the CI agrees it's uncertain.

**This is a genuine information limit, not a modeling shortfall.** More model tweaking would be
overfitting. Only two things can lock the Dal ₹4.17 L: (a) an **in-market test** (cut 3ppt on half
the Dal cells, watch 2–3 weeks), or (b) **new data that resolves the confounding** — the
100%-empty `Competitor Price` column and a promo-calendar/deal-type flag are the specific fields
that would let the model separate "Dal sells because it's discounted" from "Dal is discounted when
it's already selling."

## The honest path to ₹5L

1. **Bank the ₹2.9 L of rock-solid cuts now** (Rice, Sooji, Millet Atta, Whole Spices, Wheat Atta,
   Jaggery). Glide 3ppt/step, watch weekly, revert if units fall faster than predicted.
2. **Run the Dal & Pulses test — this is the ₹5L decision.** Cut discount 3ppt on half the Dal
   cells (A/B vs held cells), watch 2–3 weeks. If volume holds (73% of the evidence says it will),
   glide deeper. Dal alone then pushes you past ₹5L.
3. **Reinvest** the freed budget into Oil / Wheat-Daliya (the only cells where discount reliably
   *pays*, +₹33k/mo) — don't just cut, reallocate.

**Bottom line:** ₹5L is a fair *expectation*, not a guarantee. The guaranteed floor is ~₹2.9L; the
gap is one Dal test away. Anyone who tells you the ₹5L is locked today is reading a script, not the
data.
