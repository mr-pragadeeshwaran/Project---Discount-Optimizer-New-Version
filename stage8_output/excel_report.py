"""
Excel report generator — McKinsey-minimalist style with live formulas.

Design principles:
  - All numbers live on a hidden Data sheet
  - Summary, By Product, and detail sheets reference Data via SIMPLE
    formulas (SUM, SUMIF, SUMPRODUCT, IF, ROUND) — compatible with
    Excel 2010+ (no XLOOKUP, LET, LAMBDA, dynamic arrays, etc.)
  - The user can edit Data values and watch Summary recalculate live —
    nothing is hard-coded except section labels and the date stamp.
  - Styling: single font (Calibri), navy accent used sparingly,
    hairline borders only, lots of whitespace.

Sheets created:
  Summary       — Portfolio + This Week's Plan + Model Accuracy
  By Product    — Per-SKU breakdown
  Price Lifts   — Detailed cut recommendations
  Price Drops   — Detailed reinvest recommendations
  Needs Test    — Low-confidence cells
  Data          — Raw per-cell data (source of truth for formulas)
"""
from openpyxl import Workbook
from openpyxl.styles import (
    Font, Alignment, Border, Side, PatternFill, NamedStyle
)
from openpyxl.utils import get_column_letter
from openpyxl.utils.dataframe import dataframe_to_rows
import pandas as pd
import os


# ────────────────────────────────────────────────────────────────────
# Design tokens
# ────────────────────────────────────────────────────────────────────
FONT_FAMILY = "Calibri"
INK   = "FF0F172A"   # primary text (near-black)
BODY  = "FF1F2937"
MUTED = "FF6B7280"
ACCENT = "FF1E3A5F"  # slate navy — used very sparingly
POS    = "FF15803D"  # green: savings / volume gain
NEG    = "FFB91C1C"  # red: spend up / gap to target

THIN  = Side(border_style="thin",  color="FFE5E7EB")  # hairline
RULE  = Side(border_style="thin",  color="FF9CA3AF")  # medium rule
BOLD_RULE = Side(border_style="medium", color="FF0F172A")  # heavy rule

NO_FILL = PatternFill(fill_type=None)


def f(size=10, bold=False, color=BODY, italic=False):
    """Helvetica-equivalent (Calibri) font shortcut."""
    return Font(name=FONT_FAMILY, size=size, bold=bold, color=color, italic=italic)


def al(h="left", v="center", wrap=False):
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap)


def b(top=None, bottom=None, left=None, right=None):
    return Border(top=top, bottom=bottom, left=left, right=right)


# ────────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────────
def write_excel(summary, waste_main, reinvest_main, needs_test, df, run_dir):
    """
    Build the .xlsx report and return the file path.
    `summary` must include the 'business' and 'model_accuracy' sub-dicts.
    `df` is the enriched recommendations DataFrame (post Stage 8 enrich).
    """
    wb = Workbook()
    # Default sheet — rename and use as Summary
    ws_sum  = wb.active
    ws_sum.title = "Summary"
    ws_prod = wb.create_sheet("By Product")
    ws_cut  = wb.create_sheet("Price Lifts")
    ws_inv  = wb.create_sheet("Price Drops")
    ws_test = wb.create_sheet("Needs Test")
    ws_data = wb.create_sheet("Data")

    # 1. Build the Data sheet first — everything else references it.
    _build_data_sheet(ws_data, df, waste_main, reinvest_main)

    # 2. Build the Summary using formulas that reference Data!
    _build_summary_sheet(ws_sum, summary, df)

    # 3. Per-product breakdown — also formula-driven
    _build_per_product_sheet(ws_prod, summary, df)

    # 4. Detail sheets — straightforward tables
    _build_detail_sheet(ws_cut,  waste_main,    sheet_type="cut")
    _build_detail_sheet(ws_inv,  reinvest_main, sheet_type="invest")
    _build_detail_sheet(ws_test, needs_test,    sheet_type="needs_test")

    # Hide the raw data sheet — power users can unhide it
    ws_data.sheet_state = "hidden"

    path = os.path.join(run_dir, "WASTE_REINVEST_REPORT.xlsx")
    wb.save(path)
    return path


