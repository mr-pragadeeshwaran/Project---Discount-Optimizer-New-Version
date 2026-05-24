# Stage 8 — The Flywheel

> Walk each SKU × city from today's discount down to **its own
> proven-safe historical floor**, over a user-set duration. Each weekly
> step is at least 3 ppt (no trivial moves), the full gap is closed
> within that duration, and a strategic reinvestment side pumps growth
> markets at the same time. The portfolio weighted discount glides
> down on a predictable, transparent path.

The flywheel is implemented in `stage8_output/waste_reinvest.py` and
`stage8_output/excel_report.py`. It writes:

- `WASTE_REINVEST_REPORT.xlsx` — McKinsey-style 6-sheet workbook with
  live formulas (open this for the brand-team view)
- `WASTE_REINVEST_REPORT.md` — same content as plain Markdown
- `waste.csv`, `reinvest.csv` — flat tables for bulk upload
- `per_cell_detail.json` — full payload for the HTML dashboard

---

## The core idea (in plain English)

Today the portfolio sits at ~23% revenue-weighted average discount.
Cutting blindly to a 9% target would shock customers and tank volume.

The flywheel does it the safe way:

1. For each cell, look at its own past — find the **historical floor**:
   the lowest discount level the cell has actually operated at recently
   (lower quartile of last 90 days of regular-day discounts).
2. Plan a glide from current → floor, with each weekly cut at least
   3 ppt, spread over up to 3 months (12 weeks).
3. Cells where the model also sees strong volume response to deeper
   discount become **strategic reinvest** candidates — drop their price
   slightly to grow volume in those markets.
4. Every week, the system re-reads the latest sales data and re-plans
   from scratch. If a cell's response surprises us, the plan adapts.

```
                              ▲
                              │ revenue (₹/month)
   ┌──────────────────────────┴────────────────────────────┐
   │                                                       │
   │   CELLS OVER-DISCOUNTED vs THEIR OWN HISTORY          │
   │                                                       │
   │   ┌──┐ ┌──┐ ┌──┐ ┌──┐ ┌──┐                            │
   │   │  │ │  │ │  │ │  │ │  │  ← raise price toward      │
   │   │  │ │  │ │  │ │  │ │  │     the cell's own         │
   │   └──┘ └──┘ └──┘ └──┘ └──┘     historical floor       │
   │     │    │    │    │    │      (≥3 ppt/week steps)    │
   │     ▼    ▼    ▼    ▼    ▼                             │
   │   ┌────────────────────────┐                          │
   │   │  ₹ saved this cycle    │                          │
   │   └─────────────┬──────────┘                          │
   │                 │                                     │
   │                 ▼                                     │
   │   ┌────────────────────────┐                          │
   │   │ STRATEGIC REINVESTMENT │   ← drop price in        │
   │   │ POOL                   │      high-elasticity     │
   │   └─────────────┬──────────┘      growing markets     │
   │                 │                                     │
   │   ┌─────────────┴──────────────┐                      │
   │   ▼              ▼              ▼                     │
   │ ┌────┐         ┌────┐         ┌────┐                  │
   │ │HYD │         │BLR │         │MUM │   ← +3 ppt deeper│
   │ │+70%│         │+39%│         │+25%│      discount    │
   │ │vol │         │vol │         │vol │                  │
   │ └────┘         └────┘         └────┘                  │
   │                                                       │
   └───────────────────────────────────────────────────────┘

   Result: weighted discount glides toward each cell's own floor;
           volume grows in strategic markets;
           portfolio P&L improves without surprising the customer.
```

---

## The per-cycle step rule (the math you can audit)

For every cell, every week:

```
gap = | current_discount − target_discount |

if gap < 0.1:                step = 0       (already done)
elif gap ≤ MIN (3 ppt):      step = gap     (one-shot move)
else:                         step = max(MIN, gap / TIMELINE_WEEKS)
                                     bounded by MAX (5 ppt absolute)
```

Defaults (in `v4_config.py`):

| Knob | Default | What it controls |
|---|---:|---|
| `MIN_DISCOUNT_CHANGE_PPT` | 3 | Minimum weekly move. Smaller moves don't meaningfully shift the customer price. |
| `MAX_DISCOUNT_CHANGE_PPT` | 5 | Hard safety cap per week. |
| `TARGET_TIMELINE_WEEKS` | 12 | Budget — gaps > 3 ppt spread over up to this many weeks. |

### Worked examples (from a real run)

| Gap | Calculation | Per-cycle step | Cycles to close |
|---:|---|---:|---:|
| 0.5 ppt | gap < 3 → one-shot | 0.5 ppt | 1 |
| 2.2 ppt | gap < 3 → one-shot | 2.2 ppt | 1 |
| 3.4 ppt | gap > 3, gap/12 = 0.28 → use MIN | 3 ppt | 2 (3 + 0.4) |
| 4.6 ppt | gap > 3, gap/12 = 0.38 → use MIN | 3 ppt | 2 (3 + 1.6) |
| 36 ppt | gap > 3, gap/12 = 3.0 → use raw | 3 ppt | 12 |
| 60 ppt | gap > 3, gap/12 = 5.0 → at MAX cap | 5 ppt | 12 |

