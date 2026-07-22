# Data / fields that would materially improve accuracy (next run)

*Run `20260705_161703`. Ranked by how much each would tighten the discount attribution and lift the defensible savings figure.*

| # | Field | Status now | Why it matters |
|---|---|---|---|
| 1 | **Competitor price & competitor discount** (per SKU/city/week) | `Competitor Price` column exists but is **100% empty** | Competitive intensity is currently proxied by our own category-share. Real competitor price/discount would let us separate *defensive* discounting (bucket b) from *waste* (bucket c) properly — today those cells are the biggest source of uncertainty. This is the single highest-value add. |
| 2 | **Cost of goods / margin per SKU** | not supplied (net-revenue break-even used) | Break-even is computed on *revenue*. With true COGS we'd optimize on *contribution*, which is stricter and would surface more genuine waste — the honest figure could rise. |
| 3 | **Search-impression / keyword rank** (not just Ad SOV) | only `MONTHLY_AD_SOV` | Organic discoverability is a top sales driver here. A rank/impression signal would explain more of the 'flat despite discount' cells and reduce the Experimental bucket. |
| 4 | **Promo calendar / deal-type flags** (BOGO, bank offer, Blinkit-funded vs brand-funded) | inferred from discount % only | Reverse causality (discounting *because* sales dropped) is the main threat to the discount coefficient. Knowing *when and why* a promo ran would remove it and make more cells High-confidence. |
| 5 | **Stock-out timestamps / days-of-cover** | only daily `WT_AVAILABILITY_PCT` | Finer availability data would sharpen the a_stock bucket (212 cells) and stop availability noise from contaminating discount reads. |
| 6 | **New-launch / distribution-expansion dates** | not supplied | 183 cells are 'growing on non-discount' — some is just new-store rollout. Tagging launches would separate true demand growth from distribution and refine the reinvest list. |

**Bottom line for the client:** the model is already trustworthy for *directional* cut/keep/reinvest calls, but **#1 (competitor price) is empty and #2 (COGS) is missing** — supplying those two would move the biggest chunk of cells out of 'Experimental' into 'High-confidence' and is the fastest path to a tighter savings number.
