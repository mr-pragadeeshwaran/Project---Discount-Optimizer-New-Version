# Stage 8 — The Flywheel

> Cut where it hurts least, reinvest where it grows most, keep total
> discount budget on target. Every week, automatically.

The flywheel is the **business framing** that turns per-cell pricing recommendations into a portfolio strategy. It's implemented in `stage8_output/waste_reinvest.py` and produces `WASTE_REINVEST_REPORT.md`, `waste.csv`, and `reinvest.csv`.

---

## The core idea (in plain English)

Today the portfolio sits at **23% weighted average discount**.
The brand target is **9%** weighted discount.

You can't just slash 14 ppt overnight — that surprises customers and tanks volume. Instead:

1. **Identify cells where discount is mostly wasted** (low elasticity, big revenue base) → recommend a price lift.
2. **Identify cells where deeper discount drives outsized volume** (high elasticity, room to grow) → recommend a price drop.
3. **Net effect**: weighted discount moves toward 9%, week after week, while strategic markets keep getting the investment they need.

```
                              ▲
                              │ revenue (₹/month)
   ┌──────────────────────────┴────────────────────────────┐
   │                                                       │
   │   CELLS WHERE WE'RE WASTING                           │
   │   DISCOUNT BUDGET                                     │
   │                                                       │
   │   ┌──┐ ┌──┐ ┌──┐ ┌──┐ ┌──┐                            │
   │   │  │ │  │ │  │ │  │ │  │  ← raise price slightly    │
   │   │  │ │  │ │  │ │  │ │  │     (low elasticity:       │
   │   └──┘ └──┘ └──┘ └──┘ └──┘      barely loses units)   │
   │     │    │    │    │    │                             │
   │     ▼    ▼    ▼    ▼    ▼                             │
   │   ┌────────────────────────┐                          │
   │   │  ₹ saved this cycle    │                          │
   │   └─────────────┬──────────┘                          │
   │                 │                                     │
   │                 ▼                                     │
   │   ┌────────────────────────┐                          │
   │   │ STRATEGIC REINVESTMENT │   ← drop price in        │
   │   │ POOL                   │      cells where         │
   │   └─────────────┬──────────┘      volume responds     │
   │                 │                  strongly           │
   │   ┌─────────────┴──────────────┐                      │
   │   ▼              ▼              ▼                     │
   │ ┌────┐         ┌────┐         ┌────┐                  │
   │ │HYD │         │BLR │         │MUM │   ← cells with   │
   │ │+70%│         │+39%│         │+25%│      |e|>2 and   │
   │ │vol │         │vol │         │vol │      headroom    │
   │ └────┘         └────┘         └────┘                  │
   │                                                       │
   └───────────────────────────────────────────────────────┘

       Result: weighted discount % moves toward target
               +  volume grows in strategic cells
               +  margin recovered from wasted cells
```

---

## How each side is identified

### Side 1 — WASTE (raise the price)

**Mechanism:** `_build_waste_table()` filters to cells where the model's margin-optimal price (the **elbow**) is higher than the current price. In practice with current cost structure, the elbow lands at MRP (0% discount) for almost every cell — the model says "for pure margin, don't discount at all".

**What's reported:**

| Column | What it is |
|---|---|
| `current_price` | What the customer pays today (last 30-day average of regular days) |
| `this_week_price` | What to set on Blinkit this week — **throttled to a max 3 ppt change per cycle** |
| `eventual_price` | The model's margin-optimal price — only reached over multiple weeks via phasing |
| `wasted_inr_per_month` | If you went all the way to `eventual_price`, how much monthly discount spend you'd save |
| `confidence` | Inherited from Stage 5 — High / Medium / Low / Needs Experiment |

**Why the 3 ppt cap:** sudden discount changes confuse customers, hurt brand trust, and risk a volume cliff. 3 ppt is the standard CPG e-comm cadence. Configurable in `v4_config.py` as `MAX_DISCOUNT_CHANGE_PPT`.

**Filter:** cells with confidence = "Needs Experiment" are excluded (those need price tests, not blind cuts) and shown separately in the "Needs Price Test" section.

### Side 2 — STRATEGIC REINVEST (drop the price)

**Mechanism:** `_build_reinvest_table()` simulates a `+3 ppt` discount move for every cell and keeps the ones where:

| Filter | Reason |
|---|---|
| `confidence ∈ {High, Medium}` | Don't reinvest into cells the model is unsure about |
| `|price_elasticity| ≥ REINVEST_MIN_ELASTICITY` (default 2.0) | Need real volume response for the math to work |
| `current_discount_pct < category_mean − 1` | Room to grow — don't reinvest where the cell is already deeply discounted vs peers |
| `quality_note doesn't say "elasticity at floor"` | Boundary-hit cells are unreliable |
| Simulated `vol_lift ≥ REINVEST_MIN_VOL_LIFT_PCT` (default 5%) | Move must produce meaningful volume |
| Simulated `margin_sacrifice ≤ REINVEST_MAX_MARGIN_SAC_PCT` (default 10%) | Don't burn more than 10% of contribution per cell |

A cell that passes all 6 filters becomes a strategic reinvestment candidate. The simulation uses the same dual-signal math as Stage 5:

```
new_units  = current_units × (new_price / current_price)^elasticity
                          × exp(badge_sensitivity × +3 ppt)
new_margin = (new_price − variable_cost) × new_units
```

In practice, many candidates pass with **negative margin sacrifice** —
meaning volume gain outweighs price drop and the move actually grows
margin too. Pure-win opportunities.