**Always closes the full gap.** No cell stops half-way.

---

## What the target is — historical floor, not 0%

Two choices in `v4_config.py`:

```python
USE_HISTORICAL_FLOOR_TARGET  = True   # default
HISTORICAL_FLOOR_PERCENTILE  = 25     # lower quartile
HISTORICAL_FLOOR_LOOKBACK_DAYS = 90
```

### When `USE_HISTORICAL_FLOOR_TARGET = True` (default)

For each cell:
```
historical_floor = max(0, P25 of cell's regular-day discounts in last 90 days)
target_discount  = max(elbow_discount, historical_floor)
```

The cell has been at-or-below `historical_floor` on ~25% of days with healthy enough sales to be in the dataset. By construction it's a **proven safe** floor — we know returning to it won't crash volume.

The `max(elbow, floor)` is the safety belt: if Stage 6's margin-optimal elbow is somehow above the historical floor (rare), use the elbow instead.

### When `USE_HISTORICAL_FLOOR_TARGET = False`

Target = elbow discount (margin-optimal). With current cost structure (50% COGS, 15% commission), elbow lands at 0% for almost every cell — "for pure margin, don't discount at all". The glide would then walk every cell to 0% over 12 weeks. Use this mode only if you've decided to chase pure margin and accept the volume risk.

---

## How the cut side identifies cells

`_build_waste_table()` includes cells where `current_discount > elbow + 2 ppt` AND confidence is High/Medium/Low. Cells flagged "Needs Experiment" are excluded.

Each waste cell carries:

| Column | What it is |
|---|---|
| `current_price` | What the customer pays today (last 30-day average of regular days) |
| `this_week_price` | What to set on Blinkit this Monday — the throttled step calculated by the rule above |
| `historical_floor_disc` | The cell's own proven-safe floor (target for the multi-week glide) |
| `wasted_inr_per_month` | Monthly discount spend that's above the elbow |
| `confidence` | High / Medium / Low — inherited from Stage 5 |
| `phasing_plan` | Multi-step walk e.g. `25.2% → 22.2% → 20.6%` |

---

## How the reinvest side identifies cells

`_build_reinvest_table()` simulates a `+3 ppt` discount move for every cell and keeps those that pass all six gates:

| Filter | Reason |
|---|---|
| `confidence ∈ {High, Medium}` | Don't reinvest where the model is unsure |
| `|elasticity| ≥ REINVEST_MIN_ELASTICITY` (2.0) | Need real volume response |
| `current_discount < category_mean − 1 ppt` | Room to grow vs peers |
| `quality_note` doesn't say "elasticity at floor" | Boundary cells are unreliable |
| Simulated `vol_lift ≥ 5%` | Move must produce meaningful units |
| Simulated `margin_sacrifice ≤ 10%` | Don't burn more than 10% of contribution |

Cells passing all 6 gates get a `+3 ppt` one-shot move. The simulation uses the same dual-signal math as the model:

```
new_units  = current_units × (new_price / current_price)^elasticity
                          × exp(badge_sensitivity × +3 ppt)
new_margin = (new_price − variable_cost) × new_units
```

In practice many candidates pass with **negative margin sacrifice** — volume gain outweighs price drop and the move grows margin too. Pure-win opportunities.

---

## The Glide Path sheet (Excel report, sheet 2)

Visualises the week-by-week portfolio projection. Header card:

```
                         Today        After N weeks         Change
Weighted discount %      23.31%       22.40%               −0.91 ppt
Monthly discount spend   Rs.18.38L    Rs.17.91L            −Rs.46,911/mo
Monthly net revenue      Rs.60.49L    Rs.62.07L            +Rs.157,784/mo
Gap to target            +14.31 ppt   +13.40 ppt           still short
```

Then a row per cycle:

```
Cycle  Label    Wt Disc%  Gross/mo     Spend/mo     Net Rev/mo   Units/mo  Cum Save  Gap
0      Today    23.31     7,887,196    1,838,138    6,049,058    53,283    0         14.31
1      Week 1   23.09     7,985,988    1,844,194    6,141,794    53,523   −6,057     14.09
2      Week 2   22.76     7,991,088    1,818,709    6,172,378    53,580   19,428     13.76
3      Week 3   22.42     7,997,094    1,793,296    6,203,798    53,646   44,842     13.42
4      Week 4   22.40     7,998,069    1,791,227    6,206,842    53,657   46,911     13.40  ← plan complete
```

Trailing identical rows are trimmed — once all cells reach their floor, the table stops showing flat weeks.

### Why the portfolio doesn't reach 9%