# ────────────────────────────────────────────────────────────────────
# Sheet builders
# ────────────────────────────────────────────────────────────────────
def _build_data_sheet(ws, df, waste_main, reinvest_main):
    """
    Raw per-cell data + the recommended price (cut or invest) for the week.
    Formulas elsewhere SUMPRODUCT off these columns.

    Columns: cell_id | product | grammage | city | mrp |
             cur_disc% | cur_units_day | cur_price |
             aftercut_disc% | aftercut_units | aftercut_price   (cuts only applied)
             final_disc% | final_units | final_price             (cuts + reinvest)
             confidence | elasticity | category
    """
    headers = [
        "cell_id", "product", "grammage", "city", "mrp",
        "cur_disc_pct", "cur_units_day", "cur_price",
        "aftercut_disc_pct", "aftercut_units", "aftercut_price",
        "final_disc_pct", "final_units", "final_price",
        "confidence", "elasticity", "category",
    ]
    for j, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=j, value=h)
        c.font = f(10, bold=True, color=INK)
        c.alignment = al("center")
        c.border = b(bottom=RULE)

    # Map cell_id → plan price (cut)
    cut_map = {}
    if not waste_main.empty:
        for _, r in waste_main.iterrows():
            cid = r.get("cell_id")
            if pd.notna(cid):
                cut_map[cid] = {
                    "disc":  float(r.get("rec_discount_final", r["current_discount_pct"])),
                    "units": float(r.get("rec_units_day", r["current_units_day"]))
                                if "rec_units_day" in r and pd.notna(r.get("rec_units_day"))
                                else _predict(r, float(r.get("rec_discount_final",
                                                              r["current_discount_pct"]))),
                }
    inv_map = {}
    if not reinvest_main.empty:
        for _, r in reinvest_main.iterrows():
            cid = r.get("cell_id")
            if pd.notna(cid):
                rec_d = float(r.get("rec_discount_final",
                                      r.get("recommended_discount_pct",
                                            r["current_discount_pct"])))
                # ALWAYS re-predict for reinvest cells — `rec_units_day` in the
                # row was set by Stage 7's CUT logic (price-up), not the
                # reinvest action (price-down), so it's wrong here.
                inv_map[cid] = {"disc": rec_d, "units": _predict(r, rec_d)}

    rrow = 2
    for _, row in df.iterrows():
        cid = row.get("cell_id")
        mrp = float(row.get("mrp", 0))
        cur_d = float(row.get("current_discount_pct", 0))
        cur_u = float(row.get("current_units_day", 0))

        # AFTER CUTS only: apply cut to cells in cut list (waste_main is already
        # filtered to exclude reinvest cells, so 'in cut_map' implies 'not reinvest')
        if cid in cut_map:
            ac_d, ac_u = cut_map[cid]["disc"], cut_map[cid]["units"]
        else:
            ac_d, ac_u = cur_d, cur_u

        # FINAL (cuts + reinvest): reinvest takes precedence; then cuts; else current
        if cid in inv_map:
            fn_d, fn_u = inv_map[cid]["disc"], inv_map[cid]["units"]
        elif cid in cut_map:
            fn_d, fn_u = cut_map[cid]["disc"], cut_map[cid]["units"]
        else:
            fn_d, fn_u = cur_d, cur_u

        values = [
            cid, str(row.get("title", ""))[:50], row.get("grammage", ""), row.get("city", ""), mrp,
            cur_d, cur_u, mrp * (1 - cur_d / 100),
            ac_d, ac_u, mrp * (1 - ac_d / 100),
            fn_d, fn_u, mrp * (1 - fn_d / 100),
            row.get("confidence", ""),
            float(row.get("price_elasticity", row.get("elasticity", 0))),
            row.get("category", ""),
        ]
        for j, v in enumerate(values, 1):
            c = ws.cell(row=rrow, column=j, value=v)
            c.font = f(10)
            if j in (5, 8, 11, 14):
                c.number_format = "#,##0.00"
            elif j in (6, 9, 12):
                c.number_format = "0.00"
            elif j in (7, 10, 13):
                c.number_format = "#,##0.0"
            elif j == 16:
                c.number_format = "0.00"
        rrow += 1

    # Reasonable column widths
    widths = [22, 36, 8, 18, 9, 11, 13, 11, 11, 14, 11, 13, 16, 12, 14, 12, 14]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"


def _predict(row, new_d):
    """Predict units at new_d using the dual-signal model — fallback when
    rec_units_day isn't already in the row."""
    try:
        import numpy as np
        mrp   = float(row["mrp"])
        cur_d = float(row["current_discount_pct"])
        cur_u = float(row.get("current_units_day", 0))
        if cur_u <= 0 or mrp <= 0:
            return cur_u
        cur_p = mrp * (1 - cur_d / 100)
        new_p = mrp * (1 - new_d / 100)
        elast = float(row.get("price_elasticity", row.get("elasticity", -1.5)))
        badge = float(row.get("badge_sensitivity", row.get("discount_sensitivity", 0)))
        mult  = (new_p / cur_p) ** elast * np.exp(badge * (new_d - cur_d))
        return max(cur_u * mult, 0.01)
    except Exception:
        return float(row.get("current_units_day", 0))