---

## The portfolio summary (top of the report)

```
                                Avg sell price as % of MRP   (equivalent discount)
Target:                                   91.00%               ( 9.00%)
Current:                                  76.92%               (23.08%)   gap: +14.08 ppt
After this-cycle PRICE LIFTS only:        79.76%               (20.24%)
After PRICE LIFTS + STRATEGIC DROPS:      78.83%               (21.17%)   <-- flywheel plan
```

Two number systems, same information:

- **Selling-price view** (what customers see): "Today we sell at 76.92% of MRP. Target 91%."
- **Discount view** (what gets entered on Blinkit): "Today we discount 23%. Target 9%."

`100% − weighted_discount = weighted_sell_price_as_pct_of_MRP`.

The **after PRICE LIFTS + STRATEGIC DROPS** line is the actual recommended plan: lifts reduce the discount budget, drops spend some of it back. Net is a smaller move toward target than lifts-alone, but with the volume growth from reinvestment baked in.

---

## Multi-cycle journey

A single cycle (one week) moves the weighted discount by roughly 1.5–3 ppt, depending on how much revenue sits in the cells being touched. At that pace:

```
Cycle 1 (this week):   23.08% → 21.17%   (−1.91 ppt)
Cycle 2 (next week):   21.17% → 19.30%   model re-optimises against fresh data
Cycle 3:               19.30% → 17.50%
   ...
Cycle 8:               ≈ 9.00%   ← target reached
```

The report says: *"At this pace it takes ~8 cycles to reach the 9.0% target. Re-run weekly; the plan re-optimises against fresh data each time."*

**Self-correcting**: if a category responds differently than predicted (e.g. Sunflower Oil volume drops more than expected after a lift), the next week's elasticities update, and the plan adjusts.

---

## Configuration knobs

In `v4_config.py`:

```python
# Portfolio target
TARGET_WEIGHTED_DISCOUNT_PCT = 9.0    # change to retune the flywheel

# How aggressively we move per cycle (the safety throttle)
MAX_DISCOUNT_CHANGE_PPT = 3           # max % point change per week per cell

# Strategic reinvest filters
REINVEST_MIN_ELASTICITY    = 2.0      # |elast| floor for a cell to be a candidate
REINVEST_MIN_VOL_LIFT_PCT  = 5.0      # min projected volume lift at +3 ppt
REINVEST_MAX_MARGIN_SAC_PCT = 10.0    # max acceptable margin sacrifice
```

Want to be more conservative on reinvestment? Raise `REINVEST_MIN_ELASTICITY` to 2.5 or `REINVEST_MIN_VOL_LIFT_PCT` to 8.

Want to take bigger steps per week? Raise `MAX_DISCOUNT_CHANGE_PPT` to 5 — but expect a higher chance of volume surprise.

Want a more aggressive overall target? Drop `TARGET_WEIGHTED_DISCOUNT_PCT` to 7 — the gap simply takes more cycles to close.

---

## Why use weighted discount as the portfolio metric?

Unweighted average ("just average the % across cells") gives equal voice to a tiny city with 5 units/day and Bangalore with 130 units/day. That's not what's actually happening at the wallet — most of the spend is in the big cells.

Weighted:
```
weighted_discount = Σ (discount_pct × monthly_revenue) / Σ monthly_revenue
```

This is the same as **revenue-weighted average discount**, which is the metric the brand's P&L sees — actual ₹ wasted on discount per month divided by gross revenue at MRP.

Equivalently, `(100% − weighted_discount)` is the **revenue-weighted average selling price as % of MRP** — exactly what a Blinkit consumer "experiences" on average when buying 24 Mantra products.

---

## What's in the output files

| File | Lead column | Use it for |
|---|---|---|
| `WASTE_REINVEST_REPORT.md` | flywheel summary + tables | **Open this first.** Brand-team Monday read. |
| `waste.csv` | `current_price → this_week_price → eventual_price` | Bulk-load cuts into Blinkit via export tooling |
| `reinvest.csv` | `current_price → new_price`, `+units/mo`, `budget` | Strategic reinvestment list, with `funded_by` linking to the waste cells that pay for them |
| `per_cell_detail.json` | full per-cell payload | Feeds the dashboard's drill-down view |

---

## Common questions

**Q: What if cuts > reinvestment opportunity?** That's the normal case at the start of an optimization journey — current weighted discount is well above target, and there aren't yet enough reinvest-worthy cells to absorb all the savings. The savings drop straight to margin. Over time, as discounts come down across the portfolio, more cells qualify for reinvestment.

**Q: What if reinvestment > cuts?** Then weighted discount would *rise* this cycle. The system still recommends the moves (each is individually a positive economic call), but if you want to enforce a hard target, you'd skip the lowest-priority reinvest cells until cuts ≥ reinvestment for that cycle. Not implemented today; configurable knob for the next iteration.

**Q: Why doesn't the model just sweep all the way to elbow in one cycle?** Because the model is built on historical data — applying its full elbow recommendation in one step is an out-of-distribution action. The 3-ppt cap keeps each step inside what we've actually observed, and lets the model re-learn from each week's response.

**Q: How does this connect to Stage 7's "Strong Cut" tier?** Stage 7 picks the **best-fit single-cycle action** per cell (using throttled metrics). Stage 8 takes those same cells, splits them into the cut vs reinvest sides of the flywheel, and adds the portfolio-level math. Stage 7 is "what to do per cell"; Stage 8 is "how does the whole portfolio rebalance".