Per-cell floors land at 12-22%, so the weighted-average minimum the portfolio can hit (without violating "never go below proven safe") is ~22%. The Glide Path sheet honestly says "still 13.40 ppt short of 9% target — to close more, lower the floor (HISTORICAL_FLOOR_PERCENTILE) or run pilots on Needs-Test cells".

---

## Why use historical floor instead of elbow?

| Concern | Elbow (0%) target | Historical floor target ✓ |
|---|---|---|
| Customer surprise | Push price to MRP — never seen before, big volume risk | Return price to a level the cell ran at recently — proven safe |
| Volume guarantee | Predicted from model only (extrapolation) | Backed by actual past sales |
| Brand trust | One-shot 22 ppt journey eventually | Bounded 4-5 ppt total walk |
| Brand-team comfort | Hard to defend ("0% discount? customers will leave") | Easy to defend ("we've been there before") |

The historical-floor framing is also what brand managers actually negotiate around in CPG — they protect a "lowest acceptable promo depth" per SKU as part of brand health, and the system now respects that as a hard limit.

---

## Why use weighted discount as the portfolio metric

```
weighted_discount = total monthly discount spend ÷ total monthly gross sales at MRP × 100
```

This is the standard brand-finance ratio: of every ₹100 of potential
revenue at MRP, how many rupees went out as discount.

An unweighted average ("just average the % across cells") would give equal voice to a tiny city with 5 units/day and Bangalore with 130 units/day — that's not what hits the P&L.

---

## Configuration cheat sheet

In `v4_config.py`:

```python
# Targets and pace
USE_HISTORICAL_FLOOR_TARGET   = True   # walk to floor (not elbow / 0%)
HISTORICAL_FLOOR_PERCENTILE   = 25     # lower-quartile of past discounts
HISTORICAL_FLOOR_LOOKBACK_DAYS = 90    # past-90-day window for the floor
TARGET_TIMELINE_WEEKS         = 12     # 3 months budget for full glide
MIN_DISCOUNT_CHANGE_PPT       = 3      # smallest weekly move
MAX_DISCOUNT_CHANGE_PPT       = 5      # absolute safety cap

# Strategic reinvest filters
REINVEST_MIN_ELASTICITY       = 2.0
REINVEST_MIN_VOL_LIFT_PCT     = 5.0
REINVEST_MAX_MARGIN_SAC_PCT   = 10.0

# Portfolio target (for reporting the gap)
TARGET_WEIGHTED_DISCOUNT_PCT  = 9.0
```

| You want | Change |
|---|---|
| Bigger weekly moves | Raise `MIN_DISCOUNT_CHANGE_PPT` to 5 |
| Deeper end-of-glide cuts | Lower `HISTORICAL_FLOOR_PERCENTILE` to 10 |
| Finish faster | Lower `TARGET_TIMELINE_WEEKS` to 8 |
| Pure margin chase (ignore floor) | Set `USE_HISTORICAL_FLOOR_TARGET = False` |
| Fewer reinvest cells | Raise `REINVEST_MIN_ELASTICITY` to 2.5 |

---

## Output file reference

| File | Lead view | Use it for |
|---|---|---|
| `WASTE_REINVEST_REPORT.xlsx` | 6 sheets: Summary · Glide Path · By Product · Price Lifts · Price Drops · Needs Test (+ hidden Data) | **Open this first.** McKinsey-style brand-team workbook with live formulas. |
| `WASTE_REINVEST_REPORT.md` | Same content, plain text | Git-friendly version, grep-able |
| `waste.csv` | per-cell cuts | Bulk Blinkit upload tooling |
| `reinvest.csv` | per-cell drops | Strategic invest list with `funded_by` |
| `per_cell_detail.json` | full payload | Feeds the dashboard drill-down |

---

## Common questions

**Q: Why does the Glide Path stop at week 4 even though the budget is 12 weeks?**
Because most cells' gaps are small (2-5 ppt). At a 3 ppt minimum step, those gaps close in 1-2 cycles. The 12-week budget is just a ceiling — if the math finishes faster, that's fine.

**Q: What if a cell's historical floor is HIGHER than its current discount?**
Then the cell is already at-or-below its floor and won't appear in cuts. Reinvest logic still applies if it qualifies.

**Q: What if reinvestment > cuts?**
Then weighted discount would *rise* this cycle. The system still recommends the moves (each is individually a positive economic call). You can enforce a hard budget by lowering `REINVEST_MAX_MARGIN_SAC_PCT` or raising `REINVEST_MIN_ELASTICITY`.

**Q: How is this connected to Stage 7's tier?**
Same step rule. Stage 7 picks **this week's** action per cell using exactly this formula. Stage 8 simulates all weeks forward to show the full glide path. They produce consistent numbers.

**Q: What happens if a cell finishes its glide in week 3 but the report is run again in week 5?**
Stage 8 re-reads the latest data. If the cell stayed at its floor with healthy units, no further action is recommended. If the floor itself has shifted (e.g. competitor pricing changed), the new floor is recomputed and a new glide is planned.
