# output/ — generated deliverables

Everything Claude generates for you (files to open, share, or hand off) lands here —
not in the repo root and not in a temp folder. These are **regenerated from the latest
`v4_outputs/` run**, so they are outputs, not source; this folder is git-ignored.

| File | What it is |
|------|------------|
| `ACTION_PLAN_all_products.csv` | The single master sheet — one row per product × city with the action (Cut / Reinvest / Hold / Monitor), the discount to set, and why. Open in Excel/Sheets. |
| `ACTION_PLAN_all_products.xlsx` | Same, as an Excel workbook for sharing. |
| `DECISION_LOGIC_explainer.html` | The stakeholder one-pager explaining how each product gets its verdict (plain + technical). Open in any browser. |
| `OPTIMIZATION_REPORT.xlsx` | Decision-ready discount-spend optimization workbook: Executive Summary + every SKU's action / recommended spend / confidence + a full per-row confidence explanation + a Confidence Method sheet. Regenerate with `scripts/build_optimization_report.py`. |

To refresh these after a new monthly rebuild, ask Claude to "regenerate the action plan and explainer into output/".
