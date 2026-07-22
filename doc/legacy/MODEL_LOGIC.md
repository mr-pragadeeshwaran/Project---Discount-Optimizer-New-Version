# Model Logic — how the system thinks, in business language

> One-page mental model for a brand stakeholder. No statistics jargon. For the full technical design see [MODEL.md](MODEL.md); for the evidence behind every choice see [MODEL_EXPERIMENTS.md](MODEL_EXPERIMENTS.md).

---

## The core question the system answers

For every (SKU × city) combination — a "cell" — what discount level maximises profitable units this brand can sell on Blinkit *this week*, subject to (a) not destroying margin and (b) not over-promising on data we don't have?

The system answers this **per cell**, then rolls up to a portfolio plan.

---

## The four ingredients per cell

For each cell, the system needs to know four things:

1. **Where the cell is today** — current price, current discount, current daily units, current weekly revenue.
2. **How sensitive demand is to price** — the elasticity. If we move the price by 1 %, by how much do units change?
3. **What's the cost structure** — COGS, platform commission, fulfilment fee. Determines where the margin-optimal "elbow" is.
4. **How confident we are in #2** — because acting on a wrong elasticity is the most expensive mistake in this system.

Ingredients 1, 3 are factual. Ingredient 2 is learned from history. Ingredient 4 is the heart of the May 2026 redesign.

---

## How elasticity is learned

The system doesn't just look at "price went down, units went up" — that's confounded by everything else (day of week, ad spend, season, stockouts, etc.).

Instead, it fits a model **per category** (Jaggery, Moong Dal, Sunflower Oil, …) that simultaneously accounts for:

- Each cell's **baseline level** (Mumbai-Oil naturally sells more than Lucknow-Jaggery — that's not about price).
- The **price effect** itself (the elasticity).
- **Day-of-week** (Sundays are different from Wednesdays for groceries).
- **Recent momentum** (yesterday's units, last week's units, 7-day and 14-day averages — captures non-price demand drift).
- **Seasonality** (month dummies, festivals via Stage 2 filtering).
- **Operational signals** (stock availability, ad share, competitor price ratio).

What's left over after explaining all of that is, mechanically, the **pure price effect**. That's the elasticity.

This is then **shrunk** toward the category's median elasticity — a single noisy cell can't pull its own coefficient too far from what the broader category tells us. The further the cell's own data pulls, the more its data has to back the pull up.

---

## The May 2026 redesign in one sentence

The previous version got the **average** right but couldn't be trusted **per cell** — the pooled R² of 0.84 was hiding the fact that 75 % of cells had a negative within-cell R² (the model was barely better than guessing the cell's mean).

The fix had two parts:

1. **Better features** (lag, momentum, day-of-week) so the model could explain within-cell variation. This brought the median within-cell R² from −0.43 to −0.09 and the aggregated decision-grade R² from 0.93 to **0.97**.
2. **A multi-factor confidence score** (0–100) per cell. *This is what makes the system trustworthy at scale.* Even when the elasticity number looks fine, the score may say "we don't have enough evidence to act on this cell yet" — and the system then refuses to act on it.

---

## The confidence score, in plain English

For each cell, the system asks five questions:

| Question | Answer is YES when… | Weight |
|---|---|---|
| Do we have enough days of data? | ≥ 120 training rows | 25 % |
| Has the discount actually moved? | ≥ 15 distinct discount levels observed | 20 % |
| Does the model even fit this cell's history? | In-sample R² ≥ 50 % | 20 % |
| Is the elasticity in a sane CPG range? | Between −0.3 and −4.0 | 15 % |
| Is the elasticity tightly pinned down (not noisy)? | t-statistic ≥ 4 | 20 % |

The score is the weighted sum (0 to 100). The tier maps as:

| Score | Tier | What the system does |
|---|---|---|
| ≥ 70 | **HIGH** | Normal recommendations; act this week. |
| 50–70 | **MEDIUM** | Recommendations available, but with smaller weekly steps. |
| 30–50 | **LOW** | No Strong Cut allowed. Trade-off only (manager review). |
| < 30 | **DO_NOT_ACT** | Locked out. The cell needs a structured A/B price test first. |

This is the hard gate. A cell with a "great-looking" 25 % savings number but a LOW or DO_NOT_ACT tier will **never** become a Strong Cut.

---

## Why per-cell daily R² isn't the right gate

A common question: "if the model has 0.97 R² overall, why can't I see ≥ 0.70 R² for every individual cell?"

Because at the daily grain, within a single cell, demand has a **noise floor** that no model can break — weather, last-mile shocks, single-day promo confusion, weekend bunching. These are unpredictable from anything in the data.

The 0.97 figure is at the **3-percentage-point discount-bin grain** — pool a cell's days into 0 %, 3 %, 6 %, … discount bins and compare the model's predicted average to the actual average. That's the grain that matches a pricing decision ("if I go from 12 % discount to 9 %, what happens to units?"), and there the model is excellent.

We tried 7 different model classes (LightGBM, hierarchical ridge, hybrid OLS-GBM, per-cell GBM, etc.) to see if any could break the daily noise floor. None could. The complexity-budget is best spent on **features and confidence gating**, not a fancier learner. The evidence is in [MODEL_EXPERIMENTS.md](MODEL_EXPERIMENTS.md).

---

## The decision flow, summarised

```
For each cell:

   1. Predict the saturation curve   (Stage 5: sweep price, predict units)
                ↓
   2. Add the cost ladder            (Stage 6: contribution margin per step)
                ↓
   3. Find the "elbow"                (margin-optimal discount level)
                ↓
   4. Choose a target                  (historical floor if safer than elbow)
                ↓
   5. Throttle to a one-week step      (cap by TIMELINE_WEEKS)
                ↓
   6. Tier the action                  (Strong Cut / Trade-off / Hold /
                                        Increase / Do Not Act)
                ↓
   ★ 7. APPLY THE CONFIDENCE GATE      ★ (May 2026)
        - DO_NOT_ACT cell?  → force "Do Not Act"
        - LOW cell?         → cap at "Trade-off"
        - HIGH/MEDIUM?      → tier stands
                ↓
   8. Roll up into the portfolio plan  (Stage 8: cuts ↔ reinvest, weighted-disc target)
```

The brand team sees the **final tier** in the report, knows it's already been through the confidence gate, and can audit any decision back to the elasticity and confidence sub-scores in `elasticity_estimates.csv`.

---

## What the brand team is being asked to trust

Three things, in order of decreasing certainty:

1. **The data we used.** Their own daily sales export, deduplicated and outlier-cleaned, with a full audit trail.
2. **The elasticity per cell.** A coefficient computed from their own data, shrunk to their own category's median, with a confidence score per cell.
3. **The recommendation.** Derived from the elasticity, the cost structure they confirmed, and a target discount we can defend with respect to their own history.

The confidence score is what makes #2 → #3 honest. The system never recommends a Strong Cut on a cell whose elasticity it doesn't trust.

---

## Reading list

- [ARCHITECTURE.md](ARCHITECTURE.md) — the whole pipeline end to end
- [MODEL.md](MODEL.md) — Stage 4 design with formulas
- [MODEL_EXPERIMENTS.md](MODEL_EXPERIMENTS.md) — the 7-experiment robustness study
- [OUTPUTS.md](OUTPUTS.md) — every output file column by column
- [SCALING_PLAYBOOK.md](SCALING_PLAYBOOK.md) — onboarding a new brand