def _build_summary_sheet(ws, summary, df):
    """
    The Portfolio Summary — every number except labels comes from a formula
    referencing the Data sheet.

    Layout (columns):
      A: label
      B: Today
      C: After cuts
      D: After cuts + invest
      E: notes / explanation
    """
    biz = summary.get("business", {})
    acc = summary.get("model_accuracy", {})
    target = _get_target_disc(summary)
    n_data_rows = len(df)
    last_row = 1 + n_data_rows  # Data sheet last row

    # Helpers to write formula cells
    def cell(r, c, val, font=None, align=None, fmt=None, border=None, fill=None):
        x = ws.cell(row=r, column=c, value=val)
        x.font = font or f(10)
        if align: x.alignment = align
        if fmt:   x.number_format = fmt
        if border:x.border = border
        if fill:  x.fill = fill
        return x

    # Title block
    cell(1, 1, "DISCOUNT OPTIMISATION  ·  WEEKLY REPORT  ·  " +
         pd.Timestamp.now().strftime("%d %B %Y"),
         font=f(9, color=MUTED), align=al("left"))
    cell(3, 1, "Portfolio Summary", font=f(20, bold=True, color=INK))
    cell(4, 1, "24 Mantra Organic on Blinkit", font=f(10, color=BODY))

    # ── Portfolio metrics table — formulas reference Data sheet ────────
    # Row 7 = header, rows 8-12 = metrics
    HEAD_ROW = 7
    R_GROSS  = 8
    R_SPEND  = 9
    R_NET    = 10
    R_UNITS  = 11
    R_DISC   = 12
    R_TARGET = 14

    headers = ["Metric", "Today", "After cuts", "After cuts + invest"]
    for j, h in enumerate(headers, 1):
        cell(HEAD_ROW, j, h,
             font=f(10, bold=True, color=INK),
             align=al("right" if j > 1 else "left"),
             border=b(top=BOLD_RULE, bottom=RULE))

    # Reference ranges on the Data sheet
    # Data layout: E=mrp, F=cur_disc%, G=cur_units, I=plan_disc%, J=plan_units, L=inv_disc%, M=inv_units
    rng_mrp        = f"Data!E2:E{last_row}"
    rng_cur_disc   = f"Data!F2:F{last_row}"
    rng_cur_units  = f"Data!G2:G{last_row}"
    rng_plan_disc  = f"Data!I2:I{last_row}"
    rng_plan_units = f"Data!J2:J{last_row}"
    rng_inv_disc   = f"Data!L2:L{last_row}"
    rng_inv_units  = f"Data!M2:M{last_row}"

    # Monthly multiplier baked in (30 days)
    K = 30  # days per month for sales × units → monthly

    # Gross sales (at MRP) = SUMPRODUCT(MRP × units × 30)
    cell(R_GROSS, 1, "Gross sales / month (at MRP)", font=f(10), align=al("left"))
    cell(R_GROSS, 2, f"=SUMPRODUCT({rng_mrp},{rng_cur_units})*{K}",
         fmt='"Rs."#,##0', align=al("right"))
    cell(R_GROSS, 3, f"=SUMPRODUCT({rng_mrp},{rng_cur_units})*{K}",
         fmt='"Rs."#,##0', align=al("right"))
    # ↑ Gross stays same in cuts scenario because cuts use plan_units which is same when nothing changes
    # Wait actually no — when cells are cut their units drop. So 'after cuts' gross = SUM(MRP × plan_units)
    cell(R_GROSS, 3, f"=SUMPRODUCT({rng_mrp},{rng_plan_units})*{K}",
         fmt='"Rs."#,##0', align=al("right"))
    cell(R_GROSS, 4, f"=SUMPRODUCT({rng_mrp},{rng_inv_units})*{K}",
         fmt='"Rs."#,##0', align=al("right"))

    # Discount spend = SUMPRODUCT(MRP × disc%/100 × units × 30)
    cell(R_SPEND, 1, "Discount spend / month", font=f(10), align=al("left"))
    cell(R_SPEND, 2, f"=SUMPRODUCT({rng_mrp},{rng_cur_disc},{rng_cur_units})/100*{K}",
         fmt='"Rs."#,##0', align=al("right"))
    cell(R_SPEND, 3, f"=SUMPRODUCT({rng_mrp},{rng_plan_disc},{rng_plan_units})/100*{K}",
         fmt='"Rs."#,##0', align=al("right"))
    cell(R_SPEND, 4, f"=SUMPRODUCT({rng_mrp},{rng_inv_disc},{rng_inv_units})/100*{K}",
         fmt='"Rs."#,##0', align=al("right"))

    # Net revenue = Gross − Discount spend  (formula references the cells above)
    cell(R_NET, 1, "Net revenue / month", font=f(10), align=al("left"))
    cell(R_NET, 2, f"=B{R_GROSS}-B{R_SPEND}", fmt='"Rs."#,##0', align=al("right"))
    cell(R_NET, 3, f"=C{R_GROSS}-C{R_SPEND}", fmt='"Rs."#,##0', align=al("right"))
    cell(R_NET, 4, f"=D{R_GROSS}-D{R_SPEND}", fmt='"Rs."#,##0', align=al("right"))

    # Units sold / month
    cell(R_UNITS, 1, "Units sold / month", font=f(10), align=al("left"))
    cell(R_UNITS, 2, f"=SUM({rng_cur_units})*{K}",  fmt="#,##0", align=al("right"))
    cell(R_UNITS, 3, f"=SUM({rng_plan_units})*{K}", fmt="#,##0", align=al("right"))
    cell(R_UNITS, 4, f"=SUM({rng_inv_units})*{K}",  fmt="#,##0", align=al("right"))

    # Weighted discount % = discount spend ÷ gross sales × 100   (the key business formula)
    cell(R_DISC, 1, "Weighted discount %",
         font=f(10, bold=True, color=INK), align=al("left"),
         border=b(top=RULE, bottom=BOLD_RULE))
    cell(R_DISC, 2, f"=B{R_SPEND}/B{R_GROSS}*100",
         fmt="0.00\"%\"", font=f(11, bold=True, color=INK), align=al("right"),
         border=b(top=RULE, bottom=BOLD_RULE))
    cell(R_DISC, 3, f"=C{R_SPEND}/C{R_GROSS}*100",
         fmt="0.00\"%\"", font=f(11, bold=True, color=INK), align=al("right"),
         border=b(top=RULE, bottom=BOLD_RULE))
    cell(R_DISC, 4, f"=D{R_SPEND}/D{R_GROSS}*100",
         fmt="0.00\"%\"", font=f(11, bold=True, color=INK), align=al("right"),
         border=b(top=RULE, bottom=BOLD_RULE))

    # Target + gap line
    cell(R_TARGET, 1, "Target weighted discount %",
         font=f(10, italic=True, color=MUTED), align=al("left"))
    cell(R_TARGET, 2, target, fmt="0.00\"%\"",
         font=f(10, italic=True, color=MUTED), align=al("right"))
    cell(R_TARGET + 1, 1, "Gap to target (today)",
         font=f(10, italic=True, color=MUTED), align=al("left"))
    cell(R_TARGET + 1, 2, f"=B{R_DISC}-B{R_TARGET}",
         fmt="+0.00\" ppt\";-0.00\" ppt\"",
         font=f(10, italic=True, bold=True, color=NEG), align=al("right"))
    cell(R_TARGET + 2, 1, "Gap to target (after this-week plan)",
         font=f(10, italic=True, color=MUTED), align=al("left"))
    cell(R_TARGET + 2, 2, f"=D{R_DISC}-B{R_TARGET}",
         fmt="+0.00\" ppt\";-0.00\" ppt\"",
         font=f(10, italic=True, color=MUTED), align=al("right"))

    # ── This Week's Plan table ────────────────────────────────────────
    P_TITLE = 19
    P_HEAD  = 21
    P_CUT   = 22
    P_INV   = 23
    P_NET   = 24

    # Counts already pre-computed in summary (waste_main is filtered to exclude reinvest cells)
    n_waste = int(biz.get("n_waste_cells", 0))
    n_reinv = int(biz.get("n_reinvest_cells", 0))

    cell(P_TITLE, 1, "This week's plan",
         font=f(14, bold=True, color=INK), align=al("left"))

    for j, h in enumerate(["Action", "Cells", "Discount spend Δ", "Units Δ / month"], 1):
        cell(P_HEAD, j, h,
             font=f(10, bold=True, color=INK),
             align=al("right" if j > 1 else "left"),
             border=b(top=BOLD_RULE, bottom=RULE))

    # Cut row: spend change = today_spend - after_cuts_spend
    cell(P_CUT, 1, "Cut (raise price)",  font=f(10), align=al("left"))
    cell(P_CUT, 2, n_waste, fmt="0", align=al("right"))
    cell(P_CUT, 3, f"=-(B{R_SPEND}-C{R_SPEND})", fmt='"Rs."#,##0;[Red]"Rs."-#,##0',
         font=f(10, color=POS), align=al("right"))
    cell(P_CUT, 4, f"=C{R_UNITS}-B{R_UNITS}", fmt="+#,##0;-#,##0",
         font=f(10, color=NEG), align=al("right"))

    # Reinvest row: spend change = after_cuts+invest_spend - after_cuts_spend
    cell(P_INV, 1, "Reinvest (drop price)", font=f(10), align=al("left"))
    cell(P_INV, 2, n_reinv, fmt="0", align=al("right"))
    cell(P_INV, 3, f"=D{R_SPEND}-C{R_SPEND}", fmt='"Rs."+#,##0;[Red]"Rs."-#,##0',
         font=f(10, color=NEG), align=al("right"))
    cell(P_INV, 4, f"=D{R_UNITS}-C{R_UNITS}", fmt="+#,##0;-#,##0",
         font=f(10, color=POS), align=al("right"))

    # Net row
    cell(P_NET, 1, "Net change", font=f(10, bold=True, color=INK), align=al("left"),
         border=b(top=RULE, bottom=BOLD_RULE))
    cell(P_NET, 2, f"=B{P_CUT}+B{P_INV}", fmt="0", align=al("right"),
         font=f(10, bold=True), border=b(top=RULE, bottom=BOLD_RULE))
    cell(P_NET, 3, f"=C{P_CUT}+C{P_INV}", fmt='"Rs."+#,##0;[Red]"Rs."-#,##0',
         font=f(10, bold=True, color=POS), align=al("right"),
         border=b(top=RULE, bottom=BOLD_RULE))
    cell(P_NET, 4, f"=D{P_CUT}+D{P_INV}", fmt="+#,##0;-#,##0",
         font=f(10, bold=True, color=NEG), align=al("right"),
         border=b(top=RULE, bottom=BOLD_RULE))

    # ── Model Accuracy table ──────────────────────────────────────────
    M_TITLE = 27
    M_HEAD  = 29
    M_R2    = 30
    M_MAPE  = 31
    M_TRAIN = 32
    M_TIER  = 34

    cell(M_TITLE, 1, "Model accuracy",
         font=f(14, bold=True, color=INK), align=al("left"))

    for j, h in enumerate(["Metric", "Value", "What it means"], 1):
        cell(M_HEAD, j, h,
             font=f(10, bold=True, color=INK),
             align=al("right" if j == 2 else "left"),
             border=b(top=BOLD_RULE, bottom=RULE))

    if acc.get("available"):
        cell(M_R2,    1, "Out-of-sample R² (test set)", font=f(10), align=al("left"))
        cell(M_R2,    2, acc["test_r2_log"], fmt="0.00", align=al("right"))
        cell(M_R2,    3, "Fraction of week-to-week variation in unit sales the model "
                          "explains on held-out data. 1.0 = perfect, 0 = useless.",
             font=f(9, color=MUTED), align=al("left", wrap=True))

        cell(M_MAPE,  1, "Avg error at discount-bin grain", font=f(10), align=al("left"))
        cell(M_MAPE,  2, acc["test_mape_agg"], fmt='0.0"%"', align=al("right"))
        cell(M_MAPE,  3, "Average % error when comparing predicted vs actual mean units "
                          "in each 3-ppt discount band. This is the grain the saturation "
                          "curve uses.",
             font=f(9, color=MUTED), align=al("left", wrap=True))

        cell(M_TRAIN, 1, "Training-data fit (in-distribution R²)", font=f(10), align=al("left"))
        cell(M_TRAIN, 2, acc["train_r2_log"], fmt="0.00", align=al("right"))
        cell(M_TRAIN, 3, "How well the model fits the data it was trained on. High value "
                          "means the price/quantity relationship is well captured.",
             font=f(9, color=MUTED), align=al("left", wrap=True))

        # Set tall row heights so wrapped text breathes
        for r in (M_R2, M_MAPE, M_TRAIN):
            ws.row_dimensions[r].height = 32

        # Overall tier — implemented as a nested IF formula so the bar is editable.
        cell(M_TIER, 1, "Overall tier",
             font=f(10, bold=True, color=INK), align=al("left"),
             border=b(top=RULE))
        # Tier formula: based on B30 (test R^2) and B31 (MAPE)
        tier_formula = (
            f'=IF(AND(B{M_R2}>=0.7,B{M_MAPE}<=25),"Strong",'
            f'IF(AND(B{M_R2}>=0.4,B{M_MAPE}<=50),"Moderate",'
            f'IF(AND(B{M_R2}>=0.1,B{M_MAPE}<=80),"Weak","Unreliable")))'
        )
        cell(M_TIER, 2, tier_formula,
             font=f(11, bold=True, color=ACCENT), align=al("right"),
             border=b(top=RULE))
        # Tier criteria reminder
        cell(M_TIER + 1, 1,
             "Strong: R²≥0.70 AND MAPE≤25%  |  Moderate: R²≥0.40 AND MAPE≤50%  |  "
             "Weak: R²≥0.10 AND MAPE≤80%  |  else Unreliable",
             font=f(8, italic=True, color=MUTED), align=al("left"))
        ws.merge_cells(start_row=M_TIER + 1, start_column=1,
                       end_row=M_TIER + 1, end_column=4)

        cell(M_TIER + 3, 1,
             f"Trained on {acc['n_train']:,} regular-day rows, validated on "
             f"{acc['n_test']:,} held-out future rows. Daily-level predictions are "
             f"inherently noisy for CPG SKU × city data; recommendations are most "
             f"reliable on the High-confidence cells.",
             font=f(9, italic=True, color=MUTED), align=al("left", wrap=True))
        ws.merge_cells(start_row=M_TIER + 3, start_column=1,
                       end_row=M_TIER + 3, end_column=5)
        ws.row_dimensions[M_TIER + 3].height = 32

    # ── Footer note ───────────────────────────────────────────────────
    F = M_TIER + 6
    cell(F, 1,
         "Discount % is computed as total monthly discount spend ÷ total monthly "
         "gross sales at MRP. Numbers in the Summary table are live formulas — "
         "edit the hidden Data sheet (Format ▸ Sheet ▸ Unhide) to run what-if scenarios.",
         font=f(8, color=MUTED), align=al("left", wrap=True))
    ws.merge_cells(start_row=F, start_column=1, end_row=F, end_column=5)
    ws.row_dimensions[F].height = 28

    # Column widths
    widths = [44, 22, 22, 24, 60]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _build_per_product_sheet(ws, summary, df):
    """Per-product version of the portfolio table — same 5 metrics."""
    biz = summary.get("business", {})
    target = _get_target_disc(summary)
    last_row = 1 + len(df)

    def cell(r, c, val, font=None, align=None, fmt=None, border=None):
        x = ws.cell(row=r, column=c, value=val)
        x.font = font or f(10)
        if align: x.alignment = align
        if fmt: x.number_format = fmt
        if border: x.border = border
        return x

    cell(1, 1, "DISCOUNT OPTIMISATION  ·  PER-PRODUCT VIEW",
         font=f(9, color=MUTED), align=al("left"))
    cell(3, 1, "By product", font=f(20, bold=True, color=INK))
    cell(4, 1, "Same metrics as the portfolio summary, broken out by SKU. "
               "The discount % row tells you which products are furthest from the target "
               "and how this week's plan affects each.",
         font=f(10, color=BODY), align=al("left", wrap=True))
    ws.row_dimensions[4].height = 26
    ws.merge_cells(start_row=4, start_column=1, end_row=4, end_column=5)

    # Build per-product blocks. Each block = title + 5 metric rows.
    # We use SUMPRODUCT with a (product==X) condition via boolean array trick.
    # Older Excel doesn't have IFS or LET but supports SUMPRODUCT with (range=val) booleans.

    # Build (product key, display title) list from summary
    per_prod = biz.get("per_product", {})
    if not per_prod:
        return

    # Map product key (e.g. "3583 | 500g") → list of unique products in df
    # We'll match on Data!B (product) AND Data!C (grammage) when available
    cur_row = 7
    for pkey, pdata in per_prod.items():
        # display title — pdata["title"] already includes "(grammage)" suffix
        # from _compute_business_metrics, so don't re-append.
        display_title = pdata.get("title", pkey)

        # Parse pkey "ID | grammage" or just "ID" — we'll match by cell_id
        # PREFIX on the Data sheet (more robust than title matching, since
        # cell_id = "{pid}_{grammage}_{city}").
        if " | " in pkey:
            pid, grm = pkey.split(" | ", 1)
            prefix = f"{pid}_{grm}_"
        else:
            pid, grm = pkey, ""
            prefix = f"{pid}_"
        plen = len(prefix)
        cond = f'(LEFT(Data!$A$2:$A${last_row},{plen})="{prefix}")'

        cell(cur_row, 1, display_title,
             font=f(11, bold=True, color=INK), align=al("left"),
             border=b(bottom=THIN))
        ws.merge_cells(start_row=cur_row, start_column=1,
                       end_row=cur_row, end_column=4)
        cur_row += 1

        # Header
        for j, h in enumerate(["Metric", "Today", "After cuts", "After cuts + invest"], 1):
            cell(cur_row, j, h,
                 font=f(10, bold=True, color=INK),
                 align=al("right" if j > 1 else "left"),
                 border=b(top=RULE, bottom=RULE))
        cur_row += 1

        R_GROSS = cur_row
        R_SPEND = cur_row + 1
        R_UNITS = cur_row + 2
        R_DISC  = cur_row + 3

        K = 30
        rng_mrp        = f"Data!$E$2:$E${last_row}"
        rng_cur_disc   = f"Data!$F$2:$F${last_row}"
        rng_cur_units  = f"Data!$G$2:$G${last_row}"
        rng_plan_disc  = f"Data!$I$2:$I${last_row}"
        rng_plan_units = f"Data!$J$2:$J${last_row}"
        rng_inv_disc   = f"Data!$L$2:$L${last_row}"
        rng_inv_units  = f"Data!$M$2:$M${last_row}"

        cell(R_GROSS, 1, "Gross sales (MRP) / mo", font=f(10), align=al("left"))
        cell(R_GROSS, 2, f"=SUMPRODUCT({cond}*{rng_mrp}*{rng_cur_units})*{K}",
             fmt='"Rs."#,##0', align=al("right"))
        cell(R_GROSS, 3, f"=SUMPRODUCT({cond}*{rng_mrp}*{rng_plan_units})*{K}",
             fmt='"Rs."#,##0', align=al("right"))
        cell(R_GROSS, 4, f"=SUMPRODUCT({cond}*{rng_mrp}*{rng_inv_units})*{K}",
             fmt='"Rs."#,##0', align=al("right"))

        cell(R_SPEND, 1, "Discount spend / mo", font=f(10), align=al("left"))
        cell(R_SPEND, 2, f"=SUMPRODUCT({cond}*{rng_mrp}*{rng_cur_disc}*{rng_cur_units})/100*{K}",
             fmt='"Rs."#,##0', align=al("right"))
        cell(R_SPEND, 3, f"=SUMPRODUCT({cond}*{rng_mrp}*{rng_plan_disc}*{rng_plan_units})/100*{K}",
             fmt='"Rs."#,##0', align=al("right"))
        cell(R_SPEND, 4, f"=SUMPRODUCT({cond}*{rng_mrp}*{rng_inv_disc}*{rng_inv_units})/100*{K}",
             fmt='"Rs."#,##0', align=al("right"))

        cell(R_UNITS, 1, "Units / mo", font=f(10), align=al("left"))
        cell(R_UNITS, 2, f"=SUMPRODUCT({cond}*{rng_cur_units})*{K}",
             fmt="#,##0", align=al("right"))
        cell(R_UNITS, 3, f"=SUMPRODUCT({cond}*{rng_plan_units})*{K}",
             fmt="#,##0", align=al("right"))
        cell(R_UNITS, 4, f"=SUMPRODUCT({cond}*{rng_inv_units})*{K}",
             fmt="#,##0", align=al("right"))

        cell(R_DISC, 1, "Weighted discount %",
             font=f(10, bold=True, color=INK), align=al("left"),
             border=b(top=RULE, bottom=BOLD_RULE))
        cell(R_DISC, 2, f'=IF(B{R_GROSS}=0,0,B{R_SPEND}/B{R_GROSS}*100)',
             fmt="0.00\"%\"", font=f(11, bold=True, color=INK), align=al("right"),
             border=b(top=RULE, bottom=BOLD_RULE))
        cell(R_DISC, 3, f'=IF(C{R_GROSS}=0,0,C{R_SPEND}/C{R_GROSS}*100)',
             fmt="0.00\"%\"", font=f(11, bold=True, color=INK), align=al("right"),
             border=b(top=RULE, bottom=BOLD_RULE))
        cell(R_DISC, 4, f'=IF(D{R_GROSS}=0,0,D{R_SPEND}/D{R_GROSS}*100)',
             fmt="0.00\"%\"", font=f(11, bold=True, color=INK), align=al("right"),
             border=b(top=RULE, bottom=BOLD_RULE))

        cur_row = R_DISC + 3  # spacing before next product

    widths = [40, 20, 20, 22, 60]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _build_detail_sheet(ws, df, sheet_type):
    """
    Per-cell detail tables. These are values (not formulas) — they're a
    snapshot of which cells got which recommendation, sorted for action.
    """
    def cell(r, c, val, font=None, align=None, fmt=None, border=None):
        x = ws.cell(row=r, column=c, value=val)
        x.font = font or f(10)
        if align: x.alignment = align
        if fmt:   x.number_format = fmt
        if border:x.border = border
        return x

    if sheet_type == "cut":
        title = "Where to raise prices this week"
        eyebrow = "DISCOUNT OPTIMISATION  ·  PRICE LIFTS"
        desc = ("Cells sorted by Rs. wasted per month. "
                "Now is the current selling price. "
                "This Week is what to set on Blinkit this Monday — capped at a 3 ppt move "
                "per cycle. Wasted/mo is the full multi-cycle savings opportunity if you "
                "walked the price all the way back to MRP.")
        cols = [
            ("title",            "Product",         40),
            ("city",             "City",            20),
            ("mrp",              "MRP",             10),
            ("current_price",    "Now",             12),
            ("this_week_price",  "This Week",       14),
            ("wasted_inr_per_month", "Wasted/mo",   16),
            ("confidence",       "Conf",            12),
        ]
        money_cols = {"mrp", "current_price", "this_week_price", "wasted_inr_per_month"}
        pct_cols   = set()
    elif sheet_type == "invest":
        title = "Where to drop prices to grow volume"
        eyebrow = "DISCOUNT OPTIMISATION  ·  STRATEGIC INVESTMENTS"
        desc = ("Cells where dropping the price by 3 ppt is projected to add enough volume "
                "to be worth the extra discount spend. These are funded by the savings from "
                "the price lifts above.")
        cols = [
            ("title",                       "Product",       40),
            ("city",                        "City",          18),
            ("mrp",                         "MRP",           10),
            ("current_price",               "Now",           11),
            ("new_price",                   "New",           11),
            ("volume_lift_pct",             "Vol Δ",         11),
            ("extra_volume_units_per_month","+Units/mo",     14),
            ("budget_needed_inr_per_month", "Budget/mo",     14),
            ("confidence",                  "Conf",          12),
        ]
        money_cols = {"mrp", "current_price", "new_price",
                      "budget_needed_inr_per_month", "extra_volume_units_per_month"}
        pct_cols   = {"volume_lift_pct"}
    else:  # needs_test
        title = "Cells needing a price test"
        eyebrow = "DISCOUNT OPTIMISATION  ·  PILOT REQUIRED"
        desc = ("These cells don't have enough clean data to act on. The model isn't "
                "confident enough — usually because of too few observations, too little "
                "price variation, or a launch ramp confounding the elasticity signal. "
                "Run a small A/B test in one city before changing anything.")
        cols = [
            ("title",                "Product",     60),
            ("city",                 "City",        24),
            ("current_discount_pct", "Now %",       12),
            ("elbow_discount_pct",   "Model %",     12),
            ("confidence",           "Status",      20),
        ]
        money_cols = set()
        pct_cols   = {"current_discount_pct", "elbow_discount_pct"}

    cell(1, 1, eyebrow, font=f(9, color=MUTED), align=al("left"))
    cell(3, 1, title, font=f(20, bold=True, color=INK))
    cell(4, 1, desc, font=f(10, color=BODY), align=al("left", wrap=True))
    ws.merge_cells(start_row=4, start_column=1, end_row=4, end_column=len(cols))
    ws.row_dimensions[4].height = 32

    # Confidence legend
    cell(6, 1,
         "Confidence: High = 200+ days of clean history with 10+ distinct discount "
         "levels and ≥3 ppt of price variation; the model trusts this cell. "
         "Medium = 100+ days and 5+ discount levels — actionable but worth a review. "
         "Low = thin data; shown separately as 'Needs Price Test'. "
         "Cells with a data-quality concern flagged (boundary-hit elasticity or rapid "
         "demand growth) are automatically downgraded one tier.",
         font=f(8, italic=True, color=MUTED), align=al("left", wrap=True))
    ws.merge_cells(start_row=6, start_column=1, end_row=6, end_column=len(cols))
    ws.row_dimensions[6].height = 40

    # Header row
    H = 8
    for j, (_, label, _) in enumerate(cols, 1):
        cell(H, j, label,
             font=f(10, bold=True, color=INK),
             align=al("right" if j > 2 else "left"),
             border=b(top=BOLD_RULE, bottom=RULE))

    # Data rows
    if df is None or df.empty:
        cell(H + 1, 1, "(no cells)", font=f(10, italic=True, color=MUTED), align=al("left"))
    else:
        r = H + 1
        for _, row in df.head(80).iterrows():
            for j, (col, _, _) in enumerate(cols, 1):
                v = row.get(col, "")
                if pd.isna(v):
                    cell(r, j, "", border=b(bottom=THIN))
                    continue
                if col == "title":
                    cell(r, j, str(v)[:60], font=f(10),
                         align=al("left", wrap=True), border=b(bottom=THIN))
                elif col in money_cols:
                    cell(r, j, float(v), font=f(10), align=al("right"),
                         fmt="#,##0", border=b(bottom=THIN))
                elif col in pct_cols:
                    cell(r, j, float(v), font=f(10), align=al("right"),
                         fmt='+0.0"%";-0.0"%"' if col == "volume_lift_pct" else '0.0"%"',
                         border=b(bottom=THIN))
                elif isinstance(v, (int, float)):
                    cell(r, j, float(v), font=f(10), align=al("right"),
                         fmt="0.0", border=b(bottom=THIN))
                else:
                    cell(r, j, str(v), font=f(10),
                         align=al("center" if col == "confidence" else "left"),
                         border=b(bottom=THIN))
            r += 1

    # Column widths
    for j, (_, _, w) in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.freeze_panes = f"A{H + 1}"


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────
def _get_target_disc(summary):
    """Fetch target_weighted_discount_pct from config, defaulting to 9.0."""
    try:
        import v4_config as cfg
        return float(cfg.TARGET_WEIGHTED_DISCOUNT_PCT)
    except Exception:
        return 9.0


def _filter_actual_cuts(df, waste_main, reinvest_main):
    """Count cells that are actually cut (in waste but NOT in reinvest)."""
    if waste_main is None or waste_main.empty:
        return pd.DataFrame()
    if reinvest_main is None or reinvest_main.empty:
        return waste_main
    return waste_main[~waste_main["cell_id"].isin(reinvest_main["cell_id"])]
